function renderWatchlist(items) {
  const box = $('watchlistTable');
  if (!box) return;
  const rows = (items || []).map(item => `
    <div class="row clickable" data-symbol="${item.symbol}">
      <div>${item.name}<br/><small>${item.symbol}</small></div>
      <div>${item.latest_price === null || item.latest_price === undefined ? '-' : niceNumber(item.latest_price)}<br/><small class="${twColor(item.change_percent || 0)}">${item.change_percent === null || item.change_percent === undefined ? '-' : `${niceNumber(item.change_percent)}%`}</small></div>
      <div class="${twColor(item.institutional_net || 0)}">${item.institutional_net === null || item.institutional_net === undefined ? '-' : fmt(item.institutional_net)}</div>
      <div>${(item.alert_flags || []).slice(0, 2).join('、') || '觀察中'}</div>
    </div>`).join('');
  box.innerHTML = `<div class="row header"><div>股票</div><div>即時報價</div><div>法人</div><div>提醒</div></div>${rows || renderEmptyBlock('尚無自選股', '預設自選清單暫時沒有資料。')}`;
  bindSymbolOpeners('#watchlistTable .row.clickable');
}

function renderWatchlistGroups(items, report = null) {
  const box = $('watchlistGroupsBox');
  if (!box) return;
  const watchlist = items || [];
  const reportPicks = report?.picks || [];
  const strongest = [...watchlist]
    .filter(item => Number.isFinite(Number(item.change_percent)))
    .sort((a, b) => Number(b.change_percent || 0) - Number(a.change_percent || 0))
    .slice(0, 3);
  const institutional = [...watchlist]
    .filter(item => Number(item.institutional_net || 0) > 0)
    .sort((a, b) => Number(b.institutional_net || 0) - Number(a.institutional_net || 0))
    .slice(0, 3);
  const reportOverlap = reportPicks.filter(pick => watchlist.some(item => item.symbol === pick.symbol)).slice(0, 3);
  const groups = [
    { title: '核心觀察', detail: watchlist.slice(0, 4).map(item => `${item.name} ${item.symbol}`).join('、') || '尚無資料' },
    { title: '盤面偏強', detail: strongest.map(item => `${item.name} ${niceNumber(item.change_percent)}%`).join('、') || '尚未出現明顯強勢股' },
    { title: '法人偏多', detail: institutional.map(item => `${item.name} ${fmt(item.institutional_net)}`).join('、') || '今日尚無明顯法人偏多名單' },
    { title: '日報交集', detail: reportOverlap.map(item => `${item.name} ${item.signal === 'buy' ? '買進候選' : item.signal === 'watch' ? '觀察' : '保守'}`).join('、') || '自選股與今日報告尚未形成交集' },
  ];
  box.innerHTML = groups.map(group => `<div class="event"><h4>${group.title}</h4><p>${escapeHtml(group.detail)}</p></div>`).join('');
}

function renderWatchlistAlerts(items) {
  const box = $('watchlistAlertsBox');
  if (!box) return;
  const picks = (items || []).slice(0, 6);
  box.innerHTML = picks.map(item => `
    <div class="event">
      <h4>${item.name} ${item.symbol}</h4>
      <p>提醒標籤：${(item.alert_flags || []).join('、') || '觀察中'}</p>
      <p>最新新聞：${item.latest_news_title ? renderNewsSummaryLink(item.latest_news_title, item.latest_news_url, item.latest_news_title) : '目前沒有近期新聞'}</p>
    </div>`).join('') || renderEmptyBlock('尚無提醒', '後續可擴充價格、量能、法人與重大訊息警示。');
}

function populateSymbolSelect(items) {
  const select = $('symbolSelect');
  if (!select || !items?.length) return;
  const current = state.symbol || (select.value && select.value !== '' ? select.value : '');
  const groups = { TWSE: [], TPEx: [] };
  const seen = new Set();
  items.forEach(item => {
    if (!item.symbol || seen.has(item.symbol)) return;
    seen.add(item.symbol);
    const group = item.exchange === 'TPEx' || /\.TWO$/i.test(item.symbol) ? 'TPEx' : 'TWSE';
    groups[group].push(item);
  });
  Object.values(groups).forEach(rows => rows.sort((a, b) => String(a.symbol).localeCompare(String(b.symbol), 'zh-Hant')));
  select.innerHTML = '<option value="">請先搜尋並選擇股票</option>' + Object.entries(groups).map(([exchange, rows]) => rows.length ? `
    <optgroup label="${exchange === 'TWSE' ? '上市 TWSE' : '上櫃 TPEx'} · ${rows.length}">
      ${rows.map(item => `<option value="${escapeHtml(item.symbol)}">${escapeHtml(item.symbol)} · ${escapeHtml(item.name || '')}</option>`).join('')}
    </optgroup>` : '').join('');
  select.value = seen.has(current) ? current : '';
  select.title = `官方同步證券清單：${seen.size} 檔；可直接鍵入代號快速定位`;
}

function renderDailyReport(report) {
  const box = $('dailyReportBox');
  if (!box) return;
  const title = neutralResearchText(report?.title || '每日市場研究報告');
  const summary = neutralResearchText(report?.summary || '暫無報告。');
  const sourceSnapshot = (report?.source_snapshot || []).map(displaySourceLabel).filter(Boolean);
  const picks = (report?.picks || []).map(item => `
    <div class="event clickable" data-symbol="${item.symbol}">
      <h4>${item.name} ${item.symbol} ${signalLabel(item.signal)}</h4>
      <p>分數 ${item.score} · 理由：${(item.reasons || []).map(neutralResearchText).join('；')}</p>
      <p>風險：${(item.risk_factors || []).map(neutralResearchText).join('；')}</p>
      <p>事件數：${item.event_count ?? 0} · 來源：${(item.data_sources || []).map(displaySourceLabel).filter(Boolean).join('、') || '資料交叉驗證中'}</p>
    </div>`).join('');
  box.innerHTML = `
    <div class="event">
      <h4>${escapeHtml(title)}</h4>
      <p>${escapeHtml(summary)}</p>
      <p>產生時間：${report?.generated_at || '-'}</p>
      <p>資料快照：${sourceSnapshot.join('、') || '官方盤後與新聞來源交叉驗證中'}</p>
    </div>
    ${picks || renderEmptyBlock('尚無選股結果', '等待官方資料整理完成後產生。')}`;
  bindSymbolOpeners('#dailyReportBox .event.clickable');
}

function renderOverviewLeaders(items) {
  const box = $('overviewLeadersBox');
  if (!box) return;
  const sorted = [...(items || [])].sort((a, b) => (b.total_institutional_net || 0) - (a.total_institutional_net || 0)).slice(0, 8);
  const rows = sorted.map(item => `
    <div class="row clickable" data-symbol="${item.symbol}">
      <div>${item.name}<br/><small>${item.symbol}</small></div>
      <div class="${twColor(item.total_institutional_net || 0)}">${fmt(item.total_institutional_net)}</div>
      <div class="${twColor(item.foreign_net || 0)}">${fmt(item.foreign_net)}</div>
      <div class="${twColor(item.trust_net || 0)}">${fmt(item.trust_net)}</div>
    </div>`).join('');
  box.innerHTML = `<div class="row header"><div>股票</div><div>三大法人</div><div>外資</div><div>投信</div></div>${rows || renderEmptyBlock('尚無排行', '官方資料暫時沒有法人排行。')}`;
  bindSymbolOpeners('#overviewLeadersBox .row.clickable');
}

function renderInstitutionalTable(items) {
  const box = $('institutionalTable');
  if (!box) return;
  const sorted = [...(items || [])].sort((a, b) => (b.total_institutional_net || 0) - (a.total_institutional_net || 0)).slice(0, 8);
  const rows = sorted.map(item => `
    <div class="row clickable" data-symbol="${item.symbol}">
      <div>${item.name}<br/><small>${item.symbol}</small></div>
      <div class="${twColor(item.foreign_net || 0)}">${fmt(item.foreign_net)}</div>
      <div class="${twColor(item.trust_net || 0)}">${fmt(item.trust_net)}</div>
      <div class="${twColor(item.total_institutional_net || 0)}">${fmt(item.total_institutional_net)}</div>
    </div>`).join('');
  box.innerHTML = `<div class="row header"><div>股票</div><div>外資</div><div>投信</div><div>三大法人</div></div>${rows || renderEmptyBlock('尚無法人資料', '官方 T86 暫時沒有可用資料。')}`;
  bindSymbolOpeners('#institutionalTable .row.clickable');
}

function renderChipHistory(payload) {
  const institutional = payload?.institutional || {};
  const margin = payload?.margin || {};
  const companion = payload?.companion_streams || {};
  const streak = institutional.total_streak || {};
  const pitCoverage = payload?.pit_coverage || {};
  const refresh = payload?.refresh || {};
  const pitNotice = pitCoverage.exact_replay_eligible
    ? 'PIT 覆蓋已認證'
    : `PIT 覆蓋未認證${(pitCoverage.blockers || []).length ? `：${(pitCoverage.blockers || []).join('、')}` : ''}`;
  const refreshNotice = (refresh.pending_dates || []).length
    ? ` · 官方來源逾時 ${refresh.pending_dates.length} 項，保留缺失值`
    : '';
  const streamLabel = (key, label) => {
    const detail = pitCoverage?.streams?.[key] || {};
    const count = companion?.[key]?.count ?? detail.count ?? 0;
    const state = detail.historical_pit_eligible ? 'PIT 已認證' : (detail.missingness || '未認證');
    return `${label} ${count} 筆／${state}`;
  };
  $('chipHistoryStatus').textContent = `${payload.symbol} · 法人 ${institutional.count || 0} 日 · 融資券 ${margin.count || 0} 日`
    + ` · 三大法人連續${streak.direction === 'buy' ? '買超' : streak.direction === 'sell' ? '賣超' : '持平'} ${streak.days || 0} 日`
    + ` · 借券 ${streamLabel('borrowed_short', '公開活動')} · TDCC ${streamLabel('tdcc_holding_distribution', '週資料')}`
    + ` · ${pitNotice}${refreshNotice}`;
  const dates = [...new Set([...(institutional.items || []).map(item => item.trade_date), ...(margin.items || []).map(item => item.trade_date)])].sort().reverse();
  const flowByDate = Object.fromEntries((institutional.items || []).map(item => [item.trade_date, item]));
  const marginByDate = Object.fromEntries((margin.items || []).map(item => [item.trade_date, item]));
  const chipValue = value => value == null ? '-' : fmt(value);
  $('chipHistoryTable').innerHTML = `<table><thead><tr><th>日期</th><th>外資</th><th>投信</th><th>自營商</th><th>三大法人</th><th>融資餘額／增減</th><th>融券餘額／增減</th><th>券資比</th><th>資券互抵</th></tr></thead><tbody>${dates.map(date => {
    const flow = flowByDate[date] || {}, marginItem = marginByDate[date] || {};
    return `<tr><td>${escapeHtml(date)}</td><td>${chipValue(flow.foreign_net)}</td><td>${chipValue(flow.trust_net)}</td><td>${chipValue(flow.dealer_net)}</td><td>${chipValue(flow.total_institutional_net)}</td><td>${chipValue(marginItem.margin_balance)} / ${chipValue(marginItem.margin_change)}</td><td>${chipValue(marginItem.short_balance)} / ${chipValue(marginItem.short_change)}</td><td>${marginItem.short_to_margin_percent == null ? '-' : `${monthlyRevenueMetric(marginItem.short_to_margin_percent, 4)}%`}</td><td>${chipValue(marginItem.offsetting)}</td></tr>`;
  }).join('')}</tbody></table>`;
}

async function loadChipHistory() {
  const symbol = String($('chipHistorySymbol').value || '').trim().toUpperCase();
  const days = $('chipHistoryDays').value || '20';
  if (!symbol) {
    $('chipHistoryStatus').textContent = '請先輸入股票代號；不會以空白範圍回補官方籌碼資料。';
    $('chipHistoryTable').innerHTML = '';
    return null;
  }
  if ($('shortDaytradeSymbol')) $('shortDaytradeSymbol').value = symbol;
  if ($('tdccHistorySymbol')) $('tdccHistorySymbol').value = symbol;
  if ($('shortDaytradeDays')) $('shortDaytradeDays').value = days;
  $('chipHistoryStatus').textContent = '正在先保存官方借券與 TDCC 快照，再建立同一份 PIT 籌碼 receipt…';
  const companionResults = await Promise.allSettled([
    loadShortDaytradeHistory(),
    loadTdccHoldingHistory(),
  ]);
  const companionFailed = companionResults.filter(result => result.status === 'rejected').length;
  if (companionFailed) {
    $('chipHistoryStatus').textContent = `借券／TDCC 有 ${companionFailed} 項未取得；仍會建立缺失明確的 PIT receipt。`;
  }
  const payload = await api(uiDataApi(`/flow/chip/history?symbol=${encodeURIComponent(symbol)}&days=${encodeURIComponent(days)}&refresh=true`));
  renderChipHistory(payload);
  return payload;
}

function renderShortDaytradeHistory(payload) {
  const borrowed = payload?.summary?.borrowed || {};
  const daytrade = payload?.summary?.daytrade || {};
  const heat = daytrade.heat || {};
  const metricValue = value => value == null ? '-' : monthlyRevenueMetric(value, 4);
  $('shortDaytradeStatus').textContent = `${payload.symbol} · ${payload.exchange} · ${payload.coverage?.count || 0} 個交易日`
    + ` · 當沖熱度：${heat.label || '資料不足'}`
    + ' · 近期資料可能於 T+2 前修訂';
  $('shortDaytradeSummary').innerHTML = `
    <article class="metric"><span>借券賣出餘額</span><strong>${metricValue(borrowed.latest_balance)}</strong><small>較前日 ${metricValue(borrowed.latest_balance_change)}</small></article>
    <article class="metric"><span>近五日借券／還券</span><strong>${metricValue(borrowed.recent_5d_sold)} / ${metricValue(borrowed.recent_5d_returned)}</strong><small>借券賣出與還券分列</small></article>
    <article class="metric"><span>最新當沖比</span><strong>${daytrade.latest_ratio_percent == null ? '-' : `${metricValue(daytrade.latest_ratio_percent)}%`}</strong><small>日變化 ${daytrade.ratio_change_percentage_points == null ? '-' : `${metricValue(daytrade.ratio_change_percentage_points)} 個百分點`}</small></article>
    <article class="metric"><span>近五日平均／熱度</span><strong>${daytrade.recent_5d_average_ratio_percent == null ? '-' : `${metricValue(daytrade.recent_5d_average_ratio_percent)}%`}</strong><small>${escapeHtml(heat.label || '資料不足')}（機械區間，非投資訊號）</small></article>`;
  const rows = [...(payload.items || [])].reverse().map(item => {
    const short = item.borrowed || {};
    const trade = item.daytrade || {};
    const revision = trade.revision_status === 'provisional_t_plus_2' ? 'T+2 前可能修訂' : '已發布';
    return `<tr>
      <td>${escapeHtml(item.trade_date || '-')}</td>
      <td>${metricValue(short.borrowed_sell)}</td>
      <td>${metricValue(short.borrowed_return)}</td>
      <td>${metricValue(short.borrowed_sell_balance)}</td>
      <td>${metricValue(short.borrowed_sell_balance == null || short.borrowed_sell_previous_balance == null ? null : short.borrowed_sell_balance - short.borrowed_sell_previous_balance)}</td>
      <td>${metricValue(trade.daytrade_volume)}</td>
      <td>${metricValue(trade.total_traded_volume)}</td>
      <td>${trade.daytrade_ratio_percent == null ? '-' : `${metricValue(trade.daytrade_ratio_percent)}%`}</td>
      <td>${escapeHtml(trade.heat?.label || '資料不足')} · ${revision}</td>
    </tr>`;
  }).join('');
  $('shortDaytradeTable').innerHTML = rows
    ? `<table style="width:100%;min-width:1100px;border-collapse:separate;border-spacing:12px 8px;white-space:nowrap"><thead><tr><th>日期</th><th>借券賣出</th><th>還券</th><th>借券餘額</th><th>餘額增減</th><th>當沖量</th><th>總成交量</th><th>當沖比</th><th>熱度／修訂</th></tr></thead><tbody>${rows}</tbody></table>`
    : renderEmptyBlock('尚無借券與當沖資料', '請確認代號與日期範圍，非交易日不會以零補值。');
}

