// GoldCalendarBridge.mq5 — read-only automatic MT5 USD calendar exporter
// Install in MQL5/Experts, compile, attach once to any GivTrade chart.
// Uses NO trading calls; terminal Algo Trading can remain disabled.
// Output (every 5 minutes): Terminal/Common/Files/gold_calendar.csv
// Atomic tmp -> final rename. Python revalidates offset against a fresh MT5 tick.
#property strict
#property version "1.11"

input int RefreshSeconds = 300;  // 5 minutes
input int HistoryDays = 1;
input int FutureDays = 7;

string CleanCSV(string s)
{
   StringReplace(s, ";", ",");
   StringReplace(s, "\r", " ");
   StringReplace(s, "\n", " ");
   // No Excel formula execution when someone opens the CSV directly.
   if(StringLen(s) > 0)
   {
      string first=StringSubstr(s,0,1);
      if(first=="=" || first=="+" || first=="-" || first=="@") s="'"+s;
   }
   return s;
}

string ImportanceName(ENUM_CALENDAR_EVENT_IMPORTANCE importance)
{
   if(importance==CALENDAR_IMPORTANCE_HIGH) return "HIGH";
   if(importance==CALENDAR_IMPORTANCE_MODERATE) return "MEDIUM";
   if(importance==CALENDAR_IMPORTANCE_LOW) return "LOW";
   if(importance==CALENDAR_IMPORTANCE_NONE) return "NONE";
   return "UNKNOWN";
}

string OptionalScaled(long v)
{
   if(v==LONG_MIN) return "";
   return DoubleToString((double)v/1000000.0,6);
}

void ExportCalendar()
{
   datetime server_now=TimeTradeServer();
   datetime utc_now=TimeGMT();
   long observed_offset=(long)server_now-(long)utc_now;
   long offset=(long)MathRound((double)observed_offset/900.0)*900;
   if(MathAbs((double)(observed_offset-offset))>120 || MathAbs((double)offset)>14*3600)
   {
      Print("Calendar bridge: server offset ambiguous; no export. Python will fail closed.");
      return;
   }

   MqlCalendarValue values[];
   ResetLastError();
   int count=CalendarValueHistory(values,server_now-HistoryDays*86400,
                                  server_now+FutureDays*86400,"","USD");
   if(count<=0)
   {
      Print("Calendar bridge: calendar unavailable/empty; preserving old file for stale detection. Error=",GetLastError());
      return;
   }

   string tmp="gold_calendar.pending.csv";
   int h=FileOpen(tmp,FILE_WRITE|FILE_CSV|FILE_COMMON|FILE_ANSI,';',CP_UTF8);
   if(h==INVALID_HANDLE)
   {
      Print("Calendar bridge: cannot create temporary CSV. Error=",GetLastError());
      return;
   }
   FileWrite(h,"ValueID","EventID","Event","Currency","Importance","EventType",
             "ServerTime","UTC_Estimate","UTC_Status","OffsetSeconds",
             "Actual","Forecast","Previous","Unit","TimeMode",
             "SourceURL","ExportedUTC");
   int n=0;
   for(int i=0;i<count;i++)
   {
      MqlCalendarEvent ev;
      if(!CalendarEventById(values[i].event_id,ev))
      {
         FileClose(h);
         FileDelete(tmp,FILE_COMMON);
         Print("Calendar bridge: event metadata missing; rejecting partial export");
         return;
      }
      datetime event_utc=(datetime)((long)values[i].time-offset);
      string exact_status=(ev.time_mode==CALENDAR_TIMEMODE_DATETIME) ? "ESTIMATE_ONLY" : "EVENT_TIME_NOT_EXACT";
      string estimate=(ev.time_mode==CALENDAR_TIMEMODE_DATETIME) ?
         TimeToString(event_utc,TIME_DATE|TIME_SECONDS) : "";
      FileWrite(h,(string)values[i].id,(string)values[i].event_id,
                CleanCSV(ev.name),"USD",ImportanceName(ev.importance),EnumToString(ev.type),
                TimeToString(values[i].time,TIME_DATE|TIME_SECONDS),
                estimate,exact_status,(string)offset,
                OptionalScaled(values[i].actual_value),
                OptionalScaled(values[i].forecast_value),
                OptionalScaled(values[i].prev_value),
                EnumToString(ev.unit),EnumToString(ev.time_mode),
                CleanCSV(ev.source_url),TimeToString(utc_now,TIME_DATE|TIME_SECONDS));
      n++;
   }
   FileFlush(h);
   FileClose(h);
   if(n==0)
   {
      FileDelete(tmp,FILE_COMMON);
      Print("Calendar bridge: no valid USD events: retaining previous export");
      return;
   }
   ResetLastError();
   if(!FileMove(tmp,FILE_COMMON,"gold_calendar.csv",FILE_COMMON|FILE_REWRITE))
   {
      Print("Calendar bridge: failed to publish CSV. Error=",GetLastError());
      return;
   }
   PrintFormat("Calendar bridge: published %d USD events | server-UTC offset=%d seconds | %s\\Files\\gold_calendar.csv",
               n,(int)offset,TerminalInfoString(TERMINAL_COMMONDATA_PATH));
}

int OnInit()
{
   if(RefreshSeconds<60 || RefreshSeconds>3600 || HistoryDays<0 || HistoryDays>7 || FutureDays<1 || FutureDays>31)
   {
      Print("Calendar bridge: invalid timer or day-window configuration");
      return INIT_PARAMETERS_INCORRECT;
   }
   if(!EventSetTimer(RefreshSeconds))
   {
      Print("Calendar bridge: EventSetTimer failed. Error=",GetLastError());
      return INIT_FAILED;
   }
   ExportCalendar();
   return INIT_SUCCEEDED;
}

void OnTimer() { ExportCalendar(); }
void OnDeinit(const int reason) { EventKillTimer(); }
