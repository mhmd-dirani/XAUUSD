#!/usr/bin/env python3
"""6.1.4: the analyst worker reports WHY a call failed, repeated failures back off instead of
retrying every 60 s (14 Oct 01:45-02:36 UTC: 43 identical BadRequestErrors, 52 blind minutes), and
at most max_maps_per_hour paid maps run per hour.
Offline: no MT5, no network, no model."""
import os
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import gold_monitor as M  # noqa: E402

UTC = timezone.utc


class FakeAPIError(Exception):
    def __init__(self, message, status=400, body=None):
        super().__init__(message)
        self.status_code = status
        self.body = body


class ErrorDetailTests(unittest.TestCase):
    def test_class_status_and_api_message(self):
        exc = FakeAPIError("Error code: 400", body={"type": "error", "error": {
            "type": "invalid_request_error", "message": "prompt is too long: 213000 tokens > 200000 maximum"}})
        self.assertEqual(M.error_detail(exc),
                         "FakeAPIError: HTTP 400: prompt is too long: 213000 tokens > 200000 maximum")

    def test_plain_exception_and_redaction(self):
        self.assertEqual(M.error_detail(ValueError("bad json")), "ValueError: bad json")
        self.assertEqual(M.error_detail(RuntimeError("")), "RuntimeError: (no message)")
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-secret-123"}):
            self.assertNotIn("secret-123", M.error_detail(RuntimeError("key sk-ant-secret-123 rejected")))
        self.assertLessEqual(len(M.error_detail(RuntimeError("x" * 2000))), 500)


class BackoffTests(unittest.TestCase):
    """App.consume_ai / dispatch_ai on a bare App object: only the attributes they touch."""
    def setUp(self):
        self.app = M.App.__new__(M.App)
        self.app.epoch = "E1"
        self.app.analyst = Mock()
        self.app.analyst.job = None
        self.app.journal = Mock()
        self.app.next_map = self.app.next_full = 0
        self.app.history, self.app.calendar, self.app.sessions, self.app.pending = {}, {"events": []}, None, {}
        self.now = datetime.now(UTC)

    def fail(self, text="BadRequestError: HTTP 400: prompt is too long"):
        self.app.analyst.poll.return_value = ({"kind": "map", "tag": {"epoch": "E1", "payload_chars": 812345}},
                                              "error", text)
        before = time.time()
        self.app.consume_ai(self.now)
        return self.app.next_map - before

    def test_failures_back_off_exponentially_and_journal_the_reason(self):
        delays = [round(self.fail()) for _ in range(6)]
        self.assertEqual(delays, [60, 120, 240, 480, 960, 1800])            # capped at 30 min
        self.assertEqual(self.app.ai_fail_streak, 6)
        kinds = [c.args[0] for c in self.app.journal.audit.call_args_list]
        self.assertEqual(kinds, ["AI_FAILED"] * 6)
        last = self.app.journal.audit.call_args_list[-1].args[1]
        self.assertEqual((last["error"], last["streak"], last["retry_in_s"], last["payload_chars"]),
                         ("BadRequestError: HTTP 400: prompt is too long", 6, 1800, 812345))

    def test_dispatch_waits_out_the_backoff_even_after_a_reassessment_resets_next_map(self):
        self.fail()
        self.app.next_map = 0                                               # what an early reassessment does
        self.app.full_ready = True
        with patch.object(M, "history_fresh", return_value=True):
            self.app.dispatch_ai(self.now)
        self.app.analyst.start.assert_not_called()
        self.app.ai_backoff_until = time.time() - 1                         # back-off over: the map may run
        with patch.object(M, "history_fresh", return_value=True), \
             patch.object(self.app, "payload", return_value={}), \
             patch.object(self.app, "memory_payload", return_value={}), \
             patch.object(self.app, "trader_playbook", return_value=None), \
             patch.object(M.EV, "compact", return_value={}):
            self.app.evidence, self.app.bootstrap = {}, None
            self.app.feed = Mock(latest={"price": 4100.0})
            self.app.c = {"map_refresh_seconds": 5400}
            self.app.dispatch_ai(self.now)
        self.app.analyst.start.assert_called_once()

    def test_at_most_max_maps_per_hour(self):
        """15 Oct 00:00-01:03 UTC: six paid maps in one hour from zone-entry / abnormal-range reassessments."""
        self.app.full_ready, self.app.evidence, self.app.bootstrap = True, {}, None
        self.app.feed = Mock(latest={"price": 4100.0})
        self.app.c = {"map_refresh_seconds": 3600, "max_maps_per_hour": 2}
        with patch.object(M, "history_fresh", return_value=True), patch.object(self.app, "payload", return_value={}), \
             patch.object(self.app, "memory_payload", return_value={}), patch.object(self.app, "trader_playbook", return_value=None), \
             patch.object(M.EV, "compact", return_value={}):
            for _ in range(5):
                self.app.next_map, self.app.full_ready = 0, True          # what a reassessment does
                self.app.dispatch_ai(self.now)
            self.assertEqual(self.app.analyst.start.call_count, 2)
            self.assertGreater(self.app.next_map, time.time() + 3500)      # waits for the oldest to age out
            self.app.map_starts = [time.time() - 3601, time.time() - 3601]
            self.app.next_map, self.app.full_ready = 0, True
            self.app.dispatch_ai(self.now)
            self.assertEqual(self.app.analyst.start.call_count, 3)

    def test_a_failed_map_does_not_count_against_the_hourly_cap(self):
        """22 Oct 22:04: two instant 401s were counted as two maps and the next try waited 57 more minutes."""
        self.app.map_starts = [time.time() - 5, time.time() - 2]
        self.fail("AuthenticationError: HTTP 401: invalid x-api-key")
        self.assertEqual(len(self.app.map_starts), 1)
        self.fail("AuthenticationError: HTTP 401: invalid x-api-key")
        self.assertEqual(self.app.map_starts, [])
        self.fail("AuthenticationError: HTTP 401: invalid x-api-key")                     # nothing left to drop
        self.assertEqual(self.app.map_starts, [])

    def test_a_result_resets_the_streak(self):
        self.fail()
        self.fail()
        self.assertEqual(self.app.ai_fail_streak, 2)
        self.app.analyst.poll.return_value = ({"kind": "map", "tag": {"epoch": "E1"}}, "result", {})
        with patch.object(self.app, "events"), patch.object(M, "snapshot"):
            try:
                self.app.consume_ai(self.now)                               # the rest of the map path is not under test
            except Exception:  # noqa: BLE001
                pass
        self.assertEqual(self.app.ai_fail_streak, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
