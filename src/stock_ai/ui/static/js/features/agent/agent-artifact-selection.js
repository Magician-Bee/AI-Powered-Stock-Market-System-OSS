(function () {
  'use strict';

  function latestVersion(state, artifactId) {
    const versions = Object.values(state.artifact_versions || {})
      .filter(item => String(item.artifact_id || '') === String(artifactId || ''))
      .sort((left, right) => Number(right.version || 0) - Number(left.version || 0));
    const artifact = Object.values(state.artifacts || {})
      .find(item => String(item.artifact_id || '') === String(artifactId || ''));
    return Number(versions[0]?.version || artifact?.version || artifact?.artifact_version || 1);
  }

  function normalize(selection, state = window.AgentDockStore?.getState?.() || {}) {
    if (!selection) return null;
    const artifactId = selection.artifact_id || null;
    const artifactVersion = Number(
      selection.artifact_version || (artifactId ? latestVersion(state, artifactId) : 0),
    ) || null;
    const path = String(
      selection.path || selection.label || selection.node_id || selection.artifact_id || '目前選取內容',
    );
    return {
      selection_id: selection.selection_id || `ASEL-${Date.now()}-${Math.random().toString(16).slice(2)}`,
      artifact_id: artifactId,
      artifact_version: artifactVersion,
      target_type: selection.target_type || 'artifact',
      branch_id: selection.branch_id || null,
      node_id: selection.node_id || null,
      evidence_id: selection.evidence_id || null,
      automation_id: selection.automation_id || null,
      path,
      label: String(selection.label || path),
      selected_at: selection.selected_at || new Date().toISOString(),
    };
  }

  function select(selection) {
    const store = window.AgentDockStore;
    if (!store) return null;
    const normalized = normalize(selection, store.getState());
    store.set({
      active_selection: normalized,
      artifact_selections: {
        ...(store.getState().artifact_selections || {}),
        [normalized.selection_id]: normalized,
      },
    });
    return normalized;
  }

  function clear() {
    window.AgentDockStore?.set?.({ active_selection: null });
  }

  function messageContext(state) {
    const selection = normalize(state?.active_selection, state || {});
    if (!selection) return null;
    return {
      artifact_id: selection.artifact_id,
      artifact_version: selection.artifact_version,
      target_type: selection.target_type,
      branch_id: selection.branch_id,
      node_id: selection.node_id,
      evidence_id: selection.evidence_id,
      automation_id: selection.automation_id,
      path: selection.path,
      selected_at: selection.selected_at,
    };
  }

  window.AgentArtifactSelection = { clear, latestVersion, messageContext, normalize, select };
}());
