"""The SELL_4H_WEAKNESS lab engine (autotrader/weakness.py) and its parity with tools/weakness_replay.py.
Offline: no MT5, no network, no Claude, no Telegram."""

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent / "src", HERE.parent / "tools", HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from autotrader import alerts, lab as LB, weakness as WK  # noqa: E402

UTC = timezone.utc


# ----------------------------------------------------------------------------------------------------------------------
# 1.12: the SELL_4H_WEAKNESS lab engine (autotrader/weakness.py). Offline, no MT5, no Telegram.
# Parity with tools/weakness_replay.py, setup creation, cancels, entries, the BUY mirror, gates and settings.
# ----------------------------------------------------------------------------------------------------------------------
T0 = 1_793_000_000 - (1_793_000_000 % 14400)          # a 4H boundary
NOW = datetime.fromtimestamp(T0 + 40 * 14400, UTC)


def bars(closes, step, start=T0, wick=0.4):
    """Doji candles (open = close) so the fractal pivots are exactly the closes +/- wick."""
    return WK.Bars([start + i * step for i in range(len(closes))], list(closes),
                   [c + wick for c in closes], [c - wick for c in closes], list(closes))


def reflect(b: WK.Bars, k: float) -> WK.Bars:
    return WK.Bars(b.t, [k - x for x in b.o], [k - x for x in b.l], [k - x for x in b.h], [k - x for x in b.c])


H1_CLOSES = [4120, 4110, 4105, 4100, 4105, 4110, 4120, 4125, 4120, 4115, 4120, 4125, 4130, 4135]   # swing lows 4099.6, 4114.6
M5_BEARISH = [4130 + 2 * v for v in (1, 2, 3, 2, 1, 2, 4, 5, 4, 3, 2, 1, 0)]                    # last break: down
M5_BULLISH = [4130 + 2 * v for v in (1, 2, 3, 2, 1, 2, 4, 5, 6)]


def entry_frames(m5=M5_BEARISH):
    flat = [4130.0] * 40
    m5 = [4128.0] * 30 + list(m5)
    return {5: bars(m5, 300, NOW.timestamp() - 300 * len(m5)), 15: bars(flat, 900), 30: bars(flat, 1800),
            60: bars([4140.0] * 20 + H1_CLOSES, 3600, NOW.timestamp() - 3600 * (20 + len(H1_CLOSES))), 240: bars(flat, 14400),
            1440: bars(flat, 86400)}


class _State:
    def __init__(self):
        self.kv, self.active, self.updates, self.since = {}, [], [], []

    def get(self, k, default=None):
        return self.kv.get(k, default)

    def put(self, k, v):
        self.kv[k] = v

    def active_orders(self, include_lab=True):
        return self.active

    def setup_orders_since(self, sid, since, lab=False):
        return [o for o in self.since if o == sid]

    def update_order(self, ticket, **f):
        self.updates.append((ticket, f))


class _App:
    def __init__(self, block=None, news=None, hold=None):
        self.state = _State()
        self.sent = []
        si = types.SimpleNamespace(trade_stops_level=10, point=0.01)
        self.market = types.SimpleNamespace(symbol_info=lambda: si, news_block=lambda now: news,
                                            quote=lambda: self.quote)
        self.quote = {"bid": 4140.0, "ask": 4140.3, "spread": 0.3, "age": 1}
        self.lab = types.SimpleNamespace(hard_block=lambda now: block, size=lambda si, stop: (0.01, round(stop, 2), hold))
        self.executor = types.SimpleNamespace(send=lambda order, lots, risk, grade, magic=None:
                                              (self.sent.append((order, lots, risk, magic)), 777)[1])


C = {"lab_mode": True, "magic_lab": 40003, "signal_entry_tolerance": 1.0, "max_entry_deviation_cap": 3.0,
     "sessions": None, "weakness_lab": {"mode": "lab"}}


