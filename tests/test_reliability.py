"""6.1.3 regression tests: live-reliability fixes found in the pre-live code review.
Offline only: no MT5 terminal, network, OpenAI or Telegram."""
import copy
import json
import sys
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import gold_evidence as EV  # noqa: E402
import gold_monitor as M  # noqa: E402
import test_signal_gate as TM  # noqa: E402


class MT5ReliabilityTests(unittest.TestCase):
    def setUp(self):
        M.MT5Session.reset()
        self.c = M.load_config()
        self.c["provider_options"] = dict(self.c["provider_options"], mt5_server_utc_offset_minutes="auto",
                                          expected_mt5_server="")

    def tearDown(self):
        M.MT5Session.reset()

    def test_quiet_hourly_recheck_keeps_known_offset(self):
        tick = Mock(bid=4000.0, ask=4000.3, time_msc=1_000)          # never advances: no new tick
        M.MT5Session.offset, M.MT5Session.offset_at = 10800, time.time() - 3700   # expired, but recent
        with patch.object(M.MT5Session, "tick", return_value=tick), \
                patch.object(M.MT5Session, "PROBE_SECONDS", 0.05):
            self.assertEqual(M.MT5Session.offset_seconds(self.c, "XAUUSD"), 10800)

    def test_no_recent_offset_still_fails_closed(self):
        tick = Mock(bid=4000.0, ask=4000.3, time_msc=1_000)
        M.MT5Session.offset, M.MT5Session.offset_at = 10800, time.time() - 5 * 3600   # e.g. after a weekend
        with patch.object(M.MT5Session, "tick", return_value=tick), \
                patch.object(M.MT5Session, "PROBE_SECONDS", 0.05):
            with self.assertRaises(RuntimeError):
                M.MT5Session.offset_seconds(self.c, "XAUUSD")

    def test_feed_reconnects_after_repeated_failures(self):
        feed = M.MT5QuoteFeed(self.c)
        waits = []

        def fake_wait(seconds):
            waits.append(seconds)
            if len(waits) >= 8:
                feed.stop.set()
            return feed.stop.is_set()

        feed.stop.wait = fake_wait
        with patch.object(M.MT5Session, "offset_seconds", side_effect=RuntimeError("MT5 returned no rates")), \
                patch.object(M.MT5Session, "reset") as reset:
            feed.run()
        self.assertGreaterEqual(reset.call_count, 1)   # old code: never, unless the text said "initialize"

    def test_reset_closes_the_terminal_connection(self):
        fake = Mock()
        M.MT5Session.mt5 = fake
        M.MT5Session.reset()
        fake.shutdown.assert_called_once()
        self.assertIsNone(M.MT5Session.mt5)


