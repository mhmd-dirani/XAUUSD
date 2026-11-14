"""1.20.3 (13 Nov): less noise - no tiny breakeven re-moves, no hourly Python-idea Telegram by default, one entry per
Python zone per 12 h, and no Python limit whose TP1 price has almost reached already."""
from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
for p in (HERE.parent / "src", HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

try:
    import astra_analysis  # noqa: F401,E402
except ImportError:
    sys.modules["astra_analysis"] = types.ModuleType("astra_analysis")
sys.modules.setdefault("astra_anthropic", types.SimpleNamespace(model_name=lambda v6: "claude-opus-5-5",
                                                                  EFFORTS=("low", "medium", "high")))
from autotrader import alerts, app as AP, pyideas as PI  # noqa: E402
import test_v115 as T115  # noqa: E402
from test_v122 import ZONES, _App  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 11, 13, 16, 0, tzinfo=UTC)


class ProtectStepTest(unittest.TestCase):
    def setUp(self):
        self._trade = alerts.trade
        alerts.trade = lambda *a, **k: None

    def tearDown(self):
        alerts.trade = self._trade

    def test_breakeven_is_not_re_moved_by_cents(self):
        pr, app, pos, rec = T115._app(4045.0)
        pr.tick(datetime(2026, 11, 10, 15, 0, tzinfo=UTC))
        self.assertEqual(app.executor.moves, [4034.31])
        app.market.spread = 0.36                                      # breakeven is now entry + 0.36: 6 cents better
        pr.tick(datetime(2026, 11, 10, 15, 1, tzinfo=UTC))
        self.assertEqual(app.executor.moves, [4034.31])              # 13 Nov: this was one more move + message
        app.market.bid = 4049.0                                      # the lock stage is a real step: still moved
        pr.tick(datetime(2026, 11, 10, 15, 2, tzinfo=UTC))
        self.assertEqual(len(app.executor.moves), 2)


class IdeaNoiseTest(unittest.TestCase):
    def setUp(self):
        self._t, self._e = PI.alerts.trade, PI.alerts.error
        self.sent = []
        PI.alerts.trade = lambda text, short=None: self.sent.append(text)
        PI.alerts.error = lambda text: None

    def tearDown(self):
        PI.alerts.trade, PI.alerts.error = self._t, self._e

    def test_no_hourly_telegram_by_default(self):
        e = PI.PyIdeas({"python_ideas": {"mode": "only"}, "idea_max_age_minutes": 200}, _App(NOW - timedelta(minutes=70)))
        self.assertTrue(e.analysis(NOW)["ideas"])
        self.assertEqual(self.sent, [])
        e = PI.PyIdeas({"python_ideas": {"mode": "only", "telegram": True}, "idea_max_age_minutes": 200},
                       _App(NOW - timedelta(minutes=70)))
        e.analysis(NOW)
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
        self.assertIsNone(me.python_zone_traded("PY-SELL-3909", NOW))          # created 20.6 h ago: outside 12 h
        rows[0]["created_utc"] = "2026-11-13T06:00:00Z"
        self.assertIn("already traded", me.python_zone_traded("PY-SELL-3909", NOW))
        self.assertIsNone(me.python_zone_traded("PY-BUY-3873", NOW))           # placed but never filled: may retry
        self.assertIsNone(self._me(rows, hours=0).python_zone_traded("PY-SELL-3909", NOW))


if __name__ == "__main__":
    unittest.main()
