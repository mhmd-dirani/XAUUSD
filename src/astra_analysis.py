#!/usr/bin/env python3
"""Astra V6 — analyst layer: narrative schema, contract, soft validation and Telegram reports.

Astra now explains the market like a trader sitting beside you: the market story, the
historical context from Python's structure memory, 1-3 measurable scenarios, optional TRADE
IDEAs (each with an honest LOW/MEDIUM/HIGH risk class) and STRUCTURE WATCHes.

These are ANALYST REPORTS, never signals. The V5 executable setup/BUY/SELL path keeps every
check (playbook evidence, ordered confirmations, 2R, news, calendar, broker quote). Nothing in
this module can approve, arm or release a signal. Invalid analyst items are dropped one by one;
they never cause the executable market map to be rejected.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone

UTC = timezone.utc
TRIGGERS = ("CLOSE_ABOVE", "CLOSE_BELOW", "SWEEP_RECLAIM_UP", "SWEEP_RECLAIM_DOWN", "BULL_REJECTION", "BEAR_REJECTION")
BULL_TRIGGERS = ("CLOSE_ABOVE", "SWEEP_RECLAIM_UP", "BULL_REJECTION")
BEAR_TRIGGERS = ("CLOSE_BELOW", "SWEEP_RECLAIM_DOWN", "BEAR_REJECTION")
RISK_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


def _obj(props):
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _enum(*v):
    return {"type": "string", "enum": list(v)}


def _arr(items):
    return {"type": "array", "items": items}


_S, _N, _I = {"type": "string"}, {"type": "number"}, {"type": "integer"}
_NN = {"type": ["number", "null"]}

SCENARIO = _obj({
    "id": _S, "title": _S, "bias": _enum("BULLISH", "BEARISH", "NEUTRAL"),
    "likelihood": _enum("PRIMARY", "ALTERNATIVE", "LOW"),
    "trigger_type": _enum(*TRIGGERS), "trigger_timeframe": _enum("15min", "1h", "4h"), "trigger_level": _N,
    "objective_level": _NN, "invalidation_level": _NN, "horizon_hours": _I,
    "memory_ids": _arr(_S), "description": _S})
IDEA = _obj({
    "id": _S, "direction": _enum("BUY", "SELL"), "risk": _enum("LOW", "MEDIUM", "HIGH"),
    "confidence": _I, "entry_type": _enum("MARKET", "LIMIT", "BREAKOUT"),
    "readiness": _enum("DEVELOPING", "NEEDS_CONFIRMATION", "VALID_NOW"),
    "title": _S, "reasoning": _S, "entry_low": _N, "entry_high": _N,
    "invalidation": _N, "invalidation_text": _S, "targets": _arr(_N), "targets_text": _S,
    "confirmation": _S, "risk_factors": _arr(_S), "safer_alternative": _S,
    "memory_ids": _arr(_S), "scenario_id": _S,
    # v6.2 (auto-trader 1.6): Astra states which kind of trade this is; Python re-classifies from
    # closed candles and its own class decides (a mismatch is logged and journalled).
    "trade_class": _enum("TREND", "COUNTER_TREND", "REVERSAL_TRANSITION"),
    "macro_trend": _enum("BULLISH", "BEARISH", "MIXED"),
    "local_trend": _enum("BULLISH", "BEARISH", "NEUTRAL"),
    "market_phase": _enum("CONTINUATION", "PULLBACK", "TRANSITION", "REVERSAL", "RANGE")})
WATCH = _obj({
    "id": _S, "memory_id": _S, "title": _S, "situation": _S, "interpretation": _S,
    "outcomes": _arr(_obj({"if": _S, "then": _S})), "waiting_for": _S})
ANALYSIS_SCHEMA = _obj({
    "stance": _enum("TRADE_IDEA", "STRUCTURE_WATCH", "NO_TRADE"),
    "market_story": _S, "historical_context": _S,
    "scenarios": _arr(SCENARIO), "ideas": _arr(IDEA), "watches": _arr(WATCH),
    "no_trade_reason": _S})
# The model is asked for every field (strict structured output needs all of them). Python's own
# check accepts ideas without the v6.2 classification fields (older saved analyses, older prompts).
CLASS_FIELDS = ("trade_class", "macro_trend", "local_trend", "market_phase")
VALIDATION_SCHEMA = copy.deepcopy(ANALYSIS_SCHEMA)
VALIDATION_SCHEMA["properties"]["ideas"]["items"]["required"] = [
    k for k in IDEA["required"] if k not in CLASS_FIELDS]

CONTRACT = """
V6 ASTRA ANALYST CONTRACT (adds payload.market_memory and the required `analysis` object).
You are a professional XAU/USD analyst sitting beside a trader, not an alert generator.
Ask: what is the market doing, which remembered levels matter, which scenarios are developing,
and when does an opportunity become interesting? `setups` remain the strict executable
playbook plans above (unchanged rules); `analysis` is your trader reasoning for the human.

