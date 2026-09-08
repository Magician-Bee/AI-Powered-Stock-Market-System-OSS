from __future__ import annotations

import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "stock_ai" / "ui" / "static"
AGENT = STATIC / "js" / "features" / "agent"


def test_right_agent_dock_has_persistent_workspace_structure_and_accessible_controls():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "css" / "features" / "agent-dock.css").read_text(encoding="utf-8")
    store = (AGENT / "agent-dock-store.js").read_text(encoding="utf-8")

    for node_id in (
        "agentDock",
        "agentContextBar",
        "agentPinnedPlan",
        "agentChatFeed",
        "agentTasksTree",
        "agentArtifactsList",
        "agentRunControls",
        "agentComposerInput",
        "agentDockResizer",
            "agentConsoleNav",
    ):
        assert f'id="{node_id}"' in html
    assert 'role="complementary"' in html
    assert 'aria-label="調整 Stock AI Agent 寬度"' in html
    assert "position:fixed" in css
    assert "@media(max-width:1023px)" in css
    assert "@media(prefers-reduced-motion:reduce)" in css
    assert "--agent-dock-width:320px" in css
    assert "Math.min(persistedDockWidth, 320)" in store
    assert "persisted.layout_version === 4" not in store


def test_agent_dock_navigation_controls_replace_the_redundant_header_actions():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "css" / "features" / "agent-dock.css").read_text(encoding="utf-8")
    controller = (AGENT / "agent-dock-controller.js").read_text(encoding="utf-8")

    assert 'id="agentConsoleNav"' in html
    assert 'id="agentDockVisibilityNav"' not in html
    assert '<strong>開啟 Agent 控制台</strong>' in html
    assert 'aria-expanded="false"' in html
    assert 'aria-controls="agentDock"' in html
    assert 'id="agentDockMaximize"' not in html
    assert 'id="agentDockSettings"' not in html
    assert 'id="globalAgentActivityBtn"' not in html
    assert "agent-dock-visibility-toggle" not in css
    assert ".agent-dock:not(.is-open)" not in css
    assert ".agent-console-nav>strong" in css
    assert ".agent-dock.is-maximized" not in css
    assert "agent-console-open" not in css
    assert "dock.classList.toggle('is-maximized'" not in controller
    assert "consoleToggle.querySelector('strong')?.replaceChildren(label)" in controller
    assert "const label = state.dock_open ? '收合 Agent 控制台' : '開啟 Agent 控制台';" in controller
    assert "store.set({ dock_open: !state.dock_open, dock_maximized: false });" in controller
    assert "click('agentConsoleNav'" in controller
    assert "maximize: () =>" not in controller
    assert "restore: () =>" not in controller
    assert "close: () => store.set({ dock_open: false, dock_maximized: false })" in controller


def test_agent_context_does_not_promote_the_visible_stock_to_a_task_stock():
    context_bar = (AGENT / "agent-context-bar.js").read_text(encoding="utf-8")

    assert "const runSymbols = payload.symbols || run.symbols || run.request?.symbols || [];" in context_bar
    assert "const taskSymbols = runSymbols.length ? runSymbols : explicitSymbols;" in context_bar
    assert "run.autonomy || run.request?.autonomy || 'advisory'" in context_bar
    assert "const run = state.runs[state.active_run_id] || foregroundRun;" in context_bar
    assert "selectedSymbol ? [selectedSymbol]" not in context_bar
    assert "taskSymbols.join('、')" in context_bar


def test_task_forest_labels_work_items_without_claiming_parallel_branches():
    task_view = (AGENT / "agent-task-tree-view.js").read_text(encoding="utf-8")

    assert "const workCount = (plan?.nodes || []).length || steps.length;" in task_view
    assert "meta.textContent = `${workCount} 個工作項目`;" in task_view
    assert "} 個 Branch`" not in task_view


def test_session_restore_bounds_history_lookups_without_hiding_recent_foreground_runs():
    session_controller = (AGENT / "agent-session-controller.js").read_text(encoding="utf-8")

    assert "const FOREGROUND_RUN_CANDIDATE_LIMIT = 12;" in session_controller
    assert ".slice(0, FOREGROUND_RUN_CANDIDATE_LIMIT);" in session_controller
    assert "await Promise.all(candidates.map(async candidate =>" in session_controller


def test_home_does_not_have_a_unique_agent_maximize_control():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    home = (STATIC / "js" / "features" / "market-intelligence" / "workspace-home.js").read_text(encoding="utf-8")

    assert 'id="maximizeAgentFromHome"' not in html
    assert "maximizeAgentFromHome" not in home


def test_evidence_graph_uses_a_compact_workspace_summary_instead_of_raw_json():
    graph = AGENT / "agent-evidence-graph.js"
    script = f"""
global.window = global;
require({json.dumps(str(graph))});
const claim = AgentEvidenceGraph.formatEvidenceClaim({{
  schema_version: 'open_stock_ai.agent_workspace.v1',
  symbol: '2887.TW',
  recommendation_bucket: 'watch',
  execution_permission: 'blocked',
  nested: {{ raw: 'this must never become the node title' }}
}}, 'fallback');
process.stdout.write(claim);
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.stdout == "2887.TW · 持續觀察 · 執行已阻擋"


def test_desktop_shell_preserves_more_space_for_primary_market_content():
    base = (STATIC / "css" / "core" / "base.css").read_text(encoding="utf-8")
    material = (STATIC / "css" / "shell" / "material.css").read_text(encoding="utf-8")

    assert "--sidebar-width:188px" in base
    assert "--sidebar-main-offset:212px" in base
    assert "--sidebar-topbar-left:224px" in base
    assert "width:var(--sidebar-width)" in material
    assert "margin-left:var(--sidebar-main-offset)" in material


def test_agent_runtime_is_split_into_single_responsibility_modules():
    expected = {
        "agent-dock-store.js",
        "agent-event-reducer.js",
        "agent-stream-controller.js",
        "agent-session-controller.js",
        "agent-conversation-view.js",
        "agent-task-tree-view.js",
        "agent-tool-view.js",
        "agent-skill-view.js",
        "agent-approval-view.js",
        "agent-artifact-view.js",
        "agent-plan-view.js",
        "agent-context-bar.js",
        "agent-control-bar.js",
        "agent-composer.js",
        "agent-runtime-timeline.js",
        "agent-decision-card.js",
        "agent-artifact-canvas.js",
        "agent-artifact-selection.js",
        "agent-automation-view.js",
        "agent-observability-dashboard.js",
        "agent-evidence-graph.js",
        "agent-diff-view.js",
        "agent-composer-context.js",
        "agent-task-forest.js",
        "agent-dock-controller.js",
    }
    assert expected <= {path.name for path in AGENT.glob("*.js")}
    legacy = (STATIC / "js" / "features" / "agent-runtime.js").read_text(encoding="utf-8")
    assert len(legacy.splitlines()) < 180
    assert "EventSource" not in legacy
    assert "innerHTML" not in (AGENT / "agent-conversation-view.js").read_text(encoding="utf-8")
    session_controller = (AGENT / "agent-session-controller.js").read_text(encoding="utf-8")
    conversation_view = (AGENT / "agent-conversation-view.js").read_text(encoding="utf-8")
    assert "ordered_message_ids" in session_controller
    assert "stored-message" in conversation_view
    assert "message.content?.summary" in conversation_view
    assert "message.content?.text" in conversation_view
    assert "function userVisibleText(content)" in conversation_view
    assert "MODEL_TASK_KIND:[a-z_]+" in conversation_view
    assert "const publicPlanSummary = userVisibleText(" in conversation_view
    assert "準備協助目前工作區" in conversation_view
    assert "分析目前股票" in conversation_view
    assert "標記支撐壓力" in conversation_view
    assert "建立模擬交易" in conversation_view
    assert "AgentConversationView?.refreshIdle?.()" in (AGENT / "agent-context-bar.js").read_text(encoding="utf-8")
    assert "function idleActions(context)" in conversation_view
    assert "context.symbol && !String(context.symbol).startsWith('^')" in conversation_view
    assert "目前畫面中的股票" not in conversation_view
    assert "檢查模型連線" in conversation_view
    assert "請分析${context.symbol}" in conversation_view
    session_source = (AGENT / "agent-session-controller.js").read_text(encoding="utf-8")
    assert "new URL(path, window.location.origin)" in session_source
    assert "url.hostname === '127.0.0.1'" not in session_source
    assert "window.AgentRuntimeApi = runtimeApi" in session_source
    assert "window.AgentRuntimeApi('/api/agents/classify-intent'" in (AGENT / "agent-dock-controller.js").read_text(encoding="utf-8")
    assert "CLASSIFIED_SECURITY_ID" in (AGENT / "agent-dock-controller.js").read_text(encoding="utf-8")
    assert "CLASSIFIED_SECURITY_ID.test(value)" in (AGENT / "agent-dock-controller.js").read_text(encoding="utf-8")
    assert "未建立 Run，請確認模型連線後重試" in (AGENT / "agent-dock-controller.js").read_text(encoding="utf-8")
    assert "resolveSafeFallbackScope" not in (AGENT / "agent-dock-controller.js").read_text(encoding="utf-8")
    assert "INTENT_CLASSIFICATION_TIMEOUT_MS = 180_000" in (AGENT / "agent-dock-controller.js").read_text(encoding="utf-8")
    assert "controller.abort(), INTENT_CLASSIFICATION_TIMEOUT_MS" in (AGENT / "agent-dock-controller.js").read_text(encoding="utf-8")
    controller = (AGENT / "agent-dock-controller.js").read_text(encoding="utf-8")
    assert "stock-ai:chart-focus-change" in controller
    assert "agentDockFocusExpand" not in controller
    assert "agentDockFocusExpand" not in (STATIC / "index.html").read_text(encoding="utf-8")


def test_conversation_feed_is_scoped_to_the_selected_session():
    conversation = AGENT / "agent-conversation-view.js"
    script = f"""
