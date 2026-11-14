"""
1.11: the OVERNIGHT_TREND engine - Python only, no Astra, no API cost.

The one rule that passed every split of the two-year replay (30 Oct 2026, `clock_model.py`):
    when the Daily and 4H structure both read BULLISH, buy gold at 18:45 New York (after the daily open), exit
    by time at 08:00 New York next morning, protective stop at `stop_atr` x ATR15, no target, one trade per day,
    never on a Friday evening (the exit would fall on Saturday).
Replay, Dec 2024 - Oct 2026: 182 trades, 49.5 % wins, +0.34 R per trade, PF 1.72, max drawdown 14.7 R; positive in
all four half-years (+0.29 / +0.38 / +0.34 / +0.37 R). In a MIXED macro the same trade was -0.10 R, BEARISH ~0, so
the macro filter is the rule, not an option. Past replay results; not a promise.

Three regime rules are configurable (`overnight.rules`): BULL_LONG (the evidence), BEAR_SHORT and RANGE_SHORT (the
owner's request, 30 Oct; both measured flat-to-negative in the replay, default off). Whatever is switched on, one
order per day at most, in the direction of the rule that matches the current macro.

What this engine does NOT do: it never bypasses the trader's gates. Every order goes through `Validator.gates`
(kill file, demo account, terminal, trading window, news lock, quote age, max trades per day, loss limits),
`Validator.validate` (price rules, R:R, stop band, spread caps, duplicates, 4H/1H veto) and `Validator.lots`
(risk %, minimum lot refusal, account-wide 1 % cap). The only gate it is exempt from is `trade_sessions`, which is a
preference for the swing engine (18:45 New York is outside London/New York by definition).

Orders carry their own magic number (`overnight.magic`, 40004) and the engine has its own single slot (1.11.4): swing
positions and orders do not block it, and it does not use up the swing engine's slots, max_trades_per_day or duplicate
check (on a netting account everything in the symbol still blocks it). The account-wide 1 % risk cap still counts
every position and order, and its realised P/L counts toward the daily/weekly loss limits (validator.loss_magics).
The position is closed by the executor at `exit_utc` (a new journal column); the broker-side stop protects it in between.
"""

from __future__ import annotations

import logging
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import alerts
from . import news as NW
from . import bridge as BR
from .validator import Rejected

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
LOG = logging.getLogger("autotrader.overnight")

DEFAULTS = {
    "mode": "off",               # off | live (demo orders through the full validator)
    "magic": 40004,
    "entry_ny": "18:45",         # New York local time; the week's first entry is Sunday evening
    "window_minutes": 30,        # the entry is attempted inside [entry, entry + window)
    "exit_ny": "08:00",          # next morning, New York local time; closed at market by the executor
    "stop_atr": 3.0,             # protective stop = stop_atr x ATR15 (the replay's 3.0)
    "risk_pct": 0.25,            # per trade; never above tiers.A.risk_pct; the minimum lot is REFUSED when it risks more
    "max_spread": 0.60,          # $ at the entry; the validator's own caps apply as well
    # One rule per regime (regime.macro_trend = Daily AND 4H structure). The replay, Dec 2024 - Oct 2026, overnight
    # 18:45 -> 08:00 NY, 3 x ATR stop, per rule (the SELL figures are the mirror of the measured BUY trades, minus
    # costs; a short was not simulated separately):
    #   BULLISH  BUY   182 trades  +0.34 R   positive in all four half-years          <- the only one with evidence
    #   BEARISH  SELL  116 trades  -0.14 R   (BUY in a bearish macro was +0.08 R but flipped sign by half-year)
    #   MIXED    SELL  179 trades  -0.01 R   (BUY -0.05 R; the sign flipped every half-year: no edge either way)
    # The owner can switch any rule live for a demo test; the defaults follow the evidence.
    "rules": [
        {"name": "BULL_LONG", "macro": "BULLISH", "direction": "BUY", "mode": "live"},
        {"name": "BEAR_SHORT", "macro": "BEARISH", "direction": "SELL", "mode": "off"},
        {"name": "RANGE_SHORT", "macro": "MIXED", "direction": "SELL", "mode": "off"},
    ],
}
MACROS = ("BULLISH", "BEARISH", "MIXED", "NEUTRAL")


