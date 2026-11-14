"""
1.7 - post-trade review (the "after" half of the feedback loop). Deterministic, no AI call.

When a trade's money result is known, Python reconstructs what happened from the closed 15M candles
and the journal row, and stores a review next to the trade (table trade_reviews):

  outcome        TP2 / TP1 / BREAKEVEN / STOP_WICK (a candle wick took the stop, no 15M close beyond)
                 / STOP_CLOSE (a 15M candle closed beyond the stop) / MANUAL_OR_UNKNOWN
  mfe_r, mae_r   best and worst excursion in R while the trade was open (from candle highs/lows)
  after_4h       what price did in the 4 h after the exit: TP1_REACHED (the idea was right, the stop
                 was in the way), STOP_SIDE, NEITHER
  verdict        RIGHT_IDEA_WRONG_STOP / WRONG_IDEA / GOOD_EXIT / LEFT_MONEY (TP1 then far beyond)
  context        tier + its facts, trade class, confirmation, session, regime at entry (from meta)

It never changes a rule by itself. The report groups verdicts by tier / fact / session, so after
50-100 trades you can see WHICH fact was missing on the losers, and the teaching session (Astra) can
turn the review rows into lessons. That is the correction loop you asked for instead of blocking.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
LOG = logging.getLogger("autotrader.review")


def _t(b):
    return datetime.fromisoformat(str(b["time"]).replace("Z", "+00:00")).astimezone(UTC)


def _stamp(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(UTC) if s else None


def build(rec: dict, bars15: list, now: datetime) -> dict:
    """rec = the orders row (with result, closed_utc, meta columns). bars15 = closed 15M candles (UTC)."""
    buy = rec["direction"] == "BUY"
    entry, sl, tp1, tp2 = float(rec["entry"]), float(rec["sl"]), float(rec["tp1"]), rec.get("tp2")
    risk = abs(entry - sl) or 1e-9
    opened = _stamp(rec.get("fill_utc") or rec.get("created_utc"))
    closed = _stamp(rec.get("closed_utc")) or now
    live = [b for b in bars15 if opened and opened <= _t(b) + timedelta(minutes=15) and _t(b) < closed]
    after = [b for b in bars15 if closed <= _t(b) < closed + timedelta(hours=4)]
    sign = 1 if buy else -1
    mfe = max([sign * ((b["high"] if buy else b["low"]) - entry) for b in live] or [0.0]) / risk
    mae = min([sign * ((b["low"] if buy else b["high"]) - entry) for b in live] or [0.0]) / risk
    result = rec.get("result")
    r = (result / rec["risk_money"]) if rec.get("risk_money") else None
    closed_beyond = any((b["close"] <= sl) if buy else (b["close"] >= sl) for b in live)
    wick_beyond = any((b["low"] <= sl) if buy else (b["high"] >= sl) for b in live)
    if r is None:
        outcome = "MANUAL_OR_UNKNOWN"
    elif r <= -0.8:
        outcome = "STOP_CLOSE" if closed_beyond else ("STOP_WICK" if wick_beyond else "STOP_OR_MANUAL")
    elif tp2 is not None and r >= 1.5 * (abs(tp1 - entry) / risk) * 0.5:
        outcome = "TP2"
    elif r >= 0.8 * abs(tp1 - entry) / risk * (0.5 if tp2 is not None else 1.0):
        outcome = "TP1"
    elif -0.2 <= r <= 0.2:
        outcome = "BREAKEVEN"
    else:
        outcome = "PARTIAL"
    tp1_after = any((b["high"] >= tp1) if buy else (b["low"] <= tp1) for b in after)
    stop_after = any((b["low"] <= sl) if buy else (b["high"] >= sl) for b in after)
    after_4h = "TP1_REACHED" if tp1_after else ("STOP_SIDE" if stop_after else "NEITHER")
    if outcome.startswith("STOP") and after_4h == "TP1_REACHED":
        verdict = "RIGHT_IDEA_WRONG_STOP"
    elif outcome.startswith("STOP"):
        verdict = "WRONG_IDEA"
    elif outcome in ("TP1", "TP2", "PARTIAL") and mfe >= 2 * abs(tp1 - entry) / risk:
        verdict = "LEFT_MONEY"
    elif outcome in ("TP1", "TP2", "BREAKEVEN", "PARTIAL"):
        verdict = "GOOD_EXIT"
    else:
        verdict = "UNKNOWN"
    meta = {}
    try:
        meta = json.loads(rec.get("regime") or "{}") if isinstance(rec.get("regime"), str) else (rec.get("regime") or {})
    except Exception:  # noqa: BLE001
        meta = {}
    facts = {}
    try:
        facts = json.loads(rec.get("tier_facts") or "{}")
    except Exception:  # noqa: BLE001
        facts = {}
    return {"ticket": rec["ticket"], "setup_id": rec["setup_id"], "direction": rec["direction"],
            "tier": rec.get("tier"), "playbook": rec.get("playbook"), "confirmation": rec.get("confirmation"),
            "session": rec.get("session"), "r": None if r is None else round(r, 2), "outcome": outcome,
            "mfe_r": round(mfe, 2), "mae_r": round(mae, 2), "after_4h": after_4h, "verdict": verdict,
            "bars_open": len(live), "regime": meta, "facts": {k: v for k, v in facts.items() if not k.startswith("_")},
            "missing_facts": [k for k, v in facts.items() if not k.startswith("_") and v is False],
            "reviewed_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ")}


def text(rv: dict) -> str:
    miss = ", ".join(rv["missing_facts"]) or "none"
    return (f"🔎 REVIEW {rv['direction']} {rv['setup_id']} ({rv.get('tier') or '?'}): {rv['outcome']} "
            f"{'' if rv['r'] is None else f'{rv['r']:+.2f}R '}| MFE {rv['mfe_r']:+.2f}R MAE {rv['mae_r']:+.2f}R | "
            f"4h after: {rv['after_4h']} | verdict {rv['verdict']} | missing facts: {miss}")


def summary(reviews: list) -> list[str]:
    out = [f"\n=== TRADE REVIEWS (1.7) === {len(reviews)} reviewed"]
    if not reviews:
        return out
    for key in ("tier", "verdict", "session", "playbook"):
        groups: dict = {}
        for rv in reviews:
            groups.setdefault(rv.get(key) or "-", []).append(rv)
        out.append(f"  by {key}:")
        for k, rs in sorted(groups.items(), key=str):
            rr = [x["r"] for x in rs if x["r"] is not None]
            out.append(f"    {str(k):<24} {len(rs):>3} | avg {sum(rr) / len(rr) if rr else 0:+.2f}R | "
                       f"RIGHT_IDEA_WRONG_STOP {sum(x['verdict'] == 'RIGHT_IDEA_WRONG_STOP' for x in rs)} | "
                       f"WRONG_IDEA {sum(x['verdict'] == 'WRONG_IDEA' for x in rs)}")
    miss: dict = {}
    for rv in reviews:
        for f in rv.get("missing_facts") or []:
            miss.setdefault(f, []).append(rv["r"] or 0.0)
    if miss:
        out.append("  losers by missing fact (which fact was absent on trades that lost):")
        for f, rs in sorted(miss.items(), key=lambda kv: sum(kv[1])):
            out.append(f"    {f:<16} {len(rs):>3} trades | total {sum(rs):+.2f}R")
    return out
