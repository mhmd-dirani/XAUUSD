"""news_mode "strict": no time lock around releases; only market entries on a strong 15M candle at reduced risk.
Offline: no MT5, no network, no Claude, no Telegram."""

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent / "src", HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:
    import astra_analysis  # noqa: F401,E402
except ImportError:                                   # the offline sandbox has no V6 analysis module
    sys.modules["astra_analysis"] = types.ModuleType("astra_analysis")
from autotrader import executor as EX, market as MK, news as NW, settings, validator as VA  # noqa: E402

UTC = timezone.utc


# ----------------------------------------------------------------------------------------------------------------------
# 1.17: news_mode "strict" - no time lock around releases; inside the old window only market entries on a strong
# 15M candle that is not the release candle, at most news_strict_risk_pct. A stale calendar still blocks.
# ----------------------------------------------------------------------------------------------------------------------
REL = datetime(2026, 11, 10, 13, 30, tzinfo=UTC)          # CPI


def ts(h, m):
    return int(datetime(2026, 11, 10, h, m, tzinfo=UTC).timestamp())


class _M:
    def __init__(self, window="CPI m/m at 2026-11-10T13:30:00+00:00", bars=None, rel=REL):
        self.window, self.bars, self.rel = window, bars or [], rel

    def news_window(self, now):
        return self.window

    def rates(self, tf, n):
        return self.bars[-n:]

    def last_release(self, now, lookback):
        return self.rel


STRICT = {"news_mode": "strict"}
UP = (ts(13, 45), 4060.0, 4079.0, 4059.5, 4078.0)            # strong bullish candle after the release
PREV = (ts(13, 30), 4075.0, 4076.0, 4052.0, 4065.0)           # the release candle


class HoldTest(unittest.TestCase):
    def test_strong(self):
        self.assertTrue(NW.strong("BUY", UP, PREV))
        self.assertFalse(NW.strong("SELL", UP, PREV))
        doji = (ts(13, 45), 4064.0, 4071.0, 4058.0, 4066.0)
        self.assertFalse(NW.strong("BUY", doji, PREV))

    def test_lock_mode_and_no_window_change_nothing(self):
        order = {"order_type": "BUY_LIMIT", "direction": "BUY"}
        self.assertIsNone(NW.hold_reason(_M(), {"news_mode": "lock"}, order, REL))
        self.assertIsNone(NW.hold_reason(_M(window=None), STRICT, order, REL))

    def test_no_new_resting_orders_in_the_window(self):
        r = NW.hold_reason(_M(bars=[PREV, UP]), STRICT, {"order_type": "BUY_LIMIT", "direction": "BUY"}, REL)
        self.assertIn("only market entries", r)

    def test_release_candle_is_not_an_indicator(self):
        before = (ts(13, 15), 4070.0, 4072.0, 4069.0, 4071.0)
        r = NW.hold_reason(_M(bars=[before, PREV]), STRICT, {"order_type": "MARKET", "direction": "SELL"},
                           REL + timedelta(minutes=16))
        self.assertIn("release candle", r)

    def test_market_on_a_strong_candle_after_the_release_passes(self):
        order = {"order_type": "MARKET", "direction": "BUY"}
        self.assertIsNone(NW.hold_reason(_M(bars=[PREV, UP]), STRICT, order, REL + timedelta(minutes=31)))
        r = NW.hold_reason(_M(bars=[PREV, UP]), STRICT, {"order_type": "MARKET", "direction": "SELL"},
                           REL + timedelta(minutes=31))
        self.assertIn("not a strong SELL", r)

    def test_before_the_release_a_strong_candle_counts(self):
        a = (ts(13, 0), 4080.0, 4081.0, 4074.0, 4075.0)
        b = (ts(13, 15), 4075.0, 4075.5, 4066.0, 4067.0)
        r = NW.hold_reason(_M(bars=[a, b], rel=None), STRICT, {"order_type": "MARKET", "direction": "SELL"},
                           REL - timedelta(minutes=14))
        self.assertIsNone(r)

    def test_straddle_exempt(self):
        o = {"order_type": "BUY_STOP", "direction": "BUY", "meta": {"playbook": "NEWS_STRADDLE"}}
        self.assertIsNone(NW.hold_reason(_M(bars=[PREV, UP]), STRICT, o, REL))

    def test_executor_send_raises_news_hold(self):
        x = EX.Executor.__new__(EX.Executor)
        x.c, x.m = STRICT, _M(bars=[PREV, UP])
        with self.assertRaises(NW.NewsHold):
            x.send({"order_type": "SELL_LIMIT", "direction": "SELL", "setup_id": "X"}, 0.01, 5.0, "B_TRADE")


