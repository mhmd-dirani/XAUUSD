"""
1.15: trade protection - every open trade is watched after the entry, not only placed and forgotten.

Owner, 10 Nov: "many of the trades were good at first, for example the last buy from 4034 reached below the TP by
about 10 pips and then dropped to hit the SL. It should be watched and taken as profit before hitting the stop loss."
LAB-BUY-4031 filled 4034.01, stop 4024.50, TP1 4054.39 and closed -9.67 (-1.01 R). Until 1.15 lab trades had one
target and no management at all, and live trades only had the TP1 partial / breakeven.

The rules (scope "lab" = lab magic only, "all" = lab + live swing + overnight; "off" = nothing):
  1. Excursion: the best and worst price since the fill are tracked from the live quote and from the closed M1
     candles after the fill minute (so a trade that ran while this program was stopped is still measured). They are
     saved on the order as mfe_r / mae_r (R = distance to the ORIGINAL stop).
  2. Breakeven: once the trade has been +be_at_r R (1.0), the stop goes to the entry + the current spread
     (BUY; SELL mirrored), so a trade that has shown +1 R can no longer become a full loss.
  3. Lock: once it has been +lock_at_r R (1.5), the stop goes to entry + lock_r R (0.5).
  4. Near the target: once it has covered near_tp_frac (85 %) of the way to the broker TP, the first CLOSED 5M candle
     that closes beyond the previous 5M candle's low (BUY) / high (SELL) closes the trade at market.
No decision is made on a quote older than 15 s. A stop only ever moves in the trade's favour, never inside the broker's stop level; if the strongest stage is no
longer valid (price already fell back), the next weaker stage is tried. Nothing here opens or enlarges a position.

simulate() is the same rules on M1 candles for the excursion report (--excursions): actual vs unmanaged vs protected.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from . import alerts

UTC = timezone.utc
LOG = logging.getLogger("autotrader.protect")
DEFAULTS = {"mode": "off", "be_at_r": 1.0, "lock_at_r": 1.5, "lock_r": 0.5, "near_tp_frac": 0.85,
            "near_tp_exit": True, "check_seconds": 10}
KEYS = set(DEFAULTS)
MAX_QUOTE_AGE = 15.0          # seconds; as the signal gate's quote-age cap


def _ts(s) -> float | None:
    if not s:
        return None
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


# ----------------------------------------------------------------------------- pure rules
def stages(buy: bool, entry: float, risk: float, best_r: float, spread: float, cfg: dict) -> list:
    """Protective stops earned by a trade whose best excursion is best_r R, strongest first: [(sl, label), ...]."""
    s = 1 if buy else -1
    out = []
    if best_r >= cfg["lock_at_r"]:
        out.append((entry + s * cfg["lock_r"] * risk, f"lock +{cfg['lock_r']:g}R"))
    if best_r >= cfg["be_at_r"]:
        out.append((entry + s * max(spread, 0.0), "breakeven"))
    return out


def progress(buy: bool, entry: float, tp: float | None, best: float) -> float:
    """Fraction of the way from the entry to the TP covered by the best price (0 when no TP)."""
    if not tp or tp == entry:
        return 0.0
    return ((best - entry) / (tp - entry)) if buy else ((entry - best) / (entry - tp))


def reversal_5m(buy: bool, bars5: list, armed_ts: float) -> bool:
    """bars5 = closed 5M candles (t, o, h, l, c), oldest first. True when the last one closed AFTER the trade was armed
    and closes beyond the previous candle's low (BUY) / high (SELL)."""
    if len(bars5) < 2:
        return False
    t, _o, _h, _l, c = bars5[-1][:5]
    p = bars5[-2]
    if t + 300 <= armed_ts:
        return False
    return c < p[3] if buy else c > p[2]


