"""Demo results: A_TRADE vs B_TRADE in money and R (R = result / money risked at entry), and since
v1.6 per playbook, confirmation method, session and market phase; 1.7 per tier, plus the trade reviews."""

from __future__ import annotations

import csv
import json

from . import lab as LB
from . import review as RV


def stats(trades: list) -> dict:
    n = len(trades)
    if not n:
        return {"closed trades": 0}
    rs = [t["r"] for t in trades]
    wins = sum(r > 0.05 for r in rs)
    losses = sum(r < -0.05 for r in rs)
    gw = sum(t["net"] for t in trades if t["net"] > 0)
    gl = -sum(t["net"] for t in trades if t["net"] < 0)
    eq = peak = dd = 0.0
    for t in trades:
        eq += t["net"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {"closed trades": n, "wins": wins, "losses": losses, "breakeven": n - wins - losses,
            "win rate %": round(100 * wins / n, 1), "net $": round(sum(t["net"] for t in trades), 2),
            "average R": round(sum(rs) / n, 2), "total R": round(sum(rs), 2),
            "best R": round(max(rs), 2), "worst R": round(min(rs), 2),
            "profit factor": round(gw / gl, 2) if gl else None, "max drawdown $": round(dd, 2)}


def _phase(o) -> str:
    try:
        rg = json.loads(o.get("regime") or "{}")
    except ValueError:
        return ""
    return f"{rg.get('phase', '')} {rg.get('phase_direction') or ''}".strip()


def group_lines(closed: list, key: str, label: str) -> list:
    groups: dict = {}
    for t in closed:
        if t.get(key) not in (None, ""):
            groups.setdefault(t[key], []).append(t)
    if not groups:
        return []
    out = [f"\n--- by {label} ---"]
    for g, part in sorted(groups.items(), key=lambda x: str(x[0])):
        st = stats(part)
        out.append(f"  {str(g):<26} {st['closed trades']:>3} closed | win {st['win rate %']:>5}% | "
                   f"avg {st['average R']:+.2f}R | total {st['total R']:+.2f}R")
    return out


def run(market, state, csv_path) -> str:
    open_ids = {p.ticket for p in (market.trade_call("positions_get", symbol=market.symbol) or [])}
    closed, still_open, unfilled = [], 0, {"A_TRADE": 0, "B_TRADE": 0}
    for o in state.all_orders():
        if o["position_ticket"] is None:
            if o["status"] in ("CANCELLED", "EXPIRED"):
                unfilled[o["grade"]] = unfilled.get(o["grade"], 0) + 1
            continue
        if o["position_ticket"] in open_ids:
            still_open += 1
            continue
        deals = market.trade_call("history_deals_get", position=o["position_ticket"]) or []
        net = sum(d.profit + d.commission + d.swap for d in deals)
        r = net / o["risk_money"] if o["risk_money"] else 0.0
        closed.append({"created_utc": o["created_utc"], "grade": o["grade"], "setup_id": o["setup_id"],
                       "strategy": o.get("strategy") or "", "tier": o.get("tier") or "pre-1.7",
                       "direction": o["direction"], "type": o["order_type"], "lots": o["lots"],
                       "entry": o["entry"], "sl": o["sl"], "tp1": o["tp1"], "tp2": o["tp2"],
                       "planned_rr": o["rr"], "risk_$": o["risk_money"], "net": round(net, 2), "r": round(r, 2),
                       "playbook": o.get("playbook") or "pre-1.6 (not recorded)", "confirmation": o.get("confirmation") or "",
                       "session": o.get("session") or "", "phase": _phase(o), "astra_class": o.get("astra_class") or "",
                       "expansion_block": o.get("expansion_block") or 0,
                       "lab": int(o.get("lab") or 0), "violations": LB.violations_of(o)})          # 1.10
    lab_closed = [t for t in closed if t["lab"]]
    closed = [t for t in closed if not t["lab"]]                 # 1.10: lab trades never mix with live results
    lines = ["", "=== AUTO-TRADER DEMO RESULTS ===",
             f"orders: {len(state.all_orders())} | closed: {len(closed)} live + {len(lab_closed)} lab | still open: {still_open}"]
    for g in ("A_TRADE", "B_TRADE"):
        lines.append(f"\n--- {g}  (pending orders never filled: {unfilled.get(g, 0)}) ---")
        lines += [f"  {k:<16}{v}" for k, v in stats([t for t in closed if t["grade"] == g]).items()]
    names = {"SIGNAL": "confirmed SIGNALS", "SWING_TREND": "trend IDEAS", "SWING_COUNTER": "COUNTER-TREND ideas",
             "OWN": "own analysis"}
    def src(t):
        if t.get("strategy") in names:
            return names[t["strategy"]]
        return names["SIGNAL"] if t["setup_id"].startswith("SIG-") else \
            names["SWING_TREND"] if t["setup_id"].startswith("IDEA-") else names["OWN"]
    for name in names.values():
        part = [t for t in closed if src(t) == name]
        if part:
            st = stats(part)
            lines.append(f"\n--- {name}: {st['closed trades']} closed | win {st['win rate %']}% | "
                         f"avg {st['average R']}R | total {st['total R']}R | net ${st['net $']}")
    for key, label in (("tier", "tier (1.7)"), ("playbook", "playbook (v1.6)"), ("confirmation", "confirmation method"),
                       ("session", "session"), ("phase", "market phase at entry")):
        lines += group_lines(closed, key, label)
    shadow = [t for t in closed if t.get("expansion_block")]
    if shadow:
        st = stats(shadow)
        lines.append(f"\n--- trades the expansion filter (shadow) would have blocked: {st['closed trades']} | "
                     f"total {st['total R']:+.2f}R  (negative = the filter would have helped)")
    lines.append("\n--- ALL (live track) ---")
    lines += [f"  {k:<16}{v}" for k, v in stats(closed).items()]
    lines += LB.per_rule(lab_closed)
    everything = closed + lab_closed
    if everything:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            rows = [dict(t, violations=",".join(t["violations"])) for t in everything]
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        lines.append(f"\nPer-trade detail (live + lab, column 'lab'): {csv_path}")
    lines += RV.summary(state.reviews())
    lines.append("Judge each grade on average R and total R after 30+ closed trades, not on a few wins.")
    return "\n".join(lines)
