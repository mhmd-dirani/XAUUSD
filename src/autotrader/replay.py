"""
Historical replay: old rules vs new rules on the SAME candles and the SAME candidate trades.

No AI calls. Chronological, closed-candle only:
  * candles are built from 1-minute bars; a 15M/1H/4H/D1 candle becomes visible only after it
    has closed (`closes` <= the replay clock), so no decision can see the future;
  * a decision taken at a 15M close is filled no earlier than the NEXT 1-minute bar's open;
  * trade outcomes are simulated forward on 1-minute bars (or ticks when supplied), but a variant
    only learns that a trade closed when the replay clock has passed its exit time.

Candidates (what plays Astra's role, since past Opus analyses cannot be replayed for free):
  structure zones   1H/4H order blocks and FVGs still usable, and 1H/4H swing highs/lows
  session levels    Asia/London/NY highs/lows, opening ranges, previous day/week (new rules only)
  confirmation      the trader's own closed-15M rules: REJECTION, SWEEP, CHOCH, and for session
                    levels SESSION_SWEEP / SESSION_FAILED_BREAKOUT
  stop / targets    beyond the zone or the confirming candle; TP1 = nearest opposing level,
                    TP2 = the next one; entry at market if >= min R:R, otherwise a limit at the
                    min R:R price for 4 hours (exactly like the live trader after confirmation)

Execution and costs (all in $/oz, configurable):
  MT5 candle prices are BID. BUY fills at ask = bid + spread; SELL stops/targets trigger on ask.
  Spread = the 1-minute bar's recorded spread (or `spread` if missing), plus `slippage` on market
  entries and stop exits, plus `commission_per_lot` / 100 per round trip.
  If the stop and a target are both inside one 1-minute bar the order of events is unknown:
  ticks decide when available, otherwise the trade is counted as a LOSS (worst case).
"""

from __future__ import annotations

import bisect
import csv
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import median
from zoneinfo import ZoneInfo

from gold_smc_engine import _time

from . import bridge as BR
from . import breakout as BK
from . import clock_model as CK
from . import ict_model as IM
from . import scalp_model as SC
from . import playbooks as PB
from . import regime as RG
from . import sessions as SS

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
TF_MIN = {"5min": 5, "15min": 15, "1h": 60, "4h": 240, "1day": 1440}   # 5min: ICT model only (30 Oct)

DEFAULTS = {
    "server_offset": "ny+7",         # broker clock: New York + 7 h (UTC+3 summer, UTC+2 winter), or hours
    "spread": 0.30,                  # $ when the data has no spread column
    "point": 0.01,
    "slippage": 0.10,                # $ on market entries and stop exits
    "commission_per_lot": 0.0,       # $ per lot round trip (100 oz)
    "risk_pct": 0.25,                # every replayed trade is B size (counter/reversal are always B)
    "min_rr": 2.0,
    "sl_atr_min": 0.5,
    "sl_atr_max": 5.0,
    "max_spread": 0.5,
    "spread_stop_fraction": 0.05,
    "retest_hours": 4,
    "max_hold_hours": 48,
    "max_active": 2,
    "max_trades_per_day": 4,
    "max_per_zone_per_day": 2,
    "duplicate_atr": 0.5,
    "daily_loss_pct": 2.0,
    "weekly_loss_pct": 5.0,
    "counter_min_tests": 2,
    "counter_zone_tolerance": 3.0,
    "skip_after_open_min": 30,
    "skip_before_close_min": 20,
    "out_of_sample": 0.3,            # last 30 % of the period reported separately
    "regime": {},
    "ict": {},                       # overrides for ict_model.DEFAULTS (30 Oct: the owner's sweep->MSS->FVG model)
}

VARIANTS = {
    # name: (structure zones, session levels, playbook modes, expansion filter, CHoCH confirmations)
    # v1.5 never counted a CHoCH (field-name bug fixed in 1.6), so the OLD variants exclude them.
    "OLD_v1.5": (True, False, {"TREND": "live", "COUNTER_TREND": "live"}, "off", False),
    "NEW_filter_only": (True, False, {"TREND": "live", "COUNTER_TREND": "live"}, "live", False),
    "NEW_playbooks_no_filter": (True, True, {"TREND": "live", "COUNTER_TREND": "live",
                                             "REVERSAL_TRANSITION": "live", "SESSION_SWEEP": "live"}, "off", True),
    "NEW_all": (True, True, {"TREND": "live", "COUNTER_TREND": "live",
                             "REVERSAL_TRANSITION": "live", "SESSION_SWEEP": "live"}, "live", True),
    # 1.8: everything above plus the breakout-continuation entries (Python-only, with the trend)
    "NEW_all_plus_breakout": (True, True, {"TREND": "live", "COUNTER_TREND": "live", "REVERSAL_TRANSITION": "live",
                                           "SESSION_SWEEP": "live", "BREAKOUT_CONTINUATION": "live"}, "live", True),
    "BREAKOUT_only": (False, False, {"BREAKOUT_CONTINUATION": "live"}, "off", False),
    # 1.8.1: the Monday configuration (TREND + COUNTER_TREND + BREAKOUT, filter live), with and without Asia,
    # and without REJECTION confirmations. 6th element = allowed sessions, 7th = allowed confirmations.
    "CORE": (True, False, {"TREND": "live", "COUNTER_TREND": "live", "BREAKOUT_CONTINUATION": "live"}, "live", True,
             None, None),
    "CORE_noAsia": (True, False, {"TREND": "live", "COUNTER_TREND": "live", "BREAKOUT_CONTINUATION": "live"}, "live", True,
                    ("LONDON", "LONDON_NY", "NEW_YORK"), None),
    "CORE_noAsia_noRejection": (True, False, {"TREND": "live", "COUNTER_TREND": "live", "BREAKOUT_CONTINUATION": "live"},
                                "live", True, ("LONDON", "LONDON_NY", "NEW_YORK"), ("SWEEP", "CHOCH", "BREAKOUT")),
    # 1.10.6 (29 Oct replay read): three hypotheses isolated from each other. 8th element = regime gate.
    # CORE_noRejection: CORE, all sessions, REJECTION no longer an accepted confirmation (it was negative in every
    #   variant of both runs; SWEEP positive in every one).
    # CORE_regimeGated: TREND pullback entries only while Daily and 4H DISAGREE (macro MIXED/NEUTRAL); when they agree
    #   (a real trend) only BREAKOUT_CONTINUATION. 29 Oct run: TREND in a trending macro 0/14 (-14.2R), in a mixed
    #   macro 25 trades +0.22R.
    # CORE_regimeGated_noRejection: both.
    "CORE_noRejection": (True, False, {"TREND": "live", "COUNTER_TREND": "live", "BREAKOUT_CONTINUATION": "live"},
                         "live", True, None, ("SWEEP", "CHOCH", "BREAKOUT"), None),
    "CORE_regimeGated": (True, False, {"TREND": "live", "COUNTER_TREND": "live", "BREAKOUT_CONTINUATION": "live"},
                         "live", True, None, None, "pullbacks_in_range_only"),
    "CORE_regimeGated_noRejection": (True, False, {"TREND": "live", "COUNTER_TREND": "live", "BREAKOUT_CONTINUATION": "live"},
                                     "live", True, None, ("SWEEP", "CHOCH", "BREAKOUT"), "pullbacks_in_range_only"),
    # 29 Oct (owner's target: daily trades, 75-80 % hit rate): the HIGH-WIN-RATE design. 9th element = the variant's own
    # minimum R:R. The candidate's TP1 is already the NEAREST opposing level; these variants enter at MARKET on the
    # confirmation close whenever that nearest level is at least 0.5R away (the shared 2R floor is replaced for them,
    # in the replay only), TP1 only, no partial. Everything else (stops, confirmations, caps) is the same code.
    "HIGH_WINRATE": (True, False, {"TREND": "live", "COUNTER_TREND": "live", "REVERSAL_TRANSITION": "live"},
                     "live", True, None, ("SWEEP", "CHOCH"), None, 0.5),
    "HIGH_WINRATE_all": (True, True, {"TREND": "live", "COUNTER_TREND": "live", "REVERSAL_TRANSITION": "live",
                                      "SESSION_SWEEP": "live"}, "live", True, None,
                         ("SWEEP", "CHOCH", "SESSION_SWEEP", "SESSION_FAILED_BREAKOUT"), None, 0.5),
    "HIGH_WINRATE_counter_only": (True, False, {"COUNTER_TREND": "live", "REVERSAL_TRANSITION": "live"},
                                  "live", True, None, ("SWEEP", "CHOCH"), None, 0.5),
}
TRENDING = ("BULLISH", "BEARISH")          # macro_trend values where Daily and 4H agree


