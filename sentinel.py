import argparse
import json
import logging
import os
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import notifications
from market import GateClient, Market, MarketError
from replay import run_replay
from scanning import HOUR, MAX_CATCHUP, QUARTER, Candidate, align, index, inspect_bar, latest_bar
from signals import HISTORY_BARS, HOURLY_BARS
from state import State


LOG = logging.getLogger('sentinel')
BEIJING = timezone(timedelta(hours=8))
FAMILIES = {'sweep_reclaim': '刺破收回', 'breakout_retest': '突破后回踩确认'}
TRENDS = {'up': '上行', 'down': '下行', 'range': '区间'}
DEFAULT_SCORE = 94.0


def scan_market(client: GateClient, market: Market, latest: int, cursor: int | None):
    first = latest if cursor is None else max(cursor + QUARTER, latest - (MAX_CATCHUP - 1) * QUARTER)
    if first > latest:
        return cursor, 'already_checked', [], 0

    quarter = index(client.candles(market.symbol, '15m', limit=HISTORY_BARS + MAX_CATCHUP + 3))
    hour = index(client.candles(market.symbol, '1h', limit=HOURLY_BARS + 4))
    progressed, candidates = cursor, []
    gaps = 0
    for at in range(first, latest + 1, QUARTER):
        status, found = inspect_bar(market, at, quarter, hour)
        if status != 'data_gap':
            progressed = at
            candidates.extend(found)
        else:
            gaps += 1
    return progressed, status, candidates, gaps


def digest(items: list[Candidate]) -> tuple[str, str]:
    lines = ['Gate 永续行情观察', '时间为北京时间，价格为该根 15 分钟 K 线收盘时的数据。']
    for item in items:
        s, market = item.signal, item.market
        stamp = datetime.fromtimestamp(s.t + QUARTER, BEIJING).strftime('%m-%d %H:%M')
        direction = '向上观察' if s.direction == 'up' else '向下观察'
        category = '加密' if market.category == 'crypto' else 'TradFi'
        funding = '未知' if market.funding_pct is None else f'{market.funding_pct:+.4f}%'
        evidence = '，'.join(s.reasons)
        lines.extend([
            '', f'{stamp} {s.symbol} · {category} · {FAMILIES.get(s.family, s.family)} · {direction} · {s.score:g} 分',
            f'收盘 {s.close:.10g}，参考结构位 {s.level:.10g}，ATR {s.atr:.8g}，相对成交量 {s.volume_ratio:.2f} 倍',
            f'1H 结构 {TRENDS.get(s.trend, s.trend)}，{evidence}',
            f'扫描时 24h 成交额 {market.quote_volume / 1e6:.1f}M USDT，涨跌 {market.change_pct:+.2f}%，资金费率 {funding}',
        ])
    return f'Gate 行情观察 · {len(items)} 条', '\n'.join(lines)


def deliver(state: State, channels: tuple[str, ...], now: int) -> int:
    failures = 0
    for entry in state.pending(now):
        digest_id, channel = entry['id'], entry['channel']
        if channel not in channels or not state.claim(digest_id, channel):
            continue
        try:
            receipt = notifications.send(channel, entry['title'], entry['body'])
        except notifications.DeliveryError as error:
            LOG.error('推送需要核对，批次 %s，渠道 %s，%s', digest_id, channel, error)
            state.finish(digest_id, channel, 'review', str(error))
            failures += 1
        else:
            LOG.info('批次 %s 已由 %s 服务受理，回执 %s', digest_id, channel, receipt)
            state.finish(digest_id, channel, 'accepted', receipt)
    return failures


