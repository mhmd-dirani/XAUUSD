#!/usr/bin/env python3
r"""
Idea scorecard: Astra's TRADE IDEAs, scored by Python against closed candles, grouped by the rules they broke.

The signal bot already records every idea as a prediction (runtime/astra_memory.sqlite3, table predictions) and
resolves it: PLAYED_OUT = TP1 before the stop, FAILED = stop first. This tool joins those results back to the
idea itself (table analyses) and groups them the way the lab groups its orders: 4H/1H opposition, trade class,
session, R:R band, direction. It is free (no AI call, no broker), it covers every idea whether or not anything
traded it, and it grows by roughly 10 ideas a day. It is the fastest evidence available for "which rule costs us".

    python tools\idea_scorecard.py                         # last 30 days, spread cost 0.30 $/oz
    python tools\idea_scorecard.py --days 14 --spread 0.4
    python tools\idea_scorecard.py --db runtime\astra_memory.sqlite3 --csv runtime\idea_scorecard.csv

What it does NOT do (read before trusting a row): fills a LIMIT idea at its favourable edge (the signal bot's own
scoring), takes TP1 only (no TP2, no partial, no breakeven), ignores slippage, and counts a FAILED idea as -1R.
The spread is subtracted as spread / stop-distance in R. Rows with fewer than 30 ideas are screening, not evidence.
r"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
MTF_TEXTS = ("يعاكس هذا الاتجاه", "4H/1H structure opposes")      # Python's own factor, Arabic and English labels


def session_of(ts: str) -> str:
    h = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(UTC).hour
    if 0 <= h < 6:
        return "ASIA"
    if 6 <= h < 7:
        return "PRE_LONDON"
    if 7 <= h < 12:
        return "LONDON"
    if 12 <= h < 16:
        return "LONDON_NY"
    if 16 <= h < 21:
        return "NEW_YORK"
    return "OFF"


def load(db_path: Path, days: int, spread: float) -> list[dict]:
    db = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    ideas = {}
    for aid, created, data in db.execute("SELECT id, created_at, data FROM analyses WHERE created_at >= ?", (since,)):
        try:
            a = json.loads(data)
        except ValueError:
            continue
        for i in a.get("ideas") or []:
            ideas[(aid, i.get("id"))] = (i, created)
    out = []
    for (data,) in db.execute("SELECT data FROM predictions WHERE resolved_at IS NOT NULL AND created_at >= ? "
                              "AND json_extract(data, '$.likelihood') = 'IDEA'", (since,)):
        p = json.loads(data)
        if not p.get("triggered_at") or p.get("status") not in ("PLAYED_OUT", "FAILED"):
            continue                                    # not filled, or expired open: no outcome to score
        aid, iid = p["pid"].split(":")[0], str(p["id"]).replace("idea-", "", 1)
        i, created = ideas.get((aid, iid), (None, None))
        if not i:
            continue
        rr = i.get("rr_tp1")
        edge = i["entry_high"] if i["direction"] == "BUY" else i["entry_low"]
        stop_dist = abs(edge - i["invalidation"]) or None
        cost_r = (spread / stop_dist) if stop_dist else 0.0
        win = p["status"] == "PLAYED_OUT"
        r = ((rr if rr is not None else 0.0) if win else -1.0) - cost_r
        out.append({"analysis": aid, "idea": iid, "created": created, "direction": i["direction"], "win": win,
                    "rr_tp1": rr, "r_after_cost": round(r, 3), "stop_dist": stop_dist,
                    "mtf_opposed": any(any(t in f for t in MTF_TEXTS) for f in i.get("risk_factors") or []),
                    "trade_class": i.get("trade_class") or "unclassified", "session": session_of(created),
                    "confidence": i.get("confidence"), "astra_risk": i.get("astra_risk") or i.get("risk"),
                    "entry_type": i.get("entry_type"), "outcome": p.get("outcome", "")})
    db.close()
    return out


def dedupe(rows: list[dict]) -> list[dict]:
    """One row per trade. Astra re-issues the same idea in consecutive hourly maps while it is still working, so
    the 7-day file of 29 Oct held 56 rows for 44 trades (one SELL stopped at 4043.68 on 27 Oct 14:30 appeared three
    times). Rows of the same direction that resolved on the same candle at the same level are one trade; the earliest
    issue is kept. Without this the sample size is inflated by about a quarter."""
    import re
    groups: dict = {}
    for r in sorted(rows, key=lambda r: r["created"]):
        key = (r["direction"], re.sub(r"\s+at\s.*$", "", r["outcome"]).split(" (")[0], r["outcome"][-25:])
        groups.setdefault(key, r)
    return list(groups.values())


def block(rows: list[dict]) -> str:
    if not rows:
        return "n=  0"
    n, w = len(rows), sum(r["win"] for r in rows)
    rrs = sorted(r["rr_tp1"] for r in rows if r["rr_tp1"] is not None)
    med = rrs[len(rrs) // 2] if rrs else 0.0
    total = sum(r["r_after_cost"] for r in rows)
    flag = "" if n >= 30 else "  (< 30: screening only)"
    return f"n={n:3} | TP1 first {w / n:4.0%} | median R:R {med:.2f} | sum {total:+6.2f}R | avg {total / n:+.2f}R{flag}"


def report(rows: list[dict], days: int, spread: float) -> str:
    L = [f"# Astra idea scorecard - last {days} days - {len(rows)} distinct trades with an outcome "
         f"(re-issued ideas counted once) - spread cost {spread:.2f} $/oz subtracted", ""]

    def group(title, key, order=None):
        L.append(f"## {title}")
        vals = order or sorted({key(r) for r in rows}, key=str)
        for v in vals:
            sub = [r for r in rows if key(r) == v]
            if sub:
                L.append(f"  {str(v):<24} {block(sub)}")
        L.append("")

    group("4H/1H opposition (Python's veto)", lambda r: "opposed" if r["mtf_opposed"] else "with structure",
          ["with structure", "opposed"])
    group("Astra trade class", lambda r: r["trade_class"])
    group("Session of the analysis (UTC)", lambda r: r["session"], ["ASIA", "PRE_LONDON", "LONDON", "LONDON_NY", "NEW_YORK", "OFF"])
    group("R:R to TP1 from the worst edge", lambda r: "rr < 1.5" if (r["rr_tp1"] or 0) < 1.5 else "1.5-2" if r["rr_tp1"] < 2 else "rr >= 2",
          ["rr < 1.5", "1.5-2", "rr >= 2"])
    group("Direction", lambda r: r["direction"], ["BUY", "SELL"])
    group("Astra risk label", lambda r: r["astra_risk"], ["LOW", "MEDIUM", "HIGH"])
    L.append("## The live trader's own hunting ground")
    L.append("  rr >= 2 & with structure  " + block([r for r in rows if (r["rr_tp1"] or 0) >= 2 and not r["mtf_opposed"]]))
    L.append("  rr >= 2 & opposed         " + block([r for r in rows if (r["rr_tp1"] or 0) >= 2 and r["mtf_opposed"]]))
    L += ["", "Read: favourable-edge fills, TP1 only, no slippage, FAILED = -1R. Counter-structure ideas carry smaller",
          "targets (see median R:R), so compare avg R, not hit rate. A row under 30 ideas is a hypothesis for the replay,",
          "never a reason to change a rule. Nothing here is a profitability claim."]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="runtime/astra_memory.sqlite3")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--spread", type=float, default=0.30, help="$/oz subtracted from every idea as spread / stop in R")
    ap.add_argument("--csv", help="write one row per idea here")
    ap.add_argument("--all-rows", action="store_true", help="count every hourly re-issue of an idea (inflates n)")
    a = ap.parse_args()
    root = Path(__file__).resolve().parents[1]
    db = Path(a.db) if Path(a.db).is_absolute() else root / a.db
    raw = load(db, a.days, a.spread)
    rows = raw if a.all_rows else dedupe(raw)
    if not a.all_rows:
        print(f"({len(raw)} idea rows -> {len(rows)} distinct trades; --all-rows keeps every re-issue)")
    print(report(rows, a.days, a.spread))
    if a.csv and rows:
        with open(a.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"\nPer-idea rows: {a.csv}")


if __name__ == "__main__":
    main()
