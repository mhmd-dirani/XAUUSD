"""1.11.1 tests: a lab limit whose TP1 traded before the fill is kept and put back to Astra (lab_keep_after_tp_touch).
Offline, no MT5, no Telegram."""
from __future__ import annotations

import json
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from autotrader import alerts, executor as EX, lab as LB  # noqa: E402

UTC = timezone.utc
SI = types.SimpleNamespace(trade_stops_level=10, point=0.01, digits=2, volume_min=0.01, volume_step=0.01, filling_mode=7)
C_LAB = {"confirm_entry_tolerance": 3.0, "confirm_retest_hours": 4, "idea_order_hours": 12, "max_order_lifetime_hours": 48,
         "signal_entry_tolerance": 1.0, "max_entry_deviation_cap": 3.0}


def idea(direction="SELL", lo=3949.66, hi=3953.0, stop=3965.5, tp1=3926.2, iid="I1"):
    return {"id": iid, "direction": direction, "entry_low": lo, "entry_high": hi, "invalidation": stop, "targets": [tp1]}


class PlanTest(unittest.TestCase):
    NOW = datetime(2026, 11, 4, 0, 30, tzinfo=UTC)

    def test_tp1_already_reached_is_refused_by_default(self):
        o, why = LB.plan(idea(), {"bid": 3925.0, "ask": 3925.3}, C_LAB, self.NOW, None, SI)
        self.assertIsNone(o)
        self.assertEqual(why, "TP1 already reached")

    def test_with_the_switch_it_waits_at_the_zone_edge(self):
        c = dict(C_LAB, lab_keep_after_tp_touch=True)
        o, why = LB.plan(idea(), {"bid": 3925.0, "ask": 3925.3}, c, self.NOW, None, SI)
        self.assertIsNone(why)
        self.assertEqual((o["order_type"], o["entry_price"]), ("SELL_LIMIT", 3949.66))   # the zone edge nearest price
        self.assertIsNone(o["cancel_if_price_below"])                 # 1.11.3: a plain limit, no touch / re-map
        self.assertTrue(o["_tp_passed_at_entry"])
        o, _ = LB.plan(idea("BUY", 3932.0, 3935.0, 3925.9, 3949.66), {"bid": 3955.0, "ask": 3955.3}, c, self.NOW,
                       "REJECTION", SI)
        self.assertEqual((o["order_type"], o["entry_price"]), ("BUY_LIMIT", 3935.0))     # never MARKET above TP1

    def test_4_nov_sell_the_rally_idea(self):
        """18:18 map: I2-SELL-CT SELL 4075.19-4082.68 stop 4087 TP1 4057.44 with price ~4050, below its TP1."""
        c = dict(C_LAB, lab_keep_after_tp_touch=True)
        o, why = LB.plan(idea("SELL", 4075.19, 4082.68, 4087.0, 4057.44), {"bid": 4050.0, "ask": 4050.2}, c, self.NOW,
                         None, SI)
        self.assertIsNone(why)
        self.assertEqual((o["order_type"], o["entry_price"], o["stop_loss"], o["take_profit_1"]),
                         ("SELL_LIMIT", 4075.19, 4087.0, 4057.44))
        self.assertIsNone(o["cancel_if_price_above"])
        self.assertIsNone(o["cancel_if_price_below"])

    def test_normal_ideas_still_get_their_cancel_level(self):
        c = dict(C_LAB, lab_keep_after_tp_touch=True)
        o, _ = LB.plan(idea(), {"bid": 3940.0, "ask": 3940.3}, c, self.NOW, None, SI)
        self.assertEqual(o["cancel_if_price_below"], 3926.2)
        self.assertFalse(o["_tp_passed_at_entry"])

    def test_normal_ideas_unchanged_by_the_switch(self):
        c = dict(C_LAB, lab_keep_after_tp_touch=True)
        a, _ = LB.plan(idea(), {"bid": 3940.0, "ask": 3940.3}, C_LAB, self.NOW, None, SI)
        b, _ = LB.plan(idea(), {"bid": 3940.0, "ask": 3940.3}, c, self.NOW, None, SI)
        self.assertEqual((a["order_type"], a["entry_price"]), (b["order_type"], b["entry_price"]))


