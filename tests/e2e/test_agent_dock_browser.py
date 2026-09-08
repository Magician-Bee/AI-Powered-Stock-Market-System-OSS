from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[2]
AGENT = ROOT / "src" / "stock_ai" / "ui" / "static" / "js" / "features" / "agent"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch(headless=True)
        except PlaywrightError:
            try:
                instance = playwright.chromium.launch(channel="chrome", headless=True)
            except PlaywrightError as exc:
                pytest.skip(f"Chromium/Chrome is unavailable for browser E2E: {exc}")
        yield instance
        instance.close()


def _load(page, *names: str) -> None:
    for name in names:
        page.add_script_tag(content=(AGENT / name).read_text(encoding="utf-8"))


def test_one_thousand_events_use_a_keyed_window_and_preserve_scroll_behavior(browser):
    page = browser.new_page(viewport={"width": 900, "height": 700})
    page.set_content(
        """
        <style>
          #feed { width: 380px; height: 300px; overflow: auto; display: grid;
                  align-content: start; gap: 4px; }
          article { min-height: 24px; border: 1px solid #999; }
        </style>
        <div id="feed"></div>
        """
    )
    _load(
        page,
        "agent-formatters.js",
        "agent-tool-view.js",
        "agent-skill-view.js",
        "agent-approval-view.js",
        "agent-artifact-view.js",
        "agent-event-reducer.js",
        "agent-conversation-view.js",
    )
    result = page.evaluate(
        """
        () => {
          let state = {
            active_session_id: 'AS-load', active_run_id: 'AR-load',
            events: {}, ordered_event_ids: [], last_sequence_by_run: {},
            runs: {'AR-load': {run_id: 'AR-load', session_id: 'AS-load', status: 'running'}},
            plans: {}, steps: {}, tool_calls: {}, approvals: {}, artifacts: {},
            messages: {}, skills: {}, ordered_message_ids: []
          };
          for (let sequence = 1; sequence <= 1000; sequence += 1) {
            const rawId = `call-${Math.ceil(sequence / 2)}`;
            state = AgentEventReducer.reduce(state, {
              event_id: `E-${sequence}`, sequence, run_id: 'AR-load',
              session_id: 'AS-load',
              type: sequence % 2 ? 'tool.started' : 'tool.completed',
              tool_call_id: rawId,
              payload: {call_id: rawId, tool: 'market.observe'}
            });
          }
          window.testState = state;
          window.AgentDockStore = {getState: () => window.testState};
          const feed = document.getElementById('feed');
          AgentConversationView.render(feed, state);
          const initialCards = feed.querySelectorAll('.agent-tool-card').length;
          const loadLabel = feed.querySelector('.agent-feed-load-earlier')?.textContent || '';
          window.unchangedCard = feed.querySelector('[data-tool-call-id="call-450"]');

          state = AgentEventReducer.reduce(state, {
            event_id: 'E-1001', sequence: 1001, run_id: 'AR-load',
            session_id: 'AS-load', type: 'tool.retrying',
            tool_call_id: 'call-500',
            payload: {call_id: 'call-500', tool: 'market.observe'}
          });
          window.testState = state;
          AgentConversationView.render(feed, state);
          const retainedNode = window.unchangedCard ===
            feed.querySelector('[data-tool-call-id="call-450"]');

          feed.scrollTop = 0;
          state = AgentEventReducer.reduce(state, {
            event_id: 'E-1002', sequence: 1002, run_id: 'AR-load',
            session_id: 'AS-load', type: 'reasoning.summary',
            payload: {summary: 'new event while reading old content'}
          });
          window.testState = state;
          AgentConversationView.render(feed, state);
          const stayedAtTop = feed.scrollTop === 0;

          feed.scrollTop = feed.scrollHeight;
          state = AgentEventReducer.reduce(state, {
            event_id: 'E-1003', sequence: 1003, run_id: 'AR-load',
            session_id: 'AS-load', type: 'reasoning.summary',
            payload: {summary: 'new event near bottom'}
          });
          window.testState = state;
          AgentConversationView.render(feed, state);
          const bottomGap = feed.scrollHeight - feed.scrollTop - feed.clientHeight;

          feed.scrollTop = 40;
          const beforeHeight = feed.scrollHeight;
          const beforeTop = feed.scrollTop;
          feed.querySelector('.agent-feed-load-earlier').click();
          return {
            reducedEventCount: state.ordered_event_ids.length,
            reducedToolCount: Object.keys(state.tool_calls).length,
            initialCards,
            loadLabel,
            retainedNode,
            stayedAtTop,
            bottomGap,
            loadedCards: feed.querySelectorAll('.agent-tool-card').length,
            preservedOlderPosition: feed.scrollTop > beforeTop,
            heightIncreased: feed.scrollHeight > beforeHeight
          };
        }
        """
    )
    page.close()

    assert result["reducedEventCount"] == 1003
    assert result["reducedToolCount"] == 500
    assert result["initialCards"] == 120
    assert "尚有 380 筆" in result["loadLabel"]
    assert result["retainedNode"] is True
    assert result["stayedAtTop"] is True
    assert result["bottomGap"] <= 2
    assert result["loadedCards"] == 238
    assert result["preservedOlderPosition"] is True
    assert result["heightIncreased"] is True


