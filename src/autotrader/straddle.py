"""
1.10.3 - NEWS STRADDLE (lab only, demo only).

Before a listed HIGH USD release (straddle_events), while the spread is still normal:
  BUY_STOP   ask + offset              stop entry - S   TP1 entry + tp1_r*S   TP2 entry + tp2_r*S
  BUY_STOP   ask + offset + add        (the "add" level, same geometry from its own entry)
  SELL_STOP  bid - offset              mirror
  SELL_STOP  bid - offset - add        mirror
  S = max(straddle_stop_atr * ATR15, straddle_min_stop), offset = max(straddle_offset_atr * ATR15, 3 x spread)
All of it sits at the broker before the release, so the fill happens at broker speed, not at the bot's 2 s.

After the release (checked every bridge poll, ~2 s):
  * as soon as one side has a position, every pending order of the other side is cancelled (OCO)
  * TP1: half closed and stop moved to entry by the executor's normal TP1 rule
  * unfilled orders expire straddle_expire_minutes after the release (broker expiry + executor)
  * anything still open straddle_max_hold_minutes after the release is closed at market

Every order is a lab order (magic_lab, tags news_straddle + news_lock), so it is reviewed and reported like any
lab trade and never touches the live track. A whipsaw that fills both sides inside one poll is possible and is
recorded as what it is.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import gold_monitor as GM

from . import alerts
from . import bridge as BR
from . import sessions as SS

UTC = timezone.utc
LOG = logging.getLogger("autotrader.straddle")
PREFIX = "NEWS-"


def group_of(t: datetime) -> str:
    return t.strftime("%m%d%H%M")


def next_event(calendar: dict | None, now: datetime, c: dict) -> dict | None:
    """The listed HIGH release starting within straddle_place_minutes (and not yet started)."""
    if not calendar:
        return None
    words = [w.lower() for w in c["straddle_events"]]
    lead = timedelta(minutes=c["straddle_place_minutes"])
    best = None
    for e in calendar.get("events") or []:
        if e.get("event_type") == "HOLIDAY" or (e.get("impact") or "").upper() != "HIGH":
            continue
        title = (e.get("title") or "").lower()
        if not any(w in title for w in words):
            continue
        t = GM.stamp(e["time"])
        if now < t <= now + lead and (best is None or t < best["t"]):
            best = {"t": t, "title": e["title"]}
    return best


def build_orders(quote: dict, atr15: float, c: dict, t: datetime, si) -> list[dict]:
    """The straddle's pending orders (2 or 4). Raises ValueError when the broker geometry does not allow it."""
    if not atr15 or atr15 <= 0:
        raise ValueError("ATR15 unavailable")
    bid, ask, sp = quote["bid"], quote["ask"], quote["spread"]
    min_dist = si.trade_stops_level * si.point
    offset = max(c["straddle_offset_atr"] * atr15, 3 * sp, min_dist + si.point)
    stop = max(c["straddle_stop_atr"] * atr15, c["straddle_min_stop"], min_dist + si.point)
    add = c["straddle_add_atr"] * atr15
    expiry = t + timedelta(minutes=c["straddle_expire_minutes"])
    g = group_of(t)
    out = []
    levels = [(1, offset)] + ([(2, offset + add)] if add > 0 else [])
    for direction, base, sign, otype in (("BUY", ask, 1, "BUY_STOP"), ("SELL", bid, -1, "SELL_STOP")):
        for lvl, dist in levels:
            entry = round(base + sign * dist, 2)
            sl = round(entry - sign * stop, 2)
            tp1 = round(entry + sign * c["straddle_tp1_r"] * stop, 2)
            tp2 = round(entry + sign * c["straddle_tp2_r"] * stop, 2)
            o = BR._order(direction, otype, entry, sl, [tp1, tp2], expiry, f"{PREFIX}{direction}{lvl}-{g}", "B_TRADE",
                          f"news straddle level {lvl} before {t:%H:%M}", deviation=c["max_entry_deviation_cap"])
            o["rr"] = round(c["straddle_tp1_r"], 2)
            out.append(o)
    return out


def oco_actions(positions: list, pendings: list, group: str) -> list:
    """Tickets of pending orders to cancel: every pending of the side opposite to an open position."""
    tag = lambda x: getattr(x, "comment", "") or ""
    mine = lambda xs: [x for x in xs if tag(x).startswith("LAB:" + PREFIX) and tag(x).endswith(group)]
    open_sides = {("BUY" if "BUY" in tag(p) else "SELL") for p in mine(positions)}
    if not open_sides or len(open_sides) == 2:
        return [] if not open_sides else [o.ticket for o in mine(pendings)]
    side = open_sides.pop()
    other = "SELL" if side == "BUY" else "BUY"
    return [o.ticket for o in mine(pendings) if f"{PREFIX}{other}" in tag(o)]


