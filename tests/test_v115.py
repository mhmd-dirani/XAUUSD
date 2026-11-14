"""1.15: trade protection (breakeven / lock / near-TP exit) and the all-timeframe zone score (recorded only).
10 Nov LAB-BUY-4031: filled 4034.01, stop 4024.50, TP1 4054.39, ran to about 4053.4 and closed -1.01 R."""
from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
for p in (HERE.parent / "src", HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from autotrader import alerts, executor as EX, protect as PR, settings, zones as ZN  # noqa: E402

UTC = timezone.utc
CFG = dict(PR.DEFAULTS, mode="all")
T0 = int(datetime(2026, 11, 10, 14, 45, tzinfo=UTC).timestamp())     # a 5-minute boundary


def path_m1(prices, t0=T0):
    """One M1 candle per price step: open = previous close, close = price, high/low = the two."""
    out, prev = [], prices[0]
    for i, p in enumerate(prices):
        out.append((t0 + 60 * i, prev, max(prev, p), min(prev, p), p))
        prev = p
    return out


def ramp(a, b, n):
    return [a + (b - a) * (i + 1) / n for i in range(n)]


class PureRulesTest(unittest.TestCase):
    def test_stages(self):
        self.assertEqual(PR.stages(True, 4034.0, 10.0, 0.9, 0.3, CFG), [])
        st = PR.stages(True, 4034.0, 10.0, 1.2, 0.3, CFG)
        self.assertEqual(st, [(4034.3, "breakeven")])
        st = PR.stages(True, 4034.0, 10.0, 1.6, 0.3, CFG)
        self.assertEqual(st[0], (4039.0, "lock +0.5R"))
        st = PR.stages(False, 4034.0, 10.0, 1.6, 0.3, CFG)
        self.assertEqual(st[0], (4029.0, "lock +0.5R"))
        self.assertAlmostEqual(st[1][0], 4033.7)

    def test_reversal_needs_a_candle_closed_after_arming(self):
        b = [(T0, 10, 12, 9, 11), (T0 + 300, 11, 11.5, 8, 8.5)]
        self.assertTrue(PR.reversal_5m(True, b, T0 + 100))
        self.assertFalse(PR.reversal_5m(True, b, T0 + 600))     # closed before the trade was armed
        self.assertFalse(PR.reversal_5m(False, b, T0 + 100))

    def test_lab_buy_4031_unmanaged_loses_protected_keeps_profit(self):
        entry, sl, tp = 4034.01, 4024.50, 4054.39
        m1 = path_m1(ramp(4034.0, 4053.4, 30) + [4053.0, 4052.0, 4051.0, 4050.0, 4049.0] + ramp(4049.0, 4024.0, 40))
        off = PR.simulate(True, entry, sl, tp, m1, 0.3, None)
        self.assertEqual(off["reason"], "stop")
        self.assertAlmostEqual(off["r"], -1.0, places=2)
        self.assertGreater(off["mfe_r"], 2.0)
        on = PR.simulate(True, entry, sl, tp, m1, 0.3, CFG)
        self.assertEqual(on["reason"], "near-TP reversal")
        self.assertGreater(on["r"], 1.5)
        no_exit = PR.simulate(True, entry, sl, tp, m1, 0.3, dict(CFG, near_tp_exit=False))
        self.assertEqual(no_exit["reason"], "protected stop")
        self.assertAlmostEqual(no_exit["r"], 0.5, places=1)        # the +0.5 R lock

    def test_breakeven_only_trade(self):
        entry, sl, tp = 4100.0, 4090.0, 4130.0
        m1 = path_m1(ramp(4100.0, 4111.5, 10) + ramp(4111.5, 4085.0, 20))
        on = PR.simulate(True, entry, sl, tp, m1, 0.3, CFG)
        self.assertEqual(on["reason"], "protected stop")
        self.assertAlmostEqual(on["r"], 0.03, places=2)             # entry + spread

    def test_winner_untouched(self):
        m1 = path_m1(ramp(4100.0, 4105.0, 5) + ramp(4105.0, 4131.0, 10))
        for cfg in (None, CFG):
            res = PR.simulate(True, 4100.0, 4090.0, 4130.0, m1, 0.3, cfg)
            self.assertEqual(res["reason"], "tp")
            self.assertAlmostEqual(res["r"], 3.0)

    def test_sell_uses_the_ask(self):
        # SELL 4100, stop 4110: bid high 4109.8 + spread 0.3 = ask 4110.1 -> stopped
        m1 = path_m1([4105.0, 4109.8])
        res = PR.simulate(False, 4100.0, 4110.0, 4070.0, m1, 0.3, None)
        self.assertEqual(res["reason"], "stop")


# ----------------------------------------------------------------------------- live protection
class _Pos(types.SimpleNamespace):
    pass


class _Market:
    symbol = "XAUUSD"

    def __init__(self, bid, spread=0.3, m1=None, m5=None):
        self.bid, self.spread, self.m1, self.m5 = bid, spread, m1 or [], m5 or []
        self.positions = []

    def quote(self):
        return {"bid": self.bid, "ask": self.bid + self.spread, "spread": self.spread, "age": 1}

    def symbol_info(self):
        return types.SimpleNamespace(point=0.01, digits=2, trade_stops_level=10, trade_freeze_level=0)

    def trade_call(self, name, **kw):
        return self.positions if name == "positions_get" else []

    def rates(self, tf, n):
        return list(self.m1 if tf == "TIMEFRAME_M1" else self.m5)


class _State:
    def __init__(self, rows):
        self.rows = rows

    def active_orders(self, include_lab=True):
        return [r for r in self.rows if r["status"] in ("PENDING", "OPEN", "TP1_DONE")]

    def update_order(self, ticket, **f):
        for r in self.rows:
            if r["ticket"] == ticket:
                r.update(f)


class _Exec:
    def __init__(self, market):
        self.m = market
        self.moves, self.closes = [], []

    def move_stop(self, pos, sl, why):
        self.moves.append(sl)
        pos.sl = sl
        return True

    def close_position(self, pos, why, comment="TIME"):
        self.closes.append(comment)
        return True


def _app(bid, sl_now=4024.5, lab=1, m1=None, m5=None, mode="all", status="OPEN"):
    m = _Market(bid, m1=m1, m5=m5)
    pos = _Pos(ticket=60571851, price_open=4034.01, sl=sl_now, tp=4054.39, volume=0.01, type=0)
    m.positions = [pos]
    rec = {"ticket": 60571851, "position_ticket": 60571851, "setup_id": "LAB-BUY-4031", "direction": "BUY",
           "status": status, "lab": lab, "entry": 4034.06, "sl": 4024.5, "tp1": 4054.39, "tp2": None,
           "fill_utc": "2026-11-10T14:42:45Z"}
    app = types.SimpleNamespace(market=m, state=_State([rec]), executor=_Exec(m))
    pr = PR.Protect({"trade_protect": dict(CFG, mode=mode, check_seconds=0.000001)}, app)
    return pr, app, pos, rec


class LiveProtectTest(unittest.TestCase):
    def setUp(self):
        self._trade = alerts.trade
        alerts.trade = lambda *a, **k: None

    def tearDown(self):
        alerts.trade = self._trade

    def test_breakeven_then_lock(self):
        pr, app, pos, rec = _app(4045.0)                              # +1.16 R
        pr.tick(datetime(2026, 11, 10, 15, 0, tzinfo=UTC))
        self.assertEqual(app.executor.moves, [4034.31])              # entry + spread
        self.assertEqual(rec["protect"], "breakeven @ 4034.31")
        self.assertAlmostEqual(rec["mfe_r"], 1.16, places=2)
        app.market.bid = 4049.0                                      # +1.58 R
        pr.tick(datetime(2026, 11, 10, 15, 1, tzinfo=UTC))
        self.assertEqual(app.executor.moves[-1], 4038.77)            # entry + 0.5 R
        app.market.bid = 4040.0                                      # falls back: the stop never loosens
        pr.tick(datetime(2026, 11, 10, 15, 2, tzinfo=UTC))
        self.assertEqual(len(app.executor.moves), 2)

    def test_m1_history_counts_when_the_program_was_stopped(self):
        # the trade ran to +1.6 R while nothing watched; price is now back at +0.6 R: the lock (+0.5 R = 4038.77)
        # is still valid below the bid 4039.7, so it is placed
        fill = int(datetime(2026, 11, 10, 14, 42, 45, tzinfo=UTC).timestamp())
        m1 = [(fill - 45, 4030.0, 4060.0, 4030.0, 4034.0),            # the fill minute: ignored (pre-fill prices)
              (fill + 15, 4034.0, 4049.3, 4034.0, 4046.0), (fill + 75, 4046.0, 4046.0, 4039.0, 4039.7)]
        pr, app, pos, rec = _app(4039.7, m1=m1)
        pr.tick(datetime(2026, 11, 10, 15, 30, tzinfo=UTC))
        self.assertEqual(app.executor.moves, [4038.77])
        self.assertLess(rec["mfe_r"], 2.0)                            # the fill-minute high 4060 was not counted

    def test_weaker_stage_when_the_lock_is_no_longer_valid(self):
        fill = int(datetime(2026, 11, 10, 14, 42, 45, tzinfo=UTC).timestamp())
        m1 = [(fill + 15, 4034.0, 4049.3, 4034.0, 4046.0)]
        pr, app, pos, rec = _app(4036.0, m1=m1)                       # bid below the +0.5 R lock
        pr.tick(datetime(2026, 11, 10, 15, 30, tzinfo=UTC))
        self.assertEqual(app.executor.moves, [4034.31])               # breakeven instead

    def test_near_tp_exit_on_a_5m_close_back(self):
        pr, app, pos, rec = _app(4052.5)                              # 91% of the way to 4054.39
        t_arm = datetime(2026, 11, 10, 15, 37, tzinfo=UTC)
        app.market.m5 = [(T0 + 2700, 4050, 4053.4, 4049.5, 4052.8), (T0 + 3000, 4052.8, 4053.0, 4051.0, 4052.0)]
        pr.tick(t_arm)                                                # armed; the last 5M closed before arming
        self.assertEqual(app.executor.closes, [])
        app.market.bid = 4049.5
        app.market.m5 = [(T0 + 3000, 4052.8, 4053.0, 4051.0, 4052.0), (T0 + 3300, 4052.0, 4052.1, 4049.0, 4049.4)]
        pr.tick(datetime(2026, 11, 10, 15, 41, tzinfo=UTC))
        self.assertEqual(app.executor.closes, ["PROTECT"])
        self.assertTrue(rec["protect"].startswith("near-TP exit"))

    def test_scope(self):
        pr, app, pos, rec = _app(4045.0, lab=0, mode="lab")
        pr.tick(datetime(2026, 11, 10, 15, 0, tzinfo=UTC))
        self.assertEqual(app.executor.moves, [])                      # live trade, lab-only scope
        pr, app, pos, rec = _app(4045.0, lab=0, mode="off")
        self.assertEqual(pr.tick(datetime(2026, 11, 10, 15, 0, tzinfo=UTC)), [])

    def test_stale_quote_does_nothing(self):
        pr, app, pos, rec = _app(4045.0)
        app.market.quote = lambda: {"bid": 4045.0, "ask": 4045.3, "spread": 0.3, "age": 60}
        self.assertEqual(pr.tick(datetime(2026, 11, 10, 15, 0, tzinfo=UTC)), [])
        self.assertEqual(app.executor.moves, [])

    def test_stop_already_stronger_is_kept(self):
        pr, app, pos, rec = _app(4045.0, sl_now=4036.0)
        pr.tick(datetime(2026, 11, 10, 15, 0, tzinfo=UTC))
        self.assertEqual(app.executor.moves, [])


# ----------------------------------------------------------------------------- TP1 must not loosen a locked stop
class TP1Test(unittest.TestCase):
    def test_tp1_breakeven_never_loosens(self):
        sent = []
        x = EX.Executor.__new__(EX.Executor)
        x.c = {"magic": 40001}
        x.state = types.SimpleNamespace(update_order=lambda *a, **k: None)
        x.m = types.SimpleNamespace(const=lambda n: 0, symbol="XAUUSD")
        x._send = lambda req: sent.append(req)
        x._filling = lambda: 1
        si = types.SimpleNamespace(volume_step=0.01, volume_min=0.01, digits=2)
        pos = types.SimpleNamespace(ticket=1, volume=0.04, price_open=4000.0, sl=4006.0, tp=4040.0, magic=40001)
        rec = {"ticket": 1, "tp1_close_pct": 50, "be_at_tp1": 1, "direction": "BUY", "setup_id": "IDEA-BUY-4000", "lab": 0}
        old = alerts.trade
        alerts.trade = lambda *a, **k: None
        try:
            x._tp1(rec, pos, si, True, {"bid": 4020.0, "ask": 4020.3})
        finally:
            alerts.trade = old
        self.assertEqual(len(sent), 1)                                # the partial close only, no SLTP to 4000
        self.assertNotIn("sl", sent[0])


# ----------------------------------------------------------------------------- zone score
def h1_bars_with_bull_fvg(t0):
    """H1 candles: a steady base, a 3-candle bullish gap 4020-4026, then price drifting above it."""
    b = []
    px = 4000.0
    for i in range(30):
        b.append((t0 + 3600 * i, px, px + 3, px - 3, px + 0.5))
        px += 0.5
    t = t0 + 3600 * 30
    b += [(t, 4015.0, 4020.0, 4013.0, 4019.0), (t + 3600, 4019.0, 4031.0, 4018.0, 4030.0),
          (t + 7200, 4030.0, 4036.0, 4026.0, 4034.0)]
    for i in range(3, 13):
        b.append((t + 3600 * i, 4034.0, 4038.0, 4031.0, 4035.0))
    return b


class ZoneScoreTest(unittest.TestCase):
    def test_h1_fvg_supports_a_buy(self):
        t0 = int(datetime(2026, 11, 8, 0, 0, tzinfo=UTC).timestamp())
        h1 = h1_bars_with_bull_fvg(t0)
        until = h1[-1][0] + 3600
        zs = ZN.score({"H1": h1}, "BUY", 4026.5, 4017.0, 4046.0, until)
        self.assertEqual(zs["score"], 1)
        self.assertTrue(any(x.startswith("H1:") and "FVG" in x for x in zs["for"]))
        sell = ZN.score({"H1": h1}, "SELL", 4026.5, 4036.0, 4007.0, until)
        self.assertNotIn("FVG", " ".join(sell["for"]))

    def test_point_in_time(self):
        t0 = int(datetime(2026, 11, 8, 0, 0, tzinfo=UTC).timestamp())
        h1 = h1_bars_with_bull_fvg(t0)
        before_gap = t0 + 3600 * 31                                   # the gap's third candle has not closed yet
        zs = ZN.score({"H1": h1}, "BUY", 4026.5, 4017.0, 4046.0, before_gap)
        self.assertNotIn("FVG", " ".join(zs["for"]))

    def test_dead_fvg_does_not_count(self):
        t0 = int(datetime(2026, 11, 8, 0, 0, tzinfo=UTC).timestamp())
        h1 = h1_bars_with_bull_fvg(t0)
        h1.append((h1[-1][0] + 3600, 4035.0, 4035.0, 4010.0, 4015.0))  # closes through the gap's far edge 4020
        until = h1[-1][0] + 3600
        zs = ZN.score({"H1": h1}, "BUY", 4026.5, 4017.0, 4046.0, until)
        self.assertNotIn("FVG", " ".join(zs["for"]))

    def test_columns(self):
        cols = ZN.columns({"score": 2, "against": 1, "for": ["H1:FVG", "M15:OB"], "vs": ["H4:SWING"]})
        self.assertEqual(cols["zone_score"], 2)
        self.assertIn("H4:SWING", cols["zone_tfs"])

    def test_executor_records_and_never_raises(self):
        x = EX.Executor.__new__(EX.Executor)
        rows = {}
        x.state = types.SimpleNamespace(update_order=lambda t, **f: rows.update(f))
        x.zones = types.SimpleNamespace(on=True, for_order=lambda o: {"score": 3, "against": 0, "for": ["D1:SWING", "H1:FVG", "M30:OB"], "vs": []})
        x._zone_tag(1, {"setup_id": "LAB-BUY-1", "direction": "BUY"})
        self.assertEqual(rows["zone_score"], 3)

        def boom(o):
            raise RuntimeError("no candles")
        x.zones = types.SimpleNamespace(on=True, for_order=boom)
        x._zone_tag(2, {"setup_id": "LAB-BUY-2", "direction": "BUY"})   # logged, not raised


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
        self.assertEqual((c["trade_protect"]["mode"], c["zone_score"]["mode"]), ("off", "off"))
        c = self._load(trade_protect={"mode": "all"}, zone_score={"mode": "record"})
        self.assertEqual((c["trade_protect"]["mode"], c["trade_protect"]["lock_at_r"], c["zone_score"]["mode"]),
                         ("all", 1.5, "record"))
        for kw in ({"trade_protect": {"mode": "always"}}, {"trade_protect": {"lock_r": 2.0}},
                   {"trade_protect": {"near_tp_frac": 1.2}}, {"zone_score": {"mode": "gate"}},
                   {"trade_protect": {"oops": 1}}, {"trade_protect": {"near_tp_exit": "yes"}}):
            with self.assertRaises(ValueError, msg=str(kw)):
                self._load(**kw)


class ExcursionReportTest(unittest.TestCase):
    def test_report_replays_a_closed_trade(self):
        from autotrader import excursions as XC
        fill = int(datetime(2026, 11, 10, 14, 42, 45, tzinfo=UTC).timestamp())
        start = fill - fill % 60 + 60
        m1 = path_m1(ramp(4034.0, 4053.4, 30) + [4053.0, 4052.0, 4051.0, 4050.0, 4049.0] + ramp(4049.0, 4024.0, 40),
                     t0=start)
        deals = [types.SimpleNamespace(entry=0, price=4034.01, time=fill, profit=0.0, commission=0.0, swap=0.0),
                 types.SimpleNamespace(entry=1, price=4024.5, time=fill + 6000, profit=-9.67, commission=0.0, swap=0.0)]

        class M:
            symbol = "XAUUSD"

            def const(self, n):
                return {"DEAL_ENTRY_IN": 0}.get(n, 1)

            def server_to_utc(self, t):
                return t

            def trade_call(self, name, **kw):
                return deals if name == "history_deals_get" else []

            def rates_range(self, tf, a, b):
                return m1 if tf == "TIMEFRAME_M1" else []

        rec = {"ticket": 1, "position_ticket": 60571851, "created_utc": "2026-11-10T12:01:40Z", "status": "CLOSED",
               "setup_id": "LAB-BUY-4031", "direction": "BUY", "entry": 4034.06, "sl": 4024.5, "tp1": 4054.39,
               "tp2": None, "risk_money": 9.56, "lab": 1}
        st = types.SimpleNamespace(all_orders=lambda: [rec])
        rows = XC.analyse(M(), st, days=5, spread=0.3, cfg=CFG, now=datetime(2026, 11, 10, 20, 0, tzinfo=UTC))
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertAlmostEqual(r["actual_r"], -1.01, places=2)
        self.assertEqual(r["unmanaged_exit"], "stop")
        self.assertGreater(r["protected_r"], 1.0)
        text = XC.run(M(), st, None, 5, 0.3, CFG, now=datetime(2026, 11, 10, 20, 0, tzinfo=UTC))
        self.assertIn("losers that had been +1R or more first", text)


if __name__ == "__main__":
    unittest.main()
