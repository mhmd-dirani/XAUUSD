"""
1.17: news days are traded like normal days, with stricter entries (news_mode "strict").

Owner, 10 Nov: "I want to stop the news lock. Keep it the same as a normal day but stricter rules: don't enter unless
there is a strong indicator." Until 1.17 every new order was blocked from news_before_minutes before to
news_after_minutes after a major USD release (and all day on a USD holiday).

In strict mode, inside that same window, an order is sent only when ALL of these hold (Executor.send checks every
order, lab and live; the lab news straddle is exempt, it exists to test the release itself):
  1. it is a MARKET entry (a new resting limit would fill on the spike with no indicator at that moment;
     limits placed BEFORE the window stay open, as the owner chose);
  2. the last CLOSED 15M candle is a strong candle in the order's direction: BUY = it closed up, its body is at least
     half its range, and it closed above the previous candle's high (SELL mirrored);
  3. that candle is not the release candle (a 15M candle whose 15 minutes contain the release is the spike itself);
  4. risk is capped at news_strict_risk_pct (Validator.lots).
Unchanged: a stale or missing calendar still blocks every order (unknown is not "no events"), and the spread, quote
age, 2R, loss-limit and kill-switch gates all still apply. The V6 signal bot keeps its own lock (alerts only).
"""

from __future__ import annotations

from datetime import datetime, timedelta


class NewsHold(RuntimeError):
    """An order refused by the strict news rule. Callers log it quietly and may retry later (not a broker error)."""


def strong(direction: str, bar, prev) -> bool:
    """bar / prev = (t, o, h, l, c)."""
    _t, o, h, l, c = bar[:5]
    rng = h - l
    if rng <= 0:
        return False
    body = abs(c - o)
    if direction == "BUY":
        return c > o and body >= 0.5 * rng and c > prev[2]
    return c < o and body >= 0.5 * rng and c < prev[3]


def hold_reason(market, c: dict, order: dict, now: datetime) -> str | None:
    """Why the strict news rule refuses this order right now, or None."""
    if c.get("news_mode", "lock") != "strict":
        return None
    if ((order.get("meta") or {}).get("playbook") == "NEWS_STRADDLE"):
        return None
    window = market.news_window(now)
    if not window:
        return None
    if order.get("order_type") != "MARKET":
        return f"news window ({window}): only market entries on a strong 15M candle, no new resting orders"
    bars = market.rates("TIMEFRAME_M15", 3)
    if len(bars) < 2:
        return f"news window ({window}): no closed 15M candles to judge"
    bar, prev = bars[-1], bars[-2]
    rel = market.last_release(now, 120)
    if rel is not None:
        t0 = datetime.fromtimestamp(bar[0], rel.tzinfo)
        if t0 <= rel < t0 + timedelta(minutes=15):
            return f"news window ({window}): the last 15M candle is the release candle; waiting for the next one"
    if not strong(order["direction"], bar, prev):
        return (f"news window ({window}): the last 15M candle (o {bar[1]:.2f} h {bar[2]:.2f} l {bar[3]:.2f} "
                f"c {bar[4]:.2f}) is not a strong {order['direction']} candle")
    return None
