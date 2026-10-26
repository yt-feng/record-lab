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

    def test_wrapped_pdf_identity_labels_and_values_on_later_intro_page(self):
        pages = ["合成封面与目录"] * 5 + [
            "2.1 基金基本情况\n基金主代\n码 000001\n下属分级基金的基\n金简称 合成A 合成C 合成D\n"
            "下属分级基金的交\n易代码 000001 000002\n000003\n报告期末下属分级基金的份额总额\n"
            "600001 600002 600003\n4.期末基金资产净值 10,000,000.00 20,000,000.00 30,000,000.00\n"
            "5.期末基金份额净值 1.1234 1.2345 1.3456"]
        result = parse_pdf_pages(pages, "https://example.org/report.pdf", "2026-06-30", "quarterly")
        self.assertEqual(result["portfolioCode"], "000001")
        self.assertEqual(result["shareClassCodes"], ["000001", "000002", "000003"])
        self.assertEqual(result["navYuan"], 60000000)

    def test_pdf_aliases_cannot_skip_labels_or_accept_monetary_values(self):
        for values in ("000001 123456.00", "基金份额说明\n000001 000002", "000002 000003"):
            with self.subTest(values=values):
                page = "基金主代码 000001\n下属分级基金的交\n易代码 " + values
                result = parse_pdf_pages([page], "https://example.org/report.pdf", "2026-06-30", "quarterly")
                self.assertEqual(result["shareClassCodes"], [])
        page = "基金主代码 000001\n下属分级基金的基金简称 合成A 合成C\n交易明细 000001 000002"
        result = parse_pdf_pages([page], "https://example.org/report.pdf", "2026-06-30", "quarterly")
        self.assertEqual(result["shareClassCodes"], [])

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

    def test_wrapped_nav_labels_keep_exact_share_columns_and_stop_at_unit_nav(self):
        identity = "基金主代码 000001\n下属分级基金的交易代码 000001 000002 000003\n"
        metric = "4.期末基金资产净\n值 10,000,000.00 200,000.00 300.00\n5.期末基金份额净\n值 1.1234 1.2345 1.3456"
        result = parse_pdf_pages([identity + metric], "https://example.org/report.pdf", "2026-06-30", "quarterly")
        self.assertEqual(result["navYuan"], 10200300)
        self.assertIsNone(parse_pdf_pages([identity + metric.replace(" 300.00", "")],
            "https://example.org/report.pdf", "2026-06-30", "quarterly")["navYuan"])
        single = "基金主代码 000001\n4.期末基金资产净\n值 10,000,000.00\n5.期末基金份额净\n值 1.1234"
        self.assertEqual(parse_pdf_pages([single], "https://example.org/report.pdf", "2026-06-30", "quarterly")["navYuan"], 10000000)

    def test_short_operation_sentence_is_not_discarded_as_missing_strategy(self):
        sentence = "本基金本季度适度提高长债配置比例，并保留足够流动性。"
        pages = ["4.4 报告期内基金的投资策略和运作分析\n" + sentence + "\n4.5 报告期内基金的业绩表现\n不应进入摘录"]
        narrative = parse_pdf_pages(pages, "https://example.org/report.pdf", "2026-06-30", "quarterly")["narrative"]
        self.assertEqual(narrative["text"], sentence)
        self.assertEqual((narrative["page"], narrative["periodStart"]), (1, "2026-04-01"))
        self.assertIsNone(parse_pdf_pages(["4.4 报告期内基金的投资策略和运作分析\n无。\n4.5 报告期内基金的业绩表现"],
            "https://example.org/report.pdf", "2026-06-30", "quarterly")["narrative"])

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
            announcement_error_code = "ANNOUNCEMENT_PAGE_LIMIT"
            def overview(self, code):
                return {"name": "示例", "company": "机构", "managers": []}
            def announcements(self, code, as_of):
                return [{"title": "示例2026年第1季度报告", "periodEnd": "2026-03-31", "publishedAt": "2026-04-22", "kind": "quarterly", "sourceUrl": "https://example.org/report.pdf"}]
            def holdings(self, code, year):
                raise AssertionError("Do not fetch historical tables when the target report is absent")
        fund, errors = collect_fund({"code": "000001"}, "2026-09-29", provider=PreviousOnly(),
                                    target_period="2026-06-30", baseline_period="2026-03-31")
        self.assertEqual(fund["reports"], [])
        self.assertIn("TARGET_REPORT_UNAVAILABLE", [error["errorCode"] for error in errors])
        self.assertIn("ANNOUNCEMENT_PAGE_LIMIT", [error["errorCode"] for error in errors])
        self.assertNotIn("holdings", [error["stage"] for error in errors])
        self.assertTrue(fund["collectionAttempt"]["attempted"])


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
        self.assertEqual(len(provider.calls), 3)
        self.assertTrue(all(call[1]["attempts_override"] == 1 for call in provider.calls))
        self.assertTrue(all(call[1]["timeout_seconds"] <= 4 for call in provider.calls))
        self.assertEqual(result["coverage"]["completeness"], "partial")
        self.assertEqual(result["providerErrors"], [{"provider": "eastmoney", "errorCode": "TIMEOUT"},
                                                  {"provider": "tencent", "errorCode": "PRICE_IDENTITY_MISMATCH"}])


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
        self.assertEqual(fund["collectionAttempt"], {"attempted": False, "status": "deferred-budget", "errorCode": "BATCH_DEADLINE_REACHED"})

    def test_queued_budget_expiry_keeps_attempted_and_unstarted_products_separate(self):
        clock, calls = [0], []
        class Source:
            def overview(self, code):
                calls.append(code)
                clock[0] = 2
                raise DataError("TIMEOUT")
        config = {"funds": [{"code": "000001"}, {"code": "000002"}],
                  "maxWorkers": 1, "maxBatchSeconds": 1, "maxPriceSymbols": 0}
        with patch("collect.time.monotonic", side_effect=lambda: clock[0]), patch("collect.PublicProvider", Source):
            result = collect(config, "2026-09-29")
        self.assertEqual(calls, ["000001"])
        self.assertTrue(result["funds"][0]["collectionAttempt"]["attempted"])
        self.assertFalse(result["funds"][1]["collectionAttempt"]["attempted"])
        self.assertEqual(result["coverage"]["attemptedFundRequests"], 1)
        self.assertEqual(result["coverage"]["deferredFundRequests"], 1)
        self.assertIn({"code": "000001", "stage": "metadata", "errorCode": "TIMEOUT"}, result["errors"])

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

