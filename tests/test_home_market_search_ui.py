"""Execute the real search/controller JavaScript with isolated DOM/API doubles."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "src/stock_ai/ui/static/js/features/market-intelligence/workspace-home.js"
NODE = shutil.which("node")

HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const mode = process.argv[2];
const requests = [], selected = [], rendered = [], listeners = {};
const hitModes = ['outside', 'enter', 'explicit', 'alert', 'paper', 'refresh_identity'];
let sends = 0, opened = 0;
const context = { selection: {}, data: {}, chart: {}, route: {} };
function element(id) {
  return { value: '', textContent: '', dataset: {}, attributes: {},
    classList: { toggle() {} },
    setAttribute(k,v) { this.attributes[k] = v; },
    removeAttribute(k) { delete this.attributes[k]; },
    addEventListener(name, handler) { listeners[id + ':' + name] = handler; },
    click() { if (id === 'globalAgentSend') sends++; },
  };
}
const nodes = Object.fromEntries(['home', 'homeMarketSearch', 'homeActionFeedback',
  'globalAgentPrompt', 'globalAgentSend', 'workspaceSelectedInstrument',
  'homeMarketSessionBadge', 'homeMarketStatusText', 'homeMarketFreshness',
  'marketRegimeLabel', 'marketBreadthValue', 'marketUniverseCount', 'marketValidCount',
  'marketModelCount', 'marketSnapshotTime', 'marketRegimeStrip'].map(id => [id, element(id)]));
const detail = { symbol: '020000.TW', name: 'fixture ETN', entity_id: 'fixture-identity',
  decision_label: '研究保留／新進場受限', trigger: 'fixture', invalidation: 'fixture',
  product_classification: { status: 'verified', product_type: 'etn' },
  product_entry_assessment: { allowed: false, classification_verified: true },
  data_quality: { status: 'partial', source: 'fixture' } };
const sandbox = {
  document: { documentElement: { dataset: {} }, getElementById: id => nodes[id] || null, querySelectorAll: () => [], addEventListener() {} },
  state: { detailSummary: {} },
  WorkspaceContextStore: {
    get: () => context,
    set(patch) { for (const [key,value] of Object.entries(patch)) context[key] = { ...context[key], ...value }; return context; },
    async hydrate(patch) { this.set(patch || {}); },
  },
  AgentDockController: { open() { opened++; } },
  MarketDecisionBoard: { render() {}, renderDetail(value) { rendered.push(value); } },
  MarketWorkspaceNavigation: { hydrate() {} },
  loadSummary: async symbol => { selected.push(symbol); },
  uiDataApi: path => '/api/data/ui/v1' + path,
  api: async (path, options) => {
    requests.push({ path, options });
    if (options) {
      assert.equal(options.method, 'POST');
      assert.ok(['alert', 'paper'].includes(mode), 'only an explicit tested action may write');
      assert.equal(path, mode === 'alert' ? '/api/alerts' : '/api/paper-trading/preview');
      return mode === 'alert' ? { alert_id: 'fixture-alert' } : { risk_advisory: { order_allowed: false } };
    }
    assert.equal(options, undefined, 'search must only make GET requests');
    if (path.includes('/intelligence')) {
      if (hitModes.includes(mode)) {
        return { schema_version: 'stock_ai.instrument_intelligence.v1', snapshot_id: 'MIS-PERSISTED', detail };
      }
      if (mode === 'mismatch') return { snapshot_id: 'MIS-PERSISTED', detail: { ...detail, symbol: '2330.TW' } };
      throw new Error(mode === 'outage' ? '503 unavailable' : '404 instrument_not_in_latest_snapshot');
    }
    assert.ok(path.startsWith('/api/data/ui/v1/entities/search?q='));
    return { items: mode === 'wrong_entity' ? [{ symbol: '2330.TW', name: 'different security' }] : [] };
  },
};
sandbox.window = sandbox;
// Test-only access to closure state; the production script has no test exports.
const source = fs.readFileSync(process.argv[1], 'utf8').replace('window.HomeMarketWorkspace = {',
  'window.__searchTest = { setBootstrap(value) { bootstrapPayload = value; }, getSelectedDetail() { return selectedDetail; }, applyBootstrap, entityForSymbol, bindActions, action, detailForSymbol }; window.HomeMarketWorkspace = {');
vm.runInNewContext(source, sandbox, { filename: process.argv[1] });
const visible = { market_snapshot: { snapshot_id: 'MIS-RENDERED', candidate_details: {
  '2330.TW': { symbol: '2330.TW', name: 'visible only' },
} } };
sandbox.__searchTest.setBootstrap(visible);
(async () => {
  const query = mode === 'miss' ? '找不到的搜尋文字' : '020000.tw';
  if (mode === 'enter') {
    sandbox.__searchTest.bindActions();
    nodes.homeMarketSearch.value = query;
    let prevented = false;
    listeners['homeMarketSearch:keydown']({ key: 'Enter', currentTarget: nodes.homeMarketSearch,
      preventDefault() { prevented = true; } });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(prevented, true);
  } else {
    await sandbox.HomeMarketWorkspace.searchOrAskAgent(query);
  }
  assert.equal(sends, 0, 'ordinary search must never start an Agent Run');
  assert.equal(nodes.homeMarketSearch.attributes['aria-busy'], undefined);
  if (hitModes.includes(mode)) {
    assert.deepEqual(selected, ['020000.TW']);
    assert.deepEqual(requests.map(r => r.path), ['/api/instruments/020000.TW/intelligence']);
    assert.equal(context.selection.entity_id, 'fixture-identity');
    assert.equal(context.data.market_snapshot_id, 'MIS-PERSISTED');
    assert.equal(sandbox.HomeMarketWorkspace.getBootstrap().market_snapshot.snapshot_id, 'MIS-RENDERED');
    assert.equal(visible.market_snapshot.candidate_details['020000.TW'], undefined);
    assert.equal(rendered[0].product_classification.product_type, 'etn');
    assert.equal(rendered[0].product_entry_assessment.allowed, false);
    if (mode === 'explicit') {
      await sandbox.__searchTest.action({ dataset: { workspaceAction: 'agent', symbol: '020000.TW' } });
      assert.equal(sends, 1, 'the explicitly requested ask-agent action must remain enabled');
      assert.equal(opened, 1);
      assert.ok(nodes.globalAgentPrompt.value.includes('020000.TW'));
    }
    if (['alert', 'paper'].includes(mode)) {
      await sandbox.__searchTest.action({ dataset: { workspaceAction: mode, symbol: '020000.TW' } });
      assert.equal(sends, 0);
      const body = JSON.parse(requests[1].options.body);
      assert.equal(body.symbol, '020000.TW');
      if (mode === 'alert') {
        assert.equal(body.rule.snapshot_id, 'MIS-PERSISTED');
        assert.equal(body.rule.trigger, detail.trigger);
      } else {
        assert.equal(body.rationale, '首頁市場快照 MIS-PERSISTED 預覽');
      }
    }
    if (mode === 'refresh_identity') {
      const refreshed = { ...detail, entity_id: 'fixture-new-owner', name: 'refreshed name' };
      await sandbox.__searchTest.applyBootstrap({
        market_snapshot: { snapshot_id: 'MIS-REFRESHED', candidate_details: { '020000.TW': refreshed } },
        agent_context: { selection: { symbol: '020000.TW', entity_id: 'fixture-identity' } },
      }, { selectDefault: false });
      assert.equal(sandbox.__searchTest.entityForSymbol('020000.TW'), null, 'bootstrap must invalidate search identities');
      assert.equal(sandbox.__searchTest.getSelectedDetail().entity_id, 'fixture-new-owner');
      assert.equal(context.selection.entity_id, 'fixture-new-owner');
      assert.equal(context.data.market_snapshot_id, 'MIS-REFRESHED');
      assert.equal(selected.length, 1, 'refresh must not fetch a new chart');
      await sandbox.HomeMarketWorkspace.selectInstrument('020000.TW');
      assert.equal(context.selection.entity_id, 'fixture-new-owner');
      assert.equal(context.data.market_snapshot_id, 'MIS-REFRESHED');
      assert.equal(rendered.at(-1).entity_id, 'fixture-new-owner');
      assert.equal(requests.length, 1, 'bootstrap refresh and selection must not fetch search data');
      assert.equal(sends, 0);
    }
  } else {
    assert.deepEqual(selected, []);
    if (['outage', 'mismatch'].includes(mode)) {
      assert.equal(requests.length, 1);
      assert.equal(opened, 0);
      assert.equal(nodes.globalAgentPrompt.value, '');
      assert.equal(nodes.homeActionFeedback.dataset.state, 'warning');
    } else {
      assert.equal(nodes.globalAgentPrompt.value, query);
      assert.ok(nodes.homeActionFeedback.textContent.includes('請自行按送出'));
      assert.equal(requests.length, mode === 'miss' ? 1 : 2);
    }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


@pytest.mark.skipif(NODE is None, reason="Node.js is required for controller behavior tests")
@pytest.mark.parametrize("scenario", ["outside", "enter", "miss", "canonical_miss", "wrong_entity", "outage", "mismatch", "explicit", "alert", "paper", "refresh_identity"])
def test_home_search_is_read_only_until_explicit_agent_action(scenario):
    result = subprocess.run([NODE, "-e", HARNESS, str(SCRIPT), scenario], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
