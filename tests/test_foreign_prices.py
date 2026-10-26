import datetime as dt
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from foreign_prices import parse_us_yahoo_prices, us_identifiers, us_price_bars
from providers import DataError


def payload(symbol="AAPL"):
    timestamps = [int(dt.datetime(2026, 4, 1, 13, 30, tzinfo=dt.timezone.utc).timestamp()),
                  int(dt.datetime(2026, 4, 2, 1, 0, tzinfo=dt.timezone.utc).timestamp()),
                  int(dt.datetime(2026, 4, 2, 13, 30, tzinfo=dt.timezone.utc).timestamp())]
    return {"chart": {"error": None, "result": [{
        "meta": {"symbol": symbol, "currency": "USD", "instrumentType": "EQUITY", "exchangeName": "NMS",
                 "fullExchangeName": "NasdaqGS", "exchangeTimezoneName": "America/New_York"},
        "timestamp": timestamps, "events": {},
        "indicators": {"quote": [{"low": [10, 11, 12], "high": [13, 14, 15], "close": [12, 13, 14]}],
                       "adjclose": [{"adjclose": [1, 1, 1]}]},
    }]}}


class ForeignPricesTests(unittest.TestCase):
    def test_us_identifiers_share_class_and_injection_boundary(self):
        for code in ("BRK/B", "BRK-B", "BRK.B"):
            self.assertEqual(us_identifiers(code)["stockCode"], "BRK.B")
            self.assertEqual(us_identifiers(code)["yahooSymbol"], "BRK-B")
        for code in ("600519", "AAPL.HK?x=1", "AAPL/../../", "aapl", None, "", "00700.HK"):
            with self.subTest(code=code), self.assertRaises(DataError):
                us_identifiers(code)

    def test_new_york_session_date_and_raw_ohlc(self):
        rows = parse_us_yahoo_prices(payload(), "AAPL", "2026-04-01", "2026-04-02")
        self.assertEqual(rows, [{"date": "2026-04-01", "low": 11.0, "high": 14.0, "close": 13.0},
                                {"date": "2026-04-02", "low": 12.0, "high": 15.0, "close": 14.0}])

    def test_metadata_and_later_split_never_silently_accepted(self):
        changes = {"symbol": "OTHER", "currency": "HKD", "instrumentType": "ETF", "exchangeName": "HKG",
                   "fullExchangeName": "Hong Kong", "exchangeTimezoneName": "Asia/Shanghai"}
        for key, value in changes.items():
            with self.subTest(key=key):
                bad = payload()
                bad["chart"]["result"][0]["meta"][key] = value
                with self.assertRaises(DataError):
                    parse_us_yahoo_prices(bad, "AAPL", "2026-04-01", "2026-06-30")
        bad = payload()
        bad["chart"]["result"][0]["events"] = {"splits": {"2026-08-01": {"numerator": 2, "denominator": 1}}}
        with self.assertRaises(DataError) as context:
            parse_us_yahoo_prices(bad, "AAPL", "2026-04-01", "2026-06-30")
        self.assertEqual(context.exception.code, "PRICE_CORPORATE_ACTION_UNRESOLVED")

    def test_shape_errors_and_invalid_ranges(self):
        for bad in ({}, {"chart": {"result": []}}, {"chart": {"result": [None]}}):
            with self.subTest(bad=bad), self.assertRaises(DataError):
                parse_us_yahoo_prices(bad, "AAPL", "2026-04-01", "2026-06-30")
        bad = payload()
        bad["chart"]["result"][0]["indicators"]["quote"][0]["low"] = [1]
        with self.assertRaises(DataError):
            parse_us_yahoo_prices(bad, "AAPL", "2026-04-01", "2026-06-30")
        with self.assertRaises(DataError):
            parse_us_yahoo_prices(payload(), "AAPL", "2026-04-31", "2026-06-30")

    def test_transport_is_bounded_and_requests_splits_through_retrieval(self):
        class Provider:
            def __init__(self):
                self.calls = []
            def get(self, url, **options):
                self.calls.append((url, options))
                if len(self.calls) == 1:
                    raise DataError("TIMEOUT")
                return json.dumps(payload("BRK-B"))
        source = Provider()
        result = us_price_bars(source, "BRK/B", "2026-04-01", "2026-06-30", retrieval_day=dt.date(2026, 10, 26))
        self.assertEqual(result["stockCode"], "BRK.B")
        self.assertEqual(result["currency"], "USD")
        self.assertEqual(result["priceBasis"], "unadjusted")
        self.assertEqual(result["providerErrors"][0]["errorCode"], "TIMEOUT")
        self.assertEqual(len(source.calls), 2)
        for url, options in source.calls:
            self.assertTrue(url.endswith("/BRK-B"))
            self.assertEqual(options["timeout_seconds"], 4)
            self.assertEqual(options["attempts_override"], 1)
            query = options["params"]
            self.assertEqual(query["events"], "splits")
            self.assertEqual(query["includeAdjustedClose"], "false")
            self.assertEqual(dt.datetime.fromtimestamp(query["period2"], dt.timezone.utc).date(), dt.date(2026, 10, 27))

    def test_unavailable_retains_provider_errors(self):
        class Provider:
            def get(self, *_args, **_options):
                raise DataError("TIMEOUT")
        with self.assertRaises(DataError) as context:
            us_price_bars(Provider(), "AAPL", "2026-04-01", "2026-06-30")
        self.assertEqual(context.exception.code, "PRICE_SOURCES_UNAVAILABLE")
        self.assertEqual(len(context.exception.provider_errors), 2)


if __name__ == "__main__":
    unittest.main()