def simulate(buy: bool, entry: float, sl: float, tp: float | None, m1: list, spread: float, cfg: dict | None) -> dict:
    """Replay one trade on M1 candles (t, o, h, l, c; BID prices, oldest first, from the first full minute after the
    fill). cfg None = unmanaged (stop and TP only). Inside a candle the stop is checked before the TP (conservative).
    SELL stops and targets are hit on the ASK (= bid + spread). Returns {r, exit, reason, mfe_r, mae_r}."""
    risk = abs(entry - sl)
    if risk <= 0:
        return {"r": None, "exit": None, "reason": "no risk", "mfe_r": None, "mae_r": None}
    cur = sl
    best = worst = entry
    armed = None
    block = []
    prev5 = None
    s = 1 if buy else -1
    for bar in m1:
        t, o, h, l, c = bar[:5]
        hi, lo = (h, l) if buy else (h + spread, l + spread)      # the side that closes the trade
        if (lo <= cur) if buy else (hi >= cur):
            return _res("stop" if cur == sl else "protected stop", cur, entry, risk, s, best, worst)
        if tp and ((hi >= tp) if buy else (lo <= tp)):
            best = max(best, tp) if buy else min(best, tp)
            return _res("tp", tp, entry, risk, s, best, worst)
        best = max(best, hi) if buy else min(best, lo)
        worst = min(worst, lo) if buy else max(worst, hi)
        if cfg:
            best_r = s * (best - entry) / risk
            close_side = c if buy else c + spread
            for nsl, _label in stages(buy, entry, risk, best_r, spread, cfg):
                if (nsl > cur and nsl < close_side) if buy else (nsl < cur and nsl > close_side):
                    cur = nsl
                    break
                if (nsl <= cur) if buy else (nsl >= cur):
                    break
            if cfg.get("near_tp_exit") and armed is None and progress(buy, entry, tp, best) >= cfg["near_tp_frac"]:
                armed = t + 60
        # 5M candles built from the M1s (broker-aligned 5-minute blocks); a block missing its last minute is closed
        # when the next block starts (exit then at this candle's open, never at a price from the past)
        if block and int(block[0][0] // 300) != int(t // 300):
            b5 = _b5(block)
            if cfg and armed is not None and prev5 is not None and reversal_5m(buy, [prev5, b5], armed):
                return _res("near-TP reversal", o if buy else o + spread, entry, risk, s, best, worst)
            prev5, block = b5, []
        block.append(bar)
        if (t + 60) % 300 == 0:
            b5 = _b5(block)
            if cfg and armed is not None and prev5 is not None and reversal_5m(buy, [prev5, b5], armed):
                return _res("near-TP reversal", c if buy else c + spread, entry, risk, s, best, worst)
            prev5, block = b5, []
    last = m1[-1][4] if m1 else entry
    return _res("open", last if buy else last + spread, entry, risk, s, best, worst)


def _b5(block: list) -> tuple:
    t0 = block[0][0]
    return (t0 - t0 % 300, block[0][1], max(x[2] for x in block), min(x[3] for x in block), block[-1][4])


def _res(reason, px, entry, risk, s, best, worst) -> dict:
    return {"r": round(s * (px - entry) / risk, 2), "exit": round(px, 2), "reason": reason,
            "mfe_r": round(s * (best - entry) / risk, 2), "mae_r": round(s * (worst - entry) / risk, 2)}


# ----------------------------------------------------------------------------- live
class Protect:
    def __init__(self, c: dict, app):
        self.c = c
        self.cfg = dict(DEFAULTS, **(c.get("trade_protect") or {}))
        self.app = app
        self.ext: dict = {}            # position ticket -> [best, worst] price
        self.armed: dict = {}          # position ticket -> epoch when near_tp_frac was reached
        self.saved: dict = {}          # position ticket -> (mfe_r, mae_r) last written
        self.last = 0.0
        if self.cfg["mode"] != "off":
            LOG.info("Trade protection %s | stop -> entry + spread at +%.1f R | lock +%.1f R at +%.1f R | "
                     "near-TP exit %s at %.0f%% of the way", self.cfg["mode"].upper(), self.cfg["be_at_r"],
                     self.cfg["lock_r"], self.cfg["lock_at_r"], "ON" if self.cfg["near_tp_exit"] else "OFF",
                     100 * self.cfg["near_tp_frac"])

    def in_scope(self, rec: dict) -> bool:
        if str(rec.get("setup_id", "")).startswith("SCP-"):     # the scalp lab tests the replayed rule unmanaged
            return False
        if self.cfg["mode"] == "all":
            return True
        return self.cfg["mode"] == "lab" and bool(rec.get("lab"))

    def _m1_since(self, since_ts: float, now_ts: float) -> list:
        n = int(min(3000, max(2, (now_ts - since_ts) / 60 + 2)))
        return [b for b in self.app.market.rates("TIMEFRAME_M1", n) if b[0] >= since_ts]

    def tick(self, now: datetime | None = None) -> list:
        if self.cfg["mode"] == "off" or time.time() - self.last < float(self.cfg["check_seconds"]):
            return []
        self.last = time.time()
        now = now or datetime.now(UTC)
        m, x = self.app.market, self.app.executor
        recs = [r for r in self.app.state.active_orders() if r.get("status") in ("OPEN", "TP1_DONE")
                and r.get("position_ticket") is not None and self.in_scope(r)]
        if not recs:
            return []
        positions = {getattr(p, "ticket", None): p for p in (m.trade_call("positions_get", symbol=m.symbol) or [])}
        q = m.quote()
        if float(q.get("age", 0.0) or 0.0) > MAX_QUOTE_AGE:     # fail closed: no stop decisions on an old price
            LOG.debug("Trade protection waits: quote %.0f s old", q.get("age"))
            return []
        si = m.symbol_info()
        point = float(getattr(si, "point", 0.01) or 0.01)
        digits = int(getattr(si, "digits", 2) or 2)
        gap = max(int(getattr(si, "trade_stops_level", 0) or 0), int(getattr(si, "trade_freeze_level", 0) or 0)) * point
        out = []
        bars5 = None
        for rec in recs:
            pos = positions.get(rec["position_ticket"])
            if pos is None:
                continue
            try:
                act = self._one(rec, pos, q, point, digits, gap, now, lambda: bars5)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Trade protection skipped %s: %s", rec.get("setup_id"), str(exc)[:160])
                continue
            if act == "need_5m":
                try:
                    bars5 = bars5 or m.rates("TIMEFRAME_M5", 3)
                    act = self._one(rec, pos, q, point, digits, gap, now, lambda: bars5)
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("Trade protection skipped %s: %s", rec.get("setup_id"), str(exc)[:160])
                    continue
            if act:
                out.append((rec["setup_id"], act))
        live = {getattr(p, "ticket", None) for p in positions.values()}
        for k in [k for k in self.ext if k not in live]:
            self.ext.pop(k, None)
            self.armed.pop(k, None)
            self.saved.pop(k, None)
        return out

    def _one(self, rec, pos, q, point, digits, gap, now, bars5_fn):
        buy = rec["direction"] == "BUY"
        entry = float(getattr(pos, "price_open", 0.0) or rec["entry"])
        sl0 = float(rec["sl"])
        risk = abs(entry - sl0)
        if risk <= 0:
            return None
        s = 1 if buy else -1
        bid, ask, spread = float(q["bid"]), float(q["ask"]), float(q.get("spread", q["ask"] - q["bid"]))
        price = bid if buy else ask
        tk = rec["position_ticket"]
        ext = self.ext.get(tk)
        if ext is None:                                   # first sight: what happened since the fill (M1 candles)
            ext = [price, price]
            fill = _ts(rec.get("fill_utc"))
            if fill:
                start = fill - fill % 60 + 60             # the fill minute itself also holds pre-fill prices
                for b in self._m1_since(start, now.timestamp()):
                    hi, lo = (b[2], b[3]) if buy else (b[2] + spread, b[3] + spread)
                    ext[0] = max(ext[0], hi) if buy else min(ext[0], lo)
                    ext[1] = min(ext[1], lo) if buy else max(ext[1], hi)
            self.ext[tk] = ext
        ext[0] = max(ext[0], price) if buy else min(ext[0], price)
        ext[1] = min(ext[1], price) if buy else max(ext[1], price)
        best, worst = ext
        best_r, worst_r = s * (best - entry) / risk, s * (worst - entry) / risk
        last = self.saved.get(tk)
        if last is None or abs(last[0] - best_r) >= 0.05 or abs(last[1] - worst_r) >= 0.05:
            self.app.state.update_order(rec["ticket"], mfe_r=round(best_r, 2), mae_r=round(worst_r, 2))
            self.saved[tk] = (best_r, worst_r)

        x = self.app.executor
        tp = float(getattr(pos, "tp", 0.0) or 0.0) or (rec.get("tp2") or rec.get("tp1"))
        # near the target: exit on the first 5M close back against the trade
        if self.cfg["near_tp_exit"] and tp and not rec.get("conflict"):   # 1.16: a conflict trade's TP is its breakeven
            if tk not in self.armed and progress(buy, entry, tp, best) >= self.cfg["near_tp_frac"]:
                self.armed[tk] = now.timestamp()
                LOG.info("Trade protection: %s is %.0f%% of the way to TP %.2f (best %.2f); a 5M close back against "
                         "it now closes the trade", rec["setup_id"], 100 * progress(buy, entry, tp, best), tp, best)
            if tk in self.armed:
                b5 = bars5_fn()
                if b5 is None:
                    return "need_5m"
                if reversal_5m(buy, b5, self.armed[tk]):
                    r_now = s * (price - entry) / risk
                    why = f"near-TP reversal: best {best:.2f} ({best_r:+.2f} R), 5M close {b5[-1][4]:.2f}"
                    if x.close_position(pos, why, comment="PROTECT"):
                        self.app.state.update_order(rec["ticket"], protect=f"near-TP exit at {r_now:+.2f} R")
                        msg = (f"🛡 {rec['direction']} {rec['setup_id']} closed near its target at {r_now:+.2f} R "
                               f"(best {best_r:+.2f} R, TP {tp:.2f}; 5M candle closed back against it)")
                        LOG.info(msg)
                        alerts.trade(msg, short=None if rec.get("lab") else
                                     f"🛡 {rec['direction']} closed near target {r_now:+.2f}R")
                        return "near_tp_exit"
        # stop stages
        cur = float(getattr(pos, "sl", 0.0) or 0.0)
        for nsl, label in stages(buy, entry, risk, best_r, spread, self.cfg):
            nsl = round(nsl, digits)
            # 1.20.3: only a real improvement (>= 5 % of the risk): 13 Nov the breakeven stop (entry + spread) was
            # re-moved by 2-6 cents each time the spread changed, one Telegram message per move
            step = max(point / 2, 0.05 * risk)
            tighter = (not cur) or ((nsl > cur + step) if buy else (nsl < cur - step))
            if not tighter:
                return None                               # already at least this strong
            valid = (nsl <= bid - gap - point) if buy else (nsl >= ask + gap + point)
            if not valid:
                continue                                  # price is back too close: try the weaker stage
            why = f"trade protection: {label} (best {best_r:+.2f} R)"
            if x.move_stop(pos, nsl, why):
                self.app.state.update_order(rec["ticket"], protect=f"{label} @ {nsl:.2f}")
                msg = (f"🛡 {rec['direction']} {rec['setup_id']}: stop {cur:.2f} -> {nsl:.2f} ({label}); "
                       f"the trade reached {best_r:+.2f} R")
                LOG.info(msg)
                alerts.trade(msg, short=None if rec.get("lab") else f"🛡 {rec['direction']} stop → {label}")
                return label
            return None
        return None
