#!/usr/bin/env python3
"""6.1.5 / 1.7.2: three Telegram channels (analysis / signals / log), AI usage + cost in the journal,
map priorities and the soft daily budget. Offline."""
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import gold_monitor as M  # noqa: E402
from autotrader import alerts  # noqa: E402

UTC = timezone.utc
CHANS = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "@fallback", "TELEGRAM_CHAT_ANALYSIS": "@analysis",
         "TELEGRAM_CHAT_SIGNALS": "@trading", "TELEGRAM_CHAT_LOG": "@log"}


class FakeRequests:
    def __init__(self):
        self.posts = []

    def post(self, url, json=None, timeout=None):
        self.posts.append(json)
        return Mock(status_code=200, json=lambda: {"ok": True})


class SignalBotRoutingTests(unittest.TestCase):
    def test_keys_route_to_channels(self):
        for key, ch in (("daily:2026-10-22", "analysis"), ("weekly:2026-43", "analysis"),
                        ("astra-idea:2026-10-23:SELL:106:plan", "analysis"), ("astra-watch:x:ACTIVE:0", "analysis"),
                        ("2026-10-23:abcd:watch", "analysis"), ("2026-10-23:abcd:invalidated", "analysis"),
                        ("2026-10-23:abcd:signal", "signals"), ("2026-10-23:online", "log"),
                        ("2026-10-23:abcd:withdrawn", "log")):
            self.assertEqual(M.channel_for_key(key), ch, key)

    def test_chat_ids_with_and_without_configuration(self):
        with patch.dict(os.environ, CHANS):
            self.assertEqual([M.channel_chat(c) for c in ("analysis", "signals", "log")], ["@analysis", "@trading", "@log"])
        with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "@only", "TELEGRAM_CHAT_ANALYSIS": "", "TELEGRAM_CHAT_SIGNALS": "",
                                     "TELEGRAM_CHAT_LOG": ""}):
            self.assertEqual([M.channel_chat(c) for c in ("analysis", "signals", "log")], ["@only"] * 3)

    def test_delivery_posts_to_the_channel(self):
        rq = FakeRequests()
        with patch.dict(os.environ, CHANS):
            self.assertEqual(M.delivery_status(rq, "hello", "log"), "SENT")
        self.assertEqual(rq.posts[0]["chat_id"], "@log")

    def test_signal_short_text_is_levels_only_in_english(self):
        s = {"spec": {"direction": "SELL", "entry_low": 4016.91, "entry_high": 4023.51, "stop_loss": 4033.5,
                      "targets": [3986.6, 3975.32, 3960.25]}, "expires": "2026-10-23T06:30Z"}
        txt = M.signal_short_text(s, 4018.0, {"grade": "B_TRADE"})
        self.assertEqual(txt.splitlines()[0], "🔴 SELL XAUUSD — signal B_TRADE")
        for line in ("Entry 4016.91–4023.51 (now 4018)", "SL   4033.5", "TP1  3986.6", "TP2  3975.32", "TP3  3960.25"):
            self.assertIn(line, txt)
        self.assertNotIn("الهيكل", txt)


