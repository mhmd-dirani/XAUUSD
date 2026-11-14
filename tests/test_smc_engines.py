#!/usr/bin/env python3
"""Offline tests for gold_smc_engine.py, gold_confluence_v5.py and gold_evidence.py.
Synthetic candles only; no network. Run: python -m unittest -v test_gold_engines.py
"""
import json
import random
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from gold_smc_engine import analyze_frame, normalize_bars, SMCSettings, _atr  # noqa: E402
from gold_confluence_v5 import (analyze_confluence_frame, verify_breaker_retest,  # noqa: E402
                                _rsi_series, _ema_series)
import gold_evidence as EV  # noqa: E402

UTC = timezone.utc
END = datetime(2026, 10, 6, 12, tzinfo=UTC)
TFS = {"15min": 900, "1h": 3600, "4h": 14400, "1day": 86400, "1week": 604800}


def series(closes, tf="15min", end=END, wick=0.3):
    """OHLC from a close path: open = previous close, small wicks."""
    s = TFS[tf]
    n = len(closes)
    bars, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        bars.append({"time": (end - timedelta(seconds=s * (n - i))).isoformat(),
                     "open": o, "high": max(o, c) + wick, "low": min(o, c) - wick, "close": c})
        prev = c
    return bars


def walk(n, seed, start=4000., step=2.):
    r = random.Random(seed)
    out, p = [], start
    for _ in range(n):
        p += r.gauss(0, step)
        out.append(round(p, 3))
    return out


def zigzag(legs, start=4000.):
    """Piecewise-linear path: legs = [(bars, delta), ...]."""
    out, p = [], start
    for bars, delta in legs:
        for _ in range(bars):
            p += delta / bars
            out.append(round(p, 3))
    return out


