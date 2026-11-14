"""1.13.1: (a) a kept lab limit is dropped only when a newer map CONTRADICTS it (lab_drop_rule), (b) the 1H-trend
retest engine registers LTF watches from the evidence's 1H gaps/blocks. Offline, no MT5, no Telegram."""
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

from autotrader import alerts, h1trend as H1, lab as LB, ltf as LT  # noqa: E402
from test_v1111 import _Bridge, _State, _X, lab_rec  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 11, 6, 5, 35, tzinfo=UTC)

# 6 Nov 03:13: LAB-SELL-4075, SELL_LIMIT 4073.04 sl 4086.40 tp1 4046.08, TP1 touched at 03:33
SELL_REC = dict(lab_rec(), ticket=60330621, setup_id="LAB-SELL-4075", direction="SELL", entry=4073.04, sl=4086.40,
                tp1=4046.08, cancel_below=None, tp_touched_utc="2026-11-06T03:33:00Z")
BUY_IDEA_FAR = {"id": "I3", "direction": "BUY", "entry_low": 4057.45, "entry_high": 4063.77, "invalidation": 4047.38}
BUY_IDEA_AT_ZONE = {"id": "I9", "direction": "BUY", "entry_low": 4070.0, "entry_high": 4076.0, "invalidation": 4060.0}
SELL_IDEA_SAME = {"id": "I1b", "direction": "SELL", "entry_low": 4073.04, "entry_high": 4077.19, "invalidation": 4086.4}


class ContradictedTest(unittest.TestCase):
    def test_only_an_opposite_idea_at_the_zone_contradicts(self):
        self.assertIsNone(LB.contradicted(SELL_REC, [BUY_IDEA_FAR], 3.0))                 # a buy elsewhere: no
        self.assertIsNone(LB.contradicted(SELL_REC, [SELL_IDEA_SAME], 3.0))               # same direction: no
        self.assertIsNone(LB.contradicted(SELL_REC, [], 3.0))                             # omitted: no
        self.assertEqual(LB.contradicted(SELL_REC, [BUY_IDEA_AT_ZONE], 3.0)["id"], "I9")  # a buy from 4070-4076: yes
        self.assertIsNone(LB.contradicted(SELL_REC, [{"direction": "BUY"}], 3.0))         # garbage: no


class RecheckRuleTest(unittest.TestCase):
    def setUp(self):
        self._t = alerts.trade
        alerts.trade = lambda msg, short=None: None

    def tearDown(self):
        alerts.trade = self._t

    def lab_with(self, ideas, rule):
        x = _X()
        analysis = {"id": "A0533", "created_at": "2026-11-06T05:33:00Z", "ideas": ideas}
        app = types.SimpleNamespace(market=None, state=_State([dict(SELL_REC)]), validator=None, executor=x,
                                    bridge=_Bridge(analysis, 2), market_context=lambda: ({}, 5.0))
        c = {"lab_mode": True, "lab_keep_after_tp_touch": True, "lab_tp_touch_remap": False, "idea_max_age_minutes": 200,
             "duplicate_atr": 0.5, "confirm_entry_tolerance": 3.0, "lab_drop_rule": rule}
        return LB.Lab(c, app), x

    def test_the_6_nov_case_the_omitted_sell_is_kept(self):
        lab, x = self.lab_with([BUY_IDEA_FAR], "contradicted")                 # the 05:33 map: buys elsewhere, no sell
        self.assertEqual(lab.recheck(NOW), [])
        self.assertEqual(x.cancelled, [])

    def test_an_opposite_idea_at_the_zone_cancels_it(self):
        lab, x = self.lab_with([BUY_IDEA_AT_ZONE], "contradicted")
        self.assertEqual(lab.recheck(NOW), [60330621])
        self.assertIn("opposite", x.cancelled[0][1])

    def test_the_old_rule_still_works_when_chosen(self):
        lab, x = self.lab_with([BUY_IDEA_FAR], "unproposed")
        self.assertEqual(lab.recheck(NOW), [60330621])
        lab, x = self.lab_with([SELL_IDEA_SAME], "unproposed")
        self.assertEqual(lab.recheck(NOW), [])


# ----------------------------------------------------------------------------- 1H trend engine
def bar(t, o, h, l, c):
    return {"time": t, "open": o, "high": h, "low": l, "close": c}


def bars15(closes, wick=0.5):
    return [bar(f"2026-11-06 {8 + i // 4:02d}:{(i % 4) * 15:02d}:00", c, c + wick, c - wick, c) for i, c in enumerate(closes)]


