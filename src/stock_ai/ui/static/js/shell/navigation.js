const WORKSPACES = Object.freeze({
  home: { title: 'AI 全市場決策中心', subtitle: '全市場多因子分析、AI 深度判斷、中央風控與持倉適配', defaultTab: 'overview', tabs: [] },
  market: { title: '市場', subtitle: '總覽、排行、自選、篩選器、監控與新聞事件', defaultTab: 'overview', tabs: [
    ['overview', '總覽'], ['rankings', '排行'], ['watchlists', '自選與提醒'], ['screener', '條件選股'], ['monitor', '即時監控'], ['events', '新聞與事件'],
  ] },
  instrument: { title: '個股', subtitle: '總覽、圖表、技術、籌碼、財務、估值、新聞事件與風險證據', defaultTab: 'overview', tabs: [
    ['overview', '總覽'], ['chart', '圖表'], ['technical', '技術'], ['ownership', '籌碼'], ['financials', '財務'], ['valuation', '估值'], ['events', '新聞與事件'], ['evidence', '風險與證據'],
  ] },
  portfolio: { title: '投資組合', subtitle: '資產、持倉、持倉建議、委託成交、模擬帳戶、績效與風險', defaultTab: 'overview', tabs: [
    ['overview', '總覽'], ['positions', '持倉與建議'], ['orders', '委託與成交'], ['simulation', '模擬帳戶'], ['performance', '績效'], ['risk', '組合風險'],
  ] },
  research: { title: '研究', subtitle: '股票比較、AI 深度研究、策略回測、因子、關聯與研究報告', defaultTab: 'compare', tabs: [
    ['compare', '股票比較'], ['deep-research', 'AI 深度研究'], ['strategy-lab', '策略與回測'], ['factor-lab', '因子研究'], ['relationships', '關聯分析'], ['reports', '研究報告'],
  ] },
  system: { title: '系統', subtitle: '證券主檔、資料平台、券商連線、Agent、工具與介面設定', defaultTab: 'agent-models', tabs: [
    ['securities', '證券主檔'], ['data-platform', '資料平台'], ['brokers', '券商與連線'], ['agent-models', 'Agent 與模型'], ['tools', '工具與整合'], ['interface', '介面與一般'],
  ] },
});
window.WORKSPACES = WORKSPACES;

const WORKSPACE_TAB_STORAGE_KEY = 'stock_ai.workspace.last_tabs.v1';
let applyingLocationRoute = false;

function storedWorkspaceTabs() {
  try { return JSON.parse(localStorage.getItem(WORKSPACE_TAB_STORAGE_KEY) || '{}'); }
  catch (_error) { return {}; }
}

function rememberWorkspaceTab(workspace, tab) {
  const tabs = storedWorkspaceTabs();
  tabs[workspace] = tab;
  localStorage.setItem(WORKSPACE_TAB_STORAGE_KEY, JSON.stringify(tabs));
}

function routeHash(workspace, tab, context = currentContext()) {
  if (workspace === 'home') return '#/home';
  if (workspace === 'instrument') {
    const symbol = context.selection?.symbol || context.route?.params?.symbol || '^TWII';
    return `#/instrument/${encodeURIComponent(symbol)}/${tab}`;
  }
  if (workspace === 'portfolio') {
    const account = context.portfolio?.account_id || 'paper-default';
    return `#/portfolio/${encodeURIComponent(account)}/${tab}`;
  }
  return `#/${workspace}/${tab}`;
}

