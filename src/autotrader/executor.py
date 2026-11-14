"""
Order placement and management on the DEMO terminal.

send(): places the validated order with SL and final TP stored on the broker server. Pending
        orders also carry their expiry (in broker server time), so the broker removes them even
        if this program or the PC stops.
manage(): every few seconds - cancel levels and expiry backup, fill detection, TP1 partial close
        and breakeven, and keeping the local database in step with the broker.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone

import gold_monitor as GM

from . import alerts
from . import review as RV
from . import flow as FL
from . import zones as ZN
from . import news as NW

UTC = timezone.utc
LOG = logging.getLogger("autotrader.executor")
SYMBOL_EXPIRATION_SPECIFIED = 4


class Executor:
    def __init__(self, c, market, state, dry_run=False):
        self.c, self.m, self.state, self.dry_run = c, market, state, dry_run
        self._unknown_reported: set = set()        # 1.10.6: broker tickets already reported as missing from the journal
        self.zones = ZN.ZoneScorer(c, market)        # 1.15: all-timeframe zone score, recorded on every order

    # ------------------------------------------------------------------ reconciliation (1.10.6)
    def reconcile(self, pending: dict, positions: dict) -> list:
        """Every order and position the BROKER holds under our magic numbers must exist in the journal.
        28 Oct 08:00: IDEA-BUY-4031 was sent and journalled, then the notification crashed and the operator was told
        'Order send failed' while a 0.04-lot order was working at the broker. The alert itself is fixed (1.10.4), but
        nothing compared the broker's book with ours. This does, every manage() pass: a ticket we do not know is
        reported once (never touched: it may be the operator's own order under our magic, or a lost journal write),
        so the human decides in MT5. Returns the newly reported tickets."""
        known = set()
        for o in self.state.all_orders():
            known.add(int(o["ticket"]))
            if o.get("position_ticket") is not None:
                known.add(int(o["position_ticket"]))
        new = []
        for kind, items in (("pending order", pending), ("position", positions)):
            for ticket, x in items.items():
                ident = getattr(x, "identifier", None)
                if ticket in known or (ident is not None and ident in known) or ticket in self._unknown_reported:
                    continue
                self._unknown_reported.add(ticket)
                new.append(ticket)
                alerts.error(f"Broker {kind} {ticket} (magic {getattr(x, 'magic', '?')}, comment "
                             f"'{getattr(x, 'comment', '')}', {getattr(x, 'volume', getattr(x, 'volume_current', '?'))} lots) "
                             f"is NOT in the trader's journal. It was placed outside this program or its journal write "
                             f"was lost. Not touched: check it in MT5.")
        return new

    # ------------------------------------------------------------------ helpers
    def _send(self, req: dict):
        res = self.m.order_send(req)
        ok = (self.m.const("TRADE_RETCODE_DONE"), self.m.const("TRADE_RETCODE_PLACED"),
              self.m.const("TRADE_RETCODE_DONE_PARTIAL"))
        if res.retcode not in ok:
            raise RuntimeError(f"broker rejected: retcode={res.retcode} {getattr(res, 'comment', '')}")
        return res

    def _filling(self):
        fm = self.m.symbol_info().filling_mode
        if fm & 1:
            return self.m.const("ORDER_FILLING_FOK")
        if fm & 2:
            return self.m.const("ORDER_FILLING_IOC")
        return self.m.const("ORDER_FILLING_RETURN")

    def magics(self) -> set:
        """Orders this executor manages: the swing magic, in lab mode the lab magic (1.10) and, when live, the
        overnight engine's magic (1.11)."""
        out = {self.c["magic"]} | ({self.c["magic_lab"]} if self.c.get("lab_mode") else set())
        ov = self.c.get("overnight") or {}
        if ov.get("mode") == "live":
            out.add(int(ov.get("magic", 40004)))
        return out

    def _keep_after_touch(self, rec: dict, now: datetime, why: str):
        """Mark a lab limit whose TP1 traded before the fill: tag it, stop watching the cancel level, keep it."""
        try:
            tags = json.loads(rec.get("violations") or "[]")
        except (TypeError, ValueError):
            tags = []
        if "tp_touched_first" not in tags:
            tags.append("tp_touched_first")
        self.state.update_order(rec["ticket"], cancel_above=None, cancel_below=None,
                                tp_touched_utc=now.strftime("%Y-%m-%dT%H:%M:%SZ"), violations=json.dumps(tags),
                                note=f"{why}; kept working (lab_keep_after_tp_touch), Astra re-asked at its next map")
        LOG.info("Lab order %s %s: %s; kept working until expiry or until Astra's next map drops it",
                 rec["ticket"], rec["setup_id"], why)
        alerts.trade(f"🧪 LAB {rec['direction']} {rec['setup_id']}: {why}; order kept, waiting for Astra's next map",
                     short=None)

    def close_position(self, pos, why: str, comment: str = "TIME") -> bool:
        """1.11: close a whole position at market (the overnight engine's time exit; 1.15 trade protection's
        near-TP exit). Returns True when sent."""
        k = self.m.const
        q = self.m.quote()
        buy = pos.type == k("POSITION_TYPE_BUY")
        try:
            self._send({"action": k("TRADE_ACTION_DEAL"), "symbol": self.m.symbol, "position": pos.ticket,
                        "volume": pos.volume, "type": k("ORDER_TYPE_SELL") if buy else k("ORDER_TYPE_BUY"),
                        "price": q["bid"] if buy else q["ask"], "deviation": 30,
                        "magic": getattr(pos, "magic", None) or self.c["magic"], "comment": comment[:31],
                        "type_filling": self._filling()})
            LOG.info("Position %s closed at market: %s", pos.ticket, why)
            return True
        except Exception as exc:  # noqa: BLE001
            alerts.error(f"Market close ({comment}) failed on position {pos.ticket}: {exc}")
            return False

    def move_stop(self, pos, sl: float, why: str) -> bool:
        """1.14: move a position's stop (the news guard's breakeven). Keeps the broker TP. True when sent."""
        k = self.m.const
        try:
            self._send({"action": k("TRADE_ACTION_SLTP"), "symbol": self.m.symbol, "position": pos.ticket,
                        "sl": float(sl), "tp": float(getattr(pos, "tp", 0.0) or 0.0)})
            LOG.info("Stop of position %s moved to %.2f: %s", pos.ticket, sl, why)
            return True
        except Exception as exc:  # noqa: BLE001
            alerts.error(f"Stop move failed on position {pos.ticket}: {exc}")
            return False

    def set_target(self, pos, tp: float, why: str) -> bool:
        """1.16: move a position's target (opposite trades: wait for breakeven). Keeps the broker stop. True when sent."""
        k = self.m.const
        try:
            self._send({"action": k("TRADE_ACTION_SLTP"), "symbol": self.m.symbol, "position": pos.ticket,
                        "sl": float(getattr(pos, "sl", 0.0) or 0.0), "tp": float(tp)})
            LOG.info("Target of position %s moved to %.2f: %s", pos.ticket, tp, why)
            return True
        except Exception as exc:  # noqa: BLE001
            alerts.error(f"Target move failed on position {pos.ticket}: {exc}")
            return False

    def _mine(self, items):
        return [x for x in (items or []) if x.magic in self.magics()]

    # ------------------------------------------------------------------ placing
    def send(self, order: dict, lots: float, risk_money: float, grade: str, magic: int | None = None) -> int:
        why = NW.hold_reason(self.m, self.c, order, datetime.now(timezone.utc))      # 1.17: strict news entries
        if why:
            raise NW.NewsHold(why)
        si = self.m.symbol_info()
        k = self.m.const
        final_tp = order["take_profit_2"] if order["take_profit_2"] is not None else order["take_profit_1"]
        lab = magic is not None and magic != self.c["magic"]
        req = {"symbol": self.m.symbol, "volume": lots, "sl": order["stop_loss"], "tp": final_tp,
               "deviation": max(1, int(round(order["max_entry_deviation"] / si.point))),
               "magic": self.c["magic"] if magic is None else int(magic),
               "comment": (f"LAB:{order['setup_id']}" if lab else f"{grade[0]}:{order['setup_id']}")[:31]}
        if self.dry_run:
            LOG.info("[DRY RUN] would send %s %s %.2f lots @ %.2f sl %.2f tp %.2f", grade, order["order_type"],
                     lots, order["entry_price"], order["stop_loss"], final_tp)
            return 0

        if order["order_type"] == "MARKET":
            q = self.m.quote()
            buy = order["direction"] == "BUY"
            req.update(action=k("TRADE_ACTION_DEAL"), type=k("ORDER_TYPE_BUY") if buy else k("ORDER_TYPE_SELL"),
                       price=q["ask"] if buy else q["bid"], type_time=k("ORDER_TIME_GTC"),
                       type_filling=self._filling())
            res = self._send(req)
            self.state.record_order(res.order, order, grade, lots, risk_money, "OPEN", position_ticket=res.order)
            self.state.set_strategy(res.order, order.get("strategy", "SWING_TREND"))
            self.state.set_meta(res.order, order.get("meta"))
            self._zone_tag(res.order, order)
            if not lab:                                            # 1.10.1: the lab posts its own single line
                alerts.safe_trade(lambda: alerts.order_text("FILLED (market)", order, lots, risk_money, grade, res.price),
                                  lambda: alerts.signal_text("FILLED", order, lots, res.price))
            LOG.info("MARKET %s %.2f lots filled @ %.2f | sl %.2f tp %.2f | ticket %s",
                     order["direction"], lots, res.price, order["stop_loss"], final_tp, res.order)
            return res.order

        types = {"BUY_LIMIT": "ORDER_TYPE_BUY_LIMIT", "SELL_LIMIT": "ORDER_TYPE_SELL_LIMIT",
                 "BUY_STOP": "ORDER_TYPE_BUY_STOP", "SELL_STOP": "ORDER_TYPE_SELL_STOP"}
        req.update(action=k("TRADE_ACTION_PENDING"), type=k(types[order["order_type"]]),
                   price=order["entry_price"], type_filling=k("ORDER_FILLING_RETURN"))
        expiry = datetime.fromisoformat(order["order_expiry_utc"].replace("Z", "+00:00"))
        if si.expiration_mode & SYMBOL_EXPIRATION_SPECIFIED:
            req.update(type_time=k("ORDER_TIME_SPECIFIED"), expiration=self.m.utc_to_server(expiry.timestamp()))
        else:
            req["type_time"] = k("ORDER_TIME_GTC")
            LOG.warning("Broker does not accept an expiry on %s orders; the bot will cancel at expiry.", self.m.symbol)
        try:
            res = self._send(req)
        except RuntimeError as e:
            if "expiration" in req and str(k("TRADE_RETCODE_INVALID_EXPIRATION")) in str(e):
                alerts.error(f"Broker refused the order expiry ({e}); placed as GTC with bot-side cancel")
                req.pop("expiration")
                req["type_time"] = k("ORDER_TIME_GTC")
                res = self._send(req)
            else:
                raise
        self.state.record_order(res.order, order, grade, lots, risk_money, "PENDING")
        self.state.set_strategy(res.order, order.get("strategy", "SWING_TREND"))
        self.state.set_meta(res.order, order.get("meta"))
        self._zone_tag(res.order, order)
        if not lab:
            alerts.safe_trade(lambda: alerts.order_text(f"PLACED {order['order_type']}", order, lots, risk_money, grade),
                              lambda: alerts.signal_text("PENDING ORDER", order, lots))
        LOG.info("%s %.2f lots @ %.2f | sl %.2f tp %.2f | expires %s | ticket %s", order["order_type"], lots,
                 order["entry_price"], order["stop_loss"], final_tp, order["order_expiry_utc"], res.order)
        return res.order

    def _flow_tag(self, ticket: int, order: dict):
        """1.19: record the 5M/1M structure and the tick-volume read on the order (never a gate)."""
        try:
            f = FL.measure(self.m, order["direction"])
            self.state.update_order(ticket, **f)
            LOG.info("%s %s flow: %s", order["setup_id"], order["direction"], FL.text(f))
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Order flow not recorded for %s: %s", order.get("setup_id"), str(exc)[:160])

    def _zone_tag(self, ticket: int, order: dict):
        """1.15: record the all-timeframe zone score on the order (never a gate; a failure only skips the record)."""
        self._flow_tag(ticket, order)
        zs_ = getattr(self, "zones", None)
        if not (zs_ and zs_.on):
            return
        try:
            zs = zs_.for_order(order)
            self.state.update_order(ticket, **ZN.columns(zs))
            LOG.info("%s %s: %s", order["setup_id"], order["direction"], ZN.label(zs))
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Zone score not recorded for %s: %s", order.get("setup_id"), str(exc)[:160])

    def cancel(self, ticket: int, why: str, status="CANCELLED") -> bool:
        try:
            self._send({"action": self.m.const("TRADE_ACTION_REMOVE"), "order": ticket})
            self.state.update_order(ticket, status=status, note=why)
            LOG.info("Cancelled pending order %s: %s", ticket, why)
            return True
        except Exception as exc:  # noqa: BLE001
            alerts.error(f"Cancel of order {ticket} failed: {exc}")
            return False

    def cancel_all_pending(self, why: str):
        for o in self._mine(self.m.trade_call("orders_get", symbol=self.m.symbol)):
            self.cancel(o.ticket, why)

    # ------------------------------------------------------------------ monitoring
    def manage(self, now: datetime | None = None):
        now = now or datetime.now(UTC)
        try:
            self.repair_missed_fills()                            # 1.14.1: once per start
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Journal repair skipped: %s", str(exc)[:160])
        q = self.m.quote()
        bid, ask = q["bid"], q["ask"]
        si = self.m.symbol_info()
        pending = {o.ticket: o for o in self._mine(self.m.trade_call("orders_get", symbol=self.m.symbol))}
        positions = {p.ticket: p for p in self._mine(self.m.trade_call("positions_get", symbol=self.m.symbol))}

        self.fill_results()
        self.review_closed(now)
        for rec in self.state.active_orders():
            ticket = rec["ticket"]
            if rec["status"] == "PENDING":
                pos = positions.get(ticket) or next((p for p in positions.values()
                                                     if getattr(p, "identifier", None) == ticket), None)
                if ticket in pending:
                    # v1.6: a PARTIAL fill leaves the order pending AND opens a position. Track the
                    # position, and when the rest is cancelled/expired keep managing what was filled
                    # (v1.5 marked the record EXPIRED/CANCELLED and lost the open position).
                    if pos is not None and rec.get("position_ticket") != pos.ticket:
                        self.state.update_order(ticket, position_ticket=pos.ticket,
                                                note=f"partially filled: {pos.volume} of {rec['lots']} lots")
                        LOG.info("Order %s partially filled: %.2f of %.2f lots", ticket, pos.volume, rec["lots"])
                    expiry = datetime.fromisoformat(rec["expiry_utc"].replace("Z", "+00:00"))
                    why = None
                    if now >= expiry:
                        why, st = "expired", "EXPIRED"
                    elif rec["cancel_above"] is not None and ask > rec["cancel_above"]:
                        why, st = f"price traded above {rec['cancel_above']} before the fill", "CANCELLED"
                    elif rec["cancel_below"] is not None and bid < rec["cancel_below"]:
                        why, st = f"price traded below {rec['cancel_below']} before the fill", "CANCELLED"
                    if why and st == "CANCELLED" and rec.get("lab") and self.c.get("lab_keep_after_tp_touch"):
                        # 1.11.1 (owner, 4 Nov): a lab limit is NOT cancelled when TP1 trades before the fill. Price
                        # can come back to the zone and go to TP1 again; the order stays working, tagged, until its
                        # expiry or until Astra's next map no longer proposes it (Lab.recheck). Live orders unchanged.
                        self._keep_after_touch(rec, now, why)
                        continue
                    if why:
                        # only when the rest is really gone: otherwise keep it PENDING and retry next time
                        if self.cancel(ticket, why, status=st) and pos is not None:
                            self.state.update_order(ticket, status="OPEN", position_ticket=pos.ticket,
                                                    note=f"partial fill {pos.volume} lots kept; rest {why}")
                            LOG.info("Order %s: unfilled rest removed (%s); %.2f lots stay managed", ticket, why,
                                     pos.volume)
                    continue
                if pos is not None:
                    self.state.update_order(ticket, status="OPEN", position_ticket=pos.ticket,
                                            fill_utc=now.strftime("%Y-%m-%dT%H:%M:%SZ"))
                    LOG.info("Order %s filled -> position %s @ %.2f", ticket, pos.ticket, pos.price_open)
                    alerts.trade(f"✅ FILLED {rec['direction']} {pos.volume} lots @ {pos.price_open:.2f} "
                                 f"(order {ticket}, {rec.get('playbook') or rec.get('strategy') or ''})",
                                 short=None if rec.get("lab") else
                                 f"✅ FILLED {rec['direction']} {pos.volume} lots @ {pos.price_open:.2f}")
                else:
                    if self.filled_and_closed(rec):              # 1.14.1: it traded between two checks
                        continue
                    gone = "EXPIRED" if now >= datetime.fromisoformat(rec["expiry_utc"].replace("Z", "+00:00")) else "CANCELLED"
                    self.state.update_order(ticket, status=gone, note="no longer at the broker")
                continue

            pos = positions.get(rec["position_ticket"])
            if pos is None:
                self.state.update_order(ticket, status="CLOSED", closed_utc=now.strftime("%Y-%m-%dT%H:%M:%SZ"))
                LOG.info("Position %s closed by the broker (stop, target or manual).", rec["position_ticket"])
                continue
            exit_utc = rec.get("exit_utc")                                      # 1.11: time exit
            if exit_utc and now >= datetime.fromisoformat(exit_utc.replace("Z", "+00:00")):
                if self.close_position(pos, f"time exit {exit_utc}"):
                    self.state.update_order(ticket, status="CLOSED", closed_utc=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                            note=f"time exit at {exit_utc}")
                    icon = "⏱" if str(rec["setup_id"]).startswith("SCP-") else "🌙"
                    alerts.trade(f"{icon} Time exit: {rec['direction']} {rec['setup_id']} closed at market ({exit_utc[11:16]} UTC)",
                                 short=None if rec.get("lab") else f"🌙 Overnight {rec['direction']} closed by time")
                continue
            if rec["status"] == "OPEN" and rec["tp2"] is not None:
                buy = pos.type == self.m.const("POSITION_TYPE_BUY")
                if (bid >= rec["tp1"]) if buy else (ask <= rec["tp1"]):
                    self._tp1(rec, pos, si, buy, q)
        try:
            self.reconcile(pending, positions)                 # 1.10.6: after the journal is in step; report, never act
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Reconciliation skipped: %s", str(exc)[:160])

    def review_closed(self, now: datetime | None = None):
        """1.7: a deterministic post-trade review for every closed trade with a known result."""
        todo = self.state.closed_unreviewed()
        if not todo:
            return
        now = now or datetime.now(UTC)
        try:
            bars = self.m.history().get("15min") or []
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Review postponed, no candles: %s", str(exc)[:120])
            return
        for rec in todo:
            try:
                if not rec.get("fill_utc") or not rec.get("closed_utc"):
                    rec = self.backfill_times(rec)
                if not rec.get("fill_utc") or not rec.get("closed_utc"):
                    LOG.info("Review of %s postponed: open/close time unknown (no deal history yet)", rec["ticket"])
                    continue
                closed = datetime.fromisoformat(rec["closed_utc"].replace("Z", "+00:00"))
                if now < closed + timedelta(hours=4, minutes=15):     # 1.7.3: the 4h-after window must be closed
                    continue
                rv = RV.build(rec, bars, now)
                self.state.save_review(rv)
                LOG.info(RV.text(rv))
                alerts.trade(RV.text(rv))
                if rec.get("user_plan") and not rec.get("lab"):     # 1.10.1: lab samples never teach Astra
                    self.lesson_from_plan(int(rec["user_plan"]), rv)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Review of %s failed: %s", rec["ticket"], str(exc)[:160])

    def lesson_from_plan(self, plan_id: int, rv: dict):
        """1.9: the owner's plan + Astra's note + the review -> lessons/U<id>.txt, then the teaching session
        runs in the background (tools/learn_lessons.py: 2 calls for the lesson + 1 for the playbook)."""
        import subprocess
        import sys
        import user_plans as UP
        try:
            path = Path(self.c["v6"]["state_directory"]) / UP.FILE_NAME
            if not path.is_absolute():
                path = GM.ROOT / path
            plans = json.loads(path.read_text(encoding="utf-8"))
            p = next(x for x in plans if x["id"] == plan_id)
            lessons = GM.ROOT / "lessons"
            lessons.mkdir(exist_ok=True)
            out = lessons / f"U{plan_id:03d}_{p['received_utc'][:10]}.txt"
            out.write_text(UP.lesson_text(p, rv), encoding="utf-8")
            for x in plans:
                if x["id"] == plan_id:
                    x["status"], x["lesson"] = "CLOSED", out.name
            path.write_text(json.dumps(plans, ensure_ascii=False, indent=1), encoding="utf-8")
            LOG.info("Lesson written: %s", out.name)
            alerts.trade(f"📚 Lesson {out.name} written from plan U{plan_id}; teaching session started")
            if self.c.get("user_plan_auto_study", True):
                subprocess.Popen([sys.executable, str(GM.ROOT / "tools" / "learn_lessons.py")], cwd=str(GM.ROOT),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Lesson from plan U%s failed: %s", plan_id, str(exc)[:160])

    def filled_and_closed(self, rec: dict, why: str = "filled and closed between two checks") -> bool:
        """1.14.1: a pending order that is gone from the broker may have FILLED and then been CLOSED before the trader
        looked (10 Nov: LAB-BUY-4124 filled and stopped at 02:49 UTC while the PC slept, -15.10; LAB-BUY-4057 filled at
        13:30:04 on the CPI spike and stopped at 13:30:06, -4.85). Both were journalled CANCELLED and their losses were
        missing. The broker's deal history decides: an entry deal for this order's position means it traded. Then
        the record is CLOSED with the position, fill and close times; fill_results books the money result."""
        try:
            deals = self.m.trade_call("history_deals_get", position=rec["ticket"]) or []
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Deal history for order %s unavailable (%s): treated as not filled", rec["ticket"], str(exc)[:120])
            return False
        entry_in = self.m.const("DEAL_ENTRY_IN")
        ins = [d for d in deals if getattr(d, "entry", None) == entry_in]
        if not ins:
            return False
        outs = [d for d in deals if getattr(d, "entry", None) != entry_in]
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        when = lambda t: datetime.fromtimestamp(self.m.server_to_utc(t), UTC).strftime(fmt)
        fields = {"position_ticket": getattr(ins[0], "position_id", None) or rec["ticket"],
                  "fill_utc": when(min(d.time for d in ins)), "note": why}
        if outs:
            fields.update(status="CLOSED", closed_utc=when(max(d.time for d in outs)))
        else:
            fields.update(status="OPEN")                          # filled, position not found this pass: manage it
        self.state.update_order(rec["ticket"], **fields)
        LOG.info("Order %s %s %s: %s (from the broker's deals)", rec["ticket"], rec["setup_id"], fields["status"], why)
        return True

    def repair_missed_fills(self, days: int = 21) -> list:
        """1.14.1, once per start: records journalled CANCELLED/EXPIRED 'no longer at the broker' in the last `days`
        that the broker's deals show were filled are corrected to what really happened (this adds their losses or
        gains to the journal; it never removes a trade). Returns the tickets corrected."""
        if getattr(self, "_repaired", False):
            return []
        self._repaired = True
        since = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out = []
        for rec in self.state.all_orders():
            if rec.get("status") in ("CANCELLED", "EXPIRED") and rec.get("note") == "no longer at the broker" \
                    and (rec.get("created_utc") or "") >= since and not rec.get("position_ticket"):
                if self.filled_and_closed(rec, "repaired: the broker's deals show it was filled"):
                    out.append(rec["ticket"])
        if out:
            alerts.error(f"Journal repaired from the broker's deals: {len(out)} order(s) had been marked cancelled but "
                         f"were filled ({', '.join(str(t) for t in out)})")
        return out

    def backfill_times(self, rec: dict) -> dict:
        """1.7.1: fill_utc / closed_utc from the position's deals (entry IN / OUT), for records made
        before those columns existed. On 22 Oct three pre-1.7 trades were reviewed with closed_utc
        NULL, so the window ran to 'now' and MFE/MAE covered a week instead of 12 minutes."""
        if not rec.get("position_ticket"):
            return rec
        try:
            deals = self.m.trade_call("history_deals_get", position=rec["position_ticket"]) or []
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Deal history for %s unavailable: %s", rec["ticket"], str(exc)[:120])
            return rec
        entry_in = self.m.const("DEAL_ENTRY_IN")
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        fields = {}
        ins = [d.time for d in deals if getattr(d, "entry", None) == entry_in and getattr(d, "time", None)]
        outs = [d.time for d in deals if getattr(d, "entry", None) != entry_in and getattr(d, "time", None)]
        if ins and not rec.get("fill_utc"):
            fields["fill_utc"] = datetime.fromtimestamp(self.m.server_to_utc(min(ins)), UTC).strftime(fmt)
        if outs and not rec.get("closed_utc") and rec.get("status") == "CLOSED":
            fields["closed_utc"] = datetime.fromtimestamp(self.m.server_to_utc(max(outs)), UTC).strftime(fmt)
        if fields:
            self.state.update_order(rec["ticket"], **fields)
            rec = dict(rec, **fields)
        return rec

    def fill_results(self):
        """Money result of closed trades, from the broker's deal history (kept in the orders table)."""
        for rec in self.state.closed_without_result():
            deals = self.m.trade_call("history_deals_get", position=rec["position_ticket"])
            if deals:
                net = round(sum(d.profit + d.commission + d.swap for d in deals), 2)
                fields = {"result": net}
                if not rec.get("closed_utc"):                      # 1.6.1: closing time from the last deal
                    try:
                        last = max(d.time for d in deals)
                        fields["closed_utc"] = datetime.fromtimestamp(self.m.server_to_utc(last), UTC)\
                            .strftime("%Y-%m-%dT%H:%M:%SZ")
                    except Exception:  # noqa: BLE001
                        pass
                self.state.update_order(rec["ticket"], **fields)
                LOG.info("Trade %s %s closed: %+.2f", rec["setup_id"], rec["direction"], net)
                r = net / rec["risk_money"] if rec["risk_money"] else 0.0
                icon = '🟢' if net > 0 else '🔴' if net < 0 else '⚪'
                alerts.trade(f"{icon} CLOSED {rec['direction']} {rec['setup_id']}: {net:+.2f} ({r:+.2f}R)"
                             + alerts.tail(rec.get("playbook") or rec.get("strategy"), rec.get("confirmation"),
                                           rec.get("session")),
                             short=None if rec.get("lab") else
                             f"{icon} CLOSED {rec['direction']} {net:+.2f} USD ({r:+.2f}R)")

    def _tp1(self, rec, pos, si, buy, q):
        k = self.m.const
        try:
            step = si.volume_step
            close = round(int(pos.volume * rec["tp1_close_pct"] / 100 / step + 1e-9) * step, 8)
            if close < si.volume_min or close >= pos.volume:
                # 1.11.6: the position cannot be split (5 Nov IDEA-SELL-4120: 0.01 lot reached TP1, nothing could be
                # closed, the stop went to breakeven and a +2R trade ended -0.54). Take the whole position at TP1.
                self._send({"action": k("TRADE_ACTION_DEAL"), "symbol": self.m.symbol, "position": pos.ticket,
                            "volume": pos.volume, "type": k("ORDER_TYPE_SELL") if buy else k("ORDER_TYPE_BUY"),
                            "price": q["bid"] if buy else q["ask"], "deviation": 30,
                            "magic": getattr(pos, "magic", None) or self.c["magic"],
                            "comment": "TP1", "type_filling": self._filling()})
                LOG.info("TP1 reached on %s: %.2f lots cannot be split; whole position closed at TP1", pos.ticket,
                         pos.volume)
                self.state.update_order(rec["ticket"], status="TP1_DONE",
                                        note=f"TP1: whole {pos.volume} lots closed (cannot be split)")
                alerts.trade(f"🎯 TP1 reached on {rec['direction']} {rec['setup_id']}: whole position closed "
                             f"({pos.volume} lots cannot be split)",
                             short=None if rec.get("lab") else f"🎯 TP1 hit {rec['direction']} · closed")
                return
            if si.volume_min <= close < pos.volume:
                self._send({"action": k("TRADE_ACTION_DEAL"), "symbol": self.m.symbol, "position": pos.ticket,
                            "volume": close, "type": k("ORDER_TYPE_SELL") if buy else k("ORDER_TYPE_BUY"),
                            "price": q["bid"] if buy else q["ask"], "deviation": 30,
                            "magic": getattr(pos, "magic", None) or self.c["magic"],   # 1.10: keep the lab magic
                            "comment": "TP1", "type_filling": self._filling()})
                LOG.info("TP1 reached on %s: closed %.2f of %.2f lots", pos.ticket, close, pos.volume)
            cur = float(getattr(pos, "sl", 0.0) or 0.0)
            be = round(pos.price_open, si.digits)
            if rec["be_at_tp1"] and cur and ((cur >= be) if buy else (cur <= be)):
                LOG.info("TP1 on %s: stop %.2f already at or beyond breakeven (trade protection); kept", pos.ticket, cur)
            elif rec["be_at_tp1"]:                                 # 1.15: never loosen a stop trade protection locked
                self._send({"action": k("TRADE_ACTION_SLTP"), "symbol": self.m.symbol, "position": pos.ticket,
                            "sl": be, "tp": float(pos.tp)})
                LOG.info("Stop moved to breakeven %.2f on %s", be, pos.ticket)
            self.state.update_order(rec["ticket"], status="TP1_DONE")
            alerts.trade(f"🎯 TP1 reached on {rec['direction']} {rec['setup_id']}: partial closed"
                         + (", stop moved to breakeven" if rec["be_at_tp1"] else ""),
                         short=None if rec.get("lab") else
                         f"🎯 TP1 hit {rec['direction']}" + (" · stop → breakeven" if rec["be_at_tp1"] else ""))
        except Exception as exc:  # noqa: BLE001
            alerts.error(f"TP1 management failed on position {pos.ticket}: {exc}")