class Straddle:
    def __init__(self, c: dict, app):
        self.c, self.app = c, app
        self.enabled = bool(c.get("news_straddle")) and bool(c.get("lab_mode"))
        self.active: dict = {}                     # group -> event time
        self.logged: dict = {}

    def _log(self, key: str, msg: str, every: float = 600):
        if time.time() - self.logged.get(key, 0) >= every:
            LOG.info(msg)
            self.logged[key] = time.time()

    def tick(self, now: datetime):
        if not self.enabled:
            return
        try:
            self.place(now)
            self.manage(now)
        except Exception as exc:  # noqa: BLE001
            self._log("err:" + str(exc)[:40], f"Straddle skipped: {GM.safe_error(exc)[:200]}")

    def place(self, now: datetime):
        m, st = self.app.market, self.app.state
        ev = next_event(m.calendar, now, self.c)
        if ev is None:
            return
        g = group_of(ev["t"])
        if g in self.active or st.get("straddle:" + g):
            return
        block = self.app.lab.hard_block(now, check_book=False)
        if block:
            self._log("block:" + g, f"Straddle for {ev['title']} at {ev['t']:%H:%M} held: {block}", every=30)
            return
        q = m.quote()
        _, atr15 = self.app.market_context()
        si = m.symbol_info()
        orders = build_orders(q, atr15, self.c, ev["t"], si)
        lots = float(self.c["lab_lots"]) or float(si.volume_min)
        lots = max(float(si.volume_min), round(lots / si.volume_step) * si.volume_step)
        per_unit = si.trade_tick_value / si.trade_tick_size if si.trade_tick_size else 0.0
        st.put("straddle:" + g, {"event": ev["title"], "time": GM.iso(ev["t"]), "placed": GM.iso(now)})
        self.active[g] = ev["t"]
        sess = SS.session_of(now, self.c["sessions"])
        sent = []
        for o in orders:
            risk = round(lots * abs(o["entry_price"] - o["stop_loss"]) * per_unit, 2)
            o["strategy"] = "LAB"
            o["meta"] = {"lab": 1, "violations": ["news_straddle", "news_lock"], "playbook": "NEWS_STRADDLE",
                         "confirmation": "STRADDLE", "session": sess, "tier": "C",
                         "tier_reason": f"news straddle: {ev['title']} at {ev['t']:%H:%M} UTC"}
            try:
                ticket = self.app.executor.send(o, lots, risk, "B_TRADE", magic=self.c["magic_lab"])
                sent.append(f"{o['order_type']} {o['entry_price']}")
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Straddle order %s refused: %s", o["setup_id"], GM.safe_error(exc)[:160])
        LOG.info("STRADDLE %s for %s at %s UTC: %s | stop %.2f | %.2f lots", g, ev["title"], ev["t"].strftime("%H:%M"),
                 ", ".join(sent) or "nothing placed", abs(orders[0]["entry_price"] - orders[0]["stop_loss"]), lots)
        if sent:
            alerts.trade(f"🧪 LAB NEWS STRADDLE before {ev['title']} ({ev['t']:%H:%M} UTC)\n" + "\n".join(sent) +
                         f"\nstop {abs(orders[0]['entry_price'] - orders[0]['stop_loss']):.2f} each | TP1 "
                         f"{self.c['straddle_tp1_r']}R half + stop to entry | TP2 {self.c['straddle_tp2_r']}R | "
                         f"close after {self.c['straddle_max_hold_minutes']} min | {lots} lots")

    def manage(self, now: datetime):
        if not self.active and time.time() - self.logged.get("_restore", 0) < 60:
            return
        m, x = self.app.market, self.app.executor
        lab = self.c["magic_lab"]
        positions = [p for p in (m.trade_call("positions_get", symbol=m.symbol) or []) if p.magic == lab]
        pendings = [o for o in (m.trade_call("orders_get", symbol=m.symbol) or []) if o.magic == lab]
        if not self.active:                       # after a restart: pick up straddles still working at the broker
            self.logged["_restore"] = time.time()
            for it in positions + pendings:
                cm = getattr(it, "comment", "") or ""
                if cm.startswith("LAB:" + PREFIX):
                    g = cm.rsplit("-", 1)[-1]
                    try:
                        t = datetime.strptime(f"{now.year}{g}", "%Y%m%d%H%M").replace(tzinfo=UTC)
                    except ValueError:
                        continue
                    self.active.setdefault(g, t)
            if not self.active:
                return
            LOG.info("Straddle groups resumed after a restart: %s", ", ".join(sorted(self.active)))
        for g, t in list(self.active.items()):
            for ticket in oco_actions(positions, pendings, g):
                x.cancel(ticket, "news straddle: the other side filled")
            mine = [p for p in positions if (getattr(p, "comment", "") or "").startswith("LAB:" + PREFIX)
                    and (getattr(p, "comment", "") or "").endswith(g)]
            if now >= t + timedelta(minutes=self.c["straddle_max_hold_minutes"]):
                for p in mine:
                    self.close(p, f"news straddle time exit {self.c['straddle_max_hold_minutes']} min after the release")
                for o in pendings:
                    if (getattr(o, "comment", "") or "").endswith(g):
                        x.cancel(o.ticket, "news straddle finished")
                del self.active[g]

    def close(self, pos, why: str):
        m = self.app.market
        k = m.const
        q = m.quote()
        buy = pos.type == k("POSITION_TYPE_BUY")
        req = {"action": k("TRADE_ACTION_DEAL"), "symbol": m.symbol, "volume": pos.volume, "position": pos.ticket,
               "type": k("ORDER_TYPE_SELL") if buy else k("ORDER_TYPE_BUY"), "price": q["bid"] if buy else q["ask"],
               "deviation": 50, "magic": pos.magic, "comment": "TIME-EXIT", "type_filling": self.app.executor._filling()}
        if self.app.executor.dry_run:
            LOG.info("[DRY RUN] would close position %s: %s", pos.ticket, why)
            return
        self.app.executor._send(req)
        LOG.info("Closed position %s: %s", pos.ticket, why)
