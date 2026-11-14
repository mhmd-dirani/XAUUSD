"""Tests for the ICT replay model (src/autotrader/ict_model.py) - 30 Oct 2026. Offline, no MT5, no AI."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE.parent / "tools"))

from autotrader import ict_model as IM  # noqa: E402

UTC = timezone.utc
CFG = dict(IM.DEFAULTS, sl_atr_max=5.0, max_spread=0.5)


def bars(seq, start=datetime(2026, 7, 1, 7, 0, tzinfo=UTC)):
    """[(o, h, l, c), ...] -> 5M bars with time/closes like the replay's aggregate()."""
    out = []
    for i, (o, h, l, c) in enumerate(seq):
        t = start + timedelta(minutes=5 * i)
        out.append({"time": t.isoformat(), "closes": (t + timedelta(minutes=5)).isoformat(),
                    "open": o, "high": h, "low": l, "close": c, "spread": 0.3})
    return out


def flat(n, px=100.0, wiggle=0.2):
    """n quiet bars around px (bodies 0.1): the 'average body' reference."""
    return [(px, px + wiggle, px - wiggle, px + 0.1) if i % 2 else (px + 0.1, px + wiggle, px - wiggle, px) for i in range(n)]


class KillzoneTest(unittest.TestCase):
    def test_summer_and_winter_are_defined_in_new_york_time(self):
        # July (EDT, UTC-4): London KZ 02-05 NY = 06:00-09:00 UTC; NY KZ 07-10 NY = 11:00-14:00 UTC
        self.assertEqual(IM.killzone(datetime(2026, 7, 1, 6, 30, tzinfo=UTC)), "LONDON_KZ")
        self.assertEqual(IM.killzone(datetime(2026, 7, 1, 12, 0, tzinfo=UTC)), "NY_KZ")
        self.assertIsNone(IM.killzone(datetime(2026, 7, 1, 9, 30, tzinfo=UTC)))
        # December (EST, UTC-5): one hour later in UTC
        self.assertEqual(IM.killzone(datetime(2026, 12, 1, 7, 30, tzinfo=UTC)), "LONDON_KZ")
        self.assertIsNone(IM.killzone(datetime(2026, 12, 1, 6, 30, tzinfo=UTC)))
        self.assertEqual(IM.killzone(datetime(2026, 12, 1, 13, 0, tzinfo=UTC)), "NY_KZ")

    def test_weekend_is_never_a_killzone(self):
        self.assertIsNone(IM.killzone(datetime(2026, 7, 4, 12, 0, tzinfo=UTC)))   # Saturday


class SwingTest(unittest.TestCase):
    def test_only_confirmed_fractals(self):
        seq = flat(3) + [(100, 103, 99.9, 102)] + flat(2) + [(100, 100.2, 97, 99)] + flat(1)
        b = bars(seq)
        sw = IM.swings(b, 2, 2)
        highs = [s for s in sw if s["side"] == "HIGH"]
        lows = [s for s in sw if s["side"] == "LOW"]
        self.assertEqual([h["i"] for h in highs], [3])
        self.assertEqual(highs[0]["price"], 103)
        self.assertEqual(lows, [])                      # the low at index 6 has only one bar to its right: unconfirmed