async function loadShortDaytradeHistory() {
  const symbol = String($('shortDaytradeSymbol')?.value || '').trim().toUpperCase();
  if (!symbol) throw new Error('請輸入上市 .TW 或上櫃 .TWO 股票代號。');
  const days = $('shortDaytradeDays')?.value || '14';
  $('shortDaytradeStatus').textContent = '正在讀取 TWSE／TPEx 官方借券與當沖歷史…';
  const payload = await api(uiDataApi(`/flow/chip/short-daytrade?symbol=${encodeURIComponent(symbol)}&days=${encodeURIComponent(days)}&refresh=true`));
  renderShortDaytradeHistory(payload);
  return payload;
}

function renderTdccHoldingHistory(payload) {
  const summary = payload?.summary || {};
  const latest = summary.latest || {};
  const change = summary.week_over_week || {};
  const trend = summary.major_holder_1000_lot_trend || {};
  const metricValue = value => value == null ? '-' : monthlyRevenueMetric(value, 4);
  const percentValue = value => value == null ? '-' : `${metricValue(value)}%`;
  const signedValue = (value, suffix = '') => {
    if (value == null) return '-';
    return `${Number(value) > 0 ? '+' : ''}${metricValue(value)}${suffix}`;
  };
  const trendLabel = trend.direction === 'increase'
    ? '增加'
    : trend.direction === 'decrease'
      ? '減少'
      : trend.direction === 'flat'
        ? '持平'
        : '資料不足';
  const trendText = trend.direction === 'unavailable'
    ? '千張大戶趨勢：需至少 2 週'
    : `千張大戶連續${trendLabel} ${trend.streak_weeks || 0} 週`;
  const syncNote = payload?.sync?.status === 'failed'
    ? ' · 本次更新失敗，顯示已保存快取'
    : '';
  const pitNote = payload?.truthfulness?.historical_pit_eligible === true
    ? ' · PIT 歷史已認證'
    : ' · 僅當週快照／PIT 歷史未認證';
  $('tdccHistoryStatus').textContent = `${payload.symbol} · ${payload.coverage?.count || 0} 週`
    + ` · 最新 ${latest.report_date || '-'}`
    + ` · ${trendText}`
    + syncNote
    + pitNote;
  $('tdccHistorySummary').innerHTML = `
    <article class="metric"><span>集保總人數</span><strong>${metricValue(latest.total_holders)}</strong><small>週變化 ${signedValue(change.total_holders)} 人</small></article>
    <article class="metric"><span>散戶（1–10,000 股）</span><strong>${metricValue(latest.small_shareholder_count)} 人</strong><small>${percentValue(latest.small_shareholder_ratio)} · 週變化 ${signedValue(change.small_shareholder_ratio, ' 個百分點')}</small></article>
    <article class="metric"><span>千張大戶比例</span><strong>${percentValue(latest.major_holder_1000_lot_ratio)}</strong><small>週變化 ${signedValue(change.major_holder_1000_lot_ratio, ' 個百分點')}</small></article>
    <article class="metric"><span>400 張以上／集中度</span><strong>${percentValue(latest.holder_400_lot_ratio)}</strong><small>集中度 ${percentValue(latest.concentration_score)}（機械指標）</small></article>`;
  const rows = [...(payload.items || [])].reverse().map(item => {
    const wow = item.week_over_week || {};
    return `<tr>
      <td>${escapeHtml(item.report_date || '-')}</td>
      <td>${metricValue(item.total_holders)}</td>
      <td>${signedValue(wow.total_holders)}</td>
      <td>${metricValue(item.small_shareholder_count)}</td>
      <td>${percentValue(item.small_shareholder_ratio)}</td>
      <td>${percentValue(item.major_holder_1000_lot_ratio)}</td>
      <td>${signedValue(wow.major_holder_1000_lot_ratio, ' 個百分點')}</td>
      <td>${percentValue(item.holder_400_lot_ratio)}</td>
      <td>${escapeHtml(String(item.raw_hash || '').slice(0, 12) || '-')}</td>
    </tr>`;
  }).join('');
  $('tdccHistoryTable').innerHTML = rows
    ? `<table style="width:100%;min-width:1100px;border-collapse:separate;border-spacing:12px 8px;white-space:nowrap"><thead><tr><th>資料週</th><th>總人數</th><th>人數週變化</th><th>散戶人數</th><th>散戶比例</th><th>千張大戶</th><th>大戶週變化</th><th>400 張以上</th><th>原始列雜湊</th></tr></thead><tbody>${rows}</tbody></table>`
    : renderEmptyBlock('尚無 TDCC 週資料', 'TDCC OpenAPI 只提供最新快照；每次更新會保存當週資料並逐週累積，不會用零補缺週。');
}

async function loadTdccHoldingHistory() {
  const symbol = String($('tdccHistorySymbol')?.value || '').trim().toUpperCase();
  if (!symbol) throw new Error('請輸入上市 .TW 或上櫃 .TWO 股票代號。');
  const weeks = $('tdccHistoryWeeks')?.value || '52';
  $('tdccHistoryStatus').textContent = '正在讀取 TDCC 官方最新持股分級並保存每週歷史…';
  const payload = await api(uiDataApi(`/flow/chip/tdcc-history?symbol=${encodeURIComponent(symbol)}&weeks=${encodeURIComponent(weeks)}&refresh=true`));
  renderTdccHoldingHistory(payload);
  return payload;
}

function renderSourceStatus(data) {
  const box = $('sourceStatusBox');
  if (!box) return;
  const items = data?.items || [];
  $('sourceActiveCount').textContent = `${(data?.active_ids || []).length}/${items.length || '-'}`;
  box.innerHTML = items.map(item => `
    <div class="event">
      <h4>${item.name} <span class="tag ${item.status === 'active' ? 'positive' : item.status === 'partial' ? 'neutral' : 'negative'}">${openStockStatusHtml(item.status)}</span></h4>
      <p>層級：${sourcePriorityLabel(item)} · 市場：${(item.market_scope || []).join('、') || '未標示'}</p>
      <p>範圍：${(item.coverage || []).join('、')}</p>
      <p>${item.reliability_note || ''}</p>
    </div>`).join('') || renderEmptyBlock('尚無來源清單', '來源註冊中心目前沒有回傳資料。');
}

function renderOverviewIndices(items) {
  const box = $('overviewIndicesBox');
  if (!box) return;
  const rows = (items || []).map(item => `
    <div class="row">
      <div>${item.label}<br/><small>${item.symbol}</small></div>
      <div>${item.priceText}</div>
      <div class="${item.changeClass}">${item.changeText}</div>
      <div>${item.source}</div>
    </div>`).join('');
  box.innerHTML = `<div class="row header"><div>指數</div><div>最新</div><div>漲跌</div><div>來源</div></div>${rows || renderEmptyBlock('尚無指數資料', '大盤總覽稍後再試。')}`;
}

function renderEventFeed(targetId, items, emptyTitle, emptyDetail) {
  const box = $(targetId);
  if (!box) return;
  box.innerHTML = (items || []).map(item => `
    <div class="event ${item.related_symbols?.[0] ? 'clickable' : ''}" data-symbol="${item.related_symbols?.[0] || ''}">
      <h4>${escapeHtml(item.title)}</h4>
      <p>${renderNewsSummaryLink(item.summary || item.event_type || '', item.source_url, item.title || item.event_type || '閱讀全文')}</p>
      <p>${escapeHtml(item.event_time || '')} · ${escapeHtml(eventTypeLabel(item.event_type))}${isOfficialEventItem(item) ? ' · 官方驗證' : ''}</p>
    </div>`).join('') || renderEmptyBlock(emptyTitle, emptyDetail);
  bindSymbolOpeners(`#${targetId} .event.clickable`);
}

function renderNewsCenter(targetId, items, emptyTitle, emptyDetail) {
  const box = $(targetId);
  if (!box) return;
  box.innerHTML = (items || []).map(item => `
    <div class="event ${item.related_symbols?.[0] ? 'clickable' : ''}" data-symbol="${item.related_symbols?.[0] || ''}">
      <h4>${escapeHtml(item.title)}</h4>
      <p>${renderNewsSummaryLink(item.summary, item.source_url, item.title)}</p>
      <p>${escapeHtml(item.published_at || item.event_time || '')} · ${escapeHtml(newsCategoryLabel(item.category || 'company'))} · ${escapeHtml(item.source || displaySourceLabel(item.source_url) || '市場來源')}${item.official_verified ? ' · 官方驗證' : ''}</p>
      <p>情緒 ${openStockStatusHtml(item.sentiment || 'neutral')} · 可信度 ${(Number(item.credibility || 0) * 100).toFixed(0)}% · 關聯 ${escapeHtml((item.related_symbols || []).slice(0, 3).join('、') || '市場觀察')}</p>
    </div>`).join('') || renderEmptyBlock(emptyTitle, emptyDetail);
  bindSymbolOpeners(`#${targetId} .event.clickable`);
}

function corporateActionTypeLabel(value) {
  return ({
    cash_dividend: '現金股利',
    stock_dividend: '股票股利',
    capital_reduction: '減資',
    capital_increase: '現金增資',
    split: '股票分割',
    reverse_split: '反向分割',
    merger: '合併換股',
    treasury_stock: '庫藏股',
  })[value] || value || '公司行動';
}

function renderCorporateActionCards(targetId, payload) {
  const box = $(targetId);
  if (!box) return;
  const items = payload?.items || [];
  const heading = `<div class="corporate-action-heading"><span>公司行動台帳</span><span>${items.length} 筆 · 不從價格跳動推測</span></div>`;
  const cards = items.map(item => {
    const terms = item.terms || {};
    const application = item.application || null;
    const effects = [];
    if (Number(terms.cash_per_share || 0)) effects.push(`每股現金 ${niceNumber(terms.cash_per_share)} ${escapeHtml(terms.currency || 'TWD')}`);
    if (Number(terms.share_multiplier || 1) !== 1) effects.push(`股數倍率 ${niceNumber(terms.share_multiplier)}`);
    if (Number(terms.price_multiplier || 1) !== 1) effects.push(`價格倍率 ${niceNumber(terms.price_multiplier)}`);
    if (Number(terms.subscription_ratio || 0)) effects.push(`認購權 ${niceNumber(terms.subscription_ratio)} 股／股；不自動扣款`);
    if (terms.successor_symbol) effects.push(`換為 ${escapeHtml(terms.successor_symbol)} × ${niceNumber(terms.exchange_ratio)}`);
    if (!effects.length) effects.push('持有人股數與現金不變');
    const source = renderNewsSummaryLink('查看官方原始來源', item.source_url, `${corporateActionTypeLabel(item.action_type)} 官方來源`);
    const stateText = application
      ? `已同步：${escapeHtml(application.outcome)}`
      : item.terms_complete && item.official_verified
        ? '可進行到期同步'
        : `不自動套用：${item.official_verified ? '條款尚未完整' : '尚未官方驗證'}`;
    return `
      <div class="event corporate-action-card" data-corporate-action-id="${escapeHtml(item.action_id)}" data-applied="${Boolean(application)}">
        <h4>${escapeHtml(corporateActionTypeLabel(item.action_type))} · ${escapeHtml(item.effective_date)}</h4>
        <p>${effects.join('；')}</p>
        <p>${escapeHtml(item.holder_effect)} · ${escapeHtml(item.price_effect)}</p>
        <p class="corporate-action-state">${stateText}${item.official_verified ? ' · 官方驗證' : ''}</p>
        <p>${escapeHtml(item.source_id)} · revision ${Number(item.revision || 0)} · ${source}</p>
      </div>`;
  }).join('');
  box.insertAdjacentHTML(
    'afterbegin',
    heading + (cards || renderEmptyBlock('尚無公司行動台帳', '尚未匯入具官方來源與結構化條款的公司行動；系統不會從價格跳動自行猜測。')),
  );
}

function renderNewsScopeControls() {
  const box = $('newsScopeBar');
  if (!box) return;
  const options = [
    { value: 'market', label: '全市場' },
    { value: 'symbol', label: `目前個股 ${state.symbol}` },
  ];
  box.innerHTML = options.map(option => `
    <button class="filter-chip ${state.newsScope === option.value ? 'active' : ''}" data-news-scope="${option.value}">
      ${escapeHtml(option.label)}
    </button>`).join('');
  document.querySelectorAll('[data-news-scope]').forEach(el => el.addEventListener('click', async () => {
    state.newsScope = el.dataset.newsScope || 'market';
    await loadNewsCenterView();
  }));
}

function renderNewsCategoryControls(data) {
  const box = $('newsFilterBar');
  if (!box) return;
  const counts = data?.category_counts || {};
  box.innerHTML = NEWS_CATEGORY_OPTIONS.map(option => {
    const count = option.value === 'all' ? (data?.count || 0) : (counts[option.value] || 0);
    return `<button class="filter-chip ${state.newsCategory === option.value ? 'active' : ''}" data-news-category="${option.value}">${escapeHtml(option.label)} <span>${count}</span></button>`;
  }).join('');
  document.querySelectorAll('[data-news-category]').forEach(el => el.addEventListener('click', async () => {
    state.newsCategory = el.dataset.newsCategory || 'all';
    await loadNewsCenterView();
  }));
}

function renderNewsMeta(data) {
  const box = $('newsMetaBar');
  if (!box) return;
  const items = data?.items || [];
  const officialCount = items.filter(item => item.official_verified).length;
  const topSources = [...new Set(items.map(item => item.source).filter(Boolean))].slice(0, 4);
  const scopeText = state.newsScope === 'symbol' ? `目前個股 ${state.symbol}` : '全市場';
  box.innerHTML = [
    `<div class="meta-pill">範圍：${escapeHtml(scopeText)}</div>`,
    `<div class="meta-pill">筆數：${items.length}</div>`,
    `<div class="meta-pill">官方驗證：${officialCount}</div>`,
    `<div class="meta-pill">來源：${escapeHtml(topSources.join('、') || '整理中')}</div>`,
  ].join('');
}

