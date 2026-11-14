#!/usr/bin/env python3
"""
Gold AUTO-TRADER (demo) — runs next to the V6.1.3 signal bot and reuses its MT5 data layer.

  python src/auto_trader.py --check        connection, clock offset, price, calendar, market hours (no Claude, no orders)
  python src/auto_trader.py --once --dry-run   one full analysis; shows what it WOULD send, sends nothing
  python src/auto_trader.py --dry-run      run continuously, never send orders
  python src/auto_trader.py                run continuously and trade the DEMO account
  python src/auto_trader.py --report       A vs B results
  python src/auto_trader.py --excursions [--days 21] [--spread 0.3]   1.15: MFE/MAE, protection replay, zone score
  python src/auto_trader.py --zone-study [--days 30] [--spread 0.3]   Astra LIMIT ideas: limit vs 5M/1M zone reaction
  python src/auto_trader.py --test-order   place and delete one tiny demo order (checks order sending)
  python src/auto_trader.py --self-test    offline tests (no MT5, no API)
  python tools/replay.py --days 120        historical replay: old vs new rules (no API, no orders)

Scripts: scripts/run_trader.ps1, scripts/trader_check.ps1, scripts/trader_report.ps1
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import gold_monitor as GM  # noqa: E402
from autotrader import VERSION, settings  # noqa: E402

UTC = timezone.utc


def setup_logging(c):
    fmt = logging.Formatter("%(asctime)sZ | %(levelname)s | %(name)s | %(message)s")
    logging.Formatter.converter = time.gmtime
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(c["state_path"] / "trader.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    sh = logging.StreamHandler(sys.stdout)
    for h in (fh, sh):
        h.setFormatter(fmt)
        root.addHandler(h)
    for noisy in ("httpx", "anthropic", "urllib3", "gold.v5"):     # gold.v5: per-fetch candle lines
        logging.getLogger(noisy).setLevel(logging.WARNING)


def self_test() -> int:
    import os
    import unittest
    os.environ["TELEGRAM_BOT_TOKEN"] = os.environ["TELEGRAM_CHAT_ID"] = ""   # 1.7: tests never post to Telegram
    tests = HERE.parent / "tests"
    sys.path.insert(0, str(tests))
    suite = unittest.defaultTestLoader.loadTestsFromNames(["test_auto_trader", "test_v16", "test_v17", "test_v18", "test_user_plans", "test_v110", "test_v1106", "test_ict", "test_v111", "test_v1111", "test_v1114", "test_v1116", "test_v112", "test_v113", "test_v1131", "test_v114", "test_v1141", "test_v115", "test_v116", "test_v117", "test_v118", "test_v119", "test_v120", "test_v121", "test_v122", "test_v123"])
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


def check(c) -> int:
    from autotrader.market import Market
    from autotrader.validator import Validator
    from autotrader.state import State
    m = Market(c)
    info = m.connect()
    now = datetime.now(UTC)
    q = m.quote()
    print("\n=== AUTO-TRADER CHECK (nothing is analysed or sent) ===")
    print(f"Account        {info['login']} on {info['server']}  ->  {'DEMO' if info['demo'] else 'REAL'}")
    print(f"Terminal       {info['terminal_path']}")
    print(f"Algo Trading   {'ON' if info['algo_trading'] else 'OFF  <- turn it on in the demo terminal'}")
    print(f"Balance        {info['balance']:.2f} {info['currency']}")
    print(f"Broker clock   UTC{info['offset_hours']:+.1f}h (auto, from live ticks)")
    print(f"Price          bid {q['bid']}  ask {q['ask']}  spread {q['spread']:.2f}  age {q['age']:.1f}s  (UTC {q['time']:%H:%M:%S})")
    si = m.symbol_info()
    print(f"Symbol         {c['symbol']}: 1 lot moves {si.trade_tick_value / si.trade_tick_size:.0f} {info['currency']} per $1, "
          f"min lot {si.volume_min}, step {si.volume_step}, expiry on orders {'yes' if si.expiration_mode & 4 else 'no'}")
    m.refresh_calendar(force=True)
    if m.calendar:
        ev = [e for e in m.upcoming_events(now, 72) if e["impact"] in ("HIGH", "HOLIDAY")]
        print(f"Calendar       OK ({len(m.calendar['events'])} USD events, exported {m.calendar['fetched_at'][:19]}Z)")
        for e in ev[:6]:
            print(f"                 {e['time_utc'][:16]}Z  {e['impact']:<7} {e['title']}")
    else:
        print(f"Calendar       NOT AVAILABLE: {m.calendar_error}")
        print("               -> attach mql/mql5/GoldCalendarBridge.mq5 to a chart (either terminal).")
    print(f"Market window  {m.trading_window(now) or 'open for new orders'}")
    print(f"News lock      {m.news_block(now) or 'none'}")
    bar = m.last_closed_15m_open()
    print(f"Last 15M bar   opened {bar:%Y-%m-%d %H:%M} UTC (closed {(now - bar).total_seconds() / 60 - 15:.0f} min ago)")
    v = Validator(c, m, State(c["state_path"]))
    print(f"Account type   {'NETTING: one order at a time' if v.netting() else 'hedging'} | "
          f"up to {v.max_active()} orders working, total risk max {c['max_total_risk_pct']}%")
    risk, problem = v.open_risk()
    print(f"Open risk      {risk:.2f} {info['currency']} on the whole account (all bots + manual trades)"
          + (f"  <- BLOCKS NEW ORDERS: {problem}" if problem else ""))
    print(f"Magic numbers  swing {c['magic']} | scalp {c['magic_scalp']} (reserved, scalper not active) | lab {c['magic_lab']}")
    sw = ", ".join(c["straddle_events"])
    print("News straddle  " + (f"ON before: {sw} ({c['straddle_place_minutes']} min before, lab only)" if c["news_straddle"] else "off"))
    print("Lab sizing     " + (f"{c['lab_risk_pct']}% of equity per lab order, lab open risk capped at {c['lab_max_total_risk_pct']}%"
                              if c["lab_risk_pct"] > 0 else "minimum lot (0 = measurement only)"))
    print(f"Lab mode       {'ON: every rejected idea is traded at ' + (str(c['lab_lots']) if c['lab_lots'] else 'the minimum lot') + ' and tagged with the rules it broke (DEMO only)' if c['lab_mode'] else 'off'}")
    print(f"Counter-trend  {'ON' if c['counter_trend_enabled'] else 'OFF'}: {'/'.join(c['counter_timeframes'])} zones "
          f"tested >= {c['counter_min_tests']}x, confirmed (rejection / sweep / CHoCH), B size")
    pm = c["playbook_modes"]
    print(f"Playbooks      " + " | ".join(f"{k} {v}" for k, v in pm.items())
          + f" | Python session setups {c['python_session_setups']} | expansion filter {c['expansion_filter']}")
    try:
        from autotrader import regime as RG, sessions as SS
        hist = m.history()
        st = RG.market_state(hist, now, dict(c["regime"], regime_window=1000))
        snap = SS.snapshot(hist["15min"], hist["1h"], now, c["sessions"], c["opening_range_minutes"])
        print(f"Market state   {RG.summary(st)}")
        print(f"Session        {snap['session']} | " + ", ".join(f"{x['name']} {x['price']}" for x in snap["levels"]))
    except Exception as exc:  # noqa: BLE001
        print(f"Market state   not available: {GM.safe_error(exc)[:150]}")
    gate = v.gates(now)
    print(f"Would trade?   {'yes' if not gate else 'not now: ' + gate}")
    t = c["tiers"]
    print(f"Tiers (1.7)    A {t['A']['risk_pct']}% (conf >= {t['A']['min_confidence']}) | B {t['B']['risk_pct']}% "
          f"(>= {t['B']['min_confidence']}) | C {t['C']['risk_pct']}% (>= {t['C']['min_confidence']}, min lot ok) "
          f"| max {c['max_same_direction_positions']} at risk per direction")
    print(f"Grades         {', '.join(c['execute_grades'])} | signal-bot signals: A {c['risk_percent']['A_TRADE']}%  B {c['risk_percent']['B_TRADE']}%")
    if c["source"] == "signal_bot":
        from autotrader.bridge import Bridge
        b = Bridge(c, State(c["state_path"]))
        a, age = b.analysis(now)
        print(f"Source         signal bot (no Claude calls by the trader): "
              f"{'signals' if c['signal_bot_signals'] else ''}{' + ' if c['signal_bot_signals'] and c['signal_bot_ideas'] else ''}"
              f"{'ideas' if c['signal_bot_ideas'] else ''}")
        if a is None:
            print(f"Signal bot     NO ANALYSIS FOUND in {b.folder}  <- start scripts\\run.ps1")
        else:
            fresh = "OK" if age <= c["idea_max_age_minutes"] else "TOO OLD: is the signal bot running?"
            ideas = a.get("ideas") or []
            print(f"Signal bot     last analysis {age:.0f} min ago ({fresh}), {len(ideas)} idea(s)")
            for i in ideas[:3]:
                print(f"                 {i['direction']} {i.get('entry_type')} {i['entry_low']}-{i['entry_high']} "
                      f"stop {i['invalidation']} TP1 {i['targets'][0]} | {i.get('confidence')}% {i.get('risk')}")
        print(f"Signals file   {b.signal_path.name}: {'present' if b.signal_path.exists() else 'none yet (created at the first signal)'}")
    else:
        print(f"Source         own Claude analysis ({c['anthropic_model']})")
    return 0


def test_order(c) -> int:
    """Place a minimum-size BUY_LIMIT far below the price on the DEMO account, then delete it.
    Proves that order sending works before the first real idea arrives."""
    from autotrader.market import Market
    m = Market(c)
    info = m.connect()
    if not info["demo"]:
        print("REFUSED: not a demo account.")
        return 1
    k, q, si = m.const, m.quote(), m.symbol_info()
    price = round(q["bid"] - 300, si.digits)
    req = {"action": k("TRADE_ACTION_PENDING"), "symbol": c["symbol"], "volume": si.volume_min,
           "type": k("ORDER_TYPE_BUY_LIMIT"), "price": price, "sl": price - 20, "tp": price + 60,
           "deviation": 10, "magic": c["magic"] + 1, "comment": "TEST-ORDER",
           "type_filling": k("ORDER_FILLING_RETURN")}
    if si.expiration_mode & 4:
        req.update(type_time=k("ORDER_TIME_SPECIFIED"),
                   expiration=m.utc_to_server(datetime.now(UTC).timestamp() + 1200))
    else:
        req["type_time"] = k("ORDER_TIME_GTC")
    print(f"\nPlacing TEST BUY_LIMIT {si.volume_min} lot @ {price} (300 below the price, cannot fill) ...")
    try:
        res = m.order_send(req)
    except RuntimeError as exc:
        print(f"FAILED: {exc}")
        return 1
    print(f"Broker answer: retcode {res.retcode} ({getattr(res, 'comment', '')}), ticket {res.order}, "
          f"order_send form: {Market._send_form}")
    if res.retcode not in (k("TRADE_RETCODE_PLACED"), k("TRADE_RETCODE_DONE")):
        print("FAILED: the broker refused the order (see retcode above).")
        return 1
    rm = m.order_send({"action": k("TRADE_ACTION_REMOVE"), "order": res.order})
    print(f"Deleted again: retcode {rm.retcode} ({getattr(rm, 'comment', '')})")
    print("OK: the trader can place and delete orders on this demo account.")
    return 0


def main():
    if "--self-test" not in sys.argv:
        GM.load_secrets()                  # config/secrets.env -> environment (1.7.2); never for the tests
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config/auto_trader_config.json")
    p.add_argument("--check", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="analyse and validate, never send orders")
    p.add_argument("--once", action="store_true", help="one analysis, then exit")
    p.add_argument("--report", action="store_true")
    p.add_argument("--excursions", action="store_true", help="1.15: closed trades replayed on M1 (protection, zone score)")
    p.add_argument("--zone-study", action="store_true", help="Astra LIMIT ideas replayed: limit vs 5M/1M reaction")
    p.add_argument("--days", type=int, default=21)
    p.add_argument("--spread", type=float, default=0.3)
    p.add_argument("--test-order", action="store_true", help="place and delete one tiny far-away demo order")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()
    GM.utf8_console()
    if a.self_test:
        raise SystemExit(self_test())

    cfg_path = Path(a.config)
    if not cfg_path.is_absolute():
        cfg_path = GM.ROOT / cfg_path
    c = settings.load(cfg_path if cfg_path.exists() else None)
    setup_logging(c)
    logging.getLogger("autotrader").info("=== AUTO-TRADER %s (V6 %s data layer) %s ===", VERSION, GM.VERSION,
                                         "DRY RUN" if a.dry_run else "DEMO TRADING")
    if a.check:
        try:
            raise SystemExit(check(c))
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"\nCHECK FAILED: {GM.safe_error(exc)}")
            print("Common causes: demo terminal closed or not logged in, market closed (weekend / daily "
                  "17:00-18:00 New York break), TRADER_MT5_PATH wrong, or a REAL account.")
            raise SystemExit(1)
    if a.test_order:
        raise SystemExit(test_order(c))
    if a.report:
        from autotrader.market import Market
        from autotrader.report import run
        from autotrader.state import State
        m = Market(c)
        m.connect_light()
        print(run(m, State(c["state_path"]), c["state_path"] / "trades.csv"))
        return
    if a.excursions:
        from autotrader.excursions import run as excursions
        from autotrader.market import Market
        from autotrader.state import State
        m = Market(c)
        m.connect_light()
        print(excursions(m, State(c["state_path"]), c["state_path"] / "excursions.csv", a.days, a.spread,
                         c.get("trade_protect")))
        return
    if a.zone_study:
        from autotrader.market import Market
        from autotrader.zone_study import run as zone_study
        m = Market(c)
        m.connect_light()
        folder = Path(c["v6"]["state_directory"])
        folder = folder if folder.is_absolute() else GM.ROOT / folder
        print(zone_study(m, folder / "astra_memory.sqlite3", c["state_path"] / "zone_study.csv", a.days, a.spread,
                         c["min_rr"], c.get("trade_protect"), c["idea_order_hours"]))
        return
    import os
    if c["source"] == "own_analysis" and not os.getenv("ANTHROPIC_API_KEY"):
        p.error("ANTHROPIC_API_KEY is not set (setx ANTHROPIC_API_KEY \"sk-ant-...\", then open a new window)")
    from autotrader.app import App
    try:
        App(c, dry_run=a.dry_run).run(once=a.once)
    except KeyboardInterrupt:
        logging.getLogger("autotrader").info("Stopped. Open orders/positions stay at the broker with their stop and target.")


if __name__ == "__main__":
    main()
