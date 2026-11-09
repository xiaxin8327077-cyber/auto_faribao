import math
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE_PATH = Path(__file__).resolve().parents[1] / "src" / "nav_dashboard_page.html"


def _read_page() -> str:
    return PAGE_PATH.read_text(encoding="utf-8")


def test_model_flags_save_immediately_and_restore_last_saved_values_on_failure():
    html = _read_page()
    source = html[html.index('let modelProfiles ='):html.index('function positionTypeLabel(')]
    bindings = '\n'.join(line.strip() for line in html.splitlines()
                         if "addEventListener('change', saveModelFlags)" in line)
    script = r'''
const assert = require('node:assert/strict');
const elements = Object.fromEntries(['aiEnabled','estimateJobsEnabled','modelDialog','modelProfileList','aiCopyFromProfile','estimateJobStatus'].map(id => [id,{checked:false,disabled:false,open:false,innerHTML:'',value:'',addEventListener(name,fn){this[name]=fn;}}]));
const $ = id => elements[id];
const esc = String;
const restoreSkeleton = () => {};
const markSkeletonReady = () => {};
let messages=[];const showToast = text => messages.push(text);
let calls=[];let handler=async()=>({ok:true,json:async()=>({enabled:false,jobs_enabled:true,profiles:[]})});
const api=async(path,options={})=>{calls.push({path,body:options.body?JSON.parse(options.body):null});return handler();};
SOURCE
BINDINGS
(async()=>{
 await $('aiEnabled').change();
 assert.equal(calls.length,0,'unloaded flags must never overwrite server settings');
 let release;
 handler=async()=>new Promise(resolve=>{release=()=>resolve({ok:true,json:async()=>({enabled:true,jobs_enabled:true,profiles:[]})});});
 const loading=loadModelSettings();const duplicate=loadModelSettings();
 assert.equal(calls.length,1);
 assert.equal($('aiEnabled').disabled,true);
 await $('aiEnabled').change();
 assert.equal(calls.length,1);
 release();await loading;await duplicate;
 assert.equal($('aiEnabled').checked,true);
 assert.equal($('estimateJobsEnabled').checked,true);
 assert.equal($('aiEnabled').disabled,false);
 calls=[];
 handler=async()=>({ok:true,json:async()=>({enabled:false,jobs_enabled:true,profiles:[]})});
 $('aiEnabled').checked=false;
 assert.equal(typeof $('aiEnabled').change,'function');
 await $('aiEnabled').change();
 assert.equal(calls.length,1);
 assert.deepEqual(calls[0].body,{enabled:false,jobs_enabled:true});
 assert.equal($('aiEnabled').checked,false);
 assert.equal($('aiEnabled').disabled,false);
 handler=async()=>{throw new Error('offline');};
 $('estimateJobsEnabled').checked=false;
 await $('estimateJobsEnabled').change();
 assert.equal($('estimateJobsEnabled').checked,true);
 assert.equal($('estimateJobsEnabled').disabled,false);
 assert(messages.some(text=>text.includes('失败')));
})().catch(e=>{console.error(e);process.exitCode=1;});
'''.replace('SOURCE', source).replace('BINDINGS', bindings)
    result = subprocess.run([shutil.which('node'), '-e', script], capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stdout + result.stderr


def test_manual_analysis_button_uses_post_even_when_automatic_verification_is_paused():
    _run_continuity_behavior(r'''
const paused = report([row], {ai_status:'ready',ai_model:'old-model',ai_analysis:'原分析',refresh_complete:true,
 expected_fund_count:1,verified_fund_count:1,next_refresh_at:'2099-01-01T15:00:00+08:00'});
renderVerification(paused);
assert(elements.verificationContent.innerHTML.includes('重新生成分析'));
const button=elements.verificationContent.querySelector('.verification-regenerate');
assert.equal(typeof button.click,'function');
let release;
handler=async(path,options)=>{
 assert(path.endsWith('/fund-estimate-verifications/regenerate'));
 assert.equal(options.method,'POST');
 assert.deepEqual(JSON.parse(options.body),{});
 return new Promise(resolve=>{release=()=>resolve(response({...paused,ai_model:'new-model',ai_status:'generating',refresh_complete:false,next_refresh_at:null}));});
};
const first=button.click();await flush();
assert.equal(button.disabled,true);
button.click();await flush();
assert.equal(calls.length,1);
release();await first;await flush();
assert.equal(verificationState().lastData.ai_model,'new-model');
assert.deepEqual(verificationState().lastData.items,paused.items);
assert.equal(verificationPauseUntil(),0);
assert.equal(verificationState().lastData.ai_status,'generating');
''')


def _run_continuity_behavior(body: str, extra_source: str = ""):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for dashboard behavior checks")
    html = _read_page()
    source = html[html.index("function stockPercent("):html.index("function setModelSettingsStatus(")]
    harness = r"""
const assert = require('node:assert/strict');
class Element {
  constructor() { this.innerHTML = ''; this.textContent = ''; this.hidden = false; this.attrs = {}; this.controls = {}; }
  setAttribute(name, value) { this.attrs[name] = value; }
  querySelector(selector) {
    if (!this.innerHTML.includes(selector.slice(1))) return null;
    return this.controls[selector] ||= {addEventListener: (name, listener) => { this.controls[selector][name] = listener; }};
  }
  querySelectorAll() { return []; }
}
const elements = Object.fromEntries(['verificationContent', 'verificationMeta', 'verificationSection',
  'verificationStatus', 'verificationSpinner', 'stockMarketContent', 'stockMarketMeta',
  'stockMarketStatus', 'stockMarketSpinner', 'stockMarketLoadingText'].map(id => [id, new Element()]));
const $ = id => elements[id];
const restoreSkeleton = id => { const box = $(id); if (box) box.innerHTML = '<div class="loading-skeleton" aria-hidden="true"></div>'; };
const markSkeletonReady = id => { const box = $(id); if (box && box.setAttribute) box.setAttribute('aria-busy', 'false'); };
const esc = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
const tone = value => Number(value) > 0 ? 'positive' : Number(value) < 0 ? 'negative' : 'neutral';
const document = {hidden: false};
const state = {activeTab: 'market', data: null};
let marketLoading = false, marketEtag = '', marketPollTimer, lastMarketPayload = '';
let nextTimer = 1;
const timers = new Map();
const setTimeout = (fn, delay) => { const id = nextTimer++; timers.set(id, {callback: fn,
  fn: (...args) => { timers.delete(id); return fn(...args); }, delay}); return id; };
const clearTimeout = id => timers.delete(id);
const flush = async () => { for (let n = 0; n < 14; n++) await Promise.resolve(); };
const response = data => ({ok: true, status: 200, headers: {get: () => ''}, json: async () => data});
let calls = [], handler = async () => response({status: 'empty', items: []});
const api = (path, options = {}) => { calls.push({path, options}); return handler(path, options); };
const renderBarometer = () => {}, renderHeldFundEstimates = () => {}, setAccessState = () => {};
const row = {product_id: 'p', code: '918273', name: '测试基金', trade_date: '2026-10-26',
  predicted_pct: '0.08', realized_pct: '0.1', error_pct: '-0.02', direction_ok: true,
  predicted_profit: '8', realized_profit: '10', profit_error: '-2'};
const report = (items = [row], extra = {}) => ({items, status: 'ready', today_date: '2026-10-27',
  latest_available_date: items.map(item => item.trade_date).sort().at(-1),
  today_confirmed_count: items.filter(item => item.trade_date === '2026-10-27').length, ...extra});
let behaviorFinished = false;
process.on('beforeExit', () => assert.ok(behaviorFinished, 'asynchronous behavior did not finish'));
SOURCE
EXTRA_SOURCE
(async () => { BODY })().then(() => { behaviorFinished = true; }).catch(error => { behaviorFinished = true; console.error(error); process.exitCode = 1; });
"""
    # Keep the substitutions independent: the page source itself can contain arbitrary words.
    script = harness.replace("EXTRA_SOURCE", extra_source).replace("SOURCE", source).replace("BODY", body)
    result = subprocess.run([node], input=script, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr


def test_verification_initial_loading_has_animation_then_timeout_and_retry():
    _run_continuity_behavior(r"""
let resolveSlow;
handler = () => new Promise(resolve => { resolveSlow = resolve; });
const pending = loadVerification();
assert.ok(elements.verificationContent.innerHTML.includes('loading-skeleton'));
assert.equal(elements.verificationSection.attrs['aria-busy'], 'true');
assert.ok(elements.verificationStatus.innerHTML.includes('正在加载验证分析'));
const timeout = [...timers.values()].find(timer => timer.delay > 0 && timer.delay < 30000);
assert.ok(timeout, 'the verification request must have a finite timeout');
timeout.fn();
await pending;
assert.ok(elements.verificationStatus.innerHTML.includes('验证分析加载失败'));
assert.ok(elements.verificationStatus.innerHTML.includes('重试'));
assert.equal(elements.verificationSpinner.hidden, true);
handler = async () => response(report());
elements.verificationStatus.querySelector('.verification-retry').click();
await flush();
assert.ok(elements.verificationContent.innerHTML.includes('测试基金'));
resolveSlow(response(report([{...row, name: '迟到响应'}])));
await flush();
assert.ok(!elements.verificationContent.innerHTML.includes('迟到响应'));
""")


def test_verification_refresh_failure_and_transient_empty_keep_last_success():
    _run_continuity_behavior(r"""
handler = async () => response(report());
await loadVerification();
const content = elements.verificationContent.innerHTML;
let rejectSlow;
handler = () => new Promise((resolve, reject) => { rejectSlow = reject; });
const pending = loadVerification();
assert.equal(elements.verificationContent.innerHTML, content);
assert.equal(elements.verificationSpinner.hidden, false);
rejectSlow(new Error('offline'));
await pending;
assert.equal(elements.verificationContent.innerHTML, content);
assert.ok(elements.verificationStatus.innerHTML.includes('更新失败，已保留上次数据'));
assert.ok(elements.verificationStatus.innerHTML.includes('重试'));
handler = async () => response({items: []});
await loadVerification();
assert.equal(elements.verificationContent.innerHTML, content);
handler = async () => response({items: [], status: 'empty'});
await loadVerification();
assert.ok(elements.verificationContent.innerHTML.includes('尚无历史验证记录'));
""")


def test_verification_follows_latest_unless_user_selects_history():
    _run_continuity_behavior(r"""
renderVerification(report());
assert.ok(elements.verificationStatus.innerHTML.includes('今日净值尚未更新，显示 2026-10-26'));
const today = {...row, trade_date: '2026-10-27', name: '今日基金'};
const current = report([row, today]);
renderVerification(current);
assert.equal(renderVerification.selectedDate, '2026-10-27');
assert.ok(elements.verificationStatus.innerHTML.includes('今日已确认 1 只'));
elements.verificationContent.querySelector('.verification-date').change({target: {value: '2026-10-26'}});
renderVerification(report([row, today, {...today, product_id: 'p2', name: '第二只'}]));
assert.equal(renderVerification.selectedDate, '2026-10-26');
assert.ok(elements.verificationContent.innerHTML.includes('查看最新'));
elements.verificationContent.querySelector('.verification-latest').click();
assert.equal(renderVerification.selectedDate, '2026-10-27');
assert.ok(elements.verificationContent.innerHTML.includes('第二只'));
""")


def test_verification_shows_confirmed_nav_while_comparison_and_ai_are_pending():
    _run_continuity_behavior(r"""
renderVerification(report([{...row, trade_date: '2026-10-27', unit_nav: '1.2345',
  nav_return_pct: '0', realized_pct: null, error_pct: null, direction_ok: null,
  verification_status: 'pending_return'}], {ai_status: 'generating', ai_analysis: '不应显示旧版本点评'}));
const html = elements.verificationContent.innerHTML;
assert.ok(html.includes('1.2345'));
assert.ok(html.includes('收益核验中'));
assert.ok(html.includes('净值涨跌'));
assert.ok(html.includes('0.000%'));
assert.ok(html.includes('分析生成中'));
assert.ok(!html.includes('不应显示旧版本点评'));
assert.ok(!html.includes('当日无有效预估'));
assert.ok(html.includes('累计分析'));
""")


def test_verification_requests_merge_and_visible_polling_is_independent_of_market_failure():
    _run_continuity_behavior(r"""
let resolveFirst;
handler = path => path.includes('market-overview') ? Promise.reject(new Error('market offline'))
  : new Promise(resolve => { resolveFirst = resolve; });
const pending = loadVerification();
loadVerification(); loadVerification({queue: true}); loadVerification({queue: true});
await loadMarketOverview();
assert.equal(calls.filter(call => call.path.includes('verifications')).length, 1);
handler = async () => response(report([{...row, name: '最新结果'}]));
resolveFirst(response(report()));
await pending; await flush();
assert.equal(calls.filter(call => call.path.includes('verifications')).length, 2);
assert.ok(elements.verificationContent.innerHTML.includes('最新结果'));
const before = calls.length;
const poll = [...timers.values()].filter(timer => timer.delay === 30000).at(-1);
assert.ok(poll, 'visible verification must poll without a successful market request');
poll.fn(); await flush();
assert.ok(calls.length > before);
document.hidden = true;
await loadVerification();
assert.ok(![...timers.values()].some(timer => timer.callback === loadVerification));
""")


def test_stock_loading_times_out_with_retry_and_keeps_existing_market_on_failure():
    _run_continuity_behavior(r"""
handler = path => path.includes('verifications') ? Promise.resolve(response(report())) : new Promise(() => {});
const pending = loadMarketOverview();
assert.ok(elements.stockMarketContent.innerHTML.includes('loading-skeleton'));
const timeout = [...timers.values()].filter(timer => timer.delay > 0 && timer.delay < 30000).sort((left, right) => left.delay - right.delay)[0];
timeout.fn(); await pending;
assert.ok(elements.stockMarketStatus.innerHTML.includes('股市行情加载失败'));
assert.ok(elements.stockMarketStatus.innerHTML.includes('重试'));
const stock = {available: true, indices: [{name: '上证指数', market: 'A股', price: '3300', change_pct: '0.1'}]};
renderStockMarket(stock);
const content = elements.stockMarketContent.innerHTML;
renderStockMarket(undefined);
assert.equal(elements.stockMarketContent.innerHTML, content);
handler = async path => { if (path.includes('market-overview')) throw new Error('offline'); return response(report()); };
await loadMarketOverview();
assert.equal(elements.stockMarketContent.innerHTML, content);
assert.ok(elements.stockMarketStatus.innerHTML.includes('已保留上次行情'));
handler = async path => response(path.includes('market-overview') ? {stock_market: {available: false}} : report());
await loadMarketOverview();
assert.equal(elements.stockMarketContent.innerHTML, content);
assert.ok(elements.stockMarketStatus.innerHTML.includes('已保留上次行情'));
""")


def test_closed_markets_use_saved_snapshot_and_verification_still_polls():
    _run_continuity_behavior(r"""
let now = Date.parse('2026-10-27T18:00:00+08:00');
Date.now = () => now;
const opens = now + 3 * 3600000;
const stock = {available: true, live_session: {as_of: new Date(now).toISOString(), any_active: false,
  next_open_at: new Date(opens).toISOString(), next_transition_at: new Date(opens).toISOString()},
  indices_status: {CN: {collection_phase: 'close'}, HK: {collection_phase: 'close'}, US: {collection_phase: 'closed'}},
  indices: [{name: '上证指数', market: 'A股', price: '3300.01', change_pct: '0.1'}]};
state.data = {stock_market: stock};
renderStockMarket(stock);
const saved = elements.stockMarketContent.innerHTML;
handler = async () => response(report());
await loadMarketOverview(); await flush();
await loadMarketOverview(); await flush();
assert.equal(calls.filter(call => call.path.includes('market-overview')).length, 0);
assert.equal(elements.stockMarketContent.innerHTML, saved);
assert.equal(elements.stockMarketSpinner.hidden, true);
assert.equal(elements.stockMarketLoadingText.textContent, '');
assert.ok(![...timers.values()].some(timer => timer.callback === loadMarketOverview && timer.delay === 30000));
const verificationPoll = [...timers.values()].find(timer => timer.callback === loadVerification && timer.delay === 30000);
assert.ok(verificationPoll, 'closed stock markets must not stop verification polling');
const before = calls.filter(call => call.path.includes('verifications')).length;
verificationPoll.fn(); await flush();
assert.ok(calls.filter(call => call.path.includes('verifications')).length > before);
const wake = [...timers.values()].find(timer => timer.callback === loadMarketOverview);
assert.ok(wake && wake.delay > 30000, 'wake at the real server-provided next session');
now = opens + 1000;
handler = async path => response(path.includes('market-overview') ? {stock_market: {...stock,
  live_session: {as_of: new Date(now).toISOString(), any_active: true, next_transition_at: new Date(now + 3600000).toISOString()},
  indices: [{...stock.indices[0], price: '3300.02'}]}} : report());
wake.fn(); await flush();
assert.equal(calls.filter(call => call.path.includes('market-overview')).length, 1);
assert.ok(elements.stockMarketContent.innerHTML.includes('stock-digit-flip'));
assert.ok([...timers.values()].some(timer => timer.callback === loadMarketOverview && timer.delay === 30000));
""")


def test_closed_markets_skip_visibility_refresh_and_only_allow_explicit_force():
    html = _read_page()
    start = html.index("document.addEventListener('visibilitychange'")
    listener = html[start:html.index("\n    });", start) + len("\n    });")]
    _run_continuity_behavior(r"""
Date.now = () => Date.parse('2026-10-27T18:00:00+08:00');
const stock = {available: true, live_session: {as_of: '2026-10-27T18:00:00+08:00', any_active: false,
  next_open_at: '2026-10-27T21:30:00+08:00', next_transition_at: '2026-10-27T21:30:00+08:00'},
  indices: [{name: '上证指数', market: 'A股', price: '3300'}]};
state.data = {stock_market: stock}; renderStockMarket(stock);
handler = async path => response(path.includes('market-overview') ? {stock_market: stock} : report());
document.hidden = true; document.onVisibility(); await flush();
document.hidden = false; document.onVisibility(); await flush();
assert.equal(calls.filter(call => call.path.includes('market-overview')).length, 0);
assert.equal(elements.stockMarketSpinner.hidden, true);
await loadMarketOverview(true); await flush();
assert.equal(calls.filter(call => call.path.includes('market-overview')).length, 1);
await loadMarketOverview(); await flush();
assert.equal(calls.filter(call => call.path.includes('market-overview')).length, 1);
""", "document.addEventListener = (name, fn) => { document.onVisibility = fn; }; const loadDashboard = () => {};\n" + listener)


def test_stock_first_read_waits_for_dashboard_cache_and_does_not_repeat_without_cache():
    _run_continuity_behavior(r"""
marketSessionState().dashboardPending = true;
await loadMarketOverview();
assert.equal(calls.filter(call => call.path.includes('market-overview')).length, 0);
marketSessionState().dashboardPending = false;
handler = async path => response(path.includes('market-overview') ? {stock_market: {available: false,
  live_session: {as_of: new Date().toISOString(), any_active: false}}} : report());
await loadMarketOverview();
assert.equal(calls.filter(call => call.path.includes('market-overview')).length, 1);
await loadMarketOverview();
assert.equal(calls.filter(call => call.path.includes('market-overview')).length, 1);
assert.equal(elements.stockMarketSpinner.hidden, true);
assert.ok(elements.stockMarketStatus.innerHTML.includes('重试'));
""")


def test_existing_stock_background_read_is_silent_until_real_numbers_arrive():
    _run_continuity_behavior(r"""
const stock = {available: true, live_session: {as_of: new Date().toISOString(), any_active: true},
  indices: [{name: '上证指数', market: 'A股', price: '3300.01'}]};
state.data = {stock_market: stock}; renderStockMarket(stock);
let finishRead;
handler = path => path.includes('market-overview') ? new Promise(resolve => { finishRead = resolve; }) : Promise.resolve(response(report()));
const content = elements.stockMarketContent.innerHTML;
const pending = loadMarketOverview();
assert.equal(elements.stockMarketContent.innerHTML, content);
assert.equal(elements.stockMarketSpinner.hidden, true);
assert.equal(elements.stockMarketLoadingText.textContent, '');
assert.equal(elements.stockMarketStatus.innerHTML, '');
finishRead(response({stock_market: {...stock, indices: [{...stock.indices[0], price: '3300.02'}]}}));
await pending;
assert.ok(elements.stockMarketContent.innerHTML.includes('stock-digit-flip'));
""")


def test_closed_markets_continue_reading_dashboard_cache_for_other_cards():
    _run_continuity_behavior(r"""
let now = Date.parse('2026-10-27T09:00:00+08:00');
Date.now = () => now;
renderStockMarket({available: true, live_session: {as_of: new Date(now).toISOString(), any_active: false,
  next_transition_at: '2026-10-27T09:30:00+08:00'}, indices: []});
pollDashboardSnapshot();
assert.equal(dashboardReads, 1, 'bond and estimates still need local dashboard snapshots while stocks are closed');
now += 1800001;
rememberMarketSession({live_session: {as_of: new Date(now).toISOString(), any_active: true}});
pollDashboardSnapshot();
assert.equal(dashboardReads, 1, 'active markets already refresh all cards through market-overview');
document.hidden = true; state.activeTab = 'overview'; pollDashboardSnapshot();
assert.equal(dashboardReads, 1);
""", "let dashboardReads = 0; const loadDashboard = () => { dashboardReads++; };")


def test_new_session_metadata_survives_retaining_a_newer_quote_snapshot():
    _run_continuity_behavior(r"""
Date.now = () => Date.parse('2026-10-27T18:00:00+08:00');
const newer = {available: true, fetched_at: '2026-10-27T16:00:00+08:00',
  live_session: {as_of: '2026-10-27T14:00:00+08:00', any_active: true},
  indices_status: {CN: {collection_phase: 'afternoon'}},
  indices: [{name: '较新报价', market: 'A股', price: '3300.01'}]};
const currentSession = {as_of: '2026-10-27T18:00:00+08:00', any_active: false,
  next_transition_at: '2026-10-27T21:30:00+08:00'};
const incoming = {stock_market: {...newer, fetched_at: '2026-10-27T15:00:00+08:00',
  live_session: currentSession, indices_status: {CN: {collection_phase: 'close'}},
  indices: [{name: '旧报价', market: 'A股', price: '3290'}]}};
state.data = preserveMarketSnapshot(incoming, {stock_market: newer});
assert.equal(state.data.stock_market.indices[0].name, '较新报价');
assert.equal(state.data.stock_market.live_session, currentSession);
assert.equal(state.data.stock_market.indices_status.CN.collection_phase, 'close');
renderStockMarket(state.data.stock_market);
await loadMarketOverview(); await flush();
assert.equal(calls.filter(call => call.path.includes('market-overview')).length, 0);
""")


def test_stock_progress_sits_beside_title_and_top_meta_only_shows_update_time():
    _run_continuity_behavior(r"""
stockMarketStatus('正在读取股市行情…', true);
// 标题旁保留旋转圆环，文字改为骨架条。
assert.ok(elements.stockMarketLoadingText.innerHTML.includes('loading-skeleton'));
assert.equal(elements.stockMarketStatus.innerHTML, '');
assert.equal(elements.stockMarketSpinner.hidden, false);
renderStockMarket({available: true, fetched_at: '2026-10-27T16:00:00+08:00',
  indices_status: {CN: {collection_phase: 'close'}}, indices: [{name: '上证指数', market: 'A股', price: '3300'}]});
assert.equal(elements.stockMarketMeta.textContent, '16:00 更新');
stockMarketStatus('正在更新行情…', true);
assert.equal(elements.stockMarketLoadingText.textContent, '');
assert.equal(elements.stockMarketStatus.innerHTML, '');
assert.equal(elements.stockMarketSpinner.hidden, true);
""")
    html = _read_page()
    header = html[html.index('class="section-head stock-market-heading"'):html.index('id="stockMarketStatus"')]
    assert header.index('股市行情') < header.index('stockMarketSpinner') < header.index('stockMarketLoadingText') < header.index('stockMarketMeta')


def test_first_paint_shows_layout_skeletons_across_tabs():
    html = _read_page()
    # 旧的文字占位不再出现，首屏直接展示布局骨架。
    for text in ('正在读取组合数据', '正在读取行情', '正在读取持仓基金', '正在读取持仓',
                 '正在读取模型配置', '正在统计'):
        assert text not in html
    assert 'id="dashboardContent" hidden' not in html
    assert 'html.access-invalid #dashboardContent { display: none !important; }' in html
    for marker in ('loading-skeleton--total', 'loading-skeleton--rank', 'loading-skeleton--cell',
                   'loading-skeleton--sparkline', 'loading-skeleton--circle', 'loading-skeleton--button',
                   'loading-skeleton--header'):
        assert marker in html


def test_barometer_and_model_settings_show_skeletons_only_without_data():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for dashboard behavior checks")
    html = _read_page()
    barometer = html[html.index("async function loadBarometer"):html.index("function setActiveTab")]
    model = html[html.index("async function loadModelSettings"):html.index("function renderModelChoices")]
    script = r"""
const assert = require('node:assert/strict');
class Element {
  constructor() { this.innerHTML = ''; this.textContent = ''; this.hidden = false; this.disabled = false; this.attrs = {}; this.style = {}; }
  setAttribute(name, value) { this.attrs[name] = value; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
}
const elements = Object.fromEntries(['barometerContent', 'modelProfileList', 'aiEnabled', 'estimateJobsEnabled'].map(id => [id, new Element()]));
const $ = id => elements[id];
const esc = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
const skeletonSnapshots = new Map();
const restoreSkeleton = id => { const box = $(id); if (box) box.innerHTML = '<div class="loading-skeleton" aria-hidden="true"></div>'; };
const markSkeletonReady = () => {};
const renderBarometer = () => {};
const applyModelSettings = () => {};
const notifyModelSettings = () => {};
let barometerLoading = false, barometerReady = false;
let modelSettingsLoading = null, modelSettingsLoaded = false, modelFlagsSaving = false;
const response = data => ({ok: true, status: 200, headers: {get: () => ''}, json: async () => data});
let handler = async () => response({});
const api = async path => handler(path);
const setAccessState = () => {};
""" + barometer + model + r"""
(async () => {
  let release;
  handler = () => new Promise(resolve => { release = resolve; });
  const pending = loadBarometer();
  assert.ok(elements.barometerContent.innerHTML.includes('loading-skeleton'), '债市无数据时应显示骨架');
  release(response({}));
  await pending;
  elements.barometerContent.innerHTML = '<div>已加载</div>';
  barometerReady = true;
  handler = () => new Promise(() => {});
  await loadBarometer();
  assert.equal(elements.barometerContent.innerHTML, '<div>已加载</div>', '债市已有数据时不得显示骨架');

  let releaseModel;
  handler = () => new Promise(resolve => { releaseModel = resolve; });
  const pendingModel = loadModelSettings();
  assert.ok(elements.modelProfileList.innerHTML.includes('loading-skeleton'), '模型配置无数据时应显示骨架');
  releaseModel(response({profiles: []}));
  await pendingModel;
})().catch(error => { console.error(error); process.exit(1); });
"""
    result = subprocess.run([node], input=script, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr


def test_dashboard_render_preserves_market_fields_and_renders_saved_stock_snapshot():
    html = _read_page()
    start = html.index("function render(data) {")
    source = html[start:html.index("const cumulative =", start)] + "}"
    _run_continuity_behavior(r"""
const stock = {available: true, indices: [{name: '上证指数', market: 'A股', price: '3300'}]};
state.data = {stock_market: stock, fund_estimates: {p: {return_pct: '1'}}, bond_barometer: {ready: true}};
render({products: []});
assert.equal(state.data.stock_market, stock);
assert.ok(state.data.fund_estimates.p);
assert.ok(elements.stockMarketContent.innerHTML.includes('上证指数'));
""", source)


def test_stock_digits_flip_only_when_the_real_displayed_value_changes():
    _run_continuity_behavior(r"""
const stock = price => ({available: true, indices: [{name: '上证指数', market: 'A股', price,
  change_pct: '0.12'}]});
renderStockMarket(stock('3300.01'));
assert.ok(!elements.stockMarketContent.innerHTML.includes('stock-digit-flip'));
renderStockMarket(stock('3300.02'));
assert.ok(elements.stockMarketContent.innerHTML.includes('stock-digit-flip'));
assert.ok(elements.stockMarketContent.innerHTML.includes('aria-label="3300.02"'));
renderStockMarket(stock('3300.02'));
assert.ok(!elements.stockMarketContent.innerHTML.includes('stock-digit-flip'));
globalThis.matchMedia = () => ({matches: true});
renderStockMarket(stock('3300.03'));
assert.ok(!elements.stockMarketContent.innerHTML.includes('stock-digit-flip'));
assert.ok(elements.stockMarketContent.innerHTML.includes('3300.03'));
globalThis.matchMedia = () => ({matches: false});
renderStockMarket(stock(null));
assert.ok(!elements.stockMarketContent.innerHTML.includes('stock-digit-flip'));
""")
    html = _read_page()
    assert "@keyframes stock-digit-roll" in html


def test_stock_trend_heartbeat_uses_last_real_point_only_while_fresh_and_trading():
    _run_continuity_behavior(r"""
const now = Date.parse('2026-10-27T10:02:00+08:00');
const item = {name: '上证指数', market_id: 'CN', quote_date: '2026-10-27', change_pct: '1',
  trend: {quote_date: '2026-10-27', pre_close: '100', points: [
    {time: '2026-10-27T10:00:00+08:00', price: '100'},
    {time: '2026-10-27T10:01:00+08:00', price: '101'}]}};
const live = stockSparkline(item, {collection_phase: 'morning'}, now);
assert.ok(live.includes('class="stock-trend-pulse"'));
assert.ok(live.includes('class="stock-trend-endpoint" cx="118.00" cy="2.00"'));
for (const status of [{collection_phase: 'close'}, {collection_phase: 'lunch'},
  {collection_phase: 'morning', stale: true}, {collection_phase: 'morning', refresh_error: 'offline'}]) {
  assert.ok(!stockSparkline(item, status, now).includes('class="stock-trend-pulse"'));
  assert.ok(!stockSparkline(item, status, now).includes('class="stock-trend-endpoint"'));
}
assert.ok(!stockSparkline({...item, source_delayed: true}, {collection_phase: 'morning'}, now).includes('class="stock-trend-pulse"'));
assert.ok(!stockSparkline(item, {collection_phase: 'morning'}, now + 6 * 60000).includes('class="stock-trend-pulse"'));
assert.ok(!stockSparkline({...item, quote_date: '2026-10-26'}, {collection_phase: 'morning'}, now).includes('stock-trend-endpoint'));
""")


def test_verification_initial_unready_response_stops_loading_without_claiming_no_history():
    _run_continuity_behavior(r"""
handler = async () => response({items: []});
await loadVerification();
assert.ok(elements.verificationStatus.innerHTML.includes('验证分析加载失败'));
assert.ok(!elements.verificationContent.innerHTML.includes('loading-spinner'));
assert.ok(!elements.verificationContent.innerHTML.includes('尚无历史验证记录'));
""")


@pytest.mark.parametrize("failure", ["network", "timeout"])
def test_verification_generation_without_items_stops_body_animation_on_failure(failure):
    _run_continuity_behavior(r"""
handler = async () => response({status: 'updating', items: [], today_confirmed_count: 1});
await loadVerification();
assert.ok(elements.verificationContent.innerHTML.includes('loading-skeleton'));
handler = FAILURE === 'network' ? async () => { throw new Error('offline'); } : () => new Promise(() => {});
const pending = loadVerification();
if (FAILURE === 'timeout') [...timers.values()].find(timer => timer.delay === 15000).fn();
await pending;
assert.ok(elements.verificationStatus.innerHTML.includes('验证分析加载失败'));
assert.ok(elements.verificationStatus.innerHTML.includes('重试'));
assert.equal(elements.verificationSpinner.hidden, true);
assert.ok(!elements.verificationContent.innerHTML.includes('loading-spinner'));
assert.ok(!elements.verificationContent.innerHTML.includes('loading-skeleton'));
assert.ok(!elements.verificationContent.innerHTML.includes('尚无历史验证记录'));
""".replace("FAILURE", repr(failure)))


def test_verification_pending_dividend_ledger_keeps_returns_and_explains_missing_money():
    _run_continuity_behavior(r"""
renderVerification(report([{...row, profit_status: 'pending_dividend_ledger',
  verification_status: 'verified', predicted_pct: '0.08', realized_pct: '0.1',
  predicted_profit: '8', realized_profit: null, profit_error: null}]));
const html = elements.verificationContent.innerHTML;
assert.ok(html.includes('收益金额核验中'));
assert.ok(html.includes('+0.080%'));
assert.ok(html.includes('+0.100%'));
assert.ok(html.includes('+8.00元'));
assert.ok(!html.includes('收益核验中'));
""")


def test_unchanged_market_response_rechecks_trend_freshness():
    _run_continuity_behavior(r"""
let now = Date.parse('2026-10-27T10:02:00+08:00');
Date.now = () => now;
renderStockMarket({available: true, indices_status: {CN: {collection_phase: 'morning'}}, indices: [
  {name: '上证指数', market: 'A股', market_id: 'CN', quote_date: '2026-10-27', price: '101',
   trend: {quote_date: '2026-10-27', points: [
     {time: '2026-10-27T10:00:00+08:00', price: '100'},
     {time: '2026-10-27T10:01:00+08:00', price: '101'}]}}]});
assert.ok(elements.stockMarketContent.innerHTML.includes('stock-trend-pulse'));
now += 6 * 60000;
handler = async path => path.includes('market-overview') ? {status: 304, ok: false} : response(report());
await loadMarketOverview();
assert.ok(!elements.stockMarketContent.innerHTML.includes('stock-trend-pulse'));
""")


def test_fresh_market_heartbeat_is_not_blocked_by_another_markets_stale_cache():
    _run_continuity_behavior(r"""
Date.now = () => Date.parse('2026-10-27T10:02:00+08:00');
renderStockMarket({available: true, stale: true, refresh_error: 'US: offline',
  indices_status: {CN: {collection_phase: 'morning', stale: false}, US: {collection_phase: 'close', stale: true}},
  indices: [{name: '上证指数', market: 'A股', market_id: 'CN', quote_date: '2026-10-27', price: '101',
    stale: false, source_delayed: false, trend: {quote_date: '2026-10-27', points: [
      {time: '2026-10-27T10:00:00+08:00', price: '100'},
      {time: '2026-10-27T10:01:00+08:00', price: '101'}]}}]});
assert.ok(elements.stockMarketContent.innerHTML.includes('stock-trend-pulse'));
""")


def test_stock_access_failure_stops_first_load_animation():
    _run_continuity_behavior(r"""
handler = async path => path.includes('market-overview') ? {ok: false, status: 403} : response(report());
await loadMarketOverview();
assert.ok(elements.stockMarketStatus.innerHTML.includes('股市行情加载失败'));
assert.ok(!elements.stockMarketContent.innerHTML.includes('loading-spinner'));
""")


def test_dashboard_reload_requests_verification_and_retains_newer_market_snapshot():
    html = _read_page()
    start = html.index("function render(data) {")
    render_source = html[start:html.index("const cumulative =", start)] + "}"
    _run_continuity_behavior(r"""
const newer = {available: true, fetched_at: '2026-10-27T10:03:00+08:00',
  indices: [{name: '最新行情', market: 'A股', price: '3300'}]};
state.data = {stock_market: newer};
render({stock_market: {...newer, fetched_at: '2026-10-27T10:00:00+08:00',
  indices: [{name: '旧行情', market: 'A股', price: '3290'}]}});
assert.equal(state.data.stock_market, newer);
assert.ok(elements.stockMarketContent.innerHTML.includes('最新行情'));
""", render_source)
    start = html.index("async function loadDashboard()")
    load_source = html[start:html.index("async function refresh()", start)]
    _run_continuity_behavior(r"""
handler = async path => response(path === API_PATH ? {confirmed_nav_version: 'nav-v1'} : report());
await loadDashboard(); await flush();
assert.equal(calls.filter(call => call.path.includes('verifications')).length, 1);
await loadDashboard(); await flush();
assert.equal(calls.filter(call => call.path.includes('verifications')).length, 2);
""", "const accessKey = 'test', API_PATH = '/dashboard'; const disableWrites = () => {}, render = () => {};\n" + load_source)


def test_stock_market_and_estimates_use_red_up_green_down():
    html = _read_page()
    # tone() 输出的 positive/negative 需要有基础颜色规则，股市与预估卡片才是红涨绿跌。
    assert ".positive { color: var(--positive); }" in html
    assert ".negative { color: var(--negative); }" in html
    assert '--positive: #f05a24;' in html and '--negative: #07944f;' in html


def test_stock_sparklines_use_actual_same_day_points_and_change_on_live_update():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for dashboard behavior checks')
    html = _read_page()
    assert 'function stockSparkline(' in html
    source = html[html.index('function stockSparkline('):html.index('function renderStockMarket(')]
    script = r"""
const assert = require('node:assert/strict');
const esc = value => String(value).replaceAll('<', '&lt;').replaceAll('"', '&quot;');
const tone = value => Number(value) > 0 ? 'positive' : Number(value) < 0 ? 'negative' : 'neutral';
SOURCE
const item = {name: '上证指数', market_id: 'CN', quote_date: '2026-10-23', change_pct: '1',
  trend: {quote_date: '2026-10-23', pre_close: '100', points: [
    {time: '2026-10-23T09:30:00+08:00', price: '99'},
    {time: '2026-10-23T09:31:00+08:00', price: '101'}]}};
const first = stockSparkline(item);
assert.ok(first.includes('<svg'));
assert.ok(first.includes('当日走势'));
assert.ok(first.includes('positive'));
item.trend.points[1].price = '102';
assert.notEqual(stockSparkline(item), first);
item.trend.points[1].price = '99';
assert.ok(!stockSparkline(item).includes('NaN'));
assert.ok(!stockSparkline(item).includes('Infinity'));
item.trend.quote_date = '2026-10-22';
assert.ok(!stockSparkline(item).includes('<svg'));
const gold = {name: '沪金主连', market_id: 'SHFE', quote_date: '2026-10-28', change_pct: '-1',
  session_segments: [
    {start: '2026-10-27T21:00:00+08:00', end: '2026-10-28T02:30:00+08:00'},
    {start: '2026-10-28T09:00:00+08:00', end: '2026-10-28T10:15:00+08:00'}],
  trend: {quote_date: '2026-10-28', pre_close: '880', points: [
    {time: '2026-10-27T21:01:00+08:00', price: '881'},
    {time: '2026-10-28T09:01:00+08:00', price: '879'},
    {time: '2026-10-28T12:00:00+08:00', price: '1'}]}};
const goldChart = stockSparkline(gold, {collection_phase: 'night'}, Date.parse('2026-10-28T09:02:00+08:00'));
assert.ok(goldChart.includes('<svg'));
assert.ok(goldChart.includes('stock-trend-pulse'));
assert.ok(!goldChart.includes('NaN'));
""".replace('SOURCE', source)
    result = subprocess.run([node, '-e', script], capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stdout + result.stderr


def test_stock_group_titles_show_market_session_status():
    html = _read_page()
    assert "stock-group-status" in html
    assert "indices_status" in html
    for label in ("交易中", "午间休市", "已收盘"):
        assert label in html


def test_stock_cards_drop_per_quote_time_and_title_shows_single_update_time():
    html = _read_page()
    # 指数卡片不再各显示自己的报价时间；统一的本轮更新时间放在股市行情标题右侧。
    assert '<div class="stock-index-time">${esc(description)}</div>' not in html
    assert "stockMarketMeta" in html
    assert "fetched_at" in html
    assert ' · 收盘保留' not in html


def test_estimate_list_shows_single_market_time_beside_title():
    html = _read_page()
    # 预估列表的行情时间统一放“持仓涨跌幅预估”标题右侧，不在每只产品下重复。
    assert 'id="estimateMeta"' in html
    assert 'estimateMeta' in html
    assert '行情 ${String(estimate.market_as_of)' not in html
    assert '预估涨跌幅 · 行情 ${latestEstimateTime}' not in html


def test_market_panel_cards_keep_even_gap_across_notice_element():
    html = _read_page()
    # 股市与债市之间隔着 write-notice，用通用兄弟选择器保证卡片间距一致。
    assert "#marketPanel > .card.section ~ .card.section { margin-top: 14px; }" in html


def test_verification_section_renders_stats_and_optional_ai_note():
    html = _read_page()
    assert 'id="verificationSection"' in html
    assert 'id="verificationContent"' in html
    assert "renderVerification" in html
    assert "/api/portfolio/fund-estimate-verifications" in html


@pytest.mark.parametrize('status', [304, 200])
def test_verification_refreshes_when_market_payload_is_unchanged(status):
    _run_continuity_behavior(r"""
const stock = {available: true, indices: [{name: '上证指数', market: 'A股', price: '3300'}]};
renderStockMarket(stock);
const payload = {stock_market: stock};
lastMarketPayload = JSON.stringify(payload);
handler = async path => path.includes('market-overview') ? {status: STATUS, ok: STATUS === 200,
  headers: {get: () => 'same'}, json: async () => payload} : response(report());
await loadMarketOverview(); await flush();
assert.equal(calls.filter(call => call.path.includes('verifications')).length, 1);
assert.equal(marketLoading, false);
""".replace('STATUS', str(status)))


def test_verification_daily_rows_have_names_three_decimals_and_no_minute_records():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for dashboard behavior checks')
    html = _read_page()
    start = html.index('function verificationPercent(') if 'function verificationPercent(' in html else html.index('function renderVerification(')
    source = html[start:html.index('async function loadVerification(')]
    script = r"""
const assert = require('node:assert/strict');
const box = {innerHTML: '', querySelectorAll: () => [], querySelector: () => null};
const meta = {textContent: ''};
const $ = id => id === 'verificationContent' ? box : meta;
const restoreSkeleton = () => {};
const markSkeletonReady = () => {};
const esc = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
const tone = value => Number(value) > 0 ? 'positive' : Number(value) < 0 ? 'negative' : 'neutral';
SOURCE
const row = {product_id: 'p', code: '918273', name: '测试纯债A', trade_date: '2026-10-23',
  snapshot_generated_at: '2026-10-23T17:29:00+08:00', predicted_pct: '0.08', realized_pct: '0.1',
  error_pct: '-0.020000000000000000000001', direction_ok: true};
renderVerification({items: [row], stats: {pairs: 1, predicted_count: 1, direction_hits: 1,
  bias_pct: row.error_pct, mae_pct: '0.020000000000000000000001'},
  snapshot_items: [row, {...row, predicted_pct: null}], ai_analysis: '每日最终结果分析'});
assert.equal((box.innerHTML.match(/class="verification-fund"/g) || []).length, 1);
assert.ok(box.innerHTML.includes('测试纯债A'));
for (const value of ['+0.080%', '+0.100%', '0.020%']) assert.ok(box.innerHTML.includes(value), value);
assert.ok(!box.innerHTML.includes('000000000000000000001'));
assert.ok(!box.innerHTML.includes('缺预估'));
assert.ok(!box.innerHTML.includes('平均偏差'));
assert.ok(!box.innerHTML.includes('<details'));
assert.ok(!box.innerHTML.includes('分钟记录'));
assert.ok(!box.innerHTML.includes('每天每只基金仅计一次'));
assert.ok(box.innerHTML.includes('累计分析'));
assert.ok(box.innerHTML.includes('😐'));
assert.ok(meta.textContent.includes('1只基金'));
assert.equal(verificationPercent('-0.0000001', true), '0.000%');
renderVerification({items: [{...row, direction_ok: false}], stats: {pairs: 1,
  predicted_count: 1, direction_hits: 0, mae_pct: '0.02'}});
assert.ok(box.innerHTML.includes('🙁'));
renderVerification({items: [{...row, error_pct: '0.0072', direction_ok: true}], stats: {pairs: 1,
  predicted_count: 1, direction_hits: 1, mae_pct: '0.0072'}});
assert.ok(box.innerHTML.includes('🙂'));
const history = {...row, trade_date: '2026-10-22', predicted_pct: '0.24',
  error_pct: '0.14', direction_ok: false};
const cumulative = {items: [history, row], stats: {pairs: 2, predicted_count: 2,
  direction_hits: 1, mae_pct: '0.08'}, overall: {day_count: 2, fund_count: 1,
  start_date: '2026-10-22', end_date: '2026-10-23', summary: '累计覆盖全部日期。'}};
renderVerification(cumulative);
assert.ok(box.innerHTML.includes('50.000%'));
assert.ok(box.innerHTML.includes('0.080%'));
assert.equal((box.innerHTML.match(/class="verification-fund"/g) || []).length, 1);
renderVerification.selectedDate = '2026-10-22';
renderVerification(cumulative);
assert.ok(box.innerHTML.includes('50.000%'));
assert.ok(box.innerHTML.includes('0.080%'));
assert.ok(box.innerHTML.includes('+0.240%'));
assert.ok(!box.innerHTML.includes('+0.080%'));
""".replace('SOURCE', source)
    result = subprocess.run([node, '-e', script], capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stdout + result.stderr


def test_selected_logo_is_shared_by_header_and_browser_icon():
    html = _read_page()
    asset = "/nav-assets/private-wealth-logo.png"
    assert f'<link rel="icon" type="image/png" href="{asset}">' in html
    header = html[html.index('<header class="topbar">'):html.index('</header>')]
    assert '<div class="app-brand">' in header
    assert (
        f'<img class="brand-logo" src="{asset}" width="38" height="38" '
        'alt="" aria-hidden="true">'
    ) in header
    assert header.index('class="brand-logo"') < header.index('<h1>理财组合看板</h1>')
    assert ".app-brand { display: flex; align-items: center; gap: 4px; min-width: 0; }" in html
    assert ".brand-logo { width: 38px; height: 38px; flex: 0 0 38px; object-fit: contain; }" in html
    assert ".app-brand h1 { white-space: nowrap; }" in html


def test_page_has_four_management_tabs_and_quick_trade():
    html = _read_page()
    for element_id in (
        "overviewTab", "positionsTab", "transactionsTab", "sipTab",
        "overviewPanel", "positionsPanel", "transactionsPanel", "sipPanel",
        "quickTradeButton", "tradeDialog",
        "confirmDialog", "sipDialog", "adjustmentDialog",
    ):
        assert f'id="{element_id}"' in html


def test_overview_trade_button_sits_beside_asset_summary_title():
    html = _read_page()
    overview = html[html.index('id="overviewPanel"'): html.index('id="positionsPanel"')]
    assert "<h2>组合总览</h2>" not in overview
    assets = overview[overview.index("资产概况"): overview.index("overview-grid")]
    assert 'id="quickTradeButton"' in assets
    assert assets.index("资产概况") < assets.index("quickTradeButton") < assets.index("pendingStatus")
    assert ".section-head .section-meta { margin-left: auto; }" in html


def test_section_trade_buttons_use_compact_action_style():
    html = _read_page()
    assert (
        ".section-head .action-button, .panel-heading .action-button "
        "{ min-height: 28px; padding: 0 10px; border-radius: 6px; font-size: 12px; font-weight: 700; }"
    ) in html
    assert "#tradeDialog .dialog-actions .action-button { min-height: 44px; }" in html


def test_position_group_cards_drop_header_rule_and_space_products():
    html = _read_page()
    assert ".manage-card > .section-head { border-bottom: none; padding-bottom: 8px; }" in html
    assert ".manage-card > .manage-list { margin-top: 10px; gap: 12px; }" in html
    assert (
        ".manage-card > .manage-list > .manage-card "
        "{ border: 1px solid var(--line); box-shadow: none; background: var(--panel); }"
    ) in html
    assert '<div class="manage-list">${groupRows.map' in html
    assert "}).join('')}</div></section>`" in html


def test_page_scrollbar_stays_between_topbar_and_bottom_tabs():
    html = _read_page()
    assert "html, body { height: 100%; overflow: hidden; }" in html
    tabs = html[html.index(".management-tabs {"): html.index(".management-tabs {") + 420]
    assert "position: relative;" in tabs
    assert "order: 3;" in tabs
    assert "flex: 0 0 auto;" in tabs
    assert "backdrop-filter: saturate(180%) blur(18px);" in tabs
    start = html.index(".management-panel:not([hidden])")
    block = html[start: start + 420]
    assert "order: 2;" in block
    assert "overflow-y: auto;" in block
    assert "padding-top: calc(12px + 54px" not in block
    assert "padding-bottom: calc(16px + 54px" not in block


def test_bars_keep_frosted_style_and_reserve_layout_space():
    html = _read_page()
    start = html.index(".topbar {")
    block = html[start: html.index(".top-actions")]
    assert "position: relative;" in block
    assert "flex: 0 0 auto;" in block
    assert "backdrop-filter: saturate(180%) blur(18px);" in block
    assert "background: rgba(255, 255, 255, 0.66);" in block
    assert "#dashboardContent" in html and "position: relative;" in html[html.index("#dashboardContent"): html.index("#dashboardContent") + 180]
    tabs = html[html.index(".management-tabs {"): html.index(".management-tabs {") + 420]
    assert "position: relative;" in tabs
    assert "order: 3;" in tabs
    assert "backdrop-filter: saturate(180%) blur(18px);" in tabs
    assert "background: rgba(255, 255, 255, 0.66);" in tabs


def test_active_management_tab_survives_page_and_data_refresh():
    html = _read_page()

    assert "const ACTIVE_TAB_STORAGE_KEY = 'nav-dashboard-active-tab';" in html
    assert "function readStoredActiveTab()" in html
    assert "activeTab: readStoredActiveTab()," in html
    assert "sessionStorage.setItem(ACTIVE_TAB_STORAGE_KEY, tab);" in html
    assert "setActiveTab(state.activeTab);" in html


def test_management_tabs_support_horizontal_swipe_navigation():
    html = _read_page()

    assert "let managementSwipeStart = null;" in html
    assert "addEventListener('touchstart'" in html
    assert "addEventListener('touchend'" in html
    assert "Math.abs(deltaX) < 60" in html
    assert "setActiveTab(MANAGEMENT_TABS[nextIndex]);" in html


def test_page_title_uses_quiet_app_bar_style():
    html = _read_page()
    start = html.index(".topbar {")
    block = html[start: html.index(".top-actions")]
    assert "align-items: center;" in block
    assert "position: relative;" in block
    assert "background: rgba(255, 255, 255, 0.66);" in block
    assert "backdrop-filter: saturate(180%) blur(18px);" in block
    assert "min-height: 54px;" in block
    assert "border-bottom: 1px solid rgba(215, 228, 242, 0.55);" in block
    assert ".management-tabs" in html and "background: rgba(255, 255, 255, 0.66);" in html
    assert ".management-tab" in html and "min-height: 54px;" in html
    assert ".management-panel { padding: 12px 14px 16px; }" in html
    title = html[html.index("h1 { margin: 0;"): html.index("h1 { margin: 0;") + 160]
    assert "font-size: 17px;" in title
    assert "font-weight: 700;" in title
    assert "h1 { font-size: 25px; }" not in html


def test_refresh_countdown_stays_visible_on_narrow_phones():
    html = _read_page()
    start = html.index("@media (max-width: 370px)")
    block = html[start: html.index("</style>", start)]
    assert ".updated { display: none; }" not in block
    assert 'id="updatedAt"' in html
    assert "后可刷新" in html


def test_dashboard_uses_fangsong_font_stack():
    html = _read_page()
    assert 'href="/nav-assets/fonts/fz-fangsong/font.css"' in html
    assert 'font-family: "FZFangSong-Z02S"' in html


def test_section_head_titles_use_smaller_weight_than_hero():
    html = _read_page()
    assert ".section-head h2 { font-size: 16px; font-weight: 700; }" in html
    assert ".panel-heading h2 { font-size: 16px; font-weight: 700; }" in html


def test_hero_cumulative_profit_keeps_amount_and_yuan_on_one_line():
    html = _read_page()
    start = html.index(".total {")
    block = html[start: start + 280]
    assert "white-space: nowrap;" in block
    # 首屏数值改为骨架占位，金额与“元”单位由 countUp 格式化器渲染。
    assert 'id="cumulativeProfit"' in html
    assert '<span class="unit">元</span>' in html


def test_management_tabs_are_sticky_bottom_with_icons():
    html = _read_page()

    assert 'id="overviewTab"' in html
    assert 'id="positionsTab"' in html
    assert 'id="transactionsTab"' in html
    assert 'id="sipTab"' in html
    # 贴底全宽栏，不再使用毛玻璃悬浮样式。保留 indicator 节点
    # 仅为了兼容旧代码引用（不再渲染可见滑块）。
    assert ".management-tabs" in html
    assert "position: fixed" in html
    assert 'bottom: 0' in html
    # 每个 tab 内嵌 SVG 图标
    for tab in ("overviewTab", "positionsTab", "transactionsTab", "sipTab"):
        marker = f'id="{tab}"'
        idx = html.index(marker)
        snippet = html[idx:idx + 220]
        assert "<svg" in snippet, f"{tab} 应包含 SVG 图标"


def test_write_fetch_uses_private_token_idempotency_and_custom_header():
    html = _read_page()
    assert '"X-Nav-Dashboard-Key": token' in html
    assert '"X-Portfolio-Request": "1"' in html
    assert '"Idempotency-Key": idempotencyKey' in html
    assert "/api/portfolio/transactions/preview" in html
    assert "/api/portfolio/sip-plans/preview" in html


def test_sip_save_uses_the_shared_preview_confirm_pending_action_flow():
    html = _read_page()
    assert (
        "previewPath: '/api/portfolio/sip-plans/preview', "
        "submitPath: '/api/portfolio/sip-plans'"
    ) in html
    assert (
        "requestJson('/api/portfolio/sip-plans', payload, newIdempotencyKey())"
        not in html
    )


def test_read_only_state_disables_dialog_writes_and_load_failure_closes_writes():
    html = _read_page()
    assert (
        "'.management-dialog input, .management-dialog select, "
        ".management-dialog textarea, .management-dialog "
        "button:not([data-close-dialog])'"
    ) in html
    assert "function disableWrites(reason)" in html
    assert "disableWrites('组合数据读取失败，写操作已关闭。')" in html
    assert "if (!state.writeEnabled) return;" in html
    # 首屏骨架阶段不显示只读提示，只有拿到数据且不可写时才显示。
    assert "notice.hidden = state.writeEnabled || !state.data;" in html


def test_preview_and_submit_share_one_idempotency_key_from_the_first_request():
    html = _read_page()
    assert "const idempotencyKey = newIdempotencyKey();" in html
    assert "requestJson(previewPath, payload, idempotencyKey)" in html
    assert (
        "state.pendingAction = {submitPath, payload: submitPayload, "
        "idempotencyKey, originDialog"
    ) in html


def test_reverse_operation_is_not_exposed_in_the_dashboard():
    html = _read_page()
    assert 'id="reverseReasonField"' not in html
    assert 'data-write-operation="reverse"' not in html
    assert "function openReverse(transactionId)" not in html


def test_sip_save_is_draft_and_activation_is_an_explicit_operation():
    html = _read_page()
    # 服务器契约：激活 SIP 显式携带 activate: true，暂停/恢复/激活均为独立写操作。
    assert "activate: true" in html
    assert "data-write-operation=\"sip-activate\"" in html
    assert "if (existingStatus !== 'draft')" not in html
    assert "sip_id: form.dataset.sipId || undefined" in html
    assert 'name="start_date"' in html
    assert "form.start_date.value = rowValue(existing, 'start_date');" in html
    assert "syncSipScheduleFields();\n      syncSipStartDate();\n      dialogOpen('sipDialog');" in html


def test_sip_plan_can_be_deleted_after_preview():
    html = _read_page()

    assert 'data-write-operation="sip-delete"' in html
    assert "operation === 'sip-delete'" in html
    assert "operation.replace('sip-', '')" in html


def test_sip_card_hides_internal_skip_reason():
    html = _read_page()

    assert "跳过原因：" not in html


def test_sip_fee_rate_field_and_display_use_percent_units():
    html = _read_page()

    assert "申购费率（%）" in html
    assert "'purchase_fee_rate_percent'" in html
    assert "purchase_fee_rate_percent') || '--')}%" in html


def test_all_position_shares_show_two_decimal_places():
    html = _read_page()

    assert "const sharesDisplay = money(shares);" in html
    assert "const availableSharesDisplay = money(availableShares);" in html
    assert "const availableShares = rowValue(row, 'available_shares');" in html
    assert "const inTransitAmount = rowValue(row, 'in_transit_amount');" in html
    assert "<span>可用份额</span>" in html
    assert "<span>在途资金</span>" in html


def test_position_card_merges_quote_date_and_change_below_non_cash_nav():
    html = _read_page()

    assert ".manage-stat > span { display: block;" in html
    assert ".manage-stat span { display: block;" not in html
    assert (
        "const quoteDate = row.quote?.date || "
        "rowValue(row, 'nav_date', 'quote_date');"
    ) in html
    assert 'class="change-value ${tone(changePct)}"' in html
    assert "signedPercent(changePct)" in html
    assert "<span>净值日期</span>" not in html
    assert "<span>最新收益</span>" in html


def test_overview_redemption_status_uses_pending_shares_and_unsettled_amount():
    html = _read_page()

    assert "赎回待确认 ${money(pendingRedemptionShares)} 份" in html
    assert "赎回待到账 ${money(unsettledRedemptionAmount)} 元" in html


def test_latest_profit_adjustment_has_a_separate_button_and_no_date_input():
    html = _read_page()

    assert 'id="latestProfitAdjustmentDialog"' in html
    assert 'data-write-operation="adjust-latest-profit"' in html
    assert "/api/portfolio/latest-profit-adjustments/preview" in html
    dialog = html[
        html.index('id="latestProfitAdjustmentDialog"'):
        html.index('</dialog>', html.index('id="latestProfitAdjustmentDialog"'))
    ]
    assert 'name="effective_date"' not in dialog
    assert "rowValue(product, 'latest_profit_date')" in html


def test_non_nav_amounts_and_shares_use_two_decimal_display():
    html = _read_page()

    assert "function previewFieldRequiresTwoDecimals(path)" in html
    assert "return money(text);" in html
    assert "const quantity = Number.isFinite(number(match[3]))" in html
    assert "<span>${sharesLabel}</span><b>${esc(money(shares))} 份</b>" in html
    assert (
        "form.amount.value = number("
        "rowValue(existing, 'amount', 'daily_amount')).toFixed(2);"
    ) in html
    assert (
        '<div class="manage-value">${money('
        "rowValue(row, 'amount', 'daily_amount') || 0)} 元</div>"
    ) in html


def test_wallet_pending_transaction_uses_realtime_remaining_shares():
    html = _read_page()

    assert "rowValue(row, 'remaining_confirmation_shares')" in html
    assert "实时剩余确认份额" in html


def test_wallet_overview_uses_remaining_in_transit_and_preserves_zero():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for JavaScript behavior checks")
    html = _read_page()
    helper = html[html.index("function pendingPurchaseRemainingAmount("):html.index("function transactionStatusLabel(")]
    overview = html[html.index("const portfolioProduct = pfList.find("):html.index("const pendingRedemptionShares = pfTxList.filter(")]
    script = r"""
const assert = require('assert');
const rowValue = (row, ...keys) => {for (const key of keys) if (row?.[key] !== undefined && row[key] !== null) return row[key]; return '';};
const number = value => Number(value) || 0;
const normalStatus = value => value.startsWith('pending') ? 'pending' : value;
""" + helper + "\nfunction overviewAmount(pfList, pfTxList, pid) {\n" + overview + "\nreturn pendingBuyAmt;\n}\n" + r"""
const transactions = [
  {product_id:'wallet-plus',status:'pending_confirmation',transaction_type:'manual_purchase',amount:'108000',remaining_confirmation_shares:'55800'},
  {product_id:'wallet-plus',status:'pending_confirmation',transaction_type:'manual_purchase',amount:'54015',remaining_confirmation_shares:'54015'}
];
assert.strictEqual(overviewAmount([{product_id:'wallet-plus',in_transit_amount:'109815'}], transactions, 'wallet-plus'),109815);
assert.strictEqual(overviewAmount([{product_id:'wallet-plus',in_transit_amount:'0'}], transactions, 'wallet-plus'),0);
assert.strictEqual(overviewAmount([], transactions, 'wallet-plus'),109815);
assert.strictEqual(pendingPurchaseRemainingAmount({amount:'108000',remaining_confirmation_shares:'0'}),0);
assert.strictEqual(pendingPurchaseRemainingAmount({amount:'50000',remaining_confirmation_shares:null}),50000);
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_position_detail_opens_a_real_dialog():
    html = _read_page()

    assert 'id="productDetailDialog"' in html
    assert 'id="productDetailContent"' in html
    # 详情入口位于净值显示模块，以“更多”文字链呈现；操作行不再保留详情按钮。
    assert 'class="stat-more" type="button" data-view-product="${esc(id)}" aria-label="更多，查看持仓详情">更多</button>' in html
    assert 'data-view-product="${esc(id)}">详情</button>' not in html
    assert "async function openProductDetail(productId)" in html
    assert "`/api/portfolio/products/${encodeURIComponent(productId)}/history`" in html
    assert "净值日期" in html
    assert "单位净值" in html
    assert "涨跌幅" in html
    assert "当日收益" in html
    assert "dialogOpen('productDetailDialog');" in html
    assert "grid-template-columns: 82px repeat(3, minmax(0, 1fr));" in html
    assert ".product-history-head span, .product-history-row span { min-width: 0; white-space: nowrap; }" in html
    assert "align-items: center; gap: 2px; padding: 9px 4px;" in html
    assert "background: var(--blue-soft); font-size: 9px;" in html
    assert ".product-history-row { min-height: 42px; border-top: 1px solid var(--line-soft); font-size: 10px;" in html
    assert ".product-history { margin-top: 0; max-height: min(58vh, 460px);" in html
    assert "overflow-x: hidden; overflow-y: auto;" in html
    assert "position: sticky; top: 0; z-index: 1;" in html
    assert "font-variant-numeric: tabular-nums; text-align: center;" in html
    assert ".product-history-head span:first-child" not in html
    assert ".product-history-head span:nth-child(2)" not in html
    assert "const summary = fields.map" not in html
    assert "$('productDetailContent').innerHTML = `<div class=\"product-history\">" in html


def test_dividend_action_is_fund_only_and_follows_product_actions():
    html = _read_page()
    positions = html[
        html.index("function renderPositions()"):
        html.index("function normalStatus(value)")
    ]

    assert "const dividendAction = group === 'public_fund'" in positions
    assert "rowValue(row, 'dividend_upcoming_count')" in positions
    assert 'class="dividend-alert-dot"' in positions
    # 详情入口已移到净值显示模块的“更多”文字链，操作行末位是分红按钮。
    assert '校准最新收益</button>${dividendAction}</div>' in positions
    assert 'data-view-product="${esc(id)}">详情</button>' not in positions
    assert 'class="stat-more" type="button" data-view-product="${esc(id)}"' in positions
    assert 'data-view-dividends="${esc(id)}"' in positions


def test_dividend_panel_shows_modes_events_and_uses_safe_rendering():
    html = _read_page()

    assert 'id="fundDividendDialog"' in html
    # 基金名称以标题栏小字呈现，正文不再保留“当前基金”块。
    assert 'id="fundDividendDialogTitle">基金分红 <small id="fundDividendProductName">' in html
    assert "当前基金" not in html
    assert ".dialog-head h2 small { margin-left: 6px; color: var(--muted); font-size: 12px; font-weight: 600; }" in html
    assert 'type="radio" name="dividend_mode" value="cash" checked' in html
    assert 'type="radio" name="dividend_mode" value="reinvest"' in html
    assert "现金分红" in html
    assert "红利再投" in html
    # 说明性提示已移除，不再占用弹窗空间。
    assert "仅影响尚未登记的分红记录" not in html
    assert "不会向银行或基金销售平台发送真实分红指令" not in html
    # 净值模块两行布局：净值日并入标题括号，第二行净值+涨跌幅+更多。
    assert "单位净值（${esc(shortDate(quoteDate) || '--')}）" in html
    # 抓取失败的多为瞬时抖动，弹窗不再展示该类提示。
    assert "setFundDividendStatus(serviceError" not in html
    # 分红列表不再自带滚动条，避免与弹窗主体形成嵌套滚动。
    assert ".fund-dividend-list { display: grid; gap: 8px; padding: 1px 2px 2px 0; }" in html
    assert "async function openFundDividends(productId)" in html
    assert "encodeURIComponent(productId)}/dividends" in html
    assert "encodeURIComponent(productId)}/dividend-mode" in html
    assert "function safeExternalUrl(value)" in html
    assert "parsed.protocol !== 'http:' && parsed.protocol !== 'https:'" in html
    assert "['登记日', rowValue(event, 'record_date')]" in html
    assert "[isReinvest ? '确认日' : '派现日', rowValue(event, isReinvest ? 'reinvest_confirmation_date' : 'payment_date')]" in html
    assert "fundDividendField(value)" in html
    assert "${esc(error.message || '读取分红记录失败')}" in html
    assert ".fund-dividend-list" in html


def test_dividend_panel_separates_historical_records_and_uses_mode_specific_date():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for JavaScript behavior checks")
    html = _read_page()
    event_renderer = html[
        html.index("function renderFundDividendEvent("):
        html.index("function renderFundDividendRecords(")
    ]
    records_renderer = html[
        html.index("function renderFundDividendRecords("):
        html.index("async function loadFundDividendRecords(")
    ]
    script = r"""
const assert = require('assert');
const dom = {fundDividendContent: {innerHTML: ''}};
const $ = id => dom[id] || (dom[id] = {});
const rowValue = (row, ...keys) => {
  for (const key of keys) if (row?.[key] !== undefined && row[key] !== null) return row[key];
  return '';
};
const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const safeExternalUrl = () => '';
const fundDividendField = value => value === undefined || value === null || value === '' ? '--' : esc(value);
const number = value => { const parsed = Number(value); return Number.isFinite(parsed) ? parsed : 0; };
const setFundDividendStatus = () => {};
""" + event_renderer + records_renderer + r"""
const reinvest = renderFundDividendEvent({mode:'reinvest', status:'announced', payment_date:'2030-01-15', reinvest_confirmation_date:null});
assert.match(reinvest, /确认日/);
assert.match(reinvest, /<b>--<\/b>/);
assert.doesNotMatch(reinvest, /派现日|2030-01-15/);
const reinvestConfirmed = renderFundDividendEvent({mode:'reinvest', status:'announced', payment_date:'2030-01-15', reinvest_confirmation_date:'2030-01-18'});
assert.match(reinvestConfirmed, /确认日/);
assert.match(reinvestConfirmed, /2030-01-18/);
assert.doesNotMatch(reinvestConfirmed, /派现日|2030-01-15/);
const cash = renderFundDividendEvent({mode:'cash', status:'announced', payment_date:'2030-01-15'});
assert.match(cash, /派现日/);
assert.match(cash, /2030-01-15/);
// 未在本平台入账的公开历史：只保留日期与每份分红，不含金额/再投份额/登记方式。
const publicHistory = renderFundDividendEvent({mode:'cash', status:'display_only', display_only:true, per_share:'0.0082', record_date:'2026-01-20', ex_dividend_date:'2026-01-20', payment_date:'2026-01-22'});
assert.match(publicHistory, /10派¥0\.0820/);
assert.match(publicHistory, /公开历史/);
assert.match(publicHistory, /2026-01-20/);
assert.doesNotMatch(publicHistory, /分红金额|再投份额|登记方式/);
renderFundDividendRecords({events:[
  {status:'announced', is_upcoming:true, historical:false, amount:'101', mode:'cash'},
  {status:'blocked', is_upcoming:false, historical:true, amount:'202', mode:'reinvest', error:'历史公告'},
  {status:'settled', is_upcoming:false, historical:true, amount:'303', mode:'reinvest'},
  {status:'display_only', display_only:true, historical:false, per_share:'0.0082', record_date:'2026-01-20', ex_dividend_date:'2026-01-20', payment_date:'2026-01-22'}
]});
const html = $('fundDividendContent').innerHTML;
const upcoming = html.slice(html.indexOf('即将分红'), html.indexOf('待核实'));
const historical = html.slice(html.indexOf('待核实'), html.indexOf('历史分红'));
const completed = html.slice(html.indexOf('历史分红'));
assert.match(upcoming, /<b>101<\/b>/);
assert.doesNotMatch(upcoming, /<b>202<\/b>|<b>303<\/b>|公开历史/);
assert.match(historical, /<b>202<\/b>/);
assert.doesNotMatch(historical, /<b>101<\/b>|<b>303<\/b>|公开历史/);
assert.match(completed, /<b>303<\/b>/);
assert.match(completed, /公开历史/);
assert.match(completed, /10派¥0\.0820/);
assert.doesNotMatch(completed, /<b>101<\/b>|<b>202<\/b>/);
// 空区块不占位：全部为空时只显示一行提示。
renderFundDividendRecords({events:[]});
const emptyHtml = $('fundDividendContent').innerHTML;
assert.doesNotMatch(emptyHtml, /即将分红|待核实|历史分红/);
assert.match(emptyHtml, /暂无分红记录/);
// 只有已完成记录时，其余区块不渲染。
renderFundDividendRecords({events:[{status:'settled', historical:true, amount:'303'}]});
const settledOnly = $('fundDividendContent').innerHTML;
assert.match(settledOnly, /已完成/);
assert.doesNotMatch(settledOnly, /即将分红|待核实/);
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr


def test_stale_product_detail_request_cannot_overwrite_newer_dialog():
    html = _read_page()

    assert "let productDetailRequestId = 0;" in html
    assert "const requestId = ++productDetailRequestId;" in html
    assert "if (requestId !== productDetailRequestId) return;" in html


def test_change_percent_uses_independent_red_up_green_down_tone_and_two_decimals():
    html = _read_page()

    assert "number(value).toFixed(2)" in html
    assert ".change-value.positive { color: var(--positive); }" in html
    assert ".change-value.negative { color: var(--negative); }" in html
    assert 'class="change change-value ${tone(item.change_pct)}"' in html


def test_cash_overview_shows_fixed_per_ten_thousand_income_in_nav_column():
    html = _read_page()

    assert "const cashIncome = rowValue(item, 'per_10k_yield')" in html
    assert "`万份 ${number(cashIncome).toFixed(4)}`" in html


def test_adjustment_values_and_share_step_use_two_decimals():
    html = _read_page()

    assert 'name="shares" type="number" min="0" step="0.01"' in html
    assert (
        "form.elements.shares.value = number("
        "rowValue(product, 'shares', 'share')).toFixed(2);"
    ) in html
    assert (
        "form.elements.profit.value = number("
        "rowValue(product, 'holding_profit', 'cumulative_profit')).toFixed(2);"
    ) in html


def test_products_are_added_inline_from_trade_or_sip_dialog_only():
    html = _read_page()

    assert 'data-write-action="product"' not in html
    assert 'id="productDialog"' not in html
    assert 'id="productForm"' not in html
    assert 'name="new_fund_code"' in html
    assert 'name="new_fund_code" id="tradeFundCode" type="hidden"' in html
    assert 'name="new_fund_code" id="sipFundCode" type="hidden"' in html
    assert "async function ensureInlineFund" in html
    assert "await ensureInlineFund" in html
    assert "fillProductSearch('sip', 'public_fund')" in html


def test_trade_and_sip_use_one_searchable_product_field():
    html = _read_page()

    assert 'id="tradeProductQuery" role="combobox"' in html
    assert 'id="tradeProductSuggestions"' in html
    assert 'id="sipProductQuery" role="combobox"' in html
    assert 'id="sipProductSuggestions"' in html
    assert '<select name="product_id" id="tradeProduct">' not in html
    assert '<select name="product_id" id="sipProduct">' not in html
    assert "function fillProductSearch(scope, productType)" in html
    assert "function syncProductSearch(scope, productType, complete = false)" in html
    assert "code.includes(query) || name.includes(query)" in html
    assert "function productSearchProviderLabel(row)" in html
    assert "nanyin_wealth: '南银理财'" in html
    assert "citic_wealth: '信银理财'" in html
    assert "provider.includes(query)" in html
    assert "syncProductSearch('trade', $('tradeProductType').value, true);" in html
    assert "syncProductSearch('sip', 'public_fund', true);" in html


def test_unknown_six_digit_fund_code_resolves_name_before_submit():
    html = _read_page()

    assert 'id="tradeProductSearchStatus"' in html
    assert 'id="sipProductSearchStatus"' in html
    assert "async function resolvePublicFundSearch(scope)" in html
    assert "function schedulePublicFundSearch(scope, delay = 350)" in html
    assert "'/api/portfolio/products/preview'" in html
    assert "product_type: 'public_fund'" in html
    assert "已识别：${identity.name}（${identity.code}）" in html
    assert "input.dataset.resolvedFundCode = identity.code;" in html
    assert "const fundSearchPreviewCache = new Map();" in html
    assert "fundSearchPreviewCache.get(code) || await requestJson(" in html


def test_partial_code_and_chinese_name_search_external_product_candidates():
    html = _read_page()

    assert "'/api/portfolio/products/search'" in html
    assert "function scheduleProductCandidateSearch(scope" in html
    assert "async function searchProductCandidates(scope)" in html
    assert "query.length < 2" in html
    assert "productSearchRemoteRows[scope]" in html
    assert "scheduleProductCandidateSearch('trade')" in html
    assert "scheduleProductCandidateSearch('sip')" in html


def test_product_candidates_use_wide_custom_panel_with_full_names():
    html = _read_page()

    assert 'class="product-suggestion-panel"' in html
    assert 'role="listbox"' in html
    assert "width: min(460px, calc(100vw - 56px))" in html
    assert ".product-suggestion-name" in html
    assert "white-space: normal" in html
    assert "font-size: 12px" in html
    assert "function selectProductSuggestion(scope, index)" in html
    assert "data-product-suggestion-index" in html
    assert "function handleProductSearchKeydown(scope, event)" in html


def test_product_create_preserves_preview_identity_for_submit():
    html = _read_page()
    # 单弹窗添加基金时，仍将预览返回的完整身份带入产品提交。
    assert "const previewIdentity = previewData.product || null" in html
    assert "submitPayload = {...payload, ...previewIdentity" in html
    assert "const identity = preview.product;" in html
    assert "identity_fingerprint: preview.identity_fingerprint" in html
    assert "registration_code" in html


def test_management_dialogs_do_not_render_note_or_description_fields():
    html = _read_page()
    assert "payload: {operation: 'disable', product_id: control.dataset.productId}" in html
    assert '<textarea name="note"' not in html
    trade_submit = html[html.index("$('tradeForm').addEventListener"):html.index(
        "$('adjustmentForm').addEventListener"
    )]
    assert "formPayload(event.currentTarget)" in trade_submit
    assert "await ensureInlineFund" in trade_submit


def test_holding_css_rule_exists():
    page = _read_page()
    assert ".holding" in page
    assert "font-variant-numeric: tabular-nums" in page
    # holding 跨列对齐名称左边缘（从 grid column 2 到末尾）
    assert "grid-column: 2 / -1" in page


def test_product_template_contains_holding_node():
    page = _read_page()
    assert '<div class="holding">' in page
    assert "holding" in page
    # holding 在 .income 之后、</article> 之前（article 级别子节点）
    assert "</div>${(() => { const h = formatHolding" in page or "</div>${(() => {" in page


def test_overview_count_uses_merged_root_product_list_without_double_counting():
    page = _read_page()
    assert "const totalConfigured = (data.products || []).length;" in page
    assert "(data.configured_count || 0) + pfProducts.length" not in page


def test_trade_form_switches_between_purchase_amount_and_redemption_shares():
    page = _read_page()
    assert 'id="tradeProductType"' in page
    assert '<option value="wealth_nav">理财</option>' in page
    assert '<option value="cash_management">现金</option>' in page
    assert '<option value="public_fund">公募基金</option>' in page
    assert 'id="tradeKind"' in page
    assert 'id="tradeValueLabel"' in page
    assert 'id="tradeValueInput"' in page
    assert 'name="trade_time"' in page
    assert "input.name = redemption ? 'shares' : 'amount';" in page
    assert "input.step = '0.01';" in page
    assert "input.min = '0.01';" in page
    assert "$('tradeValueTitle').textContent = redemption ? '赎回份额' : '申购金额（元）';" in page
    assert "$('tradeTimeTitle').textContent = redemption ? '赎回时间' : '申购时间';" in page


def test_trade_dialog_keeps_actions_visible_and_scrolls_only_the_form_body():
    page = _read_page()

    assert "#tradeDialog { height: min(calc(100dvh - 24px), 680px);" in page
    assert "#tradeDialog { height: min(calc(100vh - 24px), 680px); max-height: min(calc(100vh - 24px), 680px); overflow: hidden; }" in page
    assert "#tradeDialog > form { display: flex; flex: 1 1 auto; flex-direction: column; min-height: 0; width: 100%; }" in page
    assert "#tradeDialog .dialog-body { flex: 1 1 auto; min-height: 0; overflow-x: hidden; overflow-y: auto;" in page
    assert "#tradeDialog .form-field { min-width: 0; max-width: 100%; }" in page
    assert 'class="date-input-shell"' in page
    assert 'id="tradeTimeTitle">申购时间<' in page
    assert ".date-input-shell input { width: 100%; min-width: 0; max-width: 100%;" in page
    assert "-webkit-appearance: none;" in page
    assert "#tradeDialog .dialog-actions { flex: 0 0 auto; margin: 0;" in page
    assert '<footer class="dialog-actions"><button class="action-button secondary" type="button" data-close-dialog="tradeDialog">取消</button><button class="action-button" type="submit">预览</button></footer>' in page


def test_trade_dialog_locks_background_scroll_on_touch():
    page = _read_page()
    assert "function updatePageScrollLock()" in page
    assert "body.classList.add('page-locked')" in page
    assert "html.classList.add('page-locked')" in page
    assert "html.page-locked, body.page-locked { overflow: hidden; overscroll-behavior: none; }" in page
    assert "body.page-locked { position: fixed; width: 100%; left: 0; right: 0; }" in page
    assert "updatePageScrollLock();" in page[page.index("const dialogOpen"): page.index("const dialogOpen") + 280]
    assert "event.target.closest('.dialog-body, .daily-profit-list, .product-suggestion-panel, .product-history')" in page
    assert "if (!document.body.classList.contains('page-locked')) return;" in page
    assert "if (document.body.classList.contains('page-locked'))" in page


def test_trade_form_filters_products_and_shows_type_specific_fields():
    page = _read_page()

    assert ".form-field[hidden] { display: none; }" in page
    assert 'id="tradeFundCodeField"' not in page
    assert 'id="tradeFeeRateField"' in page
    assert 'name="fee_rate_percent"' in page
    assert 'id="tradeSourceField"' in page
    assert 'name="source_cash_product_id"' in page
    assert 'id="tradeDestinationField"' in page
    assert 'name="destination_cash_product_id"' in page
    assert '<option value="">钱包</option>' in page
    assert "fillProductSearch('trade', productType);" in page
    assert "$('tradeFeeRateField').hidden = !(publicFund && !redemption);" in page
    assert "$('tradeSourceField').hidden = redemption;" in page
    assert "$('tradeSource').disabled = redemption;" in page
    assert "$('tradeDestinationField').hidden = !redemption;" in page
    assert "payload.fee_rate = String(number(payload.fee_rate_percent) / 100);" in page
    assert "delete payload.fee_rate_percent;" in page
    assert "const sevenDayDisplay = sevenDay === '' ? '--' : `${(number(sevenDay) * 100).toFixed(2)}%`;" in page
    assert "pending[1] === 'external_cash' ? '钱包'" in page


def test_trade_fee_rate_defaults_to_last_purchase_and_keeps_manual_edit():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for dashboard behavior checks')
    html = _read_page()
    assert 'function tradeFeeRateDefaultPercent(' in html
    assert 'function applyTradeFeeRateDefault(' in html
    # 接线：解析出交易产品时套用它；手改标记与重新打开弹窗时的清零都要存在。
    assert "if (scope === 'trade') applyTradeFeeRateDefault(product);" in html
    assert "$('tradeFeeRateInput').addEventListener('input'" in html
    assert "form.dataset.feeRateProductId = '';" in html
    assert "$('tradeFeeRateInput').dataset.feeRateManual = 'false';" in html
    source = html[html.index('function tradeFeeRateDefaultPercent('):html.index('function syncProductSearch(')]
    script = r"""
const assert = require('node:assert/strict');
const elements = {};
const $ = id => elements[id] || (elements[id] = {dataset: {}, value: ''});
SOURCE
const form = $('tradeForm');
const input = $('tradeFeeRateInput');
const withHistory = {id: 'fund-1', last_purchase_fee_rate: '0.00006', last_purchase_fee_rate_percent: '0.006'};
const withoutHistory = {id: 'fund-2', last_purchase_fee_rate: null, last_purchase_fee_rate_percent: null};

// 已申购过的产品：默认填入最近一次申购费率（百分数）。
applyTradeFeeRateDefault(withHistory);
assert.equal(input.value, '0.006');
assert.equal(input.dataset.feeRateManual, 'false');
assert.equal(form.dataset.feeRateProductId, 'fund-1');

// 手工改费率后，同一产品被重复解析（输入框失焦触发 change）不得覆盖。
input.value = '0.01';
input.dataset.feeRateManual = 'true';
applyTradeFeeRateDefault(withHistory);
assert.equal(input.value, '0.01');

// 切换到从未申购过的产品：清空默认值，要求用户填写。
applyTradeFeeRateDefault(withoutHistory);
assert.equal(input.value, '');
assert.equal(form.dataset.feeRateProductId, 'fund-2');

// 切回有历史的产品：重新套用默认值。
input.dataset.feeRateManual = 'true';
applyTradeFeeRateDefault(withHistory);
assert.equal(input.value, '0.006');
assert.equal(input.dataset.feeRateManual, 'false');

// 未匹配到产品（搜索中途）时保持现状，不清空已填费率。
applyTradeFeeRateDefault(null);
assert.equal(input.value, '0.006');

// 兼容只提供小数费率的行，以及完全缺失字段的行。
assert.equal(tradeFeeRateDefaultPercent({last_purchase_fee_rate: '0.00012'}), '0.012');
assert.equal(tradeFeeRateDefaultPercent({}), '');
assert.equal(tradeFeeRateDefaultPercent(null), '');
""".replace('SOURCE', source)
    result = subprocess.run([node, '-e', script], capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stdout + result.stderr


def test_redemption_preselects_holding_product_opened_from_positions():
    page = _read_page()
    assert "form.dataset.prefillProductId = String(productId || '');" in page
    assert "const preferredId = String(select.value || form.dataset.prefillProductId || '');" in page
    assert "if ([...select.options].some(option => option.value === preferredId)) select.value = preferredId;" in page


def test_transactions_and_preview_use_chinese_business_labels():
    page = _read_page()
    assert "function transactionTypeLabel(value)" in page
    assert "manual_redemption: '手工赎回'" in page
    assert "opening_position: '初始持仓'" in page
    assert "function previewFieldLabel(path)" in page
    assert "'normalized_input.amount': '申购金额（元）'" in page
    assert "'normalized_input.settlement_date': '预计到账日期'" in page
    assert "'status_prediction': '预计状态'" in page
    assert "The trade will remain pending until a quote arrives." in page
    assert "等待交易日净值后自动确认" in page
    assert "String(rowValue(row, 'created_by')) !== 'wallet_migration'" in page
    assert "申购资金来源：" in page
    assert "赎回到账：" in page
    assert ".transaction-summary.single { grid-template-columns: minmax(0, 1fr); }" in page
    assert ".transaction-details { display: grid;" in page
    assert 'class="manage-card transaction-card ${kind.className}"' in page
    assert 'class="transaction-head-actions"' in page
    assert "function transactionDetailsHtml(" in page


def test_page_labels_redemption_settlement_and_preserves_backend_order():
    page = _read_page()
    assert "pending_settlement: '待到账'" in page
    assert "settled: '已到账'" in page
    assert "rowValue(row, 'display_status', 'status')" in page
    assert "rowValue(row, 'purchase_origin')" in page
    render_transactions = page[
        page.index("function renderTransactions()"):
        page.index("function renderSip()")
    ]
    assert ".sort(" not in render_transactions


def test_trade_form_applies_wallet_plus_rules_and_redemption_context():
    page = _read_page()

    assert 'id="tradeFixedProductField"' in page
    assert 'id="tradeAvailableShares"' in page
    assert "function isWalletPlusProduct(product)" in page
    assert "return activeCash.find(isWalletPlusProduct) || null;" in page
    assert "function updateTradeRedemptionContext()" in page
    assert "`可用份额 ${money(availableShares)}`" in page
    assert "settlementInput.value = tradeDateFromInput();" in page
    assert "const fixedCashPurchase = !redemption && productType === 'cash_management';" in page
    assert "$('tradeProduct').value = rowValue(walletPlus, 'id', 'product_id') || '';" in page
    assert "fillTradeCashOptions(fixedCashPurchase ? walletPlusId : '', walletRedemption ? selectedProductId : '');" in page


def test_sip_cash_outflow_is_labeled_as_sip_deduction():
    page = _read_page()

    assert "function transactionDisplayLabel(value, row = {})" in page
    assert "type === 'cash_transfer_out' && rowValue(row, 'plan_id')" in page
    assert "return '定投扣款';" in page
    assert "function isVisibleTransaction(row)" in page
    assert "if (type === 'cash_transfer_out' && rowValue(row, 'plan_id')) return true;" in page


def test_sip_card_shows_deduction_count_and_amount_in_fourth_stat():
    page = _read_page()

    assert "`已扣款 ${deductionCount} 次`" in page
    assert "`${money(deductionAmount)} 元`" in page
    assert "rowValue(row, 'deduction_count')" in page
    assert "rowValue(row, 'deduction_amount')" in page


def test_sip_card_opens_full_page_deduction_records():
    page = _read_page()
    assert ">定投记录<" in page
    assert 'data-view-sip-records="${esc(id)}"' in page
    assert 'id="sipRecordBackdrop"' in page
    assert 'id="sipRecordTitle">定投记录<' in page
    assert 'aria-label="返回定投"' in page
    assert "function openSipRecords(planId)" in page
    assert "function closeSipRecords()" in page
    assert "const records = Array.isArray(plan?.records) ? plan.records : [];" in page
    assert "`已扣款 ${number(rowValue(plan, 'deduction_count'))} 次`" in page
    assert "暂无定投记录" in page
    assert "class=\"sip-record-row\"" in page
    assert "transactionStatusLabel(rowValue(record, 'status'))" in page
    assert "openSipRecords(view.dataset.viewSipRecords)" in page
    assert "if (event.key !== 'Escape') return;" in page
    assert "if (!$('sipRecordBackdrop').hidden)" in page
    assert "data-write-operation=\"sip-records\"" not in page


def test_transaction_cards_visually_distinguish_purchase_and_redemption():
    page = _read_page()

    assert "function transactionKindMeta(value, row = {})" in page
    assert "label: '申购', className: 'purchase'" in page
    assert "label: '赎回', className: 'redemption'" in page
    assert ".transaction-card.purchase" in page
    assert ".transaction-card.redemption" in page
    assert 'class="transaction-kind ${kind.className}"' in page
    assert "const amountLabel = kind.className === 'purchase' ? '申购金额'" in page
    assert "kind.className === 'redemption' ? '赎回份额' : '交易份额'" in page
    assert "type === 'cash_transfer_out' && rowValue(row, 'plan_id')" in page
    assert "transactionKindMeta(normalizedTransactionType, row)" in page


def test_positions_panel_uses_positions_plus_products_with_pending_business():
    page = PAGE_PATH.read_text(encoding="utf-8")

    assert "const allRows = [...positions, ...pendingProducts];" in page
    assert "const rows = allRows.filter" in page
    assert "const rows = products.map" not in page


def test_overview_does_not_remerge_catalog_products_into_authoritative_products():
    page = PAGE_PATH.read_text(encoding="utf-8")

    assert "const portfolioByCode = new Map" not in page
    assert "data.products = baseProducts.map" not in page


def test_total_assets_excludes_pending_purchase_already_in_holdings():
    page = PAGE_PATH.read_text(encoding="utf-8")

    assert "rowValue(data.portfolio?.summary, 'total_assets')" in page
    assert "holdingsValue + pendingBuy + inTransitSell - pendingSell" not in page


def test_overview_rounds_summary_once_instead_of_rounding_each_product():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for JavaScript behavior checks")
    page = _read_page()
    logic = page[page.index("const marketValue = rowValue(data.portfolio?.summary, 'market_value');"):page.index("const inTransitSell = pfTxList.filter(")]
    script = r"""
const assert = require('assert');
const rowValue = (row, ...keys) => {for (const k of keys) if (row?.[k] !== undefined && row[k] !== null) return row[k]; return '';};
const number = value => Number(value) || 0;
function market(data, pfList) {
""" + logic + r"""
return Math.round(holdingsValue * 100) / 100;
}
const products = [{market_value:'100.006'}, {market_value:'200.006'}];
assert.strictEqual(market({portfolio:{summary:{market_value:'300.012'}}},products),300.01);
assert.strictEqual(market({},products),300.01);
assert.strictEqual(market({portfolio:{summary:{market_value:'665555.830385323649'}}},products),665555.83);
assert.strictEqual(Math.round((717555.83-665555.83)*100)/100,52000);
assert.strictEqual(market({portfolio:{summary:{market_value:'0'}}},products),0);
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_overview_hides_zero_share_products_after_redemption_settled():
    page = PAGE_PATH.read_text(encoding="utf-8")

    assert "function isClearedAndSettled(" in page
    assert "const products = (data.products || []).filter(item => {" in page
    assert "Number(rowValue(item, 'shares', 'total_shares'))" in page
    assert "isClearedAndSettled(pid, pfPositions, pfTransactions)" in page
    assert "赎回待到账" in page or "settleDate > todayStr" in page
    assert "持有份额为0：仅在有在途申购/赎回或赎回待到账时保留展示" in page


def test_holding_card_hides_when_cleared_keeps_stat_grid_and_short_trade_label():
    page = PAGE_PATH.read_text(encoding="utf-8")

    assert 'name="profit"' in page
    assert "function isClearedAndSettled(" in page
    assert "allRows.filter(p => !isClearedAndSettled(" in page
    assert "当不再持仓并赎回已到账后自动取消" not in page
    assert "manage-auto-hint" not in page
    assert 'data-write-operation="disable-product"' not in page
    assert 'data-write-operation="delete-holding"' not in page
    assert 'class="manage-icon-action"' not in page
    assert 'class="manage-name-actions"' not in page
    assert 'class="manage-grid position-stat-grid"' in page
    assert "<span>持有份额</span>" in page
    assert "<span>可用份额</span>" in page
    assert "<span>在途资金</span>" in page
    assert (
        ".manage-card > .manage-list > .manage-card "
        "{ border: 1px solid var(--line); box-shadow: none; background: var(--panel); }"
    ) in page
    assert ".manage-stat { padding: 9px; border-radius: 7px; background: var(--bg); }" in page
    assert ">申/赎</button>" in page
    assert ">申购/赎回</button>" not in page
    assert ".manage-title-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; }" in page
    assert 'data-write-operation="reverse"' not in page



def test_transaction_list_hides_internal_ledger_rows():
    page = PAGE_PATH.read_text(encoding="utf-8")

    assert "INTERNAL_TRANSACTION_TYPES" in page
    for transaction_type in (
        "opening_position",
        "cash_transfer_in",
        "cash_transfer_out",
        "income_accrual",
        "reversal",
    ):
        assert transaction_type in page


def test_calibration_transactions_have_an_exclusive_filter():
    page = PAGE_PATH.read_text(encoding="utf-8")

    assert 'data-transaction-filter="adjustment">校准</button>' in page
    assert "const CALIBRATION_TRANSACTION_TYPES = new Set([" in page
    for transaction_type in (
        "holding_adjustment",
        "profit_adjustment",
        "latest_profit_adjustment",
    ):
        assert transaction_type in page
    assert "if (transactionFilter === 'adjustment') return calibration;" in page
    assert "if (calibration) return false;" in page
    assert "status === 'reversed' ? 'cancelled' : status" in page


def test_transactions_reuse_all_tag_and_add_purchase_and_redemption():
    page = _read_page()
    assert page.count('data-transaction-filter="all"') == 1
    assert 'data-transaction-filter="purchase">申购</button>' in page
    assert 'data-transaction-filter="redemption">赎回</button>' in page
    assert 'data-transaction-type-filter' not in page


def test_transaction_filter_tags_wrap_without_horizontal_scrolling():
    page = _read_page()
    assert '\n    .transaction-filter-row {' in page
    container = page.split('\n    .transaction-filters {', 1)[1].split('}', 1)[0]
    row = page.split('\n    .transaction-filter-row {', 1)[1].split('}', 1)[0]
    button = page.split('\n    .transaction-filter {', 1)[1].split('}', 1)[0]
    assert 'flex-direction: column;' in container
    assert 'flex-wrap: wrap;' in row
    assert 'overflow-x: auto;' not in container
    assert 'gap: 7px;' in container
    assert 'flex: 0 0 auto;' in button


def test_cancelled_and_adjustment_filters_are_on_the_second_row():
    page = _read_page()
    filters = page[page.index('aria-label="交易记录筛选"'):page.index('id="transactionsList"')]
    rows = filters.split('<div class="transaction-filter-row">')[1:]
    assert len(rows) == 2
    expected = [('all', 'purchase', 'redemption', 'pending', 'confirmed'), ('cancelled', 'adjustment')]
    for row, values in zip(rows, expected):
        found = [part.split('"', 1)[0] for part in row.split('data-transaction-filter="')[1:]]
        assert found == list(values)


def test_transaction_purchase_redemption_filters_preserve_existing_filters_and_order():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for JavaScript behavior checks")
    page = _read_page()
    logic = page[page.index("function renderTransactions() {"):page.index("$('transactionsList').innerHTML =")]
    script = r"""
const assert = require('assert');
const rowValue = (row, ...keys) => {for (const k of keys) if (row?.[k] !== undefined && row[k] !== null) return row[k]; return '';};
const normalStatus = value => ({pending_quote:'pending', pending_confirmation:'pending', settled:'confirmed'})[value] || value;
const CALIBRATION_TRANSACTION_TYPES = new Set(['holding_adjustment', 'profit_adjustment', 'latest_profit_adjustment']);
const isVisibleTransaction = () => true;
const records = [
  {id:'redemption-settled', transaction_type:'manual_redemption', status:'confirmed', display_status:'settled'},
  {id:'sip-pending', transaction_type:'sip_purchase', status:'pending_quote'},
  {id:'manual-confirmed', transaction_type:'manual_purchase', status:'confirmed'},
  {id:'manual-cancelled', transaction_type:'manual_purchase', status:'cancelled'},
  {id:'redemption-pending', transaction_type:'manual_redemption', status:'pending_confirmation'},
  {id:'cash-leg', transaction_type:'cash_transfer_out', plan_id:'sip', status:'confirmed'},
  {id:'income', transaction_type:'income_accrual', status:'confirmed'},
  {id:'adjustment', transaction_type:'holding_adjustment', status:'confirmed'},
];
const portfolioRows = () => records;
let transactionFilter = 'all';
""" + logic + r"""
return visible.map(row => row.id);
}
transactionFilter = 'purchase';
assert.deepStrictEqual(renderTransactions(), ['sip-pending','manual-confirmed','manual-cancelled']);
transactionFilter = 'pending';
assert.deepStrictEqual(renderTransactions(), ['sip-pending','redemption-pending']);
transactionFilter = 'confirmed';
assert.deepStrictEqual(renderTransactions(), ['redemption-settled','manual-confirmed','cash-leg','income']);
transactionFilter = 'redemption';
assert.deepStrictEqual(renderTransactions(), ['redemption-settled','redemption-pending']);
transactionFilter = 'cancelled';
assert.deepStrictEqual(renderTransactions(), ['manual-cancelled']);
transactionFilter = 'adjustment';
assert.deepStrictEqual(renderTransactions(), ['adjustment']);
transactionFilter = 'all';
assert.deepStrictEqual(renderTransactions(), records.filter(row => row.id !== 'adjustment').map(row => row.id));
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_top_metrics_unchanged():
    page = _read_page()
    assert "总资产" in page
    assert "估算市值" in page
    assert "最新披露" in page


def format_holding(share_text, nav_text):
    """Python 端契约函数，行为必须与 src/nav_dashboard_page.html 内的
    formatHolding() 完全一致。HTML 改动时必须同步更新本函数与用例样本。
    """
    if not share_text:
        return ""
    try:
        shares = float(share_text)
    except (TypeError, ValueError):
        return ""
    if shares != shares:  # NaN 检查
        return ""
    if math.isinf(shares):
        return ""
    shares_text = f"{shares:.2f}"
    if not nav_text:
        return f"{shares_text} 份"
    try:
        nav = float(nav_text)
    except (TypeError, ValueError):
        return f"{shares_text} 份"
    if nav != nav:  # NaN 检查
        return f"{shares_text} 份"
    if math.isinf(nav):
        return f"{shares_text} 份"
    value = shares * nav
    value_text = f"{value:,.2f}"
    return f"{shares_text} 份 · 市值 {value_text} 元"


def test_format_holding_empty_shares_returns_empty():
    assert format_holding("", "") == ""


def test_format_holding_only_shares_when_nav_missing():
    assert format_holding("401133.95", "") == "401133.95 份"
    assert format_holding("10000", "abc") == "10000.00 份"


def test_format_holding_both_present_full_text():
    assert format_holding("401133.95", "1.001234") == "401133.95 份 · 市值 401,628.95 元"
    assert format_holding("10000", "1.000000") == "10000.00 份 · 市值 10,000.00 元"


def test_format_holding_zero_values():
    assert format_holding("0", "1.0") == "0.00 份 · 市值 0.00 元"


def test_format_holding_non_numeric_shares_treated_as_empty():
    assert format_holding("abc", "1.0") == ""
    assert format_holding(None, "1.0") == ""


def test_format_holding_non_numeric_nav_only_shares():
    assert format_holding("10000", None) == "10000.00 份"


def test_format_holding_nan_shares_returns_empty():
    assert format_holding("nan", "1.0") == ""
    assert format_holding("NaN", "1.0") == ""


def test_format_holding_nan_nav_returns_only_shares():
    assert format_holding("10000", "nan") == "10000.00 份"


def test_format_holding_inf_shares_returns_empty():
    assert format_holding("inf", "1.0") == ""
    assert format_holding("-inf", "1.0") == ""


def test_format_holding_inf_nav_returns_only_shares():
    assert format_holding("10000", "inf") == "10000.00 份"
    assert format_holding("10000", "-inf") == "10000.00 份"


def test_format_holding_nav_zero():
    assert format_holding("10000", "0") == "10000.00 份 · 市值 0.00 元"


def test_profit_sheet_expands_one_period_row_at_a_time():
    html = _read_page()
    assert "let expandedProfitKey = '';" in html
    assert "function toggleProfitRow(key)" in html
    assert "data-profit-row-key" in html
    assert "aria-expanded=" in html
    assert "daily-profit-products" in html
    assert "daily-profit-product-name" in html
    assert "expandedProfitKey = expandedProfitKey === key ? '' : key;" in html
    assert "profit-row-chevron" in html
    assert "const expandable = products.length > 0;" in html


def test_profit_detail_opens_as_full_page_not_bottom_sheet():
    html = _read_page()
    sheet = html[html.index(".bottom-sheet"): html.index(".bottom-sheet") + 420]
    assert "height: 100%" in sheet
    assert "max-height: 70vh" not in html[html.index(".bottom-sheet"): html.index("@keyframes")]
    assert 'id="dailyProfitTitle">收益明细<' in html
    assert 'aria-label="返回总览"' in html
    assert "sheet-handle" not in html
    backdrop = html[html.index(".sheet-backdrop {"): html.index(".sheet-backdrop[hidden]")]
    assert "z-index: 200" in backdrop
    list_css = html[html.index(".daily-profit-list"): html.index(".daily-profit-list") + 220]
    assert "max-height: none" in list_css


def test_profit_product_rows_use_full_product_name():
    html = _read_page()
    marker = 'class="daily-profit-product-name">'
    snippet = html[html.index(marker): html.index(marker) + 120]
    assert "${esc(product.name || product.code)}" in snippet
    assert "shortName(" not in snippet
    name_css_start = html.index(".daily-profit-product-name")
    name_css = html[name_css_start:name_css_start + 280]
    assert "white-space: nowrap" not in name_css
    assert "white-space: normal" in name_css


def test_profit_sheet_clears_expanded_row_on_tab_change_and_close():
    html = _read_page()
    assert "function setProfitView(view)" in html
    assert "function closeDailyProfits()" in html
    close_idx = html.index("function closeDailyProfits()")
    set_idx = html.index("function setProfitView(view)")
    assert "expandedProfitKey = '';" in html[set_idx:set_idx + 220]
    assert "expandedProfitKey = '';" in html[close_idx:close_idx + 420]


def test_sip_dialog_has_frequency_and_schedule_controls():
    html = _read_page()
    assert 'name="frequency" id="sipFrequency"' in html
    assert '<option value="daily">每日</option>' in html
    assert '<option value="weekly">每周</option>' in html
    assert '<option value="monthly">每月</option>' in html
    assert 'id="sipWeeklyDayField"' in html
    assert 'id="sipWeeklyDay"' in html
    assert 'id="sipMonthlyDayField"' in html
    assert 'id="sipMonthlyDay"' in html


def test_sip_weekly_options_are_weekdays_only():
    html = _read_page()
    for value, label in (
        ("1", "周一"), ("2", "周二"), ("3", "周三"), ("4", "周四"), ("5", "周五"),
    ):
        assert f'<option value="{value}">{label}</option>' in html
    # 周六、周日不得出现在每周扣款日控件中。
    assert 'value="6">周六' not in html
    assert 'value="7">周日' not in html


def test_sip_monthly_options_span_one_to_thirty_one():
    html = _read_page()
    monthly = html[html.index('id="sipMonthlyDay"'):]
    monthly = monthly[:monthly.index("</select>")]
    assert '<option value="1">1 日</option>' in monthly
    assert '<option value="29">29 日</option>' in monthly
    assert '<option value="30">30 日</option>' in monthly
    assert '<option value="31">31 日</option>' in monthly
    assert '<option value="32"' not in monthly


def test_sip_schedule_conditional_sync_wired():
    html = _read_page()
    assert "function syncSipScheduleFields(event)" in html
    assert "$('sipFrequency').addEventListener('change', syncSipScheduleFields);" in html
    sync = html[html.index("function syncSipScheduleFields(event)"):]
    sync = sync[:sync.index("\n    }")]
    assert "$('sipWeeklyDayField').hidden = !weekly;" in sync
    assert "$('sipWeeklyDay').disabled = !weekly;" in sync
    assert "$('sipMonthlyDayField').hidden = !monthly;" in sync
    assert "$('sipMonthlyDay').disabled = !monthly;" in sync


def test_sip_start_date_updates_on_schedule_changes():
    html = _read_page()
    assert "$('sipWeeklyDay').addEventListener('change', syncSipStartDate);" in html
    assert "$('sipMonthlyDay').addEventListener('change', syncSipStartDate);" in html
    assert "/api/portfolio/sip-plans/start-date?" in html
    assert '<input name="start_date" type="date" required' in html
    assert "payload.auto_start_date = true;" in html
    # 手选路径不带 auto 标记，由后端按显式日期保存。
    assert "delete payload.auto_start_date;" in html


def test_sip_start_date_browser_logic_handles_changes_and_stale_responses():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for JavaScript behavior checks")
    html = _read_page()
    logic = html[html.index("let sipStartDateRequestId = 0;"):html.index("function sipScheduleLabel(row)")]
    script = r"""
const assert = require('assert');
const controls = {
  sipForm: {dataset: {}, start_date: {value: '', setCustomValidity(value) {this.error = value;}}},
  sipFrequency: {value: 'weekly'}, sipWeeklyDay: {value: '1'}, sipMonthlyDay: {value: '31'},
  sipWeeklyDayField: {}, sipMonthlyDayField: {}
};
const $ = id => controls[id];
const requests = [];
const api = path => new Promise(resolve => requests.push({path, resolve}));
const errors = [];
const showFormError = (form, error) => errors.push(error.message);
""" + logic + r"""
(async () => {
  const form = $('sipForm');
  syncSipScheduleFields();
  assert.strictEqual(requests.length, 0);
  const weekly = syncSipStartDate();
  assert(requests[0].path.includes('frequency=weekly&schedule_day=1'));
  $('sipFrequency').value = 'monthly';
  const monthly = syncSipStartDate();
  requests[1].resolve({ok: true, json: async () => ({start_date: '2026-09-30'})});
  await monthly;
  requests[0].resolve({ok: true, json: async () => ({start_date: '2026-09-07'})});
  await weekly;
  assert.strictEqual(form.start_date.value, '2026-09-30');
  const failed = syncSipStartDate();
  requests[2].resolve({ok: false, json: async () => ({message: 'calendar unavailable'})});
  await failed;
  assert.strictEqual(form.start_date.value, '');
  assert(form.start_date.error);
  assert.deepStrictEqual(errors, ['calendar unavailable']);
  $('sipFrequency').value = 'daily';
  syncSipScheduleFields();
  const daily = syncSipStartDate();
  assert(requests[3].path.endsWith('frequency=daily'));
  requests[3].resolve({ok: true, json: async () => ({start_date: '2026-09-07'})});
  await daily;
  assert.strictEqual(form.start_date.value, '2026-09-07');
  assert.strictEqual(form.start_date.error, '');
  assert.strictEqual(form.dataset.autoStartDate, 'true');
  // 手选开始日期后：syncSipStartDate 不再发起请求、不覆盖已选值。
  form.dataset.startDateManual = 'true';
  form.start_date.value = '2026-10-28';
  await Promise.race([
    syncSipStartDate(),
    new Promise(resolve => setTimeout(() => resolve('timeout'), 50)),
  ]);
  assert.strictEqual(requests.length, 4);
  assert.strictEqual(form.start_date.value, '2026-10-28');
  // 周期变化（带事件）会清除手选状态并重算默认日期。
  const rescheduled = syncSipScheduleFields({});
  assert.strictEqual(form.dataset.startDateManual, undefined);
  assert(requests[4].path.endsWith('frequency=daily'));
  requests[4].resolve({ok: true, json: async () => ({start_date: '2026-09-08'})});
  await rescheduled;
  assert.strictEqual(form.start_date.value, '2026-09-08');
})().catch(error => {console.error(error); process.exit(1);});
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_sip_start_date_supports_manual_selection():
    html = _read_page()
    # 输入框可编辑，input 事件标记手选状态。
    assert '<input name="start_date" type="date" required' in html
    assert "$('sipForm').start_date.addEventListener('input'," in html
    assert "dataset.startDateManual = 'true';" in html
    # 提交：手选不带 auto 标记（走显式日期），默认路径仍自动计算。
    assert "if (form.dataset.startDateManual === 'true') {" in html
    assert "delete payload.auto_start_date;" in html
    # 编辑回填原开始日期并标记手选，避免被自动重算覆盖。
    assert "form.start_date.value = rowValue(existing, 'start_date');" in html
    # 提示文案说明手选与补扣语义。
    assert "可手动修改" in html
    assert "自动补扣" in html


def test_sip_edit_refills_schedule_and_syncs():
    html = _read_page()
    open_sip = html[html.index("function openSip("):]
    open_sip = open_sip[:open_sip.index("\n    }")]
    assert "form.frequency.value = String(rowValue(existing, 'frequency') || 'daily');" in open_sip
    assert "$('sipWeeklyDay').value = String(" in open_sip
    assert "$('sipMonthlyDay').value = String(" in open_sip
    assert "syncSipScheduleFields();" in open_sip


def test_sip_submit_sends_frequency_and_schedule_day():
    html = _read_page()
    submit = html[html.index("$('sipForm').addEventListener('submit'"):]
    submit = submit[:submit.index("});", submit.index("previewAction"))]
    assert "payload.frequency = frequency;" in submit
    assert "payload.schedule_day = Number(" in submit
    assert "payload.schedule_day = null;" in submit


def test_sip_start_date_label_is_begin_date_not_first_deduction():
    html = _read_page()
    assert "首次扣款日" not in html
    assert "开始日期" in html
    # 卡片与表单都改用“开始日期”。
    assert "<span>开始日期</span>" in html
    assert '>开始日期<input name="start_date" type="date" required' in html
    assert "#sipDialog .form-field { min-width: 0; max-width: 100%; }" in html


def test_sip_card_shows_schedule_summary():
    html = _read_page()
    assert "function sipScheduleLabel(row)" in html
    label = html[html.index("function sipScheduleLabel(row)"):]
    label = label[:label.index("\n    }")]
    assert "return '每日';" in label
    assert "'每周'" in label
    assert "'每月 '" in label
    # 卡片正文调用摘要函数。
    assert "${esc(sipScheduleLabel(row))}" in html


def test_sip_preview_uses_chinese_schedule_field_names():
    html = _read_page()
    assert "'frequency': '定投周期'" in html
    assert "'schedule_day': '扣款日'" in html
    assert "'start_date': '开始日期'" in html
    # 预览值中文化。
    assert "daily: '每日'" in html
    assert "weekly: '每周'" in html
    assert "monthly: '每月'" in html


def test_render_sets_state_before_filtering_overview_products():
    """刷新后的首次渲染必须先写入 state.data，否则 0 份额的在途申购产品会被过滤掉。"""
    html = _read_page()
    render_body = html[
        html.index("function render(data) {"):
        html.index("async function loadDashboard()")
    ]
    assert "state.data = data || {};" in render_body
    assert render_body.index("state.data = data || {};") < render_body.index(
        "const products = (data.products || []).filter"
    )
    management = html[
        html.index("function renderManagement(data) {"):
        html.index("function renderPositions()")
    ]
    assert "state.data = data || {};" not in management


def test_market_tab_follows_overview_and_renders_bond_barometer():
    html = _read_page()

    # 页签顺序：总览之后紧跟行情
    overview = html.index('data-tab="overview"')
    market = html.index('data-tab="market"')
    positions = html.index('data-tab="positions"')
    assert overview < market < positions
    assert "const MANAGEMENT_TABS = ['overview', 'market', 'positions', 'transactions', 'sip', 'settings'];" in html
    assert 'id="marketPanel"' in html
    # 行情刷新与顶部导航刷新共用，不再有独立更新按钮。
    assert "barometerRefreshButton" not in html
    assert "async function loadBarometer(force = false)" in html
    assert "function renderBarometer(data)" in html
    # 行情通过服务端快照提供，非交易时段不主动刷新。
    # 行情由服务端库内快照提供，页面不再维护浏览器缓存。
    assert "BAROMETER_CACHE_KEY" not in html
    assert "restoreBarometerCache" not in html
    assert "barometerData" not in html
    assert "if (data.bond_barometer) renderBarometer(data.bond_barometer);" in html
    assert "if (tab === 'market') loadMarketOverview();" in html
    assert "loadBarometer(true);\n      refreshRequestPending = true;" in html
    assert "#marketPanel .section-head { border-bottom: 0; }" in html
    assert "<h2>行情</h2>" not in html
    assert "/api/portfolio/bond-barometer" in html
    assert "grid-template-columns: repeat(6, 1fr);" in html
    assert 'id="settingsTab"' in html
    assert 'id="settingsPanel"' in html
    assert 'id="aiBaseUrl"' in html
    assert 'id="aiApiKey"' in html
    assert 'id="aiTestButton"' in html
    assert 'id="modelProfileList"' in html
    assert 'id="aiNewProfileButton"' in html
    assert 'id="aiFetchModelsButton"' in html
    assert 'id="aiCopyKeyButton"' in html
    assert 'class="field-with-action"' in html
    assert 'id="aiCopyFromProfile"' in html
    assert 'id="aiModelOptions"' in html
    assert 'id="aiModelChoices"' in html
    assert 'id="aiDeleteButton"' in html
    assert 'class="settings-check-row"' in html
    assert '<label class="settings-check"><input id="estimateJobsEnabled" type="checkbox" disabled>启用盘中预估后台任务</label>' in html
    assert '<label class="settings-check"><input id="aiEnabled" type="checkbox" disabled>启用报告提取</label>' in html
    assert "/api/portfolio/fund-estimate-model-settings/models" in html
    assert "/profiles/${id}/key" in html
    assert "function copyProfileKey" in html
    assert "function renderModelProfiles" in html
    assert "function renderModelChoices" in html
    assert "function closeProfileForm" in html
    assert 'id="modelDialog"' in html
    assert 'id="modelDialogTitle"' in html
    assert 'id="aiDialogStatus"' in html
    assert 'data-delete-profile=' in html
    assert 'id="aiTestButton" type="button">测试</button>' in html
    assert "notifyModelSettings('密钥已复制', false)" in html
    # 配置卡片只显示配置名和模型名，不再显示地址与密钥脱敏值。
    card_render = html[html.index("function renderModelProfiles"):html.index("function renderCopyFromOptions")]
    assert "api_key_hint" not in card_render
    assert "base_url" not in card_render
    # 页面数据轮询重绘时不能重置正在编辑的模型表单。
    assert "if (tab === 'settings' && !modelSettingsLoaded) loadModelSettings();" in html
    assert "status.textContent = message || '';" in html
    assert "function estimateNotePieces" in html
    assert "verification-analysis-model" in html
    assert "分析模型：" in html
    market = html[html.index('id="marketPanel"'): html.index('id="positionsPanel"')]
    assert "持仓涨跌幅预估" in market
    assert "预估涨跌幅" in market
    assert "试验参考" not in market
    assert ".estimate-return.positive { color: var(--positive); }" in html
    assert "code !== 'basis_unverified'" not in html
    assert market.index('aria-label="股市行情"') < market.index('<h2>债市晴雨表</h2>')
    assert "value.toFixed(3)" in html
    assert "待确认基金参考" not in market
    assert "estimateMarkup(pid)" not in html
    # 行情面板首块与其他页签一致，与顶栏保持相同间距。
    assert "#marketPanel > .card.section { margin-top: 0; }" in html


def test_estimates_render_profit_below_rate_and_pending_purchase_without_false_profit():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for JavaScript behavior checks')
    page = _read_page()
    logic = page[page.index('const ESTIMATE_REASONS ='):page.index('function setModelSettingsStatus(')]
    script = r'''
const assert = require('assert');
const elements={estimateWatchList:{innerHTML:''},estimateMeta:{textContent:''},stockMarketContent:{innerHTML:''},stockMarketMeta:{textContent:''}};
const $=id=>elements[id];
const markSkeletonReady = () => {};
const esc=v=>String(v??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const tone=v=>Number(v)>0?'positive':Number(v)<0?'negative':'neutral';
const rowValue=(r,...ks)=>{for(const k of ks)if(r?.[k]!==undefined&&r[k]!==null)return r[k];return '';};
const portfolioRows=()=>[];
const isClearedAndSettled=()=>false;
const marketLoading=false;
const state={activeTab:'market',data:{fund_estimates:{a:{product_id:'a',code:'918273',name:'基金<名>',holding_status:'held',return_pct:'-0.024',estimated_profit:'-3.51',market_as_of:'2026-10-23T16:56:00+08:00',confidence_label:'试验，情景范围不是概率置信区间'},b:{product_id:'b',code:'564738',name:'待确认基金',holding_status:'pending_purchase',pending_purchase:true,return_pct:'0.01',estimated_profit:null,profit_reason:'awaiting_confirmed_shares',market_as_of:'2026-10-23T16:56:00+08:00',confidence_label:'试验，情景范围不是概率置信区间'}}}};
''' + logic + r'''
renderHeldFundEstimates();
const result=elements.estimateWatchList.innerHTML;
assert(result.includes('基金&lt;名&gt;'));
assert(result.indexOf('-0.024%') < result.indexOf('3.51'));
assert(result.includes('estimate-amount'));
assert(result.includes('买入待确认'));
assert(result.includes('待份额确认'));
assert(!result.includes('¥0.00'));
assert(!result.includes('行情 2026-10-23 16:56'));
assert(!result.includes('情景范围'));
assert(!result.includes('试验'));
assert.strictEqual(elements.estimateMeta.textContent, '2026-10-23 16:56');
renderStockMarket({available:true,source:'东财',fetched_at:'2026-10-23T16:20:00+08:00',indices:[{name:'标普500',market:'美股',quote_date:'2026-10-22',quote_time:'2026-10-22T16:00:00-04:00',price:'6000',change_pct:'-1.2'}],indices_status:{US:{collection_phase:'close'}},industries:{quote_date:'2026-10-23',stale:true,leaders:[{name:'电子<行业>',change_pct:'2'}],laggards:[]}});
assert(!elements.stockMarketContent.innerHTML.includes('2026-10-22 16:00 纽约'));
assert(elements.stockMarketMeta.textContent.includes('16:20 更新'));
assert.strictEqual(elements.stockMarketMeta.textContent, '16:20 更新');
assert(!elements.stockMarketContent.innerHTML.includes('更新延迟'));
assert(elements.stockMarketContent.innerHTML.includes('2026.10.22'));
assert(!elements.stockMarketContent.innerHTML.includes('当地市场日期'));
assert(elements.stockMarketContent.innerHTML.includes('已收盘'));
assert(elements.stockMarketContent.innerHTML.includes('电子&lt;行业&gt;'));
assert(elements.stockMarketContent.innerHTML.includes('-1.20%'));
renderStockMarket({available:true,source:'东财',fetched_at:'2026-10-23T10:05:00+08:00',refresh_error:'部分失败',indices:[{name:'上证指数',market:'A股',quote_date:'2026-10-23',quote_time:'2026-10-23T10:00:00+08:00',price:'3690',change_pct:'0.5',source_delayed:true}],indices_status:{CN:{collection_phase:'morning'}},industries:{quote_date:'2026-10-23',stale:true,items:[{name:'机械',change_pct:'1',source_delayed:true}],leaders:[],laggards:[]}});
assert.strictEqual(elements.stockMarketMeta.textContent, '10:05 更新');
assert(elements.stockMarketContent.innerHTML.includes('交易中'));
assert(!elements.stockMarketContent.innerHTML.includes('更新延迟'));
'''
    result = subprocess.run([node], input=script, capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr


def test_barometer_renders_source_time_egg_ranges_and_missing_quotes():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for JavaScript behavior checks")
    page = _read_page()
    logic = page[page.index("const BAROMETER_WEATHER ="):page.index("async function loadBarometer(")]
    script = r"""
const assert = require('assert');
const elements = {barometerMeta:{textContent:''},barometerContent:{innerHTML:''}};
const $ = id => elements[id];
const markSkeletonReady = () => {};
const esc = value => String(value ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
""" + logic + r"""
renderBarometer({available:true,as_of:'2026-10-23',source_id:'southernfund',source:'南方基金<行情>',
  source_updated_at:'2026-10-23T09:46:00',snapshot_at:'2026-10-23T09:50:00',change_unit:'bp',
  rate:{counts:{red:0,green:0,flat:1,missing:1},weather:'cloud',rows:[{label:'国债',cells:[
    {tenor:'1年',change:'0',color:'flat'},{tenor:'3年',change:null,color:'missing'}]}]},
  credit:{rows:[]},egg_estimates:{available:true,as_of:'2026-10-23',source_updated_at:'2026-10-23T09:49:00',
    items:[{label:'信用债·中长期',display:'预计收蛋3–6个',color:'red'}]}});
assert(elements.barometerMeta.textContent.includes('09:46'));
assert(!elements.barometerMeta.textContent.includes('09:50'));
const html = elements.barometerContent.innerHTML;
assert(html.includes('预计收蛋3–6个'));
assert(html.includes('2026-10-23 09:49'));
assert(html.includes('暂无报价'));
assert(html.includes('0 bp'));
assert(html.includes('南方基金&lt;行情&gt;'));
renderBarometer({available:true,as_of:'2026-10-22',source_id:'chinabond',is_fallback:true,rate:{rows:[]},credit:{rows:[]}});
assert(elements.barometerMeta.textContent.includes('日终'));
assert(elements.barometerContent.innerHTML.includes('日终备用'));
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
