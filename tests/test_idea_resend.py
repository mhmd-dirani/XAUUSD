#!/usr/bin/env python3
"""Idea reports: the same area is re-sent when it is a NEW trade (earlier idea finished, or the
entry/stop/TP1 changed), never for the same still-running plan. Offline."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import gold_monitor as M  # noqa: E402
import astra_analysis as AA  # noqa: E402
import astra_memory as AM  # noqa: E402

UTC = timezone.utc


def idea(**over):
    i = {"id": "ID8", "direction": "SELL", "risk": "MEDIUM", "confidence": 36, "entry_type": "LIMIT",
         "readiness": "NEEDS_CONFIRMATION", "entry_low": 4103.6, "entry_high": 4105.7, "invalidation": 4118.2,
         "targets": [4073.53, 4066.53], "memory_ids": []}
    i.update(over)
    return i


class Fixture(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.app = NS(c=M.load_config(), journal=M.Journal(d), memory=AM.MarketMemory(d / "m.sqlite3"))
        self.app.idea_finished = lambda prev: M.App.idea_finished(self.app, prev)
        self.now = datetime(2026, 10, 14, 13, 14, tzinfo=UTC)
        self.n = 0

    def report(self, i, analysis_id=None):
        """One analysis: record the idea in memory (like the monitor), then try to report it."""
        self.n += 1
        aid = analysis_id or f"A{self.n:03d}"
        self.app.memory.record_ideas(aid, M.iso(self.now), [i], 4100.0)
        key, record = M.App.idea_report_key(self.app, {"id": aid}, i, 50.0, "2026-10-14", self.now)
        sent = self.app.journal.claim(key, {"text": "x"})
        if sent and record:
            self.app.journal.put(*record)
        return key, sent

    def resolve(self, status):
        self.now += timedelta(hours=1)                                          # resolved after the report
        self.app.memory.db.execute("UPDATE predictions SET status=?, resolved_at=?", (status, M.iso(self.now)))
        self.app.memory.db.commit()


class IdeaResendTests(Fixture):
    def test_first_idea_keeps_the_old_key(self):
        key, sent = self.report(idea())
        self.assertTrue(sent)
        self.assertEqual(key, AA.idea_key(idea(), 50.0, "2026-10-14"))

    def test_same_running_plan_is_not_repeated(self):
        self.report(idea())
        _, sent = self.report(idea(entry_low=4104.16, invalidation=4118.5))     # wording/price drift only
        self.assertFalse(sent)

    def test_new_trade_after_the_first_finished_is_sent(self):
        self.report(idea())
        self.resolve("PLAYED_OUT")                                              # first SELL reached its target
        key, sent = self.report(idea())                                         # same plan again later
        self.assertTrue(sent)
        self.assertTrue(key.endswith(":r1"))
        _, again = self.report(idea())                                          # and only once per new trade
        self.assertFalse(again)

    def test_failed_or_expired_also_counts_as_finished(self):
        for status in ("FAILED", "NOT_TRIGGERED", "EXPIRED_AFTER_TRIGGER"):
            self.setUp()
            self.report(idea())
            self.resolve(status)
            self.assertTrue(self.report(idea())[1], status)

    def test_different_stop_or_target_is_a_different_order(self):
        self.report(idea())
        self.assertTrue(self.report(idea(invalidation=4126.0))[1])              # stop moved 7.8 >= 0.15 x 50
        self.assertTrue(self.report(idea(invalidation=4126.0, targets=[4060.0]))[1])  # TP1 moved
        self.assertFalse(self.report(idea(invalidation=4126.0, targets=[4060.0]))[1])

    def test_switch_off_restores_once_per_day(self):
        self.app.c["astra_idea_resend_new_trade"] = False
        self.report(idea())
        self.resolve("PLAYED_OUT")
        self.assertFalse(self.report(idea())[1])

    def test_deduplicated_history_record_found_by_group(self):
        self.report(idea())                                                     # stored as A001:idea-ID8
        # a later identical idea is NOT stored again by memory (near-duplicate) but is reported
        prev = self.app.journal.get("idea-area:" + AA.idea_key(idea(), 50.0, "2026-10-14"))
        prev["pid"] = "A999:idea-ID8"                                           # pid that memory never stored
        self.assertFalse(self.app.idea_finished(prev))                          # group still OPEN
        self.resolve("FAILED")                                                  # finished after the report
        self.assertTrue(self.app.idea_finished(prev))
        prev["sent_at"] = M.iso(self.now + timedelta(hours=1))                  # finished BEFORE this report
        self.assertFalse(self.app.idea_finished(prev))

    def test_config_validation(self):
        d = Path(tempfile.mkdtemp())
        for bad in ({"astra_idea_resend_new_trade": "yes"}, {"astra_idea_change_atr": 0}):
            (d / "c.json").write_text(json.dumps(bad))
            with self.assertRaises(ValueError):
                M.load_config(d / "c.json")


if __name__ == "__main__":
    unittest.main(verbosity=2)
