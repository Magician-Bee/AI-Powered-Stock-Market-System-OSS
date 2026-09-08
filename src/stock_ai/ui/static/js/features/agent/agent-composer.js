(function () {
  'use strict';
  function init() {
    const form = document.getElementById('agentComposerForm');
    const input = document.getElementById('agentComposerInput');
    if (!form || !input || form.dataset.bound === 'true') return;
    form.dataset.bound = 'true';
    form.addEventListener('submit', event => {
      event.preventDefault();
      const objective = input.value.trim();
      if (!objective) return;
      input.value = '';
      window.AgentDockController.submit({ objective, source: 'agent_dock' })
        .catch(error => {
          if (!input.value) input.value = objective;
          window.AgentDockController.showError(error);
        });
    });
    input.addEventListener('keydown', event => {
      const send = event.key === 'Enter' && !event.shiftKey;
      const shortcut = event.key === 'Enter' && (event.metaKey || event.ctrlKey);
      if (send || shortcut) {
        event.preventDefault();
        form.requestSubmit();
      }
    });
  }
  window.AgentComposer = { init };
}());
