# Changelog

## Auto-trader 1.10.5 — lab orders at live size (28 Oct 2026)

### Added
- `lab_risk_pct` (default 0 = minimum lot). When set, every lab idea order is sized like the live track:
  equity x pct / stop distance, floored to the lot step, never under the minimum lot, capped at `tiers.A.risk_pct`.
- `lab_max_total_risk_pct` (1.5): the lab's working orders together (from the broker's own stops) may not risk
  more than this share of equity; the next lab order is held and logged until something closes. The weekly
  `lab_drawdown_kill_pct` (15 %) still applies on top. The news straddle keeps `lab_lots` for now.
- `--check` prints the lab sizing in force.

### Tests
- `test_v110.LabSizing`: minimum lot by default, equity x pct / stop, minimum-lot floor, open-risk cap holds the
  next order, settings refuse a percentage above Tier A or a cap below the percentage. Suite: 184 tests.

## Auto-trader 1.10.4 — notification crash after a live order (28 Oct 2026)

### Fixed
- **"Order send failed: list index out of range" for an order the broker had accepted.** IDEA-BUY-4031 (TP1 4057.44,
  TP2 4064.74, no TP3) was placed and journalled, then `alerts.signal_text` read `targets[2]` and raised. The app
  reported a send failure, Telegram got no PLACED message, and the idea was marked as not sent, so the lab could
  take it as well. `signal_text` now handles 1-3 targets, and every trade notification goes through
  `alerts.safe_trade`, which logs a warning instead of raising. Behaviour of orders is unchanged.

### Tests
- `test_v110.NotificationTests`. Suite: 180 tests.

## Auto-trader 1.10.3 — lab news straddle (27 Oct 2026, for the 28 Oct FOMC)

### Added (lab only, demo only, `news_straddle: false` by default; needs `lab_mode: true`)
- `autotrader/straddle.py`: `straddle_place_minutes` (2) before each listed HIGH USD release (`straddle_events`:
  Fed rate decision, FOMC statement, federal funds, NFP, CPI, core PCE, GDP) the lab places, while the spread is
  still normal, a BUY_STOP above and a SELL_STOP below the price (offset 0.5 x ATR15, at least 3 x spread) plus an
  "add" stop order 1 x ATR15 further out on each side. Stop 1 x ATR15 (at least $3) from each entry, TP1 1R (half
  closed, stop to entry: the executor's normal TP1 rule), TP2 3R. The broker fills them in the move.
- Every bridge poll (~2 s) while a straddle is active: as soon as one side has a position, all pending orders of
  the other side are cancelled; if both sides filled (whipsaw), all remaining pendings are cancelled. Unfilled
  orders expire 15 min after the release; anything still open 30 min after the release is closed at market.
  Straddles still working after a restart are picked up from the broker (order comments).
- Orders are lab orders (magic 40003, tags `news_straddle`, `news_lock`, playbook NEWS_STRADDLE): reviewed and
  reported like any lab trade, never counted on the live track. They bypass `lab_max_open` (their own count),
  keep every other lab hard check (demo, connection, fresh quote, spread cap at placement, STOP file, weekly kill).
- `--check` shows the straddle status.

### Tests
- `test_v110.StraddleTests` / `StraddleEndToEnd`: event selection (FOMC yes, EIA and the press conference no),
  order geometry, ATR floor, OCO including the whipsaw case, settings refusals, one placement per event,
  time exit, real account refused. Suite: 178 tests.

## Auto-trader 1.10.2 — news-candle confirmation (27 Oct 2026, before FOMC)

### Fixed
- **A candle formed inside the news lock confirmed an order one second after the lock.** 27 Oct: the 12:30-12:45
  Durable Goods candle "confirmed" Astra's SELL 4026-4031; the order went in at 13:00:01 (lock 12:00-13:00).
  The live track now looks for confirmations only on candles that OPENED after the most recent lock ended
  (`Market.last_lock_end`, same "major" rule and minutes as the calendar lock, 6 h lookback). An invalidation
  (stop traded) inside the lock still kills the idea. Open positions are unchanged. The lab is unchanged
  (it already tags `news_lock`).
- The 1.10 "session sweep ... ignored" log line is written once per idea / level / candle, not every 2 s.

### Tests
- `test_v110.NewsCandleTests`: lock end after Durable Goods, still-inside = None, 6 h lookback, FOMC matched by
  name, a post-lock window ignores the spike candle. Suite: 172 tests.

## Auto-trader 1.10.1 — first lab night fixes (27 Oct 2026)

### Fixed
- **Lab duplicates.** The "one sample per setup per day" rule used the UTC date, so U1 and U2 were taken at 22:44
  and again at 02:26 (new UTC day), and both U1 trades stopped on the same tick: one sample counted twice.
  Now one working lab order per setup at a time (`State.lab_active_for_setup`), and one per New York trading
  day (`lab._trading_day0`, 17:00 NY), the same day the broker's D1 and the signal bot use.
- **Lab trades taught Astra.** The lab's U2 close (reasons: "Check", 1-point stop) wrote `lessons/U002_2026-10-26.txt`,
  ran a teaching session (3 AI calls) and produced principle P12 in `astra_playbook.json`. Lab orders no longer
  write lessons or start teaching sessions. Remove P12 and the lesson by hand (see the chat / docs).
- **Telegram.** Lab orders posted twice on the log channel (standard PLACED/FILLED text plus the 🧪 line) and
  their fills/closes went to the signals channel. Now: one 🧪 line on placement, fills/closes/reviews on the log
  channel only, nothing on the signals channel.

### Tests
- `test_v110.LabDedupe`: same setup twice within one trading day across the UTC midnight is refused; a closed
  sample on the next trading day is allowed. Suite: 170 tests.

## Auto-trader 1.10.0 — lab mode, confirmation-candle fix (27 Oct 2026)

### Fixed
- **Session sweep confirmed a SELL on a bullish candle.** `sessions.events` reported SWEEP_RECLAIM / FAILED_BREAKOUT
  for any candle closing back through the level, even a 14-point bullish bar closing 0.10 below it (26 Oct 14:30,
  o 4007.89 h 4023.08 c 4022.24, London OR high 4022.34). A SWEEP_RECLAIM now needs a rejection candle: the wick
  beyond the close at least as long as the body (`sessions.rejection_wick`, the same rule the idea REJECTION
  uses). The 14:30 bar had a 0.84 wick on a 14.35 body. Small bullish candles with a long upper wick still count
  (the 1.6/1.7 fixtures pass unchanged). `App.session_confirmation` additionally requires the sweeping candle to
  close back at the idea's zone (`bridge.sweep_bar_ok`): SELL close <= entry_high, BUY close >= entry_low.
  Note: `tools\replay.py` uses the same function, so its SESSION_SWEEP counts can change slightly.
- **Confirmed idea entered at market outside its zone.** `bridge.confirmed_order` placed a MARKET order wherever
  price was after the confirmation (26 Oct 14:45: U1 zone 4009-4016 at 4021.74, 2.8 from the stop; only the
  ATR floor rejected it). A market entry now needs price inside the zone +/- `confirm_entry_tolerance` (3.0);
  otherwise a limit at the zone edge, or no order when price is on the wrong side of the zone.
- **Owner plans with a 1-point stop were accepted** (U2, 26 Oct). `user_plans.check` now rejects a stop narrower
  than 0.25 x 1H-ATR from the zone edge (signal bot 6.2.1).
- TP1 partial close on a position keeps the position's own magic number.

### Added — LAB MODE (demo only, `lab_mode: false` by default)
- `autotrader/lab.py`: every idea the live track does NOT trade (session gate, counter-trend without a zone,
  R:R below 2, waiting for confirmation, no tier, risk-engine rejection ...) is traded at the broker's minimum
  lot under `magic_lab` (40003), TP1 only, with the list of rules it broke stored on the order
  (`orders.violations`, `orders.lab = 1`). Tags: see `lab.TAGS`.
- Hard checks that stay on in the lab: demo account, terminal connected + Algo Trading on, market open, fresh
  quote, absolute spread cap, broker stop level, executable sides, one lab order per setup per UTC day,
  `lab_max_open` (6), `lab_max_per_day` (20), `lab_drawdown_kill_pct` (15 % of balance per week), STOP file.
- Lab orders never touch the live track: separate magic, excluded from the live book count, duplicate check,
  daily cap, loss limits and the analyst's constraints. The existing executor manages them (fills, expiry,
  cancel levels, results, 1.7 reviews). Lab orders post to the log channel only, never to the signals channel.
