# BUY/SELL signal policy — SMC + Fibonacci + indicator confluence (V5.1.2 gate, unchanged in V6)

**Status:** Integrated into `gold_monitor.py`. V5.1 fixes the independent V5.0 review findings (see section 8) and adds MetaTrader 5 providers. Still **paper / human-review only**: no automatic execution, no profitability claim. All pattern methods are price-pattern conventions computed from closed OHLC, not proof of institutional order placement. Historical replay testing (section 7) is still required before trusting the alerts.

## 1. Files

| File | Role |
|---|---|
| `src/gold_monitor.py` | Live monitor: quotes, candles, Astra map/review, news, calendar, Telegram, final gate. Provider adapters. |
| `src/gold_smc_engine.py` | Closed-candle swings, BOS/CHoCH, liquidity sweeps, FVG, order-block candidates, Fibonacci/OTE. (Rebuilt for V5; the earlier copy was missing from the project.) |
| `src/gold_confluence_v5.py` | EMA20/50/200, Wilder RSI, MACD 12/26/9, RSI divergence, breaker candidates. V5 change: a breaker's break must come after the OB's confirmation. |
| `src/gold_evidence.py` | Builds the evidence snapshot, compacts it for Astra, verifies each setup's citations, re-checks them at signal time. |
| `mql/mql4/XAUUSD_QuoteBridge.mq4` | Optional MT4 Expert Advisor writing read-only Bid/Ask to a file. Contains no trading calls. |
| `tests/` | V4.5 regression suite, V5/V5.1.x tests, calendar hotfix, V6 Astra and 6.1.3 tests. Run `python src/gold_monitor.py --self-test`. |

## 2. Role of each feature

| Feature | Operational evidence | Role | Sufficient alone? |
|---|---|---|---|
| BOS / CHoCH | A **close** beyond the latest confirmed, unbroken swing; CHoCH when it reverses the prevailing structure | Structure, direction | No |
| Liquidity sweep | Wick beyond a confirmed swing (+0.05 ATR), close back inside | Potential rejection | No |
| FVG | 3 closed candles, displaced middle candle; ACTIVE / PARTIALLY_FILLED / FILLED / INVALIDATED | Candidate zone | No |
| Order block | Last opposite candle before a displaced structural break; ACTIVE / TESTED / INVALIDATED | Candidate zone | No |
| Breaker | Confirmed OB broken by a close beyond its far edge, then a later candle retests and closes on the new side | Candidate zone / confirmation | No |
| Fibonacci / OTE | Latest confirmed impulse leg containing a same-direction break; OTE = 0.618–0.786 | Confluence only | No |
| RSI divergence, MACD, EMA | Closed-candle values per timeframe | **Context only** | Never |

Daily EMA200 is a 200-day EMA; 15M EMA200 is 200 fifteen-minute candles.

**Not accepted as rules without testing:** 61.8% as the best reversal level, every FVG must fill, 4H setups having universally better win rates, OB revealing central-bank orders.

## 3. How evidence flows through the bot

