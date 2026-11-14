"""
Market state from CLOSED candles only (the same code runs live and in the historical replay).

Five separate classifications, never collapsed into one bullish/bearish flag:

  macro_trend         Daily + 4H structure            BULLISH | BEARISH | MIXED | NEUTRAL
  intermediate_trend  1H structure                    BULLISH | BEARISH | NEUTRAL
  local_trend         15M structure                   BULLISH | BEARISH | NEUTRAL
  phase               how local relates to macro      CONTINUATION | PULLBACK | TRANSITION | REVERSAL | RANGE
                      (+ phase_direction)
  volatility          recent 15M range vs its norm    EXPANDING | NORMAL | CONTRACTING
  momentum            last 4 vs previous 4 closes     ACCELERATING | STEADY | DECELERATING | REVERSING | FLAT
                      (+ momentum_direction)
  expansion           a strong directional leg that is still fresh (active, direction, size, extreme)

"Structure trend" (None = timeframe unavailable, which the veto counts as opposed) is the direction of the last confirmed break of a swing (the SMC engine's trend,
identical to what the 4H/1H veto uses). A bearish 4H, a bullish 15M CHoCH and a bullish 1H can all
be true at once; the phase says what that combination means:

  local = macro, 1H = macro        CONTINUATION
  local = macro, 1H against        PULLBACK (correction that the 15M has already resumed)
  local against macro, 1H not yet  TRANSITION in the local direction (early reversal, unproven)
  local and 1H against macro       REVERSAL in the local direction (not yet on 4H/Daily)
  macro mixed/neutral              TRANSITION if 15M and 1H agree, otherwise RANGE

Units: "baseline" is the median 15M true range of the last 96 closed bars (one day), so a news
spike does not inflate the yardstick it is measured with.
"""

from __future__ import annotations

from datetime import datetime
from statistics import median
from typing import Any

from gold_smc_engine import analyze_frame, _time

DEFAULTS = {
    "regime_window": 700,             # bars per timeframe given to the structure engine (= live history)
    "vol_expanding": 1.6,             # mean TR of the last 8 bars >= 1.6 x baseline
    "vol_contracting": 0.6,
    "expansion_min_baseline": 6.0,    # leg >= 6 x baseline inside the last 16 bars ...
    "expansion_fresh_bars": 8,        # ... with its extreme in the last 8 bars ...
    "expansion_max_retrace": 0.5,     # ... and less than half of it given back
}
BULL, BEAR = "BULLISH", "BEARISH"
SIDE = {"BUY": BULL, "SELL": BEAR}


def _dir(direction: str) -> str:
    return SIDE.get(direction, direction)


def frame(bars: list, tf: str, as_of: datetime | None, window: int) -> dict:
    """Structure trend, ATR and structure events of one timeframe (fail closed: NEUTRAL)."""
    if not bars or len(bars) < 20:
        return {"trend": None, "atr": None, "events": [], "swings": [], "available": False}
    try:
        smc = analyze_frame(bars[-window:], tf, as_of=as_of)
    except Exception as exc:  # noqa: BLE001
        return {"trend": None, "atr": None, "events": [], "swings": [], "available": False,
                "error": str(exc)[:120]}
    return {"trend": smc["trend"] if smc["trend"] in (BULL, BEAR) else "NEUTRAL", "atr": smc["atr"],
            "events": smc["events"], "swings": smc["swings"], "available": True}


def _tr(bars: list) -> list:
    out = []
    for i, b in enumerate(bars):
        pc = bars[i - 1]["close"] if i else b["open"]
        out.append(max(b["high"], pc) - min(b["low"], pc))
    return out


def macro_of(d1: str | None, h4: str | None) -> str:
    d1, h4 = d1 or "NEUTRAL", h4 or "NEUTRAL"
    if d1 == h4 and d1 in (BULL, BEAR):
        return d1
    if d1 in (BULL, BEAR) and h4 == "NEUTRAL":
        return d1
    if h4 in (BULL, BEAR) and d1 == "NEUTRAL":
        return h4
    return "MIXED" if {d1, h4} == {BULL, BEAR} else "NEUTRAL"


def phase_of(macro: str, inter: str, local: str, volatility: str) -> tuple[str, str | None]:
    opp = {BULL: BEAR, BEAR: BULL}
    inter, local = inter or "NEUTRAL", local or "NEUTRAL"
    if macro in (BULL, BEAR):
        if local == macro:
            return ("CONTINUATION", macro) if inter == macro else ("PULLBACK", macro)
        if local == opp[macro]:
            return ("REVERSAL", local) if inter == local else ("TRANSITION", local)
        return "PULLBACK", macro
    if local in (BULL, BEAR) and inter == local:
        return "TRANSITION", local
    return "RANGE", None


