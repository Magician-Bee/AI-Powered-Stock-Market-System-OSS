function currentViewId() {
  return window.WorkspaceContextStore?.get?.().route.workspace || 'home';
}

function normalizeWorkspaceSymbol(value) {
  const candidates = [
    value,
    window.WorkspaceContextStore?.get?.().selection.symbol,
    state.currentEntity?.symbol,
    state.symbol,
  ];
  const symbol = candidates
    .map(candidate => String(candidate || '').trim().toUpperCase())
    .find(candidate => /\.(TW|TWO)$/i.test(candidate) && !candidate.startsWith('^'));
  return symbol || '';
}

function normalizeLots(value) {
  const lots = Math.max(1, Math.min(999, Number.parseInt(String(value || '1'), 10) || 1));
  return lots;
}

function syncWorkspaceControls() {
  const symbol = normalizeWorkspaceSymbol(state.currentEntity?.symbol || state.symbol || '');
  ['tradingSymbol', 'assistantSymbol', 'openStockSymbol', 'riskSymbol'].forEach((id) => {
    const input = $(id);
    if (input && !String(input.value || '').trim()) input.value = symbol;
  });
  if ($('tradingSymbol')) $('tradingSymbol').value = normalizeWorkspaceSymbol($('tradingSymbol').value || symbol);
  if ($('assistantSymbol')) $('assistantSymbol').value = normalizeWorkspaceSymbol($('assistantSymbol').value || symbol);
  if ($('openStockSymbol')) $('openStockSymbol').value = normalizeWorkspaceSymbol($('openStockSymbol').value || symbol);
  if ($('riskSymbol')) $('riskSymbol').value = normalizeWorkspaceSymbol($('riskSymbol').value || symbol);
  if ($('tradingSide')) $('tradingSide').value = state.tradeSide;
  if ($('riskSide')) $('riskSide').value = state.tradeSide;
  if ($('tradingLots')) $('tradingLots').value = String(state.tradeQuantityLots);
  if ($('riskLots')) $('riskLots').value = String(state.tradeQuantityLots);
}

function updateTradeState({ symbol, side, quantityLots } = {}) {
  if (symbol) state.symbol = normalizeWorkspaceSymbol(symbol);
  if (side === 'buy' || side === 'sell') state.tradeSide = side;
  if (quantityLots !== undefined) state.tradeQuantityLots = normalizeLots(quantityLots);
  syncWorkspaceControls();
  if (symbol) {
    window.WorkspaceContextStore?.set?.({ selection: { symbol: state.symbol } }, { reason: 'trading:symbol' });
  }
}

function getTradeParams(prefix) {
  const symbol = normalizeWorkspaceSymbol($(`${prefix}Symbol`)?.value);
  const side = ($(`${prefix}Side`)?.value === 'sell' ? 'sell' : 'buy');
  const quantityLots = normalizeLots($(`${prefix}Lots`)?.value ?? state.tradeQuantityLots);
  updateTradeState({ symbol, side, quantityLots });
  return { symbol, side, quantityLots };
}

function getAssistantSymbol() {
  const symbol = normalizeWorkspaceSymbol($('assistantSymbol')?.value);
  updateTradeState({ symbol });
  return symbol;
}

function formatMoney(value) {
  const amount = Number(value);
  if (!Number.isFinite(amount)) return '-';
  return `NT$ ${amount.toLocaleString('zh-TW', { maximumFractionDigits: 2 })}`;
}

function formatSignedMoney(value) {
  const amount = Number(value);
  if (!Number.isFinite(amount)) return '-';
  const sign = amount > 0 ? '+' : '';
  return `${sign}${formatMoney(amount).replace('NT$ ', 'NT$ ')}`;
}

function formatPercent(value, digits = 2) {
  const amount = Number(value);
  if (!Number.isFinite(amount)) return '-';
  return `${amount.toFixed(digits)}%`;
}

function formatSignedPercent(value, digits = 2) {
  const amount = Number(value);
  if (!Number.isFinite(amount)) return '-';
  return `${amount > 0 ? '+' : ''}${amount.toFixed(digits)}%`;
}

function actionBiasLabel(action) {
  const key = String(action || '').toLowerCase();
  if (key === 'buy') return '偏多';
  if (key === 'watch') return '觀察';
  if (key === 'reduce') return '減碼';
  if (key === 'avoid') return '迴避';
  return '持有/等待';
}

