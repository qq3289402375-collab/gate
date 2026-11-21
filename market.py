"""Read-only access to Gate USDT perpetual market data."""

import json
import logging
import math
import re
import time
from dataclasses import dataclass
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from signals import Bar


GATE_BASE_URL = "https://api.gateio.ws/api/v4/futures/usdt"
PINNED_CRYPTO = ("BTC_USDT", "ETH_USDT", "SOL_USDT", "ZEC_USDT", "HYPE_USDT")
PINNED_TRADFI = (
    "SNDK_USDT", "SPCX_USDT", "MU_USDT", "NVDA_USDT", "TSLA_USDT",
    "CL_USDT", "XAU_USDT", "XAG_USDT",
)
_TRADFI_TYPES = ("stocks", "metals", "indices", "forex", "commodities")
INTERVAL_SECONDS = {
    "10s": 10,
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "8h": 28800,
    "1d": 86400,
    "7d": 604800,
}
_SYMBOL = re.compile(r"\w[\w-]{0,63}_USDT")
_MAX_POINTS = 1000
_TIMEOUT = 8
_RETRYABLE_HTTP = {408, 429, 500, 502, 503, 504}
_LOG = logging.getLogger(__name__)


class MarketError(RuntimeError):
    """The public data endpoint failed or returned unusable data."""


@dataclass(frozen=True)
class Market:
    symbol: str
    category: str
    quote_volume: float
    change_pct: float
    funding_pct: float | None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _number(value: object, field: str) -> float:
    try:
        if isinstance(value, bool):
            raise ValueError
        parsed = float(value)
        if math.isfinite(parsed):
            return parsed
    except (ValueError, TypeError, OverflowError):
        pass
    raise MarketError(f"invalid numeric field: {field}")


def _index_rows(payload: list, field: str, endpoint: str) -> dict[str, dict | None]:
    indexed = {}
    for row in payload:
        symbol = row.get(field) if isinstance(row, dict) else None
        if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
            _LOG.warning("Gate %s omitted a row with an invalid USDT symbol", endpoint)
            continue
        if symbol in indexed:
            _LOG.warning("Gate %s omitted duplicate metadata for %s", endpoint, symbol)
            indexed[symbol] = None
        else:
            indexed[symbol] = row
    return indexed


def _make_market(symbol: str, category: str, ticker: dict, contract: dict) -> Market:
    volume = ticker.get("volume_24h_quote")
    if volume in (None, ""):
        volume = ticker.get("volume_24h_settle")
    volume = _number(volume, "volume_24h_quote")
    if volume < 0:
        raise MarketError("negative 24-hour quote volume")
    change = _number(ticker.get("change_percentage"), "change_percentage")
    funding = ticker.get("funding_rate")
    if funding in (None, ""):
        funding = contract.get("funding_rate")
    funding_pct = None if funding in (None, "") else _number(funding, "funding_rate") * 100
    if funding_pct is not None and not math.isfinite(funding_pct):
        raise MarketError("invalid funding percentage")
    return Market(symbol, category, volume, change, funding_pct)


