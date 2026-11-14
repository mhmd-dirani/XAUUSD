"""
Bridge to the signal bot: one Opus analysis, two uses.

The V6 signal bot (gold_monitor.py) already writes, in its state_directory (runtime/):
  astra_analysis.json   every validated analysis: TRADE IDEAs with entry type, entry zone, stop,
                        targets, confidence and risk (written right after each Opus map)
  signals.jsonl         every confirmed BUY/SELL signal, written the moment it passes final_check
                        (before Telegram)
The trader turns them into orders here, with NO Claude call of its own. Every order still goes
through the trader's risk engine (2R, spread, news lock, duplicates, total risk, loss limits).
"""

from __future__ import annotations

import os

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import gold_monitor as GM
import user_plans as UP

UTC = timezone.utc
LOG = logging.getLogger("autotrader.bridge")


def _read_json(path: Path):
    for attempt in range(3):                      # OneDrive / antivirus may hold the file briefly
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (PermissionError, json.JSONDecodeError):
            time.sleep(0.1 * (attempt + 1))
    return None


class Bridge:
    def __init__(self, c, state):
        self.c, self.state = c, state
        folder = Path(c["v6"]["state_directory"])
        self.folder = folder if folder.is_absolute() else GM.ROOT / folder
        self.analysis_path = self.folder / "astra_analysis.json"
        self.signal_path = self.folder / (c["v6"].get("signal_file") or "signals.jsonl")

    # ------------------------------------------------------------------ V6 market memory (read-only)
    def memory_cursor(self, tf: str = "1h"):
        """1.20: the last candle (UTC) the signal bot's memory processed on `tf`, or None (read-only)."""
        path = self.folder / "astra_memory.sqlite3"
        if not path.exists():
            return None
        import sqlite3
        try:
            con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
            try:
                row = con.execute("SELECT value FROM meta WHERE key = ?", (f"cursor:{tf}",)).fetchone()
            finally:
                con.close()
            return GM.stamp(json.loads(row[0])) if row else None
        except (sqlite3.Error, ValueError, TypeError) as exc:
            LOG.warning("Signal bot memory cursor unreadable (%s)", exc)
            return None

    def memory_zones(self) -> list:
        path = self.folder / "astra_memory.sqlite3"
        if not path.exists():
            return []
        import sqlite3
        try:
            con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
            try:
                return [json.loads(d) for (d,) in con.execute("SELECT data FROM zones")]
            finally:
                con.close()
        except sqlite3.Error as exc:
            LOG.warning("Signal bot memory unreadable (%s); counter-trend ideas wait", exc)
            return []

    # ------------------------------------------------------------------ confirmed signals
    def new_signals(self, now: datetime) -> list:
        """Signals appended since the last read; each key once; older than signal_max_age_seconds skipped."""
        if not self.c["signal_bot_signals"] or not self.signal_path.exists():
            return []
        offset = self.state.get("bridge:signal_offset", 0)
        size = self.signal_path.stat().st_size
        if size < offset:                          # file was deleted/rotated
            offset = 0
        if size == offset:
            return []
        try:
            with open(self.signal_path, "rb") as f:
                f.seek(offset)
                chunk = f.read()
        except PermissionError:
            return []
        end = chunk.rfind(b"\n") + 1               # only complete lines
        if end == 0:
            return []
        self.state.put("bridge:signal_offset", offset + end)
        seen = self.state.get("bridge:signal_keys", [])
        out = []
        for line in chunk[:end].decode("utf-8", "replace").splitlines():
            try:
                s = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = s.get("key")
            if not key or key in seen:
                continue
            seen.append(key)
            age = (now - GM.stamp(s["at"])).total_seconds()
            if age > self.c["signal_max_age_seconds"]:
                LOG.info("Signal %s is %.0fs old; not traded", key, age)
                continue
            out.append(s)
        self.state.put("bridge:signal_keys", seen[-300:])
        return out

    # ------------------------------------------------------------------ trade ideas
    def analysis(self, now: datetime):
        """(analysis, age_minutes) of the signal bot's latest analysis, or (None, None).
        1.20: Python's own analysis instead (pyideas.py) while python_ideas says so (mode only, or fallback with no
        fresh Astra map). Everything downstream - ideas, rechecks, the lab - then reads Python's ideas."""
        a, age = self.astra_analysis(now)
        py = getattr(self, "python", None)
        if py is not None and py.replaces(a, age, now):
            p = py.analysis(now)
            if p is not None:
                return p, (now - GM.stamp(p["created_at"])).total_seconds() / 60
        return a, age

    def astra_analysis(self, now: datetime):
        if not self.analysis_path.exists():
            return None, None
        a = _read_json(self.analysis_path)
        if not isinstance(a, dict) or "id" not in a or "created_at" not in a:
            return None, None
        return a, (now - GM.stamp(a["created_at"])).total_seconds() / 60

    def open_ideas(self, now: datetime) -> tuple[dict | None, list]:
        """Ideas of the latest analysis not yet handled, while the analysis is fresh enough."""
        if not self.c["signal_bot_ideas"]:
            return None, []
        a, age = self.analysis(now)
        if a is None or age > self.c["idea_max_age_minutes"]:
            return a, []
        done = set(self.state.get("bridge:ideas_done", []))
        ideas = [i for i in a.get("ideas") or [] if f"{a['id']}:{i.get('id')}" not in done]
        ideas += [u for u in self.user_plan_ideas() if f"{a['id']}:{u['id']}" not in done]     # 1.9
        return a, ideas

    def user_plan_ideas(self) -> list:
        """1.9: the owner's plans APPROVED by Astra's map (runtime/user_plans.json), as ideas. Nothing is
        traded until the usual confirmation candle, tier (capped at B) and validator agree."""
        path = self.analysis_path.parent / UP.FILE_NAME
        if not path.exists():
            return []
        out = []
        for p in _read_json(path) or []:
            if isinstance(p, dict) and p.get("status") == "APPROVED":
                out.append(UP.as_trader_idea(p, p.get("matched_idea")))
        return out

    REMAP_FILE = "remap_request.json"                    # 1.11.2: read by the signal bot 6.2.1 (sense_trader_requests)

    def request_remap(self, key: str, reason: str, now: datetime) -> bool:
        """Ask the signal bot for an early Astra map (analysis only). One pending request at a time: a newer one
        replaces an unserved older one, and one map answers both. Never raises."""
        path = self.folder / self.REMAP_FILE
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps({"id": key, "reason": reason[:200], "at": GM.iso(now), "from": "auto_trader"}),
                           encoding="utf-8")
            os.replace(tmp, path)
            return True
        except OSError:
            return False

    def idea_done(self, a: dict, idea: dict):
        done = self.state.get("bridge:ideas_done", [])
        done.append(f"{a['id']}:{idea.get('id')}")
        self.state.put("bridge:ideas_done", done[-500:])