# ------------------------------------------------------------------ executor
class _Order:
    def __init__(self, ticket, magic):
        self.ticket, self.magic, self.identifier, self.comment, self.volume_initial = ticket, magic, ticket, "", 0.01


class _Market:
    symbol = "XAUUSD"

    def __init__(self, orders, bid, ask):
        self.orders, self.bid, self.ask, self.sent = orders, bid, ask, []

    def const(self, name):
        return {"POSITION_TYPE_BUY": 0, "TRADE_ACTION_REMOVE": 8}.get(name, 0)

    def quote(self):
        return {"bid": self.bid, "ask": self.ask, "spread": self.ask - self.bid, "age": 1}

    def symbol_info(self):
        return SI

    def trade_call(self, name, **kw):
        return self.orders if name == "orders_get" else []


class _State:
    def __init__(self, rows):
        self.rows = rows

    def active_orders(self, include_lab=True):
        return [r for r in self.rows if r["status"] in ("PENDING", "OPEN", "TP1_DONE")]

    def all_orders(self):
        return self.rows

    def update_order(self, ticket, **fields):
        for r in self.rows:
            if r["ticket"] == ticket:
                r.update(fields)

    def closed_unreviewed(self):
        return []

    def closed_without_result(self):
        return []


def lab_rec(ticket=60084880, lab=1, direction="SELL", entry=3949.66, below=3926.2, above=None):
    return {"ticket": ticket, "setup_id": "LAB-SELL-3952", "direction": direction, "status": "PENDING", "lab": lab,
            "entry": entry, "sl": 3965.5, "tp1": 3926.2, "tp2": None, "position_ticket": None,
            "expiry_utc": "2026-11-04T12:01:48Z", "cancel_above": above, "cancel_below": below,
            "violations": json.dumps(["session_off", "no_confirmation"]), "playbook": "TREND"}


class ExecutorTest(unittest.TestCase):
    NOW = datetime(2026, 11, 4, 0, 32, tzinfo=UTC)

    def setUp(self):
        self.msgs = []
        self._t, self._e = alerts.trade, alerts.error
        alerts.trade = lambda msg, short=None: self.msgs.append(msg)
        alerts.error = lambda msg: self.msgs.append("ERR " + msg)

    def tearDown(self):
        alerts.trade, alerts.error = self._t, self._e

    def run_manage(self, rec, keep, bid=3925.0, ask=3925.3, magic=40003):
        c = {"magic": 40001, "magic_lab": 40003, "lab_mode": True, "lab_keep_after_tp_touch": keep}
        m = _Market([_Order(rec["ticket"], magic)], bid, ask)
        st = _State([rec])
        x = EX.Executor(c, market=m, state=st)
        x._send = lambda req: (m.sent.append(req), types.SimpleNamespace(order=1, price=bid))[1]
        x.manage(self.NOW)
        return m, rec

    def test_without_the_switch_the_lab_order_is_cancelled_as_before(self):
        m, rec = self.run_manage(lab_rec(), keep=False)
        self.assertEqual(rec["status"], "CANCELLED")
        self.assertEqual(len(m.sent), 1)

    def test_with_the_switch_it_is_kept_and_tagged(self):
        m, rec = self.run_manage(lab_rec(), keep=True)
        self.assertEqual(m.sent, [])                                     # nothing sent to the broker
        self.assertEqual(rec["status"], "PENDING")
        self.assertIsNone(rec["cancel_below"])
        self.assertEqual(rec["tp_touched_utc"], "2026-11-04T00:32:00Z")
        self.assertIn("tp_touched_first", json.loads(rec["violations"]))
        self.assertTrue(any("kept" in msg for msg in self.msgs))

    def test_a_buy_limit_is_kept_too(self):
        rec = lab_rec(ticket=60092146, direction="BUY", entry=3940.43, below=None, above=3960.64)
        m, rec = self.run_manage(rec, keep=True, bid=3961.0, ask=3961.3)
        self.assertEqual(rec["status"], "PENDING")
        self.assertIsNotNone(rec.get("tp_touched_utc"))

    def test_live_orders_are_never_kept(self):
        m, rec = self.run_manage(lab_rec(lab=0), keep=True, magic=40001)
        self.assertEqual(rec["status"], "CANCELLED")

    def test_expiry_still_cancels(self):
        rec = lab_rec()
        rec["expiry_utc"] = "2026-11-04T00:00:00Z"
        m, rec = self.run_manage(rec, keep=True)
        self.assertEqual(rec["status"], "EXPIRED")


