"""USD equity price adapter with explicit exchange and timezone validation."""
from __future__ import annotations

import datetime as dt
import json
import math
import re
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from providers import DataError, checked_price_rows, iso_date


_EXCHANGES = {"NMS": "NasdaqGS", "NGM": "NasdaqGM", "NCM": "NasdaqCM", "NYQ": "NYSE", "ASE": "NYSE American"}
_NEW_YORK = ZoneInfo("America/New_York")


def us_identifiers(code):
    """Preserve an exchange-independent canonical share-class ticker."""
    if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9]{0,9}(?:[./-][A-Z0-9]{1,3})?", code):
        raise DataError("UNSUPPORTED_MARKET")
    canonical = code.replace("/", ".").replace("-", ".")
    return {"stockCode": canonical, "yahooSymbol": canonical.replace(".", "-"), "currency": "USD", "market": "US"}


def _check_range(start, end):
    if iso_date(start) != start or iso_date(end) != end or start > end:
        raise DataError("INVALID_PRICE_RANGE")


def parse_us_yahoo_prices(payload, code, start, end):
    _check_range(start, end)
    identity = us_identifiers(code)
    chart = payload.get("chart") if isinstance(payload, dict) else None
    results = chart.get("result") if isinstance(chart, dict) else None
    if chart is None or chart.get("error") or not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        raise DataError("INVALID_PRICE_FORMAT")
    result = results[0]
    meta = result.get("meta")
    if not isinstance(meta, dict) or meta.get("symbol") != identity["yahooSymbol"]:
        raise DataError("PRICE_IDENTITY_MISMATCH")
    if meta.get("currency") != "USD":
        raise DataError("PRICE_CURRENCY_MISMATCH")
    if meta.get("instrumentType") != "EQUITY":
        raise DataError("PRICE_INSTRUMENT_MISMATCH")
    if meta.get("exchangeName") not in _EXCHANGES or meta.get("fullExchangeName") != _EXCHANGES[meta["exchangeName"]]:
        raise DataError("PRICE_EXCHANGE_MISMATCH")
    if meta.get("exchangeTimezoneName") != "America/New_York":
        raise DataError("PRICE_TIMEZONE_MISMATCH")
    events = result.get("events", {})
    if not isinstance(events, dict):
        raise DataError("INVALID_PRICE_FORMAT")
    # Yahoo OHLC can be retroactively split-adjusted. The caller requests split
    # events through retrieval day, including splits AFTER the target quarter.
    if events.get("splits"):
        raise DataError("PRICE_CORPORATE_ACTION_UNRESOLVED")
    indicators = result.get("indicators")
    quotes = indicators.get("quote") if isinstance(indicators, dict) else None
    timestamps = result.get("timestamp")
    if not isinstance(quotes, list) or len(quotes) != 1 or not isinstance(quotes[0], dict) or not isinstance(timestamps, list):
        raise DataError("INVALID_PRICE_FORMAT")
    quote = quotes[0]
    if any(not isinstance(quote.get(field), list) or len(quote[field]) != len(timestamps) for field in ("low", "high", "close")):
        raise DataError("INVALID_PRICE_FORMAT")
    rows = []
    for index, timestamp in enumerate(timestamps):
        if isinstance(timestamp, bool) or not isinstance(timestamp, (float, int)) or not math.isfinite(timestamp):
            continue
        try:
            date = dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).astimezone(_NEW_YORK).date().isoformat()
        except (OverflowError, OSError, ValueError):
            continue
        rows.append({"date": date, **{field: quote[field][index] for field in ("low", "high", "close")}})
    return checked_price_rows(rows, start, end)


def us_price_bars(provider, code, start, end, retrieval_day=None):
    """Use the existing bounded provider.get transport; failure stays explicit."""
    _check_range(start, end)
    identity = us_identifiers(code)
    day = retrieval_day or dt.datetime.now(dt.timezone.utc).date()
    events_end = max(day, dt.date.fromisoformat(end)) + dt.timedelta(days=1)
    query = {"period1": int(dt.datetime.combine(dt.date.fromisoformat(start), dt.time.min, tzinfo=dt.timezone.utc).timestamp()),
             "period2": int(dt.datetime.combine(events_end, dt.time.min, tzinfo=dt.timezone.utc).timestamp()),
             "interval": "1d", "events": "splits", "includeAdjustedClose": "false"}
    failures = []
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        base = f"https://{host}/v8/finance/chart/{identity['yahooSymbol']}"
        try:
            payload = json.loads(provider.get(base, params=query, timeout_seconds=4, attempts_override=1))
            rows = parse_us_yahoo_prices(payload, identity["stockCode"], start, end)
            return {"stockCode": identity["stockCode"], "currency": "USD", "market": "US", "priceBasis": "unadjusted",
                    "provider": "yahoo", "sourceUrl": base + "?" + urlencode(query), "rows": rows, "providerErrors": failures}
        except DataError as error:
            failures.append({"provider": host, "errorCode": error.code})
        except (ValueError, TypeError, KeyError, IndexError, OverflowError, AttributeError):
            failures.append({"provider": host, "errorCode": "INVALID_PRICE_FORMAT"})
    error = DataError("PRICE_SOURCES_UNAVAILABLE")
    error.provider_errors = failures
    raise error