# ---------------------------------------------------------------------- conversion to orders
TRANSIENT = ("inside/through the entry zone", "outside", "already passed")


def transient(reason: str) -> bool:
    """Reasons that depend only on where price is right now: the idea is retried while fresh."""
    return any(t in reason for t in TRANSIENT)

def _order(direction, otype, entry, stop, targets, expiry, setup_id, grade, rationale, evidence_ids=(),
           cancel_above=None, cancel_below=None, deviation=1.0):
    tp2 = targets[1] if len(targets) > 1 else None
    return {"setup_id": setup_id, "grade": grade, "direction": direction, "order_type": otype,
            "entry_price": round(entry, 2), "stop_loss": stop, "take_profit_1": targets[0], "take_profit_2": tp2,
            "tp1_close_percent": 50 if tp2 is not None else 100, "move_sl_to_breakeven_at_tp1": True,
            "max_entry_deviation": deviation, "order_expiry_utc": GM.iso(expiry),
            "cancel_if_price_above": cancel_above, "cancel_if_price_below": cancel_below,
            "evidence_ids": list(evidence_ids), "rationale": rationale[:300]}


def signal_to_order(sig: dict, quote: dict, c: dict, now: datetime):
    """A confirmed signal: price is in the entry zone now, so it is a MARKET order at the live price.
    Returns (order, None) or (None, reason)."""
    buy = sig["direction"] == "BUY"
    price = quote["ask"] if buy else quote["bid"]
    tol = c["signal_entry_tolerance"]
    if not sig["entry_low"] - tol <= price <= sig["entry_high"] + tol:
        return None, f"price {price} left the entry zone {sig['entry_low']}-{sig['entry_high']}"
    if not sig.get("targets"):
        return None, "signal without targets"
    grade = sig.get("grade") if sig.get("grade") in ("A_TRADE", "B_TRADE") else "B_TRADE"
    return _order(sig["direction"], "MARKET", price, sig["stop_loss"], sig["targets"], now + timedelta(minutes=30),
                  "SIG-" + str(sig["sid"])[-24:], grade, f"signal bot {sig.get('playbook', '')}",
                  sig.get("evidence_ids") or [], deviation=max(tol, 0.5)), None


