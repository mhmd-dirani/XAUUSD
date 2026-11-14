#!/usr/bin/env python3
"""V5.1 acceptance tests: independent-review findings + MetaTrader 5 providers.
Uses a fake MetaTrader5 module; never touches a terminal, network or Telegram.
Run: python -m unittest -v test_broker_mt5.py
"""
import copy
import inspect
import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import gold_monitor as M  # noqa: E402
import gold_evidence as EV  # noqa: E402
import test_signal_gate as TM  # noqa: E402  (helpers only; its TestCases are not collected here)

UTC = timezone.utc


class FakeMT5:
    """Minimal stand-in for the MetaTrader5 package. Server time = UTC + offset."""
    TIMEFRAME_M15, TIMEFRAME_H1, TIMEFRAME_H4, TIMEFRAME_D1, TIMEFRAME_W1 = 15, 16385, 16388, 16408, 32769
    COPY_TICKS_INFO = 1

    def __init__(self, offset_s=10800, live=True):
        self.offset, self.live, self.calls, self.ticks, self.rates = offset_s, live, [], [], {}

    def initialize(self, *a, **k):
        self.calls.append("initialize"); return True

    def last_error(self):
        return (1, "Success")

    def account_info(self):
        self.calls.append("account_info")
        return SimpleNamespace(server="GIVTrade-Server", company="GIV Capital Limited")

    def symbol_select(self, symbol, enable):
        self.calls.append("symbol_select"); return True

    def add_tick(self, bid, ask, utc_ts=None):
        msc = int(((utc_ts or time.time()) + self.offset) * 1000)
        if self.ticks and msc <= self.ticks[-1]["time_msc"]:
            msc = self.ticks[-1]["time_msc"] + 1
        self.ticks.append({"bid": bid, "ask": ask, "time_msc": msc})

    def symbol_info_tick(self, symbol):
        self.calls.append("symbol_info_tick")
        if self.live or not self.ticks:
            last = self.ticks[-1] if self.ticks else {"bid": 4000.0, "ask": 4000.3}
            self.add_tick(last["bid"], last["ask"])
        t = self.ticks[-1]
        return SimpleNamespace(bid=t["bid"], ask=t["ask"], time_msc=t["time_msc"], time=t["time_msc"] // 1000)

    def copy_ticks_from(self, symbol, since, count, flags):
        self.calls.append("copy_ticks_from")
        return [t for t in self.ticks if t["time_msc"] >= since.timestamp() * 1000][:count]

    def copy_rates_from_pos(self, symbol, tf, start, count):
        self.calls.append("copy_rates_from_pos")
        return self.rates[tf][-count:]


def mt5_config(**over):
    c = M.load_config()
    c["provider_options"] = dict(c["provider_options"], **over.pop("options", {}))
    c.update(over)
    return c


class MT5Base(unittest.TestCase):
    def setUp(self):
        M.MT5Session.reset()
        self.addCleanup(M.MT5Session.reset)
        self.fake = FakeMT5()
        p = patch.dict(sys.modules, {"MetaTrader5": self.fake})
        p.start(); self.addCleanup(p.stop)


class MT5ProviderTests(MT5Base):
    def test_defaults_are_mt5_and_conservative(self):
        c = M.load_config()
        self.assertEqual({c["providers"][k] for k in ("candles", "quotes", "broker_quotes")}, {"mt5"})
        self.assertEqual((c["broker_max_spread"], c["broker_slippage_allowance"], c["broker_max_spread_stop_fraction"]),
                         (0.5, 0.25, 0.05))
        self.assertTrue(c["require_broker_quotes"] and c["block_on_mtf_opposition"])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "c.json")
            path.write_text(json.dumps({"providers": {"broker_quotes": "none"}}))
            with self.assertRaisesRegex(ValueError, "require_broker_quotes"):
                M.load_config(path)
            path.write_text(json.dumps({"providers": {"broker_quotes": "none"}, "require_broker_quotes": False}))
            M.load_config(path)
            path.write_text(json.dumps({"provider_options": {"mt5_server_utc_offset_minutes": "+3"}}))
            with self.assertRaises(ValueError):
                M.load_config(path)

    def test_auto_offset_needs_a_live_tick(self):
        c = mt5_config()
        self.assertEqual(M.MT5Session.offset_seconds(c, "XAUUSD"), 10800)
        M.MT5Session.reset()
        self.fake.live = False                       # last tick is old: cannot know "now"
        self.fake.add_tick(4000, 4000.3, time.time() - 7200)
        with patch.object(M.MT5Session, "PROBE_SECONDS", 0.3):
            with self.assertRaisesRegex(RuntimeError, "no live ticks"):
                M.MT5Session.offset_seconds(c, "XAUUSD")
        self.assertEqual(M.MT5Session.offset_seconds(mt5_config(options={"mt5_server_utc_offset_minutes": 120}), "X"), 7200)

    def test_read_only_allow_list(self):
        with self.assertRaises(PermissionError):
            M.MT5Session.call("order_send", {})
        for cls in (M.MT5Session, M.MT5Candles, M.MT5QuoteFeed, M.MT5BrokerQuotes):
            code = inspect.getsource(cls)
            for word in ("order_send(", "order_check(", "positions_get(", "position_close("):
                self.assertNotIn(word, code, (cls.__name__, word))

    def test_mt5_broker_quote_in_utc(self):
        q = M.MT5BrokerQuotes(mt5_config(options={"mt5_server_utc_offset_minutes": 180})).quote()
        self.assertLess(abs((M.stamp(q["time"]) - M.utcnow()).total_seconds()), 2)
        self.assertEqual(q["source"], "mt5")

    @patch.object(M, "utcnow", return_value=datetime(2026, 10, 8, 20, 30, tzinfo=UTC))
    def test_mt5_candles_server_time_to_utc_and_daily_labels(self, _fixed_clock):
        # Deterministic open-session test; real clock during the daily maintenance break
        # caused an unrelated false failure (there is no 15M candle inside the break).
        c = mt5_config(options={"mt5_server_utc_offset_minutes": 180})
        now = M.utcnow()
        end = int(now.timestamp() // 900 * 900)                   # forming bar start (UTC)
        m15 = [{"time": end - 900 * i + 10800, "open": 4000., "high": 4001., "low": 3999., "close": 4000.5}
               for i in range(700, -1, -1)]
        self.fake.rates[FakeMT5.TIMEFRAME_M15] = m15
        bars = M.MT5Candles(c).candles("15min")
        self.assertEqual(M.stamp(bars[-1]["time"]).timestamp(), end - 900)     # forming bar excluded
        self.assertTrue(all(M.Sessions(c).is_open(M.stamp(b["time"])) or
                            M.Sessions(c).is_open(M.stamp(b["time"]) + timedelta(minutes=14)) for b in bars))
        day, daily = now.date(), []
        while len(daily) < 300:
            day -= timedelta(days=1)
            if day.weekday() < 5:
                server_midnight = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())
                daily.append({"time": server_midnight, "open": 4000., "high": 4010., "low": 3990., "close": 4005.})
        self.fake.rates[FakeMT5.TIMEFRAME_D1] = daily[::-1]
        dbars = M.MT5Candles(c).candles("1day")
        self.assertEqual(dbars[-1]["time"][:10], daily[0] and datetime.fromtimestamp(daily[0]["time"], UTC).date().isoformat())

    def test_mt5_feed_captures_every_tick_and_uses_bid_ask_sides(self):
        c = mt5_config(options={"mt5_server_utc_offset_minutes": 180}, mt5_poll_seconds=0.05)
        self.fake.live = False
        self.fake.add_tick(4000.0, 4000.3)
        feed = M.MT5QuoteFeed(c); feed.start()
        self.addCleanup(feed.close)
        deadline = time.time() + 2
        while feed.latest is None and time.time() < deadline:
            time.sleep(0.02)
        feed.take_extremes()
        for bid, ask in ((4000.1, 4000.4), (3996.0, 3996.3), (4000.2, 4000.5)):   # dip between polls
            self.fake.add_tick(bid, ask)
        deadline = time.time() + 2
        while (feed.latest or {}).get("bid") != 4000.2 and time.time() < deadline:
            time.sleep(0.02)
        w = feed.take_extremes()
        self.assertEqual((w["low"], w["high"]), (3996.0, 4000.5))
        self.assertEqual((feed.latest["bid"], feed.latest["ask"], feed.latest["price"]), (4000.2, 4000.5, 4000.35))