def _hm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def schedule(cfg: dict, now: datetime) -> tuple[datetime, datetime] | None:
    """(entry window start UTC, exit UTC) for the trading day containing `now`, when `now` is inside the entry window
    and the trade is allowed today; otherwise None. Saturday: never. Friday: never (the exit would be Saturday).
    Sunday evening: yes (the week opens 18:00 New York)."""
    ny = now.astimezone(NY)
    if ny.weekday() in (4, 5):
        return None
    start = datetime.combine(ny.date(), _hm(cfg["entry_ny"]), NY)
    if not (start <= ny < start + timedelta(minutes=cfg["window_minutes"])):
        return None
    exit_dt = datetime.combine(ny.date() + timedelta(days=1), _hm(cfg["exit_ny"]), NY)
    return start.astimezone(UTC), exit_dt.astimezone(UTC)


def setup_id(now: datetime) -> str:
    return f"OVN-{now.astimezone(NY):%Y%m%d}"


def rule_for(cfg: dict, macro: str | None) -> dict | None:
    """The live rule for this regime, or None."""
    for r in cfg.get("rules") or []:
        if r.get("mode") == "live" and r.get("macro") == macro:
            return r
    return None


def build_order(cfg: dict, rule: dict, quote: dict, atr15: float, now: datetime, exit_utc: datetime) -> dict:
    """The MARKET order: stop at stop_atr x ATR15 beyond the entry, a far technical TP1 (the exit is by time), expiry 30
    min (it is a market order; the journal needs a value), rationale for the journal and Telegram."""
    direction = rule["direction"]
    buy = direction == "BUY"
    price = quote["ask"] if buy else quote["bid"]
    risk = cfg["stop_atr"] * atr15
    stop = round(price - risk if buy else price + risk, 2)
    tp1 = round(price + 20 * atr15 if buy else price - 20 * atr15, 2)
    order = BR._order(direction, "MARKET", price, stop, [tp1], now + timedelta(minutes=30), setup_id(now), "B_TRADE",
                      f"overnight {rule['name']}: macro {rule['macro']}, {direction} {cfg['entry_ny']} -> {cfg['exit_ny']} NY, "
                      f"stop {cfg['stop_atr']} x ATR15 ({atr15:.2f}); exit by time at {exit_utc:%H:%M} UTC")
    order["move_sl_to_breakeven_at_tp1"] = False
    order["_strategy"] = "OVERNIGHT_" + rule["name"]
    order["_exit_utc"] = exit_utc
    return order