function sessionLabel(session) {
  const key = String(session || '').toLowerCase();
  if (key === 'pre_market') return '盤前觀察';
  if (key === 'intraday') return '盤中提醒';
  if (key === 'post_market' || key === 'after_market') return '收盤檢討';
  return key || '工作區';
}

function openStockStatusLabel(value) {
  const key = String(value || '').toLowerCase();
  const labels = {
    ready: '就緒',
    active: '啟用',
    partial: '部分資料',
    planned: '規劃中',
    passed: '通過',
    review: '需檢視',
    approved: '已核准',
    blocked: '已阻擋',
    idle: '待命',
    running: '執行中',
    valid: '有效',
    verified: '已驗證',
    buy: '買進',
    sell: '賣出',
    add: '加碼',
    reduce: '減碼',
    hold: '持有',
    avoid: '迴避',
    neutral: '中性',
    positive: '正向',
    negative: '負向',
    up: '上行',
    down: '下行',
    flat: '持平',
    low: '低',
    medium: '中',
    high: '高',
    paper: '模擬',
    signal: '訊號',
    blocked_by_risk: '風控阻擋',
    validation_failed: '驗證未通過',
    bullish: '偏多',
    bearish: '偏空',
    placeholder: '占位資料',
    metadata_only: '僅中繼資料',
    legacy: '舊版',
  };
  return labels[key] || value || '-';
}

function openStockStatusHtml(value) {
  return escapeHtml(openStockStatusLabel(value));
}

function alertLevelClass(level) {
  if (level === 'block') return 'negative';
  if (level === 'warning') return 'neutral';
  return 'positive';
}

function renderWorkspaceCard(label, value, detail = '', tone = '') {
  return `<div class="mini-card ${tone}"><span>${escapeHtml(label)}</span><strong>${value}</strong>${detail ? `<small>${escapeHtml(detail)}</small>` : ''}</div>`;
}

function renderSimpleList(items, emptyText = '暫無資料') {
  const values = (items || []).filter(Boolean);
  if (!values.length) return `<p>${escapeHtml(emptyText)}</p>`;
  return `<ul class="workspace-list">${values.map(item => `<li>${escapeHtml(String(item))}</li>`).join('')}</ul>`;
}

function renderMetaEvents(meta, extraItems = []) {
  if (!meta && !extraItems.length) return renderEmptyBlock('尚無資料來源資訊', '等待 API 回傳。');
  const blocks = [];
  if (meta) {
    blocks.push(`
      <div class="event">
        <h4>資料來源與時間</h4>
        <p>產生時間：${escapeHtml(meta.generated_at || '-')}</p>
        <p>來源：${escapeHtml((meta.data_sources || []).join('、') || '整理中')}</p>
      </div>`);
    blocks.push(`
      <div class="event">
        <h4>限制與 fallback</h4>
        ${renderSimpleList(meta.limitations, '目前沒有額外限制說明。')}
        <p>Fallback：${escapeHtml(JSON.stringify(meta.fallback || {}, null, 0) || '{}')}</p>
      </div>`);
  }
  extraItems.forEach(item => blocks.push(item));
  return blocks.join('');
}

function renderRiskAlerts(alerts, emptyTitle = '目前沒有風控警示') {
  const items = alerts || [];
  if (!items.length) return renderEmptyBlock(emptyTitle, '目前這筆預覽沒有觸發額外 block/warning。');
  return items.map(alert => `
    <div class="event workspace-alert ${alert.level}">
      <h4>${escapeHtml(alert.title)} <span class="tag ${alertLevelClass(alert.level)}">${escapeHtml(alert.level)}</span></h4>
      <p>${escapeHtml(alert.message)}</p>
      <p>目前值 ${alert.metric_value ?? '-'} / 閾值 ${alert.threshold_value ?? '-'}</p>
    </div>`).join('');
}