class SweepTest(unittest.TestCase):
    def setUp(self):
        # 20 quiet bars, a swing low at 97 (index 23), 6 more quiet bars, the sweep bar (wick 96.6, close 100.3)
        self.seq = flat(20) + [(100, 100.2, 98.5, 99), (99, 99.3, 97.8, 98.2), (98.2, 98.4, 97.4, 97.9),
                               (97.9, 98.1, 97.0, 97.8), (97.8, 98.6, 97.7, 98.5), (98.5, 99.4, 98.4, 99.3)] + flat(4)
        self.sweep_bar = (100.0, 100.4, 96.6, 100.3)

    def _pools(self, b):
        sw = IM.swings(b, 2, 2)
        return IM.pools(b, sw, {"_frames": {}}, [])

    def test_wick_through_and_close_back_is_a_sweep(self):
        b = bars(self.seq + [self.sweep_bar])
        sw = IM.find_sweep(b, "BUY", self._pools(b), CFG)
        self.assertIsNotNone(sw)
        self.assertEqual(sw["extreme"], 96.6)
        self.assertEqual(sw["pool"][0], 97.0)
        self.assertEqual(sw["j"], sw["j2"], len(b) - 1)

    def test_a_deeper_low_afterwards_cancels_the_sweep(self):
        b = bars(self.seq + [self.sweep_bar, (100.3, 100.5, 96.0, 96.5)])      # deeper low, no reclaim
        self.assertIsNone(IM.find_sweep(b, "BUY", self._pools(b), CFG))
        b2 = bars(self.seq + [self.sweep_bar, (100.3, 100.5, 96.0, 99.0)])     # deeper low AND reclaim = a new sweep
        self.assertEqual(IM.find_sweep(b2, "BUY", self._pools(b2), CFG)["extreme"], 96.0)

    def test_close_below_the_pool_is_not_a_sweep_until_reclaimed(self):
        b = bars(self.seq + [(100.0, 100.2, 96.6, 96.8)])
        self.assertIsNone(IM.find_sweep(b, "BUY", self._pools(b), CFG))
        b2 = bars(self.seq + [(100.0, 100.2, 96.6, 96.8), (96.8, 98.0, 96.7, 97.9)])   # reclaim within 3 bars
        sw = IM.find_sweep(b2, "BUY", self._pools(b2), CFG)
        self.assertIsNotNone(sw)
        self.assertEqual((sw["j"], sw["j2"]), (len(b2) - 2, len(b2) - 1))
        self.assertEqual(sw["extreme"], 96.6)                   # the complete sweep, not just the reclaim bar

    def test_htf_pool_filter_ignores_5m_swings(self):
        b = bars(self.seq + [self.sweep_bar])
        self.assertIsNone(IM.find_sweep(b, "BUY", self._pools(b), CFG, pool_filter="htf"))
        lv = [{"name": "PDL", "side": "LOW", "price": 97.0, "formed_at": b[0]["time"]}]
        pl = IM.pools(b, IM.swings(b, 2, 2), {"_frames": {}}, lv)
        sw = IM.find_sweep(b, "BUY", pl, CFG, pool_filter="htf")
        self.assertEqual(sw["pool"][1], "PDL")

    def test_a_pool_formed_after_the_wick_bar_cannot_be_swept_by_it(self):
        b = bars(self.seq + [self.sweep_bar])
        lv = [{"name": "ASIA_LOW", "side": "LOW", "price": 99.0, "formed_at": b[-1]["closes"]}]
        pl = {"HIGH": [], "LOW": [(99.0, "ASIA_LOW", datetime.fromisoformat(b[-1]["closes"]))]}
        self.assertIsNone(IM.find_sweep(b, "BUY", pl, CFG))


