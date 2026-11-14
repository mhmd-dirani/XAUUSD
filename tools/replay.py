#!/usr/bin/env python3
r"""
Historical replay runner (rewritten 29 Oct 2026 after the original tools\replay.py was overwritten by mistake;
the public functions tests\test_v16.py relies on - synthetic(), fetch_mt5() - are provided again).

    python tools\replay.py --days 120            fetch 1-minute XAUUSD from the MT5 terminal, cache it, replay
    python tools\replay.py --days 720            two years, if the terminal's "Max bars in chart" allows it
    python tools\replay.py --cached               replay the cached replay_data\XAUUSD_M1.csv (no MT5 needed)
    python tools\replay.py --cached --title "after 1.10.6"
    python tools\replay.py --days 30 --ticks      also fetch ticks to settle same-minute stop+target bars (slow)

Output: replay_data\XAUUSD_M1.csv (cache), replay_data\replay_report.md, replay_data\replay_trades.csv.
No AI calls, no orders. The variants and the engine live in src\autotrader\replay.py; this file only fetches the
candles and writes the files. MT5 stores candles in SERVER time; they are converted to UTC with the broker's rule
(New York + 7 h = UTC+3 in summer, UTC+2 in winter; `_offset_rule` in the config, default "ny+7").
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from autotrader import replay as RP  # noqa: E402

UTC = timezone.utc
DATA_DIR = ROOT / "replay_data"
M1_CSV = DATA_DIR / "XAUUSD_M1.csv"


# ---------------------------------------------------------------------------------- MT5 fetch
def _mt5_kwargs() -> dict:
    """initialize() arguments from the trader's environment (TRADER_MT5_* first, then MT5_*)."""
    kw = {}
    for key, name in (("path", "PATH"), ("login", "LOGIN"), ("password", "PASSWORD"), ("server", "SERVER")):
        v = os.getenv("TRADER_MT5_" + name) or os.getenv("MT5_" + name)
        if v:
            kw[key] = int(v) if key == "login" else v
    return kw


def _server_to_utc(server_ts: float, rule) -> datetime:
    """A server-clock timestamp (labelled as if UTC) -> real UTC, using the broker offset rule."""
    srv = datetime.fromtimestamp(int(server_ts), UTC)
    return srv - RP.server_offset(srv, rule)


def fetch_mt5(c: dict, days: int, use_ticks: bool = False):
    """(m1, ticks, point) from the connected terminal. m1 = [{t (UTC), o, h, l, c, s ($ spread)}] oldest first;
    ticks = None, or a callable (start_utc, end_utc) -> [(t, bid, ask)] when use_ticks is on."""
    import MetaTrader5 as mt5
    symbol, rule = c["symbol"], c.get("_offset_rule", "ny+7")
    if not mt5.initialize(**_mt5_kwargs()):
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
    mt5.symbol_select(symbol, True)
    si = mt5.symbol_info(symbol)
    point = float(getattr(si, "point", 0.01) or 0.01)
    now = datetime.now(UTC)
    rates = None
    if hasattr(mt5, "copy_rates_from_pos"):                     # deepest history: counts from the newest bar
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, days * 1440)
    if rates is None or len(rates) == 0:
        srv_now = now + RP.server_offset(now, rule)
        rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, srv_now - timedelta(days=days + 3), srv_now + timedelta(days=1))
    if rates is None or len(rates) == 0:
        raise SystemExit(f"MT5 returned no 1-minute candles: {mt5.last_error()} (terminal logged in? 'Max bars in chart' "
                         f"large enough?)")
    names = set(rates.dtype.names or ())
    out = []
    for r in rates:
        out.append({"t": _server_to_utc(r["time"], rule), "o": float(r["open"]), "h": float(r["high"]),
                    "l": float(r["low"]), "c": float(r["close"]),
                    "s": round(float(r["spread"]) * point, 5) if "spread" in names else None})
    out.sort(key=lambda b: b["t"])
    cutoff = now - timedelta(days=days)
    out = [b for b in out if b["t"] >= cutoff] or out
    ticks = None
    if use_ticks:
        def ticks(start: datetime, end: datetime):
            s = start + RP.server_offset(start, rule)
            e = end + RP.server_offset(end, rule)
            got = mt5.copy_ticks_range(symbol, s, e, mt5.COPY_TICKS_ALL)
            if got is None:
                return []
            return [(_server_to_utc(t["time"], rule), float(t["bid"]), float(t["ask"])) for t in got
                    if t["bid"] > 0 and t["ask"] > 0]
    return out, ticks, point