function renderPositionTable(targetId, positions, emptyTitle = '尚無部位') {
  const box = $(targetId);
  if (!box) return;
  const rows = (positions || []).map(item => `
    <div class="row">
      <div>${escapeHtml(item.name || item.symbol)}<br/><small>${escapeHtml(item.symbol || '-')} / ${escapeHtml(item.industry || '未分類')}</small></div>
      <div>${fmt(item.quantity_shares)} 股<br/><small>成本 ${formatMoney(item.average_cost)}</small></div>
      <div>${formatMoney(item.market_value)}<br/><small>${formatPercent(item.weight_percent)}</small></div>
      <div class="${twColor(item.unrealized_pnl || 0)}">${formatSignedMoney(item.unrealized_pnl)}<br/><small>${formatSignedPercent(item.unrealized_pnl_percent)}</small></div>
    </div>`).join('');
  box.innerHTML = `<div class="row header"><div>部位</div><div>股數 / 成本</div><div>市值 / 佔比</div><div>未實現損益</div></div>${rows || renderEmptyBlock(emptyTitle, '目前沒有可渲染的部位資料。')}`;
}

function renderTradingWorkspace(workspace, riskPayload = null, assetSummaryPayload = null, assetPositionsPayload = null) {
  $('tradingStatusBox').innerHTML = `
    <div class="event">
      <h4>${escapeHtml(workspace.broker_status.provider_name || '券商連線狀態')}</h4>
      <p>模式：${escapeHtml(workspace.broker_status.mode || '-')} · ${workspace.broker_status.connected ? '已連線' : '未連線'} · ${workspace.broker_status.can_submit_orders ? '可送單' : '不可送單'}</p>
      <p>${escapeHtml(workspace.broker_status.note || workspace.preview.limitation_note || '目前僅提供唯讀預覽。')}</p>
    </div>`;
  $('tradingPreviewCards').innerHTML = [
    renderWorkspaceCard('標的 / 方向', `${escapeHtml(workspace.preview.name)}<br/>${escapeHtml(workspace.preview.symbol)} / ${workspace.preview.side === 'buy' ? '買進' : '賣出'}`, `${fmt(workspace.preview.quantity_lots)} 張 / ${fmt(workspace.preview.quantity_shares)} 股`),
    renderWorkspaceCard('參考價 / 預估成交', `${formatMoney(workspace.preview.reference_price)}<br/>${formatMoney(workspace.preview.estimated_fill_price)}`, '唯讀預覽價格'),
    renderWorkspaceCard('預估總額', formatMoney(workspace.preview.estimated_costs?.estimated_total), `手續費 ${formatMoney(workspace.preview.estimated_costs?.estimated_fee)} / 證交稅 ${formatMoney(workspace.preview.estimated_costs?.estimated_tax)}`),
    renderWorkspaceCard('可用現金變化', `${formatMoney(workspace.preview.available_cash_before)}<br/>${formatMoney(workspace.preview.available_cash_after)}`, '下單能力未開放'),
  ].join('');
  $('tradingEstimateBox').innerHTML = `
    <div class="event">
      <h4>委託預覽流程</h4>
      <p>成交金額 ${formatMoney(workspace.preview.estimated_costs?.gross_amount)}；預估總額 ${formatMoney(workspace.preview.estimated_costs?.estimated_total)}。</p>
      <p>${escapeHtml(workspace.preview.limitation_note || '未接券商 API 前，僅可檢視預覽與成本估算。')}</p>
    </div>
    ${renderRiskAlerts(workspace.risk_summary?.alerts || [], '交易預覽警示')}`;

  const riskSummary = riskPayload?.summary || workspace.risk_summary;
  const assetSummary = assetSummaryPayload?.summary || workspace.asset_summary;
  $('tradingLinkedSummary').innerHTML = [
    renderWorkspaceCard('K 單筆風險', formatPercent(riskSummary?.single_trade_risk_percent), `上限 ${formatPercent(riskSummary?.max_single_trade_risk_percent)}`, riskSummary?.order_allowed ? '' : 'warn'),
    renderWorkspaceCard('K 單日風險', formatPercent(riskSummary?.daily_risk_percent), `上限 ${formatPercent(riskSummary?.max_daily_risk_percent)}`),
    renderWorkspaceCard('K 部位集中度', formatPercent(riskSummary?.position_concentration_percent), `上限 ${formatPercent(riskSummary?.max_position_concentration_percent)}`),
    renderWorkspaceCard('L 總資產', formatMoney(assetSummary?.total_assets), `現金 ${formatMoney(assetSummary?.cash_available)}`),
    renderWorkspaceCard('L 持股市值', formatMoney(assetSummary?.holdings_market_value), `今日損益 ${formatSignedMoney(assetSummary?.today_pnl)}`),
    renderWorkspaceCard('L 預覽限制', escapeHtml(assetSummary?.note || '本機預覽資產資料'), '未接券商 API'),
  ].join('');
  renderPositionTable('tradingPositionsTable', assetPositionsPayload?.items || workspace.positions, '交易頁引用的資產部位暫時沒有資料。');
  $('tradingMetaBox').innerHTML = renderMetaEvents(workspace.meta, [
    `<div class="event"><h4>I 頁面限制</h4><p>委託流程目前固定為預覽模式；${escapeHtml(workspace.preview.fallback_fields?.broker_submission || 'disabled')}。</p></div>`,
    `<div class="event"><h4>K/L 串接狀態</h4><p>風控摘要 ${riskPayload ? '已同步' : '使用交易 API 內嵌 fallback'}；資產摘要 ${assetSummaryPayload ? '已同步' : '使用交易 API 內嵌 fallback'}。</p></div>`,
  ]);
}

