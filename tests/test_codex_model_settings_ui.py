from __future__ import annotations

import ast
import json
from pathlib import Path
import subprocess

import pytest


STATIC = Path(__file__).resolve().parents[1] / "src/stock_ai/ui/static"


def run_ui(script: str) -> None:
    harness = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor() { this.children = []; this.value = ''; this.textContent = ''; this.disabled = false; this.attributes = {}; this.listeners = {}; this.style = {}; }
  replaceChildren(...children) { this.children = children; this.value = children[0]?.value || ''; }
  append(...children) { this.children.push(...children); }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  emit(type, payload = {}) { this.listeners[type]?.forEach(fn => fn(payload)); }
  focus() { document.activeElement = this; }
  contains(target) { return this === target || this.children.some(child => child.contains(target)); }
  getBoundingClientRect() { return {left: 300, top: 600, bottom: 630, height: 350}; }
  querySelectorAll(selector) {
    const all = this.children.flatMap(child => [child, ...child.querySelectorAll('*')]);
    return selector === '*' ? all : all.filter(child => child.attributes.role === 'option' && !child.disabled && (!selector.includes('aria-selected') || child.attributes['aria-selected'] === 'true'));
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}
const ids = ['agentCodexModel', 'agentCodexReasoningEffort', 'agentCodexModelDescription', 'agentCodexEffortDescription', 'agentCodexModelState', 'refreshAgentCodexModels', 'agentDefaultDriver', 'agentSettingsStatus', 'agentCodexModelButton', 'agentCodexEffortButton', 'agentComposerModelButton', 'agentComposerEffortButton', 'agentComposerCodexSettings', 'agentComposerModelStatus'];
const elements = Object.fromEntries(ids.map(id => [id, new Element()]));
global.$ = id => elements[id] || null;
global.state = {};
global.window = global;
global.document = Object.assign(new Element(), { body: new Element(), createElement: () => new Element(), querySelectorAll: () => [], dispatchEvent: () => {} });
global.innerWidth = 640;
global.innerHeight = 700;
global.addEventListener = () => {};
global.CustomEvent = class { constructor(type, value) { this.type = type; this.detail = value?.detail; } };
global.agentControlApi = path => path;
global.agentControlHeaders = () => ({ 'Content-Type': 'application/json' });
global.discoverAgentModels = () => {};
const catalog = { items: [
  { id: 'gpt-daybreak-blue-latest', model: 'gpt-daybreak-blue-latest', display_name: 'Daybreak Blue', description: 'Cloud model', is_default: true, default_reasoning_effort: 'high', supported_reasoning_efforts: [{reasoning_effort: 'high', description: 'More reasoning'}, {reasoning_effort: 'ultra', description: 'Extended reasoning'}] },
  { id: 'future-model', model: 'future-model', display_name: 'Future Model', supported_reasoning_efforts: [{reasoning_effort: 'new-effort', description: 'From server'}] },
] };
"""
    sources = ["js/features/agent-codex-models.js", "js/features/agent-runtime.js"]
    harness += "\n".join(
        f"vm.runInThisContext(fs.readFileSync({json.dumps(str(STATIC / path))}, 'utf8'));"
        for path in sources
    )
    completed = subprocess.run(
        ["node", "-e", harness + "\n(async () => {\n" + script + "\n})().then(() => { process.stdout.write('UI_SCENARIO_PASSED\\n'); }).catch(error => { console.error(error); process.exitCode = 1; });"],
        capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert "UI_SCENARIO_PASSED" in completed.stdout, "Node exited before the asynchronous scenario completed"


def test_codex_catalog_accepts_all_models_and_only_server_supported_efforts():
    run_ui(r"""
