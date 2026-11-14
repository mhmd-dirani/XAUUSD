"""The Python-only overnight trend engine: schedule, order building, the time exit, loss-limit magics and its own
position slot.
Offline: no MT5, no network, no Claude, no Telegram."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent / "src", HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from autotrader import alerts, executor as EX, overnight as OV, validator as VD  # noqa: E402
from autotrader.state import State  # noqa: E402
from autotrader.validator import Rejected  # noqa: E402

UTC = timezone.utc


# ----------------------------------------------------------------------------------------------------------------------
# 1.11 tests: the Python-only overnight trend engine (autotrader/overnight.py), the executor's time exit, the loss-limit
# magics and the settings validation. Offline, no MT5, no Telegram.
# ----------------------------------------------------------------------------------------------------------------------
CFG = dict(OV.DEFAULTS, mode="live")


class ScheduleTest(unittest.TestCase):
    def test_entry_window_and_exit_in_summer(self):
        # Tue 7 Jul 2026, 18:45 NY (EDT) = 22:45 UTC; exit 08:00 NY next day = 12:00 UTC
        s = OV.schedule(CFG, datetime(2026, 7, 7, 22, 50, tzinfo=UTC))
        self.assertIsNotNone(s)
        self.assertEqual(s[1], datetime(2026, 7, 8, 12, 0, tzinfo=UTC))
        self.assertIsNone(OV.schedule(CFG, datetime(2026, 7, 7, 22, 44, tzinfo=UTC)))        # before the window
        self.assertIsNone(OV.schedule(CFG, datetime(2026, 7, 7, 23, 15, tzinfo=UTC)))        # window closed (30 min)

    def test_winter_uses_new_york_clock(self):
        # Tue 1 Dec 2026, 18:45 NY (EST) = 23:45 UTC
        self.assertIsNotNone(OV.schedule(CFG, datetime(2026, 12, 1, 23, 50, tzinfo=UTC)))
        self.assertIsNone(OV.schedule(CFG, datetime(2026, 12, 1, 22, 50, tzinfo=UTC)))

    def test_friday_and_saturday_never_sunday_yes(self):
        self.assertIsNone(OV.schedule(CFG, datetime(2026, 7, 10, 22, 50, tzinfo=UTC)))       # Friday evening
        self.assertIsNone(OV.schedule(CFG, datetime(2026, 7, 11, 22, 50, tzinfo=UTC)))       # Saturday
        s = OV.schedule(CFG, datetime(2026, 7, 12, 22, 50, tzinfo=UTC))                       # Sunday evening
        self.assertEqual(s[1], datetime(2026, 7, 13, 12, 0, tzinfo=UTC))

    def test_setup_id_is_the_new_york_date(self):
        self.assertEqual(OV.setup_id(datetime(2026, 7, 7, 22, 50, tzinfo=UTC)), "OVN-20260707")
        self.assertEqual(OV.setup_id(datetime(2026, 7, 8, 1, 0, tzinfo=UTC)), "OVN-20260707")   # still 7 Jul in NY


class BuildOrderTest(unittest.TestCase):
    def test_geometry(self):
        now = datetime(2026, 7, 7, 22, 50, tzinfo=UTC)
        o = OV.build_order(CFG, CFG["rules"][0], {"bid": 4000.0, "ask": 4000.4, "spread": 0.4}, 5.0, now, now + timedelta(hours=13))
        self.assertEqual((o["direction"], o["order_type"], o["entry_price"]), ("BUY", "MARKET", 4000.4))
        self.assertAlmostEqual(o["stop_loss"], 4000.4 - 15.0, places=2)
        self.assertAlmostEqual(o["take_profit_1"], 4000.4 + 100.0, places=2)
        self.assertIsNone(o["take_profit_2"])
        self.assertEqual(o["tp1_close_percent"], 100)
        self.assertFalse(o["move_sl_to_breakeven_at_tp1"])
        self.assertEqual(o["_strategy"], "OVERNIGHT_BULL_LONG")
        self.assertEqual(o["setup_id"], "OVN-20260707")
        self.assertIn("exit by time", o["rationale"])

    def test_short_rule_geometry(self):
        now = datetime(2026, 7, 7, 22, 50, tzinfo=UTC)
        rule = {"name": "BEAR_SHORT", "macro": "BEARISH", "direction": "SELL", "mode": "live"}
        o = OV.build_order(CFG, rule, {"bid": 4000.0, "ask": 4000.4, "spread": 0.4}, 5.0, now, now + timedelta(hours=13))
        self.assertEqual((o["direction"], o["entry_price"]), ("SELL", 4000.0))                 # sells at the bid
        self.assertAlmostEqual(o["stop_loss"], 4015.0, places=2)
        self.assertAlmostEqual(o["take_profit_1"], 3900.0, places=2)

    def test_rule_selection(self):
        cfg = dict(CFG, rules=[{"name": "BULL_LONG", "macro": "BULLISH", "direction": "BUY", "mode": "live"},
                               {"name": "BEAR_SHORT", "macro": "BEARISH", "direction": "SELL", "mode": "off"}])
        self.assertEqual(OV.rule_for(cfg, "BULLISH")["name"], "BULL_LONG")
        self.assertIsNone(OV.rule_for(cfg, "BEARISH"))                                       # switched off
        self.assertIsNone(OV.rule_for(cfg, "MIXED"))
        self.assertIsNone(OV.rule_for(cfg, None))


# ------------------------------------------------------------------ a fake app for Overnight.tick
class _State:
    def __init__(self):
        self.orders_since: list = []
        self.updates: list = []

    def setup_orders_since(self, sid, since):
        return self.orders_since

    def update_order(self, ticket, **fields):
        self.updates.append((ticket, fields))


class _Validator:
    def __init__(self, gate=None, reject=None):
        self.gate, self.reject, self.calls = gate, reject, []

    def gates(self, now, sessions=True, own_magic=None):
        self.calls.append(("gates", sessions, own_magic))
        return self.gate

    def validate(self, order, grade, evidence, atr15, now, require_evidence=None, counter_trend=False, own_magic=None):
        self.calls.append(("validate", grade, require_evidence, counter_trend, own_magic))
        if self.reject:
            raise Rejected(self.reject)
        return dict(order, rr=20.0, strategy=order["_strategy"], meta=order["_meta"])

    def lots(self, order, grade, risk_pct=None, min_lot_ok=False):
        self.calls.append(("lots", risk_pct, min_lot_ok))
        return 0.01, 15.0


class _Executor:
    def __init__(self):
        self.sent = []

    def send(self, order, lots, risk_money, grade, magic=None):
        self.sent.append((order["setup_id"], lots, magic))
        return 777


class _App:
    def __init__(self, macro="BULLISH", gate=None, reject=None, spread=0.3):
        self.state, self.validator, self.executor = _State(), _Validator(gate, reject), _Executor()
        self._macro, self._spread = macro, spread
        self.market = types.SimpleNamespace(quote=lambda: {"bid": 4000.0, "ask": 4000.0 + self._spread, "spread": self._spread, "age": 1})

    def market_context(self):
        return {"index": {}}, 5.0

    def market_state(self):
        return {"macro_trend": self._macro, "trend_1day": "BULLISH", "trend_4h": "BULLISH"}, {"session": "OFF"}

    def meta(self, direction, dec, how, idea=None, playbook=None):
        return {"playbook": playbook, "confirmation": how, "session": "OFF"}


class TickTest(unittest.TestCase):
    NOW = datetime(2026, 7, 7, 22, 50, tzinfo=UTC)          # Tuesday 18:50 NY, inside the window

    def setUp(self):
        self.msgs = []
        self._t, self._e = alerts.trade, alerts.error
        alerts.trade = lambda msg, short=None: self.msgs.append(msg)
        alerts.error = lambda msg: self.msgs.append("ERR " + msg)

    def tearDown(self):
        alerts.trade, alerts.error = self._t, self._e

    def engine(self, app, **over):
        c = {"overnight": dict(CFG, **over), "magic": 40001}
        return OV.Overnight(c, app)

    def test_off_does_nothing(self):
        app = _App()
        self.assertIsNone(self.engine(app, mode="off").tick(self.NOW))
        self.assertEqual(app.executor.sent, [])

    def test_outside_the_window_does_nothing(self):
        app = _App()
        self.assertIsNone(self.engine(app).tick(datetime(2026, 7, 7, 15, 0, tzinfo=UTC)))
        self.assertEqual(app.validator.calls, [])

    def test_bullish_macro_sends_one_order_through_the_validator(self):
        app = _App()
        eng = self.engine(app)
        out = eng.tick(self.NOW)
        self.assertEqual(out, "OVN-20260707: sent #777")
        self.assertEqual(app.executor.sent, [("OVN-20260707", 0.01, 40004)])
        self.assertIn(("gates", False, 40004), app.validator.calls)                 # sessions preference skipped...
        self.assertIn(("validate", "B_TRADE", False, False, 40004), app.validator.calls)   # ...everything else enforced
        self.assertIn(("lots", 0.25, False), app.validator.calls)            # minimum lot is refused, never forced
        self.assertEqual(app.state.updates[0][0], 777)
        self.assertEqual(app.state.updates[0][1]["exit_utc"], "2026-07-08T12:00:00Z")
        self.assertTrue(any("OVERNIGHT BULL_LONG BUY" in m for m in self.msgs))
        # a second tick in the same window: nothing more
        self.assertIsNone(eng.tick(self.NOW + timedelta(minutes=5)))
        self.assertEqual(len(app.executor.sent), 1)

    def test_mixed_macro_no_trade_and_no_retry_today(self):
        app = _App(macro="MIXED")
        eng = self.engine(app)
        self.assertIsNone(eng.tick(self.NOW))
        self.assertIsNone(eng.tick(self.NOW + timedelta(minutes=10)))
        self.assertEqual(app.executor.sent, [])
        self.assertFalse(any(c[0] == "validate" for c in app.validator.calls))

    def test_bearish_short_only_when_its_rule_is_live(self):
        app = _App(macro="BEARISH")
        self.assertIsNone(self.engine(app).tick(self.NOW))                      # default: BEAR_SHORT off
        self.assertEqual(app.executor.sent, [])
        app = _App(macro="BEARISH")
        rules = [{"name": "BULL_LONG", "macro": "BULLISH", "direction": "BUY", "mode": "live"},
                 {"name": "BEAR_SHORT", "macro": "BEARISH", "direction": "SELL", "mode": "live"}]
        out = self.engine(app, rules=rules).tick(self.NOW)
        self.assertEqual(out, "OVN-20260707: sent #777")
        self.assertEqual(app.executor.sent[0][2], 40004)
        # with-trend rules keep the veto (counter_trend False); the short in a bearish macro is with-trend too
        self.assertIn(("validate", "B_TRADE", False, False, 40004), app.validator.calls)

    def test_range_short_is_declared_counter_trend_for_the_veto(self):
        app = _App(macro="MIXED")
        rules = [{"name": "RANGE_SHORT", "macro": "MIXED", "direction": "SELL", "mode": "live"}]
        self.engine(app, rules=rules).tick(self.NOW)
        self.assertIn(("validate", "B_TRADE", False, True, 40004), app.validator.calls)

    def test_risk_engine_rejection_is_final_for_the_day(self):
        app = _App(reject="0.25% risk (12.50) is below one minimum lot (15.00)")
        eng = self.engine(app)
        out = eng.tick(self.NOW)
        self.assertIn("below one minimum lot", out)
        self.assertEqual(app.executor.sent, [])
        self.assertTrue(any("not traded" in m for m in self.msgs))
        self.assertIsNone(eng.tick(self.NOW + timedelta(minutes=5)))

    def test_hard_gate_blocks_for_the_day_news_lock_is_retried(self):
        app = _App(gate="daily loss limit hit (-40.00)")
        eng = self.engine(app)
        self.assertIsNone(eng.tick(self.NOW))
        app.validator.gate = None
        self.assertIsNone(eng.tick(self.NOW + timedelta(minutes=5)))          # day locked
        app2 = _App(gate="news lock: FOMC minutes")
        eng2 = self.engine(app2)
        self.assertIsNone(eng2.tick(self.NOW))
        app2.validator.gate = None
        self.assertEqual(eng2.tick(self.NOW + timedelta(minutes=5)), "OVN-20260707: sent #777")

    def test_wide_spread_waits_inside_the_window(self):
        app = _App(spread=0.9)
        eng = self.engine(app)
        self.assertIsNone(eng.tick(self.NOW))
        app._spread = 0.3
        self.assertEqual(eng.tick(self.NOW + timedelta(minutes=5)), "OVN-20260707: sent #777")

    def test_already_traded_today_after_a_restart(self):
        app = _App()
        app.state.orders_since = [{"ticket": 1, "status": "OPEN"}]
        self.assertIsNone(self.engine(app).tick(self.NOW))
        self.assertEqual(app.executor.sent, [])


# ------------------------------------------------------------------ executor: time exit and magics
class _Pos:
    def __init__(self, ticket, magic=40004, volume=0.01, typ=0, price_open=4000.0, sl=3985.0, tp=4100.0):
        self.ticket, self.magic, self.volume, self.type, self.price_open, self.sl, self.tp = ticket, magic, volume, typ, price_open, sl, tp
        self.identifier = ticket


class _Market:
    CONST = {"POSITION_TYPE_BUY": 0, "TRADE_ACTION_DEAL": 1, "ORDER_TYPE_SELL": 1, "ORDER_TYPE_BUY": 0,
             "ORDER_FILLING_IOC": 1, "ORDER_FILLING_FOK": 0, "ORDER_FILLING_RETURN": 2}
    symbol = "XAUUSD"

    def __init__(self, positions):
        self.positions, self.sent = positions, []

    def const(self, name):
        return self.CONST[name]

    def quote(self):
        return {"bid": 4010.0, "ask": 4010.3, "spread": 0.3, "age": 1}

    def symbol_info(self):
        return types.SimpleNamespace(point=0.01, digits=2, volume_min=0.01, volume_step=0.01, filling_mode=7)

    def trade_call(self, name, **kw):
        return self.positions if name == "positions_get" else []

    def order_send(self, req):
        self.sent.append(req)
        return types.SimpleNamespace(retcode=10009, order=1, price=4010.0, comment="ok")


class _JournalState:
    def __init__(self, rows):
        self.rows, self.updates = rows, []

    def active_orders(self, include_lab=True):
        return [r for r in self.rows if r["status"] in ("PENDING", "OPEN", "TP1_DONE")]

    def all_orders(self):
        return self.rows

    def update_order(self, ticket, **fields):
        self.updates.append((ticket, fields))
        for r in self.rows:
            if r["ticket"] == ticket:
                r.update(fields)

    def closed_unreviewed(self):
        return []

    def closed_without_result(self):
        return []


class TimeExitTest(unittest.TestCase):
    def setUp(self):
        self.msgs = []
        self._t, self._e = alerts.trade, alerts.error
        alerts.trade = lambda msg, short=None: self.msgs.append(msg)
        alerts.error = lambda msg: self.msgs.append("ERR " + msg)
        self.c = {"magic": 40001, "magic_lab": 40003, "lab_mode": False, "overnight": dict(CFG)}

    def tearDown(self):
        alerts.trade, alerts.error = self._t, self._e

    def _executor(self, rows, positions):
        m = _Market(positions)
        x = EX.Executor(self.c, market=m, state=_JournalState(rows))
        x._send = lambda req: (m.sent.append(req), types.SimpleNamespace(order=1, price=4010.0))[1]
        return x, m

    def record(self, exit_utc, status="OPEN"):
        return {"ticket": 777, "setup_id": "OVN-20260707", "direction": "BUY", "status": status, "position_ticket": 777,
                "tp1": 4100.0, "tp2": None, "tp1_close_pct": 100, "be_at_tp1": 0, "expiry_utc": "2026-07-07T23:20:00Z",
                "exit_utc": exit_utc, "lab": 0}

    def test_overnight_magic_is_managed_when_live(self):
        x, _ = self._executor([], [])
        self.assertEqual(x.magics(), {40001, 40004})
        self.c["overnight"]["mode"] = "off"
        self.assertEqual(x.magics(), {40001})

    def test_position_closed_at_market_at_exit_time(self):
        rows = [self.record("2026-07-08T12:00:00Z")]
        x, m = self._executor(rows, [_Pos(777)])
        x.manage(datetime(2026, 7, 8, 12, 0, 5, tzinfo=UTC))
        self.assertEqual(len(m.sent), 1)
        req = m.sent[0]
        self.assertEqual((req["action"], req["type"], req["position"], req["volume"]), (1, 1, 777, 0.01))   # DEAL, SELL to close
        self.assertEqual(req["price"], 4010.0)                                                          # at the bid
        self.assertEqual(req["magic"], 40004)
        self.assertEqual(rows[0]["status"], "CLOSED")
        self.assertTrue(any("Time exit" in msg for msg in self.msgs))

    def test_before_exit_time_nothing_happens(self):
        rows = [self.record("2026-07-08T12:00:00Z")]
        x, m = self._executor(rows, [_Pos(777)])
        x.manage(datetime(2026, 7, 8, 11, 59, tzinfo=UTC))
        self.assertEqual(m.sent, [])
        self.assertEqual(rows[0]["status"], "OPEN")

    def test_records_without_exit_utc_are_untouched(self):
        rows = [self.record(None)]
        x, m = self._executor(rows, [_Pos(777, magic=40001)])
        x.manage(datetime(2026, 7, 9, 12, 0, tzinfo=UTC))
        self.assertEqual(m.sent, [])

    def test_stopped_out_before_exit_is_closed_by_the_broker(self):
        rows = [self.record("2026-07-08T12:00:00Z")]
        x, m = self._executor(rows, [])                      # no position at the broker any more
        x.manage(datetime(2026, 7, 8, 3, 0, tzinfo=UTC))
        self.assertEqual(rows[0]["status"], "CLOSED")
        self.assertEqual(m.sent, [])


class LossMagicsTest(unittest.TestCase):
    def test_overnight_losses_count_toward_the_limits_when_live(self):
        from autotrader import validator as VD
        v = VD.Validator.__new__(VD.Validator)
        v.c = {"magic": 40001, "overnight": {"mode": "live", "magic": 40004}}
        self.assertEqual(v.loss_magics(), {40001, 40004})
        v.c = {"magic": 40001, "overnight": {"mode": "off", "magic": 40004}}
        self.assertEqual(v.loss_magics(), {40001})
        v.c = {"magic": 40001}
        self.assertEqual(v.loss_magics(), {40001})


class SettingsTest(unittest.TestCase):
    """settings.load with the V6 config and the Anthropic client faked (no files, no network)."""

    def setUp(self):
        from autotrader import settings as ST
        self.ST = ST
        self._lc = ST.GM.load_config
        ST.GM.load_config = lambda p: {"minimum_rr": 2.0, "block_on_mtf_opposition": True,
                                       "provider_options": {"broker_symbol": "XAUUSD"}}
        sys.modules.setdefault("astra_anthropic", types.SimpleNamespace(model_name=lambda v6: "claude-opus-5-5",
                                                                          EFFORTS=("low", "medium", "high")))

    def tearDown(self):
        self.ST.GM.load_config = self._lc

    def _load(self, **custom):
        import json
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(custom, f)
            path = f.name
        try:
            return self.ST.load(path)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_defaults_are_off(self):
        c = self._load()
        self.assertEqual(c["overnight"]["mode"], "off")
        self.assertTrue(c["expect_signal_bot"])

    def test_live_overnight_loads(self):
        c = self._load(overnight={"mode": "live"}, expect_signal_bot=False)
        self.assertEqual(c["overnight"]["mode"], "live")
        self.assertEqual(c["overnight"]["magic"], 40004)
        self.assertEqual(c["overnight"]["stop_atr"], 3.0)
        self.assertEqual([r["mode"] for r in c["overnight"]["rules"]], ["live", "off", "off"])
        c = self._load(overnight={"mode": "live", "rules": [
            {"name": "BULL_LONG", "macro": "BULLISH", "direction": "BUY", "mode": "live"},
            {"name": "BEAR_SHORT", "macro": "BEARISH", "direction": "SELL", "mode": "live"},
            {"name": "RANGE_SHORT", "macro": "MIXED", "direction": "SELL", "mode": "live"}]})
        self.assertEqual([r["mode"] for r in c["overnight"]["rules"]], ["live", "live", "live"])

    def test_live_needs_demo(self):
        with self.assertRaises(ValueError):
            self._load(overnight={"mode": "live"}, require_demo=False)

    def test_bad_values_fail_at_start(self):
        for bad in ({"mode": "paper"}, {"magic": 40001}, {"stop_atr": 9.0}, {"risk_pct": 5.0}, {"entry_ny": "25:00"},
                    {"rules": []}, {"rules": [{"name": "x", "macro": "SIDEWAYS", "direction": "BUY", "mode": "live"}]},
                    {"rules": [{"name": "a", "macro": "BULLISH", "direction": "BUY", "mode": "live"},
                               {"name": "b", "macro": "BULLISH", "direction": "SELL", "mode": "live"}]}):
            with self.assertRaises(ValueError, msg=str(bad)):
                self._load(overnight=dict({"mode": "live"}, **bad))


# ----------------------------------------------------------------------------------------------------------------------
# 1.11.4: the overnight engine has its own slot. On 4 Nov at 23:45 UTC it was refused with "1 position(s) + 1 pending
# order(s) already working (max 2)": the swing engine's slot count was applied to it. Offline, no MT5.
# ----------------------------------------------------------------------------------------------------------------------
NOW = datetime(2026, 11, 4, 23, 45, tzinfo=UTC)
OVN = 40004


def _pos(magic, ticket=1, buy=True):
    return types.SimpleNamespace(magic=magic, ticket=ticket, type=0 if buy else 1, sl=4000.0, price_open=4050.0)


class _SlotMarket:
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
        self.m = _SlotMarket()
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


class _RiskMarket(_SlotMarket):
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
