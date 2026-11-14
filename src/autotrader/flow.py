"""
1.19 (owner, 11 Nov): "check the 1 or 5 min frame at the support, resistance or any important price to see buy or sell,
and especially check the volumes being traded - did the market take the volume or not - on all the orders."

The order-flow tag, recorded on EVERY order (live and lab) when it is sent. It is a RECORD, never a gate: the Friday
review compares results by these columns before any of them becomes a rule.

  tr5 / tr1   5M and 1M structure (the last close through a fractal swing, k=2, 60 candles): BULLISH / BEARISH / NONE
  flow        WITH / AGAINST / MIXED the order's direction (WITH = both short frames agree with it)
  vol15/vol5  the last CLOSED 15M / 5M candle's tick volume / the median of the 20 closed candles before it
  vol_read    what the last closed 15M candle did with that volume:
                EXPANSION   volume >= 1.5x and a body >= 50 % of the range: the market took the volume and moved
                ABSORPTION  volume >= 1.5x and a body < 30 % of the range: heavy trading, little progress - the level
                            absorbed it (often a turn)
                THIN        volume < 0.8x: few participants, a move that may not hold
                NORMAL      anything else

Volume caveat: GivTrade XAUUSD is a CFD. MT5 has no exchange volume for it (real_volume is 0); tick_volume is the
number of price changes in the candle - a standard proxy for activity, not contracts traded.
"""

from __future__ import annotations

import logging
import statistics

from .conflict import trend

LOG = logging.getLogger("autotrader.flow")
COLUMNS = (("tr5", "TEXT"), ("tr1", "TEXT"), ("flow", "TEXT"), ("vol15", "REAL"), ("vol5", "REAL"), ("vol_read", "TEXT"))


def ratio(bars: list, n: int = 20) -> float | None:
    """Last candle's volume / median volume of the n before it. bars = (t, o, h, l, c, volume), oldest first."""
    if len(bars) < n + 1:
        return None
    base = statistics.median(b[5] for b in bars[-n - 1:-1])
    return round(bars[-1][5] / base, 2) if base > 0 else None


def read(bar, vol: float | None) -> str | None:
    """EXPANSION / ABSORPTION / THIN / NORMAL for one candle (t, o, h, l, c, ...) and its volume ratio."""
    if vol is None:
        return None
    rng = bar[2] - bar[3]
    body = abs(bar[4] - bar[1]) / rng if rng > 0 else 0.0
    if vol >= 1.5 and body >= 0.5:
        return "EXPANSION"
    if vol >= 1.5 and body < 0.3:
        return "ABSORPTION"
    if vol < 0.8:
        return "THIN"
    return "NORMAL"


def label(direction: str, tr5: str, tr1: str) -> str:
    want, against = ("BULLISH", "BEARISH") if direction == "BUY" else ("BEARISH", "BULLISH")
    if tr5 == want and tr1 == want:
        return "WITH"
    if tr5 == against and tr1 == against:
        return "AGAINST"
    return "MIXED"


def measure(market, direction: str) -> dict:
    """The tag for an order in `direction` now, from closed candles only."""
    m1 = market.rates_v("TIMEFRAME_M1", 72)
    m5 = market.rates_v("TIMEFRAME_M5", 72)
    m15 = market.rates_v("TIMEFRAME_M15", 24)
    tr1 = trend([b[:5] for b in m1], 60, 2)
    tr5 = trend([b[:5] for b in m5], 60, 2)
    v15, v5 = ratio(m15), ratio(m5)
    return {"tr5": tr5, "tr1": tr1, "flow": label(direction, tr5, tr1), "vol15": v15, "vol5": v5,
            "vol_read": read(m15[-1], v15) if m15 else None}


def text(f: dict) -> str:
    v15 = f"{f['vol15']:.1f}x" if f.get("vol15") is not None else "?"
    return f"5M {f.get('tr5')} / 1M {f.get('tr1')} ({f.get('flow')}) | 15M volume {v15} {f.get('vol_read') or ''}".strip()
