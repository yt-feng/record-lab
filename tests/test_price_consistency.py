"""Bounded synthetic witnesses; no source data or network operations."""
import copy
import datetime as dt
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import price_consistency as helper
import price_conflicts as registry
from price_coverage import CALENDAR, expected_sessions, date_set_hash

NOW = dt.datetime(2026, 10, 27, 2, tzinfo=dt.timezone.utc)


def fixture(partial=False, market='CN'):
    identity = {'market': market, 'stockCode': {'CN': '600001', 'HK': '00700', 'US': 'META'}[market],
                'currency': CALENDAR['markets'][market]['currency'], 'rangeStart': '2026-04-01',
                'rangeEnd': '2026-06-30', 'calendarId': CALENDAR['id']}
    days = expected_sessions(market, identity['rangeStart'], identity['rangeEnd'])
    def fact(low, source, incomplete=False):
        observed = days[:-1] if incomplete else days
        missing = days[-1:] if incomplete else []
        price = {**identity, 'status': 'observed', 'priceBasis': 'unadjusted', 'low': low, 'high': 2,
                 'coverageVersion': 2, 'sessionScope': CALENDAR['sessionScope'],
                 'completeness': 'partial' if incomplete else 'complete', 'observedCount': len(observed),
                 'observedStart': observed[0], 'observedEnd': observed[-1], 'expectedSessionCount': len(days),
                 'expectedDateSetHash': date_set_hash(days), 'observedDateSetHash': date_set_hash(observed),
                 'missingSessionCount': len(missing), 'missingSessionDates': missing,
                 'unexpectedSessionCount': 0, 'unexpectedSessionDates': [], 'rejectedRowCount': 0, 'conflictingDateCount': 0}
        return {'price': price, 'sourceUrl': source, 'sourceUrls': [source], 'fetchedAt': '2026-10-26T11:00:00.000001Z'}
    return identity, [fact(1, 'https://example.org/quote?b=2&a=1'), fact(.9, 'https://example.org/other', partial)]