class ReportRecoveryTests(unittest.TestCase):
    def test_wrapped_share_class_identity_recovers_only_listed_alias(self):
        report = {"title": "合成2026年第3季度报告", "periodEnd": "2026-09-30", "kind": "quarterly",
                  "publishedAt": "2026-10-20", "sourceUrl": "https://example.org/report.pdf"}
        class Source:
            def overview(self, code): return {"name": "合成份额", "company": "合成机构", "managers": []}
            def announcements(self, code, as_of): return [report]
            def holdings(self, code, year):
                return {"2026-09-30": [{"stockCode": "600001", "stockName": "合成证券", "market": "CN",
                                         "currency": "CNY", "shares": 100, "weightPct": 1, "marketValueYuan": 10000}]}
            def report_details(self, document):
                return parse_pdf_pages(["基金主代码 000001\n下属分级基金的交\n易代码 000001 000002"],
                                       document["sourceUrl"], document["periodEnd"], document["kind"])
        fund, errors = collect_fund({"code": "000002"}, "2026-10-26", provider=Source(), target_period="2026-09-30")
        self.assertEqual(fund["shareClassCodes"], ["000001", "000002"])
        self.assertEqual(len(fund["reports"]), 1)
        self.assertNotIn("REPORT_IDENTITY_MISMATCH", [e["errorCode"] for e in errors])
        unrelated, errors = collect_fund({"code": "000003"}, "2026-10-26", provider=Source(), target_period="2026-09-30")
        self.assertEqual(unrelated["reports"], [])
        self.assertIn("REPORT_IDENTITY_MISMATCH", [e["errorCode"] for e in errors])

    def test_narrative_stops_at_major_section_when_performance_heading_is_absent(self):
        pages = ["4.4 报告期内基金投资策略和运作分析\n本基金本季度依据基本面与估值水平调整投资组合，同时关注资产流动性及行业配置变化。\n§5 投资组合报告",
                 "5.1 报告期末基金资产组合情况\n本基金组合调整增加降低增持减持配置表格。"]
        result = parse_pdf_pages(pages, "https://example.org/report.pdf", "2026-06-30", "quarterly")
        self.assertEqual(result["narrative"]["page"], 1)
        self.assertNotIn("表格", result["narrative"]["text"])

    def test_empty_all_stock_detail_requires_same_page_zero_equity_and_stock_rows(self):
        from providers import parse_no_equities
        report = {"sourceUrl": "https://example.org/report.pdf", "periodEnd": "2026-06-30",
                  "publishedAt": "2026-08-31", "kind": "semiannual", "title": "合成中期报告"}
        page = ("7.1 期末基金资产组合情况\n金额单位：人民币元\n序号 项目 金额 占基金总资产的比例\n（%）\n"
                "1 权益投资 - -\n其中：股票 - -\n2 固定收益投资 100.00 100.00\n"
                "7.2 报告期末按行业分类的股票投资组合\n无。\n"
                "7.3 期末按公允价值占基金资产净值比例大小排序的所有股票投资明细\n无。\n"
                "7.4 报告期内股票投资组合的重大变动\n无。")
        evidence = parse_no_equities(["合成封面", page], report)
        self.assertEqual(evidence["page"], 2)
        self.assertEqual(evidence["evidenceType"], "empty-stock-detail-section")
        self.assertEqual(evidence["assetAllocationText"], "1 权益投资 - -\n其中：股票 - -")
        self.assertEqual(evidence["text"], "7.3 期末按公允价值占基金资产净值比例大小排序的所有股票投资明细\n无。")
        self.assertEqual(evidence["kind"], "semiannual")
        self.assertNotIn("未持有", evidence["text"])
        self.assertTrue(parse_no_equities([page.replace("- -", "0.00 0.00")], report)["confirmed"])
        self.assertIsNone(parse_no_equities([page.replace("其中：股票 - -", "其中：股票 1.00 0.01")], report))
        self.assertIsNone(parse_no_equities([page.replace("1 权益投资 - -", "1 权益投资 1.00 0.01")], report))
        self.assertIsNone(parse_no_equities([page.replace("其中：股票 - -\n", "")], report))
        self.assertIsNone(parse_no_equities([page.replace("所有股票投资明细", "前十名股票投资明细")], report))
        self.assertIsNone(parse_no_equities([page.replace("所有股票投资明细", "受限股票投资明细")], report))
        self.assertIsNone(parse_no_equities(page.split("7.3 "), report))
        self.assertIsNone(parse_no_equities([page.replace("无。\n7.4", "无。只是合成样例\n7.4")], report))

    def test_share_class_nav_sum_requires_known_exact_columns(self):
        pages = ["基金主代码 000001\n下属分级基金的基金简称 示例A 示例C\n下属分级基金的交易代码 000001 000002\n4.期末基金资产净值 10,000,000.00 20,000,000.00\n5.期末基金份额净值 1.1234 1.3456"]
        result = parse_pdf_pages(pages, "https://example.org/report.pdf", "2026-09-30", "quarterly")
        self.assertEqual(result["navYuan"], 30000000)
        wrong_columns = pages[0].replace(" 20,000,000.00", "")
        self.assertIsNone(parse_pdf_pages([wrong_columns], "https://example.org/report.pdf", "2026-09-30", "quarterly")["navYuan"])

    def test_pdf_table_fallback_is_section_bounded_and_keeps_units(self):
        from providers import parse_pdf_holdings
        text = "5.3 期末按公允价值占基金资产净值比例大小排序的前十名股票投资明细\n序号 股票代码 股票名称 数量（股） 公允价值（元） 占基金资产净值比例（%）\n1 600001 合成证券 1,000 20,000.00 2.50\n2 00700 合成港股 200 50,000.00 6.25\n5.4 期末按债券品种分类的债券投资组合\n1 110001 债券 10 100.00 1.00"
        rows = parse_pdf_holdings([text])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["marketValueYuan"], 20000)
        self.assertEqual(rows[1]["currency"], "HKD")
        self.assertEqual(parse_pdf_holdings([text.replace("股票投资明细", "债券投资明细")]), [])

    def test_pdf_table_completeness_requires_terminal_heading_units_and_all_ranks(self):
        from providers import parse_pdf_holdings
        heading = "7.3 期末按公允价值占基金资产净值比例大小排序的所有股票投资明细\n序号 股票代码 股票名称 数量（股） 公允价值（元） 占基金资产净值比例（%）\n"
        body = "1 600001 合成甲 10 100.00 1.00\n2 600002 合成乙 20 200.00 2.00\n"
        ending = "7.4 报告期内股票投资组合的重大变动\n1 600003 另一张表 30 300.00 3.00"
        rows = parse_pdf_holdings([heading + body, ending])
        self.assertTrue(rows.metadata["complete"])
        self.assertEqual((rows.metadata["coverage"], rows.metadata["rowsSeen"], rows.metadata["parsedRows"]), ("full", 2, 2))
        self.assertEqual((rows.metadata["startPage"], rows.metadata["endPage"]), (1, 2))
        for text in [heading + body, heading + body.replace("2 600002", "3 600002") + ending,
                     heading + body + "3 AAPL 海外证券 10 200.00 2.00\n" + ending,
                     heading + body + "3\nAAPL 海外证券 10 200.00 2.00\n" + ending,
                     heading.replace("数量（股）", "数量") + body + ending]:
            self.assertFalse(parse_pdf_holdings([text]).metadata["complete"])
        overseas = parse_pdf_holdings([heading + body + "3 AAPL 海外证券 10 200.00 2.00\n" + ending])
        self.assertEqual(overseas.excluded_rows, 1)
        self.assertEqual(overseas.metadata["rowsSeen"], 3)
        eleven = ''.join(f"{i} {600000+i} 合成证券 10 100.00 0.01\n" for i in range(1, 12))
        self.assertFalse(parse_pdf_holdings([heading.replace("所有", "前十名") + eleven + ending]).metadata["complete"])

    def test_proven_pdf_can_replace_api_limit_without_losing_its_own_document_evidence(self):
        from providers import HoldingRows, parse_pdf_holdings
        from collect import pdf_holdings_evidence
        page = ("7.3 期末按公允价值占基金资产净值比例大小排序的所有股票投资明细\n"
                "序号 股票代码 股票名称 数量（股） 公允价值（元） 占基金资产净值比例（%）\n"
                + ''.join(f"{i} {600000+i} 合成证券 10 100.00 0.01\n" for i in range(1, 101))
                + "7.4 报告期内股票投资组合的重大变动")
        pdf_rows = parse_pdf_holdings([page])
        self.assertTrue(pdf_rows.metadata["complete"])
        details = {"pdfHoldings": pdf_rows, "pdfHoldingsMetadata": pdf_rows.metadata,
                   "parseMethod": "pypdf", "sourceTextHash": "a" * 64, "navYuan": 100000,
                   "pageStats": {"pageCount": 1, "totalPages": 1, "processedPages": 1,
                                 "truncated": False, "pageNumbersVerified": True}}
        class Source:
            def overview(self, code): return {"name": "合成基金", "company": "合成公司", "managers": []}
            def announcements(self, code, as_of):
                return [{"title": "合成中报", "periodEnd": "2026-06-30", "kind": "semiannual", "publishedAt": "2026-08-31", "sourceUrl": "https://example.org/interim.pdf"},
                        {"title": "合成季报", "periodEnd": "2026-06-30", "kind": "quarterly", "publishedAt": "2026-07-20", "sourceUrl": "https://example.org/quarter.pdf"}]
            def holdings(self, code, year): return {"2026-06-30": HoldingRows(pdf_rows)}
            def report_details(self, document):
                if document["kind"] == "semiannual": return details
                return {"parseMethod": "pypdf", "sourceTextHash": "b" * 64,
                        "pageStats": {"pageCount": 8, "totalPages": 8, "processedPages": 8, "truncated": False},
                        "narrative": {"text": "本基金本季度调整组合，适度提高部分行业配置。", "sourceUrl": document["sourceUrl"],
                                      "page": 4, "periodStart": "2026-04-01", "periodEnd": "2026-06-30"}}
        item, _ = collect_fund({"code": "000001"}, "2026-10-26", provider=Source(), target_period="2026-06-30")
        report = item["reports"][0]
        self.assertEqual((report["coverage"], report["holdingsPossiblyTruncated"], len(report["holdings"])), ("full", False, 100))
        self.assertEqual(report["holdingsPdfEvidence"]["sourceUrl"], "https://example.org/interim.pdf")
        self.assertEqual(report["holdingsPdfEvidence"]["sourceTextHash"], "a" * 64)
        self.assertEqual(report["reportPdfSourceUrl"], "https://example.org/quarter.pdf")
        self.assertEqual(report["sourceTextHash"], "b" * 64)
        document = Source().announcements("000001", "2026-10-26")[0]
        for stats in [{**details["pageStats"], "truncated": True}, {**details["pageStats"], "totalPages": 2},
                      {**details["pageStats"], "pageNumbersVerified": False}, {**details["pageStats"], "processedPages": True}]:
            self.assertFalse(pdf_holdings_evidence({**details, "pageStats": stats}, document, pdf_rows)["complete"])

    def test_pdf_recovery_preserves_no_equity_strategy_and_report_only_evidence(self):
        report = {"title": "合成基金2026年第3季度报告", "periodEnd": "2026-09-30", "kind": "quarterly", "publishedAt": "2026-10-20", "sourceUrl": "https://example.org/report.pdf"}
        class Source:
            mode = "no-equities"
            def overview(self, code): return {"name": "合成", "company": "机构", "managers": []}
            def announcements(self, code, as_of): return [report]
            def holdings(self, code, year): raise DataError("NO_HOLDINGS")
            def report_details(self, document, **kwargs):
                result = {"navYuan": 1000000, "narrative": {"text": "本基金本季度积极调整债券组合。", "page": 4,
                          "sourceUrl": document["sourceUrl"], "periodStart": "2026-07-01", "periodEnd": "2026-09-30"},
                          "parseMethod": "pypdf", "sourceTextHash": "a" * 64,
                          "pageStats": {"charCount": 2000, "totalPages": 10, "processedPages": 10, "truncated": False, "textStatus": "usable"}}
                if self.mode == "no-equities":
                    result["noEquitiesEvidence"] = {**document, "confirmed": True, "page": 8, "text": "本基金本报告期末未持有股票。"}
                if self.mode == "table":
                    result["pdfHoldings"] = [{"stockCode": "600001", "stockName": "合成证券", "market": "CN", "currency": "CNY", "shares": 100, "weightPct": 1, "marketValueYuan": 10000}]
                return result
        source = Source()
        item, errors = collect_fund({"code": "000001"}, "2026-10-26", provider=source, target_period="2026-09-30", baseline_period="2026-06-30")
        self.assertTrue(item["reports"][0]["noEquitiesConfirmed"])
        self.assertEqual(item["reports"][0]["narrative"]["periodStart"], "2026-07-01")
        self.assertEqual(item["reports"][0]["parseMethod"], "pypdf")
        source.mode = "report-only"
        item, errors = collect_fund({"code": "000001"}, "2026-10-26", provider=source, target_period="2026-09-30")
        self.assertTrue(item["reports"][0]["reportOnly"])
        self.assertNotIn("noEquitiesEvidence", item)
        self.assertIn("HOLDINGS_UNRESOLVED", [error["errorCode"] for error in errors])
        source.mode = "table"
        item, errors = collect_fund({"code": "000001"}, "2026-10-26", provider=source, target_period="2026-09-30")
        self.assertEqual(item["reports"][0]["holdingsRetrieval"], "pdf-stock-table-text")
        self.assertEqual(item["reports"][0]["holdingsSourceUrl"], report["sourceUrl"])