global.window = global;
require({json.dumps(str(conversation))});
const entries = AgentConversationView.feedEntries({{
  active_session_id: 'AS-current',
  ordered_message_ids: ['M-current', 'M-old'],
  messages: {{
    'M-current': {{ message_id: 'M-current', session_id: 'AS-current', content: 'current' }},
    'M-old': {{ message_id: 'M-old', session_id: 'AS-old', content: 'old' }}
  }},
  ordered_event_ids: ['E-current', 'E-old'],
  events: {{
    'E-current': {{
      event_id: 'E-current', session_id: 'AS-current', run_id: 'AR-current',
      type: 'reasoning.summary', payload: {{ summary: 'current event' }}
    }},
    'E-old': {{
      event_id: 'E-old', session_id: 'AS-old', run_id: 'AR-old',
      type: 'reasoning.summary', payload: {{ summary: 'old event' }}
    }}
  }}
}});
process.stdout.write(JSON.stringify(entries.map(entry => entry.id || entry.event.event_id)));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert json.loads(completed.stdout) == ["M-current", "E-current"]


def test_plan_revision_removes_nodes_that_are_no_longer_in_the_current_plan():
    reducer = AGENT / "agent-event-reducer.js"
    script = f"""
global.window = global;
require({json.dumps(str(reducer))});
let state = {{
  active_run_id: null,
  events: {{}},
  ordered_event_ids: [],
  last_sequence_by_run: {{}},
  runs: {{}},
  plans: {{}},
  steps: {{}},
  tool_calls: {{}},
  approvals: {{}},
  artifacts: {{}},
  messages: {{}},
  skills: {{}},
  ordered_message_ids: []
}};
state = AgentEventReducer.reduce(state, {{
  event_id: 'E1', sequence: 1, type: 'plan.proposed',
  run_id: 'AR-current', session_id: 'AS-current',
  payload: {{ plan: {{
    plan_id: 'AP-current', revision_number: 1,
    nodes: [
      {{ node_id: 'keep', title: '保留', status: 'completed' }},
      {{ node_id: 'remove', title: '移除', status: 'pending' }}
    ]
  }} }}
}});
state = AgentEventReducer.reduce(state, {{
  event_id: 'E2', sequence: 2, type: 'plan.revised',
  run_id: 'AR-current', session_id: 'AS-current',
  payload: {{ plan: {{
    plan_id: 'AP-current', revision_number: 2,
    nodes: [{{ node_id: 'keep', title: '保留', status: 'completed' }}]
  }} }}
}});
process.stdout.write(JSON.stringify(Object.keys(state.steps)));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert json.loads(completed.stdout) == ["AR-current:keep"]


def test_persisted_session_restore_hydrates_durable_messages():
    session_controller = AGENT / "agent-session-controller.js"
    script = f"""
global.window = global;
const session = {{
  session_id: 'AS-current',
  active_run_id: 'AR-current',
  messages: [
    {{ message_id: 'M-user', session_id: 'AS-current', role: 'user', content: 'question' }},
    {{ message_id: 'M-agent', session_id: 'AS-current', role: 'assistant', content: 'answer' }}
  ]
}};
global.api = async path => {{
  if (path === '/api/agents/sessions/AS-current') return session;
  throw new Error(path);
}};
require({json.dumps(str(session_controller))});
let state = {{
  active_session_id: 'AS-current',
  active_run_id: null,
  sessions: {{}},
  messages: {{}},
  ordered_message_ids: [],
  events: {{}},
  ordered_event_ids: [],
  environment_snapshot: null
}};
const store = {{
  getState: () => state,
  set: patch => {{ state = {{ ...state, ...patch }}; }}
}};
(async () => {{
  const restored = await new AgentSessionController(store).ensure();
  process.stdout.write(JSON.stringify({{
    restored,
    activeRun: state.active_run_id,
    messageIds: state.ordered_message_ids,
    assistant: state.messages['M-agent'].content
  }}));
}})();
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert json.loads(completed.stdout) == {
        "restored": "AS-current",
        "activeRun": "AR-current",
        "messageIds": ["M-user", "M-agent"],
        "assistant": "answer",
    }


def test_startup_restores_host_recovery_session_over_stale_empty_ui_session():
    session_controller = AGENT / "agent-session-controller.js"
    script = f"""
global.window = global;
const recoveredSession = {{
  session_id: 'AS-recovery', active_run_id: 'AR-recovery', messages: []
}};
global.api = async path => {{
  if (path === '/api/agents/sessions?limit=100') return {{ items: [
    {{ session_id: 'AS-empty', active_run_id: null }},
    {{ session_id: 'AS-recovery', active_run_id: 'AR-recovery' }}
  ] }};
  if (path === '/api/agents/runs/AR-recovery') return {{
    run_id: 'AR-recovery', status: 'partially_completed'
  }};
  if (path === '/api/agents/sessions/AS-recovery') return recoveredSession;
  throw new Error(path);
}};
require({json.dumps(str(session_controller))});
let state = {{
  active_session_id: 'AS-empty', active_run_id: null, sessions: {{}}, messages: {{}},
  ordered_message_ids: [], events: {{}}, ordered_event_ids: [], environment_snapshot: null
}};
const store = {{
  getState: () => state,
  set: patch => {{ state = {{ ...state, ...patch }}; }}
}};
(async () => {{
  const restored = await new AgentSessionController(store).restoreForeground();
  process.stdout.write(JSON.stringify({{ restored, activeSession: state.active_session_id, activeRun: state.active_run_id }}));
}})();
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True, timeout=5
    )
    assert json.loads(completed.stdout) == {
        "restored": "AS-recovery",
        "activeSession": "AS-recovery",
        "activeRun": "AR-recovery",
    }


def test_startup_does_not_revive_an_old_recovery_after_a_newer_identical_success():
    session_controller = AGENT / "agent-session-controller.js"
    script = f"""
global.window = global;
const completedSession = {{
  session_id: 'AS-completed', active_run_id: 'AR-completed', messages: []
}};
global.api = async path => {{
  if (path === '/api/agents/sessions?limit=100') return {{ items: [
    {{ session_id: 'AS-completed', active_run_id: 'AR-completed' }},
    {{ session_id: 'AS-old-recovery', active_run_id: 'AR-old-recovery' }}
  ] }};
  if (path === '/api/agents/runs/AR-completed') return {{
    run_id: 'AR-completed', status: 'completed',
    objective: '[MODEL_TASK_KIND:market_decision] 請分析 3105.TWO 並完成紙上模擬買進交易'
  }};
  if (path === '/api/agents/runs/AR-old-recovery') return {{
    run_id: 'AR-old-recovery', status: 'waiting_user_input',
    objective: '請分析 3105.TWO 並完成紙上模擬買進交易'
  }};
  if (path === '/api/agents/sessions/AS-completed') return completedSession;
  throw new Error(path);
}};
require({json.dumps(str(session_controller))});
let state = {{
  active_session_id: 'AS-old-recovery', active_run_id: 'AR-old-recovery', sessions: {{}}, messages: {{}},
  ordered_message_ids: [], events: {{}}, ordered_event_ids: [], environment_snapshot: null
}};
const store = {{
  getState: () => state,
  set: patch => {{ state = {{ ...state, ...patch }}; }}
}};
(async () => {{
  const restored = await new AgentSessionController(store).restoreForeground();
  process.stdout.write(JSON.stringify({{ restored, activeSession: state.active_session_id, activeRun: state.active_run_id }}));
}})();
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True, timeout=5
    )
    assert json.loads(completed.stdout) == {
        "restored": "AS-completed",
        "activeSession": "AS-completed",
        "activeRun": "AR-completed",
    }


def test_startup_does_not_show_an_old_budget_checkpoint_over_a_newer_success():
    session_controller = AGENT / "agent-session-controller.js"
    script = f"""
global.window = global;
const completedSession = {{
  session_id: 'AS-completed', active_run_id: 'AR-completed', messages: []
}};
global.api = async path => {{
  if (path === '/api/agents/sessions?limit=100') return {{ items: [
    {{ session_id: 'AS-completed', active_run_id: 'AR-completed', updated_at: '2026-08-27T13:57:56.784Z' }},
    {{ session_id: 'AS-old-budget', active_run_id: 'AR-old-budget', updated_at: '2026-08-27T08:35:03.627Z' }}
  ] }};
  if (path === '/api/agents/runs/AR-completed') return {{
    run_id: 'AR-completed', status: 'completed', updated_at: '2026-08-27T13:57:56.778Z',
    objective: '請分析 2330.TW 目前的市場狀況'
  }};
  if (path === '/api/agents/runs/AR-old-budget') return {{
    run_id: 'AR-old-budget', status: 'max_steps_reached', updated_at: '2026-08-27T08:35:03.624Z',
    objective: '請分析 2330.TW 目前的市場狀況'
  }};
  if (path === '/api/agents/sessions/AS-completed') return completedSession;
  throw new Error(path);
}};
require({json.dumps(str(session_controller))});
let state = {{
  active_session_id: 'AS-old-budget', active_run_id: 'AR-old-budget', sessions: {{}}, messages: {{}},
  ordered_message_ids: [], events: {{}}, ordered_event_ids: [], environment_snapshot: null
}};
const store = {{
  getState: () => state,
  set: patch => {{ state = {{ ...state, ...patch }}; }}
}};
(async () => {{
  const restored = await new AgentSessionController(store).restoreForeground();
  process.stdout.write(JSON.stringify({{ restored, activeSession: state.active_session_id, activeRun: state.active_run_id }}));
}})();
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True, timeout=5
    )
    assert json.loads(completed.stdout) == {
        "restored": "AS-completed",
        "activeSession": "AS-completed",
        "activeRun": "AR-completed",
    }


def test_startup_does_not_show_an_old_waiting_decision_over_a_newer_success():
    session_controller = AGENT / "agent-session-controller.js"
    script = f"""
