let agentUiBridgeAfterId = 0;
let agentUiBridgeStopped = false;

function collectAgentUiState() {
  const context = window.WorkspaceContextStore?.get?.() || {};
  return {
    current_view: context.route?.workspace || 'home',
    current_tab: context.route?.tab || 'overview',
    current_symbol: context.selection?.symbol || null,
    agent_activity_open: window.AgentDockController?.getState()?.dock_open
      ?? !$('globalAgentPopover')?.hidden,
    agent_run_id: window.AgentDockController?.getState()?.active_run_id
      || state.activeAgentRunId
      || null,
    order_form: {
      symbol: $('paperTrainingSymbol')?.value || null,
      side: $('paperTrainingSide')?.value || null,
      amount: Number($('paperTrainingAmount')?.value || 0) || null,
      rationale: $('paperTrainingRationale')?.value || '',
    },
    container: window.webkit?.messageHandlers ? 'wkwebview' : 'browser',
    url: window.location.href,
  };
}

async function publishAgentUiState() {
  await api('/api/agents/ui/state', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ state: collectAgentUiState() }),
  });
}

async function applyAgentUiCommand(command) {
  const action = String(command.action || '');
  const args = command.arguments || {};
  if (action === 'execute_action') {
    const actionId = String(args.action_id || '');
    const input = args.input || {};
    await executeRegisteredUiAction(actionId, input);
  } else if (action === 'navigate') {
    const view = String(args.view || '');
    const workspace = view;
    if (!WORKSPACES[workspace]) throw new Error(`未知工作區：${view}`);
    setView(workspace, { tab: args.tab || null });
    await loadViewData(workspace);
  } else if (action === 'select_symbol') {
    const symbol = String(args.symbol || '').trim().toUpperCase();
    if (!symbol) throw new Error('缺少股票代號');
    window.WorkspaceContextStore?.set?.({
      selection: { symbol, explicit_intent_symbols: [symbol] },
    }, { reason: 'agent-ui:select-symbol' });
    setView('instrument', { tab: String(args.tab || 'overview') });
    await loadViewData('instrument');
  } else if (action === 'open_panel') {
    const panel = String(args.panel || '');
    if (panel === 'agent_activity') {
      setGlobalAgentPopover(true);
    } else if (panel === 'agent_settings') {
      setView('system');
      setWorkspaceTab('system', 'agent-models');
      await loadViewData('system');
      $('settingsAgentRuntime')?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    } else if (panel === 'paper_training') {
      setView('portfolio');
      setWorkspaceTab('portfolio', 'simulation');
      window.__paperTradingUI?.activateView?.();
    } else {
      throw new Error(`不允許開啟的面板：${panel}`);
    }
  } else if (action === 'fill_order') {
    setView('portfolio');
    setWorkspaceTab('portfolio', 'simulation');
    window.__paperTradingUI?.activateView?.();
    if ($('paperTrainingSymbol')) $('paperTrainingSymbol').value = String(args.symbol || '').toUpperCase();
    if ($('paperTrainingSide')) $('paperTrainingSide').value = String(args.side || 'buy');
    if ($('paperTrainingAmount') && args.amount !== undefined) $('paperTrainingAmount').value = String(args.amount);
    if ($('paperTrainingRationale')) $('paperTrainingRationale').value = String(args.rationale || '');
  } else if (action === 'submit_action') {
    const selected = String(args.action || '');
    if (selected === 'refresh_view') {
      await loadViewData(collectAgentUiState().current_view);
    } else if (selected === 'open_agent_activity') {
      setGlobalAgentPopover(true);
    } else if (selected === 'close_agent_activity') {
      setGlobalAgentPopover(false);
    } else {
      throw new Error(`不允許的 UI 動作：${selected}`);
    }
  } else {
    throw new Error(`未知 UI 指令：${action}`);
  }
  await publishAgentUiState();
  return collectAgentUiState();
}

async function executeRegisteredUiAction(actionId, input = {}) {
  if (actionId === 'workspace.open') {
    setView(String(input.workspace || 'home'));
  } else if (actionId === 'workspace.tab.open') {
    setView(String(input.workspace || 'home'), { tab: String(input.tab || '') });
  } else if (actionId === 'route.back') {
    history.back();
  } else if (actionId === 'route.forward') {
    history.forward();
  } else if (['entity.select', 'market.candidate.select'].includes(actionId)) {
    const symbol = String(input.symbol || '').trim().toUpperCase();
    if (!symbol) throw new Error('缺少股票代號');
    window.WorkspaceContextStore?.set?.({
      selection: { symbol, explicit_intent_symbols: [symbol] },
    }, { reason: `ui-action:${actionId}` });
    setView('instrument', { tab: String(input.tab || 'overview') });
  } else if (actionId.startsWith('chart.')) {
    if (!window.StockChartInteractions?.execute) throw new Error('圖表工作台尚未就緒');
    window.StockChartInteractions.execute(actionId, input);
  } else if (actionId === 'agent.dock.open') {
    window.AgentDockController?.open?.();
  } else if (actionId === 'agent.dock.close') {
    window.AgentDockController?.close?.();
  } else if (actionId === 'agent.dock.maximize') {
    window.AgentDockController?.maximize?.();
  } else if (actionId === 'agent.dock.restore') {
    window.AgentDockController?.restore?.();
  } else if (actionId === 'agent.dock.resize') {
    const width = Math.min(520, Math.max(320, Number(input.width) || 360));
    document.documentElement.style.setProperty('--agent-dock-width', `${width}px`);
    window.WorkspaceContextStore?.set?.({ agent: { dock_width: width } }, { reason: actionId });
  } else if (actionId === 'agent.tab.open') {
    const tab = String(input.tab || 'chat');
    document.querySelector(`[data-agent-dock-tab="${CSS.escape(tab)}"]`)?.click();
  } else if (actionId === 'agent.session.new') {
    document.getElementById('agentDockNewSession')?.click();
  } else {
    const target = document.querySelector(`[data-ui-action="${CSS.escape(actionId)}"]`);
    if (!target) throw new Error(`UI 動作尚無可用的畫面元件：${actionId}`);
    target.click();
  }
  await Promise.resolve(window.__workspaceTabLoad);
}

async function processAgentUiBridge() {
  if (agentUiBridgeStopped) return;
  try {
    await publishAgentUiState();
    const batch = await api(`/api/agents/ui/commands?after_id=${agentUiBridgeAfterId}`);
    for (const command of batch.items || []) {
      let payload;
      try {
        payload = { ok: true, result: await applyAgentUiCommand(command) };
      } catch (error) {
        payload = { ok: false, error: { type: error.name || 'Error', message: error.message || String(error) } };
      }
      await api(`/api/agents/ui/commands/${command.command_id}/result`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      agentUiBridgeAfterId = Math.max(agentUiBridgeAfterId, Number(command.command_id || 0));
    }
  } catch (error) {
    console.debug('Agent UI bridge waiting for backend', error);
  } finally {
    window.setTimeout(processAgentUiBridge, 750);
  }
}

window.addEventListener('beforeunload', () => { agentUiBridgeStopped = true; });
processAgentUiBridge();
