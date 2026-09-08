(function () {
  'use strict';

  // Remote reasoning models can spend more than 45 seconds on the small
  // structured intent call even though the provider is healthy. Keep the UI
  // below the configured 240-second provider ceiling while leaving enough
  // time for those models to return a valid classification.
  const INTENT_CLASSIFICATION_TIMEOUT_MS = 180_000;
  const CLASSIFIED_SECURITY_ID = /^(?:\^?[A-Z][A-Z0-9.-]{0,11}|\d{4,6}(?:\.(?:TW|TWO))?)$/i;
  const EXPLICIT_PAPER_ORDER = /(?:模擬(?:交易|下單)?|紙上(?:交易|下單)?|paper(?:\s*(?:trade|order))?)[^\n]{0,48}(?:下單|買進|賣出|交易|order|buy|sell)|(?:下單|買進|賣出|交易|order|buy|sell)[^\n]{0,48}(?:模擬|紙上|paper)/i;

  const store = window.AgentDockStore;
  const stream = new window.AgentStreamController(store);
  const sessions = new window.AgentSessionController(store);
  let runtimeIdentity = { provider: 'Stock AI Runtime', model: '' };
  let runtimeIdentities = {};
  let initialized = false;
  let submissionQueue = Promise.resolve();

  document.addEventListener('stock-ai-active-agent-changed', event => {
    const detail = event.detail || {};
    const provider = detail.id || detail.provider;
    if (!provider) return;
    runtimeIdentity = { provider, model: detail.model || '' };
    runtimeIdentities[provider] = runtimeIdentity;
    if (initialized) render(store.getState());
  });

  function activeRun(state) {
    return state.runs[state.active_run_id]
      || Object.values(state.runs)
        .filter(run => !state.active_session_id || run.session_id === state.active_session_id)
        .sort((a, b) => String(
          b.updated_at || b.completed_at || b.created_at || '',
        ).localeCompare(String(
          a.updated_at || a.completed_at || a.created_at || '',
        )))[0]
      || {};
  }

  async function synchronizeSingleTaskSymbol(symbols, { onlyIfUnselected = false } = {}) {
    // A Run's accepted, resolved symbol is an explicit user task contract.
    // Keep it separate from merely viewing a market index, but make it the
    // active workspace selection once the Host has durably accepted that Run.
    // Without this hand-off the Dock can say "任務股票：2887.TW" while the
    // individual-stock workspace keeps rendering the old ^TWII benchmark.
    const normalized = [...new Set((Array.isArray(symbols) ? symbols : [])
      .map(value => String(value || '').trim().toUpperCase())
      .filter(value => CLASSIFIED_SECURITY_ID.test(value) && !value.startsWith('^') && value !== 'TX=F'))];
    if (normalized.length !== 1) return null;
    const symbol = normalized[0];
    const currentSymbol = String(window.WorkspaceContextStore?.get?.().selection?.symbol || '')
      .trim().toUpperCase();
    if (onlyIfUnselected && currentSymbol && !currentSymbol.startsWith('^') && currentSymbol !== 'TX=F') {
      return currentSymbol;
    }
    window.WorkspaceContextStore?.set?.({
      selection: { symbol, explicit_intent_symbols: [symbol] },
    }, { reason: 'agent-run:task-symbol' });
    // Context alone is not enough for the chart and trading workspaces: they
    // read state.symbol/currentEntity. Refresh that local projection without
    // changing the user's route or covering the Agent console.
    if (typeof window.loadSummary === 'function') {
      await window.loadSummary(symbol, { navigate: false });
    }
    return symbol;
  }

  function identityForRun(run) {
    const isActive = [
      'queued', 'running', 'recovery_pending', 'repairing', 'suspended',
      'waiting_user_input', 'waiting_decision', 'waiting_approval',
    ].includes(run.status);
    // A completed historical Codex run must not make the Dock claim that the
    // next task will use Codex after the user has activated an Ollama model.
    if (!isActive) return runtimeIdentity;
    const provider = run.driver || run.driver_id || run.provider || runtimeIdentity.provider;
    const configured = runtimeIdentities[provider] || runtimeIdentity;
    return {
      provider,
      model: run.model || run.provider_model || configured.model || '',
    };
  }

  function renderHeader(state) {
    const run = activeRun(state);
    const session = state.sessions[state.active_session_id] || {};
    const status = window.AgentDockFormatters.status(run.status || state.connection_state);
    const runIdentity = identityForRun(run);
    const identity = document.getElementById('agentDockIdentity');
    const connection = document.getElementById('agentDockConnection');
    const title = document.getElementById('agentDockSessionTitle');
    if (identity) identity.textContent = [runIdentity.model, runIdentity.provider].filter(Boolean).join(' · ');
    if (connection) connection.textContent = `${run.autonomy || 'Advisory'} · ${status.label}`;
    if (title) title.textContent = session.title || 'Stock AI Agent';
    const statusBar = document.getElementById('agentRunStatus');
    if (statusBar) {
      statusBar.textContent = run.run_id
        ? `${status.icon} ${status.label} · Run ${run.run_id} · sequence ${state.last_sequence_by_run[run.run_id] || run.last_sequence || 0}`
        : '○ 等待新任務';
      statusBar.className = `agent-run-status is-${status.key}`;
    }
    const technical = document.getElementById('agentTechnicalViewToggle');
    if (technical) {
      technical.textContent = state.technical_view ? '使用者檢視' : '技術檢視';
      technical.setAttribute('aria-pressed', String(state.technical_view));
      technical.classList.toggle('active', state.technical_view);
    }
  }

  function renderTabs(state) {
    document.querySelectorAll('[data-agent-dock-tab]').forEach(button => {
      const active = button.dataset.agentDockTab === state.active_tab;
      button.classList.toggle('active', active);
      button.setAttribute('aria-selected', String(active));
    });
    ['chat', 'tasks', 'artifacts'].forEach(tab => {
      const panel = document.getElementById(`agent${tab[0].toUpperCase()}${tab.slice(1)}Panel`);
      if (panel) panel.hidden = state.active_tab !== tab;
    });
  }

  function render(state) {
    const dock = document.getElementById('agentDock');
    if (!dock) return;
    dock.classList.toggle('is-open', state.dock_open);
    dock.style.setProperty('--agent-dock-width', `${state.dock_width}px`);
    document.documentElement.style.setProperty('--agent-dock-width', `${state.dock_width}px`);
    document.documentElement.classList.toggle('agent-dock-open', state.dock_open);
    document.documentElement.classList.toggle('agent-dock-closed', !state.dock_open);
    const consoleToggle = document.getElementById('agentConsoleNav');
    if (consoleToggle) {
      const label = state.dock_open ? '收合 Agent 控制台' : '開啟 Agent 控制台';
      consoleToggle.querySelector('strong')?.replaceChildren(label);
      consoleToggle.setAttribute('aria-expanded', String(state.dock_open));
      consoleToggle.setAttribute('aria-pressed', String(state.dock_open));
      consoleToggle.setAttribute('aria-label', label);
      consoleToggle.classList.toggle('active', state.dock_open);
    }
    renderHeader(state);
    renderTabs(state);
    window.AgentContextBar.render(document.getElementById('agentContextBar'), state);
    window.AgentPlanView.render(document.getElementById('agentPinnedPlan'), state);
    window.AgentConversationView.render(document.getElementById('agentChatFeed'), state);
    window.AgentObservabilityDashboard?.render?.(
      document.getElementById('agentObservabilityDashboard'), state,
    );
    window.AgentRuntimeTimeline?.render?.(document.getElementById('agentRuntimeTimeline'), state);
    window.AgentDecisionCard?.render?.(document.getElementById('agentDecisionCards'), state);
    window.AgentArtifactCanvas?.render?.(
      document.getElementById('agentInteractiveArtifacts'), state, { compact: true },
    );
    (window.AgentTaskForest || window.AgentTaskTreeView).render(document.getElementById('agentTasksTree'), state);
    window.AgentArtifactView.render(document.getElementById('agentArtifactsList'), state);
    window.AgentArtifactCanvas?.render?.(document.getElementById('agentArtifactCanvas'), state);
    window.AgentComposerContext?.render?.(document.getElementById('agentComposerContextChip'), state);
    window.AgentControlBar.render(document.getElementById('agentRunControls'), state);
  }

  async function hydrateRun(runId) {
    const snapshot = await window.AgentRuntimeApi(`/api/agents/runs/${encodeURIComponent(runId)}/snapshot`);
    if (Array.isArray(snapshot.events)) {
      const last = { ...store.getState().last_sequence_by_run, [runId]: 0 };
      store.set({ last_sequence_by_run: last });
      snapshot.events.forEach(event => store.dispatch(event));
    }
    store.applySnapshot(snapshot);
    await loadObservability();
    return snapshot;
  }

  async function loadObservability() {
    const dashboard = await window.AgentRuntimeApi('/api/agents/observability');
    store.set({ observability: dashboard });
    return dashboard;
  }

  async function followRun(runId) {
    store.set({ active_run_id: runId, dock_open: true });
    const terminal = await stream.connect(runId);
    if (stream.runId !== runId) return terminal?.result || terminal;
    const snapshot = await hydrateRun(runId);
    const result = snapshot?.final_result || terminal?.result || terminal;
    await prepareResourceBoundaryDraft(snapshot?.run, result);
    return result;
  }

  function resourceBoundaryDraft(run, result) {
    const draft = result?.next_goal_draft || run?.result?.next_goal_draft || run?.next_goal_draft;
    return draft && typeof draft === 'object' && String(draft.objective || '').trim()
      ? draft
      : null;
  }

  async function prepareGoalDraft(run, draft, { automatic = false } = {}) {
    const objective = String(draft?.objective || '').trim();
    if (!objective) return null;
    const reason = String(draft?.reason_code || 'host_resource_boundary');
    const draftSession = await sessions.create(draft?.title || '從 checkpoint 重建目標');
    const composer = document.getElementById('agentComposerInput');
    const mode = document.getElementById('agentComposerMode');
    const autonomy = document.getElementById('agentAutonomySelect');
    if (composer) {
      composer.value = objective;
      composer.dispatchEvent(new Event('input', { bubbles: true }));
      composer.focus();
    }
    if (mode) mode.value = 'new_run';
    if (autonomy) autonomy.value = 'advisory';
    store.set({
      active_session_id: draftSession.session_id,
      active_run_id: null,
      active_tab: 'chat',
      dock_open: true,
      connection_state: 'idle',
      rebuilt_goal_run_ids: {
        ...(store.getState().rebuilt_goal_run_ids || {}),
        ...(run?.run_id ? { [run.run_id]: true } : {}),
      },
    });
    const status = document.getElementById('agentRunStatus');
    if (status) {
      status.className = 'agent-run-status is-idle';
      status.textContent = automatic
        ? `○ 舊目標已安全停止：${reason}；已建立可安全接續的新目標草稿。`
        : '○ 已建立新目標草稿；舊 Run 不會被重新啟動。';
    }
    return { draft: true, objective, autonomy: 'advisory', session_id: draftSession.session_id };
  }

  async function prepareResourceBoundaryDraft(run, result) {
    // A Host resource limit is displayed in the completed Run receipt.  It is
    // not a request for the user to provide recovery instructions, so never
    // create another Session or silently prefill the composer here.
    return null;
  }

  // A first request can still be waiting for its Run receipt while the user
  // naturally writes a second sentence. Serialize only until that receipt
  // updates Dock state, so the second sentence cannot attach to a stale,
  // terminal Run from the previous task.
  function submit(input = {}) {
    let markAccepted;
    const accepted = new Promise(resolve => { markAccepted = resolve; });
    const started = submissionQueue.then(() => submitNow(input, { onAccepted: markAccepted }));
    submissionQueue = accepted;
    return started.catch(error => {
      markAccepted();
      throw error;
    });
  }

  async function submitNow(input = {}, { onAccepted = () => {} } = {}) {
    const objective = String(input.objective || '').trim();
    if (!objective) throw new Error('請先輸入要交給 Stock AI Agent 的任務。');
    const state = store.getState();
    const running = state.active_run_id
      && !window.AgentEventReducer?.isTerminalRun?.(state.runs[state.active_run_id]);
    // A person supplies the desired outcome, not a scheduler instruction.
    // While work is active, the Host receives the message in that Session and
    // classifies it as a local addition, correction, or independent branch.
    // Once idle, the same composer naturally starts a new Run.
    const mode = running ? 'current_run' : 'new_run';
    if (running && mode === 'current_run') {
      const sessionId = state.active_session_id;
      if (!sessionId) throw new Error('目前 Run 沒有可接收訊息的 Session。');
      const response = await sessions.sendMessage(
        sessionId,
        objective,
        window.AgentComposerContext?.payload?.(state) || null,
      );
      (response?.events || []).forEach(event => store.dispatch(event));
      if (response?.run?.run_id) store.set({
        active_run_id: response.run.run_id,
        runs: { ...store.getState().runs, [response.run.run_id]: response.run },
      });
      store.set({ dock_open: true });
      if (response?.follow_run_id) {
        const following = followRun(response.follow_run_id);
        onAccepted();
        return following;
      }
      onAccepted();
      return response;
    }
    const sessionId = await sessions.ensure();
    const workspaceContext = window.WorkspaceContextStore?.get?.();
    const selectedSymbol = workspaceContext?.selection?.symbol;
    const explicitSymbols = workspaceContext?.selection?.explicit_intent_symbols || [];
    const driver = input.driver || state.agentSettings?.default_driver || window.__stockAIActiveAgent?.id;
    const intent = await classifyPrompt(objective, selectedSymbol, driver, input);
    // Paper simulation is an outcome stated in plain language, not a model
    // instruction that a user must phrase in a particular way.  Give the
    // bounded local-paper lane its correct autonomy as soon as the request is
    // recognized, even when the intent classifier is slow or mislabels the
    // category.  The Host still owns symbol resolution and rejects anything
    // outside the local Paper Broker boundary.
    const autoPaperExecution = input.source === 'global_composer'
      && !Object.prototype.hasOwnProperty.call(input, 'autonomy')
      && EXPLICIT_PAPER_ORDER.test(objective);
    const autonomy = input.autonomy
      || (autoPaperExecution ? 'paper_execute' : document.getElementById('agentAutonomySelect')?.value || 'advisory');
    if (autoPaperExecution) {
      const autonomySelect = document.getElementById('agentAutonomySelect');
      if (autonomySelect) autonomySelect.value = 'paper_execute';
    }
    const scope = intent.scope;
    // The classifier may preserve a literal company name in `symbols`.
    // Only pass executable identifiers to the Run;
    // unresolved names remain in the objective so the Agent must use the
    // security-search capability instead of guessing a US ticker.
    const classifiedSymbols = Array.isArray(intent.symbols)
      ? intent.symbols.map(value => String(value || '').trim()).filter(value => CLASSIFIED_SECURITY_ID.test(value))
      : [];
    const symbols = input.symbols?.length
      ? input.symbols
      : (classifiedSymbols.length ? classifiedSymbols : (scope === 'instrument' && intent.use_selected_symbol && explicitSymbols.includes(selectedSymbol) ? [selectedSymbol] : []));
    const run = await window.AgentRuntimeApi(`/api/agents/sessions/${encodeURIComponent(sessionId)}/runs`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        objective, symbols, context_scope: scope, intent, driver, autonomy, max_steps: 12,
        idempotency_key: `${sessionId}:${Date.now()}`,
      }),
    });
    if (!run?.run_id) {
      // A Host preflight can return a durable safety rejection or a decision
      // checkpoint instead of creating a provider Run. Reloading the Session
      // shows that auditable Host result without retrying the prompt with a
      // model or misreporting a transport failure.
      await sessions.select(sessionId);
      if (run?.interaction?.interaction_id) {
        const interactions = { ...store.getState().interactions, [run.interaction.interaction_id]: {
          ...run.interaction,
          kind: run.interaction.kind || 'proposal',
          status: run.interaction.status || 'waiting_decision',
          session_id: run.interaction.session_id || sessionId,
        } };
        store.set({ interactions, dock_open: true });
      }
      onAccepted();
      return run;
    }
    // A Session can be created or restored while the Composer is submitting.
    // The Host's Run receipt is authoritative: without synchronising this
    // value the new Run exists durably but the Dock keeps rendering the old
    // Session and appears not to have accepted the user's task.
    const runSessionId = String(run.session_id || sessionId || '');
    if (runSessionId && runSessionId !== store.getState().active_session_id) {
      await sessions.select(runSessionId);
    }
    // Only a durable Host Run may change the active stock context. The plain
    // language classifier resolves this symbol before the request, while the
    // Run receipt confirms it was actually accepted instead of guessed.
    await synchronizeSingleTaskSymbol(run.symbols?.length ? run.symbols : symbols);
    store.set({
      active_session_id: runSessionId || store.getState().active_session_id,
      active_run_id: run.run_id,
      dock_open: true,
      runs: { ...store.getState().runs, [run.run_id]: run },
    });
    const following = followRun(run.run_id);
    onAccepted();
    return following;
  }

  async function previewAutomationProposal(sessionId, objective, state) {
    const preview = await window.AgentRuntimeApi('/api/agents/automations/preview', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ goal: objective, context: { session_id: sessionId } }),
    });
    if (!preview?.opportunity?.worthwhile || !preview?.requires_user_confirmation) return null;
    const response = await sessions.sendMessage(
      sessionId,
      objective,
      window.AgentComposerContext?.payload?.(state) || null,
    );
    const interaction = response?.interaction;
    if (interaction?.interaction_id) {
      const interactions = { ...store.getState().interactions, [interaction.interaction_id]: {
        ...interaction,
        kind: interaction.kind || 'proposal',
        status: interaction.status || 'waiting_decision',
        session_id: interaction.session_id || sessionId,
      } };
      store.set({ interactions, dock_open: true });
    }
    return response;
  }

  async function classifyPrompt(objective, selectedSymbol, driver, input = {}) {
    if (input.intent && input.context_scope) return { ...input.intent, scope: input.context_scope };
    if (input.symbols?.length) {
      return { title: '指定標的分析', category: 'instrument_analysis', scope: 'instrument', use_selected_symbol: false, symbols: input.symbols, source: 'explicit-symbols' };
    }
    const status = document.getElementById('agentRunStatus');
    if (status) status.textContent = '◌ 正在由目前模型辨識問題類型…';
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), INTENT_CLASSIFICATION_TIMEOUT_MS);
    try {
      const intent = await window.AgentRuntimeApi('/api/agents/classify-intent', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ objective, selected_symbol: selectedSymbol || null, driver }),
        signal: controller.signal,
      });
      return { ...intent, source: 'model' };
    } catch (error) {
      // Intent is part of the model's public answer. Do not replace a failed
      // model call with a keyword guess, a fixed title, or a selected symbol.
      if (status) status.textContent = '！目前模型無法辨識問題類型；未建立 Run，請確認模型連線後重試。';
      const detail = error.name === 'AbortError' || /aborted/i.test(String(error.message || ''))
        ? `模型連線未在 ${Math.round(INTENT_CLASSIFICATION_TIMEOUT_MS / 1000)} 秒內回應，請確認模型服務與網路。`
        : (error.message || '請確認模型連線後重試。');
      throw new Error(`目前模型無法辨識問題類型：${detail}`);
    } finally {
      window.clearTimeout(timeout);
    }
  }

  async function submitMarketRadar(request) {
    const sessionId = await sessions.ensure();
    const snapshotId = String(request.market_snapshot_id || '').trim();
    if (snapshotId) {
      await api(`/api/intelligence/snapshots/${encodeURIComponent(snapshotId)}/model-overlay`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: 'running' }),
      });
      window.HomeMarketWorkspace?.load?.({ force: true, selectDefault: false });
    }
    try {
      const run = await api('/api/agents/market-radar/runs', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ...request,
        session_id: sessionId,
        idempotency_key: request.idempotency_key || `${sessionId}:radar:${Date.now()}`,
      }),
    });
    store.set({
      active_run_id: run.run_id,
      dock_open: true,
      runs: { ...store.getState().runs, [run.run_id]: run },
    });
    await followRun(run.run_id);
    const completed = await api(`/api/agents/market-radar/runs/${encodeURIComponent(run.run_id)}`);
    const radar = completed.market_radar_result;
    if (snapshotId && completed.market_radar_status === 'succeeded' && Array.isArray(radar?.items)) {
      const summaries = Object.fromEntries(radar.items.map((item) => [item.symbol, {
        action: item.action,
        confidence: item.confidence,
        reason: item.reason,
        next_action: item.next_action,
        timing: item.timing,
        trigger: item.trigger,
        evidence_ids: item.evidence_ids || [],
      }]));
      await api(`/api/intelligence/snapshots/${encodeURIComponent(snapshotId)}/model-overlay`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          succeeded: true,
          provider: completed.provenance?.provider,
          model_id: completed.provenance?.model_id,
          summaries,
          receipt: completed.model_invocation,
        }),
      });
      window.HomeMarketWorkspace?.load?.({ force: true, selectDefault: false });
    } else if (snapshotId) {
      await api(`/api/intelligence/snapshots/${encodeURIComponent(snapshotId)}/model-overlay`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          succeeded: false,
          error: completed.market_radar_error || {
            code: 'market_radar_incomplete',
            message: 'AI 深度分析未產生可驗證的完整結果。',
          },
        }),
      });
      window.HomeMarketWorkspace?.load?.({ force: true, selectDefault: false });
    }
      return completed;
    } catch (error) {
      if (snapshotId) {
        await api(`/api/intelligence/snapshots/${encodeURIComponent(snapshotId)}/model-overlay`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            succeeded: false,
            error: { code: 'market_radar_request_failed', message: error.message || 'AI 深度分析啟動或同步失敗。' },
          }),
        }).catch(() => {});
        window.HomeMarketWorkspace?.load?.({ force: true, selectDefault: false });
      }
      throw error;
    }
  }

  async function control(action, runId) {
    if (!runId) return;
    if (action === 'draft_new_goal') {
      const run = store.getState().runs[runId] || {};
      const boundaryDraft = resourceBoundaryDraft(run, run.result);
      if (boundaryDraft) return prepareGoalDraft(run, boundaryDraft);
      const composer = document.getElementById('agentComposerInput');
      const mode = document.getElementById('agentComposerMode');
      const autonomy = document.getElementById('agentAutonomySelect');
      const symbols = Array.isArray(run.symbols) ? run.symbols.filter(Boolean) : [];
      const symbolScope = symbols.length ? ` ${symbols.join('、')} ` : '目前標的';
      const safeDraft = `請重新檢視${symbolScope}的資料狀態、分析結論與主要風險；只做分析與預覽，不要建立、提交或重複任何訂單、外部操作或專案修改。若需要新的執行操作，請由我另行明確指定。`;
      // Opening an empty Session also prevents the completed execution Run
      // from remaining the Dock's foreground fallback while its safe draft is
      // being composed.  No Run is created here.
      const draftSession = await sessions.create('安全分析草稿');
      if (composer) {
        composer.value = safeDraft;
        composer.dispatchEvent(new Event('input', { bubbles: true }));
        composer.focus();
      }
      // A previous execution permission must never be inherited merely
      // because its completed objective is being used as a starting point.
      if (mode) mode.value = 'new_run';
      if (autonomy) autonomy.value = 'advisory';
      store.set({
        active_session_id: draftSession.session_id,
        active_run_id: null,
        active_tab: 'chat',
        dock_open: true,
        connection_state: 'idle',
      });
      const status = document.getElementById('agentRunStatus');
      if (status) {
        status.className = 'agent-run-status is-idle';
        status.textContent = '○ 已建立安全分析草稿；舊 Run 的執行權限不會沿用。';
      }
      return { draft: true, objective: safeDraft, autonomy: 'advisory' };
    }
    if (action === 'rerun') {
      const run = store.getState().runs[runId];
      return submit({ objective: run.objective, symbols: run.symbols, autonomy: run.autonomy });
    }
    if (action === 'new_run') {
      const run = store.getState().runs[runId];
      return submit({ objective: run.objective, symbols: run.symbols, autonomy: run.autonomy });
    }
    if (action === 'continue' || action === 'increase_limit') {
      await window.AgentRuntimeApi(`/api/agents/runs/${encodeURIComponent(runId)}/continue`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ additional_steps: action === 'increase_limit' ? 12 : 6 }),
      });
      await hydrateRun(runId);
      return followRun(runId);
    }
    if (action === 'replan') {
      const instruction = document.getElementById('agentComposerInput')?.value.trim()
        || '請根據目前錯誤、驗證與證據缺口重新規劃。';
      await window.AgentRuntimeApi(`/api/agents/runs/${encodeURIComponent(runId)}/replan`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ instruction }),
      });
    } else {
      await window.AgentRuntimeApi(`/api/agents/runs/${encodeURIComponent(runId)}/${action}`, { method: 'POST' });
    }
    if (action === 'cancel') stream.stop();
    await hydrateRun(runId);
    if (['resume', 'retry', 'replan'].includes(action)) return followRun(runId);
    return store.getState().runs[runId];
  }

  async function resolveApproval(approvalId, approved) {
    const challenge = await window.AgentRuntimeApi(`/api/agents/approvals/${encodeURIComponent(approvalId)}/challenge`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
    await window.AgentRuntimeApi(`/api/agents/approvals/${encodeURIComponent(approvalId)}/${approved ? 'approve' : 'deny'}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ challenge: challenge.challenge }),
    });
    // Approvals are stored by the run-scoped key ``<run_id>:<approval_id>``.
    // Looking them up by the raw approval id stopped the stream at
    // ``waiting_approval`` even after the Host had resumed and completed the
    // exact approved node. Rehydrate first so native WebKit immediately
    // replaces the provisional failure/waiting projection with the durable
    // Run snapshot, then reconnect from its current event sequence.
    const approval = Object.values(store.getState().approvals || {}).find(item => (
      String(item?.approval_id || '') === String(approvalId)
    ));
    if (approval?.run_id) {
      await hydrateRun(approval.run_id);
      return followRun(approval.run_id);
    }
    return null;
  }

  async function respondInteraction(interactionId, answer) {
    const response = await window.AgentRuntimeApi(`/api/agents/interactions/${encodeURIComponent(interactionId)}/respond`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(answer),
    });
    (response?.events || []).forEach(event => store.dispatch(event));
    const interactions = { ...store.getState().interactions };
    // Keep the local projection terminal even when a native WebKit repaint
    // happens before the next snapshot arrives.  A stale actionable card is
    // misleading after a successful "skip" because the completed answer does
    // not need another Run or any external acceptance.
    interactions[interactionId] = {
      ...(interactions[interactionId] || {}), interaction_id: interactionId,
      status: 'resolved', answer, response: response?.response || answer,
    };
    store.set({ interactions });
    if (response?.follow_run_id) return followRun(response.follow_run_id);
    return response;
  }

  function pinArtifactVersion(artifactId, version, selection, response) {
    const state = store.getState();
    const record = { ...response, artifact_id: artifactId, version: Number(version) };
    store.set({
      artifact_versions: {
        ...(state.artifact_versions || {}),
        [`${artifactId}:v${record.version}`]: record,
      },
    });
    if (selection) window.AgentArtifactSelection?.select?.({
      ...selection,
      selection_id: null,
      artifact_id: artifactId,
      artifact_version: record.version,
    });
    return record;
  }

  function confirmArtifactChange(message) {
    // WKWebView does not reliably present synchronous window.confirm dialogs.
    // Use an in-page modal there so the local user can explicitly approve a
    // versioned patch; retain the browser dialog for web and existing embeds.
    if (!window.webkit?.messageHandlers || typeof HTMLDialogElement === 'undefined') {
      return Promise.resolve(typeof window.confirm === 'function' && window.confirm(message));
    }
    return new Promise(resolve => {
      const dialog = document.createElement('dialog');
      const body = document.createElement('p');
      const cancel = document.createElement('button');
      const confirm = document.createElement('button');
      dialog.className = 'agent-artifact-confirmation';
      dialog.setAttribute('aria-label', '確認 Artifact 局部修改');
      body.textContent = message;
      cancel.type = 'button';
      cancel.textContent = '取消';
      cancel.addEventListener('click', () => dialog.close('cancel'));
      confirm.type = 'button';
      confirm.textContent = '確認建立新版本';
      confirm.addEventListener('click', () => dialog.close('confirm'));
      dialog.addEventListener('close', () => {
        const approved = dialog.returnValue === 'confirm';
        dialog.remove();
        resolve(approved);
      }, { once: true });
      dialog.append(body, cancel, confirm);
      document.body.append(dialog);
      dialog.showModal();
    });
  }

  async function proposeArtifactChange(artifactId, change = {}) {
    const state = store.getState();
    const selection = window.AgentComposerContext?.payload?.(state) || null;
    const currentVersion = Number(
      change.expected_version
      || selection?.artifact_version
      || window.AgentArtifactSelection?.latestVersion?.(state, artifactId)
      || 1,
    );
    const action = String(change.action || 'revise');
    const targetVersion = Number(change.source_version || change.target_version || 0);

    if (action === 'compare') {
      return { action: 'compare', artifact_id: artifactId, version: targetVersion || currentVersion };
    }

    const confirmation = action === 'restore' || action === 'undo'
      ? `確認要從 v${currentVersion} 還原為 v${targetVersion}？系統會建立新的 Artifact 版本。`
      : `確認套用這次局部修改到 Artifact v${currentVersion}？`;
    if (!(await confirmArtifactChange(confirmation))) {
      return { cancelled: true, artifact_id: artifactId, expected_version: currentVersion };
    }

    if (action === 'restore' || action === 'undo') {
      if (!(targetVersion > 0)) throw new Error('還原 Artifact 時必須指定來源版本。');
      const restored = await window.AgentRuntimeApi(`/api/agents/artifacts/${encodeURIComponent(artifactId)}/restore`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source_version: targetVersion, expected_version: currentVersion }),
      });
      return pinArtifactVersion(artifactId, restored.version, selection, restored);
    }

    if (!Object.prototype.hasOwnProperty.call(change, 'content')) {
      throw new Error('Artifact 修改必須包含局部修改後的 content。');
    }
    const reason = String(change.reason || '').trim();
    if (!reason) throw new Error('Artifact 修改必須說明 reason。');
    const affectedNodeIds = [...new Set([
      ...(Array.isArray(change.affected_node_ids) ? change.affected_node_ids : []),
      selection?.node_id,
    ].filter(Boolean).map(String))];
    const payload = {
      expected_version: currentVersion,
      content: change.content,
      reason,
      affected_node_ids: affectedNodeIds,
    };
    if (change.message_id) payload.message_id = String(change.message_id);
    const response = await window.AgentRuntimeApi(`/api/agents/artifacts/${encodeURIComponent(artifactId)}/propose-change`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    (response?.events || []).forEach(event => store.dispatch(event));
    return pinArtifactVersion(artifactId, response.version, selection, response);
  }

  function openTask(stepId) {
    store.set({ active_tab: 'tasks', dock_open: true });
    requestAnimationFrame(() => {
      const escaped = window.CSS?.escape ? window.CSS.escape(String(stepId)) : String(stepId);
      document.querySelector(`#agentTasksTree [data-node-id="${escaped}"]`)
        ?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    });
  }

  function showError(error) {
    const node = document.getElementById('agentRunStatus');
    if (node) {
      node.className = 'agent-run-status is-failed';
      node.textContent = error?.message || 'Agent 操作失敗。';
    }
  }

  function bindShell() {
    const click = (id, handler) => document.getElementById(id)?.addEventListener('click', handler);
    // Chart focus is deliberately a single-purpose canvas mode. Closing the
    // Dock here prevents it from covering the focused chart; the left
    // navigation remains the sole place to reopen or enter Agent 控制台.
    document.addEventListener('stock-ai:chart-focus-change', event => {
      if (event.detail?.enabled && store.getState().dock_open) {
        store.set({ dock_open: false });
      }
    });
    click('agentTechnicalViewToggle', () => store.set({ technical_view: !store.getState().technical_view }));
    click('agentDockNewSession', () => {
      stream.stop();
      sessions.create().catch(showError);
    });
    click('agentDockHistory', async () => {
      const menu = document.getElementById('agentSessionMenu');
      const items = await sessions.list();
      menu.replaceChildren();
      items.filter(item => !item.archived).forEach(item => {
        const row = document.createElement('div');
        row.className = 'agent-session-menu-row';
        const open = document.createElement('button');
        const archive = document.createElement('button');
        open.type = 'button';
        open.textContent = `${item.title} · ${item.message_count} 則`;
        open.setAttribute('aria-label', `開啟工作階段：${item.title}`);
        open.addEventListener('click', async () => {
          try {
            stream.stop();
            const selected = await sessions.select(item.session_id);
            if (selected.active_run_id) await hydrateRun(selected.active_run_id);
            menu.hidden = true;
          } catch (error) { showError(error); }
        });
        archive.type = 'button';
        archive.className = 'agent-session-archive';
        archive.textContent = '封存';
        archive.setAttribute('aria-label', `封存工作階段：${item.title}`);
        archive.addEventListener('click', async event => {
          event.stopPropagation();
          try {
            await sessions.archive(item.session_id);
            row.remove();
          } catch (error) { showError(error); }
        });
        row.append(open, archive);
        menu.append(row);
      });
      menu.hidden = !menu.hidden;
    });
    click('agentConsoleNav', () => {
      const state = store.getState();
      // The function-list button is the only Agent visibility control.  It
      // always opens the compact, resizable right dock; it never covers the
      // market workspace or top bar.
      store.set({ dock_open: !state.dock_open, dock_maximized: false });
    });
    click('openAgentWorkspace', () => store.set({ dock_open: true }));
    click('globalAgentSend', () => {
      const prompt = document.getElementById('globalAgentPrompt');
      submit({ objective: prompt?.value || '', source: 'global_composer' })
        .then(() => { if (prompt) prompt.value = ''; })
        .catch(showError);
    });
    document.getElementById('globalAgentPrompt')?.addEventListener('keydown', event => {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        document.getElementById('globalAgentSend')?.click();
      }
    });
    document.querySelectorAll('[data-agent-dock-tab]').forEach(button => {
      button.addEventListener('click', () => store.set({ active_tab: button.dataset.agentDockTab }));
    });
    const resizer = document.getElementById('agentDockResizer');
    let startX = 0;
    let startWidth = 0;
    resizer?.addEventListener('pointerdown', event => {
      startX = event.clientX; startWidth = store.getState().dock_width;
      resizer.setPointerCapture(event.pointerId);
    });
    resizer?.addEventListener('pointermove', event => {
      if (!resizer.hasPointerCapture(event.pointerId)) return;
      store.set({ dock_width: Math.min(640, Math.max(300, startWidth + startX - event.clientX)) });
    });
    resizer?.addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      event.preventDefault();
      const delta = event.key === 'ArrowLeft' ? 16 : -16;
      store.set({ dock_width: Math.min(640, Math.max(300, store.getState().dock_width + delta)) });
    });
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && window.innerWidth < 1280) store.set({ dock_open: false });
    });
  }

  async function init() {
    if (initialized || !document.getElementById('agentDock')) return;
    initialized = true;
    bindShell();
    window.AgentComposer.init();
    store.subscribe(render);
    render(store.getState());
    try {
      const [runtime, settings] = await Promise.all([window.AgentRuntimeApi('/api/agents'), window.AgentRuntimeApi('/api/agents/settings')]);
      const provider = settings.default_driver || runtime.default_driver || 'Stock AI Runtime';
      (runtime.providers?.items || []).forEach(item => {
        const providerId = item.provider_id;
        if (!providerId) return;
        runtimeIdentities[providerId] = {
          provider: providerId,
          model: item.capabilities?.profile?.model || item.capabilities?.model || '',
        };
      });
      const selectedConfig = provider === 'openai-compatible'
        ? settings.openai_compatible
        : provider === 'external-agent' ? settings.external_agent : null;
      runtimeIdentity = runtimeIdentities[provider] || {
        provider,
        model: selectedConfig?.model || runtime.model || '',
      };
      runtimeIdentities[provider] = {
        ...runtimeIdentity,
        model: selectedConfig?.model || runtimeIdentity.model || '',
      };
      await sessions.restoreForeground();
      // The macOS WebView starts with an empty in-memory Context after a
      // relaunch. Restore the selected Session's one explicit task symbol so
      // reopening the desktop app cannot strand a completed Agent Run on the
      // market-index screen. Never replace a real stock the user had already
      // selected before the Session was restored.
      const foregroundRun = activeRun(store.getState());
      if (foregroundRun.run_id) await hydrateRun(foregroundRun.run_id);
      await synchronizeSingleTaskSymbol(activeRun(store.getState()).symbols, { onlyIfUnselected: true });
      await loadObservability();
      const runId = store.getState().active_run_id;
      if (runId) {
        await hydrateRun(runId);
        const hydratedRun = store.getState().runs[runId];
        if (!window.AgentEventReducer?.isTerminalRun?.(hydratedRun)) {
          followRun(runId).catch(showError);
        }
      }
      render(store.getState());
    } catch (error) { showError(error); }
  }

  window.AgentDockController = {
    init, submit, submitMarketRadar, control, resolveApproval, showError, loadObservability,
    openTask, proposeArtifactChange, respondInteraction, synchronizeSingleTaskSymbol,
    open: () => store.set({ dock_open: true }),
    close: () => store.set({ dock_open: false, dock_maximized: false }),
    getState: store.getState,
  };
  window.runAutonomousAgent = objective => submit({ objective, source: 'legacy_entry' });
  document.readyState === 'loading'
    ? document.addEventListener('DOMContentLoaded', init, { once: true })
    : init();
}());
