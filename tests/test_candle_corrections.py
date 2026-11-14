#!/usr/bin/env python3
"""Additional V5.1.1 regression tests. Offline; no MT5 terminal, network or Telegram."""
import csv
import copy
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import gold_monitor as M
import test_signal_gate as TM

UTC = timezone.utc

class V511SafetyTests(unittest.TestCase):
    def setUp(self):
        self.c = M.load_config()
        self.now = datetime(2026, 10, 8, 18, 30, tzinfo=UTC)

    def test_wrong_mt5_server_rejected(self):
        from types import SimpleNamespace
        with patch.object(M.MT5Session,'call',return_value=SimpleNamespace(server='OTHER-Server')):
            with self.assertRaisesRegex(RuntimeError,'expected_mt5_server'):
                M.MT5Session.offset_seconds(self.c,'XAUUSD')

    def test_windows_temporary_sqlite_closed_before_directory_cleanup(self):
        """A retained App must not hold journal.sqlite3 open on tempdir exit.

        Windows raises WinError 32 on deletion of an open database file;
        Linux can unlink it, so assert closure explicitly on both systems.
        """
        import sqlite3
        t = TM.Tests("test_wick_invalidation")
        t.setUp()
        self.addCleanup(t.doCleanups)
        with TM.app_test_directory(t) as folder:
            app = t.app(folder)
            db_file = Path(folder) / "journal.sqlite3"
            self.assertTrue(db_file.is_file())
        self.assertFalse(db_file.exists())
        with self.assertRaises(sqlite3.ProgrammingError):
            app.journal.db.execute("SELECT 1")

    def test_corrected_confirmation_revokes_ready(self):
        t = TM.Tests("test_wick_invalidation")
        t.setUp(); self.addCleanup(t.doCleanups)
        t.arm(); t.push(60, low=101.6, open=101.7, close=102.5)
        st = t.w.setups[t.sid]
        self.assertEqual(st["stage"], 1)
        self.assertTrue(t.w.actionable(t.sid, 102.5, t.now + timedelta(minutes=60, seconds=15)))
        revised = t.bar(60, low=101.6, open=101.7, close=101.3)
        events = t.w.bars("15min", [revised], t.now + timedelta(minutes=64))
        self.assertEqual([x[0] for x in events], ["INVALIDATED"])
        self.assertEqual(st["state"], "INVALIDATED")
        self.assertFalse(t.w.actionable(t.sid, 102.5, t.now + timedelta(minutes=64)))

    def test_corrected_confirmation_revokes_queued_alert(self):
        t = TM.Tests("test_wick_invalidation")
        t.setUp(); self.addCleanup(t.doCleanups)
        with TM.app_test_directory(t) as folder:
            a, now = t.queued_signal_app(folder)
            self.assertIn(t.sid, a.delivery_gate['actionable'])
            # A candle that originally confirmed CLOSE_ABOVE 101.5 is corrected below it.
            revised = t.bar(60, low=101.6, open=101.7, close=101.3)
            events = t.w.bars('15min', [revised], t.now + timedelta(minutes=64))
            a.events(events, t.now + timedelta(minutes=64))
            self.assertNotIn(t.sid, a.delivery_gate['actionable'])
            self.assertNotIn(t.sid, a.reviewed)
            self.assertEqual(t.w.setups[t.sid]['state'], 'INVALIDATED')
            with patch.object(a, 'data_enabled', return_value=True):
                a.update_gate(t.now + timedelta(minutes=64))
            self.assertEqual(a.delivery_gate['actionable'],set())

    def test_corrected_prebirth_history_does_not_revoke(self):
        t = TM.Tests("test_wick_invalidation")
        t.setUp(); self.addCleanup(t.doCleanups)
        t.arm()
        old = copy.deepcopy(t.history["15min"][-3]); old["close"] += .1
        self.assertEqual(t.w.bars("15min", [old], t.now + timedelta(minutes=46)), [])
        self.assertEqual(t.w.setups[t.sid]["state"], "ARMED")

    def test_slippage_limit_and_combined_cap(self):
        spec={"direction":"BUY", "entry_low":100,"entry_high":110,
              "stop_loss":99,"invalidation":99.2,"targets":[150]}
        quote={"bid":102.9,"ask":103,"time":M.iso(self.now)}
        with_slip=dict(self.c,broker_slippage_allowance=.25,
                       broker_max_spread=1.0,broker_max_spread_stop_fraction=.5)
        ok, _, why=M.broker_check(spec,quote,103,self.now,with_slip,True)
        self.assertFalse(ok)
        self.assertIn("slippage",why)
        combined=dict(with_slip,broker_max_slippage_stop_fraction=.5,broker_max_combined_stop_fraction=.01)
        ok,_,why=M.broker_check(spec,quote,103,self.now,combined,True)
        self.assertFalse(ok)
        self.assertIn("Combined",why)
        no_slip=dict(self.c,broker_slippage_allowance=0,broker_max_spread=1.,
                     broker_max_spread_stop_fraction=.5)
        self.assertTrue(M.broker_check(spec,quote,103,self.now,no_slip,True)[0])

    def test_givtrade_sunday_weekly_closes_friday(self):
        session=M.Sessions(self.c)
        label=datetime(2026,9,27,0,0,tzinfo=UTC)
        _,end=M.bar_period('1week',label,label,session,weekly_friday=True)
        self.assertEqual(end,datetime(2026,10,2,21,0,tzinfo=UTC))
        _,old=M.bar_period('1week',label,label,session)
        self.assertGreater(old,end)

    def test_sunday_daily_maps_to_monday_before_validation(self):
        # No 210-bar historical requirement met here; test conversion through fake MT5 backend
        from test_broker_mt5 import FakeMT5
        fake=FakeMT5(); c=self.c
        with patch.object(M.MT5Session,'ensure_symbol'),patch.object(M.MT5Session,'offset_seconds',return_value=10800),patch.object(M.MT5Session,'api',return_value=fake),patch.object(M.MT5Session,'call') as call:
            # historical sample includes Sunday-only and Monday, verify it does not discard
            now=M.utcnow(); day=now.date()
            while day.weekday()!=6: day-=timedelta(days=1)
            data=[]
            for k in range(320,0,-1):
                date=day-timedelta(days=7*k)
                data.append({'time':int(datetime(date.year,date.month,date.day,tzinfo=UTC).timestamp()),
                             'open':100,'high':104,'low':99,'close':102})
            # deliberately insufficient interval density (210+ exists but closed-session holes),
            # still proves Sunday bar conversion isn't silently erased
            call.return_value=data
            try:
                bars=M.MT5Candles(c).candles('1day')
                self.assertTrue(bars)
                self.assertEqual(M.stamp(bars[0]['time']).weekday(),0)
            except ValueError as exc:
                self.assertNotIn('required=210',str(exc))

