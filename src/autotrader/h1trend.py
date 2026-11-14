"""
1.13.1: the 1H-trend retest engine (lab only). The 6 Nov lesson: the 1H structure was bearish all day, price retested
the 1H bearish gap 4073-4083 (and the block under it) at 08:45-11:00, then fell $50 after NFP. Astra had proposed
that sell at 03:13, dropped it, and from 09:00 only proposed buys because the Daily/4H are bullish; the live gate
refuses a SELL against a bullish Daily/4H unless it sits at a strong Daily/4H zone. So no engine owned the trade.

This one does, as a measured sample:
  - direction = the 1H structure trend (Python's evidence: last 1H BOS/CHoCH), whatever the Daily/4H say;
  - zone = a usable 1H FVG or 1H order block in that direction (from the evidence index), ahead of price;
  - stop = beyond the far edge of the zone + stop_buffer_atr15 x ATR15;
  - TP1 = the first liquidity level on the target side that gives >= min_rr from the zone's near edge: session levels
    (PDH/PDL, Asia/London/NY highs and lows, previous week) and 15M fractal swings;
  - the ENTRY is the LTF engine's: touch of the zone, then a 5M change of structure (autotrader/ltf.py), with its
    tight 5M stop and liquidity rules. Setup ids H1T-<DIR>-<zone mid>, tag h1_trend (+ with_macro / against_macro).
Demo lab only (magic_lab, Lab.hard_block / Lab.size, the hedge rule, one sample per zone per trading day).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

UTC = timezone.utc
LOG = logging.getLogger("autotrader.h1trend")
PREFIX = "H1T-"

DEFAULTS = {
    "mode": "off",                 # off | lab
    "sides": ["BUY", "SELL"],
    "zone_kinds": ["FVG", "ORDER_BLOCK_CANDIDATE"],
    "stop_buffer_atr15": 0.3,
    "min_rr": 2.0,
    "max_distance_atr15": 4.0,     # zones further than this from price are left for later
    "max_zone_age_hours": 72,
}
USABLE = {"FVG": ("ACTIVE", "PARTIALLY_FILLED"), "ORDER_BLOCK_CANDIDATE": ("ACTIVE", "TESTED")}


def _ts(s) -> float:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def fractal_levels(bars: list, k: int = 3) -> list:
    """15M fractal swing highs/lows as liquidity levels: [{price, side, name}]."""
    out = []
    for i in range(k, len(bars) - k):
        if all(bars[i]["high"] > bars[j]["high"] for j in range(i - k, i + k + 1) if j != i):
            out.append({"price": float(bars[i]["high"]), "side": "HIGH", "name": f"SWING_HIGH_{str(bars[i]['time'])[11:16]}"})
        if all(bars[i]["low"] < bars[j]["low"] for j in range(i - k, i + k + 1) if j != i):
            out.append({"price": float(bars[i]["low"]), "side": "LOW", "name": f"SWING_LOW_{str(bars[i]['time'])[11:16]}"})
    return out


def first_target(direction: str, near_edge: float, risk: float, levels: list, min_rr: float):
    """The first liquidity level on the target side giving >= min_rr from the zone's near edge, or None."""
    buy = direction == "BUY"
    need = near_edge + min_rr * risk if buy else near_edge - min_rr * risk
    cands = [lv for lv in levels if lv["side"] == ("HIGH" if buy else "LOW")
             and ((lv["price"] >= need) if buy else (lv["price"] <= need))]
    if not cands:
        return None
    return min(cands, key=lambda lv: abs(lv["price"] - near_edge))


def candidates(evidence: dict, direction: str, kinds: tuple, price: float, atr15: float, max_dist: float,
               max_age_s: float, now_ts: float) -> list:
    """Usable 1H zones in `direction`, ahead of price (SELL: above, BUY: below), within max_dist x ATR15."""
    out = []
    for it in (evidence.get("index") or {}).values():
        if it.get("timeframe") != "1h" or it.get("kind") not in kinds or it.get("direction") != direction:
            continue
        if it.get("status") not in USABLE.get(it["kind"], ()):
            continue
        try:
            lo, hi = float(it["level_low"]), float(it["level_high"])
        except (KeyError, TypeError, ValueError):
            continue
        if hi <= lo or (now_ts - _ts(it.get("at")) > max_age_s):
            continue
        ahead = (lo >= price) if direction == "SELL" else (hi <= price)
        near = (lo - price) if direction == "SELL" else (price - hi)
        if ahead and near <= max_dist * atr15:
            out.append({"id": it["id"], "kind": it["kind"], "lo": lo, "hi": hi, "dist": near})
    out.sort(key=lambda z: z["dist"])
    return out