class ShiftTest(unittest.TestCase):
    """Sweep low 96.6, the opposing swing high at 99.4 (index 25), then displacement up with an FVG."""

    def setUp(self):
        self.base = flat(20) + [(100, 100.2, 98.5, 99), (99, 99.3, 97.8, 98.2), (98.2, 98.4, 97.4, 97.9),
                                (97.9, 98.1, 97.0, 97.8), (97.8, 98.6, 97.7, 98.5), (98.5, 99.4, 98.4, 99.3),
                                (99.3, 99.35, 98.9, 99.0), (99.0, 99.1, 98.6, 98.7), (98.7, 98.8, 98.3, 98.4),
                                (98.4, 98.5, 98.0, 98.1)]
        self.sweep = (98.1, 98.2, 96.6, 98.0)                    # wick through 97.0, close back
        self.disp = (98.0, 101.2, 97.9, 101.0)                   # body 3.0 >> 1.5 x 0.1; closes above 99.4 -> MSS
        self.after = (101.0, 101.5, 100.6, 101.3)                # low 100.6 > sweep bar high 98.2 -> bullish FVG 98.2-100.6

    def _run(self, seq):
        b = bars(seq)
        sw5 = IM.swings(b, 2, 2)
        pl = IM.pools(b, sw5, {"_frames": {}}, [])
        sweep = IM.find_sweep(b, "BUY", pl, CFG)
        self.assertIsNotNone(sweep)
        return b, sw5, sweep

    def test_shift_with_displacement_and_fvg(self):
        b, sw5, sweep = self._run(self.base + [self.sweep, self.disp, self.after])
        sh = IM.shift(b, sw5, "BUY", sweep, CFG)
        self.assertIsNotNone(sh)
        self.assertEqual(sh["level"], 99.4)
        self.assertEqual((sh["fvg_low"], sh["fvg_high"]), (98.2, 100.6))
        self.assertEqual(sh["mss_i"], len(b) - 2)

    def test_no_fvg_no_setup(self):
        b, sw5, sweep = self._run(self.base + [self.sweep, self.disp, (101.0, 101.1, 98.0, 100.0)])
        self.assertIsNone(IM.shift(b, sw5, "BUY", sweep, CFG))

    def test_no_displacement_no_setup(self):
        # tiny bodies (<= 0.1 = the flat average), long wicks: price crawls above 99.4 without displacement
        slow = [(98.0, 98.9, 97.9, 98.05), (98.05, 99.2, 98.0, 98.1), (98.1, 99.6, 98.05, 98.15), (98.15, 99.8, 98.1, 99.45)]
        b, sw5, sweep = self._run(self.base + [self.sweep] + slow + [(99.45, 100.5, 99.4, 99.5)])
        self.assertIsNone(IM.shift(b, sw5, "BUY", sweep, CFG))

    def test_fires_once_per_leg(self):
        b, sw5, sweep = self._run(self.base + [self.sweep, self.disp, self.after, (101.3, 101.6, 101.0, 101.4)])
        self.assertIsNone(IM.shift(b, sw5, "BUY", sweep, CFG))    # the FVG already existed one bar earlier

    def test_not_the_first_close_beyond(self):
        b, sw5, sweep = self._run(self.base + [self.sweep, self.disp, self.after, (101.3, 101.6, 101.0, 101.4),
                                               (101.4, 102.0, 101.2, 101.9), (101.9, 103.4, 101.8, 103.3), (103.3, 103.8, 103.0, 103.6)])
        self.assertIsNone(IM.shift(b, sw5, "BUY", sweep, CFG))


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.st = {"direction": "BUY", "created": datetime(2026, 7, 1, 7, 0, tzinfo=UTC), "ce": 99.4, "edge": 100.6,
                   "stop": 96.2, "price": 101.3, "spread": 0.3, "atr15": 3.0, "leg_mid": 100.0,
                   "highs": [102.0, 104.0, 110.0], "lows": [96.0], "htf_highs": [(104.0, "PDH"), (110.0, "4h:HIGH")],
                   "htf_lows": [], "killzone": "LONDON_KZ", "sweep_time": "x", "cid": "c",
                   "sweep": {"pool": (97.0, "5m:LOW")}, "shift": {"fvg_low": 98.2, "fvg_high": 100.6}}

    def test_fixed_2r_limit_at_ce(self):
        pl = IM.plan(self.st, IM.VARIANTS["ICT_2R"], CFG)
        self.assertEqual(pl["order_type"], "BUY_LIMIT")
        self.assertEqual(pl["entry"], 99.4)
        self.assertAlmostEqual(pl["tp1"], 99.4 + 2 * 3.2, places=2)
        self.assertIsNone(pl["tp2"])
        self.assertEqual(pl["draw"], 110.0)                       # 104 < 99.4 + 2 x 3.2 = 105.8 -> the 4H high
        self.assertEqual(pl["draw_label"], "4h:HIGH")

    def test_discount_rule_refuses_an_entry_in_premium(self):
        st = dict(self.st, ce=100.2)                            # above the leg's 50 % = premium for a buy
        drops = []
        self.assertIsNone(IM.plan(st, IM.VARIANTS["ICT_2R"], CFG, drops.append))
        self.assertIn("discount", drops[0])

    def test_no_draw_no_trade(self):
        st = dict(self.st, htf_highs=[(104.0, "PDH")])          # 104 < 105.8
        drops = []
        self.assertIsNone(IM.plan(st, IM.VARIANTS["ICT_2R"], CFG, drops.append))
        self.assertIn("no HTF draw", drops[0])
        self.assertIsNone(IM.plan(st, IM.VARIANTS["ICT_1R"], CFG))   # the 1R variant still needs a 2R draw

    def test_market_when_price_already_through_the_entry(self):
        st = dict(self.st, price=99.0)                          # price below CE: no retrace to wait for
        pl = IM.plan(st, IM.VARIANTS["ICT_2R"], CFG)
        self.assertEqual(pl["order_type"], "MARKET")
        self.assertEqual(pl["entry"], 99.0)

    def test_liq_then_draw_targets(self):
        pl = IM.plan(self.st, IM.VARIANTS["ICT_liq_then_draw"], CFG)
        self.assertEqual(pl["tp1"], 104.0)                      # nearest high >= entry + 1R (102.0 < 102.6)
        self.assertEqual(pl["tp2"], 110.0)

    def test_spread_caps(self):
        st = dict(self.st, spread=0.6)
        drops = []
        self.assertIsNone(IM.plan(st, IM.VARIANTS["ICT_2R"], CFG, drops.append))
        self.assertIn("spread", drops[0])
        pl = IM.plan(dict(self.st, spread=0.3), IM.VARIANTS["ICT_2R"], CFG)
        self.assertFalse(pl["live_gate_spread_ok"])              # 0.3 > 5% of 3.2 -> the live gate would refuse
        self.assertTrue(IM.plan(dict(self.st, spread=0.15), IM.VARIANTS["ICT_2R"], CFG)["live_gate_spread_ok"])


