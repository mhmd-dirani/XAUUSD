"""
1.10 - LAB MODE (DEMO ONLY): the rule-testing track.

The live track keeps every rule. The lab track takes every idea the live track did NOT trade - held by
the session gate, classified counter-trend without a zone, below the R:R floor, waiting for a
confirmation, rejected by the risk engine - at the broker's minimum lot, under its own magic number,
and stores on the order the list of rules the trade broke ("violations"). The existing post-trade
review (review.py) measures lab trades like any other, and `--report` shows, per rule, the expectancy
of the trades that broke it against the trades that kept it. That is how a rule is tested instead of
argued about. Nothing here changes a live rule.

What stays on in the lab (they decide whether a result MEASURES anything, not whether it is "safe"):
  demo account (require_demo), terminal connected and Algo Trading on, market open, fresh quote,
  absolute spread cap (broker_max_spread), the broker's stop level, executable sides (stop and TP on
  the right side of the entry), one lab order per setup per UTC day, lab_max_open, lab_max_per_day,
  lab_drawdown_kill_pct, the STOP kill file.

Every other gate becomes a TAG (see TAGS). Lab orders use TP1 only (no partial, no breakeven move):
the cleanest question - did TP1 come before the stop - and the one Astra's confidence is defined on.
TP2/TP3 are kept in the order's meta for the review.

Sizing: lab_lots or the symbol's volume_min. R is measured from the real stop distance, so results
in R are comparable to the live track; results in money are meaningless and the report says so.
"""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime, timedelta, timezone

import gold_evidence as EV
import gold_monitor as GM

from . import alerts
from . import news as NW
from . import bridge as BR
from . import playbooks as PB
from . import sessions as SS
from . import tiers as TR

UTC = timezone.utc
LOG = logging.getLogger("autotrader.lab")

# The rules a lab trade may break. Each tag is a live gate; the report groups results by tag.
TAGS = (
    "session_off",            # session not in trade_sessions
    "open_close_buffer",      # first/last minutes of the trading day (skip_minutes_after_open / before_close)
    "news_lock",              # inside the calendar lock
    "live_book_full",         # the live track already has max_active_orders working
    "live_daily_cap",         # max_trades_per_day reached on the live track
    "loss_limit",             # a live loss limit is hit (or unverifiable)
    "mtf_opposed",            # 4H/1H structure against the direction (or unknown)
    "counter_no_zone",        # opposed and not at a strong remembered Daily/4H zone
    "class_off",              # the playbook class is NONE or its mode is off
    "expansion_against",      # a strong one-way expansion runs against the idea
    "expansion_unvalidated",  # ... and no reversal method validates fading it
    "no_confirmation",        # no closed 15M confirmation yet (entry at the zone edge)
    "rr_below_min",           # reward/risk to TP1 from the zone edge below min_rr
    "stop_below_atr_min",     # stop < sl_atr_min x ATR15
    "stop_above_atr_max",     # stop > sl_atr_max x ATR15
    "low_confidence",         # confidence below tiers.C.min_confidence
    "risk_high",              # Astra's own risk label is HIGH
    "not_anchored",           # stop or TP1 not on a remembered level
    "far_from_zone",          # zone further than 2 x ATR15 from price when the order is placed
    "zone_passed",            # price went through the zone toward the stop (entered at market beyond it)
    "risk_engine",            # the live risk engine rejected the live order (reason in note)
    "news_straddle",          # 1.10.3: a pre-release straddle order (straddle.py), not an Astra idea
    "tp_touched_first",       # 1.11.1: price reached TP1 before the limit filled; kept working (lab_keep_after_tp_touch)
    "tp_passed_at_entry",     # 1.11.3: price was already beyond TP1 when Astra proposed it (e.g. "sell the rally")
    "tp_beyond_opposite",     # 1.11.6: TP1 lies beyond the entry of the same analysis's opposite idea (measured only)
)


def _day0(now: datetime) -> str:
    return now.replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _trading_day0(now: datetime) -> str:
    """Start (UTC) of the current New York trading day (17:00 NY the evening before). 1.10.1: the first night
    took U1 at 22:44 and again at 02:26 because the UTC date had changed; both stopped on the same tick, which
    is one sample counted twice."""
    day = SS.trading_day(now)
    start = datetime(day.year, day.month, day.day, 17, 0, tzinfo=SS.NY) - timedelta(days=1)
    return start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _week0(now: datetime) -> datetime:
    d = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return d - timedelta(days=d.weekday())


def duplicate_of(order: dict, active: list, tol: float) -> dict | None:
    """1.10.6: a working LAB order of the same direction whose entry AND stop lie within `tol` of this one (the
    live track's duplicate_atr rule, applied to the lab). Setup ids are the idea's rounded mid-price, so two
    analyses an hour apart can describe one order under two ids: 28 Oct LAB-BUY-4039 (09:02) and LAB-BUY-4042
    (10:02) were both BUY_LIMIT 4043.00 / sl 4027.50 / tp1 4064.74, both filled at 14:29:06 and both stopped at
    18:33:10. One sample counted twice, and two positions stacked at one level. Returns the working order.
    1.11.6: also the same zone with a slightly different entry: stops within `tol` and entries within half the
    smaller stop distance. 5 Nov LAB-BUY-4117 (4123.62 / sl 4096.00) and LAB-BUY-4113 (4117.64 / sl 4095.50), two
    maps buying one M128 support, both stopped at 14:45 (-50.71, one idea counted as two losses)."""
    entry, sl = float(order["entry_price"]), float(order["stop_loss"])
    for a in active:
        if not a.get("lab") or a.get("direction") != order["direction"]:
            continue
        if str(a.get("setup_id", "")).startswith(("W4H-", "LTF-", "H1T-", "SCP-")):   # other engines are separate samples
            continue
        de, ds = abs(float(a["entry"]) - entry), abs(float(a["sl"]) - sl)
        if ds > tol:
            continue
        near = max(tol, 0.5 * min(abs(float(a["entry"]) - float(a["sl"])), abs(entry - sl)))
        if de <= near:
            return a
    return None


