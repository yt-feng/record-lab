const test = require('node:test');
const assert = require('node:assert/strict');
const ui = require('../web/analysis');
const { buildSnapshot } = require('../scripts/build-data');
const { analyzeQuarter } = require('../scripts/analyze-data');
const stock = (code = '600001', value = 20) => ({key:`CN:${code}`, stockCode:code, stockName:'合成证券', market:'CN',
  currentPortfolioCount:2, identityUnresolvedCount:1, disclosedValueYuan:value, navWeightEstimateYuan:null,
  changes:{increase:1, decrease:0}, details:['./analysis/details-0000.json']});
const fixture = () => ({schemaVersion:1,kind:'quarter-analysis',targetPeriodEnd:'2026-09-30',baselinePeriodEnd:'2026-06-30',
  generation:'2026-10-26T12:00:00.000Z',coverage:{status:'partial'},stocks:[stock()],themes:[],portfolioFiles:[{path:'./analysis/details-portfolios-0000.json',ids:['portfolio:000001']}]});
test('quarter analysis rejects mismatched quarters, duplicate stocks and unsafe detail paths', () => {
  assert.equal(ui.validate(fixture(),'2026Q3').stocks.length,1);
  assert.throws(()=>ui.validate(fixture(),'2026Q2'));
  const duplicate=fixture(); duplicate.stocks.push(stock()); assert.throws(()=>ui.validate(duplicate,'2026Q3'));
  const unsafe=fixture(); unsafe.stocks[0].details=['../../private.json']; assert.throws(()=>ui.validate(unsafe,'2026Q3'));
  assert.throws(()=>ui.safePath('2026Q3','./analysis/../../latest.json'));
  assert.equal(ui.safePath('2026Q3','./analysis/details-0000.json.gz'),'./data/periods/2026Q3/analysis/details-0000.json.gz');
});
test('global stock search does not silently limit results to top hundred', () => {
  const stocks=Array.from({length:150},(_,i)=>stock(String(600000+i),i));
  assert.equal(ui.selectStocks(stocks).length,150);
  assert.equal(ui.selectStocks(stocks,'600149')[0].stockCode,'600149');
  assert.equal(ui.selectStocks(stocks,'600001 CN')[0].stockCode,'600001');
  assert.equal(ui.selectStocks(stocks,'missing').length,0);
});
test('analysis CSV distinguishes unresolved identity and keeps null amount cells blank', () => {
  const one=stock();one.stockName=' =SUM(1,2)'; const result=ui.csv([one]);
  assert.match(result,/身份待核验数/);assert.match(result,/' =SUM\(1,2\)/);assert.match(result,/,"20","",/);
});

function generated(id) {
  const target = id === '2026Q2' ? '2026-06-30' : '2026-09-30';
  const baseline = id === '2026Q2' ? '2026-03-31' : '2026-06-30';
  const published = id === '2026Q2' ? '2026-07-20' : '2026-10-20';
  const start = id === '2026Q2' ? '2026-04-01' : '2026-07-01';
  const current = {stockCode:'600001',stockName:'合成证券',market:'CN',currency:'CNY',shares:100,weightPct:5,marketValueYuan:500};
  const report = {fundCode:'000001',kind:'quarterly',coverage:'top10',navYuan:10000,
    sourceUrl:`https://example.org/${id}.pdf`,periodEnd:target,publishedAt:published,holdings:[current],
    strategyExcerpts:[{text:`${id}组合按估值调整行业配置。`,sourceUrl:`https://example.org/${id}.pdf`,page:7,periodStart:start,periodEnd:target}]};
  const snapshot = buildSnapshot({asOf:'2026-10-26',retrievedAt:'2026-10-26T11:00:00Z',errors:[],barsByStock:{},
    funds:[{code:'000001',name:`${id}合成基金`,company:'合成公司',companyId:'co1',
      sourcePortfolioCode:'000001',portfolioSourceUrl:'https://example.org/identity.pdf',shareClassCodes:['000001'],
      managers:[{name:'当前经理'}],reports:[report,{...report,periodEnd:baseline,publishedAt:baseline,
        sourceUrl:`https://example.org/${id}-prior.pdf`,holdings:[{...current,shares:50}],strategyExcerpts:[]}]}]},
    {now:'2026-10-26T12:00:00Z',config:{funds:[{code:'000001'}],targetPeriodEnd:target,baselinePeriodEnd:baseline}});
  snapshot.funds[0].reportManagers=[{name:'报告经理',startDate:'2025-01-01',endDate:null,role:'本基金基金经理',periodStart:start,periodEnd:target,
    evidence:{sourceUrl:'https://example.org/manager-report.pdf',page:5,pages:[5],tablePage:5,section:'4.1',
      text:'报告经理 本基金基金经理 2025年1月1日 -',method:'explicit-report-manager-table',periodEnd:target}}];snapshot.funds[0].managerAttribution='report';
  const file='./data/companies/co1.json',parts=new Map();
  const manifest={...snapshot,funds:[],companyFiles:[{path:file}],coverage:{fundCount:1,catalogFundCount:1}};
  const index=analyzeQuarter(manifest,()=>snapshot,{now:'2026-10-26T12:00:00Z',writePart:(file,part)=>parts.set(file,part)});
  return {index,parts};
}

class Node {
  constructor(tag='div'){this.tag=tag;this.children=[];this.events={};this.value='';this._text='';}
  set textContent(value){this._text=String(value);this.children=[];}
  get textContent(){return this._text+this.children.map(child=>child.textContent??String(child)).join(' ');}
  append(...items){this.children.push(...items);}
  replaceChildren(...items){this._text='';this.children=items;}
  addEventListener(name,handler){(this.events[name]??=[]).push(handler);}
  click(){return Promise.all((this.events.click||[]).map(handler=>handler()));}
}
function runtime(resolve) {
  const nodes=new Map();
  const doc={getElementById:id=>{if(!nodes.has(id))nodes.set(id,new Node());return nodes.get(id);},createElement:tag=>new Node(tag)};
  doc.getElementById('analysis-sort').value='value';
  const win={AbortController,setTimeout,clearTimeout,fetch:async url=>({ok:true,headers:{get:()=>null},text:async()=>JSON.stringify(await resolve(url))})};
  const api=ui.mount(doc,win);
  const buttons=node=>[...(node.tag==='button'?[node]:[]),...node.children.flatMap(child=>buttons(child))];
  return {api,nodes,button:()=>buttons(nodes.get('analysis-results'))[0]};
}
function resolveBundle(bundle,id,url) {
  if(url===ui.safePath(id,'analysis.json'))return bundle.index;
  for(const [file,part] of bundle.parts)if(url===ui.safePath(id,file))return part;
  throw new Error('unexpected fixture URL');
}

test('real generated stock index, contribution parts and evidence render report managers and original quarter quotes',async()=>{
  const bundle=generated('2026Q2');ui.validate(bundle.index,'2026Q2');
  for(const part of bundle.parts.values())ui.validatePart(part,part.kind,bundle.index);
  const view=runtime(url=>resolveBundle(bundle,'2026Q2',url));
  await view.api.loadQuarter({id:'2026Q2'});await view.button().click();
  const result=view.nodes.get('analysis-detail').textContent;
  assert.match(result,/报告经理/);assert.doesNotMatch(result,/当前经理/);
  assert.match(result,/经理表原文 · 第 5 页/);assert.match(result,/任职 2025-01-01/);
  assert.match(result,/经理表待补/);assert.match(result,/期末在任/);
  assert.match(result,/持股数增加/);assert.match(result,/本期持股 100.*上期持股 50.*股数差 50/);
  assert.match(result,/2026Q2组合按估值调整行业配置/);assert.match(result,/第 7 页/);
  assert.equal(bundle.index.generation,[...bundle.parts.values()][0].generation);
});

test('same-quarter parts from another generation or baseline are rejected rather than mixed with old totals',async()=>{
  const bundle=generated('2026Q3'),part=structuredClone([...bundle.parts.values()][0]);
  part.generation='2026-10-26T12:01:00.000Z';assert.throws(()=>ui.validatePart(part,part.kind,bundle.index));
  part.generation=bundle.index.generation;part.baselinePeriodEnd='2026-03-31';assert.throws(()=>ui.validatePart(part,part.kind,bundle.index));
  const view=runtime(url=>{
    const value=structuredClone(resolveBundle(bundle,'2026Q3',url));
    if(value.kind!=='quarter-analysis')value.generation='2026-10-26T12:01:00.000Z';return value;
  });
  await view.api.loadQuarter({id:'2026Q3'});await view.button().click();
  assert.match(view.nodes.get('analysis-detail').textContent,/重新载入/);
  assert.doesNotMatch(view.nodes.get('analysis-detail').textContent,/报告经理/);
});

test('a late response from the previous quarter cannot replace current results or contaminate its detail cache',async()=>{
  const q2=generated('2026Q2'),q3=generated('2026Q3');let release;
  const delayed=new Promise(resolve=>{release=resolve;});
  const view=runtime(async url=>{
    if(url.includes('/2026Q2/')&&url.includes('/analysis/details-0000'))await delayed;
    return resolveBundle(url.includes('/2026Q2/')?q2:q3,url.includes('/2026Q2/')?'2026Q2':'2026Q3',url);
  });
  await view.api.loadQuarter({id:'2026Q2'});const old=view.button().click();
  const newer=view.api.loadQuarter({id:'2026Q3'});
  assert.equal(view.nodes.get('analysis-result-count').textContent,'本季度分析尚未载入');
  await newer;release();await old;await view.button().click();
  const result=view.nodes.get('analysis-detail').textContent;
  assert.match(result,/2026Q3合成基金/);assert.doesNotMatch(result,/2026Q2合成基金/);
  assert.match(view.nodes.get('analysis-status').textContent,/2026Q3/);
});
