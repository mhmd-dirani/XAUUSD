# Auto-trader (DEMO)

A second program in this project that **places orders on a GivTrade demo account**. It runs next
to the V6 signal bot, uses the same MT5 data layer and settings, and does not change the signal
bot: `gold_monitor.py` still never trades.

```
        your main MT5 terminal  ──►  V6 signal bot (alerts only, unchanged)
              │  GoldCalendarBridge.mq5 → Common\Files\gold_calendar.csv
              ▼                                     │ (Common folder is shared by every terminal)
        demo MT5 terminal (Algo Trading ON)  ◄──────┘
              │
              ▼
src/auto_trader.py
  market.py    V6 MT5Session (server check, auto clock offset), MT5Candles (UTC, weekend D1 merge),
               MT5CSVCalendar + calendar_block (news/holiday lock), Sessions (NY hours),
               gold_evidence (Python SMC evidence), your lessons playbook
  analyst.py   Claude proposes NO_TRADE / WATCH / A_TRADE / B_TRADE with an exact order
  validator.py hard rules: sides, R:R ≥ 2, stop vs ATR, V6 spread / slippage / quote-age limits,
               cited Python evidence must exist and agree, 4H/1H not opposed, loss limits, sizing
  executor.py  order with SL + TP on the broker server, expiry in broker time, TP1 partial + breakeven
```

## Where the trades come from (`source`)