async function loadNewsCenterView() {
  renderNewsScopeControls();
  const params = new URLSearchParams({ limit: state.newsScope === 'symbol' ? '12' : '24' });
  if (state.newsCategory !== 'all') params.set('category', state.newsCategory);
  if (state.newsScope === 'symbol' && state.symbol) params.set('symbol', state.symbol);
  try {
    const data = await api(uiDataApi(`/news/center?${params.toString()}`));
    renderNewsCategoryControls(data);
    renderNewsMeta(data);
    renderNewsCenter('newsCenterBox', data.items, '尚無新聞摘要', '目前條件下沒有可用的新聞或重大訊息。');
  } catch (err) {
    renderNewsCategoryControls({ count: 0, category_counts: {} });
    renderNewsMeta({ items: [] });
    $('newsCenterBox').innerHTML = renderEmptyBlock('新聞中心載入失敗', err.message || '請稍後再試。');
  }
}

function renderNotificationChannels(data) {
  const box = $('notificationStatusBox');
  if (!box) return;
  box.innerHTML = (data?.items || []).map(item => `
    <div class="event">
      <h4>${String(item.channel || '').toUpperCase()} <span class="tag ${item.configured ? 'positive' : 'neutral'}">${item.mode}</span></h4>
      <p>設定狀態：${item.configured ? '已填設定' : '尚未設定'}；目標 ${item.target_hint || '-'}</p>
      <p>${escapeHtml(item.note || '')}</p>
    </div>`).join('') || renderEmptyBlock('通知通道尚未初始化', '目前沒有可用通知通道資訊。');
}

function renderNotificationPreviews(data) {
  const box = $('notificationPreviewBox');
  if (!box) return;
  state.notificationPreviews = data?.items || [];
  box.innerHTML = (data?.items || []).map(item => `
    <div class="event">
      <h4>${escapeHtml(item.title)}</h4>
      <p>${escapeHtml(item.body || '')}</p>
      <p>類型 ${escapeHtml(item.category)} · 通道 ${escapeHtml((item.channels || []).join('、'))} · dry-run ${item.dry_run ? '是' : '否'}</p>
    </div>`).join('') || renderEmptyBlock('尚無推播預覽', '後續可把訊號、新聞與重大訊息串成正式推播。');
}

function renderNotificationSendResult(data) {
  const box = $('notificationSendResultBox');
  if (!box) return;
  box.innerHTML = (data?.items || []).map(item => `
    <div class="event">
      <h4>${String(item.channel || '').toUpperCase()} <span class="tag ${item.sent ? 'positive' : item.attempted ? 'negative' : 'neutral'}">${escapeHtml(item.mode || '-')}</span></h4>
      <p>attempted ${item.attempted ? '是' : '否'} · sent ${item.sent ? '是' : '否'} · target ${escapeHtml(item.target_hint || '-')}</p>
      <p>${escapeHtml(item.detail || '')}</p>
    </div>`).join('') || renderEmptyBlock('尚無發送結果', '按下 dry-run 檢查後會顯示每個通道的狀態。');
}

async function sendNotificationDryRun() {
  const preview = state.notificationPreviews[0];
  const box = $('notificationSendResultBox');
  if (!preview) {
    if (box) box.innerHTML = renderEmptyBlock('尚無可發送預覽', '請先載入通知預覽。');
    return;
  }
  if (box) box.innerHTML = renderEmptyBlock('檢查中', '正在執行 Telegram / LINE dry-run。');
  const result = await api('/api/notifications/send', {
    method: 'POST',
    body: JSON.stringify({
      title: preview.title,
      body: preview.body,
      channels: preview.channels || ['telegram', 'line'],
      related_symbols: preview.related_symbols || [],
      dry_run: true,
    }),
  });
  renderNotificationSendResult(result);
}

function renderFundamentalsBox(symbol, marginItems, revenueItems) {
  const box = $('fundamentalsBox');
  if (!box) return;
  const margin = (marginItems || [])[0];
  const revenue = (revenueItems || [])[0];
  const html = [];
  html.push(`<div class="event"><h4>${escapeHtml(symbol)} Phase 1 基本資料</h4><p>以下內容來自官方盤後資料，供首頁與個股頁共用。</p></div>`);
  if (margin) {
    html.push(`<div class="event">
      <h4>融資融券</h4>
      <p>融資餘額 ${fmt(margin.margin_balance)} / 前日 ${fmt(margin.margin_previous_balance)} / 今日買進 ${fmt(margin.margin_buy)} / 今日賣出 ${fmt(margin.margin_sell)}</p>
      <p>融券餘額 ${fmt(margin.short_balance)} / 前日 ${fmt(margin.short_previous_balance)} / 今日賣出 ${fmt(margin.short_sell)}</p>
      <p>來源：${escapeHtml(margin.source)}</p>
    </div>`);
  } else {
    html.push(renderEmptyBlock('融資融券', '這檔股票暫時沒有官方融資融券資料。'));
  }
  if (revenue) {
    html.push(`<div class="event">
      <h4>月營收</h4>
      <p>${escapeHtml(revenue.period)} 當月營收 ${fmt(revenue.current_revenue)} / MoM ${niceNumber(revenue.mom_change_percent)}% / YoY ${niceNumber(revenue.yoy_change_percent)}%</p>
      <p>累計營收 ${fmt(revenue.ytd_revenue)} / 累計年增 ${niceNumber(revenue.ytd_change_percent)}%</p>
      <p>來源：${escapeHtml(revenue.source)}</p>
    </div>`);
  } else {
    html.push(renderEmptyBlock('月營收', '這檔股票暫時沒有官方月營收資料。'));
  }
  box.innerHTML = html.join('');
  const center = $('fundamentalsCenterBox');
  if (center) center.innerHTML = html.join('');
}

function industryMetricValue(metric) {
  if (!metric?.available) return '不可用';
  const value = typeof metric.value === 'number'
    ? metric.value.toLocaleString(undefined, { maximumFractionDigits: 2 })
    : String(metric.value ?? '-');
  return `${value}${metric.unit === '%' ? '%' : ` ${metric.unit || ''}`}`;
}

function renderIndustryMetrics(payload) {
  const status = $('industryMetricsStatus');
  const table = $('industryMetricsTable');
  if (!status || !table) return;
  if (!payload?.supported) {
    status.textContent = `${payload?.symbol || '-'} 無法辨識支援的產業；系統沒有改套通用公司模板。`;
    table.innerHTML = renderEmptyBlock('尚未支援此產業', '請確認證券主檔的產業分類，或明確選擇支援的產業。');
    return;
  }
  const metrics = Array.isArray(payload.metrics) ? payload.metrics : [];
  status.textContent = `${payload.symbol} · ${payload.profile_label} · ${payload.period || '期間未提供'}`
    + `；可用 ${payload.available_metric_count || 0} / ${metrics.length} 項`
    + `；分類依據 ${payload.classification_basis || '-'}。`;
  table.innerHTML = `
    <table>
      <thead><tr><th>產業指標</th><th>數值</th><th>公式／官方欄位</th><th>產業判讀目的</th></tr></thead>
      <tbody>${metrics.map(metric => `
        <tr>
          <td>${escapeHtml(metric.label)}</td>
          <td class="${metric.available ? '' : 'muted'}">${escapeHtml(industryMetricValue(metric))}</td>
          <td>${escapeHtml(metric.formula || '-')}</td>
          <td>${escapeHtml(metric.rationale || '-')}${metric.available ? '' : '<br/><small>官方必要欄位缺漏</small>'}</td>
        </tr>`).join('')}</tbody>
    </table>
    <div class="meta-bar">來源：${(payload.sources || []).map(source => source.url
      ? `<a href="${escapeHtml(source.url)}" target="_blank" rel="noreferrer">${escapeHtml(source.source_id || 'official')}</a>`
      : escapeHtml(source.source_id || 'official')).join('、') || '已保存官方財報'}；不同產業不共用同一指標清單。</div>`;
}

async function loadIndustryMetrics(symbol = null) {
  const selectedSymbol = String(symbol || $('industryMetricsSymbol')?.value || state.symbol || '').trim().toUpperCase();
  if (!selectedSymbol) throw new Error('請輸入股票代號。');
  const industry = String($('industryMetricsProfile')?.value || '').trim();
  const period = String($('industryMetricsPeriod')?.value || '').trim().toUpperCase();
  $('industryMetricsSymbol').value = selectedSymbol;
  $('industryMetricsStatus').textContent = '正在依產業載入不同的官方指標…';
  const params = new URLSearchParams({ symbol: selectedSymbol });
  if (industry) params.set('industry', industry);
  if (period) params.set('period', period);
  const payload = await api(uiDataApi(`/fundamentals/industry-metrics?${params.toString()}`));
  renderIndustryMetrics(payload);
  return payload;
}

function renderFinancialGuidance(payload) {
  const status = $('financialGuidanceStatus');
  const table = $('financialGuidanceTable');
  if (!status || !table) return;
  const items = Array.isArray(payload?.items) ? payload.items : [];
  status.textContent = items.length
    ? `${payload.symbol} 找到 ${items.length} 筆官方量化財測，已與同期間查核／核閱實際數比較。`
    : `${payload?.symbol || '-'} 目前沒有 TWSE 自願量化財測；不以法人預估或 AI 推測補值。`;
  if (!items.length) {
    table.innerHTML = renderEmptyBlock('沒有可比較財測', '公司可能未自願發布量化財測，質化法說展望仍須保留原始來源與期間。');
    return;
  }
  table.innerHTML = `<table><thead><tr><th>期間／指標</th><th>預測區間</th><th>實際</th><th>結果</th><th>來源</th></tr></thead>
    <tbody>${items.map(item => {
      const comparison = item.comparison || {};
      const result = comparison.status === 'above_range' ? '高於區間'
        : comparison.status === 'below_range' ? '低於區間'
          : comparison.status === 'within_range' ? '落在區間' : '不可比較';
      return `<tr>
        <td>${escapeHtml(item.period)}<br/><small>${escapeHtml(item.metric_label)}</small></td>
        <td>${monthlyRevenueMetric(item.forecast_low, 0)} ～ ${monthlyRevenueMetric(item.forecast_high, 0)} ${escapeHtml(item.unit)}</td>
        <td>${monthlyRevenueMetric(item.actual, 0)} ${escapeHtml(item.unit)}</td>
        <td>${escapeHtml(result)}<br/><small>達成率 ${comparison.achievement_percent == null ? '-' : `${monthlyRevenueMetric(comparison.achievement_percent)}%`}</small></td>
        <td><a href="${escapeHtml(item.source_url)}" target="_blank" rel="noreferrer">${escapeHtml(item.source_id)}</a></td>
      </tr>`;
    }).join('')}</tbody></table>`;
}

async function loadFinancialGuidance(symbol = null) {
  const selected = String(symbol || $('financialGuidanceSymbol')?.value || state.symbol || '').trim().toUpperCase();
  if (!selected) throw new Error('請輸入股票代號。');
  $('financialGuidanceSymbol').value = selected;
  $('financialGuidanceStatus').textContent = '正在載入官方財測與查核／核閱實際數…';
  const payload = await api(uiDataApi(`/fundamentals/guidance?symbol=${encodeURIComponent(selected)}`));
  renderFinancialGuidance(payload);
  return payload;
}

function renderFinancialAnomalies(payload) {
  const status = $('financialAnomalyStatus'), table = $('financialAnomalyTable');
  if (!status || !table) return;
  const flags = payload?.flags || [];
  status.textContent = payload?.status === 'complete'
    ? `${payload.symbol} ${payload.period} vs ${payload.comparison_period}：觸發 ${payload.triggered_count} / ${flags.length} 項。`
    : `${payload?.symbol || '-'} 沒有兩個連續且三表齊全的季度。`;
  table.innerHTML = flags.length ? `<table><thead><tr><th>檢查</th><th>狀態</th><th>觀察值</th><th>公開門檻</th></tr></thead><tbody>${flags.map(item => `<tr><td>${escapeHtml(item.label)}</td><td>${item.status === 'triggered' ? '異常' : item.status === 'clear' ? '未觸發' : '不可判斷'}</td><td>${escapeHtml(JSON.stringify(item.observed))}</td><td>${escapeHtml(item.threshold)}</td></tr>`).join('')}</tbody></table>` : renderEmptyBlock('資料不足', '請先同步至少兩個連續季度的三張官方財報。');
}

async function loadFinancialAnomalies(symbol = null) {
  const selected = String(symbol || $('financialAnomalySymbol')?.value || state.symbol || '').trim().toUpperCase();
  if (!selected) throw new Error('請輸入股票代號。');
  $('financialAnomalySymbol').value = selected;
  $('financialAnomalyStatus').textContent = '正在掃描相鄰季度官方財報…';
  const payload = await api(uiDataApi(`/fundamentals/anomalies?symbol=${encodeURIComponent(selected)}`));
  renderFinancialAnomalies(payload);
  return payload;
}

function renderBasicValuation(payload) {
  const status = $('basicValuationStatus');
  const table = $('basicValuationTable');
  if (!status || !table) return;
  const metrics = Array.isArray(payload?.metrics) ? payload.metrics : [];
  if (payload?.status === 'unavailable') {
    status.textContent = `${payload?.symbol || '-'} · 估值資料目前不可計算：${payload?.unavailable_reason || '缺少同期間官方財報。'}`;
    table.innerHTML = renderEmptyBlock('估值資料不足', payload?.unavailable_reason || '請在官方三張財報完整後重新計算。');
    return;
  }
  status.textContent = `${payload?.symbol || '-'} · 估值日 ${payload?.valuation_date || '-'}`
    + ` · 財報 ${payload?.financial_period || '-'} · 可計算 ${payload?.available_count || 0} / ${metrics.length} 項。`;
  table.innerHTML = metrics.length
    ? `<table><thead><tr><th>指標</th><th>數值</th><th>一致公式</th><th>輸入／狀態</th></tr></thead><tbody>${metrics.map(item => {
      const renderedValue = item.value == null
        ? '不可計算'
        : `${monthlyRevenueMetric(item.value, 4)}${item.unit === 'percent' ? '%' : 'x'}`;
      const inputs = item.method === 'official_reported'
        ? '交易所當日公布'
        : `分子 ${monthlyRevenueMetric(item.numerator, 0)} / 分母 ${monthlyRevenueMetric(item.denominator, 0)}`;
      return `<tr>
        <td>${escapeHtml(item.label)}</td>
        <td>${escapeHtml(renderedValue)}</td>
        <td><code>${escapeHtml(item.formula)}</code></td>
        <td>${escapeHtml(item.status === 'available' ? inputs : item.unavailable_reason || '缺少必要輸入')}</td>
      </tr>`;
    }).join('')}</tbody></table>`
    : renderEmptyBlock('沒有估值結果', '請先同步官方三張財報與證券主檔。');
}

