#!/usr/bin/env python3
"""Offline, deterministic context indicators and candidate breaker zones for XAU/USD.

V5 supplement to gold_smc_engine.py. No brokerage, AI or network calls.
CONTEXT IS NOT A TRADING SIGNAL: only the independent final risk gate in
gold_monitor\.py may authorize an alert after evidence, temporal validity and safety checks.

V5.0 change: a breaker's break is searched only AFTER the order block was confirmed
(its structural break), never between the OB candle and that confirmation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Any

from gold_smc_engine import analyze_frame, _validate, _atr, _make_id, _time


@dataclass(frozen=True)
class ConfluenceSettings:
    rsi_period: int = 14
    minimum_divergence_rsi: float = 3.0
    min_price_extreme_atr: float = 0.1
    breaker_break_atr: float = 0.1
    breaker_max_age_bars: int = 80
    divergence_max_age_bars: int = 80
    pivot_left: int = 3
    pivot_right: int = 3

    def __post_init__(self):
        if not 5 <= self.rsi_period <= 50 or not 0 < self.minimum_divergence_rsi <= 30:
            raise ValueError('Invalid RSI settings')
        if not 0 < self.min_price_extreme_atr <= 5 or not 0 <= self.breaker_break_atr <= 5:
            raise ValueError('Invalid thresholds')
        if not 1 <= self.breaker_max_age_bars <= 500 or not 1 <= self.divergence_max_age_bars <= 500:
            raise ValueError('Invalid breaker/divergence age')
        if not 1 <= self.pivot_left <= 10 or not 1 <= self.pivot_right <= 10:
            raise ValueError('Invalid pivot window')


def _ema_series(values: list[float], period: int) -> list[float]:
    alpha = 2.0 / (period + 1.0)
    result = [float(values[0])]
    for x in values[1:]:
        result.append(result[-1] + alpha * (x - result[-1]))
    return result


def _rsi_series(closes: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    changes = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gain = sum(max(d, 0) for d in changes[:period]) / period
    loss = sum(max(-d, 0) for d in changes[:period]) / period
    def value(g: float, l: float) -> float:
        return 50.0 if g == l == 0 else 100.0 if l == 0 else 100.0 - 100.0/(1.0 + g/l)
    out[period] = value(gain, loss)
    for i in range(period+1, len(closes)):
        delta = changes[i-1]
        gain = (gain * (period-1) + max(delta, 0)) / period
        loss = (loss * (period-1) + max(-delta, 0)) / period
        out[i] = value(gain, loss)
    return out


def _confirmed_pivots(bars: list[dict[str, Any]], cfg: ConfluenceSettings, side: str) -> list[int]:
    attr = 'high' if side == 'HIGH' else 'low'
    cmp = (lambda a,b: a > b) if side == 'HIGH' else (lambda a,b: a < b)
    candidates = []
    for i in range(cfg.pivot_left, len(bars)-cfg.pivot_right):
        others = bars[i-cfg.pivot_left:i] + bars[i+1:i+cfg.pivot_right+1]
        if all(cmp(bars[i][attr], other[attr]) for other in others):
            candidates.append(i)
    return candidates


def _divergences(bars: list[dict[str, Any]], rsi: list[float | None], atr: list[float],
                 tf: str, cfg: ConfluenceSettings) -> list[dict[str, Any]]:
    result = []
    for side, direction in (('LOW', 'BUY'), ('HIGH', 'SELL')):
        pivots = _confirmed_pivots(bars, cfg, side)
        for a,b in zip(pivots, pivots[1:]):
            if len(bars) - 1 - (b + cfg.pivot_right) > cfg.divergence_max_age_bars:
                continue
            if rsi[a] is None or rsi[b] is None:
                continue
            if side == 'LOW':
                price_condition = bars[b]['low'] < bars[a]['low'] - cfg.min_price_extreme_atr * atr[b]
                rsi_condition = rsi[b] >= rsi[a] + cfg.minimum_divergence_rsi
            else:
                price_condition = bars[b]['high'] > bars[a]['high'] + cfg.min_price_extreme_atr * atr[b]
                rsi_condition = rsi[b] <= rsi[a] - cfg.minimum_divergence_rsi
            if price_condition and rsi_condition:
                confirmed_at = bars[b+cfg.pivot_right]['closes']
                result.append({'id': _make_id(tf, 'RSI_DIVERGENCE_' + direction, bars[b]['time'], bars[a]['time']),
                               'kind': 'RSI_DIVERGENCE', 'direction': direction, 'timeframe': tf,
                               'at': confirmed_at, 'source_times': [bars[a]['time'], bars[b]['time']],
                               'prices': [bars[a]['low' if side == 'LOW' else 'high'], bars[b]['low' if side == 'LOW' else 'high']],
                               'rsi_at_pivots': [round(rsi[a], 3), round(rsi[b], 3)],
                               'status': 'CONTEXT_ONLY'})
    return result[-12:]


def _breaker_candidates(bars: list[dict[str, Any]], atr: list[float],
                        tf: str, smc_events: list[dict[str, Any]], cfg: ConfluenceSettings) -> list[dict[str, Any]]:
    """Candidate breaker = *previous evidenced OB* decisively broken, then retested.

    Break must occur AFTER OB confirmation. Retest must occur AFTER the break candle.
    No assumption about real institutional inventory or profitability.
    """
    result = []
    for ob in smc_events:
        if ob.get('kind') != 'ORDER_BLOCK_CANDIDATE' or ob.get('timeframe') != tf:
            continue
        orig = ob['direction']
        if orig not in ('BUY', 'SELL'):
            continue
        direction = 'BUY' if orig == 'SELL' else 'SELL'
        low, high = float(ob['level_low']), float(ob['level_high'])
        formed = _time(ob['at'])
        formation_idx = next((i for i, b in enumerate(bars) if _time(b['closes']) == formed), None)
        if formation_idx is None:
            continue  # no reproducible, closed formation candle; fail closed
        start = formation_idx
        if ob.get('confirmed_at') is not None:
            confirmed = _time(ob['confirmed_at'])
            ci = next((i for i, b in enumerate(bars) if _time(b['closes']) == confirmed), None)
            if ci is None:
                continue
            start = max(start, ci)
        breakout = None
        for i in range(start+1, len(bars)):
            buffer = cfg.breaker_break_atr * atr[i-1]
            if ((direction == 'BUY' and bars[i]['close'] > high + buffer) or
                (direction == 'SELL' and bars[i]['close'] < low - buffer)):
                breakout = i
                break
        if breakout is None:
            continue
        if len(bars)-1 - breakout > cfg.breaker_max_age_bars:
            continue
        status = 'AWAITING_RETEST'
        retest_idx = None
        for i in range(breakout+1, len(bars)):
            b = bars[i]
            # Close beyond the far side is a mechanical invalidation.
            if ((direction == 'BUY' and b['close'] < low) or
                (direction == 'SELL' and b['close'] > high)):
                status = 'INVALIDATED'
                break
            overlap = b['low'] <= high and b['high'] >= low
            rejected = b['close'] > high if direction == 'BUY' else b['close'] < low
            if status == 'AWAITING_RETEST' and overlap and rejected:
                status = 'RETEST_CONFIRMED'
                retest_idx = i
        result.append({'id': _make_id(tf, 'BREAKER_' + direction, bars[breakout]['time'], ob['id']),
                       'kind': 'BREAKER_CANDIDATE', 'direction': direction, 'timeframe': tf,
                       'at': bars[retest_idx]['closes'] if retest_idx is not None else bars[breakout]['closes'],
                       'level_low': low, 'level_high': high,
                       'original_ob_id': ob['id'],
                       'source_times': [*ob.get('source_times', []), bars[breakout]['time']] + ([bars[retest_idx]['time']] if retest_idx is not None else []),
                       'breakout_at': bars[breakout]['closes'],
                       'retest_at': bars[retest_idx]['closes'] if retest_idx is not None else None,
                       'status': status})
    return result[-20:]


def analyze_confluence_frame(bars: list[dict[str, Any]], tf: str,
                             smc_frame: dict[str, Any] | None = None,
                             settings: ConfluenceSettings | None = None,
                             *, as_of: datetime | None = None) -> dict[str, Any]:
    cfg = settings or ConfluenceSettings()
    _validate(bars, tf, as_of)
    if len(bars) < 210:
        raise ValueError('EMA200 requires >=210 closed candles')
    if smc_frame is None:
        smc_frame = analyze_frame(bars, tf, as_of=as_of)
    if smc_frame['timeframe'] != tf or smc_frame['last_closed_at'] != bars[-1]['closes']:
        raise ValueError('SMC frame does not correspond to current closed candles')
    closes = [float(b['close']) for b in bars]
    if any(not isfinite(c) for c in closes):
        raise ValueError('Non-finite prices')
    ema20 = _ema_series(closes, 20)
    ema50 = _ema_series(closes, 50)
    ema200 = _ema_series(closes, 200)
    rsi = _rsi_series(closes, cfg.rsi_period)
    ema12, ema26 = _ema_series(closes,12), _ema_series(closes,26)
    macd = [a-b for a,b in zip(ema12, ema26)]
    signal = _ema_series(macd,9)
    cross = ('BULLISH_CROSS' if macd[-2] <= signal[-2] and macd[-1] > signal[-1]
             else 'BEARISH_CROSS' if macd[-2] >= signal[-2] and macd[-1] < signal[-1]
             else 'NONE')
    atr = _atr(bars)
    divergence = _divergences(bars, rsi, atr, tf, cfg)
    breaker = _breaker_candidates(bars, atr, tf, smc_frame['events'], cfg)
    return {'timeframe': tf, 'last_closed_at': bars[-1]['closes'],
            'indicators': {'ema20': round(ema20[-1], 5), 'ema50': round(ema50[-1], 5),
                           'ema200': round(ema200[-1], 5),
                           'rsi_period': cfg.rsi_period, 'rsi_value': round(rsi[-1], 3) if rsi[-1] is not None else None,
                           'rsi14': round(rsi[-1], 3) if cfg.rsi_period == 14 and rsi[-1] is not None else None,
                           'macd_line': round(macd[-1], 6), 'macd_signal': round(signal[-1],6),
                           'macd_histogram': round(macd[-1]-signal[-1],6),
                           'macd_crossover_on_last_closed_bar': cross,
                           'price_vs_ema50': 'ABOVE' if closes[-1]>ema50[-1] else 'BELOW' if closes[-1]<ema50[-1] else 'AT',
                           'price_vs_ema200': 'ABOVE' if closes[-1]>ema200[-1] else 'BELOW' if closes[-1]<ema200[-1] else 'AT'},
            'divergences': divergence, 'breakers': breaker,
            'warnings': ['Indicators and candidate breaker geometry are context only, not trade approval.',
                         'RSI divergence is only knowable after the second price pivot is confirmed.',
                         'No empirical profitability or optimal thresholds are claimed.']}


def verify_breaker_retest(confluence_frame: dict[str, Any], evidence_id: str, direction: str) -> dict[str, Any]:
    matches = [x for x in confluence_frame['breakers'] if x['id'] == evidence_id]
    if len(matches) != 1 or matches[0]['direction'] != direction or matches[0]['status'] != 'RETEST_CONFIRMED':
        raise ValueError('Breaker is not verified for this direction and current closed-candle snapshot')
    return matches[0]
