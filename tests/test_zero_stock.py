import copy
import datetime as dt
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from providers import parse_no_equities
from zero_stock import parse_zero_stock, valid_zero_stock
from traversal import no_equities_content, valid_event_evidence

FIXTURE = Path(__file__).parent / 'fixtures/zero-stock.json'


class ZeroStockTests(unittest.TestCase):
    def setUp(self):
        self.evidence = json.loads(FIXTURE.read_text())

    def test_explicit_dated_balance_sheet_row_and_public_provider_fallback(self):
        e = self.evidence
        parsed = parse_no_equities(['合成封面', e['text']], e)
        self.assertEqual(parsed['page'], 2)
        self.assertEqual(parsed['evidenceType'], 'zero-stock-balance-sheet')
        self.assertTrue(valid_zero_stock(parsed))
        self.assertTrue(no_equities_content(parsed))

    def test_no_zero_inference_from_wrong_period_column_income_or_partial_table(self):
        for text in [self.evidence['text'].replace('- -', '- 12.00'),
                     self.evidence['text'].replace('- -', '12.00 -'),
                     self.evidence['text'].replace('股票投资', '股票投资收益'),
                     self.evidence['text'].replace('2026年6月30日', '2025年6月30日'),
                     self.evidence['text'].replace('本期末', '上期末'),
                     self.evidence['text'].replace('单位：人民币元', '单位：港元'),
                     self.evidence['text'].split('其中：')[1],
                     self.evidence['text'] + '\n只是未核验样例']:
            with self.subTest(text=text[-30:]):
                self.assertFalse(valid_zero_stock({**self.evidence, 'text': text}))
        self.assertIsNone(parse_zero_stock([self.evidence['text']], {**self.evidence, 'kind': 'quarterly'}))

    def test_event_proof_survives_replay_but_mixed_periods_do_not(self):
        e = copy.deepcopy(self.evidence)
        event = {'sourceUrl': e['sourceUrl'], 'periodEnd': e['periodEnd'], 'publishedAt': e['publishedAt'],
                 'observedAt': '2026-10-26T00:00:00Z', 'noEquities': e}
        phase = {'targetPeriodEnd': '2026-06-30', 'baselinePeriodEnd': '2026-03-31'}
        now = dt.datetime(2026, 10, 26, tzinfo=dt.timezone.utc)
        self.assertTrue(valid_event_evidence(event, 'no-equities', phase, now))
        e['periodEnd'] = '2025-06-30'
        self.assertFalse(valid_event_evidence(event, 'no-equities', phase, now))


if __name__ == '__main__': unittest.main()
