"""All source responses are synthetic; local tests never enumerate remote data."""
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import cninfo_discovery as discovery

NOW = '2026-10-26T10:00:00Z'


def announcement(aid='1001', code='018774', day='2026-10-01', title=None, **extra):
    return {'announcementId': aid, 'secCode': code, 'secName': '合成基金A',
            'announcementTitle': title or '合成证券投资基金2026年第3季度报告',
            'announcementTime': int(dt.datetime.fromisoformat(day).replace(tzinfo=dt.timezone.utc).timestamp() * 1000),
            'adjunctType': 'PDF', 'adjunctUrl': f'finalpage/{day}/{aid}.PDF'} | extra


def receipt(rows, *, total=None, more=False, observed=NOW, **extra):
    payload = {'announcements': rows, 'totalRecordNum': len(rows) if total is None else total,
               'totalAnnouncement': len(rows) if total is None else total, 'hasMore': more} | extra
    raw = json.dumps(payload, ensure_ascii=False).encode()
    return {'payload': payload, 'retrievedAt': observed, 'contentHash': hashlib.sha256(raw).hexdigest(), 'responseBytes': len(raw)}


class FakeSource:
    def __init__(self, records, *, edit=None):
        self.records = records
        self.calls = []
        self.edit = edit

    def __call__(self, params):
        self.calls.append(params.copy())
        start, end = params['seDate'].split('~')
        records = [r for r in self.records if start <= dt.datetime.fromtimestamp(r['announcementTime'] / 1000, discovery.UTC).date().isoformat() <= end]
        size = int(params['pageSize']); page = int(params['pageNum'])
        result = receipt(records[(page - 1) * size:page * size], total=len(records), more=page * size < len(records))
        return self.edit(params, result) if self.edit else result


def run(source, **kwargs):
    args = {'period': '2026Q3', 'as_of': '2026-10-26', 'observed_at': NOW, 'fetch_page': source, 'window_end': '2026-10-02', 'page_size': 2}
    return discovery.discover_period(**(args | kwargs))


