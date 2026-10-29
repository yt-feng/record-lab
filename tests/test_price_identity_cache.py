"""Synthetic oracle equivalence for exact-URL reuse within one conflict fact."""
import copy
import datetime as dt
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import price_consistency as helper
from price_coverage import CALENDAR, date_set_hash, expected_sessions

NOW = dt.datetime(2026, 10, 27, 2, tzinfo=dt.timezone.utc)


def fixture(market='CN', partial=False):
    identity = {'market': market, 'stockCode': {'CN': '600001', 'HK': '00700', 'US': 'META'}[market],
                'currency': CALENDAR['markets'][market]['currency'], 'rangeStart': '2026-04-01',
                'rangeEnd': '2026-06-30', 'calendarId': CALENDAR['id']}
    days = expected_sessions(market, identity['rangeStart'], identity['rangeEnd'])
    facts = []
    for index, low in enumerate((1, .9)):
        missing = days[-1:] if partial and index else []
        observed = days[:-1] if missing else days
        price = {**identity, 'status': 'observed', 'priceBasis': 'unadjusted', 'low': low, 'high': 2,
                 'coverageVersion': 2, 'sessionScope': CALENDAR['sessionScope'],
                 'completeness': 'partial' if missing else 'complete', 'observedCount': len(observed),
                 'observedStart': observed[0], 'observedEnd': observed[-1], 'expectedSessionCount': len(days),
                 'expectedDateSetHash': date_set_hash(days), 'observedDateSetHash': date_set_hash(observed),
                 'missingSessionCount': len(missing), 'missingSessionDates': missing,
                 'unexpectedSessionCount': 0, 'unexpectedSessionDates': [], 'rejectedRowCount': 0, 'conflictingDateCount': 0}
        source = f'https://example.org/quote/{index}?b=2&a=1'
        facts.append({'price': price, 'sourceUrl': source, 'sourceUrls': [source],
                      'fetchedAt': '2026-10-26T11:00:00.000001Z'})
    return identity, facts


def previous_fact(value, row, now):
    """The pre-cache implementation, retained as an independent execution oracle."""
    if not isinstance(value, dict): return None
    price = helper._price(value.get('price'), row)
    urls = value.get('sourceUrls')
    observed = helper._instant(value.get('fetchedAt'))
    if (price is None or not isinstance(urls, list) or not 1 <= len(urls) <= 8 or not observed
            or not dt.datetime.fromisoformat(row['rangeEnd']).replace(tzinfo=dt.timezone.utc) <= observed <= now + dt.timedelta(minutes=5)):
        return None
    identities = sorted(set(helper.source_identity(source) for source in [value.get('sourceUrl'), *urls]))
    return {'factId': helper.canonical_digest({'sourceIdentity': identities, 'price': price}), 'price': price,
            'sourceUrl': value['sourceUrl'], 'sourceUrls': list(urls), 'fetchedAt': value['fetchedAt']}


def outcome(fn, *args):
    try:
        return ('return', fn(*args))
    except (ValueError, TypeError, KeyError, OverflowError) as error:
        return ('error', type(error), str(error))


