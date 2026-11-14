#!/usr/bin/env python3
"""Offline tests for gold_monitor_v5.py: the full V4.5 regression suite (fixtures now carry a
V5 playbook + evidence ids) plus V5 tests for the sender race, evidence integration and
provider adapters. Never opens a socket or sends Telegram/API requests.
Run: python -m unittest -v test_gold_monitor_v5.py   (or: python gold_monitor_v5.py --self-test)
"""
import concurrent.futures as futures
import copy
from contextlib import closing, contextmanager
import inspect
import json
import os
import queue
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import gold_monitor as M  # noqa: E402
from gold_monitor import *  # noqa: E402,F401,F403
import gold_evidence as EV  # noqa: E402


@contextmanager
def app_test_directory(test):
    """Close test App resources before Windows removes its SQLite directory.

    unittest.addCleanup runs AFTER the test method exits, which is too late for
    tempfile.TemporaryDirectory.__exit__ on Windows: journal.sqlite3 is locked.
    """
    with tempfile.TemporaryDirectory() as folder:
        try:
            yield folder
        finally:
            key = str(Path(folder).resolve())
            registry = getattr(test, "_temporary_apps", {})
            for app in reversed(registry.pop(key, [])):
                app.stop_sender.set()
                if app.sender.is_alive():
                    app.sender.join(timeout=2)
                # All test network/AI work is faked. Wait for any active test jobs
                # to finish before closing the DB and removing its parent folder.
                app.pool.shutdown(wait=True, cancel_futures=True)
                app.journal.db.close()
                if getattr(app, "memory", None):
                    app.memory.db.close()      # V6 market memory lives in the same directory


def sys_module():
    return M


def fixture_evidence(at="2026-10-06T12:00:00+00:00", trend="NEUTRAL", drop=(), **status):
    def item(i, kind, d, tf, lo, hi, st, when=at):
        return {"id": i, "kind": kind, "direction": d, "timeframe": tf, "at": when,
                "level_low": lo, "level_high": hi, "status": st}
    earlier = iso(stamp(at) - timedelta(minutes=15))
    idx = {
        "ev_bos_buy_1h": item("ev_bos_buy_1h", "BOS", "BUY", "1h", 101., 101., "CONFIRMED"),
        "ev_fvg_buy_15m": item("ev_fvg_buy_15m", "FVG", "BUY", "15min", 99., 104., status.get("fvg", "ACTIVE")),
        "ev_bos_sell_1h": item("ev_bos_sell_1h", "BOS", "SELL", "1h", 103., 103., "CONFIRMED"),
        "ev_ob_sell_15m": item("ev_ob_sell_15m", "ORDER_BLOCK_CANDIDATE", "SELL", "15min", 99., 106., "ACTIVE"),
        "ev_choch_buy_15m": item("ev_choch_buy_15m", "CHOCH", "BUY", "15min", 101., 101., "CONFIRMED"),
        "ev_sweep_buy_15m": item("ev_sweep_buy_15m", "LIQUIDITY_SWEEP", "BUY", "15min", 99.5, 99.5, "CONFIRMED",
                                 status.get("sweep_at", earlier)),
        "ev_breaker_buy_15m": item("ev_breaker_buy_15m", "BREAKER_CANDIDATE", "BUY", "15min", 100., 101.,
                                   status.get("breaker", "RETEST_CONFIRMED")),
        "ev_fib_buy_1h": item("ev_fib_buy_1h", "FIB_OTE", "BUY", "1h", 99.5, 100.8, "ACTIVE"),
        "15min:RSI_DIVERGENCE_BUY:abc": item("15min:RSI_DIVERGENCE_BUY:abc", "RSI_DIVERGENCE", "BUY", "15min",
                                             None, None, "CONTEXT_ONLY"),
    }
    for k in drop:
        idx.pop(k)
    return {"built_at": at, "frames": {tf: {"available": True, "trend": trend} for tf in TF}, "index": idx}


FIX_EV = fixture_evidence()
_real_validate = M.validate_map


def validate_map(data, history, c, now=None, born=None, evidence=None):
    return _real_validate(data, history, c, now, born, FIX_EV if evidence is None else evidence)


BUY_IDS = ["ev_bos_buy_1h", "ev_fvg_buy_15m"]
SELL_IDS = ["ev_bos_sell_1h", "ev_ob_sell_15m"]


