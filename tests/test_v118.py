"""11 Nov: the 1-minute trend-pullback scalp candidate (replay only - no engine, no orders)."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
for p in (HERE.parent / "src", HERE, HERE.parent / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from autotrader import scalp_model as SC  # noqa: E402

UTC = timezone.utc
SESS = {"ASIA": {"tz": "Asia/Tokyo", "start": "09:00", "end": "15:00"},
        "LONDON": {"tz": "Europe/London", "start": "08:00", "end": "16:30"},
        "NEW_YORK": {"tz": "America/New_York", "start": "08:00", "end": "17:00"}}


def trend_m1(n=3000, drift=0.03, seed=1):
    import random
    rnd = random.Random(seed)
    t0 = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
    px, out = 4000.0, []
    for i in range(n):
        o = px
        c = o + drift + rnd.gauss(0, 0.35)
        out.append({"t": t0 + timedelta(minutes=i), "o": o, "h": max(o, c) + abs(rnd.gauss(0, 0.15)),
                    "l": min(o, c) - abs(rnd.gauss(0, 0.15)), "c": c, "s": 0.3})
        px = c
    return out


class PullbackTest(unittest.TestCase):
    def test_ema_and_context(self):
        self.assertEqual(SC.ema([1.0] * 30, 9)[-1], 1.0)
        self.assertIsNone(SC.ema([1.0] * 5, 9)[-1])
        ctx = SC.context_5m(trend_m1(drift=0.05))
        self.assertGreater(ctx.count("UP"), 5 * ctx.count("DOWN"))
        ctx = SC.context_5m(trend_m1(drift=-0.05))
        self.assertGreater(ctx.count("DOWN"), 5 * ctx.count("UP"))

    def test_trades_follow_the_5m_trend_and_carry_costs(self):
        res = SC.run_pullbacks(trend_m1(drift=0.05), {"spread": 0.3, "slippage": 0.1}, SESS)
        tr = res["PULLBACK_rr1.5"]
        self.assertTrue(tr)
        self.assertGreater(sum(t.direction == "BUY" for t in tr), sum(t.direction == "SELL" for t in tr))
        for t in tr:
            self.assertGreaterEqual(t.cost, 0.4)                       # spread 0.30 + slippage 0.10
            risk = abs(t.fill - t.stop)
            self.assertAlmostEqual(abs(t.target - t.fill), 1.5 * risk, places=1)

    def test_no_look_ahead(self):
        m1 = trend_m1(n=4000, drift=0.02, seed=5)
        full = SC.run_pullbacks(m1, {"spread": 0.3, "slippage": 0.1}, SESS)["PULLBACK_rr2.0"]
        cut = m1[2500]["t"]
        part = SC.run_pullbacks(m1[:2500], {"spread": 0.3, "slippage": 0.1}, SESS)["PULLBACK_rr2.0"]
        done_full = [(t.created, t.fill, t.exit_price) for t in full if t.exit_time < cut - timedelta(minutes=2)]
        done_part = [(t.created, t.fill, t.exit_price) for t in part if t.exit_time < cut - timedelta(minutes=2)]
        self.assertEqual(done_full, done_part)

    def test_5m_entries(self):
        m1 = trend_m1(n=9000, drift=0.03, seed=2)
        m5 = SC.aggregate(m1, 300)
        self.assertEqual(len(m5), 1800)
        self.assertEqual(m5[1]["o"], m1[5]["o"])
        self.assertEqual(m5[0]["c"], m1[4]["c"])
        self.assertEqual(m5[0]["h"], max(b["h"] for b in m1[:5]))
        res = SC.run_pullbacks_5m(m1, {"spread": 0.3, "slippage": 0.1}, SESS)
        tr = res["PB5M_rr2.0"]
        self.assertTrue(tr)
        for t in tr:
            self.assertEqual(int(t.created.timestamp()) % 300, 0)
            self.assertAlmostEqual(abs(t.target - t.fill), 2.0 * abs(t.fill - t.stop), places=1)
        part = SC.run_pullbacks_5m(m1[:6000], {"spread": 0.3, "slippage": 0.1}, SESS)["PB5M_rr2.0"]
        cut = m1[5990]["t"]
        self.assertEqual([(t.created, t.fill) for t in tr if t.exit_time < cut - timedelta(minutes=10)],
                         [(t.created, t.fill) for t in part if t.exit_time < cut - timedelta(minutes=10)])

    def test_report_has_both_families(self):
        m1 = trend_m1(n=3000)
        res = SC.run_all(m1, {"spread": 0.3, "slippage": 0.1}, ([], []), SESS)
        res.update(SC.run_pullbacks(m1, {"spread": 0.3, "slippage": 0.1}, SESS))
        text = "\n".join(SC.report_all(res, m1[2100]["t"]))
        self.assertIn("PULLBACK_rr2.0", text)
        self.assertIn("SCALP_k2.0_s1.5", text)
        self.assertIn("Pass bar", text)


if __name__ == "__main__":
    unittest.main()