async function loadBasicValuation(symbol = null) {
  const selected = String(symbol || $('basicValuationSymbol')?.value || state.symbol || '').trim().toUpperCase();
  if (!selected) throw new Error('請輸入股票代號。');
  $('basicValuationSymbol').value = selected;
  $('basicValuationStatus').textContent = '正在對齊交易所估值日、官方股數與同期間三張財報…';
  const payload = await api(uiDataApi(`/fundamentals/valuation/basic?symbol=${encodeURIComponent(selected)}`));
  renderBasicValuation(payload);
  return payload;
}

function valuationClassification(value) {
  if (value === 'relative_low') return '相對低檔';
  if (value === 'relative_high') return '相對高檔';
  if (value === 'middle_range') return '中間區間';
  return '不可判斷';
}

function renderValuationPercentiles(payload) {
  const status = $('valuationPercentileStatus');
  const table = $('valuationPercentileTable');
  if (!status || !table) return;
  const metrics = Array.isArray(payload?.metrics) ? payload.metrics : [];
  const coverage = payload?.coverage || {};
  const sync = payload?.sync || null;
  status.textContent = `${payload?.symbol || '-'} · ${coverage.sample_count || 0} 個月樣本`
    + ` · ${coverage.first_date || '-'} ～ ${coverage.last_date || '-'}`
    + (sync ? ` · 本次成功 ${sync.stored_month_count}/${sync.requested_month_count} 月` : ' · 按按鈕同步官方歷史');
  table.innerHTML = metrics.length && coverage.sample_count
    ? `<table><thead><tr><th>指標／目前值</th><th>1 年</th><th>3 年</th><th>5 年</th><th>10 年</th></tr></thead><tbody>${metrics.map(metric => {
      const cells = (metric.windows || []).map(window => `<td>${window.percentile == null ? '不可判斷' : `${monthlyRevenueMetric(window.percentile)}% · ${valuationClassification(window.classification)}`}<br/><small>${window.sample_count}/${window.requested_months} 月 · ${escapeHtml(window.start_date || '-')} ～ ${escapeHtml(window.end_date || '-')}</small></td>`).join('');
      return `<tr><td>${escapeHtml(metric.label)}<br/><small>${metric.current_value == null ? '-' : monthlyRevenueMetric(metric.current_value, 4)} ${metric.unit === 'percent' ? '%' : 'x'}</small></td>${cells}</tr>`;
    }).join('')}</tbody></table>`
    : renderEmptyBlock('尚無歷史估值樣本', '按「同步並計算歷史分位」取得交易所逐月最後可用資料。');
}

async function loadValuationPercentiles(symbol = null, refresh = false) {
  const selected = String(symbol || $('valuationPercentileSymbol')?.value || state.symbol || '').trim().toUpperCase();
  if (!selected) throw new Error('請輸入股票代號。');
  $('valuationPercentileSymbol').value = selected;
  $('valuationPercentileStatus').textContent = refresh
    ? '正在同步最長十年交易所逐月估值，完成後計算 1／3／5／10 年分位…'
    : '正在讀取已保存歷史估值…';
  const payload = await api(uiDataApi(`/fundamentals/valuation/percentiles?symbol=${encodeURIComponent(selected)}&refresh=${refresh ? 'true' : 'false'}&months=120`));
  renderValuationPercentiles(payload);
  return payload;
}

const peerComparisonMetrics = [
  ['valuation', 'pe', '本益比 PE', 'x'],
  ['valuation', 'pb', '股價淨值比 PB', 'x'],
  ['valuation', 'dividend_yield_percent', '殖利率', '%'],
  ['operating', 'gross_margin_percent', '毛利率', '%'],
  ['operating', 'operating_margin_percent', '營業利益率', '%'],
  ['operating', 'net_margin_percent', '稅後淨利率', '%'],
  ['operating', 'roe_percent', 'ROE', '%'],
  ['operating', 'debt_ratio_percent', '負債比', '%'],
];

function renderPeerComparison(payload) {
  const status = $('peerComparisonStatus');
  const table = $('peerComparisonTable');
  if (!status || !table) return;
  const selection = payload?.selection || {};
  const companies = Array.isArray(payload?.companies) ? payload.companies : [];
  const rejected = Array.isArray(selection.rejected_peers) ? selection.rejected_peers : [];
  status.textContent = `${payload?.symbol || '-'} · 官方產業 ${payload?.industry || '-'}`
    + ` · 接受 ${selection.accepted_peers?.length || 0} 家`
    + (rejected.length ? ` · 拒絕 ${rejected.map(item => `${item.symbol}（${item.reason}）`).join('、')}` : '')
    + ` · 財報期間 ${payload?.period || '-'}`;
  if (!companies.length) {
    const preview = (selection.candidate_preview || []).slice(0, 10)
      .map(item => `${item.symbol} ${item.name || ''}`.trim()).join('、');
    table.innerHTML = renderEmptyBlock(
      '請明確輸入同業股票',
      preview ? `官方同產業候選：${preview}` : '目前沒有可用的官方同產業候選。',
    );
    return;
  }
  table.innerHTML = `<table><thead><tr><th>指標</th>${companies.map(company => `<th>${escapeHtml(company.symbol)}<br/><small>${escapeHtml(company.name || '')}</small></th>`).join('')}</tr></thead>
    <tbody>${peerComparisonMetrics.map(([section, field, label, unit]) => `<tr><td>${escapeHtml(label)}</td>${companies.map(company => {
      const value = company?.[section]?.[field];
      return `<td>${value == null ? '-' : `${monthlyRevenueMetric(value, 4)}${unit}`}</td>`;
    }).join('')}</tr>`).join('')}
    <tr><td>資料日期／期間</td>${companies.map(company => `<td>${escapeHtml(company.valuation_date || '-')}<br/><small>${escapeHtml(company.financial_period || '-')}</small></td>`).join('')}</tr></tbody></table>`;
}

async function loadPeerComparison(symbol = null) {
  const selected = String(symbol || $('peerComparisonSymbol')?.value || state.symbol || '').trim().toUpperCase();
  if (!selected) throw new Error('請輸入目標股票代號。');
  const peers = String($('peerComparisonPeers')?.value || '').trim().toUpperCase();
  $('peerComparisonSymbol').value = selected;
  $('peerComparisonStatus').textContent = '正在以官方證券主檔驗證同業，並讀取同日估值與同期間財務比率…';
  const params = new URLSearchParams({ symbol: selected, peers, refresh: 'true' });
  const payload = await api(uiDataApi(`/fundamentals/valuation/peers?${params.toString()}`));
  renderPeerComparison(payload);
  return payload;
}

function renderDcfValuation(payload) {
  const status = $('dcfValuationStatus');
  const table = $('dcfValuationTable');
  const sensitivity = $('dcfSensitivityTables');
  const scenarios = Array.isArray(payload?.scenarios) ? payload.scenarios : [];
  const range = payload?.scenario_range_per_share_twd || {};
  status.textContent = `情境估值範圍 ${monthlyRevenueMetric(range.minimum, 4)} ～ ${monthlyRevenueMetric(range.maximum, 4)} TWD／股`
    + ' · 僅為使用者假設模型，不是單一目標價或投資建議。';
  table.innerHTML = `<table><thead><tr><th>情境</th><th>每股隱含值</th><th>營收成長</th><th>毛利率</th><th>折現率</th><th>終值成長</th><th>企業價值</th></tr></thead>
    <tbody>${scenarios.map(item => `<tr><td>${escapeHtml(item.scenario)}</td><td>${monthlyRevenueMetric(item.implied_value_per_share_twd, 4)} TWD</td><td>${monthlyRevenueMetric(item.assumptions.revenue_growth_percent)}%</td><td>${monthlyRevenueMetric(item.assumptions.gross_margin_percent)}%</td><td>${monthlyRevenueMetric(item.assumptions.discount_rate_percent)}%</td><td>${monthlyRevenueMetric(item.assumptions.terminal_growth_percent)}%</td><td>${monthlyRevenueMetric(item.enterprise_value_thousand_twd, 0)} 千元</td></tr>`).join('')}</tbody></table>`;
  sensitivity.innerHTML = (payload?.sensitivity || []).map(matrix => `<section><h4>${escapeHtml(matrix.row_field)} × ${escapeHtml(matrix.column_field)}</h4><table><thead><tr><th>${escapeHtml(matrix.row_field)} \\ ${escapeHtml(matrix.column_field)}</th>${matrix.column_values.map(value => `<th>${monthlyRevenueMetric(value)}%</th>`).join('')}</tr></thead><tbody>${matrix.row_values.map((rowValue, index) => `<tr><td>${monthlyRevenueMetric(rowValue)}%</td>${(matrix.cells_implied_value_per_share_twd[index] || []).map(value => `<td>${value == null ? '-' : monthlyRevenueMetric(value, 2)}</td>`).join('')}</tr>`).join('')}</tbody></table></section>`).join('');
}

async function runDcfValuation() {
  const params = new URLSearchParams({
    base_revenue: $('dcfRevenue').value,
    gross_margin_percent: $('dcfGrossMargin').value,
    fcf_conversion_percent: $('dcfConversion').value,
    revenue_growth_percent: $('dcfGrowth').value,
    discount_rate_percent: $('dcfDiscount').value,
    terminal_growth_percent: $('dcfTerminal').value,
    net_debt: $('dcfNetDebt').value,
    shares_outstanding: $('dcfShares').value,
    forecast_years: $('dcfYears').value,
  });
  $('dcfValuationStatus').textContent = '正在計算三情境、逐年現金流與兩組敏感度矩陣…';
  const payload = await api(uiDataApi(`/fundamentals/valuation/dcf?${params.toString()}`));
  renderDcfValuation(payload);
  return payload;
}

function renderValuationPolicy(payload) {
  const selection = payload?.selection || {};
  const applicable = selection.applicable_models || [];
  const excluded = selection.excluded_models || [];
  $('valuationPolicyStatus').textContent = `${selection.industry_profile || '-'} · 適用 ${applicable.length} 個模型 · 排除 ${excluded.length} 個模型 · 輸入歸類為模型假設`;
  $('valuationPolicyTable').innerHTML = `<table><thead><tr><th>狀態</th><th>模型</th><th>角色／理由</th></tr></thead><tbody>
    ${applicable.map(item => `<tr><td>適用</td><td>${escapeHtml(item.model)}</td><td>${escapeHtml(item.role)} · ${escapeHtml(item.reason)}</td></tr>`).join('')}
    ${excluded.map(item => `<tr><td>排除</td><td>${escapeHtml(item.model)}</td><td>${escapeHtml(item.reason)}</td></tr>`).join('')}
    </tbody></table><p><strong>證據分層：</strong>歷史事實 reported_fact · 公司指引 company_guidance · 分析師預估 analyst_estimate · 模型假設 model_assumption（本次輸入）</p>`;
}

async function runValuationPolicy() {
  const params = new URLSearchParams({
    industry_profile: $('valuationPolicyIndustry').value,
    profitable: $('valuationPolicyProfitable').checked,
    positive_free_cash_flow: $('valuationPolicyFcf').checked,
    pays_dividend: $('valuationPolicyDividend').checked,
    asset_heavy: $('valuationPolicyAssetHeavy').checked,
    high_growth: $('valuationPolicyHighGrowth').checked,
  });
  const payload = await api(uiDataApi(`/fundamentals/valuation/model-policy?${params.toString()}`));
  renderValuationPolicy(payload);
  return payload;
}

function monthlyRevenueMetric(value, fractionDigits = 2) {
  return typeof value === 'number' && Number.isFinite(value)
    ? value.toLocaleString(undefined, { maximumFractionDigits: fractionDigits })
    : '-';
}

function monthlyRevenuePercent(value) {
  return typeof value === 'number' && Number.isFinite(value)
    ? `${monthlyRevenueMetric(value)}%`
    : '-';
}

function monthlyRevenueRange() {
  const symbol = String($('monthlyRevenueSymbol')?.value || state.symbol || '').trim().toUpperCase();
  const startPeriod = String($('monthlyRevenueStart')?.value || '2010-01').trim();
  const endPeriod = String($('monthlyRevenueEnd')?.value || '').trim();
  if (!symbol) throw new Error('請輸入股票代號。');
  if (!startPeriod) throw new Error('請選擇起始月份。');
  return { symbol, startPeriod, endPeriod };
}

function renderMonthlyRevenueHistory(payload) {
  const status = $('monthlyRevenueHistoryStatus');
  const table = $('monthlyRevenueHistoryTable');
  if (!status || !table) return;
  const coverage = payload?.coverage || {};
  const items = Array.isArray(payload?.items) ? payload.items : [];
  const sync = payload?.sync || null;
  const syncText = sync
    ? `；本次下載 ${sync.fetched_period_count} 月、略過 ${sync.skipped_period_count} 月、失敗 ${sync.failed_periods?.length || 0} 月`
    : '';
  status.textContent = `${payload.symbol} 已保存 ${coverage.stored_period_count || 0} / ${coverage.requested_period_count || 0} 月`
    + `（${coverage.first_stored_period || '-'} ～ ${coverage.last_stored_period || '-'}）${syncText}。`
    + ' 歷史封存未提供原始發布時間，available_at 採實際取得時間。';
  if (!items.length) {
    table.innerHTML = renderEmptyBlock('尚無月營收歷史', '按「同步官方歷史」從 MOPS 官方封存逐月保存。');
    return;
  }
  table.innerHTML = `
    <table>
      <thead><tr>
        <th>月份</th><th>當月營收</th><th>MoM</th><th>YoY</th>
        <th>累計營收</th><th>累計年增</th><th>來源</th>
      </tr></thead>
      <tbody>${items.map(item => `
        <tr>
          <td>${escapeHtml(item.period)}</td>
          <td>${monthlyRevenueMetric(item.current_revenue, 0)}</td>
          <td>${monthlyRevenuePercent(item.mom_change_percent)}</td>
          <td>${monthlyRevenuePercent(item.yoy_change_percent)}</td>
          <td>${monthlyRevenueMetric(item.ytd_revenue, 0)}</td>
          <td>${monthlyRevenuePercent(item.ytd_change_percent)}</td>
          <td>${item.source_url
            ? `<a href="${escapeHtml(item.source_url)}" target="_blank" rel="noreferrer">${escapeHtml(item.source_id || item.source)}</a>`
            : escapeHtml(item.source_id || item.source || '-')}</td>
        </tr>`).join('')}</tbody>
    </table>`;
}

async function loadMonthlyRevenueHistory(symbol = null) {
  if (symbol && $('monthlyRevenueSymbol')) $('monthlyRevenueSymbol').value = symbol;
  const range = monthlyRevenueRange();
  const params = new URLSearchParams({
    symbol: range.symbol,
    start_period: range.startPeriod,
    limit: '240',
  });
  if (range.endPeriod) params.set('end_period', range.endPeriod);
  $('monthlyRevenueHistoryStatus').textContent = '正在查詢統一資料倉庫中的月營收歷史…';
  const payload = await api(uiDataApi(`/fundamentals/revenue/history?${params.toString()}`));
  renderMonthlyRevenueHistory(payload);
  return payload;
}