class CoverageBoundaryTests(unittest.TestCase):
    def test_explicit_foundation_date_exempts_only_post_period_products(self):
        from providers import parse_overview
        overview = parse_overview('<table><tr><th>基金简称</th><td>合成</td></tr><tr><th>成立日期/规模</th><td>2026年07月06日 / 10亿份</td></tr></table>')
        self.assertEqual(overview["foundationDate"], "2026-07-06")
        class NewFund:
            def overview(self, code): return overview
            def announcements(self, *args): raise AssertionError("No report due before establishment")
        fund, errors = collect_fund({"code": "000001"}, "2026-10-26", provider=NewFund(), target_period="2026-06-30")
        self.assertEqual(fund["notApplicableEvidence"]["reason"], "ESTABLISHED_AFTER_PERIOD")
        self.assertEqual(fund["notApplicableEvidence"]["date"], "2026-07-06")
        self.assertEqual(fund["reports"], [])

    def test_announcements_paginate_past_first_hundred_and_deduplicate(self):
        class Source(PublicProvider):
            def __init__(self): self.calls = []
            def get(self, url, **kwargs):
                page = kwargs["params"]["pageIndex"]; self.calls.append(page)
                if page == 1:
                    row = {"TITLE": "合成2026年第3季度报告", "PUBLISHDATEDESC": "2026-10-20", "ART_CODE": "AN202610200000000001"}
                    return json.dumps({"Data": [row] * 100})
                return json.dumps({"Data": [{"TITLE": "合成2026年第2季度报告", "PUBLISHDATEDESC": "2026-07-20", "ART_CODE": "AN202607200000000001"}]})
        source = Source()
        reports = source.announcements("000001", "2026-10-26")
        self.assertEqual(source.calls, [1, 2])
        self.assertEqual([report["periodEnd"] for report in reports], ["2026-09-30", "2026-06-30"])