def scan(client: GateClient, state: State, args, now: int) -> int:
    channels = () if args.dry_run else notifications.available_channels()
    if not channels and not args.dry_run:
        LOG.warning('未配置已启用的提醒渠道，扫描结果将仅写入日志和运行状态。')
    markets = client.markets(args.symbols)
    latest = latest_bar(now)
    previous = state.cursors()
    cursors = {}
    candidates = []
    status_counts = Counter()
    errors = {}
    gap_symbols = []
    gaps = 0
    started = time.monotonic()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = {pool.submit(scan_market, client, market, latest, previous.get(market.symbol)): market for market in markets}
        for job in as_completed(jobs):
            market = jobs[job]
            try:
                progressed, status, found, missing = job.result()
            except MarketError as error:
                errors[market.symbol] = str(error)
                status_counts['fetch_error'] += 1
                continue
            status_counts[status] += 1
            gaps += missing
            if missing:
                gap_symbols.append(market.symbol)
            if progressed is not None:
                cursors[market.symbol] = progressed
            candidates.extend(found)

    accepted, digest_id = state.record(cursors, candidates, args.threshold, channels, now, digest)
    if accepted:
        title, body = digest(accepted)
        print(f'{title}\n{body}', flush=True)
        if (digest_id is not None and args.deepseek and os.environ.get('DEEPSEEK_API_KEY')
                and state.reserve_ai(now, args.ai_daily_cap)):
            try:
                text, usage = notifications.explain(body)
            except notifications.DeliveryError as error:
                LOG.warning('DeepSeek 摘要暂不可用，发送规则信号，%s', error)
            else:
                state.note_ai_usage(now, usage)
                if text:
                    state.append_summary(digest_id, text)

    failures = deliver(state, channels, int(time.time()))
    soft_coverage = len(markets) - status_counts['data_gap'] - status_counts['fetch_error']
    health = 'ok' if len(markets) >= args.symbols and soft_coverage >= 0.8 * len(markets) else 'degraded'
    summary = {'health': health, 'latest_bar_start_utc': datetime.fromtimestamp(latest, timezone.utc).isoformat(),
               'requested': args.symbols, 'selected': len(markets), 'coverage': soft_coverage,
               'universe': dict(Counter(market.category for market in markets)), 'status': dict(status_counts),
               'missing_slots': gaps, 'gap_symbols': sorted(gap_symbols), 'errors': errors,
               'candidates': len(candidates), 'passed': len(accepted), 'threshold': args.threshold,
               'channels': channels, 'delivery_needs_review': failures, 'seconds': round(time.monotonic() - started, 2)}
    state.note_scan(now, summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    if target := os.getenv('GITHUB_STEP_SUMMARY'):
        with Path(target).open('a', encoding='utf-8') as report:
            report.write('## Gate 行情哨兵\n\n' + '\n'.join((
                f'- 覆盖 {soft_coverage}/{len(markets)}、目标 {args.symbols}',
                f'- 候选 {len(candidates)}、发出观察 {len(accepted)}、分数门槛 {args.threshold:g}',
                f'- 缺失时段 {gaps}、提醒需核对 {failures}、状态 {health}',
            )) + '\n')
    return int(health != 'ok' or bool(failures))


def cli():
    parser = argparse.ArgumentParser(description='扫描 Gate 公共行情并发送只读观察提醒')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--watch', action='store_true', help='常驻模式，按 UTC 每刻钟收盘后 3 分钟扫描')
    mode.add_argument('--replay-days', type=int, metavar='N', help='回放最近 N 个完整 UTC 日，仅输出统计')
    mode.add_argument('--status', action='store_true', help='查看 SQLite 中最近一次扫描和投递回执')
    parser.add_argument('--dry-run', action='store_true', help='扫描公开行情，使用临时状态和控制台输出')
    parser.add_argument('--state', default=os.getenv('SENTINEL_STATE', '.sentinel/state.sqlite3'))
    parser.add_argument('--symbols', type=int, default=50)
    parser.add_argument('--threshold', type=float, default=os.getenv('SENTINEL_SCORE') or DEFAULT_SCORE)
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--deepseek', action='store_true', default=os.getenv('SENTINEL_DEEPSEEK') == '1')
    parser.add_argument('--ai-daily-cap', type=int, default=os.getenv('SENTINEL_AI_DAILY_CAP') or 8)
    args = parser.parse_args()
    if not (1 <= args.symbols <= 50 and 1 <= args.workers <= 8 and 0 <= args.threshold <= 100
            and 0 <= args.ai_daily_cap <= 20):
        parser.error('symbols 需在 1–50，workers 需在 1–8，threshold 需在 0–100，ai-daily-cap 需在 0–20')
    if args.replay_days is not None and not 1 <= args.replay_days <= 28:
        parser.error('replay-days 需在 1–28')
    if args.status and args.dry_run:
        parser.error('status 需要读取已保存状态')
    return args


def main() -> int:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    args = cli()
    client = GateClient()
    try:
        if args.replay_days is not None:
            report = run_replay(client, args.symbols, args.replay_days, int(time.time()), args.threshold, args.workers)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return int(report['coverage'].get('checked', 0) == 0)
        if args.status and args.state != ':memory:' and not Path(args.state).is_file():
            print('暂无状态文件，扫描一次后可以查询。')
            return 0
        with State(':memory:' if args.dry_run else args.state) as state:
            if args.status:
                print(json.dumps(state.report(), ensure_ascii=False, indent=2))
                return 0
            while True:
                try:
                    result = scan(client, state, args, int(time.time()))
                except (MarketError, notifications.DeliveryError) as error:
                    LOG.error('巡检中断，%s', error)
                    result = 2
                if not args.watch:
                    return result
                now = time.time()
                next_scan = align(int(now) - 180, QUARTER) + QUARTER + 180
                time.sleep(max(0.0, next_scan - now))
    except MarketError as error:
        LOG.error('行情不可用，%s', error)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
