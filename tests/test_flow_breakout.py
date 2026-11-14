"""The order-flow record on every order, the breakout lab, USD holidays as normal days and two loop fixes.
Offline: no MT5, no network, no Claude, no Telegram."""

from __future__ import annotations

import sys
import time
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent / "src", HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:
    import astra_analysis  # noqa: F401,E402
except ImportError:
    sys.modules["astra_analysis"] = types.ModuleType("astra_analysis")
sys.modules.setdefault("astra_anthropic", types.SimpleNamespace(model_name=lambda v6: "claude-opus-5-5",
                                                                  EFFORTS=("low", "medium", "high")))
from autotrader import app as AP, breakout as BK, executor as EX, flow as FL, market as MK  # noqa: E402
from autotrader import news as NW, settings  # noqa: E402
from autotrader.validator import Rejected  # noqa: E402

UTC = timezone.utc


# ----------------------------------------------------------------------------------------------------------------------
# 1.19 (11 Nov): order-flow record (5M/1M structure + tick volume) on every order, the breakout lab (breakouts
# against the trend at the minimum lot), USD holidays as normal days, and two loop fixes (a held idea re-validated every
# 2 s; a stale quote killing a live idea).
# ----------------------------------------------------------------------------------------------------------------------
NOW = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)


def zig(n, drift, t0=1_794_000_000, step=60, vol=100.0, seed=3):
    import math
    import random
    rnd = random.Random(seed)
    out, px = [], 4000.0
    for i in range(n):
        o = px
        c = o + drift + 1.2 * math.sin(i / 1.7) + rnd.gauss(0, 0.1)
        out.append((t0 + step * i, o, max(o, c) + rnd.uniform(0.05, 0.4), min(o, c) - rnd.uniform(0.05, 0.4), c, vol))
        px = c
    return out


class FlowTest(unittest.TestCase):
    def test_ratio_and_read(self):
        bars = [(i, 10, 11, 9, 10.5, 100.0) for i in range(20)] + [(20, 10, 12, 10, 11.9, 250.0)]
        self.assertEqual(FL.ratio(bars), 2.5)
        self.assertIsNone(FL.ratio(bars[:10]))
        self.assertEqual(FL.read(bars[-1], 2.5), "EXPANSION")
        self.assertEqual(FL.read((0, 10, 12, 9, 10.2), 2.0), "ABSORPTION")
        self.assertEqual(FL.read((0, 10, 12, 9, 11.5), 0.5), "THIN")
        self.assertEqual(FL.read((0, 10, 12, 9, 11.5), 1.0), "NORMAL")
        self.assertIsNone(FL.read((0, 10, 12, 9, 11.5), None))

    def test_label(self):
        self.assertEqual(FL.label("BUY", "BULLISH", "BULLISH"), "WITH")
        self.assertEqual(FL.label("SELL", "BULLISH", "BULLISH"), "AGAINST")
        self.assertEqual(FL.label("SELL", "BEARISH", "NONE"), "MIXED")

    def test_measure_from_closed_candles(self):
        up = zig(80, 0.6)
        m = types.SimpleNamespace(rates_v=lambda tf, n: up[-n:])
        f = FL.measure(m, "SELL")
        self.assertEqual((f["tr5"], f["tr1"], f["flow"]), ("BULLISH", "BULLISH", "AGAINST"))
        self.assertEqual(f["vol15"], 1.0)
        self.assertIn("15M volume 1.0x", FL.text(f))

    def test_executor_records_and_never_raises(self):
        x = EX.Executor.__new__(EX.Executor)
        got = {}
        x.state = types.SimpleNamespace(update_order=lambda t, **f: got.update(f))
        x.m = types.SimpleNamespace(rates_v=lambda tf, n: zig(80, -0.6)[-n:])
        x._flow_tag(1, {"direction": "SELL", "setup_id": "S"})
        self.assertEqual(got["flow"], "WITH")
        x.m = types.SimpleNamespace(rates_v=lambda tf, n: (_ for _ in ()).throw(RuntimeError("no candles")))
        x._flow_tag(1, {"direction": "SELL", "setup_id": "S"})          # logged, not raised


def real_like_block(cal, now, v6):
    """The V6 rule: stale -> block; a holiday on the day first; then a major release inside the window."""
    if not cal or cal.get("stale"):
        return "Economic calendar unavailable or stale"
    for e in cal["events"]:
        if e.get("event_type") == "HOLIDAY":
            return "USD holiday (all day): " + e["title"]
    for e in cal["events"]:
        t = datetime.fromisoformat(e["time"].replace("Z", "+00:00"))
        if -30 <= (t - now).total_seconds() / 60 <= 30:
            return e["title"] + " at " + e["time"]
    return None


