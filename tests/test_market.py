import io
import unittest
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from market import GateClient, MarketError, PINNED_CRYPTO, PINNED_TRADFI


def ticker(symbol, volume=100, **extra):
    return {"contract": symbol, "volume_24h_quote": volume, "change_percentage": "2.5", **extra}


def contract(symbol, **extra):
    return {"name": symbol, "status": "trading", "in_delisting": False, **extra}


def candle(timestamp, **extra):
    return {"t": timestamp, "o": "9", "h": "12", "l": "8", "c": "10", "v": "20", **extra}


class GateMarketsTest(unittest.TestCase):
    def test_pinned_markets_then_ranked_crypto_excluding_other_assets(self):
        contracts = [contract(symbol) for symbol in PINNED_CRYPTO]
        product_types = {"CL_USDT": "commodities", "XAU_USDT": "metals", "XAG_USDT": "metals"}
        contracts.extend(contract(symbol, contract_type=product_types.get(symbol, "stocks")) for symbol in PINNED_TRADFI)
        pin_count = len(contracts)
        contracts.extend([
            contract("币安人生_USDT"),
            contract("DOGE_USDT", contract_type="crypto"),
            contract("COPPER_USDT", contract_type="metals"),
            contract("SOXL_USDT", contract_type="stocks"),
            contract("UNKNOWN_USDT", contract_type="new_type"),
            contract("NEW_USDT", is_pre_market=True),
            contract("PAUSED_USDT", status="circuit_breaker"),
            contract("OUT_USDT", in_delisting=True),
        ])
        tickers = [ticker(row["name"], 1000) for row in contracts]
        tickers[0]["funding_rate"] = "-0.0002"
        tickers[pin_count]["volume_24h_quote"] = None
        tickers[pin_count]["volume_24h_settle"] = "1200"

        with patch.object(GateClient, "_get", side_effect=[contracts, tickers]):
            markets = GateClient().markets(pin_count + 2)

        self.assertEqual([market.symbol for market in markets], [
            *PINNED_CRYPTO, *PINNED_TRADFI, "币安人生_USDT", "DOGE_USDT",
        ])
        self.assertEqual([market.category for market in markets[len(PINNED_CRYPTO):pin_count]], ["tradfi"] * len(PINNED_TRADFI))
        self.assertEqual(markets[0].funding_pct, -0.02)
        self.assertEqual(markets[pin_count].quote_volume, 1200)
        self.assertIsNone(markets[pin_count].funding_pct)

    def test_invalid_rows_and_missing_coverage_are_visible(self):
        contracts = [contract("BTC_USDT"), contract("DOGE_USDT"), contract("ETH_USDT")]
        tickers = [ticker("BTC_USDT"), ticker("DOGE_USDT", "NaN"), ticker("ETH_USDT"), ticker("ETH_USDT")]
        with patch.object(GateClient, "_get", side_effect=[contracts, tickers]), self.assertLogs("market") as logs:
            markets = GateClient().markets(3)
        self.assertEqual([market.symbol for market in markets], ["BTC_USDT"])
        self.assertIn("invalid numeric field", "\n".join(logs.output))
        self.assertIn("duplicate metadata for ETH_USDT", "\n".join(logs.output))
        self.assertIn("coverage is 1/3", "\n".join(logs.output))