global.window = global;
const completedSession = {{
  session_id: 'AS-completed', active_run_id: 'AR-completed', messages: []
}};
global.api = async path => {{
  if (path === '/api/agents/sessions?limit=100') return {{ items: [
    {{ session_id: 'AS-completed', active_run_id: 'AR-completed', updated_at: '2026-08-27T13:57:56.784Z' }},
    {{ session_id: 'AS-old-question', active_run_id: 'AR-old-question', updated_at: '2026-08-27T08:19:23.344Z' }}
  ] }};
  if (path === '/api/agents/runs/AR-completed') return {{
    run_id: 'AR-completed', status: 'completed', updated_at: '2026-08-27T13:57:56.778Z',
    objective: '請分析 2887.TW 並完成紙上模擬交易'
  }};
  if (path === '/api/agents/runs/AR-old-question') return {{
    run_id: 'AR-old-question', status: 'waiting_decision', updated_at: '2026-08-27T08:19:23.344Z',
    objective: '請分析 2887.TW 並完成紙上模擬交易'
  }};
  if (path === '/api/agents/sessions/AS-completed') return completedSession;
  throw new Error(path);
}};
require({json.dumps(str(session_controller))});
let state = {{
  active_session_id: 'AS-old-question', active_run_id: 'AR-old-question', sessions: {{}}, messages: {{}},
  ordered_message_ids: [], events: {{}}, ordered_event_ids: [], environment_snapshot: null
}};
const store = {{
  getState: () => state,
  set: patch => {{ state = {{ ...state, ...patch }}; }}
}};
(async () => {{
  const restored = await new AgentSessionController(store).restoreForeground();
  process.stdout.write(JSON.stringify({{ restored, activeSession: state.active_session_id, activeRun: state.active_run_id }}));
}})();
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True, timeout=5
    )
    assert json.loads(completed.stdout) == {
        "restored": "AS-completed",
        "activeSession": "AS-completed",
        "activeRun": "AR-completed",
    }


def test_startup_restores_the_latest_completed_run_when_nothing_is_recoverable():
    session_controller = AGENT / "agent-session-controller.js"
    script = f"""
global.window = global;
const completedSession = {{
  session_id: 'AS-latest-completed', active_run_id: 'AR-latest-completed', messages: []
}};
global.api = async path => {{
  if (path === '/api/agents/sessions?limit=100') return {{ items: [
    {{ session_id: 'AS-latest-completed', active_run_id: 'AR-latest-completed' }},
    {{ session_id: 'AS-older-completed', active_run_id: 'AR-older-completed' }}
  ] }};
  if (path === '/api/agents/runs/AR-latest-completed') return {{
    run_id: 'AR-latest-completed', status: 'completed', objective: '完成紙上模擬交易'
  }};
  if (path === '/api/agents/runs/AR-older-completed') return {{
    run_id: 'AR-older-completed', status: 'completed', objective: '舊的紙上模擬交易'
  }};
  if (path === '/api/agents/sessions/AS-latest-completed') return completedSession;
  throw new Error(path);
}};
require({json.dumps(str(session_controller))});
let state = {{
  active_session_id: 'AS-empty', active_run_id: null, sessions: {{}}, messages: {{}},
  ordered_message_ids: [], events: {{}}, ordered_event_ids: [], environment_snapshot: null
}};
const store = {{
  getState: () => state,
  set: patch => {{ state = {{ ...state, ...patch }}; }}
}};
(async () => {{
  const restored = await new AgentSessionController(store).restoreForeground();
  process.stdout.write(JSON.stringify({{ restored, activeSession: state.active_session_id, activeRun: state.active_run_id }}));
}})();
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True, timeout=5
    )
    assert json.loads(completed.stdout) == {
        "restored": "AS-latest-completed",
        "activeSession": "AS-latest-completed",
        "activeRun": "AR-latest-completed",
    }


def test_recovery_escalation_snapshot_pins_its_question_and_task_forest():
    reducer = AGENT / "agent-event-reducer.js"
    store = AGENT / "agent-dock-store.js"
    script = f"""
global.window = global;
global.localStorage = {{ getItem: () => null, setItem: () => {{}} }};
global.requestAnimationFrame = callback => callback();
require({json.dumps(str(reducer))});
require({json.dumps(str(store))});
AgentDockStore.applySnapshot({{
  run: {{ run_id: 'AR-recovery', session_id: 'AS-recovery', terminal: false, status: 'waiting_user_input' }},
  interactions: [{{
    interaction_id: 'INT-recovery', session_id: 'AS-recovery', run_id: 'AR-recovery',
    status: 'waiting_user_input', interaction_purpose: 'recovery_escalation',
    prompt: '請選擇下一步'
  }}],
  forest: {{ forest_id: 'TF-recovery', branches: [] }}, steps: [], events: []
}});
const state = AgentDockStore.getState();
process.stdout.write(JSON.stringify({{
  activeRun: state.active_run_id, dockOpen: state.dock_open, activeTab: state.active_tab,
  prompt: state.interactions['INT-recovery'].prompt
}}));
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True, timeout=5
    )
    assert json.loads(completed.stdout) == {
        "activeRun": "AR-recovery",
        "dockOpen": True,
        "activeTab": "tasks",
        "prompt": "請選擇下一步",
    }