class EngineTests(unittest.TestCase):
    def test_rejects_unclosed_unsorted_and_malformed(self):
        bars = series(walk(50, 1))
        with self.assertRaisesRegex(ValueError, "not closed"):
            analyze_frame(bars, "15min", as_of=END - timedelta(minutes=10))
        analyze_frame(bars, "15min", as_of=END)
        with self.assertRaisesRegex(ValueError, "ascending"):
            analyze_frame(bars[::-1], "15min")
        bad = [dict(b) for b in bars]; bad[5]["high"] = bad[5]["low"] - 1
        with self.assertRaisesRegex(ValueError, "Malformed"):
            analyze_frame(bad, "15min")
        with self.assertRaises(ValueError):
            analyze_frame(bars, "5min")

    def test_input_not_mutated(self):
        bars = series(walk(60, 2))
        before = json.dumps(bars)
        analyze_frame(bars, "15min")
        self.assertEqual(json.dumps(bars), before)

    def test_bos_then_choch_on_closes(self):
        # up-leg, pullback, higher high (BOS up), then a drop through the last swing low (CHoCH down)
        closes = zigzag([(10, 30), (6, -15), (10, 30), (6, -12), (12, -60)])
        f = analyze_frame(series(closes), "15min")
        kinds = [(e["kind"], e["direction"]) for e in f["events"] if e["kind"] in ("BOS", "CHOCH")]
        self.assertIn(("BOS", "BUY"), kinds)
        self.assertIn(("CHOCH", "SELL"), kinds)
        self.assertLess(kinds.index(("BOS", "BUY")), kinds.index(("CHOCH", "SELL")))
        self.assertEqual(f["trend"], "BEARISH")
        bars = normalize_bars(series(closes), "15min")
        for e in f["events"]:
            if e["kind"] in ("BOS", "CHOCH"):
                bar = next(b for b in bars if b["closes"] == e["at"])
                # a break is a CLOSE beyond the swing level, never a wick
                self.assertTrue(bar["close"] > e["level_low"] if e["direction"] == "BUY" else bar["close"] < e["level_low"])

    def test_no_lookahead_prefix_events_are_stable(self):
        bars = series(walk(400, 3, step=3))
        full = analyze_frame(bars, "15min")
        for cut in (150, 250, 399):
            part = analyze_frame(bars[:cut], "15min")
            last = normalize_bars(bars[:cut], "15min")[-1]["closes"]
            full_ids = {e["id"]: e for e in full["events"]}
            for e in part["events"]:
                if e["kind"] in ("BOS", "CHOCH", "LIQUIDITY_SWEEP", "FVG"):
                    self.assertLessEqual(e["at"], last)
                    self.assertIn(e["id"], full_ids, (cut, e["kind"]))
                    self.assertEqual(full_ids[e["id"]]["at"], e["at"])

    def test_liquidity_sweep_wick_and_close_back(self):
        closes = zigzag([(10, 30), (6, -15), (5, 8)])
        bars = series(closes, wick=0.2)
        swing = max(range(len(bars)), key=lambda i: bars[i]["high"] if i < 12 else 0)
        level = bars[swing]["high"]
        last = bars[-1]["close"]
        bars.append({"time": (END).isoformat(), "open": last, "high": level + 3, "low": last - 0.5, "close": level - 1})
        f = analyze_frame(bars, "15min")
        sweeps = [e for e in f["events"] if e["kind"] == "LIQUIDITY_SWEEP"]
        self.assertTrue(any(e["direction"] == "SELL" and abs(e["level_low"] - level) < 1e-6 for e in sweeps))

    def test_fvg_detection_and_lifecycle(self):
        base = series(zigzag([(30, 5)]), wick=0.2)
        p = base[-1]["close"]
        t = lambda k: (END + timedelta(minutes=15 * k)).isoformat()
        extra = [{"time": t(0), "open": p, "high": p + 0.5, "low": p - 0.5, "close": p + 0.3},
                 {"time": t(1), "open": p + 0.3, "high": p + 12, "low": p + 0.2, "close": p + 11.5},   # displacement
                 {"time": t(2), "open": p + 11.5, "high": p + 13, "low": p + 3, "close": p + 12.5}]
        f = analyze_frame(base + extra, "15min")
        fvg = [e for e in f["events"] if e["kind"] == "FVG" and e["direction"] == "BUY"][-1]
        self.assertAlmostEqual(fvg["level_low"], p + 0.5); self.assertAlmostEqual(fvg["level_high"], p + 3)
        self.assertEqual(fvg["status"], "ACTIVE")
        dip = {"time": t(3), "open": p + 12.5, "high": p + 12.6, "low": p + 2, "close": p + 4}
        self.assertEqual([e for e in analyze_frame(base + extra + [dip], "15min")["events"] if e["id"] == fvg["id"]][0]["status"],
                         "PARTIALLY_FILLED")
        broken = {"time": t(3), "open": p + 12.5, "high": p + 12.6, "low": p - 1, "close": p}
        self.assertEqual([e for e in analyze_frame(base + extra + [broken], "15min")["events"] if e["id"] == fvg["id"]][0]["status"],
                         "INVALIDATED")

    def test_order_block_needs_displacement_and_break(self):
        closes = zigzag([(10, 10), (6, -6), (2, 0.5)])
        bars = series(closes, wick=0.2)
        swing_high = max(b["high"] for b in bars[:12])
        p = bars[-1]["close"]
        t = lambda k: (END + timedelta(minutes=15 * k)).isoformat()
        bars += [{"time": t(0), "open": p + 0.4, "high": p + 0.5, "low": p - 0.6, "close": p - 0.4},    # last bearish
                 {"time": t(1), "open": p - 0.4, "high": swing_high + 6, "low": p - 0.5, "close": swing_high + 5}]
        f = analyze_frame(bars, "15min")
        obs = [e for e in f["events"] if e["kind"] == "ORDER_BLOCK_CANDIDATE" and e["direction"] == "BUY"]
        self.assertTrue(obs)
        ob = obs[-1]
        self.assertEqual((ob["level_low"], ob["level_high"]), (p - 0.6, p + 0.5))
        self.assertLess(ob["at"], ob["confirmed_at"])
        # without displacement (tiny candles) there is no OB
        flat = series(zigzag([(10, 10), (6, -6), (12, 9)]), wick=0.05)
        self.assertFalse([e for e in analyze_frame(flat, "15min", settings=SMCSettings(displacement_atr=5))["events"]
                          if e["kind"] == "ORDER_BLOCK_CANDIDATE"])

    def test_fibonacci_ote_from_confirmed_leg(self):
        no_structure = zigzag([(10, -20), (12, 40), (8, -16), (6, 2)])
        self.assertIsNone(analyze_frame(series(no_structure), "15min")["fibonacci"])   # never forced
        closes = zigzag([(8, 10), (6, -8), (12, 30), (8, -14), (6, 2)])
        f = analyze_frame(series(closes), "15min")
        fib = f["fibonacci"]
        self.assertIsNotNone(fib)
        self.assertEqual(fib["direction"], "BUY")
        rng = fib["swing_high"] - fib["swing_low"]
        self.assertAlmostEqual(fib["levels"]["0.618"], fib["swing_high"] - 0.618 * rng, places=4)
        self.assertEqual((fib["level_low"], fib["level_high"]), (fib["levels"]["0.786"], fib["levels"]["0.618"]))
        self.assertIn(fib["status"], ("ACTIVE", "SUPERSEDED", "INVALIDATED"))

    def test_atr_positive_and_wilder(self):
        a = _atr(normalize_bars(series(walk(50, 4)), "15min"))
        self.assertTrue(all(x > 0 for x in a))
        self.assertEqual(len(a), 50)


