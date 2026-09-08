(function () {
  'use strict';
  const F = () => window.AgentDockFormatters;

  function activeRunId(state) {
    if (state.active_run_id && state.runs[state.active_run_id]) {
      return state.active_run_id;
    }
    return Object.values(state.runs)
      .filter(run => !state.active_session_id || run.session_id === state.active_session_id)
      .sort((a, b) => String(
        b.updated_at || b.completed_at || b.created_at || '',
      ).localeCompare(String(
        a.updated_at || a.completed_at || a.created_at || '',
      )))[0]?.run_id || null;
  }

  function orderedSteps(state) {
    const active = activeRunId(state);
    if (!active) return [];
    const rawSteps = Object.values(state.steps)
      .filter(step => step.run_id === active)
      .sort((a, b) => Number(a.order_index || 0) - Number(b.order_index || 0));
    // Lifecycle events and PlanGraph nodes can describe the same completed
    // tool with different durable IDs.  The pinned progress panel is a
    // concise execution summary, so render one row per named task while
    // retaining the richer result/capability fields from either record.
    const bySemanticKey = new Map();
    rawSteps.forEach(step => {
      const title = String(step.title || '').trim();
      const key = title ? `title:${title}` : `node:${String(step.node_id || step.step_id || '')}`;
      const prior = bySemanticKey.get(key);
      if (!prior) {
        bySemanticKey.set(key, step);
        return;
      }
      bySemanticKey.set(key, {
        ...prior,
        ...step,
        title: prior.title || step.title,
        capability: step.capability || prior.capability,
        assigned_agent: step.assigned_agent || prior.assigned_agent,
        result_summary: step.result_summary || prior.result_summary,
        remaining_gaps: step.remaining_gaps || prior.remaining_gaps,
        next_step: step.next_step || prior.next_step,
        metadata: { ...(prior.metadata || {}), ...(step.metadata || {}) },
      });
    });
    return [...bySemanticKey.values()].sort((a, b) => (
      Number(a.order_index || 0) - Number(b.order_index || 0)
      || String(a.node_id || a.step_id || '').localeCompare(String(b.node_id || b.step_id || ''))
    ));
  }
  function createRow(step, depth = 0) {
    const row = document.createElement('li');
    const status = F().status(step.status);
    row.className = `agent-plan-step is-${status.key}`;
    row.style.setProperty('--agent-step-depth', depth);
    const icon = document.createElement('span');
    const body = document.createElement('span');
    const title = document.createElement('strong');
    const meta = document.createElement('small');
    icon.textContent = status.icon;
    icon.setAttribute('aria-label', status.label);
    title.textContent = step.title || step.node_id;
    const result = F().text(step.result_summary || '').replace(/\s+/g, ' ').trim();
    const gaps = step.remaining_gaps || step.metadata?.remaining_gaps || [];
    const next = step.next_step || step.metadata?.next_step || '';
    const progress = [
      result ? `結果：${result}` : '',
      gaps.length ? `仍缺：${gaps.join('、')}` : '',
      next ? `下一步：${next}` : '',
    ].filter(Boolean).join(' · ');
    meta.textContent = [
      status.label,
      step.assigned_agent ? `Agent：${step.assigned_agent}` : '',
      step.capability || '',
      progress,
    ].filter(Boolean).join(' · ');
    if (progress) meta.title = progress;
    body.append(title, meta);
    row.append(icon, body);
    return row;
  }
  function render(container, state) {
    if (!container) return;
    const steps = orderedSteps(state);
    const completed = steps.filter(step => step.status === 'completed').length;
    const runId = activeRunId(state);
    const plan = runId
      ? Object.values(state.plans).find(item => item.plan_id === state.runs[runId]?.plan_id)
      : null;
    const runStatus = runId ? String(state.runs[runId]?.status || '') : '';
    const isRunning = ['queued', 'running', 'reconnecting', 'recovery_pending', 'repairing'].includes(runStatus);
    const completionLabel = runStatus === 'partially_completed'
      ? `${completed} / ${steps.length} 完成 · 有失敗分支，等待修復`
      : runStatus === 'max_steps_reached'
      ? `${completed} / ${steps.length} 完成 · 已達上限，尚未完成`
      : ['recovery_pending', 'repairing'].includes(runStatus)
      ? `${completed} / ${steps.length} 完成 · Host 正在自動修復`
      : isRunning && steps.length > 0 && completed === steps.length
        ? `${completed} / ${steps.length} 步驟完成 · 正在驗收`
        : `${completed} / ${steps.length} 完成`;
    container.replaceChildren();
    const head = document.createElement('div');
    head.className = 'agent-plan-head';
    const title = document.createElement('strong');
    const progress = document.createElement('span');
    const toggle = document.createElement('button');
    const collapsed = container.dataset.collapsed === 'true';
    const currentStep = steps.find(step => step.status === 'running');
    const nextStep = steps.find(step => ['pending', 'ready', 'proposed'].includes(step.status));
    const collapsedStatus = currentStep
      ? `執行中：${currentStep.title || currentStep.node_id}`
      : ['recovery_pending', 'repairing'].includes(runStatus)
        ? '已保留完成工作；Host 正在從 Checkpoint 自動修復'
        : runStatus === 'partially_completed'
        ? '有分支失敗；已保留完成工作，請繼續修復'
        : runStatus === 'max_steps_reached'
        ? '已達步驟上限，等待繼續或重新規劃'
        : completed === steps.length && steps.length
        ? '已全部完成'
        : nextStep
          ? `等待：${nextStep.title || nextStep.node_id}`
          : isRunning && steps.length
            ? '執行中：AI 正在規劃下一項任務'
            : isRunning
              ? '執行中：建立執行計畫'
              : '等待建立任務';
    const collapsedSummary = `${completed} / ${steps.length} 完成 · ${collapsedStatus}`;
    title.textContent = '執行計畫';
    progress.textContent = `${completionLabel} · Revision ${plan?.revision_number || plan?.revision || 1}`;
    toggle.type = 'button';
    toggle.className = 'agent-plan-toggle';
    toggle.textContent = collapsed ? collapsedSummary : '收合計畫';
    toggle.title = collapsed ? '展開完整執行計畫' : '收合為進度摘要';
    toggle.setAttribute('aria-expanded', String(!collapsed));
    head.append(title, progress, toggle);
    const list = document.createElement('ol');
    steps.forEach(step => list.append(createRow(step)));
    list.hidden = collapsed;
    container.classList.toggle('is-collapsed', collapsed);
    toggle.addEventListener('click', () => {
      const next = container.dataset.collapsed !== 'true';
      container.dataset.collapsed = String(next);
      container.classList.toggle('is-collapsed', next);
      list.hidden = next;
      toggle.textContent = next ? collapsedSummary : '收合計畫';
      toggle.title = next ? '展開完整執行計畫' : '收合為進度摘要';
      toggle.setAttribute('aria-expanded', String(!next));
    });
    if (!steps.length) {
      const empty = document.createElement('li');
      empty.className = 'agent-empty-state';
      empty.textContent = 'Run 建立後，Host 編譯的動態 Plan 會顯示在這裡。';
      list.append(empty);
    }
    container.append(head, list);
  }
  window.AgentPlanView = { activeRunId, orderedSteps, render };
}());