class MarketTest(unittest.TestCase):
    def setUp(self):
        self._cb = MK.GM.calendar_block

    def tearDown(self):
        MK.GM.calendar_block = self._cb

    def _m(self, mode, block):
        m = MK.Market.__new__(MK.Market)
        m.c, m.v6 = {"news_mode": mode}, {"news_after_minutes": 30}
        m.calendar = {"events": [{"title": "CPI m/m", "impact": "High", "time": "2026-11-10T13:30:00Z"}]}
        MK.GM.calendar_block = lambda cal, now, v6: block
        return m

    def test_strict_window_does_not_block(self):
        m = self._m("strict", "CPI m/m at 2026-11-10T13:30:00Z")
        self.assertIsNone(m.news_block(REL))
        self.assertEqual(m.news_window(REL), "CPI m/m at 2026-11-10T13:30:00Z")
        self.assertEqual(self._m("lock", "CPI m/m at x").news_block(REL), "CPI m/m at x")

    def test_stale_calendar_still_blocks(self):
        m = self._m("strict", "Economic calendar unavailable or stale")
        self.assertEqual(m.news_block(REL), "Economic calendar unavailable or stale")
        self.assertIsNone(m.news_window(REL))

    def test_lock_end_is_the_release_candle_in_strict(self):
        self.assertEqual(self._m("strict", None).last_lock_end(REL + timedelta(minutes=5)), REL + timedelta(minutes=1))
        self.assertEqual(self._m("lock", None).last_lock_end(REL + timedelta(minutes=40)), REL + timedelta(minutes=30))
        self.assertEqual(self._m("strict", None).last_release(REL + timedelta(minutes=5)), REL)


class LotsCapTest(unittest.TestCase):
    def test_risk_capped_inside_a_window(self):
        v = VA.Validator.__new__(VA.Validator)
        v.c = {"news_mode": "strict", "news_strict_risk_pct": 0.25, "risk_percent": {"B_TRADE": 0.5},
               "tiers": {"A": {"risk_pct": 1.0}, "B": {"risk_pct": 0.5}}, "max_total_risk_pct": 2.0, "lab_mode": False}
        si = types.SimpleNamespace(trade_tick_size=0.01, trade_tick_value=1.0, volume_step=0.01, volume_max=100,
                                   volume_min=0.01)
        v.m = types.SimpleNamespace(account=lambda: types.SimpleNamespace(equity=10000.0), symbol_info=lambda: si,
                                    news_window=lambda now: "CPI")
        v.open_risk = lambda ex=None: (0.0, None)
        lots, risk = v.lots({"entry_price": 4000.0, "stop_loss": 3990.0}, "B_TRADE", risk_pct=0.5)
        self.assertEqual(lots, 0.02)                                  # 0.25% of 10,000 = 25 / ($10 x 100 oz/lot)
        v.m.news_window = lambda now: None
        lots, risk = v.lots({"entry_price": 4000.0, "stop_loss": 3990.0}, "B_TRADE", risk_pct=0.5)
        self.assertEqual(lots, 0.05)


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
        self.assertEqual(self._load()["news_mode"], "lock")
        self.assertEqual(self._load(news_mode="strict")["news_mode"], "strict")
        for kw in ({"news_mode": "off"}, {"news_strict_risk_pct": 2.0}, {"news_strict_risk_pct": 0}):
            with self.assertRaises(ValueError, msg=str(kw)):
                self._load(**kw)


if __name__ == "__main__":
    unittest.main()