class ConfluenceTests(unittest.TestCase):
    def test_indicators_on_known_series(self):
        rising = [100. + i for i in range(30)]
        rsi = _rsi_series(rising, 14)
        self.assertIsNone(rsi[13]); self.assertEqual(rsi[-1], 100.0)
        self.assertEqual(_ema_series([5.] * 10, 3)[-1], 5.)
        bars = normalize_bars(series(walk(260, 5)), "15min")
        out = analyze_confluence_frame(bars, "15min")
        ind = out["indicators"]
        self.assertAlmostEqual(ind["macd_histogram"], ind["macd_line"] - ind["macd_signal"], places=5)
        self.assertIn(ind["macd_crossover_on_last_closed_bar"], ("BULLISH_CROSS", "BEARISH_CROSS", "NONE"))
        with self.assertRaisesRegex(ValueError, "210"):
            analyze_confluence_frame(bars[:150], "15min")

    def test_breaker_break_after_confirmation_then_retest(self):
        closes = zigzag([(200, 60), (10, 10), (6, -6), (2, 0.5)])
        bars = series(closes, wick=0.2)
        swing_high = max(b["high"] for b in bars[-18:-6])
        p = bars[-1]["close"]
        t = lambda k: (END + timedelta(minutes=15 * k)).isoformat()
        bars += [{"time": t(0), "open": p + 0.4, "high": p + 0.5, "low": p - 0.6, "close": p - 0.4},
                 {"time": t(1), "open": p - 0.4, "high": swing_high + 6, "low": p - 0.5, "close": swing_high + 5}]
        q = swing_high + 5
        # bullish OB fails: decisive close below its low -> SELL breaker; later retest from below
        bars += [{"time": t(2), "open": q, "high": q + 0.2, "low": p - 3, "close": p - 2.5},
                 {"time": t(3), "open": p - 2.5, "high": p - 1.5, "low": p - 4, "close": p - 3.5},
                 {"time": t(4), "open": p - 3.5, "high": p + 0.1, "low": p - 3.6, "close": p - 1.5}]
        nb = normalize_bars(bars, "15min")
        smc = analyze_frame(nb, "15min")
        out = analyze_confluence_frame(nb, "15min", smc)
        br = [b for b in out["breakers"] if b["direction"] == "SELL"]
        self.assertTrue(br)
        b = br[-1]
        self.assertEqual(b["status"], "RETEST_CONFIRMED")
        ob = next(e for e in smc["events"] if e["id"] == b["original_ob_id"])
        self.assertGreater(b["breakout_at"], ob["confirmed_at"])
        self.assertGreater(b["retest_at"], b["breakout_at"])
        verify_breaker_retest(out, b["id"], "SELL")
        with self.assertRaises(ValueError):
            verify_breaker_retest(out, b["id"], "BUY")
        # a later close back above the far edge invalidates it
        bars.append({"time": t(5), "open": p - 1.5, "high": p + 2, "low": p - 1.6, "close": p + 1.5})
        nb = normalize_bars(bars, "15min")
        out2 = analyze_confluence_frame(nb, "15min")
        self.assertEqual([x for x in out2["breakers"] if x["id"] == b["id"]][0]["status"], "INVALIDATED")


