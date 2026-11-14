"""1.14.1: a pending order that filled AND closed between two checks is journalled from the broker's deals, not as
CANCELLED (10 Nov: LAB-BUY-4124 -15.10 while the PC slept, LAB-BUY-4057 -4.85 in two seconds on the CPI spike)."""
from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
for p in (HERE.parent / "src", HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from autotrader import alerts, executor as EX  # noqa: E402

UTC = timezone.utc
T_IN = int(datetime(2026, 11, 10, 13, 30, 4, tzinfo=UTC).timestamp())
T_OUT = T_IN + 2


def deal(entry, t, profit=0.0, pos=60568676):
    return types.SimpleNamespace(entry=entry, time=t, profit=profit, commission=0.0, swap=0.0, position_id=pos)


class _Market:
    symbol = "XAUUSD"
    CONST = {"DEAL_ENTRY_IN": 0, "DEAL_ENTRY_OUT": 1}

    def __init__(self, deals):
        self.deals = deals

    def const(self, name):
        return self.CONST.get(name, 0)

    def server_to_utc(self, t):
        return t

    def quote(self):
        return {"bid": 4047.6, "ask": 4047.9, "spread": 0.3, "age": 1}

    def symbol_info(self):
        return types.SimpleNamespace(point=0.01, digits=2, volume_min=0.01, volume_step=0.01, filling_mode=7)

    def trade_call(self, name, **kw):
        if name == "history_deals_get":
            return self.deals.get(kw.get("position"), [])
        return []


class _State:
    def __init__(self, rows):
        self.rows = rows

    def active_orders(self, include_lab=True):
        return [r for r in self.rows if r["status"] in ("PENDING", "OPEN", "TP1_DONE")]

    def all_orders(self):
        return self.rows

    def update_order(self, ticket, **f):
        for r in self.rows:
            if r["ticket"] == ticket:
                r.update(f)

    def closed_without_result(self):
        return [r for r in self.rows if r["status"] == "CLOSED" and r.get("result") is None and r.get("position_ticket")]

    def closed_unreviewed(self):
        return []


def rec(ticket=60568676, status="PENDING", note=None, created="2026-11-10T11:07:45Z"):
    return {"ticket": ticket, "setup_id": "LAB-BUY-4057", "direction": "BUY", "status": status, "lab": 1,
            "position_ticket": None, "expiry_utc": "2026-11-10T23:07:45Z", "cancel_above": None, "cancel_below": None,
            "tp1": 4084.11, "tp2": None, "risk_money": 10.5, "note": note, "created_utc": created, "exit_utc": None}


class MissedFillTest(unittest.TestCase):
    def setUp(self):
        self.msgs = []
        self._t, self._e = alerts.trade, alerts.error
        alerts.trade = lambda m, short=None: self.msgs.append(m)
        alerts.error = lambda m: self.msgs.append("ERR " + m)

    def tearDown(self):
        alerts.trade, alerts.error = self._t, self._e

    def x(self, rows, deals):
        x = EX.Executor({"magic": 40001, "magic_lab": 40003, "lab_mode": True}, market=_Market(deals), state=_State(rows))
        x._repaired = True                                          # the start-up repair is tested separately
        return x

    def test_the_cpi_case_is_closed_with_its_loss_not_cancelled(self):
        rows = [rec()]
        x = self.x(rows, {60568676: [deal(0, T_IN), deal(1, T_OUT, -4.85)]})
        self.assertTrue(x.filled_and_closed(rows[0]))
        self.assertEqual((rows[0]["status"], rows[0]["position_ticket"]), ("CLOSED", 60568676))
        self.assertEqual((rows[0]["fill_utc"], rows[0]["closed_utc"]), ("2026-11-10T13:30:04Z", "2026-11-10T13:30:06Z"))
        x.fill_results()
        self.assertEqual(rows[0]["result"], -4.85)

    def test_no_deals_means_really_cancelled(self):
        rows = [rec()]
        x = self.x(rows, {})
        self.assertFalse(x.filled_and_closed(rows[0]))
        self.assertEqual(rows[0]["status"], "PENDING")

    def test_filled_without_a_close_deal_becomes_open(self):
        rows = [rec()]
        x = self.x(rows, {60568676: [deal(0, T_IN)]})
        self.assertTrue(x.filled_and_closed(rows[0]))
        self.assertEqual(rows[0]["status"], "OPEN")

    def test_startup_repair_fixes_old_wrong_cancellations_once(self):
        rows = [rec(status="CANCELLED", note="no longer at the broker", created=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")),
                dict(rec(ticket=2, status="CANCELLED", note="TP1 touched before the fill"), setup_id="LAB-SELL-1")]
        x = EX.Executor({"magic": 40001, "magic_lab": 40003, "lab_mode": True},
                        market=_Market({60568676: [deal(0, T_IN), deal(1, T_OUT, -4.85)], 2: [deal(0, T_IN, pos=2)]}),
                        state=_State(rows))
        self.assertEqual(x.repair_missed_fills(), [60568676])            # only 'no longer at the broker' rows
        self.assertEqual(rows[0]["status"], "CLOSED")
        self.assertEqual(rows[1]["status"], "CANCELLED")
        self.assertEqual(x.repair_missed_fills(), [])                     # once per start
        self.assertTrue(any("Journal repaired" in m for m in self.msgs))


if __name__ == "__main__":
    unittest.main()
