"""Synthetic parser fixtures; never market-data seeds."""
import json
import datetime as dt
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from providers import (DataError, PublicProvider, js_literal, parse_announcements, parse_holdings_html,
                       parse_pdf_pages, parse_no_equities, report_period, parse_eastmoney_prices,
                       parse_yahoo_prices, parse_tencent_prices, stock_identifiers)
from collect import collect_fund, collect


class ParserTests(unittest.TestCase):
    def test_holdings_uses_verified_limit_not_silently_downgraded_large_value(self):
        class ServerFixture(PublicProvider):
            def __init__(self):
                self.requested_limit = None
            def get(self, url, **kwargs):
                self.requested_limit = kwargs["params"]["topline"]
                count = 100 if self.requested_limit == "100" else 10
                rows = ''.join(f'<tr><td>{600000+i}</td><td>示例股票</td><td>0.01%</td><td>1</td><td>2</td></tr>' for i in range(count))
                return '<div class="box"><h4>2026年2季度股票投资明细</h4><table><thead><tr><th>股票代码</th><th>股票名称</th><th>占净值比例</th><th>持股数（万股）</th><th>持仓市值（万元）</th></tr></thead><tbody>' + rows + '</tbody></table></div>'
        provider = ServerFixture()
        holdings = provider.holdings("005827", 2026)
        self.assertEqual(provider.requested_limit, "100")
        self.assertEqual(len(holdings["2026-06-30"]), 100)

    def test_report_dates_require_real_publication_and_reject_future(self):
        data = {"Data": [
            {"TITLE": "示例基金2026年第2季度报告", "PUBLISHDATEDESC": "2026-07-21", "ART_CODE": "AN202607211234567890"},
            {"TITLE": "示例基金2026年第3季度报告", "PUBLISHDATEDESC": "2026-10-21", "ART_CODE": "AN202610211234567890"},
            {"TITLE": "示例基金2026年中期报告", "ART_CODE": "AN202608311234567890"},
        ]}
        result = parse_announcements(data, "2026-09-29")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["periodEnd"], "2026-06-30")
        self.assertEqual(report_period("示例基金2026年第 一 季度报告"), ("2026-03-31", "quarterly"))
        self.assertIsNone(report_period("示例基金2026年中期报告提示公告"))

    def test_holdings_share_and_money_units_and_hk_identity(self):
        html = '''<div class="box"><h4 class="t">示例基金 2026年2季度股票投资明细</h4>
        <p>截止至：2026-06-30</p><table><thead><tr><th>序号</th><th>股票代码</th><th>股票名称</th><th>相关资讯</th><th>占净值 比例</th><th>持股数 （万股）</th><th>持仓市值 （万元）</th></tr></thead>
        <tbody><tr><td>1</td><td>00700</td><td>示例港股</td><td>资讯</td><td>5.20%</td><td>12.50</td><td>5,000.25</td></tr></tbody></table></div>'''
        payload = "var apidata={content:" + json.dumps(html) + ",arryear:[2026]};"
        row = parse_holdings_html(payload)["2026-06-30"][0]
        self.assertEqual(row["shares"], 125000)
        self.assertEqual(row["marketValueYuan"], 50002500)
        self.assertEqual(row["weightPct"], 5.2)
        self.assertEqual(row["stockCode"], "00700")
        self.assertEqual(row["currency"], "HKD")

    def test_js_not_executed_and_truncation_is_error(self):
        self.assertEqual(js_literal('var apidata={content:"safe\\ntext"}; malicious()', "content"), "safe\ntext")
        with self.assertRaises(DataError):
            js_literal('var apidata={content:"missing end', "content")

    def test_multiple_share_classes_do_not_use_one_nav_cell(self):
        pages = ["基金主代码 000001\n下属分级基金的基金简称 示例A 示例C\n4.期末基金资产净值 10,000,000.00\n20,000,000.00"]
        result = parse_pdf_pages(pages, "https://example.org/report.pdf", "2026-06-30", "quarterly")
        self.assertIsNone(result["navYuan"])
        self.assertEqual(result["portfolioCode"], "000001")

    def test_interim_balance_uses_current_portfolio_nav_not_previous_year(self):
        pages = ["基金主代码 000001\n下属分级基金的基金简称 示例A 示例C",
                 "6.1 资产负债表\n本期末 2026 年 6 月 30 日 上年度末2025年12月31日\n净资产合计 30,000,000.00 50,000,000.00\n6.2 利润表"]
        result = parse_pdf_pages(pages, "https://example.org/report.pdf", "2026-06-30", "semiannual")
        self.assertEqual(result["navYuan"], 30000000)

    def test_nav_and_narrative_carry_period_page_and_bounded_text(self):
        pages = ["基金主代码 000001\n4.期末基金资产净值 10,000,000.00\n5.期末基金份额净值 1.20",
                 "4.4 报告期内基金投资策略和运作分析\n本基金本季度调整了行业配置，增加若干行业的配置，并根据公司经营情况、估值水平以及行业竞争格局进行了组合调整。\n4.5 报告期内基金的业绩表现\n业绩说明"]
        result = parse_pdf_pages(pages, "https://example.org/report.pdf", "2026-06-30", "quarterly")
        self.assertEqual(result["navYuan"], 10000000)
        self.assertEqual(result["narrative"]["page"], 2)
        self.assertEqual(result["narrative"]["periodStart"], "2026-04-01")
        self.assertNotIn("业绩说明", result["narrative"]["text"])

    def test_operation_excerpt_skips_macro_and_cites_actual_page(self):
        pages = ["4.4 报告期内基金投资策略和运作分析\n全球经济与证券市场受到各类事件影响。" + "指数变动与宏观背景说明。" * 30,
                 "本基金本季度增加了部分行业的配置，并依据企业经营与估值对组合进行了调整，降低了另一些行业的持仓比例。\n4.5 报告期内基金的业绩表现\n业绩说明"]
        result = parse_pdf_pages(pages, "https://example.org/report.pdf", "2026-06-30", "quarterly")
        excerpt = result["narrative"]
        self.assertEqual(excerpt["page"], 2)
        self.assertEqual(excerpt["excerptSelection"], "operation-sentences")
        self.assertIn("增加了", excerpt["text"])
        self.assertNotIn("全球经济", excerpt["text"])
        self.assertLessEqual(len(excerpt["text"]), 260)

    def test_report_identity_mismatch_is_excluded(self):
        data = {"Data": [{"FCODE": "000002", "TITLE": "示例基金2026年第2季度报告", "PUBLISHDATEDESC": "2026-07-21", "ART_CODE": "AN202607211234567890"}]}
        self.assertEqual(parse_announcements(data, "2026-09-29", "000001"), [])

    def test_no_equities_requires_whole_stock_statement(self):
        report = {"sourceUrl": "https://example.org/report.pdf", "periodEnd": "2026-06-30", "publishedAt": "2026-07-21", "kind": "quarterly", "title": "示例季度报告"}
        self.assertIsNone(parse_no_equities(["本基金本报告期末未持有流通受限股票。本基金本报告期末未投资股票期权。"], report))
        evidence = parse_no_equities(["目录", "本基金本报告期末未持有股票。"], report)
        self.assertTrue(evidence["confirmed"])
        self.assertEqual(evidence["page"], 2)
        self.assertEqual(evidence["kind"], "quarterly")

    def test_failed_source_preserves_missing_and_sanitized_errors(self):
        class FailedProvider:
            def overview(self, code):
                raise RuntimeError("secret-and-local-path")
            def announcements(self, code, as_of):
                raise DataError("TIMEOUT")
        fund, errors = collect_fund({"code": "000001"}, "2026-09-29", provider=FailedProvider())
        self.assertEqual(fund["reports"], [])
        self.assertEqual(errors[0]["errorCode"], "PARSE_FAILED")
        self.assertNotIn("secret", json.dumps(errors))

    def test_fixed_target_does_not_substitute_previous_quarter(self):
        class PreviousOnly:
            def overview(self, code):
                return {"name": "示例", "company": "机构", "managers": []}
            def announcements(self, code, as_of):
                return [{"title": "示例2026年第1季度报告", "periodEnd": "2026-03-31", "publishedAt": "2026-04-22", "kind": "quarterly", "sourceUrl": "https://example.org/report.pdf"}]
            def holdings(self, code, year):
                return {"2026-03-31": [{"stockCode": "600001"}]}
        fund, errors = collect_fund({"code": "000001"}, "2026-09-29", provider=PreviousOnly(),
                                    target_period="2026-06-30", baseline_period="2026-03-31")
        self.assertEqual(fund["reports"], [])
        self.assertIn("TARGET_REPORT_UNAVAILABLE", [error["errorCode"] for error in errors])


