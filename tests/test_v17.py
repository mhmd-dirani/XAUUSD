#!/usr/bin/env python3
"""v1.7 regression tests (no MT5, no network, no Claude):
tiers (grading + sizing by risk %), the direction rule (no pyramiding; breakeven frees the slot),
the post-trade review, one level per price (PDH == NEW_YORK_HIGH), retest limits inside the idea's
zone, Telegram texts without None/0.00, closed_utc from the deal, no paper mode.
Run:  python src/auto_trader.py --self-test   (or python -m unittest discover -s tests)"""
import json
import sqlite3
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
from autotrader import alerts, bridge as BR, review as RV, sessions as SS, settings, tiers as TR  # noqa: E402
from autotrader.app import App  # noqa: E402
from autotrader.executor import Executor  # noqa: E402
from autotrader.state import State  # noqa: E402
from autotrader.validator import Rejected, Validator  # noqa: E402
from test_auto_trader import Base, bar, evidence, sell_order  # noqa: E402

UTC = timezone.utc


def pend(ticket, price, sl, typ, magic=40001, vol=0.01):
    return NS(ticket=ticket, magic=magic, symbol="XAUUSD", type=typ, price_open=price, sl=sl,
              volume_initial=vol, volume_current=vol)


def pos(ticket, price, sl, typ, magic=40001, vol=0.01):
    return NS(ticket=ticket, identifier=ticket, magic=magic, symbol="XAUUSD", type=typ, price_open=price, sl=sl,
              volume=vol)


def deal(profit, typ, minutes_ago, magic=40001, entry=1):
    """A closing deal `minutes_ago` (server clock = UTC + 3 h in the fake)."""
    return NS(magic=magic, symbol="XAUUSD", entry=entry, type=typ, time=int(time.time()) + 10800 - 60 * minutes_ago,
              profit=profit, commission=0.0, swap=0.0)


# ----------------------------------------------------------------------------------- levels and bridge
class LevelAndBridgeTests(unittest.TestCase):
    def test_pdh_and_new_york_high_at_one_price_are_one_event(self):
        t0 = datetime(2026, 10, 15, 20, 0, tzinfo=UTC)
        lv = [{"name": "NEW_YORK_HIGH", "side": "HIGH", "price": 4177.56, "formed_at": GM.iso(t0 + timedelta(hours=1)),
               "source": "SESSION"},
              {"name": "PDH", "side": "HIGH", "price": 4177.56, "formed_at": GM.iso(t0), "source": "PREV_DAY"},
              {"name": "ASIA_LOW", "side": "LOW", "price": 4150.0, "formed_at": GM.iso(t0), "source": "SESSION"}]
        merged = SS.merge_same_price(lv)
        self.assertEqual([(m["name"], m["also"]) for m in merged], [("NEW_YORK_HIGH", ["PDH"]), ("ASIA_LOW", [])])
        self.assertEqual(merged[0]["formed_at"], GM.iso(t0))                        # earliest, so the scan covers both
        bars = [bar(t0 + timedelta(hours=6), 4176.0, 4178.9, 4175.0, 4176.3)]      # sweep above, close back below
        ev = SS.events(bars, lv, t0 + timedelta(hours=7))
        self.assertEqual([(e["event"], e["level"], e["also"]) for e in ev], [("SWEEP_RECLAIM", "NEW_YORK_HIGH", ["PDH"])])

    def test_retest_limit_must_stay_inside_the_ideas_zone(self):
        """15 Oct I2: SELL 4198.75-4205.3, stop 4221, TP1 4177.56 -> 2R needs 4206.62, above the zone."""
        c = {"min_rr": 2.0, "idea_a_grade_confidence": 60, "signal_entry_tolerance": 1.0, "confirm_retest_hours": 4,
             "max_order_lifetime_hours": 48}
        now = datetime(2026, 10, 16, 1, 0, tzinfo=UTC)
        b = bar(now - timedelta(minutes=30), 4201.0, 4206.0, 4195.0, 4196.0)
        i2 = {"id": "I2", "direction": "SELL", "confidence": 30, "risk": "HIGH", "entry_low": 4198.75,
              "entry_high": 4205.3, "invalidation": 4221.0, "targets": [4177.56, 4152.92], "title": "counter"}
        order, why = BR.confirmed_order(i2, {"bid": 4196.0, "ask": 4196.3}, c, now, b)
        self.assertIsNone(order)
        self.assertIn("outside the idea's zone 4198.75-4205.3", why)
        i1 = {"id": "I1", "direction": "BUY", "confidence": 40, "risk": "MEDIUM", "entry_low": 4143.73,
              "entry_high": 4152.92, "invalidation": 4136.25, "targets": [4177.56, 4198.75], "title": "ob"}
        order, why = BR.confirmed_order(i1, {"bid": 4155.0, "ask": 4155.3}, c, now, b)
        self.assertEqual((order["order_type"], order["entry_price"]), ("BUY_LIMIT", 4149.93))   # inside 4143.73-4152.92

    def test_telegram_texts_never_show_none(self):
        o = BR._order("SELL", "SELL_LIMIT", 4100.5, 4106.5, [4086.0], datetime.now(UTC) + timedelta(hours=2),
                      "PY-PDH-SELL-4100", "B_TRADE", "x")
        txt = alerts.order_text("PLACED SELL_LIMIT", dict(o, meta={}), "-", 0.0, "B_TRADE")
        self.assertIn("R:R 2.42 | risk n/a", txt)
        self.assertNotIn("None", txt)
        self.assertEqual(alerts.tail(None, None, None), "")
        self.assertEqual(alerts.tail("TREND", None, "ASIA"), " | TREND | ASIA")



