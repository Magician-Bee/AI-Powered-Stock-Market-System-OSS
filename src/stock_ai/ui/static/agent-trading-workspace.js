(() => {
  'use strict';

  if (window.__agentTradingWorkspaceLoaded) return;
  window.__agentTradingWorkspaceLoaded = true;

  const API_ROOT = '/api/open-stock-ai/agent/trading';
  const byId = (id) => document.getElementById(id);
  const number = (value) => Number(value || 0);
  const escapeHtml = (value) => String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
  const money = (value) => number(value).toLocaleString('zh-TW', { maximumFractionDigits: 2 });
  const currency = (value) => `NT$ ${money(value)}`;
  const percent = (value) => `${number(value).toFixed(2)}%`;

  let sessionCache = null;
  let refreshTimer = 0;
  let sessionRequestVersion = 0;

  function request(path, options = {}) {
    const method = String(options.method || 'GET').toUpperCase();
    const suffix = method === 'GET' ? `${path.includes('?') ? '&' : '?'}_agent_ts=${Date.now()}` : '';
    return fetch(`${path}${suffix}`, {
      cache: 'no-store',
      credentials: 'same-origin',
      ...options,
      headers: {
        'Content-Type': 'application/json',
        'Cache-Control': 'no-cache, no-store, max-age=0',
        Pragma: 'no-cache',
        ...(options.headers || {}),
      },
    }).then(async (response) => {
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = payload.detail;
        const message = typeof detail === 'string'
          ? detail
          : detail?.message || payload.error || `HTTP ${response.status}`;
        const error = new Error(message);
        error.payload = payload;
        throw error;
      }
      return payload;
    });
  }

  function currentSymbol() {
    return String(
      byId('agentTradingPaperSymbol')?.value
      || byId('agentTradingHomeSymbol')?.value
      || ''
    ).trim().toUpperCase();
  }

  function card(label, value, note = '') {
    return `<div class="mini-card"><span>${escapeHtml(label)}</span><strong>${value}</strong>${note ? `<small>${escapeHtml(note)}</small>` : ''}</div>`;
  }

  function setAgentStatus(text, tone = '') {
    ['agentTradingHomeStatus', 'agentTradingPaperStatus'].forEach((id) => {
      const node = byId(id);
      if (!node) return;
      node.textContent = text;
      node.dataset.tone = tone;
    });
  }

  function accountRevisionText(account) {
    const revision = String(account?.account_revision || '').replace(/^PA-/, '');
    return revision ? `帳戶版本 ${revision.slice(0, 8)}` : '帳戶版本未取得';
  }

  // The Paper Trading UI is the only owner of paperTrainingSummary and
  // paperTrainingInitialCash. This Agent layer only renders Agent/home state.
  // Keeping one writer prevents an older Agent session response from restoring
  // the previous cash balance after a successful account reset.
  function renderAccountSnapshot(account) {
    if (!account) return;

    const homeMetrics = byId('agentTradingHomeMetrics');
    if (homeMetrics) {
      homeMetrics.innerHTML = [
        card('Agent 總資產', currency(account.total_equity), `虛擬現金 ${currency(account.cash_balance)}`),
        card('持倉 / 掛單', `${account.position_count || 0} / ${(account.open_orders || []).length}`, `成交 ${account.fill_count || 0}`),
        card('累積績效', percent(account.total_return_pct), `損益 ${currency(number(account.total_equity) - number(account.initial_cash))}`),
      ].join('');
      homeMetrics.dataset.accountRevision = account.account_revision || '';
    }

    const homeRevision = byId('agentTradingHomeRevision');
    if (homeRevision) {
      const source = account.source_of_truth || {};
      homeRevision.textContent = `${accountRevisionText(account)} · ${source.account_id || 'default-paper'}`;
      homeRevision.title = source.sqlite_path || '';
    }

    const paperRevision = byId('agentTradingPaperRevision');
    if (paperRevision) {
      const source = account.source_of_truth || {};
      paperRevision.textContent = `${accountRevisionText(account)} · SQLite 單一帳戶`;
      paperRevision.title = source.sqlite_path || '';
    }

    document.dispatchEvent(new CustomEvent('stock-ai-agent-account-snapshot', { detail: account }));
  }

  function ensureHomePanel() {
    const workspace = document.querySelector('#home .home-workspace');
    if (!workspace || byId('agentTradingHomePanel')) return;

    const panel = document.createElement('article');
    panel.id = 'agentTradingHomePanel';
    panel.className = 'panel agent-trading-home-panel';
    panel.innerHTML = `
      <div class="panel-head">
        <div>
          <h3>Stock AI Agent 交易中樞</h3>
          <p class="panel-copy">Stock AI Agent 會共用市場研究、模擬帳戶、持倉、掛單與學習記憶；目前選定模型負責規劃與推理。</p>
        </div>
        <span class="chip" id="agentTradingHomeCodex">操作員載入中</span>
      </div>
      <div id="agentTradingHomeMetrics" class="summary-cards agent-trading-home-metrics"></div>
      <div class="agent-trading-home-command">
        <label>目前股票
          <input id="agentTradingHomeSymbol" value="" placeholder="選擇或輸入股票代號" autocomplete="off" />
        </label>
        <label class="agent-trading-home-goal">Agent 任務
          <input id="agentTradingHomePrompt" value="" placeholder="輸入你要 Agent 分析或執行的任務" autocomplete="off" />
        </label>
      </div>
      <div class="agent-trading-action-row">
        <button id="agentTradingHomeRefresh" type="button">同步帳戶</button>
        <button id="agentTradingHomeAnalyze" type="button">Agent 分析</button>
        <button id="agentTradingHomeExecute" class="primary" type="button">Agent 自主模擬一筆</button>
        <button id="agentTradingHomeOpen" type="button">開啟交易工作區</button>
      </div>
      <div class="agent-trading-status-row">
        <span id="agentTradingHomeStatus">正在載入 Agent 帳戶…</span>
        <small id="agentTradingHomeRevision"></small>
      </div>
      <div id="agentTradingHomeAnswer" class="agent-trading-answer" hidden></div>`;

    const codexPanel = workspace.querySelector('.codex-command-panel');
    workspace.insertBefore(panel, codexPanel || workspace.firstChild);
  }

  function ensurePaperAgentPanel() {
    const panel = byId('paperTrainingPanel');
    const layout = panel?.querySelector('.paper-broker-layout');
    if (!panel || !layout || byId('agentTradingPaperPanel')) return;

    const command = document.createElement('section');
    command.id = 'agentTradingPaperPanel';
    command.className = 'paper-broker-card agent-trading-paper-panel';
    command.innerHTML = `
      <div class="paper-broker-head">
        <div>
          <h4>Stock AI Agent 控制台</h4>
          <small>分析、委託草案、執行、重新估值與學習回合使用同一份帳戶狀態。</small>
        </div>
        <span class="chip" id="agentTradingPaperCodex">操作員載入中</span>
      </div>
      <div class="agent-trading-paper-grid">
        <label>分析股票
          <input id="agentTradingPaperSymbol" value="" placeholder="選擇或輸入股票代號" autocomplete="off" />
        </label>
        <label class="agent-trading-paper-prompt">Agent 任務
          <textarea id="agentTradingPaperPrompt" rows="3" placeholder="輸入你要 Agent 分析或執行的任務"></textarea>
        </label>
      </div>
      <div class="agent-trading-action-row">
        <button id="agentTradingPaperRefresh" type="button">同步完整 Context</button>
        <button id="agentTradingPaperAnalyze" type="button">產生委託草案</button>
        <button id="agentTradingPaperExecute" class="primary" type="button">Agent 自主執行一筆</button>
        <button id="agentTradingPaperControl" type="button">Agent 操作目前 UI</button>
      </div>
      <div class="agent-trading-status-row">
        <span id="agentTradingPaperStatus">等待 Agent 任務。</span>
        <small id="agentTradingPaperRevision"></small>
      </div>
      <div id="agentTradingPaperAnswer" class="agent-trading-answer" hidden></div>`;

    layout.insertAdjacentElement('beforebegin', command);
  }

  function applyDraftToTicket(result) {
    const draft = result?.decision?.order_draft;
    if (!draft) return;
    const values = {
      paperTrainingSymbol: result.symbol,
      paperTrainingOrderType: draft.order_type,
      paperTrainingTimeInForce: draft.time_in_force,
      paperTrainingLotType: draft.lot_type,
      paperTrainingSession: draft.session,
      paperTrainingQuantity: draft.quantity_shares || draft.quantity_lots,
      paperTrainingLimitPrice: draft.limit_price,
      paperTrainingStopPrice: draft.stop_price,
      paperTrainingRationale: draft.rationale,
    };
    Object.entries(values).forEach(([id, value]) => {
      const node = byId(id);
      if (node && value !== null && value !== undefined) node.value = String(value);
    });
    const side = draft.side === 'sell' ? 'sell' : 'buy';
    const hidden = byId('paperTrainingSide');
    if (hidden) hidden.value = side;
    byId('paperTrainingBuySide')?.classList.toggle('active', side === 'buy');
    byId('paperTrainingSellSide')?.classList.toggle('active', side === 'sell');
    byId('paperTrainingOrderType')?.dispatchEvent(new Event('change', { bubbles: true }));
  }

  async function refreshSession({ refreshPrices = false } = {}) {
    const requestVersion = ++sessionRequestVersion;
    const symbol = currentSymbol();
    const params = new URLSearchParams({ refresh_prices: refreshPrices ? 'true' : 'false' });
    if (symbol) params.set('symbol', symbol);
    const session = await request(`${API_ROOT}/session?${params.toString()}`);
    if (requestVersion !== sessionRequestVersion) return sessionCache;

    sessionCache = session;
    renderAccountSnapshot(session.account);
    const authenticated = session.codex?.authenticated === true;
    const activeAgent = window.__stockAIActiveAgent || { id: 'codex', label: 'Codex' };
    ['agentTradingHomeCodex', 'agentTradingPaperCodex'].forEach((id) => {
      const node = byId(id);
      if (!node) return;
      const connected = activeAgent.id === 'codex' ? authenticated : true;
      node.textContent = activeAgent.id === 'codex'
        ? (authenticated ? 'Codex 已連線' : 'Codex 未登入')
        : `${activeAgent.label} 已選用`;
      node.dataset.connected = connected ? 'true' : 'false';
    });
    setAgentStatus(
      session.symbol
        ? `Agent Context 已同步：${session.symbol}`
        : '模擬帳戶已同步；選擇股票並輸入任務後即可分析。',
      'ok',
    );
    return session;
  }

  document.addEventListener('stock-ai-active-agent-changed', (event) => {
    const activeAgent = event.detail || { id: 'codex', label: 'Codex' };
    ['agentTradingHomeCodex', 'agentTradingPaperCodex'].forEach((id) => {
      const node = byId(id);
      if (!node) return;
      node.textContent = activeAgent.id === 'codex' ? 'Codex' : `${activeAgent.label} 已選用`;
      node.dataset.connected = activeAgent.id === 'codex' ? node.dataset.connected : 'true';
    });
  });

  async function refreshPaperAccountFromSource() {
    if (typeof window.__paperTradingUI?.loadAccount === 'function') {
      return window.__paperTradingUI.loadAccount({ preserveStatus: true });
    }
    return null;
  }

  async function runAgent({ execute = false } = {}) {
    setAgentStatus('任務已送到右側 Stock AI Agent 工作區。');
    try {
      const symbol = String(byId('agentTradingPaperSymbol')?.value || byId('agentTradingHomeSymbol')?.value || currentSymbol()).trim();
      const prompt = String(byId('agentTradingPaperPrompt')?.value || byId('agentTradingHomePrompt')?.value || '').trim();
      if (!symbol) throw new Error('請先選擇或輸入股票代號。');
      if (!prompt) throw new Error('請先輸入要交給 Agent 的任務。');
      const mode = byId('agentAutonomySelect');
      if (execute && mode) mode.value = 'paper_execute';
      const objective = [
        `目前標的：${symbol}。`,
        prompt,
        execute
          ? '若要模擬下單，必須先以 paper.preview_order 預覽完全相同的委託；等待使用者對精確動作的批准後才可提交。'
          : '只分析與預覽，不要送出委託。',
      ].join('\n');
      if (!window.AgentDockController) throw new Error('Stock AI Agent Dock 尚未載入。');
      const runtimeResult = await window.AgentDockController.submit({
        objective,
        symbols: [symbol],
        autonomy: execute ? 'paper_execute' : 'advisory',
        source: 'agent_trading_workspace',
        context: { symbol, account_id: sessionCache?.account?.account_id || 'default-paper' },
      });
      const decision = runtimeResult?.decision || {};
      const result = {
        symbol,
        executed: Number(runtimeResult?.paper_execution_count || 0) > 0,
        decision: {
          ...decision,
          summary: runtimeResult?.summary || decision.rationale,
          evidence: (runtimeResult?.tool_trace || []).filter((item) => item.ok).map((item) => item.tool),
          risks: [],
          memory_used: [],
          next_checks: decision.next_check ? [decision.next_check] : [],
        },
      };
      applyDraftToTicket(result);
      if (execute) await refreshPaperAccountFromSource();
      setAgentStatus(execute
        ? (result.executed ? 'Stock AI Agent 已將委託送入模擬券商並更新帳戶。' : 'Stock AI Agent 完成分析，本輪沒有建立委託。')
        : 'Stock AI Agent 委託草案已填入下單欄位。', 'ok');
      await refreshSession();
    } catch (error) {
      setAgentStatus(error.message || String(error), 'error');
      throw error;
    }
  }

  async function runControl() {
    setAgentStatus('UI 操作任務已送到右側 Stock AI Agent 工作區。');
    try {
      const symbol = String(byId('agentTradingPaperSymbol')?.value || currentSymbol()).trim();
      const prompt = String(byId('agentTradingPaperPrompt')?.value || '').trim();
      if (!symbol) throw new Error('請先選擇或輸入股票代號。');
      if (!prompt) throw new Error('請先輸入要交給 Agent 的任務。');
      if (!window.AgentDockController) throw new Error('Stock AI Agent Dock 尚未載入。');
      await window.AgentDockController.submit({
        objective: `目前標的：${symbol}。請透過 Stock AI UI bridge 讀取並操作目前介面；${prompt}`,
        symbols: [symbol],
        source: 'agent_trading_ui_control',
      });
      setAgentStatus('Stock AI Agent UI 操作回合已完成；任何變更都已留在主工作台活動紀錄。', 'ok');
      await refreshSession();
    } catch (error) {
      setAgentStatus(error.message || String(error), 'error');
      throw error;
    }
  }

  function openPaperTrading() {
    window.setView?.('portfolio');
    window.setWorkspaceTab?.('portfolio', 'holdings');
    window.__paperTradingUI?.activateView?.();
  }

  function bindClick(id, handler) {
    const node = byId(id);
    if (!node || node.dataset.agentBound === 'true') return;
    node.dataset.agentBound = 'true';
    node.addEventListener('click', () => {
      Promise.resolve(handler()).catch((error) => setAgentStatus(error.message || String(error), 'error'));
    });
  }

  function bindControls() {
    bindClick('agentTradingHomeRefresh', refreshSession);
    bindClick('agentTradingHomeAnalyze', () => runAgent({ execute: false }));
    bindClick('agentTradingHomeExecute', () => {
      if (!window.confirm('確定讓 Stock AI Agent 依目前帳戶與研究資料自主執行一筆模擬交易？')) return;
      return runAgent({ execute: true });
    });
    bindClick('agentTradingHomeOpen', openPaperTrading);
    bindClick('agentTradingPaperRefresh', () => refreshSession({ refreshPrices: true }));
    bindClick('agentTradingPaperAnalyze', () => runAgent({ execute: false }));
    bindClick('agentTradingPaperExecute', () => {
      if (!window.confirm('確定讓 Stock AI Agent 自主決定並送出一筆模擬委託？')) return;
      return runAgent({ execute: true });
    });
    bindClick('agentTradingPaperControl', runControl);
  }

  function synchronizeSymbols() {
    const symbol = currentSymbol();
    if (!symbol) return;
    if (byId('agentTradingHomeSymbol') && !byId('agentTradingHomeSymbol').matches(':focus')) byId('agentTradingHomeSymbol').value = symbol;
    if (byId('agentTradingPaperSymbol') && !byId('agentTradingPaperSymbol').matches(':focus')) byId('agentTradingPaperSymbol').value = symbol;
  }

  function scheduleRefresh() {
    window.clearTimeout(refreshTimer);
    refreshTimer = window.setTimeout(() => {
      refreshSession().catch((error) => setAgentStatus(error.message || String(error), 'error'));
    }, 220);
  }

  function acceptPaperAccountEvent(event) {
    const account = event?.detail;
    if (!account) return;

    // Invalidate any Agent session request that started before this confirmed
    // Paper account update. The old response may still arrive, but it cannot be
    // accepted or redraw the home cards with the previous cash balance.
    sessionRequestVersion += 1;
    sessionCache = { ...(sessionCache || {}), account };
    renderAccountSnapshot(account);
    scheduleRefresh();
  }

  function initialize() {
    ensureHomePanel();
    ensurePaperAgentPanel();
    bindControls();
    synchronizeSymbols();

    // The reset button belongs exclusively to paper-training.js. Do not capture
    // or stop its click event here.
    document.addEventListener('stock-ai-paper-account-updated', acceptPaperAccountEvent);
    document.addEventListener('stock-ai-search-symbol-changed', (event) => {
      const symbol = String(event?.detail?.symbol || (typeof state !== 'undefined' ? state.currentEntity?.symbol : '') || '').trim().toUpperCase();
      if (symbol) {
        if (byId('agentTradingHomeSymbol')) byId('agentTradingHomeSymbol').value = symbol;
        if (byId('agentTradingPaperSymbol')) byId('agentTradingPaperSymbol').value = symbol;
      }
      synchronizeSymbols();
      scheduleRefresh();
    });

    const observer = new MutationObserver(() => {
      ensureHomePanel();
      ensurePaperAgentPanel();
      bindControls();
    });
    observer.observe(document.body, { childList: true, subtree: true });

    refreshSession().catch((error) => setAgentStatus(error.message || String(error), 'error'));

    window.__agentTradingWorkspace = {
      refreshSession,
      runAgent,
      runControl,
      renderAccountSnapshot,
    };
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initialize, { once: true });
  } else {
    initialize();
  }
})();