class CninfoDiscoveryTests(unittest.TestCase):
    def test_pagination_conserves_unique_rows_without_claiming_universe_or_pdf_verification(self):
        source = FakeSource([announcement(str(1000 + i), f'{i:06d}') for i in range(5)])
        result = run(source)
        self.assertTrue(result['windowEnumerationComplete'])
        self.assertEqual(result['status'], 'window-enumerated')
        self.assertEqual(result['counts']['requestCount'], 3)
        self.assertEqual(result['counts']['candidateCodes'], 5)
        self.assertFalse(result['historicalUniverseVerified'])
        self.assertFalse(result['allQuarterReportsVerified'])
        self.assertTrue(all(call['searchkey'] == '' and call['column'] == 'fund' and call['category'] == 'category_jdbg_jjgg' for call in source.calls))
        self.assertEqual([call['pageNum'] for call in source.calls], ['1', '2', '3'])
        record = result['announcements'][0]
        self.assertEqual(record['sourceUrl'], 'https://static.cninfo.com.cn/finalpage/2026-10-01/1000.PDF')
        self.assertFalse(record['pdfVerified'])
        self.assertFalse(result['candidates'][0]['periodExistenceVerified'])
        self.assertEqual(result['pages'][0]['contentHash'], record['sourcePage']['contentHash'])

    def test_large_slice_splits_into_disjoint_dates_before_pagination_limit(self):
        source = FakeSource([announcement(str(1000 + i), f'{i:06d}', '2026-10-01' if i < 2 else '2026-10-02') for i in range(4)])
        result = run(source, max_pages=1)
        self.assertTrue(result['windowEnumerationComplete'])
        self.assertEqual([part['status'] for part in result['slices']], ['split', 'complete', 'complete'])
        self.assertEqual(result['counts']['receivedRows'], 6)
        self.assertEqual(result['counts']['enumeratedRows'], 4)  # Parent probe is not a second observation.
        self.assertEqual(result['counts']['requestCount'], 3)
        self.assertEqual(result['counts']['duplicateRows'], 0)
        self.assertEqual({call['seDate'] for call in source.calls}, {'2026-10-01~2026-10-02', '2026-10-01~2026-10-01', '2026-10-02~2026-10-02'})

    def test_one_day_above_limit_preserves_partial_candidates(self):
        result = run(FakeSource([announcement(str(1000 + i), f'{i:06d}') for i in range(4)]), max_pages=1, window_end='2026-10-01')
        self.assertFalse(result['windowEnumerationComplete'])
        self.assertEqual(result['counts']['candidateCodes'], 2)
        self.assertIn('DAILY_PAGE_LIMIT', result['slices'][0]['errors'])
        self.assertIn('UNIQUE_ROW_COUNT_MISMATCH', result['slices'][0]['errors'])

    def test_duplicate_page_cannot_mask_unseen_rows_and_conflicting_id_is_quarantined(self):
        first = announcement('1001', '018774')
        def fetch(params):
            return receipt([first] if params['pageNum'] == '1' else [first | {'secCode': '018775'}], total=2, more=params['pageNum'] == '1')
        result = run(fetch, page_size=1)
        self.assertFalse(result['windowEnumerationComplete'])
        self.assertEqual(result['counts']['duplicateRows'], 1)
        self.assertEqual(result['counts']['conflictingAnnouncementIds'], 1)
        self.assertEqual(result['announcements'], [])
        self.assertEqual(result['candidates'], [])

    def test_identical_duplicate_preserves_one_candidate_but_never_complete(self):
        row = announcement()
        def fetch(params):
            return receipt([row], total=2, more=params['pageNum'] == '1')
        result = run(fetch, page_size=1)
        self.assertFalse(result['windowEnumerationComplete'])
        self.assertEqual(len(result['candidates']), 1)
        self.assertEqual(result['counts']['duplicateRows'], 1)

    def test_total_changes_short_pages_and_has_more_mismatch_are_visible(self):
        rows = [announcement(str(1000 + i), f'{i:06d}') for i in range(3)]
        def edit(params, answer):
            if params['pageNum'] == '2':
                answer['payload']['totalRecordNum'] = 4
                answer['payload']['announcements'] = []
                answer['payload']['hasMore'] = True
            return answer
        result = run(FakeSource(rows, edit=edit))
        self.assertFalse(result['windowEnumerationComplete'])
        self.assertEqual(len(result['candidates']), 2)
        errors = result['slices'][0]['errors']
        self.assertTrue({'TOTAL_CHANGED', 'PAGE_ROW_COUNT_MISMATCH', 'HAS_MORE_MISMATCH', 'UNIQUE_ROW_COUNT_MISMATCH'}.issubset(errors))

    def test_mid_page_network_failure_keeps_prior_evidence_and_is_not_source_absence(self):
        def fetch(params):
            if params['pageNum'] == '2':
                raise discovery.DiscoveryError('NETWORK_ERROR')
            return receipt([announcement()], total=2, more=True)
        result = run(fetch, page_size=1)
        self.assertFalse(result['windowEnumerationComplete'])
        self.assertEqual(result['counts']['candidateCodes'], 1)
        self.assertEqual(result['slices'][0]['errors'], ['NETWORK_ERROR'])

    def test_request_budget_leaves_unattempted_date_ranges_and_no_fabricated_completion(self):
        source = FakeSource([announcement()])
        result = run(source, window_end='2026-10-26', max_requests=1)
        self.assertFalse(result['windowEnumerationComplete'])
        self.assertEqual(result['limitReached'], 'REQUEST_LIMIT')
        self.assertEqual(result['counts']['requestCount'], 1)
        self.assertEqual(len(source.calls), 1)
        self.assertEqual(result['counts']['candidateCodes'], 1)
        self.assertEqual([item['status'] for item in result['slices']], ['complete', 'partial', 'unattempted', 'unattempted'])

    def test_wrong_quarter_year_and_notice_titles_are_filtered_not_marked_complete_reports(self):
        titles = ['合成证券投资基金2026年第2季度报告', '合成证券投资基金2025年第3季度报告',
                  '合成证券投资基金2026年第3季度报告提示性公告', '合成证券投资基金2026年第3季度报告更正公告',
                  '合成证券投资基金2026年度第3季度报告（更正版）', '合成证券投资基金2026年第三季度报告']
        source = FakeSource([announcement(str(1000 + i), f'{i:06d}', title=title) for i, title in enumerate(titles)])
        result = run(source)
        self.assertTrue(result['windowEnumerationComplete'])
        self.assertEqual(result['counts']['filteredNonTargetRows'], 4)
        self.assertEqual(result['counts']['targetReports'], 2)
        self.assertTrue(result['announcements'][0]['correctionLabel'])

    def test_dates_future_urls_markup_bad_codes_and_unknown_rows_cannot_silently_drop(self):
        invalid = [announcement(announcementTime=1), announcement(secCode='18774'),
                   announcement(secCode='018774,018774'), announcement(announcementTitle='<script>bad</script>'),
                   announcement(adjunctUrl='https://private.example.test/finalpage/2026-10-01/1001.PDF'),
                   announcement(adjunctUrl='https://static.cninfo.com.cn' + '@' + 'private.example.test/1001.PDF'),
                   announcement(adjunctUrl='finalpage/2026-10-27/1001.PDF'),
                   announcement(adjunctType='HTML'), announcement(announcementId='bad'), 'not-a-row']
        for row in invalid:
            with self.subTest(row=row):
                result = run(lambda _: receipt([row]))
                self.assertFalse(result['windowEnumerationComplete'])
                self.assertEqual(result['counts']['rejectedRows'], 1)
                self.assertEqual(result['candidates'], [])

    def test_date_outside_slice_rejected_even_when_it_matches_target_title(self):
        result = run(lambda _: receipt([announcement(day='2026-10-03')]))
        self.assertEqual(result['rejectionReasons'], {'PUBLICATION_OUTSIDE_WINDOW': 1})
        self.assertFalse(result['windowEnumerationComplete'])

    def test_multiple_codes_are_only_associations_without_guessing_names_or_survival(self):
        result = run(FakeSource([announcement(code='018774,018775')]))
        self.assertTrue(result['windowEnumerationComplete'])
        self.assertEqual([row['code'] for row in result['candidates']], ['018774', '018775'])
        self.assertTrue(all(row['label'] is None and not row['periodExistenceVerified'] for row in result['candidates']))
        self.assertIsNone(result['announcements'][0]['label'])

    def test_unknown_schema_receipt_hash_and_negative_limits_fail_closed(self):
        for answer in [None, {'payload': {}}, receipt([], total=True), receipt([], more=None), receipt('bad'), receipt([], total=-1)]:
            with self.subTest(answer=answer):
                result = run(lambda _: answer)
                self.assertFalse(result['windowEnumerationComplete'])
                self.assertEqual(result['candidates'], [])
        for limits in [{'page_size': 0}, {'max_pages': -1}, {'max_requests': 201}]:
            with self.assertRaises(discovery.DiscoveryError):
                run(FakeSource([]), **limits)

    def test_empty_page_is_only_a_complete_empty_window_with_exact_zero_and_no_more(self):
        for value in ([], None):
            result = run(lambda _: receipt(value, total=0))
            self.assertTrue(result['windowEnumerationComplete'])
            self.assertEqual(result['counts']['candidateCodes'], 0)
        result = run(lambda _: receipt([], total=1))
        self.assertFalse(result['windowEnumerationComplete'])

    def test_q2_window_and_late_corrected_report_support_are_explicit_and_q3_not_locked_to_october(self):
        now = dt.date(2026, 11, 5)
        self.assertEqual(discovery.window_for('2026Q2', '2026-11-05', now)[:2], (dt.date(2026, 7, 1), dt.date(2026, 7, 31)))
        self.assertEqual(discovery.window_for('2026Q2', '2026-11-05', now, include_q2_late=True)[1], now)
        self.assertEqual(discovery.window_for('2026Q3', '2026-12-01', now)[1], now)
        self.assertEqual(discovery.window_for('2026Q3', '2026-11-05', now, end='2026-10-26')[1], dt.date(2026, 10, 26))
        source = FakeSource([announcement(day='2026-08-02', title='合成基金2026年第二季度报告（更正版）')])
        result = run(source, period='2026Q2', window_end='2026-10-26', include_q2_late=True)
        self.assertTrue(result['windowEnumerationComplete'])
        self.assertEqual(result['counts']['targetReports'], 1)
        self.assertEqual(result['discoveryWindow']['end'], '2026-10-26')

    def test_actions_transport_cannot_make_local_requests_and_bad_inputs_are_bounded(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'false'}), patch('urllib.request.build_opener') as opener:
            with self.assertRaises(discovery.DiscoveryError) as error:
                discovery.ActionsTransport()
            self.assertEqual(error.exception.code, 'ACTIONS_REQUIRED')
            opener.assert_not_called()
        for value in ('2025-10-26', '2026-02-30', '2026-10-26; command'):
            with self.assertRaises(discovery.DiscoveryError):
                discovery.date_value(value)
        with self.assertRaises(discovery.DiscoveryError):
            discovery.window_for('2026Q3', '2026-09-30', dt.date(2026, 10, 26))

    def test_artifacts_are_guarded_minimal_and_keep_completeness_scopes_separate(self):
        result = run(FakeSource([announcement()]))
        encoded = discovery.artifacts_for([result])
        summary = json.loads(encoded['summary.json']); data = json.loads(encoded['candidates.json'])
        self.assertNotIn('announcements', summary['periods'][0])
        self.assertEqual(data['artifactKind'], 'candidate-manifest')
        self.assertEqual(data['periods'][0]['candidateCodeCount'], 1)
        self.assertEqual(len(json.loads(encoded['codes-2026Q3-0001.json'])['rows']), 1)
        self.assertFalse(data['historicalUniverseVerified'])
        self.assertLess(len(encoded['summary.json']), discovery.MAX_SUMMARY_BYTES)
        result['announcements'][0]['title'] = 'person' + '@' + 'private.invalid'
        with self.assertRaises(discovery.DiscoveryError) as error:
            discovery.artifacts_for([result])
        self.assertEqual(error.exception.code, 'PUBLICATION_REJECTED')

    def test_failure_does_not_leave_old_candidates_or_expose_exception_text(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder); (output / 'candidates.json').write_text('stale')
            with patch.object(discovery, 'OUTPUT', output), patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), patch.object(discovery, 'ActionsTransport', side_effect=RuntimeError('private diagnostic text')):
                self.assertEqual(discovery.main([]), 1)
            self.assertFalse((output / 'candidates.json').exists())
            self.assertNotIn('private diagnostic', (output / 'summary.json').read_text())

    def test_transport_bounds_body_http_type_redirect_and_request_budget_without_network(self):
        class Response(io.BytesIO):
            status = 200
            headers = {'Content-Type': 'application/json;charset=UTF-8'}
            def geturl(self):
                return discovery.ENDPOINT
        class Opener:
            def __init__(self, response):
                self.response = response
                self.calls = []
            def open(self, request, timeout):
                self.calls.append((request, timeout))
                return self.response
        payload = json.dumps({'announcements': [], 'totalRecordNum': 0, 'totalAnnouncement': 0, 'hasMore': False}).encode()
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}):
            opener = Opener(Response(payload))
            with patch('urllib.request.build_opener', return_value=opener):
                transport = discovery.ActionsTransport(max_requests=1)
                data = transport(discovery.params_for(dt.date(2026, 10, 1), dt.date(2026, 10, 2), 1))
                self.assertEqual(data['contentHash'], hashlib.sha256(payload).hexdigest())
                self.assertEqual(data['responseBytes'], len(payload))
                self.assertEqual(opener.calls[0][0].full_url, discovery.ENDPOINT)
                self.assertLessEqual(opener.calls[0][1], 15)
                with self.assertRaises(discovery.DiscoveryError) as error:
                    transport({})
                self.assertEqual(error.exception.code, 'REQUEST_LIMIT')
            for content, content_type, reason in [(b'x' * (discovery.MAX_RESPONSE_BYTES + 1), 'application/json', 'RESPONSE_TOO_LARGE'),
                                                  (b'<html>blocked</html>', 'text/html', 'UNEXPECTED_CONTENT_TYPE'),
                                                  (b'{"a":NaN}', 'application/json', 'INVALID_JSON')]:
                response = Response(content); response.headers = {'Content-Type': content_type}
                with patch('urllib.request.build_opener', return_value=Opener(response)):
                    with self.assertRaises(discovery.DiscoveryError) as error:
                        discovery.ActionsTransport()({})
                    self.assertEqual(error.exception.code, reason)
        with self.assertRaises(discovery.DiscoveryError) as error:
            discovery.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.test/')
        self.assertEqual(error.exception.code, 'UNEXPECTED_REDIRECT')

    def test_main_writes_two_guarded_periods_from_injected_source_and_no_production_paths(self):
        source = FakeSource([announcement(), announcement('2001', '018775', '2026-07-03', '合成基金2026年第2季度报告')],
                            edit=lambda _params, answer: answer | {'retrievedAt': '2026-10-26T23:00:00Z'})
        class FixedClock(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, 26, 10, tzinfo=discovery.UTC).astimezone(tz or discovery.UTC)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            with patch.object(discovery, 'OUTPUT', output), patch.dict(os.environ, {'GITHUB_ACTIONS': 'true'}), patch.object(discovery, 'ActionsTransport', return_value=source), patch.object(discovery.dt, 'datetime', FixedClock):
                self.assertEqual(discovery.main(['--as-of', '2026-10-26']), 0)
            self.assertEqual(sorted(path.name for path in output.iterdir()), ['announcements-2026Q2-0001.json',
                'announcements-2026Q3-0001.json', 'candidates.json', 'codes-2026Q2-0001.json', 'codes-2026Q3-0001.json', 'summary.json'])
            summary = json.loads((output / 'summary.json').read_text())
            self.assertEqual([result['targetPeriod'] for result in summary['periods']], ['2026Q2', '2026Q3'])
            self.assertEqual([result['counts']['candidateCodes'] for result in summary['periods']], [1, 1])
            self.assertFalse(summary['productionCatalogModified'])

    def test_split_probe_candidates_survive_budget_and_child_network_failure(self):
        rows = [announcement('1001'), announcement('1002', '018775', '2026-10-02')]
        result = run(FakeSource(rows), page_size=1, max_pages=1, max_requests=1)
        self.assertFalse(result['windowEnumerationComplete'])
        self.assertEqual(result['counts']['enumeratedRows'], 0)
        self.assertEqual(result['counts']['receivedRows'], 1)
        self.assertEqual(result['counts']['candidateCodes'], 1)
        self.assertTrue(result['announcements'][0]['sourcePage']['splitProbe'])
        self.assertEqual(result['counts']['unconfirmedProbeAnnouncementIds'], 1)
        def fail_children(params, answer):
            if params['seDate'].split('~')[0] == params['seDate'].split('~')[1]:
                raise discovery.DiscoveryError('NETWORK_ERROR')
            return answer
        result = run(FakeSource(rows, edit=fail_children), page_size=1, max_pages=1)
        self.assertFalse(result['windowEnumerationComplete'])
        self.assertEqual(result['counts']['candidateCodes'], 1)
        self.assertEqual(result['counts']['duplicateRows'], 0)

    def test_probe_and_leaf_duplicates_are_normal_but_missing_or_conflicting_probe_is_partial(self):
        rows = [announcement('1001'), announcement('1002', '018775', '2026-10-02')]
        result = run(FakeSource(rows), page_size=1, max_pages=1)
        self.assertTrue(result['windowEnumerationComplete'])
        self.assertEqual(result['counts']['duplicateRows'], 0)
        self.assertTrue(all(not record['sourcePage']['splitProbe'] for record in result['announcements']))
        def mutate_probe(params, answer):
            if params['seDate'] == '2026-10-01~2026-10-02':
                answer['payload']['announcements'] = [announcement('9000')]
            return answer
        result = run(FakeSource(rows, edit=mutate_probe), page_size=1, max_pages=1)
        self.assertFalse(result['windowEnumerationComplete'])
        self.assertEqual(result['counts']['targetReports'], 3)
        self.assertEqual(result['counts']['unconfirmedProbeAnnouncementIds'], 1)
        def conflict_probe(params, answer):
            if params['seDate'] == '2026-10-01~2026-10-02':
                answer['payload']['announcements'] = [announcement('1001', '012345')]
            return answer
        result = run(FakeSource(rows, edit=conflict_probe), page_size=1, max_pages=1)
        self.assertFalse(result['windowEnumerationComplete'])
        self.assertEqual(result['counts']['conflictingAnnouncementIds'], 1)
        self.assertEqual([r['announcementId'] for r in result['announcements']], ['1002'])

    def test_large_candidate_output_shards_without_dropping_rows_and_manifest_hashes_bind_parts(self):
        result = run(FakeSource([announcement(str(1000 + i), f'{i:06d}') for i in range(12)]))
        with patch.object(discovery, 'MAX_SHARD_BYTES', 1600):
            encoded = discovery.artifacts_for([result])
        manifest = json.loads(encoded['candidates.json'])['periods'][0]
        self.assertGreater(len(manifest['files']), 2)
        seen = {'announcements': [], 'codes': []}
        for descriptor in manifest['files']:
            raw = encoded[descriptor['path']]
            self.assertEqual(descriptor['sha256'], hashlib.sha256(raw).hexdigest())
            self.assertEqual(descriptor['bytes'], len(raw))
            self.assertLessEqual(len(raw), 1600)
            part = json.loads(raw)
            self.assertEqual(len(part['rows']), descriptor['rowCount'])
            seen[part['kind']].extend(part['rows'])
        self.assertEqual(seen['announcements'], result['announcements'])
        self.assertEqual(seen['codes'], result['candidates'])

    def test_parent_child_totals_must_conserve_even_when_every_probe_was_confirmed(self):
        def fetch(params):
            window = params['seDate']
            if window == '2026-10-01~2026-10-02':
                return receipt([announcement()], total=4, more=True)
            return receipt([announcement()] if window == '2026-10-01~2026-10-01' else [])
        result = run(fetch, page_size=1, max_pages=1)
        self.assertFalse(result['windowEnumerationComplete'])
        self.assertEqual(result['counts']['unconfirmedProbeAnnouncementIds'], 0)
        self.assertEqual(result['counts']['candidateCodes'], 1)
        self.assertEqual(result['slices'][0]['childDeclaredRows'], 1)
        self.assertEqual(result['slices'][0]['errors'], ['TOTAL_CHANGED_AFTER_SPLIT'])

    def test_nested_split_conservation_is_checked_bottom_up(self):
        rows = [announcement(str(1000 + i), f'{i:06d}', f'2026-10-0{i+1}') for i in range(4)]
        result = run(FakeSource(rows), page_size=1, max_pages=1, window_end='2026-10-04')
        self.assertTrue(result['windowEnumerationComplete'])
        self.assertEqual(result['slices'][0]['childDeclaredRows'], 4)
        def change_child(params, answer):
            if params['seDate'] == '2026-10-01~2026-10-02':
                answer['payload']['totalRecordNum'] = 3
                answer['payload']['totalAnnouncement'] = 3
            return answer
        result = run(FakeSource(rows, edit=change_child), page_size=1, max_pages=1, window_end='2026-10-04')
        self.assertFalse(result['windowEnumerationComplete'])
        self.assertIn('INCOMPLETE_CHILD_SLICES', result['slices'][0]['errors'])
        child = next(item for item in result['slices'] if item['sliceId'] == '2026-10-01~2026-10-02')
        self.assertEqual(child['errors'], ['TOTAL_CHANGED_AFTER_SPLIT'])
        self.assertEqual(result['counts']['candidateCodes'], 4)


if __name__ == '__main__':
    unittest.main()