global.api = async path => { assert.equal(path, '/api/agents/providers/codex/models'); return catalog; };
await discoverAgentCodexModels();
assert.equal($('agentCodexModel').children[1].textContent, 'Daybreak Blue · 預設');
assert.deepEqual($('agentCodexReasoningEffort').children.map(item => item.value), ['', 'high', 'ultra']);
$('agentCodexReasoningEffort').value = 'ultra';
$('agentCodexModel').value = 'future-model';
changeAgentCodexModel();
assert.deepEqual($('agentCodexReasoningEffort').children.map(item => item.value), ['', 'new-effort']);
assert.equal($('agentCodexReasoningEffort').value, '');
assert.equal(state.agentSettingsFormDirty, true);
assert.match($('agentCodexModelState').textContent, /不支援/);
assert.equal($('agentCodexReasoningEffort').children[1].textContent, 'new-effort');
""")


def test_codex_catalog_failure_and_missing_values_preserve_saved_selection():
    run_ui(r"""
renderAgentCodexModels({ model: 'retired-model', reasoningEffort: 'saved-effort' });
global.api = async () => { throw new Error('Codex unavailable'); };
await discoverAgentCodexModels();
assert.equal($('agentCodexModel').value, 'retired-model');
assert.equal($('agentCodexReasoningEffort').value, 'saved-effort');
assert.match($('agentCodexModelState').textContent, /Codex unavailable/);
assert.equal($('refreshAgentCodexModels').disabled, false);
assert.equal($('agentCodexModel').attributes['aria-busy'], 'false');
global.api = async () => catalog;
await discoverAgentCodexModels();
assert.equal($('agentCodexModel').value, 'retired-model');
assert.equal($('agentCodexReasoningEffort').value, 'saved-effort');
assert.match($('agentCodexModel').children.at(-1).textContent, /目前不可用/);
assert.equal($('agentCodexModel').children.at(-1).disabled, true);
""")


def test_codex_catalog_refresh_does_not_overwrite_edits_made_while_loading():
    run_ui(r"""
state.agentCodexModels = catalog.items;
renderAgentCodexModels({ model: 'gpt-daybreak-blue-latest', reasoningEffort: 'ultra' });
let resolve;
global.api = () => new Promise(done => { resolve = done; });
const pending = discoverAgentCodexModels();
assert.equal($('refreshAgentCodexModels').disabled, true);
$('agentCodexModel').value = 'future-model';
changeAgentCodexModel();
$('agentCodexReasoningEffort').value = 'new-effort';
resolve(catalog);
await pending;
assert.equal($('agentCodexModel').value, 'future-model');
assert.equal($('agentCodexReasoningEffort').value, 'new-effort');
assert.equal(state.agentSettingsFormDirty, true);
""")


def test_codex_settings_load_save_and_dirty_form_use_existing_flow():
    run_ui(r"""
state.agentCodexModelStatus = 'ready';
state.agentCodexModels = catalog.items;
let saved = { default_driver: 'codex', codex: { model: 'gpt-daybreak-blue-latest', reasoning_effort: 'ultra' } };
let posted;
global.api = async (path, options) => {
  if (path === '/api/agents') return {};
  if (options?.method === 'POST') {
    posted = JSON.parse(options.body);
    saved = { default_driver: posted.default_driver, codex: { model: posted.codex_model, reasoning_effort: posted.codex_reasoning_effort } };
  }
  return saved;
};
await loadAgentRuntimeSettings();
assert.equal($('agentCodexModel').value, 'gpt-daybreak-blue-latest');
assert.equal($('agentCodexReasoningEffort').value, 'ultra');
assert.equal(window.__stockAIActiveAgent.model, 'gpt-daybreak-blue-latest');
$('agentCodexModel').value = 'future-model';
changeAgentCodexModel();
$('agentCodexReasoningEffort').value = 'new-effort';
await loadAgentRuntimeSettings();
assert.equal($('agentCodexModel').value, 'future-model');
await saveAgentRuntimeSettings();
assert.equal(posted.codex_model, 'future-model');
assert.equal(posted.codex_reasoning_effort, 'new-effort');
assert.equal(state.agentSettingsFormDirty, false);
$('agentCodexModel').value = '';
$('agentCodexReasoningEffort').value = '';
await saveAgentRuntimeSettings();
assert.equal(posted.codex_model, '');
assert.equal(posted.codex_reasoning_effort, '');
assert.match($('agentSettingsStatus').textContent, /新任務/);
""")


@pytest.mark.parametrize("surface", ["composer", "settings"])
@pytest.mark.parametrize("stale_outcome", ["success", "error"])
def test_settings_refresh_cannot_overwrite_a_later_successful_save(surface, stale_outcome):
    run_ui(f"const surface = {json.dumps(surface)}; const staleOutcome = {json.dumps(stale_outcome)};\n" + r"""
