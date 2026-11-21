from collections import Counter
from dataclasses import dataclass

from market import Market
from signals import Bar, HISTORY_BARS, HOURLY_BARS, Signal, detect


QUARTER = 15 * 60
HOUR = 60 * 60
DAY = 24 * HOUR
CLOSE_GRACE = 45
MAX_CATCHUP = 4
COOLDOWN = 90 * 60
COHORT_LIMIT = 2


@dataclass(frozen=True)
class Candidate:
    market: Market
    signal: Signal


def align(timestamp: int, period: int) -> int:
    return timestamp // period * period


def latest_bar(now: int) -> int:
    return align(now - CLOSE_GRACE, QUARTER) - QUARTER


def index(bars: list[Bar]) -> dict[int, Bar]:
    return {bar.t: bar for bar in bars}


def get_window(bars: dict[int, Bar], last: int, period: int, count: int) -> list[Bar] | None:
    first = last - (count - 1) * period
    window = [bars.get(t) for t in range(first, last + 1, period)]
    if any(bar is None for bar in window):
        return None
    return [bar for bar in window if bar is not None]


def discontinuity(bars: list[Bar]) -> bool:
    # Discrete price resets such as a stock split must leave the entire lookback.
    return any(not 0.65 <= new.o / old.c <= 1.55 for old, new in zip(bars, bars[1:]))


def inspect_bar(
    market: Market,
    at: int,
    quarter: dict[int, Bar],
    hour: dict[int, Bar],
) -> tuple[str, list[Candidate]]:
    q = get_window(quarter, at, QUARTER, HISTORY_BARS)
    h_last = align(at + QUARTER, HOUR) - HOUR
    h = get_window(hour, h_last, HOUR, HOURLY_BARS)
    if q is None or h is None:
        return 'data_gap', []
    if discontinuity(q) or discontinuity(h):
        return 'price_reset', []
    if q[-1].v <= 0:
        return 'no_volume', []
    return 'checked', [Candidate(market, signal) for signal in detect(market.symbol, q, h)]


def select(
    candidates: list[Candidate],
    threshold: float,
    last_by_symbol: dict[str, int] | None = None,
    cohort_counts: dict[tuple[int, str, str], int] | None = None,
) -> list[Candidate]:
    last = dict(last_by_symbol or {})
    cohorts = Counter(cohort_counts or {})
    selected = []
    for item in sorted(candidates, key=lambda x: (x.signal.t, -x.signal.score, x.signal.symbol, x.signal.family)):
        signal = item.signal
        cohort = (signal.t, item.market.category, signal.direction)
        if (signal.score < threshold or signal.t - last.get(signal.symbol, -COOLDOWN) < COOLDOWN
                or cohorts[cohort] >= COHORT_LIMIT):
            continue
        selected.append(item)
        cohorts[cohort] += 1
        last[signal.symbol] = signal.t
    return selected