def test_snapshot_keeps_historical_checkpoint_owner_out_of_current_decision_cards():
    store = AGENT / "agent-dock-store.js"
    card = AGENT / "agent-decision-card.js"
    script = f"""
global.window = global;
global.localStorage = {{ getItem: () => null, setItem: () => {{}} }};
global.requestAnimationFrame = callback => callback();
global.AgentEventReducer = {{ scopedKey: (runId, id) => `${{runId}}:${{id}}` }};
global.AgentPlanView = {{ activeRunId: () => 'AR-current' }};
require({json.dumps(str(store))});
require({json.dumps(str(card))});
AgentDockStore.applySnapshot({{
  run: {{ run_id: 'AR-current', session_id: 'AS-current', terminal: true, status: 'completed' }},
  interactions: [{{
    interaction_id: 'INT-old', run_id: 'AR-old', session_id: 'AS-current',
    status: 'waiting_user_input', interaction_purpose: 'recovery_escalation', prompt: '舊問題'
  }}]
}});
const state = AgentDockStore.getState();
process.stdout.write(JSON.stringify({{
  interactionRun: state.interactions['INT-old'].run_id,
  visibleCount: AgentDecisionCard.activeInteractions(state).length
}}));
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True, timeout=5
    )
    assert json.loads(completed.stdout) == {
        "interactionRun": "AR-old",
        "visibleCount": 0,
    }


def test_recovery_decision_question_is_visible_above_all_agent_tabs():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    card = (AGENT / "agent-decision-card.js").read_text(encoding="utf-8")
    reducer = (AGENT / "agent-event-reducer.js").read_text(encoding="utf-8")

    assert html.index('id="agentDecisionCards"') < html.index('id="agentDockTabs"')
    assert "interaction.question || interaction.prompt" in card
    assert "interaction_purpose === 'recovery_escalation'" in reducer
    assert "next.active_tab = 'tasks'" in reducer


def test_agent_header_uses_the_selected_sessions_run_and_provider_identity():
    source = (AGENT / "agent-dock-controller.js").read_text(encoding="utf-8")

    assert "run.session_id === state.active_session_id" in source
    assert "const provider = run.driver || run.driver_id || run.provider || runtimeIdentity.provider" in source
    assert "runtimeIdentities[provider] || runtimeIdentity" in source
    assert "item.capabilities?.profile?.model" in source


def test_new_run_receipt_synchronizes_the_dock_to_its_authoritative_session():
    source = (AGENT / "agent-dock-controller.js").read_text(encoding="utf-8")

    assert "const runSessionId = String(run.session_id || sessionId || '');" in source
    assert "await sessions.select(runSessionId);" in source
    assert "active_session_id: runSessionId || store.getState().active_session_id" in source


def test_new_single_symbol_run_synchronizes_the_workspace_and_chart_context():
    source = (AGENT / "agent-dock-controller.js").read_text(encoding="utf-8")

    assert "async function synchronizeSingleTaskSymbol(symbols, { onlyIfUnselected = false } = {})" in source
    assert "reason: 'agent-run:task-symbol'" in source
    assert "selection: { symbol, explicit_intent_symbols: [symbol] }" in source
    assert "await window.loadSummary(symbol, { navigate: false });" in source
    assert "await synchronizeSingleTaskSymbol(run.symbols?.length ? run.symbols : symbols);" in source
    assert "await synchronizeSingleTaskSymbol(activeRun(store.getState()).symbols, { onlyIfUnselected: true });" in source


def test_completed_foreground_run_hydrates_before_restoring_its_workspace_symbol():
    dock = (AGENT / "agent-dock-controller.js").read_text(encoding="utf-8")
    sessions = (AGENT / "agent-session-controller.js").read_text(encoding="utf-8")

    assert "const selectResolvedRun = async item =>" in sessions
    assert "runs: { ...this.store.getState().runs, [runId]: item.run }" in sessions
    assert "const foregroundRun = activeRun(store.getState());" in dock
    assert "if (foregroundRun.run_id) await hydrateRun(foregroundRun.run_id);" in dock
    assert dock.index("await hydrateRun(foregroundRun.run_id);") < dock.index(
        "await synchronizeSingleTaskSymbol(activeRun(store.getState()).symbols"
    )


def test_chart_selector_keeps_the_agent_selected_stock_visible_before_catalog_loads():
    chart = (STATIC / "js" / "features" / "market-chart.js").read_text(encoding="utf-8")

    assert "option.dataset.contextSelection = 'true';" in chart
    assert "option.textContent = `${nextSymbol} · ${entityName || nextSymbol}`;" in chart
    assert "symbolSelect.value = nextSymbol;" in chart


def test_bootstrap_loads_the_existing_agent_runtime_settings_function():
    bootstrap = (STATIC / "js" / "bootstrap.js").read_text(encoding="utf-8")
    runtime = (STATIC / "js" / "features" / "agent-runtime.js").read_text(
        encoding="utf-8"
    )

    assert "loadAgentRuntimeSettings()" in bootstrap
    assert "async function loadAgentRuntimeSettings()" in runtime
    assert "loadAgentRuntime()" not in bootstrap


def test_agent_dock_uses_the_active_provider_and_neutralizes_market_questions():
    source = (AGENT / "agent-dock-controller.js").read_text(encoding="utf-8")

    assert "async function classifyPrompt" in source
    assert "context_scope: scope" in source
    assert "scope === 'instrument' && intent.use_selected_symbol" in source
    assert "state.agentSettings?.default_driver" in source
    assert "stock-ai-active-agent-changed" in source
    assert "run.driver_id" in source
    assert "/api/agents/classify-intent" in source


def test_operational_feed_cards_are_compact_and_expand_on_demand():
    conversation = (AGENT / "agent-conversation-view.js").read_text(encoding="utf-8")
    tool = (AGENT / "agent-tool-view.js").read_text(encoding="utf-8")
    skill = (AGENT / "agent-skill-view.js").read_text(encoding="utf-8")
    css = (STATIC / "css" / "features" / "agent-dock.css").read_text(
        encoding="utf-8"
    )

    assert "agent-operation-card" in conversation
    assert "agent-operation-preview" in conversation
    assert conversation.count("顯示完整資訊") == 1
    assert tool.count("createElement('details')") == 1
    assert tool.count("顯示完整資訊") == 1
    assert "驗證結果" in tool
    assert skill.count("createElement('details')") == 1
    assert skill.count("顯示完整資訊") == 1
    assert ".agent-operation-card{gap:4px;padding:6px 8px" in css
    assert ".agent-operation-disclosure>summary" in css
    assert ".agent-message-assistant" in css


def test_assistant_market_answers_render_numbered_sections_and_safe_tables():
    conversation = (AGENT / "agent-conversation-view.js").read_text(encoding="utf-8")
    css = (STATIC / "css" / "features" / "agent-dock.css").read_text(
        encoding="utf-8"
    )
    orchestrator = (
        ROOT / "src" / "open_stock_ai" / "agent_runtime" / "orchestrator.py"
    ).read_text(encoding="utf-8")

    assert "assistantAnswer(content)" in conversation
    assert "document.createElement('table')" in conversation
    assert "document.createElement('thead')" in conversation
    assert "document.createElement('ol')" not in conversation
    assert "innerHTML" not in conversation
    assert ".agent-answer-table-wrap" in css
    assert ".agent-answer-content table" in css
    assert "natural numbered sections" in orchestrator
    assert "Choose each section title, section count, order and table columns" in orchestrator
    assert "do not force a fixed answer template" in orchestrator
    assert "compact Markdown table" in orchestrator
    assert "presentation guidance standardizes readability" in orchestrator


def test_agent_dock_has_readable_light_theme_surfaces_and_controls():
    css = (STATIC / "css" / "features" / "agent-dock.css").read_text(
        encoding="utf-8"
    )

    assert 'html:is([data-ui-theme="pearl"],[data-ui-theme="daylight"]) .agent-dock{' in css
    assert "background:linear-gradient(155deg,rgba(252,254,255,.96)" in css
    assert ".agent-plan-step.is-completed{opacity:1}" in css
    assert ".agent-dock :is(button,select,textarea)" in css
    assert ".agent-operation-disclosure pre" in css
    assert ".agent-answer-table-wrap{background:rgba(255,255,255,.72)}" in css
    assert ".agent-runtime-card.is-success{" in css
    assert ".agent-tool-card.is-running{" in css
    assert ".agent-skill-card.kind-skill{" in css
    assert ".agent-skill-card.kind-package{" in css


def test_plan_status_markers_stay_aligned_and_operation_kinds_keep_their_colors():
    css = (STATIC / "css" / "features" / "agent-dock.css").read_text(
        encoding="utf-8"
    )
    skill = (AGENT / "agent-skill-view.js").read_text(encoding="utf-8")

    plan_rule = next(
        line for line in css.splitlines() if line.startswith(".agent-plan-step{")
    )
    assert "--agent-step-depth" not in plan_rule
    assert ".agent-plan-step>span:first-child{place-items:start center" in css
    assert "kind-${kind}" in skill


def test_plan_panel_collapses_to_progress_and_keeps_steps_in_chronological_order():
    plan = (AGENT / "agent-plan-view.js").read_text(encoding="utf-8")
    css = (STATIC / "css" / "features" / "agent-dock.css").read_text(
        encoding="utf-8"
    )

    assert "agent-plan-toggle" in plan
    assert "container.dataset.collapsed" in plan
    assert "aria-expanded" in plan
    assert "collapsedSummary" in plan
    assert "執行中：" in plan
    assert "已全部完成" in plan
    assert "steps.forEach(step => list.append(createRow(step)))" in plan
    assert "byParent" not in plan
    assert ".agent-pinned-plan.is-collapsed" in css
    assert ".agent-pinned-plan.is-collapsed .agent-plan-head>strong" in css


def test_final_answer_is_chronological_deduplicated_and_auto_scrolled_into_view():
    conversation = (AGENT / "agent-conversation-view.js").read_text(encoding="utf-8")

    assert "assistant-run:${message.run_id}" in conversation
    assert "entries.sort((left, right)" in conversation
    assert "leftTime - rightTime" in conversation
    assert "receivedFinalAnswer" in conversation
    assert "container.scrollTop = container.scrollHeight" in conversation


def test_plan_progress_reports_result_gap_and_next_step_without_exposing_reasoning():
    conversation = (AGENT / "agent-conversation-view.js").read_text(encoding="utf-8")
    plan = (AGENT / "agent-plan-view.js").read_text(encoding="utf-8")
    orchestrator = (
        ROOT / "src" / "open_stock_ai" / "agent_runtime" / "orchestrator.py"
    ).read_text(encoding="utf-8")

    assert "plan.step.progress" in conversation
    assert "plan.step.reported" in conversation
    assert "remaining_gaps" in conversation
    assert "next_step" in conversation
    assert "結果：" in plan
    assert "仍缺：" in plan
    assert "下一步：" in plan
    assert '"plan.step.reported"' in orchestrator
    assert "public progress report" in orchestrator
    assert "private chain-of-thought" in orchestrator
    assert "remaining_gaps must list only blockers" in orchestrator
    assert "正在驗收" in plan
    assert "-webkit-line-clamp:2" in (
        STATIC / "css" / "features" / "agent-dock.css"
    ).read_text(encoding="utf-8")


def test_plan_view_never_leaks_steps_from_another_session():
    plan_view = AGENT / "agent-plan-view.js"
    script = f"""
global.window = global;
global.AgentDockFormatters = {{
  status: value => ({{ key: value, label: value, icon: value }}),
  text: value => typeof value === 'string' ? value : JSON.stringify(value || '')
}};
require({json.dumps(str(plan_view))});
const state = {{
  active_session_id: 'AS-new',
  active_run_id: null,
  runs: {{
    'AR-old': {{
      run_id: 'AR-old', session_id: 'AS-old',
      updated_at: '2026-07-28T10:00:00Z', plan_id: 'AP-old'
    }}
  }},
  steps: {{
    'old-step': {{ node_id: 'old-step', run_id: 'AR-old', status: 'completed' }}
  }},
  plans: {{ 'AP-old': {{ plan_id: 'AP-old', revision_number: 4 }} }}
}};
process.stdout.write(JSON.stringify({{
  runId: AgentPlanView.activeRunId(state),
  steps: AgentPlanView.orderedSteps(state)
}}));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert json.loads(completed.stdout) == {"runId": None, "steps": []}


def test_pinned_plan_merges_duplicate_lifecycle_and_plan_rows():
    plan_view = AGENT / "agent-plan-view.js"
    script = f"""
global.window = global;
require({json.dumps(str(plan_view))});
const state = {{
  active_session_id: 'AS-1', active_run_id: 'AR-1',
  runs: {{'AR-1': {{run_id:'AR-1', session_id:'AS-1'}}}},
  steps: {{
    'AR-1:plan': {{run_id:'AR-1', node_id:'plan', title:'驗證行情', status:'completed', order_index:0, capability:'market.analyze_symbol'}},
    'AR-1:lifecycle': {{run_id:'AR-1', node_id:'lifecycle', title:'驗證行情', status:'completed', order_index:1, result_summary:'已取得 Host 收據'}}
  }}
}};
const steps = AgentPlanView.orderedSteps(state);
process.stdout.write(JSON.stringify({{count:steps.length, title:steps[0].title, capability:steps[0].capability, result:steps[0].result_summary}}));
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True, timeout=5
    )
    assert json.loads(completed.stdout) == {
        "count": 1,
        "title": "驗證行情",
        "capability": "market.analyze_symbol",
        "result": "已取得 Host 收據",
    }


def test_control_bar_never_leaks_failed_run_from_another_session():
    control_bar = AGENT / "agent-control-bar.js"
    script = f"""
global.window = global;
require({json.dumps(str(control_bar))});
const state = {{
  active_session_id: 'AS-new',
  active_run_id: null,
  runs: {{
    'AR-old': {{
      run_id: 'AR-old', session_id: 'AS-old',
      status: 'failed', updated_at: '2026-07-28T10:00:00Z'
    }}
  }}
}};
process.stdout.write(JSON.stringify({{
  runId: AgentControlBar.activeRunId(state)
}}));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert json.loads(completed.stdout) == {"runId": None}


def test_completed_execution_run_requires_a_fresh_advisory_draft_not_a_replay():
    control_bar = AGENT / "agent-control-bar.js"
    script = f"""
global.window = global;
require({json.dumps(str(control_bar))});
const paperRun = {{
  run_id: 'AR-paper', autonomy: 'paper_execute', status: 'completed',
}};
const advisoryRun = {{
  run_id: 'AR-analysis', autonomy: 'advisory', status: 'completed',
}};
const paper = AgentControlBar.completedControls({{
  tool_calls: {{'AR-paper:submit': {{run_id:'AR-paper',tool_name:'paper.submit_order',risk_class:'financial_paper'}}}}
}}, paperRun);
const advisory = AgentControlBar.completedControls({{tool_calls: {{}}}}, advisoryRun);
process.stdout.write(JSON.stringify({{paper, advisory}}));
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True, timeout=5,
    )

    assert json.loads(completed.stdout) == {
        "paper": [["draft_new_goal", "建立新目標草稿"]],
        "advisory": [["rerun", "再次執行"]],
    }


def test_resource_boundary_does_not_offer_a_rebuilt_goal_draft():
    control_bar = AGENT / "agent-control-bar.js"
    script = f"""