def test_task_tree_artifact_scope_and_approval_focus_are_real_dom_behaviors(browser):
    page = browser.new_page(viewport={"width": 900, "height": 700})
    page.set_content(
        """
        <input id="reader-focus" />
        <div id="tasks"></div>
        <div id="artifacts"></div>
        <div id="approvals"></div>
        """
    )
    _load(
        page,
        "agent-formatters.js",
        "agent-plan-view.js",
        "agent-task-tree-view.js",
        "agent-artifact-view.js",
        "agent-approval-view.js",
    )
    result = page.evaluate(
        """
        () => {
          window.AgentDockController = {
            openTask() {},
            resolveApproval() {}
          };
          const state = {
            active_session_id: 'AS-new',
            active_run_id: 'AR-new',
            runs: {
              'AR-new': {
                run_id: 'AR-new', session_id: 'AS-new', plan_id: 'AP-new',
                status: 'running', updated_at: '2026-07-29T00:00:00Z'
              }
            },
            plans: {
              'AR-new:AP-new': {
                run_id: 'AR-new', session_id: 'AS-new', plan_id: 'AP-new',
                completion_criteria: ['兩個候選皆有 Host Evidence']
              }
            },
            plan_revisions: {
              'AR-new': [
                {revision: 1, reason_summary: 'initial', plan: {nodes: []}},
                {revision: 2, reason_summary: 'parallel analysis', plan: {nodes: [
                  {node_id: 'root'}, {node_id: 'child-a'}, {node_id: 'child-b'}
                ]}}
              ]
            },
            steps: {
              'AR-new:root': {
                run_id: 'AR-new', session_id: 'AS-new', node_id: 'root',
                title: '分析候選股票', status: 'running', order_index: 0
              },
              'AR-new:child-a': {
                run_id: 'AR-new', session_id: 'AS-new', node_id: 'child-a',
                parent_node_id: 'root', title: '分析 2330.TW', status: 'completed',
                assigned_agent: 'market-agent', tool_call_ids: ['call-a'], order_index: 1
              },
              'AR-new:child-b': {
                run_id: 'AR-new', session_id: 'AS-new', node_id: 'child-b',
                parent_node_id: 'root', title: '比較風險', status: 'blocked',
                dependency_ids: ['child-a'], error_summary: '等待 Evidence',
                order_index: 2
              }
            },
            artifacts: {
              'AR-new:A-new': {
                artifact_id: 'A-new', run_id: 'AR-new', session_id: 'AS-new',
                step_id: 'child-a', name: 'new-report.txt', media_type: 'text/plain'
              },
              'AR-old:A-old': {
                artifact_id: 'A-old', run_id: 'AR-old', session_id: 'AS-old',
                name: 'old-report.txt', media_type: 'text/plain'
              }
            }
          };
          AgentTaskTreeView.render(document.getElementById('tasks'), state);
          AgentArtifactView.render(document.getElementById('artifacts'), state);

          const approvals = document.getElementById('approvals');
          const approval = {
            approval_id: 'APR-one', status: 'pending', tool_name: 'project.write',
            risk_class: 'write', expires_at: '2026-07-29T01:00:00Z'
          };
          const first = AgentApprovalView.create(approval);
          approvals.append(first);
          return new Promise(resolve => queueMicrotask(() => {
            const firstFocused = document.activeElement === first;
            const reader = document.getElementById('reader-focus');
            reader.focus();
            approvals.append(AgentApprovalView.create(approval));
            queueMicrotask(() => resolve({
              childCount: document.querySelectorAll('.agent-task-children > li').length,
              taskText: document.getElementById('tasks').textContent,
              artifactCards: document.querySelectorAll('#artifacts .agent-artifact-card').length,
              artifactText: document.getElementById('artifacts').textContent,
              firstFocused,
              focusStayedWithReader: document.activeElement === reader
            }));
          }));
        }
        """
    )
    page.close()

    assert result["childCount"] == 2
    assert "平行分支" in result["taskText"]
    assert "Agent：market-agent" in result["taskText"]
    assert "依賴" in result["taskText"]
    assert "阻塞原因：等待 Evidence" in result["taskText"]
    assert "Revision History · 2 版" in result["taskText"]
    assert result["artifactCards"] == 1
    assert "new-report.txt" in result["artifactText"]
    assert "old-report.txt" not in result["artifactText"]
    assert result["firstFocused"] is True
    assert result["focusStayedWithReader"] is True