- `--report`: live results exclude lab trades; a new "LAB" section shows, per rule, the average R of the lab
  trades that broke it vs the ones that kept it, plus the most common violation sets. `trades.csv` gains the
  columns `lab` and `violations`.
- `--check` prints the lab status. `settings.load` refuses `lab_mode` with `require_demo: false` and a
  `magic_lab` equal to another magic.
- New settings: `lab_mode`, `magic_lab`, `lab_lots`, `lab_max_open`, `lab_max_per_day`, `lab_drawdown_kill_pct`,
  `lab_unconfirmed_entry`, `confirm_entry_tolerance`.

### Tests
- `tests/test_v110.py` (19 tests): sweep candle direction, zone close rule, in-zone market entry, lab tags for
  the 25-26 Oct cases, lab order plan, settings refusals, real-account refusal, journal separation, report
  section, owner-plan stop floor, lab end-to-end with fakes (live trade left alone, risk-engine reject taken).
  Registered in `--self-test`.

### Not covered yet
- Breakout candidates (`breakout_setups`) and signal-bot signals are not lab-traded in this version; only Astra
  ideas and owner plans are. Next step once the idea track has data.
- Live MT5 behaviour of the lab (fills at minimum lot, GivTrade retcodes) is unverified until the first run.

## Signal bot 6.2.0 / auto-trader 1.9.0 - the owner's plans (/idea on Telegram) and lessons from them
- Send `/idea SELL 4009-4016 sl 4024.5 tp 3977 3960 3940` plus one reason per line to the bot (private chat or
  the admin group; only TELEGRAM_ADMIN_IDS). Python checks sides, R:R >= 2 from the zone edge, stop size and
  distance at once and replies. The plan then rides in every map payload (`user_plans`, reasons included) until
  Astra's map answers it: an idea of the same direction overlapping the zone = APPROVED; an opposite idea there
  or no agreement for user_plan_max_hours (12) = REJECTED. Every step is replied to you and posted to the Log.
- APPROVED plans become ideas for the auto-trader (runtime/user_plans.json): the usual 15M confirmation in the
  zone, tiers (owner plans capped at Tier B), validator, demo. Journal column `user_plan`, strategy USER_PLAN.
- When such a trade is reviewed (4 h after it closes), the plan, your reasons, Astra's note and the review are
  written to lessons/U<id>_<date>.txt and tools/learn_lessons.py runs in the background
  (user_plan_auto_study, 3 analyst calls), so the principle enters the playbook Astra reads on every map.
- Nothing here places an order by itself: the plan is an idea, Python remains the final gate.
- tests/test_user_plans.py (7 tests); both self-tests include it. Set TELEGRAM_ADMIN_IDS in secrets.env.

## Signal bot 6.1.7 - ideas far from price are not posted
- `idea_post_max_distance_atr` (1.5): an idea whose entry zone is further than 1.5 x the 1H ATR from the
  current price is kept in astra_analysis.json and the learning history but not posted as a Telegram card;
  a later map that still carries it, with price near the zone, posts it then. Astra's "price is X away -
  wait" risk factor becomes `distance_atr` on the idea. 23 Oct 17:05: a BUY 22 points below price was posted.

