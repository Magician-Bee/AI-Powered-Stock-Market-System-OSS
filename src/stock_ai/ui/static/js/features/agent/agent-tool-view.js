(function () {
  'use strict';
  const F = () => window.AgentDockFormatters;

  function detail(label, value) {
    const node = document.createElement('section');
    const heading = document.createElement('h4');
    const pre = document.createElement('pre');
    heading.textContent = label;
    pre.textContent = F().text(value, '—');
    node.append(heading, pre);
    return node;
  }

  function create(tool) {
    const card = document.createElement('article');
    const status = F().status(tool.status);
    card.className = `agent-tool-card agent-operation-card is-${status.key}`;
    card.dataset.toolCallId = tool.tool_call_id;
    const head = document.createElement('header');
    const title = document.createElement('strong');
    const state = document.createElement('span');
    title.textContent = `🔧 ${tool.tool_name || tool.tool || '工具'}`;
    state.textContent = `${status.icon} ${status.label}${tool.duration_ms ? ` · ${F().duration(tool.duration_ms)}` : ''}`;
    head.append(title, state);
    const meta = document.createElement('small');
    meta.textContent = `Step ${tool.step_id || '—'} · Call ${tool.tool_call_id || '—'}${tool.retry_count ? ` · 重試 ${tool.retry_count}` : ''}`;
    card.append(head, meta);

    const disclosure = document.createElement('details');
    const toggle = document.createElement('summary');
    const body = document.createElement('div');
    toggle.textContent = '顯示完整資訊';
    disclosure.className = 'agent-operation-disclosure';
    body.className = 'agent-operation-body';

    const evidence = tool.evidence_ids || tool.validation?.evidence_ids || [];
    if (evidence.length) {
      body.append(detail('Evidence', evidence));
    }
    body.append(detail('輸入參數', tool.arguments_redacted || tool.arguments || {}));
    if (tool.result_summary || tool.result) body.append(detail('完整結果', tool.result_summary || tool.result));
    if (tool.validation) body.append(detail('驗證結果', tool.validation));
    if (tool.error) body.append(detail('錯誤', tool.error));
    disclosure.append(toggle, body);
    card.append(disclosure);
    return card;
  }

  window.AgentToolView = { create };
}());