def prefilter(idea: dict, c: dict):
    """Reason the idea is never traded (config filters), or None."""
    if idea.get("confidence", 0) < c["idea_min_confidence"]:
        return f"confidence {idea.get('confidence')}% below idea_min_confidence"
    if idea.get("risk") not in c["idea_allowed_risk"]:
        return f"risk {idea.get('risk')} not in idea_allowed_risk"
    if not idea.get("targets"):
        return "idea without targets"
    return None


def _bar_time(b):
    return GM.stamp(b["closes"]) if b.get("closes") else GM.stamp(b["time"]) + timedelta(minutes=15)


def confirmation(idea: dict, bars: list, since: datetime, evidence: dict | None = None):
    """Watch closed 15M candles that closed after `since` (the analysis time), oldest first.
    Confirmations (SELL shown, BUY is the mirror):
      REJECTION  candle reaches the zone (high >= entry_low), closes back BELOW it, bearish,
                 upper wick >= body
      SWEEP      candle trades ABOVE the zone (high > entry_high) and closes back inside/below it
                 (close < entry_high), bearish
      CHOCH      Python evidence: a confirmed 15M CHoCH in the idea's direction after the analysis
    A candle that trades through the stop first invalidates the idea.
    Returns (status, bar, how): ("confirmed", bar, "REJECTION"|"SWEEP"|"CHOCH") |
    ("invalidated", bar, None) | ("waiting", None, None)."""
    buy = idea["direction"] == "BUY"
    lo, hi, stop = idea["entry_low"], idea["entry_high"], idea["invalidation"]
    for b in bars:
        if _bar_time(b) <= since:
            continue
        o, h, l, cl = b["open"], b["high"], b["low"], b["close"]
        body = abs(cl - o)
        if (buy and l <= stop) or (not buy and h >= stop):
            return "invalidated", b, None
        if buy and l <= hi and cl > hi and cl > o and (min(o, cl) - l) >= body:
            return "confirmed", b, "REJECTION"
        if not buy and h >= lo and cl < lo and cl < o and (h - max(o, cl)) >= body:
            return "confirmed", b, "REJECTION"
        if buy and l < lo and cl > lo and cl > o:
            return "confirmed", b, "SWEEP"
        if not buy and h > hi and cl < hi and cl < o:
            return "confirmed", b, "SWEEP"
    for item in ((evidence or {}).get("index") or {}).values():
        if (item.get("kind") == "CHOCH" and item.get("timeframe") == "15min" and item.get("status") == "CONFIRMED"
                and item.get("direction") == idea["direction"]
                and (item.get("confirmed_at") or item.get("at"))
                and GM.stamp(item.get("confirmed_at") or item["at"]) > since):
            # v1.6 fix: SMC structure events carry "at" (the close of the breaking candle), not
            # "confirmed_at", so in v1.5 a real CHoCH never counted as a confirmation.
            near = [b for b in bars if _bar_time(b) > since and b["low"] <= hi + (hi - lo) and b["high"] >= lo - (hi - lo)]
            if near:                                     # the change of character happened at the zone
                return "confirmed", near[-1], "CHOCH"
    return "waiting", None, None