class HolidayTest(unittest.TestCase):
    def setUp(self):
        self._cb = MK.GM.calendar_block
        MK.GM.calendar_block = real_like_block

    def tearDown(self):
        MK.GM.calendar_block = self._cb

    def _m(self, holiday, events, stale=False):
        m = MK.Market.__new__(MK.Market)
        m.c, m.v6 = {"news_mode": "strict", "holiday_trading": holiday}, {}
        m.calendar = {"events": events, "stale": stale}
        return m

    def test_normal_holiday_is_a_normal_day_but_releases_count(self):
        hol = {"event_type": "HOLIDAY", "title": "Veterans Day", "calendar_date": "2026-11-11"}
        cpi = {"title": "CPI m/m", "impact": "High", "time": "2026-11-11T15:10:00Z"}
        self.assertIsNone(self._m("normal", [hol]).news_window(NOW))
        self.assertEqual(self._m("normal", [hol, cpi]).news_window(NOW), "CPI m/m at 2026-11-11T15:10:00Z")
        self.assertEqual(self._m("block", [hol]).news_window(NOW), "USD holiday (all day): Veterans Day")
        self.assertEqual(self._m("normal", [hol], stale=True).news_block(NOW), "Economic calendar unavailable or stale")
        m = self._m("block", [hol])
        m.c["news_mode"] = "lock"
        self.assertEqual(m.news_block(NOW), "USD holiday (all day): Veterans Day")     # the old rule, unchanged


class BreakoutTest(unittest.TestCase):
    def _args(self, macro="BULLISH", h1="BULLISH"):
        e = {"event": "BREAKOUT", "direction": "SELL", "price": 4013.56, "level": "NEW_YORK_LOW"}
        bar = {"close": 4010.0, "low": 4009.0, "high": 4016.0}
        state = {"macro_trend": macro, "intermediate_trend": h1}
        cfg = {"sl_atr_min": 0.5, "sl_atr_max": 3.0, "min_rr": 2.0, "max_spread": 0.6, "spread_stop_fraction": 0.05}
        return e, bar, state, 4.0, [4030.0], [3995.0, 3980.0], NOW, "NEW_YORK", 0.2, cfg

    def test_against_tags_instead_of_dropping(self):
        why = []
        self.assertIsNone(BK.candidate(*self._args(), drop=why.append))
        self.assertIn("against the macro trend", why[0])
        tags = []
        c = BK.candidate(*self._args(), against=tags)
        self.assertEqual(tags, ["against_macro", "against_1h"])
        mixed = []
        self.assertIsNotNone(BK.candidate(*self._args("MIXED", "BEARISH"), against=mixed))
        self.assertEqual(mixed, ["macro_mixed"])                          # 1.20.1: only the Daily disagrees
        self.assertEqual(c["direction"], "SELL")
        self.assertGreater(c["stop"], 4013.56)
        tags = []
        self.assertIsNotNone(BK.candidate(*self._args("BEARISH", "BEARISH"), against=tags))
        self.assertEqual(tags, [])                                         # with the trend: no tag (the live rule)
        args = list(self._args())
        args[7] = "ASIA"
        self.assertIsNone(BK.candidate(*args, against=[]))                  # every other rule unchanged

    def test_lab_send(self):
        sent, alerts_ = [], []
        rows = []

        def send(order, lots, risk, grade, magic=None):
            sent.append((order, lots, magic))
            rows.append({"ticket": 77, "flow": "WITH", "tr5": "BEARISH", "tr1": "BEARISH", "vol15": 2.1,
                         "vol_read": "EXPANSION"})
            return 77
        lab = types.SimpleNamespace(hard_block=lambda now: None, hedge_with_working=lambda o, m: None,
                                    size=lambda si, d: (0.01, 5.0, None))
        me = types.SimpleNamespace(c={"breakout_lab": {"mode": "lab", "sides": ["BUY", "SELL"]}, "magic_lab": 40003,
                                      "signal_entry_tolerance": 1.0, "max_entry_deviation_cap": 1.0},
                                   lab=lab, executor=types.SimpleNamespace(send=send),
                                   state=types.SimpleNamespace(setup_orders_since=lambda *a, **k: [], all_orders=lambda: rows),
                                   market=types.SimpleNamespace(symbol_info=lambda: None))
        tags = []
        cand = BK.candidate(*self._args(), against=tags)
        old = AP.alerts.trade
        AP.alerts.trade = lambda text, short=None: alerts_.append(text)
        try:
            out = AP.App.breakout_lab(me, cand, {"level": "NEW_YORK_LOW"}, tags, {"phase": "X"}, {"session": "NEW_YORK"},
                                      4.0, {"spread": 0.2}, NOW)
        finally:
            AP.alerts.trade = old
        self.assertEqual(out, "LBK-NEW_YORK_LOW-SELL-4014: sent #77")
        order, lots, magic = sent[0]
        self.assertEqual((lots, magic, order["strategy"]), (0.01, 40003, "LAB_BREAKOUT"))
        self.assertEqual(order["meta"]["violations"], ["against_macro", "against_1h"])
        self.assertIn("15M volume 2.1x EXPANSION", alerts_[0])
        me.c["breakout_lab"]["sides"] = ["BUY"]
        self.assertIsNone(AP.App.breakout_lab(me, cand, {"level": "NEW_YORK_LOW"}, tags, {}, {}, 4.0, {"spread": 0.2}, NOW))