payload.market_memory is Python's persistent chart memory from closed candles: zones with ids,
creation date, separate tests, last test, average reaction (ATR), status ACTIVE / BROKEN (role
flipped, retest waiting) / FLIPPED (retest held), false breakouts, liquidity pools (equal
highs/lows, SWEPT/TAKEN), recent structure events, persistent SMC zones and your own
track_record of resolved scenarios. Use it the way a trader remembers a chart: "this is the same
resistance that rejected price three times since Monday". Never invent history that is not in it.
A level with many tests is not automatically stronger: it may be absorbing orders before a break.

analysis.market_story: what price is doing now and why it matters (2-4 sentences).
analysis.historical_context: the remembered structures that matter now, with their history.
analysis.scenarios: 1-3 measurable scenarios (exactly one PRIMARY when any exist). Each needs a
closed-candle trigger (same predicates as setup conditions) at a real level, objective/invalidation
levels (null only for NEUTRAL) and horizon_hours 2..72. BULLISH uses CLOSE_ABOVE/SWEEP_RECLAIM_UP/
BULL_REJECTION, BEARISH the mirrored types. Python scores every scenario later; your track record
comes back to you, so calibrate likelihoods honestly.
analysis.ideas: 1-3 TRADE IDEAs. ALWAYS give at least one: your best trade in the direction you lean,
like a professional trader who must state a plan. The FIRST idea is your main call. Only return zero
ideas (stance NO_TRADE) when the data itself is unusable. A weak or risky idea is allowed and must be
labelled honestly instead of withheld.
EVALUATE BOTH DIRECTIONS every time. When price is at, or within about one daily ATR of, a strong
remembered Daily/4H zone on the OTHER side of your lean (a support while you lean bearish, a resistance
while you lean bullish; tested at least twice, still holding), also give a COUNTER-TREND idea there:
title starting "Counter-trend:", entry at that zone, stop beyond the zone or the sweep extreme, TP1 at the
NEAREST structure in that direction (an intermediate level, never the far opposite zone), risk MEDIUM or
HIGH, and an objective closed-candle confirmation: a 15M rejection candle, a sweep below/above the zone
that closes back inside it, or a 15M CHoCH. Both ideas stay valid together, each with its own stop: a
SELL zone above never cancels a BUY at support. Never invent a counter-trend idea when no strong zone is
near; never force one because a scenario exists. A REVERSAL-TRANSITION idea is different: no strong
zone is needed, but the 15M (and ideally the 1H) must already have changed character against the 4H/Daily
direction, e.g. 4H bearish, 15M bullish CHoCH, price reclaiming a broken level. Label it, keep risk MEDIUM
or HIGH, and never use it to fade a strong one-way expansion before that move has visibly failed (a CHoCH,
a failed breakout, a break and retest, or a sweep and reclaim). Each idea:
- trade_class: TREND (with the 4H/1H structure), COUNTER_TREND (against it, at a strong remembered zone)
  or REVERSAL_TRANSITION (against it, after a lower-timeframe change of character). macro_trend = Daily+4H,
  local_trend = 15M, market_phase = CONTINUATION / PULLBACK / TRANSITION / REVERSAL / RANGE. These are
  claims: Python checks them against the closed candles and its own classification decides.
- confidence: 0-100 = your honest probability that TP1 is reached before the stop (calibrate; Python
  scores every idea against what price does and reports your hit rate per confidence band).
- risk: LOW = several confirmations align at a validated structure with clear invalidation; MEDIUM =
  valid but some confirmation missing; HIGH = aggressive entry, extended price or incomplete confirmation.
- entry_type: MARKET = enter around the current price (entry zone must contain it); LIMIT = wait for a
  pullback into a better zone; BREAKOUT = enter only when price trades through the zone.
- entry_low/entry_high: the exact entry zone. invalidation = the STOP LOSS, placed beyond real structure
  (a swing, a remembered zone edge or a sweep extreme), never a convenient round number.
- targets: 1-3 take-profits AT remembered levels from market_memory / map levels (zone edges, liquidity
  pools, swing points), nearest first. Name the levels in targets_text and invalidation_text.
- risk_factors, confirmation, safer_alternative: concrete. Python recomputes R:R, checks that the stop
  and targets sit on remembered levels and may raise (never lower) the risk. Ideas are NOT signals.
