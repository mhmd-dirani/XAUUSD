"""
Market access for the auto-trader, built on the V6 MT5 layer.

Reads go through gold_monitor.MT5Session / MT5Candles / MT5CSVCalendar exactly as the signal bot
does (server check, auto broker-clock offset, UTC candles, weekend D1 merge, fail-closed
calendar). Trading calls (order_send, positions, history) are NOT on V6's read-only allow-list,
so they go through Market.trade_call(), under the same connection lock.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone

import gold_monitor as GM
import gold_evidence as EV
import astra_analysis as AA

UTC = timezone.utc
LOG = logging.getLogger("autotrader.market")
S = GM.MT5Session
HISTORY_TFS = ("1week", "1day", "4h", "1h", "15min")   # 1week only feeds the Python evidence


class MarketError(RuntimeError):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


class Market:
    def __init__(self, c: dict):
        self.c = c
        self.v6 = c["v6"]
        self.symbol = c["symbol"]
        self.sessions = GM.Sessions(self.v6)
        self.candles_src = GM.MT5Candles(self.v6)
        self.calendar_src = GM.build_provider("calendar", self.v6)
        self.calendar = None
        self.calendar_checked = 0.0
        self.calendar_error = None

    # ------------------------------------------------------------------ connection
    @staticmethod
    def use_trader_terminal(required: bool) -> None:
        """Point MT5Session at the DEMO terminal for this process only (TRADER_MT5_* variables).
        The V6 signal bot keeps using MT5_* / its own terminal."""
        for k in ("PATH", "LOGIN", "PASSWORD", "SERVER"):
            v = os.getenv("TRADER_MT5_" + k)
            if v:
                os.environ["MT5_" + k] = v
            elif k != "PATH":
                os.environ.pop("MT5_" + k, None)
        if required and not os.getenv("TRADER_MT5_PATH"):
            raise MarketError("TRADER_MT5_PATH is not set. Point it at the DEMO terminal's terminal64.exe "
                              "(see docs/AUTO_TRADER.md) so the trader never attaches to your main terminal.")

    def connect_light(self):
        """Terminal connection only (no live-tick offset probe): for the report at weekends."""
        self.use_trader_terminal(self.c["require_trader_terminal"])
        S.reset()
        return S.api()

    def connect(self) -> dict:
        """Connect, verify DEMO + broker server, select the symbol, confirm the clock offset."""
        self.use_trader_terminal(self.c["require_trader_terminal"])
        S.reset()
        api = S.api()
        account = api.account_info()
        if account is None:
            raise MarketError(f"MT5 account_info failed: {api.last_error()}")
        demo = account.trade_mode == getattr(api, "ACCOUNT_TRADE_MODE_DEMO", 0)
        if self.c["require_demo"] and not demo:
            raise MarketError(f"REFUSING: account {account.login} is NOT a demo account "
                              f"(trade_mode={account.trade_mode}). require_demo is on.")
        S.verify_server(self.v6)
        S.ensure_symbol(self.symbol)
        # Wait for a real price after symbol_select (first tick can be all zeros).
        deadline = time.time() + 15
        while True:
            t = S.tick(self.symbol)
            if t.bid > 0 and t.ask > 0 and t.time_msc > 0:
                break
            if time.time() > deadline:
                raise MarketError(f"No price for {self.symbol} (market closed or wrong symbol?)")
            time.sleep(0.5)
        offset = S.offset_seconds(self.v6, self.symbol)   # needs a NEW tick: fails closed when market is shut
        terminal = api.terminal_info()
        info = {"login": account.login, "server": account.server, "demo": demo,
                "balance": account.balance, "equity": account.equity, "currency": account.currency,
                "offset_hours": offset / 3600, "algo_trading": bool(getattr(terminal, "trade_allowed", False)),
                "terminal_path": getattr(terminal, "path", "")}
        LOG.info("Connected: account %s on %s (%s) | balance %.2f %s | broker clock UTC%+.1fh | %s",
                 account.login, account.server, "DEMO" if demo else "REAL", account.balance,
                 account.currency, offset / 3600, self.symbol)
        if not info["algo_trading"]:
            LOG.warning("Algo Trading is OFF in the demo terminal: orders will be refused until you turn it on.")
        return info

    def reset(self):
        S.reset()

    # MT5 TradeRequest fields and their exact Python types.
    REQ_INT = ("action", "magic", "order", "deviation", "type", "type_filling", "type_time", "expiration",
               "position", "position_by")
    REQ_FLOAT = ("volume", "price", "stoplimit", "sl", "tp")
    _send_form = None                              # learned once: "positional" | "keywords" | "request"

    @classmethod
    def clean_request(cls, req: dict) -> dict:
        out = {}
        for k, v in req.items():
            if v is None:
                continue
            if k in cls.REQ_INT:
                out[k] = int(v)
            elif k in cls.REQ_FLOAT:
                out[k] = float(v)
            elif k == "comment":
                out[k] = "".join(ch for ch in str(v) if ch.isascii() and (ch.isalnum() or ch in ":-_.")) [:31]
            else:
                out[k] = str(v) if k == "symbol" else v
        return out

    def order_send(self, req: dict):
        """order_send with exact types. MetaTrader5 builds differ in how they take the request; if the
        usual positional form is refused with -2, the keyword forms are tried and the working one kept."""
        req = self.clean_request(req)
        with S.lock:
            api = S.api()
            forms = {"positional": lambda: api.order_send(req), "keywords": lambda: api.order_send(**req),
                     "request": lambda: api.order_send(request=req)}
            order = [Market._send_form] if Market._send_form else ["positional", "keywords", "request"]
            errors = []
            for form in order:
                try:
                    res = forms[form]()
                except TypeError as exc:
                    errors.append(f"{form}: {exc}")
                    continue
                if res is not None:
                    if Market._send_form != form:
                        LOG.info("MT5 order_send works with the %s form", form)
                        Market._send_form = form
                    return res
                err = api.last_error()
                errors.append(f"{form}: {err}")
                if not (isinstance(err, tuple) and err and err[0] == -2):
                    break                              # a real refusal, not an argument-form problem
            raise RuntimeError("order_send returned nothing: " + " | ".join(errors) +
                               f" | request={req}")

    def history_deals(self, date_from, date_to):
        """Deal history; positional dates first, then named. Raises MarketError if both fail."""
        with S.lock:
            api = S.api()
            got = api.history_deals_get(date_from, date_to)
            if got is None:
                first = api.last_error()
                try:
                    got = api.history_deals_get(date_from=date_from, date_to=date_to)
                except TypeError:
                    got = None
                if got is None:
                    raise MarketError(f"deal history unavailable: {first} / {api.last_error()}")
            return list(got)

    def trade_call(self, name: str, *args, **kwargs):
        """Trading/account functions that V6's read-only wrapper deliberately refuses."""
        with S.lock:
            return getattr(S.api(), name)(*args, **kwargs)

    def const(self, name: str):
        return getattr(S.api(), name)

    def is_demo(self) -> bool:
        a = self.trade_call("account_info")
        return a is not None and a.trade_mode == getattr(S.api(), "ACCOUNT_TRADE_MODE_DEMO", 0)

    def account(self):
        return self.trade_call("account_info")

    def symbol_info(self):
        return self.trade_call("symbol_info", self.symbol)

    def terminal(self):
        return self.trade_call("terminal_info")

    # ------------------------------------------------------------------ time and prices
    def offset(self) -> int:
        return S.offset_seconds(self.v6, self.symbol)

    def server_to_utc(self, server_ts: float) -> float:
        return server_ts - self.offset()

    def utc_to_server(self, utc_ts: float) -> int:
        return int(utc_ts + self.offset())

    def quote(self) -> dict:
        """Executable bid/ask with its UTC time and age (seconds)."""
        t = S.tick(self.symbol)
        if not (t.bid > 0 and t.ask > 0 and t.ask >= t.bid):
            raise MarketError(f"invalid quote bid={t.bid} ask={t.ask}")
        ts = t.time_msc / 1000.0 - self.offset()
        return {"bid": float(t.bid), "ask": float(t.ask), "spread": round(float(t.ask - t.bid), 5),
                "mid": (float(t.bid) + float(t.ask)) / 2, "time": datetime.fromtimestamp(ts, UTC),
                "age": time.time() - ts}

    def last_closed_15m_open(self) -> datetime:
        rates = S.call("copy_rates_from_pos", self.symbol, self.const("TIMEFRAME_M15"), 1, 1)
        if rates is None or len(rates) == 0:
            raise MarketError(f"no 15M candle: {S.call('last_error')}")
        return datetime.fromtimestamp(int(rates[0]["time"]) - self.offset(), UTC)

    def rates(self, tf_name: str, count: int) -> list:
        """1.12: the last `count` CLOSED candles of a broker timeframe (position 1 onward: the forming candle is never
        returned) as (utc_open_ts, open, high, low, close), oldest first. Broker-aligned (server-time) candles."""
        rows = S.call("copy_rates_from_pos", self.symbol, self.const(tf_name), 1, int(count))
        if rows is None or len(rows) == 0:
            raise MarketError(f"no {tf_name} candles: {S.call('last_error')}")
        off = self.offset()
        return [(int(r["time"]) - off, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))
                for r in rows]

    def rates_v(self, tf_name: str, count: int) -> list:
        """1.19: rates() plus the candle's tick volume: (utc_open_ts, open, high, low, close, tick_volume)."""
        rows = S.call("copy_rates_from_pos", self.symbol, self.const(tf_name), 1, int(count))
        if rows is None or len(rows) == 0:
            raise MarketError(f"no {tf_name} candles: {S.call('last_error')}")
        off = self.offset()
        return [(int(r["time"]) - off, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                 float(r["tick_volume"])) for r in rows]

    def rates_range(self, tf_name: str, from_utc: float, to_utc: float) -> list:
        """1.15: closed and forming candles between two UTC times, as rates() (for the excursion report)."""
        off = self.offset()
        a = datetime.fromtimestamp(from_utc + off, UTC)
        b = datetime.fromtimestamp(to_utc + off, UTC)
        rows = self.trade_call("copy_rates_range", self.symbol, self.const(tf_name), a, b)   # read-only data call
        if rows is None:
            raise MarketError(f"no {tf_name} candles {a:%Y-%m-%d %H:%M}..{b:%Y-%m-%d %H:%M}: {S.call('last_error')}")
        return [(int(r["time"]) - off, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))
                for r in rows]

    # ------------------------------------------------------------------ sessions
    def trading_window(self, now: datetime) -> str | None:
        """None when new orders are allowed; otherwise why not."""
        if not self.sessions.is_open(now):
            return "market closed"
        if not self.sessions.is_open(now - timedelta(minutes=self.c["skip_minutes_after_open"])):
            return f"first {self.c['skip_minutes_after_open']} min after the open"
        if not self.sessions.is_open(now + timedelta(minutes=self.c["skip_minutes_before_close"])):
            return f"last {self.c['skip_minutes_before_close']} min before the close"
        return None

    # ------------------------------------------------------------------ candles, evidence, playbook
    def history(self) -> dict:
        """Closed, validated UTC candles per timeframe (V6 MT5Candles). Raises on any bad trading
        frame; the weekly frame only feeds context evidence, so its failure is tolerated."""
        out = {}
        for tf in HISTORY_TFS:
            try:
                out[tf] = self.candles_src.candles(tf)
            except Exception as exc:
                if tf != "1week":
                    raise
                LOG.warning("Weekly candles unavailable (evidence without 1week): %s", GM.safe_error(exc)[:150])
                out[tf] = []
        return out

    @staticmethod
    def evidence(history: dict, now: datetime) -> dict:
        return EV.build_evidence(history, now)

    @staticmethod
    def atr15(history: dict) -> float:
        return float(GM.indicators(history["15min"])["atr14"])

    def playbook(self):
        path = self.v6.get("playbook_file") or ""
        return AA.load_playbook(GM.ROOT / path) if path else None

    # ------------------------------------------------------------------ calendar and news lock
    def refresh_calendar(self, force: bool = False) -> None:
        if not force and time.time() - self.calendar_checked < 60:
            return
        self.calendar_checked = time.time()
        try:
            self.calendar = self.calendar_src.fetch()
            self.calendar_error = None
        except Exception as exc:  # keep the last good copy; calendar_block() ages it out
            if self.calendar_error != str(exc):
                LOG.warning("Calendar unavailable: %s", GM.safe_error(exc))
            self.calendar_error = str(exc)

    STALE = "Economic calendar unavailable"

    @property
    def news_strict(self) -> bool:
        return (getattr(self, "c", None) or {}).get("news_mode", "lock") == "strict"

    def _calendar_block(self, now: datetime) -> str | None:
        """GM.calendar_block, except that with holiday_trading "normal" (1.19) a USD holiday is a normal day: the holiday
        events are left out, so a major release on that day still gives its window and a stale calendar still blocks.
        (GM.calendar_block returns the holiday first, which would hide a release on the same day.)"""
        cal = self.calendar
        if cal and (getattr(self, "c", None) or {}).get("holiday_trading", "block") == "normal":
            cal = dict(cal, events=[e for e in cal["events"] if e.get("event_type") != "HOLIDAY"])
        return GM.calendar_block(cal, now, self.v6)

    def news_window(self, now: datetime) -> str | None:
        """1.17: a USD holiday or a major release inside news_before_minutes / news_after_minutes (the old lock window),
        or None. A stale/missing calendar is not a window: it is a block (see news_block)."""
        r = self._calendar_block(now)
        return None if (r is None or r.startswith(self.STALE)) else r

    def news_block(self, now: datetime) -> str | None:
        """Same rule as the V6 signal gate: stale/missing calendar, USD holiday, or a major
        release inside news_before_minutes / news_after_minutes blocks new orders.
        1.17 news_mode "strict": the release window no longer blocks (news.strict_hold applies stricter entry rules
        instead); a stale or missing calendar still blocks everything (unknown is not 'no events')."""
        r = self._calendar_block(now)
        if r and self.news_strict and not r.startswith(self.STALE):
            return None
        return r

    def last_release(self, now: datetime, lookback_minutes: float = 120):
        """1.17: the time (UTC) of the latest major release at or before now, within lookback_minutes, or None."""
        if not self.calendar:
            return None
        best = None
        for e in self.calendar["events"]:
            if e.get("event_type") == "HOLIDAY":
                continue
            title = e["title"].lower()
            major = e["impact"].lower() == "high" or any(w in title for w in
                ("fomc", "non-farm", "nonfarm", "cpi", "pce", "powell", "fed chair", "federal funds"))
            if not major:
                continue
            t = GM.stamp(e["time"])
            if t <= now and now - t <= timedelta(minutes=lookback_minutes) and (best is None or t > best):
                best = t
        return best

    def last_lock_end(self, now: datetime, lookback_hours: float = 6) -> datetime | None:
        """1.10.2: the end (UTC) of the most recent news-lock window that has already ended, within
        lookback_hours. Same 'major' rule and minutes as GM.calendar_block. None = no recent lock (or no calendar;
        the live gate itself fails closed on a missing calendar)."""
        if not self.calendar:
            return None
        after = timedelta(minutes=self.v6["news_after_minutes"])
        best = None
        for e in self.calendar["events"]:
            if e.get("event_type") == "HOLIDAY":
                continue
            title = e["title"].lower()
            major = e["impact"].lower() == "high" or any(w in title for w in
                ("fomc", "non-farm", "nonfarm", "cpi", "pce", "powell", "fed chair", "federal funds"))
            if not major:
                continue
            # 1.17 strict news mode: no lock to wait out, only the release candle itself is not a confirmation
            end = GM.stamp(e["time"]) + (timedelta(minutes=1) if self.news_strict else after)
            if end <= now and now - end <= timedelta(hours=lookback_hours):
                best = end if best is None or end > best else best
        return best

    def next_major_event(self, now: datetime, within_minutes: float):
        """1.14: the next major release (same 'major' rule as last_lock_end / GM.calendar_block) due within
        `within_minutes` from now: (title, time UTC) or None. No calendar -> None (the gates fail closed on their own)."""
        if not self.calendar:
            return None
        best = None
        for e in self.calendar["events"]:
            if e.get("event_type") == "HOLIDAY":
                continue
            title = e["title"].lower()
            major = e["impact"].lower() == "high" or any(w in title for w in
                ("fomc", "non-farm", "nonfarm", "cpi", "pce", "powell", "fed chair", "federal funds"))
            if not major:
                continue
            t = GM.stamp(e["time"])
            if now <= t <= now + timedelta(minutes=within_minutes) and (best is None or t < best[1]):
                best = (e["title"], t)
        return best

    def upcoming_events(self, now: datetime, hours: int = 48) -> list:
        if not self.calendar:
            return []
        out = []
        for e in self.calendar["events"]:
            t = GM.stamp(e["time"])
            if now - timedelta(hours=2) <= t <= now + timedelta(hours=hours):
                out.append({"time_utc": GM.iso(t), "title": e["title"], "impact": e["impact"],
                            "forecast": e.get("forecast", ""), "previous": e.get("previous", ""),
                            "actual": e.get("actual", "")})
        return out