class ReviewFindingTests(unittest.TestCase):
    """Acceptance tests 1-4 and 6 from V5_INDEPENDENT_REVIEW.md."""
    def setUp(self):
        self.c = M.load_config()
        self.now = datetime(2026, 10, 8, 17, 0, tzinfo=UTC)

    def q(self, bid, ask, age=1):
        return {"bid": bid, "ask": ask, "time": (self.now - timedelta(seconds=age)).isoformat(), "source": "t"}

    def check(self, spec, bid, ask, mid=None):
        return M.broker_check(spec, self.q(bid, ask), (bid + ask) / 2 if mid is None else mid, self.now, self.c, True)

    def test_1_exit_side_already_through_invalidation(self):
        buy = {"direction": "BUY", "entry_low": 100.0, "entry_high": 101.0, "stop_loss": 99.5, "invalidation": 99.9, "targets": [107.0]}
        sell = {"direction": "SELL", "entry_low": 100.0, "entry_high": 102.0, "stop_loss": 103.0, "invalidation": 102.1, "targets": [88.0]}
        ok, _, why = self.check(buy, 99.85, 100.20)
        self.assertFalse(ok); self.assertIn("bid", why)
        ok, _, why = self.check(sell, 101.8, 102.15)
        self.assertFalse(ok); self.assertIn("ask", why)

    def test_3_spread_caps(self):
        self.c = dict(self.c, broker_slippage_allowance=0)  # separately test spread limit
        spec = {"direction": "BUY", "entry_low": 4000., "entry_high": 4002., "stop_loss": 3990., "invalidation": 3991., "targets": [4040.]}
        self.assertTrue(self.check(spec, 4000.5, 4001.0)[0])                 # $0.50, wide stop
        self.assertFalse(self.check(spec, 4000.49, 4001.0)[0])               # $0.51
        self.assertFalse(self.check(spec, 4000.25, 4001.0)[0])               # $0.75
        tight = dict(spec, stop_loss=3998., invalidation=3998.5)              # stop 3.25 -> 5% = 0.16
        ok, _, why = self.check(tight, 4000.5, 4001.0)
        self.assertFalse(ok); self.assertIn("stop distance", why)

    def test_4_adverse_slippage(self):
        spec = {"direction": "BUY", "entry_low": 4000., "entry_high": 4001.1, "stop_loss": 3990., "invalidation": 3991., "targets": [4040.]}
        ok, _, why = self.check(spec, 4000.8, 4001.0)                          # ask in range, ask+0.25 is not
        self.assertFalse(ok); self.assertIn("leaves the entry range", why)
        rr = dict(spec, entry_high=4002., targets=[4023.])                     # 2.0R at ask, 1.93R after slippage
        ok, _, why = self.check(rr, 4000.8, 4001.0)
        self.assertFalse(ok); self.assertIn("adverse fill", why)
        self.assertTrue(M.broker_check(rr, self.q(4000.8, 4001.0), 4000.9, self.now,
                                       dict(self.c, broker_slippage_allowance=0), True)[0])
        sell = {"direction": "SELL", "entry_low": 3999.9, "entry_high": 4002., "stop_loss": 4012., "invalidation": 4011., "targets": [3960.]}
        ok, _, why = self.check(sell, 4000.0, 4000.2)                          # bid-0.25 = 3999.75 < entry_low
        self.assertFalse(ok); self.assertIn("leaves the entry range", why)

    def test_2_filled_fvg_and_superseded_fib_end_the_setup(self):
        spec = {"playbook": "TREND_CONTINUATION", "verified_evidence": {"required": ["f"]}}
        fib = {"playbook": "FIB_ZONE_CONFLUENCE", "verified_evidence": {"required": ["fib", "ob"]}}
        idx = lambda st_f, st_fib: {"index": {
            "f": {"id": "f", "kind": "FVG", "status": st_f},
            "fib": {"id": "fib", "kind": "FIB_OTE", "status": st_fib},
            "ob": {"id": "ob", "kind": "ORDER_BLOCK_CANDIDATE", "status": "TESTED"}}}
        self.assertEqual(EV.recheck(spec, idx("PARTIALLY_FILLED", "ACTIVE"))[0], "OK")
        self.assertEqual(EV.recheck(spec, idx("FILLED", "ACTIVE"))[0], "INVALID")
        self.assertEqual(EV.recheck(fib, idx("ACTIVE", "ACTIVE"))[0], "OK")
        self.assertEqual(EV.recheck(fib, idx("ACTIVE", "SUPERSEDED"))[0], "INVALID")

    def test_mtf_opposition_vetoes_b_trade(self):
        review = {"decision": "APPROVE", "risk": "LOW", "criteria": {k: True for k in M.CRITERIA}}
        self.assertIsNone(M.trade_grade(review, "LOW", "LOW", False))
        self.assertEqual(M.trade_grade(review, "LOW", "LOW", False, mtf_veto=False), "B_TRADE")
        self.assertEqual(M.trade_grade(review, "LOW", "LOW", True), "A_TRADE")