def volatility_of(bars15: list, cfg: dict) -> dict:
    tr = _tr(bars15[-97:])[1:] if len(bars15) > 1 else []
    if len(tr) < 20:
        return {"volatility": "NORMAL", "baseline": None, "vol_ratio": None}
    base = median(tr) or 1e-9
    recent = sum(tr[-8:]) / len(tr[-8:])
    ratio = recent / base
    state = "EXPANDING" if ratio >= cfg["vol_expanding"] else "CONTRACTING" if ratio <= cfg["vol_contracting"] else "NORMAL"
    return {"volatility": state, "baseline": round(base, 3), "vol_ratio": round(ratio, 2)}


def momentum_of(bars15: list, base: float | None) -> dict:
    if len(bars15) < 9 or not base:
        return {"momentum": "FLAT", "momentum_direction": "NONE", "momentum_baseline": 0.0}
    c = [b["close"] for b in bars15[-9:]]
    n1, n2 = (c[8] - c[4]) / base, (c[4] - c[0]) / base
    sign = lambda x: BULL if x > 0 else BEAR
    if abs(n1) < 0.5 and abs(n2) < 0.5:
        state, d = "FLAT", "NONE"
    elif n1 * n2 > 0:
        if abs(n1) >= 1.25 * abs(n2) and abs(n1) >= 1.0:
            state = "ACCELERATING"
        elif abs(n1) <= 0.5 * abs(n2):
            state = "DECELERATING"
        else:
            state = "STEADY"
        d = sign(n1 if abs(n1) >= 0.5 else n2)
    elif abs(n1) >= 1.0:
        state, d = "REVERSING", sign(n1)
    else:
        state, d = "DECELERATING", sign(n2) if abs(n2) >= 0.5 else "NONE"
    return {"momentum": state, "momentum_direction": d, "momentum_baseline": round(n1, 2)}


def expansion_of(bars15: list, base: float | None, cfg: dict) -> dict:
    """The largest one-way leg in the last 16 closed bars, if strong, fresh and not half given back."""
    none = {"active": False, "direction": None}
    w = bars15[-16:]
    if len(w) < 8 or not base:
        return none
    best = None
    for i, a in enumerate(w):                         # up-legs: low at i, later high at j
        for j in range(i, len(w)):
            up, down = w[j]["high"] - a["low"], a["high"] - w[j]["low"]
            for size, d, origin, extreme in ((up, BULL, a["low"], w[j]["high"]), (down, BEAR, a["high"], w[j]["low"])):
                if best is None or size > best[0]:
                    best = (size, d, origin, extreme, i, j)
    size, d, origin, extreme, i, j = best
    last = w[-1]["close"]
    if size <= 0:
        return none
    retrace = (extreme - last) / size if d == BULL else (last - extreme) / size
    fresh = j >= len(w) - cfg["expansion_fresh_bars"]
    active = size >= cfg["expansion_min_baseline"] * base and fresh and retrace < cfg["expansion_max_retrace"]
    return {"active": bool(active), "direction": d if active else None, "leg_direction": d,
            "size_baseline": round(size / base, 1), "origin": round(origin, 2), "extreme": round(extreme, 2),
            "origin_at": w[i]["time"], "extreme_at": w[j]["time"], "retrace": round(max(0.0, retrace), 2)}


def market_state(history: dict, as_of: datetime | None = None, cfg: dict | None = None,
                 frames: dict | None = None) -> dict[str, Any]:
    """`frames` may hold already computed frame() results (the replay reuses higher timeframes
    until their next candle closes); missing timeframes are computed here."""
    cfg = dict(DEFAULTS, **(cfg or {}))
    win = cfg["regime_window"]
    fr = dict(frames or {})
    for tf in ("1day", "4h", "1h", "15min"):
        if tf not in fr:
            fr[tf] = frame(history.get(tf) or [], tf, as_of, win)
    bars15 = history.get("15min") or []
    macro = macro_of(fr["1day"]["trend"], fr["4h"]["trend"])
    vol = volatility_of(bars15, cfg)
    phase, pdir = phase_of(macro, fr["1h"]["trend"], fr["15min"]["trend"], vol["volatility"])
    st = {"as_of": as_of.isoformat() if as_of else None,
          "macro_trend": macro, "trend_1day": fr["1day"]["trend"], "trend_4h": fr["4h"]["trend"],
          "intermediate_trend": fr["1h"]["trend"], "local_trend": fr["15min"]["trend"],
          "phase": phase, "phase_direction": pdir, **vol}
    st.update(momentum_of(bars15, vol["baseline"]))
    st["expansion"] = expansion_of(bars15, vol["baseline"], cfg)
    st["atr15"] = fr["15min"]["atr"]
    st["_frames"] = fr                                   # internal: events/swings for reversal checks
    return st