**`"signal_bot"` (default): one Opus analysis for both bots.** The trader makes **no Claude calls**.
It reads what the signal bot already produces in `runtime\`:

| From the signal bot | Becomes | When |
|---|---|---|
| Confirmed BUY/SELL signal (`runtime\signals.jsonl`, written the moment it passes the final check, before Telegram) | MARKET order at the live price, grade A/B from the signal bot | within ~2 s |
| TRADE IDEA (`runtime\astra_analysis.json`, after every Opus map) | **confirmation mode (default):** the trader waits for a closed 15M rejection candle at the idea's zone (SELL: touches the zone, closes back below it, bearish, upper wick >= body; BUY mirrored), then enters at market if 2R is still there, otherwise a limit at the exact 2R price for `confirm_retest_hours` (4). A candle through the stop first cancels the idea. `idea_entry_mode: "limit"` places the pending order at once instead | at the 15M close that confirms |

Every order still passes the trader's own checks (2R, 4H/1H veto, spread, news lock, duplicates,
total risk, loss limits). Ideas below `idea_a_grade_confidence` (60 %) or with risk HIGH trade at
**B size**; there is no minimum confidence by default (`idea_min_confidence`: 0). An idea whose
price is currently inside its zone is retried while the analysis is fresh; a new analysis repeating
the same idea is a duplicate, not a second order. **The signal bot must be running**: with no
analysis newer than `idea_max_age_minutes` (120) the trader opens nothing and sends one Telegram
error per hour. Turn parts off with `signal_bot_signals` / `signal_bot_ideas`.

**`"own_analysis"`:** the trader calls Claude itself (its own prompt, hourly + zone triggers),
independent of the signal bot. Costs a second set of Claude calls.

## What it reuses from V6 (and so behaves identically)

| Thing | From | Setting in `config/gold_v5_config.json` |
|---|---|---|
| Broker symbol | `provider_options.broker_symbol` | `XAUUSD` |
| Broker check | `MT5Session.verify_server` | `expected_mt5_server` = `GIVTrade-*` |
| Broker clock offset | `MT5Session.offset_seconds` (live ticks, re-checked hourly, follows DST) | `mt5_server_utc_offset_minutes` = `auto` |
| Candles | `MT5Candles` (UTC, closed only, weekend daily merge, ≥210 bars) | `providers.candles` = `mt5` |
| Calendar | `MT5CSVCalendar` (fails closed if stale or inconsistent) | `providers.calendar` = `mt5_csv` |
| News lock | `calendar_block` (major USD releases, holidays all day) | `news_before_minutes` / `news_after_minutes` |
| Market hours | `Sessions` | `daily_close` / `daily_open`, `closed_dates` |
| Spread & cost limits | same numbers as the signal gate | `broker_max_spread`, `broker_max_spread_stop_fraction`, `broker_slippage_allowance`, `broker_max_combined_stop_fraction`, `broker_quote_max_age_seconds`, `minimum_stop_distance` |
| Minimum R:R, MTF veto | | `minimum_rr`, `block_on_mtf_opposition` |
| Your taught rules | `astra_analysis.load_playbook` | `playbook_file` |

Change those in the V6 config and both bots follow. Trading-only settings are in
`config/auto_trader_config.json` (grades, risk %, limits, analysis timing, Claude model).

## Install (once)

1. **Copy** the new files into your project folder, keeping the folders (see the list at the end).
2. **Signal bot running.** In the default `signal_bot` mode, start `scripts\run.ps1` too (same
   terminal is fine). **Nothing to install.** The trader uses the `anthropic` package your Claude analyst already
   needs (`requirements.txt`).
3. **Demo terminal.** Use the separate MT5 install logged into the GivTrade demo
   (for example `C:\MT5_Demo`). Turn **Algo Trading ON in that terminal only**. Then:
   ```powershell
   setx TRADER_MT5_PATH "C:\MT5_Demo\terminal64.exe"
   ```
   Optional, if you want the trader to log that terminal in itself:
   `setx TRADER_MT5_LOGIN "12345678"`, `setx TRADER_MT5_PASSWORD "..."`, `setx TRADER_MT5_SERVER "GIVTrade-Server"`.
4. **Keep the V6 bot on your main terminal.** GivTrade demo and live both use the server name
   `GIVTrade-Server`, so V6's server check cannot tell them apart. Point V6 at the main terminal:
   ```powershell
   setx MT5_PATH "C:\Program Files\GIVTrade MetaTrader 5\terminal64.exe"   # your main terminal's path
   ```
5. **Calendar.** Nothing new to install if `GoldCalendarBridge.mq5` already runs in your main
   terminal: it writes to MT5's shared Common folder, which the demo terminal reads too. If the main
   terminal is not running, attach the EA to a chart in the demo terminal instead (one terminal only).
6. **Claude key:** the trader uses the same `ANTHROPIC_API_KEY` (and `ANTHROPIC_WORKSPACE_ID`, if
   your key needs it) and the same client code (`astra_anthropic.make_client`) as your V6 Claude
   analyst, so if `tools\check_claude_key.py` works, the trader works. Its calls add to the same
   Anthropic bill: keep the monthly spend limit in the Console. Telegram error alerts reuse your
   existing `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`.
7. Open a **new** PowerShell window (setx only applies to new windows) and run the tests:
   ```powershell
   .\scripts\selftest.ps1          # V6: must end with OK
   .\scripts\trader_selftest.ps1   # auto-trader: must end with OK
   ```

## First run

```powershell
.\scripts\trader_check.ps1               # connection, DEMO, clock offset, price, calendar, hours. No Claude, no orders
.\scripts\run_trader.ps1 --once --dry-run   # ONE real analysis; prints what it would send, sends nothing
.\scripts\run_trader.ps1                 # trade the demo account
```

`trader_check` must show `DEMO`, `Algo Trading ON`, `Broker clock UTC+3.0h` (UTC+2 after the US
clock change), a live price and `Calendar OK`. Fix anything else before trading.

## Every day

- `.\scripts\run_trader.ps1`, and leave the window open (PC must not sleep; a Windows VPS is best).
- Log: `runtime_trader\trader.log`. Every Claude decision and every rejection reason is also in
  `runtime_trader\trader.sqlite3` (table `analyses`).
- Results: `.\scripts\trader_report.ps1` shows A and B separately (win rate, net $, average R,
  profit factor, drawdown) and writes `runtime_trader\trades.csv`.

## Claude model

By default the trader uses the **same model as your V6 analyst** (`anthropic_model` in
`gold_v5_config.json`, currently `claude-opus-5-5`, or `GOLD_CLAUDE_MODEL` if you set it), with
`anthropic_effort` "medium". To use a different model for trading only, set `anthropic_model` in
`config/auto_trader_config.json`. The instructions are cached between calls.

## Both directions (counter-trend)

Astra now evaluates BUY and SELL every analysis. Next to its main idea it adds a **counter-trend idea**
("Counter-trend: ...") when price is near a strong remembered Daily/4H zone on the other side, e.g.
a BUY at Daily support 4075-4080 while 4H/1H are bearish, with TP1 at the nearest structure above.
Both ideas stay valid together, each with its own stop.

The trader takes an idea against Python's 4H/1H structure only when ALL hold:
- the idea's entry overlaps a remembered `counter_timeframes` zone (Daily/4H/weekly) with the right
  role (support for BUY, resistance for SELL), status ACTIVE/FLIPPED, tested >= `counter_min_tests` (2);
- a closed 15M confirmation after the analysis: rejection candle, sweep beyond the zone that closes
  back inside it, or a confirmed 15M CHoCH in Python's evidence (always required, even in limit mode);
- 2R to TP1 from the live price (or the 2R retest limit), B size (0.25 %).
`counter_trend_enabled: false` switches it off. Confirmed signals from the signal bot keep the 4H/1H veto.

## v1.6: market state, session levels, playbooks, paper mode, replay

### Five separate classifications (Python, closed candles only, `src/autotrader/regime.py`)

| Field | From | Values |
|---|---|---|
| `macro_trend` | Daily + 4H structure | BULLISH, BEARISH, MIXED, NEUTRAL |
| `intermediate_trend` | 1H structure | BULLISH, BEARISH, NEUTRAL |
| `local_trend` | 15M structure | BULLISH, BEARISH, NEUTRAL |
| `phase` (+ direction) | how local relates to macro | CONTINUATION, PULLBACK, TRANSITION, REVERSAL, RANGE |
| `volatility` | last 8 15M ranges vs the day's median | EXPANDING, NORMAL, CONTRACTING |
| `momentum` (+ direction) | last 4 vs previous 4 closes | ACCELERATING, STEADY, DECELERATING, REVERSING, FLAT |
| `expansion` | strongest one-way leg of the last 4 h | active, direction, size, origin, extreme |

"4H bearish, 15M bullish CHoCH, 1H not yet" is phase TRANSITION BULLISH; once the 1H follows it is
REVERSAL BULLISH, before the 4H turns. An expansion is active when the leg is at least 6x the day's
median 15M range, its extreme is in the last 2 hours and less than half of it has been given back.

### Playbooks (`src/autotrader/playbooks.py`)

| Class | When | Default mode |
|---|---|---|
| TREND | not against the 4H/1H structure (v1.5 rule) | live |
| COUNTER_TREND | against it, at a strong remembered Daily/4H zone (v1.5 rule), B size | live |
| REVERSAL_TRANSITION | against it, no strong zone, but phase TRANSITION/REVERSAL this way and the latest 15M CHoCH this way, B size | **paper** |
| SESSION_SWEEP | against it, confirmed by a sweep / failed breakout of a session, opening-range or previous day/week level, B size | **paper** |

`playbook_modes` sets each one to `live`, `paper` or `off`. Python decides the class; Astra's own
`trade_class` (new idea field) is logged and journalled next to it, and a mismatch is reported.

### Expansion filter (`expansion_filter`, default **shadow**)

No trade against an active expansion unless the move has objectively failed or weakened, on closed
15M candles after its extreme. Any one of these validates the reversal (not sweep-only):
CHOCH (15M change of character), FAILED_BREAKOUT (back below the level the expansion broke),
BREAK_RETEST (close through the leg's last higher low / lower high, then a rejected retest),
SWEEP_RECLAIM (a wick through a prior high/low or session level and a close back inside).
`shadow` never blocks but marks the trades it would have blocked (journal, Telegram, report), so the
replay and the demo can show whether it helps; `paper` sends would-be-blocked trades to paper; `live` blocks.

### Session levels (`src/autotrader/sessions.py`)

Asia (Tokyo 09:00-15:00), London (08:00-16:30), New York (08:00-17:00), each in its own time zone, so
the different London/New York DST dates are handled. Opening ranges: first 60 min of London and NY.
Previous day/week use the New York 17:00 trading day. Events per closed 15M candle: SWEEP_RECLAIM,
BREAKOUT, FAILED_BREAKOUT. They confirm ideas (`SESSION_SWEEP` / `SESSION_FAILED_BREAKOUT`), serve as
targets, and drive Python's own session setups (`python_session_setups`, paper only).

### Paper trades

Paper trades are simulated on live quotes (same fill, cancel, expiry, TP1 + breakeven rules) and kept
in the `paper_trades` table; nothing reaches MT5. `trader_report` lists them per playbook and
confirmation. Telegram (`telegram_trades`) sends 📝 PAPER messages for them.

### Journal and Telegram

Every order now stores `playbook`, `confirmation`, `session`, `regime` (the full market state as JSON),
`astra_class` and `expansion_block`. Telegram trade messages (placed / filled / closed) show the same.
`trader_report` breaks results down by playbook, confirmation, session and phase.

### Historical replay (`tools/replay.py`, `scripts/run_replay.ps1`)

```
.\scripts\run_replay.ps1                 # 120 days of MT5 1-minute history, old vs new rules
.\scripts\run_replay.ps1 -Days 60 -Ticks # settle same-minute stop/target with MT5 ticks
python tools\replay.py --csv replay_data\XAUUSD_M1.csv --commission 7
```
No Claude calls, no orders. Candles are built from 1-minute bars on the broker clock and become
visible only once closed; fills happen on the next 1-minute bar; BUY fills and SELL exits use ask
(bid + recorded spread), plus slippage and commission. Four variants see the same candidates:
`OLD_v1.5`, `NEW_filter_only`, `NEW_playbooks_no_filter`, `NEW_all`. The report (`replay_data\replay_report.md`)
gives trades, win rate, average R, net R, profit factor and drawdown per variant, in-sample vs the last
30 % (out-of-sample), breakdowns by class, confirmation, direction, session, phase, volatility and
momentum, and the **trade-off**: which OLD trades the new rules removed (winners and losers) and what
they added. MT5 limits 1-minute history to "Max bars in chart" (Tools > Options > Charts): set it to
Unlimited and scroll an M1 chart back if fewer days come back than asked.

What the replay cannot do: replay Astra's past ideas (Python's zones play that role), apply the news
lock (no calendar history), or know the order inside one minute without ticks (counted as a loss).

**Switching a playbook to live** = set it to `"live"` in `playbook_modes` after the replay and 30+
closed paper trades show a positive average R after costs.

## Lab mode (1.10) — testing the rules on the demo account

The live track keeps every rule. The lab track is the experiment: it takes every idea the live track did
not trade, at the minimum lot, and records which rules the trade broke. After enough trades the report
shows which rules actually cost money and which were theory.

Turn it on in `config\auto_trader_config.json`:

```json
"lab_mode": true,
"lab_max_open": 6,
"lab_max_per_day": 20,
"lab_drawdown_kill_pct": 15.0,
"lab_unconfirmed_entry": true
```

then restart `scripts\run_trader.ps1`. `scripts\trader_check.ps1` shows `Lab mode ON`.

What you will see:
- Log channel: `🧪 LAB SELL LAB-SELL-4013 SELL_LIMIT @ 4009.0 | SL 4024.5 | TP1 3977.0 | R:R 2.07 | 0.01 lots`
  followed by `violations: session_off, mtf_opposed, counter_no_zone, class_off, no_confirmation`.
- The usual FILLED / CLOSED / REVIEW messages for lab trades (setup ids start with `LAB-`).
- Nothing on the signals channel.
- `scripts\trader_report.ps1`: a LAB section per rule, e.g.
  `session_off  broke  14 avg -0.42R | kept  22 avg +0.31R | diff +0.73R (< 20 samples)`.

How to read it: `diff` is what keeping the rule was worth per trade. A rule earns its place when `diff` is
clearly positive over 20-30 samples. Rules overlap, so read the "violation sets" lines before judging one rule.
Money results of lab trades mean nothing (0.01 lot); only R and hit rates count.

What the lab never does: trade a real account, trade on stale data, trade through a spread above
`broker_max_spread`, place two lab orders on the same setup in one day, or keep going after
`lab_drawdown_kill_pct` of the balance is lost in a week. The STOP file stops it like everything else.

## Account-wide risk

Total risk is read from the **broker**: every open position and pending order on the account (this
bot, the signal bot's future scalper, other bots, manual trades), volume x distance to its stop. A
stop past the entry counts 0. Any position or order **without a stop** makes the exposure
unmeasurable, so no new order is placed until it has one. `trader_check` prints the current figure.
Magic numbers: `magic` 40001 = swing engine (trend, counter-trend, signals); `magic_scalp` 40002 is
reserved for the 1M scalper, which is not active.

## Several orders at once

Claude may propose up to two different orders in one analysis (for example a SELL_LIMIT at
resistance and a second SELL_LIMIT higher up, or a SELL at resistance and a BUY at support).
Each is validated on its own and gets its own grade and risk (A 0.5 %, B 0.25 %).

| Setting | Default | Meaning |
|---|---|---|
| `max_active_orders` | 2 | pending + open orders of this bot together |
| `max_orders_per_analysis` | 2 | new orders from one analysis |
| `max_total_risk_pct` | 1.0 | risk of everything on the account (from the broker); a trade at breakeven counts 0 |
| `duplicate_atr` | 0.5 | same direction with entry AND stop within 0.5 x ATR15 of a working order = duplicate, rejected |
| `max_orders_per_setup_per_day` | 2 | the same setup may be traded again once its earlier order finished |
| `max_trades_per_day` | 4 | |

On a **netting** account (one position per symbol) it automatically drops to one order at a time.
GivTrade MT5 accounts are normally hedging accounts; `trader_check` shows the limit it will use.

## When it analyses (each analysis = one Claude call, about 24k input tokens)

- On the hourly 15M close (`analysis_every_minutes`: 60), and early when price reaches the zone of a
  setup Claude is watching (`zone_trigger`, once per setup per 45 min).
- **Never** while no order could be placed: market closed, first 30 min after the open, last 20 min
  before the close, news/holiday lock, calendar stale, stale price, a position or pending order
  already working (2 orders), 4 trades today, −2 % today or −5 % this week, kill file. No API cost then.

## Stopping

- `Ctrl+C`: stops the program. Orders and positions stay at the broker with their stop and target;
  pending orders still expire at the broker.
- Pause new orders but keep managing open ones: create `runtime_trader\STOP`
  (`New-Item runtime_trader\STOP`). Delete it to resume.

## Safety

- Refuses to start or trade unless MT5 reports a **DEMO** account (`require_demo`).
- Refuses to start without `TRADER_MT5_PATH`, so it never attaches to your main terminal.
- Claude never chooses the lot size and its R:R is ignored; Python recomputes everything.
- Every order must cite Python-verified evidence ids that exist, are usable and point the same way.
- A bad, cut-off or inconsistent Claude answer becomes NO_TRADE.
- Going live later means changing `require_demo` deliberately, after 30+ closed trades per grade.

## Files added

```
src/auto_trader.py                 entry point (--check, --dry-run, --once, --report, --self-test)
src/autotrader/                    settings, market, analyst, validator, executor, state, report, app, alerts,
                                   regime, sessions, playbooks, paper, replay (v1.6)