class RulesTest(unittest.TestCase):
    def test_fractal_levels_and_first_target(self):
        b = bars15([4060, 4055, 4050, 4045, 4050, 4055, 4060, 4065, 4060, 4055, 4050, 4055, 4060])
        lv = H1.fractal_levels(b)
        self.assertEqual([(x["side"], x["price"]) for x in lv], [("LOW", 4044.5), ("HIGH", 4065.5)])
        # SELL from the zone edge 4073 with risk 13.4: first level at >= 2 R below (4046.2) -> the 4044.5 swing low
        levels = lv + [{"price": 4035.87, "side": "LOW", "name": "ASIA_LOW"}, {"price": 4050.0, "side": "LOW", "name": "PDL"}]
        t = H1.first_target("SELL", 4073.04, 13.36, levels, 2.0)
        self.assertEqual(t["price"], 4044.5)
        self.assertIsNone(H1.first_target("SELL", 4073.04, 30.0, levels, 2.0))        # nothing 60 below
        self.assertEqual(H1.first_target("BUY", 4030.0, 5.0, levels, 2.0)["price"], 4065.5)

    def test_candidates_are_usable_1h_zones_ahead_of_price(self):
        now = datetime(2026, 11, 6, 9, 0, tzinfo=UTC).timestamp()
        idx = {
            "fvg_sell": {"id": "fvg_sell", "kind": "FVG", "timeframe": "1h", "direction": "SELL", "status": "ACTIVE",
                         "level_low": 4073.04, "level_high": 4083.4, "at": "2026-11-06T02:00:00+00:00"},
            "ob_sell": {"id": "ob_sell", "kind": "ORDER_BLOCK_CANDIDATE", "timeframe": "1h", "direction": "SELL",
                        "status": "ACTIVE", "level_low": 4063.0, "level_high": 4072.0, "at": "2026-11-06T01:00:00+00:00"},
            "filled": {"id": "filled", "kind": "FVG", "timeframe": "1h", "direction": "SELL", "status": "FILLED",
                       "level_low": 4090.0, "level_high": 4095.0, "at": "2026-11-06T02:00:00+00:00"},
            "m15": {"id": "m15", "kind": "FVG", "timeframe": "15min", "direction": "SELL", "status": "ACTIVE",
                    "level_low": 4075.0, "level_high": 4077.0, "at": "2026-11-06T02:00:00+00:00"},
            "buy": {"id": "buy", "kind": "FVG", "timeframe": "1h", "direction": "BUY", "status": "ACTIVE",
                    "level_low": 4030.0, "level_high": 4036.0, "at": "2026-11-06T02:00:00+00:00"},
            "behind": {"id": "behind", "kind": "FVG", "timeframe": "1h", "direction": "SELL", "status": "ACTIVE",
                       "level_low": 4040.0, "level_high": 4044.0, "at": "2026-11-06T02:00:00+00:00"},
            "old": {"id": "old", "kind": "FVG", "timeframe": "1h", "direction": "SELL", "status": "ACTIVE",
                    "level_low": 4078.0, "level_high": 4080.0, "at": "2026-10-20T02:00:00+00:00"},
        }
        z = H1.candidates({"index": idx}, "SELL", ("FVG", "ORDER_BLOCK_CANDIDATE"), 4049.0, 10.0, 4.0, 72 * 3600, now)
        self.assertEqual([x["id"] for x in z], ["ob_sell", "fvg_sell"])                  # nearest first
        z = H1.candidates({"index": idx}, "SELL", ("FVG",), 4049.0, 10.0, 1.0, 72 * 3600, now)
        self.assertEqual(z, [])                                                          # 24 away > 1 ATR


class _LTFStub:
    def __init__(self):
        self.live, self.watches, self.added = True, {}, []

    def add_watch(self, sid, direction, lo, hi, stop, tp1, tags, now, **kw):
        self.added.append((sid, direction, lo, hi, stop, tp1, tags, kw.get("playbook")))
        self.watches[sid] = True
        return True


