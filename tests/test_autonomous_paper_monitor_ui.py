"""Exercise account isolation and remaining-risk rendering without a server."""
from pathlib import Path
import json
import shutil
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "src/stock_ai/ui/static/paper-training.js"


def run_monitor(scenario: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    harness = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const elements = Object.fromEntries(['Monitor', 'Refresh', 'Status', 'Summary', 'Review', 'Plans', 'Orders', 'Positions', 'Alerts'].map(part => [`autonomousPaper${part}`, {innerHTML: '', textContent: '', dataset: {}, disabled: false}]));
elements.paperTrainingSummary = {innerHTML: 'manual-account-must-stay-separate'};
global.window = global;
global.document = {readyState: 'loading', querySelector: () => ({}), getElementById: id => elements[id] || null, addEventListener: () => {}};
const valid = () => ({
  account_id: 'autonomous-paper-v1', mode: 'paper', enabled: true,
  account_observed_at: '2026-09-12T09:00:00+00:00', plans: [],
  account: {account_id: 'autonomous-paper-v1', base_currency: 'TWD', total_equity: 99999, available_cash: 80000, cash_balance: 85000, fill_count: 2, positions: [], open_order_reservations: []},
  model_review: {enabled: true, used_today: 1, daily_limit: 1, remaining_today: 0, max_steps: 12, reviews: [], provider_model_selection: {provider: 'codex', model: 'configured-model', reasoning_effort: 'medium'}},
});
"""
    harness += "\nlet source = fs.readFileSync(" + json.dumps(str(SCRIPT)) + ", 'utf8');\n"
    harness += r"source = source.replace(/\}\)\(\);\s*$/, 'globalThis.monitor = {renderAutonomousStatus, loadAutonomousStatus};})();'); vm.runInThisContext(source);" + "\n"
    harness += "(async () => {\n" + scenario + "\n})().then(() => process.stdout.write('MONITOR_OK')).catch(error => { console.error(error); process.exitCode = 1; });"
    result = subprocess.run([node, "-e", harness], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "MONITOR_OK" in result.stdout


def test_monitor_preserves_unfilled_exit_and_account_scoped_holdings() -> None:
    run_monitor(r"""
const data = valid();
data.plans = [{account_id: data.account_id, plan_id: 'plan-1', symbol: '<unsafe>', definition: {stop_loss: 95, exit_not_after: '2026-10-01T00:00:00Z'}, state: {status: 'exit_submitted', filled_quantity: 100, remaining_quantity: 75, exit_alert: {reason: 'exit_price_floor_reached', remaining_quantity: 75, order_id: 'exit-1', replacement_count: 2}}}];
data.account.open_order_reservations = [{account_id: data.account_id, order_id: 'exit-1', symbol: '<unsafe>', side: 'sell', status: 'partially_filled', remaining_quantity: 75, reservation_price: 95, blockers: []}];
data.account.positions = [{account_id: data.account_id, symbol: '<unsafe>', quantity: 75, average_cost: 100, market_value: 7000}];
monitor.renderAutonomousStatus(data);
assert.match(elements.autonomousPaperPlans.innerHTML, /出場委託中/);
assert.match(elements.autonomousPaperPlans.innerHTML, /剩餘 75 股/);
assert.match(elements.autonomousPaperPlans.innerHTML, /停損 TWD 95/);
assert.match(elements.autonomousPaperOrders.innerHTML, /部分成交/);
assert.match(elements.autonomousPaperAlerts.innerHTML, /行情已低於允許的最低賣價/);
assert.match(elements.autonomousPaperPositions.innerHTML, /75 股/);
assert.match(elements.autonomousPaperPlans.innerHTML, /&lt;unsafe&gt;/);
assert.doesNotMatch(elements.autonomousPaperPlans.innerHTML, /<unsafe>|已確認平倉/);
assert.equal(elements.paperTrainingSummary.innerHTML, 'manual-account-must-stay-separate');
data.plans[0].state.exit_alert = {reason: 'broker_preview_or_frozen_budget', remaining_quantity: 75, preview_blockers: ['limit_price_below_daily_limit', '<unsafe>']};
monitor.renderAutonomousStatus(data);
assert.match(elements.autonomousPaperAlerts.innerHTML, /限價低於當日允許價格範圍/);
assert.match(elements.autonomousPaperAlerts.innerHTML, /尚未取得委託/);
assert.match(elements.autonomousPaperAlerts.innerHTML, /&lt;unsafe&gt;/);
assert.doesNotMatch(elements.autonomousPaperAlerts.innerHTML, /<unsafe>/);
""")


def test_empty_plan_and_missing_values_do_not_claim_no_opportunity_or_zero() -> None:
    run_monitor(r"""
const data = valid();
delete data.account.total_equity;
data.model_review.reviews = [{status: 'partially_completed', run_id: 'run-1', error: 'cost_budget_exhausted'}];
monitor.renderAutonomousStatus(data);
assert.match(elements.autonomousPaperPlans.innerHTML, /尚未建立自主交易計畫/);
assert.doesNotMatch(elements.autonomousPaperPlans.innerHTML, /沒有.*機會/);
assert.match(elements.autonomousPaperSummary.innerHTML, /自主帳戶權益<\/span><strong>未提供/);
assert.match(elements.autonomousPaperReview.innerHTML, /執行未完成.*cost_budget_exhausted/);
assert.match(elements.autonomousPaperReview.innerHTML, /剩餘 0 次/);
assert.equal(elements.autonomousPaperAlerts.innerHTML, '');
""")


def test_monitor_shows_coverage_denominator_and_missing_domains_without_claiming_completion() -> None:
    run_monitor(r"""
const data = valid();
data.security_research_coverage = {
  status: 'available', observed_at: '2026-09-13T04:00:00+00:00', security_count: 58056,
  new_entry_eligible_count: 1936, complete_deep_coverage: false, all_domains_current: false,
  universe_expires_at: '2026-09-14T07:30:00+00:00',
  deep_research_status_counts: {current: 5, stale: 2, failed: 3, never_researched: 58046},
  data_domain_counts: {
    daily_price: {needs_update: 1200}, price_history: {needs_update: 58051},
    financials: {needs_update: 50000}, news_events: {needs_update: 57000},
  },
};
monitor.renderAutonomousStatus(data);
assert.match(elements.autonomousPaperSummary.innerHTML, /逐檔覆蓋帳本<\/span><strong>58,056 檔/);
assert.match(elements.autonomousPaperSummary.innerHTML, /可新增部位 1,936 檔/);
assert.match(elements.autonomousPaperReview.innerHTML, /快照內深入 5 · 過期 2 · 失敗 3 · 尚未深入 58,046/);
assert.match(elements.autonomousPaperReview.innerHTML, /歷史行情待更新 58,051/);
assert.match(elements.autonomousPaperReview.innerHTML, /快照有效至 2026-09-14T07:30:00\+00:00/);
assert.match(elements.autonomousPaperReview.innerHTML, /有狀態不代表資料齊全或策略已驗證/);
""")


def test_refresh_is_coalesced_get_and_mismatched_account_clears_stale_data() -> None:
    run_monitor(r"""
monitor.renderAutonomousStatus(valid());
let resolveRequest;
let calls = 0;
global.fetch = (url, options) => {
  calls++;
  assert.match(url, /^\/agent\/autonomy\/status\?/);
  assert.equal(options.method || 'GET', 'GET');
  return new Promise(resolve => { resolveRequest = resolve; });
};
const first = monitor.loadAutonomousStatus();
const second = monitor.loadAutonomousStatus();
assert.equal(calls, 1);
assert.equal(first, second);
assert.equal(elements.autonomousPaperRefresh.disabled, true);
const invalid = valid();
invalid.account.account_id = 'manual-paper-account';
resolveRequest({ok: true, json: async () => invalid});
await first;
assert.equal(elements.autonomousPaperRefresh.disabled, false);
assert.equal(elements.autonomousPaperSummary.innerHTML, '');
assert.equal(elements.autonomousPaperPlans.innerHTML, '');
assert.match(elements.autonomousPaperStatus.textContent, /讀取失敗.*帳戶識別不一致/);
assert.equal(elements.paperTrainingSummary.innerHTML, 'manual-account-must-stay-separate');
global.fetch = async () => { throw new Error('network unavailable'); };
await monitor.loadAutonomousStatus();
assert.match(elements.autonomousPaperStatus.textContent, /network unavailable/);
""")
