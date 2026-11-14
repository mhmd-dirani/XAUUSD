#!/usr/bin/env python3
"""V5.1.2 acceptance tests for the V5.1.1 review findings. Offline; fake MetaTrader5 only."""
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
import test_candle_corrections as CandleCorrection  # noqa: E402  (helpers only)
from test_broker_mt5 import FakeMT5  # noqa: E402

UTC = timezone.utc


def daily_rates(with_sunday=True, with_saturday=False, weeks=70):
    """Server-midnight D1 bars, Mon-Fri plus optional short weekend bars."""
    rates, day = [], datetime(2026, 10, 8).date()
    while len(rates) < weeks * 7:
        day -= timedelta(days=1)
        wd = day.weekday()
        if wd < 5 or (wd == 6 and with_sunday) or (wd == 5 and with_saturday):
            ts = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())
            o = 4000.0 + len(rates) % 37
            rates.append({"time": ts, "open": o, "high": o + (1 if wd >= 5 else 6),
                          "low": o - (1 if wd >= 5 else 6), "close": o + 0.5})
    return rates[::-1]


class WeekendDailyTests(unittest.TestCase):
    def test_merge_rules(self):
        v = lambda d, o, h, l, c: {"datetime": d, "open": o, "high": h, "low": l, "close": c}
        out = M.merge_weekend_daily([
            v("2026-10-02", 10, 12, 9, 11),          # Friday
            v("2026-10-03", 11, 13, 10.5, 12),       # Saturday tail -> folded into Friday
            v("2026-10-04", 12, 12.5, 11, 12.2),     # Sunday head -> merged into Monday
            v("2026-10-05", 12.2, 15, 11.5, 14),     # Monday
            v("2026-10-11", 14, 14.2, 13.8, 14.1),   # lone Sunday (Monday still forming)
        ])
        self.assertEqual(out[0], v("2026-10-02", 10, 13, 9, 12))
        self.assertEqual(out[1], v("2026-10-05", 12, 15, 11, 14))
        self.assertEqual(out[2]["datetime"], "2026-10-12")
        self.assertEqual(len(out), 3)

    def test_mt5_sunday_plus_monday_no_longer_breaks_daily_fetch(self):
        fake = FakeMT5(); fake.rates[FakeMT5.TIMEFRAME_D1] = daily_rates(with_sunday=True, with_saturday=True)
        c = M.load_config(); c["provider_options"]["mt5_server_utc_offset_minutes"] = 120
        M.MT5Session.reset(); self.addCleanup(M.MT5Session.reset)
        with patch.dict(sys.modules, {"MetaTrader5": fake}):
            bars = M.MT5Candles(c).candles("1day")          # V5.1.1: CandleConflictError here
        self.assertGreaterEqual(len(bars), 210)
        self.assertTrue(all(M.stamp(b["time"]).weekday() < 5 for b in bars))
        self.assertEqual(len({b["time"] for b in bars}), len(bars))


class StopPolicyTests(unittest.TestCase):
    def setUp(self):
        self.c = M.load_config()
        self.now = datetime(2026, 10, 8, 17, tzinfo=UTC)

    def check(self, stop_dist, c=None, spread=0.15):
        spec = {"direction": "BUY", "entry_low": 3999, "entry_high": 4001, "stop_loss": 4000 - stop_dist,
                "invalidation": 4000 - stop_dist + 0.5, "targets": [4000 + 4 * stop_dist]}
        q = {"bid": 4000 - spread, "ask": 4000.0, "time": self.now.isoformat()}
        return M.broker_check(spec, q, 4000 - spread / 2, self.now, c or self.c, True)

    def test_no_hidden_ten_dollar_minimum_stop(self):
        for d in (6, 8, 9.5):                                  # all blocked in V5.1.1
            self.assertTrue(self.check(d)[0], d)
        ok, _, why = self.check(5.0)                           # (0.15+0.25)/5.25 > 7.5%: combined cap
        self.assertFalse(ok); self.assertIn("Combined", why)

    def test_explicit_minimum_stop_and_optional_slippage_cap(self):
        ok, _, why = self.check(6, dict(self.c, minimum_stop_distance=8))
        self.assertFalse(ok); self.assertIn("minimum_stop_distance", why)
        self.assertTrue(self.check(9, dict(self.c, minimum_stop_distance=8))[0])
        ok, _, why = self.check(8, dict(self.c, broker_max_slippage_stop_fraction=0.025))
        self.assertFalse(ok); self.assertIn("slippage", why)

    def test_config_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "c.json")
            for bad in ('{"minimum_stop_distance": -1}', '{"broker_max_slippage_stop_fraction": 0}',
                        '{"broker_max_slippage_stop_fraction": true}'):
                path.write_text(bad)
                with self.assertRaises(ValueError, msg=bad):
                    M.load_config(path)
            path.write_text('{"broker_max_slippage_stop_fraction": 0.025, "minimum_stop_distance": 5}')
            M.load_config(path)


