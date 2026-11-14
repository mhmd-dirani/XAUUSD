"""
6.2 - user plans: a trade plan sent by the owner on Telegram, checked by Python, judged by Astra's next
map, traded by the auto-trader when Astra agrees, and turned into a lesson when it closes.

Message shape (a private chat with the bot or the admin group; only TELEGRAM_ADMIN_IDS are accepted):

    /idea SELL 4009-4016 sl 4024.5 tp 3977 3960 3940
    1. breaker + 15M FVG overlap at the OTE
    2. daily close below 4073, all frames bearish
    3. no retest yet after the Michigan sell-off

Life of a plan (runtime/user_plans.json):
  RECEIVED  parsed; Python checks sides, R:R >= min_rr from the zone edge, stop size, zone not absurdly far
  WAITING   checks passed; the plan goes into every map payload until Astra's map answers it
  APPROVED  Astra's map carries an idea in the same direction whose zone overlaps the plan's zone
            -> the auto-trader takes it like any idea (confirmation candle, tiers capped at B, validator)
  REJECTED  Astra's map has an idea in the opposite direction at that place, or no agreeing idea for
            user_plan_max_hours; the reply says what Astra's map saw instead
  TRADED / CLOSED  set by the trader; CLOSED writes lessons/U<id>.txt and runs the teaching session
Nothing here places an order: the plan only becomes an "idea" the trader validates as usual.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOG = logging.getLogger("user_plans")
UTC = timezone.utc
FILE_NAME = "user_plans.json"
CMD = re.compile(r"^/idea\s+(BUY|SELL)\s+([\d.]+)\s*[-–]\s*([\d.]+)\s+sl\s+([\d.]+)\s+tp\s+([\d.\s]+)$", re.I)


def admin_ids() -> set[int]:
    raw = os.environ.get("TELEGRAM_ADMIN_IDS", "")
    return {int(x) for x in re.findall(r"\d+", raw)}


def parse(text: str) -> dict | None:
    """The /idea command -> plan dict, or None when the first line is not a plan."""
    lines = [l.strip() for l in (text or "").strip().splitlines() if l.strip()]
    if not lines:
        return None
    m = CMD.match(lines[0])
    if not m:
        return None
    direction = m.group(1).upper()
    lo, hi = sorted((float(m.group(2)), float(m.group(3))))
    sl = float(m.group(4))
    tps = [float(x) for x in m.group(5).split()][:3]
    reasons = [re.sub(r"^\d+[.)]\s*", "", l) for l in lines[1:]]
    return {"direction": direction, "entry_low": lo, "entry_high": hi, "stop": sl, "targets": tps, "reasons": reasons}


def check(plan: dict, price: float | None, atr1h: float | None, min_rr: float = 2.0) -> str | None:
    """Why the plan cannot be traded as written (None = passes). The trader re-checks everything later."""
    d, lo, hi, sl, tps = plan["direction"], plan["entry_low"], plan["entry_high"], plan["stop"], plan["targets"]
    if not tps:
        return "no target"
    if d == "BUY" and not (sl < lo and tps[0] > hi):
        return "BUY needs stop < zone < TP1"
    if d == "SELL" and not (sl > hi and tps[0] < lo):
        return "SELL needs TP1 < zone < stop"
    edge = lo if d == "BUY" else hi                           # the worst fill inside the zone
    rr = abs(tps[0] - edge) / abs(edge - sl)
    if rr < min_rr:
        return f"R:R {rr:.2f} to TP1 from the zone edge is below {min_rr}"
    if atr1h and abs(sl - edge) > 5 * atr1h:
        return f"stop {abs(sl - edge):.1f} wider than 5 x 1H-ATR"
    if atr1h and abs(sl - edge) < 0.25 * atr1h:        # 6.2.1: U2 on 26 Oct had a 1-point stop (spread + noise)
        return f"stop {abs(sl - edge):.1f} from the zone edge is narrower than 0.25 x 1H-ATR ({0.25 * atr1h:.1f})"
    if price is not None and atr1h and (max(lo - price, price - hi, 0)) > 4 * atr1h:
        return f"zone is {max(lo - price, price - hi):.1f} away from price (> 4 x 1H-ATR)"
    if len(plan.get("reasons") or []) == 0:
        return "give at least one reason (a line under the command): it is what the lesson learns from"
    return None


def overlap(a_lo, a_hi, b_lo, b_hi) -> bool:
    return a_lo <= b_hi and b_lo <= a_hi


def judge(plan: dict, analysis: dict | None, max_hours: float, now: datetime) -> tuple[str, str]:
    """(status, note) from Astra's latest map: APPROVED when an idea of the same direction overlaps the
    plan's zone; REJECTED when an opposite-direction idea sits there or the plan is older than max_hours;
    WAITING otherwise."""
    if analysis:
        for idea in analysis.get("ideas") or []:
            try:
                lo, hi = float(idea["entry_low"]), float(idea["entry_high"])
            except (KeyError, TypeError, ValueError):
                continue
            if overlap(lo, hi, plan["entry_low"], plan["entry_high"]):
                if idea.get("direction") == plan["direction"]:
                    return "APPROVED", (f"Astra's map agrees: idea {idea.get('id')} {idea.get('direction')} "
                                        f"{lo}-{hi}, confidence {idea.get('confidence')}%, class {idea.get('trade_class')}")
                return "REJECTED", (f"Astra's map has the opposite view there: idea {idea.get('id')} "
                                    f"{idea.get('direction')} {lo}-{hi} ({idea.get('title', '')[:80]})")
    born = datetime.fromisoformat(plan["received_utc"].replace("Z", "+00:00"))
    if now - born > timedelta(hours=max_hours):
        return "REJECTED", f"no agreeing idea in Astra's maps for {max_hours:g} h"
    return "WAITING", "not in Astra's current map; the plan is in the next map's payload"


class Store:
    def __init__(self, directory: Path):
        self.path = Path(directory) / FILE_NAME
        self.lock = threading.Lock()

    def load(self) -> list:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def save(self, plans: list):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(plans, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, plan: dict, chat_id, now: datetime) -> dict:
        with self.lock:
            plans = self.load()
            pid = 1 + max([p["id"] for p in plans] or [0])
            rec = dict(plan, id=pid, chat_id=chat_id, status="WAITING", received_utc=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                       notes=[])
            plans.append(rec)
            self.save(plans)
            return rec

    def update(self, pid: int, **fields):
        with self.lock:
            plans = self.load()
            for p in plans:
                if p["id"] == pid:
                    p.update(fields)
            self.save(plans)

    def by_status(self, *statuses) -> list:
        return [p for p in self.load() if p["status"] in statuses]


def payload_view(plans: list) -> list:
    """What the map payload carries: the owner's open plans, reasons included, for Astra to weigh."""
    return [{"id": f"U{p['id']}", "direction": p["direction"], "entry_low": p["entry_low"], "entry_high": p["entry_high"],
             "stop": p["stop"], "targets": p["targets"], "reasons": p["reasons"],
             "note": "the owner's own plan: if the data supports it, carry it as an idea at this zone and say why;"
                     " if not, do not, and let your map show the opposing reading"} for p in plans]