class CalendarBridgeTests(unittest.TestCase):
    def setUp(self):
        self.c=M.load_config()
        self.now=datetime(2026,10,8,18,30,tzinfo=UTC)
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.csv=Path(self.tmp.name)/'gold_calendar.csv'
        self.c['provider_options']=dict(self.c['provider_options'], calendar_file=str(self.csv))

    def emit(self, **changes):
        event_utc=self.now + timedelta(minutes=20)
        broker=event_utc+timedelta(hours=3)
        row={
            'ValueID':'1', 'EventID':'2', 'Event':'Consumer Price Index (CPI)', 'Currency':'USD',
            'Importance':'HIGH', 'ServerTime':broker.strftime('%Y.%m.%d %H:%M:%S'),
            'UTC_Estimate':event_utc.strftime('%Y.%m.%d %H:%M:%S'),
            'UTC_Status':'ESTIMATE_ONLY', 'OffsetSeconds':'10800', 'Actual':'', 'Forecast':'2.5',
            'Previous':'2.4','Unit':'CALENDAR_UNIT_PERCENT',
            'TimeMode':'CALENDAR_TIMEMODE_DATETIME','SourceURL':'https://example.org',
            'ExportedUTC':self.now.strftime('%Y.%m.%d %H:%M:%S')}
        row.update(changes)
        with self.csv.open('w',encoding='utf-8',newline='') as f:
            w=csv.DictWriter(f,delimiter=';',fieldnames=M.MT5CSVCalendar.REQUIRED)
            w.writeheader(); w.writerow(row)

    def fetch(self,offset=10800):
        with patch.object(M,'utcnow',return_value=self.now),patch.object(M.MT5Session,'offset_seconds',return_value=offset):
            return M.MT5CSVCalendar(self.c).fetch()

    def test_calendar_auto_discovers_mt5_common_files(self):
        from types import SimpleNamespace
        common=Path(self.tmp.name)
        shared=common/'Files'; shared.mkdir()
        self.emit()
        self.csv.replace(shared/'gold_calendar.csv')
        self.c['provider_options']['calendar_file']=''
        with patch.object(M,'utcnow',return_value=self.now), patch.object(M.MT5Session,'offset_seconds',return_value=10800), \
             patch.object(M.MT5Session,'call',return_value=SimpleNamespace(commondata_path=str(common))) as caller:
            result=M.MT5CSVCalendar(self.c).fetch()
        caller.assert_called_with('terminal_info')
        self.assertEqual(result['events'][0]['importance'] if 'importance' in result['events'][0] else result['events'][0]['impact'],'HIGH')

    def test_calendar_valid_and_blocks_cpi(self):
        self.emit()
        result=self.fetch()
        self.assertEqual(result['events'][0]['time'],M.iso(self.now+timedelta(minutes=20)))
        self.assertIn('CPI',M.calendar_block(result,self.now,self.c))
        self.assertEqual(result['source'],'mt5_calendar')

    def test_calendar_stale_fails_closed(self):
        self.emit(ExportedUTC=(self.now-timedelta(minutes=16)).strftime('%Y.%m.%d %H:%M:%S'))
        with self.assertRaisesRegex(ValueError,'stale'):
            self.fetch()

    def test_clock_offset_disagreement_fails_closed(self):
        self.emit()
        with self.assertRaisesRegex(ValueError,'differs'):
            self.fetch(7200)

    def test_calendar_nonexact_event_blocks(self):
        self.emit(TimeMode='CALENDAR_TIMEMODE_NOTIME',UTC_Status='EVENT_TIME_NOT_EXACT',UTC_Estimate='')
        with self.assertRaisesRegex(ValueError,'untimeable'):
            self.fetch()

    def test_noncritical_low_tentative_event_can_be_skipped(self):
        self.emit(Event='Minor regional survey',Importance='LOW',
                  TimeMode='CALENDAR_TIMEMODE_NOTIME',UTC_Status='EVENT_TIME_NOT_EXACT',UTC_Estimate='')
        # With no other usable events, the empty calendar still fails closed.
        with self.assertRaisesRegex(ValueError,'no relevant'):
            self.fetch()

    def test_untimed_critical_event_always_blocks(self):
        self.emit(Event='FOMC meeting',Importance='MEDIUM',
                  TimeMode='CALENDAR_TIMEMODE_NOTIME',UTC_Status='EVENT_TIME_NOT_EXACT',UTC_Estimate='')
        with self.assertRaisesRegex(ValueError, "untimeable USD event: 'FOMC meeting' \\(MEDIUM, CALENDAR_TIMEMODE_NOTIME"):
            self.fetch()

    def test_low_importance_tentative_event_with_a_headline_word_is_skipped(self):
        """16-22 Oct: 'Dallas Fed Trimmed Mean PCE Inflation Rate' (LOW, TENTATIVE) matched "pce" and
        invalidated the whole export for a week."""
        self.emit(Event='Dallas Fed Trimmed Mean PCE Inflation Rate', Importance='LOW',
                  TimeMode='CALENDAR_TIMEMODE_TENTATIVE', UTC_Status='EVENT_TIME_NOT_EXACT', UTC_Estimate='')
        self.emit(Event='Core PCE Price Index m/m', Importance='HIGH')
        cal = self.fetch()
        self.assertEqual([e['title'] for e in cal['events']], ['Core PCE Price Index m/m'])
        self.emit(Event='Core PCE Price Index m/m', Importance='HIGH',
                  TimeMode='CALENDAR_TIMEMODE_TENTATIVE', UTC_Status='EVENT_TIME_NOT_EXACT', UTC_Estimate='')
        with self.assertRaisesRegex(ValueError, "untimeable USD event: 'Core PCE Price Index m/m' \\(HIGH"):
            self.fetch()                                                        # HIGH untimed still blocks

    def test_calendar_absent_blocks(self):
        with self.assertRaises(RuntimeError):
            self.fetch()

    def test_calendar_future_date_detected(self):
        self.emit(ExportedUTC=(self.now+timedelta(hours=2)).strftime('%Y.%m.%d %H:%M:%S'))
        with self.assertRaisesRegex(ValueError,'future'):
            self.fetch()

    def test_calendar_unknown_impact_rejected(self):
        self.emit(Importance='UNKNOWN')
        with self.assertRaisesRegex(ValueError,'unknown'):
            self.fetch()

if __name__=='__main__': unittest.main(verbosity=2)
