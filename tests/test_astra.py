#!/usr/bin/env python3
"""V6 Astra layer tests: market memory, sensor, learning history, analyst validation, reports
and App integration. Offline; no sockets, Telegram or API calls."""
import copy
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import astra_analysis as AA  # noqa: E402
import astra_memory as AM  # noqa: E402
import gold_monitor as M  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 10, 1, tzinfo=UTC)


def hourly(spec, start=T0):
    """spec: list of closes or (open, high, low, close) tuples -> 1h bars."""
    bars, prev = [], None
    for i, x in enumerate(spec):
        if isinstance(x, tuple):
            o, h, l, c = x
        else:
            o = prev if prev is not None else x
            c = x
            h, l = max(o, c) + 0.5, min(o, c) - 0.5
        bars.append({"time": (start + timedelta(hours=i)).isoformat(), "open": o, "high": h, "low": l, "close": c})
        prev = c
    return bars


def ramp(a, b, n):
    return [round(a + (b - a) * (i + 1) / n, 3) for i in range(n)]


def resistance_story():
    """Pivot high at 110 (bar 30), retests at bars 45 and 60, upside break at 75, acceptance,
    then a retest that holds at 79."""
    s = [100 + (0.3 if i % 2 else 0) for i in range(25)]
    s += ramp(100, 108, 5)                                   # 25..29
    s += [(108.0, 110.0, 107.8, 108.3)]                      # 30 pivot high
    s += ramp(108, 103, 10)                                  # 31..40
    s += ramp(103, 108.4, 4)                                 # 41..44
    s += [(108.4, 109.6, 108.0, 108.6)]                      # 45 test
    s += ramp(108.6, 104, 10)                                # 46..55
    s += ramp(104, 108.5, 4)                                 # 56..59
    s += [(108.5, 109.8, 108.2, 108.9)]                      # 60 test
    s += ramp(108.9, 105, 10)                                # 61..70
    s += ramp(105, 108.7, 4)                                 # 71..74 (approach stays below the zone)
    s += [(108.7, 111.8, 108.6, 111.5)]                      # 75 break (close > high + buffer)
    s += [(111.5, 113.0, 112.1, 112.5)] * 3                  # 76..78 acceptance, no touch
    s += [(112.5, 112.6, 109.9, 111.8)]                      # 79 retest holds
    s += [(111.8, 113.0, 111.6, 112.6)] * 6                  # 80..85
    return hourly(s)


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(); self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "mem.sqlite3"
        self.m = AM.MarketMemory(self.path); self.addCleanup(lambda: self.m.db.close())
        self.bars = resistance_story()
        self.now = T0 + timedelta(hours=len(self.bars) + 1)

    def resistance(self, m=None):
        m = m or self.m
        zs = [z for z in m.zones.values() if z["kind"] == "SR" and z["original_role"] == "RESISTANCE"
              and z["low"] <= 110 <= z["high"]]
        self.assertEqual(len(zs), 1, [(z["id"], z["low"], z["high"]) for z in m.zones.values()])
        return zs[0]

    def test_tests_break_acceptance_and_retest(self):
        events = self.m.update({"1h": self.bars}, self.now)
        z = self.resistance()
        self.assertEqual(z["created_at"], AM.iso(T0 + timedelta(hours=35)))     # pivot 30 confirmed by bar 34
        self.assertEqual(z["tests"], 4)                      # pivot + 45 + 60 + held retest
        self.assertEqual((z["status"], z["role"], z["retest"]), ("FLIPPED", "SUPPORT", "CONFIRMED"))
        self.assertEqual(z["break_direction"], "UP")
        self.assertTrue(z["reactions"] and all(r > 1 for r in z["reactions"][:2]))
        kinds = [e["event"] for e in events if e["zone_id"] == z["id"]]
        self.assertEqual(kinds, ["CREATED", "TESTED", "TESTED", "BROKEN", "ACCEPTED", "RETEST_CONFIRMED"])

    def test_false_breakout_restores_role(self):
        bars = resistance_story()[:76]                                     # break at 75, no acceptance yet
        bars += hourly([(111.5, 111.6, 106.0, 106.5)] + [(106.5, 107, 105.5, 106)] * 3,
                       start=T0 + timedelta(hours=76))
        self.m.update({"1h": bars}, self.now)
        z = self.resistance()
        self.assertEqual((z["status"], z["role"], z["false_breakouts"]), ("ACTIVE", "RESISTANCE", 1))

    def test_retest_needs_departure_and_accepted_break_is_not_false(self):
        bars = resistance_story()[:76]
        # follow-through bar that still overlaps the zone is NOT a retest
        bars += hourly([(111.5, 112.0, 109.9, 111.9)] + [(111.9, 112.4, 111.5, 112.2)] * 2, start=T0 + timedelta(hours=76))
        self.m.update({"1h": bars}, T0 + timedelta(hours=80))
        z = self.resistance()
        self.assertEqual((z["status"], z["retest"], z["tests"]), ("FLIPPED", "WAITING", 3))   # accepted, not retested
        # accepted, then a much later close back through = a new break of the flipped support
        bars += hourly([(112.2, 112.6, 111.8, 112.3)] * 2 + [(112.3, 112.4, 106.0, 106.2)] + [106.0] * 3,
                       start=T0 + timedelta(hours=79))
        self.m.update({"1h": bars}, T0 + timedelta(hours=90))
        z = self.resistance()
        self.assertEqual((z["status"], z["role"], z["false_breakouts"], z["flips"]), ("FLIPPED", "RESISTANCE", 0, 2))   # re-broken and accepted

    def test_point_in_time_and_incremental_equals_replay(self):
        early = T0 + timedelta(hours=33)           # pivot at 30 needs 4 right bars: not known yet
        self.m.update({"1h": self.bars}, early)
        self.assertFalse([z for z in self.m.zones.values() if z["low"] <= 110 <= z["high"]])
        for cut in (50, 70, 78):
            self.m.update({"1h": self.bars}, T0 + timedelta(hours=cut))
        self.m.update({"1h": self.bars}, self.now)
        other = AM.MarketMemory(Path(self.dir.name) / "replay.sqlite3"); self.addCleanup(lambda: other.db.close())
        other.update({"1h": self.bars}, self.now)
        strip = lambda z: {k: v for k, v in z.items() if k != "id"}
        self.assertEqual(sorted(json.dumps(strip(z), sort_keys=True) for z in self.m.zones.values()),
                         sorted(json.dumps(strip(z), sort_keys=True) for z in other.zones.values()))

    def test_persistence_and_no_double_counting(self):
        self.m.update({"1h": self.bars}, self.now)
        before = self.resistance()["tests"]
        self.m.close()
        self.m = AM.MarketMemory(self.path)
        self.assertEqual(self.m.update({"1h": self.bars}, self.now), [])
        self.assertEqual(self.resistance()["tests"], before)

    def test_equal_highs_liquidity_and_sweep(self):
        s = [100 + (0.2 if i % 2 else 0) for i in range(20)]
        s += ramp(100, 104, 4) + [(104, 106.0, 103.8, 104.2)] + ramp(104.2, 101, 6)      # pivot 24
        s += ramp(101, 104, 4) + [(104, 106.05, 103.8, 104.1)] + ramp(104.1, 101, 6)     # pivot 35 (equal)
        s += ramp(101, 105, 4) + [(105, 106.6, 104.9, 105.4)] + [104.5] * 6              # sweep at 46
        bars = hourly(s)
        self.m.update({"1h": bars}, T0 + timedelta(hours=len(bars) + 1))
        liq = [z for z in self.m.zones.values() if z["kind"] == "LIQUIDITY" and z["role"] == "BUY_SIDE"]
        self.assertEqual(len(liq), 1)
        self.assertEqual(liq[0]["status"], "SWEPT")

    def test_sensor_entry_once_with_cooldown(self):
        self.m.update({"1h": self.bars}, self.now)
        z = self.resistance()
        hist = {"1h": self.bars}
        self.assertEqual(self.m.sense(115, self.now, hist), [])
        self.assertEqual([h["id"] for h in self.m.sense(z["high"] - 0.1, self.now, hist)], [z["id"]])
        self.assertEqual([h["id"] for h in self.m.sense(z["high"] - 0.1, self.now, hist)], [z["id"]])  # not acked yet
        self.m.ack(z["id"], self.now)
        self.assertEqual(self.m.sense(z["high"] - 0.2, self.now, hist), [])          # still inside
        self.m.sense(130, self.now, hist)                                             # leaves
        self.assertEqual(self.m.sense(z["high"], self.now + timedelta(minutes=30), hist), [])   # cooldown
        self.m.sense(130, self.now, hist)
        later = self.now + timedelta(hours=3)
        self.assertEqual([h["id"] for h in self.m.sense(z["high"], later, hist)], [z["id"]])

    def test_failed_update_rolls_back(self):
        self.m.update({"1h": self.bars[:60]}, T0 + timedelta(hours=61))
        before = json.dumps(self.m.zones, sort_keys=True)
        with patch.object(AM.MarketMemory, "_liquidity", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.m.update({"1h": self.bars}, self.now)
        self.assertEqual(json.dumps(self.m.zones, sort_keys=True), before)
        self.m.update({"1h": self.bars}, self.now)                                 # recovers cleanly
        self.assertEqual(self.resistance()["status"], "FLIPPED")

    def test_payload_is_compact_and_cites_ids(self):
        self.m.update({"1h": self.bars}, self.now)
        p = self.m.payload(112.0, self.now, {"1h": self.bars}, self.m.track_record(self.now))
        ids = {z["id"] for z in p["zones"]}
        self.assertIn(self.resistance()["id"], ids)
        self.assertLess(len(json.dumps(p)), 20000)
        self.assertEqual(p["track_record"]["resolved"], 0)

    def test_astra_level_added_and_replayed(self):
        self.m.update({"1h": self.bars}, self.now)
        lv = {"id": "x", "kind": "SUPPORT", "timeframe": "1h", "low": 106.2, "high": 106.8,
              "evidence_times": [self.bars[61]["time"]], "reason": "fixture"}
        added = self.m.add_astra_levels([lv], {"1h": self.bars}, self.now)
        self.assertEqual(len(added), 1)
        z = self.m.zone(added[0])
        self.assertEqual(z["source"], "ASTRA")
        self.assertGreaterEqual(z["tests"], 1)
        self.assertEqual(self.m.add_astra_levels([lv], {"1h": self.bars}, self.now), [])   # merged, not duplicated


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(); self.addCleanup(self.dir.cleanup)
        self.m = AM.MarketMemory(Path(self.dir.name) / "m.sqlite3"); self.addCleanup(lambda: self.m.db.close())

    @staticmethod
    def scenario(**kw):
        sc = {"id": "s1", "title": "break", "bias": "BULLISH", "likelihood": "PRIMARY", "trigger_type": "CLOSE_ABOVE",
              "trigger_timeframe": "1h", "trigger_level": 105.0, "objective_level": 110.0, "invalidation_level": 100.0,
              "horizon_hours": 12, "memory_ids": [], "description": "d"}
        sc.update(kw)
        return sc

    def frames(self, h1, m15):
        return {"1h": hourly(h1), "15min": [dict(b, time=(T0 + timedelta(minutes=15 * i)).isoformat())
                                             for i, b in enumerate(hourly(m15))]}

    def test_played_out_failed_and_not_triggered(self):
        created = AM.iso(T0)
        self.m.record_predictions("A1", created, [self.scenario()], 103)
        self.m.record_predictions("A2", created, [self.scenario(id="s2", bias="BEARISH", trigger_type="CLOSE_BELOW",
                                                                trigger_level=101, objective_level=95,
                                                                invalidation_level=120)], 103)
        self.m.record_predictions("A3", created, [self.scenario(id="s3", trigger_level=150, objective_level=160,
                                                                invalidation_level=120, horizon_hours=2)], 103)
        h1 = [103, 104, 106, 107, 108, 109]                   # 1h close 106 at bar 2 triggers s1
        m15 = [103] * 12 + [107, 109, 110.6, 111] + [111] * 8  # objective 110 reached after the trigger
        hist = self.frames(h1, m15)
        res = {p["pid"]: p["status"] for p in self.m.evaluate_predictions(hist, T0 + timedelta(hours=6, minutes=5))}
        self.assertEqual(res.get("A1:s1"), "PLAYED_OUT")
        self.assertEqual(res.get("A3:s3"), "NOT_TRIGGERED")
        self.assertNotIn("A2:s2", res)                         # still open
        self.assertEqual(self.m.evaluate_predictions(hist, T0 + timedelta(hours=13)), [])   # candles missing: wait
        res = {p["pid"]: p["status"] for p in self.m.evaluate_predictions(hist, T0 + timedelta(hours=19))}
        self.assertEqual(res.get("A2:s2"), "NOT_TRIGGERED")
        tr = self.m.track_record(T0 + timedelta(hours=19))
        self.assertEqual((tr["resolved"], tr["primary"]["played_out"], tr["primary"]["triggered"]), (3, 1, 1))

    def test_same_bar_trigger_and_invalidation_is_failed_and_neutral_not_counted(self):
        self.m.record_predictions("A1", AM.iso(T0), [self.scenario(invalidation_level=103.5)], 104)
        self.m.record_predictions("A2", AM.iso(T0), [self.scenario(id="n", bias="NEUTRAL", trigger_type="BEAR_REJECTION",
                                                                trigger_level=120, objective_level=None,
                                                                invalidation_level=None)], 104)
        h1 = [104.5, (104.5, 106.5, 103.0, 106.0), 106, 106]
        res = self.m.evaluate_predictions(self.frames(h1, [104] * 16), T0 + timedelta(hours=4, minutes=5))
        self.assertEqual([p["status"] for p in res], ["FAILED"])
        self.assertEqual(self.m.evaluate_predictions(self.frames(h1, [104] * 16), T0 + timedelta(hours=13)), [])
        later = self.m.evaluate_predictions(self.frames(h1, [104] * 16), T0 + timedelta(hours=19))   # grace passed
        self.assertEqual([p["status"] for p in later], ["NOT_TRIGGERED"])
        tr = self.m.track_record(T0 + timedelta(hours=19))
        self.assertEqual((tr["primary"]["count"], tr["neutral"]["count"]), (1, 1))

    def test_ideas_scored_by_confidence(self):
        idea = {"id": "i1", "direction": "BUY", "entry_type": "MARKET", "entry_low": 103, "entry_high": 104,
                "invalidation": 100.0, "targets": [108.0], "confidence": 75, "risk": "MEDIUM", "title": "t"}
        lim = dict(idea, id="i2", entry_type="LIMIT", entry_low=101.0, entry_high=101.5, confidence=55)
        self.m.record_ideas("A1", AM.iso(T0), [idea, lim], 103.5)
        m15 = [103.5] * 4 + [105, 107, 108.6] + [108.6] * 20                   # never back to 101.5
        res = {p["pid"]: p["status"] for p in self.m.evaluate_predictions(self.frames([103.5] * 8, m15),
                                                                           T0 + timedelta(hours=7))}
        self.assertEqual(res, {"A1:idea-i1": "PLAYED_OUT"})
        self.m.evaluate_predictions(self.frames([103.5] * 30, m15 + [108.6] * 80), T0 + timedelta(hours=31))
        tr = self.m.track_record(T0 + timedelta(hours=31))
        self.assertEqual((tr["ideas"]["played_out"], tr["ideas_by_confidence"]["70+"]["played_out"],
                          tr["primary"]["count"]), (1, 1, 0))
        self.assertIn("70%+ confidence calls 1/1", AA.track_line(tr, "English"))

    def test_failed_after_trigger_and_dedupe(self):
        created = AM.iso(T0)
        self.assertEqual(len(self.m.record_predictions("A1", created, [self.scenario()], 103)), 1)
        self.assertEqual(self.m.record_predictions("A2", AM.iso(T0 + timedelta(hours=1)), [self.scenario()], 103), [])
        h1 = [103, 104, 106, 104, 101, 99]
        m15 = [103] * 12 + [104, 102, 99.5, 99] + [99] * 8
        res = self.m.evaluate_predictions(self.frames(h1, m15), T0 + timedelta(hours=6, minutes=5))
        self.assertEqual([p["status"] for p in res], ["FAILED"])


def analysis(**over):
    a = {"stance": "TRADE_IDEA", "market_story": "Gold is testing remembered resistance.",
         "historical_context": "Tested three times.",
         "scenarios": [{"id": "s1", "title": "Break", "bias": "BULLISH", "likelihood": "PRIMARY",
                        "trigger_type": "CLOSE_ABOVE", "trigger_timeframe": "1h", "trigger_level": 104.0,
                        "objective_level": 108.0, "invalidation_level": 100.0, "horizon_hours": 12,
                        "memory_ids": ["M1-1H-RES", "ghost"], "description": "d"},
                       {"id": "s2", "title": "Bad", "bias": "BULLISH", "likelihood": "PRIMARY",
                        "trigger_type": "CLOSE_BELOW", "trigger_timeframe": "1h", "trigger_level": 101.0,
                        "objective_level": 108.0, "invalidation_level": 99.0, "horizon_hours": 12,
                        "memory_ids": [], "description": "d"}],
         "ideas": [{"id": "i1", "direction": "BUY", "risk": "LOW", "readiness": "NEEDS_CONFIRMATION",
                    "confidence": 65, "entry_type": "LIMIT",
                    "title": "Retest buy", "reasoning": "Broken resistance may act as support.",
                    "entry_low": 100.0, "entry_high": 101.0, "invalidation": 98.5, "invalidation_text": "below break",
                    "targets": [106.0, 108.0], "targets_text": "liquidity", "confirmation": "15M bullish rejection",
                    "risk_factors": ["Retest not complete"], "safer_alternative": "Wait for the retest",
                    "memory_ids": ["M1-1H-RES"], "scenario_id": "s1"},
                   {"id": "i2", "direction": "SELL", "risk": "MEDIUM", "readiness": "DEVELOPING", "title": "bad",
                    "confidence": 40, "entry_type": "LIMIT",
                    "reasoning": "r", "entry_low": 103.0, "entry_high": 104.0, "invalidation": 102.0,
                    "invalidation_text": "", "targets": [99.0], "targets_text": "", "confirmation": "",
                    "risk_factors": [], "safer_alternative": "", "memory_ids": [], "scenario_id": ""}],
         "watches": [{"id": "w1", "memory_id": "M1-1H-RES", "title": "t", "situation": "Third test",
                      "interpretation": "Decision point", "outcomes": [{"if": "close above", "then": "continuation"}],
                      "waiting_for": "confirmation"},
                     {"id": "w2", "memory_id": "nope", "title": "t", "situation": "s", "interpretation": "i",
                      "outcomes": [], "waiting_for": "w"}],
         "no_trade_reason": ""}
    a.update(over)
    return a


def validate(a, **kw):
    args = dict(price=102.5, daily_atr=5.0, h1_atr=1.0, price_low=99.0, price_high=104.0,
                memory_ids={"M1-1H-RES"}, minimum_rr=2.0, language="English")
    args.update(kw)
    return AA.validate_analysis(a, **args)


class AnalysisTests(unittest.TestCase):
    def test_soft_validation_drops_only_bad_items(self):
        clean, dropped = validate(analysis())
        self.assertEqual([s["id"] for s in clean["scenarios"]], ["s1"])
        self.assertEqual(clean["scenarios"][0]["memory_ids"], ["M1-1H-RES"])
        self.assertEqual([i["id"] for i in clean["ideas"]], ["i1"])
        self.assertEqual([w["id"] for w in clean["watches"]], ["w1"])
        self.assertEqual(len(dropped), 3)
        self.assertEqual(clean["ideas"][0]["rr_tp1"], 2.0)
        self.assertEqual(clean["ideas"][0]["risk"], "LOW")

    def test_python_raises_risk_never_lowers(self):
        a = analysis(); a["ideas"][0]["targets"] = [104.0]
        clean, _ = validate(a)
        idea = clean["ideas"][0]
        self.assertEqual((idea["risk"], idea["astra_risk"], idea["python_risk_raised"]), ("MEDIUM", "LOW", True))
        self.assertTrue(any("R:R" in f for f in idea["risk_factors"]))
        clean, _ = validate(analysis(), calendar_block="FOMC at 18:00", mtf_opposed=lambda d: True)
        self.assertEqual(clean["ideas"][0]["risk"], "HIGH")
        a = analysis(); a["ideas"][0]["risk"] = "HIGH"
        self.assertEqual(validate(a)[0]["ideas"][0]["risk"], "HIGH")

    def test_sub_1r_kept_as_high_risk_valid_now_checked_python_factors_first(self):
        a = analysis(); a["ideas"][0]["targets"] = [102.0]
        clean, dropped = validate(a)
        idea = clean["ideas"][0]                                            # always an idea, labelled honestly
        self.assertEqual((idea["risk"], idea["rr_tp1"]), ("HIGH", 0.4))
        self.assertIn("smaller than risk", idea["risk_factors"][0])
        a = analysis(); a["ideas"][0]["readiness"] = "VALID_NOW"; a["ideas"][0]["risk_factors"] = ["x"] * 7
        a["ideas"][0]["risk_factors"] = ["x"] * 7
        clean, _ = validate(a, calendar_block="CPI at 12:30")
        idea = clean["ideas"][0]
        self.assertEqual(idea["readiness"], "NEEDS_CONFIRMATION")            # price 102.5 not in 100-101
        self.assertIn("CPI", idea["risk_factors"][0])
        clean, _ = validate(a, price=100.5)
        self.assertEqual(clean["ideas"][0]["readiness"], "VALID_NOW")

    def test_sl_tp_anchored_to_remembered_levels_and_market_entry(self):
        zones = [{"id": "Z1", "timeframe": "4h", "role": "RESISTANCE", "low": 105.8, "high": 106.4, "tests": 3},
                 {"id": "Z2", "timeframe": "1h", "role": "SUPPORT", "low": 98.0, "high": 98.6, "tests": 2}]
        clean, _ = validate(analysis(), zones=zones)
        idea = clean["ideas"][0]
        self.assertEqual(idea["tp_anchors"][0]["id"], "Z1")
        self.assertIsNone(idea["tp_anchors"][1])                            # 108 is not a remembered level
        self.assertEqual(idea["sl_anchor"]["id"], "Z2")
        text = AA.idea_text(idea, "story", [], T0, "English", 102.5)
        self.assertIn("TP1: 106 — at 4H Resistance 105.8–106.4", text)
        self.assertIn("Confidence: 65%", text)
        a = analysis(); a["ideas"][0]["targets"] = [107.0]                   # 0.6 away from Z1: not "at" it
        clean, _ = validate(a, zones=zones)
        self.assertIn("TP1 is not at a remembered level.", clean["ideas"][0]["risk_factors"])
        a = analysis(); a["ideas"][0]["entry_type"] = "MARKET"
        self.assertEqual(validate(a)[0]["ideas"][0]["entry_type"], "LIMIT")   # price 102.5 not in 100-101
        self.assertEqual(validate(a, price=100.5)[0]["ideas"][0]["entry_type"], "MARKET")
        lv = [{"id": "m15_supply", "timeframe": "15min", "kind": "SUPPLY", "low": 106.9, "high": 107.2}]
        a = analysis(); a["ideas"][0]["targets"] = [107.0]
        clean, _ = validate(a, zones=AA.anchor_levels(zones, lv))
        self.assertEqual(clean["ideas"][0]["tp_anchors"][0]["id"], "m15_supply")   # Astra's own map level
        self.assertIn("at 15M Supply 106.9–107.2", AA.idea_text(clean["ideas"][0], "s", [], T0, "English"))
        clean, _ = validate(analysis(), calendar_block="USD holiday (all day): Columbus Day")
        self.assertEqual(clean["ideas"][0]["risk"], "MEDIUM")

    def test_chasing_flagged_and_stance_recomputed(self):
        clean, _ = validate(analysis(), price=103.5)
        self.assertTrue(any("wait for the pullback" in f for f in clean["ideas"][0]["risk_factors"]))  # LIMIT idea
        a = analysis(); a["ideas"][0]["entry_type"] = "BREAKOUT"
        self.assertTrue(any("chase" in f for f in validate(a, price=103.5)[0]["ideas"][0]["risk_factors"]))
        a = analysis(ideas=[], stance="TRADE_IDEA")
        self.assertEqual(validate(a)[0]["stance"], "STRUCTURE_WATCH")
        a = analysis(ideas=[], watches=[])
        self.assertEqual(validate(a)[0]["stance"], "NO_TRADE")
        self.assertIsNone(validate({"stance": "NO_TRADE"})[0])

    def test_schema_is_strict_compatible(self):
        def walk(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertFalse(node["additionalProperties"])
                    self.assertEqual(set(node["required"]), set(node["properties"]))
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(M.MAP_SCHEMA)
        self.assertIn("analysis", M.MAP_SCHEMA["required"])

    def test_reports_render_in_both_languages(self):
        clean, _ = validate(analysis())
        z = {"id": "M1-1H-RES", "kind": "SR", "timeframe": "4h", "role": "SUPPORT", "original_role": "RESISTANCE",
             "status": "BROKEN", "low": 4180.0, "high": 4185.0, "created_at": AM.iso(T0), "tests": 3,
             "last_tested_at": AM.iso(T0 + timedelta(days=2)), "reactions": [1.2, 0.8], "broken_at": AM.iso(T0 + timedelta(days=3)),
             "break_direction": "UP", "retest": "WAITING", "false_breakouts": 0}
        now = T0 + timedelta(days=3, hours=5)
        for lang in ("English", "Arabic"):
            text = AA.idea_text(clean["ideas"][0], clean["market_story"], [z], now, lang, 102.5)
            self.assertIn("100–101", text)
            self.assertIn("TP1: 106", text)
            self.assertLessEqual(len(text), 3900)
            self.assertIn(AA.labels(lang)["idea_footer"], text)
            w = AA.watch_text(clean["watches"][0], z, now, lang)
            self.assertIn("4,180–4,185", w)
        en = AA.idea_text(clean["ideas"][0], clean["market_story"], [z], now, "English")
        self.assertIn("BUY IDEA", en)
        self.assertIn("tested 3×", en)
        self.assertIn("Risk: Low", en)


class AppIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.c = M.load_config()
        self.c["providers"] = dict(self.c["providers"], broker_quotes="none")
        self.c.update(require_broker_quotes=False, telegram_language="English")
        self.now = M.stamp("2026-10-06T12:00:00Z")
        self.history = {tf: [{"time": M.iso(self.now - timedelta(seconds=s * i)), "open": 100., "high": 104.,
                              "low": 99., "close": 102.} for i in range(220, 0, -1)] for tf, s in M.TF.items()}
        self.dir = tempfile.TemporaryDirectory(); self.addCleanup(self.dir.cleanup)

    def app(self, **over):
        with patch.object(threading.Thread, "start"):
            a = M.App(dict(self.c, state_directory=self.dir.name, **over))
        self.addCleanup(lambda: (a.journal.db.close(), a.memory and a.memory.db.close(),
                                 a.pool.shutdown(wait=False, cancel_futures=True)))
        a.active = a.warmed = True; a.epoch = "test"
        a.watcher = M.Watcher(a.c, "2026-10-06"); a.history = copy.deepcopy(self.history)
        feed = M.QuoteFeed(a.c); feed.record(102.5, self.now, now=self.now); a.feed = feed
        a.calendar = {"fetched_at": M.iso(self.now), "events": []}
        a.evidence = None
        z = a.memory._new_zone("4h", "RESISTANCE", 103.0, 104.0, M.iso(self.now - timedelta(days=3)),
                               M.iso(self.now - timedelta(days=3)), "ENGINE")
        z["tests"] = 3; a.memory.flush()
        self.zid = z["id"]
        return a

    def map_with(self, a, analysis_obj):
        data = {"bias": {tf: "NEUTRAL" for tf in M.TF}, "levels": [], "setups": [], "summary": "s",
                "no_setup_reason": "none", "analysis": analysis_obj}
        return ({"kind": "map", "tag": {"epoch": "test", "history": self.history, "born": M.iso(self.now)}},
                "result", {"technical": data, "news": None})

    def run_map(self, a, an):
        a.analyst = Mock(); a.analyst.poll.return_value = self.map_with(a, an)
        with patch.object(M, "history_fresh", return_value=True):
            a.consume_ai(self.now)

    def adapted(self, zid):
        an = analysis()
        for part in ("scenarios", "ideas"):
            for x in an[part]:
                x["memory_ids"] = [zid]
        an["watches"][0]["memory_id"] = zid
        return an

    def queued(self, a):
        return [k for k, _ in list(a.outgoing.queue) + list(a.outgoing_reports.queue)]

    def test_idea_and_watch_reported_once_never_as_signal(self):
        a = self.app()
        self.run_map(a, self.adapted(self.zid))
        keys = self.queued(a)
        self.assertEqual(sum(k.startswith("astra-idea:") for k in keys), 1, keys)
        self.assertEqual(sum(k.startswith("astra-watch:") for k in keys), 1, keys)
        self.assertFalse(any(k.endswith(":signal") for k in keys))
        self.assertFalse([k for k, _ in a.outgoing.queue if k.startswith("astra-")])   # never in the signal queue
        packet = dict(list(a.outgoing_reports.queue))[next(k for k in keys if k.startswith("astra-idea:"))]
        self.assertFalse(packet["plan"]); self.assertIsNone(packet["sid"])
        self.assertIn("tested 3×", packet["text"])
        a.astra_report_at = {"idea": 0, "watch": 0}
        self.run_map(a, self.adapted(self.zid))
        self.assertEqual(len(self.queued(a)), len(keys))              # identical idea/watch: no duplicates
        self.assertEqual(a.analysis["stance"], "TRADE_IDEA")
        rows = a.memory.db.execute("SELECT count(*) FROM predictions").fetchone()[0]
        self.assertEqual(rows, 2)                                       # 1 scenario + 1 idea, both scored later

    def test_interval_throttles_and_reports_can_be_disabled(self):
        a = self.app()
        self.run_map(a, self.adapted(self.zid))
        n = len(self.queued(a))
        an = self.adapted(self.zid); an["ideas"][0]["entry_low"] = 99.0; an["ideas"][0]["entry_high"] = 99.5
        an["ideas"][0]["invalidation"] = 97.0                               # new idea, but inside the interval
        self.run_map(a, an)
        self.assertEqual(len(self.queued(a)), n)
        quiet = self.app(astra_reports=False)
        self.run_map(quiet, self.adapted(self.zid))
        self.assertEqual(self.queued(quiet), [])
        self.assertIsNotNone(quiet.analysis)

    def test_ideas_withheld_in_calendar_window_watch_still_sent(self):
        a = self.app()
        a.calendar = {"fetched_at": M.iso(self.now), "events": [
            {"title": "CPI", "impact": "High", "time": M.iso(self.now + timedelta(minutes=10))}]}
        self.run_map(a, self.adapted(self.zid))
        keys = self.queued(a)
        self.assertFalse(any(k.startswith("astra-idea:") for k in keys))
        self.assertTrue(any(k.startswith("astra-watch:") for k in keys))

    def test_v6_failure_never_touches_map_or_loop(self):
        a = self.app()
        with patch.object(M.App, "handle_analysis", side_effect=PermissionError("locked")):
            self.run_map(a, self.adapted(self.zid))                        # must not raise
        self.assertIsNotNone(a.watcher.map)
        self.assertFalse(a.journal.db.execute("SELECT count(*) FROM events WHERE status='MAP_REJECTED'").fetchone()[0])

    def test_signals_are_sent_before_reports(self):
        a = self.app()
        a.outgoing_reports.put(("astra-idea:x", {"plan": False}))
        a.outgoing.put(("s:signal", {"plan": False}))
        order = []
        def final(key, packet, broker_q=None):
            order.append(key)
            if len(order) == 2:
                a.stop_sender.set()
            return False
        with patch.object(a, "final_check", side_effect=final):
            a.send_loop()
        self.assertEqual(order, ["s:signal", "astra-idea:x"])

    def test_rejected_map_still_keeps_analysis(self):
        a = self.app()
        an = self.adapted(self.zid)
        res = self.map_with(a, an)
        res[2]["technical"]["setups"] = [{"bad": True}]
        a.analyst = Mock(); a.analyst.poll.return_value = res
        with patch.object(M, "history_fresh", return_value=True):
            a.consume_ai(self.now)
        self.assertIsNotNone(a.analysis)
        self.assertEqual(a.watcher.map, None)
        keys = self.queued(a)
        self.assertFalse(any(k.startswith("astra-idea:") for k in keys))     # no ideas from a refused map

    def test_sensor_requests_refresh_and_payload_explains_why(self):
        a = self.app()
        a.next_map = a.next_full = time.time() + 999; a.last_reassess = time.time()   # V5 cooldown active
        a.sense_zones(103.5, self.now)
        self.assertEqual(a.next_map, 0)                                   # V6 has its own cooldown
        self.assertEqual(a.last_reassess > 0, True)
        payload = a.memory_payload(self.now)
        self.assertEqual(payload["sensor_trigger"]["zone_id"], self.zid)
        self.assertIn(self.zid, {z["id"] for z in payload["market_memory"]["zones"]})
        self.assertNotIn("sensor_trigger", a.memory_payload(self.now))
        a.next_map = time.time() + 999; a.last_v6_refresh = 0
        a.sense_zones(103.6, self.now)                                    # same visit: no new request
        self.assertNotEqual(a.next_map, 0)

    def test_sensor_entry_not_lost_while_refresh_on_cooldown(self):
        a = self.app()
        a.last_v6_refresh = time.time()                                    # refresh not possible now
        a.sense_zones(103.5, self.now)
        self.assertIsNone(a.memory.get("sensor:" + self.zid))              # entry not consumed
        a.last_v6_refresh = 0
        a.sense_zones(103.5, self.now)
        self.assertEqual(a.sensor_trigger["zone_id"], self.zid)

    def test_daily_report_has_memory_section(self):
        a = self.app()
        self.run_map(a, self.adapted(self.zid))
        lines = a.memory_report_lines(self.now)
        self.assertTrue(any("Astra's view" in x for x in lines))
        self.assertTrue(any("103–104" in x for x in lines))

    def test_config_validation(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"astra_reports": "yes"}, f)
        self.addCleanup(lambda: Path(f.name).unlink())
        with self.assertRaises(ValueError):
            M.load_config(f.name)
        Path(f.name).write_text(json.dumps({"astra_report_min_interval_seconds": 600, "zone_entry_refresh": False}))
        c = M.load_config(f.name)
        self.assertEqual((c["astra_report_min_interval_seconds"], c["zone_entry_refresh"]), (600, False))


if __name__ == "__main__":
    unittest.main()


sys.path.insert(0, str(ROOT / "tools"))
import astra_dry_run as DRY  # noqa: E402


def random_history(now, seed=7):
    import random
    rnd = random.Random(seed)
    hist = {}
    for tf, sec in M.TF.items():
        n, price, bars = 260, 4000.0, []
        end = datetime.fromtimestamp(int(now.timestamp() // sec) * sec, UTC)
        vol = {"15min": 2, "1h": 4, "4h": 8, "1day": 18, "1week": 40}[tf]
        for i in range(n, 0, -1):
            o = price
            c = max(100.0, o + rnd.gauss(0, vol))
            h, l = max(o, c) + abs(rnd.gauss(0, vol / 2)), min(o, c) - abs(rnd.gauss(0, vol / 2))
            bars.append({"time": M.iso(end - timedelta(seconds=sec * i)), "open": round(o, 2), "high": round(h, 2),
                         "low": round(l, 2), "close": round(c, 2)})
            price = c
        hist[tf] = bars
    return hist


class DryRunTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(); self.addCleanup(self.dir.cleanup)
        self.state = Path(self.dir.name) / "dry"
        self.hist = Path(self.dir.name) / "history.json"
        self.hist.write_text(json.dumps(random_history(M.utcnow())))
        self.base = ["--history", str(self.hist), "--state", str(self.state), "--config", "missing.json",
                     "--language", "English"]

    def run_dry(self, *extra):
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()), patch.dict(os.environ, {"OPENAI_API_KEY": "test-not-used"}):
            code = DRY.main(self.base + list(extra))
        return code, (self.state / "report.txt").read_text(encoding="utf-8")

    def test_no_ai_builds_memory_and_payload_only(self):
        code, report = self.run_dry("--no-ai")
        self.assertEqual(code, 0)
        self.assertIn("MARKET MEMORY", report)
        self.assertIn("--no-ai", report)
        self.assertTrue((self.state / "zones.csv").is_file())
        payload = json.loads((self.state / "payload.json").read_text(encoding="utf-8"))
        self.assertIn("market_memory", payload)
        self.assertIn("smc_evidence", payload)

    def canned(self, payload, good=True):
        zid = payload["market_memory"]["zones"][0]["id"]
        an = analysis(ideas=[])
        an["scenarios"] = []
        an["watches"][0]["memory_id"] = zid
        if not good:
            an = {"stance": "NO_TRADE"}
        return ({"bias": {tf: "NEUTRAL" for tf in M.TF}, "levels": [], "setups": [], "summary": "s",
                 "no_setup_reason": "none", "analysis": an}, "fake-model",
                {"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200}, 1.0, 100)

    def test_astra_call_renders_previews_and_is_rerenderable(self):
        with patch.object(DRY, "call_astra", side_effect=lambda c, p: self.canned(p)):
            code, report = self.run_dry()
        self.assertEqual(code, 0)
        self.assertIn("Map ACCEPTED", report)
        self.assertIn("TELEGRAM PREVIEW — STRUCTURE WATCH", report)
        self.assertIn('"input_tokens": 1000', report)
        saved = self.state / "astra_response.json"
        self.assertTrue(saved.is_file())
        code, report = self.run_dry("--raw", str(saved))                # free re-render
        self.assertEqual(code, 0)
        self.assertIn("Re-rendering saved response", report)

    def test_invalid_analysis_is_reported(self):
        with patch.object(DRY, "call_astra", side_effect=lambda c, p: self.canned(p, good=False)):
            code, report = self.run_dry()
        self.assertEqual(code, 3)
        self.assertIn("ANALYSIS MISSING OR INVALID", report)

    def test_astra_failure_is_reported_not_raised(self):
        with patch.object(DRY, "call_astra", side_effect=ValueError("schema mismatch")):
            code, report = self.run_dry()
        self.assertEqual(code, 2)
        self.assertIn("ASTRA CALL FAILED", report)


import learn_lessons as LL  # noqa: E402


class FakeResponses:
    def __init__(self, decision="BUY", fail_images=False):
        self.calls, self.decision, self.fail_images = [], decision, fail_images

    def create(self, **kw):
        from types import SimpleNamespace
        self.calls.append(kw)
        props = kw["text"]["format"]["schema"]["properties"]
        has_image = any(p.get("type") == "input_image" for p in kw["input"][0]["content"])
        if self.fail_images and has_image:
            raise ValueError("model does not support image input")
        if "marks" in props:
            out = {"marks": [{"label": "fvg", "what_i_see": "three candles, gap", "why_it_qualifies": "imbalance"}],
                   "overall": "bearish imbalance"}
        elif "definitions" in props:
            out = {"agreement": "PARTIAL", "definitions": [
                {"concept": "FVG", "rule": "Gap between candle 1 low and candle 3 high left by a displacement candle."}],
                "astra_missed": "displacement requirement", "astra_note": ""}
        elif "structure_seen" in props:
            out = {"decision": self.decision, "confidence": "MEDIUM", "timeframe_hint": "4h",
                   "structure_seen": "three rejections", "key_areas": ["upper area"], "reasoning": "breakout pressure"}
        elif "trader_decision" in props:
            out = {"trader_decision": "SELL", "agreement": "DISAGREE", "situation": "REPEATED_REJECTION",
                   "structure": "s", "trader_reasons": ["liquidity above"], "astra_missed": "the liquidity",
                   "principles": ["Sell a repeatedly rejected resistance only after a sweep.", " "], "astra_note": ""}
        else:
            out = {"summary": "Wait for sweeps.", "principles": [
                {"id": "x", "situation": "REPEATED_REJECTION", "rule": "Sell only after a sweep of equal highs.",
                 "when_not": "Without the sweep", "lessons": ["001_a"]}]}
        return SimpleNamespace(status="completed", output_text=json.dumps(out), usage=SimpleNamespace(total_tokens=100))


class FakeClient:
    def __init__(self, **kw):
        self.responses = FakeResponses(**kw)


class LessonTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(); self.addCleanup(self.dir.cleanup)
        self.folder = Path(self.dir.name) / "lessons"; self.folder.mkdir()
        (self.folder / "001_a.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        (self.folder / "001_a.txt").write_text("QUESTION: Buy or sell?\nMY VIEW: Sell after the sweep.", encoding="utf-8")
        (self.folder / "002_b.txt").write_text("Text-only lesson: I wait in ranges.", encoding="utf-8")
        (self.folder / "003_c.jpg").write_bytes(b"\xff\xd8fake")
        (self.folder / "README.txt").write_text("instructions, not a lesson", encoding="utf-8")
        env = patch.dict(os.environ, {"GOLD_USE_SAVED_AGENT": "0"}); env.start(); self.addCleanup(env.stop)

    def run_ll(self, client, *extra):
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            return LL.main(["--folder", str(self.folder), "--config", "missing.json", "--language", "English"]
                           + list(extra), client=client)

    def test_parse_text_variants(self):
        q, v = LL.parse_text(self.folder / "001_a.txt")
        self.assertEqual((q, v), ("Buy or sell?", "Sell after the sweep."))
        q, v = LL.parse_text(self.folder / "002_b.txt")
        self.assertEqual((q, v), (LL.DEFAULT_QUESTION, "Text-only lesson: I wait in ranges."))
        self.assertEqual(LL.parse_text(None), (LL.DEFAULT_QUESTION, ""))

    def test_blind_first_then_compare_playbook_report_and_skip(self):
        client = FakeClient()
        self.assertEqual(self.run_ll(client), 0)
        calls = client.responses.calls
        first = json.dumps(calls[0]["input"])
        self.assertNotIn("Sell after the sweep", first)                    # blind: view not shown
        self.assertIn("input_image", first)
        self.assertIn("Sell after the sweep", json.dumps(calls[1]["input"]))
        self.assertTrue(all(c["store"] is False for c in calls))
        pb = json.loads((self.folder / "astra_playbook.json").read_text(encoding="utf-8"))
        self.assertEqual(pb["principles"][0]["id"], "P1")
        rep = (self.folder / "LESSONS_REPORT.md").read_text(encoding="utf-8")
        self.assertIn("Blind agreement:** 0/2", rep)
        self.assertIn("No 'MY VIEW' given", rep)                            # 003_c image only
        self.assertNotIn("README", rep)
        n = len(calls)
        self.assertEqual(self.run_ll(client), 0)                             # unchanged lessons: no calls
        self.assertEqual(len(client.responses.calls), n)
        (self.folder / "002_b.txt").write_text("MY VIEW: changed", encoding="utf-8")
        self.run_ll(client)
        self.assertGreater(len(client.responses.calls), n)                   # only the changed one + playbook

    def test_state_files_saved_with_a_bom_are_still_read(self):
        """14 Nov: astra_lessons.json and astra_playbook.json carried a UTF-8 BOM (saved by an editor) and the
        tool stopped at start with 'Unexpected UTF-8 BOM', so no new lesson could be studied."""
        client = FakeClient()
        self.assertEqual(self.run_ll(client), 0)
        n = len(client.responses.calls)
        for name in ("astra_lessons.json", "astra_playbook.json"):
            p = self.folder / name
            p.write_bytes(b"\xef\xbb\xbf" + p.read_bytes())
        self.assertEqual(self.run_ll(client), 0)                             # both read; nothing studied again
        self.assertEqual(len(client.responses.calls), n)
        self.assertEqual(self.run_ll(client, "--report-only"), 0)

    def test_concept_lesson_teaches_definitions_not_outcomes(self):
        (self.folder / "004_fvg.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        (self.folder / "004_fvg.txt").write_text("TYPE: CONCEPT\nQUESTION: Why is this an FVG?\n"
                                                 "MY VIEW: Big displacement candle leaves a gap between candle 1 and 3.",
                                                 encoding="utf-8")
        client = FakeClient()
        self.assertEqual(self.run_ll(client), 0)
        concept_calls = [c for c in client.responses.calls if "Concept lesson 004_fvg" in json.dumps(c["input"])]
        self.assertEqual(len(concept_calls), 2)
        self.assertNotIn("Big displacement", json.dumps(concept_calls[0]["input"]))   # blind first
        self.assertIn("not discuss what happened afterwards", concept_calls[0]["instructions"])
        rec = json.loads((self.folder / "astra_lessons.json").read_text(encoding="utf-8"))["004_fvg"]
        self.assertEqual((rec["kind"], rec["comparison"]["situation"]), ("CONCEPT", "DEFINITION"))
        self.assertTrue(rec["comparison"]["principles"][0].startswith("FVG: "))
        rep = (self.folder / "LESSONS_REPORT.md").read_text(encoding="utf-8")
        self.assertIn("004_fvg (concept)", rep)
        self.assertIn("Definitions learned", rep)
        self.assertNotIn("TYPE:", rep)

    def test_image_failure_is_reported_and_text_lessons_still_work(self):
        code = self.run_ll(FakeClient(fail_images=True))
        self.assertEqual(code, 2)
        rep = (self.folder / "LESSONS_REPORT.md").read_text(encoding="utf-8")
        self.assertIn("may not accept images", rep)
        self.assertIn("002_b", rep)

    def test_live_bot_loads_capped_playbook(self):
        self.run_ll(FakeClient())
        pb = AA.load_playbook(self.folder / "astra_playbook.json")
        self.assertEqual(pb["principles"][0]["rule"], "Sell only after a sweep of equal highs.")
        self.assertIsNone(AA.load_playbook(self.folder / "missing.json"))
        big = {"principles": [{"id": f"P{i}", "situation": "RANGE", "rule": "x" * 300} for i in range(40)]}
        (self.folder / "big.json").write_text(json.dumps(big))
        loaded = AA.load_playbook(self.folder / "big.json")
        self.assertLessEqual(len(json.dumps(loaded["principles"])), 9000)
        self.assertLessEqual(len(loaded["principles"]), 12)
        long = {"principles": [{"id": "P1", "situation": "DEFINITION", "rule": "r" * 900, "when_not": "w" * 500}]}
        (self.folder / "long.json").write_text(json.dumps(long))
        one = AA.load_playbook(self.folder / "long.json")["principles"][0]
        self.assertEqual((len(one["rule"]), len(one["when_not"])), (700, 300))
        (self.folder / "bad.json").write_text("{not json")
        self.assertIsNone(AA.load_playbook(self.folder / "bad.json"))

    def test_playbook_reaches_map_payload(self):
        self.run_ll(FakeClient())
        it = AppIntegrationTests("test_config_validation"); it.setUp()
        try:
            a = it.app(playbook_file=str(self.folder / "astra_playbook.json"))
            self.assertEqual(a.trader_playbook()["principles"][0]["id"], "P1")
            b = it.app(playbook_file="")
            self.assertIsNone(b.trader_playbook())
        finally:
            it.doCleanups()
