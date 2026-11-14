"""Auto-trader core rules: market state, session levels, playbooks, the expansion filter, the historical replay,
tiers, the direction rule, the post-trade review and BREAKOUT_CONTINUATION.
Offline: no MT5, no network, no Claude, no Telegram."""

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

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent / "src", HERE.parent / "tools", HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import astra_analysis as AA  # noqa: E402
import gold_monitor as GM  # noqa: E402
from autotrader import alerts, breakout as BK, bridge as BR, playbooks as PB, regime as RG  # noqa: E402
from autotrader import replay as RP, review as RV, sessions as SS, settings, tiers as TR  # noqa: E402
from autotrader.app import App  # noqa: E402
from autotrader.executor import Executor  # noqa: E402
from autotrader.state import State  # noqa: E402
from autotrader.validator import Rejected, Validator  # noqa: E402
from test_auto_trader import bar, Base, evidence, M69, sell_order  # noqa: E402

UTC = timezone.utc


# ----------------------------------------------------------------------------------------------------------------------
# v1.6 tests: market state, session levels, playbooks + expansion filter (paper trading removed in 1.7),
# partial fills, journal fields, Astra trade_class, and the historical replay (no MT5, no network).
# Run:  python src/auto_trader.py --self-test   (or python -m unittest discover -s tests)
# ----------------------------------------------------------------------------------------------------------------------
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


# ----------------------------------------------------------------------------------------------------------------------
# v1.7 regression tests (no MT5, no network, no Claude):
# tiers (grading + sizing by risk %), the direction rule (no pyramiding; breakeven frees the slot),
# the post-trade review, one level per price (PDH == NEW_YORK_HIGH), retest limits inside the idea's
# zone, Telegram texts without None/0.00, closed_utc from the deal, no paper mode.
# Run:  python src/auto_trader.py --self-test   (or python -m unittest discover -s tests)
# ----------------------------------------------------------------------------------------------------------------------
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


# ----------------------------------------------------------------------------------------------------------------------
# 1.8 BREAKOUT_CONTINUATION: the candidate rules, the tier cap, the replay variant and the live path.
# Offline (no MT5, no Claude).
# ----------------------------------------------------------------------------------------------------------------------
CFG = dict(RP.DEFAULTS, min_rr=2.0, sl_atr_min=0.5, sl_atr_max=5.0, max_spread=0.5, spread_stop_fraction=0.05)
STATE = {"macro_trend": "BEARISH", "intermediate_trend": "BEARISH", "trend_1day": "BEARISH", "trend_4h": "BEARISH",
         "local_trend": "BEARISH", "phase": "CONTINUATION", "expansion": {"active": True, "direction": "BEARISH"},
         "_frames": {"15min": {"events": [], "swings": []}, "1h": {"trend": "BEARISH", "swings": []}}}
BREAKOUT_T0 = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)
# 23 Oct 14:00 candle: first close below the NY opening-range low 4021.58 (o 4023.64 h 4023.64 l 4007.44 c 4008.20)
EVENT = {"event": "BREAKOUT", "direction": "SELL", "level": "NEW_YORK_OR_LOW", "price": 4021.58,
         "bar_time": GM.iso(BREAKOUT_T0), "high": 4023.64, "low": 4007.44, "close": 4008.2}
BAR = bar(BREAKOUT_T0, 4023.64, 4023.64, 4007.44, 4008.2)
LOWS = [3986.6, 3959.39]           # Asia low, then the June/July support
HIGHS = [4034.06, 4048.29]