class TraderRoutingTests(unittest.TestCase):
    def setUp(self):
        self.rq = FakeRequests()
        p = patch.dict(sys.modules, {"requests": self.rq})
        p.start()
        self.addCleanup(p.stop)
        alerts._sent.clear()

    def test_trade_goes_to_log_and_short_to_trading(self):
        with patch.dict(os.environ, CHANS):
            alerts.trade("PLACED SELL_LIMIT: long detail", short="🔴 SELL XAUUSD\nEntry 4017.97")
            alerts.error("MT5 problem")
            alerts.trade("🔎 REVIEW ...")                                   # no short: log only
        chats = [(p["chat_id"], p["text"].splitlines()[0]) for p in self.rq.posts]
        self.assertEqual(chats, [("@log", "🤖 AUTO-TRADER (demo)"), ("@trading", "🔴 SELL XAUUSD"),
                                 ("@log", "⚠️ AUTO-TRADER (demo) ERROR"), ("@log", "🤖 AUTO-TRADER (demo)")])

    def test_fallback_to_single_chat(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "@one", "TELEGRAM_CHAT_ANALYSIS": "",
                                     "TELEGRAM_CHAT_SIGNALS": "", "TELEGRAM_CHAT_LOG": ""}):
            alerts.trade("x", short="y")
        self.assertEqual([p["chat_id"] for p in self.rq.posts], ["@one", "@one"])

    def test_signal_text(self):
        order = {"direction": "SELL", "order_type": "SELL_LIMIT", "entry_price": 4017.97, "stop_loss": 4033.5,
                 "take_profit_1": 3986.6, "take_profit_2": 3975.32,
                 "meta": {"tier": "B", "targets": [3986.6, 3975.32, 3960.25]}}
        txt = alerts.signal_text("PENDING ORDER", order, 0.03)
        self.assertEqual(txt.splitlines(), ["🔴 SELL XAUUSD — PENDING ORDER", "Entry 4017.97 (limit)", "SL   4033.5",
                                            "TP1  3986.6", "TP2  3975.32", "TP3  3960.25", "Size 0.03 lots · Tier B · demo"])
        self.assertIn("Entry 4018.17 (market)", alerts.signal_text("FILLED", dict(order, order_type="MARKET"), 0.03, 4018.17))


