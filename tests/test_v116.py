"""1.16: opposite live trades (the newer trade runs, the older one is managed) and live limits vs a newer analysis.
10 Nov: live BUY 4021.82 (stop 4013.50) open when an opposite SELL 4025.26 opened."""
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

from autotrader import alerts, conflict as CF, settings  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 11, 10, 20, 0, tzinfo=UTC)
T = int(NOW.timestamp())


def candles(direction: str, n=70, step=300, t_end=T):
    """A zig-zag trend: 4 candles with the trend, 2 against, so fractal swings form and then break."""
    out, px = [], 4030.0
    d = 1.0 if direction == "BULLISH" else -1.0
    for i in range(n):
        with_trend = i % 6 < 4
        move = d * (2.0 if with_trend else -1.2)
        o, c = px, px + move
        up = c > o
        h = max(o, c) + (0.3 if up else 0.05)
        l = min(o, c) - (0.05 if up else 0.3)
        out.append((t_end - (n - i) * step, o, h, l, c))
        px = c
    return out


class PureTest(unittest.TestCase):
    def test_trend(self):
        self.assertEqual(CF.trend(candles("BULLISH"), 60, 2), "BULLISH")
        self.assertEqual(CF.trend(candles("BEARISH"), 60, 2), "BEARISH")
        self.assertEqual(CF.trend(candles("BEARISH")[:4], 60, 2), "NONE")

    def test_verdict(self):
        self.assertEqual(CF.verdict(True, 4030.0, 4021.82, 4022.0, "BEARISH", "BEARISH"), "safe")     # locked
        self.assertEqual(CF.verdict(True, 4029.7, 4021.82, 4013.5, "BEARISH", "BEARISH"), "take_profit")
        self.assertEqual(CF.verdict(True, 4018.0, 4021.82, 4013.5, "BEARISH", "BEARISH"), "close")
        self.assertEqual(CF.verdict(True, 4018.0, 4021.82, 4013.5, "BEARISH", "BULLISH"), "wait")
        self.assertEqual(CF.verdict(False, 4030.0, 4025.26, 4033.17, "BULLISH", "BULLISH"), "close")
        self.assertEqual(CF.verdict(False, 4020.0, 4025.26, 4033.17, "BULLISH", "BULLISH"), "take_profit")


class _Market:
    symbol = "XAUUSD"

    def __init__(self, bid, m5="NONE", m1="NONE"):
        self.bid, self.m5, self.m1 = bid, m5, m1
        self.positions = []
        self.n5 = 0

    def quote(self):
        return {"bid": self.bid, "ask": self.bid + 0.3, "spread": 0.3, "age": 1}

    def trade_call(self, name, **kw):
        return self.positions if name == "positions_get" else []

    def rates(self, tf, n):
        kind = self.m5 if tf == "TIMEFRAME_M5" else self.m1
        bars = candles(kind if kind != "NONE" else "BULLISH", step=300 if tf == "TIMEFRAME_M5" else 60,
                       t_end=T + 300 * self.n5)
        if kind == "NONE":
            bars = [(t, 4020.0, 4020.5, 4019.5, 4020.0) for t, *_ in bars]
        return bars


class _State:
    def __init__(self, rows):
        self.rows = rows

    def active_orders(self, include_lab=True):
        return [r for r in self.rows if r["status"] in ("PENDING", "OPEN", "TP1_DONE") and (include_lab or not r.get("lab"))]

    def update_order(self, ticket, **f):
        for r in self.rows:
            if r["ticket"] == ticket:
                r.update(f)


class _Exec:
    def __init__(self):
        self.closed, self.targets, self.cancelled = [], [], []

    def close_position(self, pos, why, comment="TIME"):
        self.closed.append((pos.ticket, comment, why))
        return True

    def set_target(self, pos, tp, why):
        self.targets.append((pos.ticket, tp))
        pos.tp = tp
        return True

    def cancel(self, ticket, why, status="CANCELLED"):
        self.cancelled.append((ticket, why))
        return True