async function syncMonthlyRevenueHistory() {
  const range = monthlyRevenueRange();
  const button = $('syncMonthlyRevenueHistoryBtn');
  button.disabled = true;
  $('monthlyRevenueHistoryStatus').textContent = '正在逐月下載、解析並保存 MOPS 官方歷史封存…';
  try {
    const payload = await api(uiDataApi('/fundamentals/revenue/history/sync'), {
      method: 'POST',
      body: JSON.stringify({
        symbol: range.symbol,
        start_period: range.startPeriod,
        end_period: range.endPeriod || null,
      }),
    });
    renderMonthlyRevenueHistory(payload);
    return payload;
  } finally {
    button.disabled = false;
  }
}

function incomeStatementDefaultEnd() {
  const cutoff = new Date();
  cutoff.setDate(cutoff.getDate() - 90);
  const currentYear = new Date().getFullYear();
  for (let year = currentYear; year >= 2013; year -= 1) {
    for (let quarter = 4; quarter >= 1; quarter -= 1) {
      const quarterEnd = new Date(year, quarter * 3, 0);
      if (quarterEnd <= cutoff) return `${year}-Q${quarter}`;
    }
  }
  return '2013-Q1';
}

function initializeIncomeStatementPeriods() {
  const start = $('incomeStatementStart');
  const end = $('incomeStatementEnd');
  if (!start || !end || start.options.length) return;
  const finalPeriod = incomeStatementDefaultEnd();
  const finalYear = Number(finalPeriod.slice(0, 4));
  const finalQuarter = Number(finalPeriod.slice(-1));
  const periods = [];
  for (let year = 2013; year <= finalYear; year += 1) {
    for (let quarter = 1; quarter <= 4; quarter += 1) {
      if (year === finalYear && quarter > finalQuarter) break;
      periods.push(`${year}-Q${quarter}`);
    }
  }
  start.innerHTML = periods.map(period => `<option value="${period}">${period}</option>`).join('');
  end.innerHTML = periods.map(period => `<option value="${period}">${period}</option>`).join('');
  start.value = '2013-Q1';
  end.value = finalPeriod;
}

function incomeStatementRange() {
  initializeIncomeStatementPeriods();
  const symbol = String($('incomeStatementSymbol')?.value || state.symbol || '').trim().toUpperCase();
  const startPeriod = String($('incomeStatementStart')?.value || '2013-Q1').trim();
  const endPeriod = String($('incomeStatementEnd')?.value || '').trim();
  if (!symbol) throw new Error('請輸入股票代號。');
  if (!startPeriod || !endPeriod) throw new Error('請選擇起訖季度。');
  return { symbol, startPeriod, endPeriod };
}

function incomeStatementPair(item, field, fractionDigits = 0) {
  const reported = monthlyRevenueMetric(item?.[field], fractionDigits);
  const quarterField = `current_quarter_${field}`;
  const singleQuarter = monthlyRevenueMetric(item?.[quarterField], fractionDigits);
  if (item?.quarter === 4) return `<strong>${reported}</strong><small>年度 / 單季未揭露</small>`;
  if (item?.quarter === 1) return `<strong>${reported}</strong><small>Q1 = 累計</small>`;
  return `<strong>${reported}</strong><small>累計 / 單季 ${singleQuarter}</small>`;
}

function renderIncomeStatementHistory(payload) {
  const status = $('incomeStatementHistoryStatus');
  const table = $('incomeStatementHistoryTable');
  if (!status || !table) return;
  const coverage = payload?.coverage || {};
  const items = Array.isArray(payload?.items) ? payload.items : [];
  const sync = payload?.sync || null;
  const syncText = sync
    ? `；本次下載 ${sync.fetched_period_count} 季、略過 ${sync.skipped_period_count} 季、失敗 ${sync.failed_periods?.length || 0} 季`
    : '';
  status.textContent = `${payload.symbol} 已保存 ${coverage.stored_period_count || 0} / ${coverage.requested_period_count || 0} 季`
    + `，涵蓋 ${coverage.covered_years || 0} 年（${coverage.first_stored_period || '-'} ～ ${coverage.last_stored_period || '-'}）${syncText}。`
    + ' 主欄為官方年初至今累計；Q2/Q3 另列官方單季，Q4 不自行推算；原始發布時間未知。';
  if (!items.length) {
    table.innerHTML = renderEmptyBlock('尚無損益表歷史', '按「同步官方歷史」從 MOPS 官方 IFRS 歷史站逐季保存。');
    return;
  }
  table.innerHTML = `
    <table>
      <thead><tr>
        <th>季度</th><th>營收</th><th>毛利</th><th>營業利益</th>
        <th>稅後利益</th><th>EPS</th><th>報表 / 來源</th>
      </tr></thead>
      <tbody>${items.map(item => `
        <tr>
          <td>${escapeHtml(item.period)}</td>
          <td>${incomeStatementPair(item, 'revenue')}</td>
          <td>${incomeStatementPair(item, 'gross_profit')}</td>
          <td>${incomeStatementPair(item, 'operating_income')}</td>
          <td>${incomeStatementPair(item, 'net_income')}</td>
          <td>${incomeStatementPair(item, 'eps', 2)}</td>
          <td><strong>${escapeHtml(item.statement_scope || '綜合損益表')}</strong>
            <small>${item.source_url
              ? `<a href="${escapeHtml(item.source_url)}" target="_blank" rel="noreferrer">${escapeHtml(item.source_id || item.source)}</a>`
              : escapeHtml(item.source_id || item.source || '-')}</small></td>
        </tr>`).join('')}</tbody>
    </table>`;
}

async function loadIncomeStatementHistory(symbol = null) {
  initializeIncomeStatementPeriods();
  if (symbol && $('incomeStatementSymbol')) $('incomeStatementSymbol').value = symbol;
  const range = incomeStatementRange();
  const params = new URLSearchParams({
    symbol: range.symbol,
    start_period: range.startPeriod,
    end_period: range.endPeriod,
    limit: '64',
  });
  $('incomeStatementHistoryStatus').textContent = '正在查詢統一資料倉庫中的損益表歷史…';
  const payload = await api(uiDataApi(`/fundamentals/income-statement/history?${params.toString()}`));
  renderIncomeStatementHistory(payload);
  return payload;
}

async function syncIncomeStatementHistory() {
  const range = incomeStatementRange();
  const button = $('syncIncomeStatementHistoryBtn');
  button.disabled = true;
  $('incomeStatementHistoryStatus').textContent = '正在逐季下載、解析並保存 MOPS 官方 IFRS 損益表…';
  try {
    const payload = await api(uiDataApi('/fundamentals/income-statement/history/sync'), {
      method: 'POST',
      body: JSON.stringify({
        symbol: range.symbol,
        start_period: range.startPeriod,
        end_period: range.endPeriod,
      }),
    });
    renderIncomeStatementHistory(payload);
    return payload;
  } finally {
    button.disabled = false;
  }
}

function initializeBalanceSheetPeriods() {
  initializeIncomeStatementPeriods();
  const start = $('balanceSheetStart');
  const end = $('balanceSheetEnd');
  if (!start || !end || start.options.length) return;
  start.innerHTML = $('incomeStatementStart')?.innerHTML || '';
  end.innerHTML = $('incomeStatementEnd')?.innerHTML || '';
  start.value = $('incomeStatementStart')?.value || '2013-Q1';
  end.value = $('incomeStatementEnd')?.value || '';
}

function balanceSheetRange() {
  initializeBalanceSheetPeriods();
  const symbol = String($('balanceSheetSymbol')?.value || state.symbol || '').trim().toUpperCase();
  const startPeriod = String($('balanceSheetStart')?.value || '2013-Q1').trim();
  const endPeriod = String($('balanceSheetEnd')?.value || '').trim();
  if (!symbol) throw new Error('請輸入股票代號。');
  if (!startPeriod || !endPeriod) throw new Error('請選擇起訖季度。');
  return { symbol, startPeriod, endPeriod };
}

function balanceSheetMetric(item, field) {
  const value = monthlyRevenueMetric(item?.[field]);
  const comparison = item?.quarter_comparison?.fields?.[field] || {};
  const change = comparison.change;
  const percent = comparison.change_percent;
  const comparisonText = change == null
    ? '無前季'
    : `前季 ${change >= 0 ? '+' : ''}${monthlyRevenueMetric(change)}`
      + `${percent == null ? '' : `（${percent >= 0 ? '+' : ''}${Number(percent).toFixed(2)}%）`}`;
  return `<strong>${value}</strong><small>${escapeHtml(comparisonText)}</small>`;
}

function renderBalanceSheetHistory(payload) {
  const status = $('balanceSheetHistoryStatus');
  const table = $('balanceSheetHistoryTable');
  if (!status || !table) return;
  const coverage = payload?.coverage || {};
  const items = Array.isArray(payload?.items) ? payload.items : [];
  const sync = payload?.sync || null;
  const syncText = sync
    ? `；本次下載 ${sync.fetched_period_count} 季、略過 ${sync.skipped_period_count} 季、失敗 ${sync.failed_periods?.length || 0} 季`
    : '';
  status.textContent = `${payload.symbol} 已保存 ${coverage.stored_period_count || 0} / ${coverage.requested_period_count || 0} 季`
    + `，涵蓋 ${coverage.covered_years || 0} 年（${coverage.first_stored_period || '-'} ～ ${coverage.last_stored_period || '-'}）${syncText}。`
    + ' 數值為官方期末金額；前季差額與百分比由相鄰已保存季度計算，原始發布時間未知。';
  if (!items.length) {
    table.innerHTML = renderEmptyBlock('尚無資產負債表歷史', '按「同步官方歷史」從 MOPS 官方 IFRS 歷史站逐季保存。');
    return;
  }
  table.innerHTML = `
    <table>
      <thead><tr>
        <th>季度</th><th>現金</th><th>總資產</th><th>總負債</th>
        <th>股東權益</th><th>存貨</th><th>應收款</th><th>報表 / 來源</th>
      </tr></thead>
      <tbody>${items.map(item => `
        <tr>
          <td>${escapeHtml(item.period)}</td>
          <td>${balanceSheetMetric(item, 'cash_and_cash_equivalents')}</td>
          <td>${balanceSheetMetric(item, 'total_assets')}</td>
          <td>${balanceSheetMetric(item, 'total_liabilities')}</td>
          <td>${balanceSheetMetric(item, 'total_equity')}</td>
          <td>${balanceSheetMetric(item, 'inventory')}</td>
          <td>${balanceSheetMetric(item, 'accounts_receivable')}</td>
          <td><strong>${escapeHtml(item.statement_scope || '資產負債表')}</strong>
            <small>${item.source_url
              ? `<a href="${escapeHtml(item.source_url)}" target="_blank" rel="noreferrer">${escapeHtml(item.source_id || item.source)}</a>`
              : escapeHtml(item.source_id || item.source || '-')}</small></td>
        </tr>`).join('')}</tbody>
    </table>`;
}

async function loadBalanceSheetHistory(symbol = null) {
  initializeBalanceSheetPeriods();
  if (symbol && $('balanceSheetSymbol')) $('balanceSheetSymbol').value = symbol;
  const range = balanceSheetRange();
  const params = new URLSearchParams({
    symbol: range.symbol,
    start_period: range.startPeriod,
    end_period: range.endPeriod,
    limit: '64',
  });
  $('balanceSheetHistoryStatus').textContent = '正在查詢統一資料倉庫中的資產負債表歷史…';
  const payload = await api(uiDataApi(`/fundamentals/balance-sheet/history?${params.toString()}`));
  renderBalanceSheetHistory(payload);
  return payload;
}

async function syncBalanceSheetHistory() {
  const range = balanceSheetRange();
  const button = $('syncBalanceSheetHistoryBtn');
  button.disabled = true;
  $('balanceSheetHistoryStatus').textContent = '正在逐季下載、解析並保存 MOPS 官方 IFRS 資產負債表…';
  try {
    const payload = await api(uiDataApi('/fundamentals/balance-sheet/history/sync'), {
      method: 'POST',
      body: JSON.stringify({
        symbol: range.symbol,
        start_period: range.startPeriod,
        end_period: range.endPeriod,
      }),
    });
    renderBalanceSheetHistory(payload);
    return payload;
  } finally {
    button.disabled = false;
  }
}

function initializeCashFlowPeriods() {
  initializeBalanceSheetPeriods();
  const start = $('cashFlowStart');
  const end = $('cashFlowEnd');
  if (!start || !end || start.options.length) return;
  start.innerHTML = $('balanceSheetStart')?.innerHTML || '';
  end.innerHTML = $('balanceSheetEnd')?.innerHTML || '';
  start.value = $('balanceSheetStart')?.value || '2013-Q1';
  end.value = $('balanceSheetEnd')?.value || '';
}

function cashFlowRange() {
  initializeCashFlowPeriods();
  const symbol = String($('cashFlowSymbol')?.value || state.symbol || '').trim().toUpperCase();
  const startPeriod = String($('cashFlowStart')?.value || '2013-Q1').trim();
  const endPeriod = String($('cashFlowEnd')?.value || '').trim();
  if (!symbol) throw new Error('請輸入股票代號。');
  if (!startPeriod || !endPeriod) throw new Error('請選擇起訖季度。');
  return { symbol, startPeriod, endPeriod };
}

function cashFlowQuality(item) {
  const quality = item?.profit_quality || {};
  const labels = {
    strong_cash_conversion: '現金轉換強',
    aligned: '獲利與現金一致',
    weak_cash_conversion: '現金轉換偏弱',
    negative_operating_cash_flow: '營業現金流為負',
    insufficient_data: '缺同期間稅後利益',
  };
  const ratio = quality.operating_cash_flow_to_net_income;
  return `<strong>${escapeHtml(labels[quality.status] || '無法判斷')}</strong>`
    + `<small>${ratio == null ? '轉換率無資料' : `營業現金流 / 稅後利益 ${Number(ratio).toFixed(2)}x`}</small>`;
}