def market_at_close(cand: dict, min_rr: float) -> dict | None:
    """The high-win-rate entry: MARKET at the confirmation close with the candidate's stop and its NEAREST-level TP1,
    taken whenever TP1 is at least `min_rr` x risk away. None when the nearest level is closer than that."""
    price = cand.get("price")
    if price is None:
        return None
    risk = abs(price - cand["stop"])
    if risk <= 0:
        return None
    rr = abs(cand["tp1"] - price) / risk
    if rr < min_rr:
        return None
    return dict(cand, order_type="MARKET", entry=price, tp2=None, expiry=cand["created"] + timedelta(minutes=30),
                cid=cand["cid"] + ":HW")


def regime_gate_blocks(gate: str | None, trade_class: str, macro: str | None) -> str | None:
    """Reason a variant's regime gate refuses this candidate, or None. 'pullbacks_in_range_only': a TREND (pullback)
    entry is only taken while the macro is MIXED/NEUTRAL; in a trending macro the continuation entry is the breakout."""
    if gate == "pullbacks_in_range_only" and trade_class == "TREND" and macro in TRENDING:
        return f"regime gate: TREND pullback in a trending macro ({macro})"
    return None


def cfg_from_settings(c: dict, **over) -> dict:
    """Replay/candidate settings taken from the trader config (so replay and live agree)."""
    v6 = c["v6"]
    cfg = {"min_rr": c["min_rr"], "sl_atr_min": c["sl_atr_min"], "sl_atr_max": c["sl_atr_max"],
           "max_spread": v6["broker_max_spread"], "spread_stop_fraction": v6["broker_max_spread_stop_fraction"],
           "retest_hours": c["confirm_retest_hours"], "max_active": c["max_active_orders"],
           "max_trades_per_day": c["max_trades_per_day"], "max_per_zone_per_day": c["max_orders_per_setup_per_day"],
           "duplicate_atr": c["duplicate_atr"], "daily_loss_pct": c["daily_loss_limit_pct"],
           "weekly_loss_pct": c["weekly_loss_limit_pct"], "counter_min_tests": c["counter_min_tests"],
           "counter_zone_tolerance": c["counter_zone_tolerance"],
           "skip_after_open_min": c["skip_minutes_after_open"], "skip_before_close_min": c["skip_minutes_before_close"],
           "regime": c["regime"], "breakout_retest_minutes": c.get("breakout_retest_minutes", 60)}
    return dict(DEFAULTS, **dict(cfg, **over))


# ---------------------------------------------------------------------- data
def server_offset(t: datetime, rule) -> timedelta:
    if isinstance(rule, (int, float)):
        return timedelta(hours=rule)
    return NY.utcoffset(t.replace(tzinfo=None)) + timedelta(hours=7) if rule == "ny+7" else timedelta(0)


def aggregate(m1: list, tf: str, rule) -> list:
    """1-minute UTC bars -> candles of tf, grouped on the BROKER clock like MT5 does (4H and D1
    boundaries are server-time boundaries). Each candle has its UTC open `time` and `closes`."""
    out, cur, key = [], None, None
    size = TF_MIN[tf]
    for b in m1:
        t = b["t"]
        off = server_offset(t, rule)
        st = t + off                                          # server clock
        day0 = datetime(st.year, st.month, st.day, tzinfo=UTC)
        if tf == "1day":
            k, bucket, length = st.date(), day0, timedelta(days=1)
        else:
            m = (st.hour * 60 + st.minute) // size
            k, bucket, length = (st.date(), m), day0 + timedelta(minutes=m * size), timedelta(minutes=size)
        if k != key:
            if cur:
                out.append(cur)
            start = bucket - off                              # back to UTC
            cur = {"time": start.isoformat(), "closes": (start + length).isoformat(),
                   "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"], "spread": b["s"]}
            key = k
        else:
            cur["high"] = max(cur["high"], b["h"])
            cur["low"] = min(cur["low"], b["l"])
            cur["close"] = b["c"]
            cur["spread"] = max(cur["spread"] or 0, b["s"] or 0)
    if cur:
        out.append(cur)
    return out


def read_m1_csv(path) -> list:
    """CSV with time_utc (ISO), open, high, low, close[, spread ($)]."""
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            t = datetime.fromisoformat(r["time_utc"].replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=UTC)
            rows.append({"t": t.astimezone(UTC), "o": float(r["open"]), "h": float(r["high"]),
                         "l": float(r["low"]), "c": float(r["close"]),
                         "s": float(r["spread"]) if r.get("spread") not in (None, "") else None})
    rows.sort(key=lambda x: x["t"])
    return rows


def write_m1_csv(path, m1: list):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["time_utc", "open", "high", "low", "close", "spread"])
        for b in m1:
            w.writerow([b["t"].isoformat(), b["o"], b["h"], b["l"], b["c"], "" if b["s"] is None else b["s"]])


# ---------------------------------------------------------------------- trade simulation
@dataclass
class Trade:
    cid: str
    variant: str
    direction: str
    order_type: str
    created: datetime
    entry_level: float
    stop: float
    tp1: float
    tp2: float | None
    expiry: datetime
    trade_class: str
    confirmation: str
    zone: str
    session: str
    state: dict
    expansion_against: bool
    would_block: bool
    status: str = "PENDING"
    fill_time: datetime | None = None
    fill_price: float | None = None
    exit_time: datetime | None = None
    r: float = 0.0
    outcome: str = ""
    ambiguous: str = ""
    notes: list = field(default_factory=list)
    hold_until: datetime | None = None          # clock model: time exit at this instant instead of max_hold_hours

    def active_at(self, t: datetime) -> bool:
        return self.exit_time is None or self.exit_time > t

    def finished_at(self, t: datetime) -> bool:
        return self.exit_time is not None and self.exit_time <= t