def rows(old_lab=0, new_lab=0, extra=()):
    return [
        {"ticket": 1, "position_ticket": 1, "setup_id": "IDEA-BUY-4028", "direction": "BUY", "status": "OPEN", "lab": old_lab,
         "entry": 4021.83, "sl": 4013.5, "tp1": 4054.39, "fill_utc": "2026-11-10T17:00:03Z", "created_utc": "2026-11-10T17:00:01Z"},
        {"ticket": 2, "position_ticket": 2, "setup_id": "IDEA-SELL-4025", "direction": "SELL", "status": "OPEN", "lab": new_lab,
         "entry": 4025.32, "sl": 4033.17, "tp1": 3990.0, "fill_utc": "2026-11-10T19:40:00Z", "created_utc": "2026-11-10T19:40:00Z"},
    ] + list(extra)


def app(bid, m5="NONE", m1="NONE", rws=None, sl_old=4013.5):
    m = _Market(bid, m5, m1)
    m.positions = [types.SimpleNamespace(ticket=1, price_open=4021.82, sl=sl_old, tp=4061.26, type=0, volume=0.01),
                   types.SimpleNamespace(ticket=2, price_open=4025.26, sl=4033.17, tp=3990.0, type=1, volume=0.01)]
    a = types.SimpleNamespace(market=m, state=_State(rws if rws is not None else rows()), executor=_Exec(),
                              bridge=types.SimpleNamespace(analysis=lambda now: (None, None)),
                              market_context=lambda: ({}, 5.0))
    cf = CF.Conflicts({"opposite_trades": {"mode": "live", "check_seconds": 0.000001}, "duplicate_atr": 0.5,
                       "confirm_entry_tolerance": 3.0}, a)
    return cf, a


class ConflictTest(unittest.TestCase):
    def setUp(self):
        self._t = alerts.trade
        alerts.trade = lambda *a, **k: None

    def tearDown(self):
        alerts.trade = self._t

    def test_older_buy_in_profit_is_closed_newer_sell_kept(self):
        cf, a = app(4029.68)
        cf.tick(NOW)
        self.assertEqual([c[:2] for c in a.executor.closed], [(1, "OPPOSITE")])
        self.assertIn("profit taken", a.executor.closed[0][2])
        self.assertEqual(a.state.rows[0]["conflict"], "vs IDEA-SELL-4025")

    def test_losing_and_both_trends_against_is_cut(self):
        cf, a = app(4018.0, m5="BEARISH", m1="BEARISH")
        cf.tick(NOW)
        self.assertEqual([c[:2] for c in a.executor.closed], [(1, "OPPOSITE")])
        self.assertIn("cut at", a.executor.closed[0][2])

    def test_losing_but_not_clearly_wrong_waits_for_breakeven_then_times_out(self):
        cf, a = app(4018.0, m5="BEARISH", m1="BULLISH")
        cf.tick(NOW)
        self.assertEqual(a.executor.closed, [])
        self.assertEqual(a.executor.targets, [(1, 4022.12)])          # entry + spread
        cf.tick(NOW + timedelta(minutes=30))
        self.assertEqual(len(a.executor.targets), 1)                  # not re-sent
        cf.tick(NOW + timedelta(minutes=121))
        self.assertEqual([c[:2] for c in a.executor.closed], [(1, "OPPOSITE")])
        self.assertIn("no recovery", a.executor.closed[0][2])

    def test_newer_trade_gone_restores_the_original_target(self):
        cf, a = app(4018.0, m5="BEARISH", m1="BULLISH")
        a.state.rows[0]["tp2"] = 4061.26
        cf.tick(NOW)
        self.assertEqual(a.executor.targets, [(1, 4022.12)])
        a.state.rows[1]["status"] = "CLOSED"                           # the newer SELL was stopped
        cf.tick(NOW + timedelta(minutes=5))
        self.assertEqual(a.executor.targets[-1], (1, 4061.26))
        self.assertIsNone(a.state.rows[0]["conflict"])

    def test_locked_trade_left_to_protection(self):
        cf, a = app(4018.0, m5="BEARISH", m1="BEARISH", sl_old=4022.12)
        cf.tick(NOW)
        self.assertEqual((a.executor.closed, a.executor.targets), ([], []))

    def test_lab_trades_never_start_or_receive_it(self):
        for old_lab, new_lab in ((0, 1), (1, 0), (1, 1)):
            cf, a = app(4029.68, rws=rows(old_lab, new_lab))
            cf.tick(NOW)
            self.assertEqual(a.executor.closed, [], (old_lab, new_lab))

    def test_newer_trade_is_never_the_one_closed(self):
        cf, a = app(4020.0, m5="BULLISH", m1="BULLISH")             # the SELL (newer) is in profit; the BUY waits
        cf.tick(NOW)
        self.assertNotIn(2, [c[0] for c in a.executor.closed])

    def test_off(self):
        cf, a = app(4029.68)
        cf.cfg["mode"] = "off"
        self.assertEqual(cf.tick(NOW), [])
        self.assertEqual(a.executor.closed, [])

    def test_older_pending_that_hedges_a_newer_position_is_cancelled(self):
        pend = {"ticket": 3, "position_ticket": None, "setup_id": "IDEA-BUY-4019", "direction": "BUY", "status": "PENDING",
                "lab": 0, "entry": 4019.0, "sl": 4010.0, "tp1": 4040.0, "created_utc": "2026-11-10T19:00:00Z"}
        r = rows()[1:] + [pend]
        cf, a = app(4020.0, rws=r)
        cf.tick(NOW)
        self.assertEqual([c[0] for c in a.executor.cancelled], [3])


