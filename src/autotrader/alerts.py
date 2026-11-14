"""
Telegram alerts (1.7.2: three channels, same bot token):
  TELEGRAM_CHAT_LOG       errors, rejections, reviews, the full order detail (tier, regime, session)
  TELEGRAM_CHAT_SIGNALS   the trading channel: one short block per order event, levels only, English
  TELEGRAM_CHAT_ANALYSIS  not used by the trader
TELEGRAM_CHAT_ID is the fallback for any channel not configured (the single-chat behaviour of 1.6).
An error text is sent at most once per 30 minutes.
"""

from __future__ import annotations

import logging
import os
import time

LOG = logging.getLogger("autotrader.alerts")
_sent: dict[str, float] = {}
ENABLED = True
TRADES = True
CHANNEL_ENV = {"log": "TELEGRAM_CHAT_LOG", "signals": "TELEGRAM_CHAT_SIGNALS", "analysis": "TELEGRAM_CHAT_ANALYSIS"}


def chat_for(channel: str) -> str:
    return (os.getenv(CHANNEL_ENV[channel], "") or os.getenv("TELEGRAM_CHAT_ID", "")).strip()


def _post(channel: str, text: str) -> bool:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), chat_for(channel)
    if not (token and chat):
        return False
    try:
        import requests
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text[:4000]}, timeout=10)
        return True
    except Exception as exc:  # noqa: BLE001
        LOG.warning("Telegram %s message failed: %s", channel, type(exc).__name__)
        return False


def error(msg: str):
    LOG.error(msg)
    if not ENABLED:
        return
    key = msg[:120]
    if time.time() - _sent.get(key, 0) < 1800:
        return
    _sent[key] = time.time()
    _post("log", f"⚠️ AUTO-TRADER (demo) ERROR\n{msg}")


def safe_trade(build_msg, build_short=None):
    """1.10.4: a notification must never fail an order that the broker already accepted. 28 Oct: IDEA-BUY-4031 was
    placed and journalled, then signal_text raised IndexError and the app reported 'Order send failed'."""
    try:
        msg = build_msg()
        short = build_short() if build_short else None
    except Exception as exc:  # noqa: BLE001
        LOG.warning("Trade notification text failed (%s); the order itself is fine", type(exc).__name__)
        return
    trade(msg, short)


def trade(msg: str, short: str | None = None):
    """Trade notification: the full text to the log channel; `short` (levels only) to the trading channel.
    No de-duplication: every order event is distinct."""
    LOG.info("TELEGRAM %s", msg.replace("\n", " | ")[:300])
    if not TRADES:
        return
    _post("log", f"🤖 AUTO-TRADER (demo)\n{msg}")
    if short:
        _post("signals", short)


def signal_text(what: str, order: dict, lots, fill=None) -> str:
    """Trading-channel block: direction, entry, SL, TP1-3, size. Nothing else."""
    m = order.get("meta") or {}
    side = "🟢 BUY" if order["direction"] == "BUY" else "🔴 SELL"
    kind = "market" if order["order_type"] == "MARKET" else order["order_type"].split("_")[1].lower()
    entry = fill if fill is not None else order["entry_price"]
    lines = [f"{side} XAUUSD — {what}", f"Entry {entry} ({kind})", f"SL   {order['stop_loss']}",
             f"TP1  {order['take_profit_1']}"]
    if order.get("take_profit_2") is not None:
        lines.append(f"TP2  {order['take_profit_2']}")
    tg = m.get("targets") if isinstance(m.get("targets"), list) else []
    tp3 = tg[2] if len(tg) > 2 else None          # 1.10.4: ideas with only TP1/TP2 crashed here after the send
    if tp3 is not None:
        lines.append(f"TP3  {tp3}")
    tier = f" · Tier {m['tier']}" if m.get("tier") else ""
    lines.append(f"Size {lots} lots{tier} · demo")
    return "\n".join(lines)


def tail(playbook, confirmation, session) -> str:
    """' | playbook | confirmation | session' with empty parts left out (legacy records have none)."""
    parts = [str(x) for x in (playbook, confirmation, session) if x]
    return (" | " + " | ".join(parts)) if parts else ""


def _rr(order: dict) -> str:
    """R:R from the validated order, recomputed from the levels when it is missing (never 'None')."""
    rr = order.get("rr")
    if rr is None:
        try:
            e, sl, tp = float(order["entry_price"]), float(order["stop_loss"]), float(order["take_profit_1"])
            rr = round(abs(tp - e) / abs(e - sl), 2)
        except Exception:  # noqa: BLE001
            return "?"
    return f"{rr:.2f}"


def order_text(what: str, order: dict, lots, risk_money, grade, fill=None) -> str:
    m = order.get("meta") or {}
    rg = m.get("regime") or {}
    tp2 = order.get("take_profit_2")
    risk = f"risk {risk_money:.2f}" if isinstance(risk_money, (int, float)) and risk_money > 0 else "risk n/a"
    tier = f"Tier {m['tier']} " if m.get("tier") else ""
    lines = [f"{what}: {order['direction']} {lots} lots {tier}{grade[0]} | {order['setup_id']}",
             f"Entry {fill if fill is not None else order['entry_price']} | SL {order['stop_loss']} | "
             f"TP1 {order['take_profit_1']}{f' | TP2 {tp2}' if tp2 is not None else ''} | R:R {_rr(order)} | {risk}",
             f"Playbook {m.get('playbook') or order.get('strategy') or '?'} | confirmation {m.get('confirmation') or '-'} | "
             f"session {m.get('session') or '-'}"]
    if rg:
        lines.append(f"Regime: macro {rg.get('macro_trend')} | 1H {rg.get('intermediate_trend')} | 15M "
                     f"{rg.get('local_trend')} | {rg.get('phase')} | vol {rg.get('volatility')} | mom {rg.get('momentum')}")
    if m.get("astra_class") and m.get("astra_class") != m.get("playbook"):
        lines.append(f"Astra called it {m['astra_class']}; Python classified {m.get('playbook')}")
    if m.get("tier_reason"):
        lines.append(str(m["tier_reason"]))
    if m.get("expansion_block"):
        lines.append("Expansion filter (shadow) would have BLOCKED this trade")
    return "\n".join(lines)