def opposite_overlap(x: dict, y: dict) -> bool:
    """1.13: x and y (direction, entry, sl, tp1) are a HEDGE when they run opposite ways and each entry lies strictly
    between the other's stop and TP1: whichever way price goes, one's win is the other's loss. 6 Nov 07:58: lab SELL
    4055.90 (sl 4073.04 / tp 4035.87) and lab BUY 4055.97 (sl 4038.48 / tp 4073.04), both open 49 minutes, -0.20 net
    plus two spreads and commissions. The ladder (SELL tp 4034.06 and a BUY limit AT 4034.06) is not a hedge: the buy
    fills only where the sell has already taken its profit."""
    if x["direction"] == y["direction"]:
        return False
    def inside(entry, other):
        lo, hi = sorted((float(other["sl"]), float(other["tp1"])))
        return lo < float(entry) < hi
    return inside(x["entry"], y) and inside(y["entry"], x)


def hedge_skips(cands: list, macro: str | None) -> dict:
    """1.13: which candidates of one sweep to skip so no two of them hedge each other. cands = [(key, geometry)].
    With a Daily/4H macro (BULLISH / BEARISH) the idea against it is dropped; with no macro (MIXED/NEUTRAL) both are
    (Astra has no clear view and the pair would only pay the spread twice). Returns {key: reason}."""
    want = {"BULLISH": "BUY", "BEARISH": "SELL"}.get(macro or "")
    out = {}
    for i, (ka, a) in enumerate(cands):
        for kb, b in cands[i + 1:]:
            if not opposite_overlap(a, b):
                continue
            for k, g in ((ka, a), (kb, b)):
                if want is None:
                    out[k] = f"hedges {kb if k == ka else ka} and the Daily/4H macro is {macro}: neither is taken"
                elif g["direction"] != want:
                    out[k] = f"hedges {kb if k == ka else ka} against the Daily/4H macro {macro}"
    return out


def geometry(order: dict) -> dict:
    return {"direction": order["direction"], "entry": order["entry_price"], "sl": order["stop_loss"],
            "tp1": order["take_profit_1"]}


def tp_beyond_opposite(order: dict, ideas: list | None) -> bool:
    """1.11.6 (measured, no trading effect): TP1 reaches past the near edge of an opposite idea in the same analysis.
    5 Nov: BUY TP1 4146.12 while the SELL idea entered from 4140.29, so the BUY needed price to push into the zone
    where the SELL expected it to turn. A BUY's opposite near edge is the SELL's entry_low; a SELL's is the BUY's
    entry_high."""
    buy = order["direction"] == "BUY"
    entry, tp1 = float(order["entry_price"]), float(order["take_profit_1"])
    for i in ideas or []:
        if i.get("direction") not in ("BUY", "SELL") or i.get("direction") == order["direction"]:
            continue
        try:
            edge = float(i["entry_low"] if buy else i["entry_high"])
        except (KeyError, TypeError, ValueError):
            continue
        if (entry < edge < tp1) if buy else (tp1 < edge < entry):
            return True
    return False


def still_proposed(rec: dict, ideas: list, tol: float) -> dict | None:
    """1.11.1: the idea of an analysis that still proposes this lab order: same direction, entry zone containing the
    order's entry (± tol), stop on the same side. None when Astra no longer proposes it."""
    buy = rec["direction"] == "BUY"
    for i in ideas:
        try:
            if i.get("direction") != rec["direction"]:
                continue
            lo, hi, stop = float(i["entry_low"]), float(i["entry_high"]), float(i["invalidation"])
        except (KeyError, TypeError, ValueError):
            continue
        if lo - tol <= float(rec["entry"]) <= hi + tol and ((stop < rec["entry"]) if buy else (stop > rec["entry"])):
            return i
    return None


def contradicted(rec: dict, ideas: list, tol: float) -> dict | None:
    """1.13.1: the idea of a newer analysis that CONTRADICTS a working lab order: the opposite direction with an entry
    zone containing the order's entry (± tol), i.e. Astra now wants to trade the other way from the same place.
    None when nothing contradicts it (an idea merely missing from the newer map is not a contradiction: on 6 Nov the
    03:13 SELL at the 1H gap 4073-4077 was dropped at 05:35 for that, then filled the whole move after NFP)."""
    for i in ideas:
        try:
            if i.get("direction") == rec["direction"] or i.get("direction") not in ("BUY", "SELL"):
                continue
            lo, hi = float(i["entry_low"]), float(i["entry_high"])
        except (KeyError, TypeError, ValueError):
            continue
        if lo - tol <= float(rec["entry"]) <= hi + tol:
            return i
    return None


