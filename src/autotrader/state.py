"""
Auto-trader state (SQLite in runtime_trader/trader.sqlite3): orders, used setup ids, the last
market map, and an audit row for every analysis (accepted or rejected, with the reason).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# 1.11.4: setup-id prefixes of engines with their own slot and one-a-day rule (overnight: OVN-YYYYMMDD). Their
# orders do not count toward the swing engine's max_trades_per_day or its duplicate check.
ENGINE_SETUP_PREFIXES = ("OVN-",)

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    ticket          INTEGER PRIMARY KEY,
    setup_id        TEXT NOT NULL,
    grade           TEXT NOT NULL,
    direction       TEXT NOT NULL,
    order_type      TEXT NOT NULL,
    entry           REAL NOT NULL,
    sl              REAL NOT NULL,
    tp1             REAL NOT NULL,
    tp2             REAL,
    tp1_close_pct   REAL NOT NULL,
    be_at_tp1       INTEGER NOT NULL,
    cancel_above    REAL,
    cancel_below    REAL,
    expiry_utc      TEXT NOT NULL,
    lots            REAL NOT NULL,
    risk_money      REAL NOT NULL,
    rr              REAL NOT NULL,
    evidence_ids    TEXT,
    created_utc     TEXT NOT NULL,
    status          TEXT NOT NULL,   -- PENDING | OPEN | TP1_DONE | CLOSED | CANCELLED | EXPIRED
    position_ticket INTEGER,
    note            TEXT
);
CREATE TABLE IF NOT EXISTS used_setups (setup_id TEXT PRIMARY KEY, used_utc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    utc TEXT NOT NULL, trigger TEXT, decision TEXT NOT NULL,
    accepted INTEGER NOT NULL, reject_why TEXT,
    input_tokens INTEGER, output_tokens INTEGER, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trade_reviews (
    ticket INTEGER PRIMARY KEY, reviewed_utc TEXT NOT NULL, verdict TEXT, outcome TEXT, r REAL,
    mfe_r REAL, mae_r REAL, after_4h TEXT, tier TEXT, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_utc TEXT NOT NULL, setup_id TEXT NOT NULL, source TEXT NOT NULL,
    direction TEXT NOT NULL, order_type TEXT NOT NULL, entry REAL NOT NULL, sl REAL NOT NULL,
    tp1 REAL NOT NULL, tp2 REAL, expiry_utc TEXT NOT NULL, cancel_above REAL, cancel_below REAL,
    status TEXT NOT NULL,            -- PENDING | OPEN | TP1_DONE | CLOSED | CANCELLED | EXPIRED
    fill_utc TEXT, fill REAL, risk REAL, realised REAL DEFAULT 0, closed_utc TEXT, r REAL, outcome TEXT,
    playbook TEXT, confirmation TEXT, session TEXT, regime TEXT, astra_class TEXT, expansion_block INTEGER,
    why TEXT
);
"""
META_COLS = (("playbook", "TEXT"), ("confirmation", "TEXT"), ("session", "TEXT"), ("regime", "TEXT"),
             ("astra_class", "TEXT"), ("expansion_block", "INTEGER"),
             ("tier", "TEXT"), ("tier_facts", "TEXT"), ("tier_reason", "TEXT"),          # 1.7
             ("user_plan", "INTEGER"),                                                   # 1.9
             ("lab", "INTEGER"), ("violations", "TEXT"),                                  # 1.10
             ("mfe_r", "REAL"), ("mae_r", "REAL"), ("protect", "TEXT"),                   # 1.15 trade protection
             ("zone_score", "INTEGER"), ("zone_against", "INTEGER"), ("zone_tfs", "TEXT"),  # 1.15 zone score
             ("conflict", "TEXT"), ("conflict_utc", "TEXT"),                               # 1.16 opposite trades
             ("tr5", "TEXT"), ("tr1", "TEXT"), ("flow", "TEXT"), ("vol15", "REAL"), ("vol5", "REAL"),
             ("vol_read", "TEXT"))                                                       # 1.19 order flow (flow.py)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class State:
    def __init__(self, directory: Path):
        self.path = Path(directory) / "trader.sqlite3"
        with self._c() as c:
            c.executescript(SCHEMA)
            cols = {r[1] for r in c.execute("PRAGMA table_info(orders)")}
            for col, typ in (("closed_utc", "TEXT"), ("result", "REAL"), ("strategy", "TEXT"),
                             ("fill_utc", "TEXT"), ("exit_utc", "TEXT"), ("tp_touched_utc", "TEXT")) + META_COLS:   # 1.4-1.7, 1.11, 1.11.1
                if col not in cols:
                    c.execute(f"ALTER TABLE orders ADD COLUMN {col} {typ}")
            # 1.7.1: reviews computed while closed_utc was unknown measured the wrong window; redo them
            c.execute("DELETE FROM trade_reviews WHERE ticket IN (SELECT ticket FROM orders WHERE closed_utc IS NULL)")
            # 1.7.3: a review made before the 4h-after window had closed measured nothing; redo it
            c.execute("DELETE FROM trade_reviews WHERE ticket IN (SELECT o.ticket FROM orders o JOIN trade_reviews v ON v.ticket=o.ticket"
                      " WHERE o.closed_utc IS NOT NULL AND v.reviewed_utc < strftime('%Y-%m-%dT%H:%M:%SZ', o.closed_utc, '+4 hours'))")
            pcols = {r[1] for r in c.execute("PRAGMA table_info(paper_trades)")}
            for col, typ in (("rr", "REAL"), ("lots", "REAL"), ("risk_money", "REAL"), ("grade", "TEXT")):  # 1.6.1
                if col not in pcols:
                    c.execute(f"ALTER TABLE paper_trades ADD COLUMN {col} {typ}")

    def _c(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    # ---------------------------------------------------------------- setups
    def setup_used(self, setup_id: str) -> bool:
        with self._c() as c:
            return c.execute("SELECT 1 FROM used_setups WHERE setup_id=?", (setup_id,)).fetchone() is not None

    def mark_setup_used(self, setup_id: str):
        with self._c() as c:
            c.execute("INSERT OR IGNORE INTO used_setups VALUES (?,?)", (setup_id, _now()))

    # ---------------------------------------------------------------- orders
    def record_order(self, ticket, order, grade, lots, risk_money, status, position_ticket=None):
        with self._c() as c:
            c.execute(
                """INSERT OR REPLACE INTO orders (ticket,setup_id,grade,direction,order_type,entry,sl,tp1,tp2,
                   tp1_close_pct,be_at_tp1,cancel_above,cancel_below,expiry_utc,lots,risk_money,rr,evidence_ids,
                   created_utc,status,position_ticket,note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ticket, order["setup_id"], grade, order["direction"], order["order_type"], order["entry_price"],
                 order["stop_loss"], order["take_profit_1"], order.get("take_profit_2"),
                 order["tp1_close_percent"], int(order["move_sl_to_breakeven_at_tp1"]),
                 order.get("cancel_if_price_above"), order.get("cancel_if_price_below"),
                 order["order_expiry_utc"], lots, risk_money, order["rr"],
                 json.dumps(order.get("evidence_ids", [])), _now(), status, position_ticket,
                 order.get("rationale", "")))

    def set_strategy(self, ticket: int, strategy: str):
        self.update_order(ticket, strategy=strategy)

    def set_meta(self, ticket: int, meta: dict | None):
        """v1.6 journal fields: playbook, confirmation, session, regime (JSON), astra_class, expansion_block."""
        if meta:
            self.update_order(ticket, **{k: (json.dumps(meta[k]) if k in ("regime", "tier_facts", "violations") else meta[k])
                                         for k, _ in META_COLS if k in meta})

    # ---------------------------------------------------------------- paper trades (v1.6)
    def paper_open(self, rec: dict) -> int:
        cols = ", ".join(rec)
        with self._c() as c:
            cur = c.execute(f"INSERT INTO paper_trades ({cols}) VALUES ({', '.join('?' * len(rec))})",
                            tuple(json.dumps(v) if isinstance(v, (dict, list)) else v for v in rec.values()))
            return cur.lastrowid

    def paper_update(self, pid: int, **fields):
        if fields:
            with self._c() as c:
                c.execute(f"UPDATE paper_trades SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                          (*fields.values(), pid))

    def paper_active(self) -> list:
        with self._c() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM paper_trades WHERE status IN ('PENDING','OPEN','TP1_DONE') ORDER BY id")]

    def paper_all(self) -> list:
        with self._c() as c:
            return [dict(r) for r in c.execute("SELECT * FROM paper_trades ORDER BY id")]

    def update_order(self, ticket: int, **fields):
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            with self._c() as c:
                c.execute(f"UPDATE orders SET {cols} WHERE ticket=?", (*fields.values(), ticket))

    def active_orders(self, include_lab: bool = True) -> list:
        """Working orders. The executor manages all of them; the live validator and the analyst's
        constraints look only at the live track (include_lab=False), so lab orders never block a live one."""
        with self._c() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM orders WHERE status IN ('PENDING','OPEN','TP1_DONE')"
                + ("" if include_lab else " AND COALESCE(lab, 0) = 0"))]

    # ---------------------------------------------------------------- trade reviews (1.7)
    def save_review(self, rv: dict):
        with self._c() as c:
            c.execute("INSERT OR REPLACE INTO trade_reviews (ticket,reviewed_utc,verdict,outcome,r,mfe_r,mae_r,after_4h,tier,data)"
                      " VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (rv["ticket"], rv["reviewed_utc"], rv["verdict"], rv["outcome"], rv["r"], rv["mfe_r"], rv["mae_r"],
                       rv["after_4h"], rv.get("tier"), json.dumps(rv, ensure_ascii=False)))

    def reviews(self) -> list:
        with self._c() as c:
            return [json.loads(r["data"]) for r in c.execute("SELECT data FROM trade_reviews ORDER BY reviewed_utc")]

    def closed_unreviewed(self) -> list:
        with self._c() as c:
            return [dict(r) for r in c.execute(
                "SELECT o.* FROM orders o LEFT JOIN trade_reviews v ON v.ticket = o.ticket "
                "WHERE o.status='CLOSED' AND o.result IS NOT NULL AND v.ticket IS NULL")]

    def closed_without_result(self) -> list:
        with self._c() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM orders WHERE status='CLOSED' AND result IS NULL AND position_ticket IS NOT NULL")]

    def all_orders(self) -> list:
        with self._c() as c:
            return [dict(r) for r in c.execute("SELECT * FROM orders ORDER BY created_utc")]

    def setup_orders_since(self, setup_id: str, since_iso: str, lab: bool = False) -> list:
        """Orders of one setup since `since_iso`: live ones by default, lab ones with lab=True."""
        with self._c() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM orders WHERE setup_id=? AND created_utc >= ? AND COALESCE(lab, 0) = ?",
                (setup_id, since_iso, int(lab)))]

    def orders_created_since(self, since_iso: str) -> int:
        """Live swing-engine orders only (lab orders and own-slot engines such as overnight have their own counters)."""
        not_engine = "".join(" AND setup_id NOT LIKE ?" for _ in ENGINE_SETUP_PREFIXES)
        with self._c() as c:
            return c.execute("SELECT COUNT(*) FROM orders WHERE created_utc >= ? AND COALESCE(lab, 0) = 0" + not_engine,
                             (since_iso, *[p + "%" for p in ENGINE_SETUP_PREFIXES])).fetchone()[0]

    def lab_active_for_setup(self, setup_id: str) -> list:
        """1.10.1: working lab orders of one setup, whatever day they were created."""
        with self._c() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM orders WHERE setup_id=? AND lab=1 AND status IN ('PENDING','OPEN','TP1_DONE')",
                (setup_id,))]

    def lab_orders_since(self, since_iso: str) -> int:
        with self._c() as c:
            return c.execute("SELECT COUNT(*) FROM orders WHERE created_utc >= ? AND lab = 1 AND setup_id NOT LIKE 'SCP-%'",
                             (since_iso,)).fetchone()[0]                    # scalps have their own daily cap

    # ---------------------------------------------------------------- key/value
    def put(self, key: str, value):
        with self._c() as c:
            c.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value)))

    def get(self, key: str, default=None):
        with self._c() as c:
            r = c.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
            return json.loads(r["v"]) if r else default

    # ---------------------------------------------------------------- audit
    def log_analysis(self, result: dict, trigger: str, accepted: bool, why: str = "", usage=None):
        usage = usage or {}
        with self._c() as c:
            c.execute("INSERT INTO analyses (utc,trigger,decision,accepted,reject_why,input_tokens,output_tokens,payload)"
                      " VALUES (?,?,?,?,?,?,?,?)",
                      (_now(), trigger, result.get("decision", "?"), int(accepted), why,
                       usage.get("input_tokens"), usage.get("output_tokens"), json.dumps(result)))