def test_text_artifact_can_be_selected_and_revised_from_the_real_card(browser):
    page = browser.new_page(viewport={"width": 900, "height": 700})
    page.set_content('<div id="artifacts"></div>')
    _load(page, "agent-formatters.js", "agent-plan-view.js", "agent-artifact-selection.js", "agent-artifact-view.js")
    result = page.evaluate(
        """
        async () => {
          let state = {
            active_session_id:'AS-1', active_run_id:'AR-1',
            runs:{'AR-1':{run_id:'AR-1',session_id:'AS-1'}}, plans:{},
            artifacts:{'AR-1:ART-1':{artifact_id:'ART-1',run_id:'AR-1',session_id:'AS-1',
              name:'法人籌碼.txt',media_type:'text/plain',step_id:'NODE-inst'}},
            artifact_versions:{'ART-1:v1':{artifact_id:'ART-1',version:1,
              content:{name:'法人籌碼.txt',content:'法人籌碼判讀目前採單日。'}}},
            artifact_selections:{}, active_selection:null
          };
          const changes = [];
          window.AgentDockStore = {getState:()=>state,set:patch=>{state={...state,...patch};return state;}};
          window.AgentDockController = {proposeArtifactChange:async (artifactId, change) => {
            changes.push({artifactId, change}); return {artifact_id:artifactId,version:2};
          }};
          AgentArtifactView.render(document.getElementById('artifacts'), state);
          document.querySelector('[aria-label="修改 Artifact：法人籌碼.txt"]').click();
          await new Promise(resolve => requestAnimationFrame(resolve));
          // A Dock state update rerenders the whole Artifact panel.  The
          // revision editor must survive that normal production path instead
          // of existing only on the pre-rendered card.
          AgentArtifactView.render(document.getElementById('artifacts'), state);
          const draftBeforeSubmit = state.artifact_revision_draft;
          const content = document.querySelector('[aria-label="修改 法人籌碼.txt 的內容"]');
          const reason = document.querySelector('[aria-label="修改 法人籌碼.txt 的原因"]');
          content.value = '法人籌碼判讀目前採三日累計。';
          reason.value = '將單日判讀改為三日累計';
          document.querySelector('.agent-artifact-revision-form').requestSubmit();
          await new Promise(resolve => setTimeout(resolve, 0));
          state = {
            ...state,
            active_selection:{...state.active_selection, artifact_version:3},
            artifact_versions:{
              ...state.artifact_versions,
              'ART-1:v3':{artifact_id:'ART-1',version:3,
                content:{name:'法人籌碼.txt',content:'已還原的內容。'}},
            },
          };
          AgentArtifactView.render(document.getElementById('artifacts'), state);
          return {
            selected:state.active_selection,
            draftBeforeSubmit,
            draft:state.artifact_revision_draft,
            changes,
            status:document.querySelector('.agent-artifact-revision-status').textContent,
          };
        }
        """
    )
    page.close()

    assert result["selected"]["artifact_id"] == "ART-1"
    assert result["selected"]["artifact_version"] == 3
    assert result["draftBeforeSubmit"] == {"artifact_id": "ART-1", "artifact_version": 1}
    assert result["draft"] == {
        "artifact_id": "ART-1",
        "artifact_version": 2,
        "status": "已建立 Artifact v2。",
    }
    assert result["changes"] == [{
        "artifactId": "ART-1",
        "change": {
            "expected_version": 1,
            "content": {"name": "法人籌碼.txt", "content": "法人籌碼判讀目前採三日累計。"},
            "reason": "將單日判讀改為三日累計",
            "affected_node_ids": ["NODE-inst"],
        },
    }]
    assert result["status"] == "會先驗證版本與相依節點，再要求你確認。"


def test_run_controls_and_approval_actions_dispatch_from_real_buttons(browser):
    page = browser.new_page(viewport={"width": 900, "height": 700})
    page.set_content(
        """
        <div id="controls"></div>
        <div id="approval"></div>
        """
    )
    _load(
        page,
        "agent-formatters.js",
        "agent-control-bar.js",
        "agent-approval-view.js",
    )
    result = page.evaluate(
        """
        () => {
          const calls = [];
          window.AgentDockController = {
            control(action, runId) {
              calls.push({kind: 'control', action, runId});
            },
            resolveApproval(approvalId, approved) {
              calls.push({kind: 'approval', approvalId, approved});
            }
          };
          const container = document.getElementById('controls');
          const renderAndClick = (status, labels) => {
            AgentControlBar.render(container, {
              active_session_id: 'AS-controls',
              active_run_id: 'AR-controls',
              runs: {
                'AR-controls': {
                  run_id: 'AR-controls', session_id: 'AS-controls', status
                }
              }
            });
            labels.forEach(label => {
              const button = Array.from(container.querySelectorAll('button'))
                .find(item => item.textContent === label);
              if (!button) throw new Error(`missing ${status} control: ${label}`);
              button.click();
            });
          };
          renderAndClick('running', ['暫停', '取消']);
          renderAndClick('paused', ['繼續']);
          renderAndClick('max_steps_reached', [
            '繼續執行', '增加步驟上限', '重新規劃', '建立新 Run'
          ]);

          const approval = AgentApprovalView.create({
            approval_id: 'APR-actions', status: 'pending',
            tool_name: 'project.write', risk_class: 'write',
            expires_at: '2026-07-29T01:00:00Z'
          });
          document.getElementById('approval').append(approval);
          approval.querySelector('.agent-approval-actions button').click();
          approval.querySelector('.agent-approval-actions button.primary').click();
          return calls;
        }
        """
    )
    page.close()

    assert result == [
        {"kind": "control", "action": "pause", "runId": "AR-controls"},
        {"kind": "control", "action": "cancel", "runId": "AR-controls"},
        {"kind": "control", "action": "resume", "runId": "AR-controls"},
        {"kind": "control", "action": "continue", "runId": "AR-controls"},
        {"kind": "control", "action": "increase_limit", "runId": "AR-controls"},
        {"kind": "control", "action": "replan", "runId": "AR-controls"},
        {"kind": "control", "action": "new_run", "runId": "AR-controls"},
        {"kind": "approval", "approvalId": "APR-actions", "approved": False},
        {"kind": "approval", "approvalId": "APR-actions", "approved": True},
    ]


