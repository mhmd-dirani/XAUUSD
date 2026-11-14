"""Lab mode (rule experiments on the demo account at the minimum lot): tags, plans, safety, sizing, the news
straddle, duplicate handling, broker/journal reconciliation, kept limits and the minimum-lot TP1.
Offline: no MT5, no network, no Claude, no Telegram."""

from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent / "src", HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import user_plans as UP  # noqa: E402
from autotrader import alerts, bridge as BR, executor as EX, lab as LB, sessions as SS, settings  # noqa: E402
from autotrader import straddle as STR  # noqa: E402
from autotrader.state import State  # noqa: E402
from test_overnight import _JournalState, _Market as _OvernightMarket, _Pos  # noqa: E402

UTC = timezone.utc


# ----------------------------------------------------------------------------------------------------------------------
# 1.10 regression tests: session-sweep confirmation, in-zone market entry, lab mode, owner-plan stop floor.
# ----------------------------------------------------------------------------------------------------------------------
T0 = datetime(2026, 10, 26, 14, 0, tzinfo=UTC)


def bar(t, o, h, l, c):
    return {"time": t.isoformat(), "open": o, "high": h, "low": l, "close": c,
            "closes": (t + timedelta(minutes=15)).isoformat()}


# The 26 Oct 14:30 candle that "confirmed" two SELLs: a 14-point bullish bar closing 0.10 below the level.
BULL_SWEEP = bar(T0 + timedelta(minutes=30), 4007.89, 4023.08, 4007.75, 4022.24)
U1 = {"id": "U1", "direction": "SELL", "entry_low": 4009.0, "entry_high": 4016.0, "invalidation": 4024.5,
      "targets": [3977.0, 3960.0], "confidence": 40, "risk": "MEDIUM", "title": "owner plan"}
I1 = {"id": "I1", "direction": "SELL", "entry_low": 4017.5, "entry_high": 4021.0, "invalidation": 4026.5,
      "targets": [3996.01, 3977.98], "confidence": 33, "risk": "MEDIUM", "title": "astra"}
CFG = {"min_rr": 2.0, "idea_a_grade_confidence": 60, "signal_entry_tolerance": 1.0, "confirm_retest_hours": 4,
       "max_order_lifetime_hours": 48, "confirm_entry_tolerance": 3.0, "idea_order_hours": 12,
       "max_entry_deviation_cap": 3.0, "sl_atr_min": 0.5, "sl_atr_max": 5.0, "trade_sessions": ["LONDON", "NEW_YORK"],
       "tiers": {"C": {"min_confidence": 30}}}


class SweepCandle(unittest.TestCase):
    def test_session_sweep_needs_a_rejection_wick(self):
        level = [{"name": "LONDON_OR_HIGH", "side": "HIGH", "price": 4022.34, "formed_at": T0.isoformat(),
                  "source": "OPENING_RANGE"}]
        bars = [bar(T0 + timedelta(minutes=15), 4005, 4010, 4004, 4007.89), BULL_SWEEP]
        evs = SS.events(bars, level, T0 + timedelta(minutes=46), last_n=1)
        self.assertEqual(evs, [], "a 14-point bullish bar with a 0.84 wick is not a sweep-and-reclaim")
        reject = bar(T0 + timedelta(minutes=30), 4018.0, 4023.08, 4012.0, 4014.0)      # wick 9.08 >= body 4
        evs = SS.events([bars[0], reject], level, T0 + timedelta(minutes=46), last_n=1)
        self.assertEqual([e["event"] for e in evs], ["SWEEP_RECLAIM"])
        small_bull = bar(T0 + timedelta(minutes=30), 4015.0, 4023.0, 4014.0, 4018.0)   # wick 5 >= body 3: still a sweep
        evs = SS.events([bars[0], small_bull], level, T0 + timedelta(minutes=46), last_n=1)
        self.assertEqual([e["event"] for e in evs], ["SWEEP_RECLAIM"])

    def test_sweep_bar_must_close_back_at_the_idea_zone(self):
        self.assertFalse(BR.sweep_bar_ok(U1, BULL_SWEEP))
        self.assertFalse(BR.sweep_bar_ok(I1, BULL_SWEEP))
        ok = bar(T0, 4019.0, 4023.0, 4014.0, 4015.5)                                     # wick 4 >= body 3.5
        self.assertTrue(BR.sweep_bar_ok(U1, ok))
        self.assertTrue(BR.sweep_bar_ok(I1, ok))
        buy = {"direction": "BUY", "entry_low": 4000.0, "entry_high": 4005.0}
        self.assertTrue(BR.sweep_bar_ok(buy, bar(T0, 4002.0, 4006.0, 3995.0, 4003.0)))      # lower wick 7 >= body 1
        self.assertFalse(BR.sweep_bar_ok(buy, bar(T0, 4008.0, 4009.0, 3995.0, 3999.0)))     # closed below the zone