function renderCashFlowHistory(payload) {
  const status = $('cashFlowHistoryStatus');
  const table = $('cashFlowHistoryTable');
  if (!status || !table) return;
  const coverage = payload?.coverage || {};
  const items = Array.isArray(payload?.items) ? payload.items : [];
  const sync = payload?.sync || null;
  const syncText = sync
    ? `；本次下載 ${sync.fetched_period_count} 季、略過 ${sync.skipped_period_count} 季、失敗 ${sync.failed_periods?.length || 0} 季`
    : '';
  status.textContent = `${payload.symbol} 已保存 ${coverage.stored_period_count || 0} / ${coverage.requested_period_count || 0} 季`
    + `，涵蓋 ${coverage.covered_years || 0} 年（${coverage.first_stored_period || '-'} ～ ${coverage.last_stored_period || '-'}）${syncText}。`
    + ' 主欄為官方累計值；自由現金流 = 營業現金流 − |資本支出|，獲利品質使用同期間官方稅後利益。';
  if (!items.length) {
    table.innerHTML = renderEmptyBlock('尚無現金流量表歷史', '按「同步官方歷史」從 MOPS 官方 IFRS 歷史站逐季保存。');
    return;
  }
  table.innerHTML = `
    <table>
      <thead><tr>
        <th>季度</th><th>營業</th><th>投資</th><th>融資</th>
        <th>資本支出</th><th>自由現金流</th><th>獲利品質</th><th>報表 / 來源</th>
      </tr></thead>
      <tbody>${items.map(item => `
        <tr>
          <td>${escapeHtml(item.period)}</td>
          <td>${monthlyRevenueMetric(item.operating_cash_flow)}</td>
          <td>${monthlyRevenueMetric(item.investing_cash_flow)}</td>
          <td>${monthlyRevenueMetric(item.financing_cash_flow)}</td>
          <td>${monthlyRevenueMetric(item.capital_expenditure)}</td>
          <td><strong>${monthlyRevenueMetric(item.free_cash_flow)}</strong>
            <small>營業 − |資本支出|</small></td>
          <td>${cashFlowQuality(item)}</td>
          <td><strong>${escapeHtml(item.statement_scope || '現金流量表')}</strong>
            <small>${item.source_url
              ? `<a href="${escapeHtml(item.source_url)}" target="_blank" rel="noreferrer">${escapeHtml(item.source_id || item.source)}</a>`
              : escapeHtml(item.source_id || item.source || '-')}</small></td>
        </tr>`).join('')}</tbody>
    </table>`;
}

async function loadCashFlowHistory(symbol = null) {
  initializeCashFlowPeriods();
  if (symbol && $('cashFlowSymbol')) $('cashFlowSymbol').value = symbol;
  const range = cashFlowRange();
  const params = new URLSearchParams({
    symbol: range.symbol,
    start_period: range.startPeriod,
    end_period: range.endPeriod,
    limit: '64',
  });
  $('cashFlowHistoryStatus').textContent = '正在查詢統一資料倉庫中的現金流量表歷史…';
  const payload = await api(uiDataApi(`/fundamentals/cash-flow/history?${params.toString()}`));
  renderCashFlowHistory(payload);
  return payload;
}

async function syncCashFlowHistory() {
  const range = cashFlowRange();
  const button = $('syncCashFlowHistoryBtn');
  button.disabled = true;
  $('cashFlowHistoryStatus').textContent = '正在逐季下載、解析並保存 MOPS 官方 IFRS 現金流量表…';
  try {
    const payload = await api(uiDataApi('/fundamentals/cash-flow/history/sync'), {
      method: 'POST',
      body: JSON.stringify({
        symbol: range.symbol,
        start_period: range.startPeriod,
        end_period: range.endPeriod,
      }),
    });
    renderCashFlowHistory(payload);
    return payload;
  } finally {
    button.disabled = false;
  }
}

function initializeFinancialRatioPeriods() {
  initializeCashFlowPeriods();
  const start = $('financialRatioStart');
  const end = $('financialRatioEnd');
  if (!start || !end || start.options.length) return;
  start.innerHTML = $('cashFlowStart')?.innerHTML || '';
  end.innerHTML = $('cashFlowEnd')?.innerHTML || '';
  start.value = $('cashFlowStart')?.value || '2013-Q1';
  end.value = $('cashFlowEnd')?.value || '';
}

function financialRatioRange() {
  initializeFinancialRatioPeriods();
  const symbol = String($('financialRatioSymbol')?.value || state.symbol || '').trim().toUpperCase();
  const startPeriod = String($('financialRatioStart')?.value || '2013-Q1').trim();
  const endPeriod = String($('financialRatioEnd')?.value || '').trim();
  if (!symbol) throw new Error('請輸入股票代號。');
  if (!startPeriod || !endPeriod) throw new Error('請選擇起訖季度。');
  return { symbol, startPeriod, endPeriod };
}

function ratioPercent(value) {
  return value == null ? '無資料' : `${Number(value).toFixed(2)}%`;
}

function renderFinancialRatioHistory(payload) {
  const status = $('financialRatioHistoryStatus');
  const table = $('financialRatioHistoryTable');
  if (!status || !table) return;
  const coverage = payload?.coverage || {};
  const items = Array.isArray(payload?.items) ? payload.items : [];
  status.textContent = `${payload.symbol} 已計算 ${coverage.ratio_period_count || 0} / ${coverage.requested_period_count || 0} 季；`
    + `損益表 ${coverage.income_statement_period_count || 0} 季、資產負債表 ${coverage.balance_sheet_period_count || 0} 季。`
    + ' 比率只使用同期間官方數值；ROE/ROA 優先使用相鄰季度平均餘額。';
  if (!items.length) {
    table.innerHTML = renderEmptyBlock('尚無可計算比率', '請先在上方同步同期間的損益表與資產負債表。');
    return;
  }
  table.innerHTML = `
    <table>
      <thead><tr>
        <th>季度</th><th>毛利率</th><th>營益率</th><th>淨利率</th>
        <th>ROE</th><th>ROA</th><th>負債比</th><th>來源對照</th>
      </tr></thead>
      <tbody>${items.map(item => {
        const sources = item.source_comparison || {};
        const income = sources.income_statement || {};
        const balance = sources.balance_sheet || {};
        const denominator = item.calculation_contract || {};
        return `<tr>
          <td>${escapeHtml(item.period)}</td>
          <td>${ratioPercent(item.gross_margin_percent)}</td>
          <td>${ratioPercent(item.operating_margin_percent)}</td>
          <td>${ratioPercent(item.net_margin_percent)}</td>
          <td><strong>${ratioPercent(item.roe_percent)}</strong><small>${escapeHtml(denominator.roe_denominator_basis || '-')}</small></td>
          <td><strong>${ratioPercent(item.roa_percent)}</strong><small>${escapeHtml(denominator.roa_denominator_basis || '-')}</small></td>
          <td>${ratioPercent(item.debt_ratio_percent)}</td>
          <td><strong>${sources.periods_match ? '同期間' : '期間不符'}</strong><small>
            ${income.source_url ? `<a href="${escapeHtml(income.source_url)}" target="_blank" rel="noreferrer">損益表</a>` : '損益表無來源'}
            · ${balance.source_url ? `<a href="${escapeHtml(balance.source_url)}" target="_blank" rel="noreferrer">資產負債表</a>` : '資產負債表無來源'}
          </small></td>
        </tr>`;
      }).join('')}</tbody>
    </table>`;
}

async function loadFinancialRatioHistory(symbol = null) {
  initializeFinancialRatioPeriods();
  if (symbol && $('financialRatioSymbol')) $('financialRatioSymbol').value = symbol;
  const range = financialRatioRange();
  const params = new URLSearchParams({
    symbol: range.symbol,
    start_period: range.startPeriod,
    end_period: range.endPeriod,
    limit: '64',
  });
  $('financialRatioHistoryStatus').textContent = '正在以一致公式計算財務比率並核對來源期間…';
  const payload = await api(uiDataApi(`/fundamentals/ratios/history?${params.toString()}`));
  renderFinancialRatioHistory(payload);
  return payload;
}

function initializeGrowthPeriods() {
  initializeFinancialRatioPeriods();
  const start = $('growthQuarterlyStart');
  const end = $('growthQuarterlyEnd');
  if (!start || !end || start.options.length) return;
  start.innerHTML = $('financialRatioStart')?.innerHTML || '';
  end.innerHTML = $('financialRatioEnd')?.innerHTML || '';
  start.value = '2013-Q1';
  end.value = $('financialRatioEnd')?.value || '';
}

function growthRange() {
  initializeGrowthPeriods();
  const symbol = String($('growthSymbol')?.value || state.symbol || '').trim().toUpperCase();
  const monthlyStart = String($('growthMonthlyStart')?.value || '2024-01').trim();
  const monthlyEnd = String($('growthMonthlyEnd')?.value || '').trim();
  const quarterlyStart = String($('growthQuarterlyStart')?.value || '2013-Q1').trim();
  const quarterlyEnd = String($('growthQuarterlyEnd')?.value || '').trim();
  if (!symbol) throw new Error('請輸入股票代號。');
  if (!monthlyStart || !quarterlyStart || !quarterlyEnd) throw new Error('請選擇完整的月、季資料範圍。');
  return { symbol, monthlyStart, monthlyEnd, quarterlyStart, quarterlyEnd };
}

function growthPercent(value) {
  return value == null ? '無資料' : `${Number(value).toFixed(2)}%`;
}

function renderGrowthHistory(payload) {
  const status = $('growthHistoryStatus');
  const table = $('growthHistoryTable');
  if (!status || !table) return;
  const monthly = payload?.monthly || {};
  const quarterly = payload?.quarterly || {};
  const annual = payload?.annual || {};
  const months = Array.isArray(monthly.items) ? monthly.items : [];
  const quarters = Array.isArray(quarterly.items) ? quarterly.items : [];
  const years = Array.isArray(annual.items) ? annual.items : [];
  const available = annual.available_range_cagr || null;
  status.textContent = `${payload.symbol}：月 ${months.length} 期、季 ${quarters.length} 期、年度 ${years.length} 年。`
    + `${available ? ` 可用區間 ${available.start_year}–${available.end_year} CAGR ${growthPercent(available.percent)}。` : ' CAGR 端點不足。'}`
    + ' 季增只使用連續官方單季值，Q4 未揭露單季時保持無資料。';
  if (!months.length && !quarters.length && !years.length) {
    table.innerHTML = renderEmptyBlock('尚無可計算成長率', '請先同步上方的月營收與損益表歷史。');
    return;
  }
  table.innerHTML = `
    <h4>月成長（官方揭露）</h4>
    <table><thead><tr><th>月份</th><th>營收</th><th>MoM</th><th>YoY</th><th>累計 YoY</th><th>來源</th></tr></thead>
      <tbody>${months.slice(0, 24).map(item => `<tr>
        <td>${escapeHtml(item.period)}</td><td>${monthlyRevenueMetric(item.revenue)}</td>
        <td>${growthPercent(item.mom_percent)}</td><td>${growthPercent(item.yoy_percent)}</td>
        <td>${growthPercent(item.ytd_yoy_percent)}</td>
        <td>${item.source_url ? `<a href="${escapeHtml(item.source_url)}" target="_blank" rel="noreferrer">${escapeHtml(item.source_id || 'MOPS')}</a>` : '-'}</td>
      </tr>`).join('')}</tbody></table>
    <h4>季成長（連續官方單季）</h4>
    <table><thead><tr><th>季度</th><th>單季營收</th><th>QoQ</th><th>比較期</th><th>狀態</th><th>來源</th></tr></thead>
      <tbody>${quarters.slice(0, 24).map(item => `<tr>
        <td>${escapeHtml(item.period)}</td><td>${monthlyRevenueMetric(item.revenue)}</td>
        <td>${growthPercent(item.qoq_percent)}</td><td>${escapeHtml(item.comparison_period || '-')}</td>
        <td>${escapeHtml(item.comparison_status)}</td>
        <td>${item.source_url ? `<a href="${escapeHtml(item.source_url)}" target="_blank" rel="noreferrer">${escapeHtml(item.source_id || 'MOPS')}</a>` : '-'}</td>
      </tr>`).join('')}</tbody></table>
    <h4>年成長與多年 CAGR</h4>
    <table><thead><tr><th>年度</th><th>營收</th><th>YoY</th><th>3 年 CAGR</th><th>5 年 CAGR</th><th>10 年 CAGR</th><th>來源</th></tr></thead>
      <tbody>${years.map(item => `<tr>
        <td>${item.year}</td><td>${monthlyRevenueMetric(item.revenue)}</td>
        <td>${growthPercent(item.yoy_percent)}</td><td>${growthPercent(item.cagr_3y_percent)}</td>
        <td>${growthPercent(item.cagr_5y_percent)}</td><td>${growthPercent(item.cagr_10y_percent)}</td>
        <td>${item.source_url ? `<a href="${escapeHtml(item.source_url)}" target="_blank" rel="noreferrer">${escapeHtml(item.source_id || 'MOPS')}</a>` : '-'}</td>
      </tr>`).join('')}</tbody></table>`;
}

async function loadGrowthHistory(symbol = null) {
  initializeGrowthPeriods();
  if (symbol && $('growthSymbol')) $('growthSymbol').value = symbol;
  const range = growthRange();
  const params = new URLSearchParams({
    symbol: range.symbol,
    monthly_start_period: range.monthlyStart,
    quarterly_start_period: range.quarterlyStart,
    quarterly_end_period: range.quarterlyEnd,
  });
  if (range.monthlyEnd) params.set('monthly_end_period', range.monthlyEnd);
  $('growthHistoryStatus').textContent = '正在分別計算月、季、年與多年 CAGR…';
  const payload = await api(uiDataApi(`/fundamentals/growth/history?${params.toString()}`));
  renderGrowthHistory(payload);
  return payload;
}

function financialRevisionQuery() {
  const symbol = String($('financialRevisionSymbol')?.value || state.symbol || '').trim().toUpperCase();
  const statementKind = String($('financialRevisionKind')?.value || 'income_statement').trim();
  const period = String($('financialRevisionPeriod')?.value || '').trim().toUpperCase();
  const knowledgeLocal = String($('financialRevisionKnowledgeAt')?.value || '').trim();
  if (!symbol) throw new Error('請輸入股票代號。');
  if (!period) throw new Error('請輸入要檢查修訂的財報期間。');
  const monthly = statementKind === 'monthly_revenue';
  const periodPattern = monthly ? /^\d{4}-(0[1-9]|1[0-2])$/ : /^\d{4}-Q[1-4]$/;
  if (!periodPattern.test(period)) {
    throw new Error(monthly ? '月營收期間請使用 YYYY-MM。' : '財報期間請使用 YYYY-Q1 至 YYYY-Q4。');
  }
  let knowledgeAt = '';
  if (knowledgeLocal) {
    const parsed = new Date(knowledgeLocal);
    if (Number.isNaN(parsed.getTime())) throw new Error('當時已知時間格式無效。');
    knowledgeAt = parsed.toISOString();
  }
  return { symbol, statementKind, period, knowledgeAt };
}

function financialRevisionValue(value) {
  if (value === null || value === undefined || value === '') return '無資料';
  if (typeof value === 'number') return Number.isFinite(value) ? niceNumber(value) : '無資料';
  return String(value);
}

function financialRevisionChanges(version) {
  const changes = Array.isArray(version?.changes) ? version.changes : [];
  if (!changes.length) {
    return version?.comparison_status === 'original'
      ? '原始保存版本，沒有前一版本可比較'
      : '欄位值未變；僅來源中繼資料或擷取時間更新';
  }
  return changes.map(change => {
    const percent = change.percent_change == null
      ? ''
      : `（${Number(change.percent_change).toFixed(2)}%）`;
    return `${escapeHtml(change.field)}：${escapeHtml(financialRevisionValue(change.before))}`
      + ` → ${escapeHtml(financialRevisionValue(change.after))}${escapeHtml(percent)}`;
  }).join('<br/>');
}

