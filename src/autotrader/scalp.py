"""
11 Nov (owner): "let's keep it in lab, let's run scalping from now till Friday" - the 5M trend-pullback scalp, forward
tested on the demo lab at the minimum lot, exactly the replayed rule (scalp_model.PB5_VARIANTS / PB_VARIANTS).

Replay (2 years, 696,397 M1 bars): PB5M_rr2.0 3,304 trades, +$99 per 0.01 lot, PF 1.01; in-sample +$235, out-of-sample
-$136 (PF 0.98). It did NOT pass the bar. This lab run measures real fills, spread and slippage on it; it is not a
live candidate.

Rule (once per closed candle of the entry timeframe; M5 for PB5M_*, M1 for PULLBACK_*):
  * context from the next timeframe up (15M for PB5M), built from the same closed candles as in the replay: EMA21 of
    its closes, rising (UP) or falling (DOWN) over 3 candles, and the last close on the same side;
  * EMA9 / EMA21 on the entry timeframe; a candle whose low (UP) / high (DOWN) reaches EMA21 arms a pullback; the
    first later candle (within 10) that CLOSES back beyond EMA9 is the signal -> MARKET entry;
  * stop beyond the last 6 candles' extreme + 0.1 ATR14, at least max(1 ATR, 3 x spread) from the entry;
    target rr x the stop distance; time exit after the variant's hold minutes (the executor's exit_utc).
Lab only (magic_lab, require_demo), setup ids SCP-<DIR>-<HHMM>, one scalp position at a time, max_per_day, a daily
realised-loss stop, max_spread. Its trades are NOT managed by trade protection (the replayed rule has no breakeven)
and never block or hedge-check the other lab engines.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import alerts
from . import bridge as BR
from . import news as NW
from . import scalp_model as SC
from . import sessions as SS

UTC = timezone.utc
LOG = logging.getLogger("autotrader.scalp")
PREFIX = "SCP-"
DEFAULTS = {"mode": "off", "variant": "PB5M_rr2.0", "max_per_day": 12, "daily_loss_usd": 25.0, "max_spread": 0.5,
            "bars": 400}
FAMILIES = {"PB5M": ("TIMEFRAME_M5", 300, 900, SC.PB5_VARIANTS), "PULLBACK": ("TIMEFRAME_M1", 60, 300, SC.PB_VARIANTS)}


def family(variant: str):
    for k, v in FAMILIES.items():
        if variant.startswith(k + "_") and variant in v[3]:
            return v
    raise ValueError(f"unknown scalp variant {variant!r}")


def last_signal(bars: list, period: int, block: int) -> dict | None:
    """The replay's arming logic run over closed candles (dicts t,o,h,l,c,s); a signal on the LAST candle, or None."""
    if len(bars) < 80:
        return None
    _sma, atrs = SC.indicators(bars, 20, 14)
    closes = [b["c"] for b in bars]
    e9, e21, ctx = SC.ema(closes, 9), SC.ema(closes, 21), SC.context_htf(bars, period, block)
    armed, sig = None, None
    n = len(bars)
    for i in range(30, n):
        b, a, c = bars[i], atrs[i], ctx[i]
        sig = None
        if a is None or a <= 0 or e9[i] is None or e21[i] is None or c is None:
            armed = None
            continue
        up = c == "UP" and e9[i] > e21[i]
        down = c == "DOWN" and e9[i] < e21[i]
        if up and b["l"] <= e21[i]:
            armed = ("BUY", i)
        elif down and b["h"] >= e21[i]:
            armed = ("SELL", i)
        if armed and i - armed[1] <= 10 and i > armed[1]:
            if armed[0] == "BUY" and up and b["c"] > e9[i]:
                sig = "BUY"
            elif armed[0] == "SELL" and down and b["c"] < e9[i]:
                sig = "SELL"
            if sig:
                armed = None
        elif armed and i - armed[1] > 10:
            armed = None
    if not sig:
        return None
    i = n - 1
    lo = min(x["l"] for x in bars[i - 5:i + 1])
    hi = max(x["h"] for x in bars[i - 5:i + 1])
    return {"direction": sig, "atr": atrs[i], "lo": lo, "hi": hi, "t": bars[i]["t"], "context": ctx[i]}


def plan(sig: dict, quote: dict, rr: float) -> dict:
    """Entry, stop and target for a signal at the current quote (the replay's geometry)."""
    buy = sig["direction"] == "BUY"
    sp = float(quote["spread"])
    a = float(sig["atr"])
    entry = quote["ask"] if buy else quote["bid"]
    stop = (sig["lo"] - 0.1 * a) if buy else (sig["hi"] + sp + 0.1 * a)
    dist = max(abs(entry - stop), a, 3 * sp)
    stop = entry - dist if buy else entry + dist
    tp = entry + rr * dist if buy else entry - rr * dist
    return {"entry": round(entry, 2), "stop": round(stop, 2), "tp": round(tp, 2), "dist": dist}


