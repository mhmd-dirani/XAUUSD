"""
11 Nov (owner): "Astra may place a limit at an order block, FVG or support/resistance, but price may cross it or retest
it; the 5M and 1M can show the bot what will happen, and how the 4H candle closes in that range."

The zone-reaction study: every LIMIT idea Astra wrote (signal bot memory, read-only) replayed two ways on MT5's own
M1 candles, nothing sent, nothing changed:

    python src/auto_trader.py --zone-study --days 30 [--spread 0.3]

  LIMIT     the live way: the limit at the trader's own level (zone edge nearest price, moved in for 2R), filled when
            price trades through it inside the idea's life (until the next analysis, at most idea_order_hours),
            cancelled if TP1 trades first.
  REACTION  wait for price to touch the zone, then read the closed 5M candles and the 1M structure (BUY at support;
            SELL is the mirror):
              HOLD      a bullish 5M candle closes back ABOVE the zone, 1M structure BULLISH, 5M not BEARISH -> market
              BROKEN    a 5M candle closes BELOW the zone with 1M structure BEARISH (or the idea's stop trades) -> no BUY
              4H close  undecided at the close of the 4H candle (the next one if the touch came in its last hour):
                        close above the zone -> HOLD_4H (market at that close); inside or below -> WEAK_4H, no trade
            Entries keep Astra's stop and TP1 and need 2R at the entry price (else HOLD_LATE, no trade).
  FLIP      exploratory, after BROKEN: the zone retested from the other side (5M high back into it, bearish close
            below it, 1M BEARISH) within 4 h -> the opposite trade, stop above the last 6 5M highs + 0.1 ATR, 2R.

Also, for every LIMIT fill: the 5M and 1M structure at the fill minute (WITH / AGAINST / MIXED the trade).
Point-in-time: every decision uses closed candles only; times converted per candle from broker time (New York + 7 h)
so October (UTC+3) and November (UTC+2) line up. Caveats in the report.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import protect as PR
from . import sessions as SS
from .conflict import trend

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
WATCH_HOURS = 8           # a touched zone is watched this long for a decision
FLIP_HOURS = 4
HOLD_HOURS = 72           # the longest a simulated trade is followed


# ----------------------------------------------------------------------------- time and candles
def server_to_utc(server_ts: int) -> int:
    """GivTrade server time is New York time + 7 h (UTC+3 summer, UTC+2 winter)."""
    local = datetime.fromtimestamp(server_ts, UTC).replace(tzinfo=None) - timedelta(hours=7)
    return int(local.replace(tzinfo=NY).astimezone(UTC).timestamp())


def rule_offset(utc_ts: float) -> int:
    return int(datetime.fromtimestamp(utc_ts, NY).utcoffset().total_seconds()) + 7 * 3600


def fetch_m1(market, from_utc: float, to_utc: float) -> list:
    """M1 candles (utc, o, h, l, c, server_ts) oldest first, in 5-day chunks, read-only."""
    out, seen = [], set()
    t = from_utc
    while t < to_utc:
        b = min(to_utc, t + 5 * 86400)
        a_srv = datetime.fromtimestamp(t + rule_offset(t) - 3600, UTC)
        b_srv = datetime.fromtimestamp(b + rule_offset(b) + 3600, UTC)
        rows = market.trade_call("copy_rates_range", market.symbol, market.const("TIMEFRAME_M1"), a_srv, b_srv)
        for r in rows if rows is not None else []:
            st = int(r["time"])
            u = server_to_utc(st)
            if u not in seen and from_utc <= u < to_utc:
                seen.add(u)
                out.append((u, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), st))
        t = b
    out.sort()
    return out


class Bars5(list):
    """5M candles with their close times (for bisect)."""
    ends: list = []


def five_minute(m1: list) -> Bars5:
    """5M candles (start, o, h, l, c, end) from M1 (UTC 5-minute blocks; the broker's are the same blocks)."""
    out, block = Bars5(), []
    for bar in m1:
        if block and block[0][0] // 300 != bar[0] // 300:
            out.append(_agg(block, 300))
            block = []
        block.append(bar)
    if block:
        out.append(_agg(block, 300))
    out.ends = [b[5] for b in out]
    return out


def four_hour_close(m1: list, i: int):
    """(close time UTC, close price) of the broker 4H candle that contains m1[i]; None if it is not complete in m1."""
    st = m1[i][5]
    end_srv = st - st % 14400 + 14400
    j = i
    while j + 1 < len(m1) and m1[j + 1][5] < end_srv:
        j += 1
    if j + 1 >= len(m1):
        return None
    return m1[j][0] + 60, m1[j][4]


def _agg(block, sec):
    t0 = block[0][0] - block[0][0] % sec
    return (t0, block[0][1], max(x[2] for x in block), min(x[3] for x in block), block[-1][4], t0 + sec)


def _atr(bars: list, n: int = 14) -> float:
    tr = [max(b[2] - b[3], abs(b[2] - a[4]), abs(b[3] - a[4])) for a, b in zip(bars[-n - 1:-1], bars[-n:])]
    return sum(tr) / len(tr) if tr else 0.0


# ----------------------------------------------------------------------------- ideas
def load_ideas(memory_path: Path, since: datetime, until: datetime) -> list:
    con = sqlite3.connect(f"file:{memory_path.as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        rows = con.execute("SELECT id, created_at, data FROM analyses ORDER BY created_at").fetchall()
    finally:
        con.close()
    analyses = []
    for aid, created, data in rows:
        t = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=UTC)
        analyses.append((t, aid, json.loads(data)))
    out = []
    for k, (t, aid, a) in enumerate(analyses):
        if not since <= t <= until:
            continue
        nxt = analyses[k + 1][0] if k + 1 < len(analyses) else None
        for idea in a.get("ideas") or []:
            out.append({"analysis": aid, "created": t, "next": nxt, **idea})
    return out


def limit_level(buy: bool, lo: float, hi: float, stop: float, tp1: float, min_rr: float):
    """The trader's own LIMIT level (bridge.idea_to_order); None when 2R is not available inside the zone."""
    r = min_rr + 0.02
    need = (tp1 + r * stop) / (1 + r)
    level = (min(hi, need) if need >= lo else lo) if buy else (max(lo, need) if need <= hi else hi)
    if (buy and level < lo) or (not buy and level > hi) or (buy and need < lo) or (not buy and need > hi):
        return None
    return round(level, 2)


# ----------------------------------------------------------------------------- one idea
def _idx(m1: list, ts: float) -> int:
    """First M1 candle starting at or after ts."""
    return bisect_left(m1, (ts,))


def _trend_at(m1: list, m5: list, end_ts: float, buy: bool) -> tuple[str, str, str]:
    """(5M structure, 1M structure, WITH/AGAINST/MIXED) from candles that CLOSED by end_ts."""
    i = _idx(m1, end_ts - 59)                         # m1 bars that closed by end_ts: start <= end_ts - 60
    tr1 = trend([b[:5] for b in m1[max(0, i - 70):i]], 60, 2)
    j = bisect_right(m5.ends, end_ts)                 # 5M candles that closed by end_ts
    tr5 = trend([b[:5] for b in m5[max(0, j - 70):j]], 60, 2)
    want, against = ("BULLISH", "BEARISH") if buy else ("BEARISH", "BULLISH")
    label = "WITH" if tr5 == want and tr1 == want else "AGAINST" if tr5 == against and tr1 == against else "MIXED"
    return tr5, tr1, label


def _sim(buy, entry, sl, tp, m1, from_ts, spread, cfg):
    i = _idx(m1, from_ts)
    bars = [b[:5] for b in m1[i:] if b[0] < from_ts + HOLD_HOURS * 3600]
    return PR.simulate(buy, entry, sl, tp, bars, spread, cfg)


def study_idea(idea: dict, m1: list, m5: list, spread: float, min_rr: float, cfg: dict | None, max_hours: float) -> dict:
    buy = idea["direction"] == "BUY"
    lo, hi = float(idea["entry_low"]), float(idea["entry_high"])
    stop, tp1 = float(idea["invalidation"]), float(idea["targets"][0])
    t0 = idea["created"].timestamp()
    t0 = t0 - t0 % 60 + 60
    t_end = min(t0 + max_hours * 3600, idea["next"].timestamp()) if idea.get("next") else t0 + max_hours * 3600
    row = {"analysis": idea["analysis"], "created_utc": idea["created"].strftime("%Y-%m-%d %H:%M"),
           "idea": idea.get("id"), "direction": idea["direction"], "trade_class": idea.get("trade_class"),
           "confidence": idea.get("confidence"), "risk": idea.get("risk"), "zone_low": lo, "zone_high": hi,
           "stop": stop, "tp1": tp1, "skip": None, "level": None, "limit": None, "limit_r": None, "limit_prot_r": None,
           "fill_utc": None, "fill_tr5": None, "fill_tr1": None, "fill_trend": None, "touch_utc": None, "session": None,
           "reaction": None, "reaction_entry": None, "reaction_r": None, "reaction_prot_r": None,
           "late_r": None, "tight_stop": None, "tight_r": None, "flip": None, "flip_r": None}
    i0 = _idx(m1, t0)
    if i0 == 0 or i0 >= len(m1):
        row["skip"] = "no candles"
        return row
    p0 = m1[i0 - 1][4]
    if (buy and p0 <= hi) or (not buy and p0 >= lo):
        row["skip"] = "price already in/through the zone"
        return row
    if (buy and not stop < lo) or (not buy and not stop > hi) or (buy and tp1 <= hi) or (not buy and tp1 >= lo):
        row["skip"] = "stop or TP1 on the wrong side"
        return row
    level = limit_level(buy, lo, hi, stop, tp1, min_rr)
    row["level"] = level

    # --- LIMIT (only when the trader would have placed it)
    if level is None:
        row["limit"] = "NO_2R_IN_ZONE"
    else:
        row["limit"] = "NOT_FILLED"
        for b in m1[i0:]:
            if b[0] >= t_end:
                break
            if (buy and b[2] >= tp1) or (not buy and b[3] <= tp1):
                row["limit"] = "CANCELLED_TP1"
                break
            if (buy and b[3] + spread <= level) or (not buy and b[2] >= level):
                row["limit"] = "FILLED"
                row["fill_utc"] = datetime.fromtimestamp(b[0], UTC).strftime("%Y-%m-%d %H:%M")
                hit_stop = (b[3] <= stop) if buy else (b[2] + spread >= stop)
                if hit_stop:
                    row["limit_r"] = row["limit_prot_r"] = -1.0
                else:
                    off = _sim(buy, level, stop, tp1, m1, b[0] + 60, spread, None)
                    on = _sim(buy, level, stop, tp1, m1, b[0] + 60, spread, cfg)
                    row["limit_r"], row["limit_prot_r"] = off["r"], on["r"]
                row["fill_tr5"], row["fill_tr1"], row["fill_trend"] = _trend_at(m1, m5, b[0], buy)
                break

    # --- REACTION
    touch = None
    for k in range(i0, len(m1)):
        b = m1[k]
        if b[0] >= t_end:
            break
        if (buy and b[2] >= tp1) or (not buy and b[3] <= tp1):
            break
        if (buy and b[3] <= hi) or (not buy and b[2] >= lo):
            touch = k
            break
    if touch is None:
        row["reaction"] = "NO_TOUCH"
        return row
    tt = m1[touch][0]
    row["touch_utc"] = datetime.fromtimestamp(tt, UTC).strftime("%Y-%m-%d %H:%M")
    row["session"] = SS.session_of(datetime.fromtimestamp(tt, UTC))
    h4 = four_hour_close(m1, touch)
    if h4 and h4[0] - tt < 3600:
        nxt = _idx(m1, h4[0])
        h4 = four_hour_close(m1, nxt) if nxt < len(m1) else None
    decision, entry, at = None, None, None
    stop_seen = False
    for c5 in m5[bisect_right(m5.ends, tt + 60):]:
        if c5[0] > tt + WATCH_HOURS * 3600:
            break
        if (c5[3] <= stop) if buy else (c5[2] + spread >= stop):
            stop_seen = True
        tr5, tr1, _ = _trend_at(m1, m5, c5[5], buy)
        want, against = ("BULLISH", "BEARISH") if buy else ("BEARISH", "BULLISH")
        if stop_seen:
            decision, at = "BROKEN_STOP", c5[5]
            break
        if (buy and c5[4] > hi and c5[4] > c5[1]) or (not buy and c5[4] < lo and c5[4] < c5[1]):
            if tr1 == want and tr5 != against:
                decision, at, entry = "HOLD", c5[5], (c5[4] + spread if buy else c5[4])
                break
        if (buy and c5[4] < lo) or (not buy and c5[4] > hi):
            if tr1 == against:
                decision, at = "BROKEN", c5[5]
                break
        if h4 and c5[5] >= h4[0]:
            close4 = h4[1]
            if (buy and close4 > hi) or (not buy and close4 < lo):
                decision, at, entry = "HOLD_4H", h4[0], (close4 + spread if buy else close4)
            else:
                decision, at = "WEAK_4H", h4[0]
            break
    if decision is None:
        row["reaction"] = "UNDECIDED"
        return row
    row["reaction"] = decision
    if entry is not None:
        row["reaction_entry"] = round(entry, 2)
        rr = abs(tp1 - entry) / abs(entry - stop) if abs(entry - stop) > 0 else 0
        r_off = _sim(buy, entry, stop, tp1, m1, at, spread, None)["r"]
        if rr >= min_rr:
            row["reaction_r"] = r_off
            row["reaction_prot_r"] = _sim(buy, entry, stop, tp1, m1, at, spread, cfg)["r"]
        else:
            row["reaction"] = decision.replace("HOLD", "HOLD_LATE")
            row["late_r"] = r_off                         # information only: the same entry below 2R
        # the tighter stop: beyond the 5M candles of the reaction (touch -> decision) + 0.1 ATR14 of 5M
        k0, k1 = bisect_right(m5.ends, tt), bisect_right(m5.ends, at)
        react = m5[k0:k1] or m5[max(0, k1 - 1):k1]
        a5 = _atr(m5[max(0, k1 - 15):k1])
        tight = (min(b[3] for b in react) - 0.1 * a5) if buy else (max(b[2] for b in react) + spread + 0.1 * a5)
        if (buy and tight < stop) or (not buy and tight > stop):
            tight = stop
        rr_t = abs(tp1 - entry) / abs(entry - tight) if abs(entry - tight) > 0 else 0
        if (buy and tight < entry or not buy and tight > entry) and rr_t >= min_rr:
            row["tight_stop"] = round(tight, 2)
            row["tight_r"] = _sim(buy, entry, tight, tp1, m1, at, spread, None)["r"]
    if decision == "BROKEN":
        _flip(row, buy, lo, hi, m1, m5, at, spread, min_rr)
    return row


def _flip(row, buy, lo, hi, m1, m5, t_break, spread, min_rr):
    """After a BROKEN zone: the retest from the other side, traded the other way (exploratory)."""
    k = bisect_right(m5.ends, t_break)
    seen = list(m5[max(0, k - 20):k])
    for c5 in m5[k:]:
        if c5[0] > t_break + FLIP_HOURS * 3600:
            return
        seen.append(c5)
        if (buy and c5[4] > hi) or (not buy and c5[4] < lo):          # back on the original side: no flip
            return
        _tr5, tr1, _ = _trend_at(m1, m5, c5[5], not buy)
        retest = (buy and c5[2] >= lo and c5[4] < lo and c5[4] < c5[1] and tr1 == "BEARISH") or \
                 (not buy and c5[3] <= hi and c5[4] > hi and c5[4] > c5[1] and tr1 == "BULLISH")
        if retest:
            a = _atr(seen[-15:])
            if buy:        # the flip is a SELL
                entry = c5[4]
                sl = max(b[2] for b in seen[-6:]) + spread + 0.1 * a
                tp = entry - min_rr * (sl - entry)
            else:
                entry = c5[4] + spread
                sl = min(b[3] for b in seen[-6:]) - 0.1 * a
                tp = entry + min_rr * (entry - sl)
            row["flip"] = "FLIP_SELL" if buy else "FLIP_BUY"
            row["flip_r"] = _sim(not buy, entry, sl, tp, m1, c5[5], spread, None)["r"]
            return


# ----------------------------------------------------------------------------- the study and the report
def analyse(ideas: list, m1: list, spread: float, min_rr: float, cfg: dict | None, max_hours: float) -> list:
    m5 = five_minute(m1)
    rows, seen = [], []
    for idea in ideas:
        if idea.get("entry_type", "LIMIT") != "LIMIT":
            continue
        try:
            for k in ("entry_low", "entry_high", "invalidation"):
                float(idea[k])
            float(idea["targets"][0])
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        r = study_idea(idea, m1, m5, spread, min_rr, cfg, max_hours)
        key = (r["direction"], r["touch_utc"] or r["fill_utc"])
        if key[1] and any(s[0] == key[0] and s[1] == key[1] and s[2] <= r["zone_high"] and r["zone_low"] <= s[3]
                          for s in seen):
            r["skip"] = "repeat of an earlier idea at the same touch"
        elif key[1]:
            seen.append((key[0], key[1], r["zone_low"], r["zone_high"]))
        rows.append(r)
    return rows


def _line(label, rs):
    rs = [x for x in rs if x is not None]
    if not rs:
        return f"  {label:34} 0 trades"
    wins = sum(1 for x in rs if x > 0.05)
    return (f"  {label:34} {len(rs):3d} trades | win {100 * wins / len(rs):5.1f}% | total {sum(rs):+6.1f}R | "
            f"avg {sum(rs) / len(rs):+.2f}R")


def report(rows: list, days: int, spread: float) -> list:
    u = [r for r in rows if not r["skip"]]
    lines = ["", f"=== ZONE REACTION STUDY: Astra LIMIT ideas, last {days} days (M1 replay, spread {spread}) ===", "",
             f"LIMIT ideas read {len(rows)} | studied {len(u)} | skipped: " +
             ", ".join(f"{k} {sum(1 for r in rows if r['skip'] == k)}" for k in sorted({r['skip'] for r in rows if r['skip']}))]
    lines += ["", "--- 1. Same ideas, two entries (unmanaged = stop/TP1 only; protected = 1.15 trade protection) ---"]
    for cls in (None, "TREND", "COUNTER_TREND"):
        g = [r for r in u if cls is None or r["trade_class"] == cls]
        name = cls or "ALL"
        lines += [f" {name}:",
                  _line("LIMIT (live today)", [r["limit_r"] for r in g if r["limit"] == "FILLED"]),
                  _line("LIMIT protected", [r["limit_prot_r"] for r in g if r["limit"] == "FILLED"]),
                  _line("REACTION, Astra's stop", [r["reaction_r"] for r in g]),
                  _line("REACTION, Astra's stop, protected", [r["reaction_prot_r"] for r in g]),
                  _line("REACTION, tight 5M stop", [r["tight_r"] for r in g]),
                  _line("info: HOLD below 2R, Astra's stop", [r["late_r"] for r in g])]
    lines += ["", "--- 2. Reactions at the touched zones ---"]
    for lab in ("HOLD", "HOLD_4H", "HOLD_LATE", "HOLD_LATE_4H", "BROKEN", "BROKEN_STOP", "WEAK_4H", "UNDECIDED"):
        g = [r for r in u if r["reaction"] == lab]
        if g:
            rs = [r["reaction_r"] if r["reaction_r"] is not None else r["late_r"] for r in g]
            lines.append(_line(f"{lab} ({len(g)} zones)", rs) if lab.startswith("HOLD")
                         else f"  {lab:34} {len(g):3d} zones (no trade)")
    lines += ["", "--- 3. The LIMIT fills, by what the reaction check said about the same zone ---",
              "      (would the check have kept us out of the losing limits?)"]
    filled = [r for r in u if r["limit"] == "FILLED"]
    for lab in sorted({r["reaction"] or "-" for r in filled}):
        lines.append(_line(f"limit filled, reaction {lab}", [r["limit_r"] for r in filled if (r["reaction"] or "-") == lab]))
    lines += ["", "--- 4. The LIMIT fills, by 5M + 1M structure at the fill minute ---"]
    for lab in ("WITH", "MIXED", "AGAINST"):
        lines.append(_line(f"5M+1M {lab} the trade", [r["limit_r"] for r in filled if r["fill_trend"] == lab]))
    lines += ["", "--- 5. FLIP after a broken zone (exploratory, 2R) ---",
              _line("flip trades", [r["flip_r"] for r in u if r["flip"]]),
              "", "--- 5b. The same zones the LIMIT lost on: what a BROKEN read would have saved ---",
              _line("limit losers the check called BROKEN", [r["limit_r"] for r in filled
                                                              if r["reaction"] in ("BROKEN", "BROKEN_STOP") and (r["limit_r"] or 0) < 0]),
              _line("limit winners the check called BROKEN", [r["limit_r"] for r in filled
                                                               if r["reaction"] in ("BROKEN", "BROKEN_STOP") and (r["limit_r"] or 0) > 0])]
    lines += ["", "--- 6. By session of the touch (LIMIT fills | REACTION trades, Astra's stop) ---"]
    for s in ("ASIA", "LONDON", "LONDON_NY", "NEW_YORK", "OFF"):
        g = [r for r in u if r["session"] == s]
        if g:
            lines.append(_line(f"{s} limit", [r["limit_r"] for r in g if r["limit"] == "FILLED"]))
            lines.append(_line(f"{s} reaction", [r["reaction_r"] for r in g]))
    lines += ["", "Caveats: ideas are Astra's raw ideas, not the trader's filtered orders (no tier, session, news, zone or",
              "risk filters); BID M1 candles; stop checked before target inside a minute; TP1 only; repeated ideas at",
              "the same touch counted once; groups under 30 trades are screening, not proof."]
    return lines


def run(market, memory_path: Path, csv_path: Path, days: int, spread: float, min_rr: float, cfg: dict | None,
        max_hours: float, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    cfg = dict(PR.DEFAULTS, **(cfg or {}))
    if not memory_path.exists():
        return f"No signal bot memory at {memory_path}"
    off_now = market.offset()
    if off_now != rule_offset(now.timestamp()):
        return (f"Broker offset {off_now / 3600:+.0f} h does not match the New York + 7 h rule "
                f"({rule_offset(now.timestamp()) / 3600:+.0f} h); the study's times would be wrong, so it stops.")
    since = now - timedelta(days=days)
    ideas = load_ideas(memory_path, since, now)
    if not ideas:
        return "No Astra ideas in this period."
    m1 = fetch_m1(market, since.timestamp() - 6 * 3600, now.timestamp())
    m1 = [b for b in m1 if b[0] + 60 <= now.timestamp()]
    if len(m1) < 1000:
        return f"Only {len(m1)} M1 candles from MT5 for this period; open the M1 chart once so the terminal loads history."
    rows = analyse(ideas, m1, spread, min_rr, cfg, max_hours)
    lines = report(rows, days, spread)
    lines.insert(2, f"M1 candles {len(m1)} | {datetime.fromtimestamp(m1[0][0], UTC):%Y-%m-%d %H:%M} -> "
                    f"{datetime.fromtimestamp(m1[-1][0], UTC):%Y-%m-%d %H:%M} UTC")
    if rows and csv_path:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        lines.append(f"CSV: {csv_path}")
    return "\n".join(lines)