function renderFinancialRevisionHistory(payload) {
  const status = $('financialRevisionHistoryStatus');
  const table = $('financialRevisionHistoryTable');
  if (!status || !table) return;
  const versions = Array.isArray(payload?.versions) ? payload.versions : [];
  const pointInTime = payload?.point_in_time || {};
  const selected = Array.isArray(pointInTime.selected_versions) ? pointInTime.selected_versions : [];
  const cutoffText = pointInTime.selection_mode === 'explicit_cutoff'
    ? `知識截止 ${pointInTime.knowledge_at || '-'}`
    : '目前最新可得版本';
  status.textContent = `${payload.symbol} ${payload.period}：保存 ${payload.version_count || 0} 個不可變版本，`
    + `其中 ${payload.restatement_count || 0} 個有財務欄位修訂；${cutoffText}，`
    + `${selected.length ? `選中 ${selected.length} 個來源版本` : '當時尚無可用版本'}。`
    + ' 修訂差異只比較同一實體、同一來源的連續版本。';
  if (!versions.length) {
    table.innerHTML = renderEmptyBlock(
      '尚無保存版本',
      '這不是「沒有修訂」的判定；目前資料庫尚未保存這個股票、報表與期間的官方版本。',
    );
    return;
  }
  table.innerHTML = `
    <table>
      <thead><tr><th>版本</th><th>來源</th><th>系統已知時間</th><th>當時選中</th><th>修訂差異</th></tr></thead>
      <tbody>${versions.map(version => {
        const source = version.source_url
          ? `<a href="${escapeHtml(version.source_url)}" target="_blank" rel="noreferrer">${escapeHtml(version.source_id || '官方來源')}</a>`
          : escapeHtml(version.source_id || '-');
        const statusLabel = version.comparison_status === 'restated'
          ? '修訂版本'
          : version.comparison_status === 'original'
            ? '原始版本'
            : '中繼資料修訂';
        return `<tr>
          <td><strong>v${Number(version.revision || 0)} · ${statusLabel}</strong>
            <small>${escapeHtml(version.revision_id || '-')}</small></td>
          <td>${source}<small>${version.raw_payload_id ? `Raw ${escapeHtml(version.raw_payload_id)}` : '未連結原始回應'}</small></td>
          <td>${escapeHtml(version.available_at || version.acquired_at || '-')}
            <small>effective ${escapeHtml(version.effective_at || '-')}</small></td>
          <td>${version.selected_for_point_in_time ? '<strong>是</strong>' : '否'}</td>
          <td>${financialRevisionChanges(version)}</td>
        </tr>`;
      }).join('')}</tbody>
    </table>`;
}

async function loadFinancialRevisionHistory() {
  const query = financialRevisionQuery();
  const params = new URLSearchParams({
    symbol: query.symbol,
    statement_kind: query.statementKind,
    period: query.period,
  });
  if (query.knowledgeAt) params.set('knowledge_at', query.knowledgeAt);
  $('financialRevisionHistoryStatus').textContent = '正在讀取不可變財報版本鏈與當時可得版本…';
  const payload = await api(uiDataApi(`/fundamentals/revisions/history?${params.toString()}`));
  renderFinancialRevisionHistory(payload);
  return payload;
}

function renderStockAiPlan(symbol, report, eventItems, marginItems, revenueItems) {
  const box = $('stockAiPlanBox');
  if (!box) return;
  const pick = (report?.picks || []).find(item => item.symbol === symbol) || null;
  const margin = (marginItems || [])[0] || null;
  const revenue = (revenueItems || [])[0] || null;
  const eventCount = eventItems?.length || 0;
  if (!pick) {
    box.innerHTML = renderEmptyBlock('個股評分尚未產生', '這檔股票目前沒有在每日報告的觀察名單中，後續會補完整個股評分。');
    return;
  }
  box.innerHTML = `
    <div class="event">
      <h4>${pick.name} ${pick.symbol} ${signalLabel(pick.signal)}</h4>
      <p>信心分數 ${pick.score}；技術/籌碼/基本面至少交叉一輪後才給建議。</p>
      <p>理由：${pick.reasons.join('；')}</p>
      <p>風險：${pick.risk_factors.join('；')}</p>
      <p>事件面：近期待關注事件 ${eventCount} 則；融資餘額 ${margin ? fmt(margin.margin_balance) : '-'}；月營收年增 ${revenue ? `${niceNumber(revenue.yoy_change_percent)}%` : '-'}</p>
      <p>交易計畫：進場價/停損價/停利價/失效條件目前先保留欄位，待第二輪規則引擎補上。</p>
    </div>`;
}

function renderMonitorSignals(symbol, quote, flowItems) {
  const box = $('monitorSignalsBox');
  if (!box) return;
  const topFlow = (flowItems || []).slice(0, 5);
  const signalBlocks = [];
  if (quote) {
    signalBlocks.push(`<div class="event"><h4>${escapeHtml(symbol)} 盤中監控</h4><p>最新價 ${niceNumber(currentDisplayPrice(quote))}；買一 ${quote.bids?.[0]?.price ?? '-'} / 賣一 ${quote.asks?.[0]?.price ?? '-'}；更新時間 ${quote.time || '-'}</p></div>`);
  } else {
    signalBlocks.push(renderEmptyBlock('盤中監控等待即時資料', '目前尚未取得即時 quote 或非交易時段。'));
  }
  signalBlocks.push(`<div class="event"><h4>量能/急漲急跌/大單偵測</h4><p>第一版先展示監控區骨架；後續會把逐筆、內外盤、大單與漲跌停監控接進這裡。</p></div>`);
  if (topFlow.length) {
    signalBlocks.push(`<div class="event"><h4>今日法人焦點</h4><p>${topFlow.map(item => `${item.name} ${fmt(item.total_institutional_net)}`).join('；')}</p></div>`);
  }
  box.innerHTML = signalBlocks.join('');
}

async function loadDashboardOverview() {
  marketSummaryDebugReport('H2', 'features/dashboard.js:loadDashboardOverview', 'overview-index-request-start', {
    symbols: OVERVIEW_INDEX_SPECS.map(spec => spec.symbol),
  });
  // A slow security-master sync must not keep all independent market cards
  // blank. Each source gets a bounded UI budget and reports its own state.
  const withinUiBudget = (promise, label) => Promise.race([
    promise,
    new Promise((_, reject) => window.setTimeout(
      () => reject(new Error(`${label} 暫時未在 4 秒內回應`)),
      4_000,
    )),
  ]);
  const [masterResult, watchlistResult, reportResult, flowResult, sourceResult, newsCenterResult, overviewIndexResult] = await Promise.allSettled([
    withinUiBudget(api(uiDataApi('/securities/master?market=taiwan&limit=3000')), '證券主檔'),
    withinUiBudget(api(uiDataApi('/watchlist/overview?limit=12')), '自選股'),
    withinUiBudget(api(uiDataApi('/reports/daily?limit=5')), '每日市場摘要'),
    withinUiBudget(api(uiDataApi('/flow/institutional?limit=20')), '法人籌碼'),
    withinUiBudget(api(uiDataApi('/sources')), '資料來源'),
    withinUiBudget(api(uiDataApi('/news/center?limit=10')), '新聞事件'),
    withinUiBudget(api(uiDataApi('/overview/indices')), '市場指數'),
  ]);
  const watchlistItems = watchlistResult.status === 'fulfilled' ? watchlistResult.value.items : [];
  const dailyReport = reportResult.status === 'fulfilled' ? reportResult.value : null;
  const masterItems = masterResult.status === 'fulfilled' ? masterResult.value.items || [] : [];
  if (masterResult.status === 'fulfilled') $('entityCount').textContent = masterResult.value.sync?.count || masterResult.value.count;
  if (watchlistResult.status === 'fulfilled') {
    renderWatchlist(watchlistItems);
    renderWatchlistGroups(watchlistItems, dailyReport);
    renderWatchlistAlerts(watchlistItems);
    populateSymbolSelect(masterItems.length ? masterItems : watchlistItems);
    syncWorkspaceControls();
  } else {
    $('watchlistTable').innerHTML = renderEmptyBlock('自選股載入失敗', watchlistResult.reason?.message || '請稍後再試。');
  }
  if (reportResult.status === 'fulfilled') renderDailyReport(dailyReport);
  else $('dailyReportBox').innerHTML = renderEmptyBlock('每日報告載入失敗', reportResult.reason?.message || '請稍後再試。');
  if (flowResult.status === 'fulfilled') {
    renderInstitutionalTable(flowResult.value.items);
    renderOverviewLeaders(flowResult.value.items);
  }
  else $('institutionalTable').innerHTML = renderEmptyBlock('法人資料載入失敗', flowResult.reason?.message || '請稍後再試。');
  if (sourceResult.status === 'fulfilled') renderSourceStatus(sourceResult.value);
  else $('sourceStatusBox').innerHTML = renderEmptyBlock('來源狀態載入失敗', sourceResult.reason?.message || '請稍後再試。');
  if (overviewIndexResult.status === 'fulfilled') {
    marketSummaryDebugReport('H2', 'features/dashboard.js:loadDashboardOverview', 'overview-index-request-fulfilled', {
      count: overviewIndexResult.value?.count || 0,
      unavailableCount: (overviewIndexResult.value?.items || []).filter(item => item.available === false).length,
    });
    renderOverviewIndices(overviewIndexResult.value.items || []);
  } else {
    marketSummaryDebugReport('H2', 'features/dashboard.js:loadDashboardOverview', 'overview-index-request-rejected', {
      error: overviewIndexResult.reason?.message || String(overviewIndexResult.reason || ''),
    });
    $('overviewIndicesBox').innerHTML = renderEmptyBlock('大盤資料載入失敗', overviewIndexResult.reason?.message || '請稍後再試。');
  }
  if (newsCenterResult.status === 'fulfilled') {
    renderNewsCenter('overviewNewsBox', newsCenterResult.value.items, '尚無重大新聞', '首頁總覽稍後再試。');
  } else {
    $('overviewNewsBox').innerHTML = renderEmptyBlock('重大新聞載入失敗', newsCenterResult.reason?.message || '請稍後再試。');
  }
  // The detailed news workspace owns its own loading state. Do not make the
  // market overview wait for that optional secondary surface.
  loadNewsCenterView().catch(() => {});
}

async function loadDashboardSymbolDetails(symbol = state.symbol) {
  if ($('financialAnomalySymbol')) $('financialAnomalySymbol').value = symbol;
  if ($('basicValuationSymbol')) $('basicValuationSymbol').value = symbol;
  if ($('valuationPercentileSymbol')) $('valuationPercentileSymbol').value = symbol;
  if ($('peerComparisonSymbol')) $('peerComparisonSymbol').value = symbol;
  if ($('financialGuidanceSymbol')) $('financialGuidanceSymbol').value = symbol;
  if ($('industryMetricsSymbol')) $('industryMetricsSymbol').value = symbol;
  if ($('financialRevisionSymbol')) $('financialRevisionSymbol').value = symbol;
  const notificationPreviewPromise = api(`/api/notifications/previews?symbol=${encodeURIComponent(symbol)}`);
  const [marginResult, revenueResult, eventResult, corporateActionResult, reportResult, flowResult] = await Promise.allSettled([
    api(uiDataApi(`/flow/margin?symbol=${encodeURIComponent(symbol)}&limit=1`)),
    api(uiDataApi(`/fundamentals/revenue?symbol=${encodeURIComponent(symbol)}&limit=1`)),
    api(uiDataApi(`/news/center?symbol=${encodeURIComponent(symbol)}&limit=10`)),
    api(uiDataApi(`/market/${encodeURIComponent(symbol)}/corporate-actions?limit=100`)),
    api(uiDataApi('/reports/daily?limit=10')),
    api(uiDataApi('/flow/institutional?limit=20')),
  ]);
  const marginItems = marginResult.status === 'fulfilled' ? marginResult.value.items : [];
  const revenueItems = revenueResult.status === 'fulfilled' ? revenueResult.value.items : [];
  const eventItems = eventResult.status === 'fulfilled' ? eventResult.value.items : [];
  renderFundamentalsBox(symbol, marginItems, revenueItems);
  renderNewsCenter('eventList', eventItems, '尚無重大訊息', '個股頁目前沒有找到可用的新聞或事件。');
  renderCorporateActionCards(
    'eventList',
    corporateActionResult.status === 'fulfilled' ? corporateActionResult.value : { items: [] },
  );
  renderStockAiPlan(symbol, reportResult.status === 'fulfilled' ? reportResult.value : null, eventItems, marginItems, revenueItems);
  renderMonitorSignals(symbol, state.realtimeQuote, flowResult.status === 'fulfilled' ? flowResult.value.items : []);
  loadTradingAnomalies(symbol).catch(() => {});
  loadMonthlyRevenueHistory(symbol).catch(err => {
    if ($('monthlyRevenueHistoryStatus')) $('monthlyRevenueHistoryStatus').textContent = err.message || '月營收歷史讀取失敗。';
  });
  loadIncomeStatementHistory(symbol).catch(err => {
    if ($('incomeStatementHistoryStatus')) $('incomeStatementHistoryStatus').textContent = err.message || '損益表歷史讀取失敗。';
  });
  loadBalanceSheetHistory(symbol).catch(err => {
    if ($('balanceSheetHistoryStatus')) $('balanceSheetHistoryStatus').textContent = err.message || '資產負債表歷史讀取失敗。';
  });
  loadCashFlowHistory(symbol).catch(err => {
    if ($('cashFlowHistoryStatus')) $('cashFlowHistoryStatus').textContent = err.message || '現金流量表歷史讀取失敗。';
  });
  loadFinancialRatioHistory(symbol).catch(err => {
    if ($('financialRatioHistoryStatus')) $('financialRatioHistoryStatus').textContent = err.message || '財務比率歷史讀取失敗。';
  });
  loadGrowthHistory(symbol).catch(err => {
    if ($('growthHistoryStatus')) $('growthHistoryStatus').textContent = err.message || '成長率歷史讀取失敗。';
  });
  loadIndustryMetrics(symbol).catch(err => {
    if ($('industryMetricsStatus')) $('industryMetricsStatus').textContent = err.message || '產業特有指標讀取失敗。';
  });
  loadFinancialGuidance(symbol).catch(err => {
    if ($('financialGuidanceStatus')) $('financialGuidanceStatus').textContent = err.message || '財測與指引讀取失敗。';
  });
  loadFinancialAnomalies(symbol).catch(err => {
    if ($('financialAnomalyStatus')) $('financialAnomalyStatus').textContent = err.message || '財報異常掃描失敗。';
  });
  loadBasicValuation(symbol).catch(err => {
    if ($('basicValuationStatus')) $('basicValuationStatus').textContent = err.message || '基本估值計算失敗。';
  });
  loadValuationPercentiles(symbol, false).catch(err => {
    if ($('valuationPercentileStatus')) $('valuationPercentileStatus').textContent = err.message || '歷史估值分位讀取失敗。';
  });
  notificationPreviewPromise.then(renderNotificationPreviews).catch(() => {});
  if (state.newsScope === 'symbol') loadNewsCenterView().catch(() => {});
}