analysis.watches: 0-2 STRUCTURE WATCHes for remembered levels where the market is making a decision;
memory_id must be a market_memory zone id. Outcomes are "if X -> then Y" pairs.
Reasoning style: think and explain naturally; mention FVG, order blocks, Fibonacci, BOS, liquidity
or indicators ONLY when they matter for this situation. Do not fill a checklist.
stance: TRADE_IDEA whenever an idea is given (normally always); NO_TRADE only with unusable data, with
a plain no_trade_reason.
All prices must come from supplied candles/memory. Write all text in payload.language, concise.
payload.trader_playbook (when present) holds your trader's principles, learned in teaching sessions
from undated chart images. Apply a principle only when the live structure genuinely matches its
situation; fresh candles override every principle; never take prices from it. When a principle
shapes your view, name it in historical_context (e.g. "Trader principle P3: wait for the sweep").
Principles with situation DEFINITION are your trader's own definitions of concepts (how THEY identify an
FVG, order block, MSS, breaker, spring...). When you identify or name those concepts, use their definition.
"""


# ---------------------------------------------------------------------------- trader playbook

SITUATIONS = ("REPEATED_REJECTION", "DEFENDED_SUPPORT", "BREAKOUT_RETEST", "FALSE_BREAKOUT",
              "LIQUIDITY_SWEEP", "RANGE", "TREND_PULLBACK", "COMPRESSION", "DEFINITION", "OTHER")
_PLAYBOOK_CACHE = {}


def load_playbook(path, max_principles=12, max_chars=9000):
    """Trader principles produced by tools/learn_lessons.py. Missing/invalid file -> None.
    Cached by modification time; capped so it can never bloat the hourly payload."""
    from pathlib import Path
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return None
    key = (str(p.resolve()), st.st_mtime_ns, st.st_size)
    if key in _PLAYBOOK_CACHE:
        return _PLAYBOOK_CACHE[key]
    out = None
    try:
        if st.st_size <= 200_000:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
            items = []
            for pr in data.get("principles", [])[:max_principles]:
                if isinstance(pr, dict) and isinstance(pr.get("rule"), str) and pr["rule"].strip():
                    items.append({"id": str(pr.get("id", ""))[:8], "situation": pr.get("situation")
                                  if pr.get("situation") in SITUATIONS else "OTHER",
                                  "rule": pr["rule"][:700], "when_not": str(pr.get("when_not", ""))[:300]})
            while items and len(json.dumps(items, ensure_ascii=False)) > max_chars:
                items.pop()
            if items:
                out = {"source": "teaching sessions (undated chart images)", "updated_at": data.get("updated_at"),
                       "principles": items,
                       "rule": "Trader's principles; apply only when live structure matches. Never a signal."}
    except Exception:
        out = None
    _PLAYBOOK_CACHE.clear()
    _PLAYBOOK_CACHE[key] = out
    return out

# ---------------------------------------------------------------------------- validation

def _in_range(x, lo, hi):
    return x is not None and lo <= x <= hi


def anchor_levels(memory_zones, map_levels):
    """Candidate levels for TP/SL labels: live memory zones first, then Astra's validated map levels."""
    out = list(memory_zones or [])
    for lv in map_levels or []:
        out.append({"id": lv["id"], "timeframe": lv["timeframe"], "role": lv["kind"],
                    "low": lv["low"], "high": lv["high"], "tests": 0})
    return out


def anchor(price, zones, tol):
    """Nearest remembered zone within `tol` of a price (for TP/SL explanations)."""
    best = None
    for z in zones or []:
        d = 0.0 if z["low"] <= price <= z["high"] else min(abs(price - z["low"]), abs(price - z["high"]))
        if d <= tol and (best is None or d < best[0]):
            best = (d, z)
    return best[1] if best else None