class GateCandlesTest(unittest.TestCase):
    def test_sorts_and_collapses_duplicates_then_limits(self):
        rows = [candle(1800), candle(0), candle(900), candle(1800)]
        with patch.object(GateClient, "_get", return_value=rows) as get:
            bars = GateClient().candles("BTC_USDT", "15m", limit=2)
        self.assertEqual([bar.t for bar in bars], [900, 1800])
        self.assertEqual([bar.v for bar in bars], [20.0, 20.0])
        get.assert_called_once_with("candlesticks", {"contract": "BTC_USDT", "interval": "15m", "limit": 2})

    def test_range_uses_inclusive_end_and_ignores_unrequested_rows(self):
        rows = [candle(2700), candle(0), candle(900, v=0), candle(1800, v="0.25")]
        with patch.object(GateClient, "_get", return_value=rows) as get:
            bars = GateClient().candles("牛来_USDT", "15m", start=900, end=1800)
        self.assertEqual([bar.t for bar in bars], [900, 1800])
        self.assertEqual(bars[0].v, 0.0)
        self.assertEqual(bars[1].v, 0.25)
        get.assert_called_once_with("candlesticks", {"contract": "牛来_USDT", "interval": "15m", "from": 900, "to": 1800})

    def test_rejects_invalid_and_conflicting_bars(self):
        for rows in ([candle(900, h="9")], [candle(900, v="Infinity")], [candle(900.5)],
                     [candle(900), candle(900, c="11")], [{"t": 900}], []):
            with self.subTest(rows=rows), patch.object(GateClient, "_get", return_value=rows):
                gate = GateClient()
                if rows:
                    with self.assertRaisesRegex(MarketError, "BTC_USDT 15m"):
                        gate.candles("BTC_USDT", "15m", limit=2)
                else:
                    self.assertEqual(gate.candles("BTC_USDT", "15m", limit=2), [])

    def test_rejects_invalid_queries_before_request(self):
        gate = GateClient()
        with patch.object(GateClient, "_get") as get:
            for args, kwargs in (
                (("/spot/orders", "15m"), {}),
                (("BTC_USDT", "2h"), {}),
                (("BTC_USDT", "15m"), {"limit": True}),
                (("BTC_USDT", "15m"), {"limit": 1001}),
                (("BTC_USDT", "15m"), {"start": 300, "end": 600, "limit": 2}),
                (("BTC_USDT", "15m"), {"start": 0, "end": 900 * 1000}),
            ):
                with self.subTest(args=args, kwargs=kwargs), self.assertRaises(ValueError):
                    gate.candles(*args, **kwargs)
            get.assert_not_called()


class GateTransportTest(unittest.TestCase):
    @patch("market.time.sleep")
    @patch("market.build_opener")
    def test_retries_transient_error_with_https_public_get(self, opening, sleep):
        opener = opening.return_value
        opener.open.side_effect = [URLError("temporary"), io.BytesIO(b"[]")]
        self.assertEqual(GateClient().candles("牛来_USDT", "15m", limit=3), [])
        self.assertEqual(opener.open.call_count, 2)
        sleep.assert_called_once()
        final_call = opener.open.call_args
        request = final_call.args[0]
        url = urlsplit(request.full_url)
        self.assertEqual((url.scheme, url.path), ("https", "/api/v4/futures/usdt/candlesticks"))
        self.assertEqual(parse_qs(url.query)["contract"], ["牛来_USDT"])
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(dict(request.header_items()), {"Accept": "application/json", "X-gate-size-decimal": "1"})
        self.assertGreater(final_call.kwargs["timeout"], 0)

    @patch("market.time.sleep")
    @patch("market.build_opener")
    def test_checks_http_status_and_retries_only_once(self, opening, sleep):
        opener = opening.return_value
        opener.open.side_effect = [HTTPError("", 429, "rate limit", {}, None),
                                   HTTPError("", 503, "unavailable", {}, None)]
        with self.assertRaisesRegex(MarketError, "HTTP 503"):
            GateClient().candles("BTC_USDT", "15m")
        self.assertEqual(opener.open.call_count, 2)
        sleep.assert_called_once()

    @patch("market.build_opener")
    def test_rejects_bad_payload_without_retry(self, opening):
        opener = opening.return_value
        for value, message in ((b"not-json", "invalid JSON"), (b"{}", "did not return a JSON array")):
            with self.subTest(value=value):
                opener.open.reset_mock()
                opener.open.return_value = io.BytesIO(value)
                with self.assertRaisesRegex(MarketError, message):
                    GateClient().candles("BTC_USDT", "15m")
                opener.open.assert_called_once()


if __name__ == "__main__":
    unittest.main()