# ------------------------------------------------------------------ re-asking Astra
class StillProposedTest(unittest.TestCase):
    def test_match_rules(self):
        rec = {"direction": "SELL", "entry": 3949.66}
        self.assertIsNotNone(LB.still_proposed(rec, [idea()], 1.0))
        self.assertIsNone(LB.still_proposed(rec, [idea("BUY", 3940, 3950, 3930, 3970)], 1.0))       # other direction
        self.assertIsNone(LB.still_proposed(rec, [idea(lo=3970, hi=3975, stop=3985, tp1=3950)], 1.0))  # zone moved
        self.assertIsNotNone(LB.still_proposed(rec, [idea(lo=3951.0, hi=3955.0)], 2.0))             # within tolerance
        self.assertIsNone(LB.still_proposed(rec, [idea(stop=3940.0)], 1.0))                           # stop now below
        self.assertIsNone(LB.still_proposed(rec, [{"direction": "SELL"}], 1.0))                       # malformed idea


class _Bridge:
    def __init__(self, a, age):
        self.a, self.age, self.requests = a, age, []

    def analysis(self, now):
        return self.a, self.age

    def request_remap(self, key, reason, now):
        self.requests.append((key, reason))
        return True


class _X:
    def __init__(self):
        self.cancelled = []

    def cancel(self, ticket, why, status="CANCELLED"):
        self.cancelled.append((ticket, why))
        return True