class EngineTest(unittest.TestCase):
    def app(self, trend="BEARISH", macro="BULLISH", built="b1"):
        idx = {"fvg": {"id": "fvg", "kind": "FVG", "timeframe": "1h", "direction": "SELL", "status": "ACTIVE",
                       "level_low": 4073.04, "level_high": 4083.4, "at": "2026-11-06T02:00:00+00:00"}}
        b = bars15([4060, 4055, 4050, 4045, 4050, 4055, 4060, 4065, 4060, 4055, 4050, 4055, 4049])
        levels = [{"price": 4035.87, "side": "LOW", "name": "ASIA_LOW", "formed_at": "2026-11-06T07:00:00+00:00"}]
        return types.SimpleNamespace(
            ltf=_LTFStub(), market=types.SimpleNamespace(quote=lambda: {"bid": 4049.0, "ask": 4049.3, "spread": 0.3}),
            market_context=lambda: ({"index": idx, "built_at": built}, 10.0),
            market_state=lambda: ({"intermediate_trend": trend, "macro_trend": macro}, {"session": "LONDON", "levels": levels}),
            recent_bars=lambda: b)

    def test_the_6_nov_sell_zone_becomes_a_watch_against_the_macro(self):
        app = self.app()
        e = H1.H1Trend({"lab_mode": True, "h1_trend_lab": {"mode": "lab"}}, app)
        now = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
        self.assertEqual(e.tick(now), ["H1T-SELL-4078"])
        sid, d, lo, hi, stop, tp1, tags, pb = app.ltf.added[0]
        self.assertEqual((d, lo, hi, pb), ("SELL", 4073.04, 4083.4, "H1_TREND_RETEST"))
        self.assertAlmostEqual(stop, 4086.4)                                              # gap top + 0.3 x ATR15
        self.assertEqual(tp1, 4044.5)                                                     # first liquidity at >= 2 R
        self.assertIn("h1_trend", tags)
        self.assertIn("against_macro", tags)
        self.assertEqual(e.tick(now), [])                                                 # same 15M bar: once

    def test_no_1h_trend_or_wrong_side_or_mode_off_registers_nothing(self):
        for kw in (dict(trend="NEUTRAL"), dict(trend=None)):
            app = self.app(**kw)
            self.assertEqual(H1.H1Trend({"lab_mode": True, "h1_trend_lab": {"mode": "lab"}}, app).tick(NOW), [])
        app = self.app()
        self.assertEqual(H1.H1Trend({"lab_mode": True, "h1_trend_lab": {"mode": "lab", "sides": ["BUY"]}}, app).tick(NOW), [])
        self.assertEqual(H1.H1Trend({"lab_mode": True, "h1_trend_lab": {"mode": "off"}}, app).tick(NOW), [])
        app.ltf.live = False
        self.assertFalse(H1.H1Trend({"lab_mode": True, "h1_trend_lab": {"mode": "lab"}}, app).live)

    def test_no_target_at_2r_means_no_watch(self):
        app = self.app()
        app.recent_bars = lambda: bars15([4049.0] * 13)
        app.market_state = lambda: ({"intermediate_trend": "BEARISH", "macro_trend": "BULLISH"}, {"session": "LONDON", "levels": []})
        e = H1.H1Trend({"lab_mode": True, "h1_trend_lab": {"mode": "lab"}}, app)
        self.assertEqual(e.tick(NOW), [])
        self.assertEqual(app.ltf.added, [])


class LTFWatchTest(unittest.TestCase):
    def test_add_watch_checks_the_geometry_and_keeps_the_playbook(self):
        app = types.SimpleNamespace(state=types.SimpleNamespace(get=lambda k, d=None: d, put=lambda k, v: None))
        e = LT.LTF({"lab_mode": True, "ltf_lab": {"mode": "lab"}}, app)
        self.assertTrue(e.add_watch("H1T-SELL-4078", "SELL", 4073.04, 4083.4, 4086.4, 4044.5, ["h1_trend"], NOW,
                                    playbook="H1_TREND_RETEST"))
        self.assertEqual(e.watches["H1T-SELL-4078"]["playbook"], "H1_TREND_RETEST")
        self.assertFalse(e.add_watch("H1T-SELL-4078", "SELL", 4073.04, 4083.4, 4086.4, 4044.5, [], NOW))   # once
        self.assertFalse(e.add_watch("X", "SELL", 4073.04, 4083.4, 4080.0, 4044.5, [], NOW))               # stop inside
        self.assertFalse(e.add_watch("Y", "BUY", 4040.0, 4044.0, 4030.0, 4042.0, [], NOW))                 # tp1 inside


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

    def test_defaults_and_lab(self):
        c = self._load()
        self.assertEqual((c["lab_drop_rule"], c["h1_trend_lab"]["mode"]), ("contradicted", "off"))
        c = self._load(lab_mode=True, ltf_lab={"mode": "lab"}, h1_trend_lab={"mode": "lab"}, lab_drop_rule="unproposed")
        self.assertEqual(c["h1_trend_lab"]["sides"], ["BUY", "SELL"])

    def test_bad_values_fail_at_start(self):
        with self.assertRaises(ValueError):
            self._load(lab_drop_rule="never")
        with self.assertRaises(ValueError):
            self._load(lab_mode=True, h1_trend_lab={"mode": "lab"})                     # needs the LTF engine
        for bad in ({"mode": "live"}, {"sides": []}, {"zone_kinds": ["FVG", "X"]}, {"min_rr": 1.0}, {"typo": 1}):
            with self.assertRaises(ValueError, msg=str(bad)):
                self._load(lab_mode=True, ltf_lab={"mode": "lab"}, h1_trend_lab=dict({"mode": "lab"}, **bad))


if __name__ == "__main__":
    unittest.main()
