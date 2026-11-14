"""
1.12: SELL_4H_WEAKNESS in the lab - the user's 4H resistance strategy traded live on the demo account at the minimum lot.

Status (5 Nov 2026, tools/weakness_replay.py 0.4.0, three years of M1): NOT validated. No variant passed the bar fixed
before the run (BASE 19 trades -0.10 R; LOOSE 60 trades flips sign out of sample). The project's next evidence is a
forward log of the detector's own signals. This module is that log: every signal becomes a 0.01-lot lab order, tagged,
so it can be scored against the hand strategy and the replay. It never trades the live (swing) track.

The rules are the replay's spec, unchanged (functions copied from tools/weakness_replay.py; tests/test_weakness.py checks
that both give the same answers on the same candles):
  1. Resistance: >= min_run (3) closed 4H candles inside touch_window (8) with highs on one line (within
     equal_tol_atr 0.25 x ATR(4H)); no close above it and no trade through it; the line is a local high (>= the
     highest high of the prior 20 4H candles). Zone = [highest body top of the touching candles, line].
  2. Area of interest whose 50% lies in the zone (bottom - 0.25 ATR .. line): 1H -> 30M -> 15M -> 5M, and on each
     timeframe OB (with FVG) -> breaker -> unfilled FVG, formed after the first touch.
  3. Entry at the 50% level: when the bid reaches it and the last closed 5M structure is BEARISH, sell at market.
     Touched but 5M not bearish: the first later 5M close that is bearish AND below the level is a market sell.
  4. A 15M close above the level = wait; a 15M close back below re-arms. A 4H close above the level, the stop traded
     before the entry, or 12 4H candles without an entry end the setup.
  5. Stop = line + max(0.1 x ATR(4H), 2 x spread). TP1 = the first 1H swing low below the entry; under 2 R = no trade.
     Max hold 5 days (time exit through the executor's exit_utc).

Differences from the replay, on purpose:
  - 4H candles are the broker's (server-time aligned, the candles you see in MT5). The replay used UTC-aligned 4H
    candles; its own caveat named this as the spec correction to make.
  - Lab orders have one target: the whole 0.01 lot closes at TP1 (the replay modelled half at TP1, rest to TP2).
    TP2 is written in the order note so the scorecard can compare.
  - No entries inside the calendar news lock (the setup waits); the replay had no calendar.
  - SELL only by default (`sides`): the BUY mirror was 0/4 and -0.11 R in the replay.

Gates: before every order, everything Lab.hard_block() checks (kill file, demo only, terminal, market open, quote age,
spread cap, lab book size, lab_max_per_day, lab drawdown kill); a blocked entry keeps the setup and retries. Magic = magic_lab. Setup ids W4H-<SIDE>-<level>, strategy LAB_WEAKNESS.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import alerts
from . import news as NW
from . import bridge as BR
from . import sessions as SS

UTC = timezone.utc
LOG = logging.getLogger("autotrader.weakness")
PREFIX = "W4H-"

DEFAULTS = {
    "mode": "off",                 # off | lab (0.01-lot demo orders through the lab's gates)
    "sides": ["SELL"],             # add "BUY" for the mirror (replay: 0/4, -0.11 R)
    "min_run": 3,
    "touch_window": 8,
    "equal_tol_atr": 0.25,
    "zone_slack_atr": 0.25,
    "stop_atr": 0.1,
    "min_rr": 2.0,
    "max_setup_4h": 12,
    "max_hold_days": 5,
    "local_high_lookback": 20,
    "zone_types": ["OB", "BREAKER", "FVG"],
    "zone_tfs": [60, 30, 15, 5],
    "respect_news_lock": True,
}
TF_NAMES = {5: "TIMEFRAME_M5", 15: "TIMEFRAME_M15", 30: "TIMEFRAME_M30", 60: "TIMEFRAME_H1", 240: "TIMEFRAME_H4",
            1440: "TIMEFRAME_D1"}
COUNTS = {5: 1200, 15: 400, 30: 250, 60: 260, 240: 170, 1440: 120}


# ----------------------------------------------------------------------------- rules (as tools/weakness_replay.py)
@dataclass
class Bars:
    """Parallel lists of closed candles. t = candle OPEN time, UTC epoch seconds."""
    t: list
    o: list
    h: list
    l: list
    c: list
    spread: list = field(default_factory=list)

    def __len__(self):
        return len(self.t)


@dataclass
class Params:
    min_run: int = 3
    touch_window: int = 8
    consecutive: bool = False
    equal_tol_atr: float = 0.25
    zone_slack_atr: float = 0.25
    stop_atr: float = 0.1
    min_rr: float = 2.0
    max_setup_4h: int = 12
    local_high_lookback: int = 20
    ob_tfs: tuple = (60, 30, 15, 5)
    zone_types: tuple = ("OB", "BREAKER", "FVG")


def index_before(bars: Bars, ts: int) -> int:
    lo, hi = 0, len(bars.t)
    while lo < hi:
        mid = (lo + hi) // 2
        if bars.t[mid] <= ts:
            lo = mid + 1
        else:
            hi = mid
    return lo - 1


def atr(bars: Bars, idx: int, n: int = 14) -> float:
    if idx < 1:
        return 0.0
    start = max(1, idx - n + 1)
    trs = [max(bars.h[i] - bars.l[i], abs(bars.h[i] - bars.c[i - 1]), abs(bars.l[i] - bars.c[i - 1]))
           for i in range(start, idx + 1)]
    return sum(trs) / len(trs) if trs else 0.0


def structure_trend(bars: Bars, end: int, lookback: int = 60, k: int = 2) -> str:
    start = max(0, end - lookback + 1)
    last_sh = last_sl = None
    state = "NONE"
    for i in range(start, end + 1):
        p = i - k
        if p - k >= start:
            if all(bars.h[p] > bars.h[j] for j in range(p - k, p + k + 1) if j != p):
                last_sh = bars.h[p]
            if all(bars.l[p] < bars.l[j] for j in range(p - k, p + k + 1) if j != p):
                last_sl = bars.l[p]
        if last_sh is not None and bars.c[i] > last_sh:
            state, last_sh = "BULLISH", None
        elif last_sl is not None and bars.c[i] < last_sl:
            state, last_sl = "BEARISH", None
    return state


def swing_lows(bars: Bars, end: int, lookback: int = 200, k: int = 3) -> list:
    out = []
    start = max(0, end - lookback + 1)
    for p in range(start + k, end - k + 1):
        if all(bars.l[p] < bars.l[j] for j in range(p - k, p + k + 1) if j != p):
            out.append(bars.l[p])
    return out


def weakness_run(h4: Bars, end: int, p: Params):
    """(first_touch_idx, line, zone_bottom, atr) when candle `end` is the min_run-th touch of a 4H resistance line."""
    if end < 15:
        return None
    a = atr(h4, end)
    if a <= 0:
        return None
    tol = p.equal_tol_atr * a
    ref = h4.h[end]
    w0 = max(0, end - p.touch_window + 1)
    touches = []
    for j in range(end, w0 - 1, -1):
        if h4.h[j] > ref + tol:
            break
        if abs(h4.h[j] - ref) <= tol:
            touches.append(j)
        elif p.consecutive:
            break
    touches.sort()
    if len(touches) < p.min_run:
        return None
    first = touches[0]
    line = max(h4.h[j] for j in touches)
    if any(h4.c[j] > line for j in range(first, end + 1)):
        return None
    prior = h4.h[max(0, first - p.local_high_lookback):first]
    if prior and line < max(prior):
        return None
    zone_bottom = max(max(h4.c[j], h4.o[j]) for j in touches)
    return first, line, zone_bottom, a


def find_ob(tf: Bars, since_ts: int, until_ts: int, zone_lo: float, zone_hi: float):
    last = index_before(tf, until_ts)
    period = tf.t[1] - tf.t[0] if len(tf) > 1 else 60
    i = last - 2
    while i >= 0 and tf.t[i] >= since_ts:
        if tf.t[i + 2] + period > until_ts:
            i -= 1
            continue
        if tf.c[i] > tf.o[i] and tf.c[i + 1] < tf.l[i] and tf.l[i] > tf.h[i + 2]:
            level = (tf.h[i] + tf.l[i]) / 2.0
            if zone_lo <= level <= zone_hi:
                return level, tf.t[i]
        i -= 1
    return None


def find_breaker(tf: Bars, since_ts: int, until_ts: int, zone_lo: float, zone_hi: float):
    last = index_before(tf, until_ts)
    period = tf.t[1] - tf.t[0] if len(tf) > 1 else 60
    if last >= 0 and tf.t[last] + period > until_ts:
        last -= 1
    i = last - 2
    while i >= 0 and tf.t[i] >= since_ts:
        if tf.c[i] < tf.o[i] and tf.c[i + 1] > tf.h[i]:
            for j in range(i + 2, last + 1):
                if tf.c[j] < tf.l[i]:
                    level = (tf.h[i] + tf.l[i]) / 2.0
                    if zone_lo <= level <= zone_hi:
                        return level, tf.t[i]
                    break
        i -= 1
    return None


def find_fvg(tf: Bars, since_ts: int, until_ts: int, zone_lo: float, zone_hi: float):
    last = index_before(tf, until_ts)
    period = tf.t[1] - tf.t[0] if len(tf) > 1 else 60
    if last >= 0 and tf.t[last] + period > until_ts:
        last -= 1
    i = last - 2
    while i >= 0 and tf.t[i] >= since_ts:
        if tf.l[i] > tf.h[i + 2]:
            filled = any(tf.c[j] > tf.l[i] for j in range(i + 3, last + 1))
            level = (tf.l[i] + tf.h[i + 2]) / 2.0
            if not filled and zone_lo <= level <= zone_hi:
                return level, tf.t[i]
        i -= 1
    return None


ZONE_FINDERS = {"OB": find_ob, "BREAKER": find_breaker, "FVG": find_fvg}


def find_zone(frames: dict, p: Params, since_ts: int, until_ts: int, zone_lo: float, zone_hi: float):
    for tf in p.ob_tfs:
        for kind in p.zone_types:
            z = ZONE_FINDERS[kind](frames[tf], since_ts, until_ts, zone_lo, zone_hi)
            if z:
                return tf, kind, z[0]
    return None


def mirror(b: Bars) -> Bars:
    """The BUY side is the SELL logic on price-negated candles (as in the replay)."""
    return Bars(b.t, [-x for x in b.o], [-x for x in b.l], [-x for x in b.h], [-x for x in b.c], b.spread)


def macro_of(frames: dict, ts: int) -> str:
    d1, h4 = frames[1440], frames[240]
    kd, k4 = index_before(d1, ts - 1), index_before(h4, ts - 1)
    dtr = structure_trend(d1, kd, 100) if kd >= 0 else "NONE"
    htr = structure_trend(h4, k4, 100) if k4 >= 0 else "NONE"
    return dtr if dtr == htr and dtr != "NONE" else "MIXED"


def entry_plan(frames: dict, setup: dict, entry: float, spread: float, min_rr: float):
    """(tp1, tp2, rr) in sell space, or (None, None, why)."""
    risk = setup["stop"] - entry
    if risk <= 0:
        return None, None, "entry at or above the stop"
    h1 = frames[60]
    k1 = len(h1) - 1
    lows = [x for x in swing_lows(h1, k1) if x < entry - 2 * spread]
    if not lows:
        return None, None, "no 1H swing low below the entry"
    tp1 = max(lows)
    rr = (entry - tp1) / risk
    if rr < min_rr:
        return None, None, f"first support {tp1:.2f} is only {rr:.2f} R (< {min_rr})"
    lower = [x for x in lows if x < tp1]
    tp2 = max(lower) if lower else entry - 2 * (entry - tp1)
    return tp1, tp2, round(rr, 2)


# ----------------------------------------------------------------------------- the live engine
class WeaknessLab:
    def __init__(self, c: dict, app):
        self.c = c
        self.cfg = dict(DEFAULTS, **(c.get("weakness_lab") or {}))
        self.app = app
        self.p = Params(min_run=self.cfg["min_run"], touch_window=self.cfg["touch_window"],
                        equal_tol_atr=self.cfg["equal_tol_atr"], zone_slack_atr=self.cfg["zone_slack_atr"],
                        stop_atr=self.cfg["stop_atr"], min_rr=self.cfg["min_rr"], max_setup_4h=self.cfg["max_setup_4h"],
                        local_high_lookback=self.cfg["local_high_lookback"], ob_tfs=tuple(self.cfg["zone_tfs"]),
                        zone_types=tuple(self.cfg["zone_types"]))
        self.frames: dict | None = None
        self.frames_key = None
        self.last_h4: int | None = None
        self.last_m15: int | None = None
        self.last_m5: int | None = None
        self.logged: set = set()
        self.setups: dict = {}
        if self.live:
            try:
                self.setups = dict(app.state.get("weakness_setups", {}) or {})
            except Exception:  # noqa: BLE001
                self.setups = {}
            LOG.info("4H weakness lab ON | sides %s | %s touches in %s 4H | stop line + %.2f ATR | min %.1f R | zones %s on %s",
                     ",".join(self.cfg["sides"]), self.p.min_run, self.p.touch_window, self.p.stop_atr, self.p.min_rr,
                     "/".join(self.p.zone_types), "/".join(f"{t}m" for t in self.p.ob_tfs))

    @property
    def live(self) -> bool:
        return self.cfg["mode"] == "lab" and bool(self.c.get("lab_mode"))

    def _once(self, key, msg, *args):
        if key not in self.logged:
            self.logged.add(key)
            LOG.info(msg, *args)

    def _save(self):
        try:
            self.app.state.put("weakness_setups", self.setups)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("4H weakness: setup state not saved (%s)", str(exc)[:120])

    # ---- candles (closed only, broker timeframes, UTC open times)
    def load_frames(self) -> dict:
        m = self.app.market
        out = {}
        for tf, name in TF_NAMES.items():
            rows = m.rates(name, COUNTS[tf])
            out[tf] = Bars([int(r[0]) for r in rows], [r[1] for r in rows], [r[2] for r in rows],
                           [r[3] for r in rows], [r[4] for r in rows])
        return out

    def refresh(self) -> bool:
        """Reload the frames when a new 5M candle has closed. True when they changed."""
        key = self.app.market.rates("TIMEFRAME_M5", 1)
        key = int(key[-1][0]) if key else None
        if self.frames is not None and key == self.frames_key:
            return False
        self.frames, self.frames_key = self.load_frames(), key
        return True

    # ---- one pass
    def tick(self, now: datetime) -> list:
        if not self.live:
            return []
        sess = getattr(self.app.market, "sessions", None)
        if sess is not None and not sess.is_open(now):
            return []
        new = self.refresh()
        f = self.frames
        if not f or any(len(f[tf]) < 30 for tf in (5, 15, 60, 240)):
            return []
        out = []
        h4_new = f[240].t[-1] != self.last_h4
        m15_new = f[15].t[-1] != self.last_m15
        m5_new = f[5].t[-1] != self.last_m5
        quote = self.app.market.quote()
        for side in self.cfg["sides"]:
            res = self.side_tick(side, now, quote, h4_new, m15_new, m5_new and new)
            if res:
                out.append(res)
        self.last_h4, self.last_m15, self.last_m5 = f[240].t[-1], f[15].t[-1], f[5].t[-1]
        return out

    def _space(self, side: str) -> dict:
        if side == "SELL":
            return self.frames
        return {tf: mirror(b) for tf, b in self.frames.items()}

    def _has_open(self, side: str) -> bool:
        for rec in self.app.state.active_orders():
            if str(rec.get("setup_id", "")).startswith(f"{PREFIX}{side}-"):
                return True
        return False

    def side_tick(self, side: str, now: datetime, quote: dict, h4_new: bool, m15_new: bool, m5_new: bool):
        buy = side == "BUY"
        f = self._space(side)
        sgn = -1.0 if buy else 1.0
        price = sgn * (quote["ask"] if buy else quote["bid"])          # sell-space price we would trade at
        spread = float(quote["spread"])
        st = self.setups.get(side)
        ts_now = int(now.timestamp())
        h4 = f[240]
        if h4_new:
            k = len(h4) - 1
            if st is not None:
                if h4.c[k] > st["level"]:
                    return self._end(side, f"4H close {sgn * h4.c[k]:.2f} beyond the level")
                if k - index_before(h4, st["signal_bar"]) >= self.p.max_setup_4h:
                    return self._end(side, f"no entry within {self.p.max_setup_4h} 4H candles")
            elif not self._has_open(side):
                w = weakness_run(h4, k, self.p)
                if w is not None:
                    first, line, zone_bottom, a = w
                    found = find_zone(f, self.p, h4.t[first], ts_now, zone_bottom - self.p.zone_slack_atr * a, line)
                    if found:
                        tf, kind, level = found
                        stop = line + max(self.p.stop_atr * a, 2 * spread)
                        st = dict(level=level, stop=stop, line=line, signal_bar=h4.t[k], tf=tf, kind=kind,
                                  waiting=False, touched=False, atr=a, created=now.strftime("%Y-%m-%dT%H:%M:%SZ"))
                        self.setups[side] = st
                        self._save()
                        msg = (f"🧪 4H WEAKNESS {side} setup: line {sgn * line:.2f} ({self.p.min_run}+ touches), "
                               f"{kind} {tf}m 50% at {sgn * level:.2f}, stop {sgn * stop:.2f}; waits for the touch + 5M")
                        LOG.info(msg)
                        alerts.trade(msg, short=None)
                    else:
                        LOG.info("4H weakness %s: line %.2f found, no OB/breaker/FVG 50%% inside the wick zone", side,
                                 sgn * line)
        if st is None:
            return None
        born = int(datetime.fromisoformat(st["created"].replace("Z", "+00:00")).timestamp())
        since = [i for i in range(len(f[5])) if f[5].t[i] >= born]            # 5M candles after the setup was made
        high_since = max((f[5].h[i] for i in since), default=None)
        if price >= st["stop"] or (high_since is not None and high_since >= st["stop"]):
            return self._end(side, f"stop {sgn * st['stop']:.2f} traded before the entry")
        if m15_new:
            c15 = f[15].c[-1]
            if c15 > st["level"]:
                if not st["waiting"]:
                    st["waiting"] = True
                    self._save()
            elif c15 < st["level"] and st["waiting"]:
                st["waiting"], st["touched"] = False, False
                self._save()
        if st["waiting"]:
            return None
        if self.cfg["respect_news_lock"]:
            news = self.app.market.news_block(now)
            if news:
                self._once(("news", side, news), "4H weakness %s: waiting, news lock: %s", side, news)
                return None
        entry_kind = None
        if not st["touched"] and (price >= st["level"] or (high_since is not None and high_since >= st["level"])):
            st["touched"] = True
            self._save()
            if price >= st["level"] - spread and structure_trend(f[5], len(f[5]) - 1) == "BEARISH":
                entry_kind = "TOUCH"
            else:
                LOG.info("4H weakness %s: level %.2f touched, 5M not bearish yet: waiting for a bearish 5M close below",
                         side, sgn * st["level"])
        elif st["touched"] and m5_new and f[5].c[-1] < st["level"] and structure_trend(f[5], len(f[5]) - 1) == "BEARISH":
            entry_kind = "5M_CLOSE"
        if entry_kind is None:
            return None
        return self.enter(side, st, price, spread, entry_kind, now, f)

    def _end(self, side: str, why: str):
        st = self.setups.pop(side, None)
        self._save()
        if st:
            LOG.info("4H weakness %s setup at %.2f ended: %s", side, (-1 if side == "BUY" else 1) * st["level"], why)
        return None

    def enter(self, side: str, st: dict, price: float, spread: float, kind: str, now: datetime, f: dict):
        buy = side == "BUY"
        sgn = -1.0 if buy else 1.0
        tp1, tp2, rr = entry_plan(f, st, price, spread, self.p.min_rr)
        if tp1 is None:
            return self._end(side, f"no trade: {rr}")
        app, lab = self.app, self.app.lab
        block = lab.hard_block(now)                    # kill file, demo only, terminal, quote age, spread, lab caps
        if block:
            self._once(("block", side, block[:30], now.strftime("%Y%m%d%H")), "4H weakness %s: holding: %s", side, block)
            return None                                # the setup stays; retried on the next pass
        entry, stop, t1, t2 = sgn * price, round(sgn * st["stop"], 2), round(sgn * tp1, 2), round(sgn * tp2, 2)
        si = app.market.symbol_info()
        min_dist = si.trade_stops_level * si.point
        if abs(entry - stop) < max(min_dist, 1e-9) or abs(t1 - entry) < max(min_dist, 1e-9):
            return self._end(side, f"stop or TP1 inside the broker stop level ({min_dist})")
        macro = macro_of(f, int(now.timestamp()))
        if buy:
            macro = {"BULLISH": "BEARISH", "BEARISH": "BULLISH"}.get(macro, macro)
        setup_id = f"{PREFIX}{side}-{round(sgn * st['level'])}"
        if app.state.setup_orders_since(setup_id, (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), lab=True):
            return self._end(side, f"{setup_id} already traded in the last 24 h")
        title = (f"4H weakness {side}: {self.p.min_run}+ 4H highs at {sgn * st['line']:.2f}; {st['kind']} {st['tf']}m 50% "
                 f"{sgn * st['level']:.2f}; entry {kind}; TP2 {t2:.2f} (not traded, lab TP1 only)")
        order = BR._order(side, "MARKET", entry, stop, [t1], now + timedelta(minutes=30), setup_id, "B_TRADE", title,
                          deviation=self.c["signal_entry_tolerance"])
        order["move_sl_to_breakeven_at_tp1"] = False
        order["rr"] = rr
        order["max_entry_deviation"] = min(float(order["max_entry_deviation"]), float(self.c["max_entry_deviation_cap"]))
        session = SS.session_of(now, self.c.get("sessions"))
        tags = ["weakness_4h", f"zone_{st['kind'].lower()}_{st['tf']}m", f"entry_{kind.lower()}",
                f"macro_{macro.lower()}", f"session_{str(session).lower()}"]
        lots, risk_money, hold = lab.size(si, abs(entry - stop))
        if hold:
            LOG.info("4H weakness %s: holding: %s", side, hold)
            return None
        order["strategy"] = "LAB_WEAKNESS"
        order["meta"] = {"playbook": "SELL_4H_WEAKNESS" if not buy else "BUY_4H_STRENGTH", "confirmation": kind,
                         "session": session, "lab": 1, "violations": tags, "tier": None,
                         "tier_reason": "lab: 4H weakness forward test (not validated in replay)"}
        order["spread"] = spread
        self.setups.pop(side, None)                    # consumed BEFORE the send: a failure after it can never resend
        self._save()
        try:
            ticket = app.executor.send(order, lots, risk_money, "B_TRADE", magic=self.c["magic_lab"])
        except NW.NewsHold as h:                                     # 1.17: this sample is skipped, not an error
            LOG.info("4H weakness %s %s not sent: %s", side, setup_id, h)
            return f"{setup_id}: {h}"
        except Exception as exc:  # noqa: BLE001
            alerts.error(f"4H weakness lab order {setup_id} failed: {exc}")
            return f"{setup_id}: broker: {exc}"
        hold_until = now + timedelta(days=int(self.cfg["max_hold_days"]))
        if ticket:
            app.state.update_order(ticket, exit_utc=hold_until.strftime("%Y-%m-%dT%H:%M:%SZ"))
        LOG.info("4H WEAKNESS %s ticket=%s %s @ %.2f sl %.2f tp1 %.2f (%.2f R) | %.2f lots risk %.2f | %s | exit by %s",
                 side, ticket, setup_id, entry, stop, t1, rr, lots, risk_money, ",".join(tags),
                 hold_until.strftime("%Y-%m-%d %H:%M"))
        alerts.trade(f"🧪 LAB 4H WEAKNESS {side} {setup_id} MARKET @ {entry:.2f} | SL {stop:.2f} | TP1 {t1:.2f} | "
                     f"R:R {rr} | {lots} lots | {st['kind']} {st['tf']}m | macro {macro} | not validated (forward test)",
                     short=None)
        return f"{setup_id}: sent #{ticket}"
