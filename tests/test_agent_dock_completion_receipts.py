"""A verified final can close a run without erasing its recovered failure history."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess

import pytest


AGENT = Path(__file__).resolve().parents[1] / "src/stock_ai/ui/static/js/features/agent"


def final_receipt():
    return {
        "status": "completed", "recovery_pending": False,
        "pending_recovery_node_ids": [], "goal_completion_gaps": [],
        "completion_validation": {
            "schema_version": "open_stock_ai.validation_result.v1",
            "validator": "completion_evaluator", "passed": True,
            "checks": [
                {"name": "failed_plan_nodes_are_nonblocking", "passed": True,
                 "failed_nodes": ["failed-activation"], "recovered_failed_nodes": ["failed-activation"],
                 "unresolved_failed_nodes": [], "recovery_coverage": {"failed-activation": ["activation-retry"]}},
                {"name": "plan_nodes_finished", "passed": True, "unfinished": []},
            ],
        },
    }


def project(final, *, cold=False):
    script = f"""
global.window = global;
global.localStorage = {{getItem:()=>null,setItem:()=>undefined}};
global.requestAnimationFrame = callback => callback();
require({json.dumps(str(AGENT / 'agent-event-reducer.js'))});
require({json.dumps(str(AGENT / 'agent-dock-store.js'))});
const final = {json.dumps(final)};
const base = {{run_id:'AR-recovered',session_id:'AS-recovered'}};
const link = {{failed_node_id:'failed-activation',recovery_call_id:'activation-retry',recovery_tool:'autonomy.activate'}};
const events = [
  {{type:'step.failed',node_id:'failed-activation',payload:{{node_id:'failed-activation',status:'failed'}}}},
  {{type:'tool.failed',tool_call_id:'original-call',payload:{{call_id:'original-call',node_id:'failed-activation',tool:'autonomy.activate',error:{{message:'scope rejected'}}}}}},
  {{type:'recovery.linked',node_id:'activation-retry',payload:{{node_id:'activation-retry',recovery_for:[link]}}}},
  {{type:'run.completed',payload:{{status:'completed',recovery_pending:false,pending_recovery_node_ids:[]}}}},
  {{type:'result.final',payload:final}},
].map((event,index)=>({{...base,...event,event_id:`E-${{index}}`,sequence:index+1}}));
events.forEach(event=>AgentDockStore.dispatch(event));
const streamed = AgentDockStore.getState();
if ({str(cold).lower()}) {{
  // hydrateRun replays events first, then applies the authoritative snapshot.
  const snapshot = {{run:{{...base,plan_id:'AP-current',status:'completed',terminal:true,result:final}},final_result:final,
    current_plan:{{plan_id:'AP-current',nodes:Object.values(streamed.steps)}},
    steps:Object.values(streamed.steps).map(step=>({{...step,metadata:undefined}})),tool_calls:Object.values(streamed.tool_calls),
    interactions:[],events,last_sequence:events.length}};
  AgentDockStore.applySnapshot(snapshot);
}}
const state = AgentDockStore.getState();
const run = state.runs[base.run_id];
process.stdout.write(JSON.stringify({{status:run.status,active:state.active_run_id,
  pending:run.recovery_pending ?? run.result?.recovery_pending,
  failedStep:state.steps['AR-recovered:failed-activation'].status,
  recoveredBy:state.steps['AR-recovered:failed-activation'].metadata.recovered_by,
  failedTool:state.tool_calls['AR-recovered:original-call'].status,
  terminal:AgentEventReducer.isTerminalRun(run)}}));
"""
    return json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True,
                                     text=True, timeout=5).stdout)


@pytest.mark.parametrize("cold", [False, True])
def test_verified_final_and_snapshot_keep_run_completed_and_failed_history(cold):
    actual = project(final_receipt(), cold=cold)
    assert actual["status"] == "completed" and actual["pending"] is False
    assert actual["active"] is None and actual["terminal"] is True
    assert actual["failedStep"] == actual["failedTool"] == "failed"
    assert actual["recoveredBy"] == [{"failed_node_id": "failed-activation", "recovery_call_id": "activation-retry",
                                      "recovery_tool": "autonomy.activate"}]


@pytest.mark.parametrize("plan_first", [False, True])
def test_plan_summary_prefers_canonical_node_over_stale_branch_mirror(plan_first):
    script = f"""