function parseRouteHash(hash = window.location.hash) {
  const parts = String(hash || '').replace(/^#\/?/, '').split('/').filter(Boolean).map(decodeURIComponent);
  if (!parts.length || parts[0] === 'home') return { workspace: 'home', tab: 'overview', params: {} };
  const workspace = parts[0];
  if (!WORKSPACES[workspace]) return null;
  if (workspace === 'instrument') return { workspace, tab: parts[2] || 'overview', params: { symbol: parts[1] || '^TWII' } };
  if (workspace === 'portfolio') return { workspace, tab: parts[2] || 'overview', params: { account_id: parts[1] || 'paper-default' } };
  return { workspace, tab: parts[1] || WORKSPACES[workspace].defaultTab, params: {} };
}

function writeRouteHash(workspace, tab, mode = 'push') {
  if (applyingLocationRoute || mode === 'none') return;
  const hash = routeHash(workspace, tab);
  if (window.location.hash === hash) return;
  if (mode === 'replace') history.replaceState(null, '', hash);
  else history.pushState(null, '', hash);
}
const STATIC_PAGE_COPY = Object.freeze({
  'market:rankings': ['市場排行', '法人買賣超與最新市場強弱排行', '資料源：法人籌碼與市場快照'],
  'instrument:technical': ['技術分析', '成交量、異常交易與趨勢判讀', '資料源：日 K 與技術指標'],
  'instrument:valuation': ['估值', '基本估值、歷史分位與同業比較', '資料源：財務資料與估值模型'],
  'instrument:events': ['新聞與事件', '公司公告、重大訊息與相關新聞', '資料源：新聞中心與公司事件'],
  'instrument:evidence': ['風險與證據', '風險限制、資料品質及可追溯證據', '資料源：Host 驗證與研究證據'],
  'portfolio:positions': ['持倉與建議', '依完整持倉整理加碼、續抱、減碼或退出條件；模型結果會標示來源與驗證狀態', '資料源：帳務、規則、風控與研究脈絡'],
  'portfolio:simulation': ['模擬帳戶', '真實行情的紙上交易與訓練回合', '資料源：模擬帳務'],
  'portfolio:performance': ['績效', '報酬、回撤與持倉貢獻會以已保存帳務計算', '資料源：本地資產帳務'],
  'research:compare': ['股票比較', '比較清單、共同因子與差異風險', '資料源：共用股票 Context'],
  'research:factor-lab': ['因子研究', '量價、基本面與籌碼因子資料', '資料源：量化研究資料'],
  'research:reports': ['研究報告', '每日市場報告與可追溯研究產物', '資料源：研究報告 API'],
  'system:brokers': ['券商與連線', '連線狀態與授權範圍；不顯示任何金鑰', '資料源：系統連線設定'],
  'system:tools': ['工具與整合', '本機技能、MCP、外部框架與通知狀態', '資料源：Codex 執行核心'],
});

function currentContext() {
  return window.WorkspaceContextStore?.get?.() || {};
}

function selectedSymbol() {
  return currentContext().selection?.symbol || '^TWII';
}

function selectedEquitySymbol() {
  const symbol = String(currentContext().selection?.symbol || '').trim().toUpperCase();
  return /\.(TW|TWO)$/i.test(symbol) && !symbol.startsWith('^') ? symbol : '';
}

function showSelectEquityState(ids, message = '請先搜尋並選擇上市 .TW 或上櫃 .TWO 股票；市場指數不會代替個股送出資料請求。') {
  ids.forEach(id => {
    const target = document.getElementById(id);
    if (target) target.textContent = message;
  });
}

function showOwnershipSelectEquityState() {
  const message = '請先搜尋並選擇上市 .TW 或上櫃 .TWO 股票；市場指數不會代替個股送出資料請求。';
  showSelectEquityState(['chipHistoryStatus', 'shortDaytradeStatus', 'tdccHistoryStatus'], message);

  // ``institutionalTable`` is populated while the market overview boots, but
  // it is also visible on the ownership tab.  Leaving a previous overview
  // timeout in that shared panel makes an expected "no stock selected" state
  // look like an institutional-data failure.  This tab owns the prerequisite,
  // so replace the stale overview result before any stock-specific request can
  // be attempted.
  const table = document.getElementById('institutionalTable');
  if (table && typeof renderEmptyBlock === 'function') {
    table.innerHTML = renderEmptyBlock('請先選擇個股', message);
  }
}

function comparisonApi(path) {
  const target = uiDataApi(path);
  // Long-lived same-origin dashboard streams can exhaust Chromium's per-host
  // HTTP/1.1 connection pool.  `localhost` is the same local runtime, but a
  // separate connection pool; the Host only allows this trusted loopback alias.
  if (window.location.hostname !== '127.0.0.1') return target;
  const url = new URL(target, window.location.origin);
  url.hostname = 'localhost';
  return url.toString();
}

function comparisonApiOptions() {
  const token = document.querySelector('meta[name="stock-ai-runtime-session"]')?.content || '';
  return token ? { headers: { 'X-Stock-AI-Session': token } } : {};
}

function routeTimeout(promise, milliseconds = 6000) {
  let timer = 0;
  return Promise.race([
    Promise.resolve(promise),
    new Promise((_, reject) => { timer = window.setTimeout(() => reject(new Error(`資料服務未在 ${Math.round(milliseconds / 1000)} 秒內回應；已保留可用頁面，請稍後重新整理。`)), milliseconds); }),
  ]).finally(() => window.clearTimeout(timer));
}

function panelSupportsTab(panel, workspace, tab) {
  return panel?.dataset.workspaceParent === workspace
    && String(panel.dataset.workspaceTab || '').split(/\s+/).includes(tab);
}

function isApplicationPanel(panel) {
  return Boolean(panel) && !panel.closest('[data-liquid-sample-clone]');
}

function hasNativePanel(workspace, tab) {
  return [...document.querySelectorAll('[data-workspace-parent]')]
    .some(panel => isApplicationPanel(panel) && panelSupportsTab(panel, workspace, tab));
}

function renderWorkspacePage(workspace, tab, payload = {}, error = null) {
  const panels = [...document.querySelectorAll(`[data-workspace-parent="${CSS.escape(workspace)}"]`)]
    .filter(candidate => isApplicationPanel(candidate) && panelSupportsTab(candidate, workspace, tab));
  const targets = panels.map(panel => panel.querySelector('[data-route-content]')).filter(Boolean);
  if (!targets.length) return panels[0];
  const [title, description, source] = STATIC_PAGE_COPY[`${workspace}:${tab}`] || [tab, '', ''];
  const cards = Array.isArray(payload.cards) ? payload.cards : [];
  const detail = error
    ? `資料載入失敗：${error.message || '請稍後再試。'}`
    : (payload.detail || '資料已同步；可切換其他分頁繼續查看。');
  const content = `<div class="workspace-route-grid">${cards.map(card => `<div class="workspace-route-card"><span>${escapeHtml(card.label)}</span><strong>${escapeHtml(card.value)}</strong><small>${escapeHtml(card.detail || '')}</small></div>`).join('')}</div><div class="event-list"><div class="event"><h4>${error ? '資料狀態' : (title || '已同步')}</h4><p>${escapeHtml(detail)}</p><p>${escapeHtml(source || description)}</p></div></div>`;
  targets.forEach(target => { target.innerHTML = content; });
  return panels[0];
}

function renderWorkspaceFailure(workspace, tab, error) {
  const detail = error?.message || '資料服務暫時無法回應，請稍後重新整理。';
  const title = '資料載入逾時';
  if (workspace === 'research' && tab === 'deep-research') {
    window.renderWorkspaceError?.(['assistantContextBox', 'assistantCardsBox', 'assistantWatchlistBox', 'assistantNotificationBox', 'assistantMetaBox'], title, detail);
  } else if (workspace === 'portfolio' && tab === 'orders') {
    window.renderWorkspaceError?.(['tradingStatusBox', 'tradingEstimateBox', 'tradingMetaBox'], title, detail);
  } else if (workspace === 'portfolio' && tab === 'positions') {
    window.renderWorkspaceError?.(['assetStatusBox', 'assetMetaBox'], title, detail);
  } else if (workspace === 'portfolio' && tab === 'overview') {
    window.renderWorkspaceError?.(['assetStatusBox', 'assetMetaBox'], title, detail);
    const summary = document.getElementById('assetSummaryCards');
    const positions = document.getElementById('assetPositionsTable');
    if (summary) summary.innerHTML = '';
    if (positions) positions.innerHTML = '';
  } else if (workspace === 'portfolio' && tab === 'risk') {
    window.renderWorkspaceError?.(['riskAlertsBox', 'riskMetaBox'], title, detail);
  } else if (workspace === 'system' && tab === 'data-platform') {
    const registryChip = document.getElementById('entityRegistryStatusChip');
    const platformChip = document.getElementById('dataPlatformStatusChip');
    if (registryChip) registryChip.textContent = '暫時無法取得';
    if (platformChip) platformChip.textContent = '暫時無法取得';
    window.renderWorkspaceError?.(
      ['dataPlatformDatasets', 'dataPlatformSources', 'dataSourceObservability', 'dataReconciliationConflicts', 'dataPlatformLineage'],
      title,
      detail,
    );
  }
}

const RESEARCH_COMPARISON_METRICS = Object.freeze([
  ['valuation', 'pe', '本益比 PE', 'x'],
  ['valuation', 'pb', '股價淨值比 PB', 'x'],
  ['valuation', 'dividend_yield_percent', '殖利率', '%'],
  ['operating', 'gross_margin_percent', '毛利率', '%'],
  ['operating', 'operating_margin_percent', '營業利益率', '%'],
  ['operating', 'net_margin_percent', '稅後淨利率', '%'],
  ['operating', 'roe_percent', 'ROE', '%'],
  ['operating', 'debt_ratio_percent', '負債比', '%'],
]);

function normalizedComparisonSymbols(value) {
  return [...new Set(String(value || '').split(/[\s,，]+/)
    .map(symbol => symbol.trim().toUpperCase()).filter(Boolean))].slice(0, 6);
}

function researchComparisonElements() {
  return {
    target: document.getElementById('researchComparisonTarget'),
    peers: document.getElementById('researchComparisonPeers'),
    status: document.getElementById('researchComparisonStatus'),
    candidates: document.getElementById('researchComparisonCandidates'),
    table: document.getElementById('researchComparisonTable'),
  };
}

function formatResearchComparisonValue(value, unit) {
  if (value === null || value === undefined || value === '') return '-';
  const number = Number(value);
  if (!Number.isFinite(number)) return '-';
  return `${number.toLocaleString('zh-TW', { maximumFractionDigits: 4 })}${unit}`;
}

function renderResearchComparison(payload) {
  const { status, candidates, table } = researchComparisonElements();
  if (!status || !candidates || !table) return;
  const selection = payload?.selection || {};
  const companies = Array.isArray(payload?.companies) ? payload.companies : [];
  const rejected = Array.isArray(selection.rejected_peers) ? selection.rejected_peers : [];
  const candidatePreview = (selection.candidate_preview || []).slice(0, 10)
    .map(item => `${item.symbol} ${item.name || ''}`.trim()).join('、');
  status.textContent = `${payload?.symbol || '-'} · 官方產業 ${payload?.industry || '-'} · 接受 ${selection.accepted_peers?.length || 0} 家 · 財報期間 ${payload?.period || '-'}`
    + (rejected.length ? ` · 未納入 ${rejected.map(item => `${item.symbol}（${item.reason}）`).join('、')}` : '');
  candidates.innerHTML = candidatePreview
    ? `<strong>官方同產業候選：</strong>${escapeHtml(candidatePreview)}${companies.length ? '' : '。請從中輸入至少一檔同業後重新比較。'}`
    : '<strong>比較範圍：</strong>只納入使用者明確選擇、且通過官方產業驗證的股票。';
  if (!companies.length) {
    table.innerHTML = '<div class="workspace-loading">尚未取得可比較公司。請輸入至少一檔同業股票；系統不會自動猜測比較對象。</div>';
    return;
  }
  table.innerHTML = `<table><caption>估值採同日交易所資料；財務比率採相同財報期間。資料缺漏以「-」呈現。</caption><thead><tr><th>指標</th>${companies.map(company => `<th>${escapeHtml(company.symbol)}<br><small>${escapeHtml(company.name || '')}</small></th>`).join('')}</tr></thead><tbody>${RESEARCH_COMPARISON_METRICS.map(([section, field, label, unit]) => `<tr><td>${escapeHtml(label)}</td>${companies.map(company => `<td>${formatResearchComparisonValue(company?.[section]?.[field], unit)}</td>`).join('')}</tr>`).join('')}<tr><td>資料日期／期間</td>${companies.map(company => `<td>${escapeHtml(company.valuation_date || '-')}<br><small>${escapeHtml(company.financial_period || '-')}</small></td>`).join('')}</tr></tbody></table>`;
}

async function loadResearchComparison({ refresh = false } = {}) {
  const elements = researchComparisonElements();
  if (!elements.target || !elements.peers || !elements.status) return null;
  const contextSymbols = Array.isArray(currentContext().comparison?.symbols) ? currentContext().comparison.symbols : [];
  const target = String(elements.target.value || contextSymbols[0] || selectedSymbol()).trim().toUpperCase();
  const peers = normalizedComparisonSymbols(elements.peers.value || contextSymbols.filter(symbol => symbol !== target).join(','))
    .filter(symbol => symbol !== target);
  elements.target.value = target;
  elements.peers.value = peers.join(', ');
  if (!target) throw new Error('請先選擇比較基準股票。');
  if (!peers.length) {
    elements.status.textContent = '請輸入至少一檔同業股票後按「開始比較」；系統只比較你明確選擇的標的。';
    if (elements.candidates) elements.candidates.innerHTML = '<strong>比較規則：</strong>將以官方證券主檔驗證同產業，不會自動猜測比較對象。';
    if (elements.table) elements.table.innerHTML = '<div class="workspace-loading">尚未建立比較表。</div>';
    return null;
  }
  elements.status.textContent = '正在以官方證券主檔驗證同業，並讀取同日估值與同期間財務比率…';
  try {
    const params = new URLSearchParams({ symbol: target, peers: peers.join(','), refresh: refresh ? 'true' : 'false' });
    const payload = await routeTimeout(api(
      comparisonApi(`/fundamentals/valuation/peers?${params.toString()}`),
      comparisonApiOptions(),
    ), 15000);
    window.WorkspaceContextStore?.set?.({
      selection: { symbol: target },
      comparison: { symbols: [target, ...peers] },
    }, { reason: 'research:comparison' });
    renderResearchComparison(payload);
    return payload;
  } catch (error) {
    elements.status.textContent = `比較資料暫時無法取得：${error.message || '請稍後再試。'}`;
    if (elements.table) elements.table.innerHTML = '<div class="workspace-loading">未顯示不完整的比較結果。請確認股票代號與資料來源後重試。</div>';
    throw error;
  }
}

async function fetchRouteData(workspace, tab) {
  const symbol = selectedSymbol();
  if (workspace === 'market' && tab === 'rankings') {
    const payload = await api(uiDataApi('/flow/institutional?limit=12'));
    const items = payload.items || [];
    renderWorkspacePage(workspace, tab, { cards: items.slice(0, 6).map(item => ({ label: item.name || item.symbol || '證券', value: String(item.total_institutional_net ?? '-'), detail: item.symbol || '' })), detail: `已載入 ${items.length} 筆法人排行。` });
    return;
  }
  if (workspace === 'instrument' && tab === 'technical') {
    await loadDashboardSymbolDetails?.(symbol);
    const payload = await api(uiDataApi(`/market/${encodeURIComponent(symbol)}/anomalies?limit=8`));
    const items = payload.items || [];
    renderWorkspacePage(workspace, tab, { cards: items.slice(0, 6).map(item => ({ label: item.type || item.label || '技術事件', value: item.severity || '已偵測', detail: item.occurred_at || item.date || symbol })), detail: `已同步 ${symbol} 的 K 線、異常與技術資料。` });
    return;
  }
  if (workspace === 'instrument' && tab === 'valuation') {
    await Promise.all([loadBasicValuation?.(symbol), loadValuationPercentiles?.(symbol, false), loadPeerComparison?.(symbol)]);
    renderWorkspacePage(workspace, tab, { cards: [{ label: '選取標的', value: symbol, detail: '基本估值、歷史分位與同業比較已重新載入。' }] });
    return;
  }
  if (workspace === 'instrument' && tab === 'events') {
    const payload = await api(uiDataApi(`/news/center?symbol=${encodeURIComponent(symbol)}&limit=8`));
    const items = payload.items || [];
    renderWorkspacePage(workspace, tab, { cards: items.slice(0, 5).map(item => ({ label: item.title || item.headline || '新聞事件', value: item.source || '新聞中心', detail: item.published_at || item.time || '' })), detail: `已載入 ${symbol} 的 ${items.length} 則新聞與事件。` });
    // The event route owns a small, bounded payload. Do not block it on the
    // individual-stock detail fan-out (margin, financial history, valuation,
    // notifications and reports); that fan-out can exceed the 6-second route
    // budget even when the news endpoint itself is healthy.
    loadDashboardSymbolDetails?.(symbol).catch?.(() => {});
    return;
  }
  if (workspace === 'instrument' && tab === 'evidence') {
    const payload = await api(`/api/instruments/${encodeURIComponent(symbol)}/evidence`);
    const items = payload.items || [];
    renderWorkspacePage(workspace, tab, { cards: items.slice(0, 6).map(item => ({ label: item.source_name || item.source || '證據', value: item.status || '已驗證', detail: item.observed_at || item.as_of || '' })), detail: `已載入 ${symbol} 的 ${items.length} 筆可追溯證據。` });
    return;
  }
  if (workspace === 'portfolio' && ['overview', 'simulation', 'performance'].includes(tab)) {
    const [summary, positions] = await Promise.all([api('/api/assets/summary?limit=12'), api('/api/assets/positions?limit=12')]);
    const cards = (summary.items || summary.summary || []).slice?.(0, 6).map?.(item => ({ label: item.label || item.name || '帳務', value: String(item.value ?? item.amount ?? '-'), detail: item.detail || '' })) || [];
    renderWorkspacePage(workspace, tab, { cards: cards.length ? cards : [{ label: '持倉筆數', value: String((positions.items || []).length), detail: '資產與部位已由帳務 API 載入。' }], detail: `已載入 ${tab === 'performance' ? '績效計算所需的' : ''}資產與 ${positions.items?.length || 0} 筆部位。` });
    return;
  }
  if (workspace === 'portfolio' && tab === 'positions') {
    const payload = await api(`/api/trading/assistant?symbol=${encodeURIComponent(symbol)}`);
    const cards = (payload.cards || payload.recommendations || []).slice(0, 6).map(item => ({ label: item.label || item.action || '建議', value: item.value || item.symbol || symbol, detail: item.detail || item.rationale || '' }));
    renderWorkspacePage(workspace, tab, { cards, detail: `已依 ${symbol} 與目前持倉脈絡載入建議；建議不會自動下單。` });
    return;
  }
  if (workspace === 'research' && tab === 'compare') {
    return loadResearchComparison();
  }
  if (workspace === 'research' && tab === 'factor-lab') {
    const payload = await api(uiDataApi(`/flow/institutional?limit=8&symbol=${encodeURIComponent(symbol)}`));
    const items = payload.items || [];
    renderWorkspacePage(workspace, tab, { cards: items.slice(0, 6).map(item => ({ label: item.name || item.symbol || symbol, value: String(item.total_institutional_net ?? '-'), detail: '法人／籌碼因子' })), detail: `已載入 ${symbol} 相關的可用因子資料。` });
    return;
  }
  if (workspace === 'research' && tab === 'reports') {
    const payload = await api(uiDataApi('/reports/daily?limit=8'));
    const items = payload.items || payload.reports || [];
    renderWorkspacePage(workspace, tab, { cards: items.slice(0, 6).map(item => ({ label: item.title || item.generated_at || '每日報告', value: item.status || '已建立', detail: item.generated_at || '' })), detail: `已載入 ${items.length} 份研究報告。` });
    return;
  }
  if (workspace === 'system' && ['brokers', 'tools'].includes(tab)) {
    const payload = await api('/api/settings/connections');
    const cards = Object.entries(payload.summary || payload).slice(0, 8).map(([label, value]) => ({ label, value: typeof value === 'object' ? (value.status || value.label || '已設定') : String(value), detail: '' }));
    renderWorkspacePage(workspace, tab, { cards, detail: '連線狀態已由系統設定 API 讀取；敏感資訊不會顯示。' });
    return;
  }
}

async function loadWorkspaceTabData(workspace, tab) {
  try {
    if (workspace === 'home') return window.HomeMarketWorkspace?.load?.({ selectDefault: false });
    if (workspace === 'market' && tab === 'overview') return loadDashboardOverview?.();
    if (workspace === 'market' && tab === 'watchlists') return loadDashboardOverview?.();
    if (workspace === 'market' && tab === 'screener') return window.runScreener?.();
    if (workspace === 'market' && tab === 'monitor') return Promise.all([loadDashboardSymbolDetails?.(selectedSymbol()), loadNotificationCenter?.(selectedSymbol())]);
    if (workspace === 'market' && tab === 'events') return loadNewsCenterView?.();
    if (workspace === 'instrument' && ['overview', 'chart'].includes(tab)) {
      const symbol = selectedEquitySymbol();
      return symbol
        ? Promise.all([window.HomeMarketWorkspace?.load?.({ selectDefault: false }), loadDashboardSymbolDetails?.(symbol)])
        : window.HomeMarketWorkspace?.load?.({ selectDefault: false });
    }
    if (workspace === 'instrument' && tab === 'technical') {
      const symbol = selectedEquitySymbol();
      if (!symbol) {
        showSelectEquityState(['tradingAnomalyStatus', 'liquidityBox']);
        return;
      }
      return loadDashboardSymbolDetails?.(symbol);
    }
    if (workspace === 'instrument' && tab === 'ownership') {
      if (!selectedEquitySymbol()) {
        showOwnershipSelectEquityState();
        return;
      }
      return Promise.all([loadChipHistory?.(), loadShortDaytradeHistory?.(), loadTdccHoldingHistory?.()]);
    }
    if (workspace === 'instrument' && ['financials', 'valuation'].includes(tab)) {
      const symbol = selectedEquitySymbol();
      if (!symbol) {
        showSelectEquityState([
          'industryMetricsStatus', 'financialGuidanceStatus', 'financialAnomalyStatus',
          'basicValuationStatus', 'valuationPercentileStatus', 'peerComparisonStatus',
          'dcfValuationStatus', 'valuationPolicyStatus', 'monthlyRevenueHistoryStatus',
          'incomeStatementHistoryStatus', 'balanceSheetHistoryStatus', 'cashFlowHistoryStatus',
          'financialRatioHistoryStatus', 'growthHistoryStatus', 'financialRevisionHistoryStatus',
        ]);
        return;
      }
      return loadDashboardSymbolDetails?.(symbol);
    }
    if (workspace === 'instrument' && ['events', 'evidence'].includes(tab)) {
      if (!selectedEquitySymbol()) {
        renderWorkspacePage(workspace, tab, {
          detail: '請先搜尋並選擇上市 .TW 或上櫃 .TWO 股票；市場指數不會代替個股送出資料請求。',
        });
        return;
      }
      return fetchRouteData(workspace, tab);
    }
    if (workspace === 'portfolio' && tab === 'overview') return loadAssetsView?.();
    if (workspace === 'portfolio' && tab === 'orders') return loadTradingView?.();
    if (workspace === 'portfolio' && tab === 'positions') return loadAssetsView?.();
    if (workspace === 'portfolio' && tab === 'simulation') return window.__paperTradingUI?.loadAccount?.({ refreshPrices: false });
    if (workspace === 'portfolio' && tab === 'risk') return loadRiskView?.();
    if (workspace === 'research' && tab === 'deep-research') return loadAssistantView?.();
    if (workspace === 'research' && tab === 'compare') return loadResearchComparison();
    if (workspace === 'research' && tab === 'strategy-lab') return window.loadOpenStockResearch?.();
    if (workspace === 'research' && tab === 'relationships') return window.runLinkage?.();
    if (workspace === 'system' && tab === 'agent-models') {
      // Model runtime and Codex diagnostics belong solely to Agent／模型.
      // They may be slow, so do not block the tab switch itself.
      loadSystemAgentPage?.(false)?.catch?.(() => {});
      loadAgentRuntimeSettings?.()?.catch?.(() => {});
      return;
    }
    if (workspace === 'system' && tab === 'tools') return loadSystemSkillsPage?.();
    if (workspace === 'system' && tab === 'brokers') return Promise.all([
      loadSystemConnectionsPage?.(),
      loadBrokerConnections?.(),
    ]);
    if (workspace === 'system' && tab === 'securities') return loadEntities?.();
    if (workspace === 'system' && tab === 'data-platform') return loadCatalog?.();
    return fetchRouteData(workspace, tab);
  } catch (error) {
    if (STATIC_PAGE_COPY[`${workspace}:${tab}`]) renderWorkspacePage(workspace, tab, {}, error);
    throw error;
  }
}

async function loadViewData(view) {
  const workspace = view;
  const tab = currentContext().route?.workspace === workspace
    ? currentContext().route?.tab
    : WORKSPACES[workspace]?.defaultTab;
  return loadWorkspaceTabData(workspace, tab);
}

function isOfficialEventItem(item) {
  if (!item) return false;
  if (item.official_verified) return true;
  if (['material_event', 'announcement'].includes(String(item.event_type || '').toLowerCase())) return true;
  return /(?:openapi\.)?twse\.com\.tw|mops\.twse\.com\.tw/i.test(String(item.source_url || ''));
}

const OVERVIEW_INDEX_SPECS = [
  { symbol: '^TWII', label: '加權指數' },
  { symbol: '^TWOII', label: '櫃買指數' },
  { symbol: 'TX=F', label: '台指期' },
  { symbol: '^DJI', label: '道瓊' },
  { symbol: '^IXIC', label: 'NASDAQ' },
  { symbol: '^GSPC', label: 'S&P 500' },
  { symbol: '^SOX', label: '費半' },
];

const NEWS_CATEGORY_OPTIONS = [
  { value: 'all', label: '全部' },
  { value: 'company', label: '公司' },
  { value: 'industry', label: '產業' },
  { value: 'international', label: '國際' },
  { value: 'macro', label: '總經' },
  { value: 'policy', label: '政策法規' },
  { value: 'analyst', label: '分析師' },
];

function newsCategoryLabel(category) {
  const key = String(category || '').toLowerCase();
  return NEWS_CATEGORY_OPTIONS.find(item => item.value === key)?.label || key || '全部';
}

function sourcePriorityLabel(item) {
  const category = String(item?.category || '').toLowerCase();
  if (category === 'realtime_quote') return '第 1 層 即時/交易資料';
  if (['official_reference', 'fundamentals_and_events'].includes(category)) return '第 2 層 官方公開資料';
  return '第 3 層 輔助資料';
}

function renderWorkspaceTabs(workspace, activeTab = WORKSPACES[workspace]?.defaultTab) {
  const config = WORKSPACES[workspace] || WORKSPACES.home;
  const target = document.getElementById('workspaceTabs');
  if (!target) return;
  target.innerHTML = (config.tabs || []).map(([tab, label]) => `<button type="button" class="workspace-tab${tab === activeTab ? ' is-active' : ''}" data-workspace-tab="${tab}">${label}</button>`).join('');
  target.querySelectorAll('[data-workspace-tab]').forEach(button => button.addEventListener('click', () => setWorkspaceTab(workspace, button.dataset.workspaceTab)));
}

function setWorkspaceTab(workspace, tab = WORKSPACES[workspace]?.defaultTab, { historyMode = 'push', load = true } = {}) {
  const config = WORKSPACES[workspace] || WORKSPACES.home;
  const activeTab = config.tabs?.some(([key]) => key === tab) ? tab : config.defaultTab;
  const view = document.getElementById(workspace);
  const nativeTabs = String(view?.dataset.nativeTabs || view?.dataset.defaultTab || 'overview').split(/\s+/);
  const showNativeView = workspace === 'home' || nativeTabs.includes(activeTab);
  if (view) view.classList.toggle('workspace-route-hidden', !showNativeView);
  document.querySelectorAll('.workspace-panel[data-workspace-parent]').forEach(panel => {
    if (!isApplicationPanel(panel)) return;
    panel.classList.toggle('is-active', panelSupportsTab(panel, workspace, activeTab));
  });
  if (!showNativeView && !hasNativePanel(workspace, activeTab)) console.error(`Missing fixed workspace page: ${workspace}/${activeTab}`);
  renderWorkspaceTabs(workspace, activeTab);
  document.documentElement.dataset.workspace = workspace;
  document.documentElement.dataset.workspaceTab = activeTab;
  if (workspace === 'system') {
    const copy = {
      securities: ['證券主檔', '搜尋、檢視與維護台股證券主檔、識別資料及生命週期。'],
      'data-platform': ['資料平台', '檢視來源、品質、快取、資料血緣、匯入與衝突狀態。'],
      'agent-models': ['Agent 與模型', '選擇模型提供者、檢查能力與連線，並調整 Agent 的安全執行設定。'],
      tools: ['工具與整合', '檢視 Skills、MCP、外部框架與通知服務；敏感權限仍由執行核心管理。'],
      brokers: ['券商與連線', '管理券商能力、帳戶授權與連線健康狀態；真實下單維持關閉。'],
      interface: ['介面與一般', '調整主題、背景、資訊密度、語言與無障礙設定。'],
    }[activeTab];
    if (copy) {
      const title = document.getElementById('settingsPageTitle');
      const subtitle = document.getElementById('settingsPageSubtitle');
      if (title) title.textContent = copy[0];
      if (subtitle) subtitle.textContent = copy[1];
    }
    // Keep the page-head chip in the same information domain as its tab.
    // In particular, Agent／模型 must not visually imply that theme or
    // backdrop controls belong to model configuration.
    const profileLabel = {
      securities: '證券主檔與識別',
      'data-platform': '資料品質與血緣',
      brokers: '連線與授權狀態',
      'agent-models': '模型與執行狀態',
      tools: '工具與橋接狀態',
      interface: '介面外觀',
    }[activeTab];
    const profileChip = document.getElementById('settingsProfileChip');
    if (profileChip && profileLabel) profileChip.textContent = profileLabel;
  }
  const routeParams = workspace === 'instrument'
    ? { symbol: selectedSymbol() }
    : (workspace === 'portfolio' ? { account_id: currentContext().portfolio?.account_id || 'paper-default' } : {});
  window.WorkspaceContextStore?.set?.({
    route: { workspace, tab: activeTab, params: routeParams },
  }, { reason: `workspace:${workspace}:${activeTab}` });
  rememberWorkspaceTab(workspace, activeTab);
  writeRouteHash(workspace, activeTab, historyMode);
  if (!load) return activeTab;
  const loadBudget = workspace === 'system' && activeTab === 'data-platform' ? 15_000 : 6_000;
  window.__workspaceTabLoad = routeTimeout(loadWorkspaceTabData(workspace, activeTab), loadBudget).catch(error => {
    if (STATIC_PAGE_COPY[`${workspace}:${activeTab}`]) renderWorkspacePage(workspace, activeTab, {}, error);
    renderWorkspaceFailure(workspace, activeTab, error);
    // A bounded route timeout has already rendered a usable, explicit
    // unavailable state. Keep it out of the browser error channel so a slow
    // optional data provider is not reported as an application failure.
    console.warn(`Workspace route ${workspace}/${activeTab} is unavailable`, error);
    return null;
  });
  return activeTab;
}

function setView(view, { tab = null, historyMode = 'push' } = {}) {
  const workspace = view;
  const previousWorkspace = window.__activeWorkspace || '';
  window.__activeWorkspace = workspace;
  // Home and 個股 render from one Workspace Context. The individual chart
  // component stays in 個股; Home uses its own context-driven summary surface.
  window.HomeMarketWorkspace?.setSurface?.(workspace);
  document.querySelectorAll('body > .app-shell .view').forEach(v => v.classList.toggle('active', v.id === workspace));
  document.querySelectorAll('.sidebar .nav-btn').forEach(b => b.classList.toggle('active', b.dataset.view === workspace));
  const context = currentContext();
  // A new workspace visit should use its designed landing tab.  In particular,
  // Research starts with 股票比較 so stale model-page requests cannot delay the
  // comparison workflow.  Preserve a tab only for a same-workspace refresh.
  const rememberedTab = tab
    || storedWorkspaceTabs()[workspace]
    || (previousWorkspace === workspace && context.route?.workspace === workspace ? context.route?.tab : null);
  setWorkspaceTab(
    workspace,
    rememberedTab || document.getElementById(workspace)?.dataset.defaultTab || WORKSPACES[workspace]?.defaultTab,
    { historyMode },
  );
  window.__glassSamplerController?.scheduleCapture?.(20);
  const main = document.querySelector('body > .app-shell > .main');
  if (main) main.scrollTop = 0;
  window.scrollTo({ top: 0, left: 0, behavior: 'auto' });
  const btn = document.querySelector(`[data-view="${workspace}"]`);
  const config = WORKSPACES[workspace];
  if (btn) {
    const textLabel = [...btn.childNodes]
      .filter(node => node.nodeType === Node.TEXT_NODE)
      .map(node => node.textContent.trim())
      .join('')
      .trim();
    $('viewTitle').textContent = config?.title || btn.dataset.title || textLabel || btn.textContent.trim();
  }
  if ($('viewSubtitle')) $('viewSubtitle').textContent = config?.subtitle || '';
}
window.setView = setView;
window.setWorkspaceTab = setWorkspaceTab;
function applyLocationRoute() {
  const parsed = parseRouteHash();
  if (!parsed) return setView('home', { historyMode: 'replace' });
  applyingLocationRoute = true;
  try {
    if (parsed.params.symbol) {
      window.WorkspaceContextStore?.set?.({
        selection: { symbol: parsed.params.symbol },
        route: parsed,
      }, { persist: false, reason: 'route:instrument' });
    } else if (parsed.params.account_id) {
      window.WorkspaceContextStore?.set?.({
        portfolio: { account_id: parsed.params.account_id },
        route: parsed,
      }, { persist: false, reason: 'route:portfolio' });
    }
    setView(parsed.workspace, { tab: parsed.tab, historyMode: 'none' });
  } finally {
    applyingLocationRoute = false;
  }
}

window.addEventListener('hashchange', applyLocationRoute);
window.WorkspaceNavigation = {
  setView, setWorkspaceTab, loadWorkspaceTabData, loadResearchComparison,
  parseRouteHash, routeHash, applyLocationRoute, workspaces: WORKSPACES,
};
// Deep links must be applied only after every classic feature script below
// navigation.js has registered its loader (for example loadCatalog). A
// microtask runs before the next script tag and produced false "not defined"
// failures on direct routes.
window.addEventListener('DOMContentLoaded', applyLocationRoute, { once: true });

document.getElementById('researchComparisonForm')?.addEventListener('submit', (event) => {
  event.preventDefault();
  loadResearchComparison().catch(error => console.warn('Research comparison is unavailable', error));
});
