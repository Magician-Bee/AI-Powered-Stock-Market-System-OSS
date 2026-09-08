(function () {
  'use strict';

  function render(container, state, options = {}) {
    if (!container) return;
    container.replaceChildren();
    const automation = document.createElement('div');
    automation.className = 'agent-artifact-canvas-automation';
    const evidence = document.createElement('div');
    evidence.className = 'agent-artifact-canvas-evidence';
    const diff = document.createElement('div');
    diff.className = 'agent-artifact-canvas-diff';
    const visualizations = document.createElement('div');
    visualizations.className = 'agent-artifact-canvas-visualizations';
    window.AgentAutomationView?.render?.(automation, state, options);
    window.AgentEvidenceGraph?.render?.(evidence, state, options);
    window.AgentSchemaVisualization?.render?.(visualizations, state, options);
    if (!options.compact) window.AgentDiffView?.render?.(diff, state);
    [automation, evidence, visualizations, diff].forEach(node => {
      if (!node.hidden && node.childElementCount) container.append(node);
    });
    container.hidden = container.childElementCount === 0;
  }

  window.AgentArtifactCanvas = { render };
}());