class RecheckTest(unittest.TestCase):
    NOW = datetime(2026, 11, 4, 1, 40, tzinfo=UTC)

    def setUp(self):
        self._t = alerts.trade
        alerts.trade = lambda msg, short=None: None

    def tearDown(self):
        alerts.trade = self._t

    def lab_with(self, analysis, age, rows, keep=True, remap=False):
        x = _X()
        self.bridge = _Bridge(analysis, age)
        app = types.SimpleNamespace(market=None, state=_State(rows), validator=None, executor=x,
                                    bridge=self.bridge, market_context=lambda: ({}, 5.0))
        c = {"lab_mode": True, "lab_keep_after_tp_touch": keep, "lab_tp_touch_remap": remap, "idea_max_age_minutes": 200,
             "duplicate_atr": 0.5, "confirm_entry_tolerance": 3.0,
             "lab_drop_rule": "unproposed"}          # these tests cover the 1.11.1 rule; 1.13.1 defaults to "contradicted"
        return LB.Lab(c, app), x

    def touched(self, at="2026-11-04T00:32:00Z"):
        r = lab_rec()
        r.update(cancel_below=None, tp_touched_utc=at)
        return r

    def test_newer_map_that_still_proposes_it_keeps_it(self):
        a = {"id": "A2", "created_at": "2026-11-04T01:34:00Z", "ideas": [idea()]}
        lab, x = self.lab_with(a, 6, [self.touched()])
        self.assertEqual(lab.recheck(self.NOW), [])
        self.assertEqual(x.cancelled, [])

    def test_newer_map_without_it_cancels_once(self):
        a = {"id": "A2", "created_at": "2026-11-04T01:34:00Z", "ideas": [idea("BUY", 3939, 3941, 3931.5, 3960.6)]}
        rows = [self.touched()]
        lab, x = self.lab_with(a, 6, rows)
        self.assertEqual(lab.recheck(self.NOW), [60084880])
        self.assertIn("no longer proposes", x.cancelled[0][1])
        self.assertEqual(lab.recheck(self.NOW), [])                      # the same map is asked once per order

    def test_a_map_made_before_the_touch_is_not_an_answer(self):
        a = {"id": "A1", "created_at": "2026-11-04T00:04:00Z", "ideas": []}
        lab, x = self.lab_with(a, 30, [self.touched()])
        self.assertEqual(lab.recheck(self.NOW), [])
        self.assertEqual(x.cancelled, [])

    def test_stale_analysis_or_switch_off_does_nothing(self):
        a = {"id": "A2", "created_at": "2026-11-04T01:34:00Z", "ideas": []}
        lab, x = self.lab_with(a, 500, [self.touched()])
        self.assertEqual(lab.recheck(self.NOW), [])
        lab, x = self.lab_with(a, 6, [self.touched()], keep=False)
        self.assertEqual(lab.recheck(self.NOW), [])
        self.assertEqual(x.cancelled, [])

    def test_untouched_and_live_orders_are_ignored(self):
        a = {"id": "A2", "created_at": "2026-11-04T01:34:00Z", "ideas": []}
        live = self.touched()
        live["lab"] = 0
        lab, x = self.lab_with(a, 6, [lab_rec(), live])
        self.assertEqual(lab.recheck(self.NOW), [])


class RemapRequestTest(RecheckTest):
    """1.11.2: a TP touch asks the signal bot for an early map at once (one request per order)."""

    def test_request_is_written_once_per_touched_order(self):
        a = {"id": "A1", "created_at": "2026-11-04T00:04:00Z", "ideas": []}          # older than the touch
        lab, x = self.lab_with(a, 30, [self.touched(), lab_rec(ticket=7)], remap=True)
        lab.recheck(self.NOW)
        lab.recheck(self.NOW)
        self.assertEqual([k for k, _ in self.bridge.requests], ["REMAP-60084880"])
        self.assertIn("TP1 3926.2 traded before the fill", self.bridge.requests[0][1])
        self.assertEqual(x.cancelled, [])                                             # the old map is no answer

    def test_no_request_when_switched_off(self):
        a = {"id": "A1", "created_at": "2026-11-04T00:04:00Z", "ideas": []}
        lab, x = self.lab_with(a, 30, [self.touched()], remap=False)
        lab.recheck(self.NOW)
        self.assertEqual(self.bridge.requests, [])


class BridgeFileTest(unittest.TestCase):
    def test_request_file_shape(self):
        import tempfile
        from autotrader import bridge as BR
        b = BR.Bridge.__new__(BR.Bridge)
        b.folder = Path(tempfile.mkdtemp())
        self.assertTrue(b.request_remap("REMAP-1", "x" * 300, datetime(2026, 11, 4, 0, 32, tzinfo=UTC)))
        req = json.loads((b.folder / "remap_request.json").read_text(encoding="utf-8"))
        self.assertEqual((req["id"], req["from"], len(req["reason"])), ("REMAP-1", "auto_trader", 200))
        self.assertTrue(req["at"].startswith("2026-11-04T00:32"))


class SettingsTest(unittest.TestCase):
    def test_default_off_and_validated(self):
        from autotrader import settings as ST
        self.assertIs(ST.DEFAULTS["lab_keep_after_tp_touch"], False)
        self.assertIs(ST.DEFAULTS["lab_tp_touch_remap"], False)
        self.assertIn("tp_touched_first", LB.TAGS)


if __name__ == "__main__":
    unittest.main()
