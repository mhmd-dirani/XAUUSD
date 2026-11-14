"""1.13: the lab hedge rule (opposite ideas that cancel each other) and the LTF 5M-entry + liquidity sample.
Offline, no MT5, no Telegram."""
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

from autotrader import alerts, lab as LB, ltf as LT  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
T0 = int(NOW.timestamp()) - 300 * 60                 # 60 closed 5M candles before NOW

# 6 Nov 07:58 UTC, the pair that cancelled out
SELL_4062 = {"direction": "SELL", "entry": 4055.77, "sl": 4073.04, "tp1": 4035.87}
BUY_4053 = {"direction": "BUY", "entry": 4055.97, "sl": 4038.48, "tp1": 4073.04}
# the ladder: the sell takes profit exactly where the buy fills
SELL_4056 = {"direction": "SELL", "entry": 4054.49, "sl": 4066.09, "tp1": 4034.06}
BUY_4032 = {"direction": "BUY", "entry": 4034.06, "sl": 4018.46, "tp1": 4056.39}


class HedgeRuleTest(unittest.TestCase):
    def test_the_6_nov_pair_is_a_hedge_and_the_ladder_is_not(self):
        self.assertTrue(LB.opposite_overlap(SELL_4062, BUY_4053))
        self.assertTrue(LB.opposite_overlap(BUY_4053, SELL_4062))
        self.assertFalse(LB.opposite_overlap(SELL_4056, BUY_4032))
        self.assertFalse(LB.opposite_overlap(BUY_4053, dict(BUY_4032)))          # same direction: never a hedge

    def test_macro_decides_which_side_is_kept(self):
        cands = [("I1", SELL_4062), ("I2", BUY_4053)]
        self.assertEqual(set(LB.hedge_skips(cands, "BULLISH")), {"I1"})          # the SELL is against the macro
        self.assertEqual(set(LB.hedge_skips(cands, "BEARISH")), {"I2"})
        self.assertEqual(set(LB.hedge_skips(cands, "MIXED")), {"I1", "I2"})       # no view: neither
        self.assertEqual(set(LB.hedge_skips(cands, None)), {"I1", "I2"})
        self.assertEqual(LB.hedge_skips([("I1", SELL_4056), ("I2", BUY_4032)], "MIXED"), {})
        self.assertIn("macro", LB.hedge_skips(cands, "BULLISH")["I1"])


class _State:
    def __init__(self, active=None):
        self.active, self.kv, self.since = active or [], {}, []

    def active_orders(self, include_lab=True):
        return self.active

    def get(self, k, default=None):
        return self.kv.get(k, default)

    def put(self, k, v):
        self.kv[k] = v

    def setup_orders_since(self, sid, since, lab=False):
        return [o for o in self.since if o == sid]


class _X:
    def __init__(self):
        self.cancelled = []

    def cancel(self, ticket, why, status="CANCELLED"):
        self.cancelled.append((ticket, why))
        return True


def working(direction, entry, sl, tp1, status="PENDING", ticket=1, sid="LAB-X"):
    return {"lab": 1, "ticket": ticket, "setup_id": sid, "direction": direction, "entry": entry, "sl": sl, "tp1": tp1,
            "status": status}


def order(direction, entry, sl, tp1, sid="LAB-NEW"):
    return {"direction": direction, "entry_price": entry, "stop_loss": sl, "take_profit_1": tp1, "setup_id": sid}


class HedgeWithWorkingTest(unittest.TestCase):
    def lab(self, active):
        x = _X()
        app = types.SimpleNamespace(market=None, state=_State(active), validator=None, executor=x)
        return LB.Lab({"lab_mode": True}, app), x

    def test_pending_opposite_against_the_macro_is_cancelled_for_the_macro_side(self):
        lab, x = self.lab([working("SELL", 4055.77, 4073.04, 4035.87)])
        self.assertIsNone(lab.hedge_with_working(order("BUY", 4055.97, 4038.48, 4073.04), "BULLISH"))
        self.assertEqual(len(x.cancelled), 1)

    def test_open_opposite_blocks_the_new_order_even_with_the_macro(self):
        lab, x = self.lab([working("SELL", 4055.77, 4073.04, 4035.87, status="OPEN")])
        self.assertIn("would hedge", lab.hedge_with_working(order("BUY", 4055.97, 4038.48, 4073.04), "BULLISH"))
        self.assertEqual(x.cancelled, [])

    def test_against_the_macro_or_mixed_is_skipped(self):
        lab, x = self.lab([working("SELL", 4055.77, 4073.04, 4035.87)])
        self.assertIn("would hedge", lab.hedge_with_working(order("BUY", 4055.97, 4038.48, 4073.04), "BEARISH"))
        self.assertIn("would hedge", lab.hedge_with_working(order("BUY", 4055.97, 4038.48, 4073.04), "MIXED"))
        self.assertEqual(x.cancelled, [])

    def test_ladder_and_live_orders_are_untouched(self):
        lab, _ = self.lab([working("SELL", 4054.49, 4066.09, 4034.06)])
        self.assertIsNone(lab.hedge_with_working(order("BUY", 4034.06, 4018.46, 4056.39), "MIXED"))
        lab, _ = self.lab([dict(working("SELL", 4055.77, 4073.04, 4035.87), lab=0)])
        self.assertIsNone(lab.hedge_with_working(order("BUY", 4055.97, 4038.48, 4073.04), "MIXED"))