class CostAndBudgetTests(unittest.TestCase):
    def setUp(self):
        self.app = M.App.__new__(M.App)
        self.app.c = M.load_config(None)
        self.app.journal = M.Journal(Path(tempfile.mkdtemp()))
        self.app.analyst = Mock(job=None)
        self.app.epoch = "E"

    def test_cost_from_usage_and_prices(self):
        u = {"model": "claude-opus-5-5", "input_tokens": 40000, "output_tokens": 8000,
             "cache_read_input_tokens": 10000, "cache_creation_input_tokens": 0}
        self.assertAlmostEqual(M.ai_cost_usd(u, self.app.c["ai_prices_per_million"]), 0.16 + 0.16 + 0.002, places=4)
        self.assertIsNone(M.ai_cost_usd(dict(u, model="unknown-model"), self.app.c["ai_prices_per_million"]))
        self.assertIsNone(M.ai_cost_usd(None, {}))

    def test_usage_is_journalled_and_summed(self):
        for _ in range(3):
            self.app.record_usage("map", {"model": "claude-opus-5-5", "input_tokens": 40000, "output_tokens": 8000})
        usd, calls = self.app.ai_spend_today()
        self.assertEqual((round(usd, 2), calls), (0.96, 3))
        self.assertEqual(self.app.ai_spend_month()[1], 3)
        self.app.record_usage("review", None)                                  # no usage: nothing recorded
        self.assertEqual(self.app.ai_spend_today()[1], 3)

    def test_priorities(self):
        z = {"zone_id": "M145", "event": "ZONE_ENTRY"}
        self.assertEqual(self.app.map_priority("Price 4004 entered remembered 1day support (tests 2, status ACTIVE)", z), "ALWAYS")
        self.assertEqual(self.app.map_priority("Price 4011 entered remembered 4h support (tests 1, status ACTIVE)", z), "NORMAL")
        self.assertEqual(self.app.map_priority("Memory: 4h sell_side LIQUIDITY_SWEPT", {"zone_id": "M70", "event": "LIQUIDITY_SWEPT"}), "ALWAYS")
        self.assertEqual(self.app.map_priority("Abnormal 15M range (>2.5 ATR)"), "LOW")
        self.assertEqual(self.app.map_priority("4h support/structure broken"), "ALWAYS")
        self.app.zone_refresh_at = {"M145": time.time() - 600}
        self.assertEqual(self.app.map_priority("Price ... (tests 2, status ACTIVE)", z), "LOW")   # same zone within the hour

    def spend(self, usd):
        self.app.record_usage("map", {"model": "claude-opus-5-5", "input_tokens": int(usd / 4 * 1e6), "output_tokens": 0})

    def test_soft_budget_skips_normal_but_never_always(self):
        self.assertTrue(self.app.map_allowed("NORMAL", "weak zone"))
        self.spend(3.5)                                                         # past the 3.3 budget
        self.assertFalse(self.app.map_allowed("NORMAL", "weak zone"))
        self.assertTrue(self.app.map_allowed("ALWAYS", "sweep"))
        self.assertFalse(self.app.map_allowed("LOW", "abnormal range"))
        self.assertTrue(self.app.scheduled_map_allowed())
        self.assertEqual(self.app.journal.db.execute("SELECT count(*) FROM events WHERE status='REFRESH_DEFERRED'").fetchone()[0], 1)

    def test_hard_limit_is_off_by_default_and_stops_everything_when_set(self):
        self.spend(50)
        self.assertTrue(self.app.map_allowed("ALWAYS", "sweep"))
        self.assertTrue(self.app.scheduled_map_allowed())
        self.app.c["ai_daily_hard_limit_usd"] = 25
        self.assertFalse(self.app.map_allowed("ALWAYS", "sweep"))
        self.assertFalse(self.app.scheduled_map_allowed())

    def test_request_refresh_uses_priority_and_remembers_the_zone(self):
        self.app.last_v6_refresh = 0
        self.app.next_map = 99
        self.app.history, self.app.sessions = {}, None
        with patch.object(M, "history_fresh", return_value=True):
            self.assertFalse(self.app.request_refresh("Abnormal 15M range (>2.5 ATR)"))
            self.assertEqual(self.app.next_map, 99)
            self.assertTrue(self.app.request_refresh("Price 4004 entered remembered 1day support (tests 2, status ACTIVE)",
                                                     {"zone_id": "M145", "event": "ZONE_ENTRY"}))
            self.assertEqual(self.app.next_map, 0)
            self.app.last_v6_refresh = 0
            self.assertFalse(self.app.request_refresh("Price 4004 entered remembered 1day support (tests 2, status ACTIVE)",
                                                      {"zone_id": "M145", "event": "ZONE_ENTRY"}))   # same zone again

    def test_idea_posting_floor(self):
        c = self.app.c
        self.assertEqual((c["idea_post_min_confidence"], c["idea_post_min_rr"]), (30, 1.5))
        self.assertIsNone(M.idea_below_posting_floor({"confidence": 45, "rr_tp1": 2.39}, c))
        self.assertIn("confidence 24%", M.idea_below_posting_floor({"confidence": 24, "rr_tp1": 0.86}, c))
        self.assertIn("R:R 0.86", M.idea_below_posting_floor({"confidence": 35, "rr_tp1": 0.86}, c))
        self.assertIsNone(M.idea_below_posting_floor({"confidence": 24, "rr_tp1": 0.86}, dict(c, idea_post_min_confidence=0,
                                                                                               idea_post_min_rr=0)))
        self.assertIsNone(M.idea_below_posting_floor({"confidence": None}, c))
        # 6.1.7: Friday 17:05, BUY zone 22 points below price (~2.4 x 1H-ATR) -> not posted until price gets there
        self.assertIn("x 1H-ATR away", M.idea_below_posting_floor({"confidence": 45, "rr_tp1": 2.2, "distance_atr": 2.4}, c))
        self.assertIsNone(M.idea_below_posting_floor({"confidence": 45, "rr_tp1": 2.2, "distance_atr": 1.2}, c))
        self.assertIsNone(M.idea_below_posting_floor({"confidence": 45, "rr_tp1": 2.2, "distance_atr": -0.3}, c))  # inside
        self.assertEqual(c["idea_post_max_distance_atr"], 1.5)

    def test_config_validation(self):
        d = Path(tempfile.mkdtemp())
        for bad in ('{"ai_daily_budget_usd": -1}', '{"ai_prices_per_million": {"x": [1, 2]}}'):
            (d / "c.json").write_text(bad)
            with self.assertRaises(ValueError, msg=bad):
                M.load_config(d / "c.json")
        (d / "c.json").write_text('{"ai_daily_budget_usd": 0, "ai_daily_hard_limit_usd": 25}')
        c = M.load_config(d / "c.json")
        self.assertEqual((c["ai_daily_budget_usd"], c["ai_daily_hard_limit_usd"]), (0, 25))


if __name__ == "__main__":
    unittest.main(verbosity=2)
