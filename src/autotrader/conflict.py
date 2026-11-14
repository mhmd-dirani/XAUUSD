"""
1.16: opposite live trades - the newer valid trade is never blocked; the OLDER one is managed.

Owner, 10 Nov: "If the new trade is the correct one it should start directly and then check the old one. If it is very
wrong we close it directly, based on the 1m or 5min trend we detect. If it is possible to wait for it to breakeven or
profit or least losses we wait, else we close it directly."
(10 Nov 19:40: the live BUY 4021.82 was open when an opposite SELL 4025.26 opened; nothing looked at the BUY again.)

Scope: LIVE positions only (swing and overnight magics; lab trades never start or receive this, they are samples).
A conflict starts when a live position is open and a NEWER live position runs the other way. Then, for the older one,
on every check (and at most once per closed 5M candle for the trend test):
  1. its stop is at the entry or better (trade protection already locked it): left alone, it can no longer lose.
  2. it is in profit (bid above entry for a BUY / ask below entry for a SELL): closed at market, the profit is kept.
  3. it is losing and VERY WRONG: the 5M structure AND the 1M structure both run against it: closed at market now,
     a smaller loss than its stop.
  4. otherwise it may still come back: its target is moved to breakeven (entry +/- spread) so it closes the moment it
     gets there; the 5M/1M test repeats at every new 5M candle; after max_wait_minutes it is closed at market.
  If the newer trade closes first (it was the wrong one), the conflict ends and the older trade's own target returns.
Opposite PENDING live orders that would hedge the newer trade are cancelled (nothing filled, nothing lost).
A stop is never widened; nothing here opens or enlarges a position.

LiveRecheck: a resting live limit whose zone a NEWER Astra analysis trades the other way (the 1.13.1 'contradicted'
rule) is cancelled before it can fill (the lab rule, now for live orders too).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from . import alerts
from . import lab as LB
from .weakness import Bars, structure_trend

UTC = timezone.utc
LOG = logging.getLogger("autotrader.conflict")
DEFAULTS = {"mode": "off", "max_wait_minutes": 120, "check_seconds": 10, "trend_lookback": 60, "swing_k": 2,
            "recheck_pending": True}


def _ts(s) -> float:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp() if s else 0.0


def trend(bars: list, lookback: int, k: int) -> str:
    """BULLISH / BEARISH / NONE: the last break of a fractal swing on these closed candles (t, o, h, l, c)."""
    if len(bars) < 2 * k + 3:
        return "NONE"
    b = Bars([x[0] for x in bars], [x[1] for x in bars], [x[2] for x in bars], [x[3] for x in bars], [x[4] for x in bars])
    return structure_trend(b, len(b) - 1, lookback, k)


def verdict(buy: bool, price: float, entry: float, sl_now: float, tr5: str, tr1: str) -> str:
    """'safe' | 'take_profit' | 'close' | 'wait' for an older trade facing a newer opposite one."""
    if sl_now and ((sl_now >= entry) if buy else (sl_now <= entry)):
        return "safe"
    if (price > entry) if buy else (price < entry):
        return "take_profit"
    against = "BEARISH" if buy else "BULLISH"
    if tr5 == against and tr1 == against:
        return "close"
    return "wait"


class Conflicts:
    def __init__(self, c: dict, app):
        self.c = c
        self.cfg = dict(DEFAULTS, **(c.get("opposite_trades") or {}))
        self.app = app
        self.last = 0.0
        self.seen5: dict = {}            # older position ticket -> last 5M candle tested
        self.analysis_seen = None
        if self.cfg["mode"] != "off":
            LOG.info("Opposite trades %s | the newer live trade runs; the older one: profit -> closed, losing and 5M+1M "
                     "against it -> closed, else target to breakeven for up to %s min", self.cfg["mode"].upper(),
                     self.cfg["max_wait_minutes"])

    @property
    def on(self) -> bool:
        return self.cfg["mode"] == "live"

    # ------------------------------------------------------------------ pending live limits vs a newer analysis
    def recheck_pending(self, now: datetime) -> list:
        if not (self.on and self.cfg["recheck_pending"]):
            return []
        a, _age = self.app.bridge.analysis(now)
        if not a or a.get("id") == self.analysis_seen:
            return []
        self.analysis_seen = a.get("id")
        born = _ts(a.get("created_at"))
        _, atr15 = self.app.market_context()
        tol = max(float(self.c.get("confirm_entry_tolerance", 0.0) or 0.0), self.c["duplicate_atr"] * (atr15 or 0.0))
        out = []
        for rec in self.app.state.active_orders(include_lab=False):
            if rec.get("status") != "PENDING" or _ts(rec.get("created_utc")) >= born:
                continue
            against = LB.contradicted(rec, a.get("ideas") or [], tol)
            if against is None:
                continue
            why = (f"Astra's {a['created_at'][11:16]} analysis trades the other way here ({against.get('id')} "
                   f"{against['direction']} {against['entry_low']}-{against['entry_high']})")
            if self.app.executor.cancel(rec["ticket"], why, status="CANCELLED"):
                out.append(rec["ticket"])
                alerts.trade(f"↩️ {rec['direction']} limit {rec['setup_id']} @ {rec['entry']} cancelled before the fill: {why}",
                             short=f"↩️ {rec['direction']} limit cancelled: newer analysis is the other way")
        return out

    # ------------------------------------------------------------------ open positions
    def tick(self, now: datetime | None = None) -> list:
        if not self.on or time.time() - self.last < float(self.cfg["check_seconds"]):
            return []
        self.last = time.time()
        now = now or datetime.now(UTC)
        try:
            self.recheck_pending(now)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Live limit recheck skipped: %s", str(exc)[:160])
        m, x, st = self.app.market, self.app.executor, self.app.state
        live = [r for r in st.active_orders(include_lab=False)
                if r.get("status") in ("OPEN", "TP1_DONE") and r.get("position_ticket") is not None]
        if len(live) < 2 and not any(r.get("conflict") for r in live):
            self._cancel_hedging_pendings(live, now)
            return []
        positions = {getattr(p, "ticket", None): p for p in (m.trade_call("positions_get", symbol=m.symbol) or [])}
        q = m.quote()
        if float(q.get("age", 0.0) or 0.0) > 15.0:
            return []
        out = []
        self._cancel_hedging_pendings(live, now)
        live.sort(key=lambda r: _ts(r.get("fill_utc") or r.get("created_utc")))
        for i, old in enumerate(live):
            newer = [n for n in live[i + 1:] if n["direction"] != old["direction"]
                     and _ts(n.get("fill_utc") or n.get("created_utc")) > _ts(old.get("fill_utc") or old.get("created_utc"))]
            if not newer and not old.get("conflict"):
                continue
            pos = positions.get(old["position_ticket"])
            if pos is None:
                continue
            if not newer:                      # the newer opposite trade is gone (stopped or closed): back to normal
                self._end(old, pos)
                continue
            if not old.get("conflict"):
                n = newer[-1]
                old["conflict"] = f"vs {n['setup_id']}"
                old["conflict_utc"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
                st.update_order(old["ticket"], conflict=old["conflict"], conflict_utc=old["conflict_utc"])
                LOG.info("Opposite trades: %s %s is older than %s %s; it is managed now", old["direction"],
                         old["setup_id"], n["direction"], n["setup_id"])
            try:
                act = self._manage(old, pos, q, now)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Opposite-trade check skipped for %s: %s", old.get("setup_id"), str(exc)[:160])
                continue
            if act:
                out.append((old["setup_id"], act))
        return out

    def _cancel_hedging_pendings(self, live: list, now: datetime):
        """A pending live order that would hedge an OPEN live position that is newer than the order: cancelled."""
        opens = [r for r in live if r.get("status") in ("OPEN", "TP1_DONE")]
        if not opens:
            return
        for rec in self.app.state.active_orders(include_lab=False):
            if rec.get("status") != "PENDING":
                continue
            for o in opens:
                if _ts(o.get("fill_utc") or o.get("created_utc")) <= _ts(rec.get("created_utc")):
                    continue
                if LB.opposite_overlap({"direction": rec["direction"], "entry": rec["entry"], "sl": rec["sl"], "tp1": rec["tp1"]},
                                       {"direction": o["direction"], "entry": o["entry"], "sl": o["sl"], "tp1": o["tp1"]}):
                    why = f"opposite to the newer open {o['direction']} {o['setup_id']}"
                    if self.app.executor.cancel(rec["ticket"], why, status="CANCELLED"):
                        alerts.trade(f"↩️ {rec['direction']} limit {rec['setup_id']} cancelled: {why}", short=None)
                    break

    def _manage(self, old: dict, pos, q: dict, now: datetime):
        x, st, m = self.app.executor, self.app.state, self.app.market
        buy = old["direction"] == "BUY"
        entry = float(getattr(pos, "price_open", 0.0) or old["entry"])
        price = q["bid"] if buy else q["ask"]
        sl_now = float(getattr(pos, "sl", 0.0) or 0.0)
        risk = abs(entry - float(old["sl"])) or 1e-9
        r_now = ((price - entry) if buy else (entry - price)) / risk
        # the trend test only on a new closed 5M candle
        bars5 = m.rates("TIMEFRAME_M5", int(self.cfg["trend_lookback"]) + 10)
        key = bars5[-1][0] if bars5 else None
        tr5 = tr1 = "NONE"
        fresh5 = key is not None and self.seen5.get(old["position_ticket"]) != key
        if fresh5:
            self.seen5[old["position_ticket"]] = key
            tr5 = trend(bars5, int(self.cfg["trend_lookback"]), int(self.cfg["swing_k"]))
            tr1 = trend(m.rates("TIMEFRAME_M1", int(self.cfg["trend_lookback"]) + 10), int(self.cfg["trend_lookback"]),
                        int(self.cfg["swing_k"]))
        v = verdict(buy, price, entry, sl_now, tr5, tr1)
        title = f"{old['direction']} {old['setup_id']} ({old.get('conflict')})"
        if v == "safe":
            return None
        if v == "take_profit":
            return self._close(old, pos, f"opposite trade: profit taken at {r_now:+.2f} R", title, r_now)
        if v == "close":
            return self._close(old, pos, f"opposite trade: 5M and 1M both {tr5}, cut at {r_now:+.2f} R", title, r_now)
        waited = (now.timestamp() - _ts(old.get("conflict_utc"))) / 60
        if waited >= float(self.cfg["max_wait_minutes"]):
            return self._close(old, pos, f"opposite trade: no recovery in {waited:.0f} min, closed at {r_now:+.2f} R",
                               title, r_now)
        be = round(entry + (q["spread"] if buy else -q["spread"]), 2)
        tp_now = float(getattr(pos, "tp", 0.0) or 0.0)
        if not tp_now or abs(tp_now - be) > 0.005:
            if x.set_target(pos, be, f"opposite trade: waiting for breakeven ({r_now:+.2f} R now)"):
                st.update_order(old["ticket"], protect=f"conflict: target to breakeven {be:.2f}")
                msg = (f"⚖️ {title}: losing {r_now:+.2f} R but the 5M/1M trend is not clearly against it; "
                       f"target moved to breakeven {be:.2f}, closed at market if it turns (max {self.cfg['max_wait_minutes']} min)")
                LOG.info(msg)
                alerts.trade(msg, short=None if old.get("lab") else f"⚖️ {old['direction']} waits for breakeven {be:.2f}")
                return "wait_breakeven"
        return None

    def _end(self, old, pos):
        tp = old.get("tp2") if old.get("tp2") is not None else old.get("tp1")
        if tp and abs(float(getattr(pos, "tp", 0.0) or 0.0) - float(tp)) > 0.005:
            self.app.executor.set_target(pos, float(tp), "opposite trade gone: original target restored")
        self.app.state.update_order(old["ticket"], conflict=None, conflict_utc=None)
        old["conflict"] = old["conflict_utc"] = None
        LOG.info("Opposite trades: %s %s - the newer opposite trade is closed; original target %s restored",
                 old["direction"], old["setup_id"], tp)

    def _close(self, old, pos, why, title, r_now):
        if self.app.executor.close_position(pos, why, comment="OPPOSITE"):
            self.app.state.update_order(old["ticket"], protect=why[:120])
            msg = f"⚖️ {title} closed: {why}"
            LOG.info(msg)
            alerts.trade(msg, short=f"⚖️ {old['direction']} closed {r_now:+.2f}R (newer opposite trade)")
            return "closed"
        return None