def sweep_bar_ok(idea: dict, bar: dict) -> bool:
    """1.10: a session sweep only confirms an idea when the sweeping candle rejected the level (wick on the
    swept side >= body) AND closed back at the idea's zone: SELL -> close <= entry_high, BUY -> close >= entry_low.
    26 Oct 14:30 closed at 4022.24, above U1's zone top 4016 and above I1's 4021: neither was rejected."""
    from .sessions import rejection_wick
    buy = idea["direction"] == "BUY"
    cl = bar["close"]
    if buy:
        return rejection_wick(bar, False) and cl >= idea["entry_low"]
    return rejection_wick(bar, True) and cl <= idea["entry_high"]


def strong_zone(idea: dict, zones: list, c: dict):
    """A remembered Daily/4H (or weekly) zone, tested enough and still holding, with the right role
    (SUPPORT for a BUY, RESISTANCE for a SELL), overlapping the idea's entry zone. Returns it or None."""
    role = "SUPPORT" if idea["direction"] == "BUY" else "RESISTANCE"
    tol = c["counter_zone_tolerance"]
    for z in zones:
        if (z.get("kind") == "SR" and z.get("timeframe") in c["counter_timeframes"] and z.get("role") == role
                and z.get("status") in ("ACTIVE", "FLIPPED") and z.get("tests", 0) >= c["counter_min_tests"]
                and z["low"] - tol <= idea["entry_high"] and idea["entry_low"] <= z["high"] + tol):
            return z
    return None


def confirmed_order(idea: dict, quote: dict, c: dict, now: datetime, bar: dict, how: str = "REJECTION"):
    """After the rejection candle: MARKET at the live price if it still gives min_rr; otherwise a LIMIT
    at the price that gives exactly min_rr (sell/buy the retest), valid for confirm_retest_hours."""
    buy = idea["direction"] == "BUY"
    price = quote["ask"] if buy else quote["bid"]
    stop, tp1 = idea["invalidation"], idea["targets"][0]
    if (buy and price <= stop) or (not buy and price >= stop):
        return None, "price is already beyond the stop"
    if (buy and price >= tp1) or (not buy and price <= tp1):
        return None, "TP1 already reached"
    grade = "A_TRADE" if (idea.get("confidence", 0) >= c["idea_a_grade_confidence"] and idea.get("risk") != "HIGH") \
        else "B_TRADE"
    setup_id = f"IDEA-{idea['direction']}-{round((idea['entry_low'] + idea['entry_high']) / 2)}"
    title = (f"idea {idea.get('confidence')}% {idea.get('risk')} confirmed by 15M {how} "
             f"closed {_bar_time(bar).strftime('%H:%M')}: {idea.get('title', '')}")
    r = c["min_rr"] + 0.02
    rr_now = abs(tp1 - price) / abs(price - stop)
    lo, hi = idea["entry_low"], idea["entry_high"]
    tol = float(c.get("confirm_entry_tolerance", 0.0) or 0.0)
    if rr_now >= r:
        # 1.10: a market entry must still be at the idea's zone. 26 Oct 14:45: U1 (4009-4016, stop 4024.5)
        # was confirmed with price at 4021.74, 5.7 above the zone and 2.8 from the stop; only the ATR floor
        # caught it. Outside the zone the idea is a different trade, so fall through to the retest limit.
        if lo - tol <= price <= hi + tol:
            return _order(idea["direction"], "MARKET", price, stop, idea["targets"], now + timedelta(minutes=30),
                          setup_id, grade, title, deviation=c["signal_entry_tolerance"]), None
        LOG.info("Idea %s %s: price %.2f is outside the zone %s-%s (tolerance %.1f); market entry skipped, "
                 "retest limit instead", idea.get("id"), idea["direction"], price, lo, hi, tol)
    level = round((tp1 + r * stop) / (1 + r), 2)                    # the min_rr price between price and stop
    if rr_now >= r:                                                 # 1.10: outside-zone market -> limit AT the zone
        level = round(hi if price > hi else lo, 2)                  # the zone edge nearest the price
        if (buy and not stop < level < price) or (not buy and not price < level < stop):
            return None, f"price {price} is on the wrong side of the zone {lo}-{hi} for a {idea['direction']} limit"
    if (buy and not stop < level < price) or (not buy and not price < level < stop):
        return None, f"2R not available after confirmation (market {rr_now:.2f}R)"
    # 1.6.1: the retest limit must stay inside the zone Astra described. 15 Oct I2 (SELL 4198.75-4205.3,
    # stop 4221, TP1 4177.56) needed 4206.62 for 2R: above the zone, so it is NOT a trade of that idea.
    if (buy and level < lo) or (not buy and level > hi):
        return None, (f"{c['min_rr']}R needs an entry at {level} which is outside the idea's zone "
                      f"{lo}-{hi} (market {rr_now:.2f}R)")
    expiry = now + timedelta(hours=min(c["confirm_retest_hours"], c["max_order_lifetime_hours"]))
    return _order(idea["direction"], "BUY_LIMIT" if buy else "SELL_LIMIT", level, stop, idea["targets"], expiry,
                  setup_id, grade, title + f" | retest entry (market was {rr_now:.2f}R)",
                  cancel_above=tp1 if buy else None, cancel_below=None if buy else tp1), None


