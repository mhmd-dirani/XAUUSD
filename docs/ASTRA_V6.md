# Astra V6: professional trading assistant layer

V6 changes the question the bot asks. It used to ask "Is there a trade right now?" Now it asks: what is the market doing, which remembered levels matter, which scenarios are developing, and when does an opportunity become interesting?

Automatic execution stays disabled. The V5.1.2 BUY/SELL signal path is unchanged in V6: playbook evidence, ordered closed-candle confirmations, ≥ 2R, news, calendar, broker quote and fail-closed data checks all still apply. V6 adds analyst reports and memory around that path. It cannot create, approve, arm or release a signal.

## Why

The runtime journal from 8–9 Oct 2026 shows the problem V6 fixes. Astra built 13 market maps and produced 0 setups. In several of them Astra could see something developing, for example: "OTE overlaps the 15M demand block but has not been tested yet." The strict playbook contract gave it no way to say so. You received one daily report and silence.

## Architecture

```
            Market Data (MT5 closed candles, live bid/ask)
                              │
                              ▼
  Historical Structure Engine (astra_memory.py, deterministic)
   pivots → S/R zones · tests · reactions · break / flip / retest / false breakout
   equal highs/lows → liquidity pools (ACTIVE / SWEPT / TAKEN) · SMC items persisted
                              │
                              ▼
        Market Memory Database   runtime/astra_memory.sqlite3
        (survives market closes and restarts)
                              │
             ┌────────────────┴───────────────────┐
             ▼                                    ▼
   Python live sensor                    Astra AI analyst (hourly + on events)
   price enters an important             market_story · historical_context
   remembered zone, or a zone            scenarios (measurable) · TRADE IDEAs (risk class)
   breaks / flips / is swept   ──────▶   STRUCTURE WATCHes · strict V5 setups (unchanged)
   → early Astra re-map                           │
                                                  ▼
                       validate_analysis (soft): bad items dropped, R:R recomputed,
                       risk raised by Python (never lowered), stance recomputed
                                                  │
                    ┌─────────────────────────────┼──────────────────────────┐
                    ▼                             ▼                          ▼
          Telegram reports              Learning history             V5 signal gate
          (low-priority queue)          scenarios scored vs          (unchanged; BUY/SELL
                                        candles → track record       only after all checks)
                                        fed back to Astra
```

## 1. Market memory

Memory is stored in `runtime/astra_memory.sqlite3` and lives in the state directory. An optional `data/gold_memory.db` file, if you create one, is still read as a read-only bootstrap seed.

**Zones.** Each zone comes from a confirmed swing pivot on 1day, 4h or 1h: 3/3 bars on 1day and 4h, 4/4 bars on 1h. A zone spans from the wick extreme to the body edge, capped at 0.35 ATR of its timeframe. If a new pivot overlaps an existing zone, it is merged into that zone (a lower-timeframe zone is promoted) instead of being duplicated. Astra's own validated 1h+ support/resistance levels also join, marked `source = ASTRA`, and their history is replayed through the closed candles.

**Each zone remembers:**
- when it was created
- how many separate tests it has had (a new test needs price to leave the zone for 2 bars first)
- when it was last tested
- the reaction after each test, in ATR, measured over the next 6 bars

**Lifecycle (closed candles only):**

| Event | Rule |
|---|---|
| `BROKEN` | Close beyond the zone by more than 0.1 ATR. The role flips (resistance becomes potential support) and the retest is `WAITING`. |
| `ACCEPTED` → `FLIPPED` | 3 closes beyond without touching the zone. The flip is established. |
| `RETEST_CONFIRMED` | After price has left the zone, it touches it again and closes on the new side. |
| `FALSE_BREAKOUT` | Before acceptance, price closes back through. The original role is restored. |
| `ARCHIVED` | The role has flipped 3 times (a chop level). |
| Liquidity | Equal highs/lows within 0.1 ATR, not wicked through in between. `SWEPT` = wick through, close back. `TAKEN` = close through. |

**Determinism.** Memory is point-in-time safe: a pivot only exists once its right-hand bars have closed. Processing candles incrementally gives the same result as replaying them all at once (this is tested). The first run replays all available history, so memory starts populated. A failed update rolls back completely.

## 2. Two brains

**Python is the sensor.** It requests an early Astra map in two cases:
- Live price enters an important remembered zone: 4h or higher, at least 2 tests, or an Astra level. The cooldown is per zone and only starts once the refresh was actually accepted.
- A recent memory event (break, retest, sweep, false breakout) happens on an important zone.

V6 has its own cooldown, so it never delays a V5 invalidation or structure re-map. It also doesn't force a new candle snapshot when history is already fresh.

**Astra is the analyst.** The map payload now includes:
- `market_memory`: nearest and most important zones, liquidity pools, events from the last 48h, persistent SMC zones, and the track record
- `sensor_trigger`: why Astra was called early
- `previous_analysis`

## 3. Trade ideas (V6.1: always given)

Every hourly analysis contains at least one **TRADE IDEA**: Astra's best trade in the direction it leans, the way a professional states a plan. Astra returns NO_TRADE only when the data is unusable.