class Simulator:
    """Walks 1-minute bars forward from a decision time. `ticks(start, end)` -> [(t, bid, ask)] is
    optional and only used to settle 1-minute bars where the stop and a target are both touched."""

    def __init__(self, m1: list, cfg: dict, ticks=None):
        self.m1, self.cfg, self.ticks = m1, cfg, ticks
        self.times = [b["t"] for b in m1]
        self.resolved = self.assumed = 0

    def _spread(self, b):
        return b["s"] if b["s"] is not None else self.cfg["spread"]

    def _first(self, b, buy, stop, target):
        """Order of two events inside one 1-minute bar using ticks: returns 'stop' | 'target' | None."""
        if not self.ticks:
            return None
        try:
            ticks = self.ticks(b["t"], b["t"] + timedelta(minutes=1))
        except Exception:  # noqa: BLE001
            return None
        if not ticks:
            return None
        sp = self._spread(b)
        if any(not (b["l"] - sp - 0.01 <= bid <= b["h"] + sp + 0.01) for _, bid, _ in ticks):
            return None                                  # ticks do not match this candle (clock mismatch)
        for _, bid, ask in ticks:
            px_stop = bid if buy else ask
            if (px_stop <= stop) if buy else (px_stop >= stop):
                return "stop"
            if (bid >= target) if buy else (ask <= target):
                return "target"
        return None

    def run(self, tr: Trade):
        """Simulate one order forward. Conservative wherever one minute hides the order of events:
        stop and target in one bar = stop (unless ticks say otherwise); a limit's fill bar can stop
        but never reach the target; TP1 and the limit in one bar = cancelled (live would usually
        cancel first); a gap through the stop exits at the open, not at the stop."""
        cfg = self.cfg
        buy = tr.direction == "BUY"
        i = bisect.bisect_left(self.times, tr.created)        # first bar opening at/after the decision
        cost = cfg["commission_per_lot"] / 100.0
        slip = cfg["slippage"]
        sl, tp1, tp2 = tr.stop, tr.tp1, tr.tp2
        entry = risk = hold_until = None
        half_done = False
        realised = 0.0
        for k in range(i, len(self.m1)):
            b = self.m1[k]
            sp = self._spread(b)
            ask_o, ask_h, ask_l = b["o"] + sp, b["h"] + sp, b["l"] + sp
            fill_bar = False
            if entry is None:
                if b["t"] >= tr.expiry:
                    tr.status, tr.outcome, tr.exit_time = "EXPIRED", "not filled", b["t"]
                    return tr
                if tr.order_type == "MARKET":
                    entry = (ask_o + slip) if buy else (b["o"] - slip)
                else:
                    # live cancels a BUY limit when ask > TP1 (cancel_above), a SELL when bid < TP1
                    cancel_hit = (ask_h > tp1) if buy else (b["l"] < tp1)
                    fill_hit = (ask_l <= tr.entry_level) if buy else (b["h"] >= tr.entry_level)
                    if cancel_hit:
                        tr.status, tr.exit_time = "CANCELLED", b["t"]
                        tr.outcome = "TP1 and entry in one minute (counted as cancelled)" if fill_hit \
                            else "TP1 before fill"
                        return tr
                    if not fill_hit:
                        continue
                    entry = min(tr.entry_level, ask_o) if buy else max(tr.entry_level, b["o"])
                    fill_bar = True
                if (buy and entry <= sl) or (not buy and entry >= sl):
                    # the broker refuses a stop on the wrong side of the price: no position
                    tr.status, tr.outcome, tr.exit_time = "CANCELLED", "gap through the stop before the fill", b["t"]
                    return tr
                tr.fill_time, tr.fill_price, tr.status = b["t"], round(entry, 3), "OPEN"
                risk = abs(entry - sl)
                hold_until = tr.hold_until or (b["t"] + timedelta(hours=cfg["max_hold_hours"]))
            # ---- position management on this bar
            stop_hit = (b["l"] <= sl) if buy else (ask_h >= sl)
            target = tp1 if not half_done else (tp2 if tp2 is not None else tp1)
            tgt_hit = False if fill_bar else ((b["h"] >= target) if buy else (ask_l <= target))
            if stop_hit and tgt_hit:
                first = self._first(b, buy, sl, target)
                if first is None:
                    self.assumed += 1
                    tr.ambiguous = "assumed stop first"
                    tgt_hit = False
                else:
                    self.resolved += 1
                    tr.ambiguous = f"ticks: {first} first"
                    if first == "stop":
                        tgt_hit = False
                    else:
                        stop_hit = False
            if stop_hit:
                gap = (b["o"] < sl) if buy else (ask_o > sl)
                px = ((b["o"] if gap else sl) - slip) if buy else ((ask_o if gap else sl) + slip)
                realised += (0.5 if half_done else 1.0) * ((px - entry) if buy else (entry - px))
                tr.outcome = ("breakeven after TP1" if abs(sl - entry) < 1e-9 else "stop after TP1") if half_done \
                    else ("stop (gap)" if gap else "stop")
                return self._close(tr, b["t"], realised, risk, cost)
            if tgt_hit:
                pnl = (target - entry) if buy else (entry - target)
                if not half_done and tp2 is not None:
                    realised += 0.5 * pnl
                    half_done = True
                    sl = entry                               # breakeven on the rest (live behaviour)
                    # the rest of this minute may come back to entry: count that (conservative)
                    if (b["l"] <= sl) if buy else (ask_h >= sl):
                        realised += 0.5 * ((sl - slip - entry) if buy else (entry - sl - slip))
                        tr.outcome = "breakeven after TP1"
                        return self._close(tr, b["t"], realised, risk, cost)
                    continue
                realised += (0.5 if half_done else 1.0) * pnl
                tr.outcome = "TP2" if half_done else "TP1"
                return self._close(tr, b["t"], realised, risk, cost)
            if b["t"] >= hold_until:
                px = b["c"] if buy else b["c"] + sp
                realised += (0.5 if half_done else 1.0) * ((px - entry) if buy else (entry - px))
                tr.outcome = "time exit"
                return self._close(tr, b["t"], realised, risk, cost)
        tr.outcome = "data ended"
        tr.exit_time = self.m1[-1]["t"] + timedelta(minutes=1)
        if entry is None:
            tr.status = "PENDING_AT_END"
            return tr
        last = self.m1[-1]
        px = last["c"] if buy else last["c"] + self._spread(last)
        realised += (0.5 if half_done else 1.0) * ((px - entry) if buy else (entry - px))
        return self._close(tr, tr.exit_time - timedelta(minutes=1), realised, risk, cost, status="OPEN_AT_END")

    @staticmethod
    def _close(tr, t, realised, risk, cost, status="CLOSED"):
        tr.status, tr.exit_time = status, t + timedelta(minutes=1)
        tr.r = round((realised - cost) / risk, 3)
        return tr


# ---------------------------------------------------------------------- candidates
def _zone_id(kind, tf, lo, hi):
    return f"{tf}:{kind}:{round((lo + hi) / 2)}"


def structure_zones(state: dict, atr15: float) -> list:
    """Usable 1H/4H order blocks / FVGs and 1H/4H swing levels, each with a direction."""
    out = []
    for tf in ("1h", "4h"):
        fr = state["_frames"].get(tf) or {}
        for ev in fr.get("events") or []:
            k, st = ev.get("kind"), ev.get("status")
            if (k == "ORDER_BLOCK_CANDIDATE" and st in ("ACTIVE", "TESTED")) or (k == "FVG" and st in ("ACTIVE", "PARTIALLY_FILLED")):
                lo, hi = ev["level_low"], ev["level_high"]
                if hi - lo > 0:
                    out.append({"id": _zone_id(k, tf, lo, hi), "direction": ev["direction"], "low": lo, "high": hi,
                                "source": "STRUCTURE"})
        for s in (fr.get("swings") or [])[-8:]:
            p = s["price"]
            if s["side"] == "HIGH":
                out.append({"id": _zone_id("SWING_H", tf, p, p), "direction": "SELL", "low": p - 0.5 * atr15, "high": p,
                            "source": "STRUCTURE"})
            else:
                out.append({"id": _zone_id("SWING_L", tf, p, p), "direction": "BUY", "low": p, "high": p + 0.5 * atr15,
                            "source": "STRUCTURE"})
    return out