state.agentCodexModels = catalog.items;
state.agentCodexModelStatus = 'ready';
const oldSettings = {default_driver: 'codex', codex: {model: 'gpt-daybreak-blue-latest', reasoning_effort: 'high'}};
state.agentSettings = oldSettings;
let serverSettings = oldSettings;
let staleGet;
global.api = (path, options = {}) => {
  if (path === '/api/agents') return Promise.resolve({});
  if (options.method === 'POST') {
    const body = JSON.parse(options.body);
    serverSettings = {default_driver: body.default_driver, codex: {model: body.codex_model, reasoning_effort: body.codex_reasoning_effort}};
    return Promise.resolve(serverSettings);
  }
  if (!staleGet) return new Promise((resolve, reject) => { staleGet = {resolve, reject}; });
  return Promise.resolve(serverSettings);
};
const refresh = loadAgentRuntimeSettings();
if (surface === 'composer') {
  openAgentCodexPopover($('agentComposerModelButton'), 'model', 'composer');
  await selectAgentCodexPickerValue('future-model');
} else {
  $('agentCodexModel').value = 'future-model';
  $('agentCodexReasoningEffort').value = 'new-effort';
  await saveAgentRuntimeSettings();
}
assert.equal(state.agentSettings.codex.model, 'future-model');
assert.equal(state.agentSettingsRevision, 1);
const settingsStatus = $('agentSettingsStatus').textContent;
if (staleOutcome === 'success') staleGet.resolve(oldSettings);
else staleGet.reject(new Error('Old refresh failed'));
await refresh;
assert.equal(state.agentSettings.codex.model, 'future-model');
assert.equal(window.__stockAIActiveAgent.model, 'future-model');
assert.equal($('agentCodexModel').value, 'future-model');
assert.match($('agentComposerModelButton').textContent, /Future Model/);
assert.equal($('agentSettingsStatus').textContent, settingsStatus);
""")


def test_settings_refresh_ignores_an_older_read_after_a_newer_read_finishes():
    run_ui(r"""
state.agentCodexModelStatus = 'ready';
const pending = [];
global.api = path => path === '/api/agents' ? Promise.resolve({}) : new Promise(resolve => pending.push(resolve));
const older = loadAgentRuntimeSettings();
const newer = loadAgentRuntimeSettings();
pending[1]({default_driver: 'codex', codex: {model: 'future-model', reasoning_effort: 'new-effort'}});
await newer;
pending[0]({default_driver: 'codex', codex: {model: 'gpt-daybreak-blue-latest', reasoning_effort: 'high'}});
await older;
assert.equal(state.agentSettings.codex.model, 'future-model');
assert.equal(window.__stockAIActiveAgent.model, 'future-model');
""")


def test_codex_popover_keyboard_selection_and_outside_close():
    run_ui(r"""
state.agentCodexModels = catalog.items;
state.agentCodexModelStatus = 'ready';
renderAgentCodexModels({model: 'gpt-daybreak-blue-latest', reasoningEffort: 'ultra'});
bindAgentCodexPickers();
const anchor = $('agentCodexModelButton');
anchor.emit('click');
assert.equal(anchor.attributes['aria-expanded'], 'true');
assert.equal(agentCodexPopover.children[0].textContent, '選取模型');
assert.equal(document.activeElement.attributes['aria-selected'], 'true');
const options = agentCodexPopover.querySelectorAll('[role="option"]:not(:disabled)');
assert.equal(options[0].children[2].textContent, '推薦的模型組合');
assert.equal(options[1].children[1].textContent, '✓');
agentCodexPopover.emit('keydown', {key: 'End', preventDefault() {}});
assert.equal(document.activeElement, options.at(-1));
document.activeElement.emit('click');
assert.equal($('agentCodexModel').value, 'future-model');
assert.equal(agentCodexPopover.hidden, true);
anchor.emit('keydown', {key: 'ArrowDown', preventDefault() {}});
document.emit('keydown', {key: 'Escape', preventDefault() {}, stopPropagation() {}});
assert.equal(agentCodexPopover.hidden, true);
assert.equal(document.activeElement, anchor);
anchor.emit('click');
document.emit('pointerdown', {target: document.body});
assert.equal(agentCodexPopover.hidden, true);
assert.ok(Number.parseFloat(agentCodexPopover.style.left) + Number.parseFloat(agentCodexPopover.style.width) <= innerWidth);
""")


def test_composer_picker_saves_only_codex_fields_and_keeps_unsaved_form_changes():
    run_ui(r"""