## Auto-trader 1.8.1 - trade_sessions, the Monday configuration, three more replay variants
Replay of 24 Oct (90 days, 1,756 candidates): the live 1.7 configuration (NEW_all) lost -11.4R / PF 0.86,
driven by SESSION_SWEEP (43 trades, 23 %, -3.5R) and REVERSAL_TRANSITION (30, -2.8R); Asia lost in every
variant; counter-trend at strong zones (44-47 %) and SWEEP confirmations were the winners; BREAKOUT_only
+3.9R / PF 1.32 on 17 trades.
- `trade_sessions` (default LONDON, LONDON_NY, NEW_YORK): a gate for every playbook; no new order outside.
- Shipped config: TREND + COUNTER_TREND + BREAKOUT_CONTINUATION live (Tier C cap), SESSION_SWEEP and
  REVERSAL_TRANSITION off.
- Replay variants CORE (the Monday config), CORE_noAsia, CORE_noAsia_noRejection (no REJECTION candles).
  Run: python tools\replay.py --days 90 --risk 0.15 --offset 3 --out runtime_replay
## Auto-trader 1.8.0 - BREAKOUT_CONTINUATION (off until replayed) / signal bot 6.1.6
- New playbook BREAKOUT_CONTINUATION (`src/autotrader/breakout.py`): the first closed 15M candle through a
  session / previous-day / previous-week level WITH the macro (Daily+4H) and 1H trend, London/NY only, enters
  at MARKET on the close when the first opposing level >= 2R away allows it; otherwise a limit at the 2R price
  back toward the level (breakout_retest_minutes, cancelled if TP1 trades first). Stop just beyond the broken
  level (max 0.3 ATR15, 2 spreads), never at the candle's extreme. Nearer levels on the way to TP1 are kept in
  the journal (`path_levels`) for the review. Evidence = Python's own breakout detection.
  Why: 23 Oct 14:15 the trader waited for a retest at 4025 while price ran from 4008 to 3975; Astra's own
  record: breakout scenarios 54 %, rejection scenarios 18 %.
- Shipped `playbook_modes.BREAKOUT_CONTINUATION: "off"`. Switch to "live" only after the replay (below).
  `tier_cap: {"BREAKOUT_CONTINUATION": "C"}` keeps it at Tier C size until reviewed trades justify more.
- The same candidate generator runs in the replay tool: variants NEW_all_plus_breakout and BREAKOUT_only.
- Signal bot: `idea_post_min_confidence` (30) and `idea_post_min_rr` (1.5): weaker ideas stay in
  astra_analysis.json and the learning history but are not posted as Telegram cards (23 Oct 17:05: a 24 % /
  0.86R counter-trend BUY 22 points away was posted as a full idea).
- tests/test_v18.py (8 tests); the trader self-test includes it.
Replay before switching on (from the project folder, MT5 running, weekend is fine):
  python tools\replay.py --days 30 --ticks --risk 0.15 --out runtime_replay
  then read runtime_replay\report.txt: compare NEW_all vs NEW_all_plus_breakout vs BREAKOUT_only (count, win %,
  avg R, total R, max drawdown, out-of-sample 30 %). Unverified: live fills on GivTrade, profitability.

## Auto-trader 1.7.3 - the review waits for its window
- 23 Oct 14:05: IDEA-SELL-4020 was reviewed 50 minutes after its close, before the 4h-after window existed
  (and before the 14:00 collapse candle had closed), giving "4h after: NEITHER / WRONG_IDEA" where the full
  window shows TP1_REACHED / RIGHT_IDEA_WRONG_STOP (stop 4033.5 taken by the 4034.06 sweep, then -58 points).
  A review now runs only once closed_utc + 4h15 has passed; rows written earlier than that are redone at start.

## Signal bot 6.1.5 / auto-trader 1.7.2 - three Telegram channels, AI cost in the journal, soft budget
- Telegram channels (same bot token). TELEGRAM_CHAT_ANALYSIS: reports, ideas, watches, plan updates (Arabic,
  as before). TELEGRAM_CHAT_SIGNALS: the trading channel - confirmed signals and every trader order event as a
  short English block (direction, entry, SL, TP1-3, size), nothing else. TELEGRAM_CHAT_LOG: ONLINE/health,
  errors, rejections, reviews, the full order detail. TELEGRAM_CHAT_ID stays the fallback for any channel not
  set, so an unchanged environment behaves exactly as 6.1.4 / 1.7.1. If a channel had the group linked as its
  discussion group, Telegram copied every post there - unlink it in the channel settings.
- AI usage: every analyst call records input / output / cache tokens and the USD cost (config
  `ai_prices_per_million`, Anthropic list prices) as an AI_USAGE journal row; the log shows today's calls and
  spend after each call; the daily report ends with "AI: today N calls $x | month N calls $y".
- Map priorities and the soft budget: an early refresh is ALWAYS (structure broken/accepted/swept, a zone tested
  >= 2x), NORMAL (a weak zone) or LOW (abnormal 15M range with no zone, the same zone again within an hour).
  LOW never runs. NORMAL is deferred once the day's spend passes `ai_daily_budget_usd` (3.3 = $100/month).
  ALWAYS and the scheduled map are never throttled; purchased credits cover the volatile days.
  `ai_daily_hard_limit_usd` (0 = off) is the only thing that stops everything, as a backstop against a bug.
- Secrets file: both bots and the tools load `config/secrets.env` (NAME=VALUE, or pasted `setx NAME "VALUE"`
  lines) into the environment at startup; a value in the file wins over a stale setx value; names are logged,
  values never; the file is git-ignored and `config/secrets.env.example` is the template. Not loaded by --self-test.