class DirectionRuleTests(Base):
    def setUp(self):
        super().setUp()
        self.m.connect()
        self.v = Validator(self.c, self.m, self.state)
        self.now = datetime.now(UTC)

    def sell(self, **over):
        return self.v.validate(sell_order(**over), "B_TRADE", evidence(), 5.0, self.now)

    def buy(self, **over):
        o = sell_order(setup_id="BUY_001", direction="BUY", order_type="BUY_LIMIT", entry_price=4088.0,
                       stop_loss=4082.0, take_profit_1=4102.0, take_profit_2=None, cancel_if_price_above=4105.0,
                       cancel_if_price_below=None, evidence_ids=["ob_buy"])
        o.update(over)
        return self.v.validate(o, "B_TRADE", evidence("BULLISH", "BULLISH"), 5.0, self.now)

    def second_sell(self):
        return self.sell(setup_id="IDEA-SELL-4137", entry_price=4136.89, stop_loss=4155.79, take_profit_1=4090.0,
                         take_profit_2=None, cancel_if_price_below=4090.0)

    def test_no_second_risky_order_in_the_same_direction(self):
        """15 Oct: SELL 4119 (00:05 filled), SELL 4137 (00:17) and SELL 4145 (00:34) stacked into one rally."""
        self.assertEqual(self.c["max_same_direction_positions"], 1)
        self.fake.orders = [pend(901, 4119.0, 4138.37, typ=3)]                      # a working SELL_LIMIT
        with self.assertRaisesRegex(Rejected, "1 SELL position\\(s\\)/order\\(s\\) at risk already working"):
            self.second_sell()
        self.assertEqual(self.buy()["direction"], "BUY")                            # the other side is free
        self.fake.orders = []
        self.fake.positions = [pos(902, 4119.12, 4138.37, typ=1)]                   # an open SELL at risk
        with self.assertRaisesRegex(Rejected, "per direction"):
            self.second_sell()
        self.fake.positions[0].sl = 4119.12                                         # stop at breakeven: no risk
        self.assertEqual(self.second_sell()["setup_id"], "IDEA-SELL-4137")
        self.fake.positions[0].sl = 4110.0                                          # stop in profit: no risk
        self.assertEqual(self.second_sell()["setup_id"], "IDEA-SELL-4137")
        self.fake.positions[0].sl = 4125.0
        self.c["max_same_direction_positions"] = 2
        self.assertEqual(self.second_sell()["setup_id"], "IDEA-SELL-4137")

    def test_other_bots_positions_do_not_count(self):
        self.fake.positions = [pos(5, 4100.0, 4106.0, typ=1, magic=0)]             # manual SELL with a stop
        self.assertEqual(self.sell()["setup_id"], "SELL_SUPPLY_001")

    def test_tier_risk_pct_sizes_the_order_and_is_capped_at_tier_a(self):
        o = self.sell()
        self.assertEqual(self.v.lots(o, "B_TRADE"), (0.04, 24.0))                   # legacy 0.25 %
        self.assertEqual(self.v.lots(o, "A_TRADE", risk_pct=1.0), (0.16, 96.0))     # Tier A 1 %
        self.assertEqual(self.v.lots(o, "B_TRADE", risk_pct=0.5), (0.08, 48.0))     # Tier B
        self.assertEqual(self.v.lots(o, "B_TRADE", risk_pct=0.15), (0.02, 12.0))    # Tier C
        self.assertEqual(self.v.lots(o, "A_TRADE", risk_pct=5.0), (0.16, 96.0))     # never above tiers.A
        wide = dict(o, stop_loss=4120.5)                                             # 20-point stop: 0.01 lot = 20 $
        with self.assertRaisesRegex(Rejected, "below one minimum lot"):
            self.v.lots(wide, "B_TRADE", risk_pct=0.15)                              # 15 $ budget
        self.assertEqual(self.v.lots(wide, "B_TRADE", risk_pct=0.15, min_lot_ok=True), (0.01, 20.0))  # Tier C floor
        huge = dict(o, stop_loss=4160.5)                                             # 60 $ at 0.01 lot > 0.5 %
        with self.assertRaisesRegex(Rejected, "below one minimum lot"):
            self.v.lots(huge, "B_TRADE", risk_pct=0.15, min_lot_ok=True)