| Field | Meaning |
|---|---|
| Confidence | 0–100 %: Astra's probability that TP1 is reached before the stop. Scored afterwards. |
| Risk | **Low**: confirmations align at a validated structure. **Medium**: some confirmation missing. **High**: aggressive entry, extended price or incomplete confirmation. |
| Entry type | MARKET (enter now; the entry zone contains the price), LIMIT (wait for a pullback into the zone), BREAKOUT (enter when price trades through) |
| Stop loss | Beyond real structure. Python names the remembered zone it sits behind. |
| TP1–TP3 | At remembered levels. Python names each one ("TP1 4219 — at Daily resistance 4205–4220 · tested 4×") and flags a TP1 that is not at a remembered level. |

**Python checks on every idea:**
- Geometry is validated, and R:R is recomputed from the worst edge of the entry zone.
- Risk is raised, never lowered, for: R:R below 1 (HIGH, "reward smaller than risk"), R:R below `minimum_rr` (MEDIUM), Python's 4H/1H structure opposing the direction, a USD holiday (MEDIUM) or a release window (HIGH).
- Price that has left the entry zone gets a "do not chase" factor.
- A MARKET entry becomes LIMIT when price is not in the zone.

**When ideas are withheld:** inside a timed high-impact release window, under fresh HIGH news risk, on stale candles or when the map was refused. USD holidays only raise the risk.

**Scoring:** every idea is scored. It counts as filled at once (MARKET) or when price touches the entry (LIMIT/BREAKOUT). It then scores PLAYED_OUT if TP1 is reached before the stop, FAILED if the stop comes first. The hit rate overall and for 70 %+ confidence calls goes into the daily report and back to Astra.

Structure watches remain optional extras. BUY/SELL signals (the V5 gate) are unchanged.

## 4. Learning history

Each scenario is stored with a measurable trigger, objective, invalidation and horizon. Python resolves it against closed candles:

| Outcome | Meaning |
|---|---|
| `PLAYED_OUT` | The objective was reached after the trigger. |
| `FAILED` | Invalidation came first after the trigger, or the trigger candle itself touched invalidation (ambiguous, so scored conservatively). |
| `NOT_TRIGGERED` | The horizon ended without the trigger, or invalidation came before it. |
| `EXPIRED_AFTER_TRIGGER` | Triggered, but neither the objective nor invalidation was reached before the horizon. |

Nothing is resolved while the candles for the horizon are missing. NEUTRAL scenarios are kept out of the primary and breakout statistics. The track record covers 30 days (primary vs alternative, breakout vs rejection, recent outcomes). It goes back to Astra for calibration and into the daily Telegram report.

## 5. Telegram

The reports follow the formats in the concept document, in Arabic (`telegram_language`, default) or English. They go through a low-priority queue, so a BUY/SELL signal is never delayed behind a report.

Deduplication: an idea is reported once per trading day per direction and entry area, and again only when it becomes valid at the current location. A watch is reported once per zone state (status and test count). There is at most one idea report and one watch report per `astra_report_min_interval_seconds`, and this throttle survives restarts.

The daily report adds Astra's view, the remembered levels nearest price and the track record.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `memory_enabled` | true | Persistent market memory |
| `astra_reports` | true | TRADE IDEA / STRUCTURE WATCH Telegram reports |
| `astra_report_min_interval_seconds` | 1800 | Per report kind |
| `zone_entry_refresh` | true | Price entering a remembered zone triggers an early Astra map |
| `zone_entry_cooldown_seconds` | 7200 | Per zone |

## Cost

The map payload grows by about 10–20 KB (memory and track record, roughly 3–5k tokens; the candles
themselves remain the largest part, ~25k tokens). The map output limit rises from 10k to 16k tokens because of the narrative. Early refreshes are limited by their own 15-minute cooldown and a 2-hour per-zone cooldown. To return to pure hourly maps, set `zone_entry_refresh: false`.

## Dry run (do this before going live)

`tools/astra_dry_run.py` makes one real Astra map call on the latest closed candles and prints
everything the live bot would do, without sending anything to Telegram or touching `runtime/`:
candle counts, the memory zones around price (also `zones.csv`), payload size, model tokens and
time, whether the executable map and the analyst narrative pass Python validation, and previews of
the IDEA / WATCH messages. Outputs go to `runtime_dryrun/`.

```powershell
.\.venv\Scripts\python.exe tools\astra_dry_run.py --offset-hours 3 --no-ai   # free: data + memory
.\.venv\Scripts\python.exe tools\astra_dry_run.py --offset-hours 3           # one Astra call
.\.venv\Scripts\python.exe tools\astra_dry_run.py --raw runtime_dryrun\astra_response.json  # re-render
```

`--offset-hours` is needed while the market is closed (the live bot infers it from fresh ticks):
broker server time minus UTC, e.g. 3 for GivTrade in summer time.

## Verification status

225 offline tests pass (29 of them new for V6), and an independent code review was run. The review found no path from V6 to a signal; its issues were fixed, including exception isolation, signal-first sending, idea vetoes, the retest/acceptance rules and scoring edge cases.

**Not verified:**
- live behaviour on your GivTrade terminal
- the model's real-world compliance with the larger schema
- the quality of Astra's ideas

Paper-monitor V6 before relying on its reports.