def validate_analysis(raw, *, price, daily_atr, h1_atr, price_low, price_high, memory_ids,
                      minimum_rr, mtf_opposed=None, calendar_block=None, language="Arabic", zones=None):
    """Return (clean_analysis, dropped_reasons). Never raises for content problems."""
    from jsonschema import validate
    dropped = []
    if raw is None:
        return None, ["analysis missing"]
    try:
        validate(raw, VALIDATION_SCHEMA)
    except Exception as exc:
        return None, ["analysis schema: " + str(exc).split("\n")[0][:200]]
    a = copy.deepcopy(raw)
    L = labels(language)
    span = 3 * (daily_atr or price * 0.01)
    lo_ok, hi_ok = min(price_low, price) - span, max(price_high, price) + span

    def mem(ids):
        return [i for i in dict.fromkeys(ids) if i in memory_ids]

    scen, seen, primary = [], set(), False
    for sc in a["scenarios"]:
        if len(scen) == 3:
            dropped.append(f"scenario {sc['id']}: more than 3 scenarios")
            continue
        why = None
        if sc["id"] in seen or not sc["id"]:
            why = "duplicate id"
        elif not _in_range(sc["trigger_level"], lo_ok, hi_ok):
            why = "trigger level outside observed range"
        elif not 2 <= sc["horizon_hours"] <= 72:
            why = "horizon outside 2..72h"
        elif sc["bias"] == "BULLISH" and sc["trigger_type"] not in BULL_TRIGGERS:
            why = "bearish trigger on bullish scenario"
        elif sc["bias"] == "BEARISH" and sc["trigger_type"] not in BEAR_TRIGGERS:
            why = "bullish trigger on bearish scenario"
        elif sc["bias"] == "NEUTRAL":
            sc["objective_level"] = sc["invalidation_level"] = None   # a neutral scenario is scored on its trigger
        elif sc["bias"] != "NEUTRAL":
            obj, inv = sc["objective_level"], sc["invalidation_level"]
            if obj is None or inv is None:
                why = "directional scenario needs objective and invalidation"
            elif not (_in_range(obj, lo_ok, hi_ok) and _in_range(inv, lo_ok, hi_ok)):
                why = "objective/invalidation outside observed range"
            elif sc["bias"] == "BULLISH" and not inv < sc["trigger_level"] < obj:
                why = "bullish geometry (invalidation < trigger < objective)"
            elif sc["bias"] == "BEARISH" and not obj < sc["trigger_level"] < inv:
                why = "bearish geometry (objective < trigger < invalidation)"
        if why:
            dropped.append(f"scenario {sc['id']}: {why}")
            continue
        if sc["likelihood"] == "PRIMARY":
            if primary:
                sc["likelihood"] = "ALTERNATIVE"
            primary = True
        sc["memory_ids"] = mem(sc["memory_ids"])
        seen.add(sc["id"])
        scen.append(sc)
    a["scenarios"] = scen[:3]

    ideas = []
    for idea in a["ideas"]:
        d = idea["direction"]
        lo, hi, inv, ts = idea["entry_low"], idea["entry_high"], idea["invalidation"], idea["targets"]
        why = None
        if not 1 <= len(ts) <= 3:
            why = "needs 1..3 targets"
        elif not all(_in_range(x, lo_ok, hi_ok) for x in [lo, hi, inv] + ts):
            why = "prices outside observed range"
        elif not 0 < lo <= hi or (hi - lo) > (daily_atr or price) * 1.0:
            why = "entry zone invalid or wider than one daily ATR"
        elif d == "BUY" and not (inv < lo and hi < ts[0] and ts == sorted(set(ts))):
            why = "BUY geometry (invalidation < entry < targets ascending)"
        elif d == "SELL" and not (inv > hi and lo > ts[0] and ts == sorted(set(ts), reverse=True)):
            why = "SELL geometry (invalidation > entry > targets descending)"
        elif (d == "BUY" and price <= inv) or (d == "SELL" and price >= inv):
            why = "price already through invalidation"
        if why:
            dropped.append(f"idea {idea['id']}: {why}")
            continue
        edge = hi if d == "BUY" else lo
        risk_dist = abs(edge - inv)
        rr = abs(ts[0] - edge) / risk_dist if risk_dist > 0 else 0.0
        idea["rr_tp1"] = round(rr, 2)
        idea["confidence"] = max(0, min(100, int(idea["confidence"])))
        idea["memory_ids"] = mem(idea["memory_ids"])
        factors = []           # Python's factors first, so they can never be cut off
        risk = idea["risk"]

        def raise_to(level, text):
            nonlocal risk
            if RISK_RANK[level] > RISK_RANK[risk]:
                risk = level
            factors.append(text)
        if rr < 1.0:
            raise_to("HIGH", L["f_rr_low"].format(rr=rr))
        elif rr < minimum_rr:
            raise_to("MEDIUM", L["f_rr"].format(rr=rr, min=minimum_rr))
        if mtf_opposed and mtf_opposed(d):
            raise_to("MEDIUM", L["f_mtf"])
        if calendar_block and str(calendar_block).startswith("USD holiday"):
            raise_to("MEDIUM", L["f_holiday"].format(event=calendar_block))     # thin liquidity, not a release
        elif calendar_block:
            raise_to("HIGH", L["f_news"].format(event=calendar_block))
        datr = daily_atr or price * 0.01
        tp_tol = max(0.05 * datr, 0.5)       # a target must sit ON a level (first real dry run: 0.15 ATR
        sl_tol = 0.15 * datr                 # labelled a TP $10 below a zone as "at" it)
        idea["tp_anchors"] = [(lambda z: z and {"id": z["id"], "timeframe": z["timeframe"], "role": z["role"],
                                                "low": z["low"], "high": z["high"], "tests": z.get("tests", 0)})
                              (anchor(t, zones, tp_tol)) for t in ts]
        sl_zone = anchor(inv, zones, sl_tol)
        idea["sl_anchor"] = sl_zone and {"id": sl_zone["id"], "timeframe": sl_zone["timeframe"],
                                         "role": sl_zone["role"], "low": sl_zone["low"], "high": sl_zone["high"],
                                         "tests": sl_zone.get("tests", 0)}
        if zones is not None and not idea["tp_anchors"][0]:
            factors.append(L["f_tp_free"])
        near = 0.25 * (h1_atr or 0)
        if idea["entry_type"] == "MARKET" and not lo - near <= price <= hi + near:
            idea["entry_type"] = "LIMIT"                       # price is not at the entry: it is a pending plan
        chase = (price - hi) if d == "BUY" else (lo - price)
        idea["distance_atr"] = round(chase / h1_atr, 2) if h1_atr else None      # 6.1.7: for the posting floor
        if h1_atr and chase > 0.5 * h1_atr:
            # a LIMIT idea is meant to wait for price; say how far, not "you are chasing"
            factors.append(L["f_wait"].format(d=chase) if idea["entry_type"] == "LIMIT" else L["f_chase"])
        if idea["readiness"] == "VALID_NOW" and not lo <= price <= hi:
            idea["readiness"] = "NEEDS_CONFIRMATION"       # "valid now" only while price is in the entry zone
        idea["python_risk_raised"] = risk != idea["risk"]
        idea["astra_risk"], idea["risk"] = idea["risk"], risk
        idea["risk_factors"] = (factors + list(idea["risk_factors"]))[:7]
        idea["price_at_analysis"] = price
        ideas.append(idea)
    a["ideas"] = ideas[:3]

    watches = []
    for w in a["watches"]:
        if w["memory_id"] not in memory_ids:
            dropped.append(f"watch {w['id']}: unknown memory zone {w['memory_id']!r}")
            continue
        w["outcomes"] = w["outcomes"][:3]
        watches.append(w)
    a["watches"] = watches[:2]
    a["stance"] = "TRADE_IDEA" if a["ideas"] else ("STRUCTURE_WATCH" if a["watches"] else "NO_TRADE")
    a["dropped"] = dropped
    return a, dropped