global.window = global;
require({json.dumps(str(AGENT / 'agent-plan-view.js'))});
const canonical = {{node_id:'obsolete-scope',run_id:'AR',title:'Scope verification',status:'skipped',order_index:{0 if plan_first else 99}}};
const staleBranch = {{node_id:'BST-old',run_id:'AR',title:'Scope verification',status:'running',order_index:{99 if plan_first else 0}}};
const state = {{active_run_id:null,active_session_id:'AS',runs:{{AR:{{run_id:'AR',session_id:'AS',plan_id:'AP',status:'completed'}}}},
  plans:{{AP:{{plan_id:'AP',run_id:'AR',nodes:[canonical]}}}},steps:{{canonical,staleBranch}}}};
const before = JSON.stringify(state);
const steps = AgentPlanView.orderedSteps(state);
process.stdout.write(JSON.stringify({{steps:steps.map(step=>({{id:step.node_id,status:step.status}})),unchanged:JSON.stringify(state)===before}}));
"""
    actual = json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True,
                                       text=True, timeout=5).stdout)
    assert actual == {"steps": [{"id": "obsolete-scope", "status": "skipped"}], "unchanged": True}


def test_completed_plan_summary_does_not_display_a_historical_running_cursor():
    script = f"""
global.window = global;
global.AgentDockFormatters = {{status:key=>({{key,label:key,icon:''}}),text:value=>String(value)}};
class Element {{
  constructor() {{this.children=[];this.dataset={{collapsed:'true'}};this.style={{setProperty(){{}}}};this.classList={{toggle(){{}}}};}}
  append(...children) {{this.children.push(...children);}}
  replaceChildren() {{this.children=[];}}
  setAttribute() {{}}
  addEventListener() {{}}
}}
global.document = {{createElement:()=>new Element()}};
require({json.dumps(str(AGENT / 'agent-plan-view.js'))});
const state = {{active_run_id:null,active_session_id:'AS',runs:{{AR:{{run_id:'AR',session_id:'AS',status:'completed'}}}},plans:{{}},
  steps:{{historical:{{node_id:'old-branch',run_id:'AR',title:'Obsolete investigation',status:'running'}}}}}};
const container = new Element();
AgentPlanView.render(container,state);
process.stdout.write(JSON.stringify({{summary:container.children[0].children[2].textContent,history:state.steps.historical.status}}));
"""
    actual = json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True,
                                       text=True, timeout=5).stdout)
    assert "已完成；保留執行與修復紀錄" in actual["summary"]
    assert "執行中" not in actual["summary"]
    assert actual["history"] == "running"


@pytest.mark.parametrize("contradiction", ["missing_validation", "failed_validation", "unresolved_failure",
                                            "unfinished_node", "failed_check", "pending_node", "goal_gap", "recovery_pending"])
def test_a_completed_label_cannot_override_unresolved_host_gaps(contradiction):
    receipt = deepcopy(final_receipt())
    checks = receipt["completion_validation"]["checks"]
    if contradiction == "missing_validation":
        del receipt["completion_validation"]
    elif contradiction == "failed_validation":
        receipt["completion_validation"]["passed"] = False
    elif contradiction == "unresolved_failure":
        checks[0]["unresolved_failed_nodes"] = ["failed-activation"]
    elif contradiction == "unfinished_node":
        checks[1]["unfinished"] = ["unfinished-task"]
    elif contradiction == "failed_check":
        checks.append({"name": "numeric_grounding", "passed": False})
    elif contradiction == "pending_node":
        receipt["pending_recovery_node_ids"] = ["failed-activation"]
    elif contradiction == "goal_gap":
        receipt["goal_completion_gaps"] = ["activation_not_verified"]
    else:
        receipt["recovery_pending"] = True
    actual = project(receipt)
    assert actual["status"] == ("recovery_pending" if contradiction == "recovery_pending" else "partially_completed")
    assert actual["failedStep"] == actual["failedTool"] == "failed"