state.agentCodexModels = catalog.items;
state.agentCodexModelStatus = 'ready';
state.agentSettings = {default_driver: 'codex', codex: {model: 'gpt-daybreak-blue-latest', reasoning_effort: 'ultra'}};
state.agentSettingsFormDirty = true;
let posted;
global.api = async (path, options) => {
  assert.equal(path, '/api/agents/settings');
  posted = JSON.parse(options.body);
  return {default_driver: 'codex', codex: {model: posted.codex_model, reasoning_effort: posted.codex_reasoning_effort}};
};
openAgentCodexPopover($('agentComposerModelButton'), 'model', 'composer');
await selectAgentCodexPickerValue('future-model');
assert.deepEqual(posted, {default_driver: 'codex', codex_model: 'future-model', codex_reasoning_effort: ''});
assert.equal(state.agentSettingsFormDirty, true);
assert.equal(window.__stockAIActiveAgent.model, 'future-model');
assert.match($('agentComposerModelButton').textContent, /Future Model/);
global.api = async () => { throw new Error('Save failed'); };
openAgentCodexPopover($('agentComposerModelButton'), 'model', 'composer');
await selectAgentCodexPickerValue('gpt-daybreak-blue-latest');
assert.equal(state.agentSettings.codex.model, 'future-model');
assert.match($('agentComposerModelStatus').textContent, /Save failed/);
assert.equal($('agentComposerModelButton').disabled, false);
""")


def test_codex_model_names_preserve_server_catalog_and_readable_gpt_names():
    run_ui(r"""
assert.equal(agentCodexModelLabel({display_name: 'GPT-6-Astra'}), 'GPT-6 Astra');
assert.equal(agentCodexModelLabel({display_name: 'GPT-5.3-Codex-Spark'}), 'GPT-5.3 Codex Spark');
assert.equal(agentCodexModelLabel({display_name: 'Daybreak Blue'}), 'Daybreak Blue');
assert.equal(agentCodexModelLabel({display_name: 'Future-v2-custom'}), 'Future-v2-custom');
""")


def test_task_submission_waits_for_composer_model_save_and_rejects_failure():
    run_ui(r"""
state.agentCodexModels = catalog.items;
state.agentCodexModelStatus = 'ready';
state.agentSettings = {default_driver: 'codex', codex: {model: '', reasoning_effort: ''}};
let rejectSave;
global.api = () => new Promise((resolve, reject) => { rejectSave = reject; });
openAgentCodexPopover($('agentComposerModelButton'), 'model', 'composer');
const save = selectAgentCodexPickerValue('future-model');
let submitted = false;
const task = waitForAgentCodexSettingsSave().then(() => { submitted = true; });
await Promise.resolve();
assert.equal(submitted, false);
rejectSave(new Error('Settings rejected'));
await save;
await assert.rejects(task, /任務未送出/);
assert.equal(submitted, false);
assert.equal(state.agentSettingsRevision || 0, 0);
""")


def test_actual_codex_session_receipt_is_projected_without_requested_value_fallback():
    reducer = json.dumps(str(STATIC / "js/features/agent/agent-event-reducer.js"))
    timeline = json.dumps(str(STATIC / "js/features/agent/agent-runtime-timeline.js"))
    run_ui(f"vm.runInThisContext(fs.readFileSync({reducer}, 'utf8'));\nvm.runInThisContext(fs.readFileSync({timeline}, 'utf8'));\n" + r"""
