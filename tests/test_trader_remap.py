#!/usr/bin/env python3
"""6.2.1: an early Astra map asked for by the auto-trader (runtime/remap_request.json). Offline."""
import json
import sys
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import gold_monitor as M  # noqa: E402


class TraderRemapTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.app = M.App.__new__(M.App)
        self.app.c = M.load_config(None)
        self.app.folder = self.dir
        self.app.journal = M.Journal(self.dir)
        self.app.analyst = Mock(job=None)
        self.app.epoch = "E"
        self.app.active = self.app.warmed = True
        self.app.analysis = None
        self.app.history, self.app.sessions = {}, None
        self.app.next_map = 999
        self.now = M.utcnow()

    def write(self, minutes_ago=1, **extra):
        req = dict(id="REMAP-60084880", reason="LAB-SELL-3952 TP1 3926.2 traded before the fill",
                   at=M.iso(self.now - timedelta(minutes=minutes_ago)), **extra)
        (self.dir / M.App.REMAP_FILE).write_text(json.dumps(req), encoding="utf-8")

    def read(self):
        return json.loads((self.dir / M.App.REMAP_FILE).read_text(encoding="utf-8"))

    def serve(self):
        with patch.object(M, "history_fresh", return_value=True):
            return self.app.sense_trader_requests(self.now)

    def test_no_file_nothing_happens(self):
        self.assertFalse(self.serve())
        self.assertEqual(self.app.next_map, 999)

    def test_a_request_starts_an_early_map_once(self):
        self.write()
        self.assertTrue(self.serve())
        self.assertEqual(self.app.next_map, 0)                       # the map loop starts a map now
        self.assertIn("served", self.read()["outcome"])
        self.app.next_map = 999
        self.assertFalse(self.serve())                                # the same request is never served twice
        self.assertEqual(self.app.next_map, 999)
        n = self.app.journal.db.execute("SELECT count(*) FROM events WHERE status='EARLY_REASSESSMENT'").fetchone()[0]
        self.assertEqual(n, 1)

    def test_a_newer_map_answers_it_for_free(self):
        self.write(minutes_ago=5)
        self.app.analysis = {"created_at": M.iso(self.now - timedelta(minutes=1))}
        self.assertFalse(self.serve())
        self.assertIn("answered", self.read()["outcome"])
        self.assertEqual(self.app.next_map, 999)

    def test_cooldown_waits_without_marking(self):
        self.app.last_trader_remap = time.time() - 60
        self.write()
        self.assertFalse(self.serve())
        self.assertNotIn("served_at", self.read())                    # still pending: retried later

    def test_daily_cap_refuses(self):
        self.app.trader_remap_times = [time.time() - 3600 * k for k in range(1, 9)
                                       if time.time() - 3600 * k >= M.utcnow().replace(hour=0, minute=0, second=0,
                                                                                       microsecond=0).timestamp()]
        self.app.trader_remap_times += [time.time()] * (8 - len(self.app.trader_remap_times))
        self.write()
        self.assertFalse(self.serve())
        self.assertIn("refused", self.read()["outcome"])

    def test_hard_limit_refuses(self):
        self.app.c["ai_daily_hard_limit_usd"] = 1
        self.app.record_usage("map", {"model": "claude-opus-5-5", "input_tokens": 1_000_000, "output_tokens": 0})
        self.write()
        self.assertFalse(self.serve())
        self.assertIn("hard limit", self.read()["outcome"])

    def test_old_or_garbage_requests(self):
        self.write(minutes_ago=45)
        self.assertFalse(self.serve())
        self.assertIn("expired", self.read()["outcome"])
        (self.dir / M.App.REMAP_FILE).write_text("{not json", encoding="utf-8")
        self.assertFalse(self.serve())

    def test_switched_off_or_market_closed(self):
        self.write()
        self.app.c["trader_remap_requests"] = False
        self.assertFalse(self.serve())
        self.app.c["trader_remap_requests"] = True
        self.app.active = False
        self.assertFalse(self.serve())
        self.assertNotIn("served_at", self.read())

    def test_config_validation(self):
        c = M.load_config(None)
        self.assertTrue(c["trader_remap_requests"])
        self.assertEqual((c["trader_remap_cooldown_seconds"], c["trader_remap_max_per_day"]), (900, 8))


if __name__ == "__main__":
    unittest.main()
