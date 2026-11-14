#!/usr/bin/env python3
"""Astra V6 — persistent market-structure memory, live zone sensor and learning history.

A professional trader remembers the chart. This module gives the bot that memory:

* Historical Structure Engine (deterministic, closed candles only, point-in-time safe)
  - Support/resistance zones from confirmed swing pivots on 1day / 4h / 1h.
  - Each zone remembers when it was created, how many SEPARATE times it was tested,
    when it was last tested and how strongly price reacted (in ATR) after each test.
  - Lifecycle: ACTIVE -> BROKEN (close beyond + ATR buffer, role flips: a broken
    resistance becomes potential support, retest WAITING) -> FLIPPED (retest held) or
    back to ACTIVE with its original role (close back through = false breakout).
    A zone that flips three times is ARCHIVED as a chop level.
  - Equal highs / equal lows become liquidity pools: ACTIVE -> SWEPT (wick through,
    close back) or TAKEN (close through).
  - Persistent copies of the SMC engine's 1h+ FVG / order block / breaker / BOS / CHoCH /
    sweep / OTE items with their status history.
  - Astra's own validated 1h+ support/resistance levels join the same lifecycle.
* Sensor: live price entering an important remembered zone -> request fresh Astra analysis.
* Learning history: every Astra scenario is stored as a measurable prediction and later
  resolved against closed candles (PLAYED_OUT / FAILED / NOT_TRIGGERED / EXPIRED).

Offline; no network, AI, broker or Telegram calls. Advisory only: nothing here can create,
approve or release a BUY/SELL signal.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from gold_smc_engine import _atr, _pivots, _time, normalize_bars

UTC = timezone.utc
MEMORY_TF = ("1day", "4h", "1h")
TF_RANK = {"15min": 0, "1h": 1, "4h": 2, "1day": 3, "1week": 4}
TF_LABEL = {"15min": "15M", "1h": "1H", "4h": "4H", "1day": "D1", "1week": "W1"}
PIVOTS = {"1day": (3, 3), "4h": (3, 3), "1h": (4, 4)}
LIVE = ("ACTIVE", "BROKEN", "FLIPPED")
OPPOSITE = {"SUPPORT": "RESISTANCE", "RESISTANCE": "SUPPORT"}
SMC_KINDS = ("FVG", "ORDER_BLOCK_CANDIDATE", "BREAKER_CANDIDATE", "BOS", "CHOCH", "LIQUIDITY_SWEEP", "FIB_OTE")
SMC_ZONE_KINDS = ("FVG", "ORDER_BLOCK_CANDIDATE", "BREAKER_CANDIDATE", "FIB_OTE")


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def trigger_passes(kind: str, level: float, bar: dict[str, Any]) -> bool:
    """Same closed-candle predicates as the V5 watcher conditions."""
    o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
    if kind == "TOUCH":                                   # a resting order at `level` would be filled
        return l <= level <= h
    return {
        "CLOSE_ABOVE": c > level, "CLOSE_BELOW": c < level,
        "SWEEP_RECLAIM_UP": l < level < c, "SWEEP_RECLAIM_DOWN": c < level < h,
        "BULL_REJECTION": l <= level < c and c > o and o - l >= c - o,
        "BEAR_REJECTION": h >= level > c and c < o and h - o >= o - c,
    }[kind]


class MemorySettings(dict):
    DEFAULTS = {
        "zone_max_width_atr": 0.35,    # pivot zone: wick extreme to body edge, capped
        "zone_min_width_atr": 0.05,
        "touch_tolerance_atr": 0.05,
        "break_buffer_atr": 0.10,      # a break needs a CLOSE this far beyond the zone
        "merge_tolerance_atr": 0.25,
        "exit_bars": 2,                # bars away from the zone before a new test can count
        "reaction_bars": 6,            # bars used to measure the reaction after a test
        "acceptance_bars": 3,          # closes beyond a broken zone that count as acceptance
        "max_flips": 3,
        "equal_tolerance_atr": 0.10,   # equal highs/lows
        "important_min_tests": 2,
        "payload_zones": 24,
        "payload_distance_daily_atr": 4.0,
        "sensor_cooldown_seconds": 7200,
        "prediction_dedupe_hours": 6,
        "history_days": 30,
    }

    def __init__(self, **overrides):
        super().__init__(self.DEFAULTS)
        unknown = set(overrides) - set(self.DEFAULTS)
        if unknown:
            raise ValueError(f"Unknown memory settings {sorted(unknown)}")
        self.update(overrides)


class MarketMemory:
    """SQLite-backed memory. Use from one thread (the monitor's main loop)."""

    def __init__(self, path: str | Path, settings: MemorySettings | None = None):
        self.s = settings or MemorySettings()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS zones (id TEXT PRIMARY KEY, timeframe TEXT, status TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS zone_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, zone_id TEXT,
                at TEXT, event TEXT, price REAL, detail TEXT);
            CREATE TABLE IF NOT EXISTS smc_items (id TEXT PRIMARY KEY, timeframe TEXT, status TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS analyses (id TEXT PRIMARY KEY, created_at TEXT, stance TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS predictions (id TEXT PRIMARY KEY, analysis_id TEXT, created_at TEXT,
                status TEXT, resolved_at TEXT, data TEXT);
            CREATE INDEX IF NOT EXISTS zone_events_at ON zone_events(at);
            CREATE INDEX IF NOT EXISTS predictions_status ON predictions(status);
        """)
        self.db.commit()
        self.zones: dict[str, dict[str, Any]] = {
            zid: json.loads(data) for zid, data in self.db.execute("SELECT id, data FROM zones")}
        self._dirty: set[str] = set()
        self._live_inside: dict[str, bool] = {}
        self.new_events: list[dict[str, Any]] = []   # events produced by the latest update()

    # ------------------------------------------------------------------ meta helpers
    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))

    def _next_id(self, tf, role):
        n = int(self.get("zone_seq", 0)) + 1
        self.put("zone_seq", n)
        code = {"SUPPORT": "SUP", "RESISTANCE": "RES", "BUY_SIDE": "LQH", "SELL_SIDE": "LQL"}[role]
        return f"M{n}-{TF_LABEL[tf]}-{code}"

    def _event(self, z, at, event, price=None, detail=""):
        self.db.execute("INSERT INTO zone_events (zone_id, at, event, price, detail) VALUES (?,?,?,?,?)",
                        (z["id"], at, event, price, detail))
        record = {"zone_id": z["id"], "at": at, "event": event, "price": price, "detail": detail,
                  "timeframe": z["timeframe"], "role": z["role"], "low": z["low"], "high": z["high"],
                  "tests": z.get("tests", 0), "kind": z["kind"]}
        self.new_events.append(record)
        self._dirty.add(z["id"])

    def flush(self):
        for zid in self._dirty:
            z = self.zones[zid]
            self.db.execute("INSERT OR REPLACE INTO zones VALUES (?,?,?,?)",
                            (zid, z["timeframe"], z["status"], json.dumps(z)))
        self._dirty.clear()
        self.db.commit()

    def close(self):
        self.flush()
        self.db.close()

    # ------------------------------------------------------------------ zone creation
    def _overlapping(self, low, high, role, tol, kind="SR"):
        out = []
        for z in self.zones.values():
            if z["kind"] == kind and z["status"] in LIVE and z["role"] == role \
                    and z["low"] - tol <= high and z["high"] + tol >= low:
                out.append(z)
        return out

    def _new_zone(self, tf, role, low, high, created_at, origin_time, source, kind="SR", detail=""):
        z = {"id": self._next_id(tf, role), "kind": kind, "timeframe": tf, "role": role, "original_role": role,
             "low": round(low, 2), "high": round(high, 2), "source": source, "status": "ACTIVE",
             "created_at": created_at, "origin_time": origin_time, "tests": 0, "test_times": [],
             "last_tested_at": None, "reactions": [], "pending_reaction": None, "inside": True,
             "outside_run": 0, "broken_at": None, "break_direction": None, "acceptance": 0,
             "retest": None, "flips": 0, "false_breakouts": 0, "swept_at": None, "astra_confirmed_at": None}
        self.zones[z["id"]] = z
        self._event(z, created_at, "CREATED", None, detail or f"{source} {TF_LABEL[tf]} {role.lower()}")
        return z

    def _pivot_zone(self, tf, side, bar, atr, created_at):
        s = self.s
        if side == "HIGH":
            hi = bar["high"]
            lo = max(max(bar["open"], bar["close"]), hi - s["zone_max_width_atr"] * atr)
            lo = min(lo, hi - s["zone_min_width_atr"] * atr)
            role = "RESISTANCE"
        else:
            lo = bar["low"]
            hi = min(min(bar["open"], bar["close"]), lo + s["zone_max_width_atr"] * atr)
            hi = max(hi, lo + s["zone_min_width_atr"] * atr)
            role = "SUPPORT"
        tol = s["merge_tolerance_atr"] * atr
        for z in self._overlapping(lo, hi, role, tol):
            if TF_RANK[z["timeframe"]] < TF_RANK[tf]:
                old = z["timeframe"]
                z["timeframe"] = tf
                self._event(z, created_at, "PROMOTED", None, f"{TF_LABEL[old]} -> {TF_LABEL[tf]} swing")
            return None            # an existing zone already represents this reaction
        z = self._new_zone(tf, role, lo, hi, created_at, bar["time"], "ENGINE")
        z["tests"], z["test_times"], z["last_tested_at"] = 1, [bar["closes"]], bar["closes"]   # the pivot rejection itself
        return z

    def _liquidity(self, tf, side, pivot, prior, bars, atr, created_at):
        """Equal highs/lows: this swing and an earlier one at (nearly) the same price, with no
        candle trading beyond them in between, not even a wick (the resting liquidity is still there)."""
        tol = self.s["equal_tolerance_atr"] * atr
        role = "BUY_SIDE" if side == "HIGH" else "SELL_SIDE"
        price = pivot["high"] if side == "HIGH" else pivot["low"]
        for prev in reversed(prior[-6:]):
            pp = prev["high"] if side == "HIGH" else prev["low"]
            if abs(pp - price) > tol:
                continue
            lo, hi = sorted((pp, price))
            between = [b for b in bars if prev["time"] < b["time"] < pivot["time"]]
            if any((b["high"] > hi) if side == "HIGH" else (b["low"] < lo) for b in between):
                continue
            if self._overlapping(lo, hi, role, tol, kind="LIQUIDITY"):
                return
            z = self._new_zone(tf, role, lo, hi, created_at, pivot["time"], "ENGINE", kind="LIQUIDITY",
                               detail=f"equal {'highs' if side == 'HIGH' else 'lows'} {prev['time']} & {pivot['time']}")
            z["inside"] = False
            return

    # ------------------------------------------------------------------ bar processing
    def _process_sr(self, z, b, atr, closes):
        s = self.s
        lo, hi = z["low"], z["high"]
        tol, buf = s["touch_tolerance_atr"] * atr, s["break_buffer_atr"] * atr
        touch = b["high"] >= lo - tol and b["low"] <= hi + tol
        role = z["role"]
        beyond = b["close"] > hi + buf if role == "RESISTANCE" else b["close"] < lo - buf
        if z["status"] in ("ACTIVE", "FLIPPED"):
            if z["status"] == "FLIPPED" and z.get("retest") == "WAITING" and touch:
                held = b["close"] > hi if role == "SUPPORT" else b["close"] < lo
                if held and not beyond:
                    self._finish_reaction(z)
                    z.update(retest="CONFIRMED", inside=True, outside_run=0)
                    z["tests"] += 1
                    z["test_times"] = (z["test_times"] + [closes])[-12:]
                    z["last_tested_at"] = closes
                    self._event(z, closes, "RETEST_CONFIRMED", b["close"],
                                f"accepted break retested and held as {role.lower()}")
                    return
            if beyond:
                self._finish_reaction(z)
                z.update(status="BROKEN", broken_at=closes, break_direction="UP" if role == "RESISTANCE" else "DOWN",
                         role=OPPOSITE[role], retest="WAITING", acceptance=1, inside=False, outside_run=0,
                         departed=False)
                z["flips"] += 1
                self._event(z, closes, "BROKEN", b["close"],
                            f"closed {'above' if role == 'RESISTANCE' else 'below'} after {z['tests']} tests; "
                            f"now potential {z['role'].lower()}")
                if z["flips"] >= s["max_flips"]:
                    z["status"] = "ARCHIVED"
                    self._event(z, closes, "ARCHIVED", b["close"], "role flipped too often (chop level)")
                return
            if touch:
                if not z["inside"]:
                    self._finish_reaction(z)
                    z["tests"] += 1
                    z["test_times"] = (z["test_times"] + [closes])[-12:]
                    z["last_tested_at"] = closes
                    self._event(z, closes, "TESTED", b["close"], f"test #{z['tests']}")
                z["inside"], z["outside_run"] = True, 0
                return
            z["outside_run"] += 1
            if z["inside"] and z["outside_run"] >= s["exit_bars"]:
                z["inside"] = False
                z["pending_reaction"] = {"bars": 0, "best": 0.0}
            self._track_reaction(z, b, atr)
            return
        if z["status"] == "BROKEN":
            # role is already the flipped role: SUPPORT after an upside break, RESISTANCE after a downside one
            back = b["close"] < lo - buf if z["role"] == "SUPPORT" else b["close"] > hi + buf
            held = b["close"] > hi if z["role"] == "SUPPORT" else b["close"] < lo
            if back:
                z.update(status="ACTIVE", role=OPPOSITE[z["role"]], retest=None, acceptance=0, inside=True,
                         outside_run=0)
                z["false_breakouts"] += 1
                self._event(z, closes, "FALSE_BREAKOUT", b["close"],
                            f"closed back {'below' if z['role'] == 'RESISTANCE' else 'above'}; "
                            f"{z['role'].lower()} restored")
            elif touch and held and z.get("departed"):
                # a retest only counts after price first left the zone (not the breakout bar's follow-through)
                z.update(status="FLIPPED", retest="CONFIRMED", inside=True, outside_run=0)
                z["tests"] += 1
                z["test_times"] = (z["test_times"] + [closes])[-12:]
                z["last_tested_at"] = closes
                self._event(z, closes, "RETEST_CONFIRMED", b["close"],
                            f"broken level held as {z['role'].lower()}")
            elif not touch and held:
                z["departed"] = True
                z["acceptance"] += 1
                if z["acceptance"] >= s["acceptance_bars"]:
                    # accepted: the flip is established (retest still pending). A later close back
                    # through is a new break of the flipped level, not a "false breakout".
                    z.update(status="FLIPPED", inside=False, outside_run=0)
                    self._event(z, closes, "ACCEPTED", b["close"],
                                f"{s['acceptance_bars']} closes beyond the broken zone; now {z['role'].lower()}")
            elif not touch:
                z["departed"] = True

    def _process_liquidity(self, z, b, atr, closes):
        buf = self.s["break_buffer_atr"] * atr
        if z["status"] != "ACTIVE":
            return
        if z["role"] == "BUY_SIDE":
            if b["close"] > z["high"] + buf:
                z["status"] = "TAKEN"; self._event(z, closes, "LIQUIDITY_TAKEN", b["close"], "closed through equal highs")
            elif b["high"] > z["high"] and b["close"] < z["high"]:
                z["status"], z["swept_at"] = "SWEPT", closes
                self._event(z, closes, "LIQUIDITY_SWEPT", b["high"], "wick above equal highs, close back below")
        else:
            if b["close"] < z["low"] - buf:
                z["status"] = "TAKEN"; self._event(z, closes, "LIQUIDITY_TAKEN", b["close"], "closed through equal lows")
            elif b["low"] < z["low"] and b["close"] > z["low"]:
                z["status"], z["swept_at"] = "SWEPT", closes
                self._event(z, closes, "LIQUIDITY_SWEPT", b["low"], "wick below equal lows, close back above")

    def _track_reaction(self, z, b, atr):
        p = z.get("pending_reaction")
        if not p:
            return
        away = (z["low"] - b["low"]) if z["role"] == "RESISTANCE" else (b["high"] - z["high"])
        p["best"] = max(p["best"], away / atr if atr > 0 else 0.0)
        p["bars"] += 1
        if p["bars"] >= self.s["reaction_bars"]:
            self._finish_reaction(z)

    def _finish_reaction(self, z):
        p = z.get("pending_reaction")
        if p and p["bars"] > 0:
            z["reactions"] = (z["reactions"] + [round(p["best"], 2)])[-12:]
        z["pending_reaction"] = None

    # ------------------------------------------------------------------ public update
    def update(self, history: dict[str, list[dict[str, Any]]], now: datetime) -> list[dict[str, Any]]:
        """Process every closed 1day/4h/1h candle not yet seen, in chronological order across
        timeframes. First run replays the whole supplied history (memory starts populated).
        All-or-nothing: on any error the database and in-memory zones are restored."""
        try:
            return self._update(history, now)
        except Exception:
            self.db.rollback()
            self.zones = {zid: json.loads(d) for zid, d in self.db.execute("SELECT id, data FROM zones")}
            self._dirty.clear()
            raise

    def _update(self, history, now):
        self.new_events = []
        stream, atr_of, pivots_at, frames, confirmed = [], {}, {}, {}, {}
        for tf in MEMORY_TF:
            raw = history.get(tf) or []
            if len(raw) < 20:
                continue
            bars = [b for b in normalize_bars(raw, tf) if _time(b["closes"]) <= now]
            frames[tf] = bars
            series = _atr(bars, 14)
            atr_of[tf] = {b["time"]: a for b, a in zip(bars, series)}
            left, right = PIVOTS[tf]
            confirmed[tf] = []
            for side in ("HIGH", "LOW"):
                for i in _pivots(bars, left, right, side):
                    if i + right < len(bars):
                        pivots_at.setdefault((tf, bars[i + right]["time"]), []).append((side, bars[i]))
                        confirmed[tf].append((bars[i + right]["closes"], side, bars[i]))
            confirmed[tf].sort(key=lambda x: x[0])
            cursor = self.get("cursor:" + tf)
            for b in bars:
                if cursor is None or b["closes"] > cursor:
                    stream.append((b["closes"], -TF_RANK[tf], tf, b))
        stream.sort(key=lambda x: (x[0], x[1]))
        for closes, _, tf, b in stream:
            atr = atr_of[tf][b["time"]]
            for z in list(self.zones.values()):
                if z["timeframe"] != tf or z["status"] not in LIVE:
                    continue
                if z["kind"] == "SR":
                    self._process_sr(z, b, atr, closes)
                elif z["kind"] == "LIQUIDITY":
                    self._process_liquidity(z, b, atr, closes)
                self._dirty.add(z["id"])
            for side, pb in pivots_at.get((tf, b["time"]), []):
                key = f"pivot:{tf}:{side}:{pb['time']}"
                if self.get(key):
                    continue
                self.put(key, True)
                self._pivot_zone(tf, side, pb, atr, closes)
                prior = [q for (cl, sd, q) in confirmed[tf] if sd == side and cl < closes]
                self._liquidity(tf, side, pb, prior, frames[tf], atr, closes)
            self.put("cursor:" + tf, closes)
        self._prune(now)
        self.flush()
        return self.new_events

    def _prune(self, now):
        cutoff = iso(now - timedelta(days=90))
        self.db.execute("DELETE FROM zone_events WHERE at < ?", (cutoff,))
        # 6.1.3: bound the database. Hourly analyses and resolved predictions older than 90 days
        # (track record uses history_days, far less) and SMC items unseen for 30 days are dropped.
        self.db.execute("DELETE FROM analyses WHERE created_at < ?", (cutoff,))
        self.db.execute("DELETE FROM predictions WHERE resolved_at IS NOT NULL AND created_at < ?", (cutoff,))
        self.db.execute("DELETE FROM smc_items WHERE COALESCE(json_extract(data,'$.seen_at'), '') < ?",
                        (iso(now - timedelta(days=30)),))
        old = iso(now - timedelta(days=200))
        for tf in MEMORY_TF:
            for side in ("HIGH", "LOW"):
                prefix = f"pivot:{tf}:{side}:"
                self.db.execute("DELETE FROM meta WHERE key >= ? AND key < ?", (prefix, prefix + old))
        self.db.execute("DELETE FROM meta WHERE key LIKE 'sensor:%' AND substr(key, 8) NOT IN (SELECT id FROM zones)")
        for zid, z in list(self.zones.items()):
            dead = z["status"] in ("ARCHIVED", "TAKEN") or (z["kind"] == "LIQUIDITY" and z["status"] == "SWEPT")
            last = z.get("last_tested_at") or z.get("broken_at") or z.get("swept_at") or z["created_at"]
            if dead and last < iso(now - timedelta(days=30)):
                self.db.execute("DELETE FROM zones WHERE id=?", (zid,))
                del self.zones[zid]
                self._dirty.discard(zid)

    # ------------------------------------------------------------------ Astra levels / SMC items
    def add_astra_levels(self, levels, history, now):
        """Promote Astra's validated 1h+ S/R levels into memory (replayed through closed candles)."""
        added = []
        for lv in levels:
            tf = lv.get("timeframe")
            if tf not in MEMORY_TF or lv.get("kind") not in ("SUPPORT", "RESISTANCE", "SUPPLY", "DEMAND"):
                continue
            role = "SUPPORT" if lv["kind"] in ("SUPPORT", "DEMAND") else "RESISTANCE"
            bars = normalize_bars(history.get(tf) or [], tf)
            if len(bars) < 20:
                continue
            atr = _atr(bars, 14)[-1]
            tol = self.s["merge_tolerance_atr"] * atr
            same = self._overlapping(lv["low"], lv["high"], role, tol)
            if same:
                for z in same:
                    if (z.get("astra_confirmed_at") or "") < iso(now - timedelta(hours=12)):
                        z["astra_confirmed_at"] = iso(now)
                        self._dirty.add(z["id"])
                continue
            times = sorted(lv.get("evidence_times") or [])
            if not times:
                continue
            first = times[0]
            cursor = self.get("cursor:" + tf)
            replay = [b for b in bars if b["time"] >= first and (cursor is None or b["closes"] <= cursor)]
            if not replay:
                continue
            series = {b["time"]: a for b, a in zip(bars, _atr(bars, 14))}
            z = self._new_zone(tf, role, lv["low"], lv["high"], replay[0]["closes"], first, "ASTRA",
                               detail=(lv.get("reason") or "")[:160])
            z["inside"] = False
            for b in replay:
                self._process_sr(z, b, series[b["time"]], b["closes"])
            z["astra_confirmed_at"] = iso(now)
            added.append(z["id"])
        self.flush()
        return added

    def sync_smc(self, evidence, now):
        """Persist 1h+ SMC items so they outlive the rolling candle window."""
        if not evidence or not evidence.get("index"):
            return
        stamp_now = iso(now)
        for item in evidence["index"].values():
            if item.get("timeframe") not in MEMORY_TF or item.get("kind") not in SMC_KINDS:
                continue
            row = self.db.execute("SELECT data FROM smc_items WHERE id=?", (item["id"],)).fetchone()
            if row:
                old = json.loads(row[0])
                old["seen_at"] = stamp_now
                if old["status"] == item["status"]:
                    self.db.execute("UPDATE smc_items SET data=? WHERE id=?", (json.dumps(old), item["id"]))
                    continue
                old["history"] = (old.get("history", []) + [[stamp_now, item["status"]]])[-8:]
                old["status"] = item["status"]
                data = old
            else:
                data = {k: item.get(k) for k in ("id", "kind", "direction", "timeframe", "at", "level_low",
                                                 "level_high", "status")}
                data["first_seen"] = data["seen_at"] = stamp_now
                data["history"] = [[stamp_now, item["status"]]]
            self.db.execute("INSERT OR REPLACE INTO smc_items VALUES (?,?,?,?)",
                            (data["id"], data["timeframe"], data["status"], json.dumps(data)))
        self.db.execute("DELETE FROM smc_items WHERE status IN ('INVALIDATED','FILLED','SUPERSEDED') "
                        "AND json_extract(data, '$.at') < ?", (iso(now - timedelta(days=21)),))
        self.db.commit()

    # ------------------------------------------------------------------ views
    def important(self, z) -> bool:
        return (z["kind"] == "SR" and z["status"] in LIVE
                and (TF_RANK[z["timeframe"]] >= TF_RANK["4h"] or z["tests"] >= self.s["important_min_tests"]
                     or z["source"] == "ASTRA"))

    def score(self, z) -> float:
        return TF_RANK[z["timeframe"]] * 1.5 + min(z.get("tests", 0), 6) * 0.6 + (1 if z["status"] == "BROKEN" else 0) \
            + (0.5 if z.get("astra_confirmed_at") else 0)

    def describe(self, z, price, now, atr):
        created = _time(z["created_at"])
        avg = round(sum(z["reactions"]) / len(z["reactions"]), 2) if z.get("reactions") else None
        pos = "INSIDE" if z["low"] <= price <= z["high"] else ("ABOVE_PRICE" if z["low"] > price else "BELOW_PRICE")
        dist = 0.0 if pos == "INSIDE" else min(abs(price - z["low"]), abs(price - z["high"]))
        out = {"id": z["id"], "kind": z["kind"], "timeframe": z["timeframe"], "role": z["role"],
               "original_role": z["original_role"], "status": z["status"], "low": z["low"], "high": z["high"],
               "source": z["source"], "created_at": z["created_at"],
               "age_days": round((now - created).total_seconds() / 86400, 1),
               "tests": z.get("tests", 0), "last_tested_at": z.get("last_tested_at"),
               "avg_reaction_atr": avg, "position": pos,
               "distance_atr": round(dist / atr, 2) if atr else None}
        for k in ("broken_at", "break_direction", "retest", "false_breakouts", "swept_at", "astra_confirmed_at"):
            if z.get(k):
                out[k] = z[k]
        return out

    def daily_atr(self, history):
        bars = history.get("1day") or []
        if len(bars) < 20:
            return None
        key = (len(bars), bars[-1]["time"], bars[-1]["close"])
        if getattr(self, "_atr_key", None) != key:       # the sensor asks every second; cache it
            self._atr_key, self._atr_val = key, _atr(normalize_bars(bars, "1day"), 14)[-1]
        return self._atr_val

    def payload(self, price, now, history, track_record=None):
        """Compact memory for Astra: nearest/most important zones, liquidity, recent events, SMC."""
        atr = self.daily_atr(history) or max(price * 0.006, 1e-6)
        reach = self.s["payload_distance_daily_atr"] * atr
        zones = [z for z in self.zones.values() if z["status"] in LIVE + ("SWEPT",)
                 and z["low"] - reach <= price <= z["high"] + reach]
        zones.sort(key=lambda z: (-self.score(z), min(abs(price - z["low"]), abs(price - z["high"]))))
        sr = [self.describe(z, price, now, atr) for z in zones if z["kind"] == "SR"][: self.s["payload_zones"]]
        liq = [self.describe(z, price, now, atr) for z in zones if z["kind"] == "LIQUIDITY"][:8]
        since = iso(now - timedelta(hours=48))
        events = [{"zone_id": r[0], "at": r[1], "event": r[2], "price": r[3], "detail": r[4]}
                  for r in self.db.execute("SELECT zone_id, at, event, price, detail FROM zone_events "
                                           "WHERE at >= ? AND event != 'CREATED' ORDER BY at DESC LIMIT 20", (since,))]
        smc = []
        # 6.1.3: filter zone kinds in SQL; BOS/CHoCH/sweeps used to fill the 60 newest rows first.
        marks = ",".join("?" * len(SMC_ZONE_KINDS))
        for (data,) in self.db.execute("SELECT data FROM smc_items WHERE status NOT IN "
                                       "('INVALIDATED','FILLED','SUPERSEDED') AND json_extract(data,'$.kind') IN "
                                       f"({marks}) ORDER BY json_extract(data,'$.at') DESC LIMIT 60", SMC_ZONE_KINDS):
            d = json.loads(data)
            if (d.get("seen_at") or "") < iso(now - timedelta(days=2)):
                continue          # rolled out of the analysed candle window: status no longer tracked
            if d["kind"] in SMC_ZONE_KINDS and d.get("level_low") is not None \
                    and d["level_low"] - reach <= price <= d["level_high"] + reach:
                smc.append({k: d.get(k) for k in ("id", "kind", "direction", "timeframe", "at", "level_low",
                                                  "level_high", "status", "first_seen")})
        return {"rule": ("Python-maintained memory from closed candles. Tests = separate visits; "
                         "avg_reaction_atr = mean move away after a test (ATR of the zone timeframe). "
                         "BROKEN = closed beyond, role flipped, retest WAITING. FLIPPED = retest held. "
                         "Cite zone ids in memory_ids. Repeated tests can mean strength OR a weakening level."),
                "daily_atr": round(atr, 2), "price": price,
                "zones": sr, "liquidity": liq, "recent_events": events, "smc_zones": smc[:12],
                "track_record": track_record}

    def zone(self, zid):
        return self.zones.get(zid)

    # ------------------------------------------------------------------ live sensor
    def sense(self, price, now, history):
        """Live price entering an important zone (first entry of a visit, per-zone cooldown)."""
        hits = []
        atr_d = self.daily_atr(history) or max(price * 0.006, 1e-6)
        for z in self.zones.values():
            if not self.important(z):
                self._live_inside.pop(z["id"], None)
                continue
            tol = 0.05 * atr_d
            inside = z["low"] - tol <= price <= z["high"] + tol
            far = price < z["low"] - tol - 0.15 * atr_d or price > z["high"] + tol + 0.15 * atr_d
            was = self._live_inside.get(z["id"])
            if inside and not was:
                last = self.get("sensor:" + z["id"])
                if last and (now - _time(last)).total_seconds() < self.s["sensor_cooldown_seconds"]:
                    self._live_inside[z["id"]] = True      # inside a cooled-down zone: this visit is spent
                    continue
                hits.append(z)                            # caller must ack() once it acted on the entry
            elif far:
                self._live_inside[z["id"]] = False
        return hits

    def ack(self, zid, now):
        """The App acted on a zone entry: start the per-zone cooldown and spend this visit."""
        self._live_inside[zid] = True
        self.put("sensor:" + zid, iso(now))
        self.db.commit()

    # ------------------------------------------------------------------ learning history
    def record_analysis(self, analysis_id, created_at, stance, analysis):
        self.db.execute("INSERT OR REPLACE INTO analyses VALUES (?,?,?,?)",
                        (analysis_id, created_at, stance, json.dumps(analysis, ensure_ascii=False)))
        self.db.commit()

    def record_predictions(self, analysis_id, created_at, scenarios, price):
        """Store measurable scenarios. Near-duplicates of a recent prediction are skipped."""
        stored = []
        created = _time(created_at)
        window = iso(created - timedelta(hours=self.s["prediction_dedupe_hours"]))
        recent = [json.loads(d) for (d,) in self.db.execute(
            "SELECT data FROM predictions WHERE created_at >= ?", (window,))]
        for sc in scenarios:
            group = f"{sc.get('likelihood')}:{sc['bias']}:{sc['trigger_type']}:{sc['trigger_timeframe']}:{round(sc['trigger_level'])}"
            if any(r.get("group") == group for r in recent):
                continue
            pid = f"{analysis_id}:{sc['id']}"
            data = dict(sc, pid=pid, group=group, created_at=created_at, ref_price=price,
                        until=iso(created + timedelta(hours=sc["horizon_hours"])),
                        triggered_at=None)
            self.db.execute("INSERT OR IGNORE INTO predictions VALUES (?,?,?,?,?,?)",
                            (pid, analysis_id, created_at, "OPEN", None, json.dumps(data, ensure_ascii=False)))
            recent.append(data)
            stored.append(pid)
        self.db.commit()
        return stored

    def record_ideas(self, analysis_id, created_at, ideas, price, horizon_hours=24):
        """Every trade idea is scored like a scenario: filled (MARKET at once, LIMIT/BREAKOUT when price
        touches the entry), then TP1 before the stop = PLAYED_OUT, stop first = FAILED."""
        scen = []
        for i in ideas:
            buy = i["direction"] == "BUY"
            level = (i["entry_high"] if buy else i["entry_low"]) if i["entry_type"] == "LIMIT" else \
                    (i["entry_low"] if buy else i["entry_high"])
            scen.append({"id": "idea-" + i["id"], "title": i.get("title", ""), "bias": "BULLISH" if buy else "BEARISH",
                         "likelihood": "IDEA", "confidence": i["confidence"], "risk": i["risk"],
                         "entry_type": i["entry_type"],
                         "trigger_type": "IMMEDIATE" if i["entry_type"] == "MARKET" else "TOUCH",
                         "trigger_timeframe": "15min", "trigger_level": level,
                         "objective_level": i["targets"][0], "invalidation_level": i["invalidation"],
                         "horizon_hours": horizon_hours, "memory_ids": i.get("memory_ids", []), "description": ""})
        return self.record_predictions(analysis_id, created_at, scen, price)

    def evaluate_predictions(self, history, now):
        """Resolve open predictions against closed candles. Returns newly resolved records."""
        rows = list(self.db.execute("SELECT id, data FROM predictions WHERE status IN ('OPEN','TRIGGERED')"))
        if not rows:
            return []
        frames = {tf: normalize_bars(history.get(tf) or [], tf) for tf in ("15min", "1h", "4h")}
        resolved = []
        for pid, data in rows:
            p = json.loads(data)
            created, until = _time(p["created_at"]), _time(p["until"])
            status = "OPEN"
            if p["trigger_type"] == "IMMEDIATE" and not p.get("triggered_at"):
                p["triggered_at"] = p["created_at"]           # market entry: filled at analysis time
            if not p.get("triggered_at"):
                for b in frames.get(p["trigger_timeframe"], []):
                    c = _time(b["closes"])
                    if c <= created or c > min(until, now):
                        continue
                    inv = p.get("invalidation_level")
                    if inv is not None and p["bias"] != "NEUTRAL" and (
                            (p["bias"] == "BULLISH" and b["low"] <= inv) or (p["bias"] == "BEARISH" and b["high"] >= inv)) \
                            and not trigger_passes(p["trigger_type"], p["trigger_level"], b):
                        status, p["outcome"] = "NOT_TRIGGERED", "invalidation reached before the trigger"
                        break
                    if trigger_passes(p["trigger_type"], p["trigger_level"], b):
                        p["triggered_at"] = b["closes"]
                        if inv is not None and p["bias"] != "NEUTRAL" and (
                                (p["bias"] == "BULLISH" and b["low"] <= inv) or (p["bias"] == "BEARISH" and b["high"] >= inv)):
                            status, p["outcome"] = "FAILED", "trigger candle also touched invalidation (ambiguous)"
                        break
            if status == "OPEN" and p.get("triggered_at"):
                status = "TRIGGERED"
                if p["bias"] == "NEUTRAL" or p.get("objective_level") is None:
                    status, p["outcome"] = "PLAYED_OUT", "trigger occurred (no separate objective)"
                else:
                    t0 = _time(p["triggered_at"])
                    for b in frames["15min"]:
                        c = _time(b["closes"])
                        if c <= t0 or c > min(until, now):
                            continue
                        bull = p["bias"] == "BULLISH"
                        reach = b["high"] >= p["objective_level"] if bull else b["low"] <= p["objective_level"]
                        inv = p.get("invalidation_level")
                        fail = inv is not None and (b["low"] <= inv if bull else b["high"] >= inv)
                        if fail:
                            status, p["outcome"] = "FAILED", f"invalidation {inv:g} hit at {b['closes']}"
                            break
                        if reach:
                            status, p["outcome"] = "PLAYED_OUT", f"objective {p['objective_level']:g} reached at {b['closes']}"
                            break
            covered = any(_time(b["closes"]) >= until for b in frames.get(p["trigger_timeframe"], [])) \
                or now >= until + timedelta(hours=6)     # never resolve on missing candles
            if status in ("OPEN", "TRIGGERED") and now >= until and covered:
                status = "EXPIRED_AFTER_TRIGGER" if p.get("triggered_at") else "NOT_TRIGGERED"
                p.setdefault("outcome", "horizon ended")
            if status in ("OPEN", "TRIGGERED"):
                self.db.execute("UPDATE predictions SET status=?, data=? WHERE id=?",
                                (status, json.dumps(p, ensure_ascii=False), pid))
                continue
            p["status"] = status
            self.db.execute("UPDATE predictions SET status=?, resolved_at=?, data=? WHERE id=?",
                            (status, iso(now), json.dumps(p, ensure_ascii=False), pid))
            resolved.append(p)
        self.db.commit()
        return resolved

    def track_record(self, now, days=None):
        days = days or self.s["history_days"]
        since = iso(now - timedelta(days=days))
        rows = [json.loads(d) for (d,) in self.db.execute(
            "SELECT data FROM predictions WHERE resolved_at IS NOT NULL AND created_at >= ?", (since,))]
        if not rows:
            return {"days": days, "resolved": 0, "note": "No resolved scenarios or ideas yet; no track record."}

        def block(items):
            trig = [r for r in items if r.get("triggered_at")]
            played = [r for r in items if r["status"] == "PLAYED_OUT"]
            failed = [r for r in items if r["status"] == "FAILED"]
            return {"count": len(items), "triggered": len(trig), "played_out": len(played), "failed": len(failed),
                    "played_out_rate_when_triggered": round(len(played) / len(trig), 2) if trig else None}

        total = len(rows)
        ideas = [r for r in rows if r.get("likelihood") == "IDEA"]
        rows = [r for r in rows if r.get("likelihood") != "IDEA"]
        neutral = [r for r in rows if r["bias"] == "NEUTRAL"]
        rows_dir = [r for r in rows if r["bias"] != "NEUTRAL"]       # neutral "trigger = outcome" never inflates stats
        breaks = [r for r in rows_dir if r["trigger_type"] in ("CLOSE_ABOVE", "CLOSE_BELOW")]
        rejections = [r for r in rows_dir if r["trigger_type"] not in ("CLOSE_ABOVE", "CLOSE_BELOW")]
        recent = sorted(rows, key=lambda r: r.get("created_at", ""), reverse=True)[:6]
        return {"days": days, "resolved": total,
                "primary": block([r for r in rows_dir if r.get("likelihood") == "PRIMARY"]),
                "alternative": block([r for r in rows_dir if r.get("likelihood") != "PRIMARY"]),
                "neutral": block(neutral),
                "ideas": block(ideas),
                "ideas_by_confidence": {band: block([r for r in ideas if lo <= (r.get("confidence") or 0) < hi])
                                        for band, lo, hi in (("<50", 0, 50), ("50-69", 50, 70), ("70+", 70, 101))},
                "breakout_scenarios": block(breaks), "rejection_scenarios": block(rejections),
                "recent": [{"title": r.get("title", "")[:80], "bias": r["bias"], "likelihood": r.get("likelihood"),
                            "status": r["status"], "outcome": r.get("outcome", "")[:100]} for r in recent],
                "rule": "Your own resolved scenarios. Use to calibrate confidence; never as a trade trigger."}