def test_completed_paper_run_creates_an_advisory_draft_instead_of_replaying(browser):
    page = browser.new_page(viewport={"width": 900, "height": 700})
    page.set_content(
        '''
        <div id="controls"></div>
        <select id="agentComposerMode"><option value="current_run">current</option><option value="new_run">new</option></select>
        <select id="agentAutonomySelect"><option value="advisory">advisory</option><option value="paper_execute" selected>paper</option></select>
        <textarea id="agentComposerInput"></textarea>
        '''
    )
    _load(page, "agent-control-bar.js")
    result = page.evaluate(
        """
        async controllerSource => {
          let state = {
            active_session_id:'AS-paper', active_run_id:'AR-paper', active_tab:'tasks', dock_open:false,
            runs:{'AR-paper':{run_id:'AR-paper',session_id:'AS-paper',status:'completed',autonomy:'paper_execute',objective:'完成一筆紙上模擬交易'}},
            tool_calls:{'AR-paper:submit':{run_id:'AR-paper',tool_name:'paper.submit_order',risk_class:'financial_paper'}}
          };
          window.AgentDockStore = {getState:()=>state,set:patch=>{state={...state,...patch};},dispatch:()=>{}};
          window.AgentStreamController = class { stop() {} };
          window.AgentSessionController = class {
            async create(title) { return {session_id:'AS-safe-draft',title}; }
          };
          window.AgentComposerContext = {payload:()=>null};
          window.AgentTaskTreeView = {render(){}};
          window.AgentPlanView = {render(){}};
          window.AgentContextBar = {render(){}};
          window.AgentConversationView = {render(){}};
          window.AgentArtifactView = {render(){}};
          window.AgentComposer = {init(){}};
          window.AgentRuntimeApi = async () => ({ });
          const script = document.createElement('script');
          script.textContent = controllerSource;
          document.head.append(script);
          AgentControlBar.render(document.getElementById('controls'), state);
          const label = document.querySelector('#controls button').textContent;
          await AgentDockController.control('draft_new_goal', 'AR-paper');
          return {
            label,
            value: document.getElementById('agentComposerInput').value,
            mode: document.getElementById('agentComposerMode').value,
            autonomy: document.getElementById('agentAutonomySelect').value,
            session: state.active_session_id,
            activeRun: state.active_run_id,
            tab: state.active_tab,
            open: state.dock_open,
          };
        }
        """,
        arg=(AGENT / "agent-dock-controller.js").read_text(encoding="utf-8"),
    )
    page.close()

    assert result == {
        "label": "建立新目標草稿",
        "value": "請重新檢視目前標的的資料狀態、分析結論與主要風險；只做分析與預覽，不要建立、提交或重複任何訂單、外部操作或專案修改。若需要新的執行操作，請由我另行明確指定。",
        "mode": "new_run",
        "autonomy": "advisory",
        "session": "AS-safe-draft",
        "activeRun": None,
        "tab": "chat",
        "open": True,
    }


def test_clean_sse_eof_reconnects_and_reaches_terminal_result(browser):
    page = browser.new_page(viewport={"width": 900, "height": 700})
    _load(page, "agent-stream-controller.js")
    result = page.evaluate(
        """
        async controllerSource => {
          const connectionStates = [];
          let fetchCount = 0;
          const state = {
            last_sequence_by_run: {'AR-stream': 0},
            runs: {'AR-stream': {run_id: 'AR-stream', status: 'running'}}
          };
          const store = {
            getState: () => state,
            set: patch => {
              if (patch.connection_state) connectionStates.push(patch.connection_state);
            },
            dispatch: event => {
              state.last_sequence_by_run[event.run_id] = event.sequence;
            }
          };
          window.fetch = async () => {
            fetchCount += 1;
            const payload = fetchCount === 1
              ? ''
              : 'data: {"type":"result","result":{"status":"completed"}}\\n\\n';
            return new Response(payload, {
              status: 200,
              headers: {'content-type': 'text/event-stream'}
            });
          };
          const controller = new AgentStreamController(store);
          const terminal = await controller.connect('AR-stream');
          controller.stop();
          return {fetchCount, connectionStates, terminal};
        }
        """
    )
    page.close()

    assert result["fetchCount"] == 2
    assert "reconnecting" in result["connectionStates"]
    assert result["connectionStates"][-1] == "offline"
    assert result["terminal"] == {"status": "completed"}


def test_task_forest_click_expands_and_creates_versioned_composer_context(browser):
    page = browser.new_page(viewport={"width": 900, "height": 700})
    page.set_content('<div id="tasks"></div><div id="context"></div>')
    _load(
        page,
        "agent-formatters.js",
        "agent-plan-view.js",
        "agent-artifact-selection.js",
        "agent-composer-context.js",
        "agent-task-tree-view.js",
        "agent-task-forest.js",
    )
    result = page.evaluate(
        """
        () => {
          let state = {
            active_session_id: 'AS-forest', active_run_id: 'AR-forest',
            runs: {'AR-forest': {run_id:'AR-forest',session_id:'AS-forest',status:'running'}},
            plans: {}, plan_revisions: {}, steps: {}, artifacts: {}, artifact_versions: {
              'ART-1:v5': {artifact_id:'ART-1',version:5},
              'ART-1:v7': {artifact_id:'ART-1',version:7}
            }, artifact_selections: {}, collapsed_task_nodes: {},
            forests: {'F-1': {forest_id:'F-1',run_id:'AR-forest',title:'緯創持股分析'}},
            branches: {
              'BR-root': {branch_id:'BR-root',run_id:'AR-forest',session_id:'AS-forest',title:'研究',status:'running',artifact_id:'ART-1',artifact_version:5},
              'BR-us': {branch_id:'BR-us',parent_branch_id:'BR-root',run_id:'AR-forest',session_id:'AS-forest',title:'美國來源',status:'queued'}
            }, active_selection: null
          };
          window.AgentDockStore = {
            getState: () => state,
            set: patch => { state = {...state, ...patch}; }
          };
          AgentTaskForest.render(document.getElementById('tasks'), state);
          const root = document.querySelector('[data-node-id="BR-root"] .agent-task-node-card');
          const childList = document.getElementById(root.getAttribute('aria-controls'));
          const expandedBefore = root.getAttribute('aria-expanded');
          root.click();
          AgentComposerContext.render(document.getElementById('context'), state);
          return {
            expandedBefore,
            collapsedAfter: childList.hidden,
            targetType: state.active_selection.target_type,
            branchId: state.active_selection.branch_id,
            selectedVersion: state.active_selection.artifact_version,
            contextText: document.getElementById('context').textContent
          };
        }
        """
    )
    page.close()

    assert result == {
        "expandedBefore": "true",
        "collapsedAfter": True,
        "targetType": "branch",
        "branchId": "BR-root",
        "selectedVersion": 5,
        "contextText": "正在討論：Task Forest ＞ 研究v5目前已更新到 v7，送出時會先檢查衝突×",
    }