# ---------------------------------------------------------------------------- reports

LABELS = {
    "English": {
        "BUY": "BUY", "SELL": "SELL", "idea_header": "{dir} IDEA", "watch_title": "STRUCTURE WATCH",
        "risk": "Risk", "LOW": "Low", "MEDIUM": "Medium", "HIGH": "High",
        "context": "Market Context", "analysis": "Analysis", "memory": "Remembered Structure",
        "readiness": "Status", "DEVELOPING": "Developing — not actionable yet",
        "NEEDS_CONFIRMATION": "Valid only after confirmation", "VALID_NOW": "Valid at current location",
        "confirm": "Confirmation to wait for", "entry": "Preferred Entry", "invalidation": "Invalidation",
        "targets": "Targets", "rr": "R:R to TP1 (from entry edge)", "factors": "Risk Factors",
        "safer": "Safer approach", "level": "Important Level", "history": "History",
        "situation": "Market Situation", "interpretation": "Current Interpretation",
        "outcomes": "Possible Outcomes", "waiting": "Waiting For", "price": "Price",
        "idea_footer": "Analyst idea, not a signal. Confirmed BUY/SELL signals arrive separately after "
                       "every check. Automatic execution is disabled.",
        "watch_footer": "Observation only — no entry.",
        "SUPPORT": "Support", "RESISTANCE": "Resistance", "SUPPLY": "Supply", "DEMAND": "Demand",
        "FVG": "FVG", "ORDER_BLOCK": "Order block", "SWING_HIGH": "Swing high", "SWING_LOW": "Swing low",
        "LIQUIDITY": "Liquidity", "BREAKER": "Breaker", "BUY_SIDE": "Buy-side liquidity (equal highs)",
        "SELL_SIDE": "Sell-side liquidity (equal lows)",
        "created": "identified {age} ago", "tests": "tested {n}×", "last_test": "last test {age} ago",
        "reaction": "avg reaction {x} ATR", "broken": "broken {dir} {age} ago — retest {retest}",
        "flipped": "broken and retest held — now acting as {role}", "false": "{n} false breakout(s)",
        "UP": "upward", "DOWN": "downward", "WAITING": "pending", "CONFIRMED": "confirmed",
        "f_rr": "R:R to TP1 is {rr:.2f}, below the {min:g} minimum.",
        "f_rr_low": "R:R to TP1 is only {rr:.2f} — reward smaller than risk.",
        "f_holiday": "{event}: thin liquidity, moves can be erratic.",
        "f_tp_free": "TP1 is not at a remembered level.",
        "confidence": "Confidence", "entry_type": "Entry type",
        "MARKET": "Market (enter now)", "LIMIT": "Limit (wait for pullback into the zone)",
        "BREAKOUT": "Breakout (enter when price trades through)",
        "sl": "Stop Loss", "at_level": "at", "beyond": "beyond",
        "f_mtf": "Python's 4H/1H structure opposes this direction.",
        "f_news": "High-impact event window: {event}.",
        "f_chase": "Price has already moved away from the preferred entry — do not chase.",
        "f_wait": "Price is {d:.2f} away from the entry — wait for the pullback; do not buy/sell at the current price.",
        "units": {"m": "min", "h": "h", "d": "d"},
        "daily_memory": "Remembered levels near price", "track": "Astra track record ({days}d)",
        "track_line": "primary scenarios played out {p}/{t} when triggered; breakout calls {bp}/{bt}",
        "story": "Astra's view",
        "track_ideas": "Trade ideas: TP1 before stop {w}/{n} when filled; 70%+ confidence calls {hw}/{hn}",
    },
    "Arabic": {
        "BUY": "شراء", "SELL": "بيع", "idea_header": "فكرة {dir}", "watch_title": "مراقبة هيكلية",
        "risk": "المخاطرة", "LOW": "منخفضة", "MEDIUM": "متوسطة", "HIGH": "مرتفعة",
        "context": "سياق السوق", "analysis": "التحليل", "memory": "الهيكل المحفوظ في الذاكرة",
        "readiness": "الحالة", "DEVELOPING": "قيد التكوّن — غير قابلة للتنفيذ بعد",
        "NEEDS_CONFIRMATION": "صالحة فقط بعد التأكيد", "VALID_NOW": "صالحة عند الموقع الحالي",
        "confirm": "التأكيد المنتظر", "entry": "منطقة الدخول المفضلة", "invalidation": "الإلغاء",
        "targets": "الأهداف", "rr": "العائد/المخاطرة إلى الهدف الأول (من طرف منطقة الدخول)",
        "factors": "عوامل المخاطرة", "safer": "البديل الأكثر أماناً", "level": "المستوى المهم",
        "history": "التاريخ", "situation": "وضع السوق", "interpretation": "القراءة الحالية",
        "outcomes": "السيناريوهات المحتملة", "waiting": "ننتظر", "price": "السعر",
        "idea_footer": "فكرة تحليلية وليست إشارة. إشارات الشراء/البيع المؤكدة تُرسل منفصلة بعد كل الفحوص. "
                       "التنفيذ الآلي معطّل.",
        "watch_footer": "للمراقبة فقط — لا دخول.",
        "SUPPORT": "دعم", "RESISTANCE": "مقاومة", "SUPPLY": "عرض", "DEMAND": "طلب",
        "FVG": "فجوة سعرية", "ORDER_BLOCK": "كتلة أوامر", "SWING_HIGH": "قمة متأرجحة", "SWING_LOW": "قاع متأرجح",
        "LIQUIDITY": "سيولة", "BREAKER": "كاسر", "BUY_SIDE": "سيولة فوق قمم متساوية",
        "SELL_SIDE": "سيولة تحت قيعان متساوية",
        "created": "حُدد قبل {age}", "tests": "عدد الاختبارات {n}", "last_test": "آخر اختبار قبل {age}",
        "reaction": "متوسط الارتداد {x} ATR", "broken": "كُسر {dir} قبل {age} — إعادة الاختبار {retest}",
        "flipped": "كُسر وصمدت إعادة الاختبار — يعمل الآن كـ{role}", "false": "{n} اختراق كاذب",
        "UP": "للأعلى", "DOWN": "للأسفل", "WAITING": "منتظرة", "CONFIRMED": "مؤكدة",
        "f_rr": "العائد/المخاطرة إلى الهدف الأول {rr:.2f} أقل من الحد الأدنى {min:g}.",
        "f_rr_low": "العائد/المخاطرة إلى الهدف الأول {rr:.2f} فقط — العائد أقل من المخاطرة.",
        "f_holiday": "{event}: سيولة ضعيفة وحركات قد تكون متقلبة.",
        "f_tp_free": "الهدف الأول ليس عند مستوى محفوظ في الذاكرة.",
        "confidence": "الثقة", "entry_type": "نوع الدخول",
        "MARKET": "فوري (الدخول الآن)", "LIMIT": "معلّق (انتظار ارتداد إلى المنطقة)",
        "BREAKOUT": "اختراق (الدخول عند تجاوز المنطقة)",
        "sl": "وقف الخسارة", "at_level": "عند", "beyond": "خلف",
        "f_mtf": "هيكل الأربع ساعات/الساعة لدى Python يعاكس هذا الاتجاه.",
        "f_news": "نافذة حدث عالي التأثير: {event}.",
        "f_chase": "ابتعد السعر عن منطقة الدخول المفضلة — لا تطارد السعر.",
        "f_wait": "السعر يبعد {d:.2f} عن منطقة الدخول — انتظر الارتداد ولا تدخل بالسعر الحالي.",
        "units": {"m": "دقيقة", "h": "ساعة", "d": "يوم"},
        "daily_memory": "مستويات محفوظة قرب السعر", "track": "سجل Astra (آخر {days} يوماً)",
        "track_line": "السيناريو الرئيسي تحقق {p}/{t} عند تفعيله؛ سيناريوهات الاختراق {bp}/{bt}",
        "story": "قراءة Astra",
        "track_ideas": "أفكار التداول: الهدف الأول قبل الوقف {w}/{n} عند التفعيل؛ أفكار الثقة 70%+ {hw}/{hn}",
    },
}
TF_NAME = {"15min": "15M", "1h": "1H", "4h": "4H", "1day": "Daily", "1week": "Weekly"}