function renderAssistantWorkspace(workspace) {
  const stockContext = state.currentEntity?.symbol || state.realtimeQuote?.symbol;
  const snapshot = workspace.market_snapshot || {};
  const reportTitle = neutralResearchText(workspace.report_title || '每日市場研究報告');
  $('assistantSummaryCards').innerHTML = [
    renderWorkspaceCard('聚焦標的', `${escapeHtml(workspace.focus_symbol || '-')}`, escapeHtml(reportTitle)),
    renderWorkspaceCard('個股頁脈絡', escapeHtml(stockContext || '尚未開啟個股頁'), state.currentEntity?.name || '可從 C 個股頁帶入'),
    renderWorkspaceCard('盤中快照', `${snapshot.source ? escapeHtml(snapshot.source) : '整理中'}<br/>${formatSignedPercent(snapshot.change_percent)}`, snapshot.symbol || workspace.focus_symbol),
    renderWorkspaceCard('通知 / 自選', `${fmt(workspace.notification_previews?.length || 0)} 則通知<br/>${fmt(workspace.watchlist_items?.length || 0)} 檔自選`, 'J 頁整合 daily / watchlist / notifications'),
  ].join('');
  $('assistantContextBox').innerHTML = `
    <div class="event">
      <h4>J 頁跨模組脈絡</h4>
      <p>每日報告：${escapeHtml(reportTitle)}；自選股：${fmt(workspace.watchlist_items?.length || 0)} 檔；通知預覽：${fmt(workspace.notification_previews?.length || 0)} 則。</p>
      <p>目前個股頁：${escapeHtml(state.currentEntity?.name || '未開啟')} ${escapeHtml(state.currentEntity?.symbol || '')}${state.realtimeQuote ? `；即時價格 ${niceNumber(currentDisplayPrice(state.realtimeQuote))}` : ''}</p>
    </div>`;
  $('assistantCardsBox').innerHTML = (workspace.cards || []).map(card => `
    <div class="event">
      <h4>${sessionLabel(card.session)}: ${escapeHtml(card.name)} ${escapeHtml(card.symbol)} <span class="tag ${card.action_bias === 'buy' ? 'positive' : card.action_bias === 'watch' ? 'neutral' : 'negative'}">${actionBiasLabel(card.action_bias)} · 規則分數 ${Number(card.rule_score || 0).toFixed(3)}</span></h4>
      <p>技術面：${escapeHtml((card.technical_reasons || []).map(neutralResearchText).join('；') || '暫無')}</p>
      <p>籌碼面：${escapeHtml((card.flow_reasons || []).map(neutralResearchText).join('；') || '暫無')}</p>
      <p>基本面：${escapeHtml((card.fundamental_reasons || []).map(neutralResearchText).join('；') || '暫無')}</p>
      <p>事件面：${escapeHtml((card.event_reasons || []).map(neutralResearchText).join('；') || '暫無')}</p>
      <p>風險面：${escapeHtml((card.risk_reasons || []).map(neutralResearchText).join('；') || '暫無')}</p>
      <p>資料來源：${escapeHtml((card.data_sources || []).map(sourceDisplayName).join('、') || '整理中')} · 時間 ${escapeHtml(card.as_of || '-')}</p>
      ${card.conflict_note ? `<p class="workspace-note warning">資料衝突：${escapeHtml(card.conflict_note)}</p>` : ''}
    </div>`).join('') || renderEmptyBlock('尚無交易卡片', '目前沒有可用建議。');
  $('assistantWatchlistBox').innerHTML = (workspace.watchlist_items || []).slice(0, 5).map(item => `
    <div class="event">
      <h4>${escapeHtml(item.name || item.symbol || '自選股')}</h4>
      <p>${escapeHtml(item.symbol || '-')} · 提醒 ${escapeHtml((item.alert_flags || []).join('、') || '觀察中')}</p>
      <p>最新價 ${item.latest_price === undefined || item.latest_price === null ? '-' : formatMoney(item.latest_price)} · 漲跌幅 ${formatSignedPercent(item.change_percent)}</p>
    </div>`).join('') || renderEmptyBlock('尚無自選股摘要', 'J 頁稍後再試。');
  $('assistantNotificationBox').innerHTML = (workspace.notification_previews || []).map(item => `
    <div class="event">
      <h4>${escapeHtml(item.title || '通知預覽')}</h4>
      <p>${escapeHtml(item.body || '')}</p>
      <p>通道 ${escapeHtml((item.channels || []).join('、') || '-')} · 類型 ${escapeHtml(item.category || '-')} · dry-run ${item.dry_run ? '是' : '否'}</p>
    </div>`).join('') || renderEmptyBlock('尚無通知預覽', '目前沒有可顯示的通知文案。');
  $('assistantMetaBox').innerHTML = renderMetaEvents(workspace.meta);
}

