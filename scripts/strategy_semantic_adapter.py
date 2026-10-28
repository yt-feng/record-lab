"""One explicit inference over one already-fetched original PDF.

This adapter has no filesystem, credential lookup, PDF HTTP request, default
model transport, or production writer. It connects the actual private parser
callback to strict inference and the canonical mapper. Its bounded candidate
still needs independent serialized admission and pool compare-and-swap.
"""
from __future__ import annotations

import copy
import datetime as dt

from deepseek_strategy import run_packet
from providers import parse_pdf_pages
from report_extract import extract_report
import strategy_evidence_pool as pool_api
import strategy_semantic_pool as mapper
import strategy_semantics as semantic

MAX_PDF_BYTES = 16_000_000


class SemanticCheckpointError(ValueError):
    pass


class _AttemptAlreadyRecorded(ValueError):
    pass


def prepare_candidate(pdf_bytes, retained_report, *, code, source_observed_at,
                      credential, usage_history, expected_usage_history_digest,
                      expected_baseline_digest, checkpoint, transport=None,
                      max_attempts=1, extractor=extract_report, now=None):
    """Return bounded candidate data; never expose the private packet/response.

The caller supplies the immutable report and complete prior attempt ledger.
Every started/finalized attempt is checkpointed, including failures and unknown
outcomes. A pending/unknown last attempt blocks another call. Explicitly
injecting a transport is required even if credentials are present.
"""
    result = {'schemaVersion': 1, 'kind': 'strategy-semantic-adapter', 'status': 'pending',
              'errorCode': None, 'parseCount': 0, 'modelRequests': 0, 'modelStatus': None,
              'requestReceipt': None, 'usageHistory': [], 'usageHistoryDigest': None,
              'candidate': None, 'productionReady': False, 'productionAdmission': 'not-connected',
              'semanticAnalysisComplete': False, 'productionDataModified': False}

    def clock():
        return semantic._instant(now or dt.datetime.now(dt.timezone.utc))

    def emit():
        try:
            checkpoint(copy.deepcopy(result))
        except Exception:
            raise SemanticCheckpointError('SEMANTIC_CHECKPOINT_FAILED') from None

    def finish(status, error=None):
        result.update(status=status, errorCode=error)
        emit()
        return result

    try:
        current = clock()
        if current is None or type(max_attempts) is not int or not 1 <= max_attempts <= mapper.MAX_HISTORY:
            return finish('rejected', 'SEMANTIC_ADAPTER_INPUT_INVALID')
        mapper.validate_usage_history(usage_history, expected_usage_history_digest, current, allow_empty=True)
        result['usageHistory'] = copy.deepcopy(usage_history)
        result['usageHistoryDigest'] = expected_usage_history_digest
        if usage_history and usage_history[-1]['outcome'] in {'started', 'unknown'}:
            return finish('pending', 'MODEL_PRIOR_OUTCOME_UNRESOLVED')
        if len(usage_history) >= max_attempts:
            return finish('pending', 'MODEL_ATTEMPT_LIMIT')
        if not isinstance(credential, str) or not credential.strip():
            return finish('pending', 'PROVIDER_CREDENTIALS_MISSING')
        if transport is None:
            return finish('pending', 'MODEL_TRANSPORT_NOT_CONFIGURED')
        if not callable(transport) or not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b'%PDF') \
                or len(pdf_bytes) > MAX_PDF_BYTES or not isinstance(retained_report, dict):
            return finish('rejected', 'SEMANTIC_ADAPTER_INPUT_INVALID')
        report = copy.deepcopy(retained_report)
        proof = report.get('strategySectionCoverage')
        pool = pool_api.normalize_strategy_evidence_pool(report.get('strategyEvidencePool'), proof, now=current)
        if not pool or pool['digest'] != expected_baseline_digest:
            return finish('rejected', 'BASELINE_POOL_CHANGED')
        source = pool['source']
        observed = semantic._instant(source_observed_at)
        if (not observed or observed < semantic._instant(source['retrievedAt'])
                or observed > current + dt.timedelta(minutes=5)):
            return finish('rejected', 'SOURCE_OBSERVATION_MISMATCH')
        captured = []
        result['parseCount'] = 1
        emit()
        extractor(pdf_bytes, source['sourceUrl'], source['periodEnd'], source['kind'],
            parse_pages=parse_pdf_pages, max_pages=300, env={}, published_at=source['publishedAt'],
            retrieved_at=source_observed_at, on_private_sections=lambda **context: captured.append(context))
        if len(captured) != 1:
            return finish('rejected', 'PRIVATE_SECTION_CALLBACK_INVALID')
        context = captured[0]
        quotes = [{**quote, **{field: source[field] for field in ('sourceUrl', 'periodStart', 'periodEnd')}}
                  for quote in pool['quotes']]
        packet = semantic.build_semantic_packet(context['pages'],
            {**source, 'retrievedAt': source_observed_at, 'strategyEvidenceRetrievedAt': source_observed_at},
            existing_evidence=quotes, sections_result=context['sections_result'],
            page_numbers=context['page_numbers'], credentials_available=True, now=current)
        if packet.get('status') != 'ready':
            return finish('rejected', 'SEMANTIC_PACKET_NOT_READY')
        identity = context['details'].get('portfolioIdentityEvidence')
        # The complete source, old quote set and quarter-PDF identity are
        # checked before the single paid request as well as by the mapper.
        mapper._validate_source(report, packet, identity, code, current)
        mapper._existing_map(pool, packet)
        prior_count = len(usage_history)

        def record(receipt, inference=None):
            if receipt['outcome'] == 'started' and any(row['requestDigest'] == receipt['requestDigest']
                    and row['outcome'] == 'validated' for row in usage_history):
                raise _AttemptAlreadyRecorded()
            row = {key: receipt[key] for key in ('requestDigest', 'inputDigest', 'attemptedAt', 'finishedAt', 'outcome')}
            row.update(responseDigest=inference.get('responseDigest') if inference else None,
                       usage=copy.deepcopy(inference.get('usage')) if inference else None)
            result['usageHistory'] = [*copy.deepcopy(usage_history), row]
            result['usageHistoryDigest'] = mapper.canonical_digest(result['usageHistory'])
            result.update(modelRequests=1, requestReceipt=copy.deepcopy(receipt), modelStatus=receipt['outcome'])
            emit()

        def validated(bound, inference):
            record(inference['receipt'], inference)
            observation = result['usageHistory'][-1]
            model = {'provider': inference['provider'], 'model': inference['requestedModel'],
                     'responseModel': inference['responseModel'], 'promptVersion': inference['promptVersion'],
                     'boundResponseDigest': inference['bindingDiagnostics']['boundResponseDigest'],
                     'observation': copy.deepcopy(observation)}
            candidate = mapper.map_strategy_semantic_pool(report, packet, bound, code=code,
                source_identity=identity, model_observation=model,
                usage_history=result['usageHistory'], expected_baseline_digest=expected_baseline_digest,
                expected_usage_history_digest=result['usageHistoryDigest'], now=clock())
            result['candidate'] = candidate

        inference = run_packet(packet, credential, checkpoint=record, transport=transport, on_validated=validated)
        # Rejections also have actual usage and response digests. The private
        # callback is only for validated output; final accounting covers all.
        if len(result['usageHistory']) != prior_count + 1:
            return finish('rejected', 'MODEL_RECEIPT_MISSING')
        record(inference['receipt'], inference)
        if result['candidate'] is not None:
            return finish('candidate-ready')
        return finish(inference['status'], inference['errorCode'] or 'SEMANTIC_CANDIDATE_UNAVAILABLE')
    except SemanticCheckpointError:
        raise
    except _AttemptAlreadyRecorded:
        return finish('already-observed', 'MODEL_VALIDATED_ATTEMPT_EXISTS')
    except mapper.MappingError as error:
        return finish('rejected', str(error))
    except Exception:
        # Exception text can contain a private quote or provider body.
        return finish('rejected', 'SEMANTIC_ADAPTER_FAILED')


__all__ = ['prepare_candidate', 'SemanticCheckpointError']