global.window = global;
require({json.dumps(str(control_bar))});
const run = {{
  run_id: 'AR-boundary', status: 'max_steps_reached', autonomy: 'advisory',
  result: {{next_goal_draft: {{objective: '從 checkpoint 接續，不重跑已完成工作。'}}}}
}};
process.stdout.write(JSON.stringify(AgentControlBar.resourceBoundaryControls(run)));
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True, timeout=5,
    )
    assert json.loads(completed.stdout) is None


def test_event_reducer_deduplicates_tool_calls_and_replays_one_thousand_events():
    reducer = AGENT / "agent-event-reducer.js"
    script = f"""
global.window = global;
require({json.dumps(str(reducer))});
let state = {{
  events: {{}}, ordered_event_ids: [], last_sequence_by_run: {{}}, runs: {{}},
  plans: {{}}, steps: {{}}, tool_calls: {{}}, approvals: {{}}, artifacts: {{}},
  messages: {{}}, skills: {{}}, ordered_message_ids: [], active_run_id: null
}};
for (let i = 1; i <= 1000; i += 1) {{
  const call = `call-${{Math.ceil(i / 2)}}`;
  const type = i % 2 ? 'tool.started' : 'tool.completed';
  state = AgentEventReducer.reduce(state, {{
    event_id: `ARE-${{i}}`, sequence: i, run_id: 'AR-load', session_id: 'AS-load',
    type, tool_call_id: call, payload: {{ call_id: call, tool: 'test.tool' }}
  }});
}}
state = AgentEventReducer.reduce(state, {{
  event_id: 'ARE-1000', sequence: 1000, run_id: 'AR-load',
  type: 'tool.completed', tool_call_id: 'call-500', payload: {{}}
}});
state = AgentEventReducer.reduce(state, {{
  event_id: 'ARE-1001', sequence: 1001, run_id: 'AR-load',
  type: 'run.started', payload: {{}}
}});
for (const [offset, type] of ['assistant.message.started', 'assistant.message.delta', 'assistant.message.completed'].entries()) {{
  state = AgentEventReducer.reduce(state, {{
    event_id: `ARE-${{1002 + offset}}`, sequence: 1002 + offset, run_id: 'AR-load',
    session_id: 'AS-load', type, payload: {{ content: `answer-${{offset}}` }}
  }});
}}
process.stdout.write(JSON.stringify({{
  eventCount: state.ordered_event_ids.length,
  toolCount: Object.keys(state.tool_calls).length,
  last: state.last_sequence_by_run['AR-load'],
  status: state.tool_calls['AR-load:call-500'].status,
  runStatus: state.runs['AR-load'].status,
  messageCount: state.ordered_message_ids.length,
  answer: state.messages['assistant:AR-load'].content
}}));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    result = json.loads(completed.stdout)
    assert result == {
        "eventCount": 1004,
        "toolCount": 500,
        "last": 1004,
        "status": "completed",
        "runStatus": "running",
        "messageCount": 1,
        "answer": "answer-2",
    }


def test_recovery_event_uses_plan_node_and_deduplicates_its_fishbone_link():
    reducer = AGENT / "agent-event-reducer.js"
    script = f"""
global.window = global;
require({json.dumps(str(reducer))});
let state = {{
  events: {{}}, ordered_event_ids: [], last_sequence_by_run: {{}}, runs: {{}},
  plans: {{}}, steps: {{}}, tool_calls: {{}}, approvals: {{}}, artifacts: {{}},
  messages: {{}}, skills: {{}}, ordered_message_ids: [], active_run_id: null
}};
const base = {{run_id: 'AR-recovery', session_id: 'AS-recovery'}};
state = AgentEventReducer.reduce(state, {{
  ...base, event_id: 'E-plan', sequence: 1, type: 'plan.updated',
  payload: {{plan: {{plan_id: 'AP-recovery', revision_number: 1, nodes: [
    {{node_id: 'tool-failed', node_type: 'tool', status: 'failed', title: '失敗來源'}}
  ]}}}}
}});
const recovery = {{failed_node_id: 'tool-failed', failed_call_id: 'call-failed', recovery_call_id: 'call-web', recovery_tool: 'web.research'}};
for (const sequence of [2, 3]) {{
  state = AgentEventReducer.reduce(state, {{
    ...base, event_id: `E-recovery-${{sequence}}`, sequence, type: 'recovery.linked',
    // The numeric turn is deliberately different from the durable node ID.
    step_id: String(sequence), node_id: 'tool-web',
    payload: {{node_id: 'tool-web', recovery_for: [recovery]}}
  }});
}}
process.stdout.write(JSON.stringify({{
  ghostTurnNode: Boolean(state.steps['AR-recovery:2']),
  recoveredBy: state.steps['AR-recovery:tool-failed'].metadata.recovered_by,
  recoveryNode: Boolean(state.steps['AR-recovery:tool-web'])
}}));
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True, timeout=5,
    )
    result = json.loads(completed.stdout)
    assert result["ghostTurnNode"] is False
    assert result["recoveryNode"] is True
    assert result["recoveredBy"] == [
        {
            "failed_node_id": "tool-failed",
            "failed_call_id": "call-failed",
            "recovery_call_id": "call-web",
            "recovery_tool": "web.research",
        }
    ]


def test_max_steps_event_is_not_reduced_to_completed_and_exposes_recovery_controls():
    reducer = AGENT / "agent-event-reducer.js"
    control = AGENT / "agent-control-bar.js"
    script = f"""
global.window = global;
require({json.dumps(str(reducer))});
let state = {{
  events: {{}}, ordered_event_ids: [], last_sequence_by_run: {{}}, runs: {{}},
  plans: {{}}, steps: {{}}, tool_calls: {{}}, approvals: {{}}, artifacts: {{}},
  messages: {{}}, skills: {{}}, ordered_message_ids: [], active_run_id: null
}};
state = AgentEventReducer.reduce(state, {{
  event_id: 'E-limit', sequence: 1, run_id: 'AR-limit', session_id: 'AS-limit',
  type: 'run.max_steps_reached',
  payload: {{ status: 'max_steps_reached', summary: 'not complete' }}
}});
process.stdout.write(JSON.stringify({{
  status: state.runs['AR-limit'].status,
  active: state.active_run_id
}}));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert json.loads(completed.stdout) == {
        "status": "max_steps_reached",
        "active": None,
    }
    controls = control.read_text(encoding="utf-8")
    assert "max_steps_reached" in controls
    assert "繼續執行" in controls
    assert "增加步驟上限" in controls
    assert "重新規劃" in controls
    assert "建立新 Run" in controls


def test_partial_completion_is_terminal_but_keeps_recovery_controls():
    reducer = AGENT / "agent-event-reducer.js"
    control = AGENT / "agent-control-bar.js"
    script = f"""
global.window = global;
require({json.dumps(str(reducer))});
let state = {{
  events: {{}}, ordered_event_ids: [], last_sequence_by_run: {{}}, runs: {{}},
  plans: {{}}, steps: {{}}, tool_calls: {{}}, approvals: {{}}, artifacts: {{}},
  messages: {{}}, skills: {{}}, ordered_message_ids: [], active_run_id: null
}};
state = AgentEventReducer.reduce(state, {{
  event_id: 'E-partial', sequence: 1, run_id: 'AR-partial', session_id: 'AS-partial',
  type: 'run.partially_completed',
  payload: {{ status: 'partially_completed', summary: 'failed branch preserved' }}
}});
process.stdout.write(JSON.stringify({{
  status: state.runs['AR-partial'].status,
  active: state.active_run_id
}}));
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True, timeout=5,
    )
    assert json.loads(completed.stdout) == {"status": "partially_completed", "active": None}
    controls = control.read_text(encoding="utf-8")
    assert "partially_completed" in controls
    assert "繼續修復" in controls


def test_completed_event_cannot_turn_a_failed_step_into_a_green_run():
    reducer = AGENT / "agent-event-reducer.js"
    script = f"""
global.window = global;
require({json.dumps(str(reducer))});
let state = {{events: {{}}, ordered_event_ids: [], last_sequence_by_run: {{}}, runs: {{}}, plans: {{}}, steps: {{}}, tool_calls: {{}}, approvals: {{}}, artifacts: {{}}, messages: {{}}, skills: {{}}, ordered_message_ids: [], active_run_id: null}};
const base = {{run_id:'AR-contradictory',session_id:'AS-contradictory'}};
state = AgentEventReducer.reduce(state, {{...base,event_id:'E-step',sequence:1,type:'step.failed',node_id:'market-search',payload:{{node_id:'market-search',status:'failed',error_summary:'source unavailable'}}}});
state = AgentEventReducer.reduce(state, {{...base,event_id:'E-complete',sequence:2,type:'run.completed',payload:{{status:'completed'}}}});
process.stdout.write(JSON.stringify({{status:state.runs['AR-contradictory'].status,pending:state.runs['AR-contradictory'].recovery_pending,active:state.active_run_id}}));
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True, timeout=5,
    )
    assert json.loads(completed.stdout) == {
        "status": "partially_completed",
        "pending": True,
        "active": "AR-contradictory",
    }


def test_reflection_request_is_reduced_to_an_actionable_user_decision_card():
    reducer = AGENT / "agent-event-reducer.js"
    script = f"""
global.window = global;
require({json.dumps(str(reducer))});
let state = {{events: {{}}, ordered_event_ids: [], last_sequence_by_run: {{}}, runs: {{}}, plans: {{}}, steps: {{}}, tool_calls: {{}}, approvals: {{}}, artifacts: {{}}, messages: {{}}, skills: {{}}, ordered_message_ids: [], active_run_id: null}};
state = AgentEventReducer.reduce(state, {{
  event_id: 'E-reflection', sequence: 1, run_id: 'AR-reflection', session_id: 'AS-reflection',
  type: 'interaction.requested', payload: {{interaction_id: 'INT-1', prompt: '要採取哪個方案？', tentative_judgment: '先採用低風險方案', preferred_option: 'safe', options: [{{option_id: 'safe', label: '低風險'}}]}}
}});
process.stdout.write(JSON.stringify(state.interactions['INT-1']));
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True, timeout=5,
    )
    result = json.loads(completed.stdout)
    assert result["status"] == "awaiting_user"
    assert result["title"] == "要採取哪個方案？"
    assert result["tentative_judgment"] == "先採用低風險方案"
    card = (AGENT / "agent-decision-card.js").read_text(encoding="utf-8")
    assert "初步判斷與未確定事項" in card


