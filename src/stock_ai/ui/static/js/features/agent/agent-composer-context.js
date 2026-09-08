(function () {
  'use strict';

  function render(container, state) {
    if (!container) return;
    container.replaceChildren();
    const selection = window.AgentArtifactSelection?.normalize?.(state.active_selection, state);
    if (!selection) {
      const idle = document.createElement('span');
      idle.className = 'agent-composer-context-idle';
      idle.textContent = '目前頁面 Context';
      container.append(idle);
      return;
    }
    const chip = document.createElement('span');
    chip.className = 'agent-composer-context-chip';
    chip.dataset.targetType = selection.target_type;
    chip.dataset.artifactId = selection.artifact_id || '';
    chip.dataset.artifactVersion = selection.artifact_version || '';
    chip.dataset.path = selection.path;
    const mutableTargets = new Set([
      'artifact', 'automation_node', 'chart_region', 'condition', 'schedule', 'visualization_node',
    ]);
    const kind = document.createElement('small');
    const label = document.createElement('strong');
    kind.className = 'agent-composer-context-kind';
    kind.textContent = mutableTargets.has(selection.target_type) ? '正在修改：' : '正在討論：';
    label.className = 'agent-composer-context-path';
    label.textContent = selection.path;
    label.title = selection.path;
    chip.append(kind, label);
    if (selection.artifact_version) {
      const version = document.createElement('small');
      version.className = 'agent-composer-context-version';
      version.textContent = `v${selection.artifact_version}`;
      chip.append(version);
      const latest = window.AgentArtifactSelection.latestVersion(state, selection.artifact_id);
      if (latest > selection.artifact_version) {
        const conflict = document.createElement('strong');
        conflict.className = 'agent-selection-conflict';
        conflict.textContent = `目前已更新到 v${latest}，送出時會先檢查衝突`;
        chip.append(conflict);
      }
    }
    const clear = document.createElement('button');
    clear.type = 'button';
    clear.className = 'agent-composer-context-clear';
    clear.setAttribute('aria-label', '清除目前選取內容');
    clear.textContent = '×';
    clear.addEventListener('click', () => window.AgentArtifactSelection.clear());
    chip.append(clear);
    container.append(chip);
  }

  function payload(state) {
    return window.AgentArtifactSelection?.messageContext?.(state) || null;
  }

  window.AgentComposerContext = { payload, render };
}());
