(function () {
  'use strict';

  function render(container, state) {
    window.AgentTaskTreeView.render(container, state);
    container?.classList.add('agent-task-forest');
  }

  window.AgentTaskForest = { render };
}());
