"""Calendar hotfix regression tests (offline, no MetaTrader/network)."""
import csv
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import gold_monitor as M

UTC = timezone.utc

class HolidayClassificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "gold_calendar.csv"
        self.now = datetime(2026, 10, 8, 20, 23, 33, tzinfo=UTC)
        self.c = M.load_config()
        self.c["provider_options"] = dict(self.c["provider_options"], calendar_file=str(self.path))
        self.headers = list(M.MT5CSVCalendar.REQUIRED) + ["EventType"]
        self.exact = dict(
            ValueID="317675", EventID="840140001", Event="Initial Jobless Claims",
            Currency="USD", Importance="HIGH", EventType="CALENDAR_TYPE_INDICATOR",
            ServerTime="2026.10.08 15:30:00", UTC_Estimate="2026.10.08 12:30:00",
            UTC_Status="ESTIMATE_ONLY", OffsetSeconds="10800", Actual="197",
            Forecast="190", Previous="197", Unit="CALENDAR_UNIT_NONE",
            TimeMode="CALENDAR_TIMEMODE_DATETIME", SourceURL="https://www.dol.gov",
            ExportedUTC="2026.10.08 20:23:33")
        self.holiday = dict(
            ValueID="237121", EventID="840031007", Event="Columbus Day",
            Currency="USD", Importance="UNKNOWN", EventType="CALENDAR_TYPE_HOLIDAY",
            ServerTime="2026.10.12 03:00:00", UTC_Estimate="",
            UTC_Status="EVENT_TIME_NOT_EXACT", OffsetSeconds="10800", Actual="",
            Forecast="", Previous="", Unit="CALENDAR_UNIT_NONE",
            TimeMode="CALENDAR_TIMEMODE_DATE", SourceURL="https://www.bls.gov",
            ExportedUTC="2026.10.08 20:23:33")

    def fetch(self, *rows):
        with self.path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=self.headers, delimiter=";")
            writer.writeheader()
            for r in rows:
                writer.writerow(r)
        with patch.object(M, "utcnow", return_value=self.now), \
             patch.object(M.MT5Session, "offset_seconds", return_value=10800):
            return M.MT5CSVCalendar(self.c).fetch()

    def test_verified_holiday_preserved_as_all_day(self):
        result = self.fetch(self.exact, self.holiday)
        self.assertEqual(len(result["events"]), 2)
        holiday = [e for e in result["events"] if e.get("event_type") == "HOLIDAY"][0]
        self.assertEqual(holiday["calendar_date"], "2026-10-12")
        self.assertEqual(holiday["impact"], "HOLIDAY")
        self.assertEqual(holiday["time"], "2026-10-12T00:00:00+00:00")

    def test_holiday_blocks_entire_new_york_day(self):
        result = self.fetch(self.exact, self.holiday)
        holiday = datetime(2026, 10, 12, 16, 0, tzinfo=UTC)  # noon in NY
        result["fetched_at"] = M.iso(holiday)
        self.assertIn("holiday", M.calendar_block(result, holiday, self.c).lower())
        next_day = holiday + timedelta(days=1)
        result["fetched_at"] = M.iso(next_day)
        self.assertIsNone(M.calendar_block(result, next_day, self.c))

    def test_unknown_ordinary_event_still_fails_closed(self):
        other = dict(self.exact, Importance="UNKNOWN")
        with self.assertRaisesRegex(ValueError, "unknown event importance"):
            self.fetch(other)

    def test_unspecified_importance_nonholiday_fails_closed(self):
        unspecified = dict(self.exact, Importance="NONE")
        with self.assertRaisesRegex(ValueError, "unspecified importance"):
            self.fetch(unspecified)

    def test_holiday_name_without_metadata_still_rejected(self):
        fake = dict(self.holiday, EventType="CALENDAR_TYPE_EVENT")
        with self.assertRaisesRegex(ValueError, "unknown event importance"):
            self.fetch(self.exact, fake)

    def test_holiday_time_mode_conflict_rejected(self):
        fake = dict(self.holiday, TimeMode="CALENDAR_TIMEMODE_DATETIME")
        with self.assertRaisesRegex(ValueError, "holiday has unexpected time mode"):
            self.fetch(self.exact, fake)

    def test_holiday_estimate_inconsistency_rejected(self):
        fake = dict(self.holiday, UTC_Estimate="2026.10.12 00:00:00")
        with self.assertRaisesRegex(ValueError, "inconsistent time metadata"):
            self.fetch(self.exact, fake)

    def test_holiday_only_is_not_proof_of_complete_calendar(self):
        with self.assertRaisesRegex(ValueError, "no relevant current/future"):
            self.fetch(self.holiday)

    def test_one_hour_dst_shift_in_date_representation(self):
        shifted = dict(self.holiday, ServerTime="2026.10.12 02:00:00")
        result = self.fetch(self.exact, shifted)
        self.assertEqual(result["events"][1]["calendar_date"], "2026-10-12")

    def test_cpi_lock_retained(self):
        cpi = dict(self.exact, Event="Consumer Price Index (CPI)",
                   ServerTime="2026.10.08 23:00:00", UTC_Estimate="2026.10.08 20:00:00")
        result = self.fetch(cpi, self.holiday)
        self.assertIn("CPI", M.calendar_block(result, self.now, self.c))

if __name__ == "__main__":
    unittest.main(verbosity=2)