def test_decision_cards_show_only_the_latest_pending_checkpoint_for_the_active_run():
    card = AGENT / "agent-decision-card.js"
    script = f"""
global.window = global;
require({json.dumps(str(card))});
const visible = AgentDecisionCard.activeInteractions({{
  active_session_id: 'AS-current', active_run_id: 'AR-current',
  interactions: {{
    'INT-old-session': {{interaction_id:'INT-old-session',session_id:'AS-old',run_id:'AR-old',status:'awaiting_user',created_at:'2026-08-20T09:00:00Z'}},
    'INT-first': {{interaction_id:'INT-first',session_id:'AS-current',run_id:'AR-current',status:'awaiting_user',created_at:'2026-08-20T10:00:00Z'}},
    'INT-current': {{interaction_id:'INT-current',session_id:'AS-current',run_id:'AR-current',status:'waiting_user_input',created_at:'2026-08-20T10:02:00Z'}},
    'INT-answered': {{interaction_id:'INT-answered',session_id:'AS-current',run_id:'AR-current',status:'answered',created_at:'2026-08-20T10:03:00Z'}}
  }}
}}).map(item => item.interaction_id);
process.stdout.write(JSON.stringify(visible));
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True, timeout=5,
    )
    assert json.loads(completed.stdout) == ["INT-current"]


def test_decision_cards_do_not_reopen_an_old_checkpoint_after_a_new_run_completes():
    card = AGENT / "agent-decision-card.js"
    plan_view = AGENT / "agent-plan-view.js"
    script = f"""
global.window = global;
require({json.dumps(str(plan_view))});
require({json.dumps(str(card))});
const visible = AgentDecisionCard.activeInteractions({{
  active_session_id: 'AS-current', active_run_id: null,
  runs: {{
    'AR-old': {{run_id:'AR-old',session_id:'AS-current',status:'waiting_user_input',updated_at:'2026-08-20T10:00:00Z'}},
    'AR-complete': {{run_id:'AR-complete',session_id:'AS-current',status:'completed',updated_at:'2026-08-20T10:05:00Z'}}
  }},
  interactions: {{
    'INT-old': {{interaction_id:'INT-old',session_id:'AS-current',run_id:'AR-old',status:'awaiting_user',created_at:'2026-08-20T10:00:00Z'}}
  }}
}}).map(item => item.interaction_id);
process.stdout.write(JSON.stringify(visible));
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True, timeout=5,
    )
    assert json.loads(completed.stdout) == []


def test_task_tab_is_a_real_hierarchical_dependency_and_revision_view():
    source = (AGENT / "agent-task-tree-view.js").read_text(encoding="utf-8")

    assert "AgentPlanView.render" not in source
    for token in (
        "parent_node_id",
        "dependency_ids",
        "assigned_agent",
        "tool_call_ids",
        "平行分支",
        "阻塞原因",
        "完成標準",
        "Revision History",
        "revisionDiff",
        "agent-task-children",
    ):
        assert token in source


def test_automation_view_renders_compiler_artifact_steps_as_a_user_safe_workflow():
    source = (AGENT / "agent-automation-view.js").read_text(encoding="utf-8")
    assert "automation.artifact?.steps" in source
    assert "automation.artifact_preview?.steps" in source
    assert "Host 執行層" in source
    assert "n8n" in source.casefold()


def test_portable_launcher_isolates_each_desktop_project_webkit_container():
    launcher = (ROOT / "open-stock-ai.sh").read_text(encoding="utf-8")
    desktop_launcher = (ROOT / "開啟股市AI系統.command").read_text(encoding="utf-8")
    # The native App identity is stable for one checkout but path-specific, so
    # Computer Use can attach without macOS merging a different worktree's
    # WebKit session into this desktop project's window.
    assert 'BUNDLE_ID="com.choubee.stockai.liquidglass.instance${PROJECT_INSTANCE_ID}"' in launcher
    assert 'plutil -replace CFBundleIdentifier -string "$BUNDLE_ID"' in launcher
    assert 'source "$RUNTIME_ROOT_HELPER" "$PROJECT_ROOT"' in desktop_launcher
    assert 'NATIVE_APP_EXECUTABLE="${STOCK_AI_RUNTIME_ROOT}/apps/Stock AI Liquid Glass.app/Contents/MacOS/StockAILiquidGlass"' in desktop_launcher
    assert 'BUNDLE_ID="com.choubee.stockai.liquidglass.instance${STOCK_AI_RUNTIME_INSTANCE_ID}"' in desktop_launcher
    assert 'pkill -f "$NATIVE_APP_EXECUTABLE"' in desktop_launcher


def test_session_switch_clears_every_run_scoped_collection():
    source = (AGENT / "agent-session-controller.js").read_text(encoding="utf-8")

    for collection in (
        "runs",
        "plans",
        "plan_revisions",
        "steps",
        "tool_calls",
        "approvals",
        "artifacts",
        "artifact_versions",
        "artifact_selections",
        "forests",
        "branches",
        "interactions",
        "automations",
        "evidence",
        "skills",
        "events",
    ):
        assert source.count(f"{collection}: {{}}") >= 2


def test_runtime_reducer_projects_forest_interaction_artifact_automation_and_evidence():
    reducer = AGENT / "agent-event-reducer.js"
    script = f"""
global.window = global;
require({json.dumps(str(reducer))});
let state = {{
  active_session_id: 'AS-new', active_run_id: null,
  events: {{}}, ordered_event_ids: [], last_sequence_by_run: {{}}, runs: {{}},
  sessions: {{}}, plans: {{}}, steps: {{}}, tool_calls: {{}}, approvals: {{}},
  artifacts: {{}}, artifact_versions: {{}}, artifact_selections: {{}},
  messages: {{}}, skills: {{}}, forests: {{}}, branches: {{}}, interactions: {{}},
  automations: {{}}, evidence: {{}}, ordered_message_ids: []
}};
const events = [
  {{event_id:'E1',sequence:1,type:'forest.created',run_id:'AR-1',session_id:'AS-new',payload:{{forest:{{forest_id:'F-1',title:'持股分析'}}}}}},
  {{event_id:'E2',sequence:2,type:'branch.started',run_id:'AR-1',session_id:'AS-new',branch_id:'BR-US',payload:{{branch:{{branch_id:'BR-US',title:'美國來源'}}}}}},
  {{event_id:'E3',sequence:3,type:'interaction.awaiting_user',run_id:'AR-1',session_id:'AS-new',payload:{{interaction:{{interaction_id:'I-1',question:'怎麼停利？',options:['分批']}}}}}},
  {{event_id:'E4',sequence:4,type:'artifact.updated',run_id:'AR-1',session_id:'AS-new',artifact_id:'ART-1',payload:{{artifact:{{artifact_id:'ART-1',version:7,title:'策略圖'}}}}}},
  {{event_id:'E5',sequence:5,type:'artifact.selected',run_id:'AR-1',session_id:'AS-new',artifact_id:'ART-1',payload:{{selection:{{selection_id:'SEL-1',artifact_version:7,target_type:'step',path:'策略圖 ＞ 法人'}}}}}},
  {{event_id:'E6',sequence:6,type:'research.evidence_added',run_id:'AR-1',session_id:'AS-new',branch_id:'BR-US',payload:{{evidence:{{evidence_id:'EV-1',claim:'需求強'}}}}}},
  {{event_id:'E7',sequence:7,type:'automation.active',run_id:'AR-1',session_id:'AS-new',automation_id:'AUTO-1',payload:{{automation:{{automation_id:'AUTO-1',title:'三日監控'}}}}}}
];
events.forEach(event => {{ state = AgentEventReducer.reduce(state, event); }});
process.stdout.write(JSON.stringify({{
  forest: state.forests['F-1'].title,
  branch: state.branches['BR-US'].status,
  interaction: state.interactions['I-1'].status,
  artifactVersion: state.artifact_versions['ART-1:v7'].version,
  selection: state.active_selection.path,
  evidence: state.evidence['EV-1'].branch_id,
  automation: state.automations['AUTO-1'].status
}}));
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True, timeout=5
    )
    assert json.loads(completed.stdout) == {
        "forest": "持股分析",
        "branch": "running",
        "interaction": "awaiting_user",
        "artifactVersion": 7,
        "selection": "策略圖 ＞ 法人",
        "evidence": "BR-US",
        "automation": "active",
    }


def test_snapshot_expands_nested_forest_branches_and_accepts_object_collections():
    store = AGENT / "agent-dock-store.js"
    script = f"""
global.window = global;
global.localStorage = {{getItem: () => null, setItem: () => undefined}};
global.requestAnimationFrame = callback => callback();
global.AgentEventReducer = {{scopedKey: (runId, id) => `${{runId}}:${{id}}`}};
require({json.dumps(str(store))});
AgentDockStore.applySnapshot({{
  run: {{run_id:'AR-1',session_id:'AS-1',status:'running',terminal:false}},
  last_sequence: 4,
  forest: {{
    forest_id:'TF-1',
    branches:[{{
      branch_id:'BR-ROOT',objective:'分析台股',status:'running',
      steps:[{{step_id:'BST-1',position:0,title:'蒐集證據',status:'ready'}}]
    }}]
  }},
  artifact_versions: {{latest:{{artifact_id:'ART-1',version:3}}}},
  interactions: {{}}, automations: {{}}, evidence: {{}}, approvals: {{}},
  artifacts: {{}}, artifact_selections: {{}}, tool_calls: {{}}, steps: {{}},
  plan_revisions: {{}}
}});
const state = AgentDockStore.getState();
process.stdout.write(JSON.stringify({{
  forest: state.forests['TF-1'].run_id,
  branch: state.branches['BR-ROOT'].objective,
  stepParent: state.steps['AR-1:BST-1'].parent_node_id,
  artifactVersion: state.artifact_versions['ART-1:v3'].version
}}));
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True, timeout=5
    )
    assert json.loads(completed.stdout) == {
        "forest": "AR-1",
        "branch": "分析台股",
        "stepParent": "BR-ROOT",
        "artifactVersion": 3,
    }


