"""
1.8 - BREAKOUT_CONTINUATION: a trend-day entry that does not wait for a pullback.

Why: every other playbook enters on a return to a zone. On 23 Oct the idea was confirmed at 14:15 with
price at 4008 after the Michigan collapse; the trader placed a retest limit at 4025 and price went to
3975 without looking back. Astra's own scorecard says its breakout scenarios work far more often than
its rejection scenarios (54 % vs 18 %), and the trader only traded the rejection kind.

Rule (deterministic, Python-verified; shared by the replay tool and the live trader):
  trigger   a closed 15M candle is the FIRST close beyond a session / previous-day / previous-week level
            (sessions.events BREAKOUT), in the direction of the macro trend (Daily + 4H) AND the 1H trend.
  session   London or New York only.
  stop      just beyond the broken level (max(0.3 x ATR15, 2 x spread) past it): the breakout is wrong
            when price closes back through the level. Never at the candle's far extreme.
  target    the FIRST opposing level (swings 15M/1H/4H + session levels) that gives >= min_rr from the
            live price; nearer levels are listed in `path_levels` for the review.
  entry     MARKET at the trigger candle's close (BUY on the ask) when that gives >= min_rr; otherwise a
            limit at the exact min_rr price, valid breakout_retest_minutes, cancelled if TP1 trades first.
  caps      the same gates as every order (news lock, spread, quote age, direction rule, loss limits);
            the tier is capped by settings.tier_cap (default C) until the reviews justify more.
"""

from __future__ import annotations

from datetime import datetime, timedelta

TRIGGERS = ("BREAKOUT",)


def level_lists(highs: list, lows: list) -> tuple[list, list]:
    return sorted(set(highs)), sorted(set(lows), reverse=True)


def candidate(e: dict, bar: dict, state: dict, atr15: float, highs: list, lows: list, as_of: datetime,
              session: str, sp: float, cfg: dict, drop=lambda why: None, against: list | None = None):
    """A BREAKOUT session event + its closed 15M candle -> an order plan dict or None (reason to drop).
    1.19: with `against` (a list) a breakout against the macro or 1H trend is NOT dropped: the tags against_macro /
    against_1h are appended to it instead (the breakout lab). Every other rule is unchanged."""
    if e.get("event") not in TRIGGERS:
        return None
    direction = e["direction"]
    buy = direction == "BUY"
    macro, h1 = state.get("macro_trend"), state.get("intermediate_trend") or (state.get("_frames") or {}).get("1h", {}).get("trend")
    want = "BULLISH" if buy else "BEARISH"
    if macro != want:
        if against is None:
            return drop(f"breakout {direction} against the macro trend {macro}")
        against.append("macro_mixed" if macro == "MIXED" else "against_macro")   # 1.20.1: Daily vs 4H split, or opposite
    if h1 != want:
        if against is None:
            return drop(f"breakout {direction} against the 1H trend {h1}")
        against.append("against_1h")
    if (session or "").upper() not in ("LONDON", "NEW_YORK", "LONDON_NY"):
        return drop(f"breakout outside London/New York ({session})")
    level = float(e["price"])
    pad = max(0.3 * atr15, 2 * sp)
    stop = round(level - pad if buy else level + pad, 2)
    price = bar["close"] + (sp if buy else 0.0)
    if (buy and price <= stop) or (not buy and price >= stop):
        return drop("price back through the level")
    risk = abs(price - stop)
    if risk < cfg["sl_atr_min"] * atr15:
        stop = round(price - cfg["sl_atr_min"] * atr15 if buy else price + cfg["sl_atr_min"] * atr15, 2)
        risk = abs(price - stop)
    if risk > cfg["sl_atr_max"] * atr15:
        return drop("stop wider than sl_atr_max (the breakout candle ran too far from the level)")
    r = cfg["min_rr"] + 0.02
    cand = [h for h in highs if h > price + 0.5 * risk] if buy else [l for l in lows if l < price - 0.5 * risk]
    if not cand:
        return drop("no opposing level for TP1")
    far = [lvl for lvl in cand if abs(lvl - price) / risk >= r]
    if far:                                                     # a level >= min_rr from the close: enter now
        tp1 = far[0]
        path = [lvl for lvl in cand if (lvl < tp1 if buy else lvl > tp1)]
        otype, entry, expiry = "MARKET", price, as_of + timedelta(minutes=30)
    else:                                                       # only nearer levels: a limit back toward the level
        tp1, path = cand[0], []
        entry = round((tp1 + r * stop) / (1 + r), 2)
        if (buy and not stop < entry < price) or (not buy and not price < entry < stop):
            return drop("2R not available")
        otype, expiry = ("BUY_LIMIT" if buy else "SELL_LIMIT"), as_of + timedelta(minutes=cfg.get("breakout_retest_minutes", 60))
    nxt = [x for x in cand if (x > tp1 if buy else x < tp1) and abs(x - tp1) >= 0.5 * risk]
    tp2 = nxt[0] if nxt else None
    rr_now = abs(tp1 - price) / risk
    if sp > cfg["max_spread"] or sp > cfg["spread_stop_fraction"] * abs(entry - stop):
        return drop("spread too large for the stop")
    zone = {"id": f"BREAKOUT:{e['level']}:{round(level)}", "direction": direction, "source": "BREAKOUT",
            "low": min(level, bar["low"]), "high": max(level, bar["high"]), "level": e["level"]}
    return {"cid": f"{as_of.strftime('%Y%m%d%H%M')}:{zone['id']}:{direction}:BREAKOUT", "zone": zone,
            "how": "BREAKOUT", "direction": direction, "order_type": otype, "entry": entry, "stop": stop,
            "tp1": tp1, "tp2": tp2, "expiry": expiry, "created": as_of, "session": session, "atr15": atr15,
            "strong": False, "level": level, "path_levels": path, "rr_now": round(rr_now, 2), "price": price}
