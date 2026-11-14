"""1.11.6: a minimum-lot position takes the whole profit at TP1; the lab treats one zone bought twice as one sample; the
lab tags a TP1 that reaches past the opposite idea's entry. Offline, no MT5, no Telegram."""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))

from autotrader import alerts, executor as EX, lab as LB  # noqa: E402
from test_v111 import _JournalState, _Market, _Pos  # noqa: E402

class _Mkt(_Market):
    CONST = dict(_Market.CONST, TRADE_ACTION_SLTP=6)


SI = types.SimpleNamespace(point=0.01, digits=2, volume_min=0.01, volume_step=0.01, filling_mode=7)
Q = {"bid": 4084.0, "ask": 4084.3, "spread": 0.3, "age": 1}


class Tp1Test(unittest.TestCase):
    def setUp(self):
        self.msgs = []
        self._t, self._e = alerts.trade, alerts.error
        alerts.trade = lambda msg, short=None: self.msgs.append(msg)
        alerts.error = lambda msg: self.msgs.append("ERR " + msg)

    def tearDown(self):
        alerts.trade, alerts.error = self._t, self._e

    def run_tp1(self, volume, buy=False):
        rec = {"ticket": 60272095, "setup_id": "IDEA-SELL-4120", "direction": "BUY" if buy else "SELL", "status": "OPEN",
               "tp1": 4084.25, "tp2": 4071.41, "tp1_close_pct": 50, "be_at_tp1": 1, "lab": 0}
        rows = [rec]
        x = EX.Executor({"magic": 40001, "magic_lab": 40003, "lab_mode": False}, market=_Mkt([]),
                        state=_JournalState(rows))
        sent = []
        x._send = lambda req: (sent.append(req), types.SimpleNamespace(order=1, price=4084.0))[1]
        pos = _Pos(60272095, magic=40001, volume=volume, typ=0 if buy else 1, price_open=4116.76, sl=4132.5, tp=4071.41)
        x._tp1(rec, pos, SI, buy, Q)
        return rec, sent

    def test_the_5_nov_case_minimum_lot_closes_whole_at_tp1(self):
        rec, sent = self.run_tp1(0.01)
        self.assertEqual(len(sent), 1)                                  # one market close, no breakeven move
        self.assertEqual((sent[0]["action"], sent[0]["volume"], sent[0]["type"], sent[0]["price"]), (1, 0.01, 0, 4084.3))
        self.assertEqual(rec["status"], "TP1_DONE")
        self.assertIn("whole position closed", self.msgs[-1])
        self.assertNotIn("partial", self.msgs[-1])

    def test_a_splittable_position_still_closes_half_and_moves_to_breakeven(self):
        rec, sent = self.run_tp1(0.04)
        self.assertEqual(sent[0]["volume"], 0.02)                       # half closed at TP1
        self.assertEqual(sent[1]["sl"], 4116.76)                        # rest at breakeven
        self.assertEqual(rec["status"], "TP1_DONE")
        self.assertIn("partial closed", self.msgs[-1])

    def test_0_03_lots_closes_one_lot_step(self):
        _, sent = self.run_tp1(0.03, buy=True)
        self.assertEqual(sent[0]["volume"], 0.01)
        self.assertEqual(sent[0]["type"], 1)                            # SELL to close a BUY


class LabDuplicateTest(unittest.TestCase):
    TOL = 0.5 * 9.0                                                     # duplicate_atr 0.5 x ATR15 ~9

    def working(self, entry, sl, direction="BUY"):
        return {"lab": 1, "ticket": 60222022, "setup_id": "LAB-BUY-4117", "direction": direction, "entry": entry, "sl": sl}

    def order(self, entry, sl, direction="BUY"):
        return {"direction": direction, "entry_price": entry, "stop_loss": sl}

    def test_the_5_nov_case_same_zone_two_maps_is_one_sample(self):
        self.assertIsNotNone(LB.duplicate_of(self.order(4117.64, 4095.50), [self.working(4123.62, 4096.00)], self.TOL))

    def test_the_28_oct_case_still_a_duplicate(self):
        self.assertIsNotNone(LB.duplicate_of(self.order(4043.0, 4027.5), [self.working(4043.0, 4027.5)], self.TOL))

    def test_a_different_stop_is_a_different_trade(self):
        # 5 Nov LAB-BUY-4118 (4119.43 / sl 4107.50) while 4117 (sl 4096) was open: another idea, kept
        self.assertIsNone(LB.duplicate_of(self.order(4119.43, 4107.50), [self.working(4123.62, 4096.00)], self.TOL))

    def test_far_entries_or_other_direction_are_not_duplicates(self):
        self.assertIsNone(LB.duplicate_of(self.order(4150.0, 4096.0), [self.working(4123.62, 4096.0)], self.TOL))
        self.assertIsNone(LB.duplicate_of(self.order(4117.64, 4095.5, "SELL"), [self.working(4123.62, 4096.0)], self.TOL))
        live = dict(self.working(4123.62, 4096.0), lab=0)
        self.assertIsNone(LB.duplicate_of(self.order(4117.64, 4095.5), [live], self.TOL))


class TpBeyondOppositeTest(unittest.TestCase):
    SELL_IDEA = {"direction": "SELL", "entry_low": 4140.29, "entry_high": 4147.03}
    BUY_IDEA = {"direction": "BUY", "entry_low": 4076.0, "entry_high": 4084.0}

    def test_the_5_nov_buy_target_inside_the_sell_zone(self):
        o = {"direction": "BUY", "entry_price": 4123.62, "take_profit_1": 4146.12}
        self.assertTrue(LB.tp_beyond_opposite(o, [self.SELL_IDEA]))

    def test_target_at_the_opposite_entry_is_consistent(self):
        o = {"direction": "BUY", "entry_price": 4110.0, "take_profit_1": 4140.29}
        self.assertFalse(LB.tp_beyond_opposite(o, [self.SELL_IDEA]))

    def test_sell_mirror(self):
        self.assertTrue(LB.tp_beyond_opposite({"direction": "SELL", "entry_price": 4117.0, "take_profit_1": 4080.0},
                                              [self.BUY_IDEA]))
        self.assertFalse(LB.tp_beyond_opposite({"direction": "SELL", "entry_price": 4117.0, "take_profit_1": 4084.25},
                                               [self.BUY_IDEA]))

    def test_no_ideas_or_same_direction_or_bad_data(self):
        o = {"direction": "BUY", "entry_price": 4123.62, "take_profit_1": 4146.12}
        self.assertFalse(LB.tp_beyond_opposite(o, None))
        self.assertFalse(LB.tp_beyond_opposite(o, [dict(self.SELL_IDEA, direction="BUY")]))
        self.assertFalse(LB.tp_beyond_opposite(o, [{"direction": "SELL", "entry_low": None}]))

    def test_tag_is_registered(self):
        self.assertIn("tp_beyond_opposite", LB.TAGS)


if __name__ == "__main__":
    unittest.main()
