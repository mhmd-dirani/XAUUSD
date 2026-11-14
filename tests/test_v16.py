#!/usr/bin/env python3
"""v1.6 tests: market state, session levels, playbooks + expansion filter (paper trading removed in 1.7),
partial fills, journal fields, Astra trade_class, and the historical replay (no MT5, no network).
Run:  python src/auto_trader.py --self-test   (or python -m unittest discover -s tests)"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "src", ROOT / "tests", ROOT / "tools"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import gold_monitor as GM  # noqa: E402
import astra_analysis as AA  # noqa: E402
from autotrader import bridge as BR, playbooks as PB, regime as RG, replay as RP  # noqa: E402
from autotrader import sessions as SS, settings  # noqa: E402
from autotrader.app import App  # noqa: E402
from autotrader.executor import Executor  # noqa: E402
from autotrader.state import State  # noqa: E402
from test_auto_trader import Base, M69, bar, evidence  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 10, 14, 0, 0, tzinfo=UTC)          # a Wednesday


def bars15(closes, start=T0, wick=1.0):
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append(bar(start + timedelta(minutes=15 * i), o, max(o, c) + wick, min(o, c) - wick, c))
        prev = c
    return out


def flat_then_spike(n_flat=100, spike=(10, 10, 10, 10, 10, 10)):
    closes = [4100 + (2 if i % 2 else -2) for i in range(n_flat)]
    p = closes[-1]
    for s in spike:
        p += s
        closes.append(p)
    return bars15(closes)


# ----------------------------------------------------------------------------------- regime
class RegimeTests(unittest.TestCase):
    def test_phase_table_keeps_layers_separate(self):
        B, S = "BULLISH", "BEARISH"
        self.assertEqual(RG.phase_of(S, S, S, "NORMAL"), ("CONTINUATION", S))
        self.assertEqual(RG.phase_of(S, B, S, "NORMAL"), ("PULLBACK", S))
        self.assertEqual(RG.phase_of(S, S, B, "NORMAL"), ("TRANSITION", B))     # 4H bear, 15M bull CHoCH
        self.assertEqual(RG.phase_of(S, B, B, "NORMAL"), ("REVERSAL", B))       # 1H followed, 4H not yet
        self.assertEqual(RG.phase_of("MIXED", B, B, "NORMAL"), ("TRANSITION", B))
        self.assertEqual(RG.phase_of("MIXED", S, B, "NORMAL"), ("RANGE", None))

    def test_macro_needs_daily_and_4h(self):
        self.assertEqual(RG.macro_of("BULLISH", "BULLISH"), "BULLISH")
        self.assertEqual(RG.macro_of("BULLISH", "BEARISH"), "MIXED")
        self.assertEqual(RG.macro_of(None, "BEARISH"), "BEARISH")
        self.assertEqual(RG.macro_of(None, None), "NEUTRAL")

    def test_spike_is_an_active_bullish_expansion_with_volatility_and_momentum(self):
        b = flat_then_spike()
        vol = RG.volatility_of(b, RG.DEFAULTS)
        self.assertEqual(vol["volatility"], "EXPANDING")
        mom = RG.momentum_of(b, vol["baseline"])
        self.assertEqual(mom["momentum_direction"], "BULLISH")
        e = RG.expansion_of(b, vol["baseline"], RG.DEFAULTS)
        self.assertTrue(e["active"])
        self.assertEqual(e["direction"], "BULLISH")
        self.assertGreaterEqual(e["size_baseline"], 6)

    def test_half_retraced_or_old_move_is_not_active(self):
        b = flat_then_spike()
        p = b[-1]["close"]
        back = bars15([p - 8 * k for k in range(1, 6)], start=T0 + timedelta(minutes=15 * len(b)))
        vol = RG.volatility_of(b + back, RG.DEFAULTS)
        self.assertFalse(RG.expansion_of(b + back, vol["baseline"], RG.DEFAULTS)["active"])

    def test_calm_market(self):
        b = bars15([4100 + (1 if i % 2 else -1) for i in range(120)])
        vol = RG.volatility_of(b, RG.DEFAULTS)
        self.assertEqual(vol["volatility"], "NORMAL")
        self.assertFalse(RG.expansion_of(b, vol["baseline"], RG.DEFAULTS)["active"])
        self.assertEqual(RG.momentum_of(b, vol["baseline"])["momentum"], "FLAT")

    def test_market_state_runs_and_unavailable_frame_fails_closed(self):
        b = flat_then_spike()
        st = RG.market_state({"15min": b, "1h": [], "4h": [], "1day": []}, datetime(2026, 10, 20, tzinfo=UTC))
        for k in ("macro_trend", "intermediate_trend", "local_trend", "phase", "volatility", "momentum", "expansion"):
            self.assertIn(k, st)
        self.assertIsNone(st["trend_4h"])                                   # no 4H candles: unknown
        self.assertTrue(PB.mtf_opposed("BUY", st) and PB.mtf_opposed("SELL", st))   # unknown = opposed (as V6)
        json.dumps(RG.public(st))                                           # journal-safe


def expansion_state(**over):
    """Bullish expansion 4073 -> 4136 (14 Oct), to fade with a SELL."""
    t = lambda m: GM.iso(T0 + timedelta(minutes=15 * m))
    st = {"trend_4h": "BEARISH", "intermediate_trend": "BEARISH", "macro_trend": "BEARISH", "local_trend": "BULLISH",
          "phase": "TRANSITION", "phase_direction": "BULLISH", "baseline": 4.0,
          "expansion": {"active": True, "direction": "BULLISH", "size_baseline": 15.8, "origin": 4073.0,
                        "extreme": 4136.0, "origin_at": t(0), "extreme_at": t(8)},
          "_frames": {"15min": {"events": [], "swings": [
              {"side": "HIGH", "time": t(-4), "price": 4120.0},          # broken by the expansion
              {"side": "LOW", "time": t(5), "price": 4110.0}]}}}         # the leg's last higher low
    st.update(over)
    return st


class ReversalMethodTests(unittest.TestCase):
    def run_bars(self, closes_ohlc, state=None):
        b = [bar(T0 + timedelta(minutes=15 * (8 + i)), *x) for i, x in enumerate(closes_ohlc)]
        return RG.reversal_methods("SELL", state or expansion_state(), b)

    def test_nothing_yet_while_price_holds_high(self):
        self.assertEqual(self.run_bars([(4134, 4136, 4130, 4133), (4133, 4135, 4129, 4132)]), [])

    def test_choch_after_the_extreme(self):
        st = expansion_state()
        st["_frames"]["15min"]["events"] = [{"kind": "CHOCH", "timeframe": "15min", "direction": "SELL",
                                             "at": GM.iso(T0 + timedelta(minutes=15 * 10))}]
        self.assertIn("CHOCH", self.run_bars([(4134, 4136, 4130, 4133)], st))

    def test_failed_breakout_back_below_the_broken_high(self):
        self.assertIn("FAILED_BREAKOUT", self.run_bars([(4134, 4136, 4130, 4131), (4131, 4132, 4117, 4118)]))

    def test_break_and_retest_of_the_last_higher_low(self):
        got = self.run_bars([(4134, 4136, 4130, 4131), (4115, 4116, 4107, 4108), (4108, 4110.5, 4106, 4107)])
        self.assertIn("BREAK_RETEST", got)

    def test_sweep_and_reclaim_of_a_prior_high(self):
        st = expansion_state()
        st["_frames"]["15min"]["swings"][0]["price"] = 4134.0
        self.assertIn("SWEEP_RECLAIM", self.run_bars([(4135, 4136, 4128, 4129)], st))

    def test_same_direction_is_never_blocked(self):
        self.assertEqual(RG.reversal_methods("BUY", expansion_state(), []), [])


# ----------------------------------------------------------------------------------- sessions
class SessionLevelTests(unittest.TestCase):
    def test_dst_windows_in_each_markets_own_zone(self):
        at = lambda d: datetime.fromisoformat(d + "T12:00:00+00:00")
        last = lambda n, d: [(s.strftime("%H:%M"), e.strftime("%H:%M")) for s, e in
                             SS.windows(n, SS.DEFAULT_SESSIONS[n], at(d), 0)][0]
        self.assertEqual(last("LONDON", "2026-07-01"), ("07:00", "15:30"))        # BST
        self.assertEqual(last("LONDON", "2026-10-28"), ("08:00", "16:30"))        # UK back on GMT 25 Oct
        self.assertEqual(last("NEW_YORK", "2026-10-28"), ("12:00", "21:00"))      # US still on EDT until 1 Nov
        self.assertEqual(last("NEW_YORK", "2026-11-04"), ("13:00", "22:00"))
        self.assertEqual(last("ASIA", "2026-07-01"), ("00:00", "06:00"))          # Tokyo: no DST

    def test_session_labels_and_trading_day(self):
        u = lambda h, m=0: datetime(2026, 7, 1, h, m, tzinfo=UTC)
        self.assertEqual(SS.session_of(u(3)), "ASIA")
        self.assertEqual(SS.session_of(u(9)), "LONDON")
        self.assertEqual(SS.session_of(u(13)), "LONDON_NY")
        self.assertEqual(SS.session_of(u(18)), "NEW_YORK")
        self.assertEqual(SS.session_of(u(22, 30)), "OFF")
        self.assertEqual(SS.trading_day(datetime(2026, 10, 14, 21, 30, tzinfo=UTC)).isoformat(), "2026-10-15")
        self.assertEqual(SS.trading_day(datetime(2026, 10, 14, 20, 30, tzinfo=UTC)).isoformat(), "2026-10-14")

    def make(self):
        """Tue 13 + Wed 14 Oct, 15M bars around 4100; Wed Asia high 4120 at 03:00 UTC."""
        start = datetime(2026, 10, 12, 21, 0, tzinfo=UTC)
        out = []
        for i in range(4 * 24 * 2):
            t = start + timedelta(minutes=15 * i)
            h = 4120.0 if t == datetime(2026, 10, 14, 3, 0, tzinfo=UTC) else 4102.0
            l = 4080.0 if t == datetime(2026, 10, 13, 15, 0, tzinfo=UTC) else 4098.0
            out.append(bar(t, 4100.0, h, l, 4100.0))
        return out

    def test_levels(self):
        b = self.make()
        now = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
        lv = {x["name"]: x["price"] for x in SS.levels(b, [], now)}
        self.assertEqual(lv["ASIA_HIGH"], 4120.0)                               # today's Asia, completed 06:00
        self.assertEqual(lv["PDL"], 4080.0)                                     # Tuesday's low
        self.assertEqual(lv["LONDON_LOW"], 4080.0)                              # Tuesday's London (15:00 UTC low)
        self.assertIn("LONDON_OR_HIGH", lv)                                     # 07:00-08:00 UTC complete
        self.assertNotIn("NEW_YORK_OR_HIGH", lv)                                # NY opens 12:00 UTC: not yet

    def test_sweep_breakout_and_failed_breakout(self):
        lv = [{"name": "ASIA_HIGH", "side": "HIGH", "price": 4120.0, "formed_at": "2026-10-14T06:00:00+00:00",
               "source": "SESSION"}]
        t = datetime(2026, 10, 14, 8, 0, tzinfo=UTC)
        sweep = [bar(t, 4115, 4122, 4114, 4118)]
        e = SS.events(sweep, lv, t + timedelta(minutes=15))
        self.assertEqual((e[0]["event"], e[0]["direction"]), ("SWEEP_RECLAIM", "SELL"))
        brk = [bar(t, 4115, 4124, 4114, 4123), bar(t + timedelta(minutes=15), 4123, 4124, 4116, 4117)]
        e = SS.events(brk, lv, t + timedelta(minutes=30), last_n=2)
        self.assertEqual([x["event"] for x in e], ["BREAKOUT", "FAILED_BREAKOUT"])
        self.assertEqual(e[1]["direction"], "SELL")

    def test_unclosed_candle_is_never_used(self):
        lv = [{"name": "ASIA_HIGH", "side": "HIGH", "price": 4120.0, "formed_at": "2026-10-14T06:00:00+00:00",
               "source": "SESSION"}]
        t = datetime(2026, 10, 14, 8, 0, tzinfo=UTC)
        self.assertEqual(SS.events([bar(t, 4115, 4122, 4114, 4118)], lv, t + timedelta(minutes=10)), [])


# ----------------------------------------------------------------------------------- playbooks
class PlaybookTests(unittest.TestCase):
    def st(self, **o):
        s = {"trend_4h": "BEARISH", "intermediate_trend": "BEARISH", "phase": "CONTINUATION",
             "phase_direction": "BEARISH", "expansion": {"active": False}, "_frames": {"15min": {"events": []}}}
        s.update(o)
        return s

    def test_classes_in_order(self):
        self.assertEqual(PB.classify("SELL", self.st(), strong_zone=False, confirmation="REJECTION")[0], "TREND")
        self.assertEqual(PB.classify("BUY", self.st(), strong_zone=True, confirmation="SWEEP")[0], "COUNTER_TREND")
        rev = self.st(phase="TRANSITION", phase_direction="BULLISH",
                      _frames={"15min": {"events": [{"kind": "CHOCH", "direction": "BUY", "at": "x"}]}})
        self.assertEqual(PB.classify("BUY", rev, strong_zone=False, confirmation="SWEEP")[0], "REVERSAL_TRANSITION")
        self.assertEqual(PB.classify("BUY", self.st(), strong_zone=False, confirmation="SESSION_SWEEP")[0],
                         "SESSION_SWEEP")
        self.assertEqual(PB.classify("BUY", self.st(), strong_zone=False, confirmation="SWEEP")[0], "NONE")

    def test_reversal_needs_its_own_latest_choch(self):
        rev = self.st(phase="TRANSITION", phase_direction="BULLISH", _frames={"15min": {"events": [
            {"kind": "CHOCH", "direction": "BUY"}, {"kind": "CHOCH", "direction": "SELL"}]}})
        self.assertEqual(PB.classify("BUY", rev, strong_zone=False, confirmation="SWEEP")[0], "NONE")

    def test_expansion_filter_modes(self):
        st = expansion_state()
        modes = dict(PB.DEFAULT_MODES, COUNTER_TREND="live")
        args = dict(strong_zone=True, confirmation="REJECTION", modes=modes, levels=[])
        live = PB.decide("SELL", st, [], expansion_mode="live", **args)
        self.assertEqual((live["mode"], live["would_block"]), ("off", True))          # no fading the spike
        self.assertEqual(PB.decide("SELL", st, [], expansion_mode="shadow", **args)["mode"], "live")
        st["_frames"]["15min"]["events"] = [{"kind": "CHOCH", "timeframe": "15min", "direction": "SELL",
                                             "at": GM.iso(T0 + timedelta(minutes=15 * 10))}]
        ok = PB.decide("SELL", st, [], expansion_mode="live", **args)
        self.assertEqual((ok["mode"], ok["expansion"]["methods"]), ("live", ["CHOCH"]))   # reversal validated

    def test_with_the_expansion_is_never_blocked(self):
        d = PB.decide("BUY", expansion_state(trend_4h="BULLISH", intermediate_trend="BULLISH"), [],
                      strong_zone=False, confirmation="REJECTION", modes=PB.DEFAULT_MODES, expansion_mode="live")
        self.assertEqual((d["trade_class"], d["mode"]), ("TREND", "live"))


# ----------------------------------------------------------------------------------- settings / Astra
class SettingsAndAstraTests(unittest.TestCase):
    def test_17_all_playbooks_live_no_paper_and_filter_in_shadow(self):
        c = settings.load(None)
        modes = dict(c["playbook_modes"])
        self.assertEqual(modes.pop("BREAKOUT_CONTINUATION"), "off")             # 1.8: off until replayed
        self.assertEqual(set(modes.values()), {"live"})
        self.assertEqual((c["expansion_filter"], c["python_session_setups"]), ("shadow", "off"))
        d = Path(tempfile.mkdtemp())
        for bad in ('{"playbook_modes": {"SESSION_SWEEP": "paper"}}', '{"python_session_setups": "paper"}',
                    '{"expansion_filter": "paper"}'):
            (d / "x.json").write_text(bad)
            with self.assertRaisesRegex(ValueError, "paper", msg=bad):
                settings.load(d / "x.json")

    def test_bad_v16_settings_fail_at_start(self):
        d = Path(tempfile.mkdtemp())
        for bad in ('{"playbook_modes": {"TREND": "maybe"}}', '{"expansion_filter": "on"}',
                    '{"python_session_setups": "live"}', '{"regime": {"nope": 1}}',
                    '{"sessions": {"ASIA": {"tz": "Mars/Base", "start": "09:00", "end": "15:00"}}}'):
            (d / "x.json").write_text(bad)
            with self.assertRaises(ValueError, msg=bad):
                settings.load(d / "x.json")

    def test_astra_is_asked_for_trade_class_but_old_ideas_still_validate(self):
        idea = AA.ANALYSIS_SCHEMA["properties"]["ideas"]["items"]
        for k in AA.CLASS_FIELDS:
            self.assertIn(k, idea["required"])                                 # asked of the model
        self.assertNotIn("trade_class", AA.VALIDATION_SCHEMA["properties"]["ideas"]["items"]["required"])
        self.assertIn("REVERSAL-TRANSITION idea", AA.CONTRACT)

    def test_choch_confirmation_uses_the_engines_at_field(self):
        idea = {"direction": "BUY", "entry_low": 4074.5, "entry_high": 4079.75, "invalidation": 4069.5}
        t0 = datetime(2026, 10, 14, 15, 0, tzinfo=UTC)
        inside = bar(t0 + timedelta(minutes=15), 4078.0, 4079.0, 4076.0, 4077.0)
        ev = {"index": {"c": {"kind": "CHOCH", "timeframe": "15min", "status": "CONFIRMED", "direction": "BUY",
                              "at": GM.iso(t0 + timedelta(minutes=30))}}}             # no confirmed_at (real engine)
        self.assertEqual(BR.confirmation(idea, [inside], t0, ev)[2], "CHOCH")


# ----------------------------------------------------------------------------------- replay
def m1(start, rows, spread=0.3):
    return [{"t": start + timedelta(minutes=i), "o": o, "h": h, "l": l, "c": c, "s": spread}
            for i, (o, h, l, c) in enumerate(rows)]


def trade(**o):
    t = dict(cid="x", variant="v", direction="BUY", order_type="MARKET", created=T0, entry_level=4100.0,
             stop=4095.0, tp1=4110.0, tp2=None, expiry=T0 + timedelta(hours=4), trade_class="TREND",
             confirmation="REJECTION", zone="z", session="LONDON", state={}, expansion_against=False,
             would_block=False)
    t.update(o)
    return RP.Trade(**t)


class ReplayTests(unittest.TestCase):
    cfg = dict(RP.DEFAULTS)

    def test_candles_follow_the_broker_clock(self):
        summer = m1(datetime(2026, 7, 1, 20, 0, tzinfo=UTC), [(1, 1, 1, 1)] * 600)
        h4 = RP.aggregate(summer, "4h", "ny+7")
        self.assertIn("2026-07-01T21:00:00+00:00", [b["time"] for b in h4])    # server 00:00 = 21:00 UTC (UTC+3)
        d1 = RP.aggregate(summer, "1day", "ny+7")
        self.assertEqual(d1[1]["time"], "2026-07-01T21:00:00+00:00")
        winter = m1(datetime(2026, 11, 4, 20, 0, tzinfo=UTC), [(1, 1, 1, 1)] * 600)
        self.assertIn("2026-11-04T22:00:00+00:00", [b["time"] for b in RP.aggregate(winter, "4h", "ny+7")])
        h1 = RP.aggregate(summer, "1h", "ny+7")
        self.assertEqual(h1[0]["closes"], "2026-07-01T21:00:00+00:00")

    def test_market_buy_fills_next_bar_at_ask_plus_slippage(self):
        rows = [(4100, 4101, 4099, 4100)] * 3 + [(4105, 4111, 4104, 4110)]
        sim = RP.Simulator(m1(T0, rows), self.cfg)
        t = sim.run(trade())
        self.assertEqual(t.fill_price, 4100.4)                                  # 4100 + 0.30 spread + 0.10 slip
        self.assertEqual(t.outcome, "TP1")
        self.assertAlmostEqual(t.r, (4110 - 4100.4) / (4100.4 - 4095), places=3)

    def test_same_minute_stop_and_target_is_a_loss_without_ticks(self):
        rows = [(4100, 4100, 4100, 4100), (4100, 4111, 4094, 4100)]
        sim = RP.Simulator(m1(T0, rows), self.cfg)
        t = sim.run(trade())
        self.assertEqual((t.outcome, sim.assumed), ("stop", 1))
        self.assertLess(t.r, -1.0)                                              # stop + slippage

    def test_ticks_settle_the_same_minute(self):
        rows = [(4100, 4100, 4100, 4100), (4100, 4111, 4094, 4100)]
        ticks = lambda a, b: [(a, 4105.0, 4105.3), (a, 4110.5, 4110.8), (a, 4094.0, 4094.3)]
        sim = RP.Simulator(m1(T0, rows), self.cfg, ticks=ticks)
        t = sim.run(trade())
        self.assertEqual((t.outcome, sim.resolved), ("TP1", 1))

    def test_sell_stop_triggers_on_ask(self):
        rows = [(4100, 4100, 4100, 4100), (4101, 4104.8, 4100, 4102)]          # bid high 4104.8 + 0.3 = ask 4105.1
        t = RP.Simulator(m1(T0, rows), self.cfg).run(trade(direction="SELL", stop=4105.0, tp1=4080.0))
        self.assertEqual(t.outcome, "stop")

    def test_limit_cancelled_when_tp1_comes_first(self):
        rows = [(4103, 4104, 4102, 4103), (4104, 4111, 4103, 4110)]
        t = RP.Simulator(m1(T0, rows), self.cfg).run(trade(order_type="BUY_LIMIT", entry_level=4100.0))
        self.assertEqual(t.status, "CANCELLED")
        self.assertIsNone(t.fill_time)

    def test_tp1_partial_then_breakeven(self):
        rows = [(4100, 4100, 4100, 4100), (4101, 4110.5, 4100.5, 4109), (4109, 4109, 4100.0, 4100.2),
                (4100.2, 4100.3, 4099.9, 4100)]
        t = RP.Simulator(m1(T0, rows), dict(self.cfg, slippage=0.0)).run(trade(tp2=4120.0))
        self.assertEqual(t.outcome, "breakeven after TP1")
        self.assertAlmostEqual(t.r, 0.5 * (4110 - 4100.3) / (4100.3 - 4095), places=2)

    def test_gap_through_the_stop_before_the_fill_is_no_trade(self):
        rows = [(4103, 4104, 4102, 4103), (4090, 4091, 4089, 4090)]           # opens below the 4095 stop
        t = RP.Simulator(m1(T0, rows), self.cfg).run(trade(order_type="BUY_LIMIT", entry_level=4100.0))
        self.assertEqual((t.status, t.fill_time), ("CANCELLED", None))
        t = RP.Simulator(m1(T0, rows[1:]), self.cfg).run(trade())             # market order into the gap
        self.assertEqual(t.status, "CANCELLED")

    def test_gap_through_the_stop_exits_at_the_open(self):
        rows = [(4100, 4100, 4100, 4100), (4090, 4091, 4089, 4090)]
        t = RP.Simulator(m1(T0, rows), dict(self.cfg, slippage=0.0)).run(trade())
        self.assertEqual(t.outcome, "stop (gap)")
        self.assertAlmostEqual(t.r, (4090 - 4100.3) / (4100.3 - 4095), places=3)   # about -1.9R, not -1R

    def test_limit_fill_bar_never_reaches_the_target(self):
        rows = [(4103, 4104, 4102, 4103), (4101, 4109, 4099.5, 4108)]          # fills and touches 4109 < TP1 4110
        rows.append((4108, 4111, 4107, 4110))
        t = RP.Simulator(m1(T0, rows), self.cfg).run(trade(order_type="BUY_LIMIT", entry_level=4100.0))
        self.assertEqual((t.outcome, t.fill_time), ("TP1", T0 + timedelta(minutes=1)))
        t2 = RP.Simulator(m1(T0, [(4103, 4104, 4102, 4103), (4101, 4111, 4099.5, 4110)]), self.cfg).run(
            trade(order_type="BUY_LIMIT", entry_level=4100.0))
        self.assertEqual(t2.status, "CANCELLED")                                # TP1 and fill in one minute

    def test_buy_limit_cancel_uses_ask_like_live(self):
        rows = [(4103, 4104, 4102, 4103), (4105, 4109.8, 4104, 4109)]          # bid 4109.8, ask 4110.1 > TP1
        t = RP.Simulator(m1(T0, rows), self.cfg).run(trade(order_type="BUY_LIMIT", entry_level=4100.0))
        self.assertEqual(t.outcome, "TP1 before fill")

    def test_window_never_contains_an_unclosed_candle(self):
        rows = [(4100, 4101, 4099, 4100)] * 200
        rp = RP.Replay(m1(datetime(2026, 7, 1, 21, 0, tzinfo=UTC), rows), {})
        as_of = datetime(2026, 7, 1, 22, 30, tzinfo=UTC)
        got = rp._window("1h", as_of, 10)
        self.assertTrue(all(datetime.fromisoformat(b["closes"]) <= as_of for b in got))
        self.assertEqual(got[-1]["closes"], "2026-07-01T22:00:00+00:00")       # 22:00-23:00 bar not yet

    def test_no_look_ahead_future_prices_never_change_past_decisions(self):
        """Change every price after a cut (+1 %, wider ranges): every decision up to the cut must be
        identical. Any look-ahead in candles, frames, sessions or targets would show here."""
        import copy
        import replay as tool
        base = tool.synthetic(days=56, seed=11)
        cut = datetime(2026, 7, 21, 13, 7, tzinfo=UTC)                        # mid-candle, inside London
        moved = copy.deepcopy(base)
        for b in moved:
            if b["t"] >= cut:
                b.update(o=b["o"] * 1.01, c=b["c"] * 1.01, h=b["h"] * 1.01 + 3, l=b["l"] * 1.01 - 3)
        cfg = {"spread_stop_fraction": 0.2}
        a, b = RP.Replay(copy.deepcopy(base), cfg).run(), RP.Replay(moved, cfg).run()
        key = lambda c: (c["cid"], c["order_type"], c["entry"], c["stop"], c["tp1"], c["tp2"], c["strong"])
        before_a = [key(c) for c in a.candidates if c["created"] <= cut]
        self.assertGreater(len(before_a), 5)
        self.assertEqual(before_a, [key(c) for c in b.candidates if c["created"] <= cut])
        for v in RP.VARIANTS:
            self.assertEqual([(t.cid, t.trade_class) for t in a.trades[v] if t.created <= cut],
                             [(t.cid, t.trade_class) for t in b.trades[v] if t.created <= cut], v)

    def test_end_to_end_compares_all_variants_on_the_same_candidates(self):
        import replay as tool
        rp = RP.Replay(tool.synthetic(days=50, seed=3), {"spread_stop_fraction": 0.2}).run()
        text = RP.report(rp, "test")
        for v in RP.VARIANTS:
            self.assertIn(v, text)
        for section in ("In-sample vs out-of-sample", "Trade-off: OLD_v1.5 -> NEW_all", "does NOT prove",
                        "Candidate funnel"):
            self.assertIn(section, text)
        for t in rp.trades["OLD_v1.5"]:
            self.assertIn(t.trade_class, ("TREND", "COUNTER_TREND"))               # old rules only
            if t.fill_time:
                self.assertGreaterEqual(t.fill_time, t.created)                     # no fill before the decision
        path = Path(tempfile.mkdtemp()) / "t.csv"
        RP.trades_csv(rp, path)
        self.assertIn("trade_class", path.read_text().splitlines()[0])


class ReplayMT5FetchTests(unittest.TestCase):
    def test_fetch_converts_server_time_and_spread_points(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed")
        import replay as tool
        srv0 = int(datetime(2026, 10, 14, 12, 0, tzinfo=UTC).timestamp()) + 3 * 3600   # 12:00 UTC = 15:00 server
        dt = np.dtype([("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"), ("close", "<f8"),
                       ("tick_volume", "<u8"), ("spread", "<i4"), ("real_volume", "<u8")])
        rates = np.array([(srv0 + 60 * i, 4100, 4101, 4099, 4100.5, 10, 25, 0) for i in range(3)], dtype=dt)
        fake = NS(initialize=lambda **k: True, last_error=lambda: (1, "ok"), symbol_select=lambda *a: True,
                  symbol_info=lambda s: NS(point=0.01), TIMEFRAME_M1=1, COPY_TICKS_ALL=-1,
                  copy_rates_range=lambda *a: rates, symbol_info_tick=lambda s: None)
        with patch.dict(sys.modules, {"MetaTrader5": fake}):
            m1, ticks, point = tool.fetch_mt5({"symbol": "XAUUSD", "_offset_rule": "ny+7"}, 1, False)
        self.assertEqual(m1[0]["t"], datetime(2026, 10, 14, 12, 0, tzinfo=UTC))
        self.assertAlmostEqual(m1[0]["s"], 0.25)                                    # 25 points x 0.01
        self.assertIsNone(ticks)


# ----------------------------------------------------------------------------------- partial fills
class PartialFillTests(Base):
    def setUp(self):
        super().setUp()
        self.m.connect()
        self.ex = Executor(self.c, self.m, self.state)
        o = {"setup_id": "IDEA-BUY-4079", "direction": "BUY", "order_type": "BUY_LIMIT", "entry_price": 4079.0,
             "stop_loss": 4069.5, "take_profit_1": 4097.95, "take_profit_2": 4110.88, "tp1_close_percent": 50,
             "move_sl_to_breakeven_at_tp1": True, "order_expiry_utc": GM.iso(datetime.now(UTC) + timedelta(hours=4)),
             "rr": 2.0}
        self.state.record_order(801, o, "B_TRADE", 0.04, 20.0, "PENDING")
        self.fake.orders = [NS(ticket=801, magic=40001)]
        self.pos = NS(ticket=901, identifier=801, magic=40001, volume=0.02, type=0, price_open=4079.0,
                      tp=4110.88, sl=4069.5)
        self.fake.positions = [self.pos]
        self.fake.add_tick(4085.0, 4085.2)

    def rec(self):
        return self.state.all_orders()[0]

    def test_partial_fill_is_tracked_while_rest_is_pending(self):
        self.ex.manage()
        r = self.rec()
        self.assertEqual((r["status"], r["position_ticket"]), ("PENDING", 901))
        self.assertIn("partially filled", r["note"])

    def test_rest_expires_but_filled_part_stays_managed(self):
        self.state.update_order(801, expiry_utc=GM.iso(datetime.now(UTC) - timedelta(minutes=1)))
        self.ex.manage()
        self.assertEqual(self.fake.sent[-1]["action"], self.fake.TRADE_ACTION_REMOVE)
        r = self.rec()
        self.assertEqual((r["status"], r["position_ticket"]), ("OPEN", 901))   # v1.5: EXPIRED, position lost
        self.assertIn("partial fill 0.02 lots kept", r["note"])

    def test_failed_cancel_keeps_the_order_pending_for_a_retry(self):
        self.state.update_order(801, expiry_utc=GM.iso(datetime.now(UTC) - timedelta(minutes=1)))
        real = self.fake.order_send
        self.fake.order_send = lambda req: NS(retcode=10013, order=0, price=0, comment="invalid request") \
            if req["action"] == self.fake.TRADE_ACTION_REMOVE else real(req)
        with patch("autotrader.alerts.error"):
            self.ex.manage()
        self.assertEqual(self.rec()["status"], "PENDING")                       # rest still at the broker
        self.fake.order_send = real
        self.ex.manage()
        self.assertEqual(self.rec()["status"], "OPEN")

    def test_tp1_closes_half_of_the_filled_volume_not_the_ordered_volume(self):
        self.state.update_order(801, expiry_utc=GM.iso(datetime.now(UTC) - timedelta(minutes=1)))
        self.ex.manage()
        self.fake.orders = []
        self.fake.add_tick(4098.0, 4098.2)
        self.ex.manage()
        close = [s for s in self.fake.sent if s.get("comment") == "TP1"][0]
        self.assertEqual(close["volume"], 0.01)                                 # 50 % of 0.02, not of 0.04
        self.assertEqual(self.rec()["status"], "TP1_DONE")

    def test_restart_resumes_from_the_database(self):
        self.ex.manage()
        fresh = Executor(self.c, self.m, State(self.c["state_path"]))         # new process, same database
        self.fake.orders = []                                                   # rest filled meanwhile
        self.pos.volume = 0.04
        fresh.manage()
        self.assertEqual(self.rec()["status"], "OPEN")
        self.fake.positions = []                                                # closed at the broker
        fresh.manage()
        self.assertEqual(self.rec()["status"], "CLOSED")


class ReportTests(Base):
    def test_report_groups_by_playbook_and_lists_reviews(self):
        from autotrader import report
        self.m.connect()
        o = {"setup_id": "IDEA-SELL-4146", "direction": "SELL", "order_type": "MARKET", "entry_price": 4146.0,
             "stop_loss": 4158.0, "take_profit_1": 4112.0, "take_profit_2": None, "tp1_close_percent": 100,
             "move_sl_to_breakeven_at_tp1": False, "order_expiry_utc": GM.iso(datetime.now(UTC)), "rr": 2.8}
        self.state.record_order(801, o, "B_TRADE", 0.01, 12.0, "CLOSED", position_ticket=801)
        self.state.set_meta(801, {"playbook": "TREND", "confirmation": "REJECTION", "session": "ASIA",
                                  "regime": {"phase": "CONTINUATION", "phase_direction": "BEARISH"},
                                  "expansion_block": True})
        self.fake.deals = [NS(profit=-12.0, commission=0.0, swap=0.0)]
        text = report.run(self.m, self.state, Path(tempfile.mkdtemp()) / "t.csv")
        for s_ in ("by playbook", "REJECTION", "ASIA", "CONTINUATION BEARISH", "would have blocked", "TRADE REVIEWS"):
            self.assertIn(s_, text)


# ----------------------------------------------------------------------------------- app routing
REV_BUY = {"id": "R1", "direction": "BUY", "risk": "HIGH", "confidence": 30, "entry_type": "LIMIT",
           "readiness": "NEEDS_CONFIRMATION", "entry_low": 4074.5, "entry_high": 4079.75, "invalidation": 4069.5,
           "targets": [4097.95, 4110.88], "title": "reversal buy", "trade_class": "REVERSAL_TRANSITION"}


class RoutingAppTests(Base):
    """Bearish 4H/1H. A BUY idea away from any strong zone, after a bullish 15M CHoCH."""
    def setUp(self):
        super().setUp()
        self.v6dir = Path(tempfile.mkdtemp())
        self.c["v6"] = dict(self.c["v6"], state_directory=str(self.v6dir))
        self.app = App(self.c)
        self.app.market = self.m
        self.app.validator.m = self.app.executor.m = self.m
        self.m.connect()
        self.now = datetime.now(UTC)
        self.t = self.now - timedelta(minutes=30)
        self.bars = [bar(self.t, 4076.0, 4080.5, 4073.5, 4079.2)]                   # sweep + reclaim
        self.state_ = {"trend_4h": "BEARISH", "intermediate_trend": "BEARISH", "trend_1day": "BEARISH",
                       "macro_trend": "BEARISH", "local_trend": "BULLISH", "phase": "TRANSITION",
                       "phase_direction": "BULLISH", "volatility": "NORMAL", "momentum": "STEADY",
                       "expansion": {"active": False},
                       "_frames": {"15min": {"events": [{"kind": "CHOCH", "direction": "BUY", "timeframe": "15min",
                                                         "at": GM.iso(self.t)}], "swings": []}}}
        self.snap = {"session": "NEW_YORK", "levels": [], "events": []}
        for x in (patch.object(self.m, "trading_window", return_value=None),
                  patch.object(self.m, "news_block", return_value=None),
                  patch.object(self.app, "market_context", return_value=(evidence(), 5.0)),
                  patch.object(self.app, "market_state", side_effect=lambda: (self.state_, self.snap)),
                  patch.object(self.app, "recent_bars", side_effect=lambda: self.bars),
                  patch.object(self.app, "memory_zones", side_effect=lambda: [dict(M69, tests=1)])):
            x.start()
            self.addCleanup(x.stop)
        self.fake.add_tick(4079.0, 4079.2)

    def run_idea(self, idea):
        a = {"id": "A1", "created_at": GM.iso(self.now - timedelta(minutes=40)), "ideas": [idea]}
        (self.v6dir / "astra_analysis.json").write_text(json.dumps(a))
        self.app.bridge_tick(self.now)

    def test_reversal_transition_is_live_at_tier_c_size_with_journal(self):
        """1.7: no paper. A REVERSAL_TRANSITION idea (HIGH risk, 30 %) is a Tier C demo order."""
        self.run_idea(REV_BUY)
        self.assertEqual(len(self.fake.sent), 1)
        o = self.app.state.all_orders()[0]
        self.assertEqual((o["grade"], o["strategy"], o["playbook"], o["confirmation"], o["session"], o["tier"]),
                         ("B_TRADE", "SWING_REVERSAL_TRANSITION", "REVERSAL_TRANSITION", "SWEEP", "NEW_YORK", "C"))
        self.assertEqual(json.loads(o["regime"])["phase"], "TRANSITION")
        self.assertEqual((o["lots"], o["risk_money"]), (0.01, 9.42))         # 0.15 % = 15 $ budget, 1 min lot
        self.assertIn("class REVERSAL_TRANSITION", o["tier_reason"])

    def test_playbook_off_sends_nothing(self):
        self.c["playbook_modes"]["REVERSAL_TRANSITION"] = "off"
        self.run_idea(REV_BUY)
        self.assertEqual((self.fake.sent, self.app.state.all_orders()), ([], []))

    def test_no_justification_is_still_refused(self):
        self.state_["phase"], self.state_["phase_direction"] = "CONTINUATION", "BEARISH"
        self.run_idea(REV_BUY)
        self.assertEqual((self.fake.sent, self.app.state.all_orders()), ([], []))

    def expansion(self):
        self.state_.update(trend_4h="BULLISH", intermediate_trend="BULLISH", phase="CONTINUATION",
                           phase_direction="BULLISH", local_trend="BULLISH", baseline=4.0,
                           expansion={"active": True, "direction": "BULLISH", "size_baseline": 15.0,
                                      "origin": 4060.0, "extreme": 4100.0, "origin_at": GM.iso(self.t - timedelta(hours=2)),
                                      "extreme_at": GM.iso(self.t - timedelta(minutes=15))})
        self.state_["_frames"]["15min"]["events"] = []

    def test_buying_with_the_expansion_is_trend_and_not_flagged(self):
        self.expansion()
        with patch.object(self.app, "market_context", return_value=(evidence("BULLISH", "BULLISH"), 5.0)):
            self.run_idea(dict(REV_BUY, trade_class="COUNTER_TREND"))            # Astra's label is wrong
        self.assertEqual(len(self.fake.sent), 1)
        o = self.app.state.all_orders()[0]
        self.assertEqual((o["playbook"], o["astra_class"], o["expansion_block"]), ("TREND", "COUNTER_TREND", 0))

    def test_fading_the_expansion_blocked_only_in_live_mode(self):
        self.expansion()
        sell = {"id": "S1", "direction": "SELL", "risk": "HIGH", "confidence": 30, "entry_type": "LIMIT",
                "readiness": "NEEDS_CONFIRMATION", "entry_low": 4094.0, "entry_high": 4099.0, "invalidation": 4108.0,
                "targets": [4070.0], "title": "fade the spike", "trade_class": "TREND"}
        self.bars = [bar(self.t, 4096.0, 4099.5, 4093.0, 4093.5)]                  # rejection candle
        self.fake.add_tick(4093.4, 4093.6)
        ev = evidence("BEARISH", "BEARISH")                                     # veto says SELL is with 4H/1H
        self.state_.update(trend_4h="BEARISH", intermediate_trend="BEARISH")
        with patch.object(self.app, "market_context", return_value=(ev, 5.0)):
            self.c["expansion_filter"] = "shadow"
            self.run_idea(sell)
            self.assertEqual(len(self.fake.sent), 1)
            self.assertEqual(self.app.state.all_orders()[0]["expansion_block"], 1)   # journalled
            self.fake.sent.clear()
            self.app.state.update_order(self.app.state.all_orders()[0]["ticket"], status="CANCELLED")
            self.app.bridge.state.put("bridge:ideas_done", [])
            self.c["expansion_filter"] = "live"
            self.run_idea(dict(sell, id="S2"))
            self.assertEqual(self.fake.sent, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
