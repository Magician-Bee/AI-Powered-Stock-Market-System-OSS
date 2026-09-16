"""Run the notification loader with isolated fetch, route and timer doubles."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "src/stock_ai/ui/static/js/features/dashboard.js"
NODE = shutil.which("node")

HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const mode = process.argv[2];
const calls = [], routeObservers = [], events = {}, timers = new Map(), nodes = new Map();
let timerId = 0;
const state = { symbol: '020000.TW', notificationPreviews: [{ title: 'old cached preview' }] };
const document = { hidden: false, documentElement: { dataset: { workspace: 'market', workspaceTab: 'monitor' } },
  addEventListener(name, fn) { events[name] = fn; } };
const sandbox = {
  state, document, AbortController,
  $: id => { if (!nodes.has(id)) nodes.set(id, { innerHTML: '', value: '' }); return nodes.get(id); },
  MutationObserver: class {
    constructor(fn) { routeObservers.push(fn); }
    observe(target, options) {
      assert.equal(target, document.documentElement);
      assert.deepEqual(Array.from(options.attributeFilter), ['data-workspace', 'data-workspace-tab']);
    }
  },
  setTimeout(fn, delay) { assert.equal(delay, 4000); timers.set(++timerId, fn); return timerId; },
  clearTimeout(id) { timers.delete(id); },
  addEventListener(name, fn) { events[name] = fn; },
  escapeHtml: value => String(value ?? ''),
  renderEmptyBlock: (title, detail) => `${title}: ${detail}`,
  uiDataApi: path => '/api/data/ui/v1' + path,
  api(path, options) {
    if (!path.startsWith('/api/notifications/')) return Promise.resolve({ items: [] });
    assert.ok(path === '/api/notifications/channels' || path.startsWith('/api/notifications/previews?symbol='));
    assert.equal(options.method, undefined, 'notification loading must stay GET-only');
    assert.ok(options.signal instanceof AbortSignal);
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    const call = { path, signal: options.signal, resolve, reject };
    calls.push(call);
    options.signal.addEventListener('abort', () => {
      if (mode !== 'late_response') reject(new Error('aborted'));
    });
    return promise;
  },
};
sandbox.window = sandbox;
// Load the real declarations and route/visibility hooks, excluding unrelated
// application button bindings that normally follow the notification loader.
const source = fs.readFileSync(process.argv[1], 'utf8').split("\n$('symbolSelect').addEventListener")[0];
vm.runInNewContext(source, sandbox, { filename: process.argv[1] });
const box = () => nodes.get('notificationPreviewBox')?.innerHTML || '';
function fulfill(start, title) {
  calls[start].resolve({ items: [] });
  calls[start + 1].resolve({ items: [{ title, body: 'fixture body', category: 'fixture', channels: [], dry_run: true }] });
}
function assertAborted(start) {
  assert.equal(calls[start].signal.aborted, true);
  assert.equal(calls[start + 1].signal.aborted, true);
}
(async () => {
  if (mode === 'offscreen_background') {
    document.documentElement.dataset.workspace = 'home';
    await sandbox.loadNotificationCenter();
    assert.equal(calls.length, 0, 'bootstrap/chart calls must not fetch hidden notifications');
    const secondary = ['renderFundamentalsBox', 'renderNewsCenter', 'renderCorporateActionCards',
      'renderStockAiPlan', 'renderMonitorSignals', 'loadTradingAnomalies', 'loadMonthlyRevenueHistory',
      'loadIncomeStatementHistory', 'loadBalanceSheetHistory', 'loadCashFlowHistory',
      'loadFinancialRatioHistory', 'loadGrowthHistory', 'loadIndustryMetrics', 'loadFinancialGuidance',
      'loadFinancialAnomalies', 'loadBasicValuation', 'loadValuationPercentiles'];
    secondary.forEach(name => { sandbox[name] = async () => {}; });
    document.documentElement.dataset.workspace = 'market';
    await sandbox.loadDashboardSymbolDetails(state.symbol);
    assert.equal(calls.length, 0, 'symbol details must not duplicate the monitor loader');
    return;
  }
  const first = sandbox.loadNotificationCenter();
  assert.equal(calls.length, 2);
  assert.equal(state.notificationPreviews.length, 0, 'stale previews cannot be sent while refreshing');
  assert.equal(calls[0].signal, calls[1].signal);
  if (mode === 'dedupe') {
    assert.equal(sandbox.loadNotificationCenter('020000.tw'), first);
    assert.equal(calls.length, 2);
    fulfill(0, 'current preview');
    await first;
    assert.equal(state.notificationPreviews[0].title, 'current preview');
    assert.ok(box().includes('current preview'));
  } else if (['superseded', 'late_response'].includes(mode)) {
    const second = sandbox.loadNotificationCenter('8299.TWO');
    assertAborted(0);
    assert.equal(calls.length, 4);
    fulfill(2, 'new symbol preview');
    await second;
    if (mode === 'late_response') {
      calls[0].reject(new Error('late channel failure'));
      calls[1].reject(new Error('late preview failure'));
    }
    await first;
    assert.equal(state.notificationPreviews[0].title, 'new symbol preview');
    assert.ok(!box().includes('failure'));
    assert.ok(box().includes('new symbol preview'));
  } else if (mode === 'leave_route') {
    document.documentElement.dataset.workspace = 'portfolio';
    document.documentElement.dataset.workspaceTab = 'accounts';
    routeObservers.forEach(fn => fn());
    assertAborted(0);
    await first;
    assert.ok(!box().includes('aborted'), 'offscreen cancellation must not render a stale error');
    await sandbox.loadNotificationCenter();
    assert.equal(calls.length, 2);
    document.documentElement.dataset.workspace = 'market';
    document.documentElement.dataset.workspaceTab = 'monitor';
    const retry = sandbox.loadNotificationCenter();
    fulfill(2, 'returned preview');
    await retry;
    assert.equal(state.notificationPreviews[0].title, 'returned preview');
  } else if (mode === 'hidden_tab') {
    document.hidden = true;
    events.visibilitychange();
    assertAborted(0);
    await first;
    document.hidden = false;
    events.visibilitychange();
    assert.equal(calls.length, 4, 'returning to the selected monitor page may retry');
    const retry = sandbox.loadNotificationCenter();
    fulfill(2, 'visible again');
    await retry;
    assert.equal(state.notificationPreviews[0].title, 'visible again');
  } else if (mode === 'pagehide') {
    events.pagehide();
    assertAborted(0);
    await first;
    assert.equal(state.notificationPreviews.length, 0);
  } else if (['timeout_retry', 'failure_retry'].includes(mode)) {
    calls[0].resolve({ items: [] });
    if (mode === 'timeout_retry') {
      Array.from(timers.values()).forEach(fn => fn());
      assertAborted(0);
    } else calls[1].reject(new Error('503 fixture unavailable'));
    await first;
    assert.ok(box().includes(mode === 'timeout_retry' ? '4 秒' : '503 fixture unavailable'));
    assert.equal(state.notificationPreviews.length, 0);
    const retry = sandbox.loadNotificationCenter();
    assert.equal(calls.length, 4, 'settled failure must permit an explicit retry');
    fulfill(2, 'retry succeeded');
    await retry;
    assert.equal(state.notificationPreviews[0].title, 'retry succeeded');
  }
  assert.equal(timers.size, 0, 'settled requests must release their timeout');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


@pytest.mark.skipif(NODE is None, reason="Node.js is required for controller behavior tests")
@pytest.mark.parametrize("scenario", [
    "offscreen_background", "dedupe", "superseded", "late_response", "leave_route",
    "hidden_tab", "pagehide", "timeout_retry", "failure_retry",
])
def test_notification_requests_are_visible_deduplicated_and_cancellable(scenario):
    result = subprocess.run([NODE, "-e", HARNESS, str(SCRIPT), scenario], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