class HedgePlanTest(unittest.TestCase):
    """The pre-pass over one analysis, with the 6 Nov 07:56 ideas at the 07:58 price."""
    C = {"lab_mode": True, "confirm_entry_tolerance": 0.0, "confirm_retest_hours": 4, "max_order_lifetime_hours": 48,
         "idea_order_hours": 12, "signal_entry_tolerance": 1.0, "max_entry_deviation_cap": 3.0,
         "lab_keep_after_tp_touch": True}
    SELL = {"id": "I1", "direction": "SELL", "entry_low": 4058.24, "entry_high": 4066.27, "invalidation": 4073.04,
            "targets": [4035.87]}
    BUY = {"id": "I2", "direction": "BUY", "entry_low": 4051.88, "entry_high": 4054.48, "invalidation": 4038.48,
           "targets": [4073.04]}

    def lab(self, macro):
        si = types.SimpleNamespace(trade_stops_level=10, point=0.01)
        market = types.SimpleNamespace(quote=lambda: {"bid": 4055.77, "ask": 4056.07, "spread": 0.3, "age": 1},
                                       symbol_info=lambda: si)
        app = types.SimpleNamespace(market=market, state=_State(), validator=None, executor=_X(),
                                    market_state=lambda: ({"macro_trend": macro}, {"session": "OFF"}))
        return LB.Lab(self.C, app)

    def test_bullish_macro_keeps_the_buy(self):
        self.assertEqual(set(self.lab("BULLISH").hedge_plan({"id": "A"}, [self.SELL, self.BUY], NOW)), {"I1"})

    def test_mixed_macro_takes_neither(self):
        self.assertEqual(set(self.lab("MIXED").hedge_plan({"id": "A"}, [self.SELL, self.BUY], NOW)), {"I1", "I2"})

    def test_one_idea_or_same_direction_is_never_skipped(self):
        self.assertEqual(self.lab("MIXED").hedge_plan({"id": "A"}, [self.BUY], NOW), {})
        self.assertEqual(self.lab("MIXED").hedge_plan({"id": "A"}, [self.BUY, dict(self.BUY, id="I3")], NOW), {})


# ----------------------------------------------------------------------------- LTF
def bars_from(closes, start=T0, wick=0.3):
    return [(start + i * 300, c, c + wick, c - wick, c) for i, c in enumerate(closes)]


# a leg down into a buy zone 4040-4044, then a 5M CHoCH up: lower highs 4047, 4045.5 then a close above 4045.5
LEG = [4060, 4058, 4056, 4054, 4052, 4050, 4048, 4047, 4045, 4044.5, 4045.5, 4044, 4042, 4041, 4043, 4044, 4046.5]
#                                                   ^ swing high 4047 (i=7)   ^ 4045.5 (i=10)      low 4041 (i=13)
PAD = [4062.0] * 20


def leg_bars(extra=None):
    return bars_from(PAD + LEG + (extra or []))