class EvidenceTests(unittest.TestCase):
    def history(self, seed=6):
        h = {}
        for i, (tf, s) in enumerate(TFS.items()):
            h[tf] = series(walk(300, seed + i, step=1.5 * (s / 900) ** .25), tf)
        return h

    def test_snapshot_shape_ids_and_determinism(self):
        h = self.history()
        a, b = EV.build_evidence(h, END), EV.build_evidence(h, END)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        self.assertTrue(all(f["available"] for f in a["frames"].values()))
        for i, item in a["index"].items():
            self.assertEqual(item["id"], i)
            self.assertIn(item["kind"], EV.USABLE)
            self.assertLessEqual(item["at"], END.isoformat())
        compact = EV.compact(a)
        for f in compact["frames"].values():
            for item in f["zones"]:
                self.assertIn(item["status"], EV.USABLE[item["kind"]])
        self.assertLess(len(json.dumps(compact)), 60000)

    def test_failed_frame_contributes_nothing(self):
        h = self.history()
        h["1week"] = h["1week"][:50]          # too short for EMA200 -> frame unavailable
        ev = EV.build_evidence(h, END)
        self.assertFalse(ev["frames"]["1week"]["available"])
        self.assertFalse([i for i in ev["index"].values() if i["timeframe"] == "1week"])
        self.assertTrue(ev["frames"]["15min"]["available"])
        self.assertTrue(EV.mtf_opposed("BUY", {"frames": {}, "index": {}}))   # unknown structure caps the grade

    def test_verify_against_real_engine_output(self):
        ev = EV.build_evidence(self.history(), END)
        zones = [i for i in ev["index"].values() if i["kind"] in ("FVG", "ORDER_BLOCK_CANDIDATE")
                 and i["timeframe"] == "15min" and i["status"] in EV.USABLE[i["kind"]]]
        bos = [i for i in ev["index"].values() if i["kind"] == "BOS" and i["timeframe"] in ("1h", "4h")]
        pair = next(((z, b) for z in zones for b in bos if z["direction"] == b["direction"]), None)
        if pair is None:
            self.skipTest("random fixture produced no matching zone/BOS pair")
        z, b = pair
        opposite = "BEARISH" if z["direction"] == "BUY" else "BULLISH"
        ev["frames"]["4h"]["trend"] = "NEUTRAL" if ev["frames"]["4h"]["trend"] == opposite else ev["frames"]["4h"]["trend"]
        setup = {"direction": z["direction"], "playbook": "TREND_CONTINUATION", "evidence_ids": [b["id"], z["id"]],
                 "conditions": [{}]}
        out = EV.verify_setup(setup, {"low": z["level_low"], "high": z["level_high"]}, ev, 0.5)
        self.assertEqual(out["required"], sorted([b["id"], z["id"]]))
        self.assertEqual(EV.recheck(dict(setup, verified_evidence=out), ev)[0], "OK")


if __name__ == "__main__":
    unittest.main(verbosity=2)