def strong_zone(zone: dict, state: dict, cfg: dict) -> bool:
    """Replay stand-in for the signal bot's memory zones: >= counter_min_tests 4H/Daily pivots of
    the right side (lows for a BUY, highs for a SELL) within tolerance of the zone."""
    side = "LOW" if zone["direction"] == "BUY" else "HIGH"
    tol = cfg["counter_zone_tolerance"]
    n = 0
    for tf in ("4h", "1day"):
        for s in (state["_frames"].get(tf) or {}).get("swings") or []:
            if s["side"] == side and zone["low"] - tol <= s["price"] <= zone["high"] + tol:
                n += 1
    return n >= cfg["counter_min_tests"]


def levels_for_targets(state: dict, lvls: list) -> tuple[list, list]:
    highs, lows = [], []
    for tf in ("15min", "1h", "4h"):
        for s in (state["_frames"].get(tf) or {}).get("swings") or []:
            (highs if s["side"] == "HIGH" else lows).append(s["price"])
    for l in lvls:
        (highs if l["side"] == "HIGH" else lows).append(l["price"])
    return sorted(set(highs)), sorted(set(lows), reverse=True)


def targets(direction, entry, stop, highs, lows):
    risk = abs(entry - stop)
    if direction == "BUY":
        cand = [h for h in highs if h > entry + 0.5 * risk]
    else:
        cand = [l for l in lows if l < entry - 0.5 * risk]
    if not cand:
        return None, None
    tp1 = cand[0]
    nxt = [x for x in cand[1:] if abs(x - tp1) >= 0.5 * risk]
    return tp1, (nxt[0] if nxt else None)


def session_zone(e: dict, atr15: float):
    """A session event (sweep/reclaim or failed breakout) -> (zone, confirmation) or (None, None)."""
    if e["event"] not in ("SWEEP_RECLAIM", "FAILED_BREAKOUT"):
        return None, None
    p = e["price"]
    z = {"id": f"LEVEL:{e['level']}:{round(p)}", "direction": e["direction"], "source": "SESSION",
         "low": p - 0.25 * atr15, "high": p + 0.25 * atr15, "level": e["level"]}
    return z, ("SESSION_SWEEP" if e["event"] == "SWEEP_RECLAIM" else "SESSION_FAILED_BREAKOUT")


def bar_confirmation(direction, zone_lo, zone_hi, bar, choch_now: bool, recent: list):
    idea = {"direction": direction, "entry_low": zone_lo, "entry_high": zone_hi,
            "invalidation": zone_lo - 1e6 if direction == "BUY" else zone_hi + 1e6}
    since = _time(bar["closes"]) - timedelta(minutes=15) - timedelta(seconds=1)
    status, _, how = BR.confirmation(idea, [bar], since)
    if status == "confirmed":
        return how
    if choch_now and any(b["low"] <= zone_hi and b["high"] >= zone_lo for b in recent):
        return "CHOCH"
    return None


def build_candidate(z, how, bar, state, atr15, highs, lows, as_of, session, sp, cfg, drop=lambda why: None):
    """One confirmed zone -> an order plan (entry type, stop, TP1/TP2) or None (reason to `drop`).
    Used by the replay for every candidate and live for Python's own session-sweep setups."""
    buy = z["direction"] == "BUY"
    pad = max(0.1 * atr15, 2 * sp)
    stop = round((min(z["low"], bar["low"]) - pad) if buy else (max(z["high"], bar["high"]) + pad), 2)
    price = bar["close"] + (sp if buy else 0.0)
    if (buy and price <= stop) or (not buy and price >= stop):
        return drop("price beyond stop")
    dist = abs(price - stop)
    if not cfg["sl_atr_min"] * atr15 <= dist <= cfg["sl_atr_max"] * atr15:
        if dist < cfg["sl_atr_min"] * atr15:
            stop = round(price - cfg["sl_atr_min"] * atr15 if buy else price + cfg["sl_atr_min"] * atr15, 2)
        else:
            return drop("stop wider than sl_atr_max")
    tp1, tp2 = targets(z["direction"], price, stop, highs, lows)
    if tp1 is None:
        return drop("no opposing level for TP1")
    r = cfg["min_rr"] + 0.02
    rr_now = abs(tp1 - price) / abs(price - stop)
    if rr_now >= r:
        otype, entry, expiry = "MARKET", price, as_of + timedelta(minutes=30)
    else:
        entry = round((tp1 + r * stop) / (1 + r), 2)
        if (buy and not stop < entry < price) or (not buy and not price < entry < stop):
            return drop("2R not available")
        otype, expiry = ("BUY_LIMIT" if buy else "SELL_LIMIT"), as_of + timedelta(hours=cfg["retest_hours"])
    if sp > cfg["max_spread"] or sp > cfg["spread_stop_fraction"] * abs(entry - stop):
        return drop("spread too large for the stop")
    return {"cid": f"{as_of.strftime('%Y%m%d%H%M')}:{z['id']}:{z['direction']}:{how}", "zone": z, "how": how,
            "direction": z["direction"], "order_type": otype, "entry": entry, "stop": stop, "tp1": tp1,
            "tp2": tp2, "expiry": expiry, "created": as_of, "session": session, "atr15": atr15, "price": price,
            "strong": strong_zone(z, state, cfg) if z["source"] == "STRUCTURE" else False}