const event = {type: 'model.session.configured', run_id: 'R1', session_id: 'S1', event_id: 'E1', sequence: 1, payload: {provider: 'codex', model: 'actual-model', reasoning_effort: 'high', selected_model: 'requested-model', resolution_source: 'sdk_thread_start'}};
const next = AgentEventReducer.reduce({runs: {R1: {status: 'running'}}}, event);
assert.equal(next.runs.R1.model, 'actual-model');
assert.equal(next.runs.R1.reasoning_effort, 'high');
assert.equal(AgentRuntimeTimeline.shouldShow(event), true);
assert.match(AgentRuntimeTimeline.semanticSummary(event), /actual-model.*high/);
assert.doesNotMatch(AgentRuntimeTimeline.semanticSummary({...event, payload: {selected_model: 'requested-model'}}), /requested-model/);
""")


def test_codex_popover_reserves_native_toolbar_but_not_browser_top_area():
    run_ui(r"""
state.agentCodexModels = catalog.items;
state.agentCodexModelStatus = 'ready';
document.documentElement = {dataset: {nativeLiquidGlass: 'appkit'}};
document.querySelector = selector => selector === '.topbar' ? {getBoundingClientRect: () => ({top: 46, bottom: 119, height: 73})} : null;
const anchor = $('agentCodexEffortButton');
anchor.getBoundingClientRect = () => ({left: 300, top: 450, bottom: 480, height: 30});
openAgentCodexPopover(anchor, 'effort', 'settings');
let top = Number.parseFloat(agentCodexPopover.style.top);
let maxHeight = Number.parseFloat(agentCodexPopover.style.maxHeight);
assert.ok(top >= 127, `Native popup overlaps toolbar: ${top}`);
assert.ok(top + maxHeight <= innerHeight - 8);
assert.ok(maxHeight < 350, 'Native popup should scroll within safe area');
document.documentElement.dataset.nativeLiquidGlass = '';
positionAgentCodexPopover();
top = Number.parseFloat(agentCodexPopover.style.top);
assert.ok(top < 127, 'Ordinary browser should retain its available top area');
document.documentElement.dataset.nativeLiquidGlass = 'appkit';
document.querySelector = () => ({getBoundingClientRect: () => ({bottom: 119, height: 0})});
positionAgentCodexPopover();
assert.equal(Number.parseFloat(agentCodexPopover.style.top), top, 'Hidden native toolbar must not reserve space');
""")


def test_dock_header_keeps_completed_run_model_when_next_task_settings_change():
    modules = ["agent-event-reducer.js", "agent-dock-store.js", "agent-dock-controller.js"]
    script = r"""
global.localStorage = {getItem: () => null, setItem() {}};
global.requestAnimationFrame = fn => fn();
document.readyState = 'loading';
document.documentElement = {style: {setProperty() {}}, classList: {toggle() {}}};
for (const id of ['agentDock', 'agentDockIdentity', 'agentDockConnection', 'agentRunStatus']) {
  elements[id] = new Element();
  elements[id].style.setProperty = () => {};
  elements[id].classList = {toggle() {}};
}
document.getElementById = id => elements[id] || null;
global.AgentStreamController = class {};
global.AgentSessionController = class {async restoreForeground() {}};
for (const name of ['AgentContextBar', 'AgentPlanView', 'AgentConversationView', 'AgentTaskTreeView', 'AgentArtifactView', 'AgentControlBar']) global[name] = {render() {}};
global.AgentComposer = {init() {}};
global.AgentDockFormatters = {status: value => ({label: value, key: value, icon: '○'})};
const receipt = {provider: 'codex', model: 'gpt-5.6-sol', reasoning_effort: 'medium', resolution_source: 'sdk_thread_start'};
const snapshot = {run: {run_id: 'R1', session_id: 'S1', driver: 'codex', status: 'completed', terminal: true, result: {provider_model_metadata: receipt}}, events: []};
global.AgentRuntimeApi = async path => path.endsWith('/settings')
  ? {default_driver: 'codex', codex: {model: 'gpt-6-astra', reasoning_effort: 'ultra'}}
  : path.endsWith('/snapshot') ? snapshot : {};
