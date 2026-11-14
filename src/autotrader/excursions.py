"""
1.15: the excursion report - the evidence for trade protection and the zone score, from MT5's own candles.

    python src/auto_trader.py --excursions              # closed trades of the last 21 days
    python src/auto_trader.py --excursions --days 10 --spread 0.3

For every closed trade (lab and live, from the journal + the broker's deals):
  actual      the broker result in R (net money / money risked)
  MFE / MAE   best / worst excursion in R on M1 candles after the fill minute
  unmanaged   the stop-and-TP replay on M1 (a check: it should match "actual" for trades nothing managed)
  protected   the same trade under the 1.15 trade_protect rules (breakeven at 1R, lock at 1.5R, near-TP exit)
  zone score  the all-timeframe zone score at the moment the order was created (point-in-time)
Read the caveats before trusting a number: M1 candles are BID candles, the order of prices inside one minute is unknown
(the replay checks the stop before the target), live TP1 partials are not replayed (final TP only), and a few dozen
trades are screening, not proof.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone

from . import protect as PR
from . import zones as ZN

UTC = timezone.utc


def _ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp() if s else None


def _bucket(rows, key, label):
    out = []
    groups = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    for k in sorted(groups, key=lambda x: (x is None, x)):
        g = groups[k]
        n = len(g)
        wins = sum(1 for r in g if r["actual_r"] > 0.05)
        out.append(f"  {label} {k!s:>6}: {n:3d} trades | win {100 * wins / n:5.1f}% | avg actual {sum(r['actual_r'] for r in g) / n:+.2f}R"
                   f" | avg protected {sum(r['protected_r'] for r in g if r['protected_r'] is not None) / n:+.2f}R")
    return out


def analyse(market, state, days: int = 21, spread: float = 0.3, cfg: dict | None = None, now: datetime | None = None):
    now = now or datetime.now(UTC)
    since = now - timedelta(days=days)
    cfg = dict(PR.DEFAULTS, **(cfg or {}))
    k_in = market.const("DEAL_ENTRY_IN")
    recs = [o for o in state.all_orders() if o.get("position_ticket") and o.get("created_utc")
            and _ts(o["created_utc"]) >= since.timestamp() and o.get("status") in ("CLOSED", "TP1_DONE")]
    open_ids = {getattr(p, "ticket", None) for p in (market.trade_call("positions_get", symbol=market.symbol) or [])}
    recs = [o for o in recs if o["position_ticket"] not in open_ids]
    if not recs:
        return []
    first = min(_ts(o["created_utc"]) for o in recs)
    frames = {}
    for name, tf, n, sec in ZN.TFS:
        try:
            frames[name] = market.rates_range(tf, first - n * sec, now.timestamp())
        except Exception:  # noqa: BLE001
            frames[name] = []
    rows = []
    for o in recs:
        deals = market.trade_call("history_deals_get", position=o["position_ticket"]) or []
        ins = [d for d in deals if d.entry == k_in]
        if not ins:
            continue
        fill_px = float(ins[0].price)
        fill_ts = market.server_to_utc(ins[0].time)
        net = sum(d.profit + d.commission + d.swap for d in deals)
        actual = net / o["risk_money"] if o.get("risk_money") else None
        if actual is None:
            continue
        buy = o["direction"] == "BUY"
        tp = o["tp2"] if o.get("tp2") is not None else o["tp1"]
        start = fill_ts - fill_ts % 60 + 60
        try:
            m1 = market.rates_range("TIMEFRAME_M1", start, min(now.timestamp(), start + 72 * 3600))
        except Exception:  # noqa: BLE001
            m1 = []
        m1 = [b for b in m1 if b[0] >= start and b[0] + 60 <= now.timestamp()]
        off = PR.simulate(buy, fill_px, float(o["sl"]), tp, m1, spread, None)
        on = PR.simulate(buy, fill_px, float(o["sl"]), tp, m1, spread, cfg)
        zs = ZN.score(frames, o["direction"], float(o["entry"]), float(o["sl"]), o.get("tp1"), _ts(o["created_utc"]))
        rows.append({"created_utc": o["created_utc"], "setup_id": o["setup_id"], "track": "lab" if o.get("lab") else "live",
                     "direction": o["direction"], "fill": round(fill_px, 2), "sl": o["sl"], "tp": tp,
                     "actual_r": round(actual, 2), "mfe_r": off["mfe_r"], "mae_r": off["mae_r"],
                     "unmanaged_r": off["r"], "unmanaged_exit": off["reason"],
                     "protected_r": on["r"], "protected_exit": on["reason"],
                     "zone_score": zs["score"], "zone_against": zs["against"],
                     "zones_for": " ".join(zs["for"]), "zones_against": " ".join(zs["vs"]),
                     "tp1_partial": o.get("tp2") is not None})
    return rows


def run(market, state, csv_path, days: int = 21, spread: float = 0.3, cfg: dict | None = None,
        now: datetime | None = None) -> str:
    rows = analyse(market, state, days, spread, cfg, now)
    lines = ["", f"=== EXCURSIONS: closed trades, last {days} days (M1 replay, spread {spread}) ===", ""]
    if not rows:
        return "\n".join(lines + ["No closed trades with broker deals in this period."])
    lines.append(f"{'created':16} {'setup':22} {'trk':4} {'actual':>7} {'MFE':>6} {'MAE':>6} {'unmgd':>6} "
                 f"{'protect':>7} {'exit':16} {'zone':>4} {'vs':>3}  zones")
    for r in rows:
        lines.append(f"{r['created_utc'][:16]:16} {r['setup_id'][:22]:22} {r['track']:4} {r['actual_r']:+7.2f} "
                     f"{(r['mfe_r'] or 0):+6.2f} {(r['mae_r'] or 0):+6.2f} {(r['unmanaged_r'] or 0):+6.2f} "
                     f"{(r['protected_r'] or 0):+7.2f} {r['protected_exit'][:16]:16} {r['zone_score']!s:>4} "
                     f"{r['zone_against']!s:>3}  {r['zones_for']}" + (" (TP1 partial not replayed)" if r["tp1_partial"] else ""))
    for track in ("lab", "live"):
        g = [r for r in rows if r["track"] == track]
        if not g:
            continue
        reached = [r for r in g if r["actual_r"] < -0.5 and (r["mfe_r"] or 0) >= 1.0]
        lines += ["", f"--- {track.upper()}: {len(g)} closed trades ---",
                  f"  actual        total {sum(r['actual_r'] for r in g):+.2f}R",
                  f"  unmanaged sim total {sum(r['unmanaged_r'] or 0 for r in g):+.2f}R   (should be close to actual)",
                  f"  protected sim total {sum(r['protected_r'] or 0 for r in g):+.2f}R",
                  f"  losers that had been +1R or more first: {len(reached)} of {sum(1 for r in g if r['actual_r'] < -0.5)}",
                  "  by zone score (timeframes with a zone under the entry):"]
        lines += _bucket(g, lambda r: r["zone_score"], "score")
        lines += ["  by opposite zones in the path to TP1:"]
        lines += _bucket(g, lambda r: min(r["zone_against"] or 0, 2), "vs")
    lines += ["", "Caveats: BID M1 candles; stop checked before target inside a minute; live TP1 partials not replayed;",
              "groups under 30 trades are screening only."]
    if csv_path:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        lines.append(f"CSV: {csv_path}")
    return "\n".join(lines)
