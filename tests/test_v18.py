#!/usr/bin/env python3
"""1.8 BREAKOUT_CONTINUATION: the candidate rules, the tier cap, the replay variant and the live path.
Offline (no MT5, no Claude)."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import gold_monitor as GM  # noqa: E402
from autotrader import breakout as BK, replay as RP, settings, tiers as TR  # noqa: E402
from autotrader.app import App  # noqa: E402
from test_auto_trader import Base, bar, evidence  # noqa: E402

UTC = timezone.utc
CFG = dict(RP.DEFAULTS, min_rr=2.0, sl_atr_min=0.5, sl_atr_max=5.0, max_spread=0.5, spread_stop_fraction=0.05)
STATE = {"macro_trend": "BEARISH", "intermediate_trend": "BEARISH", "trend_1day": "BEARISH", "trend_4h": "BEARISH",
         "local_trend": "BEARISH", "phase": "CONTINUATION", "expansion": {"active": True, "direction": "BEARISH"},
         "_frames": {"15min": {"events": [], "swings": []}, "1h": {"trend": "BEARISH", "swings": []}}}
T0 = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)
# 23 Oct 14:00 candle: first close below the NY opening-range low 4021.58 (o 4023.64 h 4023.64 l 4007.44 c 4008.20)
EVENT = {"event": "BREAKOUT", "direction": "SELL", "level": "NEW_YORK_OR_LOW", "price": 4021.58,
         "bar_time": GM.iso(T0), "high": 4023.64, "low": 4007.44, "close": 4008.2}
BAR = bar(T0, 4023.64, 4023.64, 4007.44, 4008.2)
LOWS = [3986.6, 3959.39]           # Asia low, then the June/July support
HIGHS = [4034.06, 4048.29]


class CandidateTests(unittest.TestCase):
    def cand(self, **over):
        kw = dict(e=EVENT, bar=BAR, state=STATE, atr15=8.0, highs=HIGHS, lows=LOWS, as_of=T0 + timedelta(minutes=15),
                  session="NEW_YORK", sp=0.3, cfg=CFG)
        kw.update(over)
        why = []
        c = BK.candidate(**kw, drop=why.append)
        return c, why

    def test_23_oct_collapse_is_a_market_sell_with_the_stop_at_the_level(self):
        c, why = self.cand()
        self.assertIsNotNone(c, why)
        self.assertEqual((c["direction"], c["order_type"], c["how"]), ("SELL", "MARKET", "BREAKOUT"))
        self.assertEqual(c["entry"], 4008.2)
        self.assertEqual(c["stop"], 4023.98)                                       # level + max(0.3 ATR, 2 spread)
        self.assertEqual((c["tp1"], c["tp2"]), (3959.39, None))                   # first level >= 2R; 3986.6 on the path
        self.assertEqual(c["path_levels"], [3986.6])
        self.assertAlmostEqual(c["rr_now"], (4008.2 - 3959.39) / (4023.98 - 4008.2), places=2)

    def test_limit_at_2r_price_when_the_close_gives_less(self):
        c, why = self.cand(lows=[3990.0, 3980.0])                                  # 3980: 28.2 / 15.78 = 1.79R < 2.02
        self.assertIsNotNone(c, why)
        self.assertEqual(c["order_type"], "SELL_LIMIT")
        self.assertGreater(c["entry"], 4008.2)
        self.assertLess(c["entry"], c["stop"])
        self.assertEqual(c["expiry"], T0 + timedelta(minutes=15 + 60))

    def test_refusals(self):
        self.assertIsNone(self.cand(state=dict(STATE, macro_trend="BULLISH"))[0])
        self.assertIsNone(self.cand(state=dict(STATE, intermediate_trend="BULLISH"))[0])
        self.assertIsNone(self.cand(session="ASIA")[0])
        self.assertIsNone(self.cand(e=dict(EVENT, event="SWEEP_RECLAIM"))[0])
        c, why = self.cand(lows=[4006.0])                                          # inside half a risk
        self.assertIsNone(c)
        self.assertIn("no opposing level", why[0])
        c, why = self.cand(bar=bar(T0, 4023.0, 4023.0, 3960.0, 3962.0))            # ran 60 points from the level
        self.assertIsNone(c)
        self.assertIn("sl_atr_max", why[0])

    def test_tier_is_capped_at_c_for_the_playbook(self):
        c = settings.load(None)
        idea = {"confidence": 60, "astra_risk": "MEDIUM", "sl_anchor": {"id": "x"}, "tp_anchors": [{"id": "y"}]}
        dec = {"trade_class": "BREAKOUT_CONTINUATION", "expansion": {"against": False}, "would_block": False}
        t = TR.grade(idea, "SELL", dec, STATE, "NEW_YORK", c)
        self.assertEqual((t["tier"], t["risk_pct"]), ("C", 0.15))                 # class != TREND: C anyway
        c["tier_cap"] = {"TREND": "C"}
        t = TR.grade(idea, "SELL", dict(dec, trade_class="TREND"), STATE, "NEW_YORK", c)
        self.assertEqual(t["tier"], "C")
        self.assertIn("capped at C", t["reason"])
        self.assertEqual(c["playbook_modes"]["BREAKOUT_CONTINUATION"], "off")

    def test_settings_validation(self):
        d = Path(tempfile.mkdtemp())
        for bad in ('{"tier_cap": {"NOPE": "C"}}', '{"tier_cap": {"TREND": "D"}}', '{"breakout_retest_minutes": 1}'):
            (d / "c.json").write_text(bad)
            with self.assertRaises(ValueError, msg=bad):
                settings.load(d / "c.json")


class TradeSessionTests(Base):
    def test_no_new_orders_outside_trade_sessions(self):
        from autotrader import sessions as SS
        from autotrader.validator import Validator
        self.m.connect()
        v = Validator(self.c, self.m, self.state)
        with patch.object(self.m, "trading_window", return_value=None), patch.object(self.m, "news_block", return_value=None):
            self.assertIsNone(v.gates(datetime.now(UTC)))                        # harness pins NEW_YORK
            with patch.object(SS, "session_of", return_value="ASIA"):
                self.assertIn("session ASIA is not in trade_sessions", v.gates(datetime.now(UTC)))
                self.c["trade_sessions"] = []
                self.assertIsNone(v.gates(datetime.now(UTC)))                    # [] = every session
        self.assertEqual(settings.load(None)["trade_sessions"], ["LONDON", "LONDON_NY", "NEW_YORK"])
        d = Path(tempfile.mkdtemp())
        (d / "c.json").write_text('{"trade_sessions": ["TOKYO"]}')
        with self.assertRaises(ValueError):
            settings.load(d / "c.json")


class ReplayVariantTests(unittest.TestCase):
    def test_breakout_candidates_only_in_breakout_variants(self):
        self.assertIn("BREAKOUT_CONTINUATION", RP.VARIANTS["NEW_all_plus_breakout"][2])
        self.assertNotIn("BREAKOUT_CONTINUATION", RP.VARIANTS["NEW_all"][2])
        self.assertEqual(RP.VARIANTS["BREAKOUT_only"][2], {"BREAKOUT_CONTINUATION": "live"})
        self.assertEqual(RP.VARIANTS["CORE_noAsia"][5], ("LONDON", "LONDON_NY", "NEW_YORK"))
        self.assertEqual(RP.VARIANTS["CORE_noAsia_noRejection"][6], ("SWEEP", "CHOCH", "BREAKOUT"))
        self.assertIsNone(RP.VARIANTS["CORE"][5])
        rp = RP.Replay.__new__(RP.Replay)
        rp.cfg, rp.dropped = CFG, []
        rp._drop = lambda why: rp.dropped.append(why)
        rp.sim = None
        lv = [{"name": "ASIA_LOW", "side": "LOW", "price": 3986.6}, {"name": "PWL", "side": "LOW", "price": 3959.39}]
        out = rp._candidates(BAR, [BAR], STATE, lv, [EVENT], 8.0, T0 + timedelta(minutes=15), "NEW_YORK")
        self.assertEqual([(c["how"], c["tp1"]) for c in out], [("BREAKOUT", 3959.39)])
        self.assertEqual(rp.dropped, [])


class LiveBreakoutTests(Base):
    def setUp(self):
        super().setUp()
        self.c["playbook_modes"]["BREAKOUT_CONTINUATION"] = "live"
        self.v6dir = Path(tempfile.mkdtemp())
        self.c["v6"] = dict(self.c["v6"], state_directory=str(self.v6dir))
        self.app = App(self.c)
        self.app.market = self.m
        self.app.validator.m = self.app.executor.m = self.m
        self.m.connect()
        self.now = datetime.now(UTC)                                               # the validator uses the real clock
        t0 = self.now - timedelta(minutes=16)
        self.bar0 = bar(t0, 4023.64, 4023.64, 4007.44, 4008.2)
        self.event = dict(EVENT, bar_time=GM.iso(t0))
        self.bars = [self.bar0]
        self.snap = {"session": "NEW_YORK", "levels": [{"name": "NEW_YORK_OR_LOW", "side": "LOW", "price": 4021.58,
                                                          "formed_at": GM.iso(T0 - timedelta(hours=1)), "source": "SESSION"},
                                                         {"name": "ASIA_LOW", "side": "LOW", "price": 3986.6,
                                                          "formed_at": GM.iso(T0 - timedelta(hours=10)), "source": "SESSION"},
                                                         {"name": "PWL", "side": "LOW", "price": 3959.39,
                                                          "formed_at": GM.iso(T0 - timedelta(days=5)), "source": "PREV_WEEK"}],
                     "events": [self.event]}
        for x in (patch.object(self.m, "trading_window", return_value=None),
                  patch.object(self.m, "news_block", return_value=None),
                  patch.object(self.app, "market_context", return_value=(evidence(), 8.0)),
                  patch.object(self.app, "market_state", side_effect=lambda: (STATE, self.snap)),
                  patch.object(self.app, "recent_bars", side_effect=lambda: self.bars)):
            x.start()
            self.addCleanup(x.stop)
        self.fake.add_tick(4008.0, 4008.3)

    def test_breakout_becomes_a_tier_c_market_sell_once_per_candle(self):
        (self.v6dir / "astra_analysis.json").write_text(json.dumps({"id": "A1", "created_at": GM.iso(self.now), "ideas": []}))
        self.app.bridge_tick(self.now)
        self.assertEqual(len(self.fake.sent), 1)
        o = self.app.state.all_orders()[0]
        self.assertEqual((o["direction"], o["setup_id"], o["playbook"], o["confirmation"], o["tier"], o["strategy"]),
                         ("SELL", "BK-NEW_YORK_OR_LOW-SELL-4022", "BREAKOUT_CONTINUATION", "BREAKOUT", "C", "SWING_BREAKOUT"))
        self.assertEqual((o["sl"], o["tp1"]), (4023.98, 3959.39))
        self.assertEqual(o["lots"], 0.01)                                         # Tier C, one minimum lot
        self.app.bridge_tick(self.now + timedelta(seconds=30))                      # same candle: nothing new
        self.assertEqual(len(self.fake.sent), 1)

    def test_off_by_default_and_against_trend_refused(self):
        self.c["playbook_modes"]["BREAKOUT_CONTINUATION"] = "off"
        self.app.bridge_tick(self.now)
        self.assertEqual(self.fake.sent, [])
        self.c["playbook_modes"]["BREAKOUT_CONTINUATION"] = "live"
        self.app._bk_bar = None
        self.snap["events"] = [dict(self.event, direction="BUY", level="NEW_YORK_OR_HIGH", price=4030.0)]
        self.app.bridge_tick(self.now)
        self.assertEqual(self.fake.sent, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