class RulesTest(unittest.TestCase):
    def test_choch_up_after_the_touch(self):
        b = leg_bars()
        born, touch = 20, 20 + 11                                                # the 4044 candle enters the zone
        self.assertIsNone(LT.choch(b[:-1], born, touch, "BUY", 2))               # 4044 close: no break yet
        hit = LT.choch(b, born, touch, "BUY", 2)
        self.assertIsNotNone(hit)
        swing, low = hit
        self.assertAlmostEqual(swing, 4045.5 + 0.3)                              # the last lower high before the zone
        self.assertAlmostEqual(low, 4041 - 0.3)                                  # the leg low after it
        self.assertIsNone(LT.choch(b, born, touch, "SELL", 2))
        # a swing already broken by an earlier close is not a fresh CHoCH
        b2 = b[:-1] + [(b[-1][0], 4044, 4047.0, 4043.7, 4046.5), (b[-1][0] + 300, 4046.5, 4046.8, 4045.5, 4046.0)]
        self.assertIsNone(LT.choch(b2, born, touch, "BUY", 2))

    def test_equal_lows_make_a_pool_and_sweep_or_take_it(self):
        closes = [4050, 4048, 4045, 4040, 4042, 4046, 4044, 4040.05, 4043, 4047, 4049, 4050, 4050, 4050]
        b = bars_from(closes)
        pools = LT.equal_pools(b, 2, 0.5)
        lows = [p for p in pools if p["side"] == "LOW"]
        self.assertEqual(len(lows), 1)
        self.assertAlmostEqual(lows[0]["price"], 4040 - 0.3)
        self.assertFalse(LT.swept(lows[0], b, 0))
        self.assertFalse(LT.taken(lows[0], b, 0))
        b2 = b + [(b[-1][0] + 300, 4050, 4050.3, 4039.0, 4041.0)]               # wick under, close above
        self.assertTrue(LT.swept(lows[0], b2, 0))
        b3 = b + [(b[-1][0] + 300, 4050, 4050.3, 4038.0, 4038.5)]               # close below: taken
        self.assertTrue(LT.taken(lows[0], b3, 0))

    def test_session_levels_become_pools(self):
        b = bars_from([4050.0] * 5)
        lv = [{"name": "PDL", "side": "LOW", "price": 4030.0, "formed_at": datetime.fromtimestamp(T0 + 600, UTC).isoformat()},
              {"name": "bad", "side": "LOW"}]
        p = LT.session_pools(lv, b)
        self.assertEqual([(x["name"], x["price"], x["idx"]) for x in p], [("PDL", 4030.0, 2)])

    def test_liquidity_plan_moves_the_stop_past_a_pool_and_refuses_a_pool_in_the_path(self):
        cfg = dict(LT.DEFAULTS)
        b = bars_from([4046.0] * 10)
        pools = [{"price": 4039.5, "side": "LOW", "idx": 0, "name": "PDL"}]
        stop, tags, skip = LT.liquidity_plan("BUY", 4046.0, 4040.0, 4070.0, pools, b, 0, 10.0, 1.0, cfg)
        self.assertEqual((stop, skip), (4038.5, None))                           # pool 0.5 below the stop: stop moved past it
        self.assertIn("stop_beyond_pool", tags)
        self.assertIn("no_sweep", tags)
        pools = [{"price": 4049.0, "side": "HIGH", "idx": 0, "name": "EQHx2"}]  # 3 above a 6-risk entry: in the path
        stop, tags, skip = LT.liquidity_plan("BUY", 4046.0, 4040.0, 4070.0, pools, b, 0, 10.0, 1.0, cfg)
        self.assertIn("pool_in_path", tags)
        self.assertIsNone(skip)                                                  # 1.13.2: tagged, not refused
        stop, tags, skip = LT.liquidity_plan("BUY", 4046.0, 4040.0, 4070.0, pools, b, 0, 10.0, 1.0,
                                             dict(cfg, pool_path_blocks=True))
        self.assertIn("EQHx2", skip)                                             # the old refusal, when chosen
        pools = [{"price": 4060.0, "side": "HIGH", "idx": 0, "name": "EQHx2"}]  # beyond 1 R: fine
        _, tags, skip = LT.liquidity_plan("BUY", 4046.0, 4040.0, 4070.0, pools, b, 0, 10.0, 1.0, cfg)
        self.assertIsNone(skip)

    def test_swept_first_is_recorded_and_can_be_required(self):
        cfg = dict(LT.DEFAULTS)
        b = bars_from([4050.0] * 5) + [(T0 + 1500, 4050, 4050.3, 4037.0, 4045.0)] + bars_from([4046.0] * 3, T0 + 1800)
        pools = [{"price": 4039.0, "side": "LOW", "idx": 0, "name": "ASIA_LOW"}]
        _, tags, skip = LT.liquidity_plan("BUY", 4046.0, 4036.0, 4070.0, pools, b, 0, 10.0, 1.0, cfg)
        self.assertIn("swept_first", tags)
        self.assertIsNone(skip)
        _, tags, skip = LT.liquidity_plan("BUY", 4046.0, 4036.0, 4070.0, [], b, 0, 10.0, 1.0, dict(cfg, require_sweep=True))
        self.assertIn("no_sweep", tags)
        self.assertIn("require_sweep", skip)


