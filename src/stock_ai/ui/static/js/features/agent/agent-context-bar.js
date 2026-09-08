(function () {
  'use strict';
  function render(container, state) {
    if (!container) return;
    const snapshot = state.environment_snapshot || {};
    const payload = snapshot.payload || snapshot;
    // Completed Runs intentionally leave active_run_id empty so the Dock does
    // not look like it is still executing. Keep their actual task contract in
    // the Context Bar while that Session remains selected.
    const foregroundRun = Object.values(state.runs || {})
      .filter(item => item?.session_id === state.active_session_id)
      .sort((left, right) => String(right.updated_at || right.completed_at || '')
        .localeCompare(String(left.updated_at || left.completed_at || '')))[0] || {};
    const run = state.runs[state.active_run_id] || foregroundRun;
    const workspaceContext = window.WorkspaceContextStore?.get?.() || {};
    const visibleWorkspace = workspaceContext.route?.workspace || 'home';
    const visibleTab = workspaceContext.route?.tab || 'overview';
    const selectedSymbol = workspaceContext.selection?.symbol || '';
    const explicitSymbols = workspaceContext.selection?.explicit_intent_symbols || [];
    // The stock shown on screen and the stock explicitly assigned to a task
    // are separate contracts. Merely viewing a stock must never silently turn
    // a market-wide question into a single-stock Agent task.
    const intent = run.request?.metadata?.intent || {};
    const marketScopedRun = run.request?.metadata?.context_scope === 'market';
    const runSymbols = payload.symbols || run.symbols || run.request?.symbols || [];
    const taskSymbols = runSymbols.length ? runSymbols : explicitSymbols;
    container.replaceChildren();
    const chips = [
      ['目前頁面', visibleWorkspace],
      ['目前分頁', visibleTab],
      ['畫面股票', selectedSymbol || '未指定'],
      ['任務股票', taskSymbols.join('、') || (marketScopedRun ? '未指定（中立市場）' : '未指定')],
      ['問題類型', intent.title || (run.request?.metadata?.context_scope === 'market' ? '全市場分析' : '辨識中')],
      ['自主權', run.autonomy || run.request?.autonomy || 'advisory'],
      ['Snapshot', snapshot.snapshot_id || payload.snapshot_id || '新 Run 時建立'],
    ];
    chips.forEach(([label, value]) => {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'agent-context-chip';
      chip.textContent = `${label}：${value}`;
      chip.title = label === 'Snapshot' ? JSON.stringify(window.AgentDockFormatters.redact(snapshot), null, 2) : '';
      if (label === '畫面股票' && value !== '未指定') {
        chip.addEventListener('click', () => {
  if (typeof window.setView === 'function') window.setView('instrument');
        });
      }
      container.append(chip);
    });
    if (snapshot.expired_at) {
      const stale = document.createElement('span');
      stale.className = 'agent-context-stale';
      stale.textContent = '資料已過期';
      container.append(stale);
    }
  }
  let lastContainer = null;
  let lastState = null;
  const originalRender = render;
  function renderAndRemember(container, state) {
    lastContainer = container || lastContainer;
    lastState = state || lastState;
    originalRender(lastContainer, lastState || { runs: {}, active_run_id: null });
  }
  window.addEventListener('stock-ai:workspace-context', () => {
    if (lastContainer) renderAndRemember(lastContainer, lastState);
    window.AgentConversationView?.refreshIdle?.();
  });
  window.AgentContextBar = { render: renderAndRemember };
}());
