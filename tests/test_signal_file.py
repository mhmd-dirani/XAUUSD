#!/usr/bin/env python3
"""Confirmed BUY/SELL signals are written to runtime/signals.jsonl for the auto-trader, at the
moment they pass final_check and before Telegram; a withheld signal writes nothing. Offline."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import gold_monitor as M  # noqa: E402
import test_signal_gate as TM  # noqa: E402  (fixtures only)


class SignalFileTests(unittest.TestCase):
    def setUp(self):
        self.t = TM.Tests("test_wick_invalidation")
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)

    def lines(self, a):
        p = a.folder / a.c["signal_file"]
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def test_sent_signal_is_written_with_its_plan(self):
        t = self.t
        with TM.app_test_directory(t) as folder:
            a, now = t.queued_signal_app(folder)
            order = []
            real_write = M.write_signal
            with patch.object(M, "write_signal", side_effect=lambda p, r: order.append("file") or real_write(p, r)), \
                 patch.object(M, "delivery_status", side_effect=lambda rq, text, ch="analysis": order.append(ch) or "SENT"):
                t.run_sender(a, now)
            # the trader is never behind Telegram; 6.1.5: the short form to the trading channel, then the full text
            self.assertEqual(order, ["file", "signals", "analysis"])
            [rec] = self.lines(a)
            spec = t.w.setups[t.sid]["spec"]
            self.assertEqual(rec["sid"], t.sid)
            self.assertEqual(rec["direction"], spec["direction"])
            self.assertEqual((rec["stop_loss"], rec["targets"]), (spec["stop_loss"], list(spec["targets"])))
            self.assertIn(rec["grade"], ("A_TRADE", "B_TRADE"))
            self.assertTrue(rec["key"].endswith(":signal"))

    def test_withheld_signal_writes_nothing(self):
        t = self.t
        with TM.app_test_directory(t) as folder:
            a, now = t.queued_signal_app(folder)
            a.evidence = TM.fixture_evidence(fvg="FILLED")          # evidence died after approval
            t.run_sender(a, now)
            self.assertEqual(self.lines(a), [])

    def test_switch_off(self):
        t = self.t
        with TM.app_test_directory(t) as folder:
            a, now = t.queued_signal_app(folder)
            a.c["signal_file"] = ""
            t.run_sender(a, now)
            self.assertFalse(any(p.suffix == ".jsonl" for p in a.folder.iterdir()))

    def test_locked_file_never_raises(self):
        d = Path(tempfile.mkdtemp())
        with patch("builtins.open", side_effect=PermissionError("locked")), patch.object(M.time, "sleep"):
            self.assertFalse(M.write_signal(d / "signals.jsonl", {"key": "k"}))

    def test_config_validation(self):
        d = Path(tempfile.mkdtemp())
        (d / "c.json").write_text(json.dumps({"signal_file": "..\\\\elsewhere.jsonl"}))
        with self.assertRaises(ValueError):
            M.load_config(d / "c.json")


if __name__ == "__main__":
    unittest.main(verbosity=2)