tools/replay.py                    historical replay, old vs new rules (v1.6)
config/auto_trader_config.json     trading settings (copy of .example.json)
config/auto_trader_prompt.md       Claude's instructions
scripts/run_trader.ps1  trader_check.ps1  trader_report.ps1  trader_selftest.ps1  run_replay.ps1
tests/test_auto_trader.py  tests/test_v16.py   offline tests
docs/AUTO_TRADER.md                this file
```
Edited: README.md (one section + one table row), CHANGELOG.md (one entry), .gitignore (`runtime_trader/`).
No V6 program file is changed.


## 1.7 - tiers and the post-trade review

**Why.** Until 1.6 every idea from the signal bot was traded at B size, while Astra's contract makes it
always produce an idea. The result on the demo: three counter-trend SELLs in the Asian session, all stopped,
and zero confirmed A/B signals in three days. Nothing turned a correct read into a sized position.

**Tiers (`src/autotrader/tiers.py`, config `tiers`).** After an idea is confirmed, Python grades it from
five facts it checks itself:

| fact | source |
|---|---|
| with_trend | idea direction equals the macro trend (Daily + 4H, closed candles, regime.py) |
| session | London or New York (sessions.py) |
| anchored | stop AND TP1 sit on remembered levels (sl_anchor / tp_anchors from the signal bot) |
| no_expansion | no strong one-way expansion against the idea (regime.py) |
| calm_risk | Astra's own risk label is not HIGH |

Tier A = all five and confidence >= 40 % -> 1.0 % of equity. Tier B = one missing, >= 35 % -> 0.5 %.
Tier C = two or more missing, or any counter-trend / reversal-transition idea, >= 30 % -> 0.15 % (one
minimum lot is allowed when 0.15 % is below it, within 0.5 %). Below 30 %: no trade. Against an
expansion without a validated reversal: Tier C in `expansion_filter: shadow`, no trade in `live`.
Confidence is Astra's probability that TP1 is reached before the stop; at 2R, 40 % is +0.2R expectancy.

Every order carries `tier`, `tier_facts` and `tier_reason` in the journal and the Telegram text, so the
review can say which fact was missing on the trades that lost.

**Review (`src/autotrader/review.py`).** When a trade's result is known, Python reconstructs it from the
closed 15M candles: outcome (STOP_WICK / STOP_CLOSE / TP1 / TP2 / BREAKEVEN), MFE and MAE in R, what
price did in the 4 h after the exit, and a verdict: RIGHT_IDEA_WRONG_STOP (stopped, then TP1 was reached),
WRONG_IDEA, GOOD_EXIT, LEFT_MONEY. `python src\auto_trader.py --report` groups reviews by tier, verdict,
session and playbook and lists "losers by missing fact". The review changes no rule; after 50-100 trades
it tells you which tier thresholds and facts to change, and it is the input for the next teaching session.

**Removed.** Paper mode, the loss cool-down idea, Python-only session sweeps (`python_session_setups: off`).

**Direction rule.** One position or order at risk per direction (`max_same_direction_positions`); a
position whose stop is at breakeven or better does not count.

**Run.**
```
python src\auto_trader.py --self-test
python src\auto_trader.py --check
.\scripts\run_trader.ps1
```


## 1.8 - BREAKOUT_CONTINUATION

A trend-day entry that does not wait for a pullback. Trigger: the first closed 15M candle through a session,
previous-day or previous-week level in the direction of the macro (Daily+4H) and 1H trend, London/NY only.
Entry at market on the close if the first opposing level >= 2R away allows it, otherwise a short-lived limit
at the 2R price. Stop just beyond the broken level. Tier capped at C (`tier_cap`). Off by default
(`playbook_modes.BREAKOUT_CONTINUATION`); run `python tools\replay.py --days 30 --ticks --risk 0.15` first and
compare the `NEW_all_plus_breakout` and `BREAKOUT_only` variants with `NEW_all` in the report.
