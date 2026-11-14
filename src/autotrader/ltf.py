"""
1.13: the LTF sample - a second 0.01-lot lab trade for every Astra idea the lab takes, entered on the 5M chart with
liquidity rules, so the plain zone entry (LAB-...) and the 5M entry (LTF-...) can be compared on the same ideas.

Why: the big timeframes pick the zone; nothing picked the moment. Lab limits sat at the zone edge and were run over
(BUY 4079 reached +1.96 R, then its stop), or filled before the move was ready.

The rules (demo lab only, magic_lab, through Lab.hard_block / Lab.size, one sample per setup per trading day):
  1. Watch: when the lab sends a plain order for an idea, this engine watches the same idea (zone, stop, TP1).
  2. Touch: the watch is armed when price trades into the zone (bid/ask inside it, or a closed 5M candle reaching it).
  3. Trigger (CHoCH on 5M): after the touch, a closed 5M candle that closes beyond the last 5M fractal swing made
     since the touch (BUY: above the last lower high; SELL: below the last higher low). Market entry at that close.
  4. Stop: beyond the extreme of the 5M leg into the zone (BUY: the lowest low since the touch) plus a buffer of
     max(stop_buffer_atr15 x ATR15, 2 x spread); never wider than the idea's own invalidation.
  5. Liquidity (pools = equal highs/lows on 5M within pool_tol_atr15 x ATR15, plus the session levels: PDH/PDL,
     Asia/London/NY highs and lows, previous week):
       - stop beyond a pool: a pool sitting within stop_pool_atr15 x ATR15 beyond the stop moves the stop past it
         (tag stop_beyond_pool); a stop right in front of a pool is where the hunt goes.
       - pool in the path: an untaken pool within pool_path_r x risk of the entry in the trade direction is TAGGED
         (pool_in_path). 1.13.2: tag only by default - the 5M chart times the entry, it does not veto the idea
         (9 Nov: two twins refused for PDH 4107.01 / EQH 4112.61; both plain trades ran through to TP1, +40.33 and
         +28.88). pool_path_blocks true restores the old refusal.
       - swept first: a pool on the stop side was wicked and reclaimed since the watch began (tag swept_first,
         else no_sweep). Recorded; required only with require_sweep.
  6. R:R to the idea's TP1 from the 5M entry and stop must be >= min_rr (2.0). TP1 stays the idea's TP1 so the two
     samples are measured on the same target.
  7. Never a hedge: Lab.hedge_with_working applies (1.13).
Setup ids LTF-<DIR>-<rounded zone mid>; strategy LAB_LTF; tags = the plain sample's rule tags + the LTF tags.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import alerts
from . import bridge as BR
from . import news as NW

UTC = timezone.utc
LOG = logging.getLogger("autotrader.ltf")
PREFIX = "LTF-"

DEFAULTS = {
    "mode": "off",                 # off | lab
    "swing_k": 2,                  # fractal: k candles each side
    "stop_buffer_atr15": 0.1,
    "min_rr": 2.0,
    "pool_tol_atr15": 0.1,         # equal highs/lows within this = one pool
    "pool_path_r": 1.0,            # untaken pool within this many R of the entry, in the trade direction = tag pool_in_path
    "pool_path_blocks": False,     # 1.13.2: True = refuse the entry instead of only tagging it
    "stop_pool_atr15": 0.3,        # a pool this close beyond the stop pulls the stop past it
    "require_sweep": False,
    "watch_hours": 12,
    "news_guard_minutes": 60,      # 1.14: no entry inside the news lock or with a major release due within this
    "one_per_zone": True,          # 1.14: no entry when a lab position/order already works this zone (any engine)
    "recheck_map": True,           # 1.14: no entry when Astra's newer map proposes the OPPOSITE trade at this zone
    "bars": 300,                   # closed 5M candles kept
}
TAGS = ("ltf_entry", "swept_first", "no_sweep", "stop_beyond_pool", "pool_in_path")


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


# ----------------------------------------------------------------------------- pure rules (bars = (t, o, h, l, c))
def swings(bars: list, start: int, k: int) -> list:
    """Confirmed fractal swings among bars[start:]: [(index, 'HIGH'|'LOW', price)], oldest first. A swing at i needs k
    closed candles each side, so the newest k candles can never hold one."""
    out = []
    for i in range(max(start, k), len(bars) - k):
        if all(bars[i][2] > bars[j][2] for j in range(i - k, i + k + 1) if j != i):
            out.append((i, "HIGH", bars[i][2]))
        if all(bars[i][3] < bars[j][3] for j in range(i - k, i + k + 1) if j != i):
            out.append((i, "LOW", bars[i][3]))
    return out


def choch(bars: list, since_idx: int, touch_idx: int, direction: str, k: int):
    """The 5M change of structure: the LAST closed candle closes beyond the most recent fractal swing against the leg
    into the zone (BUY: the last lower high, SELL: the last higher low), that swing made since the watch began
    (`since_idx`, it may precede the touch candle itself) and not already broken by an earlier close. The leg extreme
    is measured from that swing (BUY: the lowest low after the lower high). Returns (swing price, leg extreme) or None."""
    if since_idx < 0 or touch_idx < 0 or len(bars) < 2 * k + 2:
        return None
    side = "HIGH" if direction == "BUY" else "LOW"
    sw = [s for s in swings(bars, max(0, since_idx - k), k) if s[1] == side and s[0] >= since_idx]
    if not sw:
        return None
    idx, _, price = sw[-1]
    between = bars[idx + 1:-1]
    last = bars[-1]
    if direction == "BUY":
        if any(b[4] > price for b in between) or not last[4] > price:
            return None
        return price, min(b[3] for b in bars[idx:])
    if any(b[4] < price for b in between) or not last[4] < price:
        return None
    return price, max(b[2] for b in bars[idx:])


def equal_pools(bars: list, k: int, tol: float) -> list:
    """Liquidity pools from the 5M swings: two or more swing highs (or lows) within `tol` of each other.
    [{price, side, idx (newest), name}]."""
    out = []
    for side in ("HIGH", "LOW"):
        pts = [(i, p) for i, s, p in swings(bars, 0, k) if s == side]
        used = set()
        for a, (ia, pa) in enumerate(pts):
            if ia in used:
                continue
            group = [(ia, pa)] + [(ib, pb) for ib, pb in pts[a + 1:] if abs(pb - pa) <= tol and ib not in used]
            if len(group) >= 2:
                used.update(i for i, _ in group)
                price = max(p for _, p in group) if side == "HIGH" else min(p for _, p in group)
                out.append({"price": round(price, 2), "side": side, "idx": max(i for i, _ in group),
                            "name": f"EQ{'H' if side == 'HIGH' else 'L'}x{len(group)}"})
    return out


def session_pools(levels: list, bars: list) -> list:
    """The session engine's levels as pools (PDH, PDL, ASIA_HIGH, ..., PWH, PWL). idx = the first 5M candle after the
    level formed (so sweeps are only looked for after that)."""
    out = []
    for lv in levels or []:
        try:
            price, side, formed = float(lv["price"]), lv["side"], _ts(lv["formed_at"])
        except (KeyError, TypeError, ValueError):
            continue
        idx = next((i for i, b in enumerate(bars) if b[0] >= formed), len(bars))
        out.append({"price": round(price, 2), "side": side, "idx": idx, "name": str(lv.get("name"))})
    return out


def taken(pool: dict, bars: list, since: int) -> bool:
    """A pool is TAKEN once a candle after it closed beyond it (no longer resting liquidity)."""
    start = max(pool["idx"] + 1, since)
    if pool["side"] == "HIGH":
        return any(b[4] > pool["price"] for b in bars[start:])
    return any(b[4] < pool["price"] for b in bars[start:])


def swept(pool: dict, bars: list, since: int) -> bool:
    """A pool is SWEPT when a candle after `since` wicked beyond it and closed back inside."""
    start = max(pool["idx"] + 1, since)
    if pool["side"] == "HIGH":
        return any(b[2] > pool["price"] and b[4] < pool["price"] for b in bars[start:])
    return any(b[3] < pool["price"] and b[4] > pool["price"] for b in bars[start:])


def liquidity_plan(direction: str, entry: float, stop: float, tp1: float, pools: list, bars: list, since: int,
                   atr15: float, buffer: float, cfg: dict) -> tuple[float, list, str | None]:
    """Apply the liquidity rules. Returns (stop, tags, skip reason or None)."""
    buy = direction == "BUY"
    tags = []
    risk = abs(entry - stop)
    stop_side = "LOW" if buy else "HIGH"
    # swept first: a stop-side pool between the stop region and the entry was wicked and reclaimed since the watch
    near = [p for p in pools if p["side"] == stop_side and ((stop - atr15 <= p["price"] < entry) if buy
                                                             else (entry < p["price"] <= stop + atr15))]
    tags.append("swept_first" if any(swept(p, bars, since) for p in near) else "no_sweep")
    # stop in front of a pool: pull it past the pool
    beyond = [p for p in pools if p["side"] == stop_side and not taken(p, bars, since)
              and ((stop - cfg["stop_pool_atr15"] * atr15 <= p["price"] <= stop) if buy
                   else (stop <= p["price"] <= stop + cfg["stop_pool_atr15"] * atr15))]
    if beyond:
        edge = min(p["price"] for p in beyond) if buy else max(p["price"] for p in beyond)
        stop = round(edge - buffer if buy else edge + buffer, 2)
        risk = abs(entry - stop)
        tags.append("stop_beyond_pool")
    # pool in the path: untaken opposite-side pool within pool_path_r x risk of the entry toward TP1
    reach = cfg["pool_path_r"] * risk
    path = [p for p in pools if p["side"] == ("HIGH" if buy else "LOW") and not taken(p, bars, since)
            and ((entry < p["price"] <= entry + reach and p["price"] < tp1) if buy
                 else (entry - reach <= p["price"] < entry and p["price"] > tp1))]
    if path:
        tags.append("pool_in_path")
        p = path[0]
        if cfg.get("pool_path_blocks"):
            return stop, tags, f"untaken liquidity {p['name']} at {p['price']:.2f} within {cfg['pool_path_r']} R of the entry"
    if cfg.get("require_sweep") and "swept_first" not in tags:
        return stop, tags, "no liquidity sweep before the entry (require_sweep)"
    return stop, tags, None


# ----------------------------------------------------------------------------- the engine
class LTF:
    def __init__(self, c: dict, app):
        self.c = c
        self.cfg = dict(DEFAULTS, **(c.get("ltf_lab") or {}))
        self.app = app
        self.bars: list = []
        self.bars_key = None
        self.watches: dict = {}
        self.logged: set = set()
        if self.live:
            try:
                self.watches = dict(app.state.get("ltf_watches", {}) or {})
            except Exception:  # noqa: BLE001
                self.watches = {}
            LOG.info("LTF lab ON | 5M CHoCH entry, fractal k=%s | stop buffer %.2f ATR15 | min %.1f R | pool path %.1f R "
                     "(%s) | stop-pool %.2f ATR15 | require_sweep %s | %d watch(es) restored", self.cfg["swing_k"],
                     self.cfg["stop_buffer_atr15"], self.cfg["min_rr"], self.cfg["pool_path_r"],
                     "blocks" if self.cfg["pool_path_blocks"] else "tag only",
                     self.cfg["stop_pool_atr15"], self.cfg["require_sweep"], len(self.watches))

    @property
    def live(self) -> bool:
        return self.cfg["mode"] == "lab" and bool(self.c.get("lab_mode"))

    def _save(self):
        try:
            self.app.state.put("ltf_watches", self.watches)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("LTF: watches not saved (%s)", str(exc)[:120])

    def _once(self, key, msg, *args):
        if key not in self.logged:
            self.logged.add(key)
            LOG.info(msg, *args)

    # ---- registration
    def add_watch(self, sid: str, direction: str, lo: float, hi: float, stop: float, tp1: float, tags: list, now: datetime,
                  title: str = "", plain: str | None = None, analysis: str | None = None, idea: str | None = None,
                  playbook: str = "LTF_5M_CHOCH") -> bool:
        """1.13.1: a watch from any source (the lab's plain sample, the 1H-trend engine). False when not registered."""
        if not self.live or sid in self.watches:
            return False
        buy = direction == "BUY"
        if not ((stop < lo <= hi < tp1) if buy else (tp1 < lo <= hi < stop)):
            LOG.info("LTF watch %s refused: zone %.2f-%.2f, stop %.2f, tp1 %.2f are not in order", sid, lo, hi, stop, tp1)
            return False
        self.watches[sid] = {"setup_id": sid, "analysis": analysis, "idea": idea, "direction": direction, "lo": float(lo),
                             "hi": float(hi), "stop": float(stop), "tp1": float(tp1), "created": _iso(now),
                             "expires": _iso(now + timedelta(hours=int(self.cfg["watch_hours"]))), "touch_idx": None,
                             "touch_t": None, "tags": list(tags or []), "plain": plain, "title": str(title)[:120],
                             "playbook": playbook}
        self._save()
        LOG.info("LTF watch %s: zone %.2f-%.2f stop %.2f tp1 %.2f; waiting for the touch and a 5M CHoCH", sid, lo, hi,
                 stop, tp1)
        return True

    def watch(self, a: dict, idea: dict, order: dict, tags: list, now: datetime):
        """The lab's plain sample was sent: watch the same idea for a 5M entry."""
        lo, hi = float(idea["entry_low"]), float(idea["entry_high"])
        sid = f"{PREFIX}{idea['direction']}-{round((lo + hi) / 2)}"
        self.add_watch(sid, idea["direction"], lo, hi, float(idea["invalidation"]), float(order["take_profit_1"]), tags, now,
                       title=str(idea.get("title", "")), plain=order.get("setup_id"), analysis=a.get("id"), idea=idea.get("id"))

    # ---- candles
    def refresh(self) -> bool:
        key = self.app.market.rates("TIMEFRAME_M5", 1)
        key = int(key[-1][0]) if key else None
        if self.bars and key == self.bars_key:
            return False
        self.bars = [tuple(r) for r in self.app.market.rates("TIMEFRAME_M5", int(self.cfg["bars"]))]
        self.bars_key = key
        return True

    def _idx_at(self, t: int) -> int:
        """Index of the first closed 5M candle whose open time >= t (len(bars) when none)."""
        return next((i for i, b in enumerate(self.bars) if b[0] >= t), len(self.bars))

    # ---- one pass
    def tick(self, now: datetime) -> list:
        if not self.live or not self.watches:
            return []
        sess = getattr(self.app.market, "sessions", None)
        if sess is not None and not sess.is_open(now):
            return []
        new = self.refresh()
        if len(self.bars) < 2 * self.cfg["swing_k"] + 5:
            return []
        quote = self.app.market.quote()
        out = []
        for sid in list(self.watches):
            try:
                res = self.one(sid, now, quote, new)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("LTF %s skipped: %s", sid, str(exc)[:160])
                continue
            if res:
                out.append(res)
        return out

    def _drop(self, sid: str, why: str):
        w = self.watches.pop(sid, None)
        self._save()
        if w:
            LOG.info("LTF watch %s ended: %s", sid, why)
        return None

    def one(self, sid: str, now: datetime, quote: dict, new_bar: bool):
        w = self.watches[sid]
        buy = w["direction"] == "BUY"
        price = quote["ask"] if buy else quote["bid"]
        if now >= datetime.fromisoformat(w["expires"].replace("Z", "+00:00")):
            return self._drop(sid, "watch expired")
        if (buy and quote["bid"] <= w["stop"]) or (not buy and quote["ask"] >= w["stop"]):
            return self._drop(sid, f"price beyond the idea's stop {w['stop']:.2f}")
        born = self._idx_at(_ts(w["created"]))
        if any((b[3] <= w["stop"]) if buy else (b[2] >= w["stop"]) for b in self.bars[born:]):
            return self._drop(sid, f"a 5M candle traded through the stop {w['stop']:.2f}")
        tol = float(self.c.get("confirm_entry_tolerance", 0.0) or 0.0)
        if w["touch_idx"] is None:
            hit = w["lo"] - tol <= price <= w["hi"] + tol
            bar_hit = next((i for i, b in enumerate(self.bars) if i >= born and
                            ((b[3] <= w["hi"] + tol) if buy else (b[2] >= w["lo"] - tol))), None)
            if hit or bar_hit is not None:
                # the touch candle: the closed one that reached the zone, else the forming one (opens after the last closed)
                w["touch_t"] = self.bars[bar_hit][0] if bar_hit is not None else self.bars[-1][0] + 300
                w["touch_idx"] = bar_hit if bar_hit is not None else len(self.bars)
                self._save()
                LOG.info("LTF %s: zone touched; waiting for a 5M CHoCH %s", sid, "up" if buy else "down")
            return None
        if not new_bar:
            return None
        touch_idx = self._idx_at(int(w["touch_t"]))
        if touch_idx >= len(self.bars):
            return None
        hit = choch(self.bars, born, touch_idx, w["direction"], int(self.cfg["swing_k"]))
        if hit is None:
            return None
        swing, extreme = hit
        return self.enter(sid, w, now, quote, swing, extreme, born, touch_idx)

    # ---- 1.14: the recheck at the moment of entry
    def recheck(self, sid: str, w: dict, now: datetime, price: float, atr15: float):
        """('wait' | 'drop', reason) when the entry must not happen now, else None. Run when the 5M trigger fires,
        i.e. at the moment of entry, not when the idea was proposed (10 Nov: a BUY limit placed at 10:02 filled at
        10:27 against a bearish 1H/15M, its 5M twin bought the same zone again at 12:05, and the CPI candle at
        13:26-13:30 stopped both)."""
        app = self.app
        guard = float(self.cfg.get("news_guard_minutes") or 0)
        try:
            lock = app.market.news_block(now)
            if lock:
                return "wait", f"news lock: {lock}"
            nxt = app.market.next_major_event(now, guard) if guard > 0 else None
            if nxt:
                return "wait", f"{nxt[0]} due at {nxt[1]:%H:%M} UTC (within {guard:.0f} min)"
        except Exception as exc:  # noqa: BLE001  - an unreadable calendar is not "no news"
            return "wait", f"calendar unreadable ({str(exc)[:80]})"
        tol = max(float(self.c.get("confirm_entry_tolerance", 0.0) or 0.0), 0.5 * atr15)
        if self.cfg.get("one_per_zone"):
            for rec in app.state.active_orders():
                if not rec.get("lab") or rec.get("direction") != w["direction"] or str(rec.get("setup_id", "")).startswith("SCP-"):
                    continue
                e = float(rec["entry"])
                if w["lo"] - tol <= e <= w["hi"] + tol or abs(e - price) <= tol:
                    return "drop", f"zone already worked by {rec['setup_id']} (ticket {rec['ticket']}, {rec.get('status')})"
        if self.cfg.get("recheck_map") and getattr(app, "bridge", None) is not None:
            try:
                a, _ = app.bridge.analysis(now)
            except Exception:  # noqa: BLE001
                a = None
            if a and a.get("created_at") and _ts(a["created_at"]) > _ts(w["created"]):
                from .lab import contradicted
                against = contradicted({"direction": w["direction"], "entry": price}, a.get("ideas") or [], tol)
                if against is not None:
                    return "drop", (f"Astra's {a['created_at'][11:16]} map proposes the opposite here "
                                    f"({against.get('id')} {against['direction']} {against['entry_low']}-{against['entry_high']})")
        return None

    def enter(self, sid: str, w: dict, now: datetime, quote: dict, swing: float, extreme: float, born: int, touch_idx: int):
        app, lab = self.app, self.app.lab
        buy = w["direction"] == "BUY"
        spread = float(quote["spread"])
        _, atr15 = app.market_context()
        atr15 = float(atr15 or 0.0)
        verdict = self.recheck(sid, w, now, quote["ask"] if buy else quote["bid"], atr15)
        if verdict:
            kind, why = verdict
            if kind == "drop":
                LOG.info("LTF %s: 5M CHoCH at %.2f but the recheck refused it: %s", sid, swing, why)
                return self._drop(sid, why)
            self._once(("wait", sid, why[:40]), "LTF %s: 5M CHoCH at %.2f, entry held: %s", sid, swing, why)
            return None
        buffer = max(self.cfg["stop_buffer_atr15"] * atr15, 2 * spread)
        entry = quote["ask"] if buy else quote["bid"]
        stop = round(extreme - buffer if buy else extreme + buffer, 2)
        if (buy and stop < w["stop"]) or (not buy and stop > w["stop"]):
            stop = w["stop"]                                          # never wider than the idea's own invalidation
        tp1 = w["tp1"]
        state, snap = app.market_state()
        pools = equal_pools(self.bars, int(self.cfg["swing_k"]), self.cfg["pool_tol_atr15"] * atr15) \
            + session_pools(snap.get("levels"), self.bars)
        stop, ltags, skip = liquidity_plan(w["direction"], entry, stop, tp1, pools, self.bars, born, atr15, buffer, self.cfg)
        tags = ["ltf_entry", "rechecked"] + ltags + [t for t in w.get("tags") or [] if t not in ("no_confirmation", "news_lock")]
        if skip:
            LOG.info("LTF %s: 5M CHoCH at %.2f but no trade: %s", sid, swing, skip)
            return self._drop(sid, skip)
        risk = abs(entry - stop)
        if risk <= 0:
            return self._drop(sid, "stop at the entry")
        rr = round(abs(tp1 - entry) / risk, 2)
        if (buy and not entry < tp1) or (not buy and not entry > tp1):
            return self._drop(sid, f"price already beyond TP1 {tp1:.2f} at the CHoCH")
        if rr < float(self.cfg["min_rr"]):
            return self._drop(sid, f"R:R {rr} to TP1 {tp1:.2f} from the 5M entry is below {self.cfg['min_rr']}")
        si = app.market.symbol_info()
        min_dist = si.trade_stops_level * si.point
        if risk < max(min_dist, 1e-9) or abs(tp1 - entry) < max(min_dist, 1e-9):
            return self._drop(sid, f"stop or TP1 inside the broker stop level ({min_dist})")
        block = lab.hard_block(now)
        if block:
            self._once(("block", sid, block[:30], now.strftime("%Y%m%d%H")), "LTF %s: holding: %s", sid, block)
            return None
        day0 = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        if app.state.setup_orders_since(sid, day0, lab=True):
            return self._drop(sid, f"{sid} already traded in the last 24 h")
        order = BR._order(w["direction"], "MARKET", entry, stop, [tp1], now + timedelta(minutes=30), sid, "B_TRADE",
                          f"LTF 5M entry for {w.get('plain')}: CHoCH {'above' if buy else 'below'} {swing:.2f}, leg "
                          f"{'low' if buy else 'high'} {extreme:.2f}; {w.get('title', '')}",
                          deviation=self.c["signal_entry_tolerance"])
        order["move_sl_to_breakeven_at_tp1"] = False
        order["rr"] = rr
        order["max_entry_deviation"] = min(float(order["max_entry_deviation"]), float(self.c["max_entry_deviation_cap"]))
        why = lab.hedge_with_working(order, state.get("macro_trend"))
        if why:
            return self._drop(sid, why)
        lots, risk_money, hold = lab.size(si, risk)
        if hold:
            self._once(("size", sid), "LTF %s: holding: %s", sid, hold)
            return None
        order["strategy"] = "LAB_LTF"
        order["meta"] = {"playbook": w.get("playbook") or "LTF_5M_CHOCH", "confirmation": "CHOCH_5M",
                         "session": snap.get("session"), "lab": 1, "violations": tags, "tier": None,
                         "tier_reason": f"lab: 5M entry sample of {w.get('plain') or sid}"}
        order["spread"], order["atr15"] = spread, round(atr15, 2)
        self.watches.pop(sid, None)                                   # consumed BEFORE the send: never resent
        self._save()
        try:
            ticket = app.executor.send(order, lots, risk_money, "B_TRADE", magic=self.c["magic_lab"])
        except NW.NewsHold as h:                                     # 1.17: the watch stays; a later CHoCH may enter
            self.watches[sid] = w
            self._save()
            self._once(("newshold", sid, str(h)[:30]), "LTF %s: held: %s", sid, h)
            return None
        except Exception as exc:  # noqa: BLE001
            alerts.error(f"LTF lab order {sid} failed: {exc}")
            return f"{sid}: broker: {exc}"
        LOG.info("LTF ORDER SENT ticket=%s %s MARKET @ %.2f sl %.2f tp1 %.2f (%.2f R) | %.2f lots risk %.2f | %s", ticket, sid,
                 entry, stop, tp1, rr, lots, risk_money, ",".join(tags))
        alerts.trade(f"🧪 LAB 5M {w['direction']} {sid} MARKET @ {entry:.2f} | SL {stop:.2f} | TP1 {tp1:.2f} | R:R {rr} | "
                     f"{lots} lots | CHoCH {swing:.2f} | {', '.join(ltags)} | "
                     f"{'pair of ' + str(w.get('plain')) if w.get('plain') else '1H-trend retest'}", short=None)
        return f"{sid}: sent #{ticket}"
