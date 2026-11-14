"""
Session levels and session events from CLOSED UTC candles (live and replay use the same code).

Windows are defined in each market's own time zone, so daylight-saving changes are handled by
zoneinfo (London and New York change on different dates; Tokyo has no DST):

  ASIA       Asia/Tokyo        09:00-15:00   (00:00-06:00 UTC all year)
  LONDON     Europe/London     08:00-16:30
  NEW_YORK   America/New_York  08:00-17:00
  opening range: the first `or_minutes` of LONDON and NEW_YORK

A trading DAY is the New York day 17:00 -> 17:00 (the same day the signal bot and the broker's D1
candle use); a trading WEEK is Monday-Friday of those days.

Levels (only COMPLETED windows are levels; the running session is reported separately):
  ASIA_HIGH/LOW, LONDON_HIGH/LOW, NEW_YORK_HIGH/LOW (most recent completed instance each),
  LONDON_OR_HIGH/LOW, NEW_YORK_OR_HIGH/LOW (today's, once the opening range is complete),
  PDH/PDL (previous trading day), PWH/PWL (previous trading week).

Events on each closed 15M candle (a level is "live" until a candle CLOSES beyond it):
  SWEEP_RECLAIM    wick beyond a live level, close back inside            -> fade direction
  BREAKOUT         first close beyond the level                           -> breakout direction
  FAILED_BREAKOUT  a breakout, then within 4 candles a close back inside  -> fade direction
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from gold_smc_engine import _time

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
DEFAULT_SESSIONS = {
    "ASIA": {"tz": "Asia/Tokyo", "start": "09:00", "end": "15:00"},
    "LONDON": {"tz": "Europe/London", "start": "08:00", "end": "16:30"},
    "NEW_YORK": {"tz": "America/New_York", "start": "08:00", "end": "17:00"},
}
DEFAULT_OR_MINUTES = {"LONDON": 60, "NEW_YORK": 60}
FAIL_BARS = 4


def _hm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def windows(name: str, spec: dict, around: datetime, days: int = 9) -> list[tuple[datetime, datetime]]:
    """UTC [start, end) of the session on each local date near `around` (weekends skipped)."""
    tz = ZoneInfo(spec["tz"])
    local = around.astimezone(tz).date()
    out = []
    for k in range(days, -2, -1):
        d = local - timedelta(days=k)
        if d.weekday() >= 5:
            continue
        s = datetime.combine(d, _hm(spec["start"]), tz).astimezone(UTC)
        e = datetime.combine(d, _hm(spec["end"]), tz).astimezone(UTC)
        if e <= s:
            e += timedelta(days=1)
        out.append((s, e))
    return out


def trading_day(t: datetime) -> date:
    """New York trading day: 17:00 NY starts the next day's session."""
    loc = t.astimezone(NY)
    return (loc + timedelta(hours=7)).date()


def session_of(t: datetime, sessions: dict | None = None) -> str:
    """ASIA | LONDON | LONDON_NY (overlap) | NEW_YORK | OFF for a UTC time."""
    sessions = sessions or DEFAULT_SESSIONS
    inside = [n for n, sp in sessions.items() if any(s <= t < e for s, e in windows(n, sp, t, 1))]
    if "LONDON" in inside and "NEW_YORK" in inside:
        return "LONDON_NY"
    return inside[0] if inside else "OFF"


def _hilo(bars, s, e):
    sel = [b for b in bars if s <= _time(b["time"]) < e]
    if not sel:
        return None
    return max(b["high"] for b in sel), min(b["low"] for b in sel)


def levels(bars15: list, bars1h: list, as_of: datetime, sessions: dict | None = None,
           or_minutes: dict | None = None) -> list[dict]:
    """Completed session / opening-range / previous day / previous week levels at `as_of`.
    Only candles that CLOSED by as_of are used."""
    sessions = sessions or DEFAULT_SESSIONS
    or_minutes = DEFAULT_OR_MINUTES if or_minutes is None else or_minutes
    b15 = [b for b in bars15 if _time(b["time"]) + timedelta(minutes=15) <= as_of]
    b1h = [b for b in bars1h if _time(b["time"]) + timedelta(hours=1) <= as_of]
    out = []

    def add(name, side, price, formed, src):
        out.append({"name": name, "side": side, "price": round(price, 2), "formed_at": formed.isoformat(),
                    "source": src})

    for name, spec in sessions.items():
        wins = windows(name, spec, as_of)
        done = [(s, e) for s, e in wins if e <= as_of]
        for s, e in reversed(done):
            hl = _hilo(b15, s, e)
            if hl:
                add(f"{name}_HIGH", "HIGH", hl[0], e, "SESSION")
                add(f"{name}_LOW", "LOW", hl[1], e, "SESSION")
                break
        mins = or_minutes.get(name)
        if mins:
            for s, e in reversed(wins):
                or_end = s + timedelta(minutes=mins)
                if s <= as_of and or_end <= as_of and as_of - s < timedelta(hours=20):
                    hl = _hilo(b15, s, or_end)
                    if hl:
                        add(f"{name}_OR_HIGH", "HIGH", hl[0], or_end, "OPENING_RANGE")
                        add(f"{name}_OR_LOW", "LOW", hl[1], or_end, "OPENING_RANGE")
                    break

    src = b1h or b15
    step = timedelta(hours=1) if b1h else timedelta(minutes=15)
    today = trading_day(as_of)
    days: dict = {}
    for b in src:
        t = _time(b["time"])
        d = trading_day(t)
        hi, lo, end = days.get(d, (-1e18, 1e18, t))
        days[d] = (max(hi, b["high"]), min(lo, b["low"]), max(end, t + step))
    prev = [d for d in sorted(days) if d < today and d.weekday() < 5]
    if prev:
        hi, lo, end = days[prev[-1]]
        add("PDH", "HIGH", hi, end, "PREV_DAY")
        add("PDL", "LOW", lo, end, "PREV_DAY")
    week = lambda d: d.isocalendar()[:2]
    pw = [d for d in prev if week(d) < week(today)]
    if pw:
        wk = week(pw[-1])
        sel = [days[d] for d in pw if week(d) == wk]
        add("PWH", "HIGH", max(x[0] for x in sel), max(x[2] for x in sel), "PREV_WEEK")
        add("PWL", "LOW", min(x[1] for x in sel), max(x[2] for x in sel), "PREV_WEEK")
    return out