class LiveRecheckTest(unittest.TestCase):
    def setUp(self):
        self._t = alerts.trade
        alerts.trade = lambda *a, **k: None

    def tearDown(self):
        alerts.trade = self._t

    def test_contradicted_live_limit_is_cancelled_once(self):
        pend = {"ticket": 5, "position_ticket": None, "setup_id": "IDEA-BUY-4030", "direction": "BUY", "status": "PENDING",
                "lab": 0, "entry": 4030.0, "sl": 4020.0, "tp1": 4055.0, "created_utc": "2026-11-10T16:00:00Z"}
        cf, a = app(4040.0, rws=[pend])
        an = {"id": "Anew", "created_at": "2026-11-10T17:01:00Z",
              "ideas": [{"id": "I1", "direction": "SELL", "entry_low": 4027.07, "entry_high": 4036.41}]}
        a.bridge = types.SimpleNamespace(analysis=lambda now: (an, 5))
        cf.tick(NOW)
        self.assertEqual([c[0] for c in a.executor.cancelled], [5])
        pend["status"] = "PENDING"
        cf.tick(NOW + timedelta(minutes=1))
        self.assertEqual(len(a.executor.cancelled), 1)                # each analysis once

    def test_older_analysis_or_same_side_keeps_it(self):
        pend = {"ticket": 6, "position_ticket": None, "setup_id": "IDEA-BUY-4030", "direction": "BUY", "status": "PENDING",
                "lab": 0, "entry": 4030.0, "sl": 4020.0, "tp1": 4055.0, "created_utc": "2026-11-10T18:00:00Z"}
        cf, a = app(4040.0, rws=[pend])
        an = {"id": "Aold", "created_at": "2026-11-10T17:01:00Z",
              "ideas": [{"id": "I1", "direction": "SELL", "entry_low": 4027.07, "entry_high": 4036.41}]}
        a.bridge = types.SimpleNamespace(analysis=lambda now: (an, 60))
        cf.tick(NOW)
        self.assertEqual(a.executor.cancelled, [])


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

    def test_defaults_and_validation(self):
        c = self._load()
        self.assertEqual((c["opposite_trades"]["mode"], c["limit_entry_risk_pct"]), ("off", None))
        c = self._load(opposite_trades={"mode": "live"}, limit_entry_risk_pct=0.25, idea_entry_mode="limit")
        self.assertEqual((c["opposite_trades"]["mode"], c["limit_entry_risk_pct"]), ("live", 0.25))
        for kw in ({"opposite_trades": {"mode": "lab"}}, {"opposite_trades": {"max_wait_minutes": 0}},
                   {"opposite_trades": {"typo": 1}}, {"limit_entry_risk_pct": 5.0}, {"limit_entry_risk_pct": True}):
            with self.assertRaises(ValueError, msg=str(kw)):
                self._load(**kw)


if __name__ == "__main__":
    unittest.main()
