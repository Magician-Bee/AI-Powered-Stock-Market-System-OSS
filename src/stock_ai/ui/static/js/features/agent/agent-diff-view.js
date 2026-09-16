(function () {
  'use strict';

  function versionsForSelection(state) {
    const artifactId = state.active_selection?.artifact_id;
    if (!artifactId) return [];
    return Object.values(state.artifact_versions || {})
      .filter(item => String(item.artifact_id || '') === String(artifactId))
      .sort((left, right) => Number(left.version || 0) - Number(right.version || 0));
  }

  function render(container, state) {
    if (!container) return;
    container.replaceChildren();
    const versions = versionsForSelection(state);
    if (versions.length < 2) {
      container.hidden = true;
      return;
    }
    container.hidden = false;
    const pinnedVersion = Number(state.active_selection?.artifact_version || 0);
    const pinned = versions.find(item => Number(item.version || 0) === pinnedVersion);
    const latest = versions.at(-1);
    const current = pinned || latest;
    const previous = current === latest
      ? versions.at(-2)
      : latest;
    const card = document.createElement('section');
    card.className = 'agent-diff-view';
    const heading = document.createElement('strong');
    heading.textContent = current === latest
      ? `修改比較 · v${previous.version} → v${current.version}`
      : `版本比較 · 選取 v${current.version} ↔ 最新 v${previous.version}`;
    const columns = document.createElement('div');
    columns.className = 'agent-diff-columns';
    [previous, current].forEach(version => {
      const column = document.createElement('article');
      const label = document.createElement('b');
      const body = document.createElement('p');
      label.textContent = `v${version.version} · ${version.changed_by || 'Agent'}`;
      body.textContent = version.summary || version.reason || version.validation_result || '無摘要';
      column.append(label, body);
      const selectVersion = () => window.AgentArtifactSelection?.select?.({
        ...state.active_selection,
        artifact_id: version.artifact_id,
        artifact_version: Number(version.version),
        target_type: 'artifact',
        path: state.active_selection?.path || `Artifacts ＞ ${version.artifact_id}`,
      });
      column.addEventListener('click', selectVersion);
      column.addEventListener('keydown', event => {
        if (!['Enter', ' '].includes(event.key)) return;
        event.preventDefault();
        selectVersion();
      });
      column.tabIndex = 0;
      column.setAttribute('role', 'button');
      column.setAttribute('aria-label', `選取 Artifact v${version.version}`);
      columns.append(column);
    });
    const actions = document.createElement('div');
    actions.className = 'agent-artifact-version-actions';
    [['undo', 'Undo'], ['compare', 'Compare'], ['restore', `Restore v${current.version}`]].forEach(([action, label]) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = label;
      button.addEventListener('click', () => window.AgentDockController?.proposeArtifactChange?.(
        latest.artifact_id, {
          action,
          target_version: Number(current.version),
          expected_version: Number(latest.version),
        },
      ));
      actions.append(button);
    });
    card.append(heading, columns, actions);
    container.append(card);
  }

  window.AgentDiffView = { render, versionsForSelection };
}());