def labels(language):
    return LABELS.get(language) or LABELS["English"]


def _ago(when, now, L):
    if not when:
        return "—"
    secs = max(0, (now - datetime.fromisoformat(str(when).replace("Z", "+00:00"))).total_seconds())
    u = L["units"]
    if secs < 3600:
        return f"{int(secs // 60)} {u['m']}"
    if secs < 2 * 86400:
        return f"{int(secs // 3600)} {u['h']}"
    return f"{secs / 86400:.0f} {u['d']}"


def _p(x):
    return f"{x:,.2f}".rstrip("0").rstrip(".")


def zone_line(z, now, L):
    """'4H Resistance 4,180.5–4,185 · identified 3 d ago · tested 3× · last test 2 h ago'"""
    head = f"{TF_NAME.get(z['timeframe'], z['timeframe'])} {L.get(z['role'], z['role'])} {_p(z['low'])}–{_p(z['high'])}"
    bits = [L["created"].format(age=_ago(z["created_at"], now, L))]
    if z.get("tests"):
        bits.append(L["tests"].format(n=z["tests"]))
    if z.get("last_tested_at") and z.get("tests", 0) > 1:
        bits.append(L["last_test"].format(age=_ago(z["last_tested_at"], now, L)))
    if z.get("reactions"):
        bits.append(L["reaction"].format(x=round(sum(z["reactions"]) / len(z["reactions"]), 1)))
    if z["status"] == "BROKEN":
        bits.append(L["broken"].format(dir=L.get(z.get("break_direction"), ""), age=_ago(z.get("broken_at"), now, L),
                                       retest=L.get(z.get("retest") or "WAITING", "")))
    elif z["status"] == "FLIPPED":
        bits.append(L["flipped"].format(role=L.get(z["role"], z["role"])))
    if z.get("false_breakouts"):
        bits.append(L["false"].format(n=z["false_breakouts"]))
    return head + " · " + " · ".join(bits)