async function loadNotificationCenter(symbol = state.symbol) {
  const [channelsResult, previewsResult] = await Promise.allSettled([
    api('/api/notifications/channels'),
    api(`/api/notifications/previews?symbol=${encodeURIComponent(symbol)}`),
  ]);
  if (channelsResult.status === 'fulfilled') renderNotificationChannels(channelsResult.value);
  else $('notificationStatusBox').innerHTML = renderEmptyBlock('通知通道載入失敗', channelsResult.reason?.message || '請稍後再試。');
  if (previewsResult.status === 'fulfilled') renderNotificationPreviews(previewsResult.value);
  else $('notificationPreviewBox').innerHTML = renderEmptyBlock('推播預覽載入失敗', previewsResult.reason?.message || '請稍後再試。');
}

$('symbolSelect').addEventListener('change', e => {
  const symbol = String(e.target.value || '').trim();
  if (symbol) loadSummary(symbol);
});
if ($('askBtn')) $('askBtn').addEventListener('click', askQuestion);
if (typeof bindScreenerControls === 'function') bindScreenerControls();
$('runLinkage').addEventListener('click', runLinkage);
if ($('openAgentWorkspace')) $('openAgentWorkspace').addEventListener('click', () => { setView('home'); setGlobalAgentPopover(false); setTimeout(() => document.querySelector('.agent-runtime-panel')?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 80); });
if ($('loginWithChatGPT')) $('loginWithChatGPT').addEventListener('click', () => startCodexLogin('browser').catch(err => { $('authState').textContent = err.message || '登入失敗。'; }));
if ($('loginWithDeviceCode')) $('loginWithDeviceCode').addEventListener('click', () => startCodexLogin('device_code').catch(err => { $('authState').textContent = err.message || '登入失敗。'; }));
if ($('continueWithoutCodex')) $('continueWithoutCodex').addEventListener('click', continueWithoutCodex);
if ($('runCodexPrompt')) $('runCodexPrompt').addEventListener('click', () => runCodexPrompt().catch(err => {
  if ($('homeAgentDockStatus')) $('homeAgentDockStatus').textContent = err.message || 'Agent 執行失敗。';
}));
if ($('codexPrompt')) $('codexPrompt').addEventListener('keydown', event => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) $('runCodexPrompt').click(); });
document.querySelectorAll('[data-codex-prompt]').forEach(button => button.addEventListener('click', () => runCodexPrompt(
  button.dataset.codexPrompt,
  button.dataset.computerUse === 'true',
).catch(err => {
  if ($('homeAgentDockStatus')) $('homeAgentDockStatus').textContent = err.message || 'Agent 執行失敗。';
})));
if ($('openAgentSettings')) $('openAgentSettings').addEventListener('click', () => { setView('system', { tab: 'agent-models' }); setTimeout(() => $('settingsAgentRuntime')?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 80); });
if ($('saveAgentRuntimeSettings')) $('saveAgentRuntimeSettings').addEventListener('click', () => saveAgentRuntimeSettings().catch(err => { $('agentSettingsStatus').textContent = err.message || 'Agent 設定保存失敗。'; }));
if ($('testAgentProvider')) $('testAgentProvider').addEventListener('click', () => testAgentProvider().catch(err => { $('agentSettingsStatus').textContent = err.message || '模型連線測試失敗。'; }));
if ($('discoverAgentModels')) $('discoverAgentModels').addEventListener('click', () => discoverAgentModels().catch(err => { $('agentModelDiscoveryState').textContent = err.message || '模型清單讀取失敗。'; }));
if ($('agentDiscoveredModels')) $('agentDiscoveredModels').addEventListener('change', () => {
  state.agentSettingsFormDirty = true;
  selectDiscoveredAgentModel();
});
if ($('agentOpenAIBaseUrl')) $('agentOpenAIBaseUrl').addEventListener('change', () => {
  state.agentSettingsFormDirty = true;
  if (($('agentDefaultDriver')?.value || '') === 'openai-compatible' && $('agentOpenAIBaseUrl').value.trim()) {
    discoverAgentModels().catch(err => { $('agentModelDiscoveryState').textContent = err.message || '模型清單讀取失敗。'; });
  }
});
if ($('agentDefaultDriver')) $('agentDefaultDriver').addEventListener('change', () => {
  state.agentSettingsFormDirty = true;
  updateAgentProviderPanels();
  if (($('agentDefaultDriver')?.value || '') === 'openai-compatible' && $('agentOpenAIBaseUrl')?.value?.trim()) {
    discoverAgentModels().catch(err => { $('agentModelDiscoveryState').textContent = err.message || '模型清單讀取失敗。'; });
  }
});
if ($('syncCodexProject')) $('syncCodexProject').addEventListener('click', () => syncCodexProject().catch(err => { $('codexProjectSummary').textContent = err.message || '專案同步失敗。'; }));
if ($('settingsLoginCodex')) $('settingsLoginCodex').addEventListener('click', () => startCodexLogin('browser').catch(err => { $('codexProjectSummary').textContent = err.message || '登入失敗。'; }));
if ($('logoutCodex')) $('logoutCodex').addEventListener('click', () => logoutCodex().catch(err => { $('codexProjectSummary').textContent = err.message || '登出失敗。'; }));
if ($('refreshAgentStatus')) $('refreshAgentStatus').addEventListener('click', () => loadSystemAgentPage(true));
if ($('refreshSkillsStatus')) $('refreshSkillsStatus').addEventListener('click', () => loadSystemSkillsPage());
if ($('refreshApiStatus')) $('refreshApiStatus').addEventListener('click', () => loadSystemConnectionsPage());
document.querySelectorAll('[data-settings-target]').forEach(button => button.addEventListener('click', () => {
  document.getElementById(button.dataset.settingsTarget)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}));
if ($('accountMenuBtn')) $('accountMenuBtn').addEventListener('click', () => { setView('system', { tab: 'agent-models' }); loadSystemAgentPage(false).catch(() => {}); $('settingsAgentConnectionTitle')?.scrollIntoView({ block: 'start' }); });
if ($('refreshTradingBtn')) $('refreshTradingBtn').addEventListener('click', () => loadTradingView().catch(err => renderWorkspaceError(['tradingStatusBox', 'tradingEstimateBox', 'tradingMetaBox'], '交易預覽載入失敗', err.message || '請稍後再試。')));
if ($('refreshAssistantBtn')) $('refreshAssistantBtn').addEventListener('click', () => loadAssistantView().catch(err => renderWorkspaceError(['assistantContextBox', 'assistantCardsBox', 'assistantWatchlistBox', 'assistantNotificationBox', 'assistantMetaBox'], '量化交易參考載入失敗', err.message || '請稍後再試。')));
if ($('sendNotificationDryRunBtn')) $('sendNotificationDryRunBtn').addEventListener('click', () => sendNotificationDryRun().catch(err => {
  const box = $('notificationSendResultBox');
  if (box) box.innerHTML = renderEmptyBlock('dry-run 檢查失敗', err.message || '請稍後再試。');
}));
if ($('loadMonthlyRevenueHistoryBtn')) $('loadMonthlyRevenueHistoryBtn').addEventListener('click', () => {
  loadMonthlyRevenueHistory().catch(err => {
    $('monthlyRevenueHistoryStatus').textContent = err.message || '月營收歷史讀取失敗。';
  });
});
if ($('syncMonthlyRevenueHistoryBtn')) $('syncMonthlyRevenueHistoryBtn').addEventListener('click', () => {
  syncMonthlyRevenueHistory().catch(err => {
    $('monthlyRevenueHistoryStatus').textContent = err.message || '月營收歷史同步失敗。';
  });
});
if ($('loadIncomeStatementHistoryBtn')) $('loadIncomeStatementHistoryBtn').addEventListener('click', () => {
  loadIncomeStatementHistory().catch(err => {
    $('incomeStatementHistoryStatus').textContent = err.message || '損益表歷史讀取失敗。';
  });
});
if ($('syncIncomeStatementHistoryBtn')) $('syncIncomeStatementHistoryBtn').addEventListener('click', () => {
  syncIncomeStatementHistory().catch(err => {
    $('incomeStatementHistoryStatus').textContent = err.message || '損益表歷史同步失敗。';
  });
});
initializeIncomeStatementPeriods();
if ($('loadBalanceSheetHistoryBtn')) $('loadBalanceSheetHistoryBtn').addEventListener('click', () => {
  loadBalanceSheetHistory().catch(err => {
    $('balanceSheetHistoryStatus').textContent = err.message || '資產負債表歷史讀取失敗。';
  });
});
if ($('syncBalanceSheetHistoryBtn')) $('syncBalanceSheetHistoryBtn').addEventListener('click', () => {
  syncBalanceSheetHistory().catch(err => {
    $('balanceSheetHistoryStatus').textContent = err.message || '資產負債表歷史同步失敗。';
  });
});
initializeBalanceSheetPeriods();
if ($('loadCashFlowHistoryBtn')) $('loadCashFlowHistoryBtn').addEventListener('click', () => {
  loadCashFlowHistory().catch(err => {
    $('cashFlowHistoryStatus').textContent = err.message || '現金流量表歷史讀取失敗。';
  });
});
if ($('syncCashFlowHistoryBtn')) $('syncCashFlowHistoryBtn').addEventListener('click', () => {
  syncCashFlowHistory().catch(err => {
    $('cashFlowHistoryStatus').textContent = err.message || '現金流量表歷史同步失敗。';
  });
});
initializeCashFlowPeriods();
if ($('loadFinancialRatioHistoryBtn')) $('loadFinancialRatioHistoryBtn').addEventListener('click', () => {
  loadFinancialRatioHistory().catch(err => {
    $('financialRatioHistoryStatus').textContent = err.message || '財務比率計算失敗。';
  });
});
initializeFinancialRatioPeriods();
if ($('loadGrowthHistoryBtn')) $('loadGrowthHistoryBtn').addEventListener('click', () => {
  loadGrowthHistory().catch(err => {
    $('growthHistoryStatus').textContent = err.message || '成長率計算失敗。';
  });
});
initializeGrowthPeriods();
if ($('loadFinancialRevisionHistoryBtn')) $('loadFinancialRevisionHistoryBtn').addEventListener('click', () => {
  loadFinancialRevisionHistory().catch(err => {
    $('financialRevisionHistoryStatus').textContent = err.message || '財報修訂鏈讀取失敗。';
  });
});
if ($('loadIndustryMetricsBtn')) $('loadIndustryMetricsBtn').addEventListener('click', () => {
  loadIndustryMetrics().catch(err => {
    $('industryMetricsStatus').textContent = err.message || '產業特有指標讀取失敗。';
  });
});
if ($('loadFinancialGuidanceBtn')) $('loadFinancialGuidanceBtn').addEventListener('click', () => {
  loadFinancialGuidance().catch(err => {
    $('financialGuidanceStatus').textContent = err.message || '財測與指引讀取失敗。';
  });
});
if ($('loadFinancialAnomaliesBtn')) $('loadFinancialAnomaliesBtn').addEventListener('click', () => {
  loadFinancialAnomalies().catch(err => { $('financialAnomalyStatus').textContent = err.message || '財報異常掃描失敗。'; });
});
if ($('loadBasicValuationBtn')) $('loadBasicValuationBtn').addEventListener('click', () => {
  loadBasicValuation().catch(err => { $('basicValuationStatus').textContent = err.message || '基本估值計算失敗。'; });
});
if ($('loadValuationPercentilesBtn')) $('loadValuationPercentilesBtn').addEventListener('click', () => {
  loadValuationPercentiles(null, true).catch(err => { $('valuationPercentileStatus').textContent = err.message || '歷史估值分位同步失敗。'; });
});
if ($('loadPeerComparisonBtn')) $('loadPeerComparisonBtn').addEventListener('click', () => {
  loadPeerComparison().catch(err => { $('peerComparisonStatus').textContent = err.message || '同業比較失敗。'; });
});
if ($('runDcfValuationBtn')) $('runDcfValuationBtn').addEventListener('click', () => {
  runDcfValuation().catch(err => { $('dcfValuationStatus').textContent = err.message || 'DCF 計算失敗。'; });
});
if ($('runValuationPolicyBtn')) $('runValuationPolicyBtn').addEventListener('click', () => {
  runValuationPolicy().catch(err => { $('valuationPolicyStatus').textContent = err.message || '估值模型選擇失敗。'; });
});
if ($('loadChipHistoryBtn')) $('loadChipHistoryBtn').addEventListener('click', () => {
  loadChipHistory().catch(err => { $('chipHistoryStatus').textContent = err.message || '籌碼歷史讀取失敗。'; });
});
if ($('loadShortDaytradeBtn')) $('loadShortDaytradeBtn').addEventListener('click', () => {
  loadShortDaytradeHistory().catch(err => { $('shortDaytradeStatus').textContent = err.message || '借券與當沖歷史讀取失敗。'; });
});
if ($('loadTdccHistoryBtn')) $('loadTdccHistoryBtn').addEventListener('click', () => {
  loadTdccHoldingHistory().catch(err => { $('tdccHistoryStatus').textContent = err.message || 'TDCC 每週持股分布讀取失敗。'; });
});
if ($('runOpenStockAI')) $('runOpenStockAI').addEventListener('click', () => loadOpenStockAIView().catch(err => renderWorkspaceError(['openStockSignalBox', 'openStockRiskBox', 'openStockAdapterBox'], '策略研究載入失敗', err.message || '請稍後再試。')));
if ($('refreshRiskBtn')) $('refreshRiskBtn').addEventListener('click', () => loadRiskView().catch(err => renderWorkspaceError(['riskAlertsBox', 'riskMetaBox'], '風控摘要載入失敗', err.message || '請稍後再試。')));
if ($('refreshAssetsBtn')) $('refreshAssetsBtn').addEventListener('click', () => loadAssetsView().catch(err => renderWorkspaceError(['assetStatusBox', 'assetMetaBox'], '資產摘要載入失敗', err.message || '請稍後再試。')));
if ($('tradingSide')) $('tradingSide').addEventListener('change', () => updateTradeState({ side: $('tradingSide').value }));
if ($('riskSide')) $('riskSide').addEventListener('change', () => updateTradeState({ side: $('riskSide').value }));
if ($('tradingLots')) $('tradingLots').addEventListener('change', () => updateTradeState({ quantityLots: $('tradingLots').value }));
if ($('riskLots')) $('riskLots').addEventListener('change', () => updateTradeState({ quantityLots: $('riskLots').value }));
['tradingSymbol', 'assistantSymbol', 'openStockSymbol', 'riskSymbol'].forEach((id) => {
  if ($(id)) $(id).addEventListener('change', () => updateTradeState({ symbol: $(id).value }));
});
if ($('runOpenStockSession')) $('runOpenStockSession').addEventListener('click', () => loadOpenStockAISession().catch(err => renderWorkspaceError(['openStockSessionBox'], '策略批次執行失敗', err.message || '請稍後再試。')));
window.addEventListener('resize', () => { renderCurrentChart(); });