function renderRiskWorkspace(workspace) {
  const summary = workspace.summary;
  $('riskSummaryCards').innerHTML = [
    renderWorkspaceCard('單筆風險', formatPercent(summary.single_trade_risk_percent), `上限 ${formatPercent(summary.max_single_trade_risk_percent)}`, summary.single_trade_risk_percent > summary.max_single_trade_risk_percent ? 'warn' : ''),
    renderWorkspaceCard('單日風險', formatPercent(summary.daily_risk_percent), `上限 ${formatPercent(summary.max_daily_risk_percent)}`),
    renderWorkspaceCard('部位集中度', formatPercent(summary.position_concentration_percent), `上限 ${formatPercent(summary.max_position_concentration_percent)}`),
    renderWorkspaceCard('產業曝險', formatPercent(summary.industry_exposure_percent), `上限 ${formatPercent(summary.max_industry_exposure_percent)}`),
    renderWorkspaceCard('委託結果', summary.order_allowed ? '可保留為預覽' : '不可執行', `${escapeHtml(workspace.preview_symbol)} / ${workspace.preview_side === 'buy' ? '買進' : '賣出'}`),
  ].join('');
  $('riskAlertsBox').innerHTML = renderRiskAlerts(summary.alerts, '風控規則尚未觸發警示');
  renderPositionTable('riskPositionsTable', workspace.position_snapshot, '風控頁沒有可用部位快照。');
  $('riskMetaBox').innerHTML = renderMetaEvents(workspace.meta);
}

function renderAssetWorkspace(summaryPayload, positionsPayload) {
  const summary = summaryPayload.summary || {};
  $('assetSummaryCards').innerHTML = [
    renderWorkspaceCard('總資產', formatMoney(summary.total_assets), 'L 頁為整體資產總覽'),
    renderWorkspaceCard('可用現金', formatMoney(summary.cash_available), '未接券商 API 前為本機預覽'),
    renderWorkspaceCard('持股市值', formatMoney(summary.holdings_market_value), `今日損益 ${formatSignedMoney(summary.today_pnl)}`),
    renderWorkspaceCard('未實現損益', formatSignedMoney(summary.unrealized_pnl), `已實現 ${formatSignedMoney(summary.realized_pnl)}`),
    renderWorkspaceCard('股利收入', formatMoney(summary.dividend_income), `部位數 ${fmt(summaryPayload.positions_count ?? positionsPayload.count ?? 0)}`),
  ].join('');
  $('assetStatusBox').innerHTML = `
    <div class="event">
      <h4>資產狀態</h4>
      <p>${escapeHtml(summary.note || '本機預覽資產摘要，待正式券商 API 接入後替換。')}</p>
      <p>目前個股脈絡：${escapeHtml(state.currentEntity?.name || '-')} ${escapeHtml(state.currentEntity?.symbol || state.symbol || '-')}</p>
    </div>`;
  renderPositionTable('assetPositionsTable', positionsPayload.items, '資產頁尚無部位資料。');
  $('assetMetaBox').innerHTML = renderMetaEvents(summaryPayload.meta || positionsPayload.meta, [
    `<div class="event"><h4>L 頁面限制</h4><p>部位、已實現損益與股利收入目前為預覽或推估值，不代表正式券商帳務。</p></div>`,
  ]);
}