def zone_rr(idea: dict, min_rr: float) -> float:
    """Reward/risk to TP1 from the worst fill inside the zone (the signal bot's own rule)."""
    buy = idea["direction"] == "BUY"
    edge = idea["entry_low"] if buy else idea["entry_high"]
    risk = abs(edge - idea["invalidation"])
    return abs(idea["targets"][0] - edge) / risk if risk > 0 else 0.0


def tags_for(idea: dict, *, c: dict, state: dict, snap: dict, evidence: dict | None, atr15: float,
             quote: dict, strong: bool, dec: dict, confirmed: str | None, live: dict) -> list[str]:
    """The rules this candidate breaks, from the same functions the live track uses. `live` carries the
    account-level answers (session gate text, trading window, news, book, caps, loss limits)."""
    d = idea["direction"]
    want = "BULLISH" if d == "BUY" else "BEARISH"
    tags = []
    allowed = c.get("trade_sessions") or []
    if allowed and (snap.get("session") or "OFF") not in allowed:
        tags.append("session_off")
    if live.get("window"):
        tags.append("open_close_buffer")
    if live.get("news"):
        tags.append("news_lock")
    if live.get("book_full"):
        tags.append("live_book_full")
    if live.get("daily_cap"):
        tags.append("live_daily_cap")
    if live.get("loss_limit"):
        tags.append("loss_limit")
    opposed = EV.mtf_opposed(d, evidence)
    if opposed:
        tags.append("mtf_opposed")
        if not strong:
            tags.append("counter_no_zone")
    if dec.get("mode") == "off":
        tags.append("class_off")
    exp = dec.get("expansion") or {}
    if exp.get("against"):
        tags.append("expansion_against")
    if dec.get("would_block"):
        tags.append("expansion_unvalidated")
    if not confirmed:
        tags.append("no_confirmation")
    if zone_rr(idea, c["min_rr"]) < c["min_rr"]:
        tags.append("rr_below_min")
    edge = idea["entry_low"] if d == "BUY" else idea["entry_high"]
    stop = abs(edge - idea["invalidation"])
    if atr15 and stop < c["sl_atr_min"] * atr15:
        tags.append("stop_below_atr_min")
    if atr15 and stop > c["sl_atr_max"] * atr15:
        tags.append("stop_above_atr_max")
    conf = int(idea.get("confidence") or 0)
    if conf < c["tiers"]["C"]["min_confidence"]:
        tags.append("low_confidence")
    if (idea.get("astra_risk") or idea.get("risk")) == "HIGH":
        tags.append("risk_high")
    anchors = idea.get("tp_anchors") or []
    if not (idea.get("sl_anchor") and anchors and anchors[0]):
        tags.append("not_anchored")
    price = quote["ask"] if d == "BUY" else quote["bid"]
    dist = max(idea["entry_low"] - price, price - idea["entry_high"], 0.0)
    if atr15 and dist > 2 * atr15:
        tags.append("far_from_zone")
    # passed = price went THROUGH the zone toward the stop without us (SELL above it, BUY below it); the only
    # entry left is a market order beyond the zone. Price on the TP side has simply not reached the zone yet.
    if (d == "BUY" and price < idea["entry_low"]) or (d == "SELL" and price > idea["entry_high"]):
        tags.append("zone_passed")
    if live.get("risk_engine"):
        tags.append("risk_engine")
    return tags