class CandidateTests(unittest.TestCase):
    def cand(self, **over):
        kw = dict(e=EVENT, bar=BAR, state=STATE, atr15=8.0, highs=HIGHS, lows=LOWS, as_of=BREAKOUT_T0 + timedelta(minutes=15),
                  session="NEW_YORK", sp=0.3, cfg=CFG)
        kw.update(over)
        why = []
        c = BK.candidate(**kw, drop=why.append)
        return c, why

    def test_23_oct_collapse_is_a_market_sell_with_the_stop_at_the_level(self):
        c, why = self.cand()
        self.assertIsNotNone(c, why)
        self.assertEqual((c["direction"], c["order_type"], c["how"]), ("SELL", "MARKET", "BREAKOUT"))
        self.assertEqual(c["entry"], 4008.2)
        self.assertEqual(c["stop"], 4023.98)                                       # level + max(0.3 ATR, 2 spread)
        self.assertEqual((c["tp1"], c["tp2"]), (3959.39, None))                   # first level >= 2R; 3986.6 on the path
        self.assertEqual(c["path_levels"], [3986.6])
        self.assertAlmostEqual(c["rr_now"], (4008.2 - 3959.39) / (4023.98 - 4008.2), places=2)

    def test_limit_at_2r_price_when_the_close_gives_less(self):
        c, why = self.cand(lows=[3990.0, 3980.0])                                  # 3980: 28.2 / 15.78 = 1.79R < 2.02
        self.assertIsNotNone(c, why)
        self.assertEqual(c["order_type"], "SELL_LIMIT")
        self.assertGreater(c["entry"], 4008.2)
        self.assertLess(c["entry"], c["stop"])
        self.assertEqual(c["expiry"], BREAKOUT_T0 + timedelta(minutes=15 + 60))

    def test_refusals(self):
        self.assertIsNone(self.cand(state=dict(STATE, macro_trend="BULLISH"))[0])
        self.assertIsNone(self.cand(state=dict(STATE, intermediate_trend="BULLISH"))[0])
        self.assertIsNone(self.cand(session="ASIA")[0])
        self.assertIsNone(self.cand(e=dict(EVENT, event="SWEEP_RECLAIM"))[0])
        c, why = self.cand(lows=[4006.0])                                          # inside half a risk
        self.assertIsNone(c)
        self.assertIn("no opposing level", why[0])
        c, why = self.cand(bar=bar(BREAKOUT_T0, 4023.0, 4023.0, 3960.0, 3962.0))            # ran 60 points from the level
        self.assertIsNone(c)
        self.assertIn("sl_atr_max", why[0])

    def test_tier_is_capped_at_c_for_the_playbook(self):
        c = settings.load(None)
        idea = {"confidence": 60, "astra_risk": "MEDIUM", "sl_anchor": {"id": "x"}, "tp_anchors": [{"id": "y"}]}
        dec = {"trade_class": "BREAKOUT_CONTINUATION", "expansion": {"against": False}, "would_block": False}
        t = TR.grade(idea, "SELL", dec, STATE, "NEW_YORK", c)
        self.assertEqual((t["tier"], t["risk_pct"]), ("C", 0.15))                 # class != TREND: C anyway
        c["tier_cap"] = {"TREND": "C"}
        t = TR.grade(idea, "SELL", dict(dec, trade_class="TREND"), STATE, "NEW_YORK", c)
        self.assertEqual(t["tier"], "C")
        self.assertIn("capped at C", t["reason"])
        self.assertEqual(c["playbook_modes"]["BREAKOUT_CONTINUATION"], "off")

    def test_settings_validation(self):
        d = Path(tempfile.mkdtemp())
        for bad in ('{"tier_cap": {"NOPE": "C"}}', '{"tier_cap": {"TREND": "D"}}', '{"breakout_retest_minutes": 1}'):
            (d / "c.json").write_text(bad)
            with self.assertRaises(ValueError, msg=bad):
                settings.load(d / "c.json")


class TradeSessionTests(Base):
    def test_no_new_orders_outside_trade_sessions(self):
        from autotrader import sessions as SS
        from autotrader.validator import Validator
        self.m.connect()
        v = Validator(self.c, self.m, self.state)
        with patch.object(self.m, "trading_window", return_value=None), patch.object(self.m, "news_block", return_value=None):
            self.assertIsNone(v.gates(datetime.now(UTC)))                        # harness pins NEW_YORK
            with patch.object(SS, "session_of", return_value="ASIA"):
                self.assertIn("session ASIA is not in trade_sessions", v.gates(datetime.now(UTC)))
                self.c["trade_sessions"] = []
                self.assertIsNone(v.gates(datetime.now(UTC)))                    # [] = every session
        self.assertEqual(settings.load(None)["trade_sessions"], ["LONDON", "LONDON_NY", "NEW_YORK"])
        d = Path(tempfile.mkdtemp())
        (d / "c.json").write_text('{"trade_sessions": ["TOKYO"]}')
        with self.assertRaises(ValueError):
            settings.load(d / "c.json")