class DiscardedHoldingTests(unittest.TestCase):
    def test_mixed_and_all_unsupported_security_rows_keep_explicit_counts(self):
        head = '<div class="box"><h4>2026年3季度股票投资明细</h4><table><thead><tr><th>股票代码</th><th>股票名称</th><th>占净值比例</th><th>持股数（股）</th><th>持仓市值（元）</th></tr></thead><tbody>'
        tail = '</tbody></table></div>'
        valid = '<tr><td>00700</td><td>合成港股</td><td>3%</td><td>100</td><td>20000</td></tr>'
        unsupported = '<tr><td>AAPL</td><td>合成海外股票</td><td>5%</td><td>50</td><td>10000</td></tr>'
        rows = parse_holdings_html(head + valid + unsupported + tail)["2026-09-30"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.excluded_rows, 1)
        all_unsupported = parse_holdings_html(head + unsupported + tail)["2026-09-30"]
        self.assertEqual(len(all_unsupported), 0)
        self.assertEqual(all_unsupported.excluded_rows, 1)
        class Source:
            def overview(self, code): return {"name": "合成", "company": "机构", "managers": []}
            def announcements(self, code, as_of): return [{"title": "合成2026年第3季度报告", "periodEnd": "2026-09-30", "kind": "quarterly", "publishedAt": "2026-10-20", "sourceUrl": "https://example.org/report.pdf"}]
            def holdings(self, code, year): return {"2026-09-30": rows}
            def report_details(self, document): return {"navYuan": 1, "narrative": None}
        fund, _errors = collect_fund({"code": "000001"}, "2026-10-26", provider=Source(), target_period="2026-09-30")
        self.assertTrue(fund["reports"][0]["holdingsIncomplete"])
        self.assertEqual(fund["reports"][0]["holdingsExcludedRowCount"], 1)