def plan(idea: dict, quote: dict, c: dict, now: datetime, confirmed: str | None, si) -> tuple[dict | None, str | None]:
    """The lab order for an idea at the current price (TP1 only). (order, None) or (None, why not executable)."""
    buy = idea["direction"] == "BUY"
    lo, hi, stop, tp1 = idea["entry_low"], idea["entry_high"], idea["invalidation"], idea["targets"][0]
    bid, ask = quote["bid"], quote["ask"]
    price = ask if buy else bid
    min_dist = si.trade_stops_level * si.point
    if buy and not stop < tp1 or (not buy and not tp1 < stop):
        return None, "stop and TP1 on the wrong sides"
    if (buy and price <= stop) or (not buy and price >= stop):
        return None, "price is already beyond the stop"
    beyond_tp1 = (buy and price >= tp1) or (not buy and price <= tp1)
    if beyond_tp1 and not c.get("lab_keep_after_tp_touch"):
        return None, "TP1 already reached"
    # 1.11.1: with lab_keep_after_tp_touch the idea is placed anyway as a limit at its zone edge (price may come back);
    # the executor tags it tp_touched_first on its first pass and Astra's next map decides whether it stays.
    setup_id = f"LAB-{idea['direction']}-{round((lo + hi) / 2)}"
    title = f"lab {idea.get('confidence')}% {idea.get('risk')}{' confirmed by ' + confirmed if confirmed else ' unconfirmed'}: " \
            f"{idea.get('title', '')}"
    tol = float(c.get("confirm_entry_tolerance", 0.0) or 0.0)
    inside = lo - tol <= price <= hi + tol
    passed = (buy and price < lo - tol) or (not buy and price > hi + tol)     # through the zone, toward the stop
    if (inside or passed or confirmed) and not beyond_tp1:
        otype, entry, expiry = "MARKET", price, now + timedelta(minutes=30)
        if confirmed and not inside and not passed:            # confirmed but price moved away: limit at the zone edge
            otype, entry = ("BUY_LIMIT" if buy else "SELL_LIMIT"), round(hi if price > hi else lo, 2)
            expiry = now + timedelta(hours=min(c["confirm_retest_hours"], c["max_order_lifetime_hours"]))
    else:                                                       # no confirmation: wait at the zone edge nearest price
        otype, entry = ("BUY_LIMIT" if buy else "SELL_LIMIT"), round(hi if price > hi else lo, 2)
        expiry = now + timedelta(hours=min(c["idea_order_hours"], c["max_order_lifetime_hours"]))
    if otype != "MARKET":
        if (buy and not entry < ask - min_dist) or (not buy and not entry > bid + min_dist):
            return None, f"{otype} at {entry} is on the wrong side of the price"
        if (buy and not stop < entry < tp1) or (not buy and not tp1 < entry < stop):
            return None, f"{otype} at {entry} is not between the stop and TP1"
    if abs(entry - stop) < max(min_dist, 1e-9) or abs(tp1 - entry) < max(min_dist, 1e-9):
        return None, f"stop or TP1 inside the broker stop level ({min_dist})"
    # 1.11.3: an idea proposed with price already beyond its TP1 ("sell the rally back to 4057") is a plain limit at
    # the zone: Astra made it knowing the price, so it gets no cancel level and no early re-map; it runs to expiry.
    watch = not beyond_tp1 and otype != "MARKET"
    order = BR._order(idea["direction"], otype, entry, stop, [tp1], expiry, setup_id, "B_TRADE", title,
                      cancel_above=(tp1 if buy and watch else None),
                      cancel_below=(tp1 if not buy and watch else None),
                      deviation=c["signal_entry_tolerance"])
    order["_tp_passed_at_entry"] = beyond_tp1
    order["move_sl_to_breakeven_at_tp1"] = False
    order["rr"] = round(abs(tp1 - entry) / abs(entry - stop), 2)
    order["max_entry_deviation"] = min(float(order["max_entry_deviation"]), float(c["max_entry_deviation_cap"]))
    return order, None