def _anchor_text(a, L):
    if not a:
        return ""
    return f"{TF_NAME.get(a['timeframe'], a['timeframe'])} {L.get(a['role'], a['role'])} {_p(a['low'])}–{_p(a['high'])}" \
        + (f" · {L['tests'].format(n=a['tests'])}" if a.get("tests") else "")


def idea_text(idea, story, zones, now, language, price=None):
    L = labels(language)
    d = idea["direction"]
    lines = [f"{'🟢' if d == 'BUY' else '🔴'} XAU/USD — {L['idea_header'].format(dir=L[d])}",
             f"{L['confidence']}: {idea.get('confidence', '—')}% | {L['risk']}: {L[idea['risk']]}"
             + (f" | {L['price']}: {_p(price)}" if price else ""), "",
             f"{L['entry_type']}: {L[idea.get('entry_type', 'LIMIT')]}",
             f"{L['entry']}: {_p(idea['entry_low'])}–{_p(idea['entry_high'])}",
             f"{L['sl']}: {_p(idea['invalidation'])}"
             + (f" — {L['beyond']} {_anchor_text(idea.get('sl_anchor'), L)}" if idea.get("sl_anchor") else "")
             + (f"\n   {idea['invalidation_text'][:300]}" if idea.get("invalidation_text") else "")]
    anchors = idea.get("tp_anchors") or [None] * len(idea["targets"])
    for i, (t, a) in enumerate(zip(idea["targets"], anchors), 1):
        lines.append(f"TP{i}: {_p(t)}" + (f" — {L['at_level']} {_anchor_text(a, L)}" if a else ""))
    if idea.get("targets_text"):
        lines.append(f"   {idea['targets_text'][:360]}")
    lines += [f"{L['rr']}: {idea['rr_tp1']:.2f}", "",
              f"{L['context']}:", story[:420], "",
              f"{L['analysis']}:", idea["reasoning"][:700]]
    if zones:
        lines += ["", f"{L['memory']}:"] + ["• " + zone_line(z, now, L) for z in zones[:3]]
    if idea.get("confirmation"):
        lines += ["", f"{L['confirm']}: {idea['confirmation'][:240]}"]
    if idea["risk_factors"]:
        lines += ["", f"{L['factors']}:"] + [f"• {f[:200]}" for f in idea["risk_factors"]]
    if idea.get("safer_alternative"):
        lines += ["", f"{L['safer']}: {idea['safer_alternative'][:260]}"]
    lines += ["", L["idea_footer"]]
    return "\n".join(lines)[:3900]


