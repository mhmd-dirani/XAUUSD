"""
1.15: the all-timeframe zone score - RECORDED on every order (lab and live), never a gate, never a size.

Owner, 10 Nov: "we don't want the live trader to check some frames and neglect the others. Sometimes there is an
order block or FVG on the 1H which is not on the 4H or 15M or 30M, and vice versa. With-trend or counter-trend is not
the main importance." Until 1.15 the live trader looked at Daily/4H zones (remembered S/R) and 4H/1H trend; 1H, 30M
and 15M order blocks / FVGs never counted on their own, and 30M was not read at all.

For an order, each timeframe W1, D1, H4, H1, M30, M15 is searched for zones built from candles CLOSED before the
order time (point-in-time: the excursion report can score old trades exactly as they were):
  FVG     3-candle gap (BUY: high[i] < low[i+2]); dead once a later candle closes through its far edge.
  OB      last opposite candle before a displacement (BUY: a bearish candle, the next candle closes above its high
          and the move clears its high by >= 0.5 ATR within two candles); zone = that candle's range.
  SWING   a k=2 fractal low (BUY: support) / high (SELL: resistance); zone = wick to body edge.
  Zones are capped at 0.5 ATR of their own timeframe from the edge nearest price (a Daily candle is not a zone).
Only zones formed in the last 120 candles of their timeframe that price has NOT traded back into since (the last 3
candles excepted: that touch is the entry) count.
A zone SUPPORTS the order when it is on the order's side (BUY: bullish FVG / OB, swing low) and overlaps the entry
band, entry - 0.5 R .. entry + 0.1 R (SELL mirrored). A zone is AGAINST the order when it is an opposite zone in the
first 1 R of the path towards TP1 (the nearest obstacle).
  zone_score   = how many timeframes hold at least one supporting zone (0..6)
  zone_against = how many timeframes hold an opposite zone within the first 1 R of the path
  zone_tfs     = JSON {"for": ["H1:FVG", "M15:OB"], "against": ["H4:SWING"]}
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

UTC = timezone.utc
LOG = logging.getLogger("autotrader.zones")
TFS = (("W1", "TIMEFRAME_W1", 60, 604800), ("D1", "TIMEFRAME_D1", 120, 86400), ("H4", "TIMEFRAME_H4", 180, 14400),
       ("H1", "TIMEFRAME_H1", 240, 3600), ("M30", "TIMEFRAME_M30", 240, 1800), ("M15", "TIMEFRAME_M15", 240, 900))
PERIOD = {name: sec for name, _tf, _n, sec in TFS}
DEFAULTS = {"mode": "off", "band_r": 0.5, "cap_atr": 0.5, "cache_seconds": 60}


def _atr(b: list, i: int, n: int = 14) -> float:
    if i < 1:
        return b[i][2] - b[i][3] if b else 0.0
    lo = max(1, i - n + 1)
    trs = [max(b[j][2] - b[j][3], abs(b[j][2] - b[j - 1][4]), abs(b[j][3] - b[j - 1][4])) for j in range(lo, i + 1)]
    return sum(trs) / len(trs) if trs else 0.0


def zones(bars: list, until_ts: float, period: int, cap_atr: float = 0.5, max_age: int = 120, grace: int = 3) -> list:
    """Live, untested zones from candles (t, o, h, l, c) closed by until_ts, formed in the last max_age candles.
    [{kind, side BULL|BEAR, lo, hi, t}]"""
    b = [x for x in bars if x[0] + period <= until_ts][-(max_age + 20):]
    n = len(b)
    if n < 5:
        return []
    a_last = _atr(b, n - 1)
    cap = cap_atr * a_last if a_last > 0 else None
    out = []

    def alive(side, lo, hi, after):
        if side == "BULL":
            return not any(b[j][4] < lo for j in range(after, n))
        return not any(b[j][4] > hi for j in range(after, n))

    def fresh(lo, hi, after):
        """not traded back into between its formation and the last `grace` candles (the touch now is the entry)"""
        return not any(b[j][3] <= hi and b[j][2] >= lo for j in range(after, max(after, n - grace)))

    def add(kind, side, lo, hi, t, after):
        if hi < lo:
            lo, hi = hi, lo
        if cap and hi - lo > cap:                    # keep the edge nearest price: BULL zones are below price
            lo, hi = (hi - cap, hi) if side == "BULL" else (lo, lo + cap)
        if alive(side, lo, hi, after) and fresh(lo, hi, after):
            out.append({"kind": kind, "side": side, "lo": lo, "hi": hi, "t": t})

    first = max(0, n - max_age)
    for i in range(first, n - 2):
        t, o, h, l, c = b[i][:5]
        # FVG
        if h < b[i + 2][3]:
            add("FVG", "BULL", h, b[i + 2][3], t, i + 3)
        if l > b[i + 2][2]:
            add("FVG", "BEAR", b[i + 2][2], l, t, i + 3)
        # order block
        a = _atr(b, i + 1)
        if c < o and b[i + 1][4] > h and max(b[i + 1][4], b[i + 2][4]) - h >= 0.5 * a:
            add("OB", "BULL", l, h, t, i + 2)
        if c > o and b[i + 1][4] < l and l - min(b[i + 1][4], b[i + 2][4]) >= 0.5 * a:
            add("OB", "BEAR", l, h, t, i + 2)
    for p in range(max(2, first), n - 2):
        t, o, h, l, c = b[p][:5]
        if all(l < b[j][3] for j in (p - 2, p - 1, p + 1, p + 2)):
            add("SWING", "BULL", l, max(l, min(o, c)), t, p + 3)
        if all(h > b[j][2] for j in (p - 2, p - 1, p + 1, p + 2)):
            add("SWING", "BEAR", min(h, max(o, c)), h, t, p + 3)
    return out


def _overlap(z: dict, lo: float, hi: float) -> bool:
    return z["hi"] >= lo and z["lo"] <= hi


def score(frames: dict, direction: str, entry: float, stop: float, tp1: float | None, until_ts: float,
          band_r: float = 0.5, cap_atr: float = 0.5) -> dict:
    """frames = {"W1": bars, "D1": bars, ...}. Returns {score, against, for, vs}."""
    buy = direction == "BUY"
    risk = abs(entry - stop)
    if risk <= 0:
        return {"score": None, "against": None, "for": [], "vs": []}
    if not tp1:
        tp1 = entry + (2 * risk if buy else -2 * risk)
    edge = 0.1 * risk
    if buy:                                             # path = the first 1 R towards TP1: the nearest obstacle
        band, path = (entry - band_r * risk, entry + edge), (entry + edge, min(tp1, entry + risk))
    else:
        band, path = (entry - edge, entry + band_r * risk), (max(tp1, entry - risk), entry - edge)
    want = "BULL" if buy else "BEAR"
    sup, vs = [], []
    for name, _tf, _n, sec in TFS:
        bars = frames.get(name)
        if not bars:
            continue
        zs = zones(bars, until_ts, sec, cap_atr)
        kinds = sorted({z["kind"] for z in zs if z["side"] == want and _overlap(z, *band)})
        if kinds:
            sup.append(f"{name}:{'+'.join(kinds)}")
        if path[1] > path[0]:
            k2 = sorted({z["kind"] for z in zs if z["side"] != want and _overlap(z, *path)})
            if k2:
                vs.append(f"{name}:{'+'.join(k2)}")
    return {"score": len(sup), "against": len(vs), "for": sup, "vs": vs}


class ZoneScorer:
    """Live side: candles from the trader's MT5 session, cached for cache_seconds."""

    def __init__(self, c: dict, market):
        self.cfg = dict(DEFAULTS, **(c.get("zone_score") or {}))
        self.m = market
        self._frames = None
        self._at = 0.0

    @property
    def on(self) -> bool:
        return self.cfg["mode"] == "record"

    def frames(self) -> dict:
        if self._frames is None or time.time() - self._at > float(self.cfg["cache_seconds"]):
            self._frames = {name: [tuple(r) for r in self.m.rates(tf, n)] for name, tf, n, _s in TFS}
            self._at = time.time()
        return self._frames

    def for_order(self, order: dict, now: datetime | None = None) -> dict:
        now = now or datetime.now(UTC)
        return score(self.frames(), order["direction"], float(order["entry_price"]), float(order["stop_loss"]),
                     order.get("take_profit_1"), now.timestamp(), self.cfg["band_r"], self.cfg["cap_atr"])


def columns(zs: dict) -> dict:
    return {"zone_score": zs["score"], "zone_against": zs["against"],
            "zone_tfs": json.dumps({"for": zs["for"], "against": zs["vs"]})}


def label(zs: dict) -> str:
    return (f"zone score {zs['score']}/6 ({', '.join(zs['for']) or 'none'})"
            + (f" | against {zs['against']} ({', '.join(zs['vs'])})" if zs["against"] else ""))
