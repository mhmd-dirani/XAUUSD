"""
1.14: the pre-news guard. The news lock stops NEW orders around a major release, but orders already at the broker and
open positions rode straight into it (10 Nov: two lab BUYs at 4076 were stopped by the CPI candle at 13:26-13:30).

Once per major release, when it is due within `minutes` (default: the V6 news_before_minutes, 30):
  - PENDING orders in scope are cancelled (a limit must not fill on the release spike);
  - OPEN positions in scope that are at least `be_at_r` R in profit get their stop moved to the entry (breakeven);
    positions below that keep their own stop (closing them would be a decision about the trade, not protection).
Scope: mode "lab" = lab orders only (magic_lab); "all" = lab and live (swing + overnight); "off" = nothing.
Nothing here opens or enlarges a position.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import alerts

UTC = timezone.utc
LOG = logging.getLogger("autotrader.newsguard")
DEFAULTS = {"mode": "off", "minutes": None, "be_at_r": 1.0}


class NewsGuard:
    def __init__(self, c: dict, app):
        self.c = c
        self.cfg = dict(DEFAULTS, **(c.get("pre_news_guard") or {}))
        self.app = app
        self.done: set = set()
        if self.cfg["mode"] != "off":
            LOG.info("Pre-news guard %s | %s min before a major release: cancel pending, stop to breakeven at >= %.1f R",
                     self.cfg["mode"].upper(), self.minutes, self.cfg["be_at_r"])

    @property
    def minutes(self) -> float:
        m = self.cfg.get("minutes")
        return float(m if m is not None else self.c.get("v6", {}).get("news_before_minutes", 30))

    def in_scope(self, rec: dict) -> bool:
        if self.cfg["mode"] == "all":
            return True
        return self.cfg["mode"] == "lab" and bool(rec.get("lab"))

    def tick(self, now: datetime) -> list:
        if self.cfg["mode"] == "off":
            return []
        m = self.app.market
        nxt = m.next_major_event(now, self.minutes)
        if not nxt:
            return []
        key = (nxt[0], nxt[1].isoformat())
        if key in self.done:
            return []
        self.done.add(key)
        title, at = nxt
        x = self.app.executor
        positions = {getattr(p, "ticket", None): p for p in (m.trade_call("positions_get", symbol=m.symbol) or [])}
        q = m.quote()
        out = []
        for rec in self.app.state.active_orders():
            if not self.in_scope(rec):
                continue
            if rec.get("status") == "PENDING":
                if x.cancel(rec["ticket"], f"pre-news guard: {title} at {at:%H:%M} UTC", status="CANCELLED"):
                    out.append(("cancelled", rec["setup_id"]))
                continue
            pos = positions.get(rec.get("position_ticket"))
            if pos is None:
                continue
            buy = rec["direction"] == "BUY"
            entry, sl0 = float(rec["entry"]), float(rec["sl"])
            risk = abs(entry - sl0)
            price = q["bid"] if buy else q["ask"]
            r = ((price - entry) if buy else (entry - price)) / risk if risk > 0 else 0.0
            cur = float(getattr(pos, "sl", 0.0) or 0.0)
            already = cur and ((cur >= pos.price_open) if buy else (cur <= pos.price_open))
            if r >= float(self.cfg["be_at_r"]) and not already:
                be = round(float(pos.price_open), 2)
                if x.move_stop(pos, be, f"pre-news guard: {title} at {at:%H:%M} UTC, trade at {r:+.2f} R"):
                    out.append(("breakeven", rec["setup_id"]))
        if out:
            msg = f"🛡 Pre-news guard before {title} ({at:%H:%M} UTC): " + ", ".join(f"{a} {s}" for a, s in out)
            LOG.info(msg)
            alerts.trade(msg, short=None)
        else:
            LOG.info("Pre-news guard before %s (%s UTC): nothing to do", title, at.strftime("%H:%M"))
        return out