"""
    script += "\n".join(
        f"vm.runInThisContext(fs.readFileSync({json.dumps(str(STATIC / 'js/features/agent' / name))}, 'utf8'));"
        for name in modules
    )
    script += r"""
AgentDockStore.set({runs: {R1: {run_id: 'R1', session_id: 'S1', status: 'completed'}}, active_session_id: 'S1'});
await AgentDockController.init();
assert.equal($('agentDockIdentity').textContent, 'gpt-5.6-sol · medium · codex');
document.emit('stock-ai-active-agent-changed', {detail: {id: 'codex', model: 'gpt-6-astra', reasoning_effort: 'ultra'}});
assert.equal($('agentDockIdentity').textContent, 'gpt-5.6-sol · medium · codex');
assert.equal($('agentDockConnection').textContent, 'Advisory · completed');
// Reconnect may replay an actual receipt before a result-less snapshot.
AgentDockStore.dispatch({type: 'model.session.configured', run_id: 'R2', session_id: 'S2', event_id: 'E2', sequence: 1, payload: receipt});
AgentDockStore.applySnapshot({run: {run_id: 'R2', session_id: 'S2', driver: 'codex', status: 'running'}});
assert.equal($('agentDockIdentity').textContent, 'gpt-5.6-sol · medium · codex');
assert.deepEqual(AgentDockStore.getState().runs.R2.provider_model_metadata, receipt);
// A fresh snapshot with only final_result also restores the actual model.
AgentDockStore.applySnapshot({run: {run_id: 'R3', session_id: 'S3', driver: 'codex', status: 'completed', terminal: true}, final_result: {provider_model_metadata: receipt}});
assert.equal($('agentDockIdentity').textContent, 'gpt-5.6-sol · medium · codex');
// An unresolved Run cannot borrow either a different Run or next-task preferences.
AgentDockStore.applySnapshot({run: {run_id: 'R4', session_id: 'S4', driver: 'codex', status: 'queued'}});
assert.equal($('agentDockIdentity').textContent, 'codex');
AgentDockStore.dispatch({type: 'model.session.configured', run_id: 'R4', session_id: 'S4', event_id: 'E4', sequence: 1, payload: {provider: 'codex', model: null, reasoning_effort: null, selected_model: 'gpt-6-astra', selected_reasoning_effort: 'ultra'}});
assert.equal($('agentDockIdentity').textContent, 'codex');
AgentDockStore.set({active_run_id: null, active_session_id: 'new-empty-session'});
assert.equal($('agentDockIdentity').textContent, 'gpt-6-astra · ultra · codex');
"""
    run_ui(script)


def test_fast_follow_up_receipt_scenario_without_launching_a_browser():
    # Execute the existing E2E's JavaScript scenario in Node. This scenario
    # checks request ordering and does not need a browser layout engine.
    source = (STATIC.parents[3] / "tests/e2e/test_agent_dock_browser.py").read_text(encoding="utf-8")
    scenario = next(
        node for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef)
        and node.name == "test_fast_natural_follow_up_waits_for_the_new_run_receipt"
    )
    expression = next(
        node.args[0].value for node in ast.walk(scenario)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "evaluate"
    )
    controller = json.dumps(str(STATIC / "js/features/agent/agent-dock-controller.js"))
    run_ui(r"""
document.readyState = 'loading';
document.getElementById = id => id === 'agentAutonomySelect' ? {value: 'advisory'} : null;
document.head = {append: script => vm.runInThisContext(script.textContent)};
""" + f"const runScenario = ({expression});\nconst result = await runScenario(fs.readFileSync({controller}, 'utf8'));\n" + r"""
assert.deepEqual(result.callsBeforeReceipt, ['run']);
assert.deepEqual(result.calls.map(call => call.kind), ['run', 'message']);
assert.equal(result.calls[1].sessionId, 'AS-new');
assert.equal(result.activeSession, 'AS-new');
assert.equal(result.activeRun, 'AR-new');
""")
