"""
Time-of-day ("clock") candidate engine for the replay (30 Oct 2026).

Tests the documented intraday seasonality of gold (Batten, Lucey, McGroarty, Peat & Urquhart, "Gold price dynamics
around the clock"; overnight-vs-day return studies; the London PM-fix pattern): returns tend to be positive through
the Asian/overnight hours and negative through the London/New York day. A pure time rule, no indicators, no AI:

  OVERNIGHT_LONG   buy at `entry` New York time (18:45, after the open's wide spread and the 30-min skip), exit at `exit` New York time next morning
  DAY_SHORT        sell at `entry` New York time, exit at `exit` (the London PM fix is 15:00 London = 10:00/11:00 NY)

Every variant is MARKET in, time out, with a protective stop at `stop_atr` x ATR15 (the risk unit for R; 6 x ATR
is "practically no stop"). No target. One trade per variant per trading day. All times are New York local, so the
London/New York daylight-saving changes are handled; the London fix drifts by an hour for the few weeks when only
one side has changed, which is accepted noise.

Decisions are taken at the first closed 15M candle at or after the entry time (the replay's clock), filled on the
next 1-minute bar's open + spread + slippage, like every other replay trade.
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
NY = ZoneInfo("America/New_York")

# name: (direction, entry NY time, exit NY time, exit is next calendar day?, stop in ATR15)
VARIANTS = {
    "CLOCK_long_1845_0300": ("BUY", dtime(18, 45), dtime(3, 0), True, 3.0),
    "CLOCK_long_1845_0530": ("BUY", dtime(18, 45), dtime(5, 30), True, 3.0),      # to the London AM fix (10:30 London)
    "CLOCK_long_1845_0800": ("BUY", dtime(18, 45), dtime(8, 0), True, 3.0),
    "CLOCK_long_1845_0530_tight": ("BUY", dtime(18, 45), dtime(5, 30), True, 1.5),
    "CLOCK_long_1845_0530_nostop": ("BUY", dtime(18, 45), dtime(5, 30), True, 6.0),
    "CLOCK_long_0000_0530": ("BUY", dtime(0, 0), dtime(5, 30), False, 3.0),       # Asia proper -> AM fix
    "CLOCK_short_0800_1100": ("SELL", dtime(8, 0), dtime(11, 0), False, 3.0),     # NY morning into/after the PM fix
    "CLOCK_short_0800_1655": ("SELL", dtime(8, 0), dtime(16, 55), False, 3.0),   # before the 17:00 break
    "CLOCK_short_0300_1100": ("SELL", dtime(3, 0), dtime(11, 0), False, 3.0),     # London open -> after the PM fix
    "CLOCK_short_0800_1100_nostop": ("SELL", dtime(8, 0), dtime(11, 0), False, 6.0),
}


def due(variant: str, as_of: datetime) -> datetime | None:
    """If the 15M close `as_of` (UTC) is the first close at/after today's entry time (New York), return the planned
    exit instant (UTC); otherwise None. Saturdays never trade; a Friday entry whose exit falls on Saturday is skipped; Sunday evening entries are allowed."""
    direction, t_in, t_out, next_day, _ = VARIANTS[variant]
    ny = as_of.astimezone(NY)
    if ny.weekday() == 5:
        return None                                   # Saturday; Sunday evening IS a session (the week opens 18:00 NY)
    entry_dt = datetime.combine(ny.date(), t_in, NY)
    if not (entry_dt <= ny < entry_dt + timedelta(minutes=15)):
        return None
    exit_day = ny.date() + timedelta(days=1 if next_day else 0)
    if exit_day.weekday() == 5:
        return None                                   # a Friday overnight would exit into the weekend
    exit_dt = datetime.combine(exit_day, t_out, NY)
    if exit_dt <= ny:
        return None
    return exit_dt.astimezone(UTC)


def plan(variant: str, price_bid: float, sp: float, atr15: float) -> dict:
    """MARKET order with a protective stop at stop_atr x ATR15 and a far target (time exit is the real exit)."""
    direction, _, _, _, k = VARIANTS[variant]
    buy = direction == "BUY"
    entry = price_bid + (sp if buy else 0.0)
    risk = k * atr15
    stop = round(entry - risk if buy else entry + risk, 2)
    tp1 = round(entry + 20 * atr15 if buy else entry - 20 * atr15, 2)
    return {"direction": direction, "order_type": "MARKET", "entry": entry, "stop": stop, "tp1": tp1, "tp2": None,
            "risk": round(risk, 2)}
