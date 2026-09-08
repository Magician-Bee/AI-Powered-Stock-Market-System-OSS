(function () {
  'use strict';
  const controls = {
    queued: [['pause', '暫停'], ['cancel', '取消']],
    planning: [['pause', '暫停'], ['cancel', '取消']],
    running: [['pause', '暫停'], ['cancel', '取消']],
    recovery_pending: [['pause', '暫停自動修復'], ['cancel', '取消']],
    repairing: [['pause', '暫停自動修復'], ['cancel', '取消']],
    resuming: [['pause', '暫停'], ['cancel', '取消']],
    cancelling: [['cancel', '取消']],
    waiting_user_input: [['cancel', '取消']],
    waiting_decision: [['cancel', '取消']],
    waiting_approval: [['cancel', '取消']],
    paused: [['resume', '繼續'], ['cancel', '取消']],
    suspended: [['resume', '恢復'], ['cancel', '取消']],
    failed: [['retry', '重試失敗步驟'], ['replan', '重新規劃']],
    max_steps_reached: [
      ['continue', '繼續執行'],
      ['increase_limit', '增加步驟上限'],
      ['replan', '重新規劃'],
      ['new_run', '建立新 Run'],
    ],
    partially_completed: [
      ['continue', '繼續修復'],
      ['retry', '重試失敗步驟'],
      ['replan', '重新規劃失敗分支'],
      ['new_run', '建立新 Run'],
    ],
    completed: [['rerun', '再次執行']],
  };

  const EXECUTION_AUTONOMIES = new Set([
    'paper_execute', 'project_execute', 'external_execute', 'full_execute',
  ]);
  const MUTATING_RISK_CLASSES = new Set([
    'write', 'financial_paper', 'financial_live', 'external_write',
  ]);

  function hasMutatingToolCall(state, run) {
    return Object.values(state.tool_calls || {}).some(call => (
      call?.run_id === run?.run_id
      && (
        call.mutating === true
        || MUTATING_RISK_CLASSES.has(String(call.risk_class || ''))
        || String(call.tool_name || '').startsWith('paper.submit_order')
        || String(call.tool_name || '').startsWith('paper.mark_to_market')
      )
    ));
  }

  function completedControls(state, run) {
    // A Run-level idempotency key protects delivery retries of one request.
    // It intentionally does not make a brand-new Run's paper order a no-op.
    // Never offer a one-click replay when the completed Run had execution
    // authority or recorded a mutation: preserve its objective only as a
    // fresh advisory draft that the user must explicitly send again.
    const autonomy = String(run?.autonomy || run?.request?.autonomy || 'advisory');
    if (EXECUTION_AUTONOMIES.has(autonomy) || hasMutatingToolCall(state, run)) {
      return [['draft_new_goal', '建立新目標草稿']];
    }
    return controls.completed;
  }
  function resourceBoundaryControls(run) {
    return null;
  }
  function activeRunId(state) {
    if (state.active_run_id && state.runs[state.active_run_id]) {
      return state.active_run_id;
    }
    return Object.values(state.runs)
      .filter(run => !state.active_session_id || run.session_id === state.active_session_id)
      .sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')))[0]
      ?.run_id || null;
  }
  function render(container, state) {
    if (!container) return;
    const active = activeRunId(state);
    const run = state.runs[active] || {};
    const status = run.status === 'paused' ? 'paused' : run.status;
    container.replaceChildren();
    const availableControls = resourceBoundaryControls(run)
      || (status === 'completed'
      ? completedControls(state, run)
      : (controls[status] || []));
    availableControls.forEach(([action, label]) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = label;
      button.setAttribute('aria-label', `${label}目前 Agent Run`);
      button.addEventListener('click', () => window.AgentDockController.control(action, run.run_id));
      container.append(button);
    });
  }
  window.AgentControlBar = { activeRunId, completedControls, resourceBoundaryControls, hasMutatingToolCall, render };
}());
