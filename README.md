# Gold Monitor V6.2.1 "Astra"

An XAU/USD (gold) analysis and alert bot. **Alerts only: it never places, changes or closes a trade.**
A separate, optional demo auto-trader (1.20.3) lives in the same project; see the end of this file.

- **Python is the sensor.** It reads closed candles and live bid/ask from your MetaTrader 5 terminal (read-only) and remembers every important zone: tests, breaks, flips, retests and liquidity sweeps. It also checks the economic calendar, the news and the broker spread.
- **Astra is the analyst.** Astra is your saved OpenAI agent. Every hour, or earlier when price enters a remembered zone, it builds a multi-timeframe market map. It writes a market story, scenarios, a **TRADE IDEA** (entry, stop loss, take profits, confidence, risk) and **STRUCTURE WATCH** reports. It also uses the trading rules you taught it from your own charts.
- **Telegram gets two kinds of message:**
  - **BUY/SELL signals.** These are sent only when every Python safety check passes: fresh data, verified SMC evidence, confirmation candles, at least 2R reward/risk, no calendar or news lock, and an acceptable broker spread.
  - **Astra's reports.** Ideas and watches are analysis for you to judge, never signals.

## Folder layout

```
Forex_Bot_Trader/
├── src/
│   ├── gold_monitor.py          the signal bot (main program)
│   ├── astra_memory.py          market memory, zone sensor, learning history (scores Astra's ideas)
│   ├── astra_analysis.py        Astra's report format, validation, Telegram texts, trader playbook loader
│   ├── astra_anthropic.py       optional Claude analyst backend (docs/CLAUDE_ANALYST.md)
│   ├── user_plans.py            your own /idea plans sent on Telegram
│   ├── gold_smc_engine.py       BOS/CHoCH, liquidity sweeps, FVG, order blocks, Fibonacci/OTE
│   ├── gold_confluence_v5.py    EMA/RSI/MACD, RSI divergence, breaker candidates
│   ├── gold_evidence.py         evidence snapshot for Astra + Python verification of every setup
│   ├── auto_trader.py           the demo auto-trader (separate program, docs/AUTO_TRADER.md)
│   └── autotrader/              the auto-trader's modules
├── tools/
│   ├── astra_dry_run.py         one full Astra analysis on real candles, nothing sent to Telegram
│   ├── learn_lessons.py         teach Astra from your chart images (lessons/ → playbook)
│   ├── check_claude_key.py      check that the Anthropic key works
│   ├── idea_scorecard.py        score Astra's past ideas from the memory database
│   ├── replay.py                historical replay of the auto-trader rules
│   ├── scalp_replay.py          replay of the scalp candidate
│   ├── weakness_replay.py       replay of the 4H-weakness strategy
│   └── mt5_calendar_to_excel.py optional: view the MT5 calendar in Excel
├── mql/
│   └── mql5/GoldCalendarBridge.mq5   MT5 Expert Advisor that exports the USD calendar (required)
├── config/
│   ├── gold_v5_config.json           your signal-bot settings (used by the scripts)
│   ├── gold_v5_config.example.json   reference copy of the defaults
│   ├── auto_trader_config.json       auto-trader settings
│   ├── secrets.env                   your keys (copy secrets.env.example; never committed)
│   └── astra_instructions.md, auto_trader_prompt.md   the analysts' role texts
├── lessons/                     your teaching charts + astra_playbook.json (used live)
├── docs/                        detailed guides (see the end of this file)
├── scripts/run.ps1              start the signal bot
├── scripts/selftest.ps1         run the signal bot's offline tests
├── scripts/run_trader.ps1       start the auto-trader (also trader_check / trader_report / trader_selftest)
├── data/                        optional extras (calendar Excel view)
├── tests/                       offline tests (no MT5, network, OpenAI or Telegram needed)
├── requirements.txt
└── CHANGELOG.md
```

The bot creates these folders by itself. You can delete them at any time; they are rebuilt.
- `runtime/`: live state (journal, market map, Astra memory and analysis).
- `runtime_dryrun/`: dry-run output.

## First-time setup (Windows)

The `MetaTrader5` Python package only works on Windows, on the same PC (or Windows VPS) as the MT5 terminal.

```powershell
cd Forex_Bot_Trader
py -m venv .venv                # once; any Python 3.10+ that MetaTrader5 supports
.\.venv\Scripts\python.exe -m pip install -U -r requirements.txt
.\scripts\selftest.ps1          # must end with "OK"
```