class PriceConsistencyTests(unittest.TestCase):
    def test_exact_pair_and_original_observation_round_trip(self):
        for partial in (False, True):
            identity, facts = fixture(partial); original = copy.deepcopy(facts)
            value = helper.conflict_for(facts, identity, NOW)
            self.assertEqual(value['types'], ['partial-extreme' if partial else 'complete-source-disagreement'])
            self.assertEqual(value['resolution'], 'unresolved')
            self.assertEqual(facts, original)
            self.assertEqual(helper.normalize_price_conflict(value, 'CN', '600001', '2026-04-01', '2026-06-30', NOW), value)
            self.assertEqual(value['facts'][0]['fetchedAt'], facts[0]['fetchedAt'])
            self.assertEqual(helper.conflict_for(facts[::-1], identity, NOW), value)

    def test_numeric_and_source_canonicalization_match_existing_registry(self):
        self.assertEqual(helper.PRICE_FIELDS, registry.PRICE_FIELDS)
        for value in [0, -0.0, 1, 1.0, .9, 1.00000000001, 1e-30, 1e30, 5e-324, 1.7976931348623157e308,
                      {'a': [True, False, None, .1], 'b': 12}]:
            self.assertEqual(helper.canonical_digest(value), registry.digest(value))
        for source in ['https://example.org/x?b=2&a=1', 'https://example.org?x=%21%27%28%29%2A&x=hello+world',
                       'https://query2.finance.yahoo.com/v8/finance/chart/META?period2=99&period1=10&interval=1d',
                       'https://example.org/路径?a=%E4%B8%AD&b=%F0%9F%98%80']:
            self.assertEqual(helper.source_identity(source), registry.source_identity(source))
        identity, facts = fixture()
        value = helper.conflict_for(facts, identity, NOW)
        for fact in value['facts']:
            sources = sorted(set(registry.source_identity(url) for url in [fact['sourceUrl'], *fact['sourceUrls']]))
            self.assertEqual(fact['factId'], registry.digest({'sourceIdentity': sources, 'price': fact['price']}))
        self.assertEqual(value['witnessDigest'], registry.digest({'key': value['key'], 'types': value['types'],
                                                                  'factIds': [fact['factId'] for fact in value['facts']]}))

    def test_calendar_and_partial_observations_cannot_be_malformed_witnesses(self):
        identity, facts = fixture(True)
        for change in [{'observedCount': 1}, {'observedStart': '2026-01-01'}, {'observedEnd': '2026-06-31'},
                       {'observedDateSetHash': '0' * 64}, {'missingSessionDates': []}, {'missingSessionCount': 0},
                       {'unexpectedSessionCount': 1}, {'coverageVersion': 1}, {'calendarId': 'unknown'},
                       {'rejectedRowCount': 1}, {'conflictingDateCount': 1}, {'low': float('nan')},
                       {'low': True}, {'priceBasis': 'adjusted'}, {'currency': 'USD'}]:
            broken = copy.deepcopy(facts); broken[1]['price'].update(change)
            self.assertIsNone(helper.conflict_for(broken, identity, NOW), change)
        broken = copy.deepcopy(facts); del broken[1]['price']['observedStart']
        self.assertIsNone(helper.conflict_for(broken, identity, NOW))

    def test_stale_private_future_and_wrong_identity_are_rejected(self):
        identity, facts = fixture()
        for stamp in ['2026-06-29T00:00:00Z', '2026-10-27T02:05:00.000001Z',
                      '2026-10-26T11:00:00+00:60', '2026-10-26T24:00:00Z', '2026-10-26T11:00:00']:
            broken = copy.deepcopy(facts); broken[0]['fetchedAt'] = stamp
            self.assertIsNone(helper.conflict_for(broken, identity, NOW), stamp)
        for url in ['https://localhost/price', 'https://127.0.0.1/price', 'https://example.org/?token=value',
                    'http://example.org/price', 'https://example.org:99999/price', 'https://example.org/a\\b']:
            broken = copy.deepcopy(facts); broken[0]['sourceUrls'] = [url]
            self.assertIsNone(helper.conflict_for(broken, identity, NOW), url)
        for change in [{'rangeEnd': '2026-09-30'}, {'stockCode': '600002'}, {'currency': 'HKD'}, {'rangeStart': '2026-04-02'}]:
            self.assertIsNone(helper.conflict_for(facts, {**identity, **change}, NOW))

    def test_no_conflict_from_matching_complete_or_narrow_partial_intervals(self):
        for partial in (False, True):
            identity, facts = fixture(partial); facts[1]['price']['low'] = 1
            self.assertIsNone(helper.conflict_for(facts, identity, NOW))
        identity, facts = fixture(); facts[1] = copy.deepcopy(facts[0])
        self.assertIsNone(helper.conflict_for(facts, identity, NOW))
        self.assertIsNone(helper.conflict_for(facts[:1], identity, NOW))
        self.assertIsNone(helper.conflict_for(facts * 2, identity, NOW))

    def test_normalizer_rejects_tampered_or_noncanonical_evidence(self):
        identity, facts = fixture(); value = helper.conflict_for(facts, identity, NOW)
        mutations = [lambda item: item.update(witnessDigest='0' * 64), lambda item: item.update(resolution='resolved'),
                     lambda item: item['facts'][0].update(factId='0' * 64), lambda item: item['facts'].reverse(),
                     lambda item: item.update(types=['partial-extreme']), lambda item: item.update(extra=True),
                     lambda item: item['facts'][0]['price'].update(secret='not allowed')]
        for mutate in mutations:
            broken = copy.deepcopy(value); mutate(broken)
            self.assertIsNone(helper.normalize_price_conflict(broken, 'CN', '600001', '2026-04-01', '2026-06-30', NOW))
        facts[0]['price']['globalPriceConflict'] = {'untrusted': True}
        self.assertEqual(helper.conflict_for(facts, identity, NOW), value)

    def test_observation_refresh_and_yahoo_endpoint_changes_do_not_rearm_digest(self):
        identity, facts = fixture()
        facts[1].update(sourceUrl='https://query1.finance.yahoo.com/chart?period1=1&period2=2',
                        sourceUrls=['https://query1.finance.yahoo.com/chart?period1=1&period2=2'])
        first = helper.conflict_for(facts, identity, NOW)
        facts[1].update(sourceUrl='https://query2.finance.yahoo.com/chart?period2=3&period1=1',
                        sourceUrls=['https://query2.finance.yahoo.com/chart?period2=3&period1=1'], fetchedAt='2026-10-27T01:00:00Z')
        second = helper.conflict_for(facts, identity, NOW)
        self.assertEqual(first['witnessDigest'], second['witnessDigest'])
        self.assertNotEqual(first, second)


if __name__ == '__main__': unittest.main()
