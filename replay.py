from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from statistics import median

from market import GateClient, Market, MarketError
from scanning import DAY, HOUR, QUARTER, Candidate, align, index, inspect_bar, latest_bar, select
from signals import HISTORY_BARS, HOURLY_BARS


def historical_index(client: GateClient, symbol: str, interval: str, period: int, first: int, last: int):
    candles = {}
    batch = 900 * period
    for start in range(first, last + 1, batch):
        end = min(start + batch - period, last)
        candles.update(index(client.candles(symbol, interval, start=start, end=end)))
    return candles


def replay_market(client: GateClient, market: Market, first: int, stop: int, now: int):
    quarter = historical_index(client, market.symbol, '15m', QUARTER, first - (HISTORY_BARS - 1) * QUARTER,
                               min(latest_bar(now), stop + 16 * QUARTER))
    hour = historical_index(client, market.symbol, '1h', HOUR, align(first, HOUR) - HOURLY_BARS * HOUR,
                            stop - HOUR)
    candidates = []
    outcomes = {}
    coverage = Counter()
    for start in range(first, stop, QUARTER):
        status, found = inspect_bar(market, start, quarter, hour)
        coverage[status] += 1
        candidates.extend(found)
        for hours in (1, 4):
            step = hours * 4
            if found and all(start + i * QUARTER in quarter for i in range(1, step + 1)):
                outcomes[start, hours] = quarter[start + step * QUARTER].c
    return candidates, coverage, outcomes


def period_metrics(selected: list[Candidate], outcomes: dict, start: int, stop: int) -> dict:
    dates = {t // DAY: 0 for t in range(start, stop, DAY)}
    groups = Counter()
    families = Counter()
    moves = {1: [], 4: []}
    for item in selected:
        signal = item.signal
        if not start <= signal.t < stop:
            continue
        dates[signal.t // DAY] += 1
        groups[item.market.category] += 1
        families[signal.family] += 1
        for hours in moves:
            future = outcomes.get((signal.symbol, signal.t, hours))
            if future is not None:
                sign = 1 if signal.direction == 'up' else -1
                moves[hours].append(sign * (future - signal.close) / signal.atr)
    count = sum(dates.values())
    result = {'count': count, 'per_day': round(count / len(dates), 2),
              'by_day_utc': {datetime.fromtimestamp(day * DAY, timezone.utc).date().isoformat(): size
                             for day, size in dates.items()}, 'by_category': dict(groups), 'by_family': dict(families)}
    for hours, values in moves.items():
        result[f'move_{hours}h'] = {
            'n': len(values),
            'positive_pct': round(100 * sum(value > 0 for value in values) / len(values), 1) if values else None,
            'median_atr': round(median(values), 3) if values else None,
        }
    return result


def run_replay(client: GateClient, limit: int, days: int, now: int, threshold: float, workers: int) -> dict:
    stop = align(now, DAY)
    first = stop - days * DAY
    markets = client.markets(limit)
    candidates = []
    outcomes = {}
    coverage = Counter()
    errors = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        tasks = [(market, pool.submit(replay_market, client, market, first, stop, now)) for market in markets]
        for market, task in tasks:
            try:
                found, counted, later = task.result()
            except MarketError as error:
                errors[market.symbol] = str(error)
                continue
            coverage.update(counted)
            candidates.extend(found)
            outcomes.update({(market.symbol, start, hours): price for (start, hours), price in later.items()})

    by_threshold = {}
    split = stop - min(7, days // 3) * DAY
    for cut in sorted({80.0, 84.0, 88.0, 90.0, 92.0, 93.0, 94.0, 95.0, 96.0, 97.0, 98.0, threshold}):
        selected = select(candidates, cut)
        by_threshold[f'{cut:g}'] = {'all': period_metrics(selected, outcomes, first, stop)}
        if split < stop:
            by_threshold[f'{cut:g}'].update(earlier=period_metrics(selected, outcomes, first, split),
                                          later=period_metrics(selected, outcomes, split, stop))
    return {'utc_days': days, 'first_utc': datetime.fromtimestamp(first, timezone.utc).date().isoformat(),
            'last_utc': datetime.fromtimestamp(stop - DAY, timezone.utc).date().isoformat(),
            'universe': len(markets), 'processed_markets': len(markets) - len(errors),
            'universe_categories': dict(Counter(m.category for m in markets)),
            'expected_bars': len(markets) * days * DAY // QUARTER, 'coverage': dict(coverage), 'errors': errors,
            'candidates': len(candidates), 'thresholds': by_threshold,
            'limits': '使用运行时名单回放，不含退市标的，后续价格变化按入选时 ATR 标准化。'}