class LaggingHistoryMT5(FakeMT5):
    """Tick history hides the newest `lag` ticks for the first `lag_calls` reads."""
    def __init__(self, lag_calls, **kw):
        super().__init__(**kw)
        self.lag_calls = lag_calls

    def copy_ticks_from(self, symbol, since, count, flags):
        got = super().copy_ticks_from(symbol, since, count, flags)
        if self.lag_calls > 0:
            self.lag_calls -= 1
            return got[:-1] if got else got
        return got


class TickGapTests(unittest.TestCase):
    def run_feed(self, fake, seconds=1.2):
        c = M.load_config()
        c["provider_options"]["mt5_server_utc_offset_minutes"] = 180
        c["mt5_poll_seconds"] = 0.05
        M.MT5Session.reset(); self.addCleanup(M.MT5Session.reset)
        p = patch.dict(sys.modules, {"MetaTrader5": fake}); p.start(); self.addCleanup(p.stop)
        g = patch.object(M.MT5Session, "GAP_RETRY_SECONDS", 0.5); g.start(); self.addCleanup(g.stop)
        feed = M.MT5QuoteFeed(c); feed.start(); self.addCleanup(feed.close)
        deadline = time.time() + 2
        while feed.latest is None and time.time() < deadline:
            time.sleep(0.02)
        self.assertIsNotNone(feed.latest)
        return feed

    def test_momentary_history_lag_is_not_an_outage(self):
        fake = LaggingHistoryMT5(lag_calls=3, live=False)
        fake.add_tick(4000.0, 4000.3)
        feed = self.run_feed(fake)
        fake.add_tick(4000.4, 4000.7)                 # history hides it for 3 reads (~0.15 s)
        deadline = time.time() + 2
        while (feed.latest or {}).get("bid") != 4000.4 and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(feed.latest["bid"], 4000.4)  # caught up; latest never cleared
        self.assertIsNone(feed.error)

    def test_persistent_history_gap_still_fails_closed(self):
        fake = LaggingHistoryMT5(lag_calls=10_000, live=False)
        fake.add_tick(4000.0, 4000.3)
        feed = self.run_feed(fake)
        fake.add_tick(4000.4, 4000.7)                 # never appears in history
        deadline = time.time() + 3
        while feed.latest is not None and time.time() < deadline:
            time.sleep(0.05)
        self.assertIsNone(feed.latest)
        self.assertEqual(feed.error, "RuntimeError")


class ServerCheckTests(unittest.TestCase):
    def setUp(self):
        M.MT5Session.reset(); self.addCleanup(M.MT5Session.reset)
        self.c = M.load_config()
        self.c["provider_options"]["mt5_server_utc_offset_minutes"] = 180

    def test_patterns_actual_name_and_hourly_cache(self):
        for server, ok in (("GIVTrade-Demo", True), ("GIVTrade-Live2", True), ("givtrade-server", True),
                           ("Other-Server", False)):
            M.MT5Session.reset()
            with patch.object(M.MT5Session, "call", return_value=SimpleNamespace(server=server)):
                if ok:
                    self.assertEqual(M.MT5Session.offset_seconds(self.c, "XAUUSD"), 10800)
                else:
                    with self.assertRaisesRegex(RuntimeError, "Other-Server"):
                        M.MT5Session.offset_seconds(self.c, "XAUUSD")
        listed = dict(self.c, provider_options=dict(self.c["provider_options"], expected_mt5_server="A-Demo, B-Live"))
        M.MT5Session.reset()
        with patch.object(M.MT5Session, "call", return_value=SimpleNamespace(server="B-Live")) as call:
            for _ in range(50):
                M.MT5Session.offset_seconds(listed, "XAUUSD")
        self.assertEqual(call.call_count, 1)          # V5.1.1: 50 account_info calls


class CalendarClockTests(unittest.TestCase):
    setUp = CandleCorrection.CalendarBridgeTests.setUp
    emit = CandleCorrection.CalendarBridgeTests.emit
    fetch = CandleCorrection.CalendarBridgeTests.fetch

    def test_future_export_time_does_not_block_signals(self):
        self.emit(ExportedUTC=(self.now + timedelta(seconds=90)).strftime("%Y.%m.%d %H:%M:%S"))
        result = self.fetch()
        self.assertLessEqual(M.stamp(result["fetched_at"]), self.now)
        self.assertNotIn("unavailable", M.calendar_block(result, self.now, self.c) or "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