# ---------------------------------------------------------------------- the replay
class Replay:
    def __init__(self, m1: list, cfg: dict | None = None, ticks=None, sessions_cfg=None, market_hours=None,
                 progress=None):
        self.cfg = dict(DEFAULTS, **(cfg or {}))
        for b in m1:
            if b["s"] is None:
                b["s"] = self.cfg["spread"]
        self.m1 = m1
        rule = self.cfg["server_offset"]
        self.bars = {tf: aggregate(m1, tf, rule) for tf in TF_MIN}
        self.closes = {tf: [_time(b["closes"]) for b in self.bars[tf]] for tf in TF_MIN}
        self.sim = Simulator(m1, self.cfg, ticks)
        self.sessions_cfg = sessions_cfg
        self.market_hours = market_hours          # callable(datetime) -> bool (V6 Sessions.is_open)
        self.progress = progress
        self.trades: dict[str, list[Trade]] = {v: [] for v in list(VARIANTS) + list(IM.VARIANTS) + list(CK.VARIANTS)}
        self.candidates: list[dict] = []
        self.funnel: dict[str, int] = {}
        # ICT model (30 Oct): its own simulator (shorter hold), funnel and setup list; same caps and loss limits
        self.ict_cfg = dict(IM.DEFAULTS, **(self.cfg.get("ict") or {}), sl_atr_max=self.cfg["sl_atr_max"],
                            max_spread=self.cfg["max_spread"])
        self.sim_ict = Simulator(m1, dict(self.cfg, max_hold_hours=self.ict_cfg["max_hold_hours"]), ticks)
        self.ict_funnel: dict[str, int] = {}
        self.ict_setups: list[dict] = []
        self.macro_timeline: tuple[list, list] = ([], [])        # (15M close UTC, macro_trend) for the scalp model
        self.scalp: dict = {}

    def _drop(self, why):
        self.funnel[why] = self.funnel.get(why, 0) + 1
        return None

    def _window(self, tf, as_of, n):
        k = bisect.bisect_right(self.closes[tf], as_of)
        return self.bars[tf][max(0, k - n):k]

    def tradable(self, t):
        if self.market_hours is None:
            return True
        c = self.cfg
        return (self.market_hours(t) and self.market_hours(t - timedelta(minutes=c["skip_after_open_min"]))
                and self.market_hours(t + timedelta(minutes=c["skip_before_close_min"])))

    def run(self) -> "Replay":
        cfg = self.cfg
        win = cfg["regime"].get("regime_window", RG.DEFAULTS["regime_window"])
        cache: dict = {}
        b15 = self.bars["15min"]
        start = 0
        while start < len(b15):                   # warm-up: 30 daily, 60 4H, 120 1H candles closed
            t = _time(b15[start]["closes"])
            if all(bisect.bisect_right(self.closes[tf], t) >= n for tf, n in (("1day", 30), ("4h", 60), ("1h", 120))):
                break
            start += 1
        self.start_index = start
        total = len(b15) - start
        for idx in range(start, len(b15)):
            bar = b15[idx]
            as_of = _time(bar["closes"])
            if self.progress and (idx - start) % 500 == 0:
                self.progress(idx - start, total)
            hist = {tf: self._window(tf, as_of, win) for tf in ("1day", "4h", "1h", "15min")}
            frames = {}
            for tf in ("1day", "4h", "1h"):
                key = (tf, hist[tf][-1]["time"] if hist[tf] else None)
                if key not in cache:
                    cache.clear() if len(cache) > 50 else None
                    cache[key] = RG.frame(hist[tf], tf, as_of, win)
                frames[tf] = cache[key]
            state = RG.market_state(hist, as_of, cfg["regime"], frames=frames)
            self.macro_timeline[0].append(as_of)
            self.macro_timeline[1].append(state.get("macro_trend"))
            atr15 = state.get("atr15")
            if not atr15 or not self.tradable(as_of):
                continue
            lv = SS.levels(hist["15min"][-200:], hist["1h"][-240:], as_of, self.sessions_cfg)
            sevents = SS.events(self._window("15min", as_of, 700), lv, as_of)   # same depth as live
            session = SS.session_of(as_of - timedelta(minutes=1), self.sessions_cfg)
            for cand in self._candidates(bar, hist["15min"], state, lv, sevents, atr15, as_of, session):
                self.candidates.append(cand)
                for v in VARIANTS:
                    self._consider(v, cand, state, hist["15min"], lv, as_of)
            self._ict_step(state, lv, as_of)
            self._clock_step(bar, state, as_of, session)
        if self.progress:
            self.progress(total, total)
        # 30 Oct: the 1-minute scalp model runs over the whole series afterwards (it needs only M1 + the macro timeline)
        self.scalp = SC.run_all(self.m1, {"slippage": cfg["slippage"], "spread": cfg["spread"]}, self.macro_timeline,
                                self.sessions_cfg)
        return self

    def _candidates(self, bar, bars15, state, lv, sevents, atr15, as_of, session):
        out = []
        recent = bars15[-4:]
        chochs = {ev["direction"] for ev in (state["_frames"]["15min"].get("events") or [])
                  if ev.get("kind") == "CHOCH" and ev.get("at") == bar["closes"]}
        highs, lows = levels_for_targets(state, lv)
        sp = bar.get("spread") or self.cfg["spread"]
        for z in structure_zones(state, atr15):
            if not (bar["low"] <= z["high"] + 0.25 * atr15 and bar["high"] >= z["low"] - 0.25 * atr15) and \
                    z["direction"] not in chochs:
                continue
            how = bar_confirmation(z["direction"], z["low"], z["high"], bar, z["direction"] in chochs, recent)
            if how:
                c = self._build(z, how, bar, state, atr15, highs, lows, as_of, session, sp)
                if c:
                    out.append(c)
        for e in sevents:
            z, how = session_zone(e, atr15)
            if z is None:
                if e.get("event") == "BREAKOUT":                         # 1.8
                    c = BK.candidate(e, bar, state, atr15, highs, lows, as_of, session, sp, self.cfg, self._drop)
                    if c:
                        out.append(c)
                continue
            c = self._build(z, how, bar, state, atr15, highs, lows, as_of, session, sp)
            if c:
                out.append(c)
        return out

    def _build(self, z, how, bar, state, atr15, highs, lows, as_of, session, sp):
        return build_candidate(z, how, bar, state, atr15, highs, lows, as_of, session, sp, self.cfg, self._drop)

    def _consider(self, v, cand, state, bars15, lv, as_of):
        use_struct, use_session, modes, exp_mode, use_choch = VARIANTS[v][:5]
        sessions_ok, confs_ok, gate, var_min_rr = (VARIANTS[v] + (None, None, None, None))[5:9]
        if sessions_ok and cand["session"] not in sessions_ok:
            return
        if confs_ok and cand["how"] not in confs_ok:
            return
        src = cand["zone"]["source"]
        if (src == "STRUCTURE" and not use_struct) or (src == "SESSION" and not use_session) or \
                (cand["how"] == "CHOCH" and not use_choch):
            return
        if var_min_rr is not None and src != "BREAKOUT":
            cand = market_at_close(cand, var_min_rr)
            if cand is None:
                if v == "HIGH_WINRATE":
                    self._drop(f"high-win-rate: nearest level under {var_min_rr}R from the close")
                return
        if src == "BREAKOUT":                                             # 1.8: no playbook decision needed
            if modes.get("BREAKOUT_CONTINUATION") != "live":
                return
            dec = {"trade_class": "BREAKOUT_CONTINUATION", "mode": "live", "would_block": False,
                   "expansion": {"against": False}}
        else:
            dec = PB.decide(cand["direction"], state, bars15, strong_zone=cand["strong"], confirmation=cand["how"],
                            modes=modes, expansion_mode=exp_mode, levels=lv)
        blocked = regime_gate_blocks(gate, dec["trade_class"], state.get("macro_trend"))
        if blocked:
            if v == "CORE_regimeGated":
                self._drop(blocked)
            return
        if dec["mode"] != "live":
            if v == "OLD_v1.5":
                self._drop("OLD rules: against 4H/1H, no strong zone" if dec["trade_class"] == "NONE"
                           else f"OLD rules: no {dec['trade_class']} playbook")
            elif v == "NEW_all":
                self._drop("NEW rules: blocked by the expansion filter" if dec["would_block"] and dec["trade_class"] != "NONE"
                           else "NEW rules: against 4H/1H, no justification")
            return
        cfg = self.cfg
        mine = self.trades[v]
        active = [t for t in mine if t.active_at(as_of)]
        if len(active) >= cfg["max_active"]:
            return
        day = SS.trading_day(as_of)
        if sum(1 for t in mine if SS.trading_day(t.created) == day) >= cfg["max_trades_per_day"]:
            return
        zone_today = [t for t in mine if t.zone == cand["zone"]["id"] and SS.trading_day(t.created) == day]
        if len(zone_today) >= cfg["max_per_zone_per_day"] or any(t.active_at(as_of) for t in zone_today):
            return
        tol = cfg["duplicate_atr"] * cand["atr15"]
        if any(t.direction == cand["direction"] and abs(t.entry_level - cand["entry"]) <= tol
               and abs(t.stop - cand["stop"]) <= tol for t in active):
            return
        done = [t for t in mine if t.finished_at(as_of) and t.fill_time is not None]
        d_r = sum(t.r for t in done if SS.trading_day(t.exit_time) == day)
        week = day.isocalendar()[:2]
        w_r = sum(t.r for t in done if SS.trading_day(t.exit_time).isocalendar()[:2] == week)
        if d_r * cfg["risk_pct"] <= -cfg["daily_loss_pct"] or w_r * cfg["risk_pct"] <= -cfg["weekly_loss_pct"]:
            return
        tr = Trade(cid=cand["cid"], variant=v, direction=cand["direction"], order_type=cand["order_type"],
                   created=as_of, entry_level=cand["entry"], stop=cand["stop"], tp1=cand["tp1"], tp2=cand["tp2"],
                   expiry=cand["expiry"], trade_class=dec["trade_class"], confirmation=cand["how"],
                   zone=cand["zone"]["id"], session=cand["session"], state=RG.public(state),
                   expansion_against=dec["expansion"]["against"], would_block=dec["would_block"])
        self.sim.run(tr)
        mine.append(tr)

    # ------------------------------------------------------------------ ICT model (30 Oct 2026)
    def _ict_drop(self, why):
        self.ict_funnel[why] = self.ict_funnel.get(why, 0) + 1
        return None

    def _ict_step(self, state, lv, as_of):
        """Evaluate the ICT model at every 5M close in (as_of, as_of + 15 min]. The HTF state and the session levels
        come from the 15M close at `as_of` (older data), the 5M window ends at the 5M close: no look-ahead."""
        c = self.ict_cfg
        closes5 = self.closes["5min"]
        k0 = bisect.bisect_right(closes5, as_of)
        k1 = bisect.bisect_right(closes5, as_of + timedelta(minutes=15))
        atr15 = state["atr15"]
        wanted = {IM.bias_of(state, "macro"), IM.bias_of(state, "4h")} - {None}
        if not wanted:
            return
        for k in range(k0, k1):
            t5 = closes5[k]
            if not self.tradable(t5):
                continue
            bars5 = self.bars["5min"][max(0, k + 1 - 300):k + 1]
            sp = bars5[-1].get("spread") or self.cfg["spread"]
            pre: dict = {}                                        # swings and pools computed once per 5M close
            for d in sorted(wanted):
                for pf in ("all", "htf"):
                    st = IM.setup(bars5, dict(state, _ict_direction=d), lv, t5, sp, atr15, c, self._ict_drop, pf, pre)
                    if not st:
                        continue
                    st["state"] = RG.public(state)
                    self.ict_setups.append(st)
                    for v, spec in IM.VARIANTS.items():
                        if IM.bias_of(state, spec["bias"]) != d or spec.get("pools", "all") != pf:
                            continue
                        self._consider_ict(v, spec, st, t5)

    def _consider_ict(self, v, spec, st, t5):
        base = v in ("ICT_2R", "ICT_2R_4Hbias")                # the funnel counts these two only
        drop = (lambda why: self._ict_drop(f"[{v}] {why}")) if base else (lambda why: None)
        if spec["killzones"] and st["killzone"] is None:
            return drop("ict: outside the killzones")
        pl = IM.plan(st, spec, self.ict_cfg, drop)
        if not pl:
            return
        if base and not pl["live_gate_spread_ok"]:
            self._ict_drop(f"[{v}] info: the LIVE gate's 5% spread/stop rule would refuse this one")
        cfg = self.cfg
        mine = self.trades[v]
        active = [t for t in mine if t.active_at(t5)]
        if len(active) >= cfg["max_active"]:
            return drop("ict: max active orders")
        day = SS.trading_day(t5)
        if sum(1 for t in mine if SS.trading_day(t.created) == day) >= cfg["max_trades_per_day"]:
            return drop("ict: max trades per day")
        sweep_key = f"SWEEP:{st['sweep_time']}:{st['sweep']['pool'][1]}"
        if any(t.zone == sweep_key for t in mine):
            return drop("ict: this sweep already traded")
        bucket = st["killzone"] or SS.session_of(t5 - timedelta(minutes=1), self.sessions_cfg)
        if any(t.direction == st["direction"] and t.session == bucket and SS.trading_day(t.created) == day for t in mine):
            return drop("ict: one trade per killzone per direction")
        tol = cfg["duplicate_atr"] * st["atr15"]
        if any(t.direction == st["direction"] and abs(t.entry_level - pl["entry"]) <= tol
               and abs(t.stop - pl["stop"]) <= tol for t in active):
            return drop("ict: duplicate of a working order")
        done = [t for t in mine if t.finished_at(t5) and t.fill_time is not None]
        d_r = sum(t.r for t in done if SS.trading_day(t.exit_time) == day)
        week = day.isocalendar()[:2]
        w_r = sum(t.r for t in done if SS.trading_day(t.exit_time).isocalendar()[:2] == week)
        if d_r * cfg["risk_pct"] <= -cfg["daily_loss_pct"] or w_r * cfg["risk_pct"] <= -cfg["weekly_loss_pct"]:
            return drop("ict: daily/weekly loss limit")
        tr = Trade(cid=st["cid"] + ":" + v, variant=v, direction=st["direction"], order_type=pl["order_type"],
                   created=t5, entry_level=pl["entry"], stop=pl["stop"], tp1=pl["tp1"], tp2=pl["tp2"],
                   expiry=pl["expiry"], trade_class="ICT_SWEEP_MSS", confirmation=st["sweep"]["pool"][1],
                   zone=sweep_key, session=bucket, state=st["state"], expansion_against=False, would_block=False,
                   notes=[f"rr {pl['rr']}", f"risk ${pl['risk']}", f"draw {pl['draw_label']} {pl['draw']}",
                          f"fvg {st['shift']['fvg_low']}-{st['shift']['fvg_high']}", f"hour {IM.hour_bucket(t5)}"])
        self.sim_ict.run(tr)
        mine.append(tr)


    # ------------------------------------------------------------------ clock model (30 Oct 2026)
    def _clock_step(self, bar, state, as_of, session):
        """One MARKET order per clock variant per trading day at the first 15M close at/after its entry time."""
        atr15 = state["atr15"]
        sp = bar.get("spread") or self.cfg["spread"]
        if sp > self.cfg["max_spread"]:
            return
        for v in CK.VARIANTS:
            exit_at = CK.due(v, as_of)
            if exit_at is None:
                continue
            mine = self.trades[v]
            day = SS.trading_day(as_of)
            if any(SS.trading_day(t.created) == day for t in mine):
                continue
            pl = CK.plan(v, bar["close"], sp, atr15)
            tr = Trade(cid=f"CLOCK:{as_of:%Y%m%d%H%M}:{v}", variant=v, direction=pl["direction"], order_type="MARKET",
                       created=as_of, entry_level=pl["entry"], stop=pl["stop"], tp1=pl["tp1"], tp2=None,
                       expiry=as_of + timedelta(minutes=30), trade_class="CLOCK", confirmation="TIME",
                       zone=v, session=session, state=RG.public(state), expansion_against=False, would_block=False,
                       notes=[f"risk ${pl['risk']}", f"exit {exit_at:%H:%M}Z", f"weekday {as_of.astimezone(CK.NY):%a}"],
                       hold_until=exit_at)
            self.sim.run(tr)
            mine.append(tr)