# ----------------------------------------------------------------------------------- tiers
class TierTests(unittest.TestCase):
    def setUp(self):
        self.c = {"tiers": {"A": {"risk_pct": 1.0, "min_confidence": 40}, "B": {"risk_pct": 0.5, "min_confidence": 35},
                            "C": {"risk_pct": 0.15, "min_confidence": 30}}}
        self.idea = {"confidence": 45, "astra_risk": "MEDIUM", "sl_anchor": {"id": "M1"},
                     "tp_anchors": [{"id": "M2"}, None]}
        self.dec = {"trade_class": "TREND", "expansion": {"against": False}, "would_block": False}
        self.state = {"macro_trend": "BEARISH"}

    def g(self, **over):
        idea, dec, state, sess = dict(self.idea), dict(self.dec), dict(self.state), "LONDON"
        for k, v in over.items():
            if k == "session":
                sess = v
            elif k in idea or k in ("confidence", "astra_risk", "sl_anchor", "tp_anchors"):
                idea[k] = v
            elif k in dec:
                dec[k] = v
            else:
                state[k] = v
        return TR.grade(idea, "SELL", dec, state, sess, self.c)

    def test_tier_a_needs_all_five_facts(self):
        t = self.g()
        self.assertEqual((t["tier"], t["risk_pct"]), ("A", 1.0))
        self.assertTrue(all(t["facts"][k] for k in TR.FACTS))

    def test_one_missing_fact_is_tier_b_two_are_tier_c(self):
        self.assertEqual(self.g(session="ASIA")["tier"], "B")
        self.assertEqual(self.g(sl_anchor=None)["tier"], "B")
        self.assertEqual(self.g(session="ASIA", sl_anchor=None)["tier"], "C")
        t = self.g(macro_trend="BULLISH")                                             # against the macro trend
        self.assertEqual((t["tier"], t["facts"]["with_trend"]), ("B", False))

    def test_counter_trend_and_reversal_classes_are_tier_c_at_most(self):
        self.assertEqual(self.g(trade_class="COUNTER_TREND")["tier"], "C")
        self.assertEqual(self.g(trade_class="REVERSAL_TRANSITION", astra_risk="HIGH", confidence=31)["tier"], "C")

    def test_confidence_floors_drop_the_tier_or_the_trade(self):
        self.assertEqual(self.g(confidence=39)["tier"], "B")                         # all facts, but < A floor
        self.assertEqual(self.g(confidence=34)["tier"], "C")
        t = self.g(confidence=29)
        self.assertIsNone(t["tier"])
        self.assertIn("below tiers.C.min_confidence", t["reason"])

    def test_expansion_against_without_validated_reversal(self):
        t = self.g(would_block=True, expansion={"against": True})                     # shadow (default): Tier C
        self.assertEqual(t["tier"], "C")
        self.assertIn("expansion_filter shadow", t["reason"])
        self.c["expansion_filter"] = "live"
        t = self.g(would_block=True, expansion={"against": True})
        self.assertIsNone(t["tier"])
        self.assertIn("expansion_filter live", t["reason"])
        self.c["expansion_filter"] = "shadow"
        t = self.g(expansion={"against": True, "methods": ["CHOCH"]})                 # validated: a fact missing
        self.assertEqual((t["tier"], t["facts"]["no_expansion"]), ("B", False))


