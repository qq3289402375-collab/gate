import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from notifications import DeliveryError
from market import Market
from replay import historical_index
from scanning import COOLDOWN, DAY, QUARTER, Candidate, inspect_bar, latest_bar, select
from sentinel import deliver, scan_market
from signals import Bar, HISTORY_BARS, HOURLY_BARS, Signal
from state import State


MARKET = Market('BTC_USDT', 'crypto', 150e6, 1.0, 0.01)
START = 2_000_000 * QUARTER


def item(symbol='BTC_USDT', t=START, score=90, category='crypto', direction='up'):
    market = Market(symbol, category, 150e6, 1.0, None)
    signal = Signal(symbol, t, 'sweep_reclaim', direction, score, 99, 100, 1, 1.5, 'up', ('原因',))
    return Candidate(market, signal)


def rows(last, count, interval, price=100):
    return {t: Bar(t, price, price + 1, price - 1, price, 100)
            for t in range(last - (count - 1) * interval, last + 1, interval)}


def render(items):
    return 'digest', f'{len(items)} signals'


class ScanTests(unittest.TestCase):
    def test_close_grace_and_exact_closed_bar(self):
        self.assertEqual(latest_bar(START + 44), START - 2 * QUARTER)
        self.assertEqual(latest_bar(START + 45), START - QUARTER)
        self.assertEqual(latest_bar(START + 180), START - QUARTER)

    def test_missing_latest_does_not_relabel_stale_data(self):
        h = rows(START - 3600, HOURLY_BARS, 3600)
        q = rows(START - QUARTER, HISTORY_BARS, QUARTER)
        client = Mock()
        client.candles.side_effect = [list(q.values()), list(h.values())]
        cursor, status, candidates, gaps = scan_market(client, MARKET, START, None)
        self.assertEqual((cursor, status, candidates, gaps), (None, 'data_gap', [], 1))

    def test_split_reference_and_hour_window_ignore_future(self):
        h = rows(START - 3600, HOURLY_BARS, 3600)
        q = rows(START, HISTORY_BARS, QUARTER)
        h[START] = Bar(START, 12, 13, 11, 12, 100)
        self.assertEqual(inspect_bar(MARKET, START, q, h)[0], 'checked')
        q[START] = Bar(START, 12, 13, 11, 12, 100)
        self.assertEqual(inspect_bar(MARKET, START, q, h)[0], 'price_reset')

    def test_ranks_correlated_alerts_without_forcing_a_quota(self):
        candidates = [item('A_USDT', score=87), item('B_USDT', score=93), item('C_USDT', score=96),
                      item('D_USDT', score=81), item('SNDK_USDT', category='tradfi'),
                      item('B_USDT', t=START + QUARTER), item('B_USDT', t=START + COOLDOWN)]
        selected = select(candidates, 84)
        self.assertEqual([x.signal.symbol for x in selected], ['C_USDT', 'B_USDT', 'SNDK_USDT', 'B_USDT'])
        self.assertEqual(select([item(score=83)], 84), [])

    def test_replay_pages_without_skipping_boundary_candles(self):
        client = Mock()
        client.candles.side_effect = lambda _, __, start, end: list(rows(
            end, 1 + (end - start) // QUARTER, QUARTER).values())
        data = historical_index(client, 'BTC_USDT', '15m', QUARTER, START, START + 900 * QUARTER)
        self.assertEqual((len(data), min(data), max(data)), (901, START, START + 900 * QUARTER))
        self.assertEqual(client.candles.call_count, 2)


class StateTests(unittest.TestCase):
    def test_atomic_digest_creation_and_repeat_scan_dedup(self):
        state = State(':memory:')
        self.addCleanup(state.db.close)
        c = {MARKET.symbol: START}
        with self.assertRaises(ZeroDivisionError):
            state.record(c, [item()], 84, ('qq',), START, lambda _: 1 / 0)
        self.assertEqual(state.cursors(), {})
        accepted, digest_id = state.record(c, [item()], 84, ('qq',), START, render)
        self.assertEqual(len(accepted), 1)
        self.assertIsNotNone(digest_id)
        self.assertEqual(state.record(c, [item()], 84, ('qq',), START + 60, render), ([], None))

    def test_ambiguous_delivery_persists_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'state.sqlite3')
            with State(path) as state:
                state.record({MARKET.symbol: START}, [item()], 84, ('qq',), START, render)
                with patch('sentinel.notifications.send', side_effect=DeliveryError('timed out')):
                    self.assertEqual(deliver(state, ('qq',), START), 1)
            with State(path) as reopened:
                self.assertEqual(reopened.report()['delivery_statuses'], {'review': 1})
                with patch('sentinel.notifications.send') as send:
                    self.assertEqual(deliver(reopened, ('qq',), START + 1), 0)
                    send.assert_not_called()

    def test_persistent_utc_ai_budget(self):
        with State(':memory:') as state:
            self.assertTrue(state.reserve_ai(START, 1))
            self.assertFalse(state.reserve_ai(START + 5, 1))
            self.assertTrue(state.reserve_ai(START + DAY, 1))


if __name__ == '__main__':
    unittest.main()
