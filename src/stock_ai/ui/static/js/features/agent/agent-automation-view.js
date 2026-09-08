(function () {
  'use strict';

  function automationsForSession(state) {
    return Object.values(state.automations || {})
      .filter(item => item.state !== 'archived' && item.status !== 'archived')
      .filter(item => !state.active_session_id || item.session_id === state.active_session_id);
  }

  function semanticNodes(automation) {
    // AutomationController persists the user-safe compiler artifact as
    // ``steps``.  Prefer an explicit semantic graph when present, but never
    // leave the user with an empty card merely because an execution backend or
    // the semantic workflow is still represented by its ordered artifact.
    return automation.semantic_nodes
      || automation.nodes
      || automation.workflow?.nodes
      || automation.artifact?.steps
      || automation.artifact_preview?.steps
      || automation.steps
      || [];
  }

  function safeTechnicalFields(automation) {
    const raw = automation.technical || {};
    return {
      backend: raw.backend || automation.backend || null,
      execution_status: raw.execution_status || automation.status || null,
      workflow_configured: Boolean(raw.workflow_id || automation.workflow_id),
      external_credentials_configured: Boolean(raw.credential_ref || automation.credential_ref),
      // Credential refs, provider tokens, URLs and raw workflow payloads are
      // deliberately excluded: Technical View is diagnostics, not a secret
      // disclosure mechanism.
    };
  }

  function render(container, state, options = {}) {
    if (!container) return;
    container.replaceChildren();
    const automations = automationsForSession(state);
    if (!automations.length) {
      container.hidden = true;
      return;
    }
    container.hidden = false;
    automations.forEach(automation => {
      const card = document.createElement('section');
      card.className = `agent-automation-view${options.compact ? ' is-compact' : ''}`;
      card.dataset.automationId = automation.automation_id;
      const header = document.createElement('header');
      const title = document.createElement('strong');
      title.textContent = automation.title || automation.goal || 'Automation';
      const status = document.createElement('span');
      status.textContent = window.AgentDockFormatters.status(automation.status).label;
      const backend = String(
        automation.technical?.backend
        || automation.backend
        || automation.version?.compiled?.backend
        || 'internal_scheduler',
      );
      const host = document.createElement('small');
      host.className = 'agent-automation-host-status';
      host.textContent = `Host 執行層：${backend === 'n8n' ? 'n8n' : '內建排程'}`;
      header.append(title, status, host);
      const workflow = document.createElement('ol');
      workflow.className = 'agent-automation-semantic-flow';
      semanticNodes(automation).forEach((raw, index) => {
        const node = typeof raw === 'string' ? { label: raw } : raw;
        const item = document.createElement('li');
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = node.label || node.title || node.name || `步驟 ${index + 1}`;
        button.dataset.nodeId = node.node_id || node.id || String(index);
        button.addEventListener('click', () => window.AgentArtifactSelection?.select?.({
          artifact_id: automation.artifact_id || `automation:${automation.automation_id}`,
          artifact_version: Number(automation.artifact_version || automation.version || 1),
          target_type: 'automation_node',
          automation_id: automation.automation_id,
          branch_id: automation.branch_id,
          node_id: button.dataset.nodeId,
          path: `${title.textContent} ＞ ${button.textContent}`,
        }));
        item.append(button);
        workflow.append(item);
      });
      const hostStatus = String(automation.host_status || '').trim();
      if (hostStatus) {
        const statusNote = document.createElement('p');
        statusNote.className = 'agent-automation-host-note';
        statusNote.textContent = hostStatus;
        card.append(statusNote);
      }
      if (!workflow.childElementCount) {
        const empty = document.createElement('p');
        empty.className = 'agent-artifact-empty';
        empty.textContent = 'Automation 正在編譯，完成後會顯示語意流程。';
        card.append(header, empty);
      } else card.append(header, workflow);
      if (state.technical_view && state.debug_authorized === true && !options.compact) {
        const technical = document.createElement('details');
        technical.className = 'agent-automation-technical';
        const summary = document.createElement('summary');
        summary.textContent = 'Technical View';
        const code = document.createElement('pre');
        code.textContent = JSON.stringify(safeTechnicalFields(automation), null, 2);
        technical.append(summary, code);
        card.append(technical);
      }
      container.append(card);
    });
  }

  window.AgentAutomationView = { automationsForSession, render, semanticNodes, safeTechnicalFields };
}());
