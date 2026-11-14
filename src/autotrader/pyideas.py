"""
1.20 (owner, 12 Nov): "now I am working with no Astra credits - let's test Python-only trading for 10 days."
1.20.2 (owner): "the rules are too strict - the same as it is when Astra is included." Python's ideas now cover what
Astra's do: BOTH directions every hour, entry zones from the remembered support / resistance AND the order blocks,
FVGs and breakers Python already detects, up to 3 ideas, and the same sizing as Astra's ideas (no extra cap).

Python's own TRADE IDEAs, in Astra's idea format, from the signal bot's market memory (the remembered support /
resistance zones the signal bot keeps updating without any AI call) and the trader's own SMC evidence. They go through
EXACTLY the same trader rules as Astra's ideas: TREND vs COUNTER_TREND from Python structure, the strong-zone + 15M
confirmation rule for counter-trend, tiers, sessions, 2R recomputed, the cap on limits without confirmation, news
rules, duplicates, loss limits. The lab samples what live does not take, as it does for Astra.

Rule, once per closed 1H candle (BUY shown; SELL is the mirror):
  zones    candidates BELOW price whose top is 0.3 .. `max_distance_atr15` x ATR15 away:
             - remembered SR zones, role SUPPORT, ACTIVE/FLIPPED, timeframe in `timeframes`, tested >= `min_tests`
             - BUY order blocks / FVGs / breakers on `smc_timeframes`, still usable (not filled / invalidated)
           each scored: 2 for a Daily/4H/weekly zone or an SR zone tested >= 2 (else 1), +1 per other candidate
           overlapping it (max +2), minus 0.25 per ATR15 of distance; the best one is the entry zone
  entry    a LIMIT idea on that zone; the trader places it at the zone top, or lower inside it when needed for 2R
  stop     the zone bottom - `stop_buffer_atr15` x ATR15
  TP1/TP2  the first opposite levels above price giving >= `min_rr` from the zone top: opposite SR zones, SELL
           order blocks / FVGs, 1H/4H swing highs; TP2 the next one. Beyond `max_tp_r` R, TP1 = the max_tp_r price
           and the level becomes TP2 (not anchored for the tier)
  ideas    the best BUY and the best SELL; a third, the second-best zone in the 1H trend's direction (`max_ideas`)
  label    confidence 30, +5 with the 1H trend, +5 with confluence (max 40, Astra's usual range); risk HIGH against
           the 1H trend, else MEDIUM
Ids: the analysis is P<yyyymmddHH>; live orders PY-<DIR>-<level>, strategy PY_SWING_*; lab orders strategy LAB_PY -
their results never mix with Astra's. `risk_pct` (null = none) can cap them below Astra's sizing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import gold_monitor as GM

from . import alerts

UTC = timezone.utc
LOG = logging.getLogger("autotrader.pyideas")
DEFAULTS = {"mode": "off", "until": None, "min_tests": 1, "timeframes": ["1week", "1day", "4h", "1h"],
            "smc_timeframes": ["1day", "4h", "1h"], "max_distance_atr15": 8.0, "stop_buffer_atr15": 0.3,
            "min_rr": 2.0, "max_tp_r": 4.0, "risk_pct": None, "max_ideas": 3, "reentry_hours": 12, "telegram": False}
HTF = ("1week", "1day", "4h")
SMC_KINDS = {"FVG": ("ACTIVE", "PARTIALLY_FILLED"), "ORDER_BLOCK_CANDIDATE": ("ACTIVE", "TESTED"),
             "BREAKER_CANDIDATE": ("AWAITING_RETEST", "RETEST_CONFIRMED")}
NAMES = {"FVG": "FVG", "ORDER_BLOCK_CANDIDATE": "order block", "BREAKER_CANDIDATE": "breaker"}


def _dedupe(zones: list) -> list:
    seen, out = set(), []
    for z in sorted(zones, key=lambda z: (-(z.get("tests") or 0), z["id"])):
        k = (round(z["low"], 2), round(z["high"], 2), z.get("role"))
        if k not in seen:
            seen.add(k)
            out.append(z)
    return out


def _anchor(z: dict) -> dict:
    return {"id": z["id"], "timeframe": z.get("timeframe"), "role": z.get("role"), "low": z["low"], "high": z["high"],
            "tests": z.get("tests", 0)}


def _smc(evidence: dict | None, direction: str, tfs: list) -> list:
    """Usable order blocks / FVGs / breakers of one direction as zones (low, high, timeframe, kind, id)."""
    out = []
    for it in ((evidence or {}).get("index") or {}).values():
        ok = SMC_KINDS.get(it.get("kind"))
        if not ok or it.get("direction") != direction or it.get("status") not in ok or it.get("timeframe") not in tfs:
            continue
        lo, hi = it.get("level_low"), it.get("level_high")
        if lo is None or hi is None or hi <= lo:
            continue
        out.append({"id": it["id"], "low": float(lo), "high": float(hi), "timeframe": it["timeframe"],
                    "kind": it["kind"], "role": "SUPPORT" if direction == "BUY" else "RESISTANCE", "tests": 0,
                    "label": f"{it['timeframe']} {NAMES[it['kind']]}"})
    return out


def _swings(evidence: dict | None, side: str) -> list:
    out = []
    for tf in ("4h", "1h"):
        fr = ((evidence or {}).get("frames") or {}).get(tf) or {}
        for sw in fr.get("recent_swings") or []:
            if sw.get("side") == side and sw.get("price") is not None:
                p = float(sw["price"])
                out.append({"id": f"SWING:{tf}:{side}:{p:.2f}", "low": p, "high": p, "timeframe": tf,
                            "role": "RESISTANCE" if side == "HIGH" else "SUPPORT", "tests": 0})
    return out


def ideas(zones: list, price: float, atr15: float, cfg: dict, evidence: dict | None = None,
          h1_trend: str | None = None) -> list:
    """Python ideas (Astra's format) at this price. Pure: no I/O."""
    if not atr15 or atr15 <= 0 or not price:
        return []
    cfg = dict(DEFAULTS, **cfg)
    sr = _dedupe([z for z in zones if z.get("kind") == "SR" and z.get("status") in ("ACTIVE", "FLIPPED")])
    for z in sr:
        z.setdefault("label", f"{z.get('timeframe')} {str(z.get('role', '')).lower()} tested {z.get('tests') or 0}x")
    picks = {}
    for direction in ("BUY", "SELL"):
        buy = direction == "BUY"
        role = "SUPPORT" if buy else "RESISTANCE"
        pool = [z for z in sr if z.get("role") == role and z.get("timeframe") in cfg["timeframes"]
                and (z.get("tests") or 0) >= cfg["min_tests"]] + _smc(evidence, direction, cfg["smc_timeframes"])
        scored = []
        for z in pool:
            dist = (price - z["high"]) if buy else (z["low"] - price)
            if not 0.3 * atr15 <= dist <= cfg["max_distance_atr15"] * atr15:
                continue
            weight = 2 if (z.get("timeframe") in HTF or (z.get("tests") or 0) >= 2) else 1
            conf = min(2, sum(1 for o in pool if o is not z and o["low"] <= z["high"] and z["low"] <= o["high"]))
            scored.append((weight + conf - 0.25 * dist / atr15, conf, z))
        scored.sort(key=lambda x: -x[0])
        picks[direction] = scored
    order = [("BUY", 0), ("SELL", 0)]
    want = {"BULLISH": "BUY", "BEARISH": "SELL"}.get(h1_trend or "")
    if want:
        order.append((want, 1))
    out = []
    for direction, k in order:
        if len(out) >= cfg["max_ideas"] or len(picks[direction]) <= k:
            continue
        _s, conf_n, z = picks[direction][k]
        idea = _build(direction, z, conf_n, sr, evidence, price, atr15, cfg, h1_trend)
        if idea and not any(o["direction"] == direction and o["entry_low"] == idea["entry_low"] for o in out):
            idea["id"] = f"PI{len(out) + 1}"
            out.append(idea)
    return out


def _build(direction, z, conf_n, sr, evidence, price, atr15, cfg, h1_trend):
    buy = direction == "BUY"
    opp = "RESISTANCE" if buy else "SUPPORT"
    pad = cfg["stop_buffer_atr15"] * atr15
    lo, hi = z["low"], z["high"]
    stop = round(lo - pad if buy else hi + pad, 2)
    edge = hi if buy else lo
    risk = abs(edge - stop)
    if risk <= 0:
        return None
    levels = [t for t in sr if t.get("role") == opp] + _smc(evidence, "SELL" if buy else "BUY", cfg["smc_timeframes"]) + \
        _swings(evidence, "HIGH" if buy else "LOW")
    level = (lambda t: t["low"]) if buy else (lambda t: t["high"])
    levels = sorted((t for t in levels if ((level(t) > price) if buy else (level(t) < price))),
                    key=lambda t: level(t) if buy else -level(t))
    # 1.20.3: a target price has (almost) reached already is no target: the limit would be cancelled at once
    # ("TP1 traded before the fill"). 13 Nov 16:00 PY-SELL-3909, TP1 3872.16 with price 3873.49.
    tps = [t for t in levels if abs(level(t) - edge) / risk >= cfg["min_rr"]
           and abs(price - level(t)) >= 0.5 * risk]
    if not tps:
        return None
    tp1 = tps[0]
    tp2 = next((t for t in tps[1:] if abs(level(t) - level(tp1)) >= 0.5 * risk), None)
    tgt = [round(level(tp1), 2)] + ([round(level(tp2), 2)] if tp2 else [])
    anchors = [_anchor(tp1)] + ([_anchor(tp2)] if tp2 else [])
    if abs(tgt[0] - edge) / risk > cfg["max_tp_r"]:
        tgt = [round(edge + cfg["max_tp_r"] * risk if buy else edge - cfg["max_tp_r"] * risk, 2), tgt[0]]
        anchors = []
    with_trend = h1_trend == ("BULLISH" if buy else "BEARISH")
    conf = min(40, 30 + (5 if with_trend else 0) + (5 if conf_n else 0))
    risk_label = "HIGH" if h1_trend in ("BULLISH", "BEARISH") and not with_trend else "MEDIUM"
    return {"source": "python", "direction": direction, "entry_type": "LIMIT", "readiness": "DEVELOPING",
            "entry_low": round(lo, 2), "entry_high": round(hi, 2), "invalidation": stop, "targets": tgt,
            "confidence": conf, "risk": risk_label, "astra_risk": risk_label,
            "title": f"Python: {z.get('label')} {lo:.2f}-{hi:.2f}" + (" + confluence" if conf_n else ""),
            "sl_anchor": _anchor(z), "tp_anchors": anchors, "memory_ids": [z["id"], tp1["id"]],
            "price_at_analysis": round(price, 2), "rr_tp1": round(abs(tgt[0] - edge) / risk, 2)}


class PyIdeas:
    def __init__(self, c: dict, app):
        self.c = c
        self.cfg = dict(DEFAULTS, **(c.get("python_ideas") or {}))
        self.app = app
        self.cache = None              # (1H candle key, analysis)
        self.ended_logged = False
        self.stale_warned = None
        if self.cfg["mode"] != "off":
            LOG.info("Python ideas %s%s | SR %s tested >= %d + OB/FVG/breaker %s, within %.1f ATR15 | stop zone +%.2f "
                     "ATR15 | >= %.1f R | up to %d ideas | risk %s | strategy PY_* / LAB_PY",
                     self.cfg["mode"].upper(), f" until {self.cfg['until']}" if self.cfg["until"] else "",
                     "/".join(self.cfg["timeframes"]), self.cfg["min_tests"], "/".join(self.cfg["smc_timeframes"]),
                     self.cfg["max_distance_atr15"], self.cfg["stop_buffer_atr15"], self.cfg["min_rr"],
                     self.cfg["max_ideas"], "as Astra's ideas" if self.cfg["risk_pct"] is None else
                     f"<= {self.cfg['risk_pct']}%")

    def active(self, now: datetime) -> bool:
        if self.cfg["mode"] == "off":
            return False
        until = self.cfg.get("until")
        if until and now.date().isoformat() > str(until):
            if not self.ended_logged:
                LOG.info("Python ideas: the test window ended on %s; Astra's ideas only from now", until)
                alerts.error(f"Python-only test ended ({until}): the trader uses Astra's ideas again")
                self.ended_logged = True
            return False
        return True

    def replaces(self, astra: dict | None, age: float | None, now: datetime) -> bool:
        """True when Python's analysis is the one to trade now (mode only, or fallback with no fresh Astra map)."""
        if not self.active(now):
            return False
        if self.cfg["mode"] == "only":
            return True
        return astra is None or age is None or age > float(self.c["idea_max_age_minutes"])

    def analysis(self, now: datetime) -> dict | None:
        """Python's analysis of the last closed 1H candle (rebuilt once per hour), in Astra's file format."""
        try:
            bar = self.app.market.rates("TIMEFRAME_H1", 1)[-1][0]
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Python ideas: no 1H candle (%s)", str(exc)[:120])
            return self.cache[1] if self.cache else None
        if self.cache and self.cache[0] == bar:
            return self.cache[1]
        cur = self.app.bridge.memory_cursor("1h")
        zones = self.app.bridge.memory_zones()
        if cur is None or (now - cur).total_seconds() > 3 * 3600:
            zones = []                     # 1.20.2: stale memory -> its zones are left out; OB/FVG/breakers still work
            open_ = getattr(self.app, "trading_window_open", lambda n: True)(now)
            if open_ and (self.stale_warned is None or (now - self.stale_warned).total_seconds() > 3600):
                alerts.error("Python ideas without the remembered support/resistance: the signal bot's market memory "
                             f"is not updating (last 1H candle {cur:%Y-%m-%d %H:%M} UTC). Start scripts/run.ps1 to "
                             "add those zones back." if cur else
                             "Python ideas without the remembered support/resistance: no signal bot market memory. "
                             "Start scripts/run.ps1 to add those zones back.")
                self.stale_warned = now
        evidence, atr15 = self.app.market_context()
        state, _snap = self.app.market_state()
        q = self.app.market.quote()
        price = (q["bid"] + q["ask"]) / 2
        made = ideas(zones, price, atr15, self.cfg, evidence, state.get("intermediate_trend"))
        stamp = datetime.fromtimestamp(bar, UTC)
        a = {"id": f"P{stamp:%Y%m%d%H}", "created_at": GM.iso(now), "stance": "TRADE_IDEA" if made else "NO_TRADE",
             "ideas": made, "source": "python", "no_trade_reason": None if made else "no tested zone within reach with 2R"}
        prev = self.cache[1]["ideas"] if self.cache and self.cache[1] else None
        self.cache = (bar, a)
        # 1.20.3: the zones only (a stop moving a few cents with ATR is not news); Telegram only with "telegram": true
        sig = sorted((i["direction"], i["entry_low"], i["entry_high"]) for i in made)
        if prev is None or sig != sorted((i["direction"], i["entry_low"], i["entry_high"]) for i in prev):
            lines = [f"{i['direction']} {i['entry_low']:.2f}-{i['entry_high']:.2f} | SL {i['invalidation']:.2f} | "
                     f"TP1 {i['targets'][0]:.2f} ({i['rr_tp1']}R) | {i['title'][8:]}" for i in made]
            LOG.info("Python analysis %s @ %.2f: %s", a["id"], price, " || ".join(lines) or a["no_trade_reason"])
            if self.cfg.get("telegram"):
                alerts.trade("🐍 PYTHON IDEAS " + stamp.strftime("%H:%M") + " UTC @ " + f"{price:.2f}\n" +
                             ("\n".join(lines) if lines else "No idea: " + a["no_trade_reason"]), short=None)
        return a
