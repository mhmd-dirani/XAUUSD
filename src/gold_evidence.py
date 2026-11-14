#!/usr/bin/env python3
"""V5 evidence layer: closed-candle SMC + confluence snapshot, Astra payload, and
Python verification of every setup Astra proposes.

Astra PROPOSES a setup and cites evidence ids; Python VERIFIES that each id exists in the
snapshot Astra analysed, matches the setup direction, is still usable, overlaps the watch
zone and satisfies one pattern-specific playbook. RSI divergence, MACD and EMA are context
only and can never satisfy a playbook. Offline; no network or broker calls.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from gold_smc_engine import analyze_frame, normalize_bars, _time
from gold_confluence_v5 import analyze_confluence_frame

FRAMES = ("1week", "1day", "4h", "1h", "15min")
PLAYBOOKS = ("TREND_CONTINUATION", "CHOCH_REVERSAL", "BREAKER_RETEST", "FIB_ZONE_CONFLUENCE")
ZONE_KINDS = ("FVG", "ORDER_BLOCK_CANDIDATE", "BREAKER_CANDIDATE", "FIB_OTE")
STRUCTURE_KINDS = ("BOS", "CHOCH", "LIQUIDITY_SWEEP")
CONTEXT_KINDS = ("RSI_DIVERGENCE",)
USABLE = {"BOS": {"CONFIRMED"}, "CHOCH": {"CONFIRMED"}, "LIQUIDITY_SWEEP": {"CONFIRMED"},
          "FVG": {"ACTIVE", "PARTIALLY_FILLED"}, "ORDER_BLOCK_CANDIDATE": {"ACTIVE", "TESTED"},
          "BREAKER_CANDIDATE": {"AWAITING_RETEST", "RETEST_CONFIRMED"}, "FIB_OTE": {"ACTIVE"},
          "RSI_DIVERGENCE": {"CONTEXT_ONLY"}}
DEAD = {"INVALIDATED"}
_KEEP = ("id", "kind", "direction", "timeframe", "at", "confirmed_at", "level_low", "level_high",
         "status", "source_times", "levels", "swing_low", "swing_high", "location", "original_ob_id",
         "breakout_at", "retest_at", "prices", "rsi_at_pivots")


def _clean(item: dict[str, Any]) -> dict[str, Any]:
    return {k: item[k] for k in _KEEP if k in item}


def build_evidence(history: dict[str, list[dict[str, Any]]], as_of: datetime) -> dict[str, Any]:
    """One snapshot per set of closed candles. A frame that fails analysis contributes
    no evidence (fail closed) and reports why."""
    frames, index = {}, {}
    for tf in FRAMES:
        try:
            bars = normalize_bars(history.get(tf) or [], tf)
            smc = analyze_frame(bars, tf, as_of=as_of)
            conf = analyze_confluence_frame(bars, tf, smc, as_of=as_of)
        except Exception as exc:
            frames[tf] = {"available": False, "error": (type(exc).__name__ + ": " + str(exc))[:200]}
            continue
        items = list(smc["events"]) + ([smc["fibonacci"]] if smc["fibonacci"] else []) \
            + conf["breakers"] + conf["divergences"]
        for it in items:
            index[it["id"]] = _clean(it)
        frames[tf] = {"available": True, "trend": smc["trend"], "atr": smc["atr"],
                      "last_closed_at": smc["last_closed_at"], "indicators": conf["indicators"],
                      "recent_swings": smc["swings"][-6:],
                      "fibonacci_id": smc["fibonacci"]["id"] if smc["fibonacci"] else None}
    return {"built_at": as_of.isoformat(), "frames": frames, "index": index}


def compact(evidence: dict[str, Any] | None) -> dict[str, Any]:
    """Small subset for Astra: the newest items of each kind per frame. Ids are exactly those Python verifies."""
    if not evidence:
        return {"available": False, "rule": "No verified evidence: return no setups."}
    out = {"built_at": evidence["built_at"], "frames": {}}
    items = list(evidence["index"].values())
    for tf in FRAMES:
        frame = dict(evidence["frames"].get(tf) or {"available": False})
        # 6.1.3: the index is grouped by kind, not time, so slicing the mixed list kept only the last
        # kind (6 sweeps, 8 order blocks) and hid every BOS/CHoCH and most FVGs. Pick per kind, newest.
        mine = sorted((i for i in items if i["timeframe"] == tf), key=lambda i: str(i.get("at") or ""))
        def newest(kinds, n, usable=False):
            return [i for i in mine if i["kind"] in kinds and (not usable or i["status"] in USABLE[i["kind"]])][-n:]
        frame["structure"] = sorted(newest(("BOS", "CHOCH"), 4) + newest(("LIQUIDITY_SWEEP",), 2),
                                    key=lambda i: str(i.get("at") or ""))
        frame["zones"] = sorted(newest(("FVG",), 4, True) + newest(("ORDER_BLOCK_CANDIDATE",), 4, True),
                                key=lambda i: str(i.get("at") or ""))
        frame["breakers"] = [i for i in mine if i["kind"] == "BREAKER_CANDIDATE" and i["status"] not in DEAD][-4:]
        frame["fibonacci"] = [i for i in mine if i["kind"] == "FIB_OTE"][-1:]
        frame["context_divergences"] = [i for i in mine if i["kind"] in CONTEXT_KINDS][-3:]
        out["frames"][tf] = frame
    out["rules"] = ("Cite ids exactly. Indicators/divergences are context only. "
                    "INVALIDATED/FILLED zones are not citable.")
    return out


def _overlap(a_low, a_high, b_low, b_high, tol):
    return a_low - tol <= b_high and a_high + tol >= b_low


def verify_setup(setup: dict[str, Any], zone: dict[str, Any], evidence: dict[str, Any] | None,
                 tol: float) -> dict[str, Any]:
    """Raise ValueError unless the setup's cited evidence satisfies its playbook."""
    if not evidence or not evidence.get("index"):
        raise ValueError("Setup requires a verifiable SMC evidence snapshot")
    pb, ids, d = setup.get("playbook"), setup.get("evidence_ids") or [], setup["direction"]
    if pb not in PLAYBOOKS:
        raise ValueError(f"Unknown playbook {pb!r}")
    if not 1 <= len(ids) <= 8 or len(set(ids)) != len(ids):
        raise ValueError("evidence_ids must hold 1..8 unique ids")
    items = []
    for i in ids:
        it = evidence["index"].get(i)
        if it is None:
            raise ValueError(f"Cited evidence {i} does not exist in the analysed snapshot")
        items.append(it)
    struct = [it for it in items if it["kind"] not in CONTEXT_KINDS]
    for it in struct:
        if it["direction"] != d:
            raise ValueError(f"Evidence {it['id']} is {it['direction']}, setup is {d}")
        if it["status"] not in USABLE.get(it["kind"], ()):
            raise ValueError(f"Evidence {it['id']} ({it['kind']}) is {it['status']}, not usable")
    zones = [it for it in struct if it["kind"] in ZONE_KINDS
             and _overlap(it["level_low"], it["level_high"], zone["low"], zone["high"], tol)]
    if not zones:
        raise ValueError("No cited zone evidence (FVG/OB/breaker/OTE) overlaps the watch zone")
    opposite = "BEARISH" if d == "BUY" else "BULLISH"
    trend = {tf: (evidence["frames"].get(tf) or {}).get("trend") for tf in ("4h", "1h")}
    kinds = lambda *k: [it for it in struct if it["kind"] in k]
    if pb == "TREND_CONTINUATION":
        if not [it for it in kinds("BOS") if it["timeframe"] in ("1h", "4h")]:
            raise ValueError("TREND_CONTINUATION needs a same-direction 1h/4h BOS")
        if not [z for z in zones if z["kind"] != "FIB_OTE" and z["timeframe"] in ("15min", "1h")]:
            raise ValueError("TREND_CONTINUATION needs a 15min/1h FVG/OB/breaker overlapping the zone")
        if trend["4h"] == opposite:
            raise ValueError("4H structure opposes a continuation setup")
    elif pb == "CHOCH_REVERSAL":
        chochs = [it for it in kinds("CHOCH") if it["timeframe"] in ("15min", "1h", "4h")]
        sweeps = [s for s in kinds("LIQUIDITY_SWEEP") if any(_time(s["at"]) <= _time(c["at"]) for c in chochs)]
        if not chochs or not sweeps:
            raise ValueError("CHOCH_REVERSAL needs a same-direction CHoCH preceded by a liquidity sweep")
        if not [z for z in zones if z["kind"] != "FIB_OTE"]:
            raise ValueError("CHOCH_REVERSAL needs a non-Fibonacci zone overlapping the watch zone")
        if len(setup["conditions"]) < 2:
            raise ValueError("CHOCH_REVERSAL needs at least 2 ordered confirmation stages")
    elif pb == "BREAKER_RETEST":
        if not [z for z in zones if z["kind"] == "BREAKER_CANDIDATE"]:
            raise ValueError("BREAKER_RETEST needs a same-direction breaker overlapping the zone")
    elif pb == "FIB_ZONE_CONFLUENCE":
        fibs = [z for z in zones if z["kind"] == "FIB_OTE"]
        other = [z for z in zones if z["kind"] in ("FVG", "ORDER_BLOCK_CANDIDATE")]
        if not any(_overlap(f["level_low"], f["level_high"], o["level_low"], o["level_high"], tol)
                   for f in fibs for o in other):
            raise ValueError("FIB_ZONE_CONFLUENCE needs an OTE overlapping a separate FVG/OB in the zone")
    required = sorted(it["id"] for it in struct if it["kind"] != "FIB_OTE" or pb == "FIB_ZONE_CONFLUENCE")
    return {"playbook": pb, "required": required,
            "context": sorted(it["id"] for it in items if it["kind"] in CONTEXT_KINDS)}