class NoLookAheadTest(unittest.TestCase):
    """ICT trades decided before a cut-off are identical whether or not the replay has seen later candles."""

    def test_truncated_history_gives_the_same_early_trades(self):
        import replay as tool                                   # tools/replay.py: synthetic()
        from autotrader import replay as RP
        m1 = tool.synthetic(days=50, seed=11)
        cut = m1[0]["t"] + timedelta(days=44)
        full = RP.Replay([dict(b) for b in m1], {"server_offset": 3}).run()
        part = RP.Replay([dict(b) for b in m1 if b["t"] < cut], {"server_offset": 3}).run()
        total = 0
        for v in IM.VARIANTS:
            a = {t.cid: (t.entry_level, t.stop, t.tp1, t.order_type) for t in full.trades[v] if t.created < cut - timedelta(days=1)}
            b = {t.cid: (t.entry_level, t.stop, t.tp1, t.order_type) for t in part.trades[v] if t.created < cut - timedelta(days=1)}
            self.assertEqual(a, b, v)
            total += len(a)
        self.assertGreater(len(full.ict_setups), 0)            # the synthetic series does produce setups


if __name__ == "__main__":
    unittest.main()


class ClockModelTest(unittest.TestCase):
    """Time-of-day variants (clock_model.py)."""

    def test_entry_window_is_the_first_15m_close_at_or_after_the_entry_time(self):
        from autotrader import clock_model as CK
        # 18:45 NY on Tue 7 Jul 2026 (EDT) = 22:45 UTC
        self.assertIsNotNone(CK.due("CLOCK_long_1845_0530", datetime(2026, 7, 7, 22, 45, tzinfo=UTC)))
        self.assertIsNone(CK.due("CLOCK_long_1845_0530", datetime(2026, 7, 7, 23, 0, tzinfo=UTC)))     # 19:00 NY: too late
        self.assertIsNone(CK.due("CLOCK_long_1845_0530", datetime(2026, 7, 7, 22, 30, tzinfo=UTC)))    # 18:30 NY: too early
        ex = CK.due("CLOCK_long_1845_0530", datetime(2026, 7, 7, 22, 45, tzinfo=UTC))
        self.assertEqual(ex, datetime(2026, 7, 8, 9, 30, tzinfo=UTC))                                   # 05:30 NY EDT next day

    def test_weekend_rules(self):
        from autotrader import clock_model as CK
        self.assertIsNone(CK.due("CLOCK_long_1845_0530", datetime(2026, 7, 10, 22, 45, tzinfo=UTC)))   # Friday -> Saturday exit
        self.assertIsNotNone(CK.due("CLOCK_long_1845_0530", datetime(2026, 7, 12, 22, 45, tzinfo=UTC)))  # Sunday evening session
        self.assertIsNone(CK.due("CLOCK_short_0800_1100", datetime(2026, 7, 11, 12, 0, tzinfo=UTC)))   # Saturday

    def test_winter_time_uses_new_york_clock(self):
        from autotrader import clock_model as CK
        # 08:00 NY in December (EST) = 13:00 UTC
        self.assertIsNotNone(CK.due("CLOCK_short_0800_1100", datetime(2026, 12, 1, 13, 0, tzinfo=UTC)))
        self.assertIsNone(CK.due("CLOCK_short_0800_1100", datetime(2026, 12, 1, 12, 0, tzinfo=UTC)))

    def test_plan_geometry(self):
        from autotrader import clock_model as CK
        pl = CK.plan("CLOCK_long_1845_0530", 4000.0, 0.3, 5.0)
        self.assertEqual((pl["direction"], pl["order_type"], pl["entry"]), ("BUY", "MARKET", 4000.3))
        self.assertAlmostEqual(pl["stop"], 4000.3 - 15.0, places=2)
        self.assertIsNone(pl["tp2"])
        pl = CK.plan("CLOCK_short_0800_1100_nostop", 4000.0, 0.3, 5.0)
        self.assertEqual(pl["entry"], 4000.0)
        self.assertAlmostEqual(pl["stop"], 4030.0, places=2)

    def test_simulator_honours_hold_until(self):
        from autotrader import replay as RP
        t0 = datetime(2026, 7, 7, 22, 45, tzinfo=UTC)
        m1 = [{"t": t0 + timedelta(minutes=i), "o": 100.0, "h": 100.2, "l": 99.8, "c": 100.1, "s": 0.1} for i in range(600)]
        sim = RP.Simulator(m1, dict(RP.DEFAULTS, max_hold_hours=48))
        tr = RP.Trade(cid="x", variant="v", direction="BUY", order_type="MARKET", created=t0, entry_level=100.1,
                      stop=90.0, tp1=200.0, tp2=None, expiry=t0 + timedelta(minutes=30), trade_class="CLOCK",
                      confirmation="TIME", zone="z", session="OFF", state={}, expansion_against=False,
                      would_block=False, hold_until=t0 + timedelta(hours=2))
        sim.run(tr)
        self.assertEqual(tr.outcome, "time exit")
        self.assertEqual(tr.exit_time, t0 + timedelta(hours=2, minutes=1))