- tests/test_channels_budget.py (14 tests), tests/test_secrets_file.py (4); the gold_monitor self-test now also runs test_ai_backoff and
  test_channels_budget. tests updated: the trader harness accepts the short text; the signal-file test expects the
  trading-channel post before the analysis post.
Environment to add (setx, then a new window):
  TELEGRAM_CHAT_ANALYSIS "@XAU_USD_Bot_Analysis"  TELEGRAM_CHAT_SIGNALS "@XAU_USD_Bot_Trading"
  TELEGRAM_CHAT_LOG "@XAU_USD_Bot_Log"   (the bot must be an admin of each channel)
Unchanged: when a BUY/SELL may be sent, every trading rule of the auto-trader.

## Auto-trader 1.7.1 / signal bot 6.1.4 - test harness
- The trader's test harness could post its fake orders to the REAL Telegram chat when
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID were set in the shell (22 Oct: ~20 "SELL_SUPPLY_001" messages).
  Base.setUp now blanks both variables and patches alerts.trade / alerts.error; `--self-test` blanks them too.
- Tier tests depended on the real clock (session NEW_YORK at 19:00 UTC, ASIA at 03:00): the harness pins
  sessions.session_of to NEW_YORK; test_idea_becomes_pending_order_once_without_claude expects the Tier B size.
- test_astra_anthropic: the worker now reports the failure reason (6.1.4), the test checks it.
- `--check` prints the tiers.
- Review of pre-1.7 trades (no fill_utc / closed_utc): the window ran to "now", so MFE/MAE covered a week.
  fill_utc / closed_utc are now backfilled from the position's deals before a review; a trade whose
  times are unknown is not reviewed; reviews made without a close time are dropped and redone.
- The calendar's "untimeable USD event" rejection now names the event, its importance, time mode and date.
- Calendar: a LOW-importance untimed event can no longer be promoted to "critical" by a word in its name.
  'Dallas Fed Trimmed Mean PCE Inflation Rate' (LOW, TENTATIVE, 29 Oct) matched "pce" and invalidated the
  whole export: no map, idea or trade ran from 16 to 22 Oct (100 DATA_FAILED rows). HIGH untimed events,
  and MEDIUM ones with a headline name (FOMC, CPI, PCE, NFP, Powell), still block as before. Regression test.

## Auto-trader 1.7 - tiers, the post-trade review, no paper mode
Evidence (runtime_trader/trader.log + trader.sqlite3, 14-16 Oct): the trader took every idea the signal
bot produced (idea_min_confidence 0, all risk labels) at B size while Astra's contract says "ALWAYS
give an idea"; the strict A/B signal gate sent nothing in three days. Three demo trades, all B size,
all counter-trend SELLs in the Asian session (00:05-00:45 UTC) stacked into one rally, all stopped.
- `tiers.py`: Python grades every idea from facts it verifies itself (direction vs the closed-candle
  macro trend, London/NY session, stop AND TP1 anchored on remembered levels, no expansion against,
  Astra risk not HIGH) plus a confidence floor per tier. Tier A 1.0 % / B 0.5 % / C 0.15 % of equity
  (config `tiers`). Tier C may use one minimum lot when 0.15 % is below it, within tiers.B.risk_pct.
  Counter-trend and reversal-transition ideas are Tier C at most. Confidence is Astra's probability
  that TP1 is reached before the stop: 40 % at 2R is a good trade and now sizes as one.
- `review.py` (feedback loop, after): every closed trade gets a deterministic review from the closed
  15M candles: STOP_WICK / STOP_CLOSE / TP1 / TP2 / BREAKEVEN, MFE/MAE in R, what price did in the
  4 h after the exit, verdict RIGHT_IDEA_WRONG_STOP / WRONG_IDEA / GOOD_EXIT / LEFT_MONEY, the tier
  facts that were missing. Stored in `trade_reviews`, sent to Telegram, grouped in the report by tier,
  verdict, session, playbook and "losers by missing fact". It changes no rule by itself.
- Direction rule: at most `max_same_direction_positions` (1) positions/orders AT RISK per direction;
  a position with its stop at breakeven or better frees the slot. No time-based cool-down.
- Paper mode removed (the demo account is the trial): playbook_modes are live / off, all four live by
  default at tier size; `python_session_setups` must be "off" (Python-only session sweeps return after
  replay); `expansion_filter` is off / shadow / live. A config with "paper" fails at start with a clear message.
- Carried from the 1.6.1 review: one session level per price (PDH == NEW_YORK_HIGH produced two identical
  setups on 16 Oct); the 2R retest limit must stay inside the idea's zone (I2 on 15 Oct needed 4206.62,
  above 4205.3); Telegram texts never show "None" / "0.00" / empty "|  |  |"; closed_utc and fill_utc
  recorded; journal columns tier / tier_facts / tier_reason; report grouped by tier.
- Tests: tests/test_v17.py (new), test_v16 updated for 1.7 (paper tests removed, routing expects a Tier C
  demo order), one 1.6 validator test sets max_same_direction_positions 2 explicitly. 146 offline tests pass.
Config changes: see config/auto_trader_config.json (`tiers`, `max_same_direction_positions`, playbook
modes live, python_session_setups off). Delete src/autotrader/paper.py. Unchanged: demo lock, Python
as the final gate, >= 2R recomputed, news lock, spread / slippage / quote-age caps, 4H/1H veto for
trend trades, duplicate protection, 1 % total risk, daily -2 % / weekly -5 % limits, kill file.
Unverified: live GivTrade behaviour of the new sizes, and whether the tiers are profitable - that is what
the demo and the reviews are for.