# ---------------------------------------------------------------------- statistics and report
def stats(trades: list[Trade]) -> dict:
    filled = [t for t in trades if t.fill_time is not None]
    rs = [t.r for t in filled]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]
    eq = peak = dd = 0.0
    for t in sorted(filled, key=lambda x: x.exit_time):
        eq += t.r
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return {"orders": len(trades), "trades": len(filled), "win_rate": (len(wins) / len(rs) * 100) if rs else None,
            "avg_r": (sum(rs) / len(rs)) if rs else None, "net_r": sum(rs),
            "profit_factor": (sum(wins) / -sum(losses)) if losses else (math.inf if wins else None),
            "max_dd_r": dd, "ambiguous_assumed_loss": sum(1 for t in filled if t.ambiguous == "assumed stop first")}


def _fmt(s, risk_pct):
    f = lambda x, d=2: "-" if x is None else ("inf" if x == math.inf else f"{x:.{d}f}")
    return (f"{s['trades']:>4} | {f(s['win_rate'], 1):>5}% | {f(s['avg_r']):>6} | {f(s['net_r']):>7} | "
            f"{f(s['net_r'] * risk_pct if s['trades'] else None):>6}% | {f(s['profit_factor']):>5} | "
            f"{f(s['max_dd_r']):>6} | {s['ambiguous_assumed_loss']:>3}")