def as_trader_idea(p: dict, matched: dict | None) -> dict:
    """An APPROVED plan in the shape the auto-trader's idea path expects."""
    m = matched or {}
    return {"id": f"U{p['id']}", "direction": p["direction"], "risk": m.get("risk", "MEDIUM"),
            "confidence": m.get("confidence", 40), "entry_type": "LIMIT", "readiness": "NEEDS_CONFIRMATION",
            "title": "owner plan: " + "; ".join(p["reasons"])[:120], "entry_low": p["entry_low"],
            "entry_high": p["entry_high"], "invalidation": p["stop"], "targets": p["targets"],
            "trade_class": m.get("trade_class"), "sl_anchor": m.get("sl_anchor"), "tp_anchors": m.get("tp_anchors"),
            "astra_risk": m.get("astra_risk", m.get("risk", "MEDIUM")), "user_plan": p["id"],
            "confirmation": m.get("confirmation", "15M rejection / CHoCH in the zone")}


def lesson_text(p: dict, review: dict | None) -> str:
    """lessons/U<id>.txt in the teaching-session format: the owner's view and what happened."""
    outcome = ""
    if review:
        outcome = (f"\nOUTCOME: {review.get('outcome')} {review.get('r', '')}R | MFE {review.get('mfe_r')}R "
                   f"MAE {review.get('mae_r')}R | 4h after {review.get('after_4h')} | verdict {review.get('verdict')}")
    reasons = "\n".join(f"- {r}" for r in p["reasons"])
    return (f"QUESTION: XAUUSD {p['direction']} plan at {p['entry_low']}-{p['entry_high']}, stop {p['stop']}, "
            f"targets {p['targets']} on {p['received_utc'][:10]}. Would you take it? Where?\n"
            f"MY VIEW: {p['direction']} from {p['entry_low']}-{p['entry_high']} because:\n{reasons}\n"
            f"Astra's map: {'; '.join(p.get('notes') or [])[:400]}{outcome}\n")


class TelegramInbox(threading.Thread):
    """Long-polls getUpdates and hands /idea messages from admins to `on_plan(text, chat_id)`."""

    def __init__(self, token: str, on_plan, reply, state_dir: Path):
        super().__init__(daemon=True)
        self.token, self.on_plan, self.reply = token, on_plan, reply
        self.offset_file = Path(state_dir) / "telegram_offset.txt"
        self.admins = admin_ids()
        self.stop = False

    def run(self):
        import requests
        try:
            offset = int(self.offset_file.read_text()) + 1
        except (OSError, ValueError):
            offset = 0
        while not self.stop:
            try:
                r = requests.get(f"https://api.telegram.org/bot{self.token}/getUpdates",
                                 params={"offset": offset, "timeout": 20, "allowed_updates": '["message"]'}, timeout=30)
                for u in (r.json().get("result") or []):
                    offset = u["update_id"] + 1
                    self.offset_file.write_text(str(u["update_id"]))
                    msg = u.get("message") or {}
                    text = msg.get("text") or ""
                    if not text.lower().startswith("/idea"):
                        continue
                    uid, chat = (msg.get("from") or {}).get("id"), (msg.get("chat") or {}).get("id")
                    if uid not in self.admins:
                        LOG.warning("Telegram /idea from a non-admin user id %s ignored", uid)
                        continue
                    self.on_plan(text, chat)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Telegram inbox: %s", type(exc).__name__)
                time.sleep(10)
