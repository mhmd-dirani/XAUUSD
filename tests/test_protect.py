"""Managing open trades: protection (breakeven / lock / near-TP exit), the zone score, opposite live trades and
fills that happened between two checks.
Offline: no MT5, no network, no Claude, no Telegram."""

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent / "src", HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from autotrader import alerts, conflict as CF, executor as EX, protect as PR, settings, zones as ZN  # noqa: E402

UTC = timezone.utc


# ----------------------------------------------------------------------------------------------------------------------
# 1.15: trade protection (breakeven / lock / near-TP exit) and the all-timeframe zone score (recorded only).
# 10 Nov LAB-BUY-4031: filled 4034.01, stop 4024.50, TP1 4054.39, ran to about 4053.4 and closed -1.01 R.
# ----------------------------------------------------------------------------------------------------------------------
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


# ----------------------------------------------------------------------------------------------------------------------
# 1.20.3 (13 Nov): less noise - no tiny breakeven re-moves.
# ----------------------------------------------------------------------------------------------------------------------
class ProtectStepTest(unittest.TestCase):
    def setUp(self):
        self._trade = alerts.trade
        alerts.trade = lambda *a, **k: None

    def tearDown(self):
        alerts.trade = self._trade

    def test_breakeven_is_not_re_moved_by_cents(self):
        pr, app, pos, rec = _app(4045.0)
        pr.tick(datetime(2026, 11, 10, 15, 0, tzinfo=UTC))
        self.assertEqual(app.executor.moves, [4034.31])
        app.market.spread = 0.36                                      # breakeven is now entry + 0.36: 6 cents better
        pr.tick(datetime(2026, 11, 10, 15, 1, tzinfo=UTC))
        self.assertEqual(app.executor.moves, [4034.31])              # 13 Nov: this was one more move + message
        app.market.bid = 4049.0                                      # the lock stage is a real step: still moved
        pr.tick(datetime(2026, 11, 10, 15, 2, tzinfo=UTC))
        self.assertEqual(len(app.executor.moves), 2)


# ----------------------------------------------------------------------------------------------------------------------
# 1.16: opposite live trades (the newer trade runs, the older one is managed) and live limits vs a newer analysis.
# 10 Nov: live BUY 4021.82 (stop 4013.50) open when an opposite SELL 4025.26 opened.
# ----------------------------------------------------------------------------------------------------------------------
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


class _ConflictMarket:
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


class _ConflictState:
    def __init__(self, rows):
        self.rows = rows

    def active_orders(self, include_lab=True):
        return [r for r in self.rows if r["status"] in ("PENDING", "OPEN", "TP1_DONE") and (include_lab or not r.get("lab"))]

    def update_order(self, ticket, **f):
        for r in self.rows:
            if r["ticket"] == ticket:
                r.update(f)


