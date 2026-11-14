"""11 Nov: the zone-reaction study (Astra LIMIT ideas: limit fill vs the 5M/1M reaction at the zone). Report only."""
from __future__ import annotations

import json
import math
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
for p in (HERE.parent / "src", HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from autotrader import zone_study as Z  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 11, 4, 13, 0, tzinfo=UTC)            # Wednesday, London/NY


def path(points, t0=T0, zig=1.2, period=10, seed=7):
    """M1 candles through (minute, price) waypoints with a zig-zag and a little noise so fractal swings form."""
    import random
    rnd = random.Random(seed)
    out, prev = [], points[0][1]
    for (m0, p0), (m1, p1) in zip(points, points[1:]):
        for m in range(m0, m1):
            base = p0 + (p1 - p0) * (m - m0) / (m1 - m0)
            c = base + zig * math.sin(2 * math.pi * m / period) + rnd.gauss(0, 0.1)
            t = int(t0.timestamp()) + 60 * m
            out.append((t, prev, max(prev, c) + rnd.uniform(0.02, 0.4), min(prev, c) - rnd.uniform(0.02, 0.4), c,
                        t + Z.rule_offset(t)))
            prev = c
    return out


def idea(direction="BUY", lo=4040.0, hi=4045.0, stop=4036.0, tps=(4085.0,), created=T0, nxt=None, **kw):
    return dict({"analysis": "A1", "created": created + timedelta(minutes=4), "next": nxt, "id": "I1",
                 "direction": direction, "entry_type": "LIMIT", "entry_low": lo, "entry_high": hi,
                 "invalidation": stop, "targets": list(tps), "trade_class": "TREND", "confidence": 50, "risk": "MEDIUM"},
                **kw)


HOLD = [(0, 4060), (100, 4044.2), (130, 4042.5), (200, 4056), (320, 4090), (400, 4092)]
BREAK = [(0, 4060), (100, 4044.2), (200, 4038), (260, 4030), (400, 4020)]


def one(points, i=None, **kw):
    m1 = path(points)
    return Z.study_idea(i or idea(), m1, Z.five_minute(m1), 0.3, 2.0, dict(Z.PR.DEFAULTS, mode="all"), 12)


class TimeTest(unittest.TestCase):
    def test_broker_time_is_new_york_plus_7(self):
        oct_srv = int(datetime(2026, 10, 20, 12, 0, tzinfo=UTC).timestamp())     # server clock 12:00, UTC+3
        nov_srv = int(datetime(2026, 11, 5, 12, 0, tzinfo=UTC).timestamp())      # server clock 12:00, UTC+2
        self.assertEqual(Z.server_to_utc(oct_srv), oct_srv - 3 * 3600)
        self.assertEqual(Z.server_to_utc(nov_srv), nov_srv - 2 * 3600)
        self.assertEqual(Z.rule_offset(oct_srv), 3 * 3600)
        self.assertEqual(Z.rule_offset(nov_srv), 2 * 3600)

    def test_five_minute_and_4h(self):
        m1 = path([(0, 4000), (600, 4010)])
        m5 = Z.five_minute(m1)
        self.assertEqual(m5[0][0] % 300, 0)
        self.assertEqual(m5[0][5] - m5[0][0], 300)
        self.assertEqual(m5[0][4], m1[4][4])
        close_t, _px = Z.four_hour_close(m1, 0)
        srv = Z.server_to_utc(0) * 0 + close_t + Z.rule_offset(close_t)
        self.assertEqual(srv % 14400, 0)                                   # a broker 4H boundary


