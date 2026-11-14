"""Entries are rechecked at the moment of entry (news, one position per zone, Astra's newer map), the lab's
entry-recheck mode and the pre-news guard.
Offline: no MT5, no network, no Claude, no Telegram."""

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent / "src", HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from autotrader import alerts, lab as LB, ltf as LT, newsguard as NG  # noqa: E402
from test_ltf import _App, C, IDEA, leg_bars, PLAIN  # noqa: E402

UTC = timezone.utc


# ----------------------------------------------------------------------------------------------------------------------
# 1.14: entries are rechecked at the moment of entry (news, one position per zone, Astra's newer map), the lab stops
# placing blind limits (lab_entry recheck), and the pre-news guard protects what is already at the broker.
# The 10 Nov case: BUY limit 4076.36 placed 10:02 filled 10:27, its 5M twin bought the same zone at 12:05, and the CPI
# candle stopped both at 13:26-13:30. Offline, no MT5, no Telegram.
# ----------------------------------------------------------------------------------------------------------------------
class _Bridge:
    def __init__(self, a=None):
        self.a = a

    def analysis(self, now):
        return (self.a, 5) if self.a else (None, None)


class RecheckTest(unittest.TestCase):
    def setUp(self):
        self.msgs = []
        self._t, self._e = alerts.trade, alerts.error
        alerts.trade = lambda msg, short=None: self.msgs.append(msg)
        alerts.error = lambda msg: self.msgs.append("ERR " + msg)

    def tearDown(self):
        alerts.trade, alerts.error = self._t, self._e

    def feed(self, app, created_offset=20, cfg=None):
        e = LT.LTF(dict(C, ltf_lab=dict({"mode": "lab"}, **(cfg or {}))), app)
        full = app._bars
        e.watch({"id": "A1"}, IDEA, PLAIN, [], datetime.fromtimestamp(full[created_offset][0], UTC))
        outs = []
        for n in range(created_offset + 1, len(full) + 1):
            app._bars = full[:n]
            e.bars, e.bars_key = [], None
            outs += e.tick(datetime.fromtimestamp(full[n - 1][0] + 300, UTC))
        return e, outs

    def test_baseline_enters_and_is_tagged_rechecked(self):
        app = _App(leg_bars())
        e, outs = self.feed(app)
        self.assertEqual(outs, ["LTF-BUY-4042: sent #777"])
        self.assertIn("rechecked", app.sent[0][0]["meta"]["violations"])

    def test_news_lock_holds_the_entry_and_keeps_the_watch(self):
        app = _App(leg_bars())
        app.news = "CPI m/m at 2026-11-10T13:30:00+00:00"
        e, outs = self.feed(app)
        self.assertEqual((outs, app.sent), ([], []))
        self.assertIn("LTF-BUY-4042", e.watches)                         # waits, not dropped

    def test_major_release_due_within_the_guard_holds_the_entry(self):
        app = _App(leg_bars())
        app.next_event = ("CPI m/m", datetime(2026, 11, 10, 13, 30, tzinfo=UTC))
        e, outs = self.feed(app)
        self.assertEqual(app.sent, [])
        self.assertIn("LTF-BUY-4042", e.watches)
        e2, _ = self.feed(_App(leg_bars()), cfg={"news_guard_minutes": 0})   # guard off: the lock alone decides
        self.assertTrue(e2 is not None)

    def test_calendar_error_is_not_no_news(self):
        app = _App(leg_bars())
        app.market.news_block = lambda now: (_ for _ in ()).throw(RuntimeError("calendar stale"))
        e, outs = self.feed(app)
        self.assertEqual(app.sent, [])
        self.assertIn("LTF-BUY-4042", e.watches)

    def test_the_10_nov_case_one_position_per_zone(self):
        app = _App(leg_bars())
        app.state.active = [{"lab": 1, "ticket": 60563317, "setup_id": "LAB-BUY-4075", "direction": "BUY",
                             "entry": 4042.5, "sl": 4030.0, "tp1": 4075.0, "status": "OPEN"}]   # a buy already in the zone
        e, outs = self.feed(app)
        self.assertEqual(app.sent, [])
        self.assertEqual(e.watches, {})                                  # dropped: the zone is taken
        app2 = _App(leg_bars())                                          # a SELL far above is not "the same zone"
        app2.state.active = [dict(app.state.active[0], direction="SELL", entry=4070.0, sl=4080.0, tp1=4060.0)]
        _, outs2 = self.feed(app2)
        self.assertEqual(outs2, ["LTF-BUY-4042: sent #777"])

    def test_newer_map_proposing_the_opposite_drops_the_watch(self):
        app = _App(leg_bars())
        later = datetime.fromtimestamp(app._bars[-1][0], UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        app.bridge = _Bridge({"id": "A2", "created_at": later,
                              "ideas": [{"id": "I9", "direction": "SELL", "entry_low": 4044.0, "entry_high": 4048.0}]})
        e, outs = self.feed(app)
        self.assertEqual(app.sent, [])
        self.assertEqual(e.watches, {})

    def test_newer_map_without_an_opposite_idea_does_not_block(self):
        app = _App(leg_bars())
        later = datetime.fromtimestamp(app._bars[-1][0], UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        app.bridge = _Bridge({"id": "A2", "created_at": later,
                              "ideas": [{"id": "I9", "direction": "SELL", "entry_low": 4100.0, "entry_high": 4104.0}]})
        _, outs = self.feed(app)
        self.assertEqual(outs, ["LTF-BUY-4042: sent #777"])


class LabEntryModeTest(unittest.TestCase):
    """lab_entry recheck: Lab.consider watches the zone instead of sending a resting limit; old limits are retired."""

    class _X:
        def __init__(self):
            self.cancelled, self.sent = [], []

        def cancel(self, ticket, why, status="CANCELLED"):
            self.cancelled.append((ticket, why))
            return True

        def send(self, *a, **k):
            self.sent.append(a)
            return 1

    def test_retire_plain_limits_once_and_only_pending_lab_limits(self):
        x = self._X()
        rows = [{"lab": 1, "ticket": 1, "setup_id": "LAB-SELL-4102", "status": "PENDING"},
                {"lab": 1, "ticket": 2, "setup_id": "LAB-BUY-4031", "status": "OPEN"},
                {"lab": 1, "ticket": 3, "setup_id": "W4H-SELL-4120", "status": "PENDING"},
                {"lab": 0, "ticket": 4, "setup_id": "IDEA-BUY-4100", "status": "PENDING"}]
        state = types.SimpleNamespace(active_orders=lambda include_lab=True: rows)
        lab = LB.Lab({"lab_mode": True, "lab_entry": "recheck"},
                     types.SimpleNamespace(market=None, state=state, validator=None, executor=x))
        self.assertEqual(lab.retire_plain_limits(), [1])
        self.assertEqual(lab.retire_plain_limits(), [])                   # once per start
        lab2 = LB.Lab({"lab_mode": True, "lab_entry": "plain"},
                      types.SimpleNamespace(market=None, state=state, validator=None, executor=self._X()))
        self.assertEqual(lab2.retire_plain_limits(), [])


class LabRecheckEndToEnd(unittest.TestCase):
    """Lab.sweep with lab_entry recheck: the idea becomes an LTF watch, no order goes to the broker (uses the 1.10
    end-to-end harness; skipped where test_lab is not present)."""

    def test_sweep_watches_instead_of_sending(self):
        try:
            import tempfile
            import test_lab as T110
        except ImportError:
            self.skipTest("test_lab harness not available")
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            app, c = T110.LabEndToEnd._app(T110.LabEndToEnd(), d)
            c.update(lab_entry="recheck", ltf_lab=dict(c["ltf_lab"], mode="lab"), lab_keep_after_tp_touch=True)
            watched = []
            app.ltf = types.SimpleNamespace(live=True, watch=lambda a, idea, order, tags, now:
                                            watched.append((idea["id"], order["setup_id"], list(tags))))
            lab = LB.Lab(c, app)
            lab.sweep({"id": "A1", "created_at": "2026-10-25T22:13:31Z"}, [T110.U1], T110.T0)
            self.assertEqual(app.executor.sent, [])                         # no blind limit at the broker
            self.assertEqual(len(watched), 1)
            self.assertIn("mtf_opposed", watched[0][2])                     # the rule tags travel with the watch
            lab.sweep({"id": "A1", "created_at": "2026-10-25T22:13:31Z"}, [T110.U1], T110.T0)
            self.assertEqual(len(watched), 1)                               # handled once


class _Pos:
    def __init__(self, ticket, price_open, sl, tp=4100.0):
        self.ticket, self.price_open, self.sl, self.tp = ticket, price_open, sl, tp


class NewsGuardTest(unittest.TestCase):
    NOW = datetime(2026, 11, 10, 13, 5, tzinfo=UTC)
    CPI = ("CPI m/m", datetime(2026, 11, 10, 13, 30, tzinfo=UTC))

    def setUp(self):
        self.msgs = []
        self._t = alerts.trade
        alerts.trade = lambda msg, short=None: self.msgs.append(msg)

    def tearDown(self):
        alerts.trade = self._t

    def app(self, rows, positions, bid=4090.0, event=CPI):
        x = types.SimpleNamespace(cancelled=[], moved=[])
        x.cancel = lambda t, why, status="CANCELLED": (x.cancelled.append(t), True)[1]
        x.move_stop = lambda pos, sl, why: (x.moved.append((pos.ticket, sl)), True)[1]
        market = types.SimpleNamespace(symbol="XAUUSD", next_major_event=lambda now, m: event,
                                       trade_call=lambda name, **kw: positions,
                                       quote=lambda: {"bid": bid, "ask": bid + 0.3})
        return types.SimpleNamespace(market=market, executor=x,
                                     state=types.SimpleNamespace(active_orders=lambda include_lab=True: rows)), x

    ROWS = [
        {"lab": 1, "ticket": 10, "setup_id": "LAB-SELL-4102", "status": "PENDING", "direction": "SELL"},
        {"lab": 1, "ticket": 11, "setup_id": "LTF-BUY-4075", "status": "OPEN", "direction": "BUY", "entry": 4076.25,
         "sl": 4068.78, "position_ticket": 11},                         # +1.85 R at 4090: to breakeven
        {"lab": 1, "ticket": 12, "setup_id": "LAB-BUY-4075", "status": "OPEN", "direction": "BUY", "entry": 4076.36,
         "sl": 4053.90, "position_ticket": 12},                         # +0.61 R: keeps its stop
        {"lab": 0, "ticket": 13, "setup_id": "IDEA-BUY-4088", "status": "PENDING", "direction": "BUY"},
    ]
    POS = [_Pos(11, 4076.25, 4068.78), _Pos(12, 4076.36, 4053.90)]

    def test_lab_mode_cancels_lab_pending_and_breakevens_winners_once(self):
        app, x = self.app(self.ROWS, self.POS)
        g = NG.NewsGuard({"pre_news_guard": {"mode": "lab"}, "v6": {"news_before_minutes": 30}}, app)
        out = g.tick(self.NOW)
        self.assertEqual(x.cancelled, [10])                              # the live pending order is out of scope
        self.assertEqual(x.moved, [(11, 4076.25)])
        self.assertEqual(out, [("cancelled", "LAB-SELL-4102"), ("breakeven", "LTF-BUY-4075")])
        self.assertEqual(g.tick(self.NOW + timedelta(minutes=1)), [])     # once per release
        self.assertTrue(any("Pre-news guard" in m for m in self.msgs))

    def test_all_mode_includes_live_orders(self):
        app, x = self.app(self.ROWS, self.POS)
        NG.NewsGuard({"pre_news_guard": {"mode": "all"}, "v6": {"news_before_minutes": 30}}, app).tick(self.NOW)
        self.assertEqual(x.cancelled, [10, 13])

    def test_off_or_no_release_does_nothing(self):
        app, x = self.app(self.ROWS, self.POS)
        self.assertEqual(NG.NewsGuard({"pre_news_guard": {"mode": "off"}}, app).tick(self.NOW), [])
        app, x = self.app(self.ROWS, self.POS, event=None)
        self.assertEqual(NG.NewsGuard({"pre_news_guard": {"mode": "lab"}, "v6": {}}, app).tick(self.NOW), [])
        self.assertEqual((x.cancelled, x.moved), ([], []))

    def test_a_stop_already_at_breakeven_is_left_alone(self):
        app, x = self.app(self.ROWS, [_Pos(11, 4076.25, 4076.25), _Pos(12, 4076.36, 4053.90)])
        NG.NewsGuard({"pre_news_guard": {"mode": "lab"}, "v6": {"news_before_minutes": 30}}, app).tick(self.NOW)
        self.assertEqual(x.moved, [])


class SettingsTest(unittest.TestCase):
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

    def test_defaults_and_the_new_config(self):
        c = self._load()
        self.assertEqual((c["lab_entry"], c["pre_news_guard"]["mode"], c["ltf_lab"]["news_guard_minutes"]),
                         ("plain", "off", 60))
        c = self._load(lab_mode=True, ltf_lab={"mode": "lab"}, lab_entry="recheck", pre_news_guard={"mode": "lab"})
        self.assertEqual((c["lab_entry"], c["pre_news_guard"]["mode"]), ("recheck", "lab"))

    def test_bad_values_fail_at_start(self):
        with self.assertRaises(ValueError):
            self._load(lab_mode=True, lab_entry="recheck")                # needs the LTF engine
        for kw in ({"lab_entry": "blind"}, {"pre_news_guard": {"mode": "live"}}, {"pre_news_guard": {"minutes": 0}},
                   {"pre_news_guard": {"be_at_r": -1}}, {"pre_news_guard": {"typo": 1}},
                   {"ltf_lab": {"news_guard_minutes": 500}}, {"ltf_lab": {"one_per_zone": "yes"}}):
            with self.assertRaises(ValueError, msg=str(kw)):
                self._load(**kw)


if __name__ == "__main__":
    unittest.main()