# ---------------------------------------------------------------------------------- synthetic candles (tests)
def synthetic(days: int = 56, seed: int = 7, start: datetime | None = None, spread: float = 0.3) -> list[dict]:
    """Deterministic 1-minute candles for the offline tests: a random walk with regime drift, session volatility and
    weekend gaps, starting Monday 1 June 2026. Same seed -> identical bars."""
    rng = random.Random(seed)
    t = start or datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    end = t + timedelta(days=days)
    price, drift, out = 3300.0, 0.0, []
    regime_left = 0
    while t < end:
        wd, h = t.weekday(), t.hour
        closed = wd == 5 or (wd == 6 and h < 22) or (wd == 4 and h >= 21) or h == 21
        if closed:
            t += timedelta(minutes=1)
            continue
        if regime_left <= 0:                                   # a new drift regime every 1-3 days
            regime_left = rng.randint(1440, 3 * 1440)
            drift = rng.choice((-0.025, -0.012, 0.0, 0.0, 0.012, 0.025))
        regime_left -= 1
        vol = 0.22 if h < 7 else 0.45 if h < 16 else 0.3      # Asia / London+NY / late NY
        o = price
        steps = [o]
        for _ in range(4):
            steps.append(steps[-1] + drift + rng.gauss(0.0, vol))
        c = steps[-1]
        hi, lo = max(steps) + abs(rng.gauss(0, 0.05)), min(steps) - abs(rng.gauss(0, 0.05))
        out.append({"t": t, "o": round(o, 2), "h": round(hi, 2), "l": round(lo, 2), "c": round(c, 2), "s": spread})
        price = c
        t += timedelta(minutes=1)
    return out


# ---------------------------------------------------------------------------------- CLI
def _market_hours(c):
    import gold_monitor as GM
    return GM.Sessions(c["v6"]).is_open


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=120, help="days of 1-minute history to fetch (default 120)")
    ap.add_argument("--cached", action="store_true", help="use replay_data\\XAUUSD_M1.csv instead of fetching from MT5")
    ap.add_argument("--ticks", action="store_true", help="also use ticks to settle same-minute stop+target bars (slow)")
    ap.add_argument("--config", default="config/auto_trader_config.json")
    ap.add_argument("--title", default="")
    a = ap.parse_args()

    import gold_monitor as GM
    from autotrader import settings
    if not a.cached:
        GM.load_secrets()
    GM.utf8_console()
    cfg_path = Path(a.config)
    cfg_path = cfg_path if cfg_path.is_absolute() else ROOT / cfg_path
    c = settings.load(cfg_path if cfg_path.exists() else None)
    c.setdefault("_offset_rule", "ny+7")
    DATA_DIR.mkdir(exist_ok=True)

    ticks = None
    if a.cached:
        if not M1_CSV.exists():
            raise SystemExit(f"{M1_CSV} not found; run without --cached first")
        m1 = RP.read_m1_csv(M1_CSV)
        print(f"Using cached {len(m1):,} bars {m1[0]['t']:%Y-%m-%d} -> {m1[-1]['t']:%Y-%m-%d} ({M1_CSV})")
    else:
        print(f"Fetching {a.days} days of 1-minute {c['symbol']} from MT5 ...")
        m1, ticks, _ = fetch_mt5(c, a.days, a.ticks)
        RP.write_m1_csv(M1_CSV, m1)
        print(f"  {len(m1):,} bars {m1[0]['t']:%Y-%m-%d} -> {m1[-1]['t']:%Y-%m-%d} (saved to {M1_CSV})")

    cfg = RP.cfg_from_settings(c)
    started = time.time()
    last = {"k": -1}

    def progress(k, n):
        if k != last["k"]:
            last["k"] = k
            print(f"\r  replay {k:,}/{n:,} 15M candles ({time.time() - started:.0f}s)", end="", flush=True)

    rp = RP.Replay(m1, cfg, ticks=ticks, sessions_cfg=c["sessions"], market_hours=_market_hours(c), progress=progress).run()
    print()
    title = a.title or f"XAUUSD MT5 history, last {a.days} days"
    text = RP.report(rp, title)
    (DATA_DIR / "replay_report.md").write_text(text, encoding="utf-8")
    RP.trades_csv(rp, DATA_DIR / "replay_trades.csv")
    cut = text.find("## In-sample")
    print(text[:cut] if cut > 0 else text)
    print(f"\nFull report: {DATA_DIR / 'replay_report.md'}")
    print(f"Trades:      {DATA_DIR / 'replay_trades.csv'}")


if __name__ == "__main__":
    main()
