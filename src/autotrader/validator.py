"""
Hard validation and position sizing. Pure rules, no AI. An order reaches the broker only if
every check passes. Spread, quote-age, stop-fraction and minimum-stop limits come from the V6
config, so the trader is never looser than the signal bot.
"""

from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timedelta, timezone

import gold_evidence as EV

from . import sessions as SS
from .state import ENGINE_SETUP_PREFIXES as ENGINE_PREFIXES

UTC = timezone.utc
LOG = logging.getLogger("autotrader.validator")
PENDING = ("BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP")
ORDER_TYPES = PENDING + ("MARKET",)
CITABLE = set(EV.STRUCTURE_KINDS) | set(EV.ZONE_KINDS)          # RSI divergence is context only


class Rejected(Exception):
    pass



def _num(x, name, allow_none=False):
    if x is None and allow_none:
        return None
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        raise Rejected(f"{name} is not a finite number: {x!r}")
    return float(x)


def _utc(ts, name) -> datetime:
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        raise Rejected(f"{name} is not an ISO-8601 time: {ts!r}")
    if d.tzinfo is None:
        raise Rejected(f"{name} has no timezone")
    return d.astimezone(UTC)


class Validator:
    def __init__(self, c, market, state):
        self.c, self.v6, self.m, self.state = c, c["v6"], market, state

    # ---------------------------------------------------------------- account-level gates
    def gates(self, now: datetime, sessions: bool = True, own_magic: int | None = None) -> str | None:
        """Why no new order may be placed right now, or None. `sessions=False` (1.11 overnight engine) skips the
        trade_sessions preference. `own_magic` (1.11.4, the overnight engine) gives an engine its own single slot:
        only its own positions/orders count against it (on a netting account every position and order in the
        symbol counts), and the swing engine's max_trades_per_day does not apply (the engine has its own one-a-day
        rule). Every safety gate below (kill file, demo, terminal, window, news, quote age, loss limits) still applies."""
        kill = self.c["state_path"] / "STOP"
        if kill.exists():
            return f"kill file {kill.name} present"
        if self.c["require_demo"] and not self.m.is_demo():
            return "account is not DEMO (require_demo is on)"
        allowed = (self.c.get("trade_sessions") or []) if sessions else []
        if allowed:
            session = SS.session_of(now, self.c.get("sessions"))
            if session not in allowed:
                return f"session {session} is not in trade_sessions {allowed}"
        term = self.m.terminal()
        if term is None or not getattr(term, "connected", False):
            return "terminal not connected to the broker"
        if not getattr(term, "trade_allowed", False):
            return "Algo Trading is OFF in the demo terminal"
        window = self.m.trading_window(now)
        if window:
            return window
        news = self.m.news_block(now)
        if news:
            return "news lock: " + news
        q = self.m.quote()
        if q["age"] > self.v6["quote_max_age_seconds"]:
            return f"price feed stale ({q['age']:.0f}s)"
        day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if own_magic is None:
            positions, pendings = self.broker_orders()
            if len(positions) + len(pendings) >= self.max_active():
                return f"{len(positions)} position(s) + {len(pendings)} pending order(s) already working (max {self.max_active()})"
            if self.state.orders_created_since(day0.strftime("%Y-%m-%dT%H:%M:%SZ")) >= self.c["max_trades_per_day"]:
                return "max trades per day reached"
        else:
            if self.netting():
                positions, pendings = self.symbol_orders()
                if positions or pendings:
                    return (f"netting account: {len(positions)} position(s) + {len(pendings)} pending order(s) in "
                            f"{self.m.symbol} already working")
            positions, pendings = self.broker_orders(own_magic)
            if positions or pendings:
                return f"own slot busy: {len(positions)} position(s) + {len(pendings)} pending order(s) (magic {own_magic}, max 1)"
        acc = self.m.account()
        if acc is None:
            return "account info unavailable"
        try:
            daily = self.realised_pnl_since(day0)
        except Exception as exc:  # noqa: BLE001  - never assume "no losses" when history is unreadable
            return f"loss limits unverifiable ({str(exc)[:120]})"
        if daily <= -acc.balance * self.c["daily_loss_limit_pct"] / 100:
            return f"daily loss limit hit ({daily:.2f})"
        week0 = day0 - timedelta(days=day0.weekday())
        try:
            weekly = self.realised_pnl_since(week0)
        except Exception as exc:  # noqa: BLE001
            return f"loss limits unverifiable ({str(exc)[:120]})"
        if weekly <= -acc.balance * self.c["weekly_loss_limit_pct"] / 100:
            return f"weekly loss limit hit ({weekly:.2f})"
        return None

    def broker_orders(self, magic: int | None = None):
        want = self.c["magic"] if magic is None else magic
        mine = lambda xs: [x for x in (xs or []) if x.magic == want]
        return (mine(self.m.trade_call("positions_get", symbol=self.m.symbol)),
                mine(self.m.trade_call("orders_get", symbol=self.m.symbol)))

    def symbol_orders(self):
        """Every position and pending order in the symbol, whatever the magic (manual trades included)."""
        return (list(self.m.trade_call("positions_get", symbol=self.m.symbol) or []),
                list(self.m.trade_call("orders_get", symbol=self.m.symbol) or []))

    def netting(self) -> bool:
        """Netting accounts merge positions in one symbol, so a second order would change the first."""
        acc = self.m.account()
        modes = set()
        for name in ("ACCOUNT_MARGIN_MODE_RETAIL_NETTING", "ACCOUNT_MARGIN_MODE_EXCHANGE"):
            try:
                modes.add(self.m.const(name))
            except AttributeError:
                pass
        return acc is not None and getattr(acc, "margin_mode", None) in modes

    def max_active(self) -> int:
        return 1 if self.netting() else self.c["max_active_orders"]

    def lab_magics(self) -> set:
        """1.11.5: the lab's magic number when the lab is on (its trades stay out of the live risk total)."""
        return {self.c.get("magic_lab", 40003)} if self.c.get("lab_mode") else set()

    def loss_magics(self) -> set:
        """Magic numbers whose realised P/L counts toward the daily/weekly loss limits: the swing engine and, when it
        is live, the 1.11 overnight engine. The lab (magic_lab) keeps its own limits."""
        out = {self.c["magic"]}
        ov = self.c.get("overnight") or {}
        if ov.get("mode") == "live":
            out.add(int(ov.get("magic", 40004)))
        return out

    def realised_pnl_since(self, since: datetime) -> float:
        """Closed P/L of this bot since `since` (UTC). MT5 deal times are server time."""
        deals = self.m.history_deals(since - timedelta(days=1), datetime.now(UTC) + timedelta(days=1))
        out_entries = (self.m.const("DEAL_ENTRY_OUT"), self.m.const("DEAL_ENTRY_INOUT"))
        total = 0.0
        magics = self.loss_magics()
        for d in deals:
            if d.magic not in magics or d.symbol != self.m.symbol or d.entry not in out_entries:
                continue
            if self.m.server_to_utc(d.time) >= since.timestamp():
                total += d.profit + d.commission + d.swap
        return total

    # ---------------------------------------------------------------- direction rules (1.6.1)
    def _const(self, name: str, default):
        """MT5 constant, or the documented value when the module (or a test fake) lacks it."""
        try:
            return self.m.const(name)
        except AttributeError:
            return default

    def same_direction_working(self, direction: str) -> int:
        """Our RISKY positions and pending orders (broker records, this magic) in `direction`. A position
        whose stop is at breakeven or better (locked, e.g. after TP1) carries no risk and does not count."""
        positions, pendings = self.broker_orders()
        pos_buy = self._const("POSITION_TYPE_BUY", 0)
        buy_types = {self._const(n, None) for n in ("ORDER_TYPE_BUY_LIMIT", "ORDER_TYPE_BUY_STOP",
                                                     "ORDER_TYPE_BUY_STOP_LIMIT")} - {None}
        n = 0
        for p in positions:
            is_buy = getattr(p, "type", None) == pos_buy
            if is_buy != (direction == "BUY"):
                continue
            sl, price = getattr(p, "sl", 0.0) or 0.0, getattr(p, "price_open", 0.0) or 0.0
            risk_free = sl and ((sl >= price) if is_buy else (sl <= price))
            if not risk_free:
                n += 1
        n += sum(1 for o in pendings if (getattr(o, "type", None) in buy_types) == (direction == "BUY"))
        return n

    def check_direction_rules(self, direction: str, now: datetime):
        """1.7: no pyramiding into one direction. On 15 Oct three SELL limits were stacked into one rally
        within 48 minutes and all stopped. A breakeven position frees the slot."""
        cap = self.c["max_same_direction_positions"]
        working = self.same_direction_working(direction)
        if working >= cap:
            raise Rejected(f"{working} {direction} position(s)/order(s) at risk already working (max {cap} per direction)")

    # ---------------------------------------------------------------- the order itself
    def check_levels(self, order: dict, atr15: float, now: datetime) -> dict:
        """The price rules every order must pass: sides, reward/risk recomputed here,
        stop size, executable quote, pending-order side, cancel levels, expiry. Returns the
        normalised order (rounded prices, rr, spread, atr15). No book or evidence checks."""
        if not isinstance(order, dict):
            raise Rejected("order is not an object")
        setup_id = str(order.get("setup_id") or "").strip()[:60]
        if not setup_id:
            raise Rejected("missing setup_id")
        direction, otype = order.get("direction"), order.get("order_type")
        if direction not in ("BUY", "SELL"):
            raise Rejected(f"bad direction {direction!r}")
        if otype not in ORDER_TYPES:
            raise Rejected(f"bad order_type {otype!r}")
        if otype != "MARKET" and not otype.startswith(direction):
            raise Rejected(f"{otype} does not match {direction}")
        if otype == "MARKET" and not self.c["allow_market_orders"]:
            raise Rejected("market orders are disabled")

        entry = _num(order.get("entry_price"), "entry_price")
        sl = _num(order.get("stop_loss"), "stop_loss")
        tp1 = _num(order.get("take_profit_1"), "take_profit_1")
        tp2 = _num(order.get("take_profit_2"), "take_profit_2", True)
        dev = min(max(_num(order.get("max_entry_deviation", 0.5), "max_entry_deviation"), 0.0),
                  self.c["max_entry_deviation_cap"])
        c_above = _num(order.get("cancel_if_price_above"), "cancel_if_price_above", True)
        c_below = _num(order.get("cancel_if_price_below"), "cancel_if_price_below", True)
        pct = _num(order.get("tp1_close_percent", 100), "tp1_close_percent")
        if not 1 <= pct <= 100:
            raise Rejected("tp1_close_percent must be 1..100")
        if tp2 is None:
            pct = 100.0

        # --- sides
        if direction == "BUY":
            if not sl < entry < tp1:
                raise Rejected("BUY needs stop < entry < TP1")
            if tp2 is not None and tp2 <= tp1:
                raise Rejected("BUY TP2 must be above TP1")
        else:
            if not tp1 < entry < sl:
                raise Rejected("SELL needs TP1 < entry < stop")
            if tp2 is not None and tp2 >= tp1:
                raise Rejected("SELL TP2 must be below TP1")

        # --- reward/risk recomputed here
        risk, reward = abs(entry - sl), abs(tp1 - entry)
        rr = reward / risk
        if rr < self.c["min_rr"]:
            raise Rejected(f"R:R {rr:.2f} < {self.c['min_rr']}")

        # --- stop size: ATR band, V6 minimum stop, broker stop level
        if not self.c["sl_atr_min"] * atr15 <= risk <= self.c["sl_atr_max"] * atr15:
            raise Rejected(f"stop {risk:.2f} outside {self.c['sl_atr_min']}-{self.c['sl_atr_max']} x ATR15 ({atr15:.2f})")
        if risk < float(self.v6["minimum_stop_distance"]):
            raise Rejected(f"stop {risk:.2f} below V6 minimum_stop_distance")
        si = self.m.symbol_info()
        if si is None:
            raise Rejected("symbol info unavailable")
        min_dist = si.trade_stops_level * si.point
        if risk < min_dist or reward < min_dist:
            raise Rejected(f"stop/target inside the broker stop level ({min_dist})")

        # --- executable quote (V6 rules)
        q = self.m.quote()
        if q["age"] > self.v6["broker_quote_max_age_seconds"]:
            raise Rejected(f"broker quote {q['age']:.0f}s old")
        if q["spread"] > self.v6["broker_max_spread"]:
            raise Rejected(f"spread {q['spread']:.2f} > {self.v6['broker_max_spread']}")
        if q["spread"] > self.v6["broker_max_spread_stop_fraction"] * risk:
            raise Rejected(f"spread {q['spread']:.2f} is more than "
                           f"{self.v6['broker_max_spread_stop_fraction']:.0%} of the stop")
        cost = q["spread"] + self.v6["broker_slippage_allowance"]
        if cost > self.v6["broker_max_combined_stop_fraction"] * risk:
            raise Rejected(f"spread+slippage {cost:.2f} is more than "
                           f"{self.v6['broker_max_combined_stop_fraction']:.1%} of the stop")
        bid, ask = q["bid"], q["ask"]
        if otype == "BUY_LIMIT" and not entry < ask - min_dist:
            raise Rejected("BUY_LIMIT must be below the ask")
        if otype == "SELL_LIMIT" and not entry > bid + min_dist:
            raise Rejected("SELL_LIMIT must be above the bid")
        if otype == "BUY_STOP" and not entry > ask + min_dist:
            raise Rejected("BUY_STOP must be above the ask")
        if otype == "SELL_STOP" and not entry < bid - min_dist:
            raise Rejected("SELL_STOP must be below the bid")
        if otype == "MARKET":
            ref = ask if direction == "BUY" else bid
            if abs(ref - entry) > dev:
                raise Rejected(f"price {ref} is {abs(ref - entry):.2f} from entry {entry} (max {dev})")
        if direction == "BUY" and (bid <= sl or ask >= tp1):
            raise Rejected("price is already beyond the stop or TP1")
        if direction == "SELL" and (ask >= sl or bid <= tp1):
            raise Rejected("price is already beyond the stop or TP1")
        if c_above is not None and c_above <= ask:
            raise Rejected("cancel_if_price_above is already breached")
        if c_below is not None and c_below >= bid:
            raise Rejected("cancel_if_price_below is already breached")

        # --- expiry
        expiry = _utc(order.get("order_expiry_utc"), "order_expiry_utc")
        if expiry < now + timedelta(minutes=self.c["min_order_lifetime_minutes"]):
            raise Rejected("order expiry is too soon")
        if expiry > now + timedelta(hours=self.c["max_order_lifetime_hours"]):
            raise Rejected("order expiry is too far away")

        d = si.digits
        return {"setup_id": setup_id, "direction": direction, "order_type": otype,
                "entry_price": round(entry, d), "stop_loss": round(sl, d), "take_profit_1": round(tp1, d),
                "take_profit_2": round(tp2, d) if tp2 is not None else None, "tp1_close_percent": pct,
                "move_sl_to_breakeven_at_tp1": bool(order.get("move_sl_to_breakeven_at_tp1", False)),
                "max_entry_deviation": dev, "order_expiry_utc": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "cancel_if_price_above": c_above, "cancel_if_price_below": c_below,
                "evidence_ids": [str(i) for i in (order.get("evidence_ids") or [])],
                "rationale": str(order.get("rationale", ""))[:500],
                "rr": round(rr, 2), "atr15": round(atr15, 2), "spread": q["spread"]}

    def validate(self, order: dict, grade: str, evidence: dict, atr15: float, now: datetime,
                 require_evidence: bool | None = None, counter_trend: bool = False,
                 own_magic: int | None = None) -> dict:
        """Everything a LIVE order must pass: the level rules above plus this book (setup per day,
        duplicates, netting), the direction rules, Python evidence and the 4H/1H veto. With `own_magic` (1.11.4, the
        overnight engine) the swing engine's duplicate and same-direction rules do not apply (the engine's own slot
        and one-a-day rule replace them); a netting account still takes one order at a time across everything."""
        if grade not in self.c["execute_grades"]:
            raise Rejected(f"{grade} is not in execute_grades")
        if not isinstance(order, dict):
            raise Rejected("order is not an object")
        setup_id = str(order.get("setup_id") or "").strip()[:60]
        if not setup_id:
            raise Rejected("missing setup_id")
        # The same setup may be traded again (a new order) once its earlier order has finished.
        day0 = now.replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        earlier = self.state.setup_orders_since(setup_id, day0)
        if any(o["status"] in ("PENDING", "OPEN", "TP1_DONE") for o in earlier):
            raise Rejected(f"setup {setup_id} already has a working order")
        if len(earlier) >= self.c["max_orders_per_setup_per_day"]:
            raise Rejected(f"setup {setup_id} already traded {len(earlier)}x today")

        out = self.check_levels(order, atr15, now)
        direction, entry, sl = out["direction"], out["entry_price"], out["stop_loss"]

        # --- other working orders: no duplicates; netting accounts take one order at a time
        active = self.state.active_orders(include_lab=False)         # 1.10: lab orders are not duplicates of live ones
        if active and self.netting():
            raise Rejected("netting account: only one order at a time")
        if own_magic is None:
            tol = self.c["duplicate_atr"] * atr15
            for a in active:
                if a["setup_id"].startswith(ENGINE_PREFIXES):          # 1.11.4: another engine's order is not a duplicate
                    continue
                if a["direction"] == direction and abs(a["entry"] - entry) <= tol and abs(a["sl"] - sl) <= tol:
                    raise Rejected(f"duplicate of working order {a['ticket']} ({a['setup_id']} @ {a['entry']})")
            self.check_direction_rules(direction, now)

        # --- Python-verified evidence (V6 gold_evidence)
        ids = out["evidence_ids"]
        if self.c["require_evidence"] if require_evidence is None else require_evidence:
            self.check_evidence(ids, direction, evidence)
        if self.c["block_on_mtf_opposition"] and not counter_trend and EV.mtf_opposed(direction, evidence):
            raise Rejected("Python 4H/1H structure opposes (or is unknown for) this direction")

        out["strategy"] = order.get("_strategy") or ("SWING_COUNTER" if counter_trend else "SWING_TREND")
        out["meta"] = order.get("_meta") or {}
        return out

    @staticmethod
    def check_evidence(ids, direction: str, evidence: dict):
        if not isinstance(ids, list) or not ids:
            raise Rejected("order cites no Python evidence ids")
        index = (evidence or {}).get("index", {})
        backing = 0
        for i in ids:
            item = index.get(str(i))
            if item is None:
                raise Rejected(f"evidence id {i} does not exist")
            if item["kind"] not in CITABLE:
                continue                                    # context only, never sufficient
            if item.get("status") not in EV.USABLE.get(item["kind"], set()):
                raise Rejected(f"evidence {i} is {item.get('status')} (not usable)")
            if item.get("direction") not in (None, direction):
                raise Rejected(f"evidence {i} points {item.get('direction')}, order is {direction}")
            backing += 1
        if backing == 0:
            raise Rejected("no structural or zone evidence backs the order")

    # ---------------------------------------------------------------- account-wide exposure
    def open_risk(self, exclude_magics: set | None = None) -> tuple[float, str | None]:
        """Money lost if every open position and pending order on the account hit its stop, from the
        broker's own records (any symbol, any magic number, manual trades included). A stop already
        beyond the entry (locked profit / breakeven) counts 0. Returns (total, problem) where problem
        names a position or order without a stop, whose risk cannot be measured. `exclude_magics` (1.11.5): magic
        numbers left out of the total (the lab, which has its own book limits); a missing stop on ANY order still
        blocks."""
        total = 0.0
        skip = exclude_magics or set()
        buy_types = set()
        for name in ("ORDER_TYPE_BUY", "ORDER_TYPE_BUY_LIMIT", "ORDER_TYPE_BUY_STOP", "ORDER_TYPE_BUY_STOP_LIMIT"):
            try:
                buy_types.add(self.m.const(name))
            except AttributeError:
                pass
        items = [("position", p, p.price_open, p.volume) for p in (self.m.trade_call("positions_get") or [])]
        items += [("order", o, o.price_open, getattr(o, "volume_current", None) or o.volume_initial)
                  for o in (self.m.trade_call("orders_get") or [])]
        specs = {}
        for kind, x, price, volume in items:
            if not x.sl:
                return total, f"{kind} {x.ticket} {x.symbol} without a stop loss"
            if x.symbol not in specs:
                specs[x.symbol] = self.m.trade_call("symbol_info", x.symbol)
            si = specs[x.symbol]
            if si is None or si.trade_tick_size <= 0:
                return total, f"{kind} {x.ticket}: no contract data for {x.symbol}"
            if getattr(x, "magic", None) in skip:
                continue
            buy = (x.type in buy_types) if kind == "order" else (x.type == self.m.const("POSITION_TYPE_BUY"))
            dist = (price - x.sl) if buy else (x.sl - price)
            total += max(0.0, dist) * volume * si.trade_tick_value / si.trade_tick_size
        return round(total, 2), None

    # ---------------------------------------------------------------- sizing
    def lots(self, order: dict, grade: str, risk_pct: float | None = None, min_lot_ok: bool = False) -> tuple[float, float]:
        """(lots, money actually risked). Rounded DOWN to the volume step; never above risk %.
        risk_pct (1.7): the tier's risk per trade, replacing risk_percent[grade]; it can never exceed
        the configured maximum tier (tiers.A.risk_pct)."""
        acc, si = self.m.account(), self.m.symbol_info()
        if si.trade_tick_size <= 0 or si.trade_tick_value <= 0:
            raise Rejected("symbol tick size/value unavailable")
        per_unit_per_lot = si.trade_tick_value / si.trade_tick_size
        stop = abs(order["entry_price"] - order["stop_loss"])
        pct = self.c["risk_percent"][grade] if risk_pct is None else min(float(risk_pct), float(self.c["tiers"]["A"]["risk_pct"]))
        if self.c.get("news_mode", "lock") == "strict" and self.c.get("news_strict_risk_pct"):
            try:
                if self.m.news_window(datetime.now(timezone.utc)):      # 1.17: smaller size inside a news window
                    pct = min(pct, float(self.c["news_strict_risk_pct"]))
            except AttributeError:
                pass
        budget = acc.equity * pct / 100
        lots = math.floor(budget / (stop * per_unit_per_lot) / si.volume_step + 1e-9) * si.volume_step
        lots = round(min(lots, si.volume_max), 8)
        if lots < si.volume_min:
            one = si.volume_min * stop * per_unit_per_lot
            if min_lot_ok and one <= acc.equity * float(self.c["tiers"]["B"]["risk_pct"]) / 100 + 1e-6:
                lots = si.volume_min                     # 1.7 Tier C: the smallest size the broker allows
            else:
                raise Rejected(f"{pct}% risk ({budget:.2f}) is below one minimum lot ({one:.2f})")
        risk_money = round(lots * stop * per_unit_per_lot, 2)
        # Account-wide: everything the BROKER holds (this bot, other bots, manual trades), not our records. 1.11.5: the
        # lab's 0.01-lot samples are left out (they have their own book limits, lab_max_open), so test trades never
        # use up the live budget.
        running, unmeasured = self.open_risk(self.lab_magics())
        if unmeasured:
            raise Rejected(f"exposure cannot be measured ({unmeasured}); no new order until it has a stop")
        cap = acc.equity * self.c["max_total_risk_pct"] / 100
        if running + risk_money > cap + 1e-6:
            raise Rejected(f"total risk {running + risk_money:.2f} would exceed {self.c['max_total_risk_pct']}% ({cap:.2f})")
        LOG.info("Sizing %s: equity %.2f x %.2f%% = %.2f budget | stop %.2f | %.2f lots risk %.2f",
                 grade, acc.equity, pct, budget, stop, lots, risk_money)
        return lots, risk_money