class AppPathTests(unittest.TestCase):
    """End-to-end gate/sender paths, reusing the V5 fixture helpers."""
    def setUp(self):
        self.t = TM.Tests("test_wick_invalidation")
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)

    def test_filled_fvg_blocks_queued_signal_and_ends_setup(self):
        t = self.t
        with TM.app_test_directory(t) as folder:
            a, now = t.queued_signal_app(folder)
            a.evidence = TM.fixture_evidence(fvg="FILLED")           # closed candle filled the FVG after approval
            post = t.run_sender(a, now)
            post.assert_not_called()
            a.apply_evidence(now)
            self.assertEqual(t.w.setups[t.sid]["state"], "INVALIDATED")

    def test_broker_quote_update_through_invalidation_ends_setup(self):
        t = self.t
        with TM.app_test_directory(t) as folder:
            a = t.app(folder); t.arm()
            from concurrent.futures import Future
            f = Future(); f.set_result({"symbol": "XAUUSD", "bid": 98.4, "ask": 98.7, "time": M.iso(t.now), "source": "mt5"})
            a.data_jobs["broker"] = (f, "test")
            with patch.object(a, "data_enabled", return_value=True):
                a.consume_data(t.now)
            self.assertEqual(t.w.setups[t.sid]["state"], "INVALIDATED")     # invalidation 98.5, bid 98.4

    def test_mid_above_but_bid_through_invalidation_in_main_loop(self):
        t = self.t
        t.arm()
        feed = M.QuoteFeed(t.c)
        feed.record_quote(98.4, 98.9, t.now, now=t.now)           # mid 98.65 > 98.5 > bid 98.4
        w = feed.take_extremes()
        events = t.w.quote(feed.latest["price"], t.now + timedelta(minutes=46), w["low"], w["high"])
        self.assertEqual([e[0] for e in events], ["INVALIDATED"])

    def test_broker_required_blocks_without_quote(self):
        t = self.t
        with TM.app_test_directory(t) as folder:
            a = t.app(folder, dict(t.c, require_broker_quotes=True)); t.arm(); t.push(60)
            now = t.now + timedelta(minutes=60, seconds=15)
            t.approve_now(a, now)
            a.broker_quote = None
            with patch.object(a, "data_enabled", return_value=True):
                a.update_gate(now)
            self.assertEqual(a.delivery_gate["actionable"], set())

    def test_6_revised_candle_rechecked_and_evidence_rebuilt(self):
        t = self.t
        t.arm()
        revised = t.bar(45, low=98.0)                              # same timestamp as an already-seen candle
        events = t.w.bars("15min", [revised], t.now + timedelta(minutes=50))
        self.assertEqual([e[0] for e in events], ["INVALIDATED"])
        t.setUp(); t.arm()
        harmless = t.bar(45, high=103.0)                           # revision without a breach
        # V5.1.2: a correction that flips no contact/condition result the setup used does not
        # cancel it (still checked for invalidation); one that does flip a result cancels it.
        revised_events = t.w.bars("15min", [harmless], t.now + timedelta(minutes=50))
        self.assertEqual(revised_events, [])
        self.assertEqual(t.w.setups[t.sid]["state"], "ARMED")
        self.assertEqual(len(t.w.setups[t.sid]["touches"]), 3)          # not double-counted
        no_touch = t.bar(45, low=101.2, high=103.0, open=101.5, close=102.)   # contact disappears
        self.assertEqual([e[0] for e in t.w.bars("15min", [no_touch], t.now + timedelta(minutes=51))], ["INVALIDATED"])
        with TM.app_test_directory(t) as folder:
            a = t.app(folder)
            with patch.object(EV, "build_evidence", return_value=copy.deepcopy(TM.FIX_EV)) as build:
                a.refresh_evidence(t.now); a.refresh_evidence(t.now)
                self.assertEqual(build.call_count, 1)
                a.history["15min"][-30]["close"] = 101.0           # old candle corrected in place
                a.refresh_evidence(t.now)
                self.assertEqual(build.call_count, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
