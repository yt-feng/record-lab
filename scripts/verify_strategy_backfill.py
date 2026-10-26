"""Actions-only, two-request backfill verification against immutable old data.

Only compact checks are persisted. Original data, PDFs, extracted text, and
the candidate patch remain read-only or in memory; no production write occurs.
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import time
from unittest.mock import patch

from collect_strategy_claims import collect
from providers import PublicProvider
from publication_guard import inspect_content
from source_dates import china_today
from strategy_claim_patches import ERRORS as PATCH_ERRORS, binding_for
from strategy_claims import _instant

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / '.cache/strategy-backfill-verification/result.json'
PERIODS = {'2026Q2': ('2026-06-30', '2026-03-31'), '2026Q3': ('2026-09-30', '2026-06-30')}
MAX_PARTS = 12
MAX_READ_BYTES = 96_000_000
MAX_PART_BYTES = 8_000_000
MAX_CATALOG_BYTES = 25_000_000
MAX_OUTPUT_BYTES = 32_000
MAX_BRIDGE_BYTES = 9_000_000
ERRORS = PATCH_ERRORS | {'ACTIONS_REQUIRED', 'INVALID_DATA_COMMIT', 'DATA_READ_FAILED', 'DATA_READ_BOUND',
    'INVALID_DATA_MANIFEST', 'SAMPLE_NOT_FOUND_WITHIN_BOUND', 'BRIDGE_FAILED', 'VERIFICATION_FAILED',
    'PUBLICATION_REJECTED', 'UNSUPPORTED_OUTPUT', 'DATA_CHANGED', 'DIAGNOSTIC_BINDING_INVALID', 'CATALOG_CONTEXT_INVALID'}
DIAGNOSTIC_COMMIT = '63ab7e38ce388f246d80588146081991a88ff623'
DIAGNOSTIC_SAMPLES = {
    '000189': {'quarter': '2026Q3', 'dataPath': 'web/data/periods/2026Q3/companies/80000229-r721f3cf9b7ff16cc13d3.json',
               'dataBlobSha': '528c5ab14093590efd50e8ee1a8f1fd2ec3946a1',
               'beforeHash': '9a8bb978e2d9f6173533f7320eb7947e1c71075ca2fac6468ddf57aa026cdf05',
               'observedAfterHash': 'a551017b9b92d1d637557b50c28197bfacf4387b353dc70b03638effa7ce3b6f'},
    '002020': {'quarter': '2026Q2', 'dataPath': 'web/data/periods/2026Q2/companies/80000095-rbbbec8c43b4ba8926643.json',
               'dataBlobSha': '845039b2440e4e8b056ef091d0a992b9ba50a373',
               'beforeHash': 'f377757145d6acc6cf2e73b699a5edb259065ff07a328db681a0cc1d3f023a4a',
               'observedAfterHash': 'f377757145d6acc6cf2e73b699a5edb259065ff07a328db681a0cc1d3f023a4a'},
}
DIFF_ROOT_FIELDS = set('code name company companyId companyMapping sourcePortfolioCode portfolioSourceUrl managers reportManagers managerAttribution managerTableCoverage managerCoverageStatus managersAsOf managerScope managerSourceUrl managerEvidenceRetrievedAt metadataSourceUrl inceptionDate inceptionSourceUrl inceptionEvidence inceptionProfileEvidence inceptionConflict shareClassCodes asOf status selectedReport previousReport reports interval rows summary warnings retrievedAt dataStatus cacheReason priceUpdatedAt aliases noEquitiesEvidence notApplicableEvidence baselineExemption portfolioDeduplicated portfolioRepresentativeCode'.split())
DIFF_REPORT_FIELDS = set('periodEnd publishedAt kind portfolioIdentityEvidence sourceLocatorEvidence coverage navYuan reportOnly evidenceRetrievedAt navRetrievedAt reportPdfSourceUrl reportPdfPeriodEnd reportPdfKind sourceUrl fundCode sourceFundCode holdingsSourceUrl holdingsRetrieval holdingsRequestedLimit holdingsPdfEvidence holdingsPossiblyTruncated holdingsIncomplete holdingsExcludedRowCount holdingsIncompleteReason navSourceUrl managers managerTableCoverage managerSourceUrl managerEvidenceRetrievedAt noEquitiesConfirmed noEquitiesEvidence holdings parseMethod pageStats sourceTextHash strategySectionCoverage strategyEvidenceRetrievedAt'.split())
DIFF_TYPES = {'missing', 'null', 'object', 'array', 'string', 'boolean', 'number'}
CHECKS = ('freshFundCountZero', 'singleReceipt', 'patchAdmitted', 'mergeAccepted', 'positionsUnchanged',
          'navUnchanged', 'managersUnchanged', 'oldTimesUnchanged', 'reportSourcesUnchanged',
          'newOwnClock', 'derivedOriginalClock', 'sameAnalysisDigest', 'sameSectionProof',
          'completeFiniteScan', 'semanticLimited', 'onePortfolio', 'nonStrategyFieldsUnchanged')

NODE_BRIDGE = r"""const fs=require('node:fs'),crypto=require('node:crypto');
try {
 const x=JSON.parse(fs.readFileSync(0,'utf8')), p=require('./lib/strategy-claim-patches');
 if(x.mode==='select') {
  let index=-1;
  for(let i=0;i<x.funds.length;i++) {
   const f=x.funds[i]; if(!['ok','no-equities'].includes(f.status)) continue;
   if(p.taskFor(f,x.phase,x.now)) {index=i;break;}
  }
  process.stdout.write(JSON.stringify({index}));
 } else {
  const {buildSnapshot}=require('./scripts/build-data'),{mergeSnapshots}=require('./scripts/merge-parts');
  const {strategyClaimsFor}=require('./lib/strategy-claims');
  const canonical=p.canonical, same=(a,b)=>canonical(a)===canonical(b), clone=v=>JSON.parse(JSON.stringify(v));
  const own=(o,keys)=>Object.fromEntries(keys.filter(k=>Object.hasOwn(o||{},k)).map(k=>[k,o[k]]));
  const reports=f=>[f.selectedReport,f.previousReport,...(f.reports||[])].filter(Boolean);
  const positions=f=>({rows:f.rows,reports:reports(f).map(r=>own(r,['periodEnd','sourceUrl','holdings']))});
  const matched=(o,re)=>Object.fromEntries(Object.entries(o||{}).filter(([k])=>re.test(k)));
  const nav=f=>({fund:matched(f,/nav|assets|scale/i),reports:reports(f).map(r=>matched(r,/nav|assets|scale/i))});
  const managers=f=>({fund:matched(f,/manager/i),reports:reports(f).map(r=>matched(r,/manager/i))});
  const sources=f=>reports(f).map(r=>own(r,['sourceUrl','publishedAt','periodEnd','kind','holdingsSourceUrl','reportPdfSourceUrl','portfolioIdentityEvidence']));
  const mutable=new Set(['strategyClaims','strategyClaimsIdentityEvidence','strategyClaimsDerivedFromObservedAt',
    'strategyEvidencePool','strategyEvidenceBudget','strategyClaimsStatus','strategyAnalysis','strategyThemes','strategyExcerpts','narrative']);
  const protectedFields=value=>Array.isArray(value)?value.map(protectedFields):value&&typeof value==='object'?
   Object.fromEntries(Object.entries(value).filter(([k])=>!mutable.has(k)).map(([k,v])=>[k,protectedFields(v)])):value;
  const ROOT_FIELDS=new Set(__DIFF_ROOT_FIELDS__), REPORT_FIELDS=new Set(__DIFF_REPORT_FIELDS__);
  function differences(before,after) {
   const items=[];let mapped=0,unmapped=0;
   const type=(value,present)=>!present?'missing':value===null?'null':Array.isArray(value)?'array':typeof value;
   const add=(a,b,p,aPresent,bPresent)=>{mapped++;if(items.length<32)items.push({path:p,
    beforeType:type(a,aPresent),afterType:type(b,bPresent),change:!aPresent?'added':!bPresent?'removed':'changed'});};
   function walk(a,b,p,allowed) {
    for(const key of [...new Set([...Object.keys(a||{}),...Object.keys(b||{})])].sort()) {
     if(mutable.has(key))continue;
     const ap=Object.hasOwn(a||{},key),bp=Object.hasOwn(b||{},key),av=a?.[key],bv=b?.[key];
     if(ap===bp&&same(av,bv))continue;
     if(!allowed.has(key)){unmapped++;continue;}
     const field=p+'.'+key;
     if(p==='fund'&&['selectedReport','previousReport'].includes(key)&&av&&bv&&typeof av==='object'&&typeof bv==='object')walk(av,bv,field,REPORT_FIELDS);
     else if(p==='fund' && key==='reports' && Array.isArray(av)&&Array.isArray(bv)&&av.length===bv.length&&av.length<=32)
      av.forEach((v,i)=>walk(v,bv[i],field+'['+i+']',REPORT_FIELDS));
     else add(av,bv,field,ap,bp);
    }
   }
   walk(protectedFields(before),protectedFields(after),'fund',ROOT_FIELDS);
   return {differences:items,differenceCount:mapped+unmapped,reportedDifferenceCount:items.length,unmappedDifferenceCount:unmapped,truncated:mapped>items.length};
  }
  function clocks(value,path='',out={}) {
   if(Array.isArray(value)) value.forEach((v,i)=>clocks(v,path+'/'+i,out));
   else if(value&&typeof value==='object') for(const [k,v] of Object.entries(value)) {
    if(mutable.has(k))continue;
    if((/(?:At|AsOf)$/.test(k)||k==='asOf')&&(v===null||typeof v==='string'))out[path+'/'+k]=v;
    else clocks(v,path+'/'+k,out);
   }
   return out;
  }
  const before=clone(x.fund),company=before.companyId;
  // The legacy sample-only context remains solely for zero-request fault
  // reproduction. Verification always supplies the immutable directory rows.
  const catalog=x.catalog||(x.mode==='normalize-only'?{fundCount:1,companyCount:1,
    companies:[{code:company,name:before.company,fundCount:1}],
    funds:[{code:before.code,companyCode:company,companyName:before.company}]}:null);
  if(!catalog)throw new Error('catalog unavailable');
  if(x.mode==='normalize-only') {
   // No patch, new source observation, or file publication. Omitting the
   // previous manifest only exposes the engine's normalized in-memory output.
   const result=mergeSnapshots({snapshots:[],previousCompanies:{sample:{...x.envelope,funds:[clone(before)]}},
    previousManifest:null,config:x.config,catalog,now:x.now,reusePrices:false});
   const funds=Object.values(result.companySnapshots).flatMap(s=>s.funds),after=funds.find(f=>f.code===before.code);
   if(funds.length!==1||!after)throw new Error('control unavailable');
   process.stdout.write(JSON.stringify({normalizedFundCount:funds.length,
    nonStrategyFieldsUnchanged:same(protectedFields(before),protectedFields(after)),
    protectedBeforeHash:crypto.createHash('sha256').update(canonical(protectedFields(before))).digest('hex'),
    normalizedHash:crypto.createHash('sha256').update(canonical(protectedFields(after))).digest('hex'),
    ...differences(before,after)}));
  } else {
  const part=buildSnapshot(x.raw,{config:x.config,now:x.now});
  const previous={...x.envelope,funds:[clone(before)]};
  const result=mergeSnapshots({snapshots:[part],previousCompanies:{sample:previous},previousManifest:x.manifest,
    config:x.config,catalog,now:x.now,reusePrices:false});
  const output=Object.values(result.companySnapshots).flatMap(s=>s.funds);
  const after=result.changed?output.find(f=>f.code===before.code):before;
  const a=after?.selectedReport,c=a&&strategyClaimsFor(a,{code:before.code,now:x.now,asOf:after.asOf});
  const old=x.config.funds[0].strategyClaimsOnly.retainedStrategyObservedAt;
  const receipt=part.strategyClaimsReceipts[0],r=result.receipt.strategyClaims.find(r=>r.code===before.code);
  const parsed=p.instant(c?.source?.retrievedAt),attempted=p.instant(receipt?.attemptedAt),received=p.instant(x.raw.retrievedAt);
  const checks={freshFundCountZero:part.coverage.batchFreshFundCount===0&&result.counts.acceptedFreshFundCount===0,
   singleReceipt:part.strategyClaimsReceipts.length===1,patchAdmitted:part.strategyClaimsPatches.length===1,
   mergeAccepted:r?.accepted===true&&r.scanned===true,
   positionsUnchanged:!!after&&same(positions(before),positions(after)),navUnchanged:!!after&&same(nav(before),nav(after)),
   managersUnchanged:!!after&&same(managers(before),managers(after)),oldTimesUnchanged:!!after&&same(clocks(before),clocks(after)),
   reportSourcesUnchanged:!!after&&same(sources(before),sources(after)),
   newOwnClock:!!c&&parsed!==null&&attempted!==null&&received!==null&&parsed>=attempted&&parsed<=received&&parsed>=p.instant(old)
    &&c.source.retrievedAt===a.strategyEvidencePool?.source.retrievedAt&&c.source.retrievedAt===a.strategyClaimsIdentityEvidence?.retrievedAt,
   derivedOriginalClock:a?.strategyEvidenceRetrievedAt===old&&a.strategyClaimsDerivedFromObservedAt===old,
   sameAnalysisDigest:c?.source.digest===x.config.funds[0].strategyClaimsOnly.analysisDigest,
   sameSectionProof:!!a&&same(before.selectedReport.strategySectionCoverage,a.strategySectionCoverage),
   completeFiniteScan:c?.ruleScanComplete===true,semanticLimited:c?.semanticAnalysisComplete===false,
   onePortfolio:!result.changed||output.length===1,
   nonStrategyFieldsUnchanged:!!after&&same(protectedFields(before),protectedFields(after))};
  process.stdout.write(JSON.stringify({checks,claimCount:c?.claims.length??0,evidenceCount:c?.evidence.length??0,
   scannedClauseCount:c?.coverage.scannedClauseCount??0,classifiedClauseCount:c?.coverage.classifiedClauseCount??0,
   unknownClauseCount:c?.coverage.unknownClauseCount??0,
   quoteChars:a?.strategyEvidencePool?.usedQuoteChars??0,newStrategyObservedAt:c?.source.retrievedAt??null,
   protectedBeforeHash:crypto.createHash('sha256').update(canonical(protectedFields(before))).digest('hex'),
   protectedAfterHash:after?crypto.createHash('sha256').update(canonical(protectedFields(after))).digest('hex'):null,
   ...differences(before,after||{})}));
  }
 }
}catch(_){process.stdout.write('{}');process.exitCode=1;}
""".replace('__DIFF_ROOT_FIELDS__', json.dumps(sorted(DIFF_ROOT_FIELDS))).replace('__DIFF_REPORT_FIELDS__', json.dumps(sorted(DIFF_REPORT_FIELDS)))


class VerificationError(ValueError):
    def __init__(self, code):
        self.code = code if code in ERRORS else 'VERIFICATION_FAILED'
        super().__init__(self.code)


def bridge(value):
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    if len(encoded.encode()) > MAX_BRIDGE_BYTES: raise VerificationError('BRIDGE_FAILED')
    try:
        result = subprocess.run(['node', '-e', NODE_BRIDGE], input=encoded, text=True, cwd=ROOT,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=45)
        if result.returncode or len(result.stdout.encode()) > 12000: raise VerificationError('BRIDGE_FAILED')
        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise VerificationError('BRIDGE_FAILED') from None


class GitDataReader:
    """Only regular, size-bounded blobs under this exact immutable data commit."""
    def __init__(self, root, commit):
        if not re.fullmatch(r'[a-f0-9]{40}', commit or ''): raise VerificationError('INVALID_DATA_COMMIT')
        self.root, self.commit, self.bytes_read, self.last_blob = Path(root), commit, 0, None
        if self.command('rev-parse', 'HEAD').decode().strip() != commit:
            raise VerificationError('INVALID_DATA_COMMIT')
        if self.command('status', '--porcelain', '--untracked-files=no'):
            raise VerificationError('DATA_CHANGED')
    def command(self, *args):
        try:
            return subprocess.run(['git', '-C', str(self.root), *args], stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=45, check=True).stdout
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            raise VerificationError('DATA_READ_FAILED') from None
    def read(self, path, maximum=MAX_PART_BYTES):
        if not re.fullmatch(r'web/data/periods/2026Q[23]/(?:latest|catalog|companies/[A-Za-z0-9_-]{1,64})\.json', path):
            raise VerificationError('DATA_READ_FAILED')
        line = self.command('ls-tree', self.commit, '--', path).decode().strip()
        match = re.fullmatch(r'100644 blob ([a-f0-9]{40})\t' + re.escape(path), line)
        if not match: raise VerificationError('DATA_READ_FAILED')
        oid = match.group(1)
        size = int(self.command('cat-file', '-s', oid).strip())
        if size > maximum or self.bytes_read + size > MAX_READ_BYTES: raise VerificationError('DATA_READ_BOUND')
        raw = self.command('cat-file', 'blob', oid)
        if len(raw) != size: raise VerificationError('DATA_READ_FAILED')
        self.bytes_read += size; self.last_blob = oid
        try: return json.loads(raw)
        except (ValueError, UnicodeError): raise VerificationError('DATA_READ_FAILED') from None
    def unchanged(self):
        return (self.command('rev-parse', 'HEAD').decode().strip() == self.commit
                and not self.command('status', '--porcelain', '--untracked-files=no'))


def catalog_context(reader, quarter, fund):
    """Look up aliases in the original directory; never infer their owners."""
    end = PERIODS[quarter][0]
    codes = fund.get('shareClassCodes', [])
    if not isinstance(codes, list) or len(codes) > 64:
        raise VerificationError('CATALOG_CONTEXT_INVALID')
    codes = [fund.get('code'), *codes]
    if any(not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}', code) for code in codes):
        raise VerificationError('CATALOG_CONTEXT_INVALID')
    wanted = set(codes)
    path = f'web/data/periods/{quarter}/catalog.json'
    source = reader.read(path, maximum=MAX_CATALOG_BYTES); blob = reader.last_blob
    if (not isinstance(source, dict) or type(source.get('schemaVersion')) is not int or source['schemaVersion'] != 1 or source.get('targetPeriodEnd') != end
            or source.get('quarter', quarter) != quarter or not isinstance(source.get('funds'), list)
            or len(source['funds']) > 150000 or not isinstance(source.get('companies'), list)
            or len(source['companies']) > 5000 or not isinstance(blob, str) or not re.fullmatch(r'[a-f0-9]{40}', blob)):
        raise VerificationError('CATALOG_CONTEXT_INVALID')
    rows = [row for row in source['funds'] if isinstance(row, dict) and isinstance(row.get('code'), str) and row['code'] in wanted]
    if len(rows) != len(wanted) or {row['code'] for row in rows} != wanted:
        raise VerificationError('CATALOG_CONTEXT_INVALID')
    owners = set()
    for row in rows:
        values = [row.get(key) for key in ('companyId', 'companyCode') if row.get(key)]
        if (not values or any(not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', value)
                              or value == 'unknown' for value in values) or len(set(values)) != 1):
            raise VerificationError('CATALOG_CONTEXT_INVALID')
        owners.add(values[0])
    if len(owners) != 1 or next(iter(owners)) != fund.get('companyId'):
        raise VerificationError('CATALOG_CONTEXT_INVALID')
    owner = next(iter(owners))
    companies = []
    for row in source['companies']:
        if not isinstance(row, dict): continue
        identifiers = [row[key] for key in ('id', 'companyId', 'companyCode', 'code') if row.get(key)]
        if owner in identifiers:
            if any(value != owner for value in identifiers): raise VerificationError('CATALOG_CONTEXT_INVALID')
            companies.append({key: row[key] for key in ('id', 'companyId', 'companyCode', 'code', 'name', 'companyName', 'company') if key in row})
    if len(companies) != 1: raise VerificationError('CATALOG_CONTEXT_INVALID')
    for row in [*rows, *companies]:
        for key in ('company', 'companyName', 'name'):
            if key in row and row[key] is not None and (not isinstance(row[key], str) or len(row[key]) > 200
                                                       or re.search(r'[\x00-\x1f\x7f]', row[key])):
                raise VerificationError('CATALOG_CONTEXT_INVALID')
    # This is a read-only subset of original directory facts. Counts describe
    # the verification context, not the industry universe or original company.
    catalog = {'schemaVersion': 1, 'targetPeriodEnd': end, 'fundCount': len(rows), 'companyCount': 1,
               'companies': companies, 'funds': [{key: row[key] for key in ('code', 'companyId', 'companyCode', 'company', 'companyName')
                                                 if key in row} for row in rows]}
    evidence = {'dataCommit': reader.commit, 'path': path, 'blobSha': blob, 'targetPeriodEnd': end,
                'requiredCodeCount': len(wanted), 'matchedCodeCount': len(rows), 'companyCount': 1,
                'checks': {'sameImmutableCommit': True, 'targetQuarterMatches': True, 'allRequiredCodesUnique': True,
                           'allCompanyMappingsAgree': True, 'retainedCompanyMatches': True}}
    return catalog, evidence


def select_sample(reader, quarter, now):
    end, baseline = PERIODS[quarter]
    phase = {'targetPeriodEnd': end, 'baselinePeriodEnd': baseline}
    prefix = 'web/data/periods/' + quarter + '/'
    manifest = reader.read(prefix + 'latest.json')
    if (not isinstance(manifest, dict) or manifest.get('sourceMode') != 'actions'
            or manifest.get('targetPeriodEnd') != end or not isinstance(manifest.get('companyFiles'), list)):
        raise VerificationError('INVALID_DATA_MANIFEST')
    seen = set()
    for row in manifest['companyFiles'][:MAX_PARTS]:
        match = re.fullmatch(r'\./data/companies/([A-Za-z0-9_-]{1,64})\.json', str(row.get('path', ''))) if isinstance(row, dict) else None
        if not match or match[1] in seen: raise VerificationError('INVALID_DATA_MANIFEST')
        seen.add(match[1]); path = prefix + 'companies/' + match[1] + '.json'
        envelope = reader.read(path)
        if (not isinstance(envelope, dict) or envelope.get('sourceMode') != 'actions' or envelope.get('targetPeriodEnd') != end
                or not isinstance(envelope.get('funds'), list) or len(envelope['funds']) > 100):
            raise VerificationError('INVALID_DATA_MANIFEST')
        selection = bridge({'mode': 'select', 'funds': envelope['funds'], 'phase': phase, 'now': now})
        index = selection.get('index') if isinstance(selection, dict) else None
        if not isinstance(selection, dict) or set(selection) != {'index'} or type(index) is not int or not -1 <= index < len(envelope['funds']):
            raise VerificationError('BRIDGE_FAILED')
        if index < 0: continue
        fund = envelope['funds'][index]
        task = binding_for(fund, phase, now)
        if not task: raise VerificationError('BRIDGE_FAILED')
        data_blob = reader.last_blob
        catalog, catalog_evidence = catalog_context(reader, quarter, fund)
        # Preserve the actual observation envelope, but carry only this one
        # sample portfolio through the independent in-memory merge.
        small = {key: value for key, value in envelope.items() if key not in ('funds', 'errors', 'coverage')}
        # The bridge receives the sample separately and restores this list in
        # memory, avoiding a second serialized copy of a large portfolio.
        small['funds'] = []
        return {'quarter': quarter, 'phase': phase, 'manifest': manifest, 'envelope': small,
                'fund': fund, 'task': task, 'dataPath': path, 'dataBlobSha': data_blob,
                'catalog': catalog, 'catalogContext': catalog_evidence,
                'partsRead': len(seen)}
    raise VerificationError('SAMPLE_NOT_FOUND_WITHIN_BOUND')


def compact_receipt(raw):
    rows = raw.get('strategyClaimsReceipts', [])
    return dict(rows[0]) if len(rows) == 1 else None


DIFF_FIELDS = {'differences', 'differenceCount', 'reportedDifferenceCount', 'unmappedDifferenceCount', 'truncated'}


def valid_differences(value):
    rows = value.get('differences')
    if (not isinstance(rows, list) or len(rows) > 32 or type(value.get('truncated')) is not bool
            or any(type(value.get(key)) is not int or not 0 <= value[key] <= 10000
                   for key in ('differenceCount', 'reportedDifferenceCount', 'unmappedDifferenceCount'))
            or value['reportedDifferenceCount'] != len(rows)
            or value['differenceCount'] < len(rows) + value['unmappedDifferenceCount']
            or value['truncated'] != (value['differenceCount'] - value['unmappedDifferenceCount'] > len(rows))):
        return False
    paths = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'path', 'beforeType', 'afterType', 'change'}: return False
        field = row['path']
        if not isinstance(field, str) or field in paths: return False
        paths.add(field)
        root = re.fullmatch(r'fund\.([A-Za-z]+)', field)
        report = re.fullmatch(r'fund\.(?:selectedReport|previousReport|reports\[(?:[0-9]|[12][0-9]|3[01])\])\.([A-Za-z]+)', field)
        if not (root and root[1] in DIFF_ROOT_FIELDS or report and report[1] in DIFF_REPORT_FIELDS): return False
        if any(not isinstance(row[key], str) or row[key] not in DIFF_TYPES for key in ('beforeType', 'afterType')): return False
        if row['change'] not in ('added', 'removed', 'changed'): return False
        if (row['change'] == 'added') != (row['beforeType'] == 'missing'): return False
        if (row['change'] == 'removed') != (row['afterType'] == 'missing'): return False
    return True


def run_sample(sample, checkpoint=None, provider_factory=PublicProvider):
    started = time.monotonic()
    task, phase = sample['task'], sample['phase']
    config = {**phase, 'funds': [{'code': task['code'], 'strategyClaimsOnly': task}], 'maxWorkers': 1,
              'maxStrategyClaimsSeconds': 240, 'traversal': {'plannedAt': dt.datetime.now(dt.timezone.utc).isoformat(),
                                                           'strategyClaimsCodes': [task['code']]}}
    result = {'quarter': sample['quarter'], 'code': task['code'], 'status': 'partial', 'errorCode': None,
              'sourceUrl': task['source']['sourceUrl'], 'sourceTextHash': task['source']['sourceTextHash'],
              'sectionContentHash': task['source']['sectionContentHash'], 'pages': task['source']['pages'],
              'analysisDigest': task['analysisDigest'], 'taskDigest': task['taskDigest'],
              'retainedStrategyObservedAt': task['retainedStrategyObservedAt'],
              'dataPath': sample['dataPath'], 'dataBlobSha': sample['dataBlobSha'],
              'catalogContext': sample['catalogContext'],
              'partsRead': sample['partsRead'], 'receipt': None, 'checks': {}, 'elapsedSeconds': 0}
    def record(raw):
        result['receipt'] = compact_receipt(raw)
        result['elapsedSeconds'] = round(time.monotonic() - started, 3)
        if checkpoint: checkpoint(result)
    try:
        # The verifier never reads parser credentials or invokes another paid
        # service. The original PDF is parsed locally on this Actions runner.
        with patch('report_extract.mineru_tokens_from_env', return_value=[]):
            raw = collect(config, china_today(), record, provider_factory)
        checked = bridge({'mode': 'merge', 'fund': sample['fund'], 'envelope': sample['envelope'],
                          'manifest': sample['manifest'], 'catalog': sample['catalog'],
                          'config': config, 'raw': raw, 'now': raw['retrievedAt']})
        if (not isinstance(checked, dict) or set(checked) != {'checks', 'claimCount', 'evidenceCount', 'quoteChars',
                    'scannedClauseCount', 'classifiedClauseCount', 'unknownClauseCount', 'newStrategyObservedAt',
                    'protectedBeforeHash', 'protectedAfterHash'} | DIFF_FIELDS or not valid_differences(checked)
                or set(checked.get('checks', {})) != set(CHECKS)
                or any(type(value) is not bool for value in checked['checks'].values())
                or any(type(checked.get(key)) is not int or not 0 <= checked[key] <= limit
                       for key, limit in (('claimCount', 24), ('evidenceCount', 8), ('quoteChars', 1280),
                                          ('scannedClauseCount', 1000000), ('classifiedClauseCount', 1000000),
                                          ('unknownClauseCount', 1000000)))
                or any(value is not None and (not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value))
                       for value in (checked.get('protectedBeforeHash'), checked.get('protectedAfterHash')))
                or checked.get('newStrategyObservedAt') is not None and not _instant(checked['newStrategyObservedAt'])):
            raise VerificationError('BRIDGE_FAILED')
        result.update(checked)
        result['status'] = 'verified' if all(checked['checks'].values()) else 'partial'
        if result['status'] != 'verified':
            result['errorCode'] = (result['receipt'] or {}).get('errorCode') or 'VERIFICATION_FAILED'
    except Exception as error:
        result['errorCode'] = getattr(error, 'code', 'VERIFICATION_FAILED')
        if result['errorCode'] not in ERRORS: result['errorCode'] = 'VERIFICATION_FAILED'
    result['elapsedSeconds'] = round(time.monotonic() - started, 3)
    if checkpoint: checkpoint(result)
    return result


def save_result(output, value):
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode()
    if len(data) > MAX_OUTPUT_BYTES or inspect_content(data): raise VerificationError('PUBLICATION_REJECTED')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix('.tmp'); temporary.write_bytes(data); temporary.replace(output)


def verify(reader, output=OUTPUT, provider_factory=PublicProvider):
    value = {'schemaVersion': 1, 'status': 'partial', 'dataCommit': reader.commit,
             'scope': 'one-existing-portfolio-per-quarter-within-bounded-search', 'maxOriginalPdfRequests': 2,
             'maxPartsPerQuarter': MAX_PARTS, 'samples': [], 'productionDataModified': False,
             'semanticAnalysisComplete': False, 'originalPdfRequestsStarted': 0}
    save_result(output, value)
    for quarter in PERIODS:
        try:
            sample = select_sample(reader, quarter, dt.datetime.now(dt.timezone.utc).isoformat())
            index = len(value['samples']); value['samples'].append({})
            def checkpoint(row):
                value['samples'][index] = row
                value['originalPdfRequestsStarted'] = sum(bool(row.get('receipt')) for row in value['samples'])
                save_result(output, value)
            run_sample(sample, checkpoint, provider_factory)
        except VerificationError as error:
            value['samples'].append({'quarter': quarter, 'status': 'partial', 'errorCode': error.code})
        save_result(output, value)
    if not reader.unchanged():
        value['errorCode'] = 'DATA_CHANGED'; value['productionDataModified'] = True
    value['originalPdfRequestsStarted'] = sum(bool(row.get('receipt')) for row in value['samples'])
    value['status'] = 'verified' if (len(value['samples']) == 2 and all(row['status'] == 'verified' for row in value['samples'])
                                    and not value['productionDataModified']) else 'partial'
    save_result(output, value)
    return value


def diagnose_normalization(reader, code, output=OUTPUT):
    """Exact old artifact sample, no provider construction or source requests."""
    expected = DIAGNOSTIC_SAMPLES.get(code)
    if reader.commit != DIAGNOSTIC_COMMIT or not expected:
        raise VerificationError('DIAGNOSTIC_BINDING_INVALID')
    quarter = expected['quarter']; end, baseline = PERIODS[quarter]
    manifest = reader.read('web/data/periods/' + quarter + '/latest.json')
    reference = './data/companies/' + expected['dataPath'].rsplit('/', 1)[1]
    if (not isinstance(manifest, dict) or manifest.get('targetPeriodEnd') != end or manifest.get('sourceMode') != 'actions'
            or sum(isinstance(item, dict) and item.get('path') == reference for item in manifest.get('companyFiles', [])) != 1):
        raise VerificationError('DIAGNOSTIC_BINDING_INVALID')
    envelope = reader.read(expected['dataPath'])
    if reader.last_blob != expected['dataBlobSha'] or not isinstance(envelope, dict):
        raise VerificationError('DIAGNOSTIC_BINDING_INVALID')
    funds = [fund for fund in envelope.get('funds', []) if isinstance(fund, dict) and fund.get('code') == code]
    if len(funds) != 1 or envelope.get('targetPeriodEnd') != end or envelope.get('sourceMode') != 'actions':
        raise VerificationError('DIAGNOSTIC_BINDING_INVALID')
    phase = {'targetPeriodEnd': end, 'baselinePeriodEnd': baseline}
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    if not binding_for(funds[0], phase, now): raise VerificationError('DIAGNOSTIC_BINDING_INVALID')
    small = {key: value for key, value in envelope.items() if key not in ('funds', 'coverage', 'errors')}
    control = bridge({'mode': 'normalize-only', 'fund': funds[0], 'envelope': small, 'config': phase, 'now': now})
    if (not isinstance(control, dict) or set(control) != {'normalizedFundCount', 'nonStrategyFieldsUnchanged',
                'protectedBeforeHash', 'normalizedHash'} | DIFF_FIELDS or type(control.get('normalizedFundCount')) is not int or control['normalizedFundCount'] != 1
            or type(control.get('nonStrategyFieldsUnchanged')) is not bool or not valid_differences(control)
            or any(not isinstance(control.get(key), str) or not re.fullmatch(r'[a-f0-9]{64}', control[key])
                   for key in ('protectedBeforeHash', 'normalizedHash'))):
        raise VerificationError('BRIDGE_FAILED')
    unchanged = reader.unchanged()
    bound = control['protectedBeforeHash'] == expected['beforeHash']
    value = {'schemaVersion': 1, 'mode': 'diagnose-normalization', 'status': 'diagnosed' if bound and unchanged else 'partial',
             'dataCommit': reader.commit, 'quarter': quarter, 'code': code, 'dataPath': expected['dataPath'],
             'dataBlobSha': reader.last_blob, 'maxOriginalPdfRequests': 0, 'originalPdfRequestsStarted': 0,
             'productionDataModified': not unchanged, 'semanticAnalysisComplete': False,
             'control': {'scope': 'empty-patch-in-memory-normalization-sample-only-catalog', **control,
                         'matchesPreviouslyObservedBefore': bound,
                         'matchesPreviouslyObservedAfter': control['normalizedHash'] == expected['observedAfterHash']}}
    save_result(output, value)
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-repo', required=True)
    parser.add_argument('--data-commit', required=True)
    parser.add_argument('--mode', choices=('verify', 'diagnose-normalization'), default='verify')
    parser.add_argument('--sample-code', choices=tuple(DIAGNOSTIC_SAMPLES), default='000189')
    args = parser.parse_args()
    if os.environ.get('GITHUB_ACTIONS') != 'true': raise VerificationError('ACTIONS_REQUIRED')
    reader = GitDataReader(args.data_repo, args.data_commit)
    value = diagnose_normalization(reader, args.sample_code) if args.mode == 'diagnose-normalization' else verify(reader)
    print(json.dumps({'status': value['status'], 'sampleCount': len(value.get('samples', [value])),
                      'originalPdfRequestsStarted': value['originalPdfRequestsStarted'], 'productionDataModified': value['productionDataModified']}))
    return 0 if value['status'] in ('verified', 'diagnosed') else 2


if __name__ == '__main__':
    try: raise SystemExit(main())
    except Exception as error:
        code = getattr(error, 'code', 'VERIFICATION_FAILED')
        code = code if code in ERRORS else 'VERIFICATION_FAILED'
        failure = {'status': 'partial', 'errorCode': code}
        if not OUTPUT.exists(): save_result(OUTPUT, failure)
        print(json.dumps(failure)); raise SystemExit(2) from None
