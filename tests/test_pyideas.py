"""Python's own trade ideas from the signal bot's market memory (autotrader/pyideas.py).
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
except ImportError:
    sys.modules["astra_analysis"] = types.ModuleType("astra_analysis")
sys.modules.setdefault("astra_anthropic", types.SimpleNamespace(model_name=lambda v6: "claude-opus-5-5",
                                                                  EFFORTS=("low", "medium", "high")))
from autotrader import alerts, app as AP, bridge as BR, pyideas as PI, settings  # noqa: E402

UTC = timezone.utc


# ----------------------------------------------------------------------------------------------------------------------
# 1.20 (12 Nov): Python's own trade ideas from the signal bot's market memory - a 10-day Python-only test while the
# Anthropic credit is empty. Same trader rules as Astra's ideas; separate ids / strategies; capped risk.
# ----------------------------------------------------------------------------------------------------------------------
NOW = datetime(2026, 11, 12, 10, 5, tzinfo=UTC)
CFG = dict(PI.DEFAULTS, mode="only", max_tp_r=10.0)


def z(i, tf, role, lo, hi, tests, status="ACTIVE", kind="SR"):
    return {"id": i, "kind": kind, "timeframe": tf, "role": role, "status": status, "low": lo, "high": hi, "tests": tests}


ZONES = [z("M155", "1day", "SUPPORT", 3959.39, 3975.77, 4), z("M156", "1day", "SUPPORT", 3959.39, 3975.77, 4),
         z("M178", "1h", "RESISTANCE", 4062.33, 4066.93, 1), z("M159", "1day", "RESISTANCE", 4110.88, 4117.64, 5),
         z("M140", "4h", "RESISTANCE", 4146.12, 4147.03, 4), z("OLD", "1day", "SUPPORT", 3980.0, 3984.0, 6, "ARCHIVED")]


class IdeasTest(unittest.TestCase):
    def test_buy_at_the_tested_daily_support(self):
        got = PI.ideas(ZONES, 3990.0, 5.0, CFG)
        self.assertEqual(len(got), 1)                                   # no SELL: no tested resistance within 6 ATR
        i = got[0]
        self.assertEqual((i["direction"], i["entry_low"], i["entry_high"], i["source"]), ("BUY", 3959.39, 3975.77, "python"))
        self.assertEqual(i["invalidation"], round(3959.39 - 1.5, 2))
        self.assertEqual(i["targets"], [4062.33, 4110.88])             # TP1 the first opposite zone with >= 2R
        self.assertGreaterEqual(i["rr_tp1"], 2.0)
        self.assertEqual((i["sl_anchor"]["id"], i["tp_anchors"][0]["id"]), ("M155", "M178"))
        self.assertEqual((i["confidence"], i["risk"]), (30, "MEDIUM"))   # no 1H trend given, no confluence
        self.assertIn("1day support", i["title"])
        capped = PI.ideas(ZONES, 3990.0, 5.0, dict(CFG, max_tp_r=4.0))[0]   # 4.84 R to the zone > 4: TP1 at 4 R
        risk = 3975.77 - capped["invalidation"]
        self.assertEqual(capped["targets"], [round(3975.77 + 4 * risk, 2), 4062.33])
        self.assertEqual(capped["tp_anchors"], [])

    def test_sell_mirror_and_limits(self):
        got = PI.ideas(ZONES, 4100.0, 5.0, CFG)
        sell = next(i for i in got if i["direction"] == "SELL")
        self.assertEqual((sell["entry_low"], sell["entry_high"]), (4110.88, 4117.64))
        self.assertEqual(sell["invalidation"], round(4117.64 + 1.5, 2))
        self.assertLess(sell["targets"][0], 4110.88 - 2 * (sell["invalidation"] - 4110.88) + 0.01)
        self.assertEqual(PI.ideas(ZONES, 3976.5, 5.0, CFG), [])       # already at the zone top (< 0.3 ATR away)
        self.assertEqual(PI.ideas([ZONES[0]], 3990.0, 5.0, CFG), [])  # no opposite zone: no 2R target, no idea
        self.assertEqual(PI.ideas(ZONES, 3990.0, 0.0, CFG), [])
        far = dict(CFG, max_distance_atr15=2.0)
        self.assertEqual(PI.ideas(ZONES, 3990.0, 5.0, far), [])
        self.assertEqual(PI.ideas(ZONES, 3990.0, 5.0, dict(CFG, min_tests=5)), [])

    def test_flipped_zone_with_its_new_role(self):
        flipped = [z("F", "4h", "SUPPORT", 4000.0, 4004.0, 3, "FLIPPED"), z("R", "1day", "RESISTANCE", 4060.0, 4070.0, 2)]
        got = PI.ideas(flipped, 4015.0, 4.0, CFG)
        self.assertEqual(got[0]["entry_high"], 4004.0)


class SmcTest(unittest.TestCase):
    EV = {"index": {
        "OB1": {"id": "OB1", "kind": "ORDER_BLOCK_CANDIDATE", "direction": "SELL", "timeframe": "1h", "status": "ACTIVE",
                "level_low": 4004.0, "level_high": 4008.0},
        "FVG1": {"id": "FVG1", "kind": "FVG", "direction": "SELL", "timeframe": "4h", "status": "PARTIALLY_FILLED",
                 "level_low": 4005.0, "level_high": 4012.0},
        "DEAD": {"id": "DEAD", "kind": "FVG", "direction": "SELL", "timeframe": "1h", "status": "FILLED",
                 "level_low": 3994.0, "level_high": 3996.0},
        "BUYOB": {"id": "BUYOB", "kind": "ORDER_BLOCK_CANDIDATE", "direction": "BUY", "timeframe": "1h", "status": "TESTED",
                  "level_low": 3978.0, "level_high": 3981.0}},
        "frames": {"1h": {"recent_swings": [{"side": "LOW", "price": 3940.0}, {"side": "HIGH", "price": 4030.0}]}}}

    def test_sell_from_order_blocks_and_fvgs_with_confluence(self):
        got = PI.ideas(ZONES, 3990.0, 5.0, CFG, self.EV, "BEARISH")
        sell = next(i for i in got if i["direction"] == "SELL")
        self.assertEqual((sell["entry_low"], sell["entry_high"]), (4005.0, 4012.0))     # 4H FVG, overlapped by the 1H OB
        self.assertIn("confluence", sell["title"])
        self.assertEqual((sell["confidence"], sell["risk"]), (40, "MEDIUM"))           # with the 1H trend + confluence
        self.assertEqual(sell["targets"][0], 3981.0)                                    # the BUY order block top, >= 2R
        buy = next(i for i in got if i["direction"] == "BUY")
        self.assertEqual(buy["risk"], "HIGH")                                           # against the 1H trend
        self.assertLessEqual(len(got), 3)
        self.assertEqual(len({(i["direction"], i["entry_low"]) for i in got}), len(got))
        self.assertTrue(any(i["entry_low"] == 4004.0 for i in got if i["direction"] == "SELL")
                        or len([i for i in got if i["direction"] == "SELL"]) == 1)     # the 3rd idea: 2nd-best SELL

    def test_dead_zones_ignored(self):
        got = PI.ideas([], 3990.0, 5.0, CFG, {"index": {"DEAD": self.EV["index"]["DEAD"]}}, None)
        self.assertEqual(got, [])


class _App:
    def __init__(self, cursor):
        self.n = 0
        self.alerts = []
        self.market = types.SimpleNamespace(rates=lambda tf, k: [(int(NOW.timestamp()) // 3600 * 3600 - 3600, 0, 0, 0, 0)],
                                            quote=lambda: {"bid": 3989.9, "ask": 3990.1})
        self.bridge = types.SimpleNamespace(memory_cursor=lambda tf: cursor, memory_zones=self.zones)

    def zones(self):
        self.n += 1
        return ZONES

    def market_context(self):
        return {}, 5.0

    def market_state(self):
        return {"intermediate_trend": "BEARISH"}, {}

    def trading_window_open(self, now):
        return True


class EngineTest(unittest.TestCase):
    def setUp(self):
        self._t, self._e = PI.alerts.trade, PI.alerts.error
        self.sent = []
        PI.alerts.trade = lambda text, short=None: self.sent.append(text)
        PI.alerts.error = lambda text: self.sent.append("ERR " + text)

    def tearDown(self):
        PI.alerts.trade, PI.alerts.error = self._t, self._e

    def test_one_analysis_per_1h_candle(self):
        app = _App(NOW - timedelta(minutes=70))
        e = PI.PyIdeas({"python_ideas": {"mode": "only", "telegram": True}, "idea_max_age_minutes": 200}, app)
        a = e.analysis(NOW)
        self.assertTrue(a["id"].startswith("P2026111209"))
        self.assertEqual(a["ideas"][0]["direction"], "BUY")
        self.assertIs(e.analysis(NOW + timedelta(minutes=10)), a)       # same closed 1H candle: cached
        self.assertEqual(app.n, 1)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("PYTHON IDEAS", self.sent[0])

    def test_stale_memory_drops_its_zones_with_one_alert(self):
        app = _App(NOW - timedelta(hours=5))
        e = PI.PyIdeas({"python_ideas": {"mode": "only"}, "idea_max_age_minutes": 200}, app)
        a = e.analysis(NOW)
        self.assertEqual(a["ideas"], [])                     # memory zones left out; no OB/FVG in this stub either
        self.assertIs(e.analysis(NOW + timedelta(minutes=1)), a)
        self.assertEqual(len([s for s in self.sent if s.startswith("ERR")]), 1)

    def test_modes_and_window(self):
        e = PI.PyIdeas({"python_ideas": {"mode": "fallback"}, "idea_max_age_minutes": 200}, _App(NOW))
        self.assertTrue(e.replaces(None, None, NOW))
        self.assertTrue(e.replaces({"id": "A"}, 300, NOW))
        self.assertFalse(e.replaces({"id": "A"}, 30, NOW))
        e = PI.PyIdeas({"python_ideas": {"mode": "only", "until": "2026-11-22"}, "idea_max_age_minutes": 200}, _App(NOW))
        self.assertTrue(e.replaces({"id": "A"}, 30, NOW))
        self.assertTrue(e.active(datetime(2026, 11, 22, 23, tzinfo=UTC)))
        self.assertFalse(e.active(datetime(2026, 11, 23, 0, 1, tzinfo=UTC)))
        self.assertTrue(any("test ended" in s for s in self.sent))
        self.assertFalse(PI.PyIdeas({"python_ideas": {"mode": "off"}}, _App(NOW)).active(NOW))

    def test_bridge_serves_python_analysis(self):
        b = BR.Bridge.__new__(BR.Bridge)
        b.astra_analysis = lambda now: ({"id": "A1", "created_at": "2026-11-11T16:28:00Z"}, 1060.0)
        b.python = types.SimpleNamespace(replaces=lambda a, age, now: True,
                                         analysis=lambda now: {"id": "P1", "created_at": "2026-11-12T10:00:00Z", "ideas": []})
        a, age = b.analysis(NOW)
        self.assertEqual(a["id"], "P1")
        self.assertAlmostEqual(age, 5.0)
        b.python = types.SimpleNamespace(replaces=lambda a, age, now: False, analysis=None)
        self.assertEqual(b.analysis(NOW)[0]["id"], "A1")


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
        self.assertEqual(self._load()["python_ideas"]["mode"], "off")
        c = self._load(python_ideas={"mode": "only", "until": "2026-11-22"})
        self.assertEqual((c["python_ideas"]["risk_pct"], c["python_ideas"]["until"]), (None, "2026-11-22"))
        for kw in ({"mode": "live"}, {"until": "22 Nov"}, {"timeframes": ["15min"]}, {"min_rr": 1.5},
                   {"risk_pct": 2.0}, {"max_ideas": 0}, {"x": 1}):
            with self.assertRaises(ValueError, msg=str(kw)):
                self._load(python_ideas=dict({"mode": "only"}, **kw))


# ----------------------------------------------------------------------------------------------------------------------
# 1.20.3 (13 Nov): less noise - no hourly Python-idea Telegram by default, one entry per Python zone per 12 h,
# and no Python limit whose TP1 price has almost reached already.
# ----------------------------------------------------------------------------------------------------------------------
NOW_13NOV = datetime(2026, 11, 13, 16, 0, tzinfo=UTC)


class IdeaNoiseTest(unittest.TestCase):
    def setUp(self):
        self._t, self._e = PI.alerts.trade, PI.alerts.error
        self.sent = []
        PI.alerts.trade = lambda text, short=None: self.sent.append(text)
        PI.alerts.error = lambda text: None

    def tearDown(self):
        PI.alerts.trade, PI.alerts.error = self._t, self._e

    def test_no_hourly_telegram_by_default(self):
        e = PI.PyIdeas({"python_ideas": {"mode": "only"}, "idea_max_age_minutes": 200}, _App(NOW_13NOV - timedelta(minutes=70)))
        self.assertTrue(e.analysis(NOW_13NOV)["ideas"])
        self.assertEqual(self.sent, [])
        e = PI.PyIdeas({"python_ideas": {"mode": "only", "telegram": True}, "idea_max_age_minutes": 200},
                       _App(NOW_13NOV - timedelta(minutes=70)))
        e.analysis(NOW_13NOV)
        self.assertEqual(len(self.sent), 1)

    def test_target_almost_reached_is_skipped(self):
        cfg = dict(PI.DEFAULTS, max_tp_r=10.0)
        sell_zone = [{"id": "R", "kind": "SR", "timeframe": "4h", "role": "RESISTANCE", "status": "ACTIVE",
                      "low": 3909.06, "high": 3924.72, "tests": 2},
                     {"id": "S1", "kind": "SR", "timeframe": "1h", "role": "SUPPORT", "status": "ACTIVE",
                      "low": 3868.0, "high": 3872.16, "tests": 1},
                     {"id": "S2", "kind": "SR", "timeframe": "4h", "role": "SUPPORT", "status": "ACTIVE",
                      "low": 3850.0, "high": 3859.0, "tests": 2}]
        sell = next(i for i in PI.ideas(sell_zone, 3873.49, 5.0, cfg) if i["direction"] == "SELL")
        self.assertEqual(sell["targets"][0], 3859.0)                 # not 3872.16, 1.3 below price
        far = next(i for i in PI.ideas(sell_zone, 3890.0, 5.0, cfg) if i["direction"] == "SELL")
        self.assertEqual(far["targets"][0], 3872.16)


class ReentryTest(unittest.TestCase):
    def _me(self, rows, hours=12):
        me = AP.App.__new__(AP.App)
        me.pyideas = types.SimpleNamespace(cfg={"reentry_hours": hours})
        me.state = types.SimpleNamespace(setup_orders_since=lambda sid, since, lab=False:
                                         [r for r in rows if r["setup_id"] == sid and r["created_utc"] >= since])
        return me

    def test_one_entry_per_zone_per_12h(self):
        rows = [{"setup_id": "PY-SELL-3909", "created_utc": "2026-11-12T19:25:03Z", "fill_utc": "2026-11-13T06:20:00Z"},
                {"setup_id": "PY-BUY-3873", "created_utc": "2026-11-13T14:00:01Z", "fill_utc": None}]
        me = self._me(rows)
        self.assertIsNone(me.python_zone_traded("PY-SELL-3909", NOW_13NOV))          # created 20.6 h ago: outside 12 h
        rows[0]["created_utc"] = "2026-11-13T06:00:00Z"
        self.assertIn("already traded", me.python_zone_traded("PY-SELL-3909", NOW_13NOV))
        self.assertIsNone(me.python_zone_traded("PY-BUY-3873", NOW_13NOV))           # placed but never filled: may retry
        self.assertIsNone(self._me(rows, hours=0).python_zone_traded("PY-SELL-3909", NOW_13NOV))


if __name__ == "__main__":
    unittest.main()