def rejection_wick(b: dict, high: bool) -> bool:
    """The wick on the swept side is at least as long as the body (the same rule the idea REJECTION uses)."""
    body = abs(b["close"] - b["open"])
    wick = (b["high"] - max(b["open"], b["close"])) if high else (min(b["open"], b["close"]) - b["low"])
    return wick >= body


def merge_same_price(lvls: list, tol: float = 0.0) -> list[dict]:
    """1.6.1: levels of the same side at the same price (within tol) are one level. The previous
    day's high is often the New York high (PDH == NEW_YORK_HIGH on 16 Oct), and two levels gave two
    identical setups / paper trades. The surviving level keeps the first name and lists the others
    in "also"; its formed_at is the earliest so the event scan covers the full history."""
    out: list[dict] = []
    for lv in lvls:
        twin = next((o for o in out if o["side"] == lv["side"] and abs(o["price"] - lv["price"]) <= tol), None)
        if twin is None:
            out.append(dict(lv, also=[]))
            continue
        twin["also"].append(lv["name"])
        if lv["formed_at"] < twin["formed_at"]:
            twin["formed_at"] = lv["formed_at"]
    return out


def events(bars15: list, lvls: list, as_of: datetime, last_n: int = 1, merge_tol: float = 0.0) -> list[dict]:
    """Session events on the last `last_n` closed 15M candles (oldest first). Levels at one price
    are merged first (merge_same_price), so one sweep is one event."""
    closed = [b for b in bars15 if _time(b["time"]) + timedelta(minutes=15) <= as_of]
    if not closed:
        return []
    lvls = merge_same_price(lvls, merge_tol)
    out = []
    first_check = len(closed) - last_n
    for lv in lvls:
        formed = _time(lv["formed_at"])
        p, high = lv["price"], lv["side"] == "HIGH"
        live, broke_at = True, None
        seq = [b for b in closed if _time(b["time"]) >= formed]
        for k, b in enumerate(seq):
            idx = len(closed) - len(seq) + k
            beyond = b["close"] > p if high else b["close"] < p
            ev = None
            if live and beyond:
                live, broke_at = False, k
                ev = ("BREAKOUT", "BUY" if high else "SELL")
            elif live and ((b["high"] > p and b["close"] < p) if high else (b["low"] < p and b["close"] > p)) \
                    and rejection_wick(b, high):
                # 1.10: the candle must actually reject the level: the wick beyond the close at least as long as
                # the body. 26 Oct 14:30 (o 4007.89 h 4023.08 c 4022.24) "swept" the London OR high 4022.34 by
                # 10 cents at the end of a 14-point bullish bar (wick 0.84, body 14.35) and confirmed two SELLs.
                ev = ("SWEEP_RECLAIM", "SELL" if high else "BUY")
            elif not live and broke_at is not None and k - broke_at <= FAIL_BARS and not beyond:
                ev = ("FAILED_BREAKOUT", "SELL" if high else "BUY")
                broke_at = None                         # one failure per breakout; level is spent
            if ev and idx >= first_check:
                out.append({"event": ev[0], "direction": ev[1], "level": lv["name"], "price": p,
                            "source": lv["source"], "bar_time": b["time"], "high": b["high"], "low": b["low"],
                            "close": b["close"], "also": list(lv.get("also") or [])})
    out.sort(key=lambda x: x["bar_time"])
    return out


def snapshot(bars15: list, bars1h: list, as_of: datetime, sessions=None, or_minutes=None) -> dict:
    lv = levels(bars15, bars1h, as_of, sessions, or_minutes)
    return {"session": session_of(as_of, sessions), "levels": lv, "events": events(bars15, lv, as_of)}
