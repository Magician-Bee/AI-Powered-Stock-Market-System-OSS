(() => {
  const DEFAULT_CONTEXT = Object.freeze({
    schema_version: 'stock_ai.workspace_context.v2',
    revision: 1,
    route: { workspace: 'home', tab: 'overview', params: {} },
    selection: {
      entity_id: null,
      symbol: null,
      entity_kind: null,
      candidate_id: null,
      universe_id: 'all_taiwan_active',
      explicit_intent_symbols: [],
    },
    chart: {
      type: 'candles', timeframe: '1d', range: '1y', price_basis: 'unadjusted',
      indicators: ['MA5', 'MA20', 'MA60', 'VOLUME', 'MACD'],
      focus_mode: false, viewport: null,
    },
    comparison: { symbols: [] },
    portfolio: {
      account_id: 'paper-default', mode: 'paper',
      selected_position_id: null, order_draft_id: null,
    },
    research: { strategy_id: null, report_id: null },
    agent: {
      session_id: null, run_id: null, dock_open: true,
      dock_tab: 'chat', dock_width: 360, maximized: false,
    },
    data: {
      market_snapshot_id: null, as_of: null, freshness: {}, quality: null,
      market_session: null, latest_quote: null,
    },
    layout: { sidebar_collapsed: false, density: 'comfortable' },
  });

  const subscribers = new Set();
  let value = structuredCopy(DEFAULT_CONTEXT);
  let persistTimer = 0;
  let localRevision = 0;

  function structuredCopy(input) {
    return typeof structuredClone === 'function'
      ? structuredClone(input)
      : JSON.parse(JSON.stringify(input));
  }

  function merge(base, patch) {
    if (!patch || typeof patch !== 'object' || Array.isArray(patch)) return patch;
    const result = { ...(base || {}) };
    Object.entries(patch).forEach(([key, next]) => {
      result[key] = next && typeof next === 'object' && !Array.isArray(next)
        ? merge(result[key], next)
        : next;
    });
    return result;
  }

  function uniqueSymbols(symbols) {
    return [...new Set((Array.isArray(symbols) ? symbols : [])
      .map(symbol => String(symbol || '').trim().toUpperCase()).filter(Boolean))].slice(0, 6);
  }

  function migrate(input = {}) {
    const payload = structuredCopy(input || {});
    if (payload.schema_version === 'stock_ai.workspace_context.v2') return payload;
    return {
      schema_version: 'stock_ai.workspace_context.v2',
      revision: Number(payload.revision) || 1,
      route: {
        workspace: payload.currentWorkspace || 'home',
        tab: payload.activeWorkspaceTab || 'overview',
        params: {},
      },
      selection: {
        entity_id: payload.selectedEntityId || null,
        symbol: payload.selectedSymbol || null,
        entity_kind: null,
        candidate_id: payload.selectedCandidateId || null,
        universe_id: payload.selectedUniverse || 'all_taiwan_active',
        explicit_intent_symbols: [],
      },
      chart: {
        type: 'candles',
        timeframe: payload.timeframe || '1d',
        range: payload.dateRange || '1y',
        price_basis: payload.priceBasis || 'unadjusted',
        indicators: payload.indicators || ['MA5', 'MA20', 'MA60', 'VOLUME', 'MACD'],
        focus_mode: false,
        viewport: null,
      },
      comparison: { symbols: payload.comparisonSymbols || [] },
      portfolio: {
        account_id: payload.activePortfolio || 'paper-default', mode: 'paper',
        selected_position_id: null, order_draft_id: null,
      },
      research: { strategy_id: payload.activeStrategy || null, report_id: null },
      agent: {
        session_id: payload.agentSessionId || null,
        run_id: payload.agentRunId || null,
        dock_open: payload.layoutState?.agentDockOpen !== false,
        dock_tab: 'chat', dock_width: Number(payload.layoutState?.agentDockWidth) || 360,
        maximized: false,
      },
      data: {
        market_snapshot_id: payload.marketSnapshotId || null,
        as_of: null,
        freshness: payload.dataFreshness || {},
        quality: null,
        market_session: payload.marketSession || null,
        latest_quote: payload.latestQuote || null,
      },
      layout: {
        sidebar_collapsed: payload.layoutState?.sidebar_collapsed === true,
        density: payload.layoutState?.density || 'comfortable',
      },
    };
  }

  function normalize(input) {
    const next = merge(structuredCopy(DEFAULT_CONTEXT), migrate(input));
    next.schema_version = 'stock_ai.workspace_context.v2';
    next.revision = Math.max(1, Number(next.revision) || 1);
    next.selection.symbol = String(next.selection.symbol || '').trim().toUpperCase() || null;
    next.selection.explicit_intent_symbols = uniqueSymbols(next.selection.explicit_intent_symbols);
    next.comparison.symbols = uniqueSymbols(next.comparison.symbols);
    next.agent.dock_width = Math.min(520, Math.max(320, Number(next.agent.dock_width) || 360));
    return next;
  }

  function snapshot() { return structuredCopy(value); }

  function notify(reason = 'update') {
    const next = snapshot();
    subscribers.forEach(listener => {
      try { listener(next, reason); } catch (error) { console.error('Workspace context subscriber failed', error); }
    });
    window.dispatchEvent(new CustomEvent('stock-ai:workspace-context', { detail: { context: next, reason } }));
  }

  async function persist() {
    window.clearTimeout(persistTimer);
    const requestRevision = localRevision;
    const payload = snapshot();
    try {
      const response = await api('/api/workspace/context', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (requestRevision !== localRevision) return;
      value = normalize(response || payload);
      notify('persisted');
    } catch (error) {
      console.error('Workspace context persistence failed', error);
    }
  }

  function schedulePersist() {
    window.clearTimeout(persistTimer);
    persistTimer = window.setTimeout(persist, 180);
  }

  function set(next, { persist: shouldPersist = true, reason = 'update' } = {}) {
    localRevision += 1;
    value = normalize(merge(value, next || {}));
    value.revision = Math.max(value.revision + 1, localRevision + 1);
    notify(reason);
    if (shouldPersist) schedulePersist();
    return snapshot();
  }

  async function hydrate(initial = null) {
    const payload = initial || await api('/api/workspace/context');
    localRevision += 1;
    value = normalize(payload || {});
    notify('hydrate');
    return snapshot();
  }

  function subscribe(listener) {
    subscribers.add(listener);
    listener(snapshot(), 'subscribe');
    return () => subscribers.delete(listener);
  }

  window.WorkspaceContextStore = { get: snapshot, set, hydrate, subscribe, persist, defaults: () => structuredCopy(DEFAULT_CONTEXT) };
})();