## 6.1.4 - the analyst failure loop (signal bot only; no change to when a BUY/SELL may be sent)
Evidence: runtime/journal.sqlite3, 14 Oct 01:45-02:36 UTC: 43 `AI_FAILED {"error": "BadRequestError"}`
rows, one every ~66 s, straight after an "Abnormal 15M range" reassessment; the next map ran at 11:58.
- The AI worker reported only the exception class. It now reports `Class: HTTP status: API message`
  (`gold_monitor.error_detail`, secrets redacted), for both the OpenAI and the Claude worker, and the
  AI_FAILED row carries the request size (`payload_chars`), the failure streak and the retry delay.
- A failed map was retried after a fixed 60 s, and an early reassessment could reset the wait to 0.
  Failures now back off 60 s, 2, 4, 8, 16, 30 min (streak reset by the next result); the back-off is
  enforced in dispatch_ai, so a reassessment cannot bypass it; the third failure in a row logs ERROR.
- A failed map call is removed from the max_maps_per_hour count (22 Oct: two instant 401s postponed the
  next attempt by 57 min on top of the back-off).
- `max_maps_per_hour` (default 2): zone-entry / abnormal-range reassessments bought six paid maps between
  00:00 and 01:03 UTC on 15 Oct; a map beyond the cap is postponed until the oldest one is an hour old.
- tests/test_ai_backoff.py (6 tests). Root cause of the 400s is still unknown until the message is
  seen in the journal: watch `AI_FAILED.error` and `payload_chars` on the next occurrence.

## Auto-trader 1.6 - market state, session levels, playbooks, paper mode, historical replay
New modules (src/autotrader/): regime.py, sessions.py, playbooks.py, paper.py, replay.py; tools/replay.py.
- Market state from closed candles, never one bullish/bearish flag: macro (Daily+4H), 1H, 15M trend,
  phase (CONTINUATION / PULLBACK / TRANSITION / REVERSAL / RANGE), volatility, momentum, expansion.
- Session levels with DST-correct windows (Asia, London, NY, opening ranges, previous day/week) and
  SWEEP_RECLAIM / BREAKOUT / FAILED_BREAKOUT events; new confirmations SESSION_SWEEP and
  SESSION_FAILED_BREAKOUT.
- Playbook routing: TREND and COUNTER_TREND live (v1.5 rules, unchanged); REVERSAL_TRANSITION and
  SESSION_SWEEP new, in PAPER mode; Python's own session-sweep setups paper only.
- Expansion filter: no fading a strong one-way move unless CHoCH, failed breakout, break-and-retest or
  sweep-and-reclaim confirm it failed. Ships in SHADOW mode (journals, never blocks) until replayed.
- Paper book (table paper_trades), journal columns playbook / confirmation / session / regime /
  astra_class / expansion_block, Telegram trade messages (telegram_trades), report breakdowns.
- Historical replay with no AI calls and no look-ahead: OLD_v1.5 vs three NEW variants on the same
  candidates, costs (spread from data, slippage, commission), ticks for same-minute stop/target
  (otherwise a loss), in-sample vs out-of-sample, trade-off of removed winners/losers.
- Astra (signal bot): ideas carry trade_class, macro_trend, local_trend, market_phase (asked of the
  model; older ideas without them still validate). Contract describes REVERSAL-TRANSITION ideas.
Fixes:
- CHoCH confirmation never fired in v1.5: SMC events carry `at`, the bridge looked for `confirmed_at`.
- Partial fills: a partly filled pending order that then expired/was cancelled was marked
  EXPIRED/CANCELLED and its open position was no longer managed (TP1, breakeven). Now it stays OPEN
  with the filled volume; TP1 closes half of the filled volume.
Unchanged: MT5 execution, demo lock, risk limits (per trade, 1 % total, daily -2 %, weekly -5 %),
order management, restart recovery. No consecutive-loss cool-down.

## 1.5 - both directions and account-wide risk
Signal bot (src/astra_analysis.py): Astra's contract now evaluates BUY and SELL every time and adds a
"Counter-trend:" idea at a strong remembered Daily/4H zone on the other side (nearest-structure TP1,
objective 15M confirmation, MEDIUM/HIGH risk); up to 3 ideas (was 2). Signal gate unchanged.
Auto-trader:
- validator.py: total risk from the broker's positions and pending orders (all magics, manual trades,
  any symbol); no stop = unmeasurable = no new order. Counter-trend orders skip the 4H/1H veto.
- bridge.py: strong_zone() checks the idea against the signal bot's memory (read-only); confirmation()
  adds SWEEP (sweep beyond the zone, close back inside) and CHOCH (Python 15M CHoCH) to REJECTION.
- app.py: counter-trend ideas need a strong zone and a confirmation (also in limit mode), always B size.
- state/report: strategy column (SIGNAL, SWING_TREND, SWING_COUNTER), report per strategy.
- settings: counter_trend_enabled, counter_timeframes, counter_min_tests, counter_zone_tolerance,
  magic_scalp (40002, reserved). No consecutive-loss cool-down; daily/weekly loss limits unchanged.

## Auto-trader 1.4 - ideas traded the way Astra describes them
- `idea_entry_mode: "confirmation"` (default): an idea is only traded after a closed 15M rejection
  candle at its zone (the confirmation Astra asks for); before, limit orders were placed at once and
  filled during the 15 Oct Asian rally. MARKET if 2R remains, else a limit at the exact 2R price
  (`confirm_retest_hours`). A candle through the stop first cancels the idea. "limit" = 1.3 behaviour.
- Each closed trade's money result is stored in the orders table.

