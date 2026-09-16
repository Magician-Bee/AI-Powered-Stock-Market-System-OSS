(function () {
  'use strict';

  const HIDDEN_PREFIXES = ['assistant.message.', 'message.'];
  const SEMANTIC_TYPES = new Set([
    'objective.created', 'objective.revised', 'forest.created', 'branch.created',
    'branch.started', 'branch.paused', 'branch.resumed', 'branch.completed', 'branch.failed',
    'branch.repaired', 'research.started', 'research.source_found', 'research.source_opened',
    'research.evidence_added', 'research.conflict_detected', 'reflection.started',
    'reflection.completed', 'interaction.awaiting_user', 'proposal.evaluated',
    'repair.started', 'repair.strategy_changed', 'repair.completed', 'result.final',
    'provider.untrusted_context.bound',
    'model.session.configured',
  ]);

  function shouldShow(event) {
    const type = String(event.type || '');
    if (HIDDEN_PREFIXES.some(prefix => type.startsWith(prefix))) return false;
    return SEMANTIC_TYPES.has(type)
      || ['run.', 'step.', 'tool.', 'automation.', 'artifact.'].some(prefix => type.startsWith(prefix));
  }

  function semanticSummary(event) {
    const payload = event.payload || {};
    if (event.type === 'model.session.configured') {
      return `本次模型：${payload.model || 'SDK 未回報'} · 推理：${payload.reasoning_effort || 'SDK 未回報'}`;
    }
    if (event.summary || payload.summary) return event.summary || payload.summary;
    const subject = payload.title || payload.name || payload.tool || payload.tool_name || '';
    const verb = {
      'forest.created': '已建立 Task Forest', 'branch.created': '已建立分支',
      'branch.started': '正在執行分支', 'branch.completed': '已完成分支',
      'branch.failed': '分支執行失敗', 'branch.repaired': '已修復分支',
      'interaction.awaiting_user': '等待你的決定', 'research.evidence_added': '已加入研究證據',
      'automation.compiling': '正在建立自動化流程', 'automation.validating': '正在驗證自動化',
      'automation.testing': '正在測試自動化', 'automation.active': '自動化已啟用',
      'tool.started': '正在使用工具', 'tool.completed': '工具已完成', 'tool.failed': '工具執行失敗',
      'model.provider.admission_wait': '模型連線節流等待中',
      'provider.untrusted_context.bound': '外部資料已以 data-only 邊界送入模型',
      'step.started': '正在執行任務', 'step.completed': '任務已完成', 'step.failed': '任務失敗',
      'result.final': '已產生最終結果',
    }[event.type] || String(event.type || '').replaceAll('.', ' ');
    return [verb, subject].filter(Boolean).join('：');
  }

  function render(container, state) {
    if (!container) return;
    container.replaceChildren();
    const events = (state.ordered_event_ids || [])
      .map(id => state.events?.[id])
      .filter(Boolean)
      .filter(event => !state.active_session_id || event.session_id === state.active_session_id)
      .filter(shouldShow)
      .slice(-40);
    if (!events.length) {
      container.hidden = true;
      return;
    }
    container.hidden = false;
    const heading = document.createElement('div');
    heading.className = 'agent-section-heading';
    heading.textContent = state.technical_view ? 'Runtime Timeline · Technical View' : '執行進度';
    const list = document.createElement('ol');
    list.className = 'agent-runtime-timeline-list';
    events.forEach(event => {
      const item = document.createElement('li');
      const status = window.AgentDockFormatters.status(event.status || event.payload?.status || event.type.split('.').pop());
      item.className = `is-${status.key}`;
      const summary = document.createElement('span');
      summary.textContent = `${status.icon} ${semanticSummary(event)}`;
      item.append(summary);
      if (state.technical_view) {
        const technical = document.createElement('code');
        technical.textContent = [event.type, event.branch_id, event.node_id || event.step_id, event.event_id]
          .filter(Boolean).join(' · ');
        item.append(technical);
      }
      list.append(item);
    });
    container.append(heading, list);
  }

  window.AgentRuntimeTimeline = { render, semanticSummary, shouldShow };
}());
