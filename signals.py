"""Deterministic event detection from strictly closed Gate OHLCV bars.

Timestamps are Unix seconds for the start of each interval. Scores rank event
quality on a common 0–100 scale. They are not probabilities or order inputs.
"""

from dataclasses import dataclass
from math import isfinite
from statistics import median


BAR_SECONDS = 900
HOUR_SECONDS = 3600
HISTORY_BARS = 65
HOURLY_BARS = 24
LEVEL_BARS = 20
BASELINE_BARS = 32
RETEST_BARS = 8


@dataclass(frozen=True)
class Bar:
    t: int
    o: float
    h: float
    l: float
    c: float
    v: float


@dataclass(frozen=True)
class Signal:
    symbol: str
    t: int
    family: str
    direction: str
    score: float
    level: float
    close: float
    atr: float
    volume_ratio: float
    trend: str
    reasons: tuple[str, ...]


def _valid_series(bars: list[Bar], interval: int) -> bool:
    for bar in bars:
        if not all(isfinite(value) for value in (bar.o, bar.h, bar.l, bar.c, bar.v)):
            return False
        if not (0 < bar.l <= min(bar.o, bar.c) <= max(bar.o, bar.c) <= bar.h and bar.v >= 0):
            return False
    return all(new.t - old.t == interval for old, new in zip(bars, bars[1:]))


def _atr(bars: list[Bar]) -> float:
    ranges = [max(new.h - new.l, abs(new.h - old.c), abs(new.l - old.c))
              for old, new in zip(bars, bars[1:])]
    typical = median(ranges)
    # Cap unusually large true ranges so that one bad print does not set the scale.
    return sum(min(value, 3 * typical) for value in ranges) / len(ranges)


def _volume_baseline(bars: list[Bar]) -> float:
    volumes = [bar.v for bar in bars[-BASELINE_BARS:] if bar.v > 0]
    if len(volumes) < 24:
        return 0.0
    return median(volumes)


def _ema(prices: list[float], period: int) -> float:
    average = prices[0]
    weight = 2 / (period + 1)
    for price in prices[1:]:
        average += weight * (price - average)
    return average


def _hourly_trend(bars: list[Bar]) -> str:
    prices = [bar.c for bar in bars]
    scale = _atr(bars)
    fast = _ema(prices, 6)
    spread = fast - _ema(prices, 18)
    slope = fast - _ema(prices[:-3], 6)
    if spread > 0.25 * scale and slope > 0.12 * scale:
        return "up"
    if spread < -0.25 * scale and slope < -0.12 * scale:
        return "down"
    return "range"


def _points(value: float, low: float, high: float, weight: float) -> float:
    return weight * max(0.0, min(1.0, (value - low) / (high - low)))


def _context_points(trend: str, direction: str) -> float:
    if trend == direction:
        return 8
    if trend == "range":
        return 2
    return -5


def _make_signal(
    symbol: str,
    candidate: Bar,
    family: str,
    direction: str,
    score: float,
    level: float,
    atr: float,
    volume_ratio: float,
    trend: str,
    reasons: tuple[str, ...],
) -> Signal:
    return Signal(symbol, candidate.t, family, direction, round(min(100, max(0, score)), 2),
                  level, candidate.c, atr, volume_ratio, trend, reasons)


def _sweep_reclaim(
    symbol: str,
    bars: list[Bar],
    atr: float,
    volume_ratio: float,
    trend: str,
    below: float,
    above: float,
    low: float,
    high: float,
) -> Signal | None:
    bar = bars[-1]
    span = bar.h - bar.l
    if span < 0.75 * atr or volume_ratio < 1.05:
        return None

    if below >= 0.10:
        direction, level, penetration = "up", low, below
        reclaim = (bar.c - level) / atr
        wick = (min(bar.o, bar.c) - bar.l) / span
        close_location = (bar.c - bar.l) / span
    elif above >= 0.10:
        direction, level, penetration = "down", high, above
        reclaim = (level - bar.c) / atr
        wick = (bar.h - max(bar.o, bar.c)) / span
        close_location = (bar.h - bar.c) / span
    else:
        return None

    if penetration > 1.35 or reclaim < 0.12 or wick < 0.32 or close_location < 0.62:
        return None

    score = (52 + _points(penetration, 0.10, 0.65, 7)
             + _points(reclaim, 0.12, 0.90, 9)
             + _points(wick, 0.32, 0.70, 10)
             + _points(volume_ratio, 1.05, 2.70, 10)
             + _points(close_location, 0.62, 0.95, 4)
             + _context_points(trend, direction))
    edge = "低点" if direction == "up" else "高点"
    reasons = (
        f"越过前{LEVEL_BARS}根{edge} {penetration:.2f} ATR 后收回 {reclaim:.2f} ATR",
        f"拒绝影线占振幅 {wick:.0%}，相对成交量 {volume_ratio:.2f} 倍",
    )
    return _make_signal(symbol, bar, "sweep_reclaim", direction, score, level, atr,
                        volume_ratio, trend, reasons)


