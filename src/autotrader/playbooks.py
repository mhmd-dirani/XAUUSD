"""
Playbook routing: which kind of trade a candidate is, and whether the rules allow it.
The same functions decide live (bridge ideas) and in the historical replay.

trade_class (first match wins):
  TREND                direction not opposed by the 4H/1H structure (the v1.5 rule)
  COUNTER_TREND        opposed, but at a strong remembered Daily/4H zone (v1.5 rule; B size)
  REVERSAL_TRANSITION  opposed, no strong zone, but Python's phase is TRANSITION/REVERSAL in this
                       direction and the 15M has a CHoCH in this direction (new; B size)
  SESSION_SWEEP        opposed, confirmed by a sweep/failed breakout of a session, previous-day/week
                       or opening-range level (new; B size)
  NONE                 opposed with no justification -> not traded

Expansion filter (new): while a strong one-way leg is active AGAINST the trade, the trade needs at
least one reversal method (CHOCH / FAILED_BREAKOUT / BREAK_RETEST / SWEEP_RECLAIM, see regime.py).

Modes (config): every playbook runs "live" or "off" (1.7 removed paper); the expansion filter runs
"off", "shadow" or "live".
  live   orders go to the demo account
  shadow (expansion filter only) never blocks, but marks the trades it WOULD have blocked
"""

from __future__ import annotations

from . import regime as RG

CLASSES = ("TREND", "COUNTER_TREND", "REVERSAL_TRANSITION", "SESSION_SWEEP", "BREAKOUT_CONTINUATION")
DEFAULT_MODES = {"TREND": "live", "COUNTER_TREND": "live", "REVERSAL_TRANSITION": "live", "SESSION_SWEEP": "live",
                 "BREAKOUT_CONTINUATION": "off"}   # 1.8: off until the replay says otherwise
SESSION_CONFIRMATIONS = ("SESSION_SWEEP", "SESSION_FAILED_BREAKOUT")
B_ONLY = ("COUNTER_TREND", "REVERSAL_TRANSITION", "SESSION_SWEEP")


def mtf_opposed(direction: str, state: dict) -> bool:
    """Same rule as gold_evidence.mtf_opposed, on the market state (unknown counts as opposed)."""
    opp = "BEARISH" if direction == "BUY" else "BULLISH"
    return any(t is None or t == opp for t in (state.get("trend_4h"), state.get("intermediate_trend")))


def has_local_choch(direction: str, state: dict) -> bool:
    """The most recent 15M change of character is in `direction`."""
    fr = (state.get("_frames") or {}).get("15min") or {}
    for ev in reversed(fr.get("events") or []):
        if ev.get("kind") == "CHOCH":
            return ev.get("direction") == direction
    return False


def classify(direction: str, state: dict, *, strong_zone: bool, confirmation: str | None,
             veto: bool = True) -> tuple[str, str]:
    """(trade_class, reason). trade_class NONE means no rule allows it."""
    if not veto or not mtf_opposed(direction, state):
        return "TREND", "not opposed by 4H/1H structure"
    if strong_zone:
        return "COUNTER_TREND", "against 4H/1H, at a strong remembered Daily/4H zone"
    want = RG.SIDE[direction]
    if state.get("phase") in ("TRANSITION", "REVERSAL") and state.get("phase_direction") == want \
            and has_local_choch(direction, state):
        return "REVERSAL_TRANSITION", f"phase {state['phase']} {want} with a 15M CHoCH"
    if confirmation in SESSION_CONFIRMATIONS:
        return "SESSION_SWEEP", f"against 4H/1H, confirmed by a {confirmation.lower()} of a session level"
    return "NONE", "against 4H/1H structure with no strong zone, no reversal transition, no session sweep"


def expansion_check(direction: str, state: dict, bars15: list, levels: list | None = None) -> dict:
    """{'against': bool, 'methods': [...], 'blocks': bool} for the expansion filter."""
    e = state.get("expansion") or {}
    against = bool(e.get("active")) and e.get("direction") != RG.SIDE[direction]
    methods = RG.reversal_methods(direction, state, bars15, levels) if against else []
    return {"against": against, "methods": methods, "blocks": against and not methods,
            "expansion": {k: e.get(k) for k in ("direction", "size_baseline", "origin", "extreme", "extreme_at")}
            if against else None}


def decide(direction: str, state: dict, bars15: list, *, strong_zone: bool, confirmation: str | None,
           modes: dict, expansion_mode: str, levels: list | None = None, veto: bool = True) -> dict:
    """The full routing decision for one candidate:
    {trade_class, reason, mode: live|off, expansion: {...}, would_block: bool}"""
    cls, why = classify(direction, state, strong_zone=strong_zone, confirmation=confirmation, veto=veto)
    exp = expansion_check(direction, state, bars15, levels)
    mode = "off" if cls == "NONE" else modes.get(cls, "off")
    if exp["blocks"]:
        if expansion_mode == "live":
            why = (f"blocked: strong {exp['expansion']['direction'].lower()} expansion "
                   f"({exp['expansion']['size_baseline']}x) without a validated reversal")
            mode = "off"
    return {"trade_class": cls, "reason": why, "mode": mode, "expansion": exp,
            "would_block": exp["blocks"]}