class ScalpModelTest(unittest.TestCase):
    """1-minute mean-reversion scalp model (scalp_model.py)."""

    def _m1(self, closes, start=datetime(2026, 7, 7, 13, 0, tzinfo=UTC), spread=0.2):
        out, prev = [], closes[0]
        for i, c in enumerate(closes):
            o = prev
            out.append({"t": start + timedelta(minutes=i), "o": o, "h": max(o, c) + 0.1, "l": min(o, c) - 0.1, "c": c, "s": spread})
            prev = c
        return out

    def test_indicators_align(self):
        from autotrader import scalp_model as SC
        m1 = self._m1([100.0] * 30)
        sma, atr = SC.indicators(m1, 20, 14)
        self.assertIsNone(sma[18]); self.assertAlmostEqual(sma[19], 100.0); self.assertAlmostEqual(atr[29], 0.2, places=6)

    def test_stretch_sells_and_reverts_to_the_mean(self):
        from autotrader import scalp_model as SC
        # 70 flat bars (ATR ~0.2), then a 3$ spike up (> 2 x ATR above the mean), then a fall back to 100
        closes = [100.0] * 70 + [103.0] + [102.5, 101.5, 100.5, 99.9] + [100.0] * 10
        m1 = self._m1(closes)
        cfg = dict(SC.DEFAULTS)
        trades = SC.run_variant("SCALP_k2.0_s1.5", m1, *SC.indicators(m1, 20, 14), cfg, ([], []), None)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t.direction, "SELL")
        self.assertEqual(t.outcome, "target")
        self.assertGreater(t.usd, 0)
        self.assertAlmostEqual(t.fill, 103.0 - 0.10, places=2)        # next bar opens at the spike close (bid) minus slippage
        self.assertAlmostEqual(t.cost, 0.3, places=2)                   # spread 0.2 + slippage 0.1 (no stop)

    def test_spike_that_keeps_going_is_stopped(self):
        from autotrader import scalp_model as SC
        closes = [100.0] * 70 + [103.0] + [104.0, 106.0, 108.0, 110.0] + [110.0] * 40
        m1 = self._m1(closes)
        trades = SC.run_variant("SCALP_k2.0_s1.5", m1, *SC.indicators(m1, 20, 14), dict(SC.DEFAULTS), ([], []), None)
        self.assertGreaterEqual(len(trades), 1)
        self.assertTrue(trades[0].outcome.startswith("stop"))
        self.assertLess(trades[0].usd, 0)
        self.assertAlmostEqual(trades[0].r, -1.0, delta=0.25)           # about -1R (slippage, spread)

    def test_range_only_skips_trending_macro(self):
        from autotrader import scalp_model as SC
        closes = [100.0] * 70 + [103.0] + [102.5, 101.5, 100.5, 99.9] + [100.0] * 10
        m1 = self._m1(closes)
        tl = ([m1[0]["t"]], ["BULLISH"])
        self.assertEqual(SC.run_variant("SCALP_k2.0_s1.5_rangeOnly", m1, *SC.indicators(m1, 20, 14), dict(SC.DEFAULTS), tl, None), [])
        tl = ([m1[0]["t"]], ["MIXED"])
        self.assertEqual(len(SC.run_variant("SCALP_k2.0_s1.5_rangeOnly", m1, *SC.indicators(m1, 20, 14), dict(SC.DEFAULTS), tl, None)), 1)

    def test_stats_and_report_do_not_crash(self):
        from autotrader import scalp_model as SC
        closes = [100.0] * 70 + [103.0] + [102.5, 101.5, 100.5, 99.9] + [100.0] * 10
        m1 = self._m1(closes)
        res = SC.run_all(m1, {}, ([], []), None)
        self.assertEqual(set(res), set(SC.VARIANTS))
        text = "\n".join(SC.report(res, m1[-1]["t"]))
        self.assertIn("## Scalp model", text)
