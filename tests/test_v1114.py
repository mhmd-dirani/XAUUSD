"""1.11.4: the overnight engine has its own slot. On 4 Nov at 23:45 UTC it was refused with "1 position(s) + 1 pending
order(s) already working (max 2)": the swing engine's slot count was applied to it. Offline, no MT5."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from autotrader import validator as VD  # noqa: E402
from autotrader.state import State  # noqa: E402
from autotrader.validator import Rejected  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 11, 4, 23, 45, tzinfo=UTC)
OVN = 40004


def _pos(magic, ticket=1, buy=True):
    return types.SimpleNamespace(magic=magic, ticket=ticket, type=0 if buy else 1, sl=4000.0, price_open=4050.0)


class _Market:
    symbol = "XAUUSD"

    def __init__(self):
        self.positions, self.orders, self.news = [], [], None

    def is_demo(self):
        return True

    def terminal(self):
        return types.SimpleNamespace(connected=True, trade_allowed=True)

    def trading_window(self, now):
        return None

    def news_block(self, now):
        return self.news

    def quote(self):
        return {"bid": 4050.0, "ask": 4050.3, "spread": 0.3, "age": 1}

    def account(self):
        return types.SimpleNamespace(balance=10000.0, equity=10000.0)

    def trade_call(self, name, **kw):
        return {"positions_get": self.positions, "orders_get": self.orders}[name]


class OwnSlotTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.m = _Market()
        self.state = State(self.dir)
        c = {"state_path": self.dir, "require_demo": True, "trade_sessions": ["LONDON"], "magic": 40001,
             "max_active_orders": 2, "max_trades_per_day": 3, "daily_loss_limit_pct": 2, "weekly_loss_limit_pct": 5,
             "v6": {"quote_max_age_seconds": 15}, "execute_grades": ["A_TRADE", "B_TRADE"],
             "max_orders_per_setup_per_day": 1, "duplicate_atr": 0.5, "require_evidence": False,
             "block_on_mtf_opposition": False, "max_same_direction_positions": 1}
        self.v = VD.Validator(c, self.m, self.state)
        self.v.realised_pnl_since = lambda since: 0.0
        self.v.netting = lambda: False
        self.v.check_levels = lambda order, atr15, now: {"direction": order["direction"], "entry_price": order["entry"],
                                                         "stop_loss": order["sl"], "evidence_ids": []}

    def add(self, ticket, setup_id, direction="BUY", entry=4050.0, sl=4035.0, status="PENDING"):
        with sqlite3.connect(self.state.path) as c:
            c.execute("INSERT INTO orders (ticket,setup_id,grade,direction,order_type,entry,sl,tp1,tp1_close_pct,be_at_tp1,"
                      "expiry_utc,lots,risk_money,rr,evidence_ids,created_utc,status) VALUES "
                      "(?,?,'B_TRADE',?,'BUY_LIMIT',?,?,4100,50,1,'2026-11-05T00:00:00Z',0.01,15,2,'[]',?,?)",
                      (ticket, setup_id, direction, entry, sl, "2026-11-04T20:00:00Z", status))

    def test_the_4_nov_case_swing_slots_full_overnight_still_free(self):
        self.m.positions, self.m.orders = [_pos(40001)], [_pos(40001, 2)]
        self.assertIn("already working (max 2)", self.v.gates(NOW, sessions=False))     # the swing engine: full
        self.assertIsNone(self.v.gates(NOW, sessions=False, own_magic=OVN))               # the overnight slot: free

    def test_own_position_blocks_the_overnight_slot(self):
        self.m.positions = [_pos(OVN)]
        self.assertIn("own slot busy", self.v.gates(NOW, sessions=False, own_magic=OVN))
        self.assertIsNone(self.v.gates(NOW, sessions=False))                              # ...and not the swing slots

    def test_netting_account_any_position_blocks_it(self):
        self.v.netting = lambda: True
        self.m.positions = [_pos(12345)]                                                  # even a manual trade
        self.assertIn("netting account", self.v.gates(NOW, sessions=False, own_magic=OVN))

    def test_swing_daily_count_ignores_overnight_orders_and_vice_versa(self):
        self.add(1, "IDEA-BUY-1", status="CLOSED")
        self.add(2, "IDEA-BUY-2", status="CLOSED")
        self.add(3, "OVN-20261103", status="CLOSED")
        self.assertEqual(self.state.orders_created_since("2026-11-04T00:00:00Z"), 2)
        self.assertIsNone(self.v.gates(NOW, sessions=False))
        self.add(4, "IDEA-BUY-3", status="CLOSED")
        self.assertEqual(self.v.gates(NOW, sessions=False), "max trades per day reached")
        self.assertIsNone(self.v.gates(NOW, sessions=False, own_magic=OVN))              # its own one-a-day rule applies

    def test_safety_gates_still_apply_to_the_overnight_slot(self):
        self.m.news = "CPI at 13:30"
        self.assertTrue(self.v.gates(NOW, sessions=False, own_magic=OVN).startswith("news lock"))
        self.m.news = None
        (self.dir / "STOP").write_text("")
        self.assertIn("kill file", self.v.gates(NOW, sessions=False, own_magic=OVN))
        (self.dir / "STOP").unlink()
        self.v.realised_pnl_since = lambda since: -500.0
        self.assertIn("loss limit", self.v.gates(NOW, sessions=False, own_magic=OVN))

    def order(self, setup_id, entry=4050.0, sl=4035.0):
        return {"setup_id": setup_id, "direction": "BUY", "entry": entry, "sl": sl}

    def test_validate_overnight_is_not_a_duplicate_of_a_swing_order(self):
        self.add(1, "IDEA-BUY-1")
        self.m.positions = [_pos(40001)]                                                  # a risky swing BUY
        with self.assertRaises(Rejected):                                                 # the swing engine: duplicate
            self.v.validate(self.order("IDEA-BUY-9"), "B_TRADE", {}, 10.0, NOW)
        out = self.v.validate(self.order("OVN-20261104"), "B_TRADE", {}, 10.0, NOW, own_magic=OVN)
        self.assertEqual(out["direction"], "BUY")

    def test_validate_swing_is_not_a_duplicate_of_an_overnight_order(self):
        self.add(1, "OVN-20261104", status="OPEN")
        out = self.v.validate(self.order("IDEA-BUY-9"), "B_TRADE", {}, 10.0, NOW)
        self.assertEqual(out["direction"], "BUY")

    def test_validate_netting_still_one_order_at_a_time(self):
        self.v.netting = lambda: True
        self.add(1, "IDEA-BUY-1")
        with self.assertRaises(Rejected):
            self.v.validate(self.order("OVN-20261104", entry=4100, sl=4080), "B_TRADE", {}, 10.0, NOW, own_magic=OVN)


class _RiskMarket(_Market):
    """Positions/orders with stops for the account-wide risk total: $1 per 0.01 lot per $1 of stop."""
    SI = types.SimpleNamespace(trade_tick_size=0.01, trade_tick_value=1.0, volume_step=0.01, volume_min=0.01,
                               volume_max=100.0)

    def const(self, name):
        return {"POSITION_TYPE_BUY": 0, "ORDER_TYPE_BUY": 0, "ORDER_TYPE_BUY_LIMIT": 2}.get(name, 99)

    def symbol_info(self):
        return self.SI

    def trade_call(self, name, *a, **kw):
        if name == "symbol_info":
            return self.SI
        return super().trade_call(name, **kw)


def _risky(magic, ticket, lots=0.01, entry=4050.0, sl=4025.0, stop=True):
    return types.SimpleNamespace(magic=magic, ticket=ticket, symbol="XAUUSD", type=0, price_open=entry,
                                 sl=sl if stop else 0.0, volume=lots, volume_current=lots, volume_initial=lots)


class LabOutsideLiveRiskTest(unittest.TestCase):
    """1.11.5: on 5 Nov the lab's three samples held ~$66 of the live 1 % ($100), so a Tier B trade would be refused."""
    def setUp(self):
        self.m = _RiskMarket()
        c = {"magic": 40001, "magic_lab": 40003, "lab_mode": True, "max_total_risk_pct": 2.0,
             "tiers": {"A": {"risk_pct": 1.0}, "B": {"risk_pct": 0.5}, "C": {"risk_pct": 0.15}},
             "risk_percent": {"A_TRADE": 1.0, "B_TRADE": 0.5}}
        self.v = VD.Validator(dict(c, v6={}), self.m, None)
        self.order = {"entry_price": 4050.0, "stop_loss": 4040.0}                  # $10 stop -> 0.05 lots at 0.5 %

    def test_lab_samples_do_not_use_the_live_budget(self):
        self.m.positions = [_risky(40003, k, lots=0.06) for k in (1, 2, 3)]        # lab: 3 x $150 = $450
        self.assertEqual(self.v.open_risk({40003})[0], 0.0)
        self.assertEqual(self.v.open_risk()[0], 450.0)                               # still visible when asked
        lots, risk = self.v.lots(self.order, "B_TRADE", risk_pct=0.5)
        self.assertEqual((lots, risk), (0.05, 50.0))

    def test_live_and_manual_trades_still_count(self):
        self.m.positions = [_risky(40001, 1, lots=0.06), _risky(0, 2, lots=0.02)]   # live $150 + manual $50
        with self.assertRaises(Rejected):                                             # 200 + 50 > 2 % of 10,000
            self.v.lots(self.order, "B_TRADE", risk_pct=0.5)

    def test_a_lab_order_without_a_stop_still_blocks(self):
        self.m.positions = [_risky(40003, 1, stop=False)]
        with self.assertRaises(Rejected):
            self.v.lots(self.order, "B_TRADE", risk_pct=0.5)

    def test_lab_off_counts_everything(self):
        self.v.c["lab_mode"] = False
        self.assertEqual(self.v.lab_magics(), set())


if __name__ == "__main__":
    unittest.main()