class Lab:
    def __init__(self, c: dict, app):
        self.c, self.app = c, app
        self.m, self.state, self.v, self.x = app.market, app.state, app.validator, app.executor
        self.enabled = bool(c.get("lab_mode"))
        self.done: set = set()              # (analysis id, idea id) handled
        self.logged: dict = {}              # throttled log keys -> time
        self.killed_logged = 0.0
        self.rechecked: set = set()         # 1.11.1: (analysis id, ticket) already put back to Astra
        self.remap_asked: set = set()       # 1.11.2: tickets for which an early map was requested

    # ------------------------------------------------------------------ hard checks
    def _log(self, key: str, msg: str, every: float = 900):
        if time.time() - self.logged.get(key, 0) >= every:
            LOG.info(msg)
            self.logged[key] = time.time()

    def open_risk(self, si, positions: list, pendings: list) -> float:
        """USD at risk in the lab's working orders, from the broker's own stops (1.10.5)."""
        per_unit = si.trade_tick_value / si.trade_tick_size if si.trade_tick_size else 0.0
        total = 0.0
        for x in positions + pendings:
            sl = float(getattr(x, "sl", 0) or 0)
            ref = float(getattr(x, "price_open", 0) or 0)
            if sl and ref:
                total += abs(ref - sl) * float(x.volume_current if hasattr(x, "volume_current") else x.volume) * per_unit
        return total

    def size(self, si, stop: float) -> tuple[float, float, str | None]:
        """(lots, risk_money, hold_reason). lab_risk_pct 0 -> the minimum lot. Otherwise equity x pct / stop like the
        live track, floored to the lot step, never under the minimum lot, and refused (hold) when the lab's working
        orders already risk lab_max_total_risk_pct of equity."""
        per_unit = si.trade_tick_value / si.trade_tick_size if si.trade_tick_size else 0.0
        step, vmin = float(si.volume_step), float(si.volume_min)
        pct = float(self.c.get("lab_risk_pct") or 0)
        if pct <= 0 or stop <= 0 or per_unit <= 0:
            lots = float(self.c["lab_lots"]) or vmin
            lots = max(vmin, round(lots / step) * step)
            return lots, round(lots * stop * per_unit, 2), None
        acc = self.m.account()
        equity = float(acc.equity)
        budget = equity * pct / 100
        lots = max(vmin, math.floor(budget / (stop * per_unit) / step + 1e-9) * step)
        lots = round(lots, 2)
        risk = round(lots * stop * per_unit, 2)
        positions, pendings = self.lab_book()
        cap = equity * float(self.c["lab_max_total_risk_pct"]) / 100
        have = self.open_risk(si, positions, pendings)
        if have + risk > cap + 1e-6:
            return lots, risk, (f"lab open risk {have:.2f} + {risk:.2f} would pass the cap {cap:.2f} "
                                f"({self.c['lab_max_total_risk_pct']}% of equity)")
        return lots, risk, None

    def lab_book(self) -> tuple[list, list]:
        mine = lambda xs: [x for x in (xs or []) if x.magic == self.c["magic_lab"]]
        return (mine(self.m.trade_call("positions_get", symbol=self.m.symbol)),
                mine(self.m.trade_call("orders_get", symbol=self.m.symbol)))

    def realised_since(self, since: datetime) -> float:
        deals = self.m.history_deals(since - timedelta(days=1), datetime.now(UTC) + timedelta(days=1))
        out_entries = (self.m.const("DEAL_ENTRY_OUT"), self.m.const("DEAL_ENTRY_INOUT"))
        total = 0.0
        for d in deals:
            if d.magic != self.c["magic_lab"] or d.symbol != self.m.symbol or d.entry not in out_entries:
                continue
            if self.m.server_to_utc(d.time) >= since.timestamp():
                total += d.profit + d.commission + d.swap
        return total

    def hard_block(self, now: datetime, check_book: bool = True, check_day_cap: bool = True) -> str | None:
        """Why no lab order may be placed right now (None = may). Transient reasons are retried.
        check_book=False (1.10.3, the news straddle): the straddle has its own order count."""
        if (self.c["state_path"] / "STOP").exists():
            return "kill file STOP present"
        if not self.c["require_demo"] or not self.m.is_demo():
            return "not a DEMO account: the lab never trades real money"
        term = self.m.terminal()
        if term is None or not getattr(term, "connected", False) or not getattr(term, "trade_allowed", False):
            return "terminal not connected or Algo Trading off"
        if not self.m.sessions.is_open(now):
            return "market closed"
        q = self.m.quote()
        if q["age"] > self.c["v6"]["quote_max_age_seconds"]:
            return f"price feed stale ({q['age']:.0f}s)"
        if q["spread"] > self.c["v6"]["broker_max_spread"]:
            return f"spread {q['spread']:.2f} > broker_max_spread (a bad fill tests the broker, not the rule)"
        positions, pendings = self.lab_book()
        if check_book and len(positions) + len(pendings) >= self.c["lab_max_open"]:
            return f"lab book full: {len(positions)} open + {len(pendings)} pending (lab_max_open {self.c['lab_max_open']})"
        if check_day_cap and self.state.lab_orders_since(_day0(now)) >= self.c["lab_max_per_day"]:
            return "lab_max_per_day reached"
        acc = self.m.account()
        if acc is None:
            return "account info unavailable"
        try:
            week = self.realised_since(_week0(now))
        except Exception as exc:  # noqa: BLE001
            return f"lab drawdown unverifiable ({str(exc)[:100]})"
        if week <= -acc.balance * self.c["lab_drawdown_kill_pct"] / 100:
            return f"lab drawdown kill: {week:.2f} this week (>= {self.c['lab_drawdown_kill_pct']}% of balance)"
        return None

    def live_answers(self, now: datetime) -> dict:
        """The account-level live gates as facts for the tags (never raises)."""
        out = {}
        try:
            out["window"] = self.m.trading_window(now)
            out["news"] = self.m.news_block(now) or (getattr(self.m, "news_window", lambda n: None)(now)
                                                      if getattr(self.m, "news_strict", False) else None)   # 1.17: still tagged
            positions, pendings = self.v.broker_orders()
            out["book_full"] = len(positions) + len(pendings) >= self.v.max_active()
            out["daily_cap"] = self.state.orders_created_since(_day0(now)) >= self.c["max_trades_per_day"]
            acc = self.m.account()
            day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
            try:
                daily, weekly = self.v.realised_pnl_since(day0), self.v.realised_pnl_since(_week0(now))
                out["loss_limit"] = acc is not None and (
                    daily <= -acc.balance * self.c["daily_loss_limit_pct"] / 100
                    or weekly <= -acc.balance * self.c["weekly_loss_limit_pct"] / 100)
            except Exception:  # noqa: BLE001
                out["loss_limit"] = True
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Lab: live gate facts unavailable (%s); tags will be partial", str(exc)[:120])
        return out

    # ------------------------------------------------------------------ hedges (1.13)
    def hedge_with_working(self, order: dict, macro: str | None) -> str | None:
        """A new lab order that would hedge a WORKING lab order: skipped, unless it follows the Daily/4H macro and
        the working one is still a PENDING limit (then that limit is cancelled and the new order goes ahead). An
        OPEN position is never closed for this: the new order is skipped instead."""
        g = geometry(order)
        want = {"BULLISH": "BUY", "BEARISH": "SELL"}.get(macro or "")
        for rec in self.state.active_orders():
            if not rec.get("lab") or str(rec.get("setup_id", "")).startswith("SCP-"):    # scalps are their own sample
                continue
            if not opposite_overlap(g, {"direction": rec["direction"], "entry": rec["entry"], "sl": rec["sl"], "tp1": rec["tp1"]}):
                continue
            if want == g["direction"] and rec.get("status") == "PENDING":
                if self.x.cancel(rec["ticket"], f"hedge: {order['setup_id']} follows the Daily/4H macro {macro}"):
                    LOG.info("Lab: %s (ticket %s) cancelled: it would hedge %s, which follows the macro %s",
                             rec["setup_id"], rec["ticket"], order["setup_id"], macro)
                    continue
            return (f"would hedge working lab {rec['setup_id']} (ticket {rec['ticket']}, {rec.get('status')}); "
                    f"Daily/4H macro {macro}")
        return None

    def retire_plain_limits(self) -> list:
        """1.14, once per start: with lab_entry recheck, blind lab limits still resting at the broker (LAB-...) are
        cancelled; their ideas are already watched by the LTF engine. Open positions are never touched."""
        if self.c.get("lab_entry", "plain") != "recheck" or getattr(self, "_retired", False):
            return []
        self._retired = True
        out = []
        for rec in self.state.active_orders():
            if rec.get("lab") and rec.get("status") == "PENDING" and str(rec.get("setup_id", "")).startswith("LAB-"):
                if self.x.cancel(rec["ticket"], "lab_entry recheck: blind limits retired; the zone is watched instead",
                                 status="CANCELLED"):
                    out.append(rec["ticket"])
                    LOG.info("Lab: %s (ticket %s) cancelled: blind limit retired (lab_entry recheck)", rec["setup_id"],
                             rec["ticket"])
        return out

    def hedge_plan(self, a: dict, ideas: list, now: datetime) -> dict:
        """Pre-pass over one analysis: the orders its ideas would produce right now (unconfirmed geometry), so two
        opposite ideas that hedge each other are settled BEFORE either is sent. Returns {idea id: reason}."""
        try:
            quote, si = self.m.quote(), self.m.symbol_info()
            state, _ = self.app.market_state()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Lab: hedge pre-pass skipped (%s)", str(exc)[:120])
            return {}
        cands = []
        for idea in ideas:
            if not idea.get("targets") or (a.get("id"), idea.get("id")) in self.done:
                continue
            try:
                order, _ = plan(idea, quote, self.c, now, None, si)
            except Exception:  # noqa: BLE001
                order = None
            if order is not None:
                cands.append((idea.get("id"), geometry(order)))
        return hedge_skips(cands, state.get("macro_trend"))

    # ------------------------------------------------------------------ the sweep
    def sweep(self, a: dict | None, ideas: list, now: datetime, live_results: dict | None = None):
        """Consider every idea the live track did not trade this tick. `live_results` maps an idea id to the
        live outcome string: ideas with ': sent #' are the live track's trades and are left alone."""
        if not self.enabled or not a or not ideas:
            return
        live_results = live_results or {}
        skips = self.hedge_plan(a, ideas, now)
        for idea in ideas:
            key = (a.get("id"), idea.get("id"))
            if key in self.done or not idea.get("targets"):
                continue
            res = live_results.get(idea.get("id"), "")
            if ": sent #" in res or ": dry run" in res:
                self.done.add(key)                               # the live track took it: that is the clean sample
                continue
            try:
                self.consider(a, idea, now, risk_engine=res if res and ": gate:" not in res else None, ideas=ideas,
                              hedge=skips.get(idea.get("id")))
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Lab: idea %s skipped (%s)", idea.get("id"), GM.safe_error(exc)[:160])

    def consider(self, a: dict, idea: dict, now: datetime, risk_engine: str | None = None, ideas: list | None = None,
                 hedge: str | None = None):
        key = (a.get("id"), idea.get("id"))
        if hedge:
            self.done.add(key)
            LOG.info("Lab: idea %s %s not taken: %s", idea.get("id"), idea["direction"], hedge)
            return
        block = self.hard_block(now)
        if block:
            if block.startswith("lab drawdown kill") or block.startswith("not a DEMO"):
                if time.time() - self.killed_logged > 1800:
                    alerts.error("LAB stopped: " + block)
                    self.killed_logged = time.time()
            else:
                self._log("hard:" + block[:40], f"Lab: holding ideas: {block}")
            return
        evidence, atr15 = self.app.market_context()
        state, snap = self.app.market_state()
        quote = self.m.quote()
        born = GM.stamp(a["created_at"]) if a.get("created_at") else now - timedelta(hours=1)
        status, bar, how = BR.confirmation(idea, self.app.recent_bars(), born, evidence)
        if status == "waiting":
            sess = self.app.session_confirmation(idea, born)
            if sess:
                status, (bar, how) = "confirmed", sess
        if status == "invalidated":
            self.done.add(key)
            LOG.info("Lab: idea %s %s dropped: stop %s traded before any confirmation", idea.get("id"), idea["direction"],
                     idea["invalidation"])
            return
        confirmed = how if status == "confirmed" else None
        if not confirmed and not self.c["lab_unconfirmed_entry"]:
            return                                               # keep watching for a confirmation
        veto = self.c["block_on_mtf_opposition"]
        strong = bool(veto and EV.mtf_opposed(idea["direction"], evidence)
                      and BR.strong_zone(idea, self.app.memory_zones(), self.c) is not None)
        dec = PB.decide(idea["direction"], state, self.app.recent_bars(), strong_zone=strong, confirmation=confirmed,
                        modes=self.app.modes(), expansion_mode=self.c["expansion_filter"], levels=snap.get("levels"),
                        veto=veto)
        live = self.live_answers(now)
        if risk_engine:
            live["risk_engine"] = risk_engine
        tags = tags_for(idea, c=self.c, state=state, snap=snap, evidence=evidence, atr15=atr15, quote=quote,
                        strong=strong, dec=dec, confirmed=confirmed, live=live)
        si = self.m.symbol_info()
        order, why = plan(idea, quote, self.c, now, confirmed, si)
        if order is not None and order.pop("_tp_passed_at_entry", False) and "tp_passed_at_entry" not in tags:
            tags.append("tp_passed_at_entry")
        if order is not None and tp_beyond_opposite(order, ideas) and "tp_beyond_opposite" not in tags:
            tags.append("tp_beyond_opposite")
        if order is None:
            self._log(f"plan:{key}:{why[:30]}", f"Lab: idea {idea.get('id')} {idea['direction']} not executable: {why}")
            if "already" in why or "wrong sides" in why:
                self.done.add(key)
            return
        if self.state.lab_active_for_setup(order["setup_id"]):
            self.done.add(key)
            LOG.info("Lab: %s already has a working lab order (one sample at a time)", order["setup_id"])
            return
        if self.state.setup_orders_since(order["setup_id"], _trading_day0(now), lab=True):
            self.done.add(key)
            LOG.info("Lab: %s already has a lab order this trading day (one sample per setup per day)", order["setup_id"])
            return
        why = self.hedge_with_working(order, state.get("macro_trend"))
        if why:
            self.done.add(key)
            LOG.info("Lab: %s not taken: %s", order["setup_id"], why)
            return
        dup = duplicate_of(order, self.state.active_orders(), self.c["duplicate_atr"] * (atr15 or 0.0))
        if dup is not None:
            self.done.add(key)
            LOG.info("Lab: %s is the same order as working lab %s (ticket %s, %s @ %.2f sl %.2f); one sample at a time",
                     order["setup_id"], dup["setup_id"], dup["ticket"], dup["direction"], dup["entry"], dup["sl"])
            return
        if self.c.get("lab_entry", "plain") == "recheck":
            # 1.14: no blind resting limit. The idea becomes a watched zone; the LTF engine enters only when price
            # is there, the 5M structure breaks its way, and the recheck passes (news, one per zone, Astra's map).
            self.done.add(key)
            ltf = getattr(self.app, "ltf", None)
            if ltf is None or not ltf.live:
                self._log(f"noltf:{key}", f"Lab: idea {idea.get('id')} not watched: lab_entry recheck needs ltf_lab.mode lab")
                return
            ltf.watch(a, idea, order, tags, now)
            LOG.info("Lab: idea %s %s %s-%s -> watched zone (entry at the touch after a 5M CHoCH and the recheck) | %s",
                     idea.get("id"), idea["direction"], idea["entry_low"], idea["entry_high"], ",".join(tags) or "no tags")
            return
        tier = TR.grade(idea, idea["direction"], dec, state, snap.get("session"), self.c)
        lots, risk_money, hold = self.size(si, abs(order["entry_price"] - order["stop_loss"]))
        if hold:
            self._log("size:" + order["setup_id"], f"Lab: holding {order['setup_id']}: {hold}", every=300)
            return
        meta = dict(self.app.meta(idea["direction"], dec, confirmed or "NONE", idea), lab=1, violations=tags,
                    tier=tier.get("tier"), tier_facts=tier.get("facts"), tier_reason=tier.get("reason"),
                    user_plan=idea.get("user_plan"))
        order["strategy"], order["meta"] = ("LAB_PY" if idea.get("source") == "python" else "LAB"), meta   # 1.20
        order["spread"], order["atr15"] = quote["spread"], round(atr15, 2)
        if risk_engine:
            order["rationale"] = (order["rationale"] + f" | live risk engine: {risk_engine}")[:500]
        try:
            ticket = self.x.send(order, lots, risk_money, "B_TRADE", magic=self.c["magic_lab"])
        except NW.NewsHold as h:                                     # 1.17: retried at the next sweep
            self._log(f"newshold:{key}", f"Lab: {order['setup_id']} held: {h}", every=900)
            return
        self.done.add(key)
        ltf = getattr(self.app, "ltf", None)
        if ltf is not None and ticket:
            try:
                ltf.watch(a, idea, order, tags, now)                 # 1.13: the paired 5M-entry sample
            except Exception as exc:  # noqa: BLE001
                LOG.warning("LTF watch not registered for %s: %s", order["setup_id"], str(exc)[:120])
        LOG.info("LAB ORDER %s ticket=%s %s %s @ %.2f sl %.2f tp1 %.2f | %.2f lots risk %.2f | violations %s",
                 "SIMULATED" if self.x.dry_run else "SENT", ticket, order["setup_id"], order["order_type"],
                 order["entry_price"], order["stop_loss"], order["take_profit_1"], lots, risk_money,
                 ",".join(tags) or "none")
        alerts.trade(f"🧪 LAB {order['direction']} {order['setup_id']} {order['order_type']} @ {order['entry_price']} | "
                     f"SL {order['stop_loss']} | TP1 {order['take_profit_1']} | R:R {order['rr']} | {lots} lots\n"
                     f"violations: {', '.join(tags) or 'none (clean)'}")

    # ------------------------------------------------------------------ 1.11.1: TP1 touched before the fill
    def recheck(self, now: datetime) -> list:
        """A lab limit whose TP1 traded before it filled is kept working (lab_keep_after_tp_touch) and put back to Astra:
        at every NEW analysis made after the touch, the order stays only while Astra still proposes a same-direction
        idea whose entry zone contains the order's entry (± confirm_entry_tolerance and duplicate_atr x ATR15).
        No extra Claude call: Astra's next scheduled map is the question. Returns the tickets cancelled."""
        if not self.enabled:
            return []
        retired = self.retire_plain_limits()
        if not self.c.get("lab_keep_after_tp_touch"):
            return retired
        if self.c.get("lab_tp_touch_remap"):                     # 1.11.2: ask Astra NOW instead of waiting
            for rec in self.state.active_orders():
                if rec.get("lab") and rec.get("status") == "PENDING" and rec.get("tp_touched_utc") \
                        and rec["ticket"] not in self.remap_asked:
                    self.remap_asked.add(rec["ticket"])
                    reason = (f"{rec['setup_id']} {rec['direction']} @ {rec['entry']}: TP1 {rec['tp1']} traded before the "
                              f"fill; does Astra still propose it?")
                    if self.app.bridge.request_remap(f"REMAP-{rec['ticket']}", reason, now):
                        LOG.info("Lab: asked the signal bot for an early Astra map: %s", reason)
        a, age = self.app.bridge.analysis(now)
        if a is None or age is None or age > self.c["idea_max_age_minutes"]:
            return retired                                       # no fresh answer from Astra yet: keep waiting
        born = GM.stamp(a["created_at"])
        out = list(retired)
        for rec in self.state.active_orders():
            if not rec.get("lab") or rec.get("status") != "PENDING" or not rec.get("tp_touched_utc"):
                continue
            key = (a["id"], rec["ticket"])
            if key in self.rechecked:
                continue
            touched = GM.stamp(rec["tp_touched_utc"])
            if born <= touched:
                continue                                         # this map was made before the touch
            self.rechecked.add(key)
            _, atr15 = self.app.market_context()
            tol = max(float(self.c.get("confirm_entry_tolerance", 0.0) or 0.0), self.c["duplicate_atr"] * (atr15 or 0.0))
            if self.c.get("lab_drop_rule", "contradicted") == "contradicted":
                # 1.13.1: the order lives until its expiry, its stop, or a map that proposes the OPPOSITE trade there
                against = contradicted(rec, a.get("ideas") or [], tol)
                if against is None:
                    LOG.info("Lab: %s (ticket %s) kept: Astra's %s map does not contradict it (lab_drop_rule contradicted)",
                             rec["setup_id"], rec["ticket"], a["created_at"][11:16])
                    continue
                if self.x.cancel(rec["ticket"], f"TP1 touched before the fill and Astra's {a['created_at'][11:16]} map "
                                                f"proposes the opposite ({against.get('id')} {against['direction']} "
                                                f"{against['entry_low']}-{against['entry_high']})", status="CANCELLED"):
                    out.append(rec["ticket"])
                    alerts.trade(f"🧪 LAB {rec['direction']} {rec['setup_id']} cancelled: Astra's next map proposes the "
                                 f"opposite trade at that zone ({against['direction']} {against['entry_low']}-"
                                 f"{against['entry_high']})", short=None)
                continue
            match = still_proposed(rec, a.get("ideas") or [], tol)
            if match is not None:
                LOG.info("Lab: %s (ticket %s) kept after the TP1 touch: Astra's %s map still proposes %s %s %s-%s",
                         rec["setup_id"], rec["ticket"], a["created_at"][11:16], match.get("id"), match["direction"],
                         match["entry_low"], match["entry_high"])
                continue
            if self.x.cancel(rec["ticket"], f"TP1 touched before the fill and Astra's {a['created_at'][11:16]} map no "
                                            "longer proposes it", status="CANCELLED"):
                out.append(rec["ticket"])
                alerts.trade(f"🧪 LAB {rec['direction']} {rec['setup_id']} cancelled: TP1 traded before the fill and "
                             f"Astra's next map no longer proposes it", short=None)
        return out