# ----------------------------------------------------------------------------------- review
class ReviewTests(unittest.TestCase):
    def rec(self, **over):
        r = {"ticket": 1, "setup_id": "IDEA-SELL-4119", "direction": "SELL", "entry": 4119.0, "sl": 4138.37,
             "tp1": 4079.75, "tp2": 4073.53, "risk_money": 19.37, "result": -19.4,
             "fill_utc": "2026-10-15T00:05:53Z", "closed_utc": "2026-10-15T00:17:54Z", "tier": "C",
             "tier_facts": json.dumps({"with_trend": True, "session": False, "anchored": True,
                                       "no_expansion": True, "calm_risk": True, "_x": 1})}
        r.update(over)
        return r

    def test_stop_by_wick_then_tp1_reached_is_right_idea_wrong_stop(self):
        t0 = datetime(2026, 10, 15, 0, 0, tzinfo=UTC)
        bars = [bar(t0, 4120.0, 4125.0, 4115.0, 4122.0), bar(t0 + timedelta(minutes=15), 4122.0, 4139.0, 4118.0, 4130.0),
                bar(t0 + timedelta(minutes=30), 4130.0, 4132.0, 4100.0, 4105.0),
                bar(t0 + timedelta(minutes=45), 4105.0, 4106.0, 4078.0, 4080.0)]
        rv = RV.build(self.rec(), bars, t0 + timedelta(hours=6))
        self.assertEqual((rv["outcome"], rv["after_4h"], rv["verdict"]), ("STOP_WICK", "TP1_REACHED", "RIGHT_IDEA_WRONG_STOP"))
        self.assertEqual(rv["missing_facts"], ["session"])
        self.assertIn("missing facts: session", RV.text(rv))
        self.assertTrue(any("RIGHT_IDEA_WRONG_STOP 1" in line for line in RV.summary([rv])))

    def test_stop_by_close_and_no_follow_through_is_wrong_idea(self):
        t0 = datetime(2026, 10, 15, 0, 0, tzinfo=UTC)
        bars = [bar(t0, 4120.0, 4140.0, 4118.0, 4139.5), bar(t0 + timedelta(minutes=15), 4139.5, 4150.0, 4135.0, 4148.0),
                bar(t0 + timedelta(minutes=30), 4148.0, 4160.0, 4145.0, 4158.0)]
        rv = RV.build(self.rec(), bars, t0 + timedelta(hours=6))
        self.assertEqual((rv["outcome"], rv["after_4h"], rv["verdict"]), ("STOP_CLOSE", "STOP_SIDE", "WRONG_IDEA"))

    def test_tp1_is_a_good_exit_and_mfe_is_measured(self):
        t0 = datetime(2026, 10, 15, 0, 0, tzinfo=UTC)
        bars = [bar(t0, 4120.0, 4121.0, 4079.0, 4080.0)]
        rv = RV.build(self.rec(result=+39.2, closed_utc="2026-10-15T00:14:00Z", tp2=None), bars, t0 + timedelta(hours=6))
        self.assertEqual((rv["outcome"], rv["verdict"]), ("TP1", "GOOD_EXIT"))
        self.assertAlmostEqual(rv["mfe_r"], 40.0 / 19.37, places=2)


