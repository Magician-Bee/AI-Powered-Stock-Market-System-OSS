(() => {
  let bootstrapPayload = null;
  let selectedDetail = null;
  let homeDetailTab = 'overview';
  let selectionSequence = 0;
  let initialized = false;
  const searchedEntities = new Map();
  const searchedDetails = new Map();
  const previewState = {
    type: 'candles', range: 250, hoverIndex: null,
    indicators: { ma5: true, ma10: true, ma20: true, ma60: true, boll: true, volume: true, macd: true },
  };

  const node = id => document.getElementById(id);

  function feedback(message, stateName = '') {
    const target = node('homeActionFeedback');
    if (!target) return;
    target.textContent = message;
    target.dataset.state = stateName;
  }

  function renderInstrumentWorkbench() {
    const workspace = document.documentElement.dataset.workspace || window.__activeWorkspace || 'home';
    const host = workspace === 'instrument' ? node('instrumentOverviewChartHost') : node('homeChartHost');
    if (!host) return;
    const existingSurface = node('homePriceChart')?.closest('.home-chart-preview');
    if (existingSurface) {
      if (!host.contains(existingSurface)) host.append(existingSurface);
      requestAnimationFrame(syncHomeChartPreview);
      return;
    }
    host.innerHTML = `<article class="panel home-chart-preview"><div class="panel-head"><div><h3>選取股票走勢</h3><p class="panel-copy">快速判讀、切換圖型與週期；完整標註與研究工作台位於「個股 → 圖表」。</p></div><button id="openInstrumentChart" type="button">開啟完整圖表</button></div><div class="home-chart-layout"><div class="home-chart-main"><div class="home-chart-controls" aria-label="首頁圖表控制"><div class="home-chart-control-group" role="group" aria-label="圖型"><button type="button" class="is-active" data-home-chart-type="candles">K 線</button><button type="button" data-home-chart-type="line">折線</button></div><div class="home-chart-control-group" role="group" aria-label="時間區間"><button type="button" data-home-chart-range="1">1 日</button><button type="button" data-home-chart-range="5">5 日</button><button type="button" data-home-chart-range="22">1 月</button><button type="button" data-home-chart-range="66">3 月</button><button type="button" class="is-active" data-home-chart-range="250">1 年</button></div><div class="home-chart-control-group" role="group" aria-label="技術線與資料顯示"><button type="button" class="is-active" data-home-chart-indicator="ma5" aria-pressed="true">MA5</button><button type="button" class="is-active" data-home-chart-indicator="ma10" aria-pressed="true">MA10</button><button type="button" class="is-active" data-home-chart-indicator="ma20" aria-pressed="true">MA20</button><button type="button" class="is-active" data-home-chart-indicator="ma60" aria-pressed="true">MA60</button><button type="button" class="is-active" data-home-chart-indicator="boll" aria-pressed="true">布林通道</button><button type="button" class="is-active" data-home-chart-indicator="volume" aria-pressed="true">成交量</button><button type="button" class="is-active" data-home-chart-indicator="macd" aria-pressed="true">MACD</button></div></div><div class="home-chart-canvas-wrap"><canvas id="homePriceChart" width="1040" height="500" tabindex="0" aria-label="可互動的選取股票走勢預覽；移動游標可查看日期與價格"></canvas><div id="homeChartHoverCard" class="home-chart-hover-card" hidden></div></div><p id="homeChartPreviewStatus" class="intraday-candle-status">正在載入圖表資料…</p></div><aside id="homeChartDecisionPanel" class="home-chart-decision-panel" aria-live="polite" aria-label="目前選取股票的決策摘要"></aside></div></article>`;
    host.dataset.mounted = 'true';
    node('openInstrumentChart')?.addEventListener('click', () => window.setView?.('instrument', { tab: 'chart' }));
    host.querySelectorAll('[data-home-chart-type]').forEach(button => button.addEventListener('click', () => {
      previewState.type = button.dataset.homeChartType;
      host.querySelectorAll('[data-home-chart-type]').forEach(item => item.classList.toggle('is-active', item === button));
      syncHomeChartPreview();
    }));
    host.querySelectorAll('[data-home-chart-range]').forEach(button => button.addEventListener('click', () => {
      previewState.range = Number(button.dataset.homeChartRange);
      host.querySelectorAll('[data-home-chart-range]').forEach(item => item.classList.toggle('is-active', item === button));
      syncHomeChartPreview();
    }));
    host.querySelectorAll('[data-home-chart-indicator]').forEach(button => button.addEventListener('click', (event) => {
      const key = event.currentTarget.dataset.homeChartIndicator;
      previewState.indicators[key] = !previewState.indicators[key];
      event.currentTarget.classList.toggle('is-active', previewState.indicators[key]);
      event.currentTarget.setAttribute('aria-pressed', String(previewState.indicators[key]));
      syncHomeChartPreview();
    }));
    const canvas = node('homePriceChart');
    canvas?.addEventListener('pointermove', (event) => {
      const points = previewPoints();
      const rect = canvas.getBoundingClientRect();
      const chartLeft = 54, chartRight = 72;
      previewState.hoverIndex = Math.max(0, Math.min(points.length - 1, Math.round(((event.clientX - rect.left - chartLeft) / Math.max(1, rect.width - chartLeft - chartRight)) * Math.max(0, points.length - 1))));
      syncHomeChartPreview();
    });
    canvas?.addEventListener('pointerleave', () => { previewState.hoverIndex = null; node('homeChartHoverCard').hidden = true; syncHomeChartPreview(); });
    canvas?.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      event.preventDefault();
      const points = previewPoints();
      previewState.hoverIndex = Math.max(0, Math.min(points.length - 1, (previewState.hoverIndex ?? points.length - 1) + (event.key === 'ArrowLeft' ? -1 : 1)));
      syncHomeChartPreview();
    });
    if (typeof ResizeObserver !== 'undefined') new ResizeObserver(() => syncHomeChartPreview()).observe(host);
    renderHomeChartDecision(selectedDetail || detailForSymbol(WorkspaceContextStore.get().selection.symbol));
    syncHomeChartPreview();
  }

  function formatPrice(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number.toLocaleString('zh-TW', { maximumFractionDigits: 2 }) : '-';
  }

  function modelStatusCopy(detail) {
    const status = detail?.model_status;
    if (status === 'succeeded') return ['AI 深度分析已完成', 'ready'];
    if (status === 'queued') return ['AI 深度分析排程中', 'pending'];
    if (status === 'failed') return ['AI 深度分析暫時失敗', 'warning'];
    return ['尚未深度分析', 'muted'];
  }

  function productStatusCopy(detail) {
    const receipt = detail?.product_classification || {};
    const assessment = detail?.product_entry_assessment || {};
    const labels = { ordinary_stock: '普通股', etf: 'ETF', etn: 'ETN', depositary_receipt: '存託憑證', preferred_stock: '特別股', warrant: '權證', bond: '債券', other: '其他商品', unknown: '未分類' };
    const status = assessment.classification_verified === true ? '來源已核驗' : receipt.status === 'conflict' ? '來源衝突' : '分類待核驗／更新';
    return `${labels[receipt.product_type] || '未分類'} · ${status} · ${assessment.allowed === true ? '商品門檻通過' : '研究保留，新進場受限'}`;
  }

  function homeFactorEvidence(detail) {
    const labels = {
      fundamental_score: '基本面',
      valuation_score: '估值',
      chip_score: '籌碼',
      event_score: '事件',
      relative_strength_score: '相對強弱',
    };
    const factors = Object.entries(labels).map(([key, label]) => {
      const factor = detail?.factor_scores?.[key];
      if (factor?.value === null || factor?.value === undefined) return null;
      const availability = factor.historical_pit_eligible === true
        ? 'PIT'
        : factor.status === 'partial'
          ? '目前資料'
          : String(factor.status || '已驗證');
      const source = factor.source ? ` · ${factor.source}` : '';
      return `<span class="home-decision-factor ${escapeHtml(String(factor.status || 'unavailable'))}" title="${escapeHtml(`${label}${source}${factor.reason ? `；${factor.reason}` : ''}`)}">${escapeHtml(label)} ${escapeHtml(formatPrice(factor.value))} <small>${escapeHtml(availability)}</small></span>`;
    }).filter(Boolean);
    return factors.length
      ? `<div class="home-decision-factors" aria-label="目前候選的因子資料">${factors.join('')}</div>`
      : '<div class="home-decision-factors empty">因子資料：目前僅使用可驗證行情；其他來源尚未可用。</div>';
  }

  function renderHomeChartDecision(detail) {
    const target = node('homeChartDecisionPanel');
    if (!target) return;
    if (!detail) {
      const symbol = String(WorkspaceContextStore.get().selection.symbol || state.symbol || '')
        .trim().toUpperCase();
      if (isConcreteSecuritySymbol(symbol)) {
        const name = state.currentEntity?.name && state.currentEntity.name !== symbol
          ? `${state.currentEntity.name} ${symbol}`
          : symbol;
        target.innerHTML = `<span class="home-decision-eyebrow">目前圖表</span><h4>${escapeHtml(name)}</h4><p>已載入此標的的圖表與資料 Context；它不在本次全市場候選清單中，因此目前沒有市場快照的分類、觸發或失效條件。</p>`;
        return;
      }
      target.innerHTML = `<span class="home-decision-eyebrow">決策摘要</span><h4>選擇一檔市場候選</h4><p>從左側清單選取股票後，這裡會顯示分類、原因、觸發、失效與下一次重估條件。</p>`;
      return;
    }
    const [modelCopy, modelTone] = modelStatusCopy(detail);
    const change = Number(detail.change_percent);
    const changeCopy = Number.isFinite(change) ? `${change >= 0 ? '+' : ''}${change.toFixed(2)}%` : '-';
    const overlaySummary = String(detail.model_overlay?.summary || detail.model_overlay?.recommendation || '').trim();
    const productNote = document.createElement('p');
    productNote.className = 'home-decision-reason';
    productNote.textContent = productStatusCopy(detail);
    target.innerHTML = `<span class="home-decision-eyebrow">目前決策</span><div class="home-decision-title"><div><h4>${escapeHtml(detail.name || detail.symbol)}</h4><span>${escapeHtml(detail.symbol || '')}</span></div><strong>${escapeHtml(detail.decision_label || '尚未分類')}</strong></div><div class="home-decision-price"><strong>${formatPrice(detail.latest_price)}</strong><span class="${change >= 0 ? 'is-positive' : 'is-negative'}">${changeCopy}</span></div><p class="home-decision-reason">${escapeHtml(overlaySummary || detail.primary_reason || '等待市場資料完成後更新。')}</p>${homeFactorEvidence(detail)}<dl class="home-decision-facts"><div><dt>觸發條件</dt><dd>${escapeHtml(detail.trigger || '-')}</dd></div><div><dt>失效條件</dt><dd>${escapeHtml(detail.invalidation || '-')}</dd></div><div><dt>下一次重估</dt><dd>${escapeHtml(detail.next_review || '-')}</dd></div></dl><div class="home-decision-status ${modelTone}">${escapeHtml(modelCopy)}</div><div class="home-decision-actions"><button type="button" data-workspace-action="agent" data-symbol="${escapeHtml(detail.symbol || '')}">分析這檔</button><button type="button" data-workspace-action="alert" data-symbol="${escapeHtml(detail.symbol || '')}">建立提醒</button></div>`;
    target.querySelector('.home-decision-reason')?.after(productNote);
  }

  function setInstrumentSurface(workspace = 'home') {
    const target = workspace === 'instrument' ? node('instrumentOverviewChartHost') : node('homeChartHost');
    const surface = node('homePriceChart')?.closest('.home-chart-preview');
    if (target && surface && !target.contains(surface)) target.append(surface);
    else if (target && !surface) renderInstrumentWorkbench();
    requestAnimationFrame(syncHomeChartPreview);
  }

  function syncHomeChartPreview() {
    const target = node('homePriceChart');
    const status = node('homeChartPreviewStatus');
    if (!target) return;
    syncChartThemeFromDom(target);
    const points = previewPoints();
    const width = target.clientWidth || 900;
    const height = target.clientHeight || 390;
    const dpr = window.devicePixelRatio || 1;
    target.width = Math.floor(width * dpr);
    target.height = Math.floor(height * dpr);
    const context = target.getContext('2d');
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    context.clearRect(0, 0, width, height);
    drawHomeChartPreview(context, width, height, points);
    if (status) status.textContent = points.length ? `${WorkspaceContextStore.get().selection.symbol || state.symbol || ''} · ${points.length} 根已驗證 K 線；移動游標或用方向鍵查看價格。` : '尚無足夠 K 線；請稍後重試或開啟完整圖表查詢歷史資料。';
  }

  function previewPoints() {
    const symbol = String(WorkspaceContextStore.get().selection.symbol || state.symbol || '').toUpperCase().replace(/\.(TW|TWO)$/i, '');
    const history = (state.historySeries?.[symbol] || []).filter(point => Number.isFinite(Number(point?.close)));
    return history.slice(-Math.max(1, previewState.range));
  }

  function previewAverage(points, windowSize) {
    return points.map((_, index) => index < windowSize - 1 ? null : points.slice(index - windowSize + 1, index + 1).reduce((sum, point) => sum + Number(point.close), 0) / windowSize);
  }

  function drawHomeChartPreview(ctx, width, height, points) {
    const showVolume = previewState.indicators.volume;
    const showMacd = previewState.indicators.macd;
    const lowerHeight = (showVolume ? 42 : 0) + (showMacd ? 58 : 0);
    const left = 54, right = 72, top = 24, bottom = height - 34 - lowerHeight;
    const plotWidth = Math.max(1, width - left - right), plotHeight = Math.max(1, bottom - top);
    const colors = {
      background: CHART_THEME.background,
      panel: CHART_THEME.panel,
      grid: CHART_THEME.grid,
      text: CHART_THEME.text,
      muted: CHART_THEME.muted,
      up: CHART_THEME.up,
      down: CHART_THEME.down,
      line: CHART_THEME.price,
      ma5: CHART_THEME.ma5,
      ma10: CHART_THEME.ma10,
      ma20: CHART_THEME.ma20,
      ma60: CHART_THEME.ma60,
      boll: CHART_THEME.boll,
      macd: CHART_THEME.macd,
      signal: CHART_THEME.signal,
      trigger: CHART_THEME.macd,
      invalid: CHART_THEME.up,
    };
    ctx.fillStyle = colors.background; ctx.fillRect(0, 0, width, height);
    if (!points.length) { ctx.fillStyle = colors.text; ctx.font = '700 15px system-ui'; ctx.fillText('等待已驗證的 K 線資料', left, 58); return; }
    const allValues = points.flatMap(point => [Number(point.low ?? point.close), Number(point.high ?? point.close)]);
    const minRaw = Math.min(...allValues), maxRaw = Math.max(...allValues), padding = Math.max((maxRaw - minRaw) * .12, Math.abs(maxRaw) * .004, 1);
    const minimum = minRaw - padding, maximum = maxRaw + padding;
    const xFor = index => left + ((index + .5) / points.length) * plotWidth;
    const yFor = value => bottom - ((Number(value) - minimum) / Math.max(0.0001, maximum - minimum)) * plotHeight;
    ctx.fillStyle = colors.panel; ctx.fillRect(left, top, plotWidth, plotHeight);
    ctx.strokeStyle = colors.grid; ctx.lineWidth = 1; ctx.font = '11px system-ui'; ctx.fillStyle = colors.muted; ctx.textAlign = 'left';
    for (let index = 0; index <= 4; index += 1) { const y = top + (plotHeight / 4) * index; const value = maximum - ((maximum - minimum) / 4) * index; ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(left + plotWidth, y); ctx.stroke(); ctx.fillText(Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 }), left + plotWidth + 8, y + 3); }
    const drawSeries = (values, color) => { ctx.strokeStyle = color; ctx.lineWidth = 1.35; ctx.beginPath(); let started = false; values.forEach((value, index) => { if (!Number.isFinite(value)) return; if (!started) { ctx.moveTo(xFor(index), yFor(value)); started = true; } else ctx.lineTo(xFor(index), yFor(value)); }); ctx.stroke(); };
    if (previewState.type === 'line') drawSeries(points.map(point => Number(point.close)), colors.line);
    else { const slot = plotWidth / points.length, candleWidth = Math.max(1, Math.min(9, slot * .58)); points.forEach((point, index) => { const up = Number(point.close) >= Number(point.open); ctx.strokeStyle = ctx.fillStyle = up ? colors.up : colors.down; const x = xFor(index); ctx.beginPath(); ctx.moveTo(x, yFor(point.high)); ctx.lineTo(x, yFor(point.low)); ctx.stroke(); ctx.fillRect(x - candleWidth / 2, Math.min(yFor(point.open), yFor(point.close)), candleWidth, Math.max(1, Math.abs(yFor(point.open) - yFor(point.close)))); }); }
    [['5', colors.ma5], ['10', colors.ma10], ['20', colors.ma20], ['60', colors.ma60]].forEach(([windowSize, color]) => { if (previewState.indicators[`ma${windowSize}`]) drawSeries(previewAverage(points, Number(windowSize)), color); });
    if (previewState.indicators.boll) {
      const mid = previewAverage(points, 20);
      const deviation = points.map((_, index) => {
        if (index < 19) return null;
        const sample = points.slice(index - 19, index + 1).map(point => Number(point.close));
        const mean = mid[index];
        return Math.sqrt(sample.reduce((sum, value) => sum + ((value - mean) ** 2), 0) / sample.length);
      });
      drawSeries(mid.map((value, index) => value == null ? null : value + deviation[index] * 2), colors.boll);
      drawSeries(mid.map((value, index) => value == null ? null : value - deviation[index] * 2), colors.boll);
    }
    const detail = selectedDetail || detailForSymbol(WorkspaceContextStore.get().selection.symbol);
    [['trigger_price', colors.trigger, '買入觸發'], ['invalidation_price', colors.invalid, '失效']].forEach(([key, color, label]) => { const value = Number(detail?.[key]); if (!Number.isFinite(value) || value < minimum || value > maximum) return; const y = yFor(value); ctx.save(); ctx.setLineDash([5, 4]); ctx.strokeStyle = color; ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(left + plotWidth, y); ctx.stroke(); ctx.restore(); ctx.fillStyle = color; ctx.textAlign = 'left'; ctx.fillText(label, left + 6, y - 4); });
    let lowerTop = bottom + 14;
    if (showVolume) { const maximumVolume = Math.max(...points.map(point => Number(point.volume) || 0), 1); ctx.fillStyle = colors.muted; ctx.fillText('成交量', left, lowerTop + 7); points.forEach((point, index) => { const volumeHeight = (Number(point.volume) || 0) / maximumVolume * 25; ctx.fillStyle = Number(point.close) >= Number(point.open) ? 'rgba(239,106,106,.6)' : 'rgba(68,201,145,.6)'; ctx.fillRect(xFor(index) - Math.max(1, plotWidth / points.length * .28), lowerTop + 32 - volumeHeight, Math.max(1, plotWidth / points.length * .56), volumeHeight); }); lowerTop += 42; }
    if (showMacd) {
      const ema = (period) => { const factor = 2 / (period + 1); let previous = Number(points[0].close); return points.map((point, index) => { previous = index ? Number(point.close) * factor + previous * (1 - factor) : previous; return previous; }); };
      const fast = ema(12), slow = ema(26), macd = fast.map((value, index) => value - slow[index]);
      const factor = 2 / 10; let previousSignal = macd[0]; const signal = macd.map((value, index) => { previousSignal = index ? value * factor + previousSignal * (1 - factor) : value; return previousSignal; });
      const values = [...macd, ...signal]; const minimumMacd = Math.min(...values), maximumMacd = Math.max(...values); const macdHeight = 38;
      const yMacd = value => lowerTop + macdHeight - ((value - minimumMacd) / Math.max(.000001, maximumMacd - minimumMacd)) * macdHeight;
      ctx.fillStyle = colors.muted; ctx.fillText('MACD(12,26,9)', left, lowerTop + 7);
      const drawLower = (valuesToDraw, color) => { ctx.strokeStyle = color; ctx.beginPath(); valuesToDraw.forEach((value, index) => { const x = xFor(index), y = yMacd(value); if (index) ctx.lineTo(x, y); else ctx.moveTo(x, y); }); ctx.stroke(); };
      drawLower(macd, colors.macd); drawLower(signal, colors.signal);
    }
    const hover = previewState.hoverIndex; if (Number.isInteger(hover) && points[hover]) { const point = points[hover]; const x = xFor(hover); ctx.strokeStyle = '#d9ebff'; ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom); ctx.stroke(); ctx.beginPath(); ctx.moveTo(left, yFor(point.close)); ctx.lineTo(left + plotWidth, yFor(point.close)); ctx.stroke(); const card = node('homeChartHoverCard'); if (card) { card.hidden = false; card.style.left = `${Math.min(width - 174, Math.max(left + 8, x + 10))}px`; card.style.top = '32px'; card.textContent = `${String(point.date || '').slice(0, 10)}  開 ${Number(point.open).toFixed(2)} 高 ${Number(point.high).toFixed(2)} 低 ${Number(point.low).toFixed(2)} 收 ${Number(point.close).toFixed(2)}`; } }
  }

  function statusCopy(snapshot) {
    if (!snapshot) return ['初始化中', '正在建立第一份全市場快照', '官方批次資料完成後會自動顯示'];
    const labels = {
      ready: '資料正常',
      partial: '量化完成',
      stale: '快照過期',
      model_failed: 'AI 失敗',
      data_degraded: '資料降級',
      refreshing: '更新中',
      failed: '建立失敗',
    };
    const generated = snapshot.generated_at ? new Date(snapshot.generated_at).toLocaleString('zh-TW') : '-';
    const model = snapshot.model_overlay || {};
    const detail = model.status === 'failed'
      ? 'AI 深度分析失敗；量化結果與上一份成功 Overlay 保留'
      : `最後快照 ${generated} · ${snapshot.data_quality?.fallback ? 'FALLBACK' : '官方批次來源'}`;
    return [labels[snapshot.status] || snapshot.status, snapshot.market_regime?.summary || '市場狀態整理中', detail];
  }

  function renderSummary(payload) {
    const snapshot = payload?.market_snapshot;
    const [badge, status, freshness] = statusCopy(snapshot);
    node('homeMarketSessionBadge').textContent = badge;
    node('homeMarketStatusText').textContent = status;
    node('homeMarketFreshness').textContent = freshness;
    if (!snapshot) return;
    const regime = snapshot.market_regime || {};
    node('marketRegimeLabel').textContent = regime.label || '-';
    node('marketBreadthValue').textContent = regime.breadth_percent == null ? '-' : `${Number(regime.breadth_percent).toFixed(2)}%`;
    node('marketUniverseCount').textContent = `${Number(snapshot.universe?.resolved_count || 0).toLocaleString()} 檔`;
    node('marketValidCount').textContent = `${Number(snapshot.universe?.valid_data_count || 0).toLocaleString()} 檔`;
    const model = snapshot.model_overlay || {};
    const queued = Object.values(snapshot.candidate_details || {}).filter(item => item.model_status === 'queued').length;
    node('marketModelCount').textContent = model.status === 'running'
      ? `分析中 ${queued} 檔`
      : `${Number(model.analyzed_symbols?.length || 0)} 檔`;
    node('marketSnapshotTime').textContent = snapshot.generated_at ? new Date(snapshot.generated_at).toLocaleTimeString('zh-TW', { hour: '2-digit', minute: '2-digit' }) : '-';
    const classifications = snapshot.classification_counts || {};
    const portfolio = snapshot.portfolio_action_counts || {};
    const summary = snapshot.universe || {};
    const statValues = {
      marketInvestableCount: summary.investable_count ?? 0,
      marketClassifiedCount: summary.classified_count || 0,
      marketBuyNowCount: classifications.BUY_NOW || 0,
      marketWatchCount: classifications.WATCH || 0,
      marketFutureBuyCount: classifications.FUTURE_BUY || 0,
      marketAvoidCount: (classifications.AVOID_NOW || 0) + (classifications.INSUFFICIENT_DATA || 0),
      marketSellReduceCount: (portfolio.reduce || 0) + (portfolio.exit || 0),
    };
    Object.entries(statValues).forEach(([id, value]) => { if (node(id)) node(id).textContent = Number(value).toLocaleString(); });
    const productStatuses = summary.product_classification_status_counts || {};
    if (node('marketProductClassificationStatus')) node('marketProductClassificationStatus').textContent = `已核驗 ${Number(productStatuses.verified ?? 0)} · 待核驗 ${Number(productStatuses.unknown ?? 0)} · 衝突 ${Number(productStatuses.conflict ?? 0)}`;
    node('marketRegimeStrip').dataset.status = snapshot.status;
  }

  function detailForSymbol(symbol) {
    const normalized = String(symbol || '').toUpperCase();
    return searchedDetails.get(normalized)?.detail
      || bootstrapPayload?.market_snapshot?.candidate_details?.[normalized] || null;
  }

  function snapshotIdForSymbol(symbol) {
    return searchedDetails.get(String(symbol || '').toUpperCase())?.snapshotId
      || bootstrapPayload?.market_snapshot?.snapshot_id || null;
  }

  function isConcreteSecuritySymbol(symbol) {
    const normalized = String(symbol || '').trim().toUpperCase();
    return Boolean(normalized) && !normalized.startsWith('^') && normalized !== 'TX=F';
  }

  function preserveExplicitTaskSelection(liveContext) {
    const liveSelection = liveContext?.selection || {};
    const symbol = String(liveSelection.symbol || '').trim().toUpperCase();
    const explicitSymbols = (liveSelection.explicit_intent_symbols || [])
      .map(item => String(item || '').trim().toUpperCase());
    const hydratedSymbol = String(WorkspaceContextStore.get().selection.symbol || '')
      .trim().toUpperCase();

    // Agent Dock restoration may complete while this bootstrap request is in
    // flight. The server payload then still contains no selected security, and
    // hydrate would silently replace the accepted task symbol with the default
    // market index. Preserve only an exact single-symbol task contract; a
    // concrete selection returned by the server always remains authoritative.
    if (!isConcreteSecuritySymbol(symbol)
      || explicitSymbols.length !== 1
      || explicitSymbols[0] !== symbol
      || isConcreteSecuritySymbol(hydratedSymbol)) return;

    WorkspaceContextStore.set({
      selection: {
        ...liveSelection,
        symbol,
        explicit_intent_symbols: [symbol],
      },
    }, { persist: false, reason: 'bootstrap:preserve-explicit-task-selection' });
  }

  function entityForSymbol(symbol) {
    return searchedEntities.get(String(symbol || '').toUpperCase()) || null;
  }

  function instrumentContext(symbol, detail) {
    const entity = entityForSymbol(symbol);
    return {
      selection: {
        entity_id: detail?.entity_id || entity?.entity_id || symbol,
        symbol,
        candidate_id: detail?.symbol || null,
      },
      data: {
        market_snapshot_id: snapshotIdForSymbol(symbol),
        latest_quote: detail ? {
          symbol: detail.symbol,
          price: detail.latest_price,
          change_percent: detail.change_percent,
          source: detail.data_quality?.source,
        } : null,
        freshness: detail?.data_quality || {},
      },
    };
  }

  function contextBadges(context, detail = null) {
    const target = node('workspaceContextBadges');
    if (!target) return;
    const labels = [
      context.chart.timeframe || '1d',
      context.chart.range || '1y',
      context.chart.price_basis || 'unadjusted',
      detail?.decision_label,
      detail?.data_quality?.status,
    ].filter(Boolean);
    target.innerHTML = labels.map(label => `<span>${escapeHtml(label)}</span>`).join('');
  }

  async function selectInstrument(symbol, { source = 'workspace', focusChart = false } = {}) {
    const normalized = String(symbol || '').trim().toUpperCase();
    if (!normalized) return;
    const currentSequence = ++selectionSequence;
    const detail = detailForSymbol(normalized);
    const entity = entityForSymbol(normalized);
    selectedDetail = detail;
    state.symbol = normalized;
    node('workspaceSelectedInstrument').textContent = `${detail?.name || entity?.name || (normalized === '^TWII' ? '加權指數' : normalized)} ${normalized}`;
    const context = WorkspaceContextStore.set(instrumentContext(normalized, detail), { reason: `select:${source}` });
    contextBadges(context, detail);
    renderHomeChartDecision(detail);
    if (detail) MarketDecisionBoard.renderDetail(detail, homeDetailTab);
    feedback(`正在切換 ${normalized}；既有圖表會保留到新資料完成。`);
    try {
      await loadSummary(normalized, { navigate: false });
      if (currentSequence !== selectionSequence) return;
      if (detail) {
        MarketDecisionBoard.renderDetail(
          detail,
          homeDetailTab,
          { summary: state.detailSummary },
        );
      }
      feedback(`${normalized} 圖表、詳細資料與 Agent Context 已同步。`, 'success');
      syncHomeChartPreview();
      if (focusChart) node('priceChartOverlay')?.focus({ preventScroll: false });
    } catch (error) {
      if (currentSequence !== selectionSequence) return;
      feedback(`${normalized} 圖表更新失敗；市場快照與上一份圖表仍保留：${error.message}`, 'warning');
    }
  }

  function askAgent(symbol) {
    const detail = detailForSymbol(symbol);
    const prompt = detail
      ? `分析 ${detail.name} ${detail.symbol}。目前分類：${detail.decision_label}；觸發：${detail.trigger}；失效：${detail.invalidation}。請結合目前圖表、候選原因、正反證據與持倉狀態回答。`
      : `分析 ${symbol}，並使用目前首頁圖表、時間範圍與持倉 Context。`;
    const composer = node('globalAgentPrompt');
    if (composer) composer.value = prompt;
    window.AgentDockController?.open();
    node('globalAgentSend')?.click();
  }

  function snapshotSearchResult(query) {
    const normalized = String(query || '').trim().toUpperCase();
    return Object.values(bootstrapPayload?.market_snapshot?.candidate_details || {}).find(item => (
      item.symbol === normalized
      || item.symbol.startsWith(`${normalized}.`)
      || String(item.name || '').trim().toLocaleLowerCase('zh-TW') === String(query || '').trim().toLocaleLowerCase('zh-TW')
    )) || null;
  }

  function bestEntityMatch(query, items) {
    const normalized = String(query || '').trim().toUpperCase();
    const normalizedName = String(query || '').trim().toLocaleLowerCase('zh-TW');
    return (items || []).find(item => String(item.symbol || '').toUpperCase() === normalized)
      || (items || []).find(item => String(item.symbol || '').toUpperCase().startsWith(`${normalized}.`))
      || (items || []).find(item => String(item.name || '').trim().toLocaleLowerCase('zh-TW') === normalizedName)
      || (items || [])[0]
      || null;
  }

  async function searchOrAskAgent(query) {
    query = String(query || '').trim();
    if (!query) return;
    const input = node('homeMarketSearch');
    const local = snapshotSearchResult(query);
    if (local) {
      await selectInstrument(local.symbol, { source: 'search:snapshot', focusChart: true });
      return;
    }
    if (input) input.setAttribute('aria-busy', 'true');
    feedback(`正在搜尋「${query}」的正式證券主檔…`);
    try {
      const canonical = /^[0-9]{4}[A-Z0-9]{0,2}\.TW(?:O)?$/.test(query.toUpperCase())
        ? query.toUpperCase() : null;
      if (canonical) {
        // Bootstrap contains only the rendered ranking slices. This GET reads
        // the complete persisted snapshot, without creating a scan or Agent Run.
        try {
          const saved = await api(`/api/instruments/${encodeURIComponent(canonical)}/intelligence`);
          if (saved?.detail?.symbol !== canonical || !saved.snapshot_id) {
            throw new Error('快照明細與搜尋的證券身分不一致');
          }
          searchedDetails.set(canonical, { detail: saved.detail, snapshotId: saved.snapshot_id });
          searchedEntities.set(canonical, saved.detail);
          await selectInstrument(canonical, { source: 'search:persisted-snapshot', focusChart: true });
          return;
        } catch (error) {
          if (error.status !== 404 && !/^404(?:\s|$)/.test(String(error.message || ''))) throw error;
        }
      }
      const payload = await api(uiDataApi(`/entities/search?q=${encodeURIComponent(query)}`));
      const entity = bestEntityMatch(query, payload.items);
      if (entity?.symbol && (!canonical || String(entity.symbol).toUpperCase() === canonical)) {
        searchedEntities.set(String(entity.symbol).toUpperCase(), entity);
        await selectInstrument(entity.symbol, { source: 'search:entity', focusChart: true });
        return;
      }
      const composer = node('globalAgentPrompt');
      if (composer) composer.value = query;
      window.AgentDockController?.open();
      feedback(`未找到「${query}」的證券資料；已放入 Agent 草稿。如要詢問，請自行按送出。`);
    } catch (error) {
      feedback(`證券搜尋失敗，未誤送成 Agent 問題：${error.message}`, 'warning');
    } finally {
      if (input) input.removeAttribute('aria-busy');
    }
  }

  async function action(button) {
    const actionName = button.dataset.workspaceAction;
    const symbol = String(button.dataset.symbol || '').toUpperCase();
    const detail = detailForSymbol(symbol);
    if (actionName === 'select') return selectInstrument(symbol, { source: 'card', focusChart: true });
    if (actionName === 'agent') return askAgent(symbol);
    if (actionName === 'watch') {
      const result = await api('/api/watchlists/home-default/symbols', {
        method: 'POST',
        body: JSON.stringify({ symbol }),
      });
      feedback(`${result.symbol} 已加入首頁自選。`, 'success');
      return;
    }
    if (actionName === 'alert') {
      const result = await api('/api/alerts', {
        method: 'POST',
        body: JSON.stringify({
          symbol,
          alert_type: 'market_intelligence_trigger',
          rule: {
            trigger: detail?.trigger,
            trigger_price: detail?.trigger_price,
            invalidation: detail?.invalidation,
            snapshot_id: snapshotIdForSymbol(symbol),
          },
        }),
      });
      feedback(`${symbol} 監控提醒已建立：${result.alert_id}`, 'success');
      return;
    }
    if (actionName === 'compare') {
      const current = WorkspaceContextStore.get();
      const symbols = [...new Set([...(current.comparison.symbols || []), symbol])].slice(0, 6);
      WorkspaceContextStore.set({ comparison: { symbols } }, { reason: 'comparison:add' });
      if (symbols.length >= 2) {
        const result = await api('/api/comparisons', {
          method: 'POST',
          body: JSON.stringify({ symbols }),
        });
        feedback(`比較清單已保存：${symbols.join('、')}（${result.comparison_id}）`, 'success');
      } else {
        feedback(`${symbol} 已加入比較；再選一檔即可建立比較。`);
      }
      return;
    }
    if (actionName === 'evidence') {
      const payload = await api(`/api/instruments/${encodeURIComponent(symbol)}/evidence`);
      homeDetailTab = 'evidence';
      document.querySelectorAll('[data-workspace-detail]').forEach(tab => tab.classList.toggle('is-active', tab.dataset.workspaceDetail === 'evidence'));
      MarketDecisionBoard.renderDetail({ ...detail, evidence: payload.items }, 'evidence');
      feedback(`${symbol} 的 Host 證據、來源時間與資料品質已載入。`, 'success');
      return;
    }
    if (actionName === 'paper') {
      const result = await api('/api/paper-trading/preview', {
        method: 'POST',
        body: JSON.stringify({
          symbol,
          side: detail?.is_position ? 'reduce' : 'buy',
          quantity_lots: 1,
          rationale: `首頁市場快照 ${snapshotIdForSymbol(symbol) || ''} 預覽`,
          actor: 'user',
        }),
      });
      const advisory = result.risk_advisory || {};
      feedback(`${symbol} 模擬交易預覽完成；中央風控 ${advisory.order_allowed === false ? '阻擋' : '已評估'}，未送出任何正式委託。`, advisory.order_allowed === false ? 'warning' : 'success');
    }
  }

  function bindActions() {
    node('home')?.addEventListener('click', event => {
      const button = event.target.closest('[data-workspace-action]');
      if (!button) return;
      action(button).catch(error => feedback(`操作失敗：${error.message}`, 'warning'));
    });
    node('marketDecisionSort')?.addEventListener('change', event => {
      MarketDecisionBoard.render(bootstrapPayload, event.target.value);
    });
    document.querySelectorAll('[data-workspace-detail]').forEach(button => {
      button.addEventListener('click', () => {
        document.querySelectorAll('[data-workspace-detail]').forEach(item => item.classList.toggle('is-active', item === button));
        homeDetailTab = button.dataset.workspaceDetail;
        if (selectedDetail) MarketDecisionBoard.renderDetail(
          selectedDetail,
          button.dataset.workspaceDetail,
          { summary: state.detailSummary },
        );
      });
    });
    node('refreshMarketSnapshot')?.addEventListener('click', refresh);
    node('compareWorkspaceStocks')?.addEventListener('click', () => {
      const symbols = WorkspaceContextStore.get().comparison.symbols || [];
      feedback(symbols.length >= 2 ? `目前比較：${symbols.join('、')}` : '請從決策卡加入 2～6 檔股票。');
    });
    node('compactMarketNavigation')?.addEventListener('click', event => {
      const compact = !node('home').classList.contains('market-navigation-compact');
      node('home').classList.toggle('market-navigation-compact', compact);
      event.currentTarget.setAttribute('aria-pressed', String(compact));
      node('home').dataset.compactNavigation = String(compact);
    });
    node('homeMarketSearch')?.addEventListener('keydown', event => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      const query = event.currentTarget.value.trim();
      if (!query) return;
      searchOrAskAgent(query);
    });
    document.addEventListener('keydown', event => {
      const tag = event.target?.tagName?.toLowerCase();
      if (['input', 'textarea', 'select'].includes(tag)) return;
      if (event.key === '/') { event.preventDefault(); node('homeMarketSearch')?.focus(); }
      else if (event.key.toLowerCase() === 'g') node('priceChartOverlay')?.focus();
      else if (event.key.toLowerCase() === 'd') node('marketDecisionTitle')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      else if (event.key.toLowerCase() === 'a' && selectedDetail) action({ dataset: { workspaceAction: 'watch', symbol: selectedDetail.symbol } }).catch(() => {});
      else if (event.key.toLowerCase() === 'r' && selectedDetail) action({ dataset: { workspaceAction: 'alert', symbol: selectedDetail.symbol } }).catch(() => {});
      else if (event.key.toLowerCase() === 'c' && selectedDetail) action({ dataset: { workspaceAction: 'compare', symbol: selectedDetail.symbol } }).catch(() => {});
      else if (event.key.toLowerCase() === 't' && selectedDetail) action({ dataset: { workspaceAction: 'paper', symbol: selectedDetail.symbol } }).catch(() => {});
    });
  }

  async function applyBootstrap(payload, { selectDefault = true } = {}) {
    searchedDetails.clear();
    searchedEntities.clear();
    bootstrapPayload = payload;
    state.marketIntelligenceSnapshot = payload.market_snapshot;
    const defaultChart = payload.default_chart;
    if (defaultChart?.symbol && Array.isArray(defaultChart.payload?.points) && defaultChart.payload.points.length) {
      StockWorkspaceCache.set(`chart:index:${String(defaultChart.symbol).toUpperCase()}`, defaultChart.payload, 60_000);
    }
    // A bootstrap response may arrive after the user has already switched
    // workspaces. Persisted market context is useful, but it must never send
    // the Agent Dock back to `home` or hide the tab the user just selected.
    const liveNavigation = WorkspaceContextStore.get();
    await WorkspaceContextStore.hydrate(payload.agent_context);
    preserveExplicitTaskSelection(liveNavigation);
    // Hydration itself is asynchronous.  A user can navigate while it is
    // pending, so read the visible route *after* it completes instead of
    // restoring the route that was visible when this bootstrap began.
    const workspaceToPreserve = document.documentElement.dataset.workspace
      || window.__activeWorkspace
      || liveNavigation.route.workspace;
    const tabToPreserve = document.documentElement.dataset.workspaceTab
      || liveNavigation.route.tab;
    if (workspaceToPreserve) {
      WorkspaceContextStore.set({ route: { workspace: workspaceToPreserve, tab: tabToPreserve } }, { persist: false, reason: 'bootstrap:preserve-navigation' });
    }
    renderSummary(payload);
    MarketWorkspaceNavigation.hydrate(payload);
    MarketDecisionBoard.render(payload, node('marketDecisionSort')?.value || 'rank');
    const context = WorkspaceContextStore.get();
    selectedDetail = detailForSymbol(context.selection.symbol);
    if (!selectDefault && context.selection.symbol) {
      WorkspaceContextStore.set(instrumentContext(context.selection.symbol, selectedDetail), { reason: 'bootstrap:refresh-selected-detail' });
    }
    const compact = node('home').dataset.compactNavigation === 'true';
    node('home').classList.toggle('market-navigation-compact', compact);
    node('compactMarketNavigation')?.setAttribute('aria-pressed', String(compact));
    contextBadges(context, detailForSymbol(context.selection.symbol));
    renderHomeChartDecision(detailForSymbol(context.selection.symbol));
    if (selectDefault) await selectInstrument(context.selection.symbol || payload.default_chart?.symbol || '^TWII', { source: 'bootstrap' });
  }

  async function load({ force = false, selectDefault = true } = {}) {
    const payload = await MarketIntelligenceSnapshotService.bootstrap({ force });
    await applyBootstrap(payload, { selectDefault });
    return payload;
  }

  async function refresh() {
    const button = node('refreshMarketSnapshot');
    if (button) { button.disabled = true; button.textContent = '掃描中…'; }
    node('homeMarketStatusText').textContent = '全市場量化掃描更新中；目前卡片與圖表保留。';
    try {
      const snapshot = await MarketIntelligenceSnapshotService.refresh();
      await load({ force: true, selectDefault: false });
      const symbols = snapshot?.scan_statistics?.deep_analysis_funnel || [];
      node('homeMarketStatusText').textContent = symbols.length
        ? `量化掃描完成；已更新 ${symbols.length} 檔候選股票的深度研究候選清單。`
        : '量化掃描完成；目前沒有符合深度研究條件的候選股票。';
      feedback('全市場快照已更新；不會自動建立 Agent Run。', 'success');
    } catch (error) {
      feedback(`市場掃描失敗；上一份成功快照仍保留：${error.message}`, 'warning');
    } finally {
      if (button) { button.disabled = false; button.textContent = '執行市場掃描'; }
    }
  }

  async function init() {
    if (initialized || !node('home')) return;
    initialized = true;
    renderInstrumentWorkbench();
    document.addEventListener('stock-chart-data-updated', syncHomeChartPreview);
    new MutationObserver(syncHomeChartPreview).observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-ui-theme'],
    });
    MarketWorkspaceNavigation.init();
    bindActions();
    MarketIntelligenceSnapshotService.connect(() => load({ force: true, selectDefault: false }).catch(() => {}));
    await load();
  }

  window.HomeMarketWorkspace = {
    render: renderInstrumentWorkbench,
    setSurface: setInstrumentSurface,
    syncChartPreview: syncHomeChartPreview,
    init,
    load,
    refresh,
    selectInstrument,
    searchOrAskAgent,
    getBootstrap: () => bootstrapPayload,
  };
})();