1. On each new closed-candle set, Python builds one evidence snapshot for all five timeframes (≈0.1 s). A frame that fails analysis contributes no evidence.
2. Astra's hourly map receives `smc_evidence` with stable ids. Every setup must declare a `playbook` and cite 1–8 `evidence_ids`.
3. Python validates the map against **the snapshot Astra analysed**. Unknown ids, wrong direction, INVALIDATED/FILLED zones, or an unmet playbook reject the whole map.
4. Watch → contacts → ordered closed-candle conditions → Astra review (now sees the current status of each cited id plus Python's own structure verdict) → news + calendar → final gate.
5. Every closed-candle update re-checks the cited evidence. An invalidated cited zone ends the setup. Missing evidence, or a breaker not yet retested, withholds signals without ending the setup.
6. Grades: A_TRADE needs Astra's criteria **and** Python's 4H/1H structure not opposing the direction. V5.1: opposing (or unknown) 4H/1H structure vetoes B_TRADE too (`block_on_mtf_opposition`, default true). No points-scoring of indicators.
7. V5.1: a required citation that leaves its usable states at any time (FVG FILLED, Fibonacci SUPERSEDED, OB/breaker INVALIDATED) ends the setup — the same rule as at map validation.

## 4. Playbooks (model proposes, Python verifies)

- **TREND_CONTINUATION** — same-direction BOS on 1H or 4H, plus a same-direction FVG / OB / breaker on 15M or 1H overlapping the watch zone. 4H trend must not oppose.
- **CHOCH_REVERSAL** — same-direction CHoCH (15M/1H/4H) preceded by a same-direction liquidity sweep, a non-Fibonacci zone in the watch zone, and at least two ordered conditions (CHoCH alone only justifies WATCH).
- **BREAKER_RETEST** — same-direction breaker overlapping the watch zone. A BUY/SELL is withheld until the current snapshot shows `RETEST_CONFIRMED`.
- **FIB_ZONE_CONFLUENCE** — same-direction OTE overlapping a separate same-direction FVG or OB, both in the watch zone.

`playbook_touch_candles` lets each playbook use its own contact count before WATCH. All default to `touch_candles` (3). Lower values (e.g. 1 for breaker retests) only after historical testing.

## 5. Hard safety gates (all must pass)

- Closed, timestamp-verified candles only; fresh quote; open session; no open-session 15M gap since setup birth; no skipped invalidating candle.
- Cited evidence exists, matches direction, still usable; playbook satisfied.
- Astra approved the current immutable plan fingerprint; material changes revoke approval.
- Calendar exclusion passes; news risk LOW/MEDIUM (UNKNOWN/HIGH blocks).
- Price inside the entry range; structural SL; independent TP; RR ≥ 2.0 at the live price.
- Broker quote required by default (`require_broker_quotes`). Fresh quote; spread ≤ $0.50 **and** ≤ 5% of the stop distance; broker mid within `broker_max_mid_divergence` of the feed.
- Exit side: BUY blocked when the **bid** is at/through invalidation or stop; SELL when the **ask** is. A broker quote update through invalidation ends the setup. With the MT5 feed, the main loop tests BUY invalidation on bid lows and SELL invalidation on ask highs.
- Entry: **ask** (BUY) / **bid** (SELL) inside the entry range, and the adverse fill (± $0.25 slippage) also inside the range with RR ≥ 2.0.
- Candle revisions (same timestamp, changed OHLC) are re-checked for invalidation and rebuild the evidence snapshot.
- Sender final check runs under the main-loop lock and also rejects any not-yet-processed print through invalidation.

## 6. Providers

| Kind | Built-in | Notes |
|---|---|---|
| candles | `mt5` (default), `twelvedata` | MT5 intraday bars converted from server time to UTC; D1/W1 keep the broker date label. |
| quotes | `mt5` (default), `twelvedata_ws` | MT5: every tick via `copy_ticks_from`, two-sided bid/ask. Twelve Data: mid only. |
| calendar | `mt5_csv` (default), `mt5_dual`, `forexfactory`, `json_file`, `none` | `none` blocks BUY/SELL. |
| news | `openai_web`, `none` | `none` = UNKNOWN = BUY/SELL blocked. |
| broker_quotes | `mt5` (default), `file`, `http_json`, `none` | Read-only. MT5 calls go through an allow-list (no order functions). Server offset `"auto"` is inferred from a tick that arrives while watching; no live tick = fail closed. `file` pairs with the MT4 EA. |

Custom adapters: `"package.module:ClassName"` subclassing the matching base class.

## 7. Experiments before production

- Point-in-time historical replay through the full gate (no look-ahead), with spreads, slippage, session breaks, DST and major releases.
- Manually label real XAU/USD BOS/CHoCH/FVG/OB/breaker/sweep examples; measure false detections, delay and misses.
- Evaluate each playbook separately and jointly out of sample against the V4.5 baseline; record false alerts, alerts after invalidation (target zero), A/B/NO_TRADE distribution and paper outcomes.

## 8. V5.1 changes from the independent review

| Finding | V5.1 behaviour |
|---|---|
| Broker exit side not checked | Bid (BUY) / ask (SELL) through invalidation or stop blocks the signal and ends the setup |
| FILLED FVG / SUPERSEDED Fibonacci accepted at recheck | Any required citation outside its usable states ends the setup |
| Spread $0.80, no slippage or stop-relative cap | $0.50 cap, $0.25 adverse slippage, spread ≤ 5% of stop distance (all provisional, configurable) |
| B_TRADE despite opposing 4H/1H structure | Vetoed by default |
| Same-timestamp candle corrections missed | OHLC-keyed tracking; revised candles re-checked; evidence rebuilt on content change |
| Linux VPS cannot read the MT4/MT5 terminal | Unchanged: the MetaTrader5 package runs on Windows beside the terminal. Run the bot on a Windows VPS with MT5, or keep a secure bridge (open item). |


## V5.1.1 implementation addendum (October 2026)

- **Alert-only:** no trading API calls. `broker_slippage_allowance` is an optional
  hypothetical adverse price buffer, not observed execution slippage. Default
  remains $0.25 pending user selection; set to `0` for alerts without the buffer.
- **Corrections:** active setups are invalidated when any post-birth previously
  consumed candle is corrected; this deliberately discards potential opportunities
  rather than preserving questionable 15M touch/confirmation states. Cached approval
  and queued signals are withdrawn through the existing event pipeline.
- **GivTrade MT5 timestamp labels:** non-UTC intraday ticks are normalized by
  independently inferred fresh-tick server offset, never a permanently hardcoded
  UTC+3. Sunday D1 labels are remapped to Monday; Sunday-labelled MT5 W1 bars
  close at Friday 17:00 America/New_York. Validate on live broker export and at DST changes.
- **Calendar:** `GoldCalendarBridge.mq5` periodically creates
  `Terminal/Common/Files/gold_calendar.csv` using atomic rename. `mt5_csv` is the
  default calendar provider. Python verifies file freshness, an exact event time,
  recognized impact, event UTC, and consistency between bridge offset and fresh
  MT5 tick before permitting the calendar to enter the safety gate. A missing or
  invalid file prevents BUY/SELL. Optional `mt5_dual` combines the events with
  Forex Factory and requires both sources to work. This is **not** a calendar
  that Python retrieves directly from the MetaTrader5 package; MQL5 is necessary.
- **Trading suspension:** if candle/tick continuity is uncertain, default to no new
  alerts, require fresh prices and a rebuilt market map. A 2,000-entry tick batch
  cannot be silently deemed complete.
- **Broker check:** `expected_mt5_server=GIVTrade-Server`; reject wrong terminal.
  Spread <= absolute $0.50 and 5% stop distance. Hypothetical slippage <= $0.25
  and 2.5% stop distance; spread + allowance <= 7.5% stop distance. Check
  worst-price RR >= 2 and approved entry zone. These are paper-test parameters,
  not verified optimal levels.
- **Limitations:** MQL5 compiler and GivTrade live terminal not accessible to the
  offline test suite. Validate EA compilation, connection, timestamp offsets,
  calendar event coverage and history depth before enabling unattended alerts.

## 9. V5.1.1 / V5.1.2 additions

| Area | Behaviour |
|---|---|
| Economic calendar | Default `mt5_csv`: `GoldCalendarBridge.mq5` exports USD events every 5 min; stale (>12 min), offset-mismatched, untimeable high-impact or malformed exports block BUY/SELL. `mt5_dual` also requires Forex Factory. |
| Weekend D1 bars | Sunday + Monday merged into Monday; Saturday folded into Friday. |
| Entry cost | Spread ≤ $0.50 and ≤ 5% of stop; (spread + $0.25 allowance) ≤ 7.5% of stop; adverse fill inside entry range at ≥ 2R. No allowance-only cap by default; optional `minimum_stop_distance`. |
| Tick continuity | History gets 2 s to catch up with the live tick; a persistent gap, failed history read or full 2,000-tick page drops the quote (outage → re-warm → new map). |
| Candle revisions | A post-birth correction that flips a contact or condition result ends the setup; all corrections are re-checked for invalidation. |
| Broker server | Must match `expected_mt5_server` (name, wildcard or list); re-checked hourly. |
