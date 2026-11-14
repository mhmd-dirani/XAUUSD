"""1.10.6 regression tests: lab geometry dedupe and broker/journal reconciliation. Offline, no MT5, no Telegram."""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from autotrader import alerts, executor as EX, lab as LB  # noqa: E402


class LabDuplicateTest(unittest.TestCase):
    def order(self, direction="BUY", entry=4043.0, sl=4027.5):
        return {"direction": direction, "entry_price": entry, "stop_loss": sl}

    def working(self, ticket, setup_id, direction="BUY", entry=4043.0, sl=4027.5, lab=1):
        return {"ticket": ticket, "setup_id": setup_id, "direction": direction, "entry": entry, "sl": sl, "lab": lab,
                "status": "PENDING"}

    def test_same_geometry_under_another_setup_id_is_a_duplicate(self):
        """28 Oct: LAB-BUY-4039 and LAB-BUY-4042 were the same order."""
        active = [self.working(59510593, "LAB-BUY-4039")]
        dup = LB.duplicate_of(self.order(), active, tol=2.0)
        self.assertIsNotNone(dup)
        self.assertEqual(dup["setup_id"], "LAB-BUY-4039")

    def test_within_tolerance_counts(self):
        active = [self.working(1, "LAB-BUY-4040", entry=4041.5, sl=4026.0)]
        self.assertIsNotNone(LB.duplicate_of(self.order(), active, tol=2.0))

    def test_outside_tolerance_is_not_a_duplicate(self):
        active = [self.working(1, "LAB-BUY-4050", entry=4050.0, sl=4034.0)]
        self.assertIsNone(LB.duplicate_of(self.order(), active, tol=2.0))

    def test_other_direction_is_not_a_duplicate(self):
        active = [self.working(1, "LAB-SELL-4043", direction="SELL")]
        self.assertIsNone(LB.duplicate_of(self.order(), active, tol=2.0))

    def test_live_orders_never_block_a_lab_sample(self):
        """The live track and the lab are separate tracks: a live order at the same level is not a lab duplicate."""
        active = [self.working(1, "IDEA-BUY-4043", lab=0)]
        self.assertIsNone(LB.duplicate_of(self.order(), active, tol=2.0))

    def test_stop_moved_means_a_different_order(self):
        active = [self.working(1, "LAB-BUY-4043", sl=4020.0)]
        self.assertIsNone(LB.duplicate_of(self.order(), active, tol=2.0))


class _Item:
    def __init__(self, ticket, magic=40001, identifier=None, comment="", volume=0.04):
        self.ticket, self.magic, self.identifier, self.comment, self.volume = ticket, magic, identifier, comment, volume


class _State:
    def __init__(self, rows):
        self.rows = rows

    def all_orders(self):
        return list(self.rows)


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        self.errors = []
        self._orig = alerts.error
        alerts.error = lambda msg: self.errors.append(msg)
        c = {"magic": 40001, "magic_lab": 40003, "lab_mode": True}
        self.x = EX.Executor(c, market=None, state=_State([
            {"ticket": 59507599, "position_ticket": None},        # the 28 Oct order: journalled, alert said "failed"
            {"ticket": 59700433, "position_ticket": 59700433},
            {"ticket": 59649141, "position_ticket": 900001},      # a fill whose position ticket differs
        ]))

    def tearDown(self):
        alerts.error = self._orig

    def test_known_tickets_raise_nothing(self):
        pending = {59507599: _Item(59507599)}
        positions = {59700433: _Item(59700433), 900001: _Item(900001, identifier=59649141)}
        self.assertEqual(self.x.reconcile(pending, positions), [])
        self.assertEqual(self.errors, [])

    def test_position_known_only_by_identifier(self):
        positions = {777: _Item(777, identifier=59649141)}
        self.assertEqual(self.x.reconcile({}, positions), [])

    def test_unknown_broker_order_is_reported_once(self):
        pending = {123456: _Item(123456, comment="B:IDEA-SELL-4020")}
        self.assertEqual(self.x.reconcile(pending, {}), [123456])
        self.assertEqual(len(self.errors), 1)
        self.assertIn("123456", self.errors[0])
        self.assertIn("NOT in the trader's journal", self.errors[0])
        # second pass: already reported, no new alert
        self.assertEqual(self.x.reconcile(pending, {}), [])
        self.assertEqual(len(self.errors), 1)

    def test_unknown_lab_position_is_reported(self):
        positions = {55: _Item(55, magic=40003, comment="LAB:LAB-BUY-4039", volume=0.01)}
        self.assertEqual(self.x.reconcile({}, positions), [55])
        self.assertIn("magic 40003", self.errors[0])

    def test_reconcile_never_sends_orders(self):
        """Report only: there is no broker call in reconcile(), so a None market must be fine."""
        self.x.reconcile({1: _Item(1)}, {2: _Item(2)})
        self.assertEqual(len(self.errors), 2)