def yahoo_fixture(symbol="600519.SS", currency="CNY", exchange="SHH"):
    dates = ["2026-03-31", "2026-04-01", "2026-06-30", "2026-07-01"]
    timestamps = [int(dt.datetime.fromisoformat(date).replace(tzinfo=dt.timezone.utc).timestamp()) for date in dates]
    return {"chart": {"result": [{"meta": {"symbol": symbol, "currency": currency, "exchangeName": exchange},
                                  "timestamp": timestamps, "indicators": {"quote": [{"low": [9]*4, "high": [12]*4, "close": [10]*4}],
                                                                          "adjclose": [{"adjclose": [1]*4}]}}]}}


class PriceTests(unittest.TestCase):
    def test_yahoo_raw_quote_ignores_adjusted_and_out_of_window_rows(self):
        rows = parse_yahoo_prices(yahoo_fixture(), "600519.SS", "CNY", "SH", "2026-04-01", "2026-06-30")
        self.assertEqual([row["date"] for row in rows], ["2026-04-01", "2026-06-30"])
        self.assertEqual(rows[0]["close"], 10)

    def test_yahoo_rejects_wrong_identity_currency_exchange_and_splits(self):
        for fixture in [yahoo_fixture(symbol="000001.SZ"), yahoo_fixture(currency="USD"), yahoo_fixture(exchange="NMS")]:
            with self.assertRaises(DataError):
                parse_yahoo_prices(fixture, "600519.SS", "CNY", "SH", "2026-04-01", "2026-06-30")
        fixture = yahoo_fixture()
        fixture["chart"]["result"][0]["events"] = {"splits": {"1": {"numerator": 2, "denominator": 1}}}
        with self.assertRaises(DataError) as error:
            parse_yahoo_prices(fixture, "600519.SS", "CNY", "SH", "2026-04-01", "2026-06-30")
        self.assertEqual(error.exception.code, "PRICE_CORPORATE_ACTION_UNRESOLVED")

    def test_eastmoney_rejects_cross_security_payload(self):
        with self.assertRaises(DataError):
            parse_eastmoney_prices({"data": {"code": "600000", "klines": ["2026-04-01,10,10,12,9"]}}, "600519", "2026-04-01", "2026-06-30")

    def test_tencent_only_raw_day_and_verified_security(self):
        raw = {"data": {"hk00700": {"qt": {"hk00700": ["100", "示例", "00700"]}, "day": [["2026-04-01", "10", "11", "12", "9"]], "qfqday": [["2026-04-01", "1", "1", "2", "0.5"]]}}}
        rows = parse_tencent_prices(raw, "hk00700", "00700", "2026-04-01", "2026-06-30")
        self.assertEqual(rows[0]["close"], 11)
        del raw["data"]["hk00700"]["day"]
        with self.assertRaises(DataError) as error:
            parse_tencent_prices(raw, "hk00700", "00700", "2026-04-01", "2026-06-30")
        self.assertEqual(error.exception.code, "ADJUSTED_PRICE_REJECTED")
        self.assertEqual(stock_identifiers("00700", "HK")[2], "0700.HK")

    def test_fallback_and_per_source_single_attempt(self):
        class FixtureProvider(PublicProvider):
            def __init__(self):
                self.calls = []
            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                if "eastmoney" in url:
                    raise DataError("TIMEOUT")
                return json.dumps(yahoo_fixture())
        provider = FixtureProvider()
        result = provider.price_bars("600519", "CN", "2026-04-01", "2026-06-30")
        self.assertEqual(result["provider"], "yahoo")
        self.assertEqual(result["priceBasis"], "unadjusted")
        self.assertEqual(result["currency"], "CNY")
        self.assertEqual(len(provider.calls), 2)
        self.assertTrue(all(call[1]["attempts_override"] == 1 for call in provider.calls))
        self.assertTrue(all(call[1]["timeout_seconds"] <= 4 for call in provider.calls))
        self.assertEqual(result["providerErrors"], [{"provider": "eastmoney", "errorCode": "TIMEOUT"}])