def watch_text(w, zone, now, language, price=None):
    L = labels(language)
    lines = [f"👀 XAU/USD — {L['watch_title']}" + (f" | {L['price']}: {_p(price)}" if price else ""), "",
             f"{L['level']}:", zone_line(zone, now, L), "",
             f"{L['situation']}:", w["situation"][:420], "",
             f"{L['interpretation']}:", w["interpretation"][:420]]
    if w["outcomes"]:
        lines += ["", f"{L['outcomes']}:"]
        for o in w["outcomes"]:
            lines.append(f"• {o['if'][:120]} → {o['then'][:160]}")
    lines += ["", f"{L['waiting']}: {w['waiting_for'][:240]}", "", L["watch_footer"]]
    return "\n".join(lines)[:3900]


def track_line(track, language):
    L = labels(language)
    if not track or not track.get("resolved"):
        return None
    p, b = track["primary"], track["breakout_scenarios"]
    line = L["track"].format(days=track["days"]) + ": " + L["track_line"].format(
        p=p["played_out"], t=p["triggered"], bp=b["played_out"], bt=b["triggered"])
    ideas = track.get("ideas") or {}
    if ideas.get("triggered"):
        hi = (track.get("ideas_by_confidence") or {}).get("70+", {})
        line += "\n" + L["track_ideas"].format(w=ideas["played_out"], n=ideas["triggered"],
                                               hw=hi.get("played_out", 0), hn=hi.get("triggered", 0))
    return line


def _bucket(x, size):
    return int(round(x / size)) if size > 0 else int(round(x))


def idea_key(idea, daily_atr, day, round_=0):
    """The same idea (direction + entry area) is reported once per trading day; it is reported
    again when it becomes valid at the current location, or (round_ > 0) when it is a new trade in
    the same area: the earlier one finished, or its stop/target/entry changed. Wording drift is not news."""
    mid = (idea["entry_low"] + idea["entry_high"]) / 2
    size = 0.5 * (daily_atr or 10.0)
    stage = "now" if idea["readiness"] == "VALID_NOW" else "plan"
    key = f"astra-idea:{day}:{idea['direction']}:{_bucket(mid, size)}:{stage}"
    return key if not round_ else f"{key}:r{round_}"


def idea_group(idea):
    """The learning-history group record_ideas()/record_predictions() give this idea."""
    buy = idea["direction"] == "BUY"
    level = (idea["entry_high"] if buy else idea["entry_low"]) if idea["entry_type"] == "LIMIT" else \
            (idea["entry_low"] if buy else idea["entry_high"])
    trigger = "IMMEDIATE" if idea["entry_type"] == "MARKET" else "TOUCH"
    return f"IDEA:{'BULLISH' if buy else 'BEARISH'}:{trigger}:15min:{round(level)}"


def idea_changed(prev, idea, daily_atr, fraction):
    """True when entry, stop or first target moved by at least `fraction` x daily ATR: a different order."""
    tol = fraction * (daily_atr or 10.0)
    mid = (idea["entry_low"] + idea["entry_high"]) / 2
    return (abs(mid - prev["mid"]) >= tol or abs(idea["invalidation"] - prev["invalidation"]) >= tol
            or abs(idea["targets"][0] - prev["tp1"]) >= tol)


def watch_key(w, zone, day):
    return f"astra-watch:{day}:{zone['id']}:{zone['status']}:{zone.get('tests', 0)}"


def analysis_id(born_iso, analysis):
    return "A" + hashlib.sha1((born_iso + json.dumps(analysis, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()[:12]