class FileAndJournalTests(unittest.TestCase):
    def test_locked_snapshot_never_aborts(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            with patch.object(M.os, "replace", side_effect=PermissionError("locked by OneDrive")), \
                    patch.object(M.time, "sleep"):
                M.snapshot(Path(folder) / "news.json", {"risk": "LOW"})   # logs, does not raise
                with self.assertRaises(PermissionError):
                    M.atomic(Path(folder) / "news.json", {"risk": "LOW"})

    def test_atomic_retries_a_brief_lock(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            real, calls = M.os.replace, []

            def flaky(a, b):
                calls.append(1)
                if len(calls) < 3:
                    raise PermissionError("busy")
                return real(a, b)

            with patch.object(M.os, "replace", side_effect=flaky), patch.object(M.time, "sleep"):
                M.atomic(Path(folder) / "x.json", {"a": 1})
            self.assertEqual(json.loads((Path(folder) / "x.json").read_text(encoding="utf-8")), {"a": 1})

    def test_audit_rows_unique_with_coarse_clock(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            j = M.Journal(Path(folder))
            with patch.object(M.time, "time_ns", return_value=123):
                j.audit("GRADED", {"n": 1})
                j.audit("GRADED", {"n": 2})
            n = j.db.execute("SELECT COUNT(*) FROM events WHERE status='GRADED'").fetchone()[0]
            j.db.close()
            self.assertEqual(n, 2)


class EvidenceTests(unittest.TestCase):
    def test_compact_shows_bos_choch_and_fvgs(self):
        def item(i, kind, status="CONFIRMED", day=1):
            return {"id": f"1h:{kind}:{i}", "kind": kind, "timeframe": "1h", "status": status,
                    "at": f"2026-10-{day:02d}T00:00:00+00:00", "direction": "BUY"}
        index = {}
        # grouped by kind (as build_evidence does), sweeps and order blocks last
        for i in range(5):
            index[f"b{i}"] = item(i, "BOS", day=1 + i)
            index[f"c{i}"] = item(i, "CHOCH", day=2 + i)
            index[f"f{i}"] = item(i, "FVG", status="ACTIVE", day=3 + i)
        for i in range(10):
            index[f"s{i}"] = item(i, "LIQUIDITY_SWEEP", day=1 + i)
            index[f"o{i}"] = item(i, "ORDER_BLOCK_CANDIDATE", status="ACTIVE", day=1 + i)
        c = EV.compact({"built_at": "x", "frames": {}, "index": index})
        frame = c["frames"]["1h"]
        kinds = [i["kind"] for i in frame["structure"]]
        self.assertTrue({"BOS", "CHOCH"} <= set(kinds))
        self.assertEqual(len(frame["structure"]), 6)
        self.assertIn("FVG", [i["kind"] for i in frame["zones"]])
        self.assertEqual(len(frame["zones"]), 8)
        ats = [i["at"] for i in frame["structure"]]
        self.assertEqual(ats, sorted(ats))


class HigherFrameInvalidationTests(unittest.TestCase):
    def setUp(self):
        self.t = TM.Tests("test_wick_invalidation")
        self.t.setUp()
        self.born = self.t.now + timedelta(minutes=30)
        self.w = M.Watcher(self.t.c, "2026-10-06")
        self.w.install(copy.deepcopy(self.t.data), self.t.history, self.born, self.born)
        self.sid = next(iter(self.w.setups))

    def bar(self, start):
        return {"time": M.iso(start), "open": 101., "high": 102., "low": 98., "close": 101.5}

    def test_bar_opened_before_birth_does_not_invalidate(self):
        start = self.t.now                              # 12:00 bar, setup born 12:30
        events = self.w.bars("1h", [self.bar(start)], start + timedelta(hours=1, seconds=10))
        self.assertNotIn("INVALIDATED", [e[0] for e in events])
        self.assertIn(self.w.setups[self.sid]["state"], ("WATCHING", "ARMED"))

    def test_bar_opened_before_birth_still_confirms_conditions(self):
        data = copy.deepcopy(self.t.data)
        data["setups"][0]["conditions"] = [{"type": "CLOSE_ABOVE", "timeframe": "1h", "level": 101.5}]
        born = self.t.now + timedelta(minutes=5)          # 12:05: the 12:00 1H bar opened before birth
        w = M.Watcher(self.t.c, "2026-10-06")
        w.install(data, self.t.history, born, born)
        sid = next(iter(w.setups))
        for minutes in (15, 30, 45):                      # three 15M contacts -> ARMED at 12:45
            w.bars("15min", [self.t.bar(minutes)], self.t.now + timedelta(minutes=minutes, seconds=12))
        self.assertEqual(w.setups[sid]["state"], "ARMED")
        bar = {"time": M.iso(self.t.now), "open": 101., "high": 102.5, "low": 100.2, "close": 101.8}
        events = w.bars("1h", [bar], self.t.now + timedelta(hours=1, seconds=10))
        self.assertIn("READY", [e[0] for e in events])

    def test_bar_after_birth_still_invalidates(self):
        start = self.t.now + timedelta(hours=1)         # 13:00 bar, fully after birth
        events = self.w.bars("1h", [self.bar(start)], start + timedelta(hours=1, seconds=10))
        self.assertIn("INVALIDATED", [e[0] for e in events])


class MemoryTests(unittest.TestCase):
    def test_payload_keeps_smc_zones_and_prune_bounds_db(self):
        import astra_memory as AM
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            mem = AM.MarketMemory(Path(folder) / "m.sqlite3")
            self.addCleanup(mem.db.close)   # Windows cannot delete an open database file
            now = TM.stamp("2026-10-09T20:00:00Z")
            rows = []
            for i in range(80):                         # many newer structure items...
                rows.append({"id": f"s{i}", "kind": "BOS", "timeframe": "1h", "status": "CONFIRMED",
                             "at": M.iso(now - timedelta(minutes=i)), "seen_at": M.iso(now)})
            rows.append({"id": "fvg1", "kind": "FVG", "timeframe": "1h", "status": "ACTIVE", "direction": "BUY",
                         "at": M.iso(now - timedelta(days=3)), "seen_at": M.iso(now),
                         "level_low": 4000.0, "level_high": 4005.0})
            for d in rows:
                mem.db.execute("INSERT INTO smc_items VALUES (?,?,?,?)", (d["id"], d["timeframe"], d["status"], json.dumps(d)))
            mem.db.execute("INSERT INTO smc_items VALUES (?,?,?,?)",
                           ("old", "1h", "ACTIVE", json.dumps({"id": "old", "kind": "FVG", "seen_at": M.iso(now - timedelta(days=40))})))
            mem.db.execute("INSERT INTO analyses VALUES (?,?,?,?)", ("a_old", M.iso(now - timedelta(days=120)), "NEUTRAL", "{}"))
            mem.db.commit()
            history = {tf: [] for tf in M.TF}
            with patch.object(mem, "daily_atr", return_value=50.0):
                p = mem.payload(4002.0, now, history, {})
            self.assertIn("fvg1", [x["id"] for x in p["smc_zones"]])
            mem._prune(now)
            mem.db.commit()
            ids = {r[0] for r in mem.db.execute("SELECT id FROM smc_items")}
            self.assertNotIn("old", ids)
            self.assertIn("fvg1", ids)
            analyses = mem.db.execute("SELECT COUNT(*) FROM analyses").fetchone()[0]
            mem.db.close()                  # close before the temporary folder is deleted
            self.assertEqual(analyses, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
