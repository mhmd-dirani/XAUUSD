"""
Claude analyst: builds the payload from V6 data and returns the parsed decision JSON.
A malformed or inconsistent answer is downgraded to NO_TRADE, never to a trade.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime

import gold_monitor as GM
import gold_evidence as EV
import astra_anthropic as AN

LOG = logging.getLogger("autotrader.analyst")

REQUIRED = ("decision", "bias", "market_map", "setups", "orders", "news_risk", "reasoning_summary")
DECISIONS = ("WATCH", "A_TRADE", "B_TRADE", "NO_TRADE")


def build_payload(c, now: datetime, history: dict, evidence: dict, quote: dict, events: list,
                  playbook, previous_map, constraints: dict) -> dict:
    frames = {}
    for tf, n in c["candles_per_timeframe"].items():
        bars = history[tf]
        frames[tf] = {"indicators": GM.indicators(bars),
                      "columns": ["open_time_utc", "open", "high", "low", "close"],
                      "candles": [[b["time"][:16], round(b["open"], 2), round(b["high"], 2),
                                   round(b["low"], 2), round(b["close"], 2)] for b in bars[-n:]]}
    return {
        "symbol": "XAU/USD", "broker_symbol": c["symbol"],
        "utc_time": GM.iso(now),
        "quote": {"bid": quote["bid"], "ask": quote["ask"], "spread": quote["spread"]},
        "timeframes": frames,
        "python_evidence": EV.compact(evidence),
        "trader_playbook": playbook,
        "economic_calendar": events,
        "account_constraints": constraints,
        "previous_market_map": previous_map,
    }


class Analyst:
    def __init__(self, c):
        self.c = c
        self._client = None
        self._prompt = None
        self.last_usage = {}

    def _setup(self):
        if self._client is None:
            if not os.getenv("ANTHROPIC_API_KEY", "").strip():
                raise RuntimeError("ANTHROPIC_API_KEY is not set")
            # Same client as the V6 Claude analyst: Bearer auth (works for sk-ant-usr- keys),
            # ANTHROPIC_WORKSPACE_ID header when set, and the V6 ai_deadline_seconds timeout.
            self._client = AN.make_client(self.c["v6"], retries=1)
            self._prompt = self.c["prompt_path"].read_text(encoding="utf-8")
        return self._client, self._prompt

    def analyse(self, payload: dict) -> dict:
        """Raises on API failure (the caller counts failures)."""
        client, prompt = self._setup()
        msg = ("Analyse the following XAU/USD data and return ONLY the JSON object described in your "
               "instructions.\n\n" + json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
        resp = client.messages.create(
            model=self.c["anthropic_model"], max_tokens=self.c["max_output_tokens"],
            system=[{"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": msg}],
            extra_body={"output_config": {"effort": self.c["anthropic_effort"]}})
        u = AN._usage(resp) or {}
        self.last_usage = {"input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens")}
        stop = getattr(resp, "stop_reason", "")
        if stop == "refusal":
            return no_trade("model declined the request")
        if stop == "max_tokens":
            return no_trade("answer cut off at max_output_tokens")
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        return parse(text)


def parse(text: str) -> dict:
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    try:
        result = json.loads(t)
    except json.JSONDecodeError:
        a, b = t.find("{"), t.rfind("}")
        try:
            result = json.loads(t[a:b + 1]) if a != -1 and b > a else None
        except json.JSONDecodeError:
            result = None
    if not isinstance(result, dict):
        LOG.error("Claude returned no usable JSON; treated as NO_TRADE")
        return no_trade("unparseable model output")
    # "orders" (list, up to 2) is the contract; a single "order" object is accepted too.
    if "orders" not in result and "order" in result:
        result["orders"] = [result["order"]] if isinstance(result["order"], dict) else []
    result.pop("order", None)
    why = None
    missing = [k for k in REQUIRED if k not in result]
    orders = result.get("orders")
    if missing:
        why = f"missing fields {missing}"
    elif result["decision"] not in DECISIONS:
        why = f"invalid decision {result['decision']!r}"
    elif not isinstance(orders, list) or not all(isinstance(o, dict) for o in orders):
        why = "orders is not a list of objects"
    elif result["decision"] in ("A_TRADE", "B_TRADE") and not orders:
        why = "trade decision without orders"
    elif result["decision"] in ("WATCH", "NO_TRADE") and orders:
        why = "orders on a non-trade decision"
    if why:
        LOG.error("Claude JSON failed shape check (%s); treated as NO_TRADE", why)
        result["decision"], result["orders"] = "NO_TRADE", []
        result["reasoning_summary"] = f"[downgraded: {why}] {result.get('reasoning_summary', '')}"
        return result
    for o in orders:                       # each order carries its own grade; default = the decision
        if o.get("grade") not in ("A_TRADE", "B_TRADE"):
            o["grade"] = result["decision"] if result["decision"] in ("A_TRADE", "B_TRADE") else "B_TRADE"
    return result


def no_trade(reason: str) -> dict:
    return {"decision": "NO_TRADE", "bias": {}, "market_map": {}, "setups": [], "orders": [],
            "news_risk": "UNKNOWN", "reasoning_summary": reason}