class GateClient:
    def __init__(self, base_url: str = GATE_BASE_URL) -> None:
        parsed = urlsplit(base_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment):
            raise ValueError("Gate base_url must be an HTTPS URL without credentials or a query")
        self.base_url = base_url.rstrip("/")

    def _get(self, endpoint: str, params: dict | None = None) -> list:
        url = f"{self.base_url}/{endpoint}"
        if params:
            url = f"{url}?{urlencode(params)}"
        request = Request(url, headers={"Accept": "application/json", "X-Gate-Size-Decimal": "1"}, method="GET")
        opener = build_opener(_NoRedirect())
        for attempt in range(2):
            try:
                with opener.open(request, timeout=_TIMEOUT) as response:
                    payload = json.load(response)
            except HTTPError as error:
                error.close()
                if attempt or error.code not in _RETRYABLE_HTTP:
                    raise MarketError(f"Gate {endpoint} returned HTTP {error.code}") from error
            except (OSError, HTTPException) as error:
                if attempt:
                    raise MarketError(f"Gate {endpoint} network error: {error}") from error
            except ValueError as error:
                raise MarketError(f"Gate {endpoint} returned invalid JSON") from error
            else:
                if not isinstance(payload, list):
                    raise MarketError(f"Gate {endpoint} did not return a JSON array")
                return payload
            time.sleep(0.5)
        raise AssertionError("Gate request did not terminate")

    def markets(self, limit: int = 50) -> list[Market]:
        if type(limit) is not int or limit < 1:
            raise ValueError("market limit must be a positive integer")
        contracts = _index_rows(self._get("contracts"), "name", "contracts")
        tickers = _index_rows(self._get("tickers"), "contract", "tickers")
        eligible = {}
        for symbol, contract in contracts.items():
            if contract is None or contract.get("status") != "trading":
                continue
            if contract.get("in_delisting", False) is not False or contract.get("is_pre_market", False) is not False:
                continue
            contract_type = contract.get("contract_type")
            # Gate currently omits contract_type for plain crypto perpetuals.
            # Unrecognized explicit classifications are excluded from auto-fill.
            if symbol in PINNED_TRADFI and contract_type in _TRADFI_TYPES:
                category = "tradfi"
            elif symbol not in PINNED_TRADFI and contract_type in (None, "", "crypto"):
                category = "crypto"
            else:
                continue
            ticker = tickers.get(symbol)
            if ticker is None:
                _LOG.warning("Gate omitted %s: missing unique ticker data", symbol)
                continue
            try:
                market = _make_market(symbol, category, ticker, contract)
            except MarketError as error:
                _LOG.warning("Gate omitted %s: %s", symbol, error)
                continue
            if market.quote_volume > 0 or symbol in PINNED_CRYPTO or symbol in PINNED_TRADFI:
                eligible[symbol] = market

        chosen = []
        for symbol in (*PINNED_CRYPTO, *PINNED_TRADFI):
            market = eligible.pop(symbol, None)
            if market is None:
                _LOG.warning("Gate pinned market %s is unavailable or ineligible", symbol)
            else:
                chosen.append(market)
        chosen.extend(sorted(eligible.values(), key=lambda market: (-market.quote_volume, market.symbol)))
        if len(chosen) < limit:
            _LOG.warning("Gate market coverage is %d/%d eligible markets", len(chosen), limit)
        if not chosen:
            raise MarketError("Gate returned no eligible markets")
        return chosen[:limit]

    def candles(
        self,
        symbol: str,
        interval: str,
        *,
        limit: int | None = None,
        start: int | None = None,
        end: int | None = None,
    ) -> list[Bar]:
        if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
            raise ValueError("invalid Gate USDT contract symbol")
        if interval not in INTERVAL_SECONDS:
            raise ValueError(f"unsupported Gate candle interval: {interval}")

        params = {"contract": symbol, "interval": interval}
        if start is None and end is None:
            limit = 100 if limit is None else limit
            if type(limit) is not int or not 1 <= limit <= _MAX_POINTS:
                raise ValueError(f"candle limit must be between 1 and {_MAX_POINTS}")
            params["limit"] = limit
        else:
            if limit is not None or type(start) is not int or type(end) is not int or not 0 <= start <= end:
                raise ValueError("candle range requires nonnegative start and end without limit")
            if (end - start) // INTERVAL_SECONDS[interval] + 1 > _MAX_POINTS:
                raise ValueError(f"candle range exceeds {_MAX_POINTS} points")
            params["from"] = start
            params["to"] = end

        by_timestamp = {}
        rows = self._get("candlesticks", params)
        for row in rows:
            try:
                if not isinstance(row, dict):
                    raise MarketError("candle is not a JSON object")
                parsed_t = _number(row.get("t"), "t")
                if not parsed_t.is_integer() or not 0 <= parsed_t <= 2**53 - 1:
                    raise MarketError("invalid Unix timestamp")
                t = int(parsed_t)
                if start is not None and not start <= t <= end:
                    continue
                o, h, l, c, v = (_number(row.get(field), field) for field in ("o", "h", "l", "c", "v"))
                if l <= 0 or not l <= min(o, c) <= max(o, c) <= h or v < 0:
                    raise MarketError(f"invalid OHLC or volume at t={t}")
                bar = Bar(t=t, o=o, h=h, l=l, c=c, v=v)
                previous = by_timestamp.get(t)
                if previous is not None and previous != bar:
                    raise MarketError(f"conflicting candles at t={t}")
                by_timestamp[t] = bar
            except MarketError as error:
                raise MarketError(f"Gate {symbol} {interval}: {error}") from error
        candles = sorted(by_timestamp.values(), key=lambda bar: bar.t)
        return candles if limit is None else candles[-limit:]