class _App:
    def __init__(self, bars, macro="BULLISH", levels=None, block=None, hold=None):
        self.state = _State()
        self.sent, self._bars = [], bars
        si = types.SimpleNamespace(trade_stops_level=10, point=0.01)
        self.news, self.next_event = None, None
        self.market = types.SimpleNamespace(symbol_info=lambda: si, quote=lambda: self.quote,
                                            rates=lambda name, n: self._bars[-n:],
                                            sessions=types.SimpleNamespace(is_open=lambda now: True),
                                            news_block=lambda now: self.news,                    # 1.14 recheck
                                            next_major_event=lambda now, minutes: self.next_event)
        self.quote = {"bid": 4046.5, "ask": 4046.8, "spread": 0.3, "age": 1}
        self.lab = LB.Lab({"lab_mode": True}, types.SimpleNamespace(market=self.market, state=self.state, validator=None,
                                                                    executor=types.SimpleNamespace(cancel=lambda *a: True)))
        self.lab.hard_block = lambda now: block
        self.lab.size = lambda si, stop: (0.01, round(stop, 2), hold)
        self._macro, self._levels = macro, levels or []
        self.executor = types.SimpleNamespace(send=lambda order, lots, risk, grade, magic=None:
                                              (self.sent.append((order, lots, risk, magic)), 777)[1])

    def market_context(self):
        return {"index": {}}, 10.0

    def market_state(self):
        return {"macro_trend": self._macro}, {"session": "LONDON", "levels": self._levels}


C = {"lab_mode": True, "magic_lab": 40003, "signal_entry_tolerance": 1.0, "max_entry_deviation_cap": 3.0,
     "confirm_entry_tolerance": 0.0, "ltf_lab": {"mode": "lab"}}
IDEA = {"id": "I1", "direction": "BUY", "entry_low": 4040.0, "entry_high": 4044.0, "invalidation": 4030.0,
        "targets": [4075.0], "title": "buy the pullback"}