class ReviewTimingTests(Base):
    def test_review_waits_for_the_4h_window_and_a_premature_row_is_redone(self):
        """23 Oct 14:05: IDEA-SELL-4020 was reviewed 50 min after its close, before the collapse candle
        closed: '4h after: NEITHER / WRONG_IDEA' instead of TP1_REACHED / RIGHT_IDEA_WRONG_STOP."""
        o = dict(sell_order(), rr=2.42)
        self.state.record_order(1, o, "B_TRADE", 0.01, 6.0, "CLOSED", position_ticket=1)
        closed = datetime.now(UTC) - timedelta(hours=1)
        self.state.update_order(1, result=-6.0, fill_utc=(closed - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                closed_utc=closed.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.m.connect()
        ex = Executor(self.c, self.m, self.state)
        with patch.object(alerts, "trade"), patch.object(self.m, "history", return_value={"15min": []}):
            ex.review_closed(datetime.now(UTC))
        self.assertEqual(self.state.reviews(), [])                                  # too early: nothing written
        with patch.object(alerts, "trade"), patch.object(self.m, "history", return_value={"15min": []}):
            ex.review_closed(datetime.now(UTC) + timedelta(hours=4))
        self.assertEqual(len(self.state.reviews()), 1)
        # a premature row left by 1.7.2 is dropped at the next start
        with sqlite3.connect(self.c["state_path"] / "trader.sqlite3") as c:
            c.execute("UPDATE trade_reviews SET reviewed_utc=?", ((closed + timedelta(minutes=50)).strftime("%Y-%m-%dT%H:%M:%SZ"),))
        self.assertEqual(State(self.c["state_path"]).reviews(), [])


class ReviewAppTests(Base):
    def test_closed_trade_is_reviewed_once_with_closed_utc_from_the_deal(self):
        o = dict(sell_order(), rr=2.42, tp1_close_percent=50)
        self.state.record_order(58561465, o, "B_TRADE", 0.01, 19.37, "CLOSED", position_ticket=58561465)
        self.state.set_meta(58561465, {"tier": "C", "tier_facts": {"session": False}, "playbook": "TREND"})
        server_open = int(datetime(2026, 10, 15, 0, 5, 53, tzinfo=UTC).timestamp()) + 10800
        server_close = int(datetime(2026, 10, 15, 0, 17, 54, tzinfo=UTC).timestamp()) + 10800
        self.fake.history_deals_get = lambda *a, **k: [NS(entry=0, profit=0.0, commission=0.0, swap=0.0, time=server_open),
                                                       NS(entry=1, profit=-19.4, commission=0.0, swap=0.0, time=server_close)]
        self.m.connect()
        sent = []
        ex = Executor(self.c, self.m, self.state)
        with patch.object(alerts, "trade", side_effect=lambda m, short=None: sent.append(m)), \
             patch.object(self.m, "history", return_value={"15min": []}):
            ex.fill_results()
            ex.review_closed(datetime.now(UTC))
            ex.review_closed(datetime.now(UTC))                                     # idempotent
        row = self.state.all_orders()[0]
        self.assertEqual((row["result"], row["fill_utc"], row["closed_utc"]),
                         (-19.4, "2026-10-15T00:05:53Z", "2026-10-15T00:17:54Z"))
        self.assertEqual(sent[0], "🔴 CLOSED SELL SELL_SUPPLY_001: -19.40 (-1.00R) | TREND")
        self.assertTrue(sent[1].startswith("🔎 REVIEW SELL SELL_SUPPLY_001 (C):"))
        self.assertEqual(len(sent), 2)
        self.assertEqual(len(self.state.reviews()), 1)
        self.assertEqual(self.state.reviews()[0]["missing_facts"], ["session"])


class LegacyReviewTests(Base):
    def test_legacy_trade_without_times_is_reviewed_only_after_the_deals_date_it(self):
        """22 Oct: three pre-1.7 trades (no fill_utc / closed_utc) were reviewed over a week-long window."""
        o = dict(sell_order(), rr=2.42)
        self.state.record_order(58561465, o, "B_TRADE", 0.01, 19.37, "CLOSED", position_ticket=58561465)
        self.state.update_order(58561465, result=-19.4)                            # closed before 1.7: no times
        with sqlite3.connect(self.c["state_path"] / "trader.sqlite3") as c:
            c.execute("INSERT INTO trade_reviews (ticket,reviewed_utc,verdict,outcome,r,mfe_r,mae_r,after_4h,tier,data)"
                      " VALUES (58561465,'x','WRONG_IDEA','STOP_CLOSE',-1,4.81,-4.12,'NEITHER',NULL,'{}')")
        self.assertEqual(State(self.c["state_path"]).reviews(), [])                 # the bad review is dropped
        self.m.connect()
        ex = Executor(self.c, self.m, self.state)
        self.fake.history_deals_get = lambda *a, **k: []                            # no deals yet: postponed
        with patch.object(alerts, "trade"), patch.object(self.m, "history", return_value={"15min": []}):
            ex.review_closed(datetime.now(UTC))
        self.assertEqual(self.state.reviews(), [])
        t_in = int(datetime(2026, 10, 15, 0, 5, 53, tzinfo=UTC).timestamp()) + 10800
        t_out = int(datetime(2026, 10, 15, 0, 17, 54, tzinfo=UTC).timestamp()) + 10800
        self.fake.history_deals_get = lambda *a, **k: [NS(entry=0, time=t_in, profit=0.0, commission=0.0, swap=0.0),
                                                       NS(entry=1, time=t_out, profit=-19.4, commission=0.0, swap=0.0)]
        t0 = datetime(2026, 10, 15, 0, 0, tzinfo=UTC)
        bars = [bar(t0 + timedelta(minutes=15 * i), 4100.0 + i, 4110.0 + 10 * i, 4098.0, 4105.0 + 10 * i) for i in range(40)]
        with patch.object(alerts, "trade"), patch.object(self.m, "history", return_value={"15min": bars}):
            ex.review_closed(datetime.now(UTC))
        row = self.state.all_orders()[0]
        self.assertEqual((row["fill_utc"], row["closed_utc"]), ("2026-10-15T00:05:53Z", "2026-10-15T00:17:54Z"))
        rv = self.state.reviews()[0]
        self.assertEqual(rv["bars_open"], 2)                                        # 00:00 and 00:15 bars, not a week
        self.assertEqual((rv["outcome"], rv["verdict"]), ("STOP_CLOSE", "WRONG_IDEA"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