def _holds_retest(bar: Bar, level: float, direction: str, atr: float) -> bool:
    span = bar.h - bar.l
    if span < 0.50 * atr:
        return False
    signed = 1 if direction == "up" else -1
    extreme = bar.l if direction == "up" else bar.h
    distance = signed * (extreme - level) / atr
    reclaim = signed * (bar.c - level) / atr
    return -0.45 <= distance <= 0.24 and reclaim >= 0.25 and (reclaim - distance) * atr / span >= 0.60


def _breakout_retest(
    symbol: str,
    bars: list[Bar],
    atr: float,
    volume_ratio: float,
    trend: str,
) -> Signal | None:
    bar = bars[-1]
    if volume_ratio < 0.65:
        return None

    for position in range(len(bars) - 2, len(bars) - 2 - RETEST_BARS, -1):
        breakout = bars[position]
        span = breakout.h - breakout.l
        body = abs(breakout.c - breakout.o)
        if span <= 0 or body / span < 0.48:
            continue
        previous = bars[position - LEVEL_BARS:position]
        up = breakout.c > breakout.o
        direction = "up" if up else "down"
        signed = 1 if up else -1
        level = max(item.h for item in previous) if up else min(item.l for item in previous)
        extension = signed * (breakout.c - level) / atr
        if extension < 0.28 or signed * (breakout.o - level) > 0.30 * atr:
            continue
        if body < 0.65 * atr or not _holds_retest(bar, level, direction, atr):
            continue

        between = bars[position + 1:-1]
        if any(signed * (item.c - level) < -0.15 * atr for item in between):
            continue
        if any(_holds_retest(item, level, direction, atr) for item in between):
            continue

        baseline = _volume_baseline(bars[:position])
        if not baseline or breakout.v / baseline < 1.30:
            continue
        breakout_volume = breakout.v / baseline
        reclaim = signed * (bar.c - level) / atr
        score = (55 + _points(extension, 0.28, 1.30, 8)
                 + _points(breakout_volume, 1.30, 3.00, 10)
                 + _points(reclaim, 0.25, 1.25, 10)
                 + _points(volume_ratio, 0.65, 2.20, 7)
                 + _context_points(trend, direction))
        reasons = (
            f"{len(bars) - position - 1} 根前放量收盘突破前{LEVEL_BARS}根区间，相对量 {breakout_volume:.2f} 倍",
            f"本根首次回测原区间边界并收回 {reclaim:.2f} ATR，相对量 {volume_ratio:.2f} 倍",
        )
        return _make_signal(symbol, bar, "breakout_retest", direction, score, level, atr,
                            volume_ratio, trend, reasons)
    return None


def detect(symbol: str, bars: list[Bar], hourly: list[Bar]) -> list[Signal]:
    """Return events for the last supplied closed 15-minute bar, newest last."""
    if len(bars) < HISTORY_BARS:
        return []
    bars = bars[-HISTORY_BARS:]
    candidate = bars[-1]
    evaluation_time = candidate.t + BAR_SECONDS
    hourly = [bar for bar in hourly if bar.t + HOUR_SECONDS <= evaluation_time][-HOURLY_BARS:]
    if len(hourly) < HOURLY_BARS or hourly[-1].t + 2 * HOUR_SECONDS <= evaluation_time:
        return []
    if not _valid_series(bars, BAR_SECONDS) or not _valid_series(hourly, HOUR_SECONDS):
        return []

    previous = bars[:-1]
    atr = _atr(previous[-(BASELINE_BARS + 1):])
    volume = _volume_baseline(previous)
    if not isfinite(atr) or atr <= 0 or not volume or candidate.v <= 0:
        return []
    if candidate.h - candidate.l > 3.75 * atr:
        return []
    if any(abs(new.o - old.c) > max(3 * atr, 0.02 * old.c)
           for old, new in zip(bars, bars[1:])):
        return []

    levels = previous[-LEVEL_BARS:]
    low = min(bar.l for bar in levels)
    high = max(bar.h for bar in levels)
    below = (low - candidate.l) / atr
    above = (candidate.h - high) / atr
    if below >= 0.10 and above >= 0.10:
        return []

    trend = _hourly_trend(hourly)
    volume_ratio = candidate.v / volume
    events = [
        _sweep_reclaim(symbol, bars, atr, volume_ratio, trend, below, above, low, high),
        _breakout_retest(symbol, bars, atr, volume_ratio, trend),
    ]
    result = [signal for signal in events if signal is not None]
    if len({signal.direction for signal in result}) > 1:
        return []
    return sorted(result, key=lambda signal: signal.score, reverse=True)
