# GivTrade MT5 setup guide (Gold Monitor V6.1.3)

**Status:** Offline implementation; not cleared for unattended BUY/SELL alerts until a Windows MT5 terminal validates the bridge, timestamps and historical-feed continuity. **Never place orders.** The Python and MQL5 files include no order-submission calls. Do not share account numbers or passwords.

## Files

- `src/gold_monitor.py`: the monitor; Astra + market memory + evidence + Python safety rules + Telegram.
- `mql/mql5/GoldCalendarBridge.mq5`: Timer-driven read-only MT5 Expert Advisor; exports economic calendar every 300 seconds.
- `tools/mt5_calendar_to_excel.py`: Exports the bridge CSV to Excel. Supports optional continuous `--watch` updates.
- `src/gold_smc_engine.py`, `src/gold_confluence_v5.py`, `src/gold_evidence.py`: objective pattern and evidence modules.
- `tests/`: 294 offline regression tests (`scripts\selftest.ps1`).
- `config/gold_v5_config.example.json`: config example for the GivTrade demo.

## Step 1 — Install, compile, attach the calendar bridge

1. On Windows open MT5 logged into your GivTrade account (demo first), with `XAUUSD` in Market Watch. Note the exact server name shown in MT5 (File > Login to Trade Account).
2. Press **F4** to open MetaEditor. Create a new **Expert Advisor** named `GoldCalendarBridge`, not a Script. Copy in `mql/mql5/GoldCalendarBridge.mq5`; press **F7** to compile. Fix any compiler error before proceeding (MQL5 compilation has not been tested in this environment).
3. In MT5, drag `Navigator > Expert Advisors > GoldCalendarBridge` onto any chart. It is read-only; leave terminal **Algo Trading disabled**. Confirm the Experts tab prints **"Calendar bridge: published"** with a count of USD events. It republishes every five minutes while MT5 is running.
4. CSV location: `TerminalInfoString(TERMINAL_COMMONDATA_PATH)\Files\gold_calendar.csv`. The exporter writes a separate pending file and moves it atomically, so Python avoids partial reads.
5. The bridge records a **preliminary** UTC estimate from its server offset. The Python provider independently derives an offset from fresh MT5 ticks; disagreement, missing data, unknown impact, or stale calendar **blocks new BUY/SELL signals**.

## Step 2 — Check Excel (optional)

From the project root run:

```powershell
.\.venv\Scripts\python.exe -u .\tools\mt5_calendar_to_excel.py
```

For a live-updating workbook while this command stays running:

```powershell
.\.venv\Scripts\python.exe -u .\tools\mt5_calendar_to_excel.py --watch
```

It updates `data\gold_calendar.xlsx` when the MT5 CSV changes. Close the Excel workbook before automatic replacement on Windows to avoid a file-lock error. **The bot reads CSV, not Excel.**

## Step 3 — Configure and test Python

```powershell
$env:MT5_PATH = 'C:\Program Files\MetaTrader 5\terminal64.exe'
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\scripts\selftest.ps1
Copy-Item config\gold_v5_config.example.json config\gold_v5_config.json
```

If you already have a `gold_v5_config.json`, compare it with the example and add any key it lacks. The config now defaults to MT5 for `candles`, `quotes`, and `broker_quotes`, and `mt5_csv` for the calendar. The broker symbol is `XAUUSD`, and `provider_options.expected_mt5_server` is `GIVTrade-*` (any GivTrade server; set your exact server name to be stricter). The startup requires your OpenAI and Telegram environment keys. Use an **investor/read-only password** where supported. No MT5 password is needed if MT5 is already logged in.

To require **both** Forex Factory and the automatic MT5 calendar, change `providers.calendar` to `mt5_dual`. The dual mode blocks alerts if either calendar is unavailable; it unions high-impact event restrictions.

Entry-cost policy: `broker_slippage_allowance` ($0.25) is a hypothetical worse-entry buffer, not measured slippage; set it to `0` to check ask/bid only. The allowance-only stop cap is off by default (it implied a fixed $10 minimum stop); use `minimum_stop_distance` if you want an explicit minimum. Spread limits and structural invalidation always apply.

```powershell
.\scripts\run.ps1
```

Keep this in **demo/paper monitoring** until the live checks below pass.

## Step 4 — Required real-terminal tests (not yet done)

- Confirm Expert Advisor **compiles** and continues exporting on the timer for an hour; verify `ExportedUTC` advances, and the `mt5_csv` reader accepts it.
- Compare one USD high-impact event's computed UTC time against a second trustworthy calendar (e.g. Forex Factory). Test both during and outside daylight-saving transitions; never permanently assume UTC+3.
- Confirm the expected account server and symbol, `XAUUSD` bid/ask, and ≥210 **closed** candles for M15, H1, H4, D1, W1, with no missing open-session 15M slots.
- Verify raw D1/W1 labels against the GivTrade live chart. Weekend-labelled D1 bars are merged into Monday/Friday automatically; confirm the merged Monday OHLC matches what you expect, and validate Sunday-labelled W1 bars.
- Run a non-trading paper feed for several sessions; watch the log for `tick-history gap` warnings (each forces a re-map), and simulate an invalidation while the sender is pending, a revised 15M confirmation, a disconnected MT5 terminal and a 2,000-tick backlog.
- Before 24/5 deployment, check broker holiday calendars. MT5's terminal is required on the **same Windows machine or Windows VPS**. No true cloud deployment is included here.

## Safety notes

The code does **not** execute trades. No live MQL5 compilation, MT5 testing, or profitability analysis was performed here. Broker spread/slippage filters are provisional, and indicators/SMC evidence do not guarantee future performance. If safety inputs are missing/stale, the bot must refrain from BUY/SELL alerts.