class Base(unittest.TestCase):
    def setUp(self):
        self.msgs = []
        self._t = alerts.trade
        alerts.trade = lambda msg, short=None: self.msgs.append(msg)

    def tearDown(self):
        alerts.trade = self._t

    def engine(self, app=None, **cfg):
        app = app or _App()
        c = dict(C, weakness_lab=dict({"mode": "lab"}, **cfg))
        e = WK.WeaknessLab(c, app)
        return e, app

    def setup(self, level=4140.0, stop=4150.0, born=None, **extra):
        born = born or NOW - timedelta(hours=1)
        return dict(level=level, stop=stop, line=4148.0, signal_bar=T0 + 39 * 14400, tf=15, kind="FVG", waiting=False,
                    touched=False, atr=10.0, created=born.strftime("%Y-%m-%dT%H:%M:%SZ"), **extra)


class ParityTest(unittest.TestCase):
    """The live copy of the rules must answer exactly like the replay that measured them."""

    def test_same_lines_and_zones_as_the_replay_on_synthetic_history(self):
        import weakness_replay as WR
        m1 = WR.synthetic(150, seed=11)
        p = WR.Params()
        frames = WR.build_frames(m1, p)
        conv = {tf: WK.Bars(b.t, b.o, b.h, b.l, b.c) for tf, b in frames.items()}
        q = WK.Params()
        hits = 0
        for k in range(15, len(frames[240])):
            a, b = WR.weakness_run(frames[240], k, p), WK.weakness_run(conv[240], k, q)
            self.assertEqual(a, b)
            if a:
                hits += 1
                first, line, bottom, atr = a
                until = frames[240].t[k] + 14400
                za = WR.find_zone(frames, p, frames[240].t[first], until, bottom - p.zone_slack_atr * atr, line)
                zb = WK.find_zone(conv, q, conv[240].t[first], until, bottom - q.zone_slack_atr * atr, line)
                self.assertEqual(za, zb)
        self.assertGreater(hits, 0)
        for k in (100, 400, 900):
            self.assertEqual(WR.structure_trend(frames[5], k), WK.structure_trend(conv[5], k))
            self.assertEqual(WR.swing_lows(frames[60], k), WK.swing_lows(conv[60], k))


class SetupTest(Base):
    def frames_with_line(self):
        rise = [100.0 + i for i in range(20)]
        h4 = bars(rise, 14400)
        for (o, h, l, c) in ((119, 123.0, 118.5, 122.8), (122.8, 123.1, 120, 120.5), (120.5, 122.9, 119.5, 122.7)):
            h4.t.append(h4.t[-1] + 14400); h4.o.append(o); h4.h.append(h); h4.l.append(l); h4.c.append(c)
        first = h4.t[20]
        m15 = WK.Bars([first + i * 900 for i in range(6)], [123.0, 122.95, 122.0, 122.0, 122.1, 122.2],
                      [123.05, 122.95, 122.45, 122.3, 122.3, 122.4], [122.95, 121.9, 121.8, 121.9, 122.0, 122.0],
                      [123.0, 122.0, 122.0, 122.1, 122.2, 122.3])
        flat = bars([121.0] * 40, 300, first)
        return {240: h4, 15: m15, 60: bars([121.0] * 30, 3600, first), 30: bars([121.0] * 30, 1800, first),
                5: flat, 1440: bars([110.0] * 30, 86400)}, h4

    def test_three_4h_highs_on_a_line_plus_an_fvg_in_the_zone_make_a_setup(self):
        e, app = self.engine()
        e.frames, h4 = self.frames_with_line()
        app.quote = {"bid": 121.0, "ask": 121.3, "spread": 0.3, "age": 1}
        now = datetime.fromtimestamp(h4.t[-1] + 14400 + 5, UTC)
        self.assertIsNone(e.side_tick("SELL", now, app.quote, True, False, False))
        st = e.setups["SELL"]
        self.assertEqual((st["tf"], st["kind"]), (15, "FVG"))
        self.assertAlmostEqual(st["level"], (122.95 + 122.45) / 2)
        self.assertGreater(st["stop"], 123.1)
        self.assertIn("SELL", app.state.kv["weakness_setups"])            # survives a restart
        self.assertTrue(any("4H WEAKNESS SELL setup" in m for m in self.msgs))
        self.assertEqual(app.sent, [])                                      # a setup is not an order

    def test_no_line_no_setup_and_an_open_trade_blocks_a_new_one(self):
        e, app = self.engine()
        e.frames, h4 = self.frames_with_line()
        app.state.active = [{"setup_id": "W4H-SELL-122", "status": "OPEN"}]
        now = datetime.fromtimestamp(h4.t[-1] + 14400 + 5, UTC)
        e.side_tick("SELL", now, {"bid": 121.0, "ask": 121.3, "spread": 0.3}, True, False, False)
        self.assertNotIn("SELL", e.setups)


