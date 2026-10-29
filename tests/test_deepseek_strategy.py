"""Synthetic packets and transports only; no credentials or network required."""
import copy
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import deepseek_strategy as adapter
import strategy_semantics as semantic_core
from test_strategy_semantics import packet, claim, START, END


def text_only(value):
    if isinstance(value, list):
        return [text_only(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {'start', 'end', 'text'}:
            return {'text': value['text']}
        return {key: text_only(item) for key, item in value.items()}
    return value


def envelope(body, *, interpreted=True):
    source = json.loads(body['messages'][1]['content'])
    value = {'schemaVersion': 1, 'inputDigest': source['inputDigest'], 'sentences': [
        {'sentence_id': row['sentence_id'], 'status': 'interpreted' if interpreted and row['index'] == 0 else 'unknown',
         'reason': None if interpreted and row['index'] == 0 else 'not-classified',
         'claims': [text_only(claim(row['text']))] if interpreted and row['index'] == 0 else []}
        for row in source['sentences']]}
    return {'httpStatus': 200, 'body': json.dumps({'model': adapter.MODEL,
        'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': json.dumps(value, ensure_ascii=False)}}],
        'usage': {'prompt_tokens': 100, 'completion_tokens': 200, 'total_tokens': 300}}, ensure_ascii=False)}


def synthetic_worker(pipe, _credential, _body):
    """Exercise real spawn/pipe cleanup without an HTTP client."""
    pipe.send_bytes(b'{"errorCode":"PROVIDER_RATE_LIMITED"}')
    pipe.close()


