"""Synthetic packets and transports only; no credentials or network required."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import deepseek_strategy as adapter
from test_strategy_semantics import packet, claim


def envelope(body, *, interpreted=True):
    source = json.loads(body['messages'][1]['content'])
    value = {'schemaVersion': 1, 'inputDigest': source['inputDigest'], 'sentences': [
        {'sentence_id': row['sentence_id'], 'status': 'interpreted' if interpreted and row['index'] == 0 else 'unknown',
         'reason': None if interpreted and row['index'] == 0 else 'not-classified',
         'claims': [claim(row['text'])] if interpreted and row['index'] == 0 else []}
        for row in source['sentences']]}
    return {'httpStatus': 200, 'body': json.dumps({'model': adapter.MODEL,
        'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': json.dumps(value, ensure_ascii=False)}}],
        'usage': {'prompt_tokens': 100, 'completion_tokens': 200, 'total_tokens': 300}}, ensure_ascii=False)}


def synthetic_worker(pipe, _credential, _body):
    """Exercise real spawn/pipe cleanup without an HTTP client."""
    pipe.send_bytes(b'{"errorCode":"PROVIDER_RATE_LIMITED"}')
    pipe.close()


class DeepSeekStrategyTests(unittest.TestCase):
    def test_missing_credential_does_not_enter_transport_or_checkpoint(self):
        calls = []
        result = adapter.run_packet(packet(), None, checkpoint=calls.append,
                                    transport=lambda *_: self.fail('unexpected transport'))
        self.assertEqual((result['status'], result['attempted'], result['requestCount']), ('pending', False, 0))
        self.assertEqual(result['errorCode'], 'PROVIDER_CREDENTIALS_MISSING')
        self.assertEqual(calls, [])

    def test_one_request_checkpoint_and_source_time_are_preserved(self):
        events = []
        def transport(_credential, body):
            self.assertEqual(events[-1]['outcome'], 'started')
            self.assertEqual(body['model'], adapter.MODEL)
            self.assertEqual(body['thinking'], {'type': 'disabled'})
            self.assertEqual(body['response_format'], {'type': 'json_object'})
            self.assertFalse(body['stream']); self.assertNotIn('tools', body)
            self.assertEqual(body['max_tokens'], 12000)
            return envelope(body)
        source = packet()
        result = adapter.run_packet(source, 'synthetic-credential', checkpoint=events.append, transport=transport)
        self.assertEqual((result['status'], result['requestCount']), ('validated', 1))
        self.assertEqual([row['outcome'] for row in events], ['started', 'validated'])
        self.assertEqual(result['analysis']['source'], source['source'])
        self.assertFalse(result['semanticAnalysisComplete']); self.assertFalse(result['productionReady'])
        self.assertNotIn('synthetic-credential', json.dumps(result))

    def test_checkpoint_failure_prevents_model_request(self):
        with self.assertRaises(ValueError):
            adapter.run_packet(packet(), 'synthetic', checkpoint=lambda _: (_ for _ in ()).throw(ValueError('checkpoint')),
                               transport=lambda *_: self.fail('request after failed checkpoint'))

    def test_http_rejection_unknown_timeout_and_invalid_envelope_never_retry(self):
        for error, status in [('PROVIDER_AUTHENTICATION_FAILED', 'failed'), ('PROVIDER_BALANCE_REQUIRED', 'failed'),
                              ('PROVIDER_RATE_LIMITED', 'failed'), ('PROVIDER_TIMEOUT', 'unknown')]:
            calls = []
            def transport(*_): calls.append(True); return {'errorCode': error}
            result = adapter.run_packet(packet(), 'synthetic', checkpoint=lambda _: None, transport=transport)
            self.assertEqual(len(calls), 1); self.assertEqual(result['status'], status)
            self.assertEqual(result['errorCode'], error)

    def test_invalid_literal_empty_truncated_and_unexpected_model_are_not_validated(self):
        def altered(kind, body):
            outer = json.loads(envelope(body)['body'])
            message = outer['choices'][0]['message']
            if kind == 'empty': message['content'] = ''
            elif kind == 'length': outer['choices'][0]['finish_reason'] = 'length'
            elif kind == 'model': outer['model'] = 'unverified-model'
            elif kind == 'literal':
                value = json.loads(message['content']); value['sentences'][0]['claims'][0]['predicate']['text'] = '卖出'
                message['content'] = json.dumps(value)
            return {'httpStatus': 200, 'body': json.dumps(outer)}
        for kind in ('empty', 'length', 'model', 'literal'):
            with self.subTest(kind=kind):
                result = adapter.run_packet(packet(), 'synthetic', checkpoint=lambda _: None,
                                            transport=lambda _, body: altered(kind, body))
                self.assertNotEqual(result['status'], 'validated')
                self.assertFalse(result['semanticAnalysisComplete'])
                if kind == 'empty':
                    self.assertEqual(result['formatDiagnostics']['leadingToken'], 'empty')
                    self.assertEqual(result['formatDiagnostics']['originalBytes'], 0)
                if kind == 'length':
                    self.assertIsNotNone(result['formatDiagnostics'])
                    self.assertEqual(result['errorCode'], 'PROVIDER_OUTPUT_INCOMPLETE')

    def test_request_limit_and_digest_do_not_depend_on_source_observed_refresh(self):
        first = packet(); later = packet(report={**first['source'], 'strategyEvidenceRetrievedAt': '2026-10-26T11:01:00Z'})
        results = [adapter.run_packet(value, 'synthetic', checkpoint=lambda _: None,
                                      transport=lambda _, body: envelope(body, interpreted=False)) for value in (first, later)]
        self.assertEqual(results[0]['receipt']['requestDigest'], results[1]['receipt']['requestDigest'])
        large = packet('本基金看好科技行业。' * 129)
        result = adapter.run_packet(large, 'synthetic', checkpoint=lambda _: self.fail('unexpected checkpoint'))
        self.assertFalse(result['attempted']); self.assertEqual(result['requestCount'], 0)

    def test_http_once_disables_redirects_retries_and_never_reads_error_body(self):
        for status in (302, 401, 402):
            response, session = MagicMock(), MagicMock()
            response.status_code = status
            response.iter_content.side_effect = AssertionError('error body must not be read')
            session.__enter__.return_value = session; session.post.return_value = response
            result = adapter._http_once('synthetic', {}, session_factory=lambda: session)
            self.assertEqual(session.post.call_count, 1)
            args, kwargs = session.post.call_args
            self.assertEqual(args, (adapter.ENDPOINT,))
            self.assertFalse(kwargs['allow_redirects']); self.assertTrue(kwargs['stream'])
            self.assertEqual(session.mount.call_args.args[1].max_retries.total, 0)
            self.assertNotIn('body', result); response.close.assert_called_once()

    def test_response_byte_limit_and_hard_process_deadline(self):
        response, session = MagicMock(), MagicMock()
        response.status_code = 200; response.iter_content.return_value = [b'x' * (adapter.MAX_RESPONSE_BYTES + 1)]
        session.__enter__.return_value = session; session.post.return_value = response
        self.assertEqual(adapter._http_once('synthetic', {}, session_factory=lambda: session)['errorCode'], 'PROVIDER_RESPONSE_TOO_LARGE')
        context, receive, send, child = MagicMock(), MagicMock(), MagicMock(), MagicMock()
        context.Pipe.return_value = receive, send; context.Process.return_value = child
        receive.poll.return_value = False; child.is_alive.side_effect = [True, False]
        with patch.dict('os.environ', {'GITHUB_ACTIONS': 'true'}), patch.object(adapter.multiprocessing, 'get_context', return_value=context):
            self.assertEqual(adapter.http_transport('synthetic', {})['errorCode'], 'PROVIDER_TIMEOUT')
        receive.poll.assert_called_once_with(adapter.WALL_SECONDS)
        child.terminate.assert_called_once(); child.join.assert_called_once_with(timeout=5)

    def test_real_transport_requires_actions_without_starting_process(self):
        with patch.dict('os.environ', {'GITHUB_ACTIONS': 'false'}), patch.object(adapter.multiprocessing, 'get_context', side_effect=AssertionError):
            self.assertEqual(adapter.http_transport('synthetic', {})['errorCode'], 'PROVIDER_ACTIONS_REQUIRED')

    def test_received_malformed_json_is_rejected_and_exception_body_is_never_retained(self):
        result = adapter.run_packet(packet(), 'synthetic', checkpoint=lambda _: None,
            transport=lambda *_: {'httpStatus': 200, 'body': 'private provider text, invalid json'})
        self.assertEqual(result['status'], 'rejected'); self.assertNotIn('private provider text', json.dumps(result))
        result = adapter.run_packet(packet(), 'synthetic', checkpoint=lambda _: None,
            transport=lambda *_: (_ for _ in ()).throw(RuntimeError('do not expose arbitrary exception')))
        self.assertEqual(result['status'], 'unknown'); self.assertNotIn('arbitrary exception', json.dumps(result))

    def test_actual_spawn_pipe_bridge_without_network(self):
        with patch.dict('os.environ', {'GITHUB_ACTIONS': 'true'}):
            result = adapter.http_transport('synthetic', {}, worker_target=synthetic_worker)
        self.assertEqual(result, {'errorCode': 'PROVIDER_RATE_LIMITED'})
        self.assertEqual(adapter.multiprocessing.active_children(), [])

    def test_backend_version_labels_are_not_admitted_as_response_model_ids(self):
        for model in ('DeepSeek-V4.1-Flash-1017', 'deepseek-v4.1-flash-1017'):
            with self.subTest(model=model):
                def transport(_, body):
                    response = json.loads(envelope(body)['body'])
                    response['model'] = model
                    return {'httpStatus': 200, 'body': json.dumps(response)}
                result = adapter.run_packet(packet(), 'synthetic', checkpoint=lambda _: None, transport=transport)
                self.assertEqual(result['status'], 'rejected')
                self.assertEqual(result['errorCode'], 'PROVIDER_MODEL_UNEXPECTED')
                self.assertIsNone(result['responseModel']); self.assertIsNone(result['analysis'])
                self.assertEqual(result['requestCount'], 1)

    def test_prompt_has_literal_json_example_with_actual_ids_not_default_unknown(self):
        value = packet(); body = adapter.request_body(value)
        system = body['messages'][0]['content']
        self.assertIn('Do not copy unknown as a default', system)
        self.assertIn('Do not use Markdown or code fences', system)
        example = json.loads(system.rsplit('\n', 1)[1])
        self.assertEqual(example['inputDigest'], value['inputDigest'])
        self.assertEqual(example['sentences'][0]['sentence_id'], value.model_input()['sentences'][0]['sentence_id'])
        self.assertNotIn('本基金看好科技行业', system)

    def test_exact_single_json_fence_is_only_removed_wrapper_and_keeps_raw_digest(self):
        for opening, newline in [('json', '\n'), ('JSON', '\r\n')]:
            received = []
            def transport(_, body):
                outer = json.loads(envelope(body)['body'])
                content = outer['choices'][0]['message']['content']
                wrapped = ' \n```' + opening + newline + content + newline + '```\n '
                outer['choices'][0]['message']['content'] = wrapped
                raw = json.dumps(outer, ensure_ascii=False)
                received.append((content, wrapped, raw))
                return {'httpStatus': 200, 'body': raw}
            value = packet()
            result = adapter.run_packet(value, 'synthetic', checkpoint=lambda _: None, transport=transport)
            self.assertEqual(result['status'], 'validated')
            self.assertEqual(result['analysis']['source'], value['source'])
            diagnostic = result['formatDiagnostics']
            self.assertEqual(diagnostic['fence'], 'complete-json')
            self.assertEqual(diagnostic['normalization'], 'removed-single-json-fence')
            self.assertEqual(diagnostic['jsonStatus'], 'valid'); self.assertIsNone(diagnostic['jsonError'])
            content, wrapped, raw = received[0]
            self.assertEqual(diagnostic['originalDigest'], hashlib.sha256(wrapped.encode()).hexdigest())
            # CR before the closing delimiter is legal original JSON whitespace,
            # and may remain in the literal substring; nothing inside is fixed.
            normalized, _ = adapter.content_format(wrapped)
            self.assertEqual(normalized.strip(), content)
            self.assertEqual(diagnostic['normalizedDigest'], hashlib.sha256(normalized.encode()).hexdigest())
            self.assertEqual(result['responseDigest'], hashlib.sha256(raw.encode()).hexdigest())
            self.assertEqual(result['requestCount'], 1)

    def test_bad_wrappers_never_extract_braces_or_hide_trailing_text(self):
        cases = [('```json\n{}', 'incomplete'), ('```python\n{}\n```', 'complete-other'),
                 ('```\n{}\n```', 'complete-other'), ('```json\n{}\n```\nextra', 'mixed-content'),
                 ('intro\n```json\n{}\n```', 'mixed-content'), ('prefix {} suffix', 'none'),
                 ('```json\n{}\n```\n```json\n{}\n```', 'multiple')]
        for content, fence in cases:
            with self.subTest(fence=fence, content=content[:12]):
                normalized, diagnostic = adapter.content_format(content)
                self.assertEqual(normalized, content)
                self.assertEqual(diagnostic['fence'], fence)
                self.assertEqual(diagnostic['normalization'], 'none')
                self.assertEqual(diagnostic['jsonStatus'], 'invalid')
                self.assertEqual(diagnostic['originalDigest'], diagnostic['normalizedDigest'])

    def test_diagnostics_are_fixed_structure_with_exact_json_offsets_and_no_body(self):
        content = '{\n "private report fragment": invalid\n}'
        normalized, diagnostic = adapter.content_format(content)
        self.assertEqual(normalized, content)
        try:
            json.loads(content)
        except json.JSONDecodeError as error:
            expected = {'category': 'syntax', 'line': error.lineno, 'column': error.colno, 'position': error.pos}
        self.assertEqual(diagnostic['jsonError'], expected)
        self.assertEqual(diagnostic['originalCharacters'], len(content))
        self.assertEqual(diagnostic['originalBytes'], len(content.encode()))
        self.assertNotIn('private report fragment', json.dumps(diagnostic))
        self.assertEqual(set(diagnostic), {'schemaVersion', 'digestEncoding', 'originalBytes', 'originalCharacters',
            'originalDigest', 'leadingToken', 'fence', 'normalization', 'normalizedBytes', 'normalizedCharacters',
            'normalizedDigest', 'jsonStatus', 'jsonError'})

    def test_fenced_duplicate_keys_nonfinite_and_syntax_errors_remain_rejected(self):
        cases = [('{"x":1,"x":2}', 'duplicate-key'), ('{"x":NaN}', 'nonfinite'),
                 ('{"x":Infinity}', 'nonfinite'), ('{"x":1e999}', 'nonfinite'),
                 ('{"x":1,}', 'syntax'), ('{"x":1} trailing', 'syntax')]
        for content, category in cases:
            for wrap in (False, True):
                with self.subTest(category=category, wrap=wrap):
                    original = '```json\n' + content + '\n```' if wrap else content
                    normalized, diagnostic = adapter.content_format(original)
                    self.assertEqual(normalized, content)
                    self.assertEqual(diagnostic['jsonError']['category'], category)
                    self.assertEqual(diagnostic['jsonStatus'], 'invalid')
                    outer = {'model': adapter.MODEL, 'choices': [{'finish_reason': 'stop',
                        'message': {'role': 'assistant', 'content': original}}]}
                    result = adapter.run_packet(packet(), 'synthetic', checkpoint=lambda _: None,
                        transport=lambda *_: {'httpStatus': 200, 'body': json.dumps(outer)})
                    self.assertEqual(result['status'], 'rejected')
                    self.assertEqual(result['requestCount'], 1)

    def test_unicode_depth_size_and_non_json_token_diagnostics_stay_bounded(self):
        cases = [('\ud800', 'unicode'), ('{"x":"\\ud800"}', 'unicode'),
                 (' ' * 400001, 'size')]
        for content, category in cases:
            _, diagnostic = adapter.content_format(content)
            self.assertEqual(diagnostic['jsonError'], {'category': category, 'line': None, 'column': None, 'position': None})
            self.assertEqual(len(diagnostic['originalDigest']), 64)
        # Decoder nesting limits vary by Python version. Preserve the typed
        # failure without depending on a particular interpreter's depth cap.
        with patch.object(adapter, '_json', side_effect=RecursionError()):
            diagnostic = adapter.content_format('[]')[1]
            self.assertEqual(diagnostic['jsonError']['category'], 'depth')
        for content, token in [('', 'empty'), ('{}', 'object'), ('[]', 'array'), ('"word"', 'string'),
                               ('0', 'number'), ('true', 'true'), ('false', 'false'), ('null', 'null'), ('word', 'other')]:
            self.assertEqual(adapter.content_format(content)[1]['leadingToken'], token)

    def test_no_wrapper_normalization_preserves_whitespace_and_unicode_codepoints(self):
        content = '  {"value":"中文😀"} \n'
        normalized, diagnostic = adapter.content_format(content)
        self.assertEqual(normalized, content)
        self.assertEqual(diagnostic['normalization'], 'none')
        self.assertEqual(diagnostic['originalCharacters'], len(content))
        self.assertGreater(diagnostic['originalBytes'], diagnostic['originalCharacters'])
        self.assertEqual(diagnostic['originalDigest'], diagnostic['normalizedDigest'])


if __name__ == '__main__':
    unittest.main()