class LifecycleTest(Base):
    def test_touch_with_bearish_5m_sells_at_market_tp1_at_first_1h_low(self):
        e, app = self.engine()
        e.frames = entry_frames()
        e.setups["SELL"] = self.setup()
        out = e.side_tick("SELL", NOW, app.quote, False, False, False)
        self.assertEqual(out, "W4H-SELL-4140: sent #777")
        order, lots, risk, magic = app.sent[0]
        self.assertEqual((order["direction"], order["order_type"], magic), ("SELL", "MARKET", 40003))
        self.assertEqual((order["entry_price"], order["stop_loss"], order["take_profit_1"]), (4140.0, 4150.0, 4114.6))
        self.assertIsNone(order["take_profit_2"])                                    # lab: whole lot at TP1
        self.assertIn("TP2 4099.60", order["rationale"])
        self.assertEqual(order["rr"], 2.54)
        self.assertEqual(order["strategy"], "LAB_WEAKNESS")
        self.assertIn("weakness_4h", order["meta"]["violations"])
        self.assertIn("zone_fvg_15m", order["meta"]["violations"])
        self.assertEqual(order["meta"]["lab"], 1)
        self.assertEqual(app.state.updates[0][1]["exit_utc"], (NOW + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertNotIn("SELL", e.setups)                                           # one entry per setup

    def test_touch_with_bullish_5m_waits_then_a_bearish_5m_close_below_enters(self):
        e, app = self.engine()
        e.frames = entry_frames(M5_BULLISH)
        e.setups["SELL"] = self.setup(stop=4146.0)
        self.assertIsNone(e.side_tick("SELL", NOW, app.quote, False, False, False))
        self.assertTrue(e.setups["SELL"]["touched"])
        e.frames = entry_frames()                                                   # 5M turned bearish, close 4130 < level
        app.quote = {"bid": 4138.0, "ask": 4138.3, "spread": 0.3, "age": 1}        # 8 risk, 23.4 to 4114.6 = 2.9 R
        out = e.side_tick("SELL", NOW, app.quote, False, False, True)
        self.assertTrue(out and "sent" in out)
        self.assertIn("entry_5m_close", app.sent[0][0]["meta"]["violations"])

    def test_first_support_under_2r_is_no_trade(self):
        e, app = self.engine()
        e.frames = entry_frames()
        e.setups["SELL"] = self.setup(stop=4155.0)                                   # 25.4 / 15 = 1.69 R
        self.assertIsNone(e.side_tick("SELL", NOW, app.quote, False, False, False))
        self.assertEqual(app.sent, [])
        self.assertNotIn("SELL", e.setups)

    def test_stop_traded_before_entry_ends_the_setup(self):
        e, app = self.engine()
        e.frames = entry_frames()
        e.setups["SELL"] = self.setup(stop=4140.5)                                   # 5M high 4140.4 + bid above
        app.quote = {"bid": 4141.0, "ask": 4141.3, "spread": 0.3, "age": 1}
        e.side_tick("SELL", NOW, app.quote, False, False, False)
        self.assertNotIn("SELL", e.setups)
        self.assertEqual(app.sent, [])

    def test_4h_close_above_the_level_or_expiry_ends_it(self):
        e, app = self.engine()
        e.frames = entry_frames()
        e.frames[240] = bars([4130.0] * 39 + [4145.0], 14400)
        e.setups["SELL"] = self.setup()
        e.side_tick("SELL", NOW, {"bid": 4120.0, "ask": 4120.3, "spread": 0.3}, True, False, False)
        self.assertNotIn("SELL", e.setups)
        e.frames[240] = bars([4130.0] * 60, 14400)
        e.setups["SELL"] = self.setup()                                              # signal_bar = candle 39; last = 59
        e.side_tick("SELL", NOW, {"bid": 4120.0, "ask": 4120.3, "spread": 0.3}, True, False, False)
        self.assertNotIn("SELL", e.setups)

    def test_15m_close_above_waits_and_back_below_rearms(self):
        e, app = self.engine()
        e.frames = entry_frames()
        e.frames[15] = bars([4130.0] * 39 + [4142.0], 900)
        e.setups["SELL"] = self.setup()
        e.side_tick("SELL", NOW, app.quote, False, True, False)
        self.assertTrue(e.setups["SELL"]["waiting"])
        self.assertEqual(app.sent, [])
        e.frames[15] = bars([4130.0] * 40, 900)
        app.quote = {"bid": 4130.0, "ask": 4130.3, "spread": 0.3, "age": 1}
        e.side_tick("SELL", NOW, app.quote, False, True, False)
        self.assertFalse(e.setups["SELL"]["waiting"])

    def test_news_lock_waits_without_ending(self):
        e, app = self.engine(app=_App(news="CPI m/m at 13:30"))
        e.frames = entry_frames()
        e.setups["SELL"] = self.setup()
        self.assertIsNone(e.side_tick("SELL", NOW, app.quote, False, False, False))
        self.assertIn("SELL", e.setups)
        self.assertEqual(app.sent, [])

    def test_buy_mirror_prices_come_back_unmirrored(self):
        e, app = self.engine(sides=["BUY"])
        e.frames = {tf: reflect(b, 8280.0) for tf, b in entry_frames().items()}
        e.setups["BUY"] = self.setup(level=-4140.0, stop=-4130.0)                    # sell space of a BUY at 4140
        app.quote = {"bid": 4139.7, "ask": 4140.0, "spread": 0.3, "age": 1}
        out = e.side_tick("BUY", NOW, app.quote, False, False, False)
        self.assertEqual(out, "W4H-BUY-4140: sent #777")
        o = app.sent[0][0]
        self.assertEqual((o["direction"], o["entry_price"], o["stop_loss"], o["take_profit_1"]), ("BUY", 4140.0, 4130.0, 4165.4))


class GateTest(Base):
    def test_lab_hard_block_holds_the_entry_and_keeps_the_setup(self):
        e, app = self.engine(app=_App(block="kill file STOP present"))
        e.frames = entry_frames()
        e.setups["SELL"] = self.setup()
        self.assertIsNone(e.side_tick("SELL", NOW, app.quote, False, False, False))
        self.assertEqual(app.sent, [])
        self.assertIn("SELL", e.setups)

    def test_market_closed_or_mode_off_does_nothing(self):
        e, app = self.engine()
        app.market.sessions = types.SimpleNamespace(is_open=lambda now: False)
        e.refresh = lambda: (_ for _ in ()).throw(AssertionError("must not load candles"))
        self.assertEqual(e.tick(NOW), [])
        e2 = WK.WeaknessLab(dict(C, weakness_lab={"mode": "off"}), _App())
        self.assertFalse(e2.live)
        self.assertEqual(e2.tick(NOW), [])
        e3 = WK.WeaknessLab(dict(C, lab_mode=False), _App())
        self.assertFalse(e3.live)

    def test_tick_loads_broker_candles_once_per_5m_bar_and_enters(self):
        e, app = self.engine()
        fr = entry_frames()
        calls = []
        name_tf = {v: k for k, v in WK.TF_NAMES.items()}

        def rates(name, n):
            calls.append(name)
            b = fr[name_tf[name]]
            rows = list(zip(b.t, b.o, b.h, b.l, b.c))
            return rows[-n:]
        app.market.rates = rates
        app.market.sessions = types.SimpleNamespace(is_open=lambda now: True)
        e.setups["SELL"] = self.setup()
        self.assertEqual(e.tick(NOW), ["W4H-SELL-4140: sent #777"])
        loads = len(calls)
        self.assertEqual(e.tick(NOW), [])                                            # same 5M bar: no reload
        self.assertEqual(len(calls), loads + 1)                                      # only the 1-candle key probe

    def test_a_broker_failure_is_reported_once_and_never_retried(self):
        e, app = self.engine()
        errors = []
        self._e = alerts.error
        alerts.error = errors.append
        try:
            app.executor.send = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("broker rejected: retcode=10016"))
            e.frames = entry_frames()
            e.setups["SELL"] = self.setup()
            out = e.side_tick("SELL", NOW, app.quote, False, False, False)
            self.assertIn("broker", out)
            self.assertNotIn("SELL", e.setups)
            self.assertEqual(len(errors), 1)
            self.assertIsNone(e.side_tick("SELL", NOW, app.quote, False, False, False))
        finally:
            alerts.error = self._e

    def test_lab_size_hold_sends_nothing(self):
        e, app = self.engine(app=_App(hold="lab open risk would pass the cap"))
        e.frames = entry_frames()
        e.setups["SELL"] = self.setup()
        self.assertIsNone(e.side_tick("SELL", NOW, app.quote, False, False, False))
        self.assertEqual(app.sent, [])

    def test_already_traded_today_is_not_repeated(self):
        e, app = self.engine()
        e.frames = entry_frames()
        e.setups["SELL"] = self.setup()
        app.state.since = ["W4H-SELL-4140"]
        self.assertIsNone(e.side_tick("SELL", NOW, app.quote, False, False, False))
        self.assertEqual(app.sent, [])

    def test_lab_duplicates_ignore_the_weakness_engine(self):
        working = [{"lab": 1, "setup_id": "W4H-SELL-4140", "direction": "SELL", "entry": 4140.0, "sl": 4150.0, "ticket": 1}]
        self.assertIsNone(LB.duplicate_of({"direction": "SELL", "entry_price": 4140.0, "stop_loss": 4150.0}, working, 5.0))


class SettingsTest(unittest.TestCase):
    def setUp(self):
        from autotrader import settings as ST
        self.ST = ST
        self._lc = ST.GM.load_config
        ST.GM.load_config = lambda p: {"minimum_rr": 2.0, "block_on_mtf_opposition": True,
                                       "provider_options": {"broker_symbol": "XAUUSD"}}
        sys.modules.setdefault("astra_anthropic", types.SimpleNamespace(model_name=lambda v6: "claude-opus-5-5",
                                                                          EFFORTS=("low", "medium", "high")))

    def tearDown(self):
        self.ST.GM.load_config = self._lc

    def _load(self, **custom):
        import json
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(custom, f)
            path = f.name
        try:
            return self.ST.load(path)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_default_off_and_lab_loads(self):
        self.assertEqual(self._load()["weakness_lab"]["mode"], "off")
        c = self._load(lab_mode=True, weakness_lab={"mode": "lab"})
        self.assertEqual(c["weakness_lab"]["sides"], ["SELL"])
        self.assertEqual(c["weakness_lab"]["stop_atr"], 0.1)

    def test_bad_values_fail_at_start(self):
        with self.assertRaises(ValueError):
            self._load(weakness_lab={"mode": "lab"})                                 # lab_mode off
        for bad in ({"mode": "live"}, {"sides": []}, {"sides": ["LONG"]}, {"min_rr": 1.5}, {"zone_types": ["OB", "X"]},
                    {"zone_tfs": [240]}, {"min_run": 0}, {"stop_atr": -1}, {"typo": 1}):
            with self.assertRaises(ValueError, msg=str(bad)):
                self._load(lab_mode=True, weakness_lab=dict({"mode": "lab"}, **bad))


if __name__ == "__main__":
    unittest.main()