class Overnight:
    def __init__(self, c: dict, app):
        self.c = c
        self.cfg = dict(DEFAULTS, **(c.get("overnight") or {}))
        self.app = app
        self.logged: set = set()
        live = [f"{r['name']} ({r['macro']} -> {r['direction']})" for r in self.cfg.get("rules") or [] if r.get("mode") == "live"]
        LOG.info("Overnight engine %s | %s -> %s New York | stop %s x ATR15 | risk %s%% | magic %s | live rules: %s",
                 self.cfg["mode"].upper(), self.cfg["entry_ny"], self.cfg["exit_ny"], self.cfg["stop_atr"],
                 self.cfg["risk_pct"], self.cfg["magic"], ", ".join(live) or "none")

    @property
    def live(self) -> bool:
        return self.cfg["mode"] == "live"

    def _once(self, key, msg, *args):
        if key not in self.logged:
            self.logged.add(key)
            LOG.info(msg, *args)

    def tick(self, now: datetime) -> str | None:
        """Called every loop pass. Places at most one order per trading day inside the entry window."""
        if not self.live:
            return None
        sched = schedule(self.cfg, now)
        if sched is None:
            return None
        _, exit_utc = sched
        sid = setup_id(now)
        day_key = ("day", sid)
        if day_key in self.logged:
            return None
        app = self.app
        # already traded today (restart inside the window, or a refused attempt that must not be retried)?
        day0 = now.astimezone(NY).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        if app.state.setup_orders_since(sid, day0.strftime("%Y-%m-%dT%H:%M:%SZ")):
            self.logged.add(day_key)
            return None
        gate = app.validator.gates(now, sessions=False, own_magic=self.cfg["magic"])   # 1.11.4: its own slot
        if gate:
            self._once(("gate", sid, gate), "Overnight %s: not traded: %s", sid, gate)
            if "news lock" not in gate and "stale" not in gate and "terminal" not in gate:
                self.logged.add(day_key)                     # hard gates do not clear inside the window
            return None
        evidence, atr15 = app.market_context()
        state, snap = app.market_state()
        macro = state.get("macro_trend")
        rule = rule_for(self.cfg, macro)
        if rule is None:
            self.logged.add(day_key)
            live = [f"{r['macro']}->{r['direction']}" for r in self.cfg["rules"] if r.get("mode") == "live"]
            LOG.info("Overnight %s: no trade: macro is %s (Daily %s, 4H %s); live rules: %s", sid, macro,
                     state.get("trend_1day"), state.get("trend_4h"), ", ".join(live) or "none")
            return None
        direction = rule["direction"]
        quote = app.market.quote()
        if quote["spread"] > self.cfg["max_spread"]:
            self._once(("spread", sid), "Overnight %s: waiting, spread %.2f > %.2f", sid, quote["spread"], self.cfg["max_spread"])
            return None                                       # spread may narrow later in the window
        if not atr15:
            self.logged.add(day_key)
            return None
        order = build_order(self.cfg, rule, quote, atr15, now, exit_utc)
        order["_meta"] = dict(app.meta(direction, None, "TIME", playbook="OVERNIGHT_" + rule["name"]),
                              tier="O", tier_facts={"rule": rule["name"], "macro": macro, "entry_ny": self.cfg["entry_ny"],
                                                    "exit_ny": self.cfg["exit_ny"], "stop_atr": self.cfg["stop_atr"]},
                              tier_reason=f"overnight engine, rule {rule['name']} ({self.cfg['risk_pct']}%)")
        self.logged.add(day_key)                              # one attempt per day, whatever happens next
        try:
            # the 4H/1H veto stays on for with-trend rules; a rule that trades against or without the structure is
            # declared counter-trend so the veto's "opposes" branch does not refuse what the owner switched on
            counter = rule["macro"] != "BULLISH" if direction == "BUY" else rule["macro"] != "BEARISH"
            validated = app.validator.validate(order, "B_TRADE", evidence, atr15, now, require_evidence=False,
                                               counter_trend=counter, own_magic=self.cfg["magic"])
            lots, risk_money = app.validator.lots(validated, "B_TRADE", risk_pct=self.cfg["risk_pct"], min_lot_ok=False)
        except Rejected as r:
            LOG.warning("Overnight %s REJECTED by the risk engine: %s", sid, r)
            alerts.trade(f"🌙 Overnight trend {sid}: not traded: {r}", short=None)
            return f"{sid}: {r}"
        try:
            ticket = app.executor.send(validated, lots, risk_money, "B_TRADE", magic=self.cfg["magic"])
        except NW.NewsHold as h:                                     # 1.17: strict news rule, not a broker error
            LOG.info("Overnight %s not sent: %s", sid, h)
            return f"{sid}: {h}"
        except Exception as exc:  # noqa: BLE001
            alerts.error(f"Overnight order send failed: {exc}")
            return f"{sid}: broker: {exc}"
        if ticket:
            app.state.update_order(ticket, exit_utc=exit_utc.strftime("%Y-%m-%dT%H:%M:%SZ"))
        LOG.info("OVERNIGHT %s ticket=%s %s %.2f lots @ %.2f sl %.2f | exit %s UTC | risk %.2f", sid, ticket,
                 validated["direction"], lots, validated["entry_price"], validated["stop_loss"], exit_utc.strftime("%H:%M"),
                 risk_money)
        alerts.trade(f"🌙 OVERNIGHT {rule['name']} {validated['direction']} {lots} lots @ {validated['entry_price']:.2f} | stop "
                     f"{validated['stop_loss']:.2f} ({self.cfg['stop_atr']} x ATR15) | exit by time {self.cfg['exit_ny']} NY | "
                     f"risk {risk_money:.2f} | macro {macro} | demo",
                     short=f"🌙 Overnight {validated['direction']} {lots} @ {validated['entry_price']:.2f}, exit {self.cfg['exit_ny']} NY")
        return f"{sid}: sent #{ticket}"
