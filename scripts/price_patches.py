"""Small source-bound price repair plans; no report text is carried in a plan."""
import datetime as dt
import hashlib
import re
from urllib.parse import urlparse, parse_qsl


def stock_key(value):
    if not isinstance(value, dict) or value.get('market') not in ('CN', 'HK', 'US'):
        return None
    code = value.get('stockCode')
    pattern = {'US': r'[A-Z][A-Z0-9]{0,9}(?:\.[A-Z0-9]{1,3})?', 'CN': r'\d{6}', 'HK': r'\d{5}'}[value['market']]
    return value['market'] + ':' + code if isinstance(code, str) and re.fullmatch(pattern, code) else None


def public_url(value):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value): return False
    try:
        parsed = urlparse(value)
        parsed.port  # Reject malformed ports before any quote request is scheduled.
        host = (parsed.hostname or '').lower()
        return (parsed.scheme == 'https' and not parsed.username and not parsed.password and not parsed.fragment
                and bool(re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host)) and '.' in host
                and bool(re.search(r'\.[a-z][a-z0-9-]*$', host)) and not host.endswith(('.localhost', '.local', '.internal'))
                and not any(k.lower() in {'token', 'key', 'secret', 'password', 'signature', 'authorization', 'access_token', 'api_key'}
                            for k, _ in parse_qsl(parsed.query, keep_blank_values=True)))
    except ValueError:
        return False


def date(value):
    try:
        return dt.date.fromisoformat(value) if isinstance(value, str) and dt.date.fromisoformat(value).isoformat() == value else None
    except ValueError:
        return None


def report_identity(report, period):
    if (not isinstance(report, dict) or report.get('periodEnd') != period or not date(period)
            or not date(report.get('publishedAt')) or report['publishedAt'] < period or not public_url(report.get('sourceUrl'))):
        return None
    result = {key: report.get(key) for key in ('periodEnd', 'publishedAt', 'sourceUrl', 'holdingsSourceUrl', 'reportPdfSourceUrl')}
    if any(value is not None and not public_url(value) for key, value in result.items() if key.endswith('Url')):
        return None
    return result


def binding_for(fund, phase):
    if not isinstance(fund, dict) or fund.get('status') not in ('ok', 'no-equities'):
        return None
    code = fund.get('code')
    if not isinstance(code, str) or not re.fullmatch(r'\d{6}', code): return None
    current = fund.get('selectedReport') or {}
    selected = report_identity(current, phase.get('targetPeriodEnd'))
    if selected is None or not isinstance(current.get('holdings'), list): return None
    previous = fund.get('previousReport')
    baseline = report_identity(previous, phase.get('baselinePeriodEnd')) if previous else None
    if previous and baseline is None: return None
    holdings = current['holdings'] + (previous.get('holdings', []) if baseline else [])
    if not all(stock_key(row) for row in holdings): return None
    keys = sorted(set(stock_key(row) for row in holdings))
    if not keys or len(keys) > 20000: return None
    company = fund.get('companyId')
    result = {'schemaVersion': 1, 'code': code, 'companyCode': None if company == 'unknown' else company,
              'targetPeriodEnd': phase['targetPeriodEnd'], 'baselinePeriodEnd': phase['baselinePeriodEnd'],
              'selectedReport': selected, 'previousReport': baseline, 'stockKeys': keys,
              'holdingsIdentity': hashlib.sha256('|'.join(keys).encode()).hexdigest()}
    return result if valid_binding(result, phase, code) else None


def valid_binding(value, phase, code):
    if (not isinstance(value, dict) or value.get('schemaVersion') != 1 or value.get('code') != code
            or not isinstance(code, str) or not re.fullmatch(r'\d{6}', code)):
        return False
    if any(value.get(k) != phase.get(k) for k in ('targetPeriodEnd', 'baselinePeriodEnd')): return False
    target, baseline = date(phase.get('targetPeriodEnd')), date(phase.get('baselinePeriodEnd'))
    if not target or not baseline: return False
    start = dt.date(target.year, (target.month-1)//3*3+1, 1)
    if baseline != start - dt.timedelta(days=1): return False
    selected = report_identity(value.get('selectedReport'), target.isoformat())
    if selected is None or selected != value.get('selectedReport'): return False
    company = value.get('companyCode')
    if company is not None and (not isinstance(company, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', company)): return False
    prior = value.get('previousReport')
    if prior is not None and report_identity(prior, baseline.isoformat()) != prior: return False
    keys = value.get('stockKeys')
    if not isinstance(keys, list) or not 1 <= len(keys) <= 20000 or not all(isinstance(k, str) for k in keys) or keys != sorted(set(keys)): return False
    for key in keys:
        if not isinstance(key, str) or ':' not in key: return False
        market, symbol = key.split(':', 1)
        if stock_key({'market': market, 'stockCode': symbol}) != key: return False
    return value.get('holdingsIdentity') == hashlib.sha256('|'.join(keys).encode()).hexdigest()


def valid_target(item, phase):
    binding = item.get('priceOnly') if isinstance(item, dict) else None
    if not valid_binding(binding, phase, item.get('code')) or binding.get('companyCode') != (item.get('companyCode') or item.get('companyId')):
        return False
    targets = item.get('priceTargets')
    return bool(isinstance(targets, list) and targets and all(stock_key(row) in binding['stockKeys'] for row in targets)
                and len({stock_key(row) for row in targets}) == len(targets))


def status_for(item, patch, phase):
    binding = item.get('priceOnly')
    valid = valid_target(item, phase) and isinstance(patch, dict) and patch.get('binding') == binding
    keys = patch.get('attemptedStockKeys', []) if valid else []
    targets = {stock_key(row) for row in item.get('priceTargets', [])}
    valid_keys = isinstance(keys, list) and all(isinstance(k, str) and k in targets for k in keys) and len(keys) == len(set(keys))
    started = bool(valid_keys and keys and isinstance(patch.get('attemptedAt'), str))
    return {'code': item['code'], 'companyCode': item.get('companyCode') or item.get('companyId'),
            'status': 'failed', 'priceOnly': True, 'attempted': started, 'enrichmentAttempt': started,
            'qualityAttempt': False, 'managerAttempt': False, 'strategyAttempt': False,
            'priceBinding': binding if valid else None, 'attemptedStockKeys': keys if started else [],
            'priceAttemptedAt': patch.get('attemptedAt') if started else None,
            'errorCode': 'ENRICHMENT_FAILED' if started else 'PRICE_BUDGET_REACHED' if valid else 'PRICE_BINDING_INVALID'}