class _ConflictExec:
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
    m = _ConflictMarket(bid, m5, m1)
    m.positions = [types.SimpleNamespace(ticket=1, price_open=4021.82, sl=sl_old, tp=4061.26, type=0, volume=0.01),
                   types.SimpleNamespace(ticket=2, price_open=4025.26, sl=4033.17, tp=3990.0, type=1, volume=0.01)]
    a = types.SimpleNamespace(market=m, state=_ConflictState(rws if rws is not None else rows()), executor=_ConflictExec(),
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


class ConflictSettingsTest(unittest.TestCase):
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


# ----------------------------------------------------------------------------------------------------------------------
# 1.14.1: a pending order that filled AND closed between two checks is journalled from the broker's deals, not as
# CANCELLED (10 Nov: LAB-BUY-4124 -15.10 while the PC slept, LAB-BUY-4057 -4.85 in two seconds on the CPI spike).
# ----------------------------------------------------------------------------------------------------------------------
T_IN = int(datetime(2026, 11, 10, 13, 30, 4, tzinfo=UTC).timestamp())
T_OUT = T_IN + 2


def deal(entry, t, profit=0.0, pos=60568676):
    return types.SimpleNamespace(entry=entry, time=t, profit=profit, commission=0.0, swap=0.0, position_id=pos)


class _MissedFillMarket:
    symbol = "XAUUSD"
    CONST = {"DEAL_ENTRY_IN": 0, "DEAL_ENTRY_OUT": 1}

    def __init__(self, deals):
        self.deals = deals

    def const(self, name):
        return self.CONST.get(name, 0)

    def server_to_utc(self, t):
        return t

    def quote(self):
        return {"bid": 4047.6, "ask": 4047.9, "spread": 0.3, "age": 1}

    def symbol_info(self):
        return types.SimpleNamespace(point=0.01, digits=2, volume_min=0.01, volume_step=0.01, filling_mode=7)

    def trade_call(self, name, **kw):
        if name == "history_deals_get":
            return self.deals.get(kw.get("position"), [])
        return []


class _MissedFillState:
    def __init__(self, rows):
        self.rows = rows

    def active_orders(self, include_lab=True):
        return [r for r in self.rows if r["status"] in ("PENDING", "OPEN", "TP1_DONE")]

    def all_orders(self):
        return self.rows

    def update_order(self, ticket, **f):
        for r in self.rows:
            if r["ticket"] == ticket:
                r.update(f)

    def closed_without_result(self):
        return [r for r in self.rows if r["status"] == "CLOSED" and r.get("result") is None and r.get("position_ticket")]

    def closed_unreviewed(self):
        return []


def rec(ticket=60568676, status="PENDING", note=None, created="2026-11-10T11:07:45Z"):
    return {"ticket": ticket, "setup_id": "LAB-BUY-4057", "direction": "BUY", "status": status, "lab": 1,
            "position_ticket": None, "expiry_utc": "2026-11-10T23:07:45Z", "cancel_above": None, "cancel_below": None,
            "tp1": 4084.11, "tp2": None, "risk_money": 10.5, "note": note, "created_utc": created, "exit_utc": None}


class MissedFillTest(unittest.TestCase):
    def setUp(self):
        self.msgs = []
        self._t, self._e = alerts.trade, alerts.error
        alerts.trade = lambda m, short=None: self.msgs.append(m)
        alerts.error = lambda m: self.msgs.append("ERR " + m)

    def tearDown(self):
        alerts.trade, alerts.error = self._t, self._e

    def x(self, rows, deals):
        x = EX.Executor({"magic": 40001, "magic_lab": 40003, "lab_mode": True}, market=_MissedFillMarket(deals), state=_MissedFillState(rows))
        x._repaired = True                                          # the start-up repair is tested separately
        return x

    def test_the_cpi_case_is_closed_with_its_loss_not_cancelled(self):
        rows = [rec()]
        x = self.x(rows, {60568676: [deal(0, T_IN), deal(1, T_OUT, -4.85)]})
        self.assertTrue(x.filled_and_closed(rows[0]))
        self.assertEqual((rows[0]["status"], rows[0]["position_ticket"]), ("CLOSED", 60568676))
        self.assertEqual((rows[0]["fill_utc"], rows[0]["closed_utc"]), ("2026-11-10T13:30:04Z", "2026-11-10T13:30:06Z"))
        x.fill_results()
        self.assertEqual(rows[0]["result"], -4.85)

    def test_no_deals_means_really_cancelled(self):
        rows = [rec()]
        x = self.x(rows, {})
        self.assertFalse(x.filled_and_closed(rows[0]))
        self.assertEqual(rows[0]["status"], "PENDING")

    def test_filled_without_a_close_deal_becomes_open(self):
        rows = [rec()]
        x = self.x(rows, {60568676: [deal(0, T_IN)]})
        self.assertTrue(x.filled_and_closed(rows[0]))
        self.assertEqual(rows[0]["status"], "OPEN")

    def test_startup_repair_fixes_old_wrong_cancellations_once(self):
        rows = [rec(status="CANCELLED", note="no longer at the broker", created=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")),
                dict(rec(ticket=2, status="CANCELLED", note="TP1 touched before the fill"), setup_id="LAB-SELL-1")]
        x = EX.Executor({"magic": 40001, "magic_lab": 40003, "lab_mode": True},
                        market=_MissedFillMarket({60568676: [deal(0, T_IN), deal(1, T_OUT, -4.85)], 2: [deal(0, T_IN, pos=2)]}),
                        state=_MissedFillState(rows))
        self.assertEqual(x.repair_missed_fills(), [60568676])            # only 'no longer at the broker' rows
        self.assertEqual(rows[0]["status"], "CLOSED")
        self.assertEqual(rows[1]["status"], "CANCELLED")
        self.assertEqual(x.repair_missed_fills(), [])                     # once per start
        self.assertTrue(any("Journal repaired" in m for m in self.msgs))


if __name__ == "__main__":
    unittest.main()
