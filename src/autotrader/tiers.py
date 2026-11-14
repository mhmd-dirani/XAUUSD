"""
1.7 - tiers: Python grades every idea from facts it can verify and sizes it by tier.

Why: until 1.6 the trader took every idea the signal bot produced (idea_min_confidence 0, all risk
labels) at B size, while Astra's contract tells it to ALWAYS produce an idea. Three demo trades,
all B size, all counter-trend SELLs in the Asian session, all stopped. The strict A/B signal gate
sent nothing in three days. Neither path turned a correct read into a sized position.

confidence (0-100) is Astra's probability that TP1 is reached before the stop. For a 2R target 40 %
is a GOOD trade (+0.2R expectancy before costs); its track record so far is roughly calibrated
(30 %-band ideas hit 38 %). So confidence sets the floor per tier, and the tier itself comes from
facts Python checks itself, never from Astra's own label:

  with_trend   the idea's direction equals the macro trend (Daily + 4H) from closed candles
  session      London or New York (Asia and off-hours carried every loss so far)
  anchored     stop AND TP1 sit on remembered levels (Astra's sl_anchor / tp_anchors, checked by
               the signal bot's validate_analysis against market memory)
  no_expansion the regime module sees no strong one-way expansion AGAINST the idea. Against one
               WITHOUT a validated reversal: no trade when expansion_filter is "live", Tier C in "shadow"
  calm_risk    Astra's own risk label is not HIGH

Tier A  all five facts and confidence >= tiers.A.min_confidence   -> tiers.A.risk_pct (1.0 %)
Tier B  one fact missing (never the expansion one)                -> tiers.B.risk_pct (0.5 %)
Tier C  two or more missing, or counter-trend / reversal class    -> tiers.C.risk_pct (0.15 %); when
        that budget is below one minimum lot (0.01 lot on a 10k demo = a 15-point stop), ONE minimum
        lot is allowed as long as its risk stays within tiers.B.risk_pct (tiers.C.min_lot_if_below)
none    confidence below tiers.C.min_confidence, an expansion against, or a news lock -> no trade

Every tier decision records the facts and the reason, so the post-trade review can tell which fact
was missing on the trades that lost. Thresholds and risk per tier are config (settings.tiers).
"""

from __future__ import annotations

FACTS = ("with_trend", "session", "anchored", "no_expansion", "calm_risk")
# 1.10.6: sessions.session_of() reports the London/New York overlap as "LONDON_NY"; it was missing here, so every
# idea confirmed between 12:00 and 15:30 UTC lost the `session` fact and Tier A was impossible in the most liquid
# window (23 Oct 14:15 IDEA-SELL-4028: "Tier B: missing session" with session LONDON_NY). breakout.py already
# accepted the overlap. NOTE: this makes an all-facts overlap idea Tier A (tiers.A.risk_pct) instead of B.
ACTIVE_SESSIONS = ("LONDON", "NEW_YORK", "LONDON_NY")


def facts(idea: dict, direction: str, dec: dict | None, state: dict | None, session: str | None) -> dict:
    """The five verifiable facts as booleans (and the values they came from)."""
    st = state or {}
    macro = st.get("macro_trend")
    want = "BULLISH" if direction == "BUY" else "BEARISH"
    exp = (dec or {}).get("expansion") or {}
    anchors = idea.get("tp_anchors") or []
    out = {
        "with_trend": macro == want,
        "session": (session or "").upper() in ACTIVE_SESSIONS,
        "anchored": bool(idea.get("sl_anchor")) and bool(anchors and anchors[0]),
        "no_expansion": not bool(exp.get("against")),
        "calm_risk": (idea.get("astra_risk") or idea.get("risk")) != "HIGH",
        "_macro_trend": macro, "_session": session, "_trade_class": (dec or {}).get("trade_class"),
        "_confidence": idea.get("confidence"), "_astra_risk": idea.get("astra_risk") or idea.get("risk"),
    }
    return out


def grade(idea: dict, direction: str, dec: dict | None, state: dict | None, session: str | None, c: dict) -> dict:
    """{"tier": "A"|"B"|"C"|None, "risk_pct": float|None, "reason": str, "facts": {...}}"""
    f = facts(idea, direction, dec, state, session)
    tiers = c["tiers"]
    conf = int(idea.get("confidence") or 0)
    missing = [k for k in FACTS if not f[k]]
    cls = f["_trade_class"]
    why = []
    if conf < tiers["C"]["min_confidence"]:
        return {"tier": None, "risk_pct": None, "facts": f,
                "reason": f"confidence {conf}% below tiers.C.min_confidence {tiers['C']['min_confidence']}"}
    blocks = bool((dec or {}).get("would_block"))   # against a strong expansion with NO validated reversal
    if blocks and c.get("expansion_filter") == "live":
        return {"tier": None, "risk_pct": None, "facts": f,
                "reason": "a strong expansion runs against the idea and no CHoCH / failed breakout / sweep-and-reclaim "
                          "shows it failed (expansion_filter live)"}
    if blocks:
        why.append("fading an expansion without a validated reversal (expansion_filter shadow)")
    if blocks or (cls != "TREND"):
        tier = "C"
        why.append(f"class {cls}" if cls != "TREND" else "")
    elif not missing:
        tier = "A"
    elif len(missing) <= 1:
        tier = "B"
        why.append(f"missing {missing[0]}")
    else:
        tier = "C"
        why.append(f"missing {', '.join(missing)}")
    # the confidence floor of the tier; a lower confidence drops the idea one tier at a time
    order = ["A", "B", "C"]
    i = order.index(tier)
    while i < 3 and conf < tiers[order[i]]["min_confidence"]:
        why.append(f"confidence {conf}% < {order[i]}.min_confidence {tiers[order[i]]['min_confidence']}")
        i += 1
    if i == 3:
        return {"tier": None, "risk_pct": None, "facts": f, "reason": "; ".join(w for w in why if w)}
    tier = order[i]
    cap = (c.get("tier_cap") or {}).get(cls)
    if cap and order.index(cap) > order.index(tier):
        why.append(f"capped at {cap} for {cls} (tier_cap)")
        tier = cap
    text = ", ".join(w for w in why if w) or "all facts present"
    return {"tier": tier, "risk_pct": float(tiers[tier]["risk_pct"]), "facts": f,
            "min_lot_ok": tier == "C" and bool(tiers["C"].get("min_lot_if_below", True)),
            "reason": f"Tier {tier} ({tiers[tier]['risk_pct']}%): {text}"}