class H1Trend:
    def __init__(self, c: dict, app):
        self.c = c
        self.cfg = dict(DEFAULTS, **(c.get("h1_trend_lab") or {}))
        self.app = app
        self.seen_bar = None
        self.logged: set = set()
        if self.live:
            LOG.info("1H-trend lab ON | sides %s | zones %s | stop +%.2f ATR15 | min %.1f R | entries by the LTF 5M engine",
                     ",".join(self.cfg["sides"]), "/".join(self.cfg["zone_kinds"]), self.cfg["stop_buffer_atr15"],
                     self.cfg["min_rr"])

    @property
    def live(self) -> bool:
        ltf = getattr(self.app, "ltf", None)
        return self.cfg["mode"] == "lab" and bool(self.c.get("lab_mode")) and ltf is not None and ltf.live

    def tick(self, now: datetime) -> list:
        """Once per closed 15M candle: register LTF watches for the 1H-trend zones ahead of price."""
        if not self.live:
            return []
        evidence, atr15 = self.app.market_context()
        bar = evidence.get("built_at")
        if bar == self.seen_bar or not atr15:
            return []
        self.seen_bar = bar
        state, snap = self.app.market_state()
        trend = state.get("intermediate_trend")
        direction = {"BULLISH": "BUY", "BEARISH": "SELL"}.get(trend or "")
        if direction is None or direction not in self.cfg["sides"]:
            return []
        quote = self.app.market.quote()
        price = quote["ask"] if direction == "BUY" else quote["bid"]
        zones = candidates(evidence, direction, tuple(self.cfg["zone_kinds"]), price, float(atr15),
                           float(self.cfg["max_distance_atr15"]), float(self.cfg["max_zone_age_hours"]) * 3600,
                           now.timestamp())
        if not zones:
            return []
        levels = list(snap.get("levels") or []) + fractal_levels(self.app.recent_bars())
        macro = state.get("macro_trend")
        out = []
        for z in zones[:2]:
            sid = f"{PREFIX}{direction}-{round((z['lo'] + z['hi']) / 2)}"
            if sid in self.app.ltf.watches or sid in self.logged:
                continue
            buffer = float(self.cfg["stop_buffer_atr15"]) * float(atr15)
            buy = direction == "BUY"
            stop = round((z["lo"] - buffer) if buy else (z["hi"] + buffer), 2)
            near_edge = z["hi"] if buy else z["lo"]
            risk = abs(near_edge - stop)
            tgt = first_target(direction, near_edge, risk, levels, float(self.cfg["min_rr"]))
            if tgt is None:
                self.logged.add(sid)
                LOG.info("1H-trend %s: zone %.2f-%.2f (%s) has no liquidity target at >= %.1f R from %.2f; skipped", sid,
                         z["lo"], z["hi"], z["kind"], self.cfg["min_rr"], near_edge)
                continue
            tags = ["h1_trend", f"zone_{'fvg' if z['kind'] == 'FVG' else 'ob'}_1h",
                    "with_macro" if {"BULLISH": "BUY", "BEARISH": "SELL"}.get(macro or "") == direction else "against_macro"]
            title = (f"1H {trend}: {direction} the 1H {'gap' if z['kind'] == 'FVG' else 'block'} {z['lo']:.2f}-{z['hi']:.2f}, "
                     f"target {tgt['name']} {tgt['price']:.2f}")
            if self.app.ltf.add_watch(sid, direction, z["lo"], z["hi"], stop, round(tgt["price"], 2), tags, now, title=title,
                                      playbook="H1_TREND_RETEST"):
                self.logged.add(sid)
                out.append(sid)
                LOG.info("1H-trend %s registered: %s | stop %.2f | macro %s", sid, title, stop, macro)
        return out