class CollectionSchedulingTests(unittest.TestCase):
    class Source:
        def __init__(self, periods, fail_overview=False, fail_holdings=True):
            self.periods = periods
            self.fail_overview = fail_overview
            self.fail_holdings = fail_holdings
            self.calls = []

        def overview(self, code):
            self.calls.append(('overview', code))
            if self.fail_overview:
                raise DataError('NETWORK_ERROR')
            return {'name': '合成基金', 'company': '合成机构', 'managers': []}

        def announcements(self, code, as_of):
            self.calls.append(('announcements', code))
            return [{'title': '合成报告', 'periodEnd': period, 'kind': 'quarterly',
                     'publishedAt': (dt.date.fromisoformat(period) + dt.timedelta(days=20)).isoformat(),
                     'sourceUrl': f'https://example.org/report-{period}.pdf'} for period in self.periods]

        def holdings(self, code, year):
            self.calls.append(('holdings', year))
            if self.fail_holdings:
                raise DataError('NETWORK_ERROR')
            return {}

        def report_details(self, report):
            self.calls.append(('pdf', report['periodEnd']))
            # Retain a useful PDF row without manufacturing complete-table proof.
            return {'navYuan': 10000, 'narrative': {'text': '本基金调整了组合配置。', 'page': 1,
                    'sourceUrl': report['sourceUrl'], 'periodEnd': report['periodEnd']},
                    'pdfHoldings': [{'stockCode': '600001', 'market': 'CN', 'currency': 'CNY',
                                     'shares': 100, 'weightPct': 1, 'marketValueYuan': 100}]}

    def test_overview_has_one_short_attempt_without_changing_report_transport_budget(self):
        from requests.exceptions import ConnectionError
        source = PublicProvider()
        with patch.object(source.session, 'get', side_effect=ConnectionError('synthetic unavailable endpoint')) as request, \
                patch('providers.time.sleep') as sleep:
            with self.assertRaises(DataError):
                source.overview('000001')
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.kwargs['timeout'], (2, 2))
            sleep.assert_not_called()
            self.assertEqual((source.timeout, source.attempts), (18, 2))
            for operation in (lambda: source.announcements('000001', '2026-10-26'),
                              lambda: source.report_details({'sourceUrl': 'https://example.org/report.pdf'})):
                request.reset_mock(); sleep.reset_mock()
                with self.assertRaises(DataError):
                    operation()
                self.assertEqual(request.call_count, 2)
                self.assertEqual(request.call_args.kwargs['timeout'], (8, 18))
                sleep.assert_called_once()

    def test_overview_failure_still_reaches_target_pdf_and_preserves_error(self):
        source = self.Source(['2026-09-30'], fail_overview=True)
        fund, errors = collect_fund({'code': '000001', 'name': '目录基金'}, '2026-10-26', provider=source,
                                    target_period='2026-09-30', baseline_period='2026-06-30')
        self.assertEqual(source.calls, [('overview', '000001'), ('announcements', '000001'),
                                        ('holdings', 2026), ('pdf', '2026-09-30')])
        self.assertEqual(fund['name'], '目录基金')
        self.assertEqual(fund['reports'][0]['holdings'][0]['stockCode'], '600001')
        self.assertNotIn('holdingsPdfEvidence', fund['reports'][0])
        self.assertIn({'code': '000001', 'stage': 'metadata', 'errorCode': 'NETWORK_ERROR'}, errors)
        self.assertTrue(fund['collectionAttempt']['attempted'])

    def test_same_year_target_and_baseline_skip_unrelated_year_even_when_table_fails_or_is_empty(self):
        for target, baseline in [('2026-06-30', '2026-03-31'), ('2026-09-30', '2026-06-30')]:
            for failed in [True, False]:
                with self.subTest(target=target, failed=failed):
                    source = self.Source([target, baseline], fail_holdings=failed)
                    fund, _ = collect_fund({'code': '000001'}, '2026-10-26', provider=source, history_years=3,
                                           target_period=target, baseline_period=baseline)
                    self.assertEqual([value for stage, value in source.calls if stage == 'holdings'], [2026])
                    self.assertEqual([value for stage, value in source.calls if stage == 'pdf'], [target, baseline])
                    self.assertEqual([r['periodEnd'] for r in fund['reports']], [target, baseline])

    def test_explicit_cross_year_comparison_fetches_both_needed_years_even_with_one_history_year(self):
        source = self.Source(['2026-03-31', '2025-12-31'])
        fund, _ = collect_fund({'code': '000001'}, '2026-10-26', provider=source, history_years=1,
                               target_period='2026-03-31', baseline_period='2025-12-31')
        self.assertEqual([value for stage, value in source.calls if stage == 'holdings'], [2026, 2025])
        self.assertEqual([r['periodEnd'] for r in fund['reports']], ['2026-03-31', '2025-12-31'])

    def test_target_without_baseline_requests_only_its_actual_year(self):
        source = self.Source(['2026-09-30'])
        collect_fund({'code': '000001'}, '2026-10-26', provider=source, history_years=3, target_period='2026-09-30')
        self.assertEqual([value for stage, value in source.calls if stage == 'holdings'], [2026])

    def test_unspecified_target_keeps_bounded_history_fallback(self):
        for history, expected in [(1, [2026]), (2, [2026, 2025]), (7, [2026, 2025, 2024])]:
            source = self.Source(['2026-09-30', '2026-06-30'])
            collect_fund({'code': '000001'}, '2026-10-26', provider=source, history_years=history)
            self.assertEqual([value for stage, value in source.calls if stage == 'holdings'], expected)
