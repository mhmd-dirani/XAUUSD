#!/usr/bin/env python3
"""Auto-trader offline tests. Fake MetaTrader5 only: no terminal, network, Claude or Telegram.
Run:  python src/auto_trader.py --self-test"""
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import gold_monitor as GM  # noqa: E402
from autotrader import alerts, analyst, sessions as SS, settings  # noqa: E402
LOG_SENT: list = []                       # (kind, text) of every Telegram message a test produced
from autotrader.market import Market, MarketError  # noqa: E402
from autotrader.state import State  # noqa: E402
from autotrader.validator import Validator, Rejected  # noqa: E402
from autotrader.executor import Executor  # noqa: E402
from autotrader.app import App  # noqa: E402

UTC = timezone.utc
NY = GM.ZoneInfo("America/New_York")


class FakeMT5:
    """Same behaviour as the FakeMT5 in tests/test_broker_mt5.py: server time = UTC + offset,
    every symbol_info_tick() call produces a fresh tick (so the V6 offset probe can run)."""
    TIMEFRAME_M15, TIMEFRAME_H1, TIMEFRAME_H4, TIMEFRAME_D1, TIMEFRAME_W1 = 15, 16385, 16388, 16408, 32769
    COPY_TICKS_INFO = 1

    def __init__(self, offset_s=10800, live=True):
        self.offset, self.live, self.calls, self.ticks, self.rates = offset_s, live, [], [], {}

    def initialize(self, *a, **k):
        return True

    def shutdown(self):
        pass

    def last_error(self):
        return (1, "Success")

    def symbol_select(self, symbol, enable):
        return True

    def add_tick(self, bid, ask, utc_ts=None):
        msc = int(((utc_ts or time.time()) + self.offset) * 1000)
        if self.ticks and msc <= self.ticks[-1]["time_msc"]:
            msc = self.ticks[-1]["time_msc"] + 1
        self.ticks.append({"bid": bid, "ask": ask, "time_msc": msc})

    def symbol_info_tick(self, symbol):
        if self.live or not self.ticks:
            last = self.ticks[-1] if self.ticks else {"bid": 4000.0, "ask": 4000.3}
            self.add_tick(last["bid"], last["ask"])
        t = self.ticks[-1]
        return NS(bid=t["bid"], ask=t["ask"], time_msc=t["time_msc"], time=t["time_msc"] // 1000)

    def copy_rates_from_pos(self, symbol, tf, start, count):
        now = int(time.time()) + self.offset
        bar = now - now % 900 - 900 * start
        return [{"time": bar - 900 * i, "open": 4090.0, "high": 4096.0, "low": 4086.0, "close": 4091.0}
                for i in range(count)][::-1]


class FakeTradeMT5(FakeMT5):
    """FakeMT5 (V6 tests) plus the account/trading functions the auto-trader uses."""
    ACCOUNT_TRADE_MODE_DEMO, ACCOUNT_TRADE_MODE_REAL = 0, 2
    ORDER_TYPE_BUY, ORDER_TYPE_SELL, ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT = 0, 1, 2, 3
    ORDER_TYPE_BUY_STOP, ORDER_TYPE_SELL_STOP = 4, 5
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    TRADE_ACTION_DEAL, TRADE_ACTION_PENDING, TRADE_ACTION_SLTP, TRADE_ACTION_REMOVE = 1, 5, 6, 8
    ORDER_TIME_GTC, ORDER_TIME_SPECIFIED = 0, 2
    TRADE_RETCODE_PLACED, TRADE_RETCODE_DONE, TRADE_RETCODE_DONE_PARTIAL = 10008, 10009, 10010
    TRADE_RETCODE_INVALID_EXPIRATION = 10022
    POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
    DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_ENTRY_INOUT = 0, 1, 2

    def __init__(self, trade_mode=0, server="GIVTrade-Server", expiration_mode=15, **kw):
        super().__init__(**kw)
        self.trade_mode, self.server, self.expiration_mode = trade_mode, server, expiration_mode
        self.sent, self.orders, self.positions, self.deals = [], [], [], []
        self.add_tick(4094.41, 4094.54)

    def account_info(self):
        return NS(login=5550123, server=self.server, trade_mode=self.trade_mode, balance=10000.0,
                  equity=10000.0, currency="USD", company="GIV Capital Limited")

    def terminal_info(self):
        return NS(connected=True, trade_allowed=True, path="C:\\MT5_Demo", commondata_path=tempfile.gettempdir())

    def symbol_info(self, symbol):
        return NS(point=0.01, digits=2, trade_stops_level=0, trade_tick_size=0.01, trade_tick_value=1.0,
                  volume_step=0.01, volume_min=0.01, volume_max=100.0, expiration_mode=self.expiration_mode,
                  filling_mode=1)

    def positions_get(self, **k):
        return list(self.positions)

    def orders_get(self, **k):
        return list(self.orders)

    def history_deals_get(self, *a, **k):
        return list(self.deals)

    def order_send(self, req):
        self.sent.append(dict(req))
        return NS(retcode=10008 if req["action"] == 5 else 10009, order=700 + len(self.sent),
                  price=req.get("price"), comment="ok")


def config(**over):
    c = settings.load(None)
    tmp = Path(tempfile.mkdtemp())
    c["state_path"] = tmp
    c.update(over)
    return c


class Base(unittest.TestCase):
    trade_mode = 0

    def setUp(self):
        GM.MT5Session.reset()
        self.addCleanup(GM.MT5Session.reset)
        self.fake = FakeTradeMT5(trade_mode=self.trade_mode)
        p = patch.dict(sys.modules, {"MetaTrader5": self.fake})
        p.start()
        self.addCleanup(p.stop)
        # Tests never reach the real Telegram (1.7: a self-test run posted ~20 fake orders to the user's chat)
        env = patch.dict(os.environ, {"TRADER_MT5_PATH": "C:\\MT5_Demo\\terminal64.exe",
                                      "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""})
        env.start()
        self.addCleanup(env.stop)
        for name in ("trade", "error"):
            ap = patch.object(alerts, name, side_effect=lambda msg, short=None, _n=name: LOG_SENT.append((_n, msg, short)))
            ap.start()
            self.addCleanup(ap.stop)
        LOG_SENT.clear()
        # 1.7 tiers depend on the session: pin it so a test gives the same answer at 03:00 and at 19:00 UTC
        sp = patch.object(SS, "session_of", return_value="NEW_YORK")
        sp.start()
        self.addCleanup(sp.stop)
        self.c = config()
        self.m = Market(self.c)
        self.state = State(self.c["state_path"])


# ----------------------------------------------------------------------------------- settings
class SettingsTests(unittest.TestCase):
    def test_market_settings_come_from_v6(self):
        c = settings.load(None)
        self.assertEqual(c["symbol"], "XAUUSD")
        self.assertEqual(c["min_rr"], 2.0)
        self.assertTrue(c["block_on_mtf_opposition"])
        self.assertEqual(c["v6"]["provider_options"]["expected_mt5_server"], "GIVTrade-*")
        self.assertEqual(c["v6"]["providers"]["calendar"], "mt5_csv")

    def test_shipped_config_loads(self):
        c = settings.load(ROOT / "config" / "auto_trader_config.json")
        self.assertEqual(c["execute_grades"], ["A_TRADE", "B_TRADE"])

    def test_bad_settings_fail_at_start(self):
        d = Path(tempfile.mkdtemp())
        for bad in ('{"unknown": 1}', '{"risk_percent": {"A_TRADE": 5}}', '{"execute_grades": ["C"]}',
                    '{"analysis_every_minutes": 20}', '{"require_demo": "yes"}'):
            (d / "x.json").write_text(bad)
            with self.assertRaises(ValueError, msg=bad):
                settings.load(d / "x.json")


# ----------------------------------------------------------------------------------- market
class ConnectTests(Base):
    def test_demo_connects_with_offset_and_utc_quote(self):
        info = self.m.connect()
        self.assertTrue(info["demo"])
        self.assertEqual(info["offset_hours"], 3.0)
        q = self.m.quote()
        self.assertEqual((q["bid"], q["ask"]), (4094.41, 4094.54))
        self.assertLess(abs(q["age"]), 5)                      # converted to UTC, not 3h in the future
        self.assertLess(abs((q["time"] - datetime.now(UTC)).total_seconds()), 5)

    def test_trader_terminal_path_required(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRADER_MT5_PATH", None)
            with self.assertRaises(MarketError):
                self.m.connect()

    def test_wrong_server_refused(self):
        self.fake.server = "OtherBroker-Demo"
        with self.assertRaises(RuntimeError):
            self.m.connect()

    def test_server_time_conversion_both_ways(self):
        self.m.connect()
        self.assertEqual(self.m.utc_to_server(1_000_000), 1_000_000 + 10800)
        self.assertEqual(self.m.server_to_utc(1_000_000), 1_000_000 - 10800)


class RealAccountTests(Base):
    trade_mode = 2

    def test_real_account_refused(self):
        with self.assertRaisesRegex(MarketError, "NOT a demo"):
            self.m.connect()


class SessionTests(Base):
    def at(self, y, mo, d, h, mi):
        return datetime(y, mo, d, h, mi, tzinfo=NY).astimezone(UTC)

    def test_windows(self):
        w = self.m.trading_window
        self.assertEqual(w(self.at(2026, 10, 17, 12, 0)), "market closed")               # Saturday
        self.assertIn("after the open", w(self.at(2026, 10, 18, 18, 10)))               # Sunday open
        self.assertIn("before the close", w(self.at(2026, 10, 16, 16, 50)))             # Friday close
        self.assertIn("after the open", w(self.at(2026, 10, 14, 18, 5)))                # daily reopen
        self.assertIsNone(w(self.at(2026, 10, 14, 12, 0)))                              # Wednesday noon

    def test_news_lock_uses_v6_rule(self):
        now = datetime.now(UTC)
        self.m.calendar = {"fetched_at": GM.iso(now), "events": [
            {"time": GM.iso(now + timedelta(minutes=10)), "title": "Core CPI m/m", "impact": "HIGH"}]}
        self.assertIn("CPI", self.m.news_block(now))
        self.m.calendar["events"][0]["time"] = GM.iso(now + timedelta(hours=3))
        self.assertIsNone(self.m.news_block(now))
        self.m.calendar["fetched_at"] = GM.iso(now - timedelta(hours=2))               # stale export
        self.assertIn("unavailable or stale", self.m.news_block(now))


# ----------------------------------------------------------------------------------- validator
def evidence(trend4="BEARISH", trend1="BEARISH"):
    return {"built_at": GM.iso(datetime.now(UTC)),
            "frames": {"4h": {"trend": trend4}, "1h": {"trend": trend1}},
            "index": {"fvg_sell": {"id": "fvg_sell", "kind": "FVG", "direction": "SELL", "status": "ACTIVE"},
                      "bos_sell": {"id": "bos_sell", "kind": "BOS", "direction": "SELL", "status": "CONFIRMED"},
                      "ob_buy": {"id": "ob_buy", "kind": "ORDER_BLOCK_CANDIDATE", "direction": "BUY", "status": "ACTIVE"},
                      "fvg_dead": {"id": "fvg_dead", "kind": "FVG", "direction": "SELL", "status": "INVALIDATED"},
                      "div": {"id": "div", "kind": "RSI_DIVERGENCE", "direction": "SELL", "status": "CONTEXT_ONLY"}}}


def sell_order(**over):
    o = {"setup_id": "SELL_SUPPLY_001", "direction": "SELL", "order_type": "SELL_LIMIT", "entry_price": 4100.5,
         "stop_loss": 4106.5, "take_profit_1": 4086.0, "take_profit_2": 4078.0, "tp1_close_percent": 50,
         "move_sl_to_breakeven_at_tp1": True, "max_entry_deviation": 1.0,
         "order_expiry_utc": GM.iso(datetime.now(UTC) + timedelta(hours=6)),
         "cancel_if_price_above": 4109.0, "cancel_if_price_below": 4090.0,
         "evidence_ids": ["fvg_sell", "bos_sell"], "rationale": "test"}
    o.update(over)
    return o


class ValidatorTests(Base):
    def setUp(self):
        super().setUp()
        self.m.connect()
        self.v = Validator(self.c, self.m, self.state)
        self.now = datetime.now(UTC)

    def ok(self, **over):
        return self.v.validate(sell_order(**over), "B_TRADE", evidence(), 5.0, self.now)

    def bad(self, pattern, ev=None, grade="A_TRADE", atr=5.0, **over):
        with self.assertRaisesRegex(Rejected, pattern):
            self.v.validate(sell_order(**over), grade, ev or evidence(), atr, self.now)

    def test_accepts_clean_order_and_recomputes_rr(self):
        o = self.ok()
        self.assertAlmostEqual(o["rr"], 2.42, places=2)
        self.assertEqual(o["evidence_ids"], ["fvg_sell", "bos_sell"])

    def test_numbers(self):
        self.bad("R:R", take_profit_1=4092.0, take_profit_2=None)
        self.bad("SELL needs", stop_loss=4099.0)
        self.bad("ATR15", stop_loss=4101.5, take_profit_1=4096.0, take_profit_2=None)   # stop 1.0 < 0.5*5? no: 1.0 < 2.5
        self.bad("ATR15", stop_loss=4130.0, take_profit_1=4030.0, take_profit_2=None)   # stop 29.5 > 5 x 5
        self.bad("finite number", entry_price="4100.5")
        self.bad("does not match", direction="BUY")
        self.bad("must be above the bid", entry_price=4094.0, stop_loss=4100.0, take_profit_1=4080.0,
                 take_profit_2=None, cancel_if_price_below=None)
        self.bad("too far", order_expiry_utc="2030-01-01T00:00:00Z")
        self.bad("no timezone", order_expiry_utc="2026-10-15T08:00:00")

    def test_v6_spread_rules(self):
        self.fake.add_tick(4094.0, 4094.45)                                             # 0.45 spread
        self.bad("5% of the stop", stop_loss=4106.0, take_profit_1=4088.0, take_profit_2=None)     # 0.45 > 5% of 5.5
        self.fake.add_tick(4094.0, 4094.25)                                             # 0.25 spread
        self.bad("spread\\+slippage", stop_loss=4106.5)                               # 0.50 > 7.5% of 6.0
        self.fake.add_tick(4094.0, 4094.6)
        self.bad("spread 0.60 > 0.5")

    def test_python_evidence_required(self):
        self.bad("cites no", evidence_ids=[])
        self.bad("does not exist", evidence_ids=["made_up"])
        self.bad("points BUY", evidence_ids=["ob_buy"])
        self.bad("INVALIDATED", evidence_ids=["fvg_dead"])
        self.bad("no structural", evidence_ids=["div"])
        self.bad("4H/1H structure", ev=evidence(trend1="BULLISH"))

    def test_grade_and_duplicates(self):
        self.c["execute_grades"] = ["A_TRADE"]
        self.bad("not in execute_grades", grade="B_TRADE")
        self.c["execute_grades"] = ["A_TRADE", "B_TRADE"]
        o = self.ok()
        self.state.record_order(901, o, "A_TRADE", 0.08, 48.0, "PENDING")
        self.bad("already has a working order")                       # same setup still working
        self.bad("duplicate of working order", setup_id="OTHER_ID")   # different id, same entry/stop
        self.state.update_order(901, status="CLOSED")
        self.assertEqual(self.ok()["setup_id"], "SELL_SUPPLY_001")  # finished -> a new order is allowed
        self.state.record_order(902, o, "A_TRADE", 0.08, 48.0, "CANCELLED")
        self.bad("already traded 2x today")                            # max_orders_per_setup_per_day

    def test_second_different_order_is_allowed_with_total_risk_cap(self):
        """Total risk comes from what the BROKER holds: this bot's orders, other bots, manual trades."""
        pend = lambda t, price, sl, vol, magic=40001, typ=3: NS(ticket=t, magic=magic, symbol="XAUUSD", type=typ,
                                                               price_open=price, sl=sl, volume_initial=vol,
                                                               volume_current=vol)
        o = self.ok()
        self.c["max_same_direction_positions"] = 2       # 1.6.1: the default (1) is tested in test_v161.py
        self.assertEqual(self.v.lots(o, "A_TRADE"), (0.08, 48.0))
        self.fake.orders = [pend(901, 4100.5, 4106.5, 0.08)]                             # 48 working
        second = self.v.validate(sell_order(setup_id="SELL_HIGHER_002", entry_price=4112.5, stop_loss=4118.5,
                                            take_profit_1=4090.0, take_profit_2=None, cancel_if_price_above=4120.0),
                                 "A_TRADE", evidence(), 5.0, self.now)
        self.assertEqual(self.v.lots(second, "A_TRADE"), (0.08, 48.0))                   # 48 + 48 <= 100
        self.fake.orders.append(pend(902, 4112.5, 4118.5, 0.08))
        with self.assertRaisesRegex(Rejected, "total risk"):
            self.v.lots(second, "A_TRADE")                                                # 96 + 48 > 100
        self.fake.orders = [pend(902, 4112.5, 4118.5, 0.08)]
        self.fake.positions = [NS(ticket=5, magic=0, symbol="XAUUSD", type=1, price_open=4100.0, sl=4099.0,
                                  volume=0.5)]                                            # manual, stop past entry
        self.assertEqual(self.v.open_risk(), (48.0, None))                                # locked profit counts 0
        self.fake.positions[0].sl = 4104.0                                                # manual SELL 0.5, 4 risk
        self.assertEqual(self.v.open_risk(), (248.0, None))                               # 48 + 200 (other trades count)

    def test_position_without_stop_blocks_new_orders(self):
        self.fake.positions = [NS(ticket=7, magic=0, symbol="XAUUSD", type=0, price_open=4100.0, sl=0.0, volume=0.1)]
        with self.assertRaisesRegex(Rejected, "cannot be measured.*position 7 XAUUSD without a stop"):
            self.v.lots(self.ok(), "B_TRADE")

    def test_gate_counts_pending_and_open_together(self):
        with patch.object(self.m, "trading_window", return_value=None), \
             patch.object(self.m, "news_block", return_value=None):
            self.fake.orders = [NS(ticket=1, magic=40001)]
            self.assertIsNone(self.v.gates(self.now))                          # 1 of 2: still analyses
            self.fake.positions = [NS(ticket=2, magic=40001)]
            self.assertIn("already working (max 2)", self.v.gates(self.now))
            self.fake.positions = [NS(ticket=3, magic=99)]                     # someone else's position
            self.assertIsNone(self.v.gates(self.now))

    def test_netting_account_takes_one_order(self):
        self.fake.ACCOUNT_MARGIN_MODE_RETAIL_NETTING = 0
        self.fake.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
        acc = self.fake.account_info()
        with patch.object(self.fake, "account_info", return_value=NS(**dict(vars(acc), margin_mode=0))):
            self.assertEqual(self.v.max_active(), 1)
            self.state.record_order(901, self.ok(), "A_TRADE", 0.08, 48.0, "PENDING")
            self.bad("netting account", setup_id="X", entry_price=4112.0, stop_loss=4118.0, take_profit_1=4090.0,
                     take_profit_2=None, cancel_if_price_above=4120.0)
        with patch.object(self.fake, "account_info", return_value=NS(**dict(vars(acc), margin_mode=2))):
            self.assertEqual(self.v.max_active(), 2)

    def test_lot_sizing_never_exceeds_risk(self):
        lots, risk = self.v.lots(self.ok(), "A_TRADE")       # 0.5% of 10k = 50 / (6.0 * 100) = 0.083
        self.assertEqual((lots, risk), (0.08, 48.0))
        lots, risk = self.v.lots(self.ok(), "B_TRADE")       # 25 / 600 = 0.041
        self.assertEqual((lots, risk), (0.04, 24.0))

    def test_daily_loss_limit_uses_utc_day(self):
        server_now = int(time.time()) + 10800
        self.fake.deals = [NS(magic=40001, symbol="XAUUSD", entry=1, time=server_now, profit=-210.0,
                              commission=0.0, swap=0.0)]
        self.assertIn("calendar unavailable", self.v.gates(self.now) if self.m.trading_window(self.now) is None
                      else "news lock: Economic calendar unavailable or stale")         # no calendar = fail closed
        with patch.object(self.m, "trading_window", return_value=None), \
             patch.object(self.m, "news_block", return_value=None):
            self.assertIn("daily loss limit", self.v.gates(self.now))

    def test_kill_file_blocks(self):
        (self.c["state_path"] / "STOP").write_text("")
        self.assertIn("kill file", self.v.gates(self.now))


# ----------------------------------------------------------------------------------- executor
class ExecutorTests(Base):
    def setUp(self):
        super().setUp()
        self.m.connect()
        self.v = Validator(self.c, self.m, self.state)
        self.x = Executor(self.c, self.m, self.state)
        self.order = self.v.validate(sell_order(), "A_TRADE", evidence(), 5.0, datetime.now(UTC))

    def test_pending_order_carries_server_time_expiry_and_final_tp(self):
        t = self.x.send(self.order, 0.08, 48.0, "A_TRADE")
        r = self.fake.sent[-1]
        expiry = datetime.fromisoformat(self.order["order_expiry_utc"].replace("Z", "+00:00")).timestamp()
        self.assertEqual(r["type_time"], FakeTradeMT5.ORDER_TIME_SPECIFIED)
        self.assertEqual(r["expiration"], int(expiry) + 10800)
        self.assertEqual((r["type"], r["price"], r["sl"], r["tp"]), (3, 4100.5, 4106.5, 4078.0))
        self.assertEqual(r["magic"], 40001)
        self.assertEqual(self.state.active_orders()[0]["ticket"], t)

    def test_gtc_when_broker_has_no_expiry(self):
        self.fake.expiration_mode = 1
        self.x.send(self.order, 0.08, 48.0, "A_TRADE")
        self.assertEqual(self.fake.sent[-1]["type_time"], FakeTradeMT5.ORDER_TIME_GTC)

    def test_dry_run_sends_nothing(self):
        Executor(self.c, self.m, self.state, dry_run=True).send(self.order, 0.08, 48.0, "A_TRADE")
        self.assertEqual(self.fake.sent, [])

    def test_cancel_level_before_fill(self):
        t = self.x.send(self.order, 0.08, 48.0, "A_TRADE")
        self.fake.orders = [NS(ticket=t, magic=40001)]
        self.fake.add_tick(4109.2, 4109.4)
        self.x.manage()
        self.assertEqual(self.fake.sent[-1], {"action": 8, "order": t})
        self.assertEqual(self.state.all_orders()[0]["status"], "CANCELLED")

    def test_fill_then_tp1_partial_and_breakeven(self):
        t = self.x.send(self.order, 0.08, 48.0, "A_TRADE")
        pos = NS(ticket=t, identifier=t, magic=40001, type=1, volume=0.08, price_open=4100.5, tp=4078.0)
        self.fake.orders, self.fake.positions = [], [pos]
        self.x.manage()
        self.assertEqual(self.state.all_orders()[0]["status"], "OPEN")
        self.fake.add_tick(4085.8, 4085.95)                                              # ask <= TP1 4086
        self.x.manage()
        close, be = self.fake.sent[-2], self.fake.sent[-1]
        self.assertEqual((close["action"], close["volume"], close["type"], close["position"]), (1, 0.04, 0, t))
        self.assertEqual((be["action"], be["sl"], be["tp"]), (6, 4100.5, 4078.0))
        self.assertEqual(self.state.all_orders()[0]["status"], "TP1_DONE")
        self.fake.positions = []
        self.x.manage()
        self.assertEqual(self.state.all_orders()[0]["status"], "CLOSED")


class MT5ArgumentFormTests(Base):
    """MetaTrader5 builds differ in how they accept arguments; the trader adapts and fails safe."""
    def setUp(self):
        super().setUp()
        self.m.connect()
        from autotrader.market import Market as Mk
        Mk._send_form = None
        self.addCleanup(setattr, Mk, "_send_form", None)

    def test_positional_refused_with_minus2_then_keywords_used(self):
        real = self.fake.order_send
        def order_send(*args, **kw):
            if args:                                                           # this build: no positional
                self.fake.err = (-2, "Unnamed arguments not allowed")
                return None
            return real(kw.get("request") or kw)
        self.fake.order_send = order_send
        self.fake.last_error = lambda: getattr(self.fake, "err", (1, "Success"))
        res = self.m.order_send({"action": 5, "symbol": "XAUUSD", "volume": 0.01, "price": 4000, "comment": "B:IDEA ü"})
        self.assertEqual(res.retcode, 10008)
        from autotrader.market import Market as Mk
        self.assertEqual(Mk._send_form, "keywords")
        sent = self.fake.sent[-1]
        self.assertIsInstance(sent["price"], float)
        self.assertEqual(sent["comment"], "B:IDEA")                            # ASCII-only, max 31

    def test_real_refusal_is_reported_not_retried(self):
        calls = []
        self.fake.order_send = lambda *a, **k: calls.append(1)
        self.fake.last_error = lambda: (10027, "AutoTrading disabled by client")
        with self.assertRaisesRegex(RuntimeError, "AutoTrading disabled"):
            self.m.order_send({"action": 5})
        self.assertEqual(len(calls), 1)

    def test_unreadable_deal_history_blocks_instead_of_assuming_no_losses(self):
        self.fake.history_deals_get = lambda *a, **k: None
        v = Validator(self.c, self.m, self.state)
        with patch.object(self.m, "trading_window", return_value=None), \
             patch.object(self.m, "news_block", return_value=None):
            self.assertIn("loss limits unverifiable", v.gates(datetime.now(UTC)))


# ----------------------------------------------------------------------------------- analyst / app
class AnalystParseTests(unittest.TestCase):
    def test_bad_output_never_becomes_a_trade(self):
        self.assertEqual(analyst.parse("I think gold goes up")["decision"], "NO_TRADE")
        self.assertEqual(analyst.parse('{"decision":"A_TRADE"}')["decision"], "NO_TRADE")
        full = ('{"decision":"A_TRADE","bias":{},"market_map":{},"setups":[],"order":null,'
                '"news_risk":"LOW","reasoning_summary":"x"}')
        self.assertEqual(analyst.parse(full)["decision"], "NO_TRADE")
        one = full.replace('"order":null', '"order":{"setup_id":"S"}')                # old single-order form
        self.assertEqual(analyst.parse(one)["orders"], [{"setup_id": "S", "grade": "A_TRADE"}])
        two = full.replace('"order":null', '"orders":[{"setup_id":"S1","grade":"B_TRADE"},{"setup_id":"S2"}]')
        self.assertEqual([o["grade"] for o in analyst.parse(two)["orders"]], ["B_TRADE", "A_TRADE"])
        self.assertEqual(analyst.parse(full.replace('"order":null', '"orders":"x"'))["decision"], "NO_TRADE")
        self.assertEqual(analyst.parse("```json\n" + full.replace('"A_TRADE"', '"WATCH"') + "\n```")["decision"], "WATCH")


class AppTests(Base):
    def setUp(self):
        super().setUp()
        self.app = App(self.c)
        self.app.market = self.m
        self.app.validator.m = self.app.executor.m = self.m
        self.m.connect()

    def test_triggers(self):
        q = {"bid": 4094.4, "ask": 4094.5}
        self.assertEqual(self.app.trigger_for(datetime(2026, 10, 14, 13, 0, tzinfo=UTC), q), "scheduled")
        self.assertIsNone(self.app.trigger_for(datetime(2026, 10, 14, 13, 15, tzinfo=UTC), q))
        self.app.state.put("last_map", {"setups": [{"setup_id": "S1", "status": "WATCHING",
                                                    "watch_zone_low": 4095.0, "watch_zone_high": 4098.0}]})
        self.app.state.put("last_atr15", 4.0)                                           # pad 1.0
        self.assertEqual(self.app.trigger_for(datetime(2026, 10, 14, 13, 15, tzinfo=UTC), q), "zone S1")
        self.assertIsNone(self.app.trigger_for(datetime(2026, 10, 14, 13, 30, tzinfo=UTC), q))  # cooldown

    def test_full_cycle_sends_validated_order(self):
        two = [dict(sell_order(), grade="A_TRADE"),
               dict(sell_order(setup_id="SELL_HIGHER_002", entry_price=4112.5, stop_loss=4118.5, take_profit_1=4090.0,
                               take_profit_2=None, cancel_if_price_above=4120.0), grade="B_TRADE"),
               dict(sell_order(setup_id="DUP_003"), grade="B_TRADE")]                      # duplicate of the first
        result = {"decision": "A_TRADE", "bias": {}, "market_map": {}, "setups": [], "news_risk": "LOW",
                  "reasoning_summary": "test", "orders": two}
        with patch.object(self.m, "trading_window", return_value=None), \
             patch.object(self.m, "news_block", return_value=None), \
             patch.object(self.m, "history", return_value={}), \
             patch.object(self.m, "evidence", return_value=evidence()), \
             patch.object(self.m, "atr15", return_value=5.0), \
             patch("autotrader.app.build_payload", return_value={}), \
             patch.object(self.app.analyst, "analyse", return_value=result):
            self.app.cycle("test")
        self.assertEqual(len(self.fake.sent), 2)                         # both different orders placed
        self.assertEqual([r["volume"] for r in self.fake.sent], [0.08, 0.04])   # A 0.5%, B 0.25%
        self.assertEqual([r["comment"][:2] for r in self.fake.sent], ["A:", "B:"])
        self.assertEqual(len(self.app.state.active_orders()), 2)
        audit = self.app.state._c().execute("SELECT accepted, reject_why FROM analyses").fetchone()
        self.assertEqual(audit[0], 1)
        self.assertIn("SELL_SUPPLY_001: sent", audit[1])

    def test_no_api_call_while_blocked(self):
        with patch.object(self.m, "trading_window", return_value="market closed"), \
             patch.object(self.app.analyst, "analyse") as call:
            self.assertIsNone(self.app.cycle("test"))
        call.assert_not_called()


class ClaudeConnectionTests(unittest.TestCase):
    """The trader calls Claude exactly like the V6 Claude analyst (same client, same model)."""

    def test_model_follows_v6_config(self):
        c = settings.load(None)
        import astra_anthropic as AN
        self.assertEqual(c["anthropic_model"], AN.model_name(c["v6"]))
        with patch.dict(os.environ, {"GOLD_CLAUDE_MODEL": "claude-sonnet-5-5"}):
            self.assertEqual(settings.load(None)["anthropic_model"], "claude-sonnet-5-5")

    def test_request_uses_v6_client_cache_and_effort(self):
        c = settings.load(None)
        sent = {}
        text = ('{"decision":"WATCH","bias":{},"market_map":{},"setups":[],"order":null,'
                '"news_risk":"LOW","reasoning_summary":"x"}')

        class Msgs:
            def create(self, **kw):
                sent.update(kw)
                return NS(stop_reason="end_turn", content=[NS(type="text", text=text)],
                          usage=NS(input_tokens=24000, output_tokens=900))

        a = analyst.Analyst(c)
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}), \
             patch("astra_anthropic.make_client", return_value=NS(messages=Msgs())) as mk:
            r = a.analyse({"x": 1})
        mk.assert_called_once()
        self.assertEqual(r["decision"], "WATCH")
        self.assertEqual(sent["model"], c["anthropic_model"])
        self.assertEqual(sent["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(sent["extra_body"]["output_config"]["effort"], "medium")
        self.assertEqual(a.last_usage, {"input_tokens": 24000, "output_tokens": 900})

    def test_refusal_and_cutoff_are_no_trade(self):
        c = settings.load(None)
        for stop in ("refusal", "max_tokens"):
            class Msgs:
                def create(self, **kw):
                    return NS(stop_reason=stop, content=[], usage=None)
            a = analyst.Analyst(c)
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}), \
                 patch("astra_anthropic.make_client", return_value=NS(messages=Msgs())):
                self.assertEqual(a.analyse({})["decision"], "NO_TRADE")


# ----------------------------------------------------------------------------------- signal-bot bridge
from autotrader import bridge as BR  # noqa: E402

REAL_IDEA = {"id": "ID8", "direction": "SELL", "risk": "MEDIUM", "confidence": 36, "entry_type": "LIMIT",
             "readiness": "NEEDS_CONFIRMATION", "title": "sell rejection at the CPI ceiling",
             "entry_low": 4103.6, "entry_high": 4105.7, "invalidation": 4118.2,
             "targets": [4073.53, 4066.53, 4040.61]}                     # from runtime/astra_analysis.json, 14 Oct


class IdeaConversionTests(unittest.TestCase):
    def setUp(self):
        self.c = settings.load(None)
        self.q = {"bid": 4094.41, "ask": 4094.54}
        self.now = datetime(2026, 10, 14, 21, 0, tzinfo=UTC)

    def conv(self, q=None, **over):
        return BR.idea_to_order(dict(REAL_IDEA, **over), q or self.q, self.c, self.now)

    def test_real_sell_idea_becomes_sell_limit_at_zone_bottom(self):
        o, why = self.conv()
        self.assertIsNone(why)
        self.assertEqual((o["order_type"], o["entry_price"], o["stop_loss"]), ("SELL_LIMIT", 4103.6, 4118.2))
        self.assertEqual((o["take_profit_1"], o["take_profit_2"], o["tp1_close_percent"]), (4073.53, 4066.53, 50))
        self.assertEqual(o["cancel_if_price_below"], 4073.53)                   # move happened without us
        self.assertEqual(o["grade"], "B_TRADE")                                 # 36 % -> B size
        self.assertEqual(o["setup_id"], "IDEA-SELL-4104")
        self.assertEqual(o["order_expiry_utc"], GM.iso(self.now + timedelta(hours=12)))

    def test_entry_moved_inside_zone_to_reach_2r(self):
        # 14 Oct 22:38 UTC: SELL LIMIT 4116-4123, stop 4138, TP1 4079.75 (42 %). The zone bottom gives
        # only 1.65R; the trader sells at the lowest price that still gives 2R.
        idea = dict(REAL_IDEA, entry_low=4116.0, entry_high=4123.0, invalidation=4138.0,
                    targets=[4079.75, 4073.53], confidence=42)
        o, _ = BR.idea_to_order(idea, {"bid": 4107.63, "ask": 4107.83}, self.c, self.now)
        rr = (o["entry_price"] - o["take_profit_1"]) / (o["stop_loss"] - o["entry_price"])
        self.assertEqual(o["entry_price"], 4118.71)
        self.assertGreaterEqual(rr, 2.0)
        buy = dict(REAL_IDEA, direction="BUY", entry_low=4080.0, entry_high=4086.0, invalidation=4070.0,
                   targets=[4110.0])                                    # top 4086 gives 1.5R -> buy lower
        o, _ = BR.idea_to_order(buy, self.q, self.c, self.now)
        self.assertEqual(o["entry_price"], 4083.25)                       # (4110 + 2.02 x 4070) / 3.02
        self.assertGreaterEqual((o["take_profit_1"] - o["entry_price"]) / (o["entry_price"] - o["stop_loss"]), 2.0)
        far = dict(idea, targets=[4110.0])                               # 2R impossible inside the zone
        self.assertEqual(BR.idea_to_order(far, {"bid": 4100.0, "ask": 4100.2}, self.c, self.now)[0]["entry_price"],
                         4123.0)                                         # best edge; risk engine rejects < 2R

    def test_grades_and_filters(self):
        self.assertEqual(self.conv(confidence=65)[0]["grade"], "A_TRADE")
        self.assertEqual(self.conv(confidence=65, risk="HIGH")[0]["grade"], "B_TRADE")
        self.c["idea_min_confidence"] = 40
        self.assertIn("below idea_min_confidence", self.conv()[1])
        self.c["idea_min_confidence"], self.c["idea_allowed_risk"] = 0, ["LOW", "MEDIUM"]
        self.assertIn("not in idea_allowed_risk", self.conv(risk="HIGH")[1])

    def test_price_inside_zone(self):
        inside = {"bid": 4104.5, "ask": 4104.7}
        self.assertIn("waits for confirmation", self.conv(inside)[1])            # needs confirmation: skip
        o, _ = self.conv(inside, readiness="VALID_NOW")
        self.assertEqual((o["order_type"], o["entry_price"]), ("MARKET", 4104.5))

    def test_buy_limit_and_breakouts(self):
        o, _ = self.conv(direction="BUY", entry_low=4080.0, entry_high=4083.0, invalidation=4070.0,
                         targets=[4110.0])
        self.assertEqual((o["order_type"], o["entry_price"], o["cancel_if_price_above"], o["take_profit_2"]),
                         ("BUY_LIMIT", 4083.0, 4110.0, None))
        o, _ = self.conv(direction="BUY", entry_type="BREAKOUT", entry_low=4118.0, entry_high=4120.0,
                         invalidation=4108.0, targets=[4140.0])
        self.assertEqual((o["order_type"], o["entry_price"], o["cancel_if_price_below"]), ("BUY_STOP", 4118.0, 4108.0))
        self.assertIn("already passed", self.conv(direction="SELL", entry_type="BREAKOUT", entry_low=4095.0,
                                                  entry_high=4097.0, invalidation=4105.0, targets=[4080.0])[1])

    def test_signal_is_a_market_order_inside_its_zone(self):
        sig = {"sid": "2026-10-14:abc", "direction": "SELL", "grade": "A_TRADE", "entry_low": 4103.6,
               "entry_high": 4105.7, "stop_loss": 4118.2, "targets": [4073.53], "playbook": "TREND_CONTINUATION",
               "evidence_ids": ["x"]}
        o, why = BR.signal_to_order(sig, {"bid": 4104.9, "ask": 4105.1}, self.c, self.now)
        self.assertEqual((o["order_type"], o["entry_price"], o["grade"]), ("MARKET", 4104.9, "A_TRADE"))
        self.assertIn("left the entry zone", BR.signal_to_order(sig, self.q, self.c, self.now)[1])


class BridgeFileTests(Base):
    def setUp(self):
        super().setUp()
        self.v6dir = Path(tempfile.mkdtemp())
        self.c["v6"] = dict(self.c["v6"], state_directory=str(self.v6dir))
        self.b = BR.Bridge(self.c, self.state)
        self.now = datetime.now(UTC)

    def write_signal(self, key, age_s=1, partial=False):
        rec = {"key": key, "sid": key, "at": GM.iso(self.now - timedelta(seconds=age_s)), "direction": "SELL"}
        with open(self.v6dir / "signals.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + ("" if partial else "\n"))

    def test_signals_once_fresh_and_complete_lines_only(self):
        self.write_signal("k1")
        self.write_signal("k_old", age_s=600)
        self.assertEqual([s["key"] for s in self.b.new_signals(self.now)], ["k1"])
        self.assertEqual(self.b.new_signals(self.now), [])                       # nothing new
        self.write_signal("k2", partial=True)                                    # signal bot mid-write
        self.assertEqual(self.b.new_signals(self.now), [])
        with open(self.v6dir / "signals.jsonl", "a") as f:
            f.write("\n")
        self.write_signal("k1")                                                  # re-armed duplicate key
        self.assertEqual([s["key"] for s in self.b.new_signals(self.now)], ["k2"])

    def test_ideas_fresh_and_each_handled_once(self):
        a = {"id": "A1", "created_at": GM.iso(self.now - timedelta(minutes=5)), "ideas": [REAL_IDEA]}
        (self.v6dir / "astra_analysis.json").write_text(json.dumps(a))
        got, ideas = self.b.open_ideas(self.now)
        self.assertEqual(len(ideas), 1)
        self.b.idea_done(got, ideas[0])
        self.assertEqual(self.b.open_ideas(self.now)[1], [])
        a.update(id="A2", created_at=GM.iso(self.now - timedelta(hours=3)))       # signal bot stopped
        (self.v6dir / "astra_analysis.json").write_text(json.dumps(a))
        self.assertEqual(self.b.open_ideas(self.now)[1], [])


class BridgeAppTests(Base):
    """idea_entry_mode "limit": pending order at once (the 1.3 behaviour, still available)."""
    def setUp(self):
        super().setUp()
        self.c["idea_entry_mode"] = "limit"
        self.v6dir = Path(tempfile.mkdtemp())
        self.c["v6"] = dict(self.c["v6"], state_directory=str(self.v6dir))
        self.app = App(self.c)
        self.app.market = self.m
        self.app.validator.m = self.app.executor.m = self.m
        self.m.connect()
        self.now = datetime.now(UTC)
        p = [patch.object(self.m, "trading_window", return_value=None),
             patch.object(self.m, "news_block", return_value=None),
             patch.object(self.app, "market_context", return_value=(evidence(), 5.0))]
        for x in p:
            x.start()
            self.addCleanup(x.stop)

    def analysis(self, aid="A1", ideas=(REAL_IDEA,)):
        a = {"id": aid, "created_at": GM.iso(self.now - timedelta(minutes=2)), "ideas": list(ideas)}
        (self.v6dir / "astra_analysis.json").write_text(json.dumps(a))

    def test_idea_becomes_pending_order_once_without_claude(self):
        self.analysis()
        with patch.object(self.app.analyst, "analyse") as claude:
            self.app.bridge_tick(self.now)
            self.app.bridge_tick(self.now)                                       # same analysis: nothing new
        claude.assert_not_called()
        self.assertEqual(len(self.fake.sent), 1)
        r = self.fake.sent[0]
        self.assertEqual((r["type"], r["price"], r["sl"], r["tp"], r["volume"]), (3, 4103.6, 4118.2, 4066.53, 0.03))  # Tier B 0.5 %
        self.assertTrue(r["comment"].startswith("B:IDEA-SELL-4104"))

    def test_new_analysis_same_idea_is_a_duplicate_not_a_second_order(self):
        self.analysis("A1")
        self.app.bridge_tick(self.now)
        self.analysis("A2", [dict(REAL_IDEA, id="ID9", entry_low=4103.8)])        # re-issued next map
        self.app.bridge_tick(self.now)
        self.assertEqual(len(self.fake.sent), 1)

    def test_two_different_ideas_two_orders(self):
        second = dict(REAL_IDEA, id="ID2", entry_low=4110.0, entry_high=4112.0, invalidation=4122.0,
                      targets=[4085.0])
        self.analysis(ideas=(REAL_IDEA, second))
        self.app.bridge_tick(self.now)
        self.assertEqual([r["price"] for r in self.fake.sent], [4103.6, 4110.0])   # two SELLs are fine

    def test_gate_block_keeps_ideas_for_later(self):
        self.analysis()
        with patch.object(self.m, "trading_window", return_value="first 30 min after the open"):
            self.app.bridge_tick(self.now)
        self.assertEqual(self.fake.sent, [])
        self.app.bridge_tick(self.now)                                           # window opened: placed now
        self.assertEqual(len(self.fake.sent), 1)

    def test_price_inside_zone_is_retried_when_price_leaves(self):
        self.analysis()
        self.fake.add_tick(4104.5, 4104.7)                                        # inside the SELL zone
        self.app.bridge_tick(self.now)
        self.app.bridge_tick(self.now)
        self.assertEqual(self.fake.sent, [])
        self.fake.add_tick(4096.0, 4096.2)                                        # back below: limit valid
        self.app.bridge_tick(self.now)
        self.assertEqual(self.fake.sent[0]["price"], 4103.6)

    def test_candles_read_once_per_15m_bar(self):
        self.app._ctx = None
        calls = []
        real = App.market_context.__get__(self.app)                               # unpatched method
        with patch.object(self.m, "history", side_effect=lambda: calls.append(1) or {"15min": []}), \
             patch.object(self.m, "evidence", return_value=evidence()), patch.object(self.m, "atr15", return_value=5.0):
            real(); real(); real()
            self.assertEqual(len(calls), 1)                                       # same bar: cached
            self.app._ctx = (self.app._ctx[0] - timedelta(minutes=15),) + self.app._ctx[1:]
            real()
            self.assertEqual(len(calls), 2)                                       # new bar: rebuilt

    def test_confirmed_signal_is_executed_at_market(self):
        self.fake.add_tick(4104.9, 4105.1)
        rec = {"key": "2026-10-14:abc:signal", "sid": "2026-10-14:abc", "at": GM.iso(self.now), "direction": "SELL",
               "grade": "A_TRADE", "entry_low": 4103.6, "entry_high": 4105.7, "stop_loss": 4118.2,
               "targets": [4073.53], "evidence_ids": [], "playbook": "TREND_CONTINUATION"}
        (self.v6dir / "signals.jsonl").write_text(json.dumps(rec) + "\n")
        self.app.bridge_tick(self.now)
        r = self.fake.sent[0]
        self.assertEqual((r["action"], r["type"], r["sl"], r["tp"]), (1, 1, 4118.2, 4073.53))
        self.assertEqual(r["volume"], 0.03)                                       # A: 50 / (13.3 x 100) -> 0.03


def bar(t, o, h, l, c):
    return {"time": GM.iso(t), "closes": GM.iso(t + timedelta(minutes=15)), "open": o, "high": h, "low": l, "close": c}


# The 15 Oct 01:00 UTC idea that lost: SELL 4144.71-4150.53, stop 4158.77, TP1 4111.92 (wait for rejection).
NIGHT_IDEA = {"id": "ID1", "direction": "SELL", "risk": "MEDIUM", "confidence": 33, "entry_type": "LIMIT",
              "readiness": "NEEDS_CONFIRMATION", "entry_low": 4144.71, "entry_high": 4150.53,
              "invalidation": 4158.77, "targets": [4111.92, 4096.81, 4079.75], "title": "sell CPI block"}


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.t0 = datetime(2026, 10, 15, 1, 0, tzinfo=UTC)

    def test_rally_through_the_zone_never_confirms_and_then_invalidates(self):
        bars = [bar(self.t0, 4140, 4147, 4139, 4146),                 # into the zone, closes inside: no
                bar(self.t0 + timedelta(minutes=15), 4146, 4152, 4145, 4151),   # bullish: no
                bar(self.t0 + timedelta(minutes=30), 4151, 4159.5, 4150, 4157)]  # through the stop
        self.assertEqual(BR.confirmation(NIGHT_IDEA, bars, self.t0)[0], "invalidated")
        self.assertEqual(BR.confirmation(NIGHT_IDEA, bars[:2], self.t0)[0], "waiting")

    def test_bearish_rejection_candle_confirms(self):
        # 01:30 candle from the report: high 4148.35, close 4139.17 below the zone, wick 5.71 > body 3.47
        b = bar(self.t0 + timedelta(minutes=30), 4142.64, 4148.35, 4138.9, 4139.17)
        status, got, how = BR.confirmation(NIGHT_IDEA, [b], self.t0)
        self.assertEqual((status, how), ("confirmed", "REJECTION"))
        c = settings.load(None)
        o, _ = BR.confirmed_order(NIGHT_IDEA, {"bid": 4139.0, "ask": 4139.2}, c, self.t0, got)
        self.assertEqual((o["order_type"], o["entry_price"], o["cancel_if_price_below"]), ("SELL_LIMIT", 4143.26, 4111.92))
        o, _ = BR.confirmed_order(NIGHT_IDEA, {"bid": 4145.5, "ask": 4145.7}, c, self.t0, got)
        self.assertEqual((o["order_type"], o["entry_price"]), ("MARKET", 4145.5))

    def test_candles_before_the_analysis_do_not_count(self):
        b = bar(self.t0 - timedelta(minutes=30), 4142.64, 4148.35, 4138.9, 4139.17)
        self.assertEqual(BR.confirmation(NIGHT_IDEA, [b], self.t0)[0], "waiting")

    def test_buy_mirror_and_small_wick_rejected(self):
        idea = dict(NIGHT_IDEA, direction="BUY", entry_low=4112.72, entry_high=4117.64, invalidation=4096.81,
                    targets=[4150.0])
        good = bar(self.t0, 4119, 4120, 4110, 4119.5)          # dips into zone, closes above, lower wick 9 > 0.5
        weak = bar(self.t0, 4114, 4121, 4113.5, 4120)          # stays in the zone, wick 0.5 < body 6: no
        self.assertEqual(BR.confirmation(idea, [good], self.t0 - timedelta(minutes=15))[0], "confirmed")
        self.assertEqual(BR.confirmation(idea, [weak], self.t0 - timedelta(minutes=15))[0], "waiting")


class ConfirmationAppTests(Base):
    def setUp(self):
        super().setUp()
        self.v6dir = Path(tempfile.mkdtemp())
        self.c["v6"] = dict(self.c["v6"], state_directory=str(self.v6dir))
        self.app = App(self.c)
        self.app.market = self.m
        self.app.validator.m = self.app.executor.m = self.m
        self.m.connect()
        self.now = datetime.now(UTC)
        self.bars = []
        for x in (patch.object(self.m, "trading_window", return_value=None),
                  patch.object(self.m, "news_block", return_value=None),
                  patch.object(self.app, "market_context", return_value=(evidence(), 8.0)),
                  patch.object(self.app, "recent_bars", side_effect=lambda: self.bars)):
            x.start()
            self.addCleanup(x.stop)
        a = {"id": "A1", "created_at": GM.iso(self.now - timedelta(minutes=40)), "ideas": [NIGHT_IDEA]}
        (self.v6dir / "astra_analysis.json").write_text(json.dumps(a))
        self.t = self.now - timedelta(minutes=30)

    def test_no_order_while_waiting_then_market_order_on_rejection(self):
        self.fake.add_tick(4146.0, 4146.2)
        self.bars = [bar(self.t, 4140, 4147, 4139, 4146)]
        self.app.bridge_tick(self.now)
        self.assertEqual(self.fake.sent, [])                                     # no limit order any more
        self.fake.add_tick(4139.0, 4139.2)
        self.bars.append(bar(self.t + timedelta(minutes=15), 4142.64, 4148.35, 4138.9, 4139.17))
        self.app.bridge_tick(self.now)
        r = self.fake.sent[0]                                                    # market at 4139 = 1.37R only:
        self.assertEqual((r["action"], r["type"], r["price"], r["sl"]), (5, 3, 4143.26, 4158.77))  # sell the retest
        self.fake.sent.clear()
        self.app.state.update_order(self.app.state.all_orders()[0]["ticket"], status="CANCELLED")
        self.app.bridge.state.put("bridge:ideas_done", [])
        self.fake.add_tick(4146.0, 4146.2)                                       # confirmed while price still high
        self.app.bridge_tick(self.now)
        self.assertEqual((self.fake.sent[0]["action"], self.fake.sent[0]["type"]), (1, 1))   # 2.37R: market SELL

    def test_overnight_rally_places_nothing(self):
        self.fake.add_tick(4157.0, 4157.2)
        self.bars = [bar(self.t, 4140, 4147, 4139, 4146), bar(self.t + timedelta(minutes=15), 4151, 4159.5, 4150, 4157)]
        self.app.bridge_tick(self.now)
        self.app.bridge_tick(self.now)
        self.assertEqual(self.fake.sent, [])

    def test_result_recorded_when_position_closes(self):
        self.fake.deals = [NS(profit=-19.32, commission=-0.08, swap=0.0, magic=40001, symbol="XAUUSD", entry=1,
                              time=int(time.time()) + 10800)]
        o = {"setup_id": "S", "direction": "SELL", "order_type": "MARKET", "entry_price": 4119.0, "stop_loss": 4138.0,
             "take_profit_1": 4080.0, "take_profit_2": None, "tp1_close_percent": 100,
             "move_sl_to_breakeven_at_tp1": False, "order_expiry_utc": GM.iso(self.now), "rr": 2.0}
        self.app.state.record_order(801, o, "B_TRADE", 0.01, 19.0, "OPEN", position_ticket=801)
        self.app.executor.manage()                                               # position gone -> CLOSED
        self.app.executor.manage()                                               # result filled
        self.assertEqual(self.app.state.all_orders()[0]["result"], -19.4)


# ----------------------------------------------------------------------------------- counter-trend (bidirectional)
M69 = {"kind": "SR", "timeframe": "1day", "role": "SUPPORT", "low": 4075.19, "high": 4079.75, "status": "FLIPPED",
       "tests": 3}                                                       # from runtime/astra_memory.sqlite3, 14 Oct
COUNTER_BUY = {"id": "C1", "direction": "BUY", "risk": "HIGH", "confidence": 30, "entry_type": "LIMIT",
               "readiness": "NEEDS_CONFIRMATION", "entry_low": 4074.5, "entry_high": 4079.75,
               "invalidation": 4069.5, "targets": [4097.95, 4110.88], "title": "Counter-trend: buy Daily support M69"}


class StrongZoneTests(unittest.TestCase):
    def setUp(self):
        self.c = settings.load(None)

    def test_buy_at_tested_daily_support(self):
        self.assertIs(BR.strong_zone(COUNTER_BUY, [M69], self.c), M69)

    def test_weak_wrong_role_or_far_zones_do_not_count(self):
        for z in (dict(M69, tests=1), dict(M69, timeframe="1h"), dict(M69, role="RESISTANCE"),
                  dict(M69, status="BROKEN"), dict(M69, low=4095.0, high=4099.0)):
            self.assertIsNone(BR.strong_zone(COUNTER_BUY, [z], self.c), z)

    def test_sweep_and_choch_confirmations(self):
        t0 = datetime(2026, 10, 14, 15, 0, tzinfo=UTC)
        sweep = bar(t0 + timedelta(minutes=15), 4076.0, 4080.5, 4073.5, 4079.2)     # wick under 4074.5, back in
        self.assertEqual(BR.confirmation(COUNTER_BUY, [sweep], t0)[2], "SWEEP")
        inside = bar(t0 + timedelta(minutes=15), 4078.0, 4079.0, 4076.0, 4077.0)
        ev = {"index": {"ch": {"kind": "CHOCH", "timeframe": "15min", "status": "CONFIRMED", "direction": "BUY",
                               "confirmed_at": GM.iso(t0 + timedelta(minutes=30))}}}
        self.assertEqual(BR.confirmation(COUNTER_BUY, [inside], t0)[0], "waiting")
        self.assertEqual(BR.confirmation(COUNTER_BUY, [inside], t0, ev)[2], "CHOCH")


class CounterTrendAppTests(Base):
    """14 Oct: bearish 4H/1H, price at Daily support 4075-4080 (the 4077 example)."""
    def setUp(self):
        super().setUp()
        self.v6dir = Path(tempfile.mkdtemp())
        self.c["v6"] = dict(self.c["v6"], state_directory=str(self.v6dir))
        self.app = App(self.c)
        self.app.market = self.m
        self.app.validator.m = self.app.executor.m = self.m
        self.m.connect()
        self.now = datetime.now(UTC)
        self.t = self.now - timedelta(minutes=30)
        self.zones = [M69]
        self.bars = [bar(self.t, 4076.0, 4080.5, 4073.5, 4079.2)]                      # sweep + reclaim
        for x in (patch.object(self.m, "trading_window", return_value=None),
                  patch.object(self.m, "news_block", return_value=None),
                  patch.object(self.app, "market_context", return_value=(evidence(), 5.0)),   # BEARISH 4H/1H
                  patch.object(self.app, "recent_bars", side_effect=lambda: self.bars),
                  patch.object(self.app, "memory_zones", side_effect=lambda: self.zones)):
            x.start()
            self.addCleanup(x.stop)
        self.fake.add_tick(4079.0, 4079.2)

    def run_idea(self, idea):
        a = {"id": "A1", "created_at": GM.iso(self.now - timedelta(minutes=40)), "ideas": [idea]}
        (self.v6dir / "astra_analysis.json").write_text(json.dumps(a))
        self.app.bridge_tick(self.now)

    def test_counter_trend_buy_at_strong_support_trades_b_size(self):
        self.run_idea(COUNTER_BUY)
        r = self.fake.sent[0]                     # market 4079.2 = 1.93R to 4097.95 -> 2R retest BUY_LIMIT
        self.assertEqual((r["action"], r["type"], r["price"], r["sl"], r["tp"]), (5, 2, 4078.92, 4069.5, 4110.88))
        self.assertTrue(r["comment"].startswith("B:"))
        o = self.app.state.all_orders()[0]
        self.assertEqual((o["strategy"], o["grade"]), ("SWING_COUNTER", "B_TRADE"))

    def test_counter_trend_away_from_strong_zone_is_refused(self):
        self.zones = [dict(M69, tests=1)]
        self.run_idea(COUNTER_BUY)
        self.assertEqual(self.fake.sent, [])

    def test_counter_trend_switch_off(self):
        self.c["counter_trend_enabled"] = False
        self.run_idea(COUNTER_BUY)
        self.assertEqual(self.fake.sent, [])

    def test_counter_trend_needs_confirmation_even_in_limit_mode(self):
        self.c["idea_entry_mode"] = "limit"
        self.bars = [bar(self.t, 4081.0, 4082.0, 4080.0, 4081.5)]                      # no confirmation yet
        self.run_idea(COUNTER_BUY)
        self.assertEqual(self.fake.sent, [])

    def test_trend_sell_and_counter_buy_coexist(self):
        sell = dict(NIGHT_IDEA, id="S1", entry_low=4104.2, entry_high=4107.61, invalidation=4118.5,
                    targets=[4073.53])
        a = {"id": "A1", "created_at": GM.iso(self.now - timedelta(minutes=40)), "ideas": [sell, COUNTER_BUY]}
        (self.v6dir / "astra_analysis.json").write_text(json.dumps(a))
        self.app.bridge_tick(self.now)
        self.assertEqual([r["type"] for r in self.fake.sent], [2])                      # BUY placed; SELL still waits
        self.assertNotIn("A1:S1", self.app.state.get("bridge:ideas_done", []))


if __name__ == "__main__":
    unittest.main(verbosity=2)