class PriceIdentityCacheTests(unittest.TestCase):
    def assert_equivalent(self, identity, facts, now=NOW):
        before = copy.deepcopy((identity, facts))
        with patch.object(helper, '_fact', previous_fact):
            expected = helper.conflict_for(facts, identity, now)
        actual = helper.conflict_for(facts, identity, now)
        self.assertEqual(actual, expected)
        self.assertEqual((identity, facts), before)
        for fact in facts:
            self.assertEqual(outcome(helper._fact, fact, identity, now), outcome(previous_fact, fact, identity, now))
        return actual

    def test_oracle_equivalence_preserves_all_raw_evidence_and_order(self):
        for market in ('CN', 'HK', 'US'):
            for partial in (False, True):
                identity, facts = fixture(market, partial)
                for fact in facts:
                    source = fact['sourceUrl']
                    fact['sourceUrls'] = [source, 'https://example.org/secondary?b=2&a=1', source,
                                          'https://example.org/secondary?a=1&b=2', source]
                with self.subTest(market=market, partial=partial):
                    value = self.assert_equivalent(identity, facts)
                    self.assertIsNotNone(value)
                    originals = {fact['sourceUrl']: fact for fact in facts}
                    for clean in value['facts']:
                        original = originals[clean['sourceUrl']]
                        self.assertEqual(clean['sourceUrls'], original['sourceUrls'])
                        self.assertEqual(clean['fetchedAt'], original['fetchedAt'])
                        self.assertEqual(clean['price'], original['price'])
                        self.assertIsNot(clean['sourceUrls'], original['sourceUrls'])

    def test_primary_duplicate_reduces_normalizations_from_two_to_one(self):
        identity, facts = fixture()
        original = helper.source_identity
        with patch.object(helper, 'source_identity', wraps=original) as calls:
            old = previous_fact(facts[0], identity, NOW)
            self.assertEqual(calls.call_count, 2)
        with patch.object(helper, 'source_identity', wraps=original) as calls:
            new = helper._fact(facts[0], identity, NOW)
            self.assertEqual(calls.call_count, 1)
        self.assertEqual(new, old)

    def test_up_to_eight_identical_secondary_urls_preserve_multiplicity(self):
        identity, facts = fixture()
        fact = facts[0]
        original = helper.source_identity
        for count in (1, 2, 8):
            fact['sourceUrls'] = [fact['sourceUrl']] * count
            with self.subTest(count=count), patch.object(helper, 'source_identity', wraps=original) as calls:
                actual = helper._fact(fact, identity, NOW)
                self.assertEqual(calls.call_count, 1)
                self.assertEqual(actual['sourceUrls'], fact['sourceUrls'])
            self.assertEqual(actual, previous_fact(fact, identity, NOW))

    def test_different_raw_urls_are_each_normalized_even_if_identities_match(self):
        cases = [
            ['https://example.org/x?a=1&b=2', 'https://example.org/x?b=2&a=1'],
            ['https://query1.finance.yahoo.com/chart?period1=1&period2=2',
             'https://query2.finance.yahoo.com/chart?period2=3&period1=1'],
            ['https://example.org/x?v=hello+world', 'https://example.org/x?v=hello%20world'],
            ['https://example.org/x?a=1&a=2', 'https://example.org/x?a=2&a=1'],
            ['https://example.org/x?a=', 'https://example.org/x?a'],
            ['https://example.org/x?' + f'a={index}' for index in range(8)],
        ]
        original = helper.source_identity
        for urls in cases:
            identity, facts = fixture()
            facts[0].update(sourceUrl=urls[0], sourceUrls=urls)
            with self.subTest(urls=urls), patch.object(helper, 'source_identity', wraps=original) as calls:
                actual = helper._fact(facts[0], identity, NOW)
                self.assertEqual(calls.call_count, len(urls))
            self.assertEqual(actual, previous_fact(facts[0], identity, NOW))
            self.assert_equivalent(identity, facts)

    def test_cache_does_not_cross_facts_or_invocations(self):
        identity, facts = fixture()
        source = facts[0]['sourceUrl']
        facts[1].update(sourceUrl=source, sourceUrls=[source])
        original = helper.source_identity
        with patch.object(helper, 'source_identity', wraps=original) as calls:
            first = helper.conflict_for(facts, identity, NOW)
            self.assertEqual(calls.call_count, 2)
            self.assertEqual(helper.conflict_for(facts, identity, NOW), first)
            self.assertEqual(calls.call_count, 4)
        self.assertIsNotNone(first)
        self.assert_equivalent(identity, facts)

    def test_invalid_and_unhashable_sources_keep_original_failure_behavior(self):
        invalid = [None, False, 1, [], {}, {'x'}, ('https://example.org/x',), bytearray(b'bad'),
                   'https://localhost/x', 'https://example.org/x?token=test', 'http://example.org/x',
                   'https://example.org:99999/x', 'https://example.org/a\\b', 'https://example.org/x#f',
                   'https://example.org/with space', 'https://example.org/\x7f', 'https://example.org/\ud800']
        for source in invalid:
            for primary in (False, True):
                identity, facts = fixture()
                if primary: facts[0]['sourceUrl'] = source
                else: facts[0]['sourceUrls'] = [facts[0]['sourceUrl'], source]
                with self.subTest(source=repr(source), primary=primary):
                    self.assertIsNone(self.assert_equivalent(identity, facts))

    def test_string_subclasses_are_not_used_as_cache_keys(self):
        class StringSubclass(str):
            pass
        class UnhashableString(str):
            __hash__ = None
        original = helper.source_identity
        for kind in (StringSubclass, UnhashableString):
            identity, facts = fixture()
            source = kind(facts[0]['sourceUrl'])
            facts[0].update(sourceUrl=source, sourceUrls=[source])
            with self.subTest(kind=kind.__name__):
                self.assert_equivalent(identity, facts)
                with patch.object(helper, 'source_identity', wraps=original) as calls:
                    expected = outcome(previous_fact, facts[0], identity, NOW)
                    previous_calls = calls.call_count
                with patch.object(helper, 'source_identity', wraps=original) as calls:
                    actual = outcome(helper._fact, facts[0], identity, NOW)
                    self.assertEqual(calls.call_count, previous_calls)
                self.assertEqual(actual, expected)

    def test_source_length_and_sensitive_query_boundaries_are_unchanged(self):
        prefix = 'https://example.org/'
        cases = [(prefix + 'x' * (2048 - len(prefix)), True),
                 (prefix + 'x' * (2049 - len(prefix)), False),
                 (prefix + '?%74oken=test', False), (prefix + '?API_KEY=test', False),
                 (prefix + '?tokenized=synthetic', True), (prefix + '?a=%26token%3Dsynthetic', True)]
        for source, accepted in cases:
            identity, facts = fixture()
            facts[0].update(sourceUrl=source, sourceUrls=[source])
            with self.subTest(length=len(source), accepted=accepted):
                self.assertEqual(self.assert_equivalent(identity, facts) is not None, accepted)

    def test_changed_input_is_revalidated_on_every_invocation(self):
        identity, facts = fixture()
        first = self.assert_equivalent(identity, facts)
        facts[0]['sourceUrls'].append('https://example.org/new-source')
        second = self.assert_equivalent(identity, facts)
        self.assertNotEqual(first['witnessDigest'], second['witnessDigest'])
        facts[0]['sourceUrls'][-1] = 'https://example.org/x?secret=test'
        self.assertIsNone(self.assert_equivalent(identity, facts))
        facts[0]['sourceUrls'] = [facts[0]['sourceUrl']]
        self.assertEqual(self.assert_equivalent(identity, facts), first)
        facts[0]['sourceUrl'] = 'https://example.org/changed-primary'
        self.assertNotEqual(self.assert_equivalent(identity, facts)['witnessDigest'], first['witnessDigest'])

    def test_observation_and_now_boundaries_are_rechecked(self):
        stamps = [('2026-06-30T00:00:00Z', True), ('2026-06-29T23:59:59.999999Z', False),
                  ('2026-10-27T02:05:00Z', True), ('2026-10-27T02:05:00.000001Z', False),
                  ('2026-10-27T10:05:00+08:00', True), ('2026-10-27T10:05:00.000001+08:00', False),
                  ('2026-10-27T02:00:00+00:60', False), ('2026-10-27T24:00:00Z', False),
                  ('2026-10-27T02:00:00', False)]
        for stamp, accepted in stamps:
            identity, facts = fixture()
            facts[0]['fetchedAt'] = stamp
            with self.subTest(stamp=stamp):
                self.assertEqual(self.assert_equivalent(identity, facts) is not None, accepted)
        identity, facts = fixture()
        facts[0]['fetchedAt'] = '2026-10-27T02:05:00Z'
        self.assertIsNone(self.assert_equivalent(identity, facts, NOW - dt.timedelta(microseconds=1)))
        self.assertIsNotNone(self.assert_equivalent(identity, facts, NOW))
        facts[0]['fetchedAt'] = '2026-06-29T23:59:59.999999Z'
        self.assertIsNone(self.assert_equivalent(identity, facts))

    def test_implicit_clock_is_still_read_once_for_each_conflict_call(self):
        identity, facts = fixture()
        facts[0]['fetchedAt'] = '2026-10-27T02:05:00Z'
        original_datetime = dt.datetime

        def run(oracle):
            class Clock(original_datetime):
                calls = 0

                @classmethod
                def now(cls, tz=None):
                    values = ['2026-10-27T01:59:59.999999+00:00', '2026-10-27T02:00:00+00:00']
                    value = cls.fromisoformat(values[cls.calls])
                    cls.calls += 1
                    return value

            with patch.object(helper.dt, 'datetime', Clock):
                if oracle:
                    with patch.object(helper, '_fact', previous_fact):
                        values = [helper.conflict_for(facts, identity), helper.conflict_for(facts, identity)]
                else:
                    values = [helper.conflict_for(facts, identity), helper.conflict_for(facts, identity)]
            return values, Clock.calls

        expected, before_calls = run(True)
        actual, after_calls = run(False)
        self.assertEqual(actual, expected)
        self.assertEqual((before_calls, after_calls), (2, 2))
        self.assertIsNone(actual[0])
        self.assertIsNotNone(actual[1])

    def test_pre_url_validation_and_list_limits_are_unchanged(self):
        for size in (0, 9):
            identity, facts = fixture()
            facts[0]['sourceUrls'] *= size
            with self.subTest(size=size), patch.object(helper, 'source_identity', wraps=helper.source_identity) as calls:
                self.assertIsNone(helper._fact(facts[0], identity, NOW))
                self.assertEqual(calls.call_count, 0)
            self.assertIsNone(self.assert_equivalent(identity, facts))
        identity, facts = fixture()
        for invalid_price in ({}, None, {'status': 'missing'}):
            facts[0]['price'] = invalid_price
            self.assertIsNone(self.assert_equivalent(identity, facts))

    def test_normalizer_remains_exact_and_rejects_tampering(self):
        identity, facts = fixture()
        facts[0]['sourceUrls'] *= 3
        witness = self.assert_equivalent(identity, facts)
        args = ('CN', '600001', '2026-04-01', '2026-06-30', NOW)
        mutations = [lambda value: None, lambda value: value.update(witnessDigest='0' * 64),
                     lambda value: value['facts'][0]['sourceUrls'].append('https://example.org/additional'),
                     lambda value: value['facts'][0].update(fetchedAt='2026-10-27T02:05:00.000001Z'),
                     lambda value: value['facts'][0].update(factId='0' * 64), lambda value: value['facts'].reverse()]
        for index, mutate in enumerate(mutations):
            value = copy.deepcopy(witness)
            mutate(value)
            with self.subTest(mutation=index):
                with patch.object(helper, '_fact', previous_fact):
                    expected = helper.normalize_price_conflict(value, *args)
                self.assertEqual(helper.normalize_price_conflict(value, *args), expected)
                self.assertEqual(expected is not None, index == 0)


if __name__ == '__main__': unittest.main()