HEAD = "trades | win % | avg R | net R | net % | PF | max DD R | amb."


def report(rp: Replay, title: str = "") -> str:
    cfg = rp.cfg
    b15 = rp.bars["15min"]
    if rp.start_index >= len(b15):
        return "# Replay\n\nNot enough history: need about 30 trading days of 1-minute candles (warm-up)."
    t0, t1 = _time(b15[rp.start_index]["time"]), _time(b15[-1]["closes"])
    split = t0 + (t1 - t0) * (1 - cfg["out_of_sample"])
    L = [f"# Historical replay: old rules vs new rules{(' - ' + title) if title else ''}", "",
         f"Period {t0:%Y-%m-%d} -> {t1:%Y-%m-%d} (after warm-up) | {len(rp.m1):,} one-minute bars | "
         f"{len(rp.candidates)} candidate setups seen by every variant",
         f"Costs: spread from data (fallback {cfg['spread']} $), slippage {cfg['slippage']} $ on market entries and "
         f"stops, commission {cfg['commission_per_lot']} $/lot | risk {cfg['risk_pct']}% per trade | "
         f"min R:R {cfg['min_rr']} | out-of-sample = last {int(cfg['out_of_sample'] * 100)}% (from {split:%Y-%m-%d})",
         f"Same-minute stop+target: {rp.sim.resolved} settled by ticks, {rp.sim.assumed} counted as LOSS (no ticks)",
         "Candidate funnel (why setups did not become orders): " + (", ".join(
             f"{k} {v}" for k, v in sorted(rp.funnel.items(), key=lambda x: -x[1])) or "-"),
         "", "## Summary per variant", "",
         "| variant | " + HEAD.replace(" | ", " | ") + " |", "|" + "---|" * 10]
    for v in VARIANTS:
        L.append(f"| {v} | " + _fmt(stats(rp.trades[v]), cfg["risk_pct"]).replace(" | ", " | ") + " |")
    L += ["", "Variants: OLD_v1.5 = 4H/1H veto + strong-zone counter-trend, REJECTION/SWEEP confirmations (the "
          "live v1.5 rules) | NEW_filter_only = OLD + expansion filter | NEW_playbooks_no_filter = OLD + CHoCH "
          "confirmation (1.6 fix) + REVERSAL_TRANSITION + SESSION_SWEEP (session-level candidates) | NEW_all = "
          "everything. | CORE = TREND + COUNTER_TREND + BREAKOUT, filter live, all sessions | CORE_noRejection = CORE "
          "without REJECTION confirmations | CORE_regimeGated = CORE with TREND pullbacks only in a MIXED macro "
          "(breakouts carry the trend) | CORE_regimeGated_noRejection = both. | HIGH_WINRATE* = market entry at the "
          "confirmation close, TP1 = nearest level (>= 0.5R), no 2R floor, no REJECTION, filter live.", ""]

    L += ["## In-sample vs out-of-sample", "", "| variant | part | " + HEAD + " |", "|" + "---|" * 11]
    for v in VARIANTS:
        for name, sel in (("in", lambda t: t.created < split), ("out", lambda t: t.created >= split)):
            L.append(f"| {v} | {name} | " + _fmt(stats([t for t in rp.trades[v] if sel(t)]), cfg["risk_pct"]) + " |")

    def breakdown(v, label, key):
        groups: dict = {}
        for t in rp.trades[v]:
            groups.setdefault(key(t), []).append(t)
        out = [f"### {v} by {label}", "", f"| {label} | " + HEAD + " |", "|" + "---|" * 9]
        for g in sorted(groups, key=str):
            out.append(f"| {g} | " + _fmt(stats(groups[g]), cfg["risk_pct"]) + " |")
        return out + [""]

    L += ["", "## Breakdowns", ""]
    for v in ("OLD_v1.5", "NEW_all", "CORE", "CORE_regimeGated_noRejection", "HIGH_WINRATE", "HIGH_WINRATE_counter_only"):
        L += breakdown(v, "trade class", lambda t: t.trade_class)
        L += breakdown(v, "trade class x macro (MIXED = Daily and 4H disagree)",
                       lambda t: f"{t.trade_class} / {t.state.get('macro_trend')}")
        L += breakdown(v, "confirmation", lambda t: t.confirmation)
        L += breakdown(v, "direction", lambda t: t.direction)
        L += breakdown(v, "session", lambda t: t.session)
        L += breakdown(v, "market phase", lambda t: t.state.get("phase"))
        L += breakdown(v, "volatility", lambda t: t.state.get("volatility"))
        L += breakdown(v, "momentum", lambda t: t.state.get("momentum"))
        L += breakdown(v, "against an active expansion", lambda t: "yes" if t.expansion_against else "no")

    old = {t.cid: t for t in rp.trades["OLD_v1.5"]}
    for v in ("NEW_filter_only", "NEW_all", "CORE_regimeGated_noRejection"):
        new = {t.cid: t for t in rp.trades[v]}
        removed = [old[c] for c in old if c not in new and old[c].fill_time is not None]
        added = [new[c] for c in new if c not in old and new[c].fill_time is not None]
        rw = [t for t in removed if t.r > 0]
        rl = [t for t in removed if t.r < 0]
        L += [f"## Trade-off: OLD_v1.5 -> {v}", "",
              f"Removed {len(removed)} filled OLD trades: {len(rw)} winners ({sum(t.r for t in rw):+.2f} R) and "
              f"{len(rl)} losers ({sum(t.r for t in rl):+.2f} R) -> net effect of removing them "
              f"{-sum(t.r for t in removed):+.2f} R.",
              f"Added {len(added)} trades the OLD rules did not take: {sum(t.r for t in added):+.2f} R "
              f"({sum(1 for t in added if t.r > 0)} winners)."]
        by: dict = {}
        for t in added:
            by.setdefault(t.trade_class, []).append(t.r)
        for k, rs in sorted(by.items()):
            L.append(f"  added {k}: {len(rs)} trades, {sum(rs):+.2f} R")
        byc: dict = {}
        for t in added:
            byc.setdefault(t.confirmation, []).append(t.r)
        if byc:
            L.append("  added by confirmation: " + ", ".join(f"{k} {len(rs)} ({sum(rs):+.2f} R)"
                                                          for k, rs in sorted(byc.items())))
        exp_removed = [t for t in removed if t.expansion_against]
        if exp_removed:
            L.append(f"Removed while fading an active expansion: {len(exp_removed)} trades, "
                     f"{sum(t.r for t in exp_removed):+.2f} R (these are the trades the expansion filter exists for).")
        L.append("(Slots are shared: removing a trade can let a later one in, so totals are not a simple sum.)")
        L.append("")
    # ---- ICT model (30 Oct)
    weeks = max(1e-9, (t1 - t0).days / 7)
    L += ["## ICT model: liquidity sweep -> market structure shift (displacement + FVG) -> FVG entry", "",
          f"Setups found (both directions, before variant filters): {sum(1 for s_ in rp.ict_setups if s_['pool_filter'] == 'all')} "
          f"(all pools) / {sum(1 for s_ in rp.ict_setups if s_['pool_filter'] == 'htf')} (HTF pools) | 5M decisions; HTF state from "
          f"the previous 15M close | hold max {rp.ict_cfg['max_hold_hours']} h | limit expiry "
          f"{rp.ict_cfg['expiry_minutes']} min | replay spread cap {int(rp.ict_cfg['spread_stop_fraction'] * 100)}% of "
          f"the stop (live gate: 5%) | same-minute stop+target: {rp.sim_ict.resolved} by ticks, {rp.sim_ict.assumed} "
          f"counted as LOSS",
          "Funnel (ICT_2R and ICT_2R_4Hbias): " + (", ".join(f"{k} {v}" for k, v in sorted(rp.ict_funnel.items(),
                                                                                       key=lambda x: -x[1])) or "-"),
          "", "| variant | bias | killzones | pools | entry | target | " + HEAD + " | trades/week |", "|" + "---|" * 15]
    for v, spec in IM.VARIANTS.items():
        s_ = stats(rp.trades[v])
        L.append(f"| {v} | {spec['bias']} | {'yes' if spec['killzones'] else 'no'} | {spec.get('pools', 'all')} | "
                 f"{spec['entry']} | {spec['target']} | " + _fmt(s_, cfg["risk_pct"]) + f" | {s_['trades'] / weeks:.1f} |")
    L += ["", "### ICT in-sample vs out-of-sample", "", "| variant | part | " + HEAD + " |", "|" + "---|" * 11]
    for v in IM.VARIANTS:
        for name, sel in (("in", lambda t: t.created < split), ("out", lambda t: t.created >= split)):
            L.append(f"| {v} | {name} | " + _fmt(stats([t for t in rp.trades[v] if sel(t)]), cfg["risk_pct"]) + " |")
    L.append("")
    for v in ("ICT_2R", "ICT_1R", "ICT_2R_4Hbias", "ICT_1R_4Hbias_allHours", "ICT_2R_htfSweep", "ICT_2R_marketEntry_4Hbias",
              "ICT_1R_marketEntry_4Hbias_allHours"):
        L += breakdown(v, "killzone / session", lambda t: t.session)
        L += breakdown(v, "direction", lambda t: t.direction)
        L += breakdown(v, "macro", lambda t: t.state.get("macro_trend"))
        L += breakdown(v, "swept pool", lambda t: t.confirmation)
        L += breakdown(v, "order type", lambda t: t.order_type)
        L += breakdown(v, "outcome", lambda t: t.outcome)
        L += breakdown(v, "hour (New York)", lambda t: next((n[5:] for n in t.notes if n.startswith("hour ")), "?"))
        L += breakdown(v, "year", lambda t: t.created.year)
    L += ["Reading the ICT table: the fixed-R rows show the win-rate / expectancy trade-off of the SAME entries; "
          "`liq_then_draw` is the live trader's partial + breakeven management; `draw` holds for the HTF target. "
          "`4Hbias` and `allHours` relax rules 1 and 9. Below ~30 trades a row is noise.", ""]
    # ---- clock model (30 Oct)
    L += ["## Clock model: gold's intraday seasonality (overnight long / day short), time in, time out", "",
          "MARKET at the first 15M close at/after the entry time (New York), exit at the exit time, protective stop at "
          "k x ATR15 (the R unit; 6 x ATR = practically no stop), no target, one trade per variant per day. No indicators, "
          "no AI. Spread above max_spread at the entry close skips the day.",
          "", "| variant | side | entry NY | exit NY | stop ATR | " + HEAD + " | trades/week |", "|" + "---|" * 14]
    for v, (d_, t_in, t_out, nd, k) in CK.VARIANTS.items():
        s_ = stats(rp.trades[v])
        L.append(f"| {v} | {d_} | {t_in:%H:%M} | {t_out:%H:%M}{'+1' if nd else ''} | {k} | " + _fmt(s_, cfg["risk_pct"])
                 + f" | {s_['trades'] / weeks:.1f} |")
    L += ["", "### Clock in-sample vs out-of-sample", "", "| variant | part | " + HEAD + " |", "|" + "---|" * 11]
    for v in CK.VARIANTS:
        for name, sel in (("in", lambda t: t.created < split), ("out", lambda t: t.created >= split)):
            L.append(f"| {v} | {name} | " + _fmt(stats([t for t in rp.trades[v] if sel(t)]), cfg["risk_pct"]) + " |")
    L.append("")
    for v in ("CLOCK_long_1845_0530", "CLOCK_long_1845_0530_nostop", "CLOCK_short_0800_1100", "CLOCK_short_0800_1100_nostop"):
        L += breakdown(v, "year", lambda t: t.created.year)
        L += breakdown(v, "weekday (NY)", lambda t: next((n[8:] for n in t.notes if n.startswith("weekday ")), "?"))
        L += breakdown(v, "macro", lambda t: t.state.get("macro_trend"))
        L += breakdown(v, "outcome", lambda t: t.outcome)
    L += ["Reading the clock table: avg R is in units of the k x ATR15 stop, so compare rows with the SAME k; "
          "`nostop` rows show the raw time-of-day drift. A real edge here is a win rate of 53-56% with positive avg R in "
          "BOTH halves and in both years; costs (spread at the Asia open) are included.", ""]
    if rp.scalp:
        L += SC.report(rp.scalp, split)
    L += ["## What this replay does NOT prove", "",
          "- Candidates are Python's zones, not Astra's ideas (past Opus analyses cannot be replayed without paying "
          "for them). The comparison is rules-vs-rules on the same candidates.",
          "- Counter-trend 'strong zones' are approximated by repeated 4H/Daily pivots; live uses the signal bot's memory.",
          "- No news lock (the calendar export has no history). Results include news spikes the live bot would skip.",
          "- 1-minute candles cannot order events inside one minute; ambiguous minutes are losses unless ticks settle them.",
          "- Small samples: a variant with fewer than ~30 trades per class says little. Treat results as screening.", ""]
    return "\n".join(L)