1. **MT5:** log in to your GivTrade account, put `XAUUSD` in Market Watch, and keep **Algo Trading disabled**.
2. **Calendar EA:** in MetaEditor, compile `mql/mql5/GoldCalendarBridge.mq5` (F7) and attach it to any chart. The Experts tab should print `Calendar bridge: published …` every 5 minutes. Leave `RefreshSeconds` at 300; anything above 600 makes the calendar look stale and blocks signals. Full guide: `docs/GIVTRADE_SETUP.md`.
3. **Keys:** copy `config\secrets.env.example` to `config\secrets.env` and fill in your values
   (`OPENAI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, …). The bots read this file at startup and it is
   never committed. Alternatively set the same names once with `setx` and open a new PowerShell window.
4. **OpenAI budget:** set a monthly limit in your OpenAI account (Billing → Limits). A live week costs roughly $50–70 at the default hourly analysis.

## Every day

```powershell
.\scripts\run.ps1
```

Start it any time. When the market is closed, the bot waits and stops paid analysis. Gold trades Sunday 18:00 to Friday 17:00 New York time, with a daily 17:00–18:00 break. To stop, press `Ctrl+C`.

The first Telegram message starts with `🟢 GOLD — ONLINE`, followed by Astra's first report.

## Before going live: dry run

The dry run runs one complete Astra analysis on real closed candles and writes a readable report. It never sends to Telegram.

```powershell
.\.venv\Scripts\python.exe tools\astra_dry_run.py                     # market open
.\.venv\Scripts\python.exe tools\astra_dry_run.py --offset-hours 3    # market closed (broker time − UTC)
.\.venv\Scripts\python.exe tools\astra_dry_run.py --no-ai             # free: data + memory only
```

Read `runtime_dryrun\report.txt`. It shows the candles, the remembered zones (`zones.csv` lets you compare them with your own chart), the payload size and token cost, Astra's validated answer and the exact Telegram texts.

## Teaching Astra (lessons)

Put a chart image and a `.txt` file with the same name in `lessons/`, then run:

```powershell
.\.venv\Scripts\python.exe tools\learn_lessons.py
```

The `.txt` file holds an optional `TYPE: CONCEPT` line, then `QUESTION:` and `MY VIEW:`.
1. Astra first answers blind, without seeing your view.
2. It then compares its answer with yours.
3. Your rules are merged into `lessons/astra_playbook.json`, which the live bot uses from its next hourly analysis.

Lessons already studied are skipped, so only new or edited files cost anything. Results are in `lessons/LESSONS_REPORT.md`. Full guide: `docs/TEACHING_SESSIONS.md`.

- Use **concept lessons** (`TYPE: CONCEPT`) on labelled charts to teach definitions.
- Use **trade lessons** on clean charts to test whether Astra finds the setups itself.

## Main settings (`config/gold_v5_config.json`)

| Setting | Value | Meaning |
|---|---|---|
| `telegram_language` | `Arabic` | `Arabic` or `English` |
| `report_timezone` / `report_hour` | `Europe/London` / 8 | daily report time |
| `map_refresh_seconds` | 3600 | Astra analysis every hour (main cost driver) |
| `zone_entry_refresh` / `zone_entry_cooldown_seconds` | true / 7200 | early analysis when price enters a remembered zone (once per zone per 2 h) |
| `astra_reports` / `astra_report_min_interval_seconds` | true / 1800 | TRADE IDEA and STRUCTURE WATCH reports, at most one of each kind per 30 min |
| `minimum_rr` | 2.0 | minimum reward:risk for BUY/SELL signals |
| `news_before_minutes` / `news_after_minutes` | 30 / 30 | no signals or ideas around high-impact USD news |
| `providers` | MT5 for candles, quotes and broker checks; `mt5_csv` calendar; `openai_web` news | data sources |
| `provider_options.broker_symbol` | `XAUUSD` | exact MT5 symbol |
| `provider_options.expected_mt5_server` | `GIVTrade-*` | the bot refuses any other account server |
| `provider_options.mt5_server_utc_offset_minutes` | `auto` | broker clock offset, detected from live ticks (follows DST) |
| `broker_max_spread` | 0.50 | maximum spread in $/oz for a signal |
| `playbook_file` | `lessons/astra_playbook.json` | your teaching playbook (empty = off) |
| `state_directory` | `runtime` | where live state is kept |

**Keep the project folder out of OneDrive** if you can, for example in `C:\Bots\Forex_Bot_Trader`. The bot now tolerates short file locks, but synced folders slow it down.

## Safety

- The signal bot has no order code, in Python or MQL: its MT5 access is limited to a fixed list of read-only calls. Only the separate demo auto-trader (below) sends orders. Keep Algo Trading disabled in the signal bot's terminal as a second safeguard.
- Missing, stale or inconsistent data blocks BUY/SELL signals: quotes, candles, calendar, news, broker server, clock offset or tick continuity. The bot fails closed.
- If the MT5 terminal restarts, the bot reconnects by itself within about 30 seconds.
- Astra's ideas carry a confidence and a risk class. They are analysis, not instructions. The bot scores every idea afterwards (played out, failed, not triggered), and the daily report shows the track record. Paper-trade the ideas until that record shows they are worth following.
- Passing tests does not prove profitability.

## Optional: demo auto-trader

`src/auto_trader.py` is a separate program that places orders on a **GivTrade demo account**,
using this bot's MT5 connection, broker clock offset, candles, calendar, news lock, market hours,
spread limits, Python SMC evidence, lessons playbook and Claude client. The signal bot above is
unchanged and still never trades. Setup, safety rules and daily use: `docs/AUTO_TRADER.md`.
Since 1.6 it also classifies the market state (trend layers, phase, volatility, momentum,
expansion), uses session levels, and ships a historical replay (`scripts/run_replay.ps1`) that
compares the old and new rules on your MT5 history. Later versions added the lab track (rule
experiments at the minimum lot) and trade protection; see `docs/AUTO_TRADER.md` and `CHANGELOG.md`.

## More documentation

| File | What it covers |
|---|---|
| `docs/ASTRA_V6.md` | market memory, zone sensor, ideas/watches, learning history |
| `docs/TEACHING_SESSIONS.md` | lessons in detail |
| `docs/GIVTRADE_SETUP.md` | MT5 + calendar EA setup and live checks |
| `docs/V5_SMC_SIGNAL_POLICY.md` | exact BUY/SELL signal rules (SMC playbooks, evidence, broker checks) |
| `docs/AUTO_TRADER.md` | the separate demo auto-trader |
| `CHANGELOG.md` | every version, newest first |