def from_evidence(evidence: dict | None) -> dict:
    """A minimal state carrying only the evidence's structure trends (no 15M events, no expansion)."""
    fr = (evidence or {}).get("frames") or {}
    t = {tf: (fr.get(tf) or {}).get("trend") for tf in ("1day", "4h", "1h", "15min")}
    t = {k: (v if v in (BULL, BEAR, "NEUTRAL") else None) for k, v in t.items()}
    macro = macro_of(t["1day"], t["4h"])
    phase, pdir = phase_of(macro, t["1h"], t["15min"], "NORMAL")
    return {"as_of": (evidence or {}).get("built_at"), "macro_trend": macro, "trend_1day": t["1day"],
            "trend_4h": t["4h"], "intermediate_trend": t["1h"], "local_trend": t["15min"], "phase": phase,
            "phase_direction": pdir, "volatility": "NORMAL", "baseline": None, "vol_ratio": None,
            "momentum": "FLAT", "momentum_direction": "NONE", "momentum_baseline": 0.0,
            "expansion": {"active": False, "direction": None}, "atr15": None,
            "_frames": {tf: {"trend": t[tf], "events": [], "swings": []} for tf in t}}


def public(state: dict) -> dict:
    """The state without internal fields (for the journal, Telegram and JSON)."""
    return {k: v for k, v in state.items() if not k.startswith("_")}


def summary(state: dict) -> str:
    e = state.get("expansion") or {}
    exp = f" | expansion {e['direction']} {e['size_baseline']}x" if e.get("active") else ""
    pd = f" {state['phase_direction']}" if state.get("phase_direction") else ""
    return (f"macro {state['macro_trend']} (D {state['trend_1day']}, 4H {state['trend_4h']}) | 1H "
            f"{state['intermediate_trend']} | 15M {state['local_trend']} | {state['phase']}{pd} | vol "
            f"{state['volatility']} | mom {state['momentum']} {state['momentum_direction']}{exp}")


# ---------------------------------------------------------------------- reversal validation
def reversal_methods(direction: str, state: dict, bars15: list, levels: list | None = None) -> list[str]:
    """Objective evidence, on closed 15M candles AFTER the expansion extreme, that a strong move
    against `direction` has failed or materially weakened. Any one is enough (not sweep-only):

      CHOCH            a confirmed 15M change of character in `direction` after the extreme
      FAILED_BREAKOUT  the expansion closed beyond a prior 15M swing level, then a later candle
                       closed back on the other side of it
      BREAK_RETEST     a close through the leg's last 15M swing (the last higher low / lower high),
                       then a candle retests that level and closes on the reversal side
      SWEEP_RECLAIM    a candle at/after the extreme wicks through a prior swing or session level
                       and closes back inside, in `direction`
    Returns the methods found (empty = the move has not failed)."""
    e = state.get("expansion") or {}
    want = _dir(direction)
    if not e.get("active") or e["direction"] == want:
        return []
    ext_at = _time(e["extreme_at"])
    up = e["direction"] == BULL                      # the expansion we are fading went up
    fr = (state.get("_frames") or {}).get("15min") or {}
    base = state.get("baseline") or 1.0
    after = [b for b in bars15 if _time(b["time"]) >= ext_at]
    later = [b for b in after if _time(b["time"]) > ext_at]
    origin_at = _time(e["origin_at"])
    found = []

    for ev in fr.get("events") or []:
        if ev.get("kind") == "CHOCH" and ev.get("timeframe") == "15min" and SIDE.get(ev.get("direction")) == want \
                and _time(ev["at"]) > ext_at:
            found.append("CHOCH")
            break

    swings = fr.get("swings") or []
    prior = [s for s in swings if _time(s["time"]) < origin_at and s["side"] == ("HIGH" if up else "LOW")]
    if prior:
        lvl = prior[-1]["price"]
        broke = (e["extreme"] > lvl) if up else (e["extreme"] < lvl)
        if broke and any((b["close"] < lvl) if up else (b["close"] > lvl) for b in later):
            found.append("FAILED_BREAKOUT")

    inside = [s for s in swings if origin_at < _time(s["time"]) < ext_at and s["side"] == ("LOW" if up else "HIGH")]
    if inside:
        lvl = inside[-1]["price"]
        broken_at = next((k for k, b in enumerate(later) if (b["close"] < lvl if up else b["close"] > lvl)), None)
        if broken_at is not None:
            for b in later[broken_at + 1:]:
                near = (b["high"] >= lvl - 0.3 * base) if up else (b["low"] <= lvl + 0.3 * base)
                if near and ((b["close"] < lvl and b["close"] < b["open"]) if up else (b["close"] > lvl and b["close"] > b["open"])):
                    found.append("BREAK_RETEST")
                    break

    refs = [s["price"] for s in prior[-3:]] + [l["price"] for l in (levels or [])
                                               if l["side"] == ("HIGH" if up else "LOW")]
    for b in after:
        if any((b["high"] > p and b["close"] < p and b["close"] < b["open"]) if up else
               (b["low"] < p and b["close"] > p and b["close"] > b["open"]) for p in refs):
            found.append("SWEEP_RECLAIM")
            break
    return found