def test_evidence_click_keeps_the_source_artifact_version_in_composer(browser):
    page = browser.new_page(viewport={"width": 900, "height": 700})
    page.set_content('<div id="evidence"></div><div id="context"></div>')
    _load(page, "agent-artifact-selection.js", "agent-composer-context.js", "agent-evidence-graph.js")
    result = page.evaluate(
        """
        () => {
          let state = {
            active_session_id:'AS-1',
            artifacts:{},
            artifact_versions:{
              'ART-risk:v2':{artifact_id:'ART-risk',version:2},
              'ART-risk:v3':{artifact_id:'ART-risk',version:3}
            },
            artifact_selections:{},
            active_selection:null,
            evidence:{
              'EV-1':{evidence_id:'EV-1',session_id:'AS-1',claim:{symbol:'2330.TW',recommendation_bucket:'data_blocked'},source_type:'market',
                artifact_id:'ART-risk',artifact_version:2,branch_id:'BR-risk',node_id:'NODE-risk'},
              'EV-2':{evidence_id:'EV-2',session_id:'AS-1',
                claim:'{"schema_version":"open_stock_ai.web_research.v1","query":"TSMC 2330.TW today","source_count":3}',
                source_type:'web'}
            }
          };
          window.AgentDockStore = {
            getState:()=>state,
            set:patch=>{state={...state,...patch};}
          };
          AgentEvidenceGraph.render(document.getElementById('evidence'), state);
          document.querySelector('[data-evidence-id="EV-1"]').click();
          AgentComposerContext.render(document.getElementById('context'), state);
          return {
            artifactId:state.active_selection.artifact_id,
            version:state.active_selection.artifact_version,
            evidenceId:state.active_selection.evidence_id,
            branchId:state.active_selection.branch_id,
            nodeId:state.active_selection.node_id,
            webClaim:document.querySelector('[data-evidence-id="EV-2"] strong').textContent,
            context:document.getElementById('context').textContent
          };
        }
        """
    )
    page.close()

    assert result == {
        "artifactId": "ART-risk",
        "version": 2,
        "evidenceId": "EV-1",
        "branchId": "BR-risk",
        "nodeId": "NODE-risk",
        "webClaim": "外部研究已取得 3 個來源：TSMC 2330.TW today",
        "context": "正在討論：Evidence Graph ＞ 2330.TW：資料不足，尚無法形成可靠判斷。v2目前已更新到 v3，送出時會先檢查衝突×",
    }