class LoopFixTest(unittest.TestCase):
    def _app(self, exc):
        me = AP.App.__new__(AP.App)
        me.c = {"close_delay_seconds": 5}
        me.idea_logged, me._paused_ids, me.dry_run = set(), {}, False
        me.validator = types.SimpleNamespace(gates=lambda now: None,
                                             validate=lambda *a, **k: {"setup_id": "IDEA-SELL-4024", "order_type": "SELL_LIMIT"},
                                             lots=lambda *a, **k: (0.01, 5.0))
        if isinstance(exc, Rejected):
            me.validator.validate = lambda *a, **k: (_ for _ in ()).throw(exc)
        me.executor = types.SimpleNamespace(send=lambda *a, **k: (_ for _ in ()).throw(exc))
        return me

    def test_news_hold_waits_for_the_next_15m_close(self):
        me = self._app(NW.NewsHold("news window (x): only market entries"))
        done = []
        out = me.place_orders([{"grade": "B_TRADE", "setup_id": "IDEA-SELL-4024"}], {}, 4.0, on_rejected=done.append)
        until = me._paused_ids["IDEA-SELL-4024"]
        self.assertEqual((until - 5) % 900, 0)
        self.assertGreater(until, time.time())
        self.assertEqual(done, [])
        self.assertIn("news window", out[0])

    def test_stale_quote_keeps_the_idea(self):
        me = self._app(Rejected("broker quote 16s old"))
        done = []
        out = me.place_orders([{"grade": "B_TRADE", "setup_id": "IDEA-SELL-4024"}], {}, 4.0, on_rejected=done.append)
        self.assertEqual(done, [])
        self.assertIn("(retry)", out[0])
        self.assertLessEqual(me._paused_ids["IDEA-SELL-4024"] - time.time(), 30.5)
        me = self._app(Rejected("R:R 1.2 < 2.0"))
        me.place_orders([{"grade": "B_TRADE", "setup_id": "IDEA-SELL-4024"}], {}, 4.0, on_rejected=done.append)
        self.assertEqual(len(done), 1)                                      # a real rejection still ends the idea


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self._lc = settings.GM.load_config
        settings.GM.load_config = lambda p: {"minimum_rr": 2.0, "block_on_mtf_opposition": True,
                                             "provider_options": {"broker_symbol": "XAUUSD"}}
        sys.modules.setdefault("astra_anthropic", types.SimpleNamespace(model_name=lambda v6: "claude-opus-5-5",
                                                                          EFFORTS=("low", "medium", "high")))

    def tearDown(self):
        settings.GM.load_config = self._lc

    def _load(self, **custom):
        import json
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(custom, f)
            path = f.name
        try:
            return settings.load(path)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_values(self):
        c = self._load()
        self.assertEqual((c["holiday_trading"], c["breakout_lab"]["mode"]), ("block", "off"))
        self.assertEqual(self._load(lab_mode=True, breakout_lab={"mode": "lab"})["breakout_lab"]["sides"], ["BUY", "SELL"])
        for kw in ({"holiday_trading": "skip"}, {"breakout_lab": {"mode": "live"}}, {"breakout_lab": {"mode": "lab"}},
                   {"lab_mode": True, "breakout_lab": {"mode": "lab", "sides": []}}, {"breakout_lab": {"x": 1}}):
            with self.assertRaises(ValueError, msg=str(kw)):
                self._load(**kw)


if __name__ == "__main__":
    unittest.main()