## Auto-trader 1.0 (demo) — separate program, signal bot unchanged
- New `src/auto_trader.py` + `src/autotrader/`: Claude proposes orders; Python validates and executes
  them on a GivTrade DEMO account (refuses real accounts). Reuses MT5Session (server check, auto
  clock offset), MT5Candles, MT5CSVCalendar + calendar_block, Sessions, gold_evidence, the lessons
  playbook and astra_anthropic.make_client (same key, workspace header and model as the V6 analyst),
  and applies the V6 spread / slippage / quote-age / minimum-R:R / MTF-opposition limits.
- Orders carry SL + final TP on the broker server and a broker-side expiry; TP1 partial close and
  breakeven; daily/weekly loss limits; kill file; A vs B results report. See docs/AUTO_TRADER.md.
- Up to two different orders at once (own grade and risk each), duplicates rejected, total risk
  capped at 1 %, a setup may be traded again after its earlier order finished; netting accounts: one.

## One analysis for both bots
- Auto-trader `source: "signal_bot"` (default): no Claude calls of its own. Confirmed signals from
  `runtime/signals.jsonl` become MARKET orders; TRADE IDEAs from `runtime/astra_analysis.json`
  become pending orders (no confidence minimum; < 60 % or HIGH risk = B size). `own_analysis` keeps
  the trader's separate Claude analysis. Report splits results by source.
- Signal bot: `signal_file` (default `signals.jsonl`) - each confirmed BUY/SELL signal is appended
  the moment it passes final_check, before Telegram. tests/test_signal_file.py.

## Signal bot: idea reports for a new trade in the same area
- A TRADE IDEA in an area already reported today is sent again when it is a NEW trade: the earlier
  idea finished in the learning history (target, stop or expiry), or its entry / stop / TP1 moved by
  at least `astra_idea_change_atr` (0.15) x daily ATR. The same still-running plan is not repeated.
  `astra_idea_resend_new_trade: false` restores once per area per day. tests/test_idea_resend.py.

## 6.1.3 + Claude analyst option
- Optional Claude (Opus 5.5) analyst: `"analyst_provider": "anthropic"` in the config. Default stays
  `"openai"`; every Python check after the model answer is unchanged. See docs/CLAUDE_ANALYST.md.
- Main file renamed `src/gold_monitor_v51.py` → `src/gold_monitor.py`; test_signal_gate.py and docs
  updated to the new name (the rename had broken 5 test modules).
- Analyst switch without editing the config: `scripts\\run.ps1 -Analyst claude|astra` or the
  `GOLD_ANALYST` environment variable. Startup log and the ONLINE message name the analyst. 296 offline tests.

## 6.1.3 — clean release after a full pre-live code review
Reliability fixes (no change to when a BUY/SELL may be sent):
- MT5 reconnect: after a terminal restart/disconnect every MT5 call just returned nothing and the bot
  stayed blind until restarted by hand. The quote feed now reconnects after 4 failed polls (~30 s).
- Offset re-check: the hourly server-time check needs a new tick within 3 s; in a quiet minute it
  failed, dropped the quote and wiped every WATCH/ARMED setup (plus a paid re-map). The last confirmed
  offset is now kept for up to 4 hours; with no recent confirmation (e.g. after a weekend) it still
  fails closed.
- Windows file locks (OneDrive, antivirus, Excel): snapshot files (news.json, market_map.json,
  calendar.json, astra_analysis.json) retry briefly and never abort a tick. Before, a locked
  market_map.json made an installed map look "rejected" and triggered another paid map.
- Astra evidence: the compact SMC list per timeframe kept only the last kind in the index (6 sweeps,
  8 order blocks), so Astra never saw a BOS/CHoCH or most FVGs and trend-continuation / CHoCH setups
  could not be verified. Now: newest 4 BOS/CHoCH + 2 sweeps, newest 4 FVGs + 4 order blocks (same size).
- Memory payload: usable SMC zones are filtered in SQL (structure items no longer crowd them out).
- Memory database is bounded: analyses and resolved predictions older than 90 days and SMC items
  unseen for 30 days are pruned.
- A 1H/4H candle that opened before a setup existed no longer invalidates it with a pre-birth extreme
  (live quotes and 15M candles cover everything after birth); it still counts for the setup's conditions.
- Structure-watch reports are not sent from a refused map or on stale candles.
- Journal audit rows can no longer collide on coarse Windows clocks; feed/price races guarded.
- Arabic text and symbols never crash or vanish on a cp1252 console or redirected output.
- dry run: zones.csv open in Excel no longer aborts the run. Calendar → Excel shows EventType.
- --self-test now runs every test file (it skipped the calendar hotfix tests). 259 offline tests.
- requirements: openai >= 3.13 (saved-agent and Responses APIs); older 1.x installs failed at runtime.
Clean-up: removed the applied calendar-hotfix patch and notes, a stray outdated copy of the calendar
exporter and workbook in src/, the Forex Factory test config and its runtime folder, an obsolete V5.0
probe script, an unused Excel template, dead functions and unused imports. New README.

## 6.1.2 — full playbook reaches Astra
- The live playbook cap was 4,000 characters and 300 per rule. A 12-principle Arabic playbook was cut to
  P1–P8, with long rules truncated mid-sentence, so Spring / BBR / X / range-support never reached Astra.
  Caps are now 9,000 characters, 700 per rule, 300 per when_not (≈ +2k input tokens per map at most).
- learn_lessons tells Astra these limits when consolidating, so it merges rules instead of overflowing.

## 6.1.1 — from the second real dry run
- TP labels: a target is "at" a level only within max(0.05 daily ATR, $0.50) (was 0.15 ATR, which labelled
  TP1 4195 — $10 below the zone — as "at Daily resistance 4205–4220"). Astra's own validated map levels
  (e.g. 15M supply) are now label candidates too. Longer target/stop explanations (no mid-word cut).