def test_schema_visualizations_cover_p28_renderers_without_model_svg(browser):
    page = browser.new_page(viewport={"width": 1200, "height": 900})
    page.set_content('<div id="visualizations"></div>')
    _load(page, "agent-schema-visualization.js")
    result = page.evaluate(
        """
        () => {
          const schemas = [
            {renderer:'table',title:'Table',columns:[{key:'label',label:'項目'},{key:'value',label:'值'}],rows:[{id:'row',label:'市場',value:'正常'}]},
            {renderer:'chart',title:'Chart',items:[{id:'price',label:'價格',value:100}]},
            {renderer:'graph',title:'Graph',nodes:[{id:'node',label:'研究節點'}]},
            {renderer:'canvas',title:'Canvas',items:[{id:'card',label:'假設'}]},
            {renderer:'map',title:'Map',features:[{id:'taiwan',label:'台灣'}]},
            {renderer:'evidence_graph',title:'Evidence Graph',nodes:[{id:'evidence',label:'證據'}]},
            {renderer:'task_forest',title:'Task Forest',nodes:[{id:'task',label:'任務'}]},
            {renderer:'dag',title:'Plan DAG',nodes:[{id:'A',label:'研究'},{id:'B',label:'決策',dependencies:['A']}]},
            {renderer:'swimlane',title:'Branches',lanes:[{label:'研究線',steps:[{label:'搜尋',status:'completed'}]}]},
            {renderer:'workflow',title:'Automation',steps:[{label:'Observe'},{label:'Notify'}]},
            {renderer:'timeline',title:'Events',events:[{label:'開始',start:0,end:1}]},
            {renderer:'gantt',title:'Long Run',tasks:[{label:'研究',start:0,end:4},{label:'驗證',start:3,end:6}]},
            {renderer:'fishbone',title:'Root Cause',effect:'資料延遲',causes:[{label:'來源',items:['逾時','缺值']}]},
            {renderer:'decision_matrix',title:'Options',criteria:[{id:'risk',label:'風險'}],options:[{label:'等待',values:{risk:'低'},total:8}]},
            {renderer:'risk_table',title:'Risk Table',factors:[{label:'波動',value:0.4}]},
            {renderer:'risk_bar',title:'Risk Bar',factors:[{label:'流動性',value:0.6}]},
            {renderer:'risk_radar',title:'Risk Radar',factors:[{label:'市場',value:0.5},{label:'信用',value:0.3}]},
          ];
          const artifacts = Object.fromEntries(schemas.map((visualization, index) => [
            `ART-${index}`,
            index === 0
              ? {artifact_id:`ART-${index}`,session_id:'AS-1',title:visualization.title,
                  renderer:visualization.renderer,schema_version:'open_stock_ai.visualization.v1',
                  document:{title:visualization.title,columns:visualization.columns,rows:visualization.rows}}
              : {artifact_id:`ART-${index}`,session_id:'AS-1',title:visualization.title,visualization},
          ]));
          AgentSchemaVisualization.render(document.getElementById('visualizations'), {
            active_session_id:'AS-1', artifacts,
          });
          return {
            renderers:Array.from(document.querySelectorAll('[data-renderer]')).map(node => node.dataset.renderer),
            titles:Array.from(document.querySelectorAll('.agent-schema-visualization>strong')).map(node => node.textContent),
            svgCount:document.querySelectorAll('svg').length,
            scriptCount:document.querySelectorAll('#visualizations script').length,
            ganttBars:document.querySelectorAll('[data-renderer="gantt"] .agent-schema-time-rail i').length,
            fishboneText:document.querySelector('[data-renderer="fishbone"]').textContent,
          };
        }
        """
    )
    page.close()

    assert result["renderers"] == [
        "table", "chart", "graph", "canvas", "map", "evidence_graph", "task_forest",
        "dag", "swimlane", "workflow", "timeline", "gantt", "fishbone",
        "decision_matrix", "risk_table", "risk_bar", "risk_radar",
    ]
    assert result["svgCount"] == 0
    assert result["scriptCount"] == 0
    assert result["ganttBars"] == 2
    assert "資料延遲" in result["fishboneText"]


def test_artifact_revision_posts_exact_api_contract_and_restore_uses_restore_endpoint(browser):
    page = browser.new_page(viewport={"width": 900, "height": 700})
    page.set_content('<div id="fixture"></div>')
    _load(page, "agent-artifact-selection.js")
    result = page.evaluate(
        """
        async controllerSource => {
          let state = {
            active_selection:{artifact_id:'ART-risk',artifact_version:5,target_type:'automation_node',
              branch_id:'BR-auto',node_id:'NODE-risk',path:'Automation ＞ Risk Score',label:'Risk Score'},
            artifact_versions:{'ART-risk:v5':{artifact_id:'ART-risk',version:5}},
            artifact_selections:{},runs:{},last_sequence_by_run:{},connection_state:'offline'
          };
          const calls = [];
          const confirmations = [];
          window.AgentDockStore = {
            getState:()=>state,
            set:patch=>{state={...state,...patch};return state;},
            dispatch:()=>{},applySnapshot:()=>{}
          };
          window.AgentStreamController = class { stop() {} };
          window.AgentSessionController = class {};
          window.AgentComposerContext = {payload:value=>AgentArtifactSelection.messageContext(value)};
          window.AgentRuntimeApi = async (url, options={}) => {
            calls.push({url,body:options.body ? JSON.parse(options.body) : null});
            if (url.endsWith('/restore')) return {artifact_id:'ART-risk',version:7,restored_from_version:4};
            return {artifact_id:'ART-risk',version:6,content:{risk_score:'dynamic'}};
          };
          window.confirm = message => {confirmations.push(message);return true;};
          const script = document.createElement('script');
          script.textContent = controllerSource;
          document.head.append(script);

          const revised = await AgentDockController.proposeArtifactChange('ART-risk', {
            content:{risk_score:'dynamic'},
            reason:'Use volatility-aware threshold',
            message_id:'MSG-9',
            affected_node_ids:['NODE-volatility']
          });
          const afterRevision = {...state.active_selection};
          const compared = await AgentDockController.proposeArtifactChange('ART-risk', {
            action:'compare',target_version:5
          });
          const restored = await AgentDockController.proposeArtifactChange('ART-risk', {
            action:'restore',target_version:4
          });
          return {calls,confirmations,revised,afterRevision,compared,restored,finalSelection:state.active_selection};
        }
        """,
        arg=(AGENT / "agent-dock-controller.js").read_text(encoding="utf-8"),
    )
    page.close()

    assert result["calls"] == [
        {
            "url": "/api/agents/artifacts/ART-risk/propose-change",
            "body": {
                "expected_version": 5,
                "content": {"risk_score": "dynamic"},
                "reason": "Use volatility-aware threshold",
                "affected_node_ids": ["NODE-volatility", "NODE-risk"],
                "message_id": "MSG-9",
            },
        },
        {
            "url": "/api/agents/artifacts/ART-risk/restore",
            "body": {"source_version": 4, "expected_version": 6},
        },
    ]
    assert len(result["confirmations"]) == 2
    assert result["revised"]["version"] == 6
    assert result["afterRevision"]["artifact_version"] == 6
    assert result["afterRevision"]["node_id"] == "NODE-risk"
    assert result["compared"] == {"action": "compare", "artifact_id": "ART-risk", "version": 5}
    assert result["restored"]["version"] == 7
    assert result["finalSelection"]["artifact_version"] == 7


