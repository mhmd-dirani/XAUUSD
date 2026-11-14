#!/usr/bin/env python3
"""6.2 / 1.9: the owner's /idea plans - parse, Python checks, Astra's verdict from the map, the trader's
idea path (Tier B cap), and the lesson written after the review. Offline."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import gold_monitor as GM  # noqa: E402
import user_plans as UP  # noqa: E402
from autotrader import bridge as BR  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
TEXT = "/idea SELL 4009-4016 sl 4024.5 tp 3977 3960 3940\n1. breaker + FVG overlap\n2) daily close below 4073\n"


class ParseAndCheckTests(unittest.TestCase):
    def test_parse(self):
        p = UP.parse(TEXT)
        self.assertEqual((p["direction"], p["entry_low"], p["entry_high"], p["stop"], p["targets"]),
                         ("SELL", 4009.0, 4016.0, 4024.5, [3977.0, 3960.0, 3940.0]))
        self.assertEqual(p["reasons"], ["breaker + FVG overlap", "daily close below 4073"])
        self.assertIsNone(UP.parse("hello"))
        self.assertIsNone(UP.parse("/idea SELL 4009 sl 4024"))
        self.assertEqual(UP.parse("/idea buy 3996–4000 sl 3985 tp 4017")["direction"], "BUY")

    def test_checks(self):
        p = UP.parse(TEXT)
        self.assertIsNone(UP.check(p, 3999.0, 8.0))
        self.assertIn("R:R", UP.check(dict(p, targets=[4005.0]), 3999.0, 8.0))          # 1.3R from the zone edge
        self.assertIn("stop", UP.check(dict(p, stop=4090.0, targets=[3850.0]), 3999.0, 8.0))   # 74 > 5 ATR
        self.assertIn("away from price", UP.check(p, 3900.0, 8.0))                        # 109 points away
        self.assertIn("reason", UP.check(dict(p, reasons=[]), 3999.0, 8.0))
        self.assertIn("SELL needs", UP.check(dict(p, stop=4000.0), 3999.0, 8.0))
        self.assertIn("BUY needs", UP.check({"direction": "BUY", "entry_low": 4000, "entry_high": 4005, "stop": 4010,
                                             "targets": [4030], "reasons": ["x"]}, 4002.0, 8.0))

    def test_judge_from_the_map(self):
        p = dict(UP.parse(TEXT), received_utc="2026-10-25T11:00:00Z")
        agree = {"ideas": [{"id": "I1", "direction": "SELL", "entry_low": 4010.0, "entry_high": 4018.0, "confidence": 40,
                            "trade_class": "TREND"}]}
        self.assertEqual(UP.judge(p, agree, 12, NOW)[0], "APPROVED")
        oppose = {"ideas": [{"id": "I2", "direction": "BUY", "entry_low": 4005.0, "entry_high": 4012.0, "title": "spring"}]}
        self.assertEqual(UP.judge(p, oppose, 12, NOW)[0], "REJECTED")
        elsewhere = {"ideas": [{"id": "I3", "direction": "SELL", "entry_low": 4040.0, "entry_high": 4048.0}]}
        self.assertEqual(UP.judge(p, elsewhere, 12, NOW)[0], "WAITING")
        self.assertEqual(UP.judge(p, None, 12, NOW)[0], "WAITING")
        self.assertEqual(UP.judge(p, elsewhere, 12, NOW + timedelta(hours=13))[0], "REJECTED")   # too old

    def test_store_payload_idea_and_lesson(self):
        st = UP.Store(Path(tempfile.mkdtemp()))
        rec = st.add(UP.parse(TEXT), chat_id=123, now=NOW)
        self.assertEqual((rec["id"], rec["status"]), (1, "WAITING"))
        view = UP.payload_view(st.by_status("WAITING"))
        self.assertEqual(view[0]["id"], "U1")
        self.assertIn("owner", view[0]["note"])
        st.update(1, status="APPROVED", matched_idea={"confidence": 45, "trade_class": "TREND", "risk": "MEDIUM"})
        idea = UP.as_trader_idea(st.by_status("APPROVED")[0], st.by_status("APPROVED")[0]["matched_idea"])
        self.assertEqual((idea["id"], idea["direction"], idea["invalidation"], idea["confidence"], idea["user_plan"]),
                         ("U1", "SELL", 4024.5, 45, 1))
        txt = UP.lesson_text(st.load()[0], {"outcome": "TP1", "r": 2.1, "mfe_r": 2.4, "mae_r": -0.3, "after_4h": "NEITHER",
                                            "verdict": "GOOD_EXIT"})
        self.assertIn("MY VIEW: SELL from 4009.0-4016.0", txt)
        self.assertIn("- breaker + FVG overlap", txt)
        self.assertIn("OUTCOME: TP1 2.1R", txt)

    def test_admin_ids(self):
        with patch.dict(os.environ, {"TELEGRAM_ADMIN_IDS": "123, 456"}):
            self.assertEqual(UP.admin_ids(), {123, 456})
        with patch.dict(os.environ, {"TELEGRAM_ADMIN_IDS": ""}):
            self.assertEqual(UP.admin_ids(), set())


class BridgeTests(unittest.TestCase):
    def test_approved_plans_ride_with_the_analysis_ideas(self):
        d = Path(tempfile.mkdtemp())
        a = {"id": "A1", "created_at": GM.iso(datetime.now(UTC)), "ideas": [{"id": "I1", "direction": "SELL",
                                                                              "entry_low": 4010, "entry_high": 4018}]}
        (d / "astra_analysis.json").write_text(json.dumps(a))
        plans = [dict(UP.parse(TEXT), id=1, status="APPROVED", received_utc="2026-10-25T11:00:00Z",
                      matched_idea={"confidence": 40}),
                 dict(UP.parse(TEXT), id=2, status="WAITING", received_utc="2026-10-25T11:00:00Z")]
        (d / UP.FILE_NAME).write_text(json.dumps(plans))

        class S:
            def __init__(self): self.d = {}
            def get(self, k, default=None): return self.d.get(k, default)
            def put(self, k, v): self.d[k] = v
        br = BR.Bridge.__new__(BR.Bridge)
        br.c = {"signal_bot_ideas": True, "idea_max_age_minutes": 120}
        br.state = S()
        br.analysis_path = d / "astra_analysis.json"
        got, ideas = br.open_ideas(datetime.now(UTC))
        self.assertEqual([i["id"] for i in ideas], ["I1", "U1"])
        br.idea_done(got, ideas[1])
        self.assertEqual([i["id"] for i in br.open_ideas(datetime.now(UTC))[1]], ["I1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
