#!/usr/bin/env python3
r"""
Scalp replay (11 Nov 2026): the two 1-minute scalping candidates on the cached 1-minute history, before any scalping
engine is built. No AI calls, no orders, no MT5 needed.

    python tools\scalp_replay.py                    uses replay_data\XAUUSD_M1.csv (made by tools\replay.py)
    python tools\scalp_replay.py --spread 0.35      default spread when a bar has none recorded
    python tools\scalp_replay.py --slippage 0.15

Output: replay_data\scalp_report.md and replay_data\scalp_trades.csv; the summary table is printed.
Families: SCALP_* = mean reversion (fade a 1M stretch back to the 20-bar mean); PULLBACK_* = EMA 9/21 pullback in the
direction of the 5M trend; PB5M_* = the same pullback with 5M entries and a 15M trend. See src\autotrader\scalp_model.py for the exact rules. Out-of-sample = the last 30% of days.
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from autotrader import replay as RP, scalp_model as SC  # noqa: E402

M1_CSV = ROOT / "replay_data" / "XAUUSD_M1.csv"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default=str(M1_CSV))
    ap.add_argument("--spread", type=float, default=0.30)
    ap.add_argument("--slippage", type=float, default=0.10)
    a = ap.parse_args()
    path = Path(a.csv)
    if not path.exists():
        raise SystemExit(f"{path} not found: run  python tools\\replay.py --days 720  once to fetch and cache the M1 history")
    m1 = RP.read_m1_csv(path)
    print(f"{len(m1):,} M1 bars {m1[0]['t']:%Y-%m-%d} -> {m1[-1]['t']:%Y-%m-%d}")
    t0 = time.time()
    cfg = {"spread": a.spread, "slippage": a.slippage}
    from autotrader import settings
    sessions_cfg = settings.DEFAULTS.get("sessions")
    res = SC.run_all(m1, cfg, ([], []), sessions_cfg)
    res.pop("SCALP_k2.0_s1.5_rangeOnly", None)                  # needs the main replay's macro timeline
    res.update(SC.run_pullbacks(m1, cfg, sessions_cfg))
    res.update(SC.run_pullbacks_5m(m1, cfg, sessions_cfg))           # 11 Nov: the same rule with 5M entries
    split = m1[int(len(m1) * 0.7)]["t"]
    lines = [f"# Scalp replay {m1[0]['t']:%Y-%m-%d} -> {m1[-1]['t']:%Y-%m-%d}, out-of-sample from {split:%Y-%m-%d}", ""]
    lines += SC.report_all(res, split)
    out = ROOT / "replay_data" / "scalp_report.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    with open(ROOT / "replay_data" / "scalp_trades.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["variant", "direction", "created", "fill", "stop", "target", "exit_time", "exit_price", "outcome",
                    "usd_per_0.01", "r", "context", "session", "hold_min", "cost"])
        for v, tr in res.items():
            for t in tr:
                w.writerow([v, t.direction, t.created.isoformat(), t.fill, t.stop, t.target, t.exit_time.isoformat(),
                            t.exit_price, t.outcome, t.usd, t.r, t.macro, t.session, t.hold_min, t.cost])
    print("\n".join(lines[:4 + len(res) + 1]))
    print(f"\n{time.time() - t0:.0f}s | full report: {out}")


if __name__ == "__main__":
    main()
