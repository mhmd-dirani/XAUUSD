#!/usr/bin/env python3
"""Deterministic, closed-candle SMC structure engine for XAU/USD (Gold Monitor V5).

Offline only: no network, AI or broker calls. Everything here is a price-pattern
hypothesis computed from CLOSED candles, never proof of institutional orders and
never a trade approval. Point-in-time safe: an event is only emitted on the bar at
which it became knowable (pivots are confirmed `pivot_right` bars later).

Outputs (per frame):
- confirmed swings (fractal pivots)
- BOS / CHOCH: a CLOSE beyond the latest confirmed, unbroken swing
- LIQUIDITY_SWEEP: wick beyond a confirmed, unbroken swing (+ATR buffer), close back inside
- FVG: three-candle imbalance with a displacement middle candle; ACTIVE / PARTIALLY_FILLED /
  FILLED / INVALIDATED
- ORDER_BLOCK_CANDIDATE: last opposite candle before a displaced structural break;
  ACTIVE / TESTED / INVALIDATED
- FIB_OTE: retracement of the latest confirmed impulse leg (0.382..0.786), OTE = 0.618-0.786;
  ACTIVE / SUPERSEDED / INVALIDATED
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any

UTC = timezone.utc
TF_SECONDS = {"15min": 900, "1h": 3600, "4h": 14400, "1day": 86400, "1week": 604800}
FIB_LEVELS = (0.382, 0.5, 0.618, 0.705, 0.786)


@dataclass(frozen=True)
class SMCSettings:
    pivot_left: int = 3
    pivot_right: int = 3
    atr_period: int = 14
    sweep_buffer_atr: float = 0.05
    displacement_atr: float = 1.2
    fvg_min_atr: float = 0.1
    ob_lookback: int = 10
    max_events: int = 200

    def __post_init__(self):
        if not 1 <= self.pivot_left <= 10 or not 1 <= self.pivot_right <= 10:
            raise ValueError("Invalid pivot window")
        if not 5 <= self.atr_period <= 50:
            raise ValueError("Invalid ATR period")
        if not 0 <= self.sweep_buffer_atr <= 2 or not 0 < self.displacement_atr <= 5 or not 0 <= self.fvg_min_atr <= 3:
            raise ValueError("Invalid ATR thresholds")
        if not 1 <= self.ob_lookback <= 50 or not 10 <= self.max_events <= 1000:
            raise ValueError("Invalid lookback/event limits")


def _time(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Timestamp requires an explicit timezone")
    return dt.astimezone(UTC)


def _closes(bar: dict[str, Any], tf: str) -> datetime:
    if bar.get("closes") is not None:
        return _time(bar["closes"])
    return _time(bar["time"]) + timedelta(seconds=TF_SECONDS[tf])


def _make_id(tf: str, kind: str, *parts: Any) -> str:
    h = hashlib.sha1(json.dumps([str(p) for p in parts]).encode()).hexdigest()[:10]
    return f"{tf}:{kind}:{h}"


def normalize_bars(bars: list[dict[str, Any]], tf: str) -> list[dict[str, Any]]:
    """Copies with float OHLC and an explicit ISO `closes`. Input is never mutated."""
    out = []
    for b in bars:
        t = _time(b["time"])
        out.append({"time": b["time"] if isinstance(b["time"], str) else t.isoformat(),
                    "open": float(b["open"]), "high": float(b["high"]),
                    "low": float(b["low"]), "close": float(b["close"]),
                    "closes": _closes(b, tf).isoformat()})
    return out


def _validate(bars: list[dict[str, Any]], tf: str, as_of: datetime | None = None) -> None:
    if tf not in TF_SECONDS:
        raise ValueError(f"Unsupported timeframe {tf}")
    if not bars:
        raise ValueError("No candles")
    prev = None
    for b in bars:
        o, h, l, c = (float(b[k]) for k in ("open", "high", "low", "close"))
        if not all(isfinite(x) and x > 0 for x in (o, h, l, c)) or h < max(o, l, c) or l > min(o, h, c):
            raise ValueError(f"Malformed OHLC at {b.get('time')}")
        t = _time(b["time"])
        if prev is not None and t <= prev:
            raise ValueError("Candles must be strictly ascending and unique")
        if as_of is not None and _closes(b, tf) > as_of:
            raise ValueError(f"Candle {b['time']} is not closed at as_of")
        prev = t


def _atr(bars: list[dict[str, Any]], period: int = 14) -> list[float]:
    out, atr = [], 0.0
    for i, b in enumerate(bars):
        if i == 0:
            tr = b["high"] - b["low"]
        else:
            pc = bars[i - 1]["close"]
            tr = max(b["high"] - b["low"], abs(b["high"] - pc), abs(b["low"] - pc))
        atr = tr if i == 0 else ((atr * i + tr) / (i + 1) if i < period else (atr * (period - 1) + tr) / period)
        out.append(max(atr, 1e-9))
    return out


def _pivots(bars: list[dict[str, Any]], left: int, right: int, side: str) -> list[int]:
    """Fractal pivots: strictly beyond the `left` bars, at least equal to the `right` bars,
    so the FIRST of equal highs/lows (common in gold quotes) is the pivot, exactly once."""
    attr = "high" if side == "HIGH" else "low"
    strict = (lambda a, b: a > b) if side == "HIGH" else (lambda a, b: a < b)
    loose = (lambda a, b: a >= b) if side == "HIGH" else (lambda a, b: a <= b)
    found = []
    for i in range(left, len(bars) - right):
        v = bars[i][attr]
        if all(strict(v, o[attr]) for o in bars[i - left:i]) and all(loose(v, o[attr]) for o in bars[i + 1:i + right + 1]):
            found.append(i)
    return found


def _order_block(bars, atr, tf, direction, j, origin, cfg):
    lo = max(0, j - cfg.ob_lookback)
    if origin is not None:
        lo = max(lo, origin)
    k = None
    for m in range(j - 1, lo - 1, -1):
        b = bars[m]
        if (direction == "BUY" and b["close"] < b["open"]) or (direction == "SELL" and b["close"] > b["open"]):
            k = m
            break
    if k is None:
        return None
    displaced = any(bars[m]["high"] - bars[m]["low"] >= cfg.displacement_atr * atr[max(m - 1, 0)]
                    for m in range(k + 1, j + 1))
    if not displaced:
        return None
    return {"id": _make_id(tf, "OB_" + direction, bars[k]["time"]), "kind": "ORDER_BLOCK_CANDIDATE",
            "direction": direction, "timeframe": tf, "at": bars[k]["closes"], "confirmed_at": bars[j]["closes"],
            "level_low": bars[k]["low"], "level_high": bars[k]["high"],
            "source_times": [bars[k]["time"], bars[j]["time"]], "status": "ACTIVE", "_idx": k, "_confirm": j}


def analyze_frame(bars: list[dict[str, Any]], tf: str, *, as_of: datetime | None = None,
                  settings: SMCSettings | None = None) -> dict[str, Any]:
    cfg = settings or SMCSettings()
    _validate(bars, tf, as_of)
    bars = normalize_bars(bars, tf)
    n = len(bars)
    atr = _atr(bars, cfg.atr_period)
    confirm: dict[int, list[tuple[int, str]]] = {}
    for side in ("HIGH", "LOW"):
        for i in _pivots(bars, cfg.pivot_left, cfg.pivot_right, side):
            confirm.setdefault(i + cfg.pivot_right, []).append((i, side))

    def event(kind, direction, j, low, high, sources, **extra):
        return {"id": _make_id(tf, kind + "_" + direction, *sources), "kind": kind, "direction": direction,
                "timeframe": tf, "at": bars[j]["closes"], "level_low": round(low, 5), "level_high": round(high, 5),
                "source_times": list(sources), "status": "CONFIRMED", **extra}

    structure, sweeps, fvgs, obs, swings = [], [], [], [], []
    seen_ob = set()
    trend = "NEUTRAL"
    last: dict[str, int | None] = {"HIGH": None, "LOW": None}
    broken: set[tuple[str, int]] = set()
    swept: set[tuple[str, int]] = set()
    for j in range(n):
        b = bars[j]
        prev_atr = atr[j - 1] if j else atr[0]
        broke = set()
        for side, direction in (("HIGH", "BUY"), ("LOW", "SELL")):
            i = last[side]
            if i is None or (side, i) in broken:
                continue
            lvl = bars[i]["high"] if side == "HIGH" else bars[i]["low"]
            if (side == "HIGH" and b["close"] > lvl) or (side == "LOW" and b["close"] < lvl):
                kind = "CHOCH" if trend == ("BEARISH" if direction == "BUY" else "BULLISH") else "BOS"
                structure.append(event(kind, direction, j, lvl, lvl, [bars[i]["time"], b["time"]],
                                       swing_time=bars[i]["time"]))
                broken.add((side, i))
                broke.add(side)
                trend = "BULLISH" if direction == "BUY" else "BEARISH"
                ob = _order_block(bars, atr, tf, direction, j, last["LOW" if direction == "BUY" else "HIGH"], cfg)
                if ob and ob["id"] not in seen_ob:
                    seen_ob.add(ob["id"])
                    obs.append(ob)
        for side, direction in (("HIGH", "SELL"), ("LOW", "BUY")):
            i = last[side]
            if i is None or side in broke or (side, i) in broken or (side, i) in swept:
                continue
            lvl = bars[i]["high"] if side == "HIGH" else bars[i]["low"]
            buf = cfg.sweep_buffer_atr * prev_atr
            if ((side == "HIGH" and b["high"] > lvl + buf and b["close"] < lvl)
                    or (side == "LOW" and b["low"] < lvl - buf and b["close"] > lvl)):
                sweeps.append(event("LIQUIDITY_SWEEP", direction, j, lvl, lvl, [bars[i]["time"], b["time"]]))
                swept.add((side, i))
        if j >= 2:
            a, m, c3 = bars[j - 2], bars[j - 1], bars[j]
            displaced = m["high"] - m["low"] >= cfg.displacement_atr * atr[j - 2]
            gap_min = cfg.fvg_min_atr * atr[j - 2]
            src = [a["time"], m["time"], c3["time"]]
            if displaced and c3["low"] > a["high"] and c3["low"] - a["high"] >= gap_min:
                fvgs.append(dict(event("FVG", "BUY", j, a["high"], c3["low"], src), status="ACTIVE", _idx=j))
            elif displaced and c3["high"] < a["low"] and a["low"] - c3["high"] >= gap_min:
                fvgs.append(dict(event("FVG", "SELL", j, c3["high"], a["low"], src), status="ACTIVE", _idx=j))
        for i, side in confirm.get(j, []):   # known only from the close of bar j onward
            last[side] = i
            swings.append({"side": side, "time": bars[i]["time"],
                           "price": bars[i]["high" if side == "HIGH" else "low"],
                           "confirmed_at": bars[j]["closes"], "_idx": i})

    for f in fvgs:
        j, status = f.pop("_idx"), "ACTIVE"
        for b in bars[j + 1:]:
            if f["direction"] == "BUY":
                if b["close"] < f["level_low"]:
                    status = "INVALIDATED"; break
                if b["low"] <= f["level_low"]:
                    status = "FILLED"
                elif b["low"] < f["level_high"] and status == "ACTIVE":
                    status = "PARTIALLY_FILLED"
            else:
                if b["close"] > f["level_high"]:
                    status = "INVALIDATED"; break
                if b["high"] >= f["level_high"]:
                    status = "FILLED"
                elif b["high"] > f["level_low"] and status == "ACTIVE":
                    status = "PARTIALLY_FILLED"
        f["status"] = status
    for ob in obs:
        ob.pop("_idx")
        start, status = ob.pop("_confirm"), "ACTIVE"
        for b in bars[start + 1:]:
            if ob["direction"] == "BUY":
                if b["close"] < ob["level_low"]:
                    status = "INVALIDATED"; break
                if b["low"] <= ob["level_high"]:
                    status = "TESTED"
            else:
                if b["close"] > ob["level_high"]:
                    status = "INVALIDATED"; break
                if b["high"] >= ob["level_low"]:
                    status = "TESTED"
        ob["status"] = status

    fib = _fibonacci(bars, tf, trend, swings, structure)
    cap = cfg.max_events
    events = structure[-cap:] + sweeps[-cap:] + fvgs[-cap:] + obs[-cap:]
    return {"timeframe": tf, "last_closed_at": bars[-1]["closes"], "trend": trend,
            "atr": round(atr[-1], 5),
            "swings": [{k: v for k, v in s.items() if k != "_idx"} for s in swings[-12:]],
            "events": events, "fibonacci": fib,
            "warnings": ["Pattern hypotheses from closed OHLC only; not proof of institutional orders."]}


def _fibonacci(bars, tf, trend, swings, structure):
    if trend not in ("BULLISH", "BEARISH"):
        return None
    bull = trend == "BULLISH"
    ends = [s for s in swings if s["side"] == ("HIGH" if bull else "LOW")]
    if not ends:
        return None
    end = ends[-1]
    starts = [s for s in swings if s["side"] == ("LOW" if bull else "HIGH") and s["_idx"] < end["_idx"]]
    if not starts:
        return None
    start = starts[-1]
    hi, lo = (end["price"], start["price"]) if bull else (start["price"], end["price"])
    rng = hi - lo
    if rng <= 0:
        return None
    direction = "BUY" if bull else "SELL"
    start_close = bars[start["_idx"]]["closes"]
    if not any(e["direction"] == direction and e["kind"] in ("BOS", "CHOCH") and _time(e["at"]) >= _time(start_close)
               for e in structure):
        return None   # the leg must contain a same-direction structural break
    levels = {str(r): round(hi - r * rng if bull else lo + r * rng, 5) for r in FIB_LEVELS}
    ote = sorted((levels["0.618"], levels["0.786"]))
    after = bars[end["_idx"] + 1:]
    if any((b["close"] < lo) if bull else (b["close"] > hi) for b in after):
        status = "INVALIDATED"
    elif any((b["high"] > hi) if bull else (b["low"] < lo) for b in after):
        status = "SUPERSEDED"
    else:
        status = "ACTIVE"
    mid = (hi + lo) / 2
    last = bars[-1]["close"]
    return {"id": _make_id(tf, "FIB_" + direction, start["time"], end["time"]), "kind": "FIB_OTE",
            "direction": direction, "timeframe": tf, "at": end["confirmed_at"],
            "level_low": ote[0], "level_high": ote[1], "levels": levels,
            "swing_low": lo, "swing_high": hi, "source_times": [start["time"], end["time"]],
            "location": "DISCOUNT" if last < mid else "PREMIUM" if last > mid else "EQUILIBRIUM",
            "status": status}