def trades_csv(rp: Replay, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["variant", "created_utc", "direction", "type", "entry", "stop", "tp1", "tp2", "fill_utc", "fill",
                    "exit_utc", "status", "outcome", "R", "trade_class", "confirmation", "zone", "session", "macro",
                    "trend_4h", "trend_1h", "local", "phase", "phase_dir", "volatility", "momentum", "mom_dir",
                    "expansion_active", "expansion_dir", "against_expansion", "filter_would_block", "ambiguous", "notes"])
        for v, ts in rp.trades.items():
            for t in ts:
                s = t.state
                w.writerow([v, t.created.isoformat(), t.direction, t.order_type, t.entry_level, t.stop, t.tp1, t.tp2,
                            t.fill_time.isoformat() if t.fill_time else "", t.fill_price or "",
                            t.exit_time.isoformat() if t.exit_time else "", t.status, t.outcome, t.r, t.trade_class,
                            t.confirmation, t.zone, t.session, s.get("macro_trend"), s.get("trend_4h"),
                            s.get("intermediate_trend"), s.get("local_trend"), s.get("phase"),
                            s.get("phase_direction"), s.get("volatility"), s.get("momentum"),
                            s.get("momentum_direction"), (s.get("expansion") or {}).get("active"),
                            (s.get("expansion") or {}).get("direction"), t.expansion_against, t.would_block,
                            t.ambiguous, "; ".join(t.notes)])