# ---------------------------------------------------------------------- report helpers
def violations_of(rec: dict) -> list[str]:
    try:
        v = json.loads(rec.get("violations") or "[]")
        return [str(x) for x in v] if isinstance(v, list) else []
    except ValueError:
        return []


def per_rule(closed: list) -> list[str]:
    """Report lines: for each rule, the lab trades that broke it vs the lab trades that kept it (R only)."""
    lab = [t for t in closed if t.get("lab")]
    if not lab:
        return []
    out = ["", f"=== LAB (1.10) === {len(lab)} closed lab trades (minimum lot; judge R, never money)"]
    clean = [t for t in lab if not t["violations"]]
    rr = lambda ts: (sum(t["r"] for t in ts) / len(ts)) if ts else 0.0
    out.append(f"  clean (no violation)        {len(clean):>3} | avg {rr(clean):+.2f}R | total {sum(t['r'] for t in clean):+.2f}R")
    for tag in TAGS:
        broke = [t for t in lab if tag in t["violations"]]
        if not broke:
            continue
        kept = [t for t in lab if tag not in t["violations"]]
        out.append(f"  {tag:<26} broke {len(broke):>3} avg {rr(broke):+.2f}R | kept {len(kept):>3} avg {rr(kept):+.2f}R"
                   f" | diff {rr(kept) - rr(broke):+.2f}R{'  (< 20 samples)' if len(broke) < 20 else ''}")
    combos: dict = {}
    for t in lab:
        combos.setdefault(",".join(sorted(t["violations"])) or "clean", []).append(t["r"])
    out.append("  most common violation sets:")
    for k, rs in sorted(combos.items(), key=lambda kv: -len(kv[1]))[:8]:
        out.append(f"    {k:<48} {len(rs):>3} | avg {sum(rs) / len(rs):+.2f}R")
    out.append("  A rule earns its place when 'diff' is clearly positive over 20-30 samples. Rules overlap: read the sets.")
    return out