def test_snapshot_store_flattens_durable_artifact_history_without_losing_versions():
    store = AGENT / "agent-dock-store.js"
    script = f"""
global.window = global;
global.localStorage = {{getItem: () => null, setItem: () => undefined}};
global.requestAnimationFrame = callback => callback();
global.AgentEventReducer = {{scopedKey: (runId, id) => `${{runId}}:${{id}}`}};
require({json.dumps(str(store))});
AgentDockStore.applySnapshot({{
  run: {{run_id:'AR-real',session_id:'AS-real',status:'completed',terminal:true}},
  artifact_versions: {{
    'ART-risk': [
      {{artifact_id:'ART-risk',version:1,content:{{risk_score:75}}}},
      {{artifact_id:'ART-risk',version:2,content:{{risk_score:'dynamic'}}}}
    ]
  }},
  evidence: [{{evidence_id:'EV-1',artifact_id:'ART-risk',artifact_version:2,claim:'波動升高'}}]
}});
const state = AgentDockStore.getState();
process.stdout.write(JSON.stringify({{
  versions: Object.values(state.artifact_versions).map(item => item.version).sort(),
  versionSession: state.artifact_versions['ART-risk:v2'].session_id,
  evidenceVersion: state.evidence['EV-1'].artifact_version
}}));
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True, timeout=5
    )

    assert json.loads(completed.stdout) == {
        "versions": [1, 2],
        "versionSession": "AS-real",
        "evidenceVersion": 2,
    }


def test_snapshot_removes_archived_automation_from_the_same_session_cache():
    store = AGENT / "agent-dock-store.js"
    script = f"""
global.window = global;
global.localStorage = {{getItem: () => null, setItem: () => undefined}};
global.requestAnimationFrame = callback => callback();
global.AgentEventReducer = {{scopedKey: (runId, id) => `${{runId}}:${{id}}`}};
require({json.dumps(str(store))});
AgentDockStore.applySnapshot({{
  run: {{run_id:'AR-automation',session_id:'AS-automation',status:'completed',terminal:true}},
  automations: [{{automation_id:'AUT-stale',session_id:'AS-automation',state:'paused',status:'paused'}}]
}});
AgentDockStore.applySnapshot({{
  run: {{run_id:'AR-automation',session_id:'AS-automation',status:'completed',terminal:true}},
  automations: []
}});
process.stdout.write(JSON.stringify(Object.keys(AgentDockStore.getState().automations)));
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True, timeout=5
    )
    assert json.loads(completed.stdout) == []


def test_snapshot_rejects_automation_owned_by_another_session():
    store = AGENT / "agent-dock-store.js"
    script = f"""
global.window = global;
global.localStorage = {{getItem: () => null, setItem: () => undefined}};
global.requestAnimationFrame = callback => callback();
global.AgentEventReducer = {{scopedKey: (runId, id) => `${{runId}}:${{id}}`}};
require({json.dumps(str(store))});
AgentDockStore.applySnapshot({{
  run: {{run_id:'AR-current',session_id:'AS-current',status:'completed',terminal:true}},
  automations: [{{automation_id:'AUT-other',session_id:'AS-other',state:'active',status:'active'}}]
}});
process.stdout.write(JSON.stringify(Object.keys(AgentDockStore.getState().automations)));
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True, timeout=5
    )
    assert json.loads(completed.stdout) == []


def test_current_run_composer_posts_a_session_message_with_precise_selection():
    controller = (AGENT / "agent-dock-controller.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    current_run_block = controller.split("if (running && mode === 'current_run')", 1)[1].split(
        "const sessionId = await sessions.ensure()", 1
    )[0]

    assert 'id="agentComposerMode" type="hidden" value="current_run"' in html
    assert 'aria-label="執行中補充指示方式"' not in html
    assert "const mode = running ? 'current_run' : 'new_run';" in controller
    assert "sessions.sendMessage(" in current_run_block
    assert "AgentComposerContext?.payload?.(state)" in current_run_block
    assert "/replan" not in current_run_block
    assert "POST /sessions/:session_id/messages" not in controller
    assert "/api/agents/sessions/${encodeURIComponent(sessionId)}/messages" in (
        AGENT / "agent-session-controller.js"
    ).read_text(encoding="utf-8")


def test_new_run_composer_leaves_automation_intent_to_the_agent_model():
    controller = (AGENT / "agent-dock-controller.js").read_text(encoding="utf-8")
    session_controller = (AGENT / "agent-session-controller.js").read_text(encoding="utf-8")

    # The model/Host now owns automation intent extraction. The legacy preview
    # helper remains available to explicit callers, but a normal new message
    # must not be intercepted by a client-side regex preflight.
    assert "async function previewAutomationProposal" in controller
    assert "const automationProposal = await previewAutomationProposal(sessionId, objective, state);" not in controller
    assert "if (automationProposal) return automationProposal;" not in controller
    assert "'/api/agents/automations/preview'" in controller
    assert "sessions.sendMessage(" in controller
    assert "const interaction = message?.interaction;" in session_controller
    assert "interactions[interaction.interaction_id]" in session_controller


def test_agent_dock_exposes_the_durable_observability_dashboard():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    controller = (AGENT / "agent-dock-controller.js").read_text(encoding="utf-8")
    dashboard = (AGENT / "agent-observability-dashboard.js").read_text(encoding="utf-8")

    assert 'id="agentObservabilityDashboard"' in html
    assert "'/api/agents/observability'" in controller
    assert "AgentObservabilityDashboard?.render" in controller
    assert "false_automation_notification_rate" in dashboard
    assert "average_tool_latency_ms" in dashboard
    assert "average_model_latency_ms" in dashboard
    assert "task_completion_rate" in dashboard
    assert "session_context_retrieval_precision" in dashboard
    assert "Runtime storage:" in dashboard
    assert "reclaimable_bytes" in dashboard
    assert "Operational alerts" in dashboard
    assert "operational_alerts" in dashboard
    assert "Chaos recovery:" in dashboard
    assert "scenario_count" in dashboard
    assert "Live order gate:" in dashboard


def test_all_three_waiting_states_have_distinct_ui_and_stream_semantics():
    formatters = (AGENT / "agent-formatters.js").read_text(encoding="utf-8")
    controls = (AGENT / "agent-control-bar.js").read_text(encoding="utf-8")
    stream = (AGENT / "agent-stream-controller.js").read_text(encoding="utf-8")

    assert "waiting_user_input: '等待補充資訊'" in formatters
    assert "waiting_decision: '等待使用者決策'" in formatters
    assert "waiting_approval: '等待批准'" in formatters
    assert "waiting_user_input: [['cancel', '取消']]" in controls
    assert "waiting_decision: [['cancel', '取消']]" in controls
    assert "'waiting_user_input', 'waiting_decision', 'waiting_approval'" in stream
    decision_card = (AGENT / "agent-decision-card.js").read_text(encoding="utf-8")
    assert "'resolved'" in decision_card


def test_new_or_selected_idle_session_is_not_mislabeled_as_provider_offline():
    store = (AGENT / "agent-dock-store.js").read_text(encoding="utf-8")
    session_controller = (AGENT / "agent-session-controller.js").read_text(encoding="utf-8")
    formatters = (AGENT / "agent-formatters.js").read_text(encoding="utf-8")

    assert "connection_state: 'idle'" in store
    assert session_controller.count("connection_state: 'idle'") == 2
    assert "idle: '待命'" in formatters


def test_artifact_selection_is_version_pinned_and_semantic_view_hides_debug_fields():
    selection = (AGENT / "agent-artifact-selection.js").read_text(encoding="utf-8")
    composer = (AGENT / "agent-composer-context.js").read_text(encoding="utf-8")
    automation = (AGENT / "agent-automation-view.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert "artifact_version" in selection
    assert "latest > selection.artifact_version" in composer
    assert "送出時會先檢查衝突" in composer
    assert "state.technical_view && state.debug_authorized === true && !options.compact" in automation
    assert "external_credentials_configured" in automation
    assert 'id="agentTechnicalViewToggle"' in html
    assert 'id="agentComposerContextChip"' in html
    assert 'id="agentRuntimeTimeline"' in html
    assert 'id="agentDecisionCards"' in html
    assert 'id="agentArtifactCanvas"' in html


def test_recoverable_partial_and_max_step_checkpoints_remain_active_through_continuation():
    reducer = AGENT / "agent-event-reducer.js"
    script = f"""
global.window = global;
require({json.dumps(str(reducer))});
let state = {{
  active_session_id: 'AS-repair', active_run_id: null,
  events: {{}}, ordered_event_ids: [], last_sequence_by_run: {{}}, runs: {{}},
  sessions: {{}}, plans: {{}}, steps: {{}}, tool_calls: {{}}, approvals: {{}},
  artifacts: {{}}, artifact_versions: {{}}, artifact_selections: {{}},
  messages: {{}}, skills: {{}}, forests: {{}}, branches: {{}}, interactions: {{}},
  automations: {{}}, evidence: {{}}, ordered_message_ids: []
}};
const base = {{run_id:'AR-repair',session_id:'AS-repair'}};
const project = (sequence, type, payload) => {{
  state = AgentEventReducer.reduce(state, {{...base,event_id:`E-${{sequence}}`,sequence,type,payload}});
  return {{status:state.runs['AR-repair'].status,active:state.active_run_id,pending:state.runs['AR-repair'].recovery_pending}};
}};
const checkpoints = [
  project(1, 'run.max_steps_reached', {{status:'max_steps_reached',recovery_pending:true}}),
  project(2, 'recovery.continuation_scheduled', {{summary:'switch source'}}),
  project(3, 'run.continuation_requested', {{resume_from_status:'max_steps_reached'}}),
  project(4, 'run.resumed', {{}}),
  project(5, 'run.completed', {{status:'completed'}})
];
process.stdout.write(JSON.stringify(checkpoints));
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True, timeout=5
    )
    assert json.loads(completed.stdout) == [
        {"status": "recovery_pending", "active": "AR-repair", "pending": True},
        {"status": "repairing", "active": "AR-repair", "pending": True},
        {"status": "queued", "active": "AR-repair", "pending": True},
        {"status": "running", "active": "AR-repair", "pending": True},
        {"status": "completed", "active": None, "pending": False},
    ]