class InZoneEntry(unittest.TestCase):
    def test_confirmed_market_entry_beyond_the_zone_is_refused(self):
        q = {"bid": 4021.74, "ask": 4022.04}                       # 26 Oct 14:45: 5.7 above U1's zone
        order, why = BR.confirmed_order(U1, q, CFG, T0, BULL_SWEEP, "SESSION_SWEEP")
        self.assertIsNone(order)
        self.assertIn("wrong side", why)

    def test_confirmed_with_price_short_of_the_zone_waits_at_the_edge(self):
        far = dict(U1, targets=[3900.0])                           # 2R available at market, but price is not at the zone
        order, why = BR.confirmed_order(far, {"bid": 4000.0, "ask": 4000.3}, CFG, T0, BULL_SWEEP, "CHOCH")
        self.assertIsNone(why)
        self.assertEqual((order["order_type"], order["entry_price"]), ("SELL_LIMIT", 4009.0))
        self.assertEqual(order["cancel_if_price_below"], 3900.0)

    def test_confirmed_market_entry_inside_the_zone_stays_market(self):
        q = {"bid": 4014.5, "ask": 4014.8}
        order, why = BR.confirmed_order(U1, q, CFG, T0, BULL_SWEEP, "REJECTION")
        self.assertIsNone(why)
        self.assertEqual(order["order_type"], "MARKET")

    def test_below_2r_at_market_keeps_the_old_retest_limit(self):
        q = {"bid": 4000.0, "ask": 4000.3}            # 0.94R at market: the 1.6 rule, a limit at the 2R price
        order, why = BR.confirmed_order(U1, q, CFG, T0, BULL_SWEEP, "REJECTION")
        self.assertIsNone(why)
        self.assertEqual(order["order_type"], "SELL_LIMIT")
        self.assertAlmostEqual(order["entry_price"], 4008.77, places=2)   # the exact 2R price (1.6 rule)


class LabTags(unittest.TestCase):
    def test_monday_morning_sell_gets_the_expected_tags(self):
        evidence = {"frames": {"4h": {"trend": "BEARISH"}, "1h": {"trend": "BULLISH"}}, "index": {}}
        dec = {"mode": "off", "trade_class": "NONE", "expansion": {"against": False}, "would_block": False}
        tags = LB.tags_for(U1, c=CFG, state={}, snap={"session": "OFF"}, evidence=evidence, atr15=7.9,
                           quote={"bid": 3993.3, "ask": 3993.6}, strong=False, dec=dec, confirmed=None,
                           live={"window": "first 30 min", "news": None, "book_full": False})
        for t in ("session_off", "open_close_buffer", "mtf_opposed", "counter_no_zone", "class_off",
                  "no_confirmation", "not_anchored"):
            self.assertIn(t, tags)
        for t in ("news_lock", "rr_below_min", "risk_high", "zone_passed", "low_confidence", "risk_engine"):
            self.assertNotIn(t, tags)

    def test_rr_and_stop_tags(self):
        narrow = dict(I1, invalidation=4022.0)          # 1 point from the zone edge: below 0.5 x ATR, R:R huge
        dec = {"mode": "live", "expansion": {"against": True}, "would_block": True}
        tags = LB.tags_for(narrow, c=CFG, state={}, snap={"session": "LONDON"},
                           evidence={"frames": {"4h": {"trend": "BEARISH"}, "1h": {"trend": "BEARISH"}}, "index": {}},
                           atr15=7.9, quote={"bid": 4019.0, "ask": 4019.3}, strong=False, dec=dec, confirmed="REJECTION",
                           live={"risk_engine": "stop 1.0 outside 0.5-5.0 x ATR15"})
        self.assertIn("stop_below_atr_min", tags)
        self.assertIn("expansion_against", tags)
        self.assertIn("expansion_unvalidated", tags)
        self.assertIn("risk_engine", tags)
        self.assertNotIn("session_off", tags)
        self.assertNotIn("no_confirmation", tags)
        self.assertNotIn("mtf_opposed", tags)
        low_rr = dict(I1, targets=[4012.0])
        self.assertIn("rr_below_min", LB.tags_for(low_rr, c=CFG, state={}, snap={"session": "LONDON"},
                                                   evidence={"frames": {}, "index": {}}, atr15=7.9,
                                                   quote={"bid": 4019.0, "ask": 4019.3}, strong=False,
                                                   dec={"mode": "live", "expansion": {}}, confirmed="REJECTION", live={}))


SI = SimpleNamespace(trade_stops_level=0, point=0.01, volume_min=0.01, volume_step=0.01, volume_max=100.0,
                     trade_tick_value=1.0, trade_tick_size=0.01, digits=2)


class LabPlan(unittest.TestCase):
    def test_unconfirmed_idea_waits_at_the_zone_edge_with_tp1_only(self):
        order, why = LB.plan(U1, {"bid": 3993.3, "ask": 3993.6}, CFG, T0, None, SI)
        self.assertIsNone(why)
        self.assertEqual((order["order_type"], order["entry_price"]), ("SELL_LIMIT", 4009.0))
        self.assertIsNone(order["take_profit_2"])
        self.assertEqual(order["tp1_close_percent"], 100)
        self.assertFalse(order["move_sl_to_breakeven_at_tp1"])
        self.assertTrue(order["setup_id"].startswith("LAB-SELL-"))

    def test_price_beyond_the_zone_enters_at_market(self):
        order, why = LB.plan(U1, {"bid": 4021.74, "ask": 4022.04}, CFG, T0, None, SI)
        self.assertIsNone(why)
        self.assertEqual(order["order_type"], "MARKET")
        self.assertEqual(order["entry_price"], 4021.74)

    def test_unexecutable_ideas_are_refused(self):
        self.assertEqual(LB.plan(U1, {"bid": 4025.0, "ask": 4025.3}, CFG, T0, None, SI)[0], None)
        self.assertEqual(LB.plan(U1, {"bid": 3970.0, "ask": 3970.3}, CFG, T0, None, SI)[0], None)