def recheck(spec: dict[str, Any], evidence: dict[str, Any] | None) -> tuple[str, str | None]:
    """Gate-time check on the CURRENT snapshot: OK / UNKNOWN (withhold) / INVALID (kill)."""
    if not evidence or not evidence.get("index"):
        return "UNKNOWN", "Evidence snapshot unavailable"
    pb = spec.get("playbook")
    required = (spec.get("verified_evidence") or {}).get("required")
    if required is None:   # not verified at install: check every non-context, non-Fibonacci citation
        required = [i for i in spec.get("evidence_ids") or []
                    if ":RSI_DIVERGENCE" not in i and (":FIB_" not in i or pb == "FIB_ZONE_CONFLUENCE")]
    if not required:
        return "UNKNOWN", "Setup has no structural evidence"
    for i in required:
        it = evidence["index"].get(i)
        if it is None:
            return "UNKNOWN", f"Evidence {i} absent from the current snapshot"
        if it["status"] in DEAD:
            return "INVALID", f"Cited {it['kind']} {i} invalidated by a closed candle"
        # V5.1: same rule as verify_setup for the whole lifecycle. A FILLED FVG or a SUPERSEDED
        # Fibonacci leg no longer supports the plan, so the setup ends (no stale approval).
        if it["status"] not in USABLE.get(it["kind"], ()):
            return "INVALID", f"Cited {it['kind']} {i} is now {it['status']}, no longer usable"
        if pb == "BREAKER_RETEST" and it["kind"] == "BREAKER_CANDIDATE" and it["status"] != "RETEST_CONFIRMED":
            return "UNKNOWN", "Breaker retest not yet confirmed by a closed candle"
    return "OK", None


def mtf_opposed(direction: str, evidence: dict[str, Any] | None) -> bool:
    """True when the current 4H or 1H structural trend opposes the direction, or unknown."""
    if not evidence:
        return True
    opposite = "BEARISH" if direction == "BUY" else "BULLISH"
    trends = [(evidence["frames"].get(tf) or {}).get("trend") for tf in ("4h", "1h")]
    return any(t is None or t == opposite for t in trends)