def test_nonrecoverable_partial_checkpoint_is_still_terminal():
    reducer = AGENT / "agent-event-reducer.js"
    script = f"""
global.window = global;
require({json.dumps(str(reducer))});
let state = {{events:{{}},ordered_event_ids:[],last_sequence_by_run:{{}},runs:{{}},plans:{{}},steps:{{}},tool_calls:{{}},approvals:{{}},artifacts:{{}},artifact_versions:{{}},artifact_selections:{{}},messages:{{}},skills:{{}},sessions:{{}},forests:{{}},branches:{{}},interactions:{{}},automations:{{}},evidence:{{}},ordered_message_ids:[]}};
state = AgentEventReducer.reduce(state, {{
  event_id:'E-terminal',sequence:1,type:'run.partially_completed',
  run_id:'AR-terminal',session_id:'AS-terminal',
  payload:{{status:'partially_completed',recovery_pending:false}}
}});
process.stdout.write(JSON.stringify({{status:state.runs['AR-terminal'].status,active:state.active_run_id,terminal:AgentEventReducer.isTerminalRun(state.runs['AR-terminal'])}}));
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True, timeout=5
    )
    assert json.loads(completed.stdout) == {
        "status": "partially_completed",
        "active": None,
        "terminal": True,
    }


def test_recovery_pending_snapshot_and_stream_keep_the_same_run_live():
    reducer_path = AGENT / "agent-event-reducer.js"
    store_path = AGENT / "agent-dock-store.js"
    store = store_path.read_text(encoding="utf-8")
    stream = (AGENT / "agent-stream-controller.js").read_text(encoding="utf-8")
    formatters = (AGENT / "agent-formatters.js").read_text(encoding="utf-8")
    controller = (AGENT / "agent-dock-controller.js").read_text(encoding="utf-8")

    script = f"""
global.window = global;
global.localStorage = {{getItem:()=>null,setItem:()=>undefined}};
global.requestAnimationFrame = callback => callback();
require({json.dumps(str(reducer_path))});
require({json.dumps(str(store_path))});
AgentDockStore.applySnapshot({{
  run:{{
    run_id:'AR-snapshot',session_id:'AS-snapshot',status:'partially_completed',terminal:true,
    result:{{status:'partially_completed',recovery_pending:true}}
  }},
  artifact_versions:{{}},steps:[],interactions:[]
}});
const run = AgentDockStore.getState().runs['AR-snapshot'];
process.stdout.write(JSON.stringify({{status:run.status,active:AgentDockStore.getState().active_run_id,terminal:run.terminal,pending:run.recovery_pending}}));
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True, timeout=5
    )
    assert json.loads(completed.stdout) == {
        "status": "recovery_pending",
        "active": "AR-snapshot",
        "terminal": False,
        "pending": True,
    }

    assert "snapshotRecoveryPending" in store
    assert "status: 'recovery_pending'" in store
    assert "active_run_id: projectedRun.terminal ? null : run.run_id" in store
    assert "result.recovery_pending === true" in stream
    assert "if (!isTerminalRun(run))" in stream
    assert "connection_state: 'streaming'" in stream
    assert "if (!window.AgentEventReducer?.isTerminalRun?.(hydratedRun))" in controller
    assert "recovery_pending: '已保留進度，準備自動修復'" in formatters
    assert "repairing: '正在從 Checkpoint 修復'" in formatters


def test_approval_resume_uses_the_run_scoped_approval_record_and_rehydrates_before_streaming():
    controller = (AGENT / "agent-dock-controller.js").read_text(encoding="utf-8")

    assert "Object.values(store.getState().approvals || {}).find" in controller
    assert "String(item?.approval_id || '') === String(approvalId)" in controller
    assert "await hydrateRun(approval.run_id);" in controller
    assert "return followRun(approval.run_id);" in controller


def test_task_dag_uses_host_layout_and_real_dependency_edges():
    task_view = AGENT / "agent-task-tree-view.js"
    script = f"""
global.window = global;
global.AgentPlanView = {{activeRunId: () => 'AR-graph'}};
require({json.dumps(str(task_view))});
const layout = AgentTaskTreeView.dagLayout([
  {{node_id:'root',title:'研究'}},
  {{node_id:'left',title:'台灣',dependencies:['root']}},
  {{node_id:'right',title:'美國',dependency_ids:[{{node_id:'root'}}]}},
  {{node_id:'join',title:'整合',dependencies:['left','right']}}
]);
process.stdout.write(JSON.stringify({{
  rootX:layout.positions.get('root').x,
  leftX:layout.positions.get('left').x,
  rightX:layout.positions.get('right').x,
  joinX:layout.positions.get('join').x,
  dependencies:AgentTaskTreeView.dependencyIds({{dependencies:[{{id:'root'}},'left']}})
}}));
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True, timeout=5
    )
    result = json.loads(completed.stdout)
    assert result["rootX"] < result["leftX"] == result["rightX"] < result["joinX"]
    assert result["dependencies"] == ["root", "left"]
    source = task_view.read_text(encoding="utf-8")
    assert "document.createElementNS('http://www.w3.org/2000/svg', name)" in source
    assert "class: 'agent-dag-edge'" in source
    assert "agent-fishbone-axis" in source
    assert "agent-fishbone-causes" in source


def test_task_view_hides_branch_projections_of_existing_plan_nodes():
    task_view = AGENT / "agent-task-tree-view.js"
    script = f"""
global.window = global;
global.AgentPlanView = {{activeRunId: () => 'AR-graph'}};
require({json.dumps(str(task_view))});
const visible = AgentTaskTreeView.stepsForRun({{
  steps: {{
    'AR-graph:tool-1': {{
      run_id: 'AR-graph', node_id: 'tool-1', title: '取得行情', type: 'tool', order_index: 0
    }}
  }},
  branches: {{
    'BR-root': {{
      branch_id: 'BR-root', run_id: 'AR-graph', objective: '研究 2330.TW', order_index: 0
    }},
    'BR-projection': {{
      branch_id: 'BR-projection', run_id: 'AR-graph', objective: '取得行情',
      source_plan_node_id: 'tool-1', order_index: 1
    }}
  }}
}}, 'AR-graph');
process.stdout.write(JSON.stringify(visible.map(item => item.node_id)));
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True, timeout=5
    )
    assert json.loads(completed.stdout) == ["BR-root", "tool-1"]


def test_task_view_only_marks_independent_execution_lanes_as_parallel():
    task_view = AGENT / "agent-task-tree-view.js"
    script = f"""
global.window = global;
global.AgentPlanView = {{activeRunId: () => 'AR-graph'}};
require({json.dumps(str(task_view))});
const sequential = [
  {{node_id:'analysis', branch_id:'BR-analysis', tool_name:'market.analyze_symbol'}},
  {{node_id:'research', branch_id:'BR-research', tool_name:'market.research_pack', dependencies:['analysis']}},
  {{node_id:'preview', branch_id:'BR-preview', tool_name:'paper.preview_order', dependencies:['research']}}
];
const parallel = [
  {{node_id:'market', branch_id:'BR-market', tool_name:'market.analyze_symbol'}},
  {{node_id:'news', branch_id:'BR-news', tool_name:'market.research_pack'}},
  {{node_id:'join', branch_id:'BR-main', tool_name:'artifact.create_structured', dependencies:['market','news']}}
];
process.stdout.write(JSON.stringify({{
  sequential: AgentTaskTreeView.hasParallelBranches(sequential),
  parallel: AgentTaskTreeView.hasParallelBranches(parallel)
}}));
"""
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True, timeout=5
    )
    assert json.loads(completed.stdout) == {"sequential": False, "parallel": True}


def test_visual_nodes_and_decisions_expose_versioned_selection_and_visible_alternatives():
    schema = (AGENT / "agent-schema-visualization.js").read_text(encoding="utf-8")
    evidence = (AGENT / "agent-evidence-graph.js").read_text(encoding="utf-8")
    decision = (AGENT / "agent-decision-card.js").read_text(encoding="utf-8")
    composer = (AGENT / "agent-composer-context.js").read_text(encoding="utf-8")
    artifact = (AGENT / "agent-artifact-view.js").read_text(encoding="utf-8")
    css = (STATIC / "css" / "features" / "agent-dock.css").read_text(encoding="utf-8")

    for source in (schema, evidence, decision):
        assert "artifact_version" in source
        assert "path:" in source or "path," in source
        assert "AgentArtifactSelection" in source
    assert "agent-dom-dag-edge" in schema
    assert "agent-fishbone-causes" in schema
    assert "agent-evidence-edges" in evidence
    assert "agent-evidence-edge is-${link.kind}" in evidence
    assert "Agent 暫定建議" in decision
    assert "Agent 建議" in decision
    assert "替代方案 ${index + 1}" in decision
    assert "normalized.reason || normalized.tradeoff" in decision
    assert "agent-composer-context-path" in composer
    assert "chip.dataset.artifactVersion" in composer
    assert "agent-artifact-version-picker" in artifact
    assert "aria-pressed" in artifact
    assert ".agent-dag-edge{" in css
    assert ".agent-fishbone-axis{" in css
    assert ".agent-evidence-edge.is-support" in css


def test_skill_audit_keys_include_run_step_tool_call_and_event_type():
    source = (AGENT / "agent-event-reducer.js").read_text(encoding="utf-8")
    conversation = (AGENT / "agent-conversation-view.js").read_text(encoding="utf-8")

    assert "[runId, stepId, toolCallId, type, rawId].join(':')" in source
    assert "[eventRunId, stepId, toolCallId, type, rawId].join(':')" in conversation


def test_conversation_feed_uses_keyed_windowing_and_keeps_older_records_loadable():
    source = (AGENT / "agent-conversation-view.js").read_text(encoding="utf-8")

    assert "container.replaceChildren" not in source
    assert "container._agentFeedNodes" in source
    assert "dataset.windowSize" in source
    assert "載入較早紀錄" in source
    assert "entries.slice(-windowSize)" in source
    assert "nearBottom" in source
