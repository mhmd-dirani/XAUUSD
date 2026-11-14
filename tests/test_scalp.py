"""The trend-pullback scalp: the replay candidate (scalp_model) and the scalp lab engine.
Offline: no MT5, no network, no Claude, no Telegram."""

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent / "src", HERE.parent / "tools", HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from autotrader import alerts, scalp as SP, scalp_model as SC, settings  # noqa: E402

UTC = timezone.utc


# ----------------------------------------------------------------------------------------------------------------------
# 11 Nov: the 1-minute trend-pullback scalp candidate (replay only - no engine, no orders).
# ----------------------------------------------------------------------------------------------------------------------
SESS = {"ASIA": {"tz": "Asia/Tokyo", "start": "09:00", "end": "15:00"},
        "LONDON": {"tz": "Europe/London", "start": "08:00", "end": "16:30"},
        "NEW_YORK": {"tz": "America/New_York", "start": "08:00", "end": "17:00"}}


def trend_m1(n=3000, drift=0.03, seed=1):
    import random
    rnd = random.Random(seed)
    t0 = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
    px, out = 4000.0, []
    for i in range(n):
        o = px
        c = o + drift + rnd.gauss(0, 0.35)
        out.append({"t": t0 + timedelta(minutes=i), "o": o, "h": max(o, c) + abs(rnd.gauss(0, 0.15)),
                    "l": min(o, c) - abs(rnd.gauss(0, 0.15)), "c": c, "s": 0.3})
        px = c
    return out


class PullbackTest(unittest.TestCase):
    def test_ema_and_context(self):
        self.assertEqual(SC.ema([1.0] * 30, 9)[-1], 1.0)
        self.assertIsNone(SC.ema([1.0] * 5, 9)[-1])
        ctx = SC.context_5m(trend_m1(drift=0.05))
        self.assertGreater(ctx.count("UP"), 5 * ctx.count("DOWN"))
        ctx = SC.context_5m(trend_m1(drift=-0.05))
        self.assertGreater(ctx.count("DOWN"), 5 * ctx.count("UP"))

    def test_trades_follow_the_5m_trend_and_carry_costs(self):
        res = SC.run_pullbacks(trend_m1(drift=0.05), {"spread": 0.3, "slippage": 0.1}, SESS)
        tr = res["PULLBACK_rr1.5"]
        self.assertTrue(tr)
        self.assertGreater(sum(t.direction == "BUY" for t in tr), sum(t.direction == "SELL" for t in tr))
        for t in tr:
            self.assertGreaterEqual(t.cost, 0.4)                       # spread 0.30 + slippage 0.10
            risk = abs(t.fill - t.stop)
            self.assertAlmostEqual(abs(t.target - t.fill), 1.5 * risk, places=1)

    def test_no_look_ahead(self):
        m1 = trend_m1(n=4000, drift=0.02, seed=5)
        full = SC.run_pullbacks(m1, {"spread": 0.3, "slippage": 0.1}, SESS)["PULLBACK_rr2.0"]
        cut = m1[2500]["t"]
        part = SC.run_pullbacks(m1[:2500], {"spread": 0.3, "slippage": 0.1}, SESS)["PULLBACK_rr2.0"]
        done_full = [(t.created, t.fill, t.exit_price) for t in full if t.exit_time < cut - timedelta(minutes=2)]
        done_part = [(t.created, t.fill, t.exit_price) for t in part if t.exit_time < cut - timedelta(minutes=2)]
        self.assertEqual(done_full, done_part)

    def test_5m_entries(self):
        m1 = trend_m1(n=9000, drift=0.03, seed=2)
        m5 = SC.aggregate(m1, 300)
        self.assertEqual(len(m5), 1800)
        self.assertEqual(m5[1]["o"], m1[5]["o"])
        self.assertEqual(m5[0]["c"], m1[4]["c"])
        self.assertEqual(m5[0]["h"], max(b["h"] for b in m1[:5]))
        res = SC.run_pullbacks_5m(m1, {"spread": 0.3, "slippage": 0.1}, SESS)
        tr = res["PB5M_rr2.0"]
        self.assertTrue(tr)
        for t in tr:
            self.assertEqual(int(t.created.timestamp()) % 300, 0)
            self.assertAlmostEqual(abs(t.target - t.fill), 2.0 * abs(t.fill - t.stop), places=1)
        part = SC.run_pullbacks_5m(m1[:6000], {"spread": 0.3, "slippage": 0.1}, SESS)["PB5M_rr2.0"]
        cut = m1[5990]["t"]
        self.assertEqual([(t.created, t.fill) for t in tr if t.exit_time < cut - timedelta(minutes=10)],
                         [(t.created, t.fill) for t in part if t.exit_time < cut - timedelta(minutes=10)])

    def test_report_has_both_families(self):
        m1 = trend_m1(n=3000)
        res = SC.run_all(m1, {"spread": 0.3, "slippage": 0.1}, ([], []), SESS)
        res.update(SC.run_pullbacks(m1, {"spread": 0.3, "slippage": 0.1}, SESS))
        text = "\n".join(SC.report_all(res, m1[2100]["t"]))
        self.assertIn("PULLBACK_rr2.0", text)
        self.assertIn("SCALP_k2.0_s1.5", text)
        self.assertIn("Pass bar", text)