class TierOverlapTest(unittest.TestCase):
    """1.10.6: the London/New York overlap counts as an active session for the tier facts."""

    def setUp(self):
        from autotrader import tiers as TR
        self.TR = TR
        self.c = {"tiers": {"A": {"risk_pct": 1.0, "min_confidence": 40}, "B": {"risk_pct": 0.5, "min_confidence": 35},
                            "C": {"risk_pct": 0.15, "min_confidence": 30, "min_lot_if_below": True}},
                  "tier_cap": {}, "expansion_filter": "shadow"}
        self.idea = {"confidence": 40, "astra_risk": "MEDIUM", "sl_anchor": {"id": "M1"}, "tp_anchors": [{"id": "M2"}]}
        self.dec = {"trade_class": "TREND", "expansion": {"against": False}, "would_block": False}
        self.state = {"macro_trend": "BEARISH"}

    def test_overlap_is_an_active_session(self):
        g = self.TR.grade(self.idea, "SELL", self.dec, self.state, "LONDON_NY", self.c)
        self.assertTrue(g["facts"]["session"])
        self.assertEqual(g["tier"], "A")

    def test_london_and_new_york_unchanged(self):
        for s in ("LONDON", "NEW_YORK"):
            self.assertEqual(self.TR.grade(self.idea, "SELL", self.dec, self.state, s, self.c)["tier"], "A")

    def test_asia_and_off_still_lose_the_fact(self):
        for s in ("ASIA", "OFF", None):
            g = self.TR.grade(self.idea, "SELL", self.dec, self.state, s, self.c)
            self.assertFalse(g["facts"]["session"])
            self.assertEqual(g["tier"], "B")



class ReplayVariantTest(unittest.TestCase):
    """1.10.6: the three isolated replay variants (regime gate, no-REJECTION)."""

    def setUp(self):
        from datetime import datetime, timedelta, timezone
        from autotrader import replay as RP
        self.RP = RP
        t0 = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
        m1 = [{"t": t0 + timedelta(minutes=k), "o": 4000.0, "h": 4000.5, "l": 3999.5, "c": 4000.0, "s": 0.2} for k in range(60)]
        self.rp = RP.Replay(m1, {"server_offset": 3})
        self.as_of = t0
        self.state = {"macro_trend": "BULLISH", "trend_1day": "BULLISH", "trend_4h": "BULLISH", "intermediate_trend": "BULLISH",
                      "local_trend": "BULLISH", "phase": "CONTINUATION", "phase_direction": "BULLISH", "volatility": "NORMAL",
                      "baseline": 2.0, "momentum": "STEADY", "momentum_direction": "BULLISH",
                      "expansion": {"active": False, "direction": None}, "atr15": 4.0, "_frames": {"15min": {"events": [], "swings": []}}}

    def cand(self, how="SWEEP"):
        from datetime import timedelta
        return {"cid": f"c:{how}", "zone": {"id": "1h:OB:4000", "source": "STRUCTURE"}, "how": how, "direction": "BUY",
                "order_type": "MARKET", "entry": 4000.2, "stop": 3996.0, "tp1": 4009.0, "tp2": None,
                "expiry": self.as_of + timedelta(minutes=30), "created": self.as_of, "session": "LONDON", "atr15": 4.0,
                "strong": False}

    def test_gate_function(self):
        g = self.RP.regime_gate_blocks
        self.assertIsNotNone(g("pullbacks_in_range_only", "TREND", "BEARISH"))
        self.assertIsNotNone(g("pullbacks_in_range_only", "TREND", "BULLISH"))
        self.assertIsNone(g("pullbacks_in_range_only", "TREND", "MIXED"))
        self.assertIsNone(g("pullbacks_in_range_only", "COUNTER_TREND", "BEARISH"))
        self.assertIsNone(g("pullbacks_in_range_only", "BREAKOUT_CONTINUATION", "BEARISH"))
        self.assertIsNone(g(None, "TREND", "BEARISH"))

    def test_trend_pullback_in_trending_macro_is_refused_only_by_the_gated_variants(self):
        for v in ("CORE", "CORE_regimeGated", "CORE_noRejection", "CORE_regimeGated_noRejection"):
            self.rp._consider(v, self.cand("SWEEP"), self.state, [], [], self.as_of)
        self.assertEqual(len(self.rp.trades["CORE"]), 1)
        self.assertEqual(len(self.rp.trades["CORE_noRejection"]), 1)
        self.assertEqual(len(self.rp.trades["CORE_regimeGated"]), 0)
        self.assertEqual(len(self.rp.trades["CORE_regimeGated_noRejection"]), 0)
        self.assertTrue(any(k.startswith("regime gate") for k in self.rp.funnel))

    def test_trend_pullback_in_mixed_macro_passes_the_gate(self):
        st = dict(self.state, macro_trend="MIXED", trend_1day="BEARISH")
        self.rp._consider("CORE_regimeGated", self.cand("SWEEP"), st, [], [], self.as_of)
        self.assertEqual(len(self.rp.trades["CORE_regimeGated"]), 1)
        self.assertEqual(self.rp.trades["CORE_regimeGated"][0].trade_class, "TREND")

    def test_rejection_confirmation_is_refused_by_the_no_rejection_variants(self):
        st = dict(self.state, macro_trend="MIXED")
        for v in ("CORE", "CORE_noRejection", "CORE_regimeGated", "CORE_regimeGated_noRejection"):
            self.rp._consider(v, self.cand("REJECTION"), st, [], [], self.as_of)
        self.assertEqual(len(self.rp.trades["CORE"]), 1)
        self.assertEqual(len(self.rp.trades["CORE_regimeGated"]), 1)
        self.assertEqual(len(self.rp.trades["CORE_noRejection"]), 0)
        self.assertEqual(len(self.rp.trades["CORE_regimeGated_noRejection"]), 0)

    def test_old_variants_unchanged_by_the_eighth_element(self):
        """OLD_v1.5 has five elements; the unpacking must still default sessions/confirmations/gate to None."""
        self.rp._consider("OLD_v1.5", self.cand("REJECTION"), self.state, [], [], self.as_of)
        self.assertEqual(len(self.rp.trades["OLD_v1.5"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