class ReplayVariantTests(unittest.TestCase):
    def test_breakout_candidates_only_in_breakout_variants(self):
        self.assertIn("BREAKOUT_CONTINUATION", RP.VARIANTS["NEW_all_plus_breakout"][2])
        self.assertNotIn("BREAKOUT_CONTINUATION", RP.VARIANTS["NEW_all"][2])
        self.assertEqual(RP.VARIANTS["BREAKOUT_only"][2], {"BREAKOUT_CONTINUATION": "live"})
        self.assertEqual(RP.VARIANTS["CORE_noAsia"][5], ("LONDON", "LONDON_NY", "NEW_YORK"))
        self.assertEqual(RP.VARIANTS["CORE_noAsia_noRejection"][6], ("SWEEP", "CHOCH", "BREAKOUT"))
        self.assertIsNone(RP.VARIANTS["CORE"][5])
        rp = RP.Replay.__new__(RP.Replay)
        rp.cfg, rp.dropped = CFG, []
        rp._drop = lambda why: rp.dropped.append(why)
        rp.sim = None
        lv = [{"name": "ASIA_LOW", "side": "LOW", "price": 3986.6}, {"name": "PWL", "side": "LOW", "price": 3959.39}]
        out = rp._candidates(BAR, [BAR], STATE, lv, [EVENT], 8.0, BREAKOUT_T0 + timedelta(minutes=15), "NEW_YORK")
        self.assertEqual([(c["how"], c["tp1"]) for c in out], [("BREAKOUT", 3959.39)])
        self.assertEqual(rp.dropped, [])


class LiveBreakoutTests(Base):
    def setUp(self):
        super().setUp()
        self.c["playbook_modes"]["BREAKOUT_CONTINUATION"] = "live"
        self.v6dir = Path(tempfile.mkdtemp())
        self.c["v6"] = dict(self.c["v6"], state_directory=str(self.v6dir))
        self.app = App(self.c)
        self.app.market = self.m
        self.app.validator.m = self.app.executor.m = self.m
        self.m.connect()
        self.now = datetime.now(UTC)                                               # the validator uses the real clock
        t0 = self.now - timedelta(minutes=16)
        self.bar0 = bar(t0, 4023.64, 4023.64, 4007.44, 4008.2)
        self.event = dict(EVENT, bar_time=GM.iso(t0))
        self.bars = [self.bar0]
        self.snap = {"session": "NEW_YORK", "levels": [{"name": "NEW_YORK_OR_LOW", "side": "LOW", "price": 4021.58,
                                                          "formed_at": GM.iso(BREAKOUT_T0 - timedelta(hours=1)), "source": "SESSION"},
                                                         {"name": "ASIA_LOW", "side": "LOW", "price": 3986.6,
                                                          "formed_at": GM.iso(BREAKOUT_T0 - timedelta(hours=10)), "source": "SESSION"},
                                                         {"name": "PWL", "side": "LOW", "price": 3959.39,
                                                          "formed_at": GM.iso(BREAKOUT_T0 - timedelta(days=5)), "source": "PREV_WEEK"}],
                     "events": [self.event]}
        for x in (patch.object(self.m, "trading_window", return_value=None),
                  patch.object(self.m, "news_block", return_value=None),
                  patch.object(self.app, "market_context", return_value=(evidence(), 8.0)),
                  patch.object(self.app, "market_state", side_effect=lambda: (STATE, self.snap)),
                  patch.object(self.app, "recent_bars", side_effect=lambda: self.bars)):
            x.start()
            self.addCleanup(x.stop)
        self.fake.add_tick(4008.0, 4008.3)

    def test_breakout_becomes_a_tier_c_market_sell_once_per_candle(self):
        (self.v6dir / "astra_analysis.json").write_text(json.dumps({"id": "A1", "created_at": GM.iso(self.now), "ideas": []}))
        self.app.bridge_tick(self.now)
        self.assertEqual(len(self.fake.sent), 1)
        o = self.app.state.all_orders()[0]
        self.assertEqual((o["direction"], o["setup_id"], o["playbook"], o["confirmation"], o["tier"], o["strategy"]),
                         ("SELL", "BK-NEW_YORK_OR_LOW-SELL-4022", "BREAKOUT_CONTINUATION", "BREAKOUT", "C", "SWING_BREAKOUT"))
        self.assertEqual((o["sl"], o["tp1"]), (4023.98, 3959.39))
        self.assertEqual(o["lots"], 0.01)                                         # Tier C, one minimum lot
        self.app.bridge_tick(self.now + timedelta(seconds=30))                      # same candle: nothing new
        self.assertEqual(len(self.fake.sent), 1)

    def test_off_by_default_and_against_trend_refused(self):
        self.c["playbook_modes"]["BREAKOUT_CONTINUATION"] = "off"
        self.app.bridge_tick(self.now)
        self.assertEqual(self.fake.sent, [])
        self.c["playbook_modes"]["BREAKOUT_CONTINUATION"] = "live"
        self.app._bk_bar = None
        self.snap["events"] = [dict(self.event, direction="BUY", level="NEW_YORK_OR_HIGH", price=4030.0)]
        self.app.bridge_tick(self.now)
        self.assertEqual(self.fake.sent, [])


if __name__ == "__main__":
    unittest.main()
