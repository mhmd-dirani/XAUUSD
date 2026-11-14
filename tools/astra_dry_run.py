#!/usr/bin/env python3
"""Astra V6 dry run — one real Astra market map on the latest CLOSED candles, nothing sent.

Use it while the market is closed (e.g. the weekend) to check, before going live:
  * that MT5 candles load and the market memory builds (zones you can compare with your chart);
  * that your Astra model returns the V6 schema (executable map + analyst narrative);
  * what the TRADE IDEA / STRUCTURE WATCH Telegram messages would look like;
  * how many tokens one hourly map costs and how long it takes.

Nothing is sent to Telegram. Nothing is written to the live state folder: everything goes to
--state (default runtime_dryrun/). Read-only MT5 calls only; no orders, ever.

Examples (Windows, from the project root, MT5 terminal running and logged in):
  .\\.venv\\Scripts\\python.exe tools\\astra_dry_run.py --offset-hours 3 --no-ai     # free: data + memory only
  .\\.venv\\Scripts\\python.exe tools\\astra_dry_run.py --offset-hours 3             # one Astra call
  .\\.venv\\Scripts\\python.exe tools\\astra_dry_run.py --raw runtime_dryrun\\astra_response.json   # re-render, free

--offset-hours: your broker's server time minus UTC (GivTrade is usually +3 in summer, +2 in winter;
the MT5 Market Watch clock vs UTC tells you). Needed while the market is closed, because the live
monitor infers it from fresh ticks. A wrong offset shifts every intraday candle, so check it.
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import astra_analysis as AA  # noqa: E402
import astra_anthropic as AN  # noqa: E402
import astra_memory as AM  # noqa: E402
import gold_evidence as EV  # noqa: E402
import gold_monitor as M  # noqa: E402
M.load_secrets()


def section(out, title):
    out.append("")
    out.append("=" * 78)
    out.append(title)
    out.append("=" * 78)


def usage_dict(usage):
    if usage is None:
        return {}
    try:
        return usage.model_dump()
    except Exception:
        return {k: getattr(usage, k, None) for k in ("input_tokens", "output_tokens", "total_tokens")}


def load_calendar(c, state):
    """Live MT5 calendar if possible, else the last runtime/calendar.json snapshot (labelled)."""
    try:
        cal = M.build_provider("calendar", c).fetch()
        return cal, "live MT5 calendar export"
    except Exception as exc:
        reason = f"{type(exc).__name__}: {M.safe_error(exc)[:120]}"
    for folder in (Path(c["state_directory"]), ROOT / "runtime"):
        path = folder / "calendar.json"
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8")), f"saved snapshot {path} (live fetch failed: {reason})"
    return None, f"none (live fetch failed: {reason})"


def zone_rows(memory, price, now, history):
    atr = memory.daily_atr(history) or 1.0
    rows = []
    for z in memory.zones.values():
        d = memory.describe(z, price, now, atr)
        d["important"] = memory.important(z)
        d["reactions"] = z.get("reactions", [])
        rows.append(d)
    rows.sort(key=lambda r: -r["high"])
    return rows, atr


def build_payload(c, history, evidence, memory, price, quote_time, born, now, calendar, previous_map):
    sessions = M.Sessions(c)
    data = {"now": M.iso(born), "quote": {"price": price, "time": quote_time,
                                          "source": "last closed 15min candle (dry run: real broker candles, no live bid/ask read)"},
            "market": M.market_payload(history), "calendar": calendar,
            # a saved snapshot is judged for its EVENTS at analysis time, not for its age (dry run only)
            "calendar_block": (M.calendar_block(dict(calendar, fetched_at=M.iso(born)), born, c)
                               if calendar else "Economic calendar unavailable"),
            "previous_map": previous_map, "minimum_rr": c["minimum_rr"],
            "touch_candles": c["touch_candles"], "window_bars": c["touch_window_bars"],
            "playbook_touch_candles": c["playbook_touch_candles"],
            "touch_mode": c["touch_mode"], "touch_exit_bars": c["touch_exit_bars"],
            "language": c["telegram_language"], "session": sessions.key(born),
            "smc_evidence": EV.compact(evidence)}
    if memory:
        data["market_memory"] = memory.payload(price, now, history, memory.track_record(now))
    name = c.get("playbook_file") or ""
    playbook = AA.load_playbook(Path(name) if Path(name).is_absolute() else ROOT / name) if name else None
    if playbook:
        data["trader_playbook"] = playbook
    return data


def call_astra(c, payload):
    if c.get("analyst_provider") == "anthropic":
        return AN.dry_run_map(M, c, payload)
    from openai import OpenAI
    client = OpenAI(timeout=min(c["ai_deadline_seconds"], 360), max_retries=0)
    saved = M.load_saved_agent(client)
    instructions = M.astra_instructions(saved, "map")
    started = time.time()
    result = M.response_json(client, saved["model"], M.MAP_SCHEMA, instructions, payload,
                             c["astra_reasoning"], 16000)
    return result, saved["model"], usage_dict(M.LAST_USAGE), time.time() - started, len(instructions)


def run(args):
    c = M.load_config(args.config)
    if args.offset_hours is not None:
        c["provider_options"]["mt5_server_utc_offset_minutes"] = int(round(args.offset_hours * 60))
    if args.language:
        c["telegram_language"] = args.language
    if args.analyst:
        c["analyst_provider"] = AN.normalize(args.analyst)
    state = Path(args.state)
    state.mkdir(parents=True, exist_ok=True)
    out = [f"ASTRA V6 DRY RUN — Gold Monitor V{M.VERSION} — nothing sent to Telegram",
           f"Run at {M.iso(M.utcnow())} | state folder: {state.resolve()}"]
    now = M.utcnow()

    # ------------------------------------------------------------------ data
    section(out, "1. CLOSED CANDLES")
    if args.history:
        history = json.loads(Path(args.history).read_text(encoding="utf-8"))
        out.append(f"Loaded from {args.history}")
    else:
        history = M.build_provider("candles", c).snapshot()
        M.atomic(state / "history.json", history)
    for tf in M.TF:
        bars = history.get(tf) or []
        out.append(f"{tf:>6}: {len(bars):4d} bars | last {bars[-1]['time'] if bars else '—'} "
                   f"close {bars[-1]['close'] if bars else '—'}")
    last15 = history["15min"][-1]
    price = float(last15["close"])
    # Analysis time = the latest close of ANY intraday frame (on Friday the 18:00 4H bar is labelled to
    # close at 22:00 although trading stopped at 21:00). Using the 15M close made that 4H bar look
    # "unclosed" and dropped all 4H evidence in the first real dry run.
    born = min(now, max(M.last_close(history, tf) for tf in ("15min", "1h", "4h")))
    out.append(f"Reference price: {price:g} (last 15M close) | analysis time {M.iso(born)}")

    evidence = EV.build_evidence(history, born)
    missing = [tf for tf, f in evidence["frames"].items() if not f.get("available")]
    out.append(f"SMC evidence: {len(evidence['index'])} items" + (f"; unavailable on {missing}" if missing else ""))

    # ------------------------------------------------------------------ memory
    section(out, "2. MARKET MEMORY (compare these zones with your own chart)")
    memory = AM.MarketMemory(state / "astra_memory.sqlite3")
    memory.update(history, now)
    memory.sync_smc(evidence, now)
    rows, atr = zone_rows(memory, price, now, history)
    by = {}
    for r in rows:
        by.setdefault((r["timeframe"], r["status"]), 0)
        by[(r["timeframe"], r["status"])] += 1
    out.append(f"{len(rows)} zones stored | daily ATR {atr:.2f} | "
               + ", ".join(f"{tf}/{st}: {n}" for (tf, st), n in sorted(by.items())))
    out.append("Zones within 4 daily ATR of price (highest first; * = important = can trigger the sensor):")
    marked = False
    for r in rows:
        if r["status"] not in ("ACTIVE", "BROKEN", "FLIPPED", "SWEPT") or (r["distance_atr"] or 0) > 4:
            continue
        if not marked and r["high"] < price:
            out.append(f"{'':4}---------------- price {price:g} ----------------")
            marked = True
        react = f"{sum(r['reactions']) / len(r['reactions']):.1f}" if r["reactions"] else "—"
        out.append(f"{'*' if r['important'] else ' '} {r['id']:<14} {r['timeframe']:>5} {r['role']:<10} "
                   f"{r['low']:>9.2f}–{r['high']:<9.2f} {r['status']:<8} tests {r['tests']:<2} "
                   f"react {react:>4} ATR  created {r['created_at'][:16]}")
    if not marked:
        out.append(f"{'':4}---------------- price {price:g} ----------------")
    try:
        with open(state / "zones.csv", "w", newline="", encoding="utf-8") as f:
            cols = ["id", "timeframe", "kind", "role", "original_role", "status", "low", "high", "tests",
                    "last_tested_at", "avg_reaction_atr", "created_at", "source", "important", "distance_atr"]
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        out.append(f"All zones: {state / 'zones.csv'}")
    except PermissionError:
        out.append("zones.csv NOT written: it is open in another program (close Excel and run again)")

    # ------------------------------------------------------------------ payload
    section(out, "3. ASTRA PAYLOAD")
    calendar, cal_source = load_calendar(c, state)
    out.append(f"Calendar: {cal_source}")
    previous = None
    live_map = ROOT / "runtime" / "market_map.json"
    if live_map.is_file():
        try:
            previous = {k: v for k, v in json.loads(live_map.read_text(encoding="utf-8"))["map"].items()
                        if k != "analysis"}
        except Exception:
            previous = None
    payload = build_payload(c, history, evidence, memory, price, last15["time"], born, now, calendar, previous)
    text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    M.atomic(state / "payload.json", payload)
    out.append("Trader playbook: " + (f"{len(payload['trader_playbook']['principles'])} principles included"
                                      if payload.get("trader_playbook") else "none (run tools/learn_lessons.py)"))
    out.append(f"Payload {len(text):,} characters (~{len(text) // 4:,} tokens). Memory part: "
               f"{len(json.dumps(payload.get('market_memory'), ensure_ascii=False)):,} characters.")

    # ------------------------------------------------------------------ Astra
    section(out, "4. ASTRA RESPONSE")
    if args.raw:
        raw = json.loads(Path(args.raw).read_text(encoding="utf-8"))
        out.append(f"Re-rendering saved response {args.raw} (no AI call, no cost)")
    elif args.no_ai:
        out.append("--no-ai: Astra not called. Data, memory and payload checks above are complete.")
        return finish(out, state, memory)
    else:
        who = "Claude" if c.get("analyst_provider") == "anthropic" else "Astra (OpenAI)"
        print(f"Data and memory ready. Waiting for {who}'s market map: this usually takes 1-6 minutes and "
              "prints nothing until it finishes. Ctrl+C cancels.", flush=True)
        try:
            raw, model, usage, secs, ins = call_astra(c, payload)
        except Exception as exc:
            out.append(f"ASTRA CALL FAILED: {type(exc).__name__}: {M.safe_error(exc)[:600]}")
            out.append("If this is a schema/validation error the live bot would reject the whole hourly map. "
                       "Share this report so the contract can be adjusted.")
            usage = usage_dict(M.LAST_USAGE)
            if usage:
                out.append(f"Tokens used before failure: {usage}")
            return finish(out, state, memory, code=2)
        M.atomic(state / "astra_response.json", raw)
        out.append(f"Model {model} | {secs:.0f}s | instructions {ins:,} characters")
        out.append("Tokens: " + json.dumps({k: usage.get(k) for k in ("input_tokens", "output_tokens", "total_tokens")})
                   + (f" | reasoning {usage.get('output_tokens_details', {}).get('reasoning_tokens')}"
                      if isinstance(usage.get("output_tokens_details"), dict) else ""))
        out.append(f"Saved: {state / 'astra_response.json'} (re-render free with --raw)")

    # ------------------------------------------------------------------ executable map
    section(out, "5. EXECUTABLE MAP (V5 rules, unchanged)")
    out.append(f"Bias: {raw.get('bias')}")
    out.append(f"Summary: {raw.get('summary', '')[:500]}")
    out.append(f"Levels {len(raw.get('levels', []))} | setups {len(raw.get('setups', []))}")
    try:
        data = M.validate_map(copy.deepcopy(raw), history, c, born, born, evidence)
        out.append("Map ACCEPTED by Python validation." + ("" if data["setups"] else
                                                          f" No setups: {raw.get('no_setup_reason', '')[:300]}"))
        added = memory.add_astra_levels(data["levels"], history, now)
        if added:
            out.append(f"Astra levels added to memory: {', '.join(added)}")
    except Exception as exc:
        out.append(f"Map REJECTED: {type(exc).__name__}: {M.safe_error(exc)[:400]}")
        out.append("(The live bot would discard this map's setups; the analyst narrative below is still kept.)")
        data = None

    # ------------------------------------------------------------------ analysis
    section(out, "6. ASTRA ANALYST LAYER (V6)")
    recent = history["1h"][-150:]
    cal_for_block = dict(calendar, fetched_at=M.iso(born)) if calendar else None
    block = M.calendar_block(cal_for_block, born, c) if cal_for_block else None
    clean, dropped = AA.validate_analysis(
        raw.get("analysis"), price=price, daily_atr=memory.daily_atr(history), h1_atr=M.frame_atr(history, "1h"),
        price_low=min(b["low"] for b in recent), price_high=max(b["high"] for b in recent),
        memory_ids=set(memory.zones), minimum_rr=c["minimum_rr"],
        mtf_opposed=lambda d: EV.mtf_opposed(d, evidence), calendar_block=block, language=c["telegram_language"],
        zones=AA.anchor_levels([z for z in memory.zones.values() if z["status"] in AM.LIVE],
                               data["levels"] if data else None))
    if clean is None:
        out.append("ANALYSIS MISSING OR INVALID: " + "; ".join(dropped))
        return finish(out, state, memory, code=3)
    out.append(f"Stance: {clean['stance']}" + ("" if clean["ideas"] else "  (WARNING: no trade idea returned)"))
    out.append(f"Market story: {clean['market_story']}")
    out.append(f"Historical context: {clean['historical_context']}")
    if clean["stance"] == "NO_TRADE":
        out.append(f"No-trade reason: {clean['no_trade_reason']}")
    for sc in clean["scenarios"]:
        out.append(f"Scenario {sc['id']} [{sc['likelihood']}] {sc['bias']}: {sc['title']} — trigger "
                   f"{sc['trigger_timeframe']} {sc['trigger_type']} {sc['trigger_level']:g}, objective "
                   f"{sc['objective_level']}, invalidation {sc['invalidation_level']}, {sc['horizon_hours']}h")
    if dropped:
        out.append("Dropped by Python: " + "; ".join(dropped))
    if block:
        out.append(f"NOTE: calendar window active at {M.iso(born)} ({block}) — the live bot would withhold ideas.")
    now_render = born
    for idea in clean["ideas"]:
        section(out, "TELEGRAM PREVIEW — TRADE IDEA")
        zones = [z for z in (memory.zone(i) for i in idea["memory_ids"]) if z]
        out.append(AA.idea_text(idea, clean["market_story"], zones, now_render, c["telegram_language"], price))
    for w in clean["watches"]:
        z = memory.zone(w["memory_id"])
        if z:
            section(out, "TELEGRAM PREVIEW — STRUCTURE WATCH")
            out.append(AA.watch_text(w, z, now_render, c["telegram_language"], price))
    M.atomic(state / "astra_analysis.json", clean)
    return finish(out, state, memory)


def finish(out, state, memory, code=0):
    memory.close()
    report = "\n".join(out)
    (state / "report.txt").write_text(report, encoding="utf-8")
    print(report)
    print(f"\nReport saved: {state / 'report.txt'}")
    return code


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=str(ROOT / "config" / "gold_v5_config.json"))
    p.add_argument("--offset-hours", type=float, help="broker server time minus UTC (needed when market closed)")
    p.add_argument("--state", default=str(ROOT / "runtime_dryrun"), help="output folder (never the live one)")
    p.add_argument("--no-ai", action="store_true", help="skip the Astra call (free)")
    p.add_argument("--raw", help="re-render a saved Astra response JSON instead of calling Astra")
    p.add_argument("--history", help="use a saved history.json instead of MT5")
    p.add_argument("--language", help="override telegram_language (Arabic/English)")
    p.add_argument("--analyst", choices=("astra", "claude", "openai", "anthropic"),
                   help="analyst for this run: astra (OpenAI) or claude")
    args = p.parse_args(argv)
    M.utf8_console()
    if not Path(args.config).is_file():
        args.config = None
    key = AN.analyst_key_name(args.config, args.analyst)
    if not args.no_ai and not args.raw and not os.getenv(key):
        p.error(f"{key} is not set (or use --no-ai / --raw)")
    import logging
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s | %(message)s")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