class DeepSeekStrategyTests(unittest.TestCase):
    def test_input_limit_preflight_is_explicit_and_never_enters_transport(self):
        checkpoints = []
        with patch.object(adapter, 'request_body', side_effect=ValueError('SEMANTIC_INPUT_LIMIT')):
            result = adapter.run_packet(packet(), 'synthetic', checkpoint=checkpoints.append,
                transport=lambda *_: self.fail('preflight limit must not call transport'))
        self.assertEqual((result['status'], result['errorCode'], result['attempted'], result['requestCount']),
                         ('rejected', 'SEMANTIC_INPUT_LIMIT', False, 0))
        self.assertEqual(checkpoints, [])

    def test_private_callback_runs_after_final_checkpoint_and_cannot_mutate_public_result(self):
        checkpoints, received = [], []
        def callback(bound, inference):
            self.assertEqual(checkpoints[-1]['outcome'], 'validated')
            self.assertIsNotNone(checkpoints[-1]['finishedAt'])
            self.assertEqual(inference['usage']['total_tokens'], 300)
            self.assertEqual(inference['bindingDiagnostics']['boundResponseDigest'],
                hashlib.sha256(adapter.canonical(bound).encode()).hexdigest())
            received.append(copy.deepcopy(bound))
            bound['privateSentinel'] = 'PRIVATE_CALLBACK_SENTINEL'
            inference['analysis'] = 'PRIVATE_CALLBACK_SENTINEL'
            return {'privateSentinel': 'PRIVATE_CALLBACK_SENTINEL'}
        result = adapter.run_packet(packet(), 'synthetic', checkpoint=checkpoints.append,
            transport=lambda _, body: envelope(body), on_validated=callback)
        self.assertEqual(len(received), 1)
        self.assertEqual(result['status'], 'validated')
        self.assertNotIn('PRIVATE_CALLBACK_SENTINEL', json.dumps(result))

    def test_private_callback_is_never_called_for_rejection_unknown_or_failed_final_checkpoint(self):
        for response in ({'errorCode': 'PROVIDER_TIMEOUT'}, {'errorCode': 'PROVIDER_AUTHENTICATION_FAILED'},
                         {'httpStatus': 200, 'body': 'malformed'}):
            adapter.run_packet(packet(), 'synthetic', checkpoint=lambda _: None,
                transport=lambda *_: response, on_validated=lambda *_: self.fail('unexpected callback'))
        def final_checkpoint(receipt):
            if receipt['outcome'] == 'validated': raise ValueError('checkpoint failure')
        with self.assertRaises(ValueError):
            adapter.run_packet(packet(), 'synthetic', checkpoint=final_checkpoint,
                transport=lambda _, body: envelope(body), on_validated=lambda *_: self.fail('unexpected callback'))

    def test_callback_failure_keeps_final_usage_receipt_and_never_exposes_exception_body(self):
        checkpoints = []
        with self.assertRaisesRegex(ValueError, '^VALIDATED_CALLBACK_FAILED$'):
            adapter.run_packet(packet(), 'synthetic', checkpoint=checkpoints.append,
                transport=lambda _, body: envelope(body),
                on_validated=lambda *_: (_ for _ in ()).throw(RuntimeError('PRIVATE_CALLBACK_SENTINEL')))
        self.assertEqual(checkpoints[-1]['outcome'], 'validated')

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
            self.assertEqual(body['thinking'], {'type': 'enabled'})
            self.assertEqual(body['reasoning_effort'], 'low')
            self.assertNotIn('temperature', body)
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
            self.assertEqual(kwargs['timeout'], (10, 130))
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

    def test_model_schema_is_text_only_and_bound_output_restores_core_offsets(self):
        value = packet('😀本基金看好科技行业。')
        schema = adapter.model_response_schema(value)
        def walk(node):
            if isinstance(node, dict):
                if node.get('type') == 'object':
                    self.assertNotIn('start', node['properties']); self.assertNotIn('end', node['properties'])
                    self.assertFalse(node['additionalProperties'])
                for item in node.values(): walk(item)
            elif isinstance(node, list):
                for item in node: walk(item)
        walk(schema)
        body = adapter.request_body(value)
        raw = json.loads(json.loads(envelope(body)['body'])['choices'][0]['message']['content'])
        original = copy.deepcopy(raw)
        bound, diagnostic, preflight = adapter.bind_literal_response(value, raw)
        self.assertIsNone(preflight); self.assertEqual(raw, original)
        self.assertEqual(bound['sentences'][0]['claims'][0]['subject']['span'], {'text': '本基金', 'start': 1, 'end': 4})
        self.assertEqual(diagnostic['status'], 'bound')
        self.assertEqual(diagnostic['examinedSpanCount'], diagnostic['boundSpanCount'])
        self.assertEqual(diagnostic['boundResponseDigest'], hashlib.sha256(adapter.canonical(bound).encode()).hexdigest())
        self.assertNotIn('本基金', json.dumps(diagnostic, ensure_ascii=False))
        analysis = adapter.validate_semantic_response(value, bound)
        self.assertEqual(analysis['status'], 'structure-and-evidence-validated')
        self.assertFalse(analysis['semanticAnalysisComplete'])

    def test_binding_rejects_old_offsets_extra_fields_non_dict_empty_and_fuzzy_text(self):
        value = packet(); body = adapter.request_body(value)
        source = json.loads(json.loads(envelope(body)['body'])['choices'][0]['message']['content'])
        cases = [({'text': '看好', 'start': 3, 'end': 5}, 'MODEL_SPAN_SHAPE_INVALID'),
                 ({'text': '看好', 'other': 1}, 'MODEL_SPAN_SHAPE_INVALID'),
                 ('看好', 'MODEL_SPAN_SHAPE_INVALID'), ({'text': ''}, 'MODEL_LITERAL_EMPTY'),
                 ({'text': ' '}, 'MODEL_LITERAL_EMPTY'), ({'text': ' 看好'}, 'MODEL_LITERAL_NOT_FOUND'),
                 ({'text': '看 好'}, 'MODEL_LITERAL_NOT_FOUND'), ({'text': '看好。'}, 'MODEL_LITERAL_NOT_FOUND')]
        for span_value, code in cases:
            changed = copy.deepcopy(source); changed['sentences'][0]['claims'][0]['predicate'] = span_value
            bound, diagnostic, _ = adapter.bind_literal_response(value, changed)
            self.assertIsNone(bound); self.assertEqual(diagnostic['errorCode'], code)
            self.assertIsNone(diagnostic['boundResponseDigest'])

    def test_unique_match_rejects_overlapping_and_repeated_literals(self):
        for text, target in [('本基金看好哈哈哈行业。', '哈哈'), ('本基金看好科技行业及科技行业。', '科技行业')]:
            value = packet(text); item = text_only(claim(text, target=target))
            raw = {'schemaVersion': 1, 'inputDigest': value['inputDigest'], 'sentences': [
                {'sentence_id': value.model_input()['sentences'][0]['sentence_id'], 'status': 'interpreted', 'reason': None, 'claims': [item]}]}
            bound, diagnostic, _ = adapter.bind_literal_response(value, raw)
            self.assertIsNone(bound); self.assertEqual(diagnostic['errorCode'], 'MODEL_LITERAL_AMBIGUOUS')

    def test_preflight_never_repairs_unknown_ids_duplicates_fields_status_or_reason(self):
        value = packet('本基金看好科技行业。市场情况难以判断。')
        body = adapter.request_body(value)
        raw = json.loads(json.loads(envelope(body, interpreted=False)['body'])['choices'][0]['message']['content'])
        mutations = [lambda row: row.update(extra=True),
            lambda row: row['sentences'][0].update(extra=True),
            lambda row: row['sentences'][0].update(sentence_id='unknown'),
            lambda row: row['sentences'][1].update(sentence_id=row['sentences'][0]['sentence_id']),
            lambda row: row['sentences'][0].update(status='invalid'),
            lambda row: row['sentences'][0].update(reason='invalid'),
            lambda row: row['sentences'][0].update(status='interpreted', reason=None),
            lambda row: row['sentences'][0].update(claims=[{'predicate': {'text': '看好', 'start': 3, 'end': 5}}]),
            lambda row: row['sentences'][0].update(claims=[{'predicate': '看好'}])]
        for mutate in mutations:
            changed = copy.deepcopy(raw); mutate(changed); original = copy.deepcopy(changed)
            bound, diagnostic, preflight = adapter.bind_literal_response(value, changed)
            self.assertIsNone(bound); self.assertEqual(changed, original)
            self.assertEqual(diagnostic['errorCode'], 'STRICT_VALIDATOR_PREFLIGHT_REJECTED')
            self.assertEqual(preflight['status'], 'rejected')

    def test_binding_does_not_bypass_negation_condition_or_author_attribution(self):
        cases = [
            ('本基金并不看好科技行业。', 'NEGATION_REQUIRES_REVIEW'),
            ('如果盈利改善，本基金看好科技行业。', 'CONDITION_NOT_PRESERVED'),
            ('报告引用：“本基金看好科技行业”。', 'QUOTED_ATTRIBUTION_CHANGED')]
        for text, reason in cases:
            value = packet(text); body = adapter.request_body(value)
            result = adapter.run_packet(value, 'synthetic', checkpoint=lambda _: None,
                transport=lambda *_: envelope(body))
            self.assertEqual(result['bindingDiagnostics']['status'], 'bound')
            self.assertEqual(result['status'], 'rejected')
            self.assertIn(reason, result['analysis']['reasonCodes'])
            self.assertFalse(result['semanticAnalysisComplete'])

    def test_binding_success_changes_only_span_positions_and_keeps_other_extra_fields_for_core(self):
        value = packet(); body = adapter.request_body(value)
        raw = json.loads(json.loads(envelope(body)['body'])['choices'][0]['message']['content'])
        raw['sentences'][0]['claims'][0]['fabricatedSummary'] = 'must be rejected'
        bound, diagnostic, _ = adapter.bind_literal_response(value, raw)
        self.assertEqual(diagnostic['status'], 'bound')
        self.assertEqual(bound['sentences'][0]['claims'][0]['fabricatedSummary'], 'must be rejected')
        self.assertEqual(adapter.validate_semantic_response(value, bound)['status'], 'rejected')

    def test_binding_zero_spans_for_unknown_remains_accounted_and_not_semantic_complete(self):
        value = packet(); body = adapter.request_body(value)
        result = adapter.run_packet(value, 'synthetic', checkpoint=lambda _: None,
            transport=lambda *_: envelope(body, interpreted=False))
        self.assertEqual(result['bindingDiagnostics']['status'], 'bound')
        self.assertEqual(result['bindingDiagnostics']['examinedSpanCount'], 0)
        self.assertEqual(result['bindingDiagnostics']['boundSpanCount'], 0)
        self.assertEqual(result['analysis']['coverage']['unknown'], 1)
        self.assertFalse(result['analysis']['semanticCorrectnessVerified'])

    def test_model_can_cite_unique_verbatim_context_but_adapter_never_expands_a_span(self):
        text = '本基金持续看好科技行业，也看好消费行业。'
        value = packet(text); row = text_only(claim(text))
        raw = {'schemaVersion': 1, 'inputDigest': value['inputDigest'], 'sentences': [
            {'sentence_id': value.model_input()['sentences'][0]['sentence_id'], 'status': 'interpreted', 'reason': None, 'claims': [row]}]}
        bound, diagnostic, _ = adapter.bind_literal_response(value, raw)
        self.assertIsNone(bound); self.assertEqual(diagnostic['errorCode'], 'MODEL_LITERAL_AMBIGUOUS')
        raw['sentences'][0]['claims'][0]['predicate'] = {'text': '持续看好'}
        bound, diagnostic, _ = adapter.bind_literal_response(value, raw)
        self.assertEqual(diagnostic['status'], 'bound')
        self.assertEqual(bound['sentences'][0]['claims'][0]['predicate']['text'], '持续看好')
        self.assertEqual(adapter.validate_semantic_response(value, bound)['status'], 'structure-and-evidence-validated')
        raw['sentences'][0]['claims'][0]['subject']['span'] = {'text': '本基金持续看好科技行业'}
        bound, diagnostic, _ = adapter.bind_literal_response(value, raw)
        self.assertEqual(diagnostic['status'], 'bound')
        self.assertIn('INDIVIDUAL_MANAGER_ATTRIBUTION_NOT_PROVEN', adapter.validate_semantic_response(value, bound)['reasonCodes'])

    def test_private_constraints_use_core_regex_objects_and_exact_unicode_marker_positions(self):
        text = '😀如果盈利改善，本基金并不看好科技行业，相比上季我们预计将关注消费。'
        value = packet(text)
        constraints = adapter.constrained_model_input(value)['sentences'][0]['validation_constraints']
        for name, field in [('_NEGATION', 'negation_spans'), ('_CONDITION', 'condition_spans'), ('_COMPARISON', 'comparison_spans')]:
            pattern = getattr(semantic_core, name)
            self.assertIs(getattr(adapter, name), pattern)
            expected = [{'text': hit.group(), 'start': hit.start(), 'end': hit.end()} for hit in pattern.finditer(text)]
            self.assertEqual(constraints['requiredLiteralMarkers'][field], expected)
            for hit in expected:
                self.assertEqual(text[hit['start']:hit['end']], hit['text'])
        self.assertIs(adapter._FUTURE, semantic_core._FUTURE)
        self.assertEqual(constraints['futureMarkers'], [{'text': hit.group(), 'start': hit.start(), 'end': hit.end()}
                                                       for hit in semantic_core._FUTURE.finditer(text)])
        self.assertEqual(constraints['allowedTimes'], ['conditional', 'mixed'])
        self.assertEqual(constraints['allowedNegations'], ['negated', 'uncertain'])
        self.assertTrue(constraints['conditionalRequired']); self.assertFalse(constraints['isSourceFact'])
        self.assertEqual(constraints['fundReportSubjectLiterals'], ['本基金', '本组合', '本产品', '我们'])

    def test_quote_constraints_follow_full_context_across_sentences_and_pages(self):
        value = packet(pages=[START + '报告引用：“市场平稳。', '本基金看好科技行业。继续观察”。' + END], page_numbers=[7, 8])
        rows = adapter.constrained_model_input(value)['sentences']
        self.assertTrue(any(row['pages'] == [8] for row in rows))
        for row in rows:
            if row['quoted_context']:
                self.assertEqual(row['validation_constraints']['allowedSubjectScopes'], ['quoted-other', 'unknown'])
        for body in ('报告引用：“市场平稳。本基金看好科技行业。', '本基金看好“科技行业”。'):
            for row in adapter.constrained_model_input(packet(body))['sentences']:
                if row['quoted_context']:
                    self.assertEqual(row['validation_constraints']['allowedSubjectScopes'], ['unknown'])

    def test_constraints_do_not_mutate_packet_source_clock_or_public_output(self):
        value = packet()
        original_manifest, original_private = copy.deepcopy(dict(value)), value.model_input()
        captured = []
        def transport(_, body):
            captured.append(json.loads(body['messages'][1]['content']))
            return envelope(body)
        result = adapter.run_packet(value, 'synthetic', checkpoint=lambda _: None, transport=transport)
        self.assertEqual(dict(value), original_manifest); self.assertEqual(value.model_input(), original_private)
        self.assertIn('validation_constraints', captured[0]['sentences'][0])
        self.assertEqual(captured[0]['source'], original_private['source'])
        self.assertEqual(captured[0]['inputDigest'], original_private['inputDigest'])
        public = json.dumps(result)
        self.assertNotIn('validation_constraints', public); self.assertNotIn('allowedSubjectScopes', public)
        self.assertNotIn('requiredLiteralMarkers', public); self.assertNotIn('constraintsDigest', public)

    def test_request_digest_binds_constraints_separately_from_unchanged_packet_digest(self):
        value = packet()
        first = adapter.run_packet(value, 'synthetic', checkpoint=lambda _: None,
                                   transport=lambda _, body: envelope(body))
        original = adapter.constrained_model_input
        def changed(packet_value):
            private = original(packet_value)
            private['sentences'][0]['validation_constraints']['allowedTimes'] = ['unspecified']
            return private
        with patch.object(adapter, 'constrained_model_input', side_effect=changed):
            second = adapter.run_packet(value, 'synthetic', checkpoint=lambda _: None,
                                        transport=lambda _, body: envelope(body))
        self.assertEqual(first['receipt']['inputDigest'], second['receipt']['inputDigest'])
        self.assertNotEqual(first['receipt']['requestDigest'], second['receipt']['requestDigest'])
        self.assertEqual(first['analysis']['source'], second['analysis']['source'])

    def test_unknown_quote_subject_can_retain_supported_opinion_without_full_sentence_unknown(self):
        value = packet('本基金看好“科技行业”。')
        body = adapter.request_body(value)
        self.assertEqual(json.loads(body['messages'][1]['content'])['sentences'][0]['validation_constraints']['allowedSubjectScopes'], ['unknown'])
        response = json.loads(envelope(body)['body'])
        content = json.loads(response['choices'][0]['message']['content'])
        content['sentences'][0]['claims'][0]['subject'] = {'scope': 'unknown', 'span': None}
        response['choices'][0]['message']['content'] = json.dumps(content, ensure_ascii=False)
        result = adapter.run_packet(value, 'synthetic', checkpoint=lambda _: None,
                                    transport=lambda *_: {'httpStatus': 200, 'body': json.dumps(response, ensure_ascii=False)})
        self.assertEqual(result['status'], 'validated')
        self.assertEqual(result['analysis']['coverage']['interpreted'], 1)
        self.assertEqual(result['analysis']['claims'][0]['subject']['scope'], 'unknown')
        self.assertFalse(result['analysis']['semanticCorrectnessVerified'])

    def test_supported_negation_and_condition_can_satisfy_unchanged_validator(self):
        for text, update in [
            ('本基金并不看好科技行业。', {'negation': 'negated', 'polarity': 'negative', 'negation_spans': [{'text': '并不'}]}),
            ('如果盈利改善，本基金看好科技行业。', {'conditional': True, 'time': 'conditional', 'condition_spans': [{'text': '如果盈利改善'}]})]:
            value = packet(text); body = adapter.request_body(value)
            outer = json.loads(envelope(body)['body']); content = json.loads(outer['choices'][0]['message']['content'])
            content['sentences'][0]['claims'][0].update(update)
            outer['choices'][0]['message']['content'] = json.dumps(content, ensure_ascii=False)
            result = adapter.run_packet(value, 'synthetic', checkpoint=lambda _: None,
                                        transport=lambda *_: {'httpStatus': 200, 'body': json.dumps(outer, ensure_ascii=False)})
            self.assertEqual(result['status'], 'validated')
            self.assertEqual(result['analysis']['coverage']['interpreted'], 1)
            self.assertFalse(result['analysis']['semanticAnalysisComplete'])

    def test_private_constraint_echo_or_instruction_as_literal_is_never_public_analysis(self):
        value = packet(); body = adapter.request_body(value)
        private = json.loads(body['messages'][1]['content'])
        for kind in ('extra-field', 'instruction-literal'):
            outer = json.loads(envelope(body)['body']); content = json.loads(outer['choices'][0]['message']['content'])
            if kind == 'extra-field':
                content['sentences'][0]['validation_constraints'] = private['sentences'][0]['validation_constraints']
            else:
                content['sentences'][0]['claims'][0]['predicate'] = {'text': 'respect validation_constraints'}
            outer['choices'][0]['message']['content'] = json.dumps(content)
            result = adapter.run_packet(value, 'synthetic', checkpoint=lambda _: None,
                                        transport=lambda *_: {'httpStatus': 200, 'body': json.dumps(outer)})
            self.assertEqual(result['status'], 'rejected')
            self.assertNotIn('validation_constraints', json.dumps(result))
            self.assertNotIn('allowedSubjectScopes', json.dumps(result))

    def test_future_only_hints_do_not_invent_a_condition_or_negation(self):
        value = packet('未来本基金预计看好科技行业。')
        constraint = adapter.constrained_model_input(value)['sentences'][0]['validation_constraints']
        self.assertEqual(constraint['allowedTimes'], ['future', 'conditional', 'mixed', 'unspecified'])
        self.assertEqual(constraint['allowedNegations'], ['affirmed', 'negated', 'uncertain'])
        self.assertIsNone(constraint['conditionalRequired'])
        self.assertEqual(constraint['requiredLiteralMarkers']['negation_spans'], [])

    def test_thinking_and_effort_are_bound_from_actual_request_settings(self):
        value = packet()
        def run():
            return adapter.run_packet(value, 'synthetic', checkpoint=lambda _: None,
                                      transport=lambda _, body: envelope(body))
        base = run()
        with patch.object(adapter, 'THINKING_MODE', 'disabled'):
            mode_changed = run()
        with patch.object(adapter, 'REASONING_EFFORT', 'high'):
            effort_changed = run()
        self.assertEqual(base['receipt']['inputDigest'], mode_changed['receipt']['inputDigest'])
        self.assertEqual(base['receipt']['inputDigest'], effort_changed['receipt']['inputDigest'])
        self.assertEqual(len({row['receipt']['requestDigest'] for row in (base, mode_changed, effort_changed)}), 3)
        self.assertEqual(adapter.MAX_OUTPUT_TOKENS, 12000)
        self.assertEqual(adapter.WALL_SECONDS, 150)
        self.assertEqual(adapter.MAX_RESPONSE_BYTES, 600000)
        body = adapter.request_body(value)
        self.assertEqual(body['thinking'], {'type': 'enabled'})
        self.assertEqual(body['reasoning_effort'], 'low'); self.assertNotIn('temperature', body)

    def test_reasoning_content_never_enters_success_rejection_unknown_checkpoint_or_logs(self):
        sentinel = 'PRIVATE_REASONING_SENTINEL_NEVER_PUBLISHED'
        for outcome in ('validated', 'binding-rejected', 'truncated', 'no-answer', 'unknown-timeout', 'unknown-exception'):
            checkpoints, raw_responses = [], []
            def transport(_, body):
                if outcome == 'unknown-timeout':
                    return {'errorCode': 'PROVIDER_TIMEOUT', 'reasoning_content': sentinel}
                if outcome == 'unknown-exception':
                    raise RuntimeError(sentinel)
                response = json.loads(envelope(body)['body'])
                message = response['choices'][0]['message']
                message['reasoning_content'] = sentinel
                if outcome == 'binding-rejected':
                    content = json.loads(message['content'])
                    content['sentences'][0]['claims'][0]['predicate'] = {'text': 'source absent literal'}
                    message['content'] = json.dumps(content)
                elif outcome == 'truncated':
                    response['choices'][0]['finish_reason'] = 'length'
                elif outcome == 'no-answer':
                    message['content'] = None
                raw = json.dumps(response, ensure_ascii=False)
                raw_responses.append(raw)
                return {'httpStatus': 200, 'body': raw}
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = adapter.run_packet(packet(), 'synthetic', checkpoint=checkpoints.append, transport=transport)
            expected = 'validated' if outcome == 'validated' else 'unknown' if outcome.startswith('unknown-') else 'rejected'
            self.assertEqual(result['status'], expected)
            self.assertEqual(result['requestCount'], 1)
            self.assertEqual(checkpoints[0]['outcome'], 'started')
            self.assertNotIn(sentinel, json.dumps(result))
            self.assertNotIn(sentinel, json.dumps(checkpoints))
            self.assertNotIn('reasoning_content', json.dumps(result))
            self.assertEqual(stdout.getvalue(), ''); self.assertEqual(stderr.getvalue(), '')
            if outcome == 'no-answer':
                self.assertIsNone(result['analysis'])
                self.assertEqual(result['errorCode'], 'PROVIDER_RESPONSE_INVALID')
            elif raw_responses:
                self.assertEqual(result['responseDigest'], hashlib.sha256(raw_responses[0].encode()).hexdigest())


if __name__ == '__main__':
    unittest.main()
