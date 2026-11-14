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

## Auto-trader 1.10.4 — notification crash after a live order (28 Oct 2026)

### Fixed
- **"Order send failed: list index out of range" for an order the broker had accepted.** IDEA-BUY-4031 (TP1 4057.44,
  TP2 4064.74, no TP3) was placed and journalled, then `alerts.signal_text` read `targets[2]` and raised. The app
  reported a send failure, Telegram got no PLACED message, and the idea was marked as not sent, so the lab could
  take it as well. `signal_text` now handles 1-3 targets, and every trade notification goes through
  `alerts.safe_trade`, which logs a warning instead of raising. Behaviour of orders is unchanged.

### Tests
- `test_v110.NotificationTests`. Suite: 180 tests.

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