def idea_to_order(idea: dict, quote: dict, c: dict, now: datetime):
    """A TRADE IDEA: a pending order at the idea's entry, using the same entry level the signal bot
    scores it with (LIMIT: BUY at the zone top / SELL at the zone bottom; BREAKOUT: the opposite edge).
    Returns (order, None) or (None, reason)."""
    why = prefilter(idea, c)
    if why:
        return None, why
    buy = idea["direction"] == "BUY"
    lo, hi = idea["entry_low"], idea["entry_high"]
    bid, ask = quote["bid"], quote["ask"]
    stop, tp1 = idea["invalidation"], idea["targets"][0]
    grade = "A_TRADE" if (idea.get("confidence", 0) >= c["idea_a_grade_confidence"] and idea.get("risk") != "HIGH") \
        else "B_TRADE"
    expiry = now + timedelta(hours=min(c["idea_order_hours"], c["max_order_lifetime_hours"]))
    kind = idea.get("entry_type", "LIMIT")
    title = f"idea {idea.get('confidence')}% {idea.get('risk')}: {idea.get('title', '')}"
    in_zone = lo <= (ask if buy else bid) <= hi
    if kind == "MARKET" or (in_zone and idea.get("readiness") == "VALID_NOW"):
        if not in_zone:
            return None, f"market idea but price is outside {lo}-{hi}"
        level, otype = (ask if buy else bid), "MARKET"
    elif kind == "LIMIT":
        # The zone edge nearest price fills first; move inside the zone only as far as needed for
        # min_rr: BUY as high, SELL as low as possible while reward/risk to TP1 is still >= min_rr.
        r = c["min_rr"] + 0.02
        need = (tp1 + r * stop) / (1 + r)
        level = (min(hi, need) if need >= lo else lo) if buy else (max(lo, need) if need <= hi else hi)
        level = round(level, 2)
        otype = "BUY_LIMIT" if buy else "SELL_LIMIT"
        if (buy and ask <= level) or (not buy and bid >= level):
            return None, "price is already inside/through the entry zone (waits for confirmation)"
    else:                                                       # BREAKOUT
        level = lo if buy else hi
        otype = "BUY_STOP" if buy else "SELL_STOP"
        if (buy and ask >= level) or (not buy and bid <= level):
            return None, "breakout level already passed"
    setup_id = f"IDEA-{idea['direction']}-{round(level)}"
    # Cancel before the fill if the move happens without us (TP1 reached) or, for stop orders, if
    # the idea is invalidated first.
    cancel_above = tp1 if buy else (stop if otype == "SELL_STOP" else None)
    cancel_below = (stop if otype == "BUY_STOP" else None) if buy else tp1
    if otype == "MARKET":
        cancel_above = cancel_below = None
    return _order(idea["direction"], otype, level, stop, idea["targets"], expiry, setup_id, grade, title,
                  cancel_above=cancel_above, cancel_below=cancel_below), None