class ReactionTest(unittest.TestCase):
    def test_hold_is_traded_and_the_limit_fills(self):
        r = one(HOLD)
        self.assertEqual(r["limit"], "FILLED")
        self.assertEqual(r["level"], 4045.0)
        self.assertIn(r["reaction"], ("HOLD", "HOLD_4H"))
        self.assertIsNotNone(r["reaction_r"])
        self.assertGreater(r["reaction_r"], 1.5)                            # TP1 4085
        self.assertGreater(r["limit_r"], 1.5)
        self.assertGreater(r["reaction_entry"], 4045.0)                     # the reaction pays a worse price

    def test_break_skips_the_trade_the_limit_lost(self):
        r = one(BREAK)
        self.assertEqual(r["limit"], "FILLED")
        self.assertAlmostEqual(r["limit_r"], -1.0, places=1)
        self.assertIn(r["reaction"], ("BROKEN", "BROKEN_STOP"))
        self.assertIsNone(r["reaction_r"])

    def test_sell_is_the_mirror(self):
        mirror = [(m, 8100 - p) for m, p in BREAK]
        i = idea("SELL", lo=8100 - 4045.0, hi=8100 - 4040.0, stop=8100 - 4036.0, tps=(8100 - 4085.0,))
        r = one(mirror, i)
        self.assertEqual(r["limit"], "FILLED")
        self.assertIn(r["reaction"], ("BROKEN", "BROKEN_STOP"))

    def test_no_look_ahead(self):
        full = one(HOLD)
        m1 = path(HOLD)
        hit = datetime.strptime(full["touch_utc"], "%Y-%m-%d %H:%M").replace(tzinfo=UTC).timestamp()
        for cut_min in (60, 120, 240):
            cut = [b for b in m1 if b[0] < hit + cut_min * 60]
            r = Z.study_idea(idea(), cut, Z.five_minute(cut), 0.3, 2.0, None, 12)
            if r["reaction"] not in ("UNDECIDED", None):
                self.assertEqual(r["reaction"], full["reaction"])
                self.assertEqual(r["reaction_entry"], full["reaction_entry"])

    def test_limit_cancelled_when_tp1_trades_first(self):
        up = [(0, 4060), (60, 4090), (200, 4044), (300, 4040)]
        r = one(up)
        self.assertEqual(r["limit"], "CANCELLED_TP1")
        self.assertIsNone(r["touch_utc"])

    def test_idea_life_ends_at_the_next_analysis(self):
        r = one(HOLD, idea(nxt=T0 + timedelta(minutes=30)))
        self.assertEqual(r["limit"], "NOT_FILLED")
        self.assertEqual(r["reaction"], "NO_TOUCH")

    def test_skips(self):
        self.assertEqual(one(HOLD, idea(lo=4055, hi=4065, stop=4050))["skip"], "price already in/through the zone")
        self.assertEqual(one(HOLD, idea(stop=4041))["skip"], "stop or TP1 on the wrong side")
        self.assertIsNone(Z.limit_level(True, 4040, 4045, 4036, 4046, 2.0))        # 2R not inside the zone


class StudyTest(unittest.TestCase):
    def test_repeats_counted_once_and_report(self):
        m1 = path(HOLD)
        ideas = [idea(), dict(idea(), analysis="A2", created=T0 + timedelta(minutes=20)),
                 dict(idea(), entry_type="BREAKOUT")]
        rows = Z.analyse(ideas, m1, 0.3, 2.0, None, 12)
        self.assertEqual(len(rows), 2)                                       # the BREAKOUT idea is not a limit
        self.assertEqual(rows[1]["skip"], "repeat of an earlier idea at the same touch")
        text = "\n".join(Z.report(rows, 30, 0.3))
        self.assertIn("LIMIT (live today)", text)
        self.assertIn("5M+1M", text)

    def test_load_ideas_reads_the_memory_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "astra_memory.sqlite3"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE analyses (id TEXT PRIMARY KEY, created_at TEXT, stance TEXT, data TEXT)")
            for k, h in enumerate((10, 12)):
                con.execute("INSERT INTO analyses VALUES (?,?,?,?)",
                            (f"A{k}", f"2026-11-04T{h}:00:00+00:00", "X", json.dumps({"ideas": [{"id": "I1"}]})))
            con.commit()
            con.close()
            got = Z.load_ideas(db, datetime(2026, 11, 4, tzinfo=UTC), datetime(2026, 11, 5, tzinfo=UTC))
            self.assertEqual(len(got), 2)
            self.assertEqual(got[0]["next"], got[1]["created"])
            self.assertIsNone(got[1]["next"])

    def test_wrong_offset_stops(self):
        m = types.SimpleNamespace(offset=lambda: 3 * 3600)
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "m.sqlite3"
            db.write_bytes(b"")
            out = Z.run(m, db, None, 30, 0.3, 2.0, None, 12, now=datetime(2026, 11, 11, tzinfo=UTC))
        self.assertIn("does not match", out)


if __name__ == "__main__":
    unittest.main()