class CheckpointTests(unittest.TestCase):
    @staticmethod
    def fake_fund(item, *args, **kwargs):
        base = int(item["code"][-1])
        holdings = [{"market": "CN", "stockCode": f"600{base}{rank:02d}"} for rank in range(3)]
        return {"code": item["code"], "reports": [{"periodEnd": "2026-06-30", "holdings": holdings, "narrative": None},
                                                  {"periodEnd": "2026-03-31", "holdings": [], "narrative": None}]}, []

    def test_checkpoint_before_prices_and_round_robin_top_holdings(self):
        calls, saved = [], []
        class Prices:
            def price_bars(self, code, market, start, end):
                calls.append(code)
                return {"rows": [{"date": start, "low": 1, "high": 2, "close": 1.5}]}
        config = {"funds": [{"code": "000001"}, {"code": "000002"}], "maxPriceSymbols": 2}
        with patch("collect.collect_fund", self.fake_fund), patch("collect.PublicProvider", Prices):
            result = collect(config, "2026-09-29", checkpoint=lambda p: saved.append(json.loads(json.dumps(p))))
        self.assertEqual(set(calls), {"600100", "600200"})
        self.assertTrue(any(p["coverage"]["collectionStage"] == "prices" and len(p["funds"]) == 2 and not p["barsByStock"] for p in saved))
        self.assertTrue(any(len(p["funds"]) == 1 for p in saved))
        self.assertEqual(result["coverage"]["priceSymbolsFetched"], 2)

    def test_enrichment_requests_only_missing_disclosed_targets(self):
        calls = []
        class Prices:
            def price_bars(self, code, market, start, end):
                calls.append(code)
                return {"rows": []}
        config = {"funds": [{"code": "000001", "priceTargets": [{"stockCode": "600102", "market": "CN"}]}]}
        with patch("collect.collect_fund", self.fake_fund), patch("collect.PublicProvider", Prices):
            collect(config, "2026-09-29")
        self.assertEqual(calls, ["600102"])

    def test_expired_fund_budget_does_not_start_requests(self):
        class NeverRequest:
            def overview(self, code):
                raise AssertionError("must not request")
        fund, errors = collect_fund({"code": "000001"}, "2026-09-29", provider=NeverRequest(), deadline=time.monotonic() - 1)
        self.assertEqual(fund["reports"], [])
        self.assertEqual(errors[0]["errorCode"], "BATCH_DEADLINE_REACHED")

    def test_q2_prices_do_not_require_q1_holdings(self):
        calls = []
        class Prices:
            def price_bars(self, code, market, start, end):
                calls.append((code, start, end))
                return {"rows": [{"date": start, "low": 1, "high": 2, "close": 1.5}]}
        def current_only(item, *args, **kwargs):
            return {"code": item["code"], "reports": [{"periodEnd": "2026-06-30", "narrative": None,
                                                       "holdings": [{"market": "CN", "stockCode": "600519"}]}]}, []
        config = {"funds": [{"code": "000001", "priceTargets": [{"stockCode": "600519", "market": "CN"}]}],
                  "targetPeriodEnd": "2026-06-30", "baselinePeriodEnd": "2026-03-31"}
        with patch("collect.collect_fund", current_only), patch("collect.PublicProvider", Prices):
            result = collect(config, "2026-09-29")
        self.assertEqual(calls, [("600519", "2026-04-01", "2026-06-30")])
        self.assertEqual(result["coverage"]["comparable"], 0)
        self.assertEqual(result["coverage"]["priceSymbolsFetched"], 1)
        self.assertEqual(len(result["funds"][0]["reports"]), 1)

    def test_requested_price_count_can_exceed_old_300_cap(self):
        class Prices:
            def price_bars(self, code, market, start, end):
                return {"rows": [{"date": start, "low": 1, "high": 2, "close": 1.5}]}
        def portfolio(item, *args, **kwargs):
            return {"code": item["code"], "reports": [{"periodEnd": "2026-06-30", "narrative": None,
                                                       "holdings": [{"market": "CN", "stockCode": str(600000 + n)} for n in range(350)]}]}, []
        config = {"funds": [{"code": "000001"}], "targetPeriodEnd": "2026-06-30", "maxPriceSymbols": 350}
        with patch("collect.collect_fund", portfolio), patch("collect.PublicProvider", Prices):
            result = collect(config, "2026-09-29")
        self.assertEqual(result["coverage"]["priceSymbolsFetched"], 350)


if __name__ == "__main__":
    unittest.main()