class Tests(unittest.TestCase):
    def setUp(self):
        self.c = load_config()
        self.c["providers"] = dict(self.c["providers"], broker_quotes="none")
        self.c["require_broker_quotes"] = False      # V5.0 mid-price mode; V5.1 broker rules in test_gold_v51.py
        self.now = stamp("2026-10-06T12:00:00Z")
        self.history = {}
        for tf, seconds in TF.items():
            self.history[tf] = [{"time": iso(self.now - timedelta(seconds=seconds * i)),
                                "open": 100., "high": 104., "low": 99., "close": 102.}
                               for i in range(220, 0, -1)]
        self.data = {
            "bias": {tf: "NEUTRAL" for tf in TF},
            "levels": [{"id": "demand1", "kind": "SUPPORT", "timeframe": "15min",
                        "low": 100., "high": 101., "evidence_times": [self.history["15min"][-1]["time"]], "reason": "fixture"}],
            "setups": [{"id": "buy1", "direction": "BUY", "zone_id": "demand1",
                        "playbook": "TREND_CONTINUATION", "evidence_ids": list(BUY_IDS),
                        "conditions": [{"type": "CLOSE_ABOVE", "timeframe": "15min", "level": 101.5}],
                        "entry_low": 102., "entry_high": 103., "stop_loss": 98., "targets": [115., 120.],
                        "risk": "LOW", "invalidation": 98.5, "expires_minutes": 90, "reason": "fixture"}],
            "summary": "test", "no_setup_reason": ""}
        self.w = Watcher(self.c, "2026-10-06")
        self.w.install(copy.deepcopy(self.data), self.history, self.now, self.now)
        self.sid = next(iter(self.w.setups))

    def bar(self, minutes, **changes):
        b = {"time": iso(self.now + timedelta(minutes=minutes - 15)),
             "open": 101., "high": 102., "low": 100., "close": 101.8}
        b.update(changes)
        return b

    def push(self, minutes, **changes):
        return self.w.bars("15min", [self.bar(minutes, **changes)], self.now + timedelta(minutes=minutes, seconds=12))

    def arm(self):
        self.push(15); self.push(30)
        return self.push(45)

    # ------------------------------------------------------------------ V4.5 regression suite
    def test_third_distinct_candle_only(self):
        self.assertEqual(self.push(15), [])
        self.assertEqual(self.push(15), [])
        self.assertEqual(self.push(30), [])
        result = self.push(45)
        self.assertEqual([x[0] for x in result], ["WATCH"])
        s = self.w.setups[self.sid]
        self.assertEqual(len(s["touches"]), 3)
        self.assertEqual(s["visits"], 1)
        self.assertEqual(s["stage"], 0)

    def test_fourth_touch_not_second_watch(self):
        self.arm()
        result = self.push(60)
        self.assertEqual([x[0] for x in result], ["READY"])
        self.assertFalse(any(x[0] == "WATCH" for x in self.push(75)))

    def test_ticks_do_not_count_contacts(self):
        for _ in range(200):
            self.w.quote(100.5, self.now + timedelta(minutes=1))
        self.assertEqual(self.w.setups[self.sid]["touches"], [])

    def test_no_same_candle_confirmation(self):
        self.arm()
        self.assertEqual(self.w.setups[self.sid]["stage"], 0)
        self.push(60)
        self.assertEqual(self.w.setups[self.sid]["stage"], 1)

    def test_ordered_stages_require_later_bars(self):
        self.w.setups[self.sid]["spec"]["conditions"].append({"type": "CLOSE_ABOVE", "timeframe": "15min", "level": 101.6})
        self.arm()
        self.assertEqual(self.push(60), [])
        self.assertEqual(self.w.setups[self.sid]["stage"], 1)
        self.assertEqual(self.push(75)[0][0], "READY")

    def test_touch_window_drops_old_contacts(self):
        self.w.c = dict(self.c, touch_window_bars=3)
        self.push(15); self.push(30, low=102, high=104, open=103, close=103)
        self.push(45, low=102, high=104, open=103, close=103)
        self.push(60)
        self.assertEqual(len(self.w.setups[self.sid]["touches"]), 1)

    def test_no_historical_replay(self):
        self.assertEqual(self.w.bars("15min", self.history["15min"], self.now), [])
        self.assertEqual(self.w.setups[self.sid]["touches"], [])

    def test_outage_catchup_not_a_signal(self):
        self.assertEqual(self.w.bars("15min", [self.bar(15)], self.now + timedelta(minutes=40)), [])
        self.assertEqual(self.w.setups[self.sid]["touches"], [])

    def test_reviewer_late_breach_candle_beats_newer_confirmation(self):
        self.assertEqual(self.arm()[0][0], "WATCH")
        breach = self.bar(60, low=98.0)
        confirm = self.bar(75, low=101.6, open=101.7, close=102.5)
        events = self.w.bars("15min", [breach, confirm], self.now + timedelta(minutes=75, seconds=12))
        self.assertEqual([e[0] for e in events], ["INVALIDATED"])
        self.assertIn("received late", events[0][2])
        self.assertEqual(self.w.setups[self.sid]["state"], "INVALIDATED")
        later = self.bar(90, low=101.6, open=101.7, close=102.5)
        self.assertEqual(self.w.bars("15min", [later], self.now + timedelta(minutes=90, seconds=12)), [])
        self.assertFalse(self.w.actionable(self.sid, 102.5, self.now + timedelta(minutes=90, seconds=20)))

    def test_breach_seen_only_on_higher_frame_invalidates(self):
        self.arm()
        h1 = {"time": iso(self.now), "open": 101., "high": 102., "low": 98.2, "close": 101.9}
        events = self.w.bars("1h", self.history["1h"] + [h1], self.now + timedelta(hours=1, seconds=12))
        self.assertEqual([e[0] for e in events], ["INVALIDATED"])

    def test_reviewer_late_breach_through_app_gate_sends_nothing(self):
        with app_test_directory(self) as folder:
            a = self.app(folder); self.arm()
            now = self.now + timedelta(minutes=75, seconds=12)
            news = {"risk": "LOW", "checked_at": iso(now), "summary": "t", "facts": [], "opinions": []}
            a.news = news; a.calendar["fetched_at"] = iso(now)
            a.reviewed[self.sid] = {"review": {"decision": "APPROVE", "risk": "LOW", "criteria": {k: True for k in CRITERIA},
                                               "reason": "t"}, "news": news, "setup_risk": "LOW", "until": time.time() + 600}
            bars = copy.deepcopy(self.history["15min"]) + [self.bar(60, low=98.0), self.bar(75, low=101.6, open=101.7, close=102.5)]
            f = futures.Future(); f.set_result(bars); a.data_jobs["fast"] = (f, "test")
            for tf in a.history:
                a.history[tf][-1]["time"] = iso(now - timedelta(seconds=TF[tf] + 12))
            a.history["15min"] = copy.deepcopy(self.history["15min"])
            with patch.object(a, "data_enabled", return_value=True):
                a.consume_data(now)
                a.update_gate(now)
            self.assertEqual(self.w.setups[self.sid]["state"], "INVALIDATED")
            self.assertEqual(a.delivery_gate.get("actionable", set()), set())
            self.assertFalse(any(k.endswith(":signal") for k, _ in list(a.outgoing.queue)))

    def test_wick_invalidation(self):
        event = self.push(15, low=98)
        self.assertEqual(event[0][0], "INVALIDATED")
        self.assertEqual(self.w.setups[self.sid]["state"], "INVALIDATED")

    def test_tick_invalidation(self):
        event = self.w.quote(98.5, self.now + timedelta(minutes=1))
        self.assertEqual(event[0][0], "INVALIDATED")
        self.assertEqual(self.w.quote(98., self.now + timedelta(minutes=2)), [])

    def test_expiry(self):
        event = self.w.quote(102., self.now + timedelta(minutes=91))
        self.assertEqual(event[0][0], "INVALIDATED")
        self.assertEqual(self.w.setups[self.sid]["state"], "EXPIRED")

    def test_refresh_preserves_state_and_expiry(self):
        self.arm()
        old = copy.deepcopy(self.w.setups[self.sid])
        self.w.install(copy.deepcopy(self.data), self.history, self.now + timedelta(minutes=46), self.now + timedelta(minutes=46))
        self.assertEqual(self.w.setups[self.sid]["touches"], old["touches"])
        self.assertEqual(self.w.setups[self.sid]["expires"], old["expires"])

    def refreshed(self, **changes):
        d = copy.deepcopy(self.data)
        d["setups"][0].update(changes.pop("setup", {}))
        d["levels"][0].update(changes.pop("zone", {}))
        return d

    def test_reviewer_hourly_refresh_continues_active_setup(self):
        self.push(15); self.push(30)
        before = copy.deepcopy(self.w.setups[self.sid])
        new = self.refreshed(setup={"stop_loss": 97.5, "entry_high": 102.8}, zone={"low": 100.2, "high": 101.2})
        validate_map(new, self.history, self.c)
        later = self.now + timedelta(minutes=31)
        self.w.install(new, self.history, later, later)
        self.assertEqual(list(self.w.setups), [self.sid])
        s = self.w.setups[self.sid]
        self.assertEqual(s["touches"], before["touches"])
        self.assertEqual(s["expires"], before["expires"])
        self.assertEqual(s["spec"]["stop_loss"], 97.5)
        self.assertEqual(self.w.revoked, [self.sid])
        self.assertEqual(self.push(45)[0][0], "WATCH")

    def test_review2_finished_continued_setup_never_revived(self):
        map2 = self.refreshed(setup={"stop_loss": 97.9})
        for terminal in ("TRIGGERED", "INVALIDATED", "EXPIRED", "CANCELLED"):
            w = Watcher(self.c, "s"); w.install(copy.deepcopy(self.data), self.history, self.now, self.now)
            sid = next(iter(w.setups))
            t1 = self.now + timedelta(minutes=10)
            w.install(copy.deepcopy(map2), self.history, t1, t1)
            self.assertEqual(list(w.setups), [sid])
            w.setups[sid]["state"] = terminal
            for k in (20, 30):
                t = self.now + timedelta(minutes=k)
                w.install(copy.deepcopy(map2 if k == 20 else self.data), self.history, t, t)
                self.assertEqual(list(w.setups), [sid], terminal)
                self.assertEqual(w.setups[sid]["state"], terminal)

    def test_review2_more_cautious_map_is_adopted_not_ignored(self):
        for change in ({"invalidation": 99.0}, {"stop_loss": 98.8, "invalidation": 98.9}, {"risk": "MEDIUM"}):
            w = Watcher(self.c, "s"); w.install(copy.deepcopy(self.data), self.history, self.now, self.now)
            sid = next(iter(w.setups)); w.setups[sid]["touches"] = ["x"]
            t = self.now + timedelta(minutes=10)
            w.install(self.refreshed(setup=change), self.history, t, t)
            self.assertEqual(w.setups[sid]["touches"], ["x"], change)
            for k, v in change.items():
                self.assertEqual(w.setups[sid]["spec"][k], v)
            self.assertEqual(w.revoked, [sid])

    def test_review2_high_rerating_always_sticks(self):
        w = Watcher(self.c, "s"); w.install(copy.deepcopy(self.data), self.history, self.now, self.now)
        sid = next(iter(w.setups))
        t1 = self.now + timedelta(minutes=10)
        w.install(self.refreshed(setup={"stop_loss": 97.9}), self.history, t1, t1)
        for k, m in ((20, self.refreshed(setup={"stop_loss": 97.9, "risk": "HIGH"})),
                     (30, self.refreshed(setup={"risk": "HIGH"}))):
            t = self.now + timedelta(minutes=k)
            w.install(m, self.history, t, t)
            self.assertEqual(w.setups[sid]["spec"]["risk"], "HIGH")

    def test_review2_level_grounding_edges(self):
        h = copy.deepcopy(self.history)
        h["15min"][-5].update(low=49., high=51.); h["15min"][-3].update(low=149., high=151.)
        giant = copy.deepcopy(self.data)
        giant["levels"].append({"id": "wide", "kind": "SUPPORT", "timeframe": "1week", "low": 0.01, "high": 1e9,
                                "evidence_times": [h["1week"][-1]["time"]], "reason": "x"})
        spread = copy.deepcopy(self.data)
        spread["levels"].append({"id": "spread", "kind": "SUPPORT", "timeframe": "15min", "low": 103.4, "high": 103.6,
                                 "evidence_times": [h["15min"][-5]["time"], h["15min"][-3]["time"]], "reason": "x"})
        for d in (giant, spread):
            with self.assertRaisesRegex(ValueError, "evidence candles|wider"):
                validate_map(d, h, self.c)
        h2 = copy.deepcopy(self.history)
        h2["15min"][-4].update(low=99., high=100.2); h2["15min"][-2].update(low=100.8, high=104.)
        fvg = copy.deepcopy(self.data)
        fvg["levels"].append({"id": "fvg", "kind": "FVG", "timeframe": "15min", "low": 100.2, "high": 100.8,
                              "evidence_times": [h2["15min"][-4]["time"], h2["15min"][-2]["time"]], "reason": "x"})
        validate_map(fvg, h2, self.c)

    def test_review2_sweep_beyond_extreme_and_meaningless_close(self):
        sell = copy.deepcopy(self.data)
        sell["levels"][0].update(kind="RESISTANCE", low=103.5, high=104.)
        sell["setups"][0].update(direction="SELL", entry_low=102., entry_high=103., stop_loss=106., invalidation=105.5,
                                 targets=[90.], evidence_ids=list(SELL_IDS),
                                 conditions=[{"type": "SWEEP_RECLAIM_DOWN", "timeframe": "15min", "level": 104.2}])
        validate_map(sell, self.history, self.c)
        buy = copy.deepcopy(self.data)
        buy["setups"][0]["conditions"][0].update(level=100.5)
        with self.assertRaisesRegex(ValueError, "not a confirmation"):
            validate_map(buy, self.history, self.c)
        h = copy.deepcopy(self.history)
        for b in h["15min"]:
            b.update(high=150., low=50.)
        far = copy.deepcopy(self.data); far["setups"][0]["conditions"][0].update(level=114.)
        with self.assertRaisesRegex(ValueError, "not anchored"):
            validate_map(far, h, self.c)

    def test_review2_telegram_retry_after(self):
        import requests
        r = Mock(status_code=429); r.json.return_value = {"ok": False, "parameters": {"retry_after": 42}}
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}), patch.object(requests, "post", return_value=r):
            self.assertEqual(delivery_status(requests, "x"), ("DELIVERY_FAILED", 42))
        with app_test_directory(self) as folder:
            a = self.app(folder)
            a.emit("k:watch", "W"); _, packet = a.outgoing.get_nowait()
            packet["retry_after"] = 42
            a.delivery_results.put(("k:watch", "DELIVERY_FAILED", packet)); a.drain_delivery()
            self.assertGreaterEqual(a.journal.get("rearm:k:watch")["after"] - time.time(), 41)
            self.assertGreaterEqual(a.retries[0][0] - time.time(), 41)

    def test_reviewer_terminated_id_not_recycled_with_new_zone(self):
        self.w.setups[self.sid]["state"] = "INVALIDATED"
        moved = self.refreshed(zone={"low": 102.5, "high": 103.0},
                               setup={"entry_low": 103.2, "entry_high": 103.5, "conditions":
                                      [{"type": "CLOSE_ABOVE", "timeframe": "15min", "level": 103.1}]})
        for k in (10, 20):
            t = self.now + timedelta(minutes=k)
            self.w.install(copy.deepcopy(moved), self.history, t, t)
            self.assertEqual([st["state"] for st in self.w.setups.values()], [], k)
        renamed = copy.deepcopy(moved); renamed["setups"][0]["id"] = "buy2"
        t = self.now + timedelta(minutes=30)
        self.w.install(renamed, self.history, t, t)
        self.assertEqual([st["spec"]["id"] for st in self.w.setups.values()], ["buy2"])

    def test_dropped_id_cannot_return_later(self):
        t = self.now + timedelta(minutes=10)
        self.w.install(dict(copy.deepcopy(self.data), setups=[]), self.history, t, t)
        t = self.now + timedelta(minutes=20)
        self.w.install(copy.deepcopy(self.data), self.history, t, t)
        self.assertEqual(self.w.setups, {})

    def approve_now(self, a, now, risk="LOW"):
        news = {"risk": "LOW", "checked_at": iso(now), "summary": "t", "facts": [], "opinions": []}
        a.news = news; a.calendar["fetched_at"] = iso(now)
        self.fresh_history(a, now)
        st = self.w.setups[self.sid]
        a.reviewed[self.sid] = {"review": {"decision": "APPROVE", "risk": risk, "reason": "t",
                                           "criteria": {k: True for k in CRITERIA}},
                                "news": news, "setup_risk": st["spec"]["risk"], "plan": plan_fingerprint(st),
                                "until": time.time() + 600}

    def map_result(self, data, now):
        return ({"kind": "map", "tag": {"epoch": "test", "history": self.history, "born": iso(now)}},
                "result", {"technical": data, "news": None})

    def test_reviewer_changed_condition_blocks_signal_end_to_end(self):
        with app_test_directory(self) as folder:
            a = self.app(folder); self.arm()
            now = self.now + timedelta(minutes=50)
            self.approve_now(a, now)
            new = self.refreshed(setup={"entry_low": 103.6, "entry_high": 104.0, "targets": [120.], "conditions":
                                        [{"type": "CLOSE_ABOVE", "timeframe": "15min", "level": 103.5}]})
            new["levels"].append({"id": "r1", "kind": "RESISTANCE", "timeframe": "15min", "low": 103.4, "high": 103.6,
                                  "evidence_times": [self.history["15min"][-1]["time"]], "reason": "fixture"})
            validate_map(copy.deepcopy(new), self.history, self.c)
            a.analyst = Mock(); a.analyst.poll.return_value = self.map_result(new, now)
            with patch.object(sys_module(), "history_fresh", return_value=True):
                a.consume_ai(now)
            st = self.w.setups[self.sid]
            self.assertEqual((st["stage"], st["spec"]["conditions"][0]["level"]), (0, 103.5))
            self.assertNotIn(self.sid, a.reviewed)
            self.assertEqual(a.pending.get(self.sid), "WATCH")
            self.w.bars("15min", [self.bar(60, low=101.6, open=101.7, close=102.5)],
                        self.now + timedelta(minutes=60, seconds=12))
            self.assertEqual(st["stage"], 0)
            later = self.now + timedelta(minutes=60, seconds=15)
            with patch.object(a, "data_enabled", return_value=True):
                a.update_gate(later)
            self.assertEqual(a.delivery_gate["actionable"], set())
            self.assertFalse(any(k.endswith(":signal") for k, _ in list(a.outgoing.queue)))

    def test_reviewer_medium_reclassification_never_sends_a_trade(self):
        with app_test_directory(self) as folder:
            a = self.app(folder); self.arm()
            t1 = self.now + timedelta(minutes=46)
            cont = self.refreshed(setup={"stop_loss": 97.9})
            self.w.install(copy.deepcopy(cont), self.history, t1, t1)
            self.push(60)
            now = self.now + timedelta(minutes=60, seconds=15)
            self.approve_now(a, now)
            with patch.object(a, "data_enabled", return_value=True):
                a.update_gate(now)
            _, review, _ = a.delivery_gate["plans"][self.sid]
            self.assertEqual(review["grade"], "A_TRADE")
            a.outgoing = queue.Queue(); a.journal.db.execute("DELETE FROM events"); a.journal.db.commit()
            medium = copy.deepcopy(cont); medium["setups"][0]["risk"] = "MEDIUM"
            a.analyst = Mock(); a.analyst.poll.return_value = self.map_result(medium, now)
            with patch.object(sys_module(), "history_fresh", return_value=True):
                a.consume_ai(now)
            self.assertEqual(self.w.setups[self.sid]["spec"]["risk"], "MEDIUM")
            self.assertNotIn(self.sid, a.reviewed)
            with patch.object(a, "data_enabled", return_value=True):
                a.update_gate(now)
            self.assertEqual(a.delivery_gate["actionable"], set())
            self.assertFalse(any(k.endswith(":signal") for k, _ in list(a.outgoing.queue)))
            self.approve_now(a, now); a.reviewed[self.sid]["setup_risk"] = "LOW"
            with patch.object(a, "data_enabled", return_value=True):
                a.update_gate(now)
            self.assertEqual(a.delivery_gate["plans"][self.sid][1]["grade"], "B_TRADE")

    def test_inflight_review_of_superseded_plan_is_discarded(self):
        with app_test_directory(self) as folder:
            a = self.app(folder); self.arm()
            stale_tag = plan_fingerprint(self.w.setups[self.sid])
            t = self.now + timedelta(minutes=46)
            self.w.install(self.refreshed(setup={"stop_loss": 97.9}), self.history, t, t)
            a.analyst = Mock()
            a.analyst.poll.return_value = ({"kind": "review", "tag": {"sid": self.sid, "epoch": "test", "purpose": "WATCH",
                                                                     "plan": stale_tag}}, "result",
                {"technical": {"decision": "APPROVE", "risk": "LOW", "criteria": {k: True for k in CRITERIA}, "reason": "t"},
                 "news": {"risk": "LOW", "checked_at": iso(t), "facts": [], "opinions": [], "summary": "t"}})
            a.consume_ai(t)
            self.assertNotIn(self.sid, a.reviewed)
            self.assertEqual(a.pending.get(self.sid), "WATCH")

    def test_revised_invalidation_already_breached_invalidates(self):
        self.arm()
        hist = copy.deepcopy(self.history)
        hist["15min"].append(dict(self.bar(30, low=99.2), closes=iso(self.now + timedelta(minutes=30))))
        t = self.now + timedelta(minutes=46)
        self.w.install(self.refreshed(setup={"invalidation": 99.5, "stop_loss": 99.4, "entry_low": 102.}), hist, t, t)
        self.assertEqual(self.w.setups[self.sid]["state"], "INVALIDATED")
        self.assertEqual(self.w.install_events[0][0], "INVALIDATED")

    def test_review3_breach_during_analysis_kills_new_setup(self):
        born = self.now - timedelta(minutes=13)
        hist = copy.deepcopy(self.history)
        hist["15min"][-1].update(low=97.0, closes=iso(self.now))
        w = Watcher(self.c, "s"); install_at = self.now + timedelta(minutes=3)
        w.install(copy.deepcopy(self.data), hist, install_at, born)
        sid = next(iter(w.setups))
        self.assertEqual(w.setups[sid]["state"], "INVALIDATED")
        self.assertIn("during analysis", w.install_events[0][2])
        for m in (15, 30, 45, 60):
            b = self.bar(m, **({"low": 101.6, "open": 101.7, "close": 102.5} if m == 60 else {}))
            self.assertEqual(w.bars("15min", [b], self.now + timedelta(minutes=m, seconds=12)), [])

    def test_review3_live_tick_during_analysis_kills_setup_end_to_end(self):
        with app_test_directory(self) as folder:
            a = self.app(folder)
            a.watcher = Watcher(self.c, "s"); a.extremes = {"low": 98.2, "high": 101.}
            a.analyst = Mock(); a.analyst.poll.return_value = (
                {"kind": "map", "tag": {"epoch": "test", "history": self.history, "born": iso(self.now)}},
                "result", {"technical": copy.deepcopy(self.data), "news": None})
            with patch.object(sys_module(), "history_fresh", return_value=True):
                a.consume_ai(self.now + timedelta(minutes=3))
            self.assertEqual([st["state"] for st in a.watcher.setups.values()], ["INVALIDATED"])

    def test_review3_backfilled_candle_inspected_and_gap_blocks_signal(self):
        self.arm()
        confirm = self.bar(75, low=101.6, open=101.7, close=102.5)
        events = self.w.bars("15min", self.history["15min"] + [self.bar(15), self.bar(30), self.bar(45), confirm],
                             self.now + timedelta(minutes=75, seconds=12))
        self.assertEqual([e[0] for e in events], ["READY"])
        hist = self.history["15min"] + [self.bar(m) for m in (15, 30, 45)] + [confirm]
        self.assertTrue(history_gap(hist, self.now, Sessions(self.c)))
        backfill = self.bar(60, low=97.0)
        events = self.w.bars("15min", hist[:-1] + [backfill, confirm], self.now + timedelta(minutes=80))
        self.assertEqual([e[0] for e in events], ["INVALIDATED"])
        self.assertFalse(self.w.actionable(self.sid, 102.5, self.now + timedelta(minutes=80)))

    def test_review3_gap_withholds_signal_at_gate(self):
        with app_test_directory(self) as folder:
            a = self.app(folder)
            self.arm(); self.push(60)
            now = self.now + timedelta(minutes=60, seconds=15)
            self.approve_now(a, now)
            complete = copy.deepcopy(a.history["15min"])
            with patch.object(a, "data_enabled", return_value=True):
                a.update_gate(now)
            self.assertIn(self.sid, a.delivery_gate["actionable"])
            a.outgoing = queue.Queue(); a.journal.db.execute("DELETE FROM events"); a.journal.db.commit()
            del a.history["15min"][-3]
            with patch.object(a, "data_enabled", return_value=True):
                a.update_gate(now)
            self.assertEqual(a.delivery_gate["actionable"], set())
            self.assertTrue(a.outgoing.empty())
            a.history["15min"] = complete
            with patch.object(a, "data_enabled", return_value=True):
                a.update_gate(now)
            self.assertIn(self.sid, a.delivery_gate["actionable"])

    def test_review3_sender_rechecks_live_state_and_keeps_invalidated(self):
        import requests
        with app_test_directory(self) as folder:
            a = self.app(folder)
            self.arm(); self.push(60)
            st = self.w.setups[self.sid]
            review = {"risk": "LOW", "reason": "t", "grade": "A_TRADE"}
            news = {"risk": "LOW", "summary": "t"}
            a.delivery_gate = {"epoch": "test", "fresh": True, "valid": {self.sid}, "actionable": {self.sid},
                               "price": 102.5, "plans": {self.sid: (copy.deepcopy(st), review, news)}}
            st["state"] = "INVALIDATED"
            a.outgoing.put((self.sid + ":signal", {"text": "x", "epoch": "test", "sid": self.sid, "plan": True,
                                                   "expires": time.time() + 60, "report": None}))
            original_put = a.delivery_results.put
            def finish(value):
                original_put(value); a.stop_sender.set()
            with patch.object(sys_module(), "utcnow", return_value=self.now + timedelta(minutes=61)), \
                 patch.object(a, "quote_fresh", return_value=True), \
                 patch.object(a.delivery_results, "put", side_effect=finish), patch.object(requests, "post") as post:
                a.send_loop()
            post.assert_not_called()
            a.delivery_results.put((self.sid + ":signal", "DELIVERY_UNCERTAIN", {"plan": True, "sid": self.sid, "report": None}))
            a.drain_delivery()
            self.assertEqual(st["state"], "INVALIDATED")

    def test_review3_invalidation_event_withdraws_gate_immediately(self):
        with app_test_directory(self) as folder:
            a = self.app(folder)
            a.delivery_gate = {"epoch": "test", "fresh": True, "valid": {self.sid}, "actionable": {self.sid}, "plans": {}}
            self.w.setups[self.sid]["state"] = "INVALIDATED"
            a.events([("INVALIDATED", self.sid, "t")], self.now)
            self.assertNotIn(self.sid, a.delivery_gate["actionable"])
            self.assertNotIn(self.sid, a.delivery_gate["valid"])

    def test_review3_newest_plan_can_shorten_never_extend_expiry(self):
        original = self.w.setups[self.sid]["expires"]
        t = self.now + timedelta(minutes=10)
        self.w.install(self.refreshed(setup={"expires_minutes": 60}), self.history, t, t)
        self.assertEqual(self.w.setups[self.sid]["expires"], iso(t + timedelta(minutes=60)))
        t2 = self.now + timedelta(minutes=20)
        self.w.install(self.refreshed(setup={"expires_minutes": 180}), self.history, t2, t2)
        self.assertEqual(self.w.setups[self.sid]["expires"], iso(t + timedelta(minutes=60)))
        self.assertLess(stamp(self.w.setups[self.sid]["expires"]), stamp(original))

    def test_review3_ready_shortcut_requires_current_plan(self):
        with app_test_directory(self) as folder:
            a = self.app(folder); self.arm(); self.push(60)
            now = self.now + timedelta(minutes=60, seconds=15)
            self.approve_now(a, now)
            a.reviewed[self.sid]["plan"] = "an-older-plan"
            a.pending[self.sid] = "READY"; a.analyst = Mock(job=None); a.full_ready = False
            with patch.object(sys_module(), "history_fresh", return_value=True):
                a.dispatch_ai(now)
            a.analyst.start.assert_called_once()

    def test_reworded_reason_keeps_approval(self):
        fp = plan_fingerprint(self.w.setups[self.sid])
        t = self.now + timedelta(minutes=10)
        self.w.install(self.refreshed(setup={"reason": "reworded"}), self.history, t, t)
        self.assertEqual(plan_fingerprint(self.w.setups[self.sid]), fp)
        self.assertEqual(self.w.revoked, [])

    def test_review4_candle_arriving_after_install_but_closed_during_analysis(self):
        born = self.now + timedelta(minutes=2)
        w = Watcher(self.c, "s"); at = self.now + timedelta(minutes=15, seconds=5)
        w.install(copy.deepcopy(self.data), self.history, at, born, last_close(self.history, "15min"))
        late = self.bar(15, low=97.0)
        events = w.bars("15min", self.history["15min"] + [late], self.now + timedelta(minutes=15, seconds=30))
        self.assertEqual([e[0] for e in events], ["INVALIDATED"])

    def test_review4_candle_missing_from_snapshot_is_scanned(self):
        snapshot = copy.deepcopy(self.history); snapshot["15min"] = snapshot["15min"][:-1]
        live = copy.deepcopy(self.history); live["15min"][-1].update(low=97.0)
        w = Watcher(self.c, "s")
        w.install(copy.deepcopy(self.data), live, self.now + timedelta(minutes=3), self.now - timedelta(minutes=5),
                  last_close(snapshot, "15min"))
        self.assertEqual(next(iter(w.setups.values()))["state"], "INVALIDATED")

    def test_review4_feed_window_sees_every_print(self):
        f = PriceFeed(self.c)
        for price in (102.5, 98.0, 102.4):
            with f.window_lock:
                w = f.window
                f.window = {"low": price, "high": price} if w is None else {"low": min(w["low"], price), "high": max(w["high"], price)}
        self.assertEqual(f.take_extremes(), {"low": 98.0, "high": 102.5})
        self.assertIsNone(f.take_extremes())
        self.arm()
        events = self.w.quote(102.4, self.now + timedelta(minutes=46), low=98.0, high=102.5)
        self.assertEqual([e[0] for e in events], ["INVALIDATED"])

    def test_review4_extremes_checked_inside_install_before_io(self):
        w = Watcher(self.c, "s")
        w.install(copy.deepcopy(self.data), self.history, self.now + timedelta(minutes=3), self.now, None,
                  {"low": 98.2, "high": 101.})
        self.assertEqual(next(iter(w.setups.values()))["state"], "INVALIDATED")
        with app_test_directory(self) as folder:
            a = self.app(folder); a.watcher = Watcher(self.c, "s"); a.extremes = {"low": 98.2, "high": 101.}
            a.analyst = Mock(); a.analyst.poll.return_value = (
                {"kind": "map", "tag": {"epoch": "test", "history": self.history, "born": iso(self.now)}},
                "result", {"technical": copy.deepcopy(self.data), "news": None})
            real_audit = a.journal.audit
            def locked_after_install(kind, data):
                if kind == "AI_RESULT":
                    return real_audit(kind, data)
                raise sqlite3.OperationalError("database is locked")
            with patch.object(sys_module(), "history_fresh", return_value=True), \
                 patch.object(a.journal, "audit", side_effect=locked_after_install):
                try:
                    a.consume_ai(self.now + timedelta(minutes=3))
                except sqlite3.OperationalError:
                    pass
            self.assertEqual([st["state"] for st in a.watcher.setups.values()], ["INVALIDATED"])

    def test_review4_tail_gap_withholds_signal(self):
        bars = copy.deepcopy(self.history["15min"])
        S = Sessions(self.c); since = self.now - timedelta(hours=1)
        self.assertFalse(history_gap(bars, since, S, self.now + timedelta(minutes=2)))
        self.assertTrue(history_gap(bars, since, S, self.now + timedelta(minutes=19)))

    def test_review4_failed_intraday_fetch_really_retries(self):
        with app_test_directory(self) as folder:
            a = self.app(folder)
            f = futures.Future(); f.set_exception(RuntimeError("provider timeout"))
            a.data_jobs["intraday"] = (f, "test")
            with patch.object(a, "data_enabled", return_value=True):
                a.consume_data(self.now)
            self.assertIsNone(a.last_bar_slot)
            self.assertGreater(a.intraday_retry_at, time.time() + 20)

    def queued_signal_app(self, folder):
        a = self.app(folder); self.arm(); self.push(60)
        now = self.now + timedelta(minutes=60, seconds=15)
        self.approve_now(a, now)
        with patch.object(a, "data_enabled", return_value=True):
            a.update_gate(now)
        self.assertIn(self.sid, a.delivery_gate["actionable"])
        return a, now

    def run_sender(self, a, now, http=None):
        import requests
        original_put = a.delivery_results.put
        def finish(value):
            original_put(value); a.stop_sender.set()
        with patch.object(sys_module(), "utcnow", return_value=now), patch.object(a, "quote_fresh", return_value=True), \
             patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "offline", "TELEGRAM_CHAT_ID": "offline"}), \
             patch.object(a.delivery_results, "put", side_effect=finish), patch.object(requests, "post") as post:
            if http:
                post.side_effect = http
            else:
                post.return_value = Mock(status_code=200, json=Mock(return_value={"ok": True}))
            a.send_loop()
        return post

    def test_review4_queued_signal_dropped_after_revision_or_high_news(self):
        for change in ("revision", "news", "unapproved", "baseline"):
            self.setUp()
            with app_test_directory(self) as folder:
                a, now = self.queued_signal_app(folder)
                st = self.w.setups[self.sid]
                if change == "revision":
                    st["spec"] = dict(st["spec"], stop_loss=99.0, invalidation=99.5); st["stage"] = 0
                elif change == "news":
                    a.news = dict(a.news, risk="HIGH")
                elif change == "unapproved":
                    a.reviewed.clear()
                post = self.run_sender(a, now)
                status = a.delivery_results.queue[-1][1] if a.delivery_results.queue else None
                self.assertEqual(post.called, change == "baseline", (change, status))

    def test_review4_high_news_withdraws_gate_immediately(self):
        with app_test_directory(self) as folder:
            a, now = self.queued_signal_app(folder)
            a.analyst = Mock(); a.analyst.poll.return_value = (
                {"kind": "review", "tag": {"sid": "other", "epoch": "test", "purpose": "WATCH", "plan": "x"}}, "result",
                {"technical": {"decision": "WATCH", "risk": "LOW", "criteria": {k: True for k in CRITERIA}, "reason": "t"},
                 "news": {"risk": "HIGH", "checked_at": iso(now), "facts": [], "opinions": [], "summary": "war"}})
            a.consume_ai(now)
            self.assertEqual(a.delivery_gate["actionable"], set())

    def test_refresh_with_different_direction_or_zone_is_new_setup(self):
        self.arm()
        far = self.refreshed(zone={"low": 101.8, "high": 102.0}, setup={"entry_low": 102.5, "entry_high": 103.})
        later = self.now + timedelta(minutes=46)
        self.w.install(far, self.history, later, later)
        self.assertNotIn(self.sid, self.w.setups)
        self.assertEqual(next(iter(self.w.setups.values()))["touches"], [])

    def test_reviewer_visits_mode_survives_map_refresh(self):
        c = dict(self.c, touch_mode="visits", touch_exit_bars=2)
        d = copy.deepcopy(self.data); d["setups"][0]["expires_minutes"] = 180
        validate_map(copy.deepcopy(d), self.history, c)
        w = Watcher(c, "visits"); w.install(copy.deepcopy(d), self.history, self.now, self.now)
        sid = next(iter(w.setups)); events = []
        pattern = [True, False, False, True, False, False, True, None]
        for i, t in enumerate(pattern, 1):
            minutes = 15 * i
            if minutes == 60:
                at = self.now + timedelta(minutes=60)
                w.install(self.refreshed(setup={"expires_minutes": 180, "stop_loss": 97.9}), self.history, at, at)
            changes = {} if t else dict(low=102.2, high=103, open=102.5, close=102.8)
            if t is None:
                changes = dict(low=101.6, high=103, open=101.7, close=102.5)
            events += w.bars("15min", [self.bar(minutes, **changes)], self.now + timedelta(minutes=minutes, seconds=12))
        self.assertEqual([e[0] for e in events], ["WATCH", "READY"])
        self.assertEqual(w.setups[sid]["visits"], 3)

    def test_refresh_cannot_revive_terminal(self):
        self.w.quote(98, self.now)
        self.w.install(copy.deepcopy(self.data), self.history, self.now + timedelta(minutes=1), self.now)
        self.assertEqual(self.w.setups[self.sid]["state"], "INVALIDATED")

    def test_geometry_and_latest_entry(self):
        validate_map(copy.deepcopy(self.data), self.history, self.c)
        self.arm(); self.push(60)
        now = self.now + timedelta(minutes=60, seconds=15)
        self.assertTrue(self.w.actionable(self.sid, 102.5, now))
        self.assertFalse(self.w.actionable(self.sid, 104., now))
        self.assertFalse(self.w.actionable(self.sid, 97., now))
        self.w.setups[self.sid]["spec"]["risk"] = "HIGH"
        self.assertFalse(self.w.actionable(self.sid, 102.5, now))

    def test_bad_map_rejected(self):
        for mutate in (
            lambda d: d["levels"][0].update(evidence_times=["invented"]),
            lambda d: d["setups"][0].update(stop_loss=104),
            lambda d: d["setups"][0].update(targets=[104]),
            lambda d: d["setups"][0]["conditions"][0].update(type="UNKNOWN"),
            lambda d: d["setups"][0].update(entry_low=float("nan")),
            lambda d: d["levels"][0].update(low=True),
            lambda d: d["setups"][0].update(playbook="GUT_FEELING"),
            lambda d: d["setups"][0].pop("evidence_ids"),
        ):
            d = copy.deepcopy(self.data); mutate(d)
            with self.assertRaises(Exception):
                validate_map(d, self.history, self.c)

    def test_reviewer_ungrounded_confirmation_rejected(self):
        cases = {
            "far level 1.0": lambda d: d["setups"][0]["conditions"][0].update(level=1.0),
            "outside range": lambda d: d["setups"][0]["conditions"][0].update(level=110.),
            "wrong direction": lambda d: d["setups"][0]["conditions"][0].update(type="CLOSE_BELOW"),
            "below invalidation": lambda d: d["setups"][0]["conditions"][0].update(level=99.2, type="SWEEP_RECLAIM_UP")
                or d["setups"][0].update(invalidation=99.5, stop_loss=99.4, entry_low=99.6) or d["levels"][0].update(low=99.6),
            "level not on evidence": lambda d: d["levels"][0].update(low=5000., high=5001.),
        }
        for name, mutate in cases.items():
            d = copy.deepcopy(self.data); mutate(d)
            with self.assertRaises(ValueError, msg=name):
                validate_map(d, self.history, self.c)
        validate_map(copy.deepcopy(self.data), self.history, self.c)

    def test_condition_must_anchor_to_mapped_level(self):
        d = copy.deepcopy(self.data)
        hist = copy.deepcopy(self.history)
        for b in hist["15min"][-30:]:
            b.update(high=112., low=99.)
        d["setups"][0]["conditions"][0]["level"] = 108.
        with self.assertRaisesRegex(ValueError, "not anchored"):
            validate_map(d, hist, self.c)
        d["levels"].append({"id": "r1", "kind": "RESISTANCE", "timeframe": "15min", "low": 107.8, "high": 108.2,
                            "evidence_times": [hist["15min"][-2]["time"]], "reason": "fixture"})
        validate_map(d, hist, self.c)

    def test_timeline_uses_each_frames_next_close(self):
        d = copy.deepcopy(self.data)
        d["setups"][0]["conditions"] = [{"type": "CLOSE_ABOVE", "timeframe": "1h", "level": 101.5},
                                        {"type": "CLOSE_ABOVE", "timeframe": "15min", "level": 101.6}]
        self.assertEqual(earliest_ready_seconds(d["setups"][0], self.history, self.c), 75 * 60)
        for minutes in (70, 75, 78):
            d["setups"][0]["expires_minutes"] = minutes
            with self.assertRaisesRegex(ValueError, "Lifetime too short"):
                validate_map(d, self.history, self.c)
        d["setups"][0]["expires_minutes"] = 79
        validate_map(d, self.history, self.c)
        d["setups"][0]["conditions"] = [{"type": "CLOSE_ABOVE", "timeframe": "4h", "level": 101.5}]
        d["setups"][0]["expires_minutes"] = 180
        with self.assertRaisesRegex(ValueError, "Lifetime too short"):
            validate_map(d, self.history, self.c)
        h = copy.deepcopy(self.history)
        for b in h["4h"]:
            b["time"] = iso(stamp(b["time"]) + timedelta(minutes=60))
        h["4h"] = h["4h"][:-1]
        self.assertEqual(earliest_ready_seconds(d["setups"][0], h, self.c), 60 * 60)
        validate_map(d, h, self.c)
        d = copy.deepcopy(self.data)
        with self.assertRaisesRegex(ValueError, "Lifetime too short"):
            validate_map(d, self.history, dict(self.c, touch_mode="visits", touch_exit_bars=2))

    def test_reviewer_60_minute_setup_with_ready_at_60_rejected(self):
        d = copy.deepcopy(self.data); d["setups"][0]["expires_minutes"] = 60
        self.assertEqual(earliest_ready_seconds(d["setups"][0], self.history, self.c), 3600)
        with self.assertRaisesRegex(ValueError, "Lifetime too short"):
            validate_map(d, self.history, self.c)
        d["setups"][0]["expires_minutes"] = 64
        validate_map(d, self.history, self.c)

    def test_expiry_counts_install_delay(self):
        d = copy.deepcopy(self.data); d["setups"][0]["expires_minutes"] = 75
        born = last_close(self.history, "15min")
        with self.assertRaisesRegex(ValueError, "Lifetime too short"):
            validate_map(d, self.history, self.c, born + timedelta(minutes=20), born)
        d["setups"][0]["expires_minutes"] = 79
        validate_map(d, self.history, self.c, born + timedelta(minutes=20), born)
        w = Watcher(self.c, "late"); at = born + timedelta(minutes=20)
        w.install(copy.deepcopy(d), self.history, at, born)
        events = []
        for m in (30, 45, 60, 75):
            b = dict(self.bar(m), **({"low": 101.6, "open": 101.7, "close": 102.5} if m == 75 else {}))
            events += w.bars("15min", [b], self.now + timedelta(minutes=m, seconds=12))
        self.assertEqual([e[0] for e in events], ["WATCH", "READY"])

    def test_empty_map_allowed(self):
        d = copy.deepcopy(self.data); d.update(levels=[], setups=[], no_setup_reason="Insufficient evidence")
        validate_map(d, self.history, self.c)
        _real_validate(copy.deepcopy(d), self.history, self.c)   # no evidence needed without setups

    def test_condition_definitions(self):
        b = {"open": 101, "high": 103, "low": 99, "close": 102}
        self.assertTrue(condition_passes({"type": "SWEEP_RECLAIM_UP", "level": 100}, b))
        self.assertTrue(condition_passes({"type": "BULL_REJECTION", "level": 100}, b))
        self.assertFalse(condition_passes({"type": "CLOSE_ABOVE", "level": 102}, b))
        b["close"] = b["open"]
        self.assertFalse(condition_passes({"type": "BULL_REJECTION", "level": 100}, b))

    def test_weekend_break_and_dst(self):
        s = Sessions(self.c)
        self.assertFalse(s.is_open(stamp("2026-10-10T12:00:00Z")))
        self.assertFalse(s.is_open(stamp("2026-10-09T21:00:00Z")))
        self.assertFalse(s.is_open(stamp("2026-10-11T21:59:00Z")))
        self.assertTrue(s.is_open(stamp("2026-10-11T22:00:00Z")))
        self.assertFalse(s.is_open(stamp("2026-10-06T21:30:00Z")))
        self.assertTrue(s.is_open(stamp("2026-10-06T22:00:00Z")))
        self.assertFalse(s.is_open(stamp("2026-11-08T22:59:00Z")))
        self.assertTrue(s.is_open(stamp("2026-11-08T23:00:00Z")))

    def test_holidays_and_early_close(self):
        c = dict(self.c, closed_dates=["2026-10-06"])
        self.assertFalse(Sessions(c).is_open(self.now))
        c = dict(self.c, closed_intervals_utc=[["2026-10-06T11:00:00Z", "2026-10-06T13:00:00Z"]])
        self.assertFalse(Sessions(c).is_open(self.now))

    def test_news_calendar_fail_closed(self):
        self.assertIsNotNone(calendar_block(None, self.now, self.c))
        cal = {"fetched_at": iso(self.now), "events": [{"time": iso(self.now + timedelta(minutes=20)), "title": "CPI", "impact": "High"}]}
        self.assertIn("CPI", calendar_block(cal, self.now, self.c))
        cal["events"][0]["time"] = iso(self.now + timedelta(hours=2))
        self.assertIsNone(calendar_block(cal, self.now, self.c))
        cal["fetched_at"] = iso(self.now - timedelta(hours=1))
        self.assertIsNotNone(calendar_block(cal, self.now, self.c))

    def test_daily_in_progress_label_excluded(self):
        values = self.weekday_daily(221)
        bars = parse_bars(values, "1day", self.now)
        self.assertEqual(len(bars), 220)
        self.assertEqual(stamp(bars[-1]["time"]).date(), self.now.date() - timedelta(days=1))
        later = parse_bars(values, "1day", stamp("2026-10-06T21:00:00Z"))
        self.assertEqual(later[-1]["time"][:10], "2026-10-06")

    def test_incomplete_intraday_excluded(self):
        now = stamp("2026-10-08T12:00:00Z")
        values = [{"datetime": (now - timedelta(minutes=15*i)).strftime("%Y-%m-%d %H:%M:%S"),
                   "open": 100, "high": 103, "low": 99, "close": 101} for i in range(229)]
        bars = parse_bars(values, "15min", now + timedelta(minutes=1))
        self.assertEqual(len(bars), 228 - 8)
        self.assertEqual(stamp(bars[-1]["time"]), now - timedelta(minutes=15))

    def test_dropped_signal_can_rearm_uncertain_cannot(self):
        with app_test_directory(self) as folder:
            a = self.app(folder)
            self.assertTrue(a.emit("s1:signal", "x", sid=self.sid))
            self.assertFalse(a.emit("s1:signal", "x", sid=self.sid))
            a.delivery_results.put(("s1:signal", "DROPPED_STALE", {"plan": False, "report": None}))
            a.drain_delivery()
            self.assertFalse(a.emit("s1:signal", "x", sid=self.sid))
            rearm = a.journal.get("rearm:s1:signal"); rearm["after"] = 0
            a.journal.put("rearm:s1:signal", rearm)
            self.assertTrue(a.emit("s1:signal", "x", sid=self.sid))
            self.assertEqual(a.journal.db.execute("SELECT count(*) FROM events WHERE id LIKE 's1:signal:released:%'").fetchone()[0], 1)
            a.delivery_results.put(("s1:signal", "DELIVERY_UNCERTAIN", {"plan": False, "report": None}))
            a.drain_delivery()
            a.journal.put("rearm:s1:signal", {"count": 1, "after": 0})
            self.assertFalse(a.emit("s1:signal", "x", sid=self.sid))

    def test_reviewer_failed_watch_is_resent_while_valid(self):
        with app_test_directory(self) as folder:
            a = self.app(folder); a.c = dict(a.c, rearm_cooldown_seconds=0.01)
            self.arm()
            key = self.sid + ":watch"
            self.assertTrue(a.emit(key, "WATCH text", sid=self.sid))
            key0, packet = a.outgoing.get_nowait()
            a.delivery_results.put((key, "DELIVERY_FAILED", packet))
            a.drain_delivery(); time.sleep(.6)
            with patch.object(sys_module(), "utcnow", return_value=self.now + timedelta(minutes=50)):
                a.process_retries()
            self.assertTrue(a.outgoing.empty())
            self.assertEqual(a.journal.get("rearm:" + key)["count"], 1)
            a.retries = [(0, k, p) for _, k, p in a.retries]
            a.delivery_gate = {"fresh": True, "valid": {self.sid}}
            with patch.object(sys_module(), "utcnow", return_value=self.now + timedelta(minutes=50)):
                a.process_retries()
            key1, again = a.outgoing.get_nowait()
            self.assertEqual((key1, again["text"]), (key, "WATCH text"))
            self.assertFalse(a.emit(key, "WATCH text", sid=self.sid))
            a.delivery_results.put((key, "DELIVERY_UNCERTAIN", again)); a.drain_delivery(); time.sleep(.6)
            a.process_retries()
            self.assertTrue(a.outgoing.empty())

    def test_failed_watch_not_resent_after_invalidation_or_new_epoch(self):
        with app_test_directory(self) as folder:
            a = self.app(folder); a.c = dict(a.c, rearm_cooldown_seconds=0.01)
            self.arm(); key = self.sid + ":watch"
            a.emit(key, "W", sid=self.sid); _, packet = a.outgoing.get_nowait()
            a.delivery_results.put((key, "DROPPED_STALE", packet)); a.drain_delivery()
            self.w.setups[self.sid]["state"] = "INVALIDATED"; time.sleep(.6)
            a.process_retries(); self.assertTrue(a.outgoing.empty())
            a.emit("t:invalidated", "terminal"); _, packet = a.outgoing.get_nowait()
            a.delivery_results.put(("t:invalidated", "DROPPED_STALE", packet)); a.drain_delivery()
            a.epoch = "new-session"; time.sleep(.6)
            a.process_retries(); self.assertTrue(a.outgoing.empty())

    def test_reviewer_displayed_risk_matches_current_news(self):
        with app_test_directory(self) as folder:
            a = self.app(folder)
            self.arm(); self.push(60)
            now = self.now + timedelta(minutes=60, seconds=15)
            self.fresh_history(a, now)
            old = {"risk": "LOW", "checked_at": iso(now), "summary": "calm", "facts": [], "opinions": []}
            a.news = dict(old, risk="MEDIUM", summary="CPI tension")
            a.calendar["fetched_at"] = iso(now)
            review = {"decision": "APPROVE", "risk": "LOW", "criteria": {k: True for k in CRITERIA}, "reason": "t", "grade": "A_TRADE"}
            a.reviewed[self.sid] = {"review": review, "news": old, "until": time.time() + 60, "setup_risk": "LOW",
                                    "plan": plan_fingerprint(self.w.setups[self.sid])}
            with patch.object(a, "data_enabled", return_value=True):
                a.update_gate(now)
            _, review_out, news_out = a.delivery_gate["plans"][self.sid]
            self.assertEqual(review_out["grade"], "B_TRADE")
            text = plan_text(self.w.setups[self.sid], 102.5, review_out, news_out)
            self.assertIn("B_TRADE", text); self.assertIn("MEDIUM", text); self.assertIn("CPI tension", text)
            self.assertNotIn("A_TRADE", text)

    def test_rearm_cap(self):
        with app_test_directory(self) as folder:
            a = self.app(folder); a.c = dict(a.c, max_rearms_per_event=1, rearm_cooldown_seconds=0.001)
            for expected in (True, False):
                a.emit("k", "x"); a.journal.status("k", "DROPPED_STALE")
                self.assertEqual(a.journal.release("k", 0, a.c["max_rearms_per_event"]), expected)

    def test_delivery_status_classification(self):
        import requests
        def resp(code, body=None, bad_json=False):
            r = Mock(status_code=code)
            r.json.side_effect = ValueError if bad_json else None
            r.json.return_value = body or {}
            return r
        cases = [(resp(200, {"ok": True}), "SENT"), (resp(400), "DELIVERY_FAILED"),
                 (resp(429), "DELIVERY_FAILED"), (resp(502), "DELIVERY_UNCERTAIN"),
                 (resp(200, bad_json=True), "DELIVERY_UNCERTAIN"), (resp(200, {"ok": False}), "DELIVERY_UNCERTAIN")]
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}):
            for response, expected in cases:
                with patch.object(requests, "post", return_value=response):
                    self.assertEqual(delivery_status(requests, "x"), expected)
            with patch.object(requests, "post", side_effect=TimeoutError):
                self.assertEqual(delivery_status(requests, "x"), "DELIVERY_UNCERTAIN")

    def test_durable_event_deduplication(self):
        with app_test_directory(self) as folder:
            j = Journal(Path(folder))
            self.assertTrue(j.claim("same-event", {})); j.db.close()
            j = Journal(Path(folder))
            self.assertFalse(j.claim("same-event", {})); j.db.close()

    def fresh_history(self, a, now):
        for tf in a.history:
            bars = a.history[tf]
            last = stamp(bars[-1]["time"])
            while last + timedelta(seconds=2 * TF[tf]) <= now:
                last += timedelta(seconds=TF[tf])
                bars.append(dict(bars[-1], time=iso(last)))

    def app(self, folder, c=None):
        with patch.object(threading.Thread, "start"):
            a = App(dict(c or self.c, state_directory=folder))
        a.active = True; a.warmed = True; a.epoch = "test"
        a.watcher = self.w; a.history = copy.deepcopy(self.history)
        feed = QuoteFeed(a.c); feed.record(102.5, self.now, now=self.now)
        a.feed = feed
        a.calendar = {"fetched_at": iso(self.now), "events": []}
        a.evidence = copy.deepcopy(FIX_EV)
        # Always release SQLite before the surrounding temp directory exits.
        self._temporary_apps = getattr(self, "_temporary_apps", {})
        self._temporary_apps.setdefault(str(Path(folder).resolve()), []).append(a)
        # Backup cleanup for callers not using app_test_directory.
        self.addCleanup(a.pool.shutdown, wait=False, cancel_futures=True)
        self.addCleanup(a.journal.db.close)
        return a

    def test_closed_market_stops_work(self):
        with app_test_directory(self) as folder:
            a = self.app(folder)
            a.analyst = Mock()
            a.tick(stamp("2026-10-10T12:00:00Z"))
            a.analyst.stop.assert_called_once()
            self.assertFalse(a.active)
            self.assertEqual(a.data_jobs, {})
            self.assertEqual(a.delivery_gate, {})
            self.assertIsNone(a.evidence)

    def test_stale_feed_discards_old_map(self):
        with app_test_directory(self) as folder:
            a = self.app(folder); a.analyst = Mock()
            a.feed.latest["time"] = iso(self.now - timedelta(minutes=4))
            a.tick(self.now)
            self.assertFalse(a.warmed)
            self.assertIsNone(a.watcher.map)
            self.assertEqual(a.delivery_gate, {})
            self.assertEqual(a.next_map, 0)
            self.assertIsNone(a.evidence)

    def test_intraday_worker_result_shape(self):
        with app_test_directory(self) as folder:
            a = self.app(folder)
            f = futures.Future(); f.set_result({tf: self.history[tf] for tf in ("15min", "1h", "4h")})
            a.data_jobs["intraday"] = (f, "test")
            with patch.object(a, "data_enabled", return_value=True):
                a.consume_data(self.now)
            self.assertIsInstance(a.history["15min"], list)
            self.assertEqual(a.data_jobs, {})

    def test_ai_timeout_terminates_locally(self):
        a = Analyst(dict(self.c, ai_deadline_seconds=1))
        p = Mock(); p.is_alive.return_value = True
        pipe = Mock(); pipe.poll.return_value = False
        a.job = {"process": p, "pipe": pipe, "start": time.monotonic()-5, "logged": 0, "kind": "map"}
        result = a.poll()
        self.assertEqual(result[1], "error")
        p.terminate.assert_called_once()
        self.assertIsNone(a.job)

    def test_background_analysis_does_not_block_watcher(self):
        a = Analyst(self.c)
        p = Mock(); p.is_alive.return_value = True
        pipe = Mock(); pipe.poll.return_value = False
        a.job = {"process": p, "pipe": pipe, "start": time.monotonic(), "logged": 0, "kind": "map"}
        self.assertIsNone(a.poll())
        self.assertEqual(self.arm()[0][0], "WATCH")

    def test_late_review_cannot_revive_invalidated_setup(self):
        with app_test_directory(self) as folder:
            a = self.app(folder); self.w.quote(98, self.now)
            a.analyst = Mock()
            a.analyst.poll.return_value = ({"kind": "review", "tag": {"sid": self.sid, "epoch": "test", "purpose": "READY"}}, "result",
                {"technical": {"decision": "APPROVE", "risk": "LOW", "reason": "test"},
                 "news": {"risk": "LOW", "checked_at": iso(self.now), "facts": [], "opinions": [], "summary": "test"}})
            a.consume_ai(self.now)
            self.assertEqual(a.reviewed, {})
            self.assertEqual(self.w.setups[self.sid]["state"], "INVALIDATED")

    def test_unknown_news_prevents_signal(self):
        with app_test_directory(self) as folder:
            a = self.app(folder)
            self.arm(); self.push(60)
            now = self.now + timedelta(minutes=60, seconds=15)
            self.fresh_history(a, now)
            news = {"risk": "UNKNOWN", "checked_at": iso(now), "summary": "missing"}
            a.news = news; a.calendar["fetched_at"] = iso(now)
            a.reviewed[self.sid] = {"review": {"risk": "LOW", "reason": "test"}, "news": news, "until": time.time()+60}
            with patch.object(a, "data_enabled", return_value=True):
                a.update_gate(now)
            self.assertEqual(a.delivery_gate["actionable"], set())

    def test_approved_watch_authorizes_later_trigger_without_second_call(self):
        with app_test_directory(self) as folder:
            a = self.app(folder)
            self.arm(); self.push(60)
            now = self.now + timedelta(minutes=60, seconds=15)
            self.fresh_history(a, now)
            news = {"risk": "LOW", "checked_at": iso(now), "summary": "test", "facts": [], "opinions": []}
            a.news = news; a.calendar["fetched_at"] = iso(now)
            a.reviewed[self.sid] = {"review": {"decision": "APPROVE", "risk": "LOW", "reason": "test",
                                               "criteria": {k: True for k in CRITERIA}},
                                   "news": news, "until": time.time()+60, "setup_risk": "LOW",
                                   "plan": plan_fingerprint(self.w.setups[self.sid])}
            a.pending[self.sid] = "READY"
            a.analyst = Mock(job=None)
            a.full_ready = False
            a.dispatch_ai(now)
            a.analyst.start.assert_not_called()
            with patch.object(a, "data_enabled", return_value=True):
                a.update_gate(now)
            self.assertIn(self.sid, a.delivery_gate["actionable"])
            self.assertEqual(a.outgoing.qsize(), 1)
            with patch.object(a, "data_enabled", return_value=True):
                a.update_gate(now)
            self.assertEqual(a.outgoing.qsize(), 1)

    def test_calendar_blocks_preapproved_plan(self):
        with app_test_directory(self) as folder:
            a = self.app(folder)
            self.arm(); self.push(60)
            now = self.now + timedelta(minutes=60, seconds=15)
            self.fresh_history(a, now)
            news = {"risk": "LOW", "checked_at": iso(now), "summary": "test"}
            a.news = news
            a.calendar = {"fetched_at": iso(now), "events": [{"time": iso(now), "title": "FOMC", "impact": "High"}]}
            a.reviewed[self.sid] = {"review": {"risk": "LOW", "reason": "test"}, "news": news, "until": time.time()+60}
            with patch.object(a, "data_enabled", return_value=True):
                a.update_gate(now)
            self.assertFalse(a.delivery_gate["actionable"])
            self.assertTrue(a.outgoing.empty())

    def test_sell_geometry_and_conditions(self):
        d = copy.deepcopy(self.data)
        d["levels"][0].update(kind="RESISTANCE", low=104., high=105.)
        d["setups"][0].update(direction="SELL", entry_low=102., entry_high=103., stop_loss=106.,
                              invalidation=105.5, targets=[90., 85.], evidence_ids=list(SELL_IDS),
                              conditions=[{"type": "CLOSE_BELOW", "timeframe": "15min", "level": 103.5}])
        validate_map(d, self.history, self.c)
        w = Watcher(self.c, "sell-test"); w.install(d, self.history, self.now, self.now)
        sid = next(iter(w.setups))
        for minutes in (15, 30, 45):
            b = self.bar(minutes, high=105., low=103., open=104., close=104.)
            ev = w.bars("15min", [b], self.now+timedelta(minutes=minutes))
        self.assertEqual(ev[0][0], "WATCH")
        w.bars("15min", [self.bar(60, high=104, low=102, open=104, close=102.5)], self.now+timedelta(minutes=60))
        self.assertTrue(w.actionable(sid, 102.5, self.now+timedelta(minutes=60)))
        self.assertEqual(w.quote(106., self.now+timedelta(minutes=61))[0][0], "INVALIDATED")

    def test_emergency_structure_and_volatility(self):
        z = {"id": "h4support", "kind": "SUPPORT", "timeframe": "4h", "low": 100., "high": 101., "evidence_times": [], "reason": "fixture"}
        self.w.map["levels"].append(z)
        b = dict(self.history["4h"][-1], time=iso(self.now), close=99.)
        self.assertIn("broken", self.w.structural_event("4h", self.history["4h"], [b]))
        b = self.bar(15, high=120, low=90)
        self.assertIn("Abnormal", self.w.structural_event("15min", self.history["15min"], [b]))

    def run_worker(self, kind, technical, news):
        pipe = Mock()
        with patch.dict(sys.modules, {"openai": Mock()}), patch.object(M, "response_json", side_effect=technical), \
             patch.object(M, "news_check", side_effect=news) as news_mock:
            ai_worker(kind, {}, self.c, {"model": "offline", "instructions": ""}, pipe)
        sent = [call.args[0] for call in pipe.send.call_args_list]
        return [x[0] for x in sent], sent[-1][1], news_mock

    def test_review_runs_news_and_technical_concurrently(self):
        barrier = threading.Barrier(2, timeout=3)
        def technical(*args, **kwargs):
            barrier.wait(); return {"decision": "WATCH"}
        def news(*args, **kwargs):
            barrier.wait(); return {"risk": "LOW", "summary": "test"}
        kinds, result, _ = self.run_worker("review", technical, news)
        self.assertEqual(kinds, ["progress", "result"])
        self.assertEqual(result["news"]["risk"], "LOW")

    def test_map_without_candidates_skips_news(self):
        empty = dict(copy.deepcopy(self.data), setups=[])
        kinds, result, news = self.run_worker("map", lambda *a, **k: empty, lambda *a, **k: {"risk": "LOW"})
        news.assert_not_called()
        self.assertIsNone(result["news"])
        high = copy.deepcopy(self.data); high["setups"][0]["risk"] = "HIGH"
        _, _, news = self.run_worker("map", lambda *a, **k: high, lambda *a, **k: {"risk": "LOW"})
        news.assert_not_called()

    def test_map_with_candidate_still_skips_news_until_review(self):
        kinds, result, news = self.run_worker("map", lambda *a, **k: copy.deepcopy(self.data), lambda *a, **k: {"risk": "LOW"})
        news.assert_not_called()
        self.assertEqual(kinds, ["progress", "result"])
        self.assertIsNone(result["news"])

    def parsed_history(self, now, last_daily):
        S = Sessions(self.c)
        def intraday(tf, n):
            end = now - timedelta(seconds=(now.timestamp() % TF[tf]))
            vals = [{"datetime": (end - timedelta(seconds=TF[tf]*i)).strftime("%Y-%m-%d %H:%M:%S"),
                     "open": 100, "high": 101, "low": 99, "close": 100} for i in range(n, -1, -1)]
            return parse_bars(vals, tf, now, sessions=S)
        monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        weekly = [{"datetime": (monday - timedelta(weeks=i)).strftime("%Y-%m-%d"),
                   "open": 100, "high": 101, "low": 99, "close": 100} for i in range(230, -1, -1)]
        return {"15min": intraday("15min", 700), "1h": intraday("1h", 700), "4h": intraday("4h", 700),
                "1day": parse_bars(self.weekday_daily(240, end=stamp(last_daily + "T12:00:00Z")), "1day", now, sessions=S),
                "1week": parse_bars(weekly, "1week", now, sessions=S)}

    def test_reviewer_history_fresh_after_daily_reopen_and_sunday(self):
        S = Sessions(self.c)
        for now, daily in (("2026-10-13T22:30:00Z", "2026-10-13"), ("2026-11-10T23:30:00Z", "2026-11-10"),
                           ("2026-10-11T22:30:00Z", "2026-10-09"), ("2026-10-12T09:00:00Z", "2026-10-12"),
                           ("2026-10-14T12:00:00Z", "2026-10-14")):
            n = stamp(now)
            h = self.parsed_history(n, daily)
            self.assertTrue(history_fresh(h, n, S), now)
        n = stamp("2026-10-13T22:30:00Z")
        h = self.parsed_history(n, "2026-10-13")
        self.assertEqual(h["1day"][-1]["time"][:10], "2026-10-13")
        self.assertFalse(history_fresh(h, n + timedelta(hours=3), S))
        self.assertFalse(history_fresh(h, n - timedelta(hours=2), S))

    def test_map_result_without_news_keeps_previous_news(self):
        with app_test_directory(self) as folder:
            a = self.app(folder); a.news = {"risk": "LOW", "checked_at": iso(self.now)}
            a.analyst = Mock()
            a.analyst.poll.return_value = ({"kind": "map", "tag": {"epoch": "test", "history": self.history, "born": iso(self.now)}},
                                           "result", {"technical": dict(copy.deepcopy(self.data), setups=[]), "news": None})
            with patch.object(sys_module(), "history_fresh", return_value=True):
                a.consume_ai(self.now)
            self.assertEqual(a.news["risk"], "LOW")

    def test_trade_grades(self):
        ok = {k: True for k in CRITERIA}
        review = {"decision": "APPROVE", "risk": "LOW", "criteria": ok, "reason": "t"}
        self.assertEqual(trade_grade(review, "LOW"), "A_TRADE")
        self.assertEqual(trade_grade(review, "MEDIUM"), "B_TRADE")
        self.assertEqual(trade_grade(review, "LOW", "LOW", mtf_ok=False, mtf_veto=False), "B_TRADE")
        self.assertIsNone(trade_grade(review, "LOW", "LOW", mtf_ok=False))
        self.assertEqual(trade_grade(dict(review, risk="MEDIUM"), "LOW"), "B_TRADE")
        self.assertEqual(trade_grade(dict(review, criteria=dict(ok, mtf_agreement=False)), "LOW"), "B_TRADE")
        self.assertEqual(trade_grade(dict(review, criteria=dict(ok, clear_structure=False)), "LOW"), "B_TRADE")
        for missing in ("valid_trigger", "defensible_stop", "logical_targets"):
            self.assertIsNone(trade_grade(dict(review, criteria=dict(ok, **{missing: False})), "LOW"))
        self.assertIsNone(trade_grade(review, "UNKNOWN"))
        self.assertIsNone(trade_grade(review, "HIGH"))
        self.assertIsNone(trade_grade(dict(review, risk="HIGH"), "LOW"))
        self.assertIsNone(trade_grade(dict(review, decision="WATCH"), "LOW"))
        strict_validate(review, REVIEW_SCHEMA)

    def review_result(self, criteria):
        return ({"kind": "review", "tag": {"sid": self.sid, "epoch": "test", "purpose": "WATCH",
                                           "plan": plan_fingerprint(self.w.setups[self.sid])}}, "result",
                {"technical": {"decision": "APPROVE", "risk": "LOW", "criteria": criteria, "reason": "t"},
                 "news": {"risk": "LOW", "checked_at": iso(self.now), "facts": [], "opinions": [], "summary": "t"}})

    def test_approval_without_required_criteria_not_authorized(self):
        with app_test_directory(self) as folder:
            a = self.app(folder); a.analyst = Mock()
            a.analyst.poll.return_value = self.review_result(dict({k: True for k in CRITERIA}, defensible_stop=False))
            a.consume_ai(self.now)
            self.assertEqual(a.reviewed, {})
            a.analyst.poll.return_value = self.review_result({k: True for k in CRITERIA})
            a.consume_ai(self.now)
            self.assertEqual(a.reviewed[self.sid]["review"]["grade"], "A_TRADE")
            self.assertIn("A_TRADE", plan_text(self.w.setups[self.sid], 102.5, a.reviewed[self.sid]["review"], a.news))

    def test_report_sent_without_news(self):
        with app_test_directory(self) as folder:
            a = self.app(folder); a.news = None
            now = stamp("2026-10-06T09:00:00Z")
            with patch.object(sys_module(), "history_fresh", return_value=True), patch.object(a.watcher, "live", return_value=True):
                a.reports(now)
            self.assertEqual(a.outgoing.qsize(), 2)

    def visits_watcher(self, exit_bars=1):
        c = dict(self.c, touch_mode="visits", touch_exit_bars=exit_bars, map_max_age_seconds=10800)
        d = copy.deepcopy(self.data); d["setups"][0]["expires_minutes"] = 180
        w = Watcher(c, "visits"); w.install(d, self.history, self.now, self.now)
        return w, next(iter(w.setups))

    def push_to(self, w, minutes, touch):
        changes = {} if touch else dict(low=102.2, high=103, open=102.5, close=102.8)
        return w.bars("15min", [self.bar(minutes, **changes)], self.now + timedelta(minutes=minutes, seconds=12))

    def test_visits_mode_ignores_consecutive_contacts(self):
        w, sid = self.visits_watcher()
        for m in (15, 30, 45, 60):
            self.assertEqual(self.push_to(w, m, True), [])
        self.assertEqual(w.setups[sid]["visits"], 1)

    def test_visits_mode_requires_exit_and_reentry(self):
        w, sid = self.visits_watcher()
        events = [self.push_to(w, m, t) for m, t in ((15, True), (30, False), (45, True), (60, False), (75, True))]
        self.assertEqual([e[0][0] for e in events if e], ["WATCH"])
        self.assertEqual(len(w.setups[sid]["visit_starts"]), 3)

    def test_visits_mode_exit_bars(self):
        w, sid = self.visits_watcher(exit_bars=2)
        for m, t in ((15, True), (30, False), (45, True)):
            self.push_to(w, m, t)
        self.assertEqual(w.setups[sid]["visits"], 1)
        for m, t in ((60, False), (75, False), (90, True)):
            self.push_to(w, m, t)
        self.assertEqual(w.setups[sid]["visits"], 2)

    def test_visits_config_validation(self):
        with app_test_directory(self) as folder:
            path = Path(folder, "c.json")
            path.write_text(json.dumps({"touch_mode": "visits", "touch_exit_bars": 8, "touch_candles": 3}))
            with self.assertRaises(ValueError):
                load_config(path)
            path.write_text(json.dumps({"touch_mode": "visits", "touch_exit_bars": 1}))
            self.assertEqual(load_config(path)["touch_mode"], "visits")
        d = copy.deepcopy(self.data); d["setups"][0]["expires_minutes"] = 75
        with self.assertRaises(ValueError):
            validate_map(d, self.history, dict(self.c, touch_mode="visits"))

    def test_telegram_delivery_guard_drops_expired_packet_without_http(self):
        import requests
        with app_test_directory(self) as folder:
            a = self.app(folder)
            packet = {"text": "test", "epoch": "test", "sid": self.sid, "plan": True,
                      "expires": time.time()-1, "report": None}
            a.outgoing.put(("expired", packet))
            original_put = a.delivery_results.put
            def finish(value):
                original_put(value); a.stop_sender.set()
            with patch.object(a.delivery_results, "put", side_effect=finish), patch.object(requests, "post") as post:
                a.send_loop()
            post.assert_not_called()
            self.assertEqual(a.delivery_results.get()[1], "DROPPED_STALE")

    def test_sender_rechecks_new_quote_before_http(self):
        import requests
        with app_test_directory(self) as folder:
            a = self.app(folder)
            news = {"risk": "LOW", "summary": "test"}
            review = {"risk": "LOW", "reason": "test"}
            a.delivery_gate = {"epoch": "test", "fresh": True, "valid": {self.sid},
                               "actionable": {self.sid}, "price": 102.5,
                               "plans": {self.sid: (self.w.setups[self.sid], review, news)}}
            a.feed.latest["price"] = 110.
            a.outgoing.put(("stale-price", {"text": "test", "epoch": "test", "sid": self.sid,
                "plan": True, "expires": time.time()+60, "report": None}))
            original_put = a.delivery_results.put
            def finish(value):
                original_put(value); a.stop_sender.set()
            with patch.object(M, "utcnow", return_value=self.now), patch.object(a, "quote_fresh", return_value=True), \
                 patch.object(a.delivery_results, "put", side_effect=finish), patch.object(requests, "post") as post:
                a.send_loop()
            post.assert_not_called()
            self.assertEqual(a.delivery_results.get()[1], "DROPPED_STALE")

    def test_bootstrap_context_is_dated_and_bounded(self):
        with app_test_directory(self) as folder:
            data = {"symbol": "XAU/USD", "initialized_at": iso(self.now), "model": "offline",
                    "analysis": {"market_stance": "CONFLICT", "important_levels": list(range(30))}}
            Path(folder, "gold_bootstrap_context.json").write_text(json.dumps(data))
            seed = load_bootstrap(folder, self.now)
            self.assertTrue(seed["historical_only"])
            self.assertEqual(len(seed["analysis"]["important_levels"]), 12)
            self.assertIsNone(load_bootstrap(folder, self.now + timedelta(days=8)))
            self.assertIsNone(load_bootstrap(folder, self.now - timedelta(hours=1)))

    def test_bootstrap_database_is_read_only_fallback(self):
        with app_test_directory(self) as folder:
            path = Path(folder, "gold_memory.db")
            with closing(sqlite3.connect(path)) as conn:
                with conn:
                    conn.execute("CREATE TABLE bootstrap_analysis(id INTEGER PRIMARY KEY, analysis_json TEXT)")
                    conn.execute("INSERT INTO bootstrap_analysis VALUES(1,?)", (json.dumps({"symbol": "XAU/USD",
                        "initialized_at": iso(self.now), "analysis": {"market_stance": "WAIT"}}),))
            before = path.read_bytes()
            seed = load_bootstrap(folder, self.now)
            self.assertEqual(seed["source"], "gold_memory.db")
            self.assertEqual(path.read_bytes(), before)

    def test_bootstrap_reader_closes_sqlite_connection(self):
        """The bootstrap reader must not retain gold_memory.db on Windows."""
        with app_test_directory(self) as folder:
            path = Path(folder, "gold_memory.db")
            with closing(sqlite3.connect(path)) as writer:
                with writer:
                    writer.execute("CREATE TABLE bootstrap_analysis(id INTEGER PRIMARY KEY, analysis_json TEXT)")
                    writer.execute("INSERT INTO bootstrap_analysis VALUES(1,?)", (
                        json.dumps({"symbol": "XAU/USD", "initialized_at": iso(self.now),
                                    "analysis": {"market_stance": "WAIT"}}),))

            original_connect = sqlite3.connect
            opened = []

            def track_read_connection(*args, **kwargs):
                conn = original_connect(*args, **kwargs)
                if kwargs.get("uri"):
                    opened.append(conn)
                return conn

            with patch.object(M.sqlite3, "connect", side_effect=track_read_connection):
                seed = load_bootstrap(folder, self.now)
            self.assertEqual(seed["source"], "gold_memory.db")
            self.assertEqual(len(opened), 1)
            # SQLite connections kept in `opened` are still referenced: only an
            # explicit close() makes this raise. A transaction context alone won't.
            with self.assertRaises(sqlite3.ProgrammingError):
                opened[0].execute("SELECT 1")

    def test_daily_request_omits_timezone_and_retains_closed_history(self):
        import requests
        with app_test_directory(self) as folder:
            source = DataSource(dict(self.c, state_directory=folder, rest_spacing_seconds=.001), lambda: True)
            values = self.weekday_daily(220)
            response = Mock()
            response.json.return_value = {"meta": {"symbol": "XAU/USD", "interval": "1day"}, "values": values}
            with patch.dict(os.environ, {"TWELVE_DATA_API_KEY": "offline-key"}), patch.object(requests, "get", return_value=response) as get:
                bars = source.candles("1day")
            self.assertNotIn("timezone", get.call_args.kwargs["params"])
            self.assertEqual(get.call_args.kwargs["params"]["outputsize"], OUTPUTSIZE["1day"])
            self.assertEqual(len(bars), 220)

    def test_candle_failure_writes_specific_diagnostic(self):
        import requests
        with app_test_directory(self) as folder:
            source = DataSource(dict(self.c, state_directory=folder), lambda: True)
            response = Mock()
            response.json.return_value = {"meta": {"symbol": "XAU/USD", "interval": "1day"},
                "values": [{"datetime": "2026-10-01", "open": 100, "high": 103, "low": 99, "close": 105}]}
            with patch.dict(os.environ, {"TWELVE_DATA_API_KEY": "offline-key"}), patch.object(requests, "get", return_value=response):
                with self.assertRaisesRegex(ValueError, "malformed OHLC"):
                    source.candles("1day")
            diagnostic = json.loads(Path(folder, "candle_error_1day.json").read_text())
            self.assertIn("O=100", diagnostic["error"])
            self.assertNotIn("offline-key", json.dumps(diagnostic))

    def test_insufficient_history_explains_counts(self):
        vals = [{"datetime": "2026-10-01", "open": 100, "high": 103, "low": 99, "close": 101}]
        with self.assertRaisesRegex(ValueError, "received=1, closed=1, required=210"):
            parse_bars(vals, "1day", self.now)

    def test_error_redacts_credentials(self):
        with patch.dict(os.environ, {"TWELVE_DATA_API_KEY": "super-secret-key"}):
            error = safe_error("https://example.test?apikey=super-secret-key&interval=1day")
        self.assertNotIn("super-secret-key", error)
        self.assertIn("interval=1day", error)

    def test_bootstrap_only_in_initial_map_payload(self):
        with app_test_directory(self) as folder:
            a = self.app(folder)
            a.bootstrap = {"historical_only": True}; a.bootstrap_consumed = False
            a.full_ready = True; a.next_map = 0; a.analyst = Mock(job=None)
            with patch.object(sys_module(), "history_fresh", return_value=True):
                a.dispatch_ai(self.now)
                self.assertIn("bootstrap_context", a.analyst.start.call_args.args[1])
                a.bootstrap_consumed = True; a.full_ready = True; a.next_map = 0
                a.dispatch_ai(self.now)
            self.assertNotIn("bootstrap_context", a.analyst.start.call_args.args[1])

    def weekday_daily(self, count, end=None):
        day, out = (end or self.now).date(), []
        while len(out) < count:
            if day.weekday() < 5:
                out.append({"datetime": day.isoformat(), "open": 100., "high": 104., "low": 99., "close": 102.})
            day -= timedelta(days=1)
        return out[::-1]

    def test_weekend_conflicting_daily_duplicate_is_dropped(self):
        values = self.weekday_daily(230)
        values += [{"datetime": "2026-10-03", "open": 4140.47, "high": 4143.473, "low": 4137.189, "close": 4137.611},
                   {"datetime": "2026-10-04", "open": 4137.535, "high": 4137.724, "low": 4137.473, "close": 4137.511},
                   {"datetime": "2026-10-04", "open": 4137.549, "high": 4148.517, "low": 4130.368, "close": 4142.543}]
        values.sort(key=lambda v: v["datetime"])
        bars = parse_bars(values, "1day", self.now, sessions=Sessions(self.c))
        days = {stamp(b["time"]).date().weekday() for b in bars}
        self.assertTrue(days <= {0, 1, 2, 3, 4})
        self.assertNotIn("2026-10-04", {b["time"][:10] for b in bars})
        self.assertIn("2026-10-05", {b["time"][:10] for b in bars})

    def test_weekday_conflicting_duplicate_still_fails_closed(self):
        values = self.weekday_daily(230)
        dup = dict(values[-3]); dup["close"] = 103.
        values.append(dup)
        with self.assertRaises(CandleConflictError):
            parse_bars(values, "1day", self.now, sessions=Sessions(self.c))

    def test_fully_closed_daily_period_dropped(self):
        values = self.weekday_daily(230)
        closed = [["2026-09-29T21:00:00Z", "2026-09-30T21:00:00Z"]]
        bars = parse_bars(values, "1day", self.now, sessions=Sessions(dict(self.c, closed_intervals_utc=closed)))
        self.assertNotIn("2026-09-30", {b["time"][:10] for b in bars})
        self.assertIn("2026-09-29", {b["time"][:10] for b in bars})

    def test_reviewer_friday_kept_after_sunday_reopen(self):
        values = self.weekday_daily(230, end=stamp("2026-10-09T12:00:00Z"))
        for now in ("2026-10-11T22:30:00Z", "2026-10-11T23:05:00Z"):
            bars = parse_bars(values, "1day", stamp(now), sessions=Sessions(self.c))
            self.assertEqual(bars[-1]["time"][:10], "2026-10-09")
        bars = parse_bars(values, "1day", stamp("2026-10-09T21:00:00Z"))
        self.assertEqual(bars[-1]["time"][:10], "2026-10-09")
        bars = parse_bars(values, "1day", stamp("2026-10-09T20:59:00Z"))
        self.assertEqual(bars[-1]["time"][:10], "2026-10-08")

    def test_reviewer_malformed_sunday_filler_does_not_reject_history(self):
        values = self.weekday_daily(230)
        values += [{"datetime": "2026-10-04", "open": 4137.5, "high": 4130.0, "low": 4140.0, "close": 4137.5},
                   {"datetime": "2026-10-03", "open": "nan", "high": None, "low": 1, "close": 1},
                   {"datetime": "2026-09-27"}]
        values.sort(key=lambda v: v["datetime"])
        bars = parse_bars(values, "1day", self.now)
        self.assertNotIn("2026-10-04", {b["time"][:10] for b in bars})
        values.append({"datetime": "2026-10-02", "open": 100, "high": 99, "low": 98, "close": 100})
        with self.assertRaisesRegex(ValueError, "malformed OHLC"):
            parse_bars(values, "1day", self.now)

    def test_sunday_label_classified_from_its_period(self):
        s = Sessions(self.c)
        sunday, monday = datetime(2026, 10, 4), datetime(2026, 10, 5)
        self.assertTrue(closed_session_bar("1day", sunday, stamp("2026-10-04T00:00:00Z"), s))
        self.assertTrue(closed_session_bar("1day", datetime(2026, 10, 3), stamp("2026-10-03T00:00:00Z"), s))
        self.assertFalse(closed_session_bar("1day", monday, stamp("2026-10-05T00:00:00Z"), s))
        a, b = bar_period("1day", monday, None, s)
        self.assertEqual((iso(a), iso(b)), ("2026-10-04T21:00:00+00:00", "2026-10-05T21:00:00+00:00"))
        a, b = bar_period("1day", datetime(2026, 11, 9), None, s)
        self.assertEqual(iso(b), "2026-11-09T22:00:00+00:00")

    def test_sunday_open_keeps_friday_daily(self):
        sunday = stamp("2026-10-11T23:00:00Z")
        values = self.weekday_daily(230, end=sunday - timedelta(days=2))
        values += [{"datetime": "2026-10-10", "open": 100., "high": 100.2, "low": 99.9, "close": 100.},
                   {"datetime": "2026-10-11", "open": 100., "high": 101., "low": 99., "close": 100.5}]
        bars = parse_bars(values, "1day", sunday, sessions=Sessions(self.c))
        self.assertEqual(bars[-1]["time"][:10], "2026-10-09")

    def test_intraday_weekend_filler_dropped_open_bars_kept(self):
        now = stamp("2026-10-06T12:00:00Z")
        start = now - timedelta(hours=4 * 300)
        values = [{"datetime": (start + timedelta(hours=4*i)).strftime("%Y-%m-%d %H:%M:%S"),
                   "open": 100, "high": 101, "low": 99, "close": 100} for i in range(300)]
        bars = parse_bars(values, "4h", now, sessions=Sessions(self.c))
        sessions = Sessions(self.c)
        for b in bars:
            t = stamp(b["time"])
            self.assertTrue(sessions.is_open(t) or sessions.is_open(t + timedelta(seconds=TF["4h"] - 1)))
        self.assertNotIn("2026-10-03T12:00:00+00:00", {b["time"] for b in bars})
        self.assertIn("2026-10-04T20:00:00+00:00", {b["time"] for b in bars})

    def test_identical_daily_duplicates_are_collapsed(self):
        values = self.weekday_daily(220)
        duplicate = dict(values[-2]); duplicate["open"] = "100.000"
        values.insert(5, duplicate)
        result = parse_bars(values, "1day", self.now)
        self.assertEqual(len(result), 219)
        self.assertEqual(len({b["time"] for b in result}), 219)

    def test_duplicate_newest_is_not_closure_successor(self):
        values = self.weekday_daily(220)
        newest = values[-1]["datetime"]
        values.append(dict(values[-1]))
        result = parse_bars(values, "1day", self.now)
        self.assertNotIn(newest, {b["time"][:10] for b in result})
        self.assertEqual(len(result), 219)

    def test_conflicting_daily_duplicate_preserves_both_rows(self):
        values = self.weekday_daily(220)
        duplicate = dict(values[-2]); duplicate["close"] = 103
        values.append(duplicate)
        with self.assertRaises(CandleConflictError) as ctx:
            parse_bars(values, "1day", self.now)
        self.assertEqual(len(ctx.exception.duplicate_rows), 2)
        self.assertEqual({b["close"] for b in ctx.exception.duplicate_rows}, {102., 103.})

    def test_duplicate_weekly_bars_collapsed_before_closure(self):
        values = [{"datetime": b["time"], **{k: b[k] for k in ("open", "high", "low", "close")}}
                  for b in self.history["1week"]]
        values.append(dict(values[-1]))
        self.assertEqual(len(parse_bars(values, "1week", self.now)), 219)

    def test_conflict_diagnostic_contains_actual_duplicate_prices(self):
        import requests
        with app_test_directory(self) as folder:
            source = DataSource(dict(self.c, state_directory=folder), lambda: True)
            values = self.weekday_daily(220)
            duplicate = dict(values[-2]); duplicate["close"] = 103
            values.append(duplicate)
            response = Mock()
            response.json.return_value = {"meta": {"symbol": "XAU/USD", "interval": "1day"}, "values": values}
            with patch.dict(os.environ, {"TWELVE_DATA_API_KEY": "offline"}), patch.object(requests, "get", return_value=response):
                with self.assertRaisesRegex(ValueError, "conflicting OHLC"):
                    source.candles("1day")
            diagnostic = json.loads(Path(folder, "candle_error_1day.json").read_text())
            self.assertEqual(len(diagnostic["duplicate_rows"]), 2)

    # ------------------------------------------------------------------ V5: price-update / sender race
    def test_v5_unprocessed_print_through_invalidation_blocks_sender(self):
        with app_test_directory(self) as folder:
            a, now = self.queued_signal_app(folder)
            a.feed.record(98.0, now, now=now)            # after the gate snapshot, before the next tick
            a.feed.record(102.5, now, now=now)           # price is back in the entry range
            self.assertEqual(a.feed.latest["price"], 102.5)
            post = self.run_sender(a, now)
            post.assert_not_called()
            self.assertEqual(a.delivery_results.queue[-1][1], "DROPPED_STALE")
            self.assertEqual(self.w.setups[self.sid]["state"], "ARMED")       # sender never mutates strategy state
            self.assertEqual(a.feed.peek_extremes()["low"], 98.0)             # still pending for the main loop
            w = a.feed.take_extremes()
            events = self.w.quote(a.feed.latest["price"], now, w["low"], w["high"])
            self.assertEqual([e[0] for e in events], ["INVALIDATED"])

    def test_v5_sender_final_check_waits_for_main_loop_tick(self):
        import requests
        with app_test_directory(self) as folder:
            a, now = self.queued_signal_app(folder)
            key, packet = a.outgoing.get_nowait()
            with patch.object(M, "utcnow", return_value=now), patch.object(a, "quote_fresh", return_value=True), \
                 patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "offline", "TELEGRAM_CHAT_ID": "offline"}), \
                 patch.object(requests, "post") as post:
                post.return_value = Mock(status_code=200, json=Mock(return_value={"ok": True}))
                sender = threading.Thread(target=a.send_loop, daemon=True)
                with a.lock:                                     # the main loop is mid-tick
                    a.outgoing.put((key, packet))
                    sender.start()
                    time.sleep(.4)                               # sender holds the packet, waits for the lock
                    self.assertFalse(post.called)
                    self.w.setups[self.sid]["state"] = "INVALIDATED"   # the tick processes an invalidating print
                result = a.delivery_results.get(timeout=3)
                a.stop_sender.set(); sender.join(3)
            self.assertEqual(result[1], "DROPPED_STALE")
            post.assert_not_called()

    def test_v5_lock_released_during_http_and_inflight_invalidation_withdraws(self):
        with app_test_directory(self) as folder:
            a, now = self.queued_signal_app(folder)
            pending_events = []
            def http(*args, **kwargs):
                def tick_invalidates():            # another thread = the main loop's tick
                    with a.lock:
                        pending_events.extend(self.w.quote(98.0, now, 98.0, 102.5))
                t = threading.Thread(target=tick_invalidates); t.start(); t.join(3)
                self.assertFalse(t.is_alive(), "sender must not hold the strategy lock during HTTP")
                return Mock(status_code=200, json=Mock(return_value={"ok": True}))
            post = self.run_sender(a, now, http)
            self.assertTrue(post.called)
            self.assertEqual([e[0] for e in pending_events], ["INVALIDATED"])
            self.assertEqual(self.w.setups[self.sid].get("signal_inflight"), self.sid + ":signal")
            a.events(pending_events, now)          # rest of that tick (journal I/O stays on the main thread)
            a.drain_delivery()
            st = self.w.setups[self.sid]
            self.assertEqual(st["state"], "INVALIDATED")                # never overwritten to TRIGGERED
            self.assertNotIn("signal_inflight", st)
            keys = [k for k, _ in list(a.outgoing.queue)]
            self.assertIn(self.sid + ":withdrawn", keys)

    def test_v5_successful_send_marks_triggered_once(self):
        with app_test_directory(self) as folder:
            a, now = self.queued_signal_app(folder)
            post = self.run_sender(a, now)
            self.assertTrue(post.called)
            a.drain_delivery()
            self.assertEqual(self.w.setups[self.sid]["state"], "TRIGGERED")
            self.assertFalse(any(k.endswith(":withdrawn") for k, _ in list(a.outgoing.queue)))

    def test_v5_same_timestamp_and_out_of_order_prints(self):
        f = QuoteFeed(self.c)
        self.assertTrue(f.record(100., self.now, now=self.now))
        self.assertTrue(f.record(101., self.now, now=self.now))                  # same second: newest wins
        self.assertEqual(f.latest["price"], 101.)
        self.assertTrue(f.record(99., self.now - timedelta(seconds=1), now=self.now))   # older: extremes only
        self.assertEqual(f.latest["price"], 101.)
        self.assertEqual(f.peek_extremes(), {"low": 99., "high": 101.})
        self.assertFalse(f.record(50., self.now - timedelta(minutes=10), now=self.now))  # stale print rejected
        self.assertFalse(f.record(-1., self.now, now=self.now))
        self.assertEqual(f.seq, 3)

    def test_v5_concurrent_prints_never_lose_extremes(self):
        f = QuoteFeed(self.c)
        prices = [100. + (i % 50) / 10 for i in range(4000)] + [90.]
        def writer(chunk):
            for p in chunk:
                f.record(p, self.now, now=self.now)
        threads = [threading.Thread(target=writer, args=(prices[i::4],)) for i in range(4)]
        seen = []
        for t in threads:
            t.start()
        while any(t.is_alive() for t in threads):
            w = f.take_extremes()
            if w:
                seen.append(w)
        for t in threads:
            t.join()
        w = f.take_extremes()
        if w:
            seen.append(w)
        self.assertEqual(min(x["low"] for x in seen), 90.)
        self.assertEqual(max(x["high"] for x in seen), 104.9)
        self.assertEqual(f.seq, len(prices))

    # ------------------------------------------------------------------ V5: evidence integration
    def test_v5_setup_without_valid_evidence_rejected(self):
        def attempt(evidence, **setup):
            d = copy.deepcopy(self.data); d["setups"][0].update(setup)
            return _real_validate(d, self.history, self.c, evidence=evidence)
        cases = {
            "no snapshot": (None, {}),
            "empty snapshot": ({"built_at": "x", "frames": {}, "index": {}}, {}),
            "unknown id": (FIX_EV, {"evidence_ids": ["ev_bos_buy_1h", "invented"]}),
            "wrong direction": (FIX_EV, {"evidence_ids": list(SELL_IDS)}),
            "context only": (FIX_EV, {"evidence_ids": ["15min:RSI_DIVERGENCE_BUY:abc"]}),
            "invalidated zone": (fixture_evidence(fvg="INVALIDATED"), {}),
            "filled zone": (fixture_evidence(fvg="FILLED"), {}),
            "4h opposes continuation": (fixture_evidence(trend="BEARISH"), {}),
            "no BOS": (FIX_EV, {"evidence_ids": ["ev_fvg_buy_15m"]}),
            "duplicate ids": (FIX_EV, {"evidence_ids": ["ev_fvg_buy_15m", "ev_fvg_buy_15m"]}),
        }
        for name, (evidence, setup) in cases.items():
            with self.assertRaises(ValueError, msg=name):
                attempt(evidence, **setup)
        d = copy.deepcopy(self.data)
        d["setups"][0]["verified_evidence"] = {"required": []}            # model cannot supply this
        out = _real_validate(d, self.history, self.c, evidence=FIX_EV)
        self.assertEqual(out["setups"][0]["verified_evidence"]["required"], sorted(BUY_IDS))
        _real_validate(out, self.history, self.c, evidence=FIX_EV)        # re-validation is idempotent

    def test_v5_playbook_rules(self):
        def attempt(playbook, ids, conditions=None, evidence=FIX_EV):
            d = copy.deepcopy(self.data)
            d["setups"][0].update(playbook=playbook, evidence_ids=ids)
            if conditions:
                d["setups"][0]["conditions"] = conditions
            return _real_validate(d, self.history, self.c, evidence=evidence)
        two = [{"type": "CLOSE_ABOVE", "timeframe": "15min", "level": 101.5},
               {"type": "CLOSE_ABOVE", "timeframe": "15min", "level": 101.6}]
        reversal = ["ev_choch_buy_15m", "ev_sweep_buy_15m", "ev_fvg_buy_15m"]
        with self.assertRaisesRegex(ValueError, "2 ordered"):
            attempt("CHOCH_REVERSAL", reversal)
        attempt("CHOCH_REVERSAL", reversal, two)
        late_sweep = fixture_evidence(sweep_at="2026-10-06T12:15:00+00:00")
        with self.assertRaisesRegex(ValueError, "preceded"):
            attempt("CHOCH_REVERSAL", reversal, two, late_sweep)
        with self.assertRaisesRegex(ValueError, "preceded"):
            attempt("CHOCH_REVERSAL", ["ev_choch_buy_15m", "ev_fvg_buy_15m"], two)
        attempt("BREAKER_RETEST", ["ev_breaker_buy_15m"])
        attempt("BREAKER_RETEST", ["ev_breaker_buy_15m"], evidence=fixture_evidence(breaker="AWAITING_RETEST"))
        with self.assertRaisesRegex(ValueError, "breaker"):
            attempt("BREAKER_RETEST", ["ev_fvg_buy_15m"])
        attempt("FIB_ZONE_CONFLUENCE", ["ev_fib_buy_1h", "ev_fvg_buy_15m"])
        with self.assertRaisesRegex(ValueError, "OTE"):
            attempt("FIB_ZONE_CONFLUENCE", ["ev_fib_buy_1h"])
        with self.assertRaisesRegex(ValueError, "OTE"):
            attempt("FIB_ZONE_CONFLUENCE", ["ev_fvg_buy_15m", "15min:RSI_DIVERGENCE_BUY:abc"])
        # Context can be cited alongside structure, but is not required later.
        out = attempt("TREND_CONTINUATION", BUY_IDS + ["15min:RSI_DIVERGENCE_BUY:abc", "ev_fib_buy_1h"])
        self.assertEqual(out["setups"][0]["verified_evidence"]["required"], sorted(BUY_IDS))

    def test_v5_recheck_unknown_withholds_invalid_kills(self):
        spec = dict(self.data["setups"][0], verified_evidence={"required": sorted(BUY_IDS)})
        self.assertEqual(EV.recheck(spec, FIX_EV)[0], "OK")
        self.assertEqual(EV.recheck(spec, None)[0], "UNKNOWN")
        self.assertEqual(EV.recheck(spec, fixture_evidence(drop=["ev_fvg_buy_15m"]))[0], "UNKNOWN")
        self.assertEqual(EV.recheck(spec, fixture_evidence(fvg="INVALIDATED"))[0], "INVALID")
        self.assertEqual(EV.recheck(spec, fixture_evidence(fvg="FILLED"))[0], "INVALID")   # V5.1: filled ends it
        breaker = dict(spec, playbook="BREAKER_RETEST", evidence_ids=["ev_breaker_buy_15m"], verified_evidence=None)
        self.assertEqual(EV.recheck(breaker, fixture_evidence(breaker="AWAITING_RETEST"))[0], "UNKNOWN")
        self.assertEqual(EV.recheck(breaker, FIX_EV)[0], "OK")
        self.assertEqual(EV.recheck(breaker, fixture_evidence(breaker="INVALIDATED"))[0], "INVALID")

    def test_v5_gate_withholds_then_kills_on_evidence(self):
        with app_test_directory(self) as folder:
            a, now = self.queued_signal_app(folder)
            a.outgoing = queue.Queue(); a.journal.db.execute("DELETE FROM events"); a.journal.db.commit()
            a.evidence = fixture_evidence(drop=["ev_fvg_buy_15m"])
            with patch.object(a, "data_enabled", return_value=True):
                a.update_gate(now)
            self.assertEqual(a.delivery_gate["actionable"], set())
            self.assertEqual(self.w.setups[self.sid]["state"], "ARMED")       # withheld, still observed
            a.evidence = fixture_evidence(fvg="INVALIDATED")
            a.apply_evidence(now)
            self.assertEqual(self.w.setups[self.sid]["state"], "INVALIDATED")
            self.assertIn(self.sid + ":invalidated", [k for k, _ in list(a.outgoing.queue)])

    def test_v5_sender_rechecks_evidence(self):
        with app_test_directory(self) as folder:
            a, now = self.queued_signal_app(folder)
            a.evidence = fixture_evidence(fvg="INVALIDATED")     # new closed candle between gate and send
            post = self.run_sender(a, now)
            post.assert_not_called()

    def test_v5_python_structure_caps_grade(self):
        with app_test_directory(self) as folder:
            a = self.app(folder); self.arm(); self.push(60)
            now = self.now + timedelta(minutes=60, seconds=15)
            self.approve_now(a, now)
            a.evidence = fixture_evidence(trend="BEARISH")
            with patch.object(a, "data_enabled", return_value=True):
                a.update_gate(now)
            self.assertNotIn(self.sid, a.delivery_gate["actionable"])          # V5.1 default: veto
            a.c = dict(a.c, block_on_mtf_opposition=False)
            with patch.object(a, "data_enabled", return_value=True):
                a.update_gate(now)
            self.assertEqual(a.delivery_gate["plans"][self.sid][1]["grade"], "B_TRADE")

    def test_v5_playbook_specific_touch_threshold(self):
        c = load_config()
        c["playbook_touch_candles"]["BREAKER_RETEST"] = 1
        d = copy.deepcopy(self.data)
        d["setups"][0].update(playbook="BREAKER_RETEST", evidence_ids=["ev_breaker_buy_15m"], expires_minutes=60)
        _real_validate(copy.deepcopy(d), self.history, c, evidence=FIX_EV)     # 60 min suffices with one contact
        with self.assertRaisesRegex(ValueError, "Lifetime"):
            _real_validate(copy.deepcopy(d), self.history, self.c, evidence=FIX_EV)
        w = Watcher(c, "pb"); w.install(d, self.history, self.now, self.now)
        events = w.bars("15min", [self.bar(15)], self.now + timedelta(minutes=15, seconds=12))
        self.assertEqual([e[0] for e in events], ["WATCH"])
        with app_test_directory(self) as folder:
            path = Path(folder, "c.json")
            path.write_text(json.dumps({"playbook_touch_candles": {"BREAKER_RETEST": 0}}))
            with self.assertRaises(ValueError):
                load_config(path)
            path.write_text(json.dumps({"playbook_touch_candles": {"MADE_UP": 2}}))
            with self.assertRaises(ValueError):
                load_config(path)

    def test_v5_map_payload_carries_evidence_and_review_carries_citations(self):
        with app_test_directory(self) as folder:
            a = self.app(folder)
            a.full_ready = True; a.next_map = 0; a.analyst = Mock(job=None)
            with patch.object(sys_module(), "history_fresh", return_value=True):
                a.dispatch_ai(self.now)
            payload, tag = a.analyst.start.call_args.args[1:]
            ids = {i["id"] for f in payload["smc_evidence"]["frames"].values() for k in ("structure", "zones")
                   for i in f.get(k, [])}
            self.assertTrue(set(BUY_IDS) <= ids)
            self.assertIs(tag["evidence"], a.evidence)
            self.arm(); a.analyst = Mock(job=None); a.pending[self.sid] = "WATCH"
            with patch.object(sys_module(), "history_fresh", return_value=True):
                a.dispatch_ai(self.now + timedelta(minutes=46))
            payload = a.analyst.start.call_args.args[1]
            self.assertEqual(set(payload["cited_evidence"]), set(BUY_IDS))
            self.assertEqual(payload["python_checks"]["evidence_status"], "OK")

    def test_v5_consume_ai_validates_against_analysed_snapshot(self):
        with app_test_directory(self) as folder:
            a = self.app(folder); a.watcher = Watcher(self.c, "s")
            a.analyst = Mock(); a.analyst.poll.return_value = (
                {"kind": "map", "tag": {"epoch": "test", "history": self.history, "born": iso(self.now),
                                        "evidence": {"built_at": "x", "frames": {}, "index": {}}}},
                "result", {"technical": copy.deepcopy(self.data), "news": None})
            with patch.object(sys_module(), "history_fresh", return_value=True):
                a.consume_ai(self.now + timedelta(minutes=3))
            self.assertEqual(a.watcher.setups, {})                       # map rejected
            rows = a.journal.db.execute("SELECT payload FROM events WHERE status='MAP_REJECTED'").fetchall()
            self.assertIn("evidence", rows[-1][0])

    def test_v5_real_engine_evidence_end_to_end(self):
        h = self.history
        ev = EV.build_evidence(h, self.now)
        self.assertEqual(set(ev["frames"]), set(TF))
        self.assertTrue(all(f["available"] for f in ev["frames"].values()))
        json.dumps(ev, allow_nan=False)
        self.assertEqual(EV.compact(ev)["frames"]["15min"]["structure"], [])   # flat fixture: no structure

    # ------------------------------------------------------------------ V5: providers
    def test_v5_provider_registry(self):
        self.assertIs(provider_class("news", "none"), NoNews)
        self.assertIs(provider_class("broker_quotes", "mt5"), MT5BrokerQuotes)
        self.assertIs(provider_class("news", "gold_monitor:NoNews"), M.NoNews)
        for kind, name in (("news", "made_up"), ("calendar", "gold_monitor:NoNews"),
                           ("quotes", "os:path"), ("candles", "bad name!")):
            with self.assertRaises(Exception, msg=(kind, name)):
                provider_class(kind, name)
        with app_test_directory(self) as folder:
            path = Path(folder, "c.json")
            for bad in ({"providers": {"broker_quotes": "file"}}, {"providers": {"calendar": "json_file"}},
                        {"providers": {"news": "nope"}}, {"providers": {"brokers": "mt5"}},
                        {"provider_options": {"api_key": "x"}}):
                path.write_text(json.dumps(bad))
                with self.assertRaises(ValueError, msg=bad):
                    load_config(path)
            path.write_text(json.dumps({"providers": {"broker_quotes": "file"},
                                        "provider_options": {"broker_file": "q.json"}}))
            c = load_config(path)
            self.assertEqual((c["providers"]["candles"], c["providers"]["broker_quotes"]), ("mt5", "file"))

    def test_v5_null_providers_fail_closed(self):
        with self.assertRaises(RuntimeError):
            NoCalendar(self.c).fetch()
        self.assertEqual(NoNews(self.c).check({})["risk"], "UNKNOWN")
        self.assertIsNone(NoBrokerQuotes(self.c).quote())
        self.assertFalse(NoBrokerQuotes.required)

    def test_v5_json_file_calendar_matches_forexfactory_rules(self):
        with app_test_directory(self) as folder:
            path = Path(folder, "cal.json")
            c = dict(self.c, provider_options=dict(self.c["provider_options"], calendar_file=str(path)))
            path.write_text(json.dumps([{"country": "USD", "date": iso(utcnow()), "title": "CPI m/m", "impact": "High"}]))
            cal = JsonFileCalendar(c).fetch()
            self.assertEqual(cal["events"][0]["title"], "CPI m/m")
            path.write_text(json.dumps([{"country": "USD", "date": "2020-01-01T00:00:00Z", "title": "old", "impact": "Low"}]))
            with self.assertRaisesRegex(ValueError, "stale"):
                JsonFileCalendar(c).fetch()

    def test_v5_file_broker_bridge(self):
        with app_test_directory(self) as folder:
            path = Path(folder, "q.json")
            c = dict(self.c, provider_options=dict(self.c["provider_options"], broker_file=str(path)))
            t = time.time()
            path.write_text(json.dumps({"symbol": "XAUUSD", "bid": 4000.1, "ask": 4000.4, "time": t}))
            q = FileBrokerQuotes(c).quote()
            self.assertEqual((q["bid"], q["ask"], q["source"]), (4000.1, 4000.4, "file:q.json"))
            self.assertLess(abs(stamp(q["time"]).timestamp() - t), 1)
            path.write_text(json.dumps({"symbol": "EURUSD", "bid": 1, "ask": 1.1, "time": t}))
            with self.assertRaisesRegex(ValueError, "symbol"):
                FileBrokerQuotes(c).quote()
            path.write_text(json.dumps({"symbol": "XAUUSD", "bid": "nan", "ask": 1, "time": t}))
            with self.assertRaises(ValueError):
                FileBrokerQuotes(c).quote()

    def test_v5_broker_check_rules(self):
        self.c = dict(self.c, broker_slippage_allowance=0)  # isolate legacy broker gates from V5.1.1 planning allowance
        spec = self.data["setups"][0]          # BUY, entry 102-103, SL 98, TP1 115
        now = self.now
        q = lambda bid, ask, age=1: {"bid": bid, "ask": ask, "time": iso(now - timedelta(seconds=age)), "source": "t"}
        self.assertEqual(broker_check(spec, None, 102.5, now, self.c, False), (True, 102.5, None))
        self.assertEqual(broker_check(spec, q(102.45, 102.6), 102.5, now, self.c, True)[:2], (True, 102.6))  # BUY uses ask
        for name, quote, mid in (("missing", None, 102.5), ("stale", q(102.45, 102.6, age=60), 102.5),
                                 ("future", q(102.45, 102.6, age=-30), 102.5), ("spread", q(102.0, 103.0), 102.5),
                                 ("divergence", q(102.45, 102.6), 107.0), ("ask outside", q(102.9, 103.2), 103.0),
                                 ("crossed", q(102.6, 102.3), 102.5)):
            self.assertFalse(broker_check(spec, quote, mid, now, self.c, True)[0], name)
        sell = dict(spec, direction="SELL", entry_low=102., entry_high=103., stop_loss=106., invalidation=105.5, targets=[90.])
        self.assertEqual(broker_check(sell, q(102.45, 102.6), 102.5, now, self.c, True)[:2], (True, 102.45))  # SELL uses bid

    def test_v5_broker_gate_and_sender(self):
        self.c = dict(self.c, broker_slippage_allowance=0)  # isolate legacy broker gates from V5.1.1 planning allowance
        with app_test_directory(self) as folder:
            a = self.app(folder); self.arm(); self.push(60)
            now = self.now + timedelta(minutes=60, seconds=15)
            self.approve_now(a, now)
            good = {"symbol": "XAUUSD", "bid": 102.45, "ask": 102.6, "time": iso(now), "source": "mt5"}
            a.broker = Mock(required=True, quote=Mock(return_value=good))
            a.broker_quote = None
            with patch.object(a, "data_enabled", return_value=True):
                a.update_gate(now)
            self.assertEqual(a.delivery_gate["actionable"], set())                 # no broker quote yet
            a.broker_quote = dict(good, bid=101.0, ask=102.6)                      # spread 1.6 > 0.8
            with patch.object(a, "data_enabled", return_value=True):
                a.update_gate(now)
            self.assertEqual(a.delivery_gate["actionable"], set())
            a.broker_quote = good
            with patch.object(a, "data_enabled", return_value=True):
                a.update_gate(now)
            self.assertIn(self.sid, a.delivery_gate["actionable"])
            _, packet = list(a.outgoing.queue)[-1]
            self.assertIn("Ask 102.6", packet["text"])
            self.assertIn("الدخول المرجعي الآن: 102.6", packet["text"])
            a.broker.quote.side_effect = RuntimeError("terminal closed")          # fresh read fails at send
            post = self.run_sender(a, now)
            post.assert_not_called()

    def test_v5_broker_adapters_are_read_only(self):
        forbidden = ("order_send", "order_check", "positions_get", "OrderSend", "OrderClose", "OrderModify",
                     "position_close", "TRADE_ACTION")
        for cls in (BrokerQuotes, FileBrokerQuotes, HttpJsonBrokerQuotes, MT5BrokerQuotes, NoBrokerQuotes):
            code = inspect.getsource(cls)
            for word in forbidden:
                self.assertNotIn(word + "(", code, (cls.__name__, word))
        module_code = Path(M.__file__).read_text(encoding="utf-8")
        for word in forbidden:
            self.assertNotIn(word + "(", module_code, word)
        mq4 = ROOT / "mql" / "mql4" / "XAUUSD_QuoteBridge.mq4"
        if mq4.exists():
            code = mq4.read_text(encoding="utf-8")
            for word in ("OrderSend", "OrderClose", "OrderModify", "OrderDelete"):
                self.assertNotIn(word + "(", code, word)

    def test_v5_mt5_adapter_applies_server_offset(self):
        fake = Mock()
        fake.initialize.return_value = True
        fake.account_info.return_value = Mock(server="GIVTrade-Server")
        fake.symbol_info_tick.return_value = Mock(bid=4000.0, ask=4000.3, time_msc=(time.time() + 10800) * 1000)
        c = dict(self.c, provider_options=dict(self.c["provider_options"], mt5_server_utc_offset_minutes=180))
        M.MT5Session.reset()
        with patch.dict(sys.modules, {"MetaTrader5": fake}):
            q = MT5BrokerQuotes(c).quote()
        M.MT5Session.reset()
        self.assertLess(abs((stamp(q["time"]) - utcnow()).total_seconds()), 2)
        # reset() closes the connection (6.1.3 reconnect); shutdown is not a trading call.
        self.assertEqual([call[0] for call in fake.method_calls],
                         ["initialize", "symbol_select", "symbol_info_tick", "account_info", "shutdown"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