class ScalpLab:
    def __init__(self, c: dict, app):
        self.c = c
        self.cfg = dict(DEFAULTS, **(c.get("scalp_lab") or {}))
        self.app = app
        self.last_bar = None
        self.logged: set = set()
        if self.on:
            tf, period, block, table = family(self.cfg["variant"])
            rr, hold, sess = table[self.cfg["variant"]]
            LOG.info("Scalp lab ON | %s | entries %s, trend %s min | target %.1f R | time exit %d min | sessions %s | "
                     "max %d/day, daily loss stop $%.0f, spread <= %.2f", self.cfg["variant"], tf, block // 60, rr, hold,
                     ",".join(sess) if sess else "all", self.cfg["max_per_day"], self.cfg["daily_loss_usd"],
                     self.cfg["max_spread"])

    @property
    def on(self) -> bool:
        return self.cfg["mode"] == "lab" and bool(self.c.get("lab_mode")) and bool(self.c.get("require_demo"))

    def _once(self, key, msg, *args):
        if key not in self.logged:
            self.logged.add(key)
            LOG.info(msg, *args)

    def bars(self, tf: str, period: int) -> list:
        rows = self.app.market.rates(tf, int(self.cfg["bars"]))
        return [{"t": datetime.fromtimestamp(r[0], UTC), "o": r[1], "h": r[2], "l": r[3], "c": r[4], "s": None}
                for r in rows]

    def today(self, now: datetime) -> list:
        day0 = now.replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        return [o for o in self.app.state.all_orders() if str(o.get("setup_id", "")).startswith(PREFIX)
                and (o.get("created_utc") or "") >= day0]

    def tick(self, now: datetime | None = None):
        if not self.on:
            return None
        now = now or datetime.now(UTC)
        tf, period, block, table = family(self.cfg["variant"])
        rr, hold, sessions_ok = table[self.cfg["variant"]]
        m = self.app.market
        key = m.rates(tf, 1)
        if not key or key[-1][0] == self.last_bar:
            return None
        self.last_bar = key[-1][0]
        bars = self.bars(tf, period)
        sig = last_signal(bars, period, block)
        if not sig:
            return None
        sid = f"{PREFIX}{sig['direction']}-{sig['t'] + timedelta(seconds=period):%H%M}"
        if sessions_ok and SS.session_of(sig["t"], self.c.get("sessions")) not in sessions_ok:
            return None
        why = self.blocked(now)
        if why:
            self._once(("block", why[:40], now.strftime("%Y%m%d%H")), "Scalp %s %s skipped: %s", sid, sig["direction"], why)
            return None
        q = m.quote()
        if q["spread"] > float(self.cfg["max_spread"]):
            LOG.info("Scalp %s skipped: spread %.2f > %.2f", sid, q["spread"], self.cfg["max_spread"])
            return None
        p = plan(sig, q, rr)
        si = m.symbol_info()
        if p["dist"] < si.trade_stops_level * si.point:
            LOG.info("Scalp %s skipped: stop inside the broker stop level", sid)
            return None
        order = BR._order(sig["direction"], "MARKET", p["entry"], p["stop"], [p["tp"]], now + timedelta(minutes=10), sid,
                          "B_TRADE", f"scalp lab {self.cfg['variant']}: {tf[10:]} pullback in a {sig['context']} "
                          f"{block // 60}M trend", deviation=self.c["signal_entry_tolerance"])
        order["move_sl_to_breakeven_at_tp1"] = False
        order["rr"] = rr
        order["max_entry_deviation"] = min(float(order["max_entry_deviation"]), float(self.c["max_entry_deviation_cap"]))
        lab = self.app.lab
        lots, risk_money, hold_reason = lab.size(si, p["dist"])
        if hold_reason:
            LOG.info("Scalp %s skipped: %s", sid, hold_reason)
            return None
        order["strategy"] = "LAB_SCALP"
        order["meta"] = {"playbook": "SCALP_" + self.cfg["variant"], "confirmation": "EMA9_RECLAIM", "session":
                         SS.session_of(now, self.c.get("sessions")), "lab": 1, "violations": ["scalp_lab"], "tier": None,
                         "tier_reason": "lab: scalp forward test (failed the replay bar: PF 1.01)"}
        order["spread"] = q["spread"]
        try:
            ticket = self.app.executor.send(order, lots, risk_money, "B_TRADE", magic=self.c["magic_lab"])
        except NW.NewsHold as h:
            LOG.info("Scalp %s not sent: %s", sid, h)
            return None
        except Exception as exc:  # noqa: BLE001
            alerts.error(f"Scalp lab order {sid} failed: {exc}")
            return None
        exit_at = now + timedelta(minutes=int(hold))
        if ticket:
            self.app.state.update_order(ticket, exit_utc=exit_at.strftime("%Y-%m-%dT%H:%M:%SZ"))
        LOG.info("SCALP ORDER SENT ticket=%s %s MARKET @ %.2f sl %.2f tp %.2f (%.1f R) | %.2f lots risk %.2f | exit by %s",
                 ticket, sid, p["entry"], p["stop"], p["tp"], rr, lots, risk_money, exit_at.strftime("%H:%M"))
        alerts.trade(f"⚡ LAB SCALP {sig['direction']} {sid} @ {p['entry']:.2f} | SL {p['stop']:.2f} | TP {p['tp']:.2f} | "
                     f"{rr:g}R | {lots} lots | {block // 60}M trend {sig['context']} | time exit {exit_at:%H:%M} UTC",
                     short=None)
        return ticket

    def blocked(self, now: datetime) -> str | None:
        lab = self.app.lab
        why = lab.hard_block(now, check_book=False, check_day_cap=False)     # the scalp has its own daily cap
        if why:
            return why
        mine = [o for o in self.app.state.active_orders() if str(o.get("setup_id", "")).startswith(PREFIX)]
        if mine:
            return f"a scalp is already working ({mine[0]['setup_id']})"
        today = self.today(now)
        if len(today) >= int(self.cfg["max_per_day"]):
            return f"max_per_day {self.cfg['max_per_day']} reached"
        lost = sum(float(o.get("result") or 0.0) for o in today)
        if lost <= -float(self.cfg["daily_loss_usd"]):
            return f"daily loss stop: {lost:.2f} today"
        return None