function renderWorkspaceError(targetIds, title, detail) {
  targetIds.forEach((id) => {
    const box = $(id);
    if (box) box.innerHTML = renderEmptyBlock(title, detail);
  });
}

async function loadTradingView() {
  const { symbol, side, quantityLots } = getTradeParams('trading');
  renderWorkspaceError(['tradingStatusBox', 'tradingEstimateBox', 'tradingMetaBox'], '載入中', '正在整理交易預覽、風控與資產摘要...');
  $('tradingPreviewCards').innerHTML = '';
  $('tradingLinkedSummary').innerHTML = '';
  $('tradingPositionsTable').innerHTML = '';
  if (!symbol) {
    renderWorkspaceError(
      ['tradingStatusBox', 'tradingEstimateBox', 'tradingMetaBox'],
      '尚未指定股票',
      '請先輸入股票代號；未指定標的時不會建立交易預覽。',
    );
    return;
  }
  const [previewResult, riskResult, assetSummaryResult, assetPositionsResult] = await Promise.allSettled([
    api(`/api/trading/preview?symbol=${encodeURIComponent(symbol)}&side=${side}&quantity_lots=${quantityLots}`),
    api(`/api/risk/summary?symbol=${encodeURIComponent(symbol)}&side=${side}&quantity_lots=${quantityLots}`),
    api('/api/assets/summary?limit=6'),
    api('/api/assets/positions?limit=6'),
  ]);
  if (previewResult.status !== 'fulfilled') {
    renderWorkspaceError(['tradingStatusBox', 'tradingEstimateBox', 'tradingMetaBox'], '交易預覽載入失敗', previewResult.reason?.message || '請稍後再試。');
    return;
  }
  renderTradingWorkspace(
    previewResult.value,
    riskResult.status === 'fulfilled' ? riskResult.value : null,
    assetSummaryResult.status === 'fulfilled' ? assetSummaryResult.value : null,
    assetPositionsResult.status === 'fulfilled' ? assetPositionsResult.value : null,
  );
}

async function loadAssistantView() {
  const symbol = getAssistantSymbol();
  renderWorkspaceError(['assistantContextBox', 'assistantCardsBox', 'assistantWatchlistBox', 'assistantNotificationBox', 'assistantMetaBox'], '載入中', '正在整合每日報告、自選股、通知與個股脈絡...');
  $('assistantSummaryCards').innerHTML = '';
  if (!symbol) {
    renderWorkspaceError(
      ['assistantContextBox', 'assistantCardsBox', 'assistantWatchlistBox', 'assistantNotificationBox', 'assistantMetaBox'],
      '尚未指定股票',
      '請先輸入股票代號；本輪不會執行規則或模型分析。',
    );
    return;
  }
  const workspace = await api(`/api/trading/assistant?symbol=${encodeURIComponent(symbol)}`);
  renderAssistantWorkspace(workspace);
}

async function loadRiskView() {
  const { symbol, side, quantityLots } = getTradeParams('risk');
  renderWorkspaceError(['riskAlertsBox', 'riskMetaBox'], '載入中', '正在計算風控摘要與部位快照...');
  $('riskSummaryCards').innerHTML = '';
  $('riskPositionsTable').innerHTML = '';
  if (!symbol) {
    renderWorkspaceError(
      ['riskAlertsBox', 'riskMetaBox'],
      '尚未指定股票',
      '請先輸入股票代號；未指定標的時不會產生風控判定。',
    );
    return;
  }
  const workspace = await api(`/api/risk/summary?symbol=${encodeURIComponent(symbol)}&side=${side}&quantity_lots=${quantityLots}`);
  renderRiskWorkspace(workspace);
}

async function loadAssetsView() {
  renderWorkspaceError(['assetStatusBox', 'assetMetaBox'], '載入中', '正在整理總資產、損益與部位明細...');
  $('assetSummaryCards').innerHTML = '';
  $('assetPositionsTable').innerHTML = '';
  const [summaryPayload, positionsPayload] = await Promise.all([
    api('/api/assets/summary?limit=6'),
    api('/api/assets/positions?limit=6'),
  ]);
  renderAssetWorkspace(summaryPayload, positionsPayload);
}