class LabSafety(unittest.TestCase):
    def test_lab_mode_requires_demo_and_its_own_magic(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:   # Windows: sqlite handle (see test_reliability)
            root = Path(d)
            (root / "config").mkdir()
            v6 = root / "config" / "gold_v5_config.json"
            v6.write_text("{}", encoding="utf-8")
            import json
            base = {"v6_config": str(v6), "state_directory": str(root / "rt"), "lab_mode": True}
            p = root / "config" / "t.json"
            p.write_text(json.dumps(dict(base, require_demo=False)), encoding="utf-8")
            with self.assertRaises(ValueError):
                settings.load(p)
            p.write_text(json.dumps(dict(base, magic_lab=40001)), encoding="utf-8")
            with self.assertRaises(ValueError):
                settings.load(p)
            p.write_text(json.dumps(base), encoding="utf-8")
            c = settings.load(p)
            self.assertTrue(c["lab_mode"])
            self.assertEqual(c["magic_lab"], 40003)

    def test_hard_block_refuses_a_real_account(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:   # Windows: sqlite handle (see test_reliability)
            market = SimpleNamespace(is_demo=lambda: False, symbol="XAUUSD")
            app = SimpleNamespace(market=market, state=State(Path(d)), validator=None, executor=None)
            lab = LB.Lab({"lab_mode": True, "require_demo": True, "state_path": Path(d), "magic_lab": 40003}, app)
            self.assertIn("DEMO", lab.hard_block(T0))
            lab2 = LB.Lab({"lab_mode": True, "require_demo": False, "state_path": Path(d), "magic_lab": 40003}, app)
            self.assertIn("DEMO", lab2.hard_block(T0))

    def test_lab_orders_are_counted_apart_from_live_orders(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:   # Windows: sqlite handle (see test_reliability)
            st = State(Path(d))
            order = {"setup_id": "LAB-SELL-4013", "direction": "SELL", "order_type": "SELL_LIMIT", "entry_price": 4009.0,
                     "stop_loss": 4024.5, "take_profit_1": 3977.0, "take_profit_2": None, "tp1_close_percent": 100,
                     "move_sl_to_breakeven_at_tp1": False, "cancel_if_price_above": None, "cancel_if_price_below": 3977.0,
                     "order_expiry_utc": "2026-10-27T02:00:00Z", "rr": 2.07, "evidence_ids": [], "rationale": "lab"}
            st.record_order(1, order, "B_TRADE", 0.01, 0.16, "PENDING")
            st.set_meta(1, {"lab": 1, "violations": ["session_off", "no_confirmation"], "session": "OFF"})
            st.record_order(2, dict(order, setup_id="IDEA-SELL-4013"), "B_TRADE", 0.03, 46.0, "PENDING")
            self.assertEqual(st.orders_created_since("2026-01-01T00:00:00Z"), 1)
            self.assertEqual(st.lab_orders_since("2026-01-01T00:00:00Z"), 1)
            self.assertEqual(len(st.setup_orders_since("LAB-SELL-4013", "2026-01-01T00:00:00Z", lab=True)), 1)
            self.assertEqual(len(st.setup_orders_since("LAB-SELL-4013", "2026-01-01T00:00:00Z")), 0)
            rec = st.all_orders()[0]
            self.assertEqual(LB.violations_of(rec), ["session_off", "no_confirmation"])


class LabReport(unittest.TestCase):
    def test_per_rule_lines(self):
        closed = [{"lab": 1, "r": -1.0, "violations": ["session_off"]},
                  {"lab": 1, "r": 2.0, "violations": []},
                  {"lab": 1, "r": -1.0, "violations": ["session_off", "no_confirmation"]},
                  {"lab": 0, "r": 5.0, "violations": []}]
        lines = "\n".join(LB.per_rule(closed))
        self.assertIn("3 closed lab trades", lines)
        self.assertIn("session_off", lines)
        self.assertIn("broke   2 avg -1.00R | kept   1 avg +2.00R", lines)
        self.assertEqual(LB.per_rule([{"lab": 0, "r": 1.0, "violations": []}]), [])


class OwnerPlanStop(unittest.TestCase):
    def test_one_point_stop_is_rejected(self):
        plan = UP.parse("/idea SELL 4017-4020 sl 4021 tp 4007 3997 3989\nCheck")
        self.assertIsNotNone(plan)
        why = UP.check(plan, price=4014.76, atr1h=8.0, min_rr=2.0)
        self.assertIsNotNone(why)
        self.assertIn("narrower", why)
        good = UP.parse("/idea SELL 4009-4016 sl 4024.5 tp 3977 3960 3940\nbreaker")
        self.assertIsNone(UP.check(good, price=3993.0, atr1h=8.0, min_rr=2.0))


class _FakeMarket:
    symbol = "XAUUSD"

    def __init__(self, demo=True, price=3993.3):
        self.demo, self.price = demo, price
        self.sessions = SimpleNamespace(is_open=lambda now: True)

    def is_demo(self):
        return self.demo

    def account(self):
        return SimpleNamespace(equity=10000.0, balance=10000.0)

    def terminal(self):
        return SimpleNamespace(connected=True, trade_allowed=True)

    def quote(self):
        return {"bid": self.price, "ask": self.price + 0.3, "spread": 0.3, "age": 1.0}

    def trade_call(self, name, **kw):
        return []

    def history_deals(self, a, b):
        return []

    def const(self, name):
        return {"DEAL_ENTRY_OUT": 1, "DEAL_ENTRY_INOUT": 2}.get(name, 0)

    def server_to_utc(self, t):
        return t

    def account(self):
        return SimpleNamespace(balance=10000.0, equity=10000.0)

    def symbol_info(self):
        return SI

    def trading_window(self, now):
        return None

    def news_block(self, now):
        return None


class _FakeExecutor:
    dry_run = False

    def __init__(self):
        self.sent = []

    def send(self, order, lots, risk_money, grade, magic=None):
        self.sent.append((order, lots, risk_money, grade, magic))
        return 100 + len(self.sent)


class LabEndToEnd(unittest.TestCase):
    def _app(self, tmp, demo=True, price=3993.3):
        market = _FakeMarket(demo, price)
        state = State(Path(tmp))
        validator = SimpleNamespace(broker_orders=lambda: ([], []), max_active=lambda: 2,
                                    realised_pnl_since=lambda since: 0.0)
        evidence = {"frames": {"4h": {"trend": "BEARISH"}, "1h": {"trend": "BULLISH"}}, "index": {}}
        state_rg = {"macro_trend": "BEARISH", "trend_4h": "BEARISH", "intermediate_trend": "BULLISH",
                    "local_trend": "BEARISH", "phase": "PULLBACK", "phase_direction": "BEARISH",
                    "expansion": {"active": False}, "_frames": {"15min": {"events": []}}}
        snap = {"session": "OFF", "levels": [], "events": []}
        app = SimpleNamespace(market=market, state=state, validator=validator, executor=_FakeExecutor(),
                              market_context=lambda: (evidence, 7.9), market_state=lambda: (state_rg, snap),
                              recent_bars=lambda: [], memory_zones=lambda: [],
                              modes=lambda: {"TREND": "live", "COUNTER_TREND": "live", "REVERSAL_TRANSITION": "live",
                                             "SESSION_SWEEP": "live", "BREAKOUT_CONTINUATION": "off"},
                              meta=lambda d, dec, how, idea=None, playbook=None: {"session": "OFF", "confirmation": how,
                                                                                    "regime": {}, "playbook": "NONE"},
                              session_confirmation=lambda idea, since: None)
        c = settings.load(None)
        c.update(lab_mode=True, state_path=Path(tmp), block_on_mtf_opposition=True, min_rr=2.0,
                 v6={"quote_max_age_seconds": 125, "broker_max_spread": 0.5})
        return app, c

    def test_sunday_idea_is_lab_traded_once_with_its_tags(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:   # Windows: sqlite handle (see test_reliability)
            app, c = self._app(d)
            lab = LB.Lab(c, app)
            a = {"id": "A1", "created_at": "2026-10-25T22:13:31Z"}
            lab.sweep(a, [U1], T0)
            self.assertEqual(len(app.executor.sent), 1)
            order, lots, risk, grade, magic = app.executor.sent[0]
            self.assertEqual(magic, 40003)
            self.assertEqual(lots, 0.01)
            self.assertEqual(order["order_type"], "SELL_LIMIT")
            self.assertEqual(order["strategy"], "LAB")
            tags = order["meta"]["violations"]
            for t in ("session_off", "mtf_opposed", "counter_no_zone", "class_off", "no_confirmation"):
                self.assertIn(t, tags)
            self.assertEqual(order["meta"]["lab"], 1)
            lab.sweep(a, [U1], T0)                                  # same tick again: handled once
            self.assertEqual(len(app.executor.sent), 1)
            lab.sweep({"id": "A2", "created_at": "2026-10-26T00:00:00Z"}, [U1], T0)
            # a new analysis with the same zone: the live journal row is not there (fake executor), so the
            # in-memory key is new and a second order would go; the DB rule needs executor.send -> record_order
            self.assertEqual(len(app.executor.sent), 2)

    def test_live_trade_is_left_alone_and_risk_engine_rejects_are_taken(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:   # Windows: sqlite handle (see test_reliability)
            app, c = self._app(d, price=4019.0)                  # inside I1's zone
            lab = LB.Lab(c, app)
            a = {"id": "A1", "created_at": "2026-10-25T22:13:31Z"}
            lab.sweep(a, [U1, I1], T0, {"U1": "IDEA-SELL-4012: sent #59", "I1": "IDEA-SELL-4019: spread too big"})
            self.assertEqual([o[0]["setup_id"] for o in app.executor.sent], ["LAB-SELL-4019"])
            self.assertIn("risk_engine", app.executor.sent[0][0]["meta"]["violations"])

    def test_real_account_never_gets_a_lab_order(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:   # Windows: sqlite handle (see test_reliability)
            app, c = self._app(d, demo=False)
            lab = LB.Lab(c, app)
            lab.sweep({"id": "A1", "created_at": "2026-10-25T22:13:31Z"}, [U1], T0)
            self.assertEqual(app.executor.sent, [])


class LabDedupe(unittest.TestCase):
    """1.10.1: the first night took the same owner plan twice (22:44 and 02:26 UTC) and both stopped together."""

    def test_one_sample_per_setup_per_trading_day_and_never_two_working(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            st = State(Path(d))
            order = {"setup_id": "LAB-SELL-4012", "direction": "SELL", "order_type": "MARKET", "entry_price": 4014.1,
                     "stop_loss": 4024.5, "take_profit_1": 3977.0, "take_profit_2": None, "tp1_close_percent": 100,
                     "move_sl_to_breakeven_at_tp1": False, "cancel_if_price_above": None, "cancel_if_price_below": None,
                     "order_expiry_utc": "2026-10-27T02:00:00Z", "rr": 3.57, "evidence_ids": [], "rationale": "lab"}
            st.record_order(1, order, "B_TRADE", 0.01, 10.39, "OPEN")
            st.set_meta(1, {"lab": 1, "violations": []})
            st.update_order(1, created_utc="2026-10-26T22:44:10Z")
            self.assertEqual(len(st.lab_active_for_setup("LAB-SELL-4012")), 1)
            # 02:26 UTC on 27 Oct is the same New York trading day (it started 21:00 UTC on 26 Oct)
            t = datetime(2026, 10, 27, 2, 26, tzinfo=UTC)
            self.assertEqual(LB._trading_day0(t), "2026-10-26T21:00:00Z")
            self.assertEqual(len(st.setup_orders_since("LAB-SELL-4012", LB._trading_day0(t), lab=True)), 1)
            # closed, and a new trading day: a new sample is allowed
            st.update_order(1, status="CLOSED")
            self.assertEqual(st.lab_active_for_setup("LAB-SELL-4012"), [])
            t2 = datetime(2026, 10, 27, 22, 30, tzinfo=UTC)
            self.assertEqual(LB._trading_day0(t2), "2026-10-27T21:00:00Z")
            self.assertEqual(st.setup_orders_since("LAB-SELL-4012", LB._trading_day0(t2), lab=True), [])


class NewsCandleTests(unittest.TestCase):
    """1.10.2: 27 Oct the 12:30 Durable Goods candle confirmed a SELL that was placed at 13:00:01."""

    def _market(self, events):
        from autotrader.market import Market
        m = Market.__new__(Market)
        m.v6 = {"news_before_minutes": 30, "news_after_minutes": 30}
        m.calendar = {"events": events, "fetched_at": "2026-10-27T13:00:00Z"}
        return m

    def test_last_lock_end(self):
        ev = [{"time": "2026-10-27T12:30:00+00:00", "impact": "HIGH", "title": "Durable Goods Orders m/m"},
              {"time": "2026-10-27T13:00:00+00:00", "impact": "MEDIUM", "title": "S&P/CS HPI"},
              {"time": "2026-10-27T14:00:00+00:00", "impact": "HIGH", "title": "New Home Sales"},
              {"time": "2026-10-28T18:00:00+00:00", "impact": "MEDIUM", "title": "FOMC Statement"}]
        m = self._market(ev)
        self.assertEqual(m.last_lock_end(datetime(2026, 10, 27, 13, 0, 1, tzinfo=UTC)),
                         datetime(2026, 10, 27, 13, 0, tzinfo=UTC))
        self.assertIsNone(m.last_lock_end(datetime(2026, 10, 27, 12, 45, tzinfo=UTC)))     # still inside: gate blocks
        self.assertEqual(m.last_lock_end(datetime(2026, 10, 27, 15, 0, tzinfo=UTC)),
                         datetime(2026, 10, 27, 14, 30, tzinfo=UTC))
        self.assertIsNone(m.last_lock_end(datetime(2026, 10, 27, 22, 0, tzinfo=UTC)))      # older than 6 h
        self.assertEqual(m.last_lock_end(datetime(2026, 10, 28, 18, 31, tzinfo=UTC)),
                         datetime(2026, 10, 28, 18, 30, tzinfo=UTC))                       # FOMC by name
        self.assertIsNone(self._market([]).last_lock_end(datetime(2026, 10, 27, 13, 1, tzinfo=UTC)))

    def test_candle_inside_the_lock_does_not_confirm(self):
        sell = {"id": "I1", "direction": "SELL", "entry_low": 4026.0, "entry_high": 4031.0, "invalidation": 4037.4,
                "targets": [4016.31]}
        spike = bar(datetime(2026, 10, 27, 12, 30, tzinfo=UTC), 4022.0, 4031.5, 4021.5, 4024.0)   # 12:30-12:45
        born = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
        status, b, how = BR.confirmation(sell, [spike], born)
        lock_end = datetime(2026, 10, 27, 13, 0, tzinfo=UTC)
        if status == "confirmed":                                  # the full window sees it ...
            again = BR.confirmation(sell, [spike], lock_end - timedelta(seconds=1))
            self.assertEqual(again[0], "waiting")                  # ... the post-lock window does not
        after = bar(datetime(2026, 10, 27, 13, 0, tzinfo=UTC), 4027.0, 4031.0, 4024.0, 4025.0)
        self.assertNotEqual(BR.confirmation(sell, [spike, after], lock_end - timedelta(seconds=1))[0], "invalidated")

FOMC_CAL = {"fetched_at": "2026-10-28T17:55:00Z", "events": [
    {"time": "2026-10-28T14:30:00+00:00", "impact": "HIGH", "title": "EIA Crude Oil Stocks Change"},
    {"time": "2026-10-28T18:00:00+00:00", "impact": "HIGH", "title": "Fed Interest Rate Decision"},
    {"time": "2026-10-28T18:30:00+00:00", "impact": "HIGH", "title": "FOMC Press Conference"}]}


def _stcfg(**over):
    c = settings.load(None)
    c.update(lab_mode=True, news_straddle=True)
    c.update(over)
    return c


class StraddleTests(unittest.TestCase):
    def test_only_listed_high_events_inside_the_lead_time(self):
        c = _stcfg()
        t = datetime(2026, 10, 28, 18, 0, tzinfo=UTC)
        self.assertEqual(STR.next_event(FOMC_CAL, t - timedelta(minutes=1, seconds=30), c)["title"],
                         "Fed Interest Rate Decision")
        self.assertIsNone(STR.next_event(FOMC_CAL, t - timedelta(minutes=5), c))          # too early
        self.assertIsNone(STR.next_event(FOMC_CAL, t + timedelta(seconds=1), c))          # already out
        self.assertIsNone(STR.next_event(FOMC_CAL, datetime(2026, 10, 28, 14, 29, tzinfo=UTC), c))   # EIA not listed
        self.assertIsNone(STR.next_event(FOMC_CAL, datetime(2026, 10, 28, 18, 29, tzinfo=UTC), c))   # press conf.
        self.assertIsNone(STR.next_event(None, t, c))

    def test_order_geometry(self):
        c = _stcfg()
        t = datetime(2026, 10, 28, 18, 0, tzinfo=UTC)
        orders = STR.build_orders({"bid": 4060.0, "ask": 4060.2, "spread": 0.2}, 8.0, c, t, SI)
        self.assertEqual([o["order_type"] for o in orders], ["BUY_STOP", "BUY_STOP", "SELL_STOP", "SELL_STOP"])
        b1, b2, s1, s2 = orders
        self.assertEqual((b1["entry_price"], b1["stop_loss"], b1["take_profit_1"], b1["take_profit_2"]),
                         (4064.2, 4056.2, 4072.2, 4088.2))
        self.assertEqual(b2["entry_price"], 4072.2)
        self.assertEqual((s1["entry_price"], s1["stop_loss"], s1["take_profit_1"]), (4056.0, 4064.0, 4048.0))
        self.assertEqual(s2["entry_price"], 4048.0)
        self.assertTrue(all(o["tp1_close_percent"] == 50 and o["move_sl_to_breakeven_at_tp1"] for o in orders))
        self.assertEqual(b1["order_expiry_utc"][:16], "2026-10-28T18:15")
        self.assertEqual(b1["setup_id"], "NEWS-BUY1-10281800")
        tight = STR.build_orders({"bid": 4060.0, "ask": 4060.2, "spread": 0.2}, 2.0, c, t, SI)
        self.assertEqual(abs(tight[0]["entry_price"] - tight[0]["stop_loss"]), 3.0)           # straddle_min_stop
        self.assertEqual(len(STR.build_orders({"bid": 4060.0, "ask": 4060.2, "spread": 0.2}, 8.0,
                                              _stcfg(straddle_add_atr=0), t, SI)), 2)
        with self.assertRaises(ValueError):
            STR.build_orders({"bid": 4060.0, "ask": 4060.2, "spread": 0.2}, 0, c, t, SI)

    def test_oco_cancels_the_other_side_only(self):
        P = lambda cm, tk=1: SimpleNamespace(comment=cm, ticket=tk)
        g = "10281800"
        pend = [P("LAB:NEWS-BUY2-10281800", 11), P("LAB:NEWS-SELL1-10281800", 12), P("LAB:NEWS-SELL2-10281800", 13),
                P("LAB:LAB-SELL-4077", 14), P("LAB:NEWS-SELL1-10291230", 15)]
        self.assertEqual(STR.oco_actions([P("LAB:NEWS-BUY1-10281800")], pend, g), [12, 13])
        self.assertEqual(STR.oco_actions([], pend, g), [])
        both = [P("LAB:NEWS-BUY1-10281800"), P("LAB:NEWS-SELL1-10281800")]
        self.assertEqual(STR.oco_actions(both, pend, g), [11, 12, 13])                      # whipsaw: stop adding

    def test_settings(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            import json
            root = Path(d)
            v6 = root / "v6.json"
            v6.write_text("{}", encoding="utf-8")
            p = root / "t.json"
            p.write_text(json.dumps({"v6_config": str(v6), "state_directory": str(root / "rt"), "news_straddle": True}),
                         encoding="utf-8")
            with self.assertRaises(ValueError):
                settings.load(p)                                                            # needs lab_mode
            p.write_text(json.dumps({"v6_config": str(v6), "state_directory": str(root / "rt"), "lab_mode": True,
                                     "news_straddle": True, "straddle_tp1_r": 3, "straddle_tp2_r": 2}), encoding="utf-8")
            with self.assertRaises(ValueError):
                settings.load(p)
        self.assertFalse(settings.load(None)["news_straddle"])


class StraddleEndToEnd(unittest.TestCase):
    def test_places_once_before_fomc_and_time_exits(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            m = _FakeMarket(price=4060.0)
            m.calendar = FOMC_CAL
            m.symbol = "XAUUSD"
            positions = []
            m.trade_call = lambda name, **kw: positions if name == "positions_get" else []
            m.const = lambda n: {"POSITION_TYPE_BUY": 0, "TRADE_ACTION_DEAL": 1, "ORDER_TYPE_SELL": 1,
                                 "ORDER_TYPE_BUY": 0, "DEAL_ENTRY_OUT": 1, "DEAL_ENTRY_INOUT": 2}.get(n, 0)
            st = State(Path(d))
            x = _FakeExecutor()
            x._filling = lambda: 0
            x.closed = []
            x._send = lambda req: x.closed.append(req)
            x.cancel = lambda t, why: True
            c = _stcfg(state_path=Path(d), v6={"quote_max_age_seconds": 125, "broker_max_spread": 0.5})
            app = SimpleNamespace(market=m, state=st, executor=x, market_context=lambda: ({}, 8.0))
            app.lab = LB.Lab(c, SimpleNamespace(market=m, state=st, validator=None, executor=x))
            s = STR.Straddle(c, app)
            t = datetime(2026, 10, 28, 18, 0, tzinfo=UTC)
            s.tick(t - timedelta(minutes=1, seconds=50))
            self.assertEqual(len(x.sent), 4)
            self.assertTrue(all(o[4] == 40003 for o in x.sent))
            self.assertIn("news_straddle", x.sent[0][0]["meta"]["violations"])
            s.tick(t - timedelta(minutes=1))                                                # same event: once
            self.assertEqual(len(x.sent), 4)
            positions.append(SimpleNamespace(comment="LAB:NEWS-BUY1-10281800", ticket=77, magic=40003, type=0,
                                             volume=0.01))
            s.tick(t + timedelta(minutes=31))
            self.assertEqual([r["position"] for r in x.closed], [77])
            self.assertEqual(s.active, {})

    def test_real_account_gets_no_straddle(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            m = _FakeMarket(demo=False, price=4060.0)
            m.calendar = FOMC_CAL
            st, x = State(Path(d)), _FakeExecutor()
            c = _stcfg(state_path=Path(d), v6={"quote_max_age_seconds": 125, "broker_max_spread": 0.5})
            app = SimpleNamespace(market=m, state=st, executor=x, market_context=lambda: ({}, 8.0))
            app.lab = LB.Lab(c, SimpleNamespace(market=m, state=st, validator=None, executor=x))
            STR.Straddle(c, app).tick(datetime(2026, 10, 28, 17, 58, 30, tzinfo=UTC))
            self.assertEqual(x.sent, [])


class NotificationTests(unittest.TestCase):
    """1.10.4: 28 Oct IDEA-BUY-4031 (targets 4057.44, 4064.74) was placed, then the Telegram text crashed."""

    def test_two_targets_do_not_crash(self):
        from autotrader import alerts
        order = {"direction": "BUY", "order_type": "BUY_LIMIT", "entry_price": 4033.4, "stop_loss": 4021.5,
                 "take_profit_1": 4057.44, "take_profit_2": 4064.74, "meta": {"tier": "B", "targets": [4057.44, 4064.74]}}
        txt = alerts.signal_text("PENDING ORDER", order, 0.04)
        self.assertIn("TP2  4064.74", txt)
        self.assertNotIn("TP3", txt)
        self.assertIn("TP3  3940.0", alerts.signal_text("X", dict(order, meta={"targets": [1, 2, 3940.0]}), 0.01))

    def test_a_failing_text_never_raises(self):
        from autotrader import alerts
        alerts.safe_trade(lambda: 1 / 0)                       # logs a warning, returns


class LabSizing(unittest.TestCase):
    """1.10.5: lab orders sized like the live track when lab_risk_pct > 0, with the lab's own open-risk cap."""

    def _lab(self, **over):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            c = _stcfg(state_path=Path(d), v6={"quote_max_age_seconds": 125, "broker_max_spread": 0.5})
            c.update(over)
            m = _FakeMarket(price=4060.0)
            return LB.Lab(c, SimpleNamespace(market=m, state=State(Path(d)), validator=None, executor=_FakeExecutor())), m

    def test_min_lot_by_default(self):
        lab, _ = self._lab()
        self.assertEqual(lab.size(SI, 10.0), (0.01, 10.0, None))      # 0.01 x 10 x $100

    def test_pct_sizes_from_equity_and_stop(self):
        lab, _ = self._lab(lab_risk_pct=0.5)
        lots, risk, hold = lab.size(SI, 10.0)             # 10000 x 0.5% = 50 budget / (10 x 100) = 0.05 lots
        self.assertEqual((lots, risk, hold), (0.05, 50.0, None))
        lots, risk, hold = lab.size(SI, 200.0)            # budget 50 / 20000 -> below the minimum lot -> min lot
        self.assertEqual((lots, risk), (0.01, 200.0))

    def test_open_risk_cap_holds_the_next_order(self):
        lab, m = self._lab(lab_risk_pct=0.5, lab_max_total_risk_pct=1.0)
        pos = SimpleNamespace(magic=40003, sl=4050.0, price_open=4060.0, volume=0.06)   # 10 x 0.06 x 100 = 60 at risk
        m.trade_call = lambda name, **kw: [pos] if name == "positions_get" else []
        lots, risk, hold = lab.size(SI, 10.0)
        self.assertEqual(risk, 50.0)
        self.assertIn("would pass the cap 100.00", hold)
        m.trade_call = lambda name, **kw: []
        self.assertIsNone(lab.size(SI, 10.0)[2])

    def test_settings_refuse_above_tier_a(self):
        import json
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            root = Path(d)
            (root / "v6.json").write_text("{}", encoding="utf-8")
            p = root / "t.json"
            p.write_text(json.dumps({"v6_config": str(root / "v6.json"), "state_directory": str(root / "rt"),
                                     "lab_mode": True, "lab_risk_pct": 5.0}), encoding="utf-8")
            with self.assertRaises(ValueError):
                settings.load(p)
            p.write_text(json.dumps({"v6_config": str(root / "v6.json"), "state_directory": str(root / "rt"),
                                     "lab_mode": True, "lab_risk_pct": 0.5, "lab_max_total_risk_pct": 0.2}), encoding="utf-8")
            with self.assertRaises(ValueError):
                settings.load(p)


# ----------------------------------------------------------------------------------------------------------------------
# 1.10.6 regression tests: lab geometry dedupe and broker/journal reconciliation. Offline, no MT5, no Telegram.
# ----------------------------------------------------------------------------------------------------------------------
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


class _ReconcileState:
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
        self.x = EX.Executor(c, market=None, state=_ReconcileState([
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


# ----------------------------------------------------------------------------------------------------------------------
# 1.11.1 tests: a lab limit whose TP1 traded before the fill is kept and put back to Astra (lab_keep_after_tp_touch).
# Offline, no MT5, no Telegram.
# ----------------------------------------------------------------------------------------------------------------------
SI_STOPS10 = types.SimpleNamespace(trade_stops_level=10, point=0.01, digits=2, volume_min=0.01, volume_step=0.01, filling_mode=7)
C_LAB = {"confirm_entry_tolerance": 3.0, "confirm_retest_hours": 4, "idea_order_hours": 12, "max_order_lifetime_hours": 48,
         "signal_entry_tolerance": 1.0, "max_entry_deviation_cap": 3.0}


def idea(direction="SELL", lo=3949.66, hi=3953.0, stop=3965.5, tp1=3926.2, iid="I1"):
    return {"id": iid, "direction": direction, "entry_low": lo, "entry_high": hi, "invalidation": stop, "targets": [tp1]}


class PlanTest(unittest.TestCase):
    NOW = datetime(2026, 11, 4, 0, 30, tzinfo=UTC)

    def test_tp1_already_reached_is_refused_by_default(self):
        o, why = LB.plan(idea(), {"bid": 3925.0, "ask": 3925.3}, C_LAB, self.NOW, None, SI_STOPS10)
        self.assertIsNone(o)
        self.assertEqual(why, "TP1 already reached")

    def test_with_the_switch_it_waits_at_the_zone_edge(self):
        c = dict(C_LAB, lab_keep_after_tp_touch=True)
        o, why = LB.plan(idea(), {"bid": 3925.0, "ask": 3925.3}, c, self.NOW, None, SI_STOPS10)
        self.assertIsNone(why)
        self.assertEqual((o["order_type"], o["entry_price"]), ("SELL_LIMIT", 3949.66))   # the zone edge nearest price
        self.assertIsNone(o["cancel_if_price_below"])                 # 1.11.3: a plain limit, no touch / re-map
        self.assertTrue(o["_tp_passed_at_entry"])
        o, _ = LB.plan(idea("BUY", 3932.0, 3935.0, 3925.9, 3949.66), {"bid": 3955.0, "ask": 3955.3}, c, self.NOW,
                       "REJECTION", SI_STOPS10)
        self.assertEqual((o["order_type"], o["entry_price"]), ("BUY_LIMIT", 3935.0))     # never MARKET above TP1

    def test_4_nov_sell_the_rally_idea(self):
        """18:18 map: I2-SELL-CT SELL 4075.19-4082.68 stop 4087 TP1 4057.44 with price ~4050, below its TP1."""
        c = dict(C_LAB, lab_keep_after_tp_touch=True)
        o, why = LB.plan(idea("SELL", 4075.19, 4082.68, 4087.0, 4057.44), {"bid": 4050.0, "ask": 4050.2}, c, self.NOW,
                         None, SI_STOPS10)
        self.assertIsNone(why)
        self.assertEqual((o["order_type"], o["entry_price"], o["stop_loss"], o["take_profit_1"]),
                         ("SELL_LIMIT", 4075.19, 4087.0, 4057.44))
        self.assertIsNone(o["cancel_if_price_above"])
        self.assertIsNone(o["cancel_if_price_below"])

    def test_normal_ideas_still_get_their_cancel_level(self):
        c = dict(C_LAB, lab_keep_after_tp_touch=True)
        o, _ = LB.plan(idea(), {"bid": 3940.0, "ask": 3940.3}, c, self.NOW, None, SI_STOPS10)
        self.assertEqual(o["cancel_if_price_below"], 3926.2)
        self.assertFalse(o["_tp_passed_at_entry"])

    def test_normal_ideas_unchanged_by_the_switch(self):
        c = dict(C_LAB, lab_keep_after_tp_touch=True)
        a, _ = LB.plan(idea(), {"bid": 3940.0, "ask": 3940.3}, C_LAB, self.NOW, None, SI_STOPS10)
        b, _ = LB.plan(idea(), {"bid": 3940.0, "ask": 3940.3}, c, self.NOW, None, SI_STOPS10)
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
        return SI_STOPS10

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


# ----------------------------------------------------------------------------------------------------------------------
# 1.11.6: a minimum-lot position takes the whole profit at TP1; the lab treats one zone bought twice as one sample; the
# lab tags a TP1 that reaches past the opposite idea's entry. Offline, no MT5, no Telegram.
# ----------------------------------------------------------------------------------------------------------------------
class _Mkt(_OvernightMarket):
    CONST = dict(_OvernightMarket.CONST, TRADE_ACTION_SLTP=6)


SI_MINLOT = types.SimpleNamespace(point=0.01, digits=2, volume_min=0.01, volume_step=0.01, filling_mode=7)
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
        x._tp1(rec, pos, SI_MINLOT, buy, Q)
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


class LabSameZoneTest(unittest.TestCase):
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