def test_decision_card_accepts_preferred_option_and_free_text(browser):
    page = browser.new_page(viewport={"width": 900, "height": 700})
    page.set_content('<div id="decisions"></div>')
    _load(page, "agent-artifact-selection.js", "agent-decision-card.js")
    result = page.evaluate(
        """
        () => {
          let state = {active_session_id:'AS-1',artifact_versions:{},artifact_selections:{},active_selection:null};
          const answers = [];
          window.AgentDockStore = {getState:()=>state,set:patch=>{state={...state,...patch};}};
          window.AgentDockController = {respondInteraction:(id,answer)=>answers.push({id,answer})};
          const viewState = {...state, interactions:{
            'I-1': {interaction_id:'I-1',session_id:'AS-1',status:'awaiting_user',
              question:'偏向分批停利，採用哪個方案？',artifact_id:'ART-D',artifact_version:3,
              agent_view:'分批停利先降低風險，再保留部分上行空間。',preferred_option:'recommended',
              options:[{option_id:'recommended',label:'分批停利',value:'staged'},{option_id:'hold',label:'續抱',value:'hold'}]}
          }};
          AgentDecisionCard.render(document.getElementById('decisions'), viewState);
          document.querySelector('[data-option-id="recommended"]').click();
          const input = document.querySelector('.agent-decision-free-text input');
          input.value = '先賣三成，其餘續抱';
          document.querySelector('.agent-decision-free-text').requestSubmit();
          return {
            answers,
            text:document.getElementById('decisions').textContent,
            preferredClass:document.querySelector('[data-option-id="recommended"]').className
          };
        }
        """
    )
    page.close()

    assert "is-preferred" in result["preferredClass"]
    assert "分批停利 · Agent 建議" in result["text"]
    assert "分批停利先降低風險" in result["text"]
    assert result["answers"] == [
        {"id": "I-1", "answer": {"option_id": "recommended", "value": "staged", "artifact_version": 3}},
        {"id": "I-1", "answer": {"free_text": "先賣三成，其餘續抱", "artifact_version": 3}},
    ]


def test_post_answer_skip_replaces_the_stale_action_card_after_the_response(browser):
    page = browser.new_page(viewport={"width": 900, "height": 700})
    page.set_content('<div id="decisions"></div>')
    _load(page, "agent-artifact-selection.js", "agent-decision-card.js")
    result = page.evaluate(
        """
        async () => {
          let state = {active_session_id:'AS-1',artifact_versions:{},artifact_selections:{},active_selection:null};
          const answers = [];
          window.AgentDockStore = {getState:()=>state,set:patch=>{state={...state,...patch};}};
          window.AgentDockController = {respondInteraction:async (id,answer)=>{
            answers.push({id,answer});
            return {status:'resolved'};
          }};
          AgentDecisionCard.render(document.getElementById('decisions'), {
            ...state,
            interactions:{
              'I-skip': {interaction_id:'I-skip',session_id:'AS-1',run_id:'AR-complete',
                status:'waiting_decision',interaction_purpose:'post_answer_proposal',
                title:'Review Order Details',options:[
                  {option_id:'start_follow_up',label:'開始下一步'},
                  {option_id:'skip',label:'先略過'}
                ]}
            }
          });
          document.querySelector('[data-option-id="skip"]').click();
          await new Promise(resolve => setTimeout(resolve, 0));
          return {answers, text:document.getElementById('decisions').textContent};
        }
        """
    )
    page.close()

    assert result["answers"] == [{"id": "I-skip", "answer": {"option_id": "skip", "value": "先略過", "artifact_version": 1}}]
    assert "已略過此建議；目前結果保持完成，沒有建立新的 Run。" in result["text"]
    assert "Review Order Details" not in result["text"]


def test_current_run_message_and_semantic_automation_view_are_real_browser_behaviors(browser):
    page = browser.new_page(viewport={"width": 900, "height": 700})
    page.set_content(
        '<select id="agentComposerMode"><option value="current_run" selected>current</option></select>'
        '<div id="automation"></div>'
    )
    _load(page, "agent-formatters.js", "agent-artifact-selection.js", "agent-automation-view.js")
    result = page.evaluate(
        """
        async controllerSource => {
          let state = {
            active_session_id:'AS-current',active_run_id:'AR-current',
            runs:{'AR-current':{run_id:'AR-current',session_id:'AS-current',status:'running'}},
            artifact_selections:{},active_selection:{target_type:'branch',branch_id:'BR-us',path:'研究 ＞ 美國來源'}
          };
          const sent = [];
          window.AgentDockStore = {getState:()=>state,set:patch=>{state={...state,...patch};},dispatch:()=>{}};
          window.AgentStreamController = class { stop() {} };
          window.AgentSessionController = class {
            async sendMessage(sessionId, content, context) { sent.push({sessionId,content,context}); return {accepted:true}; }
          };
          window.AgentComposerContext = {payload: value => value.active_selection};
          window.AgentTaskTreeView = {render(){}};
          window.AgentPlanView = {activeRunId:()=>null,render(){}};
          window.AgentContextBar = {render(){}};
          window.AgentConversationView = {render(){}};
          window.AgentArtifactView = {render(){}};
          window.AgentControlBar = {render(){}};
          window.AgentComposer = {init(){}};
          const script = document.createElement('script');
          script.textContent = controllerSource;
          document.head.append(script);
          await AgentDockController.submit({objective:'美國來源也要查'});

          const automationState = {active_session_id:'AS-current',technical_view:false,automations:{
            'AUTO-1':{automation_id:'AUTO-1',session_id:'AS-current',title:'持股監控',status:'active',
              semantic_nodes:[{id:'quotes',label:'取得行情'},{id:'analyze',label:'分析'},{id:'notify',label:'重大改變才通知'}],
              workflow_id:'wf-secret',credential_ref:'cred-secret'}
          }};
          const container = document.getElementById('automation');
          AgentAutomationView.render(container, automationState);
          const semanticText = container.textContent;
          AgentAutomationView.render(container, {...automationState,technical_view:true});
          const unauthorizedTechnicalText = container.textContent;
          AgentAutomationView.render(container, {...automationState,technical_view:true,debug_authorized:true});
          return {sent,semanticText,unauthorizedTechnicalText,technicalText:container.textContent};
        }
        """,
        arg=(AGENT / "agent-dock-controller.js").read_text(encoding="utf-8"),
    )
    page.close()

    assert result["sent"] == [{
        "sessionId": "AS-current",
        "content": "美國來源也要查",
        "context": {"target_type": "branch", "branch_id": "BR-us", "path": "研究 ＞ 美國來源"},
    }]
    assert "取得行情" in result["semanticText"]
    assert "wf-secret" not in result["semanticText"]
    assert "wf-secret" not in result["unauthorizedTechnicalText"]
    assert "Technical View" not in result["unauthorizedTechnicalText"]
    assert "wf-secret" not in result["technicalText"]
    assert '"workflow_configured": true' in result["technicalText"]
    assert '"external_credentials_configured": true' in result["technicalText"]