## 6.1.0 — Astra always gives a trade idea
- Every analysis now includes at least one TRADE IDEA (Astra's best trade in the direction it leans):
  confidence 0–100 %, risk LOW/MEDIUM/HIGH, entry type MARKET / LIMIT / BREAKOUT, exact entry zone,
  stop loss beyond real structure, 1–3 take-profits at remembered levels. NO_TRADE only for unusable data.
- Python labels the stop and each target with the remembered zone they sit on ("TP1 4219 — at Daily
  resistance 4205–4220 · tested 4×") and flags a TP1 that is not at a remembered level. Ideas below 1R are
  kept but marked HIGH risk ("reward smaller than risk"). MARKET entries become LIMIT when price is not in
  the entry zone.
- USD holidays no longer withhold ideas (risk raised, thin-liquidity warning); timed high-impact release
  windows, HIGH news risk, stale candles and refused maps still withhold them.
- Every idea is scored (filled → TP1 before stop?) with its confidence; the daily report and Astra's
  track record show the hit rate overall and for 70 %+ confidence calls.
- BUY/SELL signals (V5 gate) unchanged. Tests: 236 offline.

## 6.0.2 — teaching Astra from your charts
- `lessons/` folder + `tools/learn_lessons.py`: the SAME saved Astra agent answers each chart lesson
  blind, compares with your view, extracts price-free principles, and consolidates a trader playbook.
  Writes `LESSONS_REPORT.md` (agreement, misses, principles) and `astra_playbook.json`. Unchanged
  lessons are skipped. Image failures are reported; text-only lessons still work.
- Live bot: `playbook_file` (default `lessons/astra_playbook.json`) is sent to Astra with every hourly
  map as `trader_playbook` (≤12 principles, ≤4000 characters; raised to 9000 in 6.1.2); the contract says to apply a principle
  only when live structure matches and to name it. Never affects the signal gate. Dry run shows it.
- Tests: 234 offline.

## 6.0.1 — pre-live tooling
- `tools/astra_dry_run.py`: one real Astra map on closed candles while the market is closed; prints
  memory zones (and `zones.csv`), payload size, tokens/time, map + narrative validation and Telegram
  previews. Never sends Telegram, never writes `runtime/`. `--no-ai` and `--raw` modes are free.
- Shared helpers `load_saved_agent` / `astra_instructions` (live worker and dry run use the same prompt);
  `LAST_USAGE` records token usage for diagnostics.
- Calendar: confirmed the V5.1.2 holiday hotfix (EventType) is applied in Python and the MQL5 bridge;
  the 8 Oct "unknown event importance" failures predate it (9 Oct export accepted Columbus Day).
- Design: `docs/TEACHING_SESSIONS.md` (teaching sessions via Telegram + MT5 line export; not built yet).
- Tests: 229 offline.

## 6.0.0 — Astra professional trading assistant (see docs/ASTRA_V6.md)
- **Market memory** (`src/astra_memory.py`, `runtime/astra_memory.sqlite3`): 1day/4h/1h S/R zones from
  confirmed pivots with creation time, separate tests, last test and reaction (ATR); lifecycle
  BROKEN → ACCEPTED/FLIPPED → RETEST_CONFIRMED, FALSE_BREAKOUT, ARCHIVED; equal-high/low liquidity
  (SWEPT/TAKEN); persistent 1h+ SMC items; Astra's validated levels join the same lifecycle. Survives
  market closes and restarts; point-in-time safe; incremental == replay; failed updates roll back.
- **Python sensor**: price entering an important remembered zone, or a recent break/retest/sweep on
  one, requests an early Astra map (own cooldown; never delays V5 re-maps).
- **Astra analyst narrative** (`src/astra_analysis.py`): the map schema adds `analysis`
  (market story, historical context, measurable scenarios, TRADE IDEAs with LOW/MEDIUM/HIGH risk,
  STRUCTURE WATCHes). Soft validation: bad items are dropped, the executable map is never rejected
  for them. Python recomputes R:R, raises (never lowers) risk, drops <1R ideas, flags chasing.
- **Telegram**: BUY/SELL IDEA and STRUCTURE WATCH reports (Arabic/English), deduplicated, throttled
  and sent on a low-priority queue behind signals. Ideas withheld during calendar windows, HIGH news,
  stale candles or refused maps. Daily report adds Astra's view, remembered levels and track record.
- **Learning history**: scenarios scored against closed candles (PLAYED_OUT / FAILED /
  NOT_TRIGGERED / EXPIRED_AFTER_TRIGGER); 30-day track record fed back to Astra.
- **Unchanged**: every V5.1.2 BUY/SELL rule. V6 code cannot create, approve, arm or release signals;
  its failures are isolated from the map install, watcher and signal gate.
- Config: `memory_enabled`, `astra_reports`, `astra_report_min_interval_seconds`,
  `zone_entry_refresh`, `zone_entry_cooldown_seconds`. Tests: 225 offline (new `tests/test_astra_v6.py`).

## 5.1.2 — review of 5.1.1
- **MT5 weekend daily bars:** Sunday + Monday D1 bars are merged into one Monday bar (Sunday open,
  combined high/low, Monday close); a lone Sunday becomes Monday; a Saturday tail is folded into
  Friday. 5.1.1 renamed Sunday to Monday, which collided with the real Monday bar and made every
  daily fetch fail (no market maps).
- **No hidden minimum stop:** the allowance-only cap (`0.25 / 2.5%` = a fixed $10 minimum stop) is
  disabled by default (`broker_max_slippage_stop_fraction: null`). New explicit
  `minimum_stop_distance` (default 0). Spread (5%) and combined spread+allowance (7.5%) caps remain.
- **Tick continuity without false outages:** if the live tick is ahead of MT5 tick history, history
  gets up to 2 s to catch up before a gap is declared. A persistent gap still fails closed.
- **Broker-server check:** exact name, wildcard (default `GIVTrade-*`, covers demo and live) or a
  comma list; checked once per hour instead of ~4×/s; the error names the actual server.
- **Candle revisions:** a correction ends a setup only if it flips a contact or condition result
  that setup used; every revision is still re-checked for invalidation.
- **Calendar clock skew:** the MT5 calendar fetch time is clamped to now, so a PC clock slightly
  ahead no longer blocks signals for up to two minutes after each export.
- **Project layout:** `src/ tests/ tools/ mql/ config/ data/ docs/ scripts/`; bootstrap files are
  read from `data/`; `--self-test` runs `tests/`. Leftover `.bak` files removed.
- Tests: 196 offline tests passing (new: `tests/test_gold_v512.py`).

## 5.1.1
1. Corrected-candle safety: every active setup affected by an OHLC revision to a candle closed after setup birth is immediately INVALIDATED. This safely revokes prior confirmation/touch history and any cached AI authorization, preventing stale READY alerts. Historical pre-birth corrections do not automatically kill a current setup.
2. MT5 GivTrade timestamp handling: retain the runtime fresh-tick offset inference; verify server name `GIVTrade-Server` through the read-only MT5 API; remap Sunday-labelled daily candles to Monday and use a Friday session close for MT5 weekly candle labels. General Twelve Data weekly parsing behavior remains unchanged.
3. Entry-cost validation: added configurable caps `broker_max_slippage_stop_fraction` (2.5%) and `broker_max_combined_stop_fraction` (7.5%). Both are enforced after verifying entry zone and minimum R:R. The $0.25 slippage allowance is hypothetical (optional; set 0 to disable) because the monitor never trades.
4. Automatic MT5 USD economic calendar: new read-only `GoldCalendarBridge.mq5` Expert Advisor reads the MQL5 calendar and atomically updates MT5 Common/Files/gold_calendar.csv every 300 seconds. New Python `mt5_csv` provider reads this file, validates broker offset against a live tick, rejects missing/stale/invalid exports, and normalizes exact event times to UTC for economic-calendar safety blocks. Optional `mt5_dual` also requires Forex Factory and unions events.
5. Tick continuity: MT5 history retrieval failure, a saturated 2000-tick batch, or a new broker tick missing from the history feed causes a fail-closed quote-stream interruption rather than a silent incomplete scan. Reconnect/re-warm and validate against GivTrade's tick-history semantics before live signal release.
6. Excel viewer: `mt5_calendar_to_excel.py --watch` automatically updates the workbook as the CSV changes. The strategy itself reads CSV and does not depend on Excel.
7. New offline regression suite: `test_gold_v511.py` plus updated legacy tests; **185 tests passing**.

**Important deployment limitations:** MQL5 source has not been compiled in MetaEditor in this sandbox. No GivTrade terminal live integration, 210+ history depth, weekend/DST replay, sustained calendar operation, historical profitability, or cloud deployment has been verified. Begin with demo and paper alerts; do not treat passing offline tests as readiness for unattended BUY/SELL.

## 5.1
- MetaTrader 5 (read-only) as default provider for candles, live bid/ask and broker checks.
- Broker exit-side invalidation (bid for BUY, ask for SELL); spread $0.50, 5% of stop; $0.25 adverse
  slippage; broker quote required for signals.
- Evidence lifecycle: FILLED FVG / SUPERSEDED Fibonacci / any non-usable citation ends the setup.
- Opposing 4H/1H structure vetoes B_TRADE; candle revisions re-checked; evidence rebuilt on content change.

## 5.0
- SMC / Fibonacci / breaker / RSI / MACD / EMA evidence integrated with Astra through four verified
  playbooks; sender/price-update race closed; replaceable provider adapters.

Design notes (formerly in the code header):

```
V5.0 (on top of every V4.5 behaviour, all V4.5 tests retained):
1. Price-update / sender race closed.
   - The quote feed records every print atomically (sequence number, low/high window). A print
     with the same timestamp as `latest` now updates it (V4.5 kept the first one).
   - The main loop runs each tick under `App.lock`. The sender performs its final validation
     under the same lock, so no tick can change price/strategy state half-way through the check.
   - The sender peeks the prints the main loop has NOT processed yet; a print through the
     invalidation drops the signal even if the setup still reads ARMED.
   - The lock is released before the HTTP request. If invalidation happens while a signal is
     in flight, the setup stays INVALIDATED and an explicit withdrawal message follows.
   - A fresh read-only broker quote is taken just before the final check.
2. SMC / Fibonacci / breaker / RSI / MACD / EMA evidence is integrated with Astra.
   - Python builds a closed-candle evidence snapshot (gold_evidence.py) once per new candle set.
   - Astra receives it with ids and must give every setup a playbook + evidence_ids.
   - Python verifies ids exist, match direction, are usable, overlap the watch zone and satisfy
     the playbook (trend continuation, CHoCH reversal, breaker retest, Fibonacci+zone).
     RSI divergence, MACD and EMA are context only and never satisfy a playbook.
   - Each closed-candle update re-checks cited evidence: an invalidated cited zone ends the
     setup; missing/unconfirmed evidence (e.g. breaker not yet retested) withholds signals.
   - A_TRADE additionally needs Python's own 4H/1H structure not to oppose the direction.
   - Contacts before WATCH can be set per playbook (default: touch_candles for all).
3. Providers are replaceable adapters: candles, live quotes, calendar, news, read-only broker
   quotes. Built-in names or "package.module:ClassName". Broker adapters are READ-ONLY.
   When a broker adapter is configured, BUY/SELL need a fresh executable quote: spread cap,
   mid-price divergence cap, ask (BUY) / bid (SELL) inside the entry range, RR >= minimum.

WATCH is an observation for human review. A reviewed plan still requires the user to check
their broker's executable price/spread. No profitability claim is made. No automatic execution.
```