# ----------------------------------------------------------------------------------------------------------------------
# 11 Nov: the scalp lab (5M pullback forward test on the demo lab, minimum lot).
# ----------------------------------------------------------------------------------------------------------------------
def m1_series(n=24000, drift=0.03, seed=3):
    import random
    rnd = random.Random(seed)
    t0 = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
    px, out = 4000.0, []
    for i in range(n):
        d = drift if (i // 2000) % 2 == 0 else -drift
        o = px
        c = o + d + rnd.gauss(0, 0.35)
        out.append({"t": t0 + timedelta(minutes=i), "o": o, "h": max(o, c) + abs(rnd.gauss(0, 0.15)),
                    "l": min(o, c) - abs(rnd.gauss(0, 0.15)), "c": c, "s": 0.3})
        px = c
    return out


class SignalTest(unittest.TestCase):
    def test_live_signal_matches_the_replay(self):
        m1 = m1_series()
        m5 = SC.aggregate(m1, 300)
        trades = SC.run_pullbacks_5m(m1, {"spread": 0.3, "slippage": 0.1}, SESS)["PB5M_rr2.0"]
        self.assertGreater(len(trades), 20)
        idx = {b["t"]: i for i, b in enumerate(m5)}
        hit = 0
        for t in trades:
            i = idx[t.created]
            window = m5[max(0, i - 399):i + 1]
            s = SP.last_signal(window, 300, 900)
            hit += bool(s and s["direction"] == t.direction)
        self.assertGreaterEqual(hit / len(trades), 0.9)

    def test_plan(self):
        sig = {"direction": "BUY", "atr": 2.0, "lo": 4010.0, "hi": 4020.0, "t": None, "context": "UP"}
        p = SP.plan(sig, {"bid": 4015.0, "ask": 4015.3, "spread": 0.3}, 2.0)
        self.assertEqual(p["entry"], 4015.3)
        self.assertAlmostEqual(p["stop"], 4009.8, places=2)
        self.assertAlmostEqual(p["tp"], 4015.3 + 2 * 5.5, places=2)
        sig = dict(sig, direction="SELL")
        p = SP.plan(sig, {"bid": 4015.0, "ask": 4015.3, "spread": 0.3}, 2.0)
        self.assertGreater(p["stop"], 4020.0)
        self.assertLess(p["tp"], 4015.0)

    def test_variants(self):
        self.assertEqual(SP.family("PB5M_rr2.0")[0], "TIMEFRAME_M5")
        self.assertEqual(SP.family("PULLBACK_rr2.0")[0], "TIMEFRAME_M1")
        with self.assertRaises(ValueError):
            SP.family("SCALP_k2.0_s1.5")


class _State:
    def __init__(self, rows=None):
        self.rows = rows or []

    def all_orders(self):
        return self.rows

    def active_orders(self, include_lab=True):
        return [r for r in self.rows if r["status"] in ("PENDING", "OPEN", "TP1_DONE")]

    def update_order(self, ticket, **f):
        for r in self.rows:
            if r["ticket"] == ticket:
                r.update(f)


class EngineTest(unittest.TestCase):
    NOW = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)

    def setUp(self):
        self._t = alerts.trade
        alerts.trade = lambda *a, **k: None
        m1 = m1_series()
        m5 = SC.aggregate(m1, 300)
        trades = SC.run_pullbacks_5m(m1, {"spread": 0.3, "slippage": 0.1}, SESS)["PB5M_rr2.0"]
        idx = {b["t"]: i for i, b in enumerate(m5)}
        i = next(idx[t.created] for t in trades if SP.last_signal(m5[max(0, idx[t.created] - 399):idx[t.created] + 1], 300, 900))
        self.window = [(int(b["t"].timestamp()), b["o"], b["h"], b["l"], b["c"]) for b in m5[max(0, i - 399):i + 1]]

    def tearDown(self):
        alerts.trade = self._t

    def app(self, rows=None, spread=0.3, block=None):
        sent = []
        st = _State(rows)
        w = self.window

        class X:
            def send(self, order, lots, risk, grade, magic=None):
                sent.append((order, lots, magic))
                t = 900000 + len(sent)
                st.rows.append({"ticket": t, "setup_id": order["setup_id"], "status": "OPEN", "created_utc": "2026-11-11T10:00:00Z",
                                "result": None})
                return t

        last = w[-1][4]
        m = types.SimpleNamespace(rates=lambda tf, n: w[-n:], quote=lambda: {"bid": last, "ask": last + spread, "spread": spread, "age": 1},
                                  symbol_info=lambda: types.SimpleNamespace(trade_stops_level=10, point=0.01))
        lab = types.SimpleNamespace(hard_block=lambda now, check_book=True, check_day_cap=True: block,
                                    size=lambda si, d: (0.01, round(d, 2), None))
        a = types.SimpleNamespace(market=m, state=st, executor=X(), lab=lab)
        c = {"scalp_lab": {"mode": "lab"}, "lab_mode": True, "require_demo": True, "magic_lab": 40003,
             "signal_entry_tolerance": 1.0, "max_entry_deviation_cap": 1.0, "sessions": SESS}
        return SP.ScalpLab(c, a), a, sent

    def test_sends_one_market_scalp_with_a_time_exit(self):
        sp, a, sent = self.app()
        t = sp.tick(self.NOW)
        self.assertTrue(t)
        order, lots, magic = sent[0]
        self.assertEqual((order["order_type"], lots, magic), ("MARKET", 0.01, 40003))
        self.assertTrue(order["setup_id"].startswith("SCP-"))
        self.assertEqual(a.state.rows[0]["exit_utc"], "2026-11-11T14:00:00Z")      # PB5M_rr2.0 holds 240 min
        self.assertIsNone(sp.tick(self.NOW))                                         # same closed candle: nothing

    def test_one_at_a_time_caps_and_stops(self):
        working = [{"ticket": 1, "setup_id": "SCP-BUY-0900", "status": "OPEN", "created_utc": "2026-11-11T09:00:00Z", "result": None}]
        sp, a, sent = self.app(rows=working)
        sp.tick(self.NOW)
        self.assertEqual(sent, [])
        done = [{"ticket": i, "setup_id": f"SCP-BUY-{i:04d}", "status": "CLOSED", "created_utc": "2026-11-11T01:00:00Z",
                 "result": -3.0} for i in range(12)]
        sp, a, sent = self.app(rows=done)
        sp.tick(self.NOW)
        self.assertEqual(sent, [])                                                   # max_per_day 12
        loss = [{"ticket": i, "setup_id": f"SCP-BUY-{i:04d}", "status": "CLOSED", "created_utc": "2026-11-11T01:00:00Z",
                 "result": -9.0} for i in range(3)]
        sp, a, sent = self.app(rows=loss)
        sp.tick(self.NOW)
        self.assertEqual(sent, [])                                                   # -27 <= -25 daily loss stop
        sp, a, sent = self.app(spread=0.8)
        sp.tick(self.NOW)
        self.assertEqual(sent, [])                                                   # spread cap
        sp, a, sent = self.app(block="kill file STOP present")
        sp.tick(self.NOW)
        self.assertEqual(sent, [])

    def test_off_and_demo_only(self):
        sp, a, sent = self.app()
        sp.cfg["mode"] = "off"
        self.assertIsNone(sp.tick(self.NOW))
        sp, a, sent = self.app()
        sp.c["require_demo"] = False
        self.assertIsNone(sp.tick(self.NOW))
        self.assertEqual(sent, [])


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
        self.assertEqual(self._load()["scalp_lab"]["mode"], "off")
        self.assertEqual(self._load(lab_mode=True, scalp_lab={"mode": "lab"})["scalp_lab"]["variant"], "PB5M_rr2.0")
        for kw in ({"scalp_lab": {"mode": "lab"}}, {"lab_mode": True, "scalp_lab": {"mode": "live"}},
                   {"scalp_lab": {"variant": "NOPE_rr2"}}, {"scalp_lab": {"max_spread": 0}}, {"scalp_lab": {"x": 1}}):
            with self.assertRaises(ValueError, msg=str(kw)):
                self._load(**kw)


if __name__ == "__main__":
    unittest.main()
