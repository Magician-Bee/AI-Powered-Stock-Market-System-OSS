(function () {
  'use strict';
  const F = () => window.AgentDockFormatters;

  function create(item) {
    const card = document.createElement('article');
    const kind = ['package', 'mcp'].includes(item.kind) ? item.kind : 'skill';
    card.className = `agent-skill-card agent-operation-card kind-${kind}`;
    const head = document.createElement('header');
    const title = document.createElement('strong');
    const meta = document.createElement('span');
    const disclosure = document.createElement('details');
    const toggle = document.createElement('summary');
    const pre = document.createElement('pre');
    const name = item.skill_id || item.package_id || item.mcp_server || item.id;
    title.textContent = `${item.kind === 'package' ? 'Package' : item.kind === 'mcp' ? 'MCP' : '技能'}：${name}`;
    meta.textContent = `${item.status || 'selected'} · ${item.tool || 'Host Capability Registry'}`;
    toggle.textContent = '顯示完整資訊';
    pre.textContent = F().text(item);
    head.append(title, meta);
    disclosure.className = 'agent-operation-disclosure';
    disclosure.append(toggle, pre);
    card.append(head, disclosure);
    return card;
  }
  window.AgentSkillView = { create };
}());