def test_fast_natural_follow_up_waits_for_the_new_run_receipt(browser):
    page = browser.new_page(viewport={"width": 900, "height": 700})
    page.set_content('<select id="agentAutonomySelect"><option value="advisory" selected>advisory</option></select><div id="agentRunStatus"></div>')
    result = page.evaluate(
        """
        async controllerSource => {
          let state = {
            active_session_id:'AS-old', active_run_id:'AR-old', dock_open:false,
            runs:{'AR-old':{run_id:'AR-old',session_id:'AS-old',status:'completed'}},
            last_sequence_by_run:{}, agentSettings:{default_driver:'openai-compatible'}
          };
          const calls = [];
          let resolveRun;
          window.AgentDockStore = {
            getState:()=>state,
            set:patch=>{state={...state,...patch};},
            dispatch:()=>{}, applySnapshot:()=>{}
          };
          window.AgentEventReducer = {isTerminalRun:run => run?.status === 'completed'};
          window.AgentStreamController = class {
            async connect(runId) { this.runId = runId; return {}; }
            stop() {}
          };
          window.AgentSessionController = class {
            async ensure() { return 'AS-new'; }
            async select(sessionId) { state={...state,active_session_id:sessionId}; return {session_id:sessionId}; }
            async sendMessage(sessionId, content) { calls.push({kind:'message',sessionId,content}); return {accepted:true}; }
          };
          window.AgentComposerContext = {payload:()=>null};
          window.AgentTaskTreeView = {render(){}};
          window.AgentPlanView = {render(){}};
          window.AgentContextBar = {render(){}};
          window.AgentConversationView = {render(){}};
          window.AgentArtifactView = {render(){}};
          window.AgentControlBar = {render(){}};
          window.AgentComposer = {init(){}};
          window.AgentRuntimeApi = (path, options={}) => {
            if (path.includes('/runs') && options.method === 'POST') {
              calls.push({kind:'run',body:JSON.parse(options.body)});
              return new Promise(resolve => { resolveRun = resolve; });
            }
            if (path.includes('/snapshot')) return Promise.resolve({events:[],run:state.runs['AR-new']});
            if (path.includes('/observability')) return Promise.resolve({});
            return Promise.resolve({});
          };
          const script = document.createElement('script');
          script.textContent = controllerSource;
          document.head.append(script);
          const intent = {category:'market_information',scope:'instrument',use_selected_symbol:false,symbols:['2330.TW']};
          const first = AgentDockController.submit({objective:'分析台積電的技術與風險。',intent,context_scope:'instrument'});
          await Promise.resolve(); await Promise.resolve();
          const second = AgentDockController.submit({objective:'順便比較台新新光金的風險差異。'});
          await Promise.resolve();
          const callsBeforeReceipt = calls.map(item => item.kind);
          resolveRun({run_id:'AR-new',session_id:'AS-new',status:'running',autonomy:'advisory'});
          await Promise.all([first, second]);
          return {calls,callsBeforeReceipt,activeSession:state.active_session_id,activeRun:state.active_run_id};
        }
        """,
        arg=(AGENT / "agent-dock-controller.js").read_text(encoding="utf-8"),
    )
    page.close()

    assert result["callsBeforeReceipt"] == ["run"]
    assert result["calls"] == [
        {"kind": "run", "body": {
            "objective": "分析台積電的技術與風險。", "symbols": ["2330.TW"],
            "context_scope": "instrument", "intent": {
                "category": "market_information", "scope": "instrument",
                "use_selected_symbol": False, "symbols": ["2330.TW"],
            }, "driver": "openai-compatible", "autonomy": "advisory", "max_steps": 12,
            "idempotency_key": result["calls"][0]["body"]["idempotency_key"],
        }},
        {"kind": "message", "sessionId": "AS-new", "content": "順便比較台新新光金的風險差異。"},
    ]
    assert result["activeSession"] == "AS-new"
    assert result["activeRun"] == "AR-new"