PLAIN = {"setup_id": "LAB-BUY-4042", "take_profit_1": 4075.0}


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.msgs = []
        self._t, self._e = alerts.trade, alerts.error
        alerts.trade = lambda msg, short=None: self.msgs.append(msg)
        alerts.error = lambda msg: self.msgs.append("ERR " + msg)

    def tearDown(self):
        alerts.trade, alerts.error = self._t, self._e

    def engine(self, bars, **kw):
        app = _App(bars, **kw)
        return LT.LTF(C, app), app

    def run_leg(self, e, app, created_offset=20):
        """Register the watch before the leg, then feed the candles one closed bar at a time."""
        full = app._bars
        created = datetime.fromtimestamp(full[created_offset][0], UTC)
        e.watch({"id": "A1"}, IDEA, PLAIN, ["no_confirmation", "mtf_opposed"], created)
        outs = []
        for n in range(created_offset + 1, len(full) + 1):
            app._bars = full[:n]
            e.bars, e.bars_key = [], None
            now = datetime.fromtimestamp(full[n - 1][0] + 300, UTC)
            outs += e.tick(now)
        app._bars = full
        return outs

    def test_touch_then_5m_choch_enters_with_a_tight_stop_and_the_ideas_tp1(self):
        e, app = self.engine(leg_bars())
        outs = self.run_leg(e, app)
        self.assertEqual(outs, ["LTF-BUY-4042: sent #777"])
        o, lots, risk, magic = app.sent[0]
        self.assertEqual((o["direction"], o["order_type"], magic, o["strategy"]), ("BUY", "MARKET", 40003, "LAB_LTF"))
        self.assertEqual(o["take_profit_1"], 4075.0)                             # same target as the plain sample
        self.assertAlmostEqual(o["stop_loss"], round(4041 - 0.3 - 1.0, 2))      # leg low - max(0.1 ATR15, 2 spreads)
        self.assertGreater(o["stop_loss"], 4030.0)                               # tighter than the idea's stop
        self.assertGreaterEqual(o["rr"], 2.0)
        self.assertIn("ltf_entry", o["meta"]["violations"])
        self.assertIn("mtf_opposed", o["meta"]["violations"])                   # the plain sample's rule tags carried
        self.assertNotIn("no_confirmation", o["meta"]["violations"])
        self.assertEqual(e.watches, {})
        self.assertTrue(any("LAB 5M BUY" in m for m in self.msgs))

    def test_no_choch_no_trade_and_stop_hit_ends_the_watch(self):
        bars = bars_from(PAD + LEG[:14])                                         # ends at the leg low, no break
        e, app = self.engine(bars)
        self.assertEqual(self.run_leg(e, app), [])
        self.assertIn("LTF-BUY-4042", e.watches)
        self.assertIsNotNone(e.watches["LTF-BUY-4042"]["touch_idx"])
        app._bars = bars + [(bars[-1][0] + 300, 4041, 4041.3, 4029.0, 4031.0)]  # through the idea's stop
        e.bars, e.bars_key = [], None
        e.tick(datetime.fromtimestamp(app._bars[-1][0] + 300, UTC))
        self.assertEqual(e.watches, {})
        self.assertEqual(app.sent, [])

    def test_pool_in_the_path_is_tagged_and_the_entry_still_taken(self):
        # 9 Nov: the twins of LAB-BUY-4091 (PDH 4107.01) and LAB-BUY-4104 (EQH 4112.61) were refused this way, and
        # both plain trades ran through the pool to TP1. 1.13.2: the 5M chart times the entry, it does not veto it.
        levels = [{"name": "PDH", "side": "HIGH", "price": 4049.5,                 # untaken, 2.7 above a ~7-risk entry
                   "formed_at": datetime.fromtimestamp(T0 + 27 * 300, UTC).isoformat()}]
        e, app = self.engine(leg_bars(), levels=levels)
        self.assertEqual(self.run_leg(e, app), ["LTF-BUY-4042: sent #777"])
        self.assertIn("pool_in_path", app.sent[0][0]["meta"]["violations"])

    def test_pool_in_the_path_refuses_only_when_configured(self):
        levels = [{"name": "PDH", "side": "HIGH", "price": 4049.5,
                   "formed_at": datetime.fromtimestamp(T0 + 27 * 300, UTC).isoformat()}]
        app = _App(leg_bars(), levels=levels)
        e = LT.LTF(dict(C, ltf_lab={"mode": "lab", "pool_path_blocks": True}), app)
        self.assertEqual(self.run_leg(e, app), [])
        self.assertEqual(app.sent, [])

    def test_rr_below_2_from_the_5m_entry_is_no_trade(self):
        e, app = self.engine(leg_bars())
        near = dict(IDEA, targets=[4054.0])                                      # ~7.5 reward vs ~6 risk
        full = app._bars
        created = datetime.fromtimestamp(full[20][0], UTC)
        e.watch({"id": "A1"}, near, dict(PLAIN, take_profit_1=4054.0), [], created)
        for n in range(21, len(full) + 1):
            app._bars = full[:n]
            e.bars, e.bars_key = [], None
            e.tick(datetime.fromtimestamp(full[n - 1][0] + 300, UTC))
        self.assertEqual(app.sent, [])
        self.assertEqual(e.watches, {})

    def test_hedge_with_a_working_lab_sell_is_refused(self):
        e, app = self.engine(leg_bars())
        app.state.active = [working("SELL", 4046.0, 4060.0, 4030.0, status="OPEN")]
        self.assertEqual(self.run_leg(e, app), [])
        self.assertEqual(app.sent, [])

    def test_watch_persists_and_restores(self):
        e, app = self.engine(leg_bars())
        e.watch({"id": "A1"}, IDEA, PLAIN, [], NOW)
        self.assertIn("LTF-BUY-4042", app.state.kv["ltf_watches"])
        e2 = LT.LTF(C, app)
        self.assertIn("LTF-BUY-4042", e2.watches)

    def test_off_or_lab_mode_off_does_nothing(self):
        app = _App(leg_bars())
        e = LT.LTF(dict(C, ltf_lab={"mode": "off"}), app)
        e.watch({"id": "A1"}, IDEA, PLAIN, [], NOW)
        self.assertEqual((e.watches, e.tick(NOW)), ({}, []))
        self.assertFalse(LT.LTF(dict(C, lab_mode=False), app).live)


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
        self.assertEqual(self._load()["ltf_lab"]["mode"], "off")
        c = self._load(lab_mode=True, ltf_lab={"mode": "lab"})
        self.assertEqual((c["ltf_lab"]["swing_k"], c["ltf_lab"]["min_rr"], c["ltf_lab"]["require_sweep"]), (2, 2.0, False))

    def test_bad_values_fail_at_start(self):
        with self.assertRaises(ValueError):
            self._load(ltf_lab={"mode": "lab"})
        for bad in ({"mode": "live"}, {"min_rr": 1.0}, {"swing_k": 0}, {"pool_path_r": -1}, {"require_sweep": "yes"},
                    {"pool_path_blocks": "no"}, {"typo": 1}):
            with self.assertRaises(ValueError, msg=str(bad)):
                self._load(lab_mode=True, ltf_lab=dict({"mode": "lab"}, **bad))


if __name__ == "__main__":
    unittest.main()
