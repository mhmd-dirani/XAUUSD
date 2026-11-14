"""
The auto-trader loop.

source "signal_bot" (default): every 2 s read the signal bot's confirmed signals and latest analysis
(runtime/signals.jsonl, runtime/astra_analysis.json) and turn them into orders. No Claude call.
source "own_analysis": the trader runs its own Claude analysis, as described below.

Every 2 s:   check the clock.
Every 20 s:  manage orders/positions (expiry, cancel levels, fills, TP1, breakeven).
Every 60 s:  refresh the MT5 calendar (GoldCalendarBridge export).
On 15M closes: analyse when it is the scheduled time (hourly by default) or when price has reached
              the zone of a setup Claude is watching. Analysis is skipped (no API cost) whenever no
              order could be placed: market closed, open/close buffer, news lock, stale feed,
              position already open, limits hit, kill file.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

import gold_monitor as GM
import gold_evidence as EV

from . import alerts
from . import bridge as BR
from . import breakout as BK
from . import replay as RP
from . import review as RV
from . import tiers as TR
from . import playbooks as PB
from . import regime as RG
from . import sessions as SS
from .analyst import Analyst, build_payload
from .executor import Executor
from .lab import Lab
from .straddle import Straddle
from .overnight import Overnight
from .weakness import WeaknessLab
from .ltf import LTF
from .h1trend import H1Trend
from .newsguard import NewsGuard
from .protect import Protect
from .conflict import Conflicts
from . import news as NW
from . import flow as FL
from .scalp import ScalpLab
from .pyideas import PyIdeas
from .market import Market, MarketError
from .state import State
from .validator import Rejected, Validator

UTC = timezone.utc
LOG = logging.getLogger("autotrader")


class App:
    def __init__(self, c, dry_run=False):
        self.c = c
        alerts.ENABLED = c["telegram_errors"]
        alerts.TRADES = c["telegram_trades"]
        self.market = Market(c)
        self.state = State(c["state_path"])
        self.validator = Validator(c, self.market, self.state)
        self.executor = Executor(c, self.market, self.state, dry_run=dry_run)
        self.lab = Lab(c, self)                                   # 1.10: the rule-testing track (demo only)
        self.sweep_logged: set = set()
        self.straddle = Straddle(c, self)                         # 1.10.3: lab news straddle (demo only)
        self.overnight = Overnight(c, self)                       # 1.11: Python-only overnight trend engine
        self.weakness = WeaknessLab(c, self)                      # 1.12: SELL_4H_WEAKNESS forward test (lab only)
        self.ltf = LTF(c, self)                                   # 1.13: 5M-entry + liquidity sample of each lab idea
        self.h1 = H1Trend(c, self)                                # 1.13.1: 1H-trend zone retests, entered by the LTF engine
        self.newsguard = NewsGuard(c, self)                       # 1.14: cancel pending / breakeven before major releases
        self.protect = Protect(c, self)                           # 1.15: breakeven / lock / near-TP exit on open trades
        self.conflicts = Conflicts(c, self)                       # 1.16: newer opposite live trade -> manage the older one
        self.scalp = ScalpLab(c, self)                            # 11 Nov: the 5M pullback scalp, lab forward test
        self.analyst = Analyst(c)
        self.dry_run = dry_run
        self.api_failures = 0
        self.feed_failures = 0
        self.last_bar = None
        self.zone_fired: dict[str, float] = {}
        self.bridge = BR.Bridge(c, self.state)
        self.pyideas = PyIdeas(c, self)                           # 1.20: Python's own ideas (no AI), see pyideas.py
        self.bridge.python = self.pyideas
        self.bridge_gate_logged = None
        self.idea_logged: set = set()
        self.idea_pause: dict = {}        # 1.19: (analysis, idea) -> epoch: not re-evaluated before then
        self._paused_ids: dict = {}       # 1.19: setup_id -> epoch, filled by place_orders (holds, stale quote)
        self._ctx = None
        self._zones = None
        self.quiet_warned = 0.0
        self.rcfg = RP.cfg_from_settings(c)
        self.session_bar_done = None

    # ------------------------------------------------------------------ deciding when to analyse
    def trigger_for(self, close: datetime, quote: dict) -> str | None:
        minutes = close.hour * 60 + close.minute
        if minutes % self.c["analysis_every_minutes"] == 0:
            return "scheduled"
        if not self.c["zone_trigger"]:
            return None
        last = self.state.get("last_map") or {}
        atr = self.state.get("last_atr15") or 0.0
        pad = self.c["zone_trigger_atr"] * atr
        for s in last.get("setups") or []:
            if s.get("status") not in ("WATCHING", "ARMED"):
                continue
            lo, hi = s.get("watch_zone_low"), s.get("watch_zone_high")
            if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)):
                continue
            lo, hi = min(lo, hi) - pad, max(lo, hi) + pad
            sid = str(s.get("setup_id"))
            if (lo <= quote["bid"] <= hi or lo <= quote["ask"] <= hi) and \
                    time.time() - self.zone_fired.get(sid, 0) > self.c["zone_trigger_cooldown_minutes"] * 60:
                self.zone_fired[sid] = time.time()
                return f"zone {sid}"
        return None

    # ------------------------------------------------------------------ one analysis
    def cycle(self, trigger: str) -> dict | None:
        now = datetime.now(UTC)
        gate = self.validator.gates(now)
        if gate:
            LOG.info("No analysis (%s): %s", trigger, gate)
            return None
        history = self.market.history()
        evidence = self.market.evidence(history, now)
        atr15 = self.market.atr15(history)
        quote = self.market.quote()
        constraints = {
            "executable_grades": self.c["execute_grades"], "min_rr": self.c["min_rr"],
            "stop_distance_atr15": [self.c["sl_atr_min"], self.c["sl_atr_max"]], "atr15": round(atr15, 2),
            "max_spread": self.c["v6"]["broker_max_spread"],
            "min_stop_for_current_spread": round(max(
                quote["spread"] / self.c["v6"]["broker_max_spread_stop_fraction"],
                (quote["spread"] + self.c["v6"]["broker_slippage_allowance"]) / self.c["v6"]["broker_max_combined_stop_fraction"],
                self.c["sl_atr_min"] * atr15), 2),
            "order_lifetime": [f"{self.c['min_order_lifetime_minutes']} min", f"{self.c['max_order_lifetime_hours']} h"],
            "news_lock_minutes": [self.c["v6"]["news_before_minutes"], self.c["v6"]["news_after_minutes"]],
            "evidence_required": self.c["require_evidence"],
            "mtf_opposition_blocks": self.c["block_on_mtf_opposition"],
            "max_new_orders_now": max(0, min(self.c["max_orders_per_analysis"],
                                             self.validator.max_active() - len(self.state.active_orders(include_lab=False)))),
            "working_orders": [{"setup_id": o["setup_id"], "direction": o["direction"], "type": o["order_type"],
                                "status": o["status"], "entry": o["entry"], "stop": o["sl"], "tp1": o["tp1"]}
                               for o in self.state.active_orders(include_lab=False)],
        }
        payload = build_payload(self.c, now, history, evidence, quote, self.market.upcoming_events(now),
                                self.market.playbook(), self.state.get("last_map"), constraints)
        try:
            result = self.analyst.analyse(payload)
            self.api_failures = 0
        except Exception as exc:  # noqa: BLE001
            self.api_failures += 1
            alerts.error(f"Claude API failure #{self.api_failures}: {GM.safe_error(exc)[:300]}")
            if self.api_failures >= self.c["max_consecutive_api_failures"]:
                alerts.error("Repeated Claude failures: cancelling pending orders; analysis resumes at the next scheduled time.")
                self.executor.cancel_all_pending("analyst unavailable")
            return None

        usage = self.analyst.last_usage
        self.state.put("last_map", {"setups": result.get("setups"), "bias": result.get("bias"),
                                    "market_map": result.get("market_map"), "decision": result.get("decision"),
                                    "at": GM.iso(now)})
        self.state.put("last_atr15", atr15)
        decision = result.get("decision")
        LOG.info("Decision %s (%s) | news %s | tokens in/out %s/%s | %s", decision, trigger, result.get("news_risk"),
                 usage.get("input_tokens"), usage.get("output_tokens"), str(result.get("reasoning_summary", ""))[:300])
        for s in result.get("setups") or []:
            LOG.info("  setup %s %s %s zone %s-%s", s.get("setup_id"), s.get("direction"), s.get("status"),
                     s.get("watch_zone_low"), s.get("watch_zone_high"))
        if decision not in ("A_TRADE", "B_TRADE"):
            self.state.log_analysis(result, trigger, False, decision, usage)
            return result

        outcomes = self.place_orders(result["orders"][: self.c["max_orders_per_analysis"]], evidence, atr15)
        sent = any(": sent #" in o or ": dry run" in o for o in outcomes)
        self.state.log_analysis(result, trigger, sent, " | ".join(outcomes), usage)
        return result

    def place_orders(self, proposals, evidence, atr15, require_evidence=None, on_rejected=None) -> list:
        """Validate and send each proposal in turn (gates re-checked before every order).
        Returns one outcome line per proposal handled; stops at the first gate block."""
        outcomes = []
        for proposal in proposals:
            grade = proposal["grade"]
            now = datetime.now(UTC)
            gate = self.validator.gates(now)
            if gate:
                LOG.info("%s %s blocked: %s", grade, proposal.get("setup_id"), gate)
                outcomes.append(f"{proposal.get('setup_id')}: gate: {gate}")
                break
            try:
                order = self.validator.validate(proposal, grade, evidence, atr15, now, require_evidence,
                                                counter_trend=proposal.get("_counter", False))
                lots, risk_money = self.validator.lots(order, grade, risk_pct=proposal.get("risk_pct"),
                                                       min_lot_ok=bool(proposal.get("min_lot_ok")))
            except Rejected as r:
                if str(r).startswith("broker quote") and on_rejected:   # 1.19: a stale quote is a moment, not a verdict
                    sid = proposal.get("setup_id")
                    self._paused_ids[sid] = time.time() + 30
                    k = ("stalequote", sid, int(time.time() // 600))
                    if k not in self.idea_logged:
                        LOG.info("%s %s waits: %s (retried in 30 s; the idea is kept)", grade, sid, r)
                        self.idea_logged.add(k)
                    outcomes.append(f"{sid}: {r} (retry)")
                    continue
                LOG.warning("%s %s REJECTED by risk engine: %s | %s", grade, proposal.get("setup_id"), r,
                            json.dumps(proposal)[:400])
                outcomes.append(f"{proposal.get('setup_id')}: {r}")
                if on_rejected:
                    on_rejected(proposal)
                continue
            try:
                ticket = self.executor.send(order, lots, risk_money, grade)
                outcomes.append(f"{order['setup_id']}: {'dry run' if self.dry_run else 'sent #' + str(ticket)}")
                LOG.info("ORDER %s ticket=%s %s %s %s @ %.2f R:R %.2f risk %.2f", "SIMULATED" if self.dry_run else "SENT",
                         ticket, grade, order["setup_id"], order["order_type"], order["entry_price"], order["rr"],
                         risk_money)
            except NW.NewsHold as h:                                   # 1.17: retried while the idea is fresh
                # 1.19: once per closed 15M candle (the strong-candle test cannot change sooner). 11 Nov: a held
                # limit was re-validated every 2 s for 75 minutes (2,886 times).
                self._paused_ids[order["setup_id"]] = (int(time.time()) // 900 + 1) * 900 + \
                    float(self.c.get("close_delay_seconds", 5) or 5)
                k = ("newshold", order["setup_id"], str(h)[:40])
                if k not in self.idea_logged:
                    LOG.info("%s %s held: %s", grade, order["setup_id"], h)
                    self.idea_logged.add(k)
                outcomes.append(f"{order['setup_id']}: {h}")
                continue
            except Exception as exc:  # noqa: BLE001
                alerts.error(f"Order send failed: {exc} | {json.dumps(order)[:300]}")
                outcomes.append(f"{order['setup_id']}: broker: {exc}")
            if on_rejected:
                on_rejected(proposal)          # handled (sent or broker error): not retried
        return outcomes

    # ------------------------------------------------------------------ signal-bot source (no Claude call)
    def market_context(self):
        """Python evidence + ATR15 (+ v1.6 market state and session levels), rebuilt only when a new
        15M candle has closed."""
        bar = self.market.last_closed_15m_open()
        if self._ctx is None or self._ctx[0] != bar:
            history = self.market.history()
            now = datetime.now(UTC)
            evidence = self.market.evidence(history, now)
            # the whole history window, so the 4H/1H trends equal the evidence the veto uses
            state = RG.market_state(history, now, dict(self.c["regime"], regime_window=1000))
            snap = SS.snapshot(history.get("15min") or [], history.get("1h") or [], now,
                               self.c["sessions"], self.c["opening_range_minutes"])
            self._ctx = (bar, evidence, self.market.atr15(history), history.get("15min") or [], state, snap)
            LOG.info("Market state %s | session %s | %d session levels%s", RG.summary(state), snap["session"],
                     len(snap["levels"]), "".join(f" | {e['event']} {e['level']} {e['price']}" for e in snap["events"]))
        return self._ctx[1], self._ctx[2]

    def market_state(self):
        """(market state, session snapshot) of the last closed 15M candle."""
        evidence, _ = self.market_context()
        if self._ctx is not None and len(self._ctx) == 6:
            return self._ctx[4], self._ctx[5]
        # market_context() replaced (tests): structure trends from the evidence, nothing else known
        return RG.from_evidence(evidence), {"session": SS.session_of(datetime.now(UTC), self.c["sessions"]),
                                            "levels": [], "events": []}

    def modes(self) -> dict:
        m = dict(self.c["playbook_modes"])
        if not self.c["counter_trend_enabled"]:
            m["COUNTER_TREND"] = "off"
        return m

    def meta(self, direction: str, dec: dict | None, how: str | None, idea: dict | None = None,
             playbook: str | None = None) -> dict:
        state, snap = self.market_state()
        return {"playbook": playbook or (dec or {}).get("trade_class"), "confirmation": how,
                "session": snap["session"], "regime": RG.public(state),
                "astra_class": (idea or {}).get("trade_class"),
                "targets": list((idea or {}).get("targets") or [])[:3],   # TP3 for the trading channel (1.7.2)
                "expansion_block": bool((dec or {}).get("would_block"))}

    def session_confirmation(self, idea: dict, since: datetime):
        """A session / opening-range / previous day-week level swept (or a failed breakout) in the
        idea's direction, at the idea's zone, on a 15M candle closed after `since`."""
        state, snap = self.market_state()
        bars = self._ctx[3] if self._ctx else self.recent_bars()     # whole history: level state from its start
        n = sum(1 for b in bars if BR._bar_time(b) > since)
        if not n:
            return None
        width = max(idea["entry_high"] - idea["entry_low"], 1.0)
        for e in SS.events(bars, snap["levels"], datetime.now(UTC), last_n=n):
            if e["direction"] == idea["direction"] and e["event"] in ("SWEEP_RECLAIM", "FAILED_BREAKOUT") and \
                    idea["entry_low"] - width <= e["price"] <= idea["entry_high"] + width:
                bar = next((b for b in bars if b["time"] == e["bar_time"]), None)
                if bar and BR.sweep_bar_ok(idea, bar):             # 1.10: the candle must close back at the zone
                    return bar, "SESSION_SWEEP" if e["event"] == "SWEEP_RECLAIM" else "SESSION_FAILED_BREAKOUT"
                if bar and (idea.get("id"), e["level"], e["bar_time"]) not in self.sweep_logged:
                    self.sweep_logged.add((idea.get("id"), e["level"], e["bar_time"]))      # 1.10.2: once, not every poll
                    LOG.info("Idea %s %s: %s of %s at %s on candle %s ignored: close %.2f is not back at the zone "
                             "%s-%s in the idea's direction", idea.get("id"), idea["direction"], e["event"], e["level"],
                             e["price"], bar["time"][11:16], bar["close"], idea["entry_low"], idea["entry_high"])
        return None

    def python_session_setups(self, now: datetime, quote: dict):
        """1.7: Python-only session sweeps are not traded (python_session_setups must be \"off\")."""
        return

    def breakout_setups(self, now: datetime) -> list:
        """1.8 BREAKOUT_CONTINUATION: once per closed 15M candle, a first close through a session /
        previous-day / previous-week level WITH the macro and 1H trend becomes a live order (see
        breakout.py). Python's own verification is the evidence; the tier is capped (tier_cap)."""
        live_on = self.modes().get("BREAKOUT_CONTINUATION") == "live"
        lab_on = (self.c.get("breakout_lab") or {}).get("mode") == "lab"
        if not (live_on or lab_on):
            return []
        evidence, atr15 = self.market_context()
        state, snap = self.market_state()
        bars = self.recent_bars()
        if not bars or not atr15:
            return []
        last = bars[-1]
        if getattr(self, "_bk_bar", None) == last["time"]:
            return []
        self._bk_bar = last["time"]
        events = [e for e in (snap.get("events") or []) if e.get("event") == "BREAKOUT" and e.get("bar_time") == last["time"]]
        if not events:
            return []
        highs, lows = RP.levels_for_targets(state, snap.get("levels") or [])
        quote = self.market.quote()
        cfg = RP.cfg_from_settings(self.c)
        proposals = []
        lab_out = []
        for e in events:
            why: list = []
            cand = BK.candidate(e, last, state, atr15, highs, lows, now, snap.get("session"), quote["spread"], cfg,
                                drop=why.append) if live_on else None
            if not cand:
                if live_on:
                    LOG.info("Breakout %s of %s %s not traded: %s", e["direction"], e["level"], e["price"], "; ".join(why))
                if lab_on:                                     # 1.19: against the Daily/4H or 1H trend -> the lab
                    tags: list = []
                    lwhy: list = []
                    lc = BK.candidate(e, last, state, atr15, highs, lows, now, snap.get("session"), quote["spread"],
                                      cfg, drop=lwhy.append, against=tags)
                    if lc and tags:
                        lab_out.append(self.breakout_lab(lc, e, tags, state, snap, atr15, quote, now))
                    elif lwhy or not live_on:                  # 1.20.1: say why the lab did not sample it either
                        LOG.info("Breakout lab %s of %s %s not sampled: %s", e["direction"], e["level"], e["price"],
                                 "; ".join(lwhy) or "with the trend (the live rule's trade, live breakouts are off)")
                continue
            targets = [cand["tp1"]] + ([cand["tp2"]] if cand["tp2"] is not None else [])
            sid = f"BK-{e['level']}-{cand['direction']}-{round(cand['level'])}"
            order = BR._order(cand["direction"], cand["order_type"], cand["entry"], cand["stop"], targets, cand["expiry"],
                              sid, "B_TRADE", f"python breakout of {e['level']} {cand['level']} ({cand['rr_now']}R at the close)",
                              cancel_above=cand["tp1"] if cand["direction"] == "BUY" and cand["order_type"] != "MARKET" else None,
                              cancel_below=cand["tp1"] if cand["direction"] == "SELL" and cand["order_type"] != "MARKET" else None)
            dec = {"trade_class": "BREAKOUT_CONTINUATION", "mode": "live", "reason": "python breakout",
                   "expansion": state.get("expansion") or {}, "would_block": False}
            pseudo = {"confidence": self.c["tiers"]["A"]["min_confidence"], "astra_risk": "MEDIUM",
                      "sl_anchor": {"id": cand["zone"]["id"]}, "tp_anchors": [{"id": f"LEVEL:{cand['tp1']}"}]}
            tier = TR.grade(pseudo, cand["direction"], dec, state, snap.get("session"), self.c)
            LOG.info("Breakout %s of %s %s -> %s | path levels %s", cand["direction"], e["level"], cand["level"],
                     tier["reason"], cand["path_levels"])
            if tier["tier"] is None:
                continue
            order["_counter"], order["_strategy"] = False, "SWING_BREAKOUT"
            order["_meta"] = dict(self.meta(cand["direction"], dec, "BREAKOUT", playbook="BREAKOUT_CONTINUATION"),
                                  tier=tier["tier"], tier_facts=tier["facts"], tier_reason=tier["reason"],
                                  targets=targets, path_levels=cand["path_levels"])
            order["risk_pct"], order["min_lot_ok"] = tier["risk_pct"], tier.get("min_lot_ok", False)
            proposals.append(order)
        lab_out = [x for x in lab_out if x]
        if not proposals:
            return lab_out
        LOG.info("--- Python breakout setups: %d order(s) ---", len(proposals))
        return lab_out + self.place_orders(proposals[: self.c["max_orders_per_analysis"]], evidence, atr15,
                                           require_evidence=False)

    def breakout_lab(self, cand: dict, e: dict, tags: list, state: dict, snap: dict, atr15: float, quote: dict,
                     now: datetime) -> str | None:
        """1.19: a breakout AGAINST the Daily/4H or 1H trend, sent to the lab at the minimum lot (magic_lab) with the
        same geometry as a live breakout (stop past the level, TP1 the first opposing level >= 2R). A sample, not a
        trade idea: it never reaches the live book, and the volume / 5M-1M read at the send is recorded on it."""
        d = cand["direction"]
        if d not in (self.c["breakout_lab"].get("sides") or []):
            return None
        sid = f"LBK-{e['level']}-{d}-{round(cand['level'])}"
        lab = self.lab
        block = lab.hard_block(now)
        if block:
            LOG.info("Breakout lab %s: holding: %s", sid, block)
            return None
        day0 = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        if self.state.setup_orders_since(sid, day0, lab=True):
            LOG.info("Breakout lab %s: already sampled in the last 24 h", sid)
            return None
        targets = [cand["tp1"]] + ([cand["tp2"]] if cand["tp2"] is not None else [])
        order = BR._order(d, cand["order_type"], cand["entry"], cand["stop"], targets, cand["expiry"], sid, "B_TRADE",
                          f"lab breakout of {e['level']} {cand['level']} against the trend ({', '.join(tags)}; "
                          f"{cand['rr_now']}R at the close)",
                          cancel_above=cand["tp1"] if d == "BUY" and cand["order_type"] != "MARKET" else None,
                          cancel_below=cand["tp1"] if d == "SELL" and cand["order_type"] != "MARKET" else None,
                          deviation=self.c["signal_entry_tolerance"])
        order["rr"] = round(abs(cand["tp1"] - cand["entry"]) / abs(cand["entry"] - cand["stop"]), 2)
        order["max_entry_deviation"] = min(float(order["max_entry_deviation"]), float(self.c["max_entry_deviation_cap"]))
        why = lab.hedge_with_working(order, state.get("macro_trend"))
        if why:
            LOG.info("Breakout lab %s: skipped: %s", sid, why)
            return None
        si = self.market.symbol_info()
        lots, risk_money, hold = lab.size(si, abs(cand["entry"] - cand["stop"]))
        if hold:
            LOG.info("Breakout lab %s: holding: %s", sid, hold)
            return None
        order["strategy"] = "LAB_BREAKOUT"
        order["meta"] = {"playbook": "BREAKOUT_AGAINST", "confirmation": "BREAKOUT_15M", "session": snap.get("session"),
                         "regime": state.get("phase"), "lab": 1, "violations": tags, "tier": None,
                         "tier_reason": "lab: breakout against the trend (" + ", ".join(tags) + ")"}
        order["spread"], order["atr15"] = quote["spread"], round(atr15, 2)
        try:
            ticket = self.executor.send(order, lots, risk_money, "B_TRADE", magic=self.c["magic_lab"])
        except NW.NewsHold as h:
            LOG.info("Breakout lab %s: held: %s", sid, h)
            return None
        except Exception as exc:  # noqa: BLE001
            alerts.error(f"Breakout lab order {sid} failed: {exc}")
            return f"{sid}: broker: {exc}"
        rec = next((o for o in self.state.all_orders() if o.get("ticket") == ticket), {}) if ticket else {}
        LOG.info("BREAKOUT LAB ORDER SENT ticket=%s %s %s @ %.2f sl %.2f tp1 %.2f (%.2f R) | %.2f lots risk %.2f | %s",
                 ticket, sid, cand["order_type"], cand["entry"], cand["stop"], cand["tp1"], order["rr"], lots, risk_money,
                 ",".join(tags))
        alerts.trade(f"🧪 LAB BREAKOUT {d} {sid} {cand['order_type']} @ {cand['entry']:.2f} | SL {cand['stop']:.2f} | "
                     f"TP1 {cand['tp1']:.2f} | R:R {order['rr']} | {lots} lots | {', '.join(tags)} | "
                     f"{FL.text(rec) if rec.get('flow') else 'flow not recorded'}", short=None)
        return f"{sid}: sent #{ticket}"

    def memory_zones(self) -> list:
        """The signal bot's remembered zones, re-read once per 15M bar."""
        bar = self._ctx[0] if self._ctx else None
        if self._zones is None or self._zones[0] != bar:
            self._zones = (bar, self.bridge.memory_zones())
        return self._zones[1]

    def recent_bars(self) -> list:
        """Closed 15M candles (cached with the market context)."""
        self.market_context()
        return self._ctx[3][-96:] if self._ctx else []

    def bridge_tick(self, now: datetime) -> list:
        """Confirmed signals first (act at once), then any unhandled ideas of the latest analysis."""
        outcomes = []
        py_only = self.pyideas.active(now) and self.pyideas.cfg["mode"] == "only"
        signals = [] if py_only else self.bridge.new_signals(now)     # 1.20: a Python-only test takes no Astra signal
        a, ideas = self.bridge.open_ideas(now)
        if not signals and not ideas:
            self.warn_if_signal_bot_quiet(now)
        try:
            outcomes += self.breakout_setups(now)                      # 1.8: first, a trend-day entry never waits
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Breakout setups skipped: %s", GM.safe_error(exc)[:200])
        gate = self.validator.gates(now)
        if gate:
            for s in signals:
                LOG.info("Signal %s %s not traded: %s", s["key"], s["direction"], gate)
                self.state.log_analysis(s, "signal", False, "gate: " + gate)
            if ideas and self.bridge_gate_logged != (a["id"], gate):
                LOG.info("%d idea(s) from analysis %s waiting: %s", len(ideas), a["id"], gate)
                self.bridge_gate_logged = (a["id"], gate)
            self.lab.sweep(a, ideas, now)                       # 1.10: the lab takes what the live gate holds
            return outcomes                                     # ideas are retried; signals are time-critical
        quote = self.market.quote()
        for s in signals:
            order, why = BR.signal_to_order(s, quote, self.c, now)
            if order is None:
                LOG.info("Signal %s not traded: %s", s["key"], why)
                self.state.log_analysis(s, "signal", False, why)
                continue
            LOG.info("--- Signal bot %s signal %s (%s) ---", s["direction"], s["sid"], s.get("grade"))
            order["_strategy"] = "SIGNAL"
            evidence, atr15 = self.market_context()
            order["_meta"] = self.meta(s["direction"], None, "SIGNAL_BOT_CONFIRMED", playbook="SIGNAL")
            out = self.place_orders([order], evidence, atr15, require_evidence=False)
            self.state.log_analysis(s, "signal", any(": sent #" in o or ": dry run" in o for o in out), " | ".join(out))
            outcomes += out
        proposals, live_results = [], {}
        born = GM.stamp(a["created_at"]) if a else None
        modes = self.modes()
        alt = modes["REVERSAL_TRANSITION"] != "off" or modes["SESSION_SWEEP"] != "off"
        for idea in ideas:
            key = (a["id"], idea.get("id"))
            if time.time() < self.idea_pause.get(key, 0.0):          # 1.19: held -> next 15M close / quote retry
                continue
            why = BR.prefilter(idea, self.c)
            order = None
            if why is None:
                evidence, _ = self.market_context()
                state, snap = self.market_state()
                veto = self.c["block_on_mtf_opposition"]
                counter = veto and EV.mtf_opposed(idea["direction"], evidence)
                strong = bool(counter and modes["COUNTER_TREND"] != "off"
                              and BR.strong_zone(idea, self.memory_zones(), self.c) is not None)
                if counter and not strong and not alt:
                    why = ("against Python 4H/1H structure (counter_trend_enabled is off)"
                           if not self.c["counter_trend_enabled"] else
                           "counter-trend idea is not at a strong remembered Daily/4H zone "
                           f"(tested >= {self.c['counter_min_tests']}x, right role)")
                elif counter or self.c["idea_entry_mode"] == "confirmation":
                    status, bar, how = BR.confirmation(idea, self.recent_bars(), born, evidence)
                    # 1.10.2: a candle that formed inside a news lock is the release, not a confirmation. 27 Oct:
                    # the 12:30-12:45 Durable Goods candle "confirmed" a SELL and the order went in at 13:00:01,
                    # one second after the lock. Look only at candles that OPENED after the latest lock ended
                    # (an invalidation inside the lock still counts: that check used the full window above).
                    since = born
                    lock_end = self.market.last_lock_end(now)
                    if status != "invalidated" and lock_end is not None and lock_end > born:
                        since = lock_end - timedelta(seconds=1)
                        if status == "confirmed" and BR._bar_time(bar) <= since:
                            if key + ("news-candle",) not in self.idea_logged:
                                LOG.info("Idea %s %s: confirmation on candle %s ignored: it formed inside the news lock "
                                         "(ended %s); waiting for a candle after it", idea.get("id"), idea["direction"],
                                         bar["time"][11:16], lock_end.strftime("%H:%M"))
                                self.idea_logged.add(key + ("news-candle",))
                            status, bar, how = BR.confirmation(idea, self.recent_bars(), since, evidence)
                    if status == "waiting":
                        sess = self.session_confirmation(idea, since)
                        if sess:
                            status, (bar, how) = "confirmed", sess
                    if status == "waiting":
                        if key + ("waiting",) not in self.idea_logged:
                            LOG.info("%sIdea %s %s %s-%s stop %s: waiting for a closed 15M confirmation",
                                     "Counter-trend " if counter else "", idea.get("id"), idea["direction"],
                                     idea["entry_low"], idea["entry_high"], idea["invalidation"])
                            self.idea_logged.add(key + ("waiting",))
                        continue
                    if status == "invalidated":
                        why = f"stop {idea['invalidation']} traded before any confirmation (candle {bar['time'][11:16]})"
                    else:
                        LOG.info("Idea %s %s CONFIRMED (%s) by the 15M candle %s (o %.2f h %.2f l %.2f c %.2f)",
                                 idea.get("id"), idea["direction"], how, bar["time"][11:16], bar["open"], bar["high"],
                                 bar["low"], bar["close"])
                        dec = PB.decide(idea["direction"], state, self.recent_bars(), strong_zone=strong,
                                        confirmation=how, modes=modes, expansion_mode=self.c["expansion_filter"],
                                        levels=snap["levels"], veto=veto)
                        self.log_decision(idea, dec)
                        if dec["mode"] == "off":
                            why = f"{dec['trade_class']}: {dec['reason']}"
                        else:
                            order, why = BR.confirmed_order(idea, quote, self.c, now, bar, how)
                            if order is not None:
                                order["_dec"], order["_how"] = dec, how
                else:
                    order, why = BR.idea_to_order(idea, quote, self.c, now)
                    if order is None and BR.transient(why):          # limit mode: retry while fresh
                        if key + (why,) not in self.idea_logged:
                            LOG.info("Idea %s %s %s-%s not traded yet: %s", idea.get("id"), idea["direction"],
                                     idea["entry_low"], idea["entry_high"], why)
                            self.idea_logged.add(key + (why,))
                        continue
                    if order is not None:
                        dec = PB.decide(idea["direction"], state, self.recent_bars(), strong_zone=strong,
                                        confirmation=None, modes=modes, expansion_mode=self.c["expansion_filter"],
                                        levels=snap["levels"], veto=veto)
                        self.log_decision(idea, dec)
                        order["_dec"], order["_how"] = dec, "LIMIT_NO_CONFIRMATION"
                        if dec["mode"] == "off":
                            order, why = None, f"{dec['trade_class']}: {dec['reason']}"
                if order is not None:
                    dec = order.pop("_dec")
                    cls = dec["trade_class"]
                    # 1.7: Python grades the idea from verifiable facts and sizes it by tier
                    tier = TR.grade(idea, idea["direction"], dec, state, snap.get("session"), self.c)
                    LOG.info("Idea %s %s tier: %s | facts %s", idea.get("id"), idea["direction"], tier["reason"],
                             {k: v for k, v in tier["facts"].items() if not k.startswith("_")})
                    if tier["tier"] is None:
                        why = "no tier: " + tier["reason"]
                    else:
                        order["_idea"], order["_counter"] = idea, cls != "TREND"
                        order["_strategy"] = "USER_PLAN" if idea.get("user_plan") else \
                            {"TREND": "SWING_TREND", "COUNTER_TREND": "SWING_COUNTER"}.get(cls, "SWING_" + cls)
                        if idea.get("user_plan") and tier["tier"] == "A":            # 1.9: owner plans at B at most
                            tier = dict(tier, tier="B", risk_pct=float(self.c["tiers"]["B"]["risk_pct"]),
                                        reason=tier["reason"] + "; owner plan capped at B")
                        order["_meta"] = dict(self.meta(idea["direction"], dec, order.pop("_how"), idea),
                                              tier=tier["tier"], tier_facts=tier["facts"], tier_reason=tier["reason"],
                                              user_plan=idea.get("user_plan"))
                        order["grade"] = "A_TRADE" if tier["tier"] == "A" else "B_TRADE"
                        order["risk_pct"], order["min_lot_ok"] = tier["risk_pct"], tier.get("min_lot_ok", False)
                        if idea.get("source") == "python":                  # 1.20: a separate, capped sample
                            order["setup_id"] = "PY-" + order["setup_id"].split("-", 1)[1]
                            order["_strategy"] = "PY_" + order["_strategy"]
                            pcap = self.pyideas.cfg["risk_pct"]                # null = Astra's sizing (1.20.2)
                            if pcap is not None and order["risk_pct"] > float(pcap):
                                order["risk_pct"], order["grade"] = pcap, "B_TRADE"
                                order["_meta"]["tier_reason"] = (order["_meta"].get("tier_reason") or "") + \
                                    f"; Python idea capped at {pcap}%"
                        cap = self.c.get("limit_entry_risk_pct")
                        if cap and order["_meta"].get("confirmation") == "LIMIT_NO_CONFIRMATION" \
                                and order["risk_pct"] > float(cap):            # 1.16: unconfirmed limits at most cap %
                            order["risk_pct"], order["grade"] = float(cap), "B_TRADE"
                            order["_meta"]["tier_reason"] = (order["_meta"].get("tier_reason") or "") + \
                                f"; limit without confirmation capped at {cap}%"
                        traded = self.python_zone_traded(order["setup_id"], now) if idea.get("source") == "python" else None
                        if traded:
                            why = traded
                        else:
                            proposals.append(order)
                            continue
            LOG.info("Idea %s %s %s-%s not traded: %s", idea.get("id"), idea["direction"], idea["entry_low"],
                     idea["entry_high"], why)
            self.bridge.idea_done(a, idea)
        if proposals:
            LOG.info("--- Signal bot analysis %s: %d idea order(s) ---", a["id"], len(proposals))
            evidence, atr15 = self.market_context()
            done = lambda p: self.bridge.idea_done(a, p["_idea"])
            self._paused_ids = {}
            out = self.place_orders(proposals[: self.c["max_orders_per_analysis"]], evidence, atr15,
                                    require_evidence=self.c["idea_require_evidence"], on_rejected=done)
            for p in proposals:
                until = self._paused_ids.get(p["setup_id"])
                if until:
                    self.idea_pause[(a["id"], p["_idea"].get("id"))] = until
            self.state.log_analysis({"analysis": a["id"], "ideas": [p["setup_id"] for p in proposals]}, "ideas",
                                    any(": sent #" in o or ": dry run" in o for o in out), " | ".join(out))
            outcomes += out
            live_results.update({p["_idea"].get("id"): o for p, o in zip(proposals, out)})
        self.lab.sweep(a, ideas, now, live_results)             # 1.10: everything the live track did not send
        return outcomes

    def python_zone_traded(self, setup_id: str, now: datetime) -> str | None:
        """1.20.3: a Python idea re-enters a zone it already TRADED (filled) at most once per `reentry_hours`. 13 Nov:
        Python re-issues the same zone every hour, so PY-SELL-3909 was placed three times and PY-BUY-3880 filled twice
        (the second one -1 R) after the first trades had closed."""
        hours = float(self.pyideas.cfg.get("reentry_hours") or 0)
        if hours <= 0:
            return None
        since = (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        filled = [o for o in self.state.setup_orders_since(setup_id, since) if o.get("fill_utc")]
        if not filled:
            return None
        return (f"Python zone {setup_id} already traded (filled {str(filled[-1]['fill_utc'])[11:16]} UTC); "
                f"one entry per zone per {hours:g} h")

    def log_decision(self, idea: dict, dec: dict):
        astra = idea.get("trade_class")
        LOG.info("Idea %s %s -> %s (%s) mode %s%s%s", idea.get("id"), idea["direction"], dec["trade_class"],
                 dec["reason"], dec["mode"],
                 f" | Astra said {astra}" + (" (DIFFERENT: Python's class is used)" if astra and astra != dec["trade_class"] else "")
                 if astra else "",
                 f" | expansion filter ({self.c['expansion_filter']}) WOULD BLOCK" if dec["would_block"] else
                 (f" | fading an expansion, reversal confirmed by {dec['expansion']['methods']}"
                  if dec["expansion"]["against"] else ""))

    def warn_if_signal_bot_quiet(self, now: datetime):
        """No usable analysis while the market is open usually means the signal bot is not running."""
        if not self.c.get("expect_signal_bot", True):
            return                                             # 1.11: gold_monitor is off on purpose
        if self.trading_window_open(now) and time.time() - self.quiet_warned > 3600:
            a, age = self.bridge.analysis(now)
            if a is None or age > self.c["idea_max_age_minutes"]:
                alerts.error("No recent analysis from the signal bot "
                             f"({'none found' if a is None else f'last one {age:.0f} min ago'}). "
                             "Is gold_monitor (scripts/run.ps1) running?")
                self.quiet_warned = time.time()

    def trading_window_open(self, now):
        return self.market.trading_window(now) is None

        # ------------------------------------------------------------------ loop
    def reconnect(self):
        while True:
            try:
                return self.market.connect()
            except MarketError as exc:
                if "REFUSING" in str(exc) or "TRADER_MT5_PATH" in str(exc):
                    raise
                LOG.warning("Waiting for MT5 / market: %s", GM.safe_error(exc)[:200])
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Waiting for MT5 / market: %s", GM.safe_error(exc)[:200])
            time.sleep(60)

    def run(self, once=False):
        self.reconnect()
        self.market.refresh_calendar(force=True)
        if self.market.calendar_error:
            LOG.warning("Calendar not available yet: %s (no orders until it is)", self.market.calendar_error[:200])
        next_manage = 0.0
        first = self.c["analyse_on_start"] or once
        while True:
            try:
                now = datetime.now(UTC)
                self.market.refresh_calendar()
                if time.time() >= next_manage:
                    self.executor.manage(now)
                    next_manage = time.time() + self.c["monitor_interval_seconds"]
                try:
                    self.protect.tick(now)                         # 1.15: every check_seconds, open trades only
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("Trade protection skipped: %s", GM.safe_error(exc)[:200])
                try:
                    self.conflicts.tick(now)                       # 1.16: opposite live trades, live limits vs newer map
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("Opposite-trade check skipped: %s", GM.safe_error(exc)[:200])
                try:
                    self.overnight.tick(now)                       # 1.11: cheap unless inside its entry window
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("Overnight engine skipped: %s", GM.safe_error(exc)[:200])
                try:
                    self.weakness.tick(now)                        # 1.12: candles reloaded once per closed 5M bar
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("4H weakness lab skipped: %s", GM.safe_error(exc)[:200])
                try:
                    self.scalp.tick(now)                           # 11 Nov: once per closed 5M candle
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("Scalp lab skipped: %s", GM.safe_error(exc)[:200])
                try:
                    self.newsguard.tick(now)                       # 1.14: once per major release
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("Pre-news guard skipped: %s", GM.safe_error(exc)[:200])
                try:
                    self.h1.tick(now)                              # 1.13.1: once per 15M bar, registers LTF watches
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("1H-trend lab skipped: %s", GM.safe_error(exc)[:200])
                try:
                    self.ltf.tick(now)                             # 1.13: 5M entries for the watched lab ideas
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("LTF lab skipped: %s", GM.safe_error(exc)[:200])
                if self.c["source"] == "signal_bot":
                    try:
                        self.lab.recheck(now)                      # 1.11.1: TP1-touched lab limits back to Astra
                    except Exception as exc:  # noqa: BLE001
                        LOG.warning("Lab recheck skipped: %s", GM.safe_error(exc)[:200])
                    self.bridge_tick(now)
                    self.straddle.tick(now)
                    if once:
                        return
                    self.feed_failures = 0
                    time.sleep(self.c["bridge_poll_seconds"])
                    continue
                bar = self.market.last_closed_15m_open()
                close = bar + timedelta(minutes=15)
                if first:
                    first = False
                    self.last_bar = bar
                    self.cycle("start")
                    if once:
                        return
                elif bar != self.last_bar and now >= close + timedelta(seconds=self.c["close_delay_seconds"]):
                    self.last_bar = bar
                    trigger = self.trigger_for(close, self.market.quote())
                    if trigger:
                        LOG.info("--- 15M close %s UTC: analysis (%s) ---", close.strftime("%H:%M"), trigger)
                        self.cycle(trigger)
                self.feed_failures = 0
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001
                self.feed_failures += 1
                LOG.warning("Loop error #%d: %s", self.feed_failures, GM.safe_error(exc)[:300])
                if self.feed_failures % 4 == 0:
                    if self.market.sessions.is_open(datetime.now(UTC)):   # quiet over weekends/breaks
                        alerts.error(f"MT5 problem, reconnecting: {GM.safe_error(exc)[:200]}")
                    self.reconnect()
                time.sleep(5)
                continue
            time.sleep(2)
