"""
Auto-trader settings: config/auto_trader_config.json on top of the V6 config.

Market settings (symbol, broker server, clock offset, sessions, calendar, news windows, spread
limits, minimum R:R, playbook file) are read from the V6 config (v6_config), so both bots always
agree. Only trading/risk settings live in the auto-trader config.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path

import gold_monitor as GM

ROOT = GM.ROOT

DEFAULTS = {
    # ---- link to the V6 settings (symbol, server, offset, sessions, calendar, spread, minimum_rr)
    "v6_config": "config/gold_v5_config.json",

    # ---- where trades come from
    "source": "signal_bot",               # "signal_bot": use the signal bot's Opus analysis (no extra Claude
                                          #   cost); "own_analysis": the trader calls Claude itself
    "signal_bot_signals": True,           # trade the signal bot's confirmed BUY/SELL signals (MARKET, at once)
    "signal_bot_ideas": True,             # turn its TRADE IDEAs into pending orders
    "idea_min_confidence": 0,             # % ; Astra's ideas are usually 30-40 %
    "idea_allowed_risk": ["LOW", "MEDIUM", "HIGH"],
    "idea_a_grade_confidence": 60,        # >= this and risk not HIGH -> A size, otherwise B size
    "idea_order_hours": 12,               # pending idea orders expire after this
    "idea_max_age_minutes": 120,          # ignore an analysis older than this (signal bot stopped?)
    "idea_entry_mode": "confirmation",    # "confirmation": wait for a closed 15M rejection candle at the zone,
                                          #   then enter at market (what Astra's ideas ask for);
                                          # "limit": place the pending order at once (no confirmation)
    "counter_trend_enabled": True,        # ideas against Python's 4H/1H structure (e.g. BUY at Daily support
                                          #   while bearish): only at a strong remembered zone, only after a
                                          #   closed confirmation, always B size
    "counter_timeframes": ["1day", "4h", "1week"],
    "counter_min_tests": 2,               # the zone must have held at least this many separate tests
    "counter_zone_tolerance": 3.0,        # $ ; idea entry zone and remembered zone must overlap within this
    "confirm_retest_hours": 4,            # confirmed but 2R gone at market: limit at the 2R price, valid this long
    "idea_require_evidence": False,       # ideas cite memory zones, not evidence ids (the 4H/1H veto still applies)
    "signal_max_age_seconds": 90,         # a confirmed signal older than this is not traded
    "signal_entry_tolerance": 1.0,        # $ outside the signal's entry zone still accepted
    "bridge_poll_seconds": 2,

    # ---- v1.6 market state, session levels and playbooks (see docs/AUTO_TRADER.md, "v1.6")
    "playbook_modes": {                   # live = demo orders (sized by tier) | off. 1.7: no paper mode
        "TREND": "live",                  #   with the 4H/1H structure (v1.5 rule)
        "COUNTER_TREND": "live",          #   against it, at a strong remembered Daily/4H zone (v1.5 rule)
        "REVERSAL_TRANSITION": "live",    #   against 4H/1H, but 15M+phase show an early reversal (Tier C)
        "SESSION_SWEEP": "live",          #   confirmed by a session / previous-day / week level sweep
        "BREAKOUT_CONTINUATION": "off",   #   1.8: first 15M close through a level WITH the trend, market entry
    },
    # 1.8.1: new orders only in these sessions (every playbook). Replay 24 Oct: Asia lost in every variant
    # (NEW_all: 62 Asia trades -3.5R); our four live losses were all Asia. [] = every session.
    "trade_sessions": ["LONDON", "LONDON_NY", "NEW_YORK"],
    "user_plan_auto_study": True,         # 1.9: a closed owner-plan trade is written as a lesson and studied
    "breakout_retest_minutes": 60,        # 1.8: a breakout limit (when the close gives < 2R) lives this long
    "tier_cap": {"BREAKOUT_CONTINUATION": "C"},   # 1.8: playbook -> highest tier allowed (until reviewed),
    "python_session_setups": "off",     # NEW: Python's own session-sweep setups (no Astra idea): paper | off
    "expansion_filter": "shadow",         # NEW: no trade against a strong one-way move without a validated
                                          #   reversal. shadow = never blocks, journals what it WOULD block;
                                          #   paper = blocked trades go to paper; live = blocked
    "regime": {},                         # overrides of autotrader/regime.py DEFAULTS (thresholds)
    "sessions": {                         # local times in each market's own zone (DST handled)
        "ASIA": {"tz": "Asia/Tokyo", "start": "09:00", "end": "15:00"},
        "LONDON": {"tz": "Europe/London", "start": "08:00", "end": "16:30"},
        "NEW_YORK": {"tz": "America/New_York", "start": "08:00", "end": "17:00"},
    },
    "opening_range_minutes": {"LONDON": 60, "NEW_YORK": 60},
    "telegram_trades": True,              # trade messages (sent / filled / closed / paper) with playbook,
                                          #   confirmation, regime and session

    # ---- account safety
    "require_demo": True,                 # refuse to start or trade unless MT5 reports a DEMO account
    "require_trader_terminal": True,      # TRADER_MT5_PATH must point at the demo terminal
    "magic": 40001,                       # SWING engine orders in MT5 (trend, counter-trend, signals)
    "magic_scalp": 40002,                 # reserved for the future 1M scalping engine (not active)

    # ---- which grades execute and how much they risk (% of equity per trade)
    "execute_grades": ["A_TRADE", "B_TRADE"],
    "risk_percent": {"A_TRADE": 0.5, "B_TRADE": 0.25},

    # ---- order rules (Python re-checks every number; Claude's own figures are ignored)
    "min_rr": None,                       # null = V6 minimum_rr (2.0)
    "sl_atr_min": 0.5,                    # stop distance >= 0.5 x ATR14(15M)
    "sl_atr_max": 5.0,                    # stop distance <= 5.0 x ATR14(15M) (Astra's stops are structural;
                                          #   lots shrink with the stop, so money at risk stays the same)
    "require_evidence": True,             # order must cite usable Python SMC evidence ids
    "block_on_mtf_opposition": None,      # null = V6 setting (4H/1H structure against = no trade)
    "allow_market_orders": True,
    "max_entry_deviation_cap": 3.0,       # $/oz
    "min_order_lifetime_minutes": 15,
    "max_order_lifetime_hours": 48,

    # ---- exposure and loss limits
    "max_active_orders": 2,               # pending + open together (two different orders may run at once)
    "max_open_positions": 2,
    "max_pending_orders": 2,
    "max_orders_per_analysis": 2,
    "max_total_risk_pct": 1.0,            # risk of all running orders together (breakeven trades count 0)
    "max_orders_per_setup_per_day": 2,    # a setup may be traded again after its earlier order finished
    "duplicate_atr": 0.5,                 # same direction, entry AND stop within 0.5 x ATR15 = duplicate
    # 1.7 direction rule. 15 Oct: three SELL limits stacked into one rally in 48 min. A position whose
    # stop is at breakeven or better carries no risk and does not count.
    "max_same_direction_positions": 1,    # RISKY positions + pending orders of this bot in ONE direction
    # 1.7 tiers: Python grades every idea from verifiable facts and sizes it by tier (risk % of equity).
    # confidence = Astra's probability that TP1 is reached before the stop (40 % at 2R is a good trade).
    "tiers": {
        "A": {"risk_pct": 1.0, "min_confidence": 40},   # with the 4H trend, London/NY, anchored stop+TP, no expansion against
        "B": {"risk_pct": 0.5, "min_confidence": 35},   # one A condition missing
        "C": {"risk_pct": 0.15, "min_confidence": 30, "min_lot_if_below": True},  # counter-trend / reversal / Asia / HIGH
    },
    "max_trades_per_day": 4,
    "daily_loss_limit_pct": 2.0,
    "weekly_loss_limit_pct": 5.0,
    # 1.10: a confirmed idea enters at market only while price is still at the zone (+/- this, $/oz);
    # otherwise a limit at the zone edge. 26 Oct: U1 (4009-4016) was "confirmed" with price at 4021.74.
    "confirm_entry_tolerance": 3.0,

    # ---- 1.10 LAB MODE (DEMO ONLY): the rule-testing track. Every candidate the live rules reject is still
    # traded, at the broker's minimum lot under its own magic number, with the list of rules it violated
    # stored on the order ("violations"). The post-trade review and `--report` then show, per rule, whether
    # the trades that broke it lost more than the trades that kept it. Nothing here changes the live rules.
    # Hard checks that stay on (they decide whether a result MEASURES anything, not whether it is safe):
    # demo account, terminal connected + Algo Trading on, market open, fresh quote, absolute spread cap,
    # broker stop level, one lab order per setup per day, lab_max_open, lab_max_per_day, lab_drawdown_kill_pct.
    "lab_mode": False,
    "lab_keep_after_tp_touch": False,
    "lab_entry": "plain",                 # 1.14: "recheck" = no blind lab limits; ideas become zones the LTF engine enters
                                          # after a 5M CHoCH and the entry recheck. "plain" = the 1.10 resting limits.
    "pre_news_guard": {"mode": "off", "minutes": None, "be_at_r": 1.0},   # 1.14: off | lab | all; see autotrader/newsguard.py
    "trade_protect": {"mode": "off", "be_at_r": 1.0, "lock_at_r": 1.5, "lock_r": 0.5, "near_tp_frac": 0.85,
                      "near_tp_exit": True, "check_seconds": 10},   # 1.15: off | lab | all; see autotrader/protect.py
    "opposite_trades": {"mode": "off", "max_wait_minutes": 120, "check_seconds": 10, "trend_lookback": 60,
                        "swing_k": 2, "recheck_pending": True},   # 1.16: off | live; see autotrader/conflict.py
    "scalp_lab": {"mode": "off", "variant": "PB5M_rr2.0", "max_per_day": 12, "daily_loss_usd": 25.0,
                  "max_spread": 0.5, "bars": 400},   # 11 Nov: off | lab; the replayed scalp forward-tested (autotrader/scalp.py)
    "news_mode": "lock",                  # 1.17: "lock" = no new order in the news window; "strict" = trade it like a
                                          # normal day, but only market entries on a strong 15M candle (autotrader/news.py)
    "news_strict_risk_pct": 0.25,         # 1.17: risk cap (%) for any order sent inside a news window in strict mode
    "holiday_trading": "block",           # 1.19: "block" = a USD holiday is a news window all day (old rule);
                                          # "normal" = a normal day (Astra already raises idea risk); releases still count
    "python_ideas": {"mode": "off", "until": None, "min_tests": 1, "timeframes": ["1week", "1day", "4h", "1h"],
                     "smc_timeframes": ["1day", "4h", "1h"], "max_distance_atr15": 8.0, "stop_buffer_atr15": 0.3,
                     "min_rr": 2.0, "max_tp_r": 4.0, "risk_pct": None, "max_ideas": 3, "reentry_hours": 12,
                     "telegram": False},   # 1.20: off | fallback | only; Python's own ideas from the market memory (pyideas.py)
    "breakout_lab": {"mode": "off", "sides": ["BUY", "SELL"]},   # 1.19: off | lab; breakouts AGAINST the Daily/4H or
                                          # 1H trend, sent to the lab at the minimum lot (live breakouts unchanged)
    "limit_entry_risk_pct": None,         # 1.16: cap (%) for live ideas entered as a limit WITHOUT a confirmation candle
    "zone_score": {"mode": "off", "band_r": 0.5, "cap_atr": 0.5, "cache_seconds": 60},   # 1.15: off | record; autotrader/zones.py
    "lab_drop_rule": "contradicted",      # 1.13.1: a kept lab limit is cancelled only when a newer map proposes the OPPOSITE
                                          # trade at its zone ("contradicted"), or as before when the map omits it ("unproposed")     # 1.11.1: lab limits survive TP1 trading before the fill; Astra's next map decides
    "lab_tp_touch_remap": False,          # 1.11.2: ... and the signal bot (6.2.1) is asked for that map at once
    "magic_lab": 40003,                   # lab orders in MT5; never the swing magic (live limits ignore them)
    "lab_lots": 0.0,                      # 0 = the broker's minimum lot (0.01 on XAUUSD = ~$1 per point)
    "lab_max_open": 6,                    # lab positions + pending lab orders working at once
    "lab_max_per_day": 20,                # new lab orders per UTC day
    "lab_drawdown_kill_pct": 15.0,        # lab realised loss this week >= this % of balance -> lab stops
    # ---- 1.10.3 NEWS STRADDLE (lab only, demo only). Before each listed release, while the spread is still
    # normal, pending stop orders are placed on both sides of the price (plus an optional "add" level further
    # out on each side). The broker fills them in the move; the bot cancels the other side as soon as one side
    # has a position, takes half at TP1 and moves the stop to entry (the executor's usual TP1 rule), and closes
    # whatever is left after straddle_max_hold_minutes. All levels are in ATR15 multiples.
    "news_straddle": False,
    "straddle_events": ["fed interest rate", "fomc statement", "federal funds", "non-farm", "nonfarm",
                        "cpi", "core pce", "gdp"],     # title substrings (lower case); HIGH USD events only
    "straddle_place_minutes": 2,           # place the orders this many minutes before the release
    "straddle_offset_atr": 0.5,            # first stop order this far beyond the ask / bid
    "straddle_add_atr": 1.0,               # the add order this much further out (0 = no add level)
    "straddle_stop_atr": 1.0,              # stop distance from each entry
    "straddle_min_stop": 3.0,              # ... but never under this many $ (the spread widens at the release)
    "straddle_tp1_r": 1.0,                 # TP1 (half closed, stop to entry) in R
    "straddle_tp2_r": 3.0,                 # final target in R
    "straddle_expire_minutes": 15,         # unfilled stop orders expire this long after the release
    "straddle_max_hold_minutes": 30,       # positions still open this long after the release are closed
    # ---- 1.10.5 lab sizing: 0 = the minimum lot (default, pure measurement). A percentage sizes every lab order
    # like the live track (equity x pct / stop), capped at tiers.A, with the lab's own open-risk cap on top of
    # the weekly lab_drawdown_kill_pct. The news straddle keeps lab_lots (slippage at releases is measured first).
    "lab_risk_pct": 0.0,
    "lab_max_total_risk_pct": 1.5,         # all working lab orders together may risk at most this much of equity
    "lab_unconfirmed_entry": True,        # also take ideas with NO closed confirmation (limit at the zone edge,
                                          #   tagged no_confirmation); False = confirmed ideas only

    # ---- when to analyse (each analysis is one paid Claude call)
    "analysis_every_minutes": 60,         # scheduled analysis on these 15M closes (60 = hourly)
    "analyse_on_start": True,
    "zone_trigger": True,                 # also analyse early when price reaches a WATCH zone
    "zone_trigger_atr": 0.25,             # "reached" = within 0.25 x ATR14(15M) of the zone
    "zone_trigger_cooldown_minutes": 45,  # per setup
    "skip_minutes_after_open": 30,        # no new orders just after the Sunday/daily open
    "skip_minutes_before_close": 20,      # ... or just before the daily/Friday close
    "close_delay_seconds": 10,
    "monitor_interval_seconds": 20,
    "max_consecutive_api_failures": 3,

    # ---- Claude
    "anthropic_model": None,              # null = the same model as the V6 analyst (anthropic_model / GOLD_CLAUDE_MODEL)
    "anthropic_effort": "medium",         # low | medium | high | xhigh | max (more = slower and costlier)
    "max_output_tokens": 16000,           # thinking + JSON answer
    "prompt_file": "config/auto_trader_prompt.md",
    "candles_per_timeframe": {"1day": 60, "4h": 120, "1h": 120, "15min": 120},

    # ---- 1.11 overnight trend engine (Python only, no Astra): see autotrader/overnight.py
    "overnight": {"mode": "off", "magic": 40004, "entry_ny": "18:45", "window_minutes": 30, "exit_ny": "08:00",
                  "stop_atr": 3.0, "risk_pct": 0.25, "max_spread": 0.60,
                  "rules": [{"name": "BULL_LONG", "macro": "BULLISH", "direction": "BUY", "mode": "live"},
                            {"name": "BEAR_SHORT", "macro": "BEARISH", "direction": "SELL", "mode": "off"},
                            {"name": "RANGE_SHORT", "macro": "MIXED", "direction": "SELL", "mode": "off"}]},
    # ---- 1.12 SELL_4H_WEAKNESS forward test in the lab (0.01 lot, demo): see autotrader/weakness.py
    "weakness_lab": {"mode": "off", "sides": ["SELL"], "min_run": 3, "touch_window": 8, "equal_tol_atr": 0.25,
                     "zone_slack_atr": 0.25, "stop_atr": 0.1, "min_rr": 2.0, "max_setup_4h": 12, "max_hold_days": 5,
                     "local_high_lookback": 20, "zone_types": ["OB", "BREAKER", "FVG"], "zone_tfs": [60, 30, 15, 5],
                     "respect_news_lock": True},
    # ---- 1.13 LTF sample: a 5M-entry + liquidity twin of every plain lab idea: see autotrader/ltf.py
    "ltf_lab": {"mode": "off", "swing_k": 2, "stop_buffer_atr15": 0.1, "min_rr": 2.0, "pool_tol_atr15": 0.1,
                "pool_path_r": 1.0, "pool_path_blocks": False, "stop_pool_atr15": 0.3, "require_sweep": False,
                "watch_hours": 12, "bars": 300, "news_guard_minutes": 60, "one_per_zone": True, "recheck_map": True},
    # ---- 1.13.1 1H-trend retest engine (lab only, entries by the LTF engine): see autotrader/h1trend.py
    "h1_trend_lab": {"mode": "off", "sides": ["BUY", "SELL"], "zone_kinds": ["FVG", "ORDER_BLOCK_CANDIDATE"],
                     "stop_buffer_atr15": 0.3, "min_rr": 2.0, "max_distance_atr15": 4.0, "max_zone_age_hours": 72},
    "expect_signal_bot": True,            # false: no hourly "no recent analysis" alert when gold_monitor is off on purpose
    # ---- files and alerts
    "state_directory": "runtime_trader",
    "telegram_errors": True,              # errors only (uses the V6 TELEGRAM_* variables); no trade messages
}
NESTED = ("risk_percent", "candles_per_timeframe", "playbook_modes", "regime", "opening_range_minutes", "tiers",
          "tier_cap", "overnight", "weakness_lab", "ltf_lab", "h1_trend_lab", "pre_news_guard", "trade_protect", "zone_score", "opposite_trades", "scalp_lab", "breakout_lab", "python_ideas")
GRADES = ("A_TRADE", "B_TRADE")


def load(path: str | Path | None = None) -> dict:
    c = copy.deepcopy(DEFAULTS)
    if path:
        p = Path(path)
        if not p.is_absolute():
            p = ROOT / p
        custom = json.loads(p.read_text(encoding="utf-8-sig"))
        custom.pop("_comment", None)
        unknown = set(custom) - set(c)
        if unknown:
            raise ValueError(f"Unknown auto-trader settings: {sorted(unknown)}")
        for k in NESTED:
            if k in custom:
                if not isinstance(custom[k], dict):
                    raise ValueError(f"{k} must be an object")
                c[k] = dict(c[k], **custom.pop(k))
        c.update(custom)

    v6_path = Path(c["v6_config"])
    if not v6_path.is_absolute():
        v6_path = ROOT / v6_path
    v6 = GM.load_config(v6_path if v6_path.exists() else None)
    c["v6"] = v6

    if c["min_rr"] is None:
        c["min_rr"] = float(v6["minimum_rr"])
    if c["block_on_mtf_opposition"] is None:
        c["block_on_mtf_opposition"] = bool(v6["block_on_mtf_opposition"])
    import astra_anthropic as AN
    if c["anthropic_model"] is None:
        c["anthropic_model"] = AN.model_name(v6)
    if c["anthropic_effort"] not in AN.EFFORTS:
        raise ValueError(f"anthropic_effort must be one of {AN.EFFORTS}")
    if not isinstance(c["max_output_tokens"], int) or not 2048 <= c["max_output_tokens"] <= 128000:
        raise ValueError("max_output_tokens must be an integer 2048..128000")

    # ---- validation (fail at start-up, not in the middle of a trade)
    grades = c["execute_grades"]
    if not isinstance(grades, list) or not set(grades) <= set(GRADES):
        raise ValueError(f"execute_grades must be a subset of {GRADES}")
    for g in GRADES:
        r = c["risk_percent"].get(g)
        if not isinstance(r, (int, float)) or isinstance(r, bool) or not 0 < r <= 2:
            raise ValueError(f"risk_percent.{g} must be > 0 and <= 2")
    for k in ("sl_atr_min", "sl_atr_max", "min_rr", "max_entry_deviation_cap", "daily_loss_limit_pct",
              "weekly_loss_limit_pct", "zone_trigger_atr"):
        if not isinstance(c[k], (int, float)) or isinstance(c[k], bool) or c[k] <= 0:
            raise ValueError(f"{k} must be a positive number")
    if c["sl_atr_min"] >= c["sl_atr_max"]:
        raise ValueError("sl_atr_min must be below sl_atr_max")
    if not isinstance(c["counter_trend_enabled"], bool):
        raise ValueError("counter_trend_enabled must be true or false")
    if not isinstance(c["counter_timeframes"], list) or not set(c["counter_timeframes"]) <= {"1week", "1day", "4h", "1h"}:
        raise ValueError("counter_timeframes must be a list of 1week / 1day / 4h / 1h")
    if not isinstance(c["counter_min_tests"], int) or c["counter_min_tests"] < 1:
        raise ValueError("counter_min_tests must be an integer >= 1")
    import autotrader.playbooks as PBK
    import autotrader.regime as RGM
    if "paper" in c["playbook_modes"].values() or c["python_session_setups"] == "paper":
        raise ValueError("1.7 removed paper trading: set playbook_modes to live / off and python_session_setups to off "
                         "(the demo account is the trial)")
    if set(c["playbook_modes"]) != set(PBK.CLASSES) or not set(c["playbook_modes"].values()) <= {"live", "off"}:
        raise ValueError(f"playbook_modes needs exactly {PBK.CLASSES}, each live / off")
    if c["expansion_filter"] not in ("off", "shadow", "live"):
        raise ValueError('expansion_filter must be "off", "shadow" or "live" (1.7 removed paper)')
    if c["python_session_setups"] != "off":
        raise ValueError('python_session_setups must be "off" (Python-only session sweeps are not offered until replayed)')
    # 1.11 overnight engine
    ov = c["overnight"]
    if ov["mode"] not in ("off", "live"):
        raise ValueError('overnight.mode must be "off" or "live"')
    if ov["mode"] == "live" and not c["require_demo"]:
        raise ValueError("overnight.mode live needs require_demo true (demo account only)")
    if not isinstance(ov["magic"], int) or ov["magic"] <= 0 or ov["magic"] in (c["magic"], c["magic_scalp"], c["magic_lab"]):
        raise ValueError("overnight.magic must be a positive integer different from magic, magic_scalp and magic_lab")
    if not isinstance(ov["rules"], list) or not ov["rules"]:
        raise ValueError("overnight.rules must be a non-empty list")
    seen = set()
    for r in ov["rules"]:
        if not isinstance(r, dict) or r.get("direction") not in ("BUY", "SELL") or r.get("mode") not in ("off", "live") \
                or r.get("macro") not in ("BULLISH", "BEARISH", "MIXED", "NEUTRAL") or not str(r.get("name", "")).strip():
            raise ValueError("each overnight rule needs name, macro (BULLISH/BEARISH/MIXED/NEUTRAL), direction (BUY/SELL), "
                             "mode (off/live)")
        if r["mode"] == "live" and r["macro"] in seen:
            raise ValueError(f"two live overnight rules for macro {r['macro']}")
        if r["mode"] == "live":
            seen.add(r["macro"])
    if not isinstance(ov["stop_atr"], (int, float)) or not c["sl_atr_min"] <= ov["stop_atr"] <= c["sl_atr_max"]:
        raise ValueError(f"overnight.stop_atr must be within sl_atr_min..sl_atr_max ({c['sl_atr_min']}..{c['sl_atr_max']})")
    if not isinstance(ov["risk_pct"], (int, float)) or not 0 < ov["risk_pct"] <= c["tiers"]["A"]["risk_pct"]:
        raise ValueError("overnight.risk_pct must be > 0 and <= tiers.A.risk_pct")
    for key in ("entry_ny", "exit_ny"):
        h, m = str(ov[key]).split(":")
        if not (0 <= int(h) < 24 and 0 <= int(m) < 60):
            raise ValueError(f"overnight.{key} must be HH:MM")
    # 1.12 4H weakness lab
    wk = c["weakness_lab"]
    unknown = set(wk) - {"mode", "sides", "min_run", "touch_window", "equal_tol_atr", "zone_slack_atr", "stop_atr", "min_rr",
                         "max_setup_4h", "max_hold_days", "local_high_lookback", "zone_types", "zone_tfs",
                         "respect_news_lock"}
    if unknown:
        raise ValueError(f"Unknown weakness_lab settings: {sorted(unknown)}")
    if wk["mode"] not in ("off", "lab"):
        raise ValueError('weakness_lab.mode must be "off" or "lab"')
    if wk["mode"] == "lab" and not (c["lab_mode"] and c["require_demo"]):
        raise ValueError("weakness_lab.mode lab needs lab_mode true and require_demo true (demo lab only)")
    if not isinstance(wk["sides"], list) or not wk["sides"] or not set(wk["sides"]) <= {"BUY", "SELL"}:
        raise ValueError('weakness_lab.sides must be a non-empty list of "SELL" / "BUY"')
    for k in ("min_run", "touch_window", "max_setup_4h", "max_hold_days"):
        if not isinstance(wk[k], int) or isinstance(wk[k], bool) or wk[k] <= 0:
            raise ValueError(f"weakness_lab.{k} must be a positive integer")
    if not isinstance(wk["local_high_lookback"], int) or wk["local_high_lookback"] < 0:
        raise ValueError("weakness_lab.local_high_lookback must be an integer >= 0 (0 = off)")
    for k in ("equal_tol_atr", "zone_slack_atr", "stop_atr"):
        if isinstance(wk[k], bool) or not isinstance(wk[k], (int, float)) or not 0 <= wk[k] <= 3:
            raise ValueError(f"weakness_lab.{k} must be a number 0..3")
    if isinstance(wk["min_rr"], bool) or not isinstance(wk["min_rr"], (int, float)) or wk["min_rr"] < 2.0:
        raise ValueError("weakness_lab.min_rr must be >= 2.0 (the project's 2R minimum)")
    if not isinstance(wk["zone_types"], list) or not wk["zone_types"] or not set(wk["zone_types"]) <= {"OB", "BREAKER", "FVG"}:
        raise ValueError("weakness_lab.zone_types must be a non-empty list drawn from OB, BREAKER, FVG")
    if not isinstance(wk["zone_tfs"], list) or not wk["zone_tfs"] or not set(wk["zone_tfs"]) <= {60, 30, 15, 5}:
        raise ValueError("weakness_lab.zone_tfs must be a non-empty list drawn from 60, 30, 15, 5")
    if not isinstance(wk["respect_news_lock"], bool):
        raise ValueError("weakness_lab.respect_news_lock must be true or false")
    # 1.13 LTF lab
    lt = c["ltf_lab"]
    unknown = set(lt) - {"mode", "swing_k", "stop_buffer_atr15", "min_rr", "pool_tol_atr15", "pool_path_r", "stop_pool_atr15",
                         "require_sweep", "pool_path_blocks", "watch_hours", "bars", "news_guard_minutes", "one_per_zone",
                         "recheck_map"}
    if unknown:
        raise ValueError(f"Unknown ltf_lab settings: {sorted(unknown)}")
    if lt["mode"] not in ("off", "lab"):
        raise ValueError('ltf_lab.mode must be "off" or "lab"')
    if lt["mode"] == "lab" and not (c["lab_mode"] and c["require_demo"]):
        raise ValueError("ltf_lab.mode lab needs lab_mode true and require_demo true (demo lab only)")
    for k in ("swing_k", "watch_hours", "bars"):
        if not isinstance(lt[k], int) or isinstance(lt[k], bool) or lt[k] <= 0:
            raise ValueError(f"ltf_lab.{k} must be a positive integer")
    for k in ("stop_buffer_atr15", "pool_tol_atr15", "pool_path_r", "stop_pool_atr15"):
        if isinstance(lt[k], bool) or not isinstance(lt[k], (int, float)) or not 0 <= lt[k] <= 5:
            raise ValueError(f"ltf_lab.{k} must be a number 0..5")
    if isinstance(lt["min_rr"], bool) or not isinstance(lt["min_rr"], (int, float)) or lt["min_rr"] < 2.0:
        raise ValueError("ltf_lab.min_rr must be >= 2.0 (the project's 2R minimum)")
    for k in ("require_sweep", "pool_path_blocks", "one_per_zone", "recheck_map"):
        if not isinstance(lt[k], bool):
            raise ValueError(f"ltf_lab.{k} must be true or false")
    if isinstance(lt["news_guard_minutes"], bool) or not isinstance(lt["news_guard_minutes"], (int, float)) \
            or not 0 <= lt["news_guard_minutes"] <= 240:
        raise ValueError("ltf_lab.news_guard_minutes must be a number 0..240")
    if c["lab_entry"] not in ("plain", "recheck"):
        raise ValueError('lab_entry must be "plain" or "recheck"')
    if c["lab_entry"] == "recheck" and c["lab_mode"] and lt["mode"] != "lab":
        raise ValueError("lab_entry recheck needs ltf_lab.mode lab (the LTF engine makes the entries)")
    ng = c["pre_news_guard"]
    if set(ng) - {"mode", "minutes", "be_at_r"}:
        raise ValueError(f"Unknown pre_news_guard settings: {sorted(set(ng) - {'mode', 'minutes', 'be_at_r'})}")
    if ng["mode"] not in ("off", "lab", "all"):
        raise ValueError('pre_news_guard.mode must be "off", "lab" or "all"')
    if ng["minutes"] is not None and (isinstance(ng["minutes"], bool) or not isinstance(ng["minutes"], (int, float))
                                      or not 0 < ng["minutes"] <= 240):
        raise ValueError("pre_news_guard.minutes must be null (= news_before_minutes) or a number 1..240")
    if isinstance(ng["be_at_r"], bool) or not isinstance(ng["be_at_r"], (int, float)) or ng["be_at_r"] < 0:
        raise ValueError("pre_news_guard.be_at_r must be a number >= 0")
    # 1.15 trade protection
    tp = c["trade_protect"]
    if set(tp) - set(DEFAULTS["trade_protect"]):
        raise ValueError(f"Unknown trade_protect settings: {sorted(set(tp) - set(DEFAULTS['trade_protect']))}")
    if tp["mode"] not in ("off", "lab", "all"):
        raise ValueError('trade_protect.mode must be "off", "lab" or "all"')
    for k in ("be_at_r", "lock_at_r", "lock_r", "near_tp_frac", "check_seconds"):
        if isinstance(tp[k], bool) or not isinstance(tp[k], (int, float)) or tp[k] <= 0:
            raise ValueError(f"trade_protect.{k} must be a number > 0")
    if not tp["lock_r"] < tp["lock_at_r"] or tp["lock_at_r"] < tp["be_at_r"]:
        raise ValueError("trade_protect needs lock_r < lock_at_r and lock_at_r >= be_at_r")
    if not 0.5 <= tp["near_tp_frac"] < 1.0:
        raise ValueError("trade_protect.near_tp_frac must be 0.5..0.99")
    if not isinstance(tp["near_tp_exit"], bool):
        raise ValueError("trade_protect.near_tp_exit must be true or false")
    ot = c["opposite_trades"]
    if set(ot) - set(DEFAULTS["opposite_trades"]):
        raise ValueError(f"Unknown opposite_trades settings: {sorted(set(ot) - set(DEFAULTS['opposite_trades']))}")
    if ot["mode"] not in ("off", "live"):
        raise ValueError('opposite_trades.mode must be "off" or "live"')
    for k in ("max_wait_minutes", "check_seconds", "trend_lookback", "swing_k"):
        if isinstance(ot[k], bool) or not isinstance(ot[k], (int, float)) or ot[k] <= 0:
            raise ValueError(f"opposite_trades.{k} must be a number > 0")
    if not isinstance(ot["recheck_pending"], bool):
        raise ValueError("opposite_trades.recheck_pending must be true or false")
    sl = c["scalp_lab"]
    if set(sl) - {"mode", "variant", "max_per_day", "daily_loss_usd", "max_spread", "bars"}:
        raise ValueError(f"Unknown scalp_lab settings: {sorted(set(sl) - {'mode', 'variant', 'max_per_day', 'daily_loss_usd', 'max_spread', 'bars'})}")
    if sl["mode"] not in ("off", "lab"):
        raise ValueError('scalp_lab.mode must be "off" or "lab" (it is a lab sample, never live)')
    if sl["mode"] == "lab" and not (c["lab_mode"] and c["require_demo"]):
        raise ValueError("scalp_lab.mode lab needs lab_mode true and require_demo true (demo lab only)")
    from . import scalp as _SCALP
    try:
        _SCALP.family(str(sl["variant"]))
    except ValueError as exc:
        raise ValueError(f"scalp_lab.variant: {exc}") from None
    for k in ("max_per_day", "daily_loss_usd", "max_spread", "bars"):
        if isinstance(sl[k], bool) or not isinstance(sl[k], (int, float)) or sl[k] <= 0:
            raise ValueError(f"scalp_lab.{k} must be a number > 0")
    if c["news_mode"] not in ("lock", "strict"):
        raise ValueError('news_mode must be "lock" or "strict"')
    pi = c["python_ideas"]
    keys = {"mode", "until", "min_tests", "timeframes", "smc_timeframes", "max_distance_atr15", "stop_buffer_atr15",
            "min_rr", "max_tp_r", "risk_pct", "max_ideas", "reentry_hours", "telegram"}
    if set(pi) - keys:
        raise ValueError(f"Unknown python_ideas settings: {sorted(set(pi) - keys)}")
    if pi["mode"] not in ("off", "fallback", "only"):
        raise ValueError('python_ideas.mode must be "off", "fallback" or "only"')
    if pi["mode"] != "off" and not c["require_demo"]:
        raise ValueError("python_ideas needs require_demo true (an unproven engine: demo only)")
    if pi["until"] is not None:
        try:
            datetime.strptime(str(pi["until"]), "%Y-%m-%d")
        except ValueError:
            raise ValueError('python_ideas.until must be null or a date "YYYY-MM-DD"') from None
    if not isinstance(pi["timeframes"], list) or not pi["timeframes"] or \
            set(pi["timeframes"]) - {"1week", "1day", "4h", "1h"}:
        raise ValueError('python_ideas.timeframes must be a non-empty list of "1week" / "1day" / "4h" / "1h"')
    if not isinstance(pi["telegram"], bool):
        raise ValueError("python_ideas.telegram must be true or false")
    if isinstance(pi["reentry_hours"], bool) or not isinstance(pi["reentry_hours"], (int, float)) or pi["reentry_hours"] < 0:
        raise ValueError("python_ideas.reentry_hours must be a number >= 0 (0 = no limit)")
    if not isinstance(pi["smc_timeframes"], list) or set(pi["smc_timeframes"]) - {"1week", "1day", "4h", "1h", "15min"}:
        raise ValueError('python_ideas.smc_timeframes must be a list of "1week" / "1day" / "4h" / "1h" / "15min"')
    for k in ("min_tests", "max_distance_atr15", "stop_buffer_atr15", "max_ideas"):
        if isinstance(pi[k], bool) or not isinstance(pi[k], (int, float)) or pi[k] <= 0:
            raise ValueError(f"python_ideas.{k} must be a number > 0")
    if isinstance(pi["min_rr"], bool) or not isinstance(pi["min_rr"], (int, float)) or pi["min_rr"] < c["min_rr"]:
        raise ValueError("python_ideas.min_rr must be a number >= min_rr")
    if isinstance(pi["max_tp_r"], bool) or not isinstance(pi["max_tp_r"], (int, float)) or pi["max_tp_r"] < pi["min_rr"]:
        raise ValueError("python_ideas.max_tp_r must be a number >= python_ideas.min_rr")
    if pi["risk_pct"] is not None and (isinstance(pi["risk_pct"], bool) or not isinstance(pi["risk_pct"], (int, float))
                                       or not 0 < pi["risk_pct"] <= c["tiers"]["B"]["risk_pct"]):
        raise ValueError("python_ideas.risk_pct must be null (Astra's sizing) or a number > 0 and <= tiers.B.risk_pct")
    if c["holiday_trading"] not in ("block", "normal"):
        raise ValueError('holiday_trading must be "block" or "normal"')
    bl = c["breakout_lab"]
    if set(bl) - {"mode", "sides"}:
        raise ValueError(f"Unknown breakout_lab settings: {sorted(set(bl) - {'mode', 'sides'})}")
    if bl["mode"] not in ("off", "lab"):
        raise ValueError('breakout_lab.mode must be "off" or "lab" (it is a lab sample, never live)')
    if bl["mode"] == "lab" and not (c["lab_mode"] and c["require_demo"]):
        raise ValueError("breakout_lab.mode lab needs lab_mode true and require_demo true (demo lab only)")
    if not isinstance(bl["sides"], list) or not bl["sides"] or set(bl["sides"]) - {"BUY", "SELL"}:
        raise ValueError('breakout_lab.sides must be a non-empty list of "BUY" / "SELL"')
    nr = c["news_strict_risk_pct"]
    if isinstance(nr, bool) or not isinstance(nr, (int, float)) or not 0 < nr <= c["tiers"]["B"]["risk_pct"]:
        raise ValueError("news_strict_risk_pct must be a number > 0 and <= tiers.B.risk_pct")
    lr = c["limit_entry_risk_pct"]
    if lr is not None and (isinstance(lr, bool) or not isinstance(lr, (int, float)) or not 0 < lr <= c["tiers"]["B"]["risk_pct"]):
        raise ValueError("limit_entry_risk_pct must be null or a number > 0 and <= tiers.B.risk_pct")
    zsc = c["zone_score"]
    if set(zsc) - set(DEFAULTS["zone_score"]):
        raise ValueError(f"Unknown zone_score settings: {sorted(set(zsc) - set(DEFAULTS['zone_score']))}")
    if zsc["mode"] not in ("off", "record"):
        raise ValueError('zone_score.mode must be "off" or "record" (it is never a gate)')
    for k in ("band_r", "cap_atr", "cache_seconds"):
        if isinstance(zsc[k], bool) or not isinstance(zsc[k], (int, float)) or zsc[k] <= 0:
            raise ValueError(f"zone_score.{k} must be a number > 0")
    # 1.13.1 1H-trend lab
    h1 = c["h1_trend_lab"]
    unknown = set(h1) - {"mode", "sides", "zone_kinds", "stop_buffer_atr15", "min_rr", "max_distance_atr15", "max_zone_age_hours"}
    if unknown:
        raise ValueError(f"Unknown h1_trend_lab settings: {sorted(unknown)}")
    if h1["mode"] not in ("off", "lab"):
        raise ValueError('h1_trend_lab.mode must be "off" or "lab"')
    if h1["mode"] == "lab" and not (c["lab_mode"] and c["require_demo"] and c["ltf_lab"]["mode"] == "lab"):
        raise ValueError("h1_trend_lab.mode lab needs lab_mode, require_demo and ltf_lab.mode lab (its entries are the LTF engine's)")
    if not isinstance(h1["sides"], list) or not h1["sides"] or not set(h1["sides"]) <= {"BUY", "SELL"}:
        raise ValueError('h1_trend_lab.sides must be a non-empty list of "BUY" / "SELL"')
    if not isinstance(h1["zone_kinds"], list) or not h1["zone_kinds"] or not set(h1["zone_kinds"]) <= {"FVG", "ORDER_BLOCK_CANDIDATE"}:
        raise ValueError("h1_trend_lab.zone_kinds must be a non-empty list drawn from FVG, ORDER_BLOCK_CANDIDATE")
    for k in ("stop_buffer_atr15", "max_distance_atr15", "max_zone_age_hours"):
        if isinstance(h1[k], bool) or not isinstance(h1[k], (int, float)) or h1[k] <= 0:
            raise ValueError(f"h1_trend_lab.{k} must be a positive number")
    if isinstance(h1["min_rr"], bool) or not isinstance(h1["min_rr"], (int, float)) or h1["min_rr"] < 2.0:
        raise ValueError("h1_trend_lab.min_rr must be >= 2.0 (the project's 2R minimum)")
    if not isinstance(c["expect_signal_bot"], bool):
        raise ValueError("expect_signal_bot must be true or false")
    # 1.10 lab mode
    if not isinstance(c["lab_mode"], bool):
        raise ValueError("lab_mode must be true or false")
    for k in ("lab_keep_after_tp_touch", "lab_tp_touch_remap"):
        if not isinstance(c[k], bool):
            raise ValueError(k + " must be true or false")
    if c["lab_drop_rule"] not in ("contradicted", "unproposed"):
        raise ValueError('lab_drop_rule must be "contradicted" or "unproposed"')
    if c["lab_mode"] and not c["require_demo"]:
        raise ValueError("lab_mode needs require_demo true: the lab never runs on a real account")
    if not isinstance(c["magic_lab"], int) or c["magic_lab"] in (c["magic"], c["magic_scalp"]) or c["magic_lab"] <= 0:
        raise ValueError("magic_lab must be a positive integer different from magic and magic_scalp")
    for k in ("lab_lots", "lab_drawdown_kill_pct", "confirm_entry_tolerance"):
        if not isinstance(c[k], (int, float)) or isinstance(c[k], bool) or c[k] < 0:
            raise ValueError(k + " must be a number >= 0")
    for k in ("lab_max_open", "lab_max_per_day"):
        if not isinstance(c[k], int) or isinstance(c[k], bool) or not 1 <= c[k] <= 100:
            raise ValueError(k + " must be an integer 1..100")
    for k in ("lab_risk_pct", "lab_max_total_risk_pct"):
        if not isinstance(c[k], (int, float)) or isinstance(c[k], bool) or c[k] < 0:
            raise ValueError(k + " must be a number >= 0")
    if c["lab_risk_pct"] > c["tiers"]["A"]["risk_pct"]:
        raise ValueError(f"lab_risk_pct {c['lab_risk_pct']} is above tiers.A.risk_pct {c['tiers']['A']['risk_pct']}")
    if c["lab_risk_pct"] > 0 and c["lab_max_total_risk_pct"] < c["lab_risk_pct"]:
        raise ValueError("lab_max_total_risk_pct must be at least lab_risk_pct")
    if not isinstance(c["news_straddle"], bool):
        raise ValueError("news_straddle must be true or false")
    if c["news_straddle"] and not c["lab_mode"]:
        raise ValueError("news_straddle is a lab playbook: it needs lab_mode true")
    if not isinstance(c["straddle_events"], list) or not all(isinstance(x, str) and x for x in c["straddle_events"]):
        raise ValueError("straddle_events must be a list of title words")
    for k in ("straddle_place_minutes", "straddle_offset_atr", "straddle_add_atr", "straddle_stop_atr",
              "straddle_min_stop", "straddle_tp1_r", "straddle_tp2_r", "straddle_expire_minutes",
              "straddle_max_hold_minutes"):
        if not isinstance(c[k], (int, float)) or isinstance(c[k], bool) or c[k] < 0:
            raise ValueError(k + " must be a number >= 0")
    if not (0 < c["straddle_place_minutes"] <= 30 and c["straddle_offset_atr"] > 0 and c["straddle_stop_atr"] > 0
            and 0 < c["straddle_tp1_r"] < c["straddle_tp2_r"] and c["straddle_expire_minutes"] > 0
            and c["straddle_max_hold_minutes"] >= c["straddle_expire_minutes"]):
        raise ValueError("straddle settings: place 1-30 min, offset/stop > 0, 0 < tp1_r < tp2_r, "
                         "expire > 0 and max_hold >= expire")
    if not isinstance(c["lab_unconfirmed_entry"], bool):
        raise ValueError("lab_unconfirmed_entry must be true or false")
    unknown_rg = set(c["regime"]) - set(RGM.DEFAULTS)
    if unknown_rg:
        raise ValueError(f"Unknown regime settings: {sorted(unknown_rg)}")
    for name, sp in c["sessions"].items():
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(sp["tz"])
            [int(x) for x in sp["start"].split(":") + sp["end"].split(":")]
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"sessions.{name} must be {{tz, start HH:MM, end HH:MM}}: {exc}") from None
    if not isinstance(c["telegram_trades"], bool):
        raise ValueError("telegram_trades must be true or false")
    if c["magic_scalp"] == c["magic"]:
        raise ValueError("magic_scalp must differ from magic")
    if c["idea_entry_mode"] not in ("confirmation", "limit"):
        raise ValueError('idea_entry_mode must be "confirmation" or "limit"')
    if c["source"] not in ("signal_bot", "own_analysis"):
        raise ValueError('source must be "signal_bot" or "own_analysis"')
    if not set(c["idea_allowed_risk"]) <= {"LOW", "MEDIUM", "HIGH"} or not isinstance(c["idea_allowed_risk"], list):
        raise ValueError("idea_allowed_risk must be a list of LOW / MEDIUM / HIGH")
    for k in ("idea_min_confidence", "idea_a_grade_confidence"):
        if isinstance(c[k], bool) or not isinstance(c[k], (int, float)) or not 0 <= c[k] <= 100:
            raise ValueError(f"{k} must be 0..100")
    for k in ("signal_bot_signals", "signal_bot_ideas", "idea_require_evidence"):
        if not isinstance(c[k], bool):
            raise ValueError(f"{k} must be true or false")
    for k in ("counter_zone_tolerance", "confirm_retest_hours", "idea_order_hours", "idea_max_age_minutes", "signal_max_age_seconds", "signal_entry_tolerance",
              "bridge_poll_seconds"):
        if isinstance(c[k], bool) or not isinstance(c[k], (int, float)) or c[k] <= 0:
            raise ValueError(f"{k} must be a positive number")
    for k in ("max_total_risk_pct", "duplicate_atr"):
        if not isinstance(c[k], (int, float)) or isinstance(c[k], bool) or c[k] <= 0:
            raise ValueError(f"{k} must be a positive number")
    if not isinstance(c["trade_sessions"], list) or not set(c["trade_sessions"]) <= {"ASIA", "LONDON", "LONDON_NY", "NEW_YORK", "OFF"}:
        raise ValueError("trade_sessions must be a list drawn from ASIA, LONDON, LONDON_NY, NEW_YORK, OFF ([] = all)")
    if not isinstance(c["tier_cap"], dict) or any(k not in PBK.CLASSES or v not in ("A", "B", "C") for k, v in c["tier_cap"].items()):
        raise ValueError(f"tier_cap must map a playbook in {PBK.CLASSES} to A, B or C")
    if not isinstance(c["breakout_retest_minutes"], int) or not 5 <= c["breakout_retest_minutes"] <= 240:
        raise ValueError("breakout_retest_minutes must be an integer 5..240")
    if set(c["tiers"]) != {"A", "B", "C"}:
        raise ValueError("tiers needs exactly A, B and C")
    for t in ("A", "B", "C"):
        spec = c["tiers"][t]
        r, m = spec.get("risk_pct"), spec.get("min_confidence")
        if not isinstance(r, (int, float)) or isinstance(r, bool) or not 0 < r <= 2:
            raise ValueError(f"tiers.{t}.risk_pct must be > 0 and <= 2")
        if not isinstance(m, int) or isinstance(m, bool) or not 0 <= m <= 100:
            raise ValueError(f"tiers.{t}.min_confidence must be an integer 0..100")
    if not isinstance(c["tiers"]["C"].get("min_lot_if_below", True), bool):
        raise ValueError("tiers.C.min_lot_if_below must be true or false")
    if not c["tiers"]["A"]["risk_pct"] >= c["tiers"]["B"]["risk_pct"] >= c["tiers"]["C"]["risk_pct"]:
        raise ValueError("tiers.risk_pct must be A >= B >= C")
    if not c["tiers"]["A"]["min_confidence"] >= c["tiers"]["B"]["min_confidence"] >= c["tiers"]["C"]["min_confidence"]:
        raise ValueError("tiers.min_confidence must be A >= B >= C")
    for k in ("max_active_orders", "max_open_positions", "max_pending_orders", "max_orders_per_analysis",
              "max_same_direction_positions", "max_orders_per_setup_per_day", "max_trades_per_day", "analysis_every_minutes",
              "monitor_interval_seconds", "max_order_lifetime_hours", "min_order_lifetime_minutes"):
        if not isinstance(c[k], int) or isinstance(c[k], bool) or c[k] <= 0:
            raise ValueError(f"{k} must be a positive integer")
    if c["analysis_every_minutes"] % 15:
        raise ValueError("analysis_every_minutes must be a multiple of 15")
    for k in ("require_demo", "require_trader_terminal", "require_evidence", "block_on_mtf_opposition",
              "allow_market_orders", "analyse_on_start", "zone_trigger", "telegram_errors", "user_plan_auto_study"):
        if not isinstance(c[k], bool):
            raise ValueError(f"{k} must be true or false")

    state = Path(c["state_directory"])
    if not state.is_absolute():
        state = ROOT / state
    state.mkdir(parents=True, exist_ok=True)
    c["state_path"] = state
    prompt = Path(c["prompt_file"])
    c["prompt_path"] = prompt if prompt.is_absolute() else ROOT / prompt
    c["symbol"] = v6["provider_options"]["broker_symbol"]
    return c
