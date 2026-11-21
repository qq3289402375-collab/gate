import unittest
from dataclasses import replace

from signals import BAR_SECONDS, HISTORY_BARS, HOUR_SECONDS, HOURLY_BARS, Bar, detect


EVALUATION_TIME = 1000 * HOUR_SECONDS


def bars_15m() -> list[Bar]:
    return [Bar(t, 100.0, 101.0, 99.0, 100.0, 100.0)
            for t in range(EVALUATION_TIME - HISTORY_BARS * BAR_SECONDS, EVALUATION_TIME, BAR_SECONDS)]


def bars_1h(slope: float = 0.25) -> list[Bar]:
    times = range(EVALUATION_TIME - HOURLY_BARS * HOUR_SECONDS, EVALUATION_TIME, HOUR_SECONDS)
    result = []
    for index, t in enumerate(times):
        close = 100.0 + index * slope
        result.append(Bar(t, close - slope / 2, close + 0.6, close - 0.6, close, 500.0))
    return result


def sweep() -> list[Bar]:
    bars = bars_15m()
    bars[-1] = Bar(bars[-1].t, 99.85, 101.02, 98.20, 100.70, 265.0)
    return bars


def breakout_retest() -> list[Bar]:
    bars = bars_15m()
    bars[-4] = Bar(bars[-4].t, 100.40, 103.10, 100.20, 102.65, 330.0)
    bars[-3] = Bar(bars[-3].t, 102.65, 103.60, 101.90, 103.20, 120.0)
    bars[-2] = Bar(bars[-2].t, 103.20, 104.00, 102.60, 102.65, 105.0)
    bars[-1] = Bar(bars[-1].t, 102.65, 103.55, 100.80, 103.15, 150.0)
    return bars


def mirror(bar: Bar) -> Bar:
    return Bar(bar.t, 200 - bar.o, 200 - bar.l, 200 - bar.h, 200 - bar.c, bar.v)


class SweepReclaimTests(unittest.TestCase):
    def test_supported_long_and_short_shapes(self):
        ascending = sweep()
        descending = [mirror(bar) for bar in ascending]
        positive, = detect("BTC_USDT", ascending, bars_1h())
        negative, = detect("ETH_USDT", descending, bars_1h(-0.25))

        self.assertEqual((positive.symbol, positive.t), ("BTC_USDT", ascending[-1].t))
        self.assertEqual((positive.family, negative.family), ("sweep_reclaim", "sweep_reclaim"))
        self.assertEqual((positive.direction, negative.direction), ("up", "down"))
        self.assertEqual((positive.level, negative.level), (99.0, 101.0))
        self.assertAlmostEqual(positive.score, negative.score)
        self.assertEqual(positive.volume_ratio, 2.65)
        self.assertEqual(positive.atr, 2.0)
        self.assertTrue(0 < positive.score <= 100)

    def test_double_sweep_and_flat_bar_cannot_signal(self):
        bars = sweep()
        bars[-1] = Bar(bars[-1].t, 100.0, 102.0, 98.0, 100.0, 350.0)
        self.assertEqual(detect("BTC_USDT", bars, bars_1h()), [])
        bars[-1] = Bar(bars[-1].t, 100.0, 100.0, 100.0, 100.0, 350.0)
        self.assertEqual(detect("BTC_USDT", bars, bars_1h()), [])

    def test_price_reclaim_without_volume_is_rejected(self):
        bars = sweep()
        bars[-1] = replace(bars[-1], v=50.0)
        self.assertEqual(detect("BTC_USDT", bars, bars_1h()), [])

    def test_outlying_prior_volume_does_not_set_baseline(self):
        bars = sweep()
        bars[-7] = replace(bars[-7], v=20_000.0)
        signal, = detect("BTC_USDT", bars, bars_1h())
        self.assertEqual(signal.volume_ratio, 2.65)


class BreakoutRetestTests(unittest.TestCase):
    def test_breakout_followed_by_first_retest_signals(self):
        bars = breakout_retest()
        positive, = detect("BTC_USDT", bars, bars_1h())
        negative, = detect("BTC_USDT", [mirror(bar) for bar in bars], bars_1h(-0.25))
        self.assertEqual((positive.family, negative.family), ("breakout_retest", "breakout_retest"))
        self.assertEqual((positive.direction, negative.direction), ("up", "down"))
        self.assertEqual((positive.level, negative.level), (101.0, 99.0))
        self.assertAlmostEqual(positive.score, negative.score)

    def test_invalidation_before_retest_blocks_event(self):
        bars = breakout_retest()
        bars[-2] = Bar(bars[-2].t, 102.9, 103.5, 100.0, 100.1, 100.0)
        self.assertEqual(detect("BTC_USDT", bars, bars_1h()), [])

    def test_a_second_retest_of_the_same_breakout_does_not_repeat(self):
        bars = breakout_retest()
        bars[-2] = replace(bars[-1], t=bars[-2].t)
        self.assertEqual(detect("BTC_USDT", bars, bars_1h()), [])

    def test_a_breakout_alone_is_not_a_confirmed_retest(self):
        bars = bars_15m()
        bars[-1] = breakout_retest()[-4]
        bars[-1] = replace(bars[-1], t=EVALUATION_TIME - BAR_SECONDS)
        self.assertEqual(detect("BTC_USDT", bars, bars_1h()), [])


class TimingAndQualityTests(unittest.TestCase):
    def test_closed_hour_at_evaluation_time_is_visible_and_open_hour_is_ignored(self):
        hourly = bars_1h()
        baseline = detect("BTC_USDT", sweep(), hourly)
        open_hour = Bar(EVALUATION_TIME, 80, 80, 75, 75, 900)
        self.assertEqual(detect("BTC_USDT", sweep(), [*hourly, open_hour]), baseline)
        self.assertEqual(detect("BTC_USDT", sweep(), [*hourly[:-1], open_hour]), [])

    def test_hourly_bias_changes_score_but_not_sweep_definition(self):
        aligned, = detect("BTC_USDT", sweep(), bars_1h())
        opposed, = detect("BTC_USDT", sweep(), bars_1h(-0.25))
        self.assertEqual((aligned.trend, opposed.trend), ("up", "down"))
        self.assertEqual((aligned.level, aligned.direction), (opposed.level, opposed.direction))
        self.assertGreater(aligned.score, opposed.score)

    def test_missing_and_malformed_candles_do_not_produce_signals(self):
        bars = sweep()
        self.assertEqual(detect("BTC_USDT", bars[1:], bars_1h()), [])
        bars[-5] = replace(bars[-5], t=bars[-6].t)
        self.assertEqual(detect("BTC_USDT", bars, bars_1h()), [])
        bars = sweep()
        bars[-1] = replace(bars[-1], h=float("nan"))
        self.assertEqual(detect("BTC_USDT", bars, bars_1h()), [])

    def test_split_like_gap_disqualifies_window(self):
        bars = sweep()
        bars[0] = Bar(bars[0].t, 800.0, 801.0, 799.0, 800.0, 110.0)
        self.assertEqual(detect("BTC_USDT", bars, bars_1h()), [])


if __name__ == "__main__":
    unittest.main()
