function twColor(value) { return value >= 0 ? 'tw-red' : 'tw-green'; }
function fmt(n) { return typeof n === 'number' ? n.toLocaleString() : n; }
function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
function symbolKey(symbol) { return String(symbol || '').trim().toUpperCase().replace(/\.(TW|TWO)$/i, ''); }
function isActiveRealtimeRequest(requestId, requestKey, payloadSymbol) {
  if (state.activeRealtimeRequestId !== requestId) return false;
  if (state.activeRealtimeSymbolKey !== requestKey) return false;
  if (payloadSymbol === undefined || payloadSymbol === null || payloadSymbol === '') return true;
  return symbolKey(payloadSymbol) === requestKey;
}

function openSecuredEventStream(url, eventNames, onEvent, onError) {
  let closed = false;
  let controller = null;
  const decoder = new TextDecoder();

  const connect = async () => {
    while (!closed) {
      controller = new AbortController();
      try {
        // Use the project's authenticated fetch wrapper. Native EventSource
        // cannot attach the process-local runtime header and therefore turned
        // every otherwise valid stream into a visible 403 error.
        const response = await fetch(url, {
          cache: 'no-store',
          headers: { Accept: 'text/event-stream' },
          signal: controller.signal,
        });
        if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);
        const reader = response.body.getReader();
        let buffer = '';
        while (!closed) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true }).replaceAll('\r\n', '\n');
          let boundary = buffer.indexOf('\n\n');
          while (boundary >= 0) {
            const block = buffer.slice(0, boundary);
            buffer = buffer.slice(boundary + 2);
            let type = 'message';
            const data = [];
            block.split('\n').forEach((line) => {
              if (line.startsWith('event:')) type = line.slice(6).trim() || 'message';
              if (line.startsWith('data:')) data.push(line.slice(5).trimStart());
            });
            if (data.length && eventNames.has(type)) onEvent({ type, data: data.join('\n') });
            boundary = buffer.indexOf('\n\n');
          }
        }
      } catch (error) {
        if (!closed && error?.name !== 'AbortError') onError(error);
      }
      if (!closed) await new Promise((resolve) => window.setTimeout(resolve, 1500));
    }
  };

  void connect();
  return {
    close() {
      closed = true;
      controller?.abort();
    },
  };
}

function realtimeTradingStatusLabel(status) {
  return ({
    pre_open: '開盤前試撮',
    trading: '盤中交易',
    closing_auction: '收盤試撮',
    halted: '暫停交易',
    delayed_open: '延後開盤',
    delayed_close: '延後收盤',
    trial: '試撮',
    closed: '已收盤',
    unknown: '狀態未知',
  })[status] || status || '狀態未知';
}

function realtimeFreshnessLabel(data) {
  const age = Number.isFinite(Number(data?.quote_age_ms))
    ? `${Math.round(Number(data.quote_age_ms) / 1000)} 秒前`
    : '時間未知';
  return `${data?.freshness === 'live' ? '即時' : data?.freshness === 'closed' ? '收盤快照' : data?.freshness === 'stale' ? '逾時' : '未知'} · ${age}`;
}

function renderRealtimePanel(statusText, payload = null) {
  const box = $('realtimePanel');
  if (!box) return;
  const data = payload?.data || payload;
  let quote = '';
  if (data && (data.last_price !== undefined || data.bids || data.asks)) {
    const chg = data.change_percent ?? null;
    const displayPrice = currentDisplayPrice(data);
    const displayLabel = currentDisplayLabel(data);
    const bidRows = (data.bids || []).map(x => `<tr><td>${x.price ?? '-'}</td><td>${x.size ?? '-'}</td></tr>`).join('');
    const askRows = (data.asks || []).map(x => `<tr><td>${x.price ?? '-'}</td><td>${x.size ?? '-'}</td></tr>`).join('');
    const bestBid = data.best_bid || data.bids?.[0] || null;
    const bestAsk = data.best_ask || data.asks?.[0] || null;
    quote = `<div class="rt-grid">
      <div class="rt-card"><span>交易狀態</span><strong>${realtimeTradingStatusLabel(data.trading_status)}</strong><small>${data.exchange || '-'} · ${realtimeFreshnessLabel(data)}</small></div>
      <div class="rt-card"><span>${displayLabel}</span><strong class="${twColor(chg || 0)}">${displayPrice === null ? '-' : niceNumber(displayPrice)}</strong><small>單筆 ${fmt(data.last_trade_size_lots ?? '-')} 張 · ${data.time || '-'}</small></div>
      <div class="rt-card"><span>最佳委買 / 委賣</span><strong>${bestBid?.price ?? '-'} / ${bestAsk?.price ?? '-'}</strong><small>${bestBid?.size ?? '-'} 張 / ${bestAsk?.size ?? '-'} 張</small></div>
      <div class="rt-card"><span>即時源漲跌幅</span><strong class="${twColor(chg || 0)}">${chg === null ? '-' : niceNumber(chg) + '%'}</strong><small>由 ${data.provider || data.source || 'realtime'} payload 提供</small></div>
      <div class="rt-card"><span>即時源開高低</span><strong>開 ${data.open ?? '-'} / 高 ${data.high ?? '-'}</strong><small>低 ${data.low ?? '-'}</small></div>
      <div class="rt-card"><span>累計成交量 / 更新</span><strong>${fmt(data.total_volume_lots ?? '-')} 張</strong><small>${data.user_delay_ms ? data.user_delay_ms/1000 + ' 秒快照' : '授權串流'} · 序號 ${data.sequence ?? '-'}</small></div>
      <div class="rt-book"><b>委買五檔</b><table><thead><tr><th>價格</th><th>張數</th></tr></thead><tbody>${bidRows}</tbody></table></div>
      <div class="rt-book"><b>委賣五檔</b><table><thead><tr><th>價格</th><th>張數</th></tr></thead><tbody>${askRows}</tbody></table></div>
    </div>`;
  }
  box.innerHTML = `<div class="event"><h4>盤中即時行情</h4><p>${statusText}</p>${quote}</div>`;
}

async function startRealtime(symbol) {
  const requestKey = symbolKey(symbol);
  const requestId = state.activeRealtimeRequestId + 1;
  state.activeRealtimeRequestId = requestId;
  if (state.realtimeSource) {
    state.realtimeSource.close();
    state.realtimeSource = null;
  }
  try {
    const st = await api(uiDataApi('/realtime/status'));
    if (!st.enabled) {
      renderRealtimePanel(`授權即時行情源未啟用：${st.message}`);
      return;
    }
    renderRealtimePanel(`${st.provider} 已啟用，訂閱 ${symbol} 行情中；更新間隔 ${st.update_interval_ms ? st.update_interval_ms/1000 + ' 秒' : '串流'}。所有可見行情與圖表參數只用即時 payload。`);
    try {
      const snapshot = await api(uiDataApi(`/realtime/quote/${encodeURIComponent(symbol)}`));
      if (snapshot?.data) {
        debugReport('B', 'features/market-chart.js:startRealtime.snapshot', 'snapshot-received', {
          requestSymbol: symbol,
          payloadSymbol: snapshot.data.symbol || null,
          hasLastPrice: snapshot.data.last_price !== null && snapshot.data.last_price !== undefined,
          bidCount: snapshot.data.bids?.length || 0,
          askCount: snapshot.data.asks?.length || 0,
        });
        if (!isActiveRealtimeRequest(requestId, requestKey, snapshot.data.symbol) || currentDisplayPrice(snapshot.data) === null) {
          renderRealtimePanel(`等待 ${symbol} 可用的即時快照；不接受舊標的或不可用 payload。`, snapshot.data);
          return;
        }
        handleRealtimeData(snapshot.data);
        renderRealtimePanel(`已載入 ${snapshot.provider} 即時快照；時間 ${snapshot.received_at}`, snapshot.data);
      } else if (snapshot?.degraded) {
        renderRealtimePanel(`即時報價暫時無法連線；目前保留交易所最新可用收盤資料。${snapshot.message || ''}`);
      }
    } catch (snapErr) {
      debugReport('E', 'features/market-chart.js:startRealtime.snapshot', 'snapshot-error', {
        requestSymbol: symbol,
        error: snapErr.message,
      });
      renderRealtimePanel(`即時快照尚未取得：${snapErr.message}；等待串流，不用歷史資料補圖。`);
    }
    const eventNames = new Set(['authenticated','subscribed','snapshot','data','quote','heartbeat','provider_error','pong','trades','books','candle','candles','message']);
    const handle = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        const incoming = msg.message?.data || msg.message?.last_good_data || msg.data || null;
        if (incoming?.schema_version === 'stock_ai.intraday_candle.v1') {
          if (!isActiveRealtimeRequest(requestId, requestKey, incoming.symbol)) return;
          scheduleIntradayCandleRefresh();
          renderRealtimePanel(
            `收到 ${msg.provider} ${symbol} 1 分 K；時間 ${incoming.bucket_end || msg.received_at}`,
            state.realtimeQuote || incoming,
          );
          return;
        }
        const payload = incoming?.schema_version === 'stock_ai.realtime_quote.v1'
          ? incoming
          : state.realtimeQuote;
        debugReport('B', 'features/market-chart.js:startRealtime.sse', 'sse-event', {
          eventType: ev.type,
          requestSymbol: symbol,
          payloadSymbol: payload?.symbol || null,
          hasPayload: !!payload,
          hasBids: !!payload?.bids,
          hasAsks: !!payload?.asks,
          hasLastPrice: payload?.last_price !== null && payload?.last_price !== undefined,
        });
        if (!isActiveRealtimeRequest(requestId, requestKey, payload?.symbol)) return;
        if (payload?.schema_version === 'stock_ai.realtime_quote.v1') {
          handleRealtimeData(payload);
          scheduleIntradayCandleRefresh();
        }
        renderRealtimePanel(`收到 ${msg.provider} ${symbol} 即時 ${ev.type}；時間 ${msg.received_at}`, payload || msg.message);
      } catch (e) {
        debugReport('D', 'features/market-chart.js:startRealtime.sse', 'sse-parse-error', {
          eventType: ev.type,
          error: e.message,
          raw: String(ev.data || '').slice(0, 300),
        });
        renderRealtimePanel(`收到即時資料，但解析失敗：${e.message}`);
      }
    };
    state.realtimeSource = openSecuredEventStream(
      uiDataApi(`/realtime/stream/${encodeURIComponent(symbol)}?channels=quote,books,candles`),
      eventNames,
      handle,
      () => renderRealtimePanel('即時行情連線中斷，系統正在重新連線；不會用收盤資料冒充盤中跳價。'),
    );
  } catch (err) {
    debugReport('E', 'features/market-chart.js:startRealtime', 'realtime-status-error', {
      requestSymbol: symbol,
      error: err.message,
    });
    renderRealtimePanel(`即時行情狀態查詢失敗：${err.message}`);
  }
}


function currentDisplayPrice(data) {
  if (!data) return null;
  if (data.last_price !== null && data.last_price !== undefined && Number.isFinite(Number(data.last_price))) return Number(data.last_price);
  const bid = data.bids?.[0]?.price, ask = data.asks?.[0]?.price;
  if (bid !== null && bid !== undefined && ask !== null && ask !== undefined && Number.isFinite(Number(bid)) && Number.isFinite(Number(ask))) return (Number(bid) + Number(ask)) / 2;
  return null;
}

function currentDisplayLabel(data) {
  if (!data) return '等待即時 payload';
  if (data.last_price !== null && data.last_price !== undefined && Number.isFinite(Number(data.last_price))) return '最後成交價';
  if (currentDisplayPrice(data) !== null) return '委買委賣中價（非成交價）';
  return '等待成交/五檔';
}
function updateRealtimeSummary(data) {
  if (!data || !$('summaryCards')) return;
  const price = currentDisplayPrice(data);
  const priceLabel = currentDisplayLabel(data);
  const chg = data.change ?? null;
  const chgPct = data.change_percent ?? null;

  $('summaryCards').innerHTML = `
    <div class="mini-card"><span>名稱 / 代號</span><strong>${data.name || '-'}<br/>${data.symbol || '-'}</strong></div>
    <div class="mini-card"><span>${priceLabel}</span><strong class="${twColor(chgPct || 0)}">${price === null ? '-' : niceNumber(price)}</strong></div>
    <div class="mini-card"><span>即時源漲跌</span><strong class="${twColor(chgPct || 0)}">${chg === null ? '-' : niceNumber(chg)} / ${chgPct === null ? '-' : niceNumber(chgPct) + '%'}</strong></div>
    <div class="mini-card"><span>即時源開高低</span><strong>開 ${data.open ?? '-'} / 高 ${data.high ?? '-'}<br/>低 ${data.low ?? '-'}</strong></div>
    <div class="mini-card"><span>累計量 / 時間</span><strong>${fmt(data.total_volume_lots ?? '-')} 張<br/>${data.time || '-'}</strong></div>
    <div class="mini-card"><span>委買一檔</span><strong>${data.bids?.[0]?.price ?? '-'} / ${data.bids?.[0]?.size ?? '-'}</strong></div>
    <div class="mini-card"><span>委賣一檔</span><strong>${data.asks?.[0]?.price ?? '-'} / ${data.asks?.[0]?.size ?? '-'}</strong></div>
    <div class="mini-card"><span>交易狀態 / 新鮮度</span><strong>${realtimeTradingStatusLabel(data.trading_status)}<br/>${realtimeFreshnessLabel(data)}</strong></div>
    <div class="mini-card"><span>資料時間 / 間隔</span><strong>${data.date || '-'} ${data.time || '-'}<br/>${data.user_delay_ms ? data.user_delay_ms/1000 + '秒' : '授權串流'}</strong></div>`;
}

function normalizeQuoteDate(data) {
  const rawDate = String(data?.date || '').replaceAll('-', '');
  if (rawDate.length >= 8) return `${rawDate.slice(0,4)}-${rawDate.slice(4,6)}-${rawDate.slice(6,8)}`;
  return new Date().toISOString().slice(0,10);
}

function formatClockNow() {
  return new Date().toLocaleString('zh-TW', {
    hour12: false,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function describeRealtimeFreshness(latestTimeLabel) {
  const now = new Date();
  const hhmm = now.getHours() * 100 + now.getMinutes();
  const today = now.toISOString().slice(0, 10);
  const isTaiwanBoard = /\.(TW|TWO)$/i.test(state.symbol || state.currentEntity?.symbol || '');
  if (!isTaiwanBoard) return `最新有效交易時間 ${latestTimeLabel}；系統現在時間 ${formatClockNow()}`;
  if (String(latestTimeLabel).startsWith(today) && hhmm >= 1335) {
    return `台股已收盤，最新有效交易時間 ${latestTimeLabel}；系統現在時間 ${formatClockNow()}`;
  }
  if (hhmm < 900) {
    return `台股未開盤，最新有效交易時間 ${latestTimeLabel}；系統現在時間 ${formatClockNow()}`;
  }
  return `盤中即時更新中，最新有效交易時間 ${latestTimeLabel}；系統現在時間 ${formatClockNow()}`;
}

function mergedChartSeries(key) {
  const history = (state.historySeries[key] || []).map((point) => ({ ...point }));
  const realtime = state.realtimeCandles[key] || [];
  if (!realtime.length) return history;
  const latestRealtime = { ...realtime[realtime.length - 1] };
  const realtimeDay = String(latestRealtime.date || '').slice(0, 10);
  const historyDay = String(history[history.length - 1]?.date || '').slice(0, 10);
  if (history.length && realtimeDay && historyDay === realtimeDay) history[history.length - 1] = latestRealtime;
  else history.push(latestRealtime);
  return history;
}

function intradaySeriesKey(symbol, tradingDate, timeframe) {
  return `${symbolKey(symbol)}|${String(tradingDate || 'latest')}|${Number(timeframe)}`;
}

function renderIntradayCandleStatus(payload = null, message = '') {
  const box = $('intradayCandleStatus');
  if (!box) return;
  if (!payload) {
    box.textContent = message || '尚未載入盤中 K 線。';
    return;
  }
  const sources = (payload.source_ids || []).join('、') || '尚無來源';
  const statusLabel = {
    complete: '完整來源批次',
    partial: '盤中／部分資料',
    no_data: '查無資料',
  }[payload.reconstruction_status] || payload.reconstruction_status;
  const availableDates = (payload.available_dates || []).join('、') || '尚無已保存交易日';
  const refreshError = payload.refresh?.error
    ? `；來源更新失敗：${payload.refresh.error}`
    : '';
  box.innerHTML = `<strong>${payload.trading_date} · ${payload.timeframe_minutes} 分 K · ${statusLabel}</strong>`
    + `；由 ${payload.source_one_minute_count} 根已保存 1 分 K 重建為 ${payload.candle_count} 根`
    + `；來源 ${escapeHtml(sources)}；已保存日期 ${escapeHtml(availableDates)}${escapeHtml(refreshError)}`;
}

function ensureDailyHistoryDates() {
  const startInput = $('dailyHistoryStart');
  const endInput = $('dailyHistoryEnd');
  if (!startInput || !endInput) return null;
  const today = new Date();
  const end = today.toISOString().slice(0, 10);
  const startDate = new Date(today);
  startDate.setUTCFullYear(startDate.getUTCFullYear() - 1);
  const start = startDate.toISOString().slice(0, 10);
  if (!endInput.value) endInput.value = end;
  if (!startInput.value) startInput.value = start;
  return { start: startInput.value, end: endInput.value };
}

function dailyHistoryQueryUrl(symbol, options = {}) {
  const range = ensureDailyHistoryDates();
  if (!range) return uiDataApi(`/market/${encodeURIComponent(symbol)}/history`);
  const priceBasis = String($('dailyHistoryPriceBasis')?.value || 'unadjusted');
  const query = new URLSearchParams({
    start: range.start,
    end: range.end,
    limit: String(options.limit || 5000),
    refresh: options.refresh === false ? 'false' : 'true',
    allow_fallback: 'true',
    price_basis: priceBasis,
    refresh_adjustments: options.refreshAdjustments === false ? 'false' : 'true',
    require_complete_adjustment: 'true',
  });
  if (options.cursor) query.set('cursor', options.cursor);
  return uiDataApi(`/market/${encodeURIComponent(symbol)}/history?${query.toString()}`);
}

function renderDailyHistoryStatus(payload, loadedCount = null) {
  const box = $('intradayCandleStatus');
  if (!box || !payload) return;
  const count = Number(loadedCount ?? payload.total_point_count ?? payload.point_count ?? 0);
  const sources = (payload.source_ids || []).join('、') || '尚無來源';
  const complete = payload.range_complete ? '完整查詢' : '部分覆蓋';
  const turnover = payload.turnover_complete
    ? '成交金額完整'
    : `成交金額 ${payload.field_coverage?.turnover || 0}/${count}`;
  const fallback = payload.is_fallback ? '；含研究用途 fallback' : '';
  const basis = {
    unadjusted: '原始未復權',
    forward_adjusted: '前復權（終點錨定）',
    backward_adjusted: '後復權（起點錨定）',
  }[payload.price_basis] || payload.price_basis || '原始未復權';
  const adjustment = payload.price_basis === 'unadjusted'
    ? ''
    : `；權息因子${payload.adjustment_complete ? '完整' : '不完整'}`
      + `、${Number(payload.adjustment_event_count || 0)} 個事件`
      + `、來源 ${escapeHtml(payload.factor_source_id || '尚無來源')}`;
  box.innerHTML = `<strong>${escapeHtml(payload.requested_start || '-')} ～ ${escapeHtml(payload.requested_end || '-')} · ${complete}</strong>`
    + `；共 ${count} 根${escapeHtml(basis)}日 K；${escapeHtml(turnover)}`
    + `；來源 ${escapeHtml(sources)}${adjustment}${escapeHtml(fallback)}`;
}

function liquidityNumber(value, digits = 2) {
  if (value === null || value === undefined || value === '') return '無資料';
  const number = Number(value);
  return Number.isFinite(number) ? number.toLocaleString(undefined, { maximumFractionDigits: digits }) : '無資料';
}

function renderLiquidityAssessment(payload) {
  const box = $('liquidityBox');
  if (!box) return;
  const metrics = payload?.metrics || {};
  const order = payload?.order_assessment || {};
  const tradability = payload?.tradability || {};
  const labels = {
    highly_tradeable: '高度可交易',
    tradeable: '可交易',
    constrained: '流動性受限',
    insufficient_data: '資料不足',
  };
  const blockers = (tradability.blockers || []).map(item => `<li>${escapeHtml(item)}</li>`).join('');
  const share = payload?.sources?.share_revision;
  box.innerHTML = `<div class="event">
    <h4>${escapeHtml(payload.symbol || '')} · ${escapeHtml(labels[tradability.status] || tradability.status || '尚未評估')}</h4>
    <p>近 ${escapeHtml(String(payload.window_sessions || '-'))} 個交易日；平均量 ${liquidityNumber(metrics.average_daily_volume_shares, 0)} 股；平均成交額 TWD ${liquidityNumber(metrics.average_daily_turnover_twd, 0)}</p>
    <p>最新成交額 TWD ${liquidityNumber(metrics.latest_turnover_twd, 0)}；換手率 ${liquidityNumber(metrics.turnover_rate_percent, 4)}%；即時買賣價差 ${liquidityNumber(metrics.bid_ask_spread_bps, 2)} bps</p>
    <p>委託 ${liquidityNumber(order.order_quantity_shares, 0)} 股；平均量占比 ${liquidityNumber(order.average_volume_participation_percent, 4)}%；預估滑價 ${liquidityNumber(order.estimated_slippage_bps, 2)} bps（模型估計，非成交保證）</p>
    <small>日資料來源：${escapeHtml((payload.sources?.history_source_ids || []).join('、') || '無')}；即時價差：${escapeHtml(payload.sources?.quote_source_id || '無')}；股數：${escapeHtml(share?.source_id || '無')} ${escapeHtml(share?.effective_date || '')}</small>
    ${blockers ? `<ul>${blockers}</ul>` : ''}
  </div>`;
}

async function loadLiquidityAssessment(symbol, options = {}) {
  const box = $('liquidityBox');
  if (!symbol || !box) return null;
  const quantityText = String($('liquidityOrderQuantity')?.value || '').trim();
  const query = new URLSearchParams({
    window_sessions: '20',
    refresh: options.refresh === false ? 'false' : 'true',
  });
  if (quantityText) query.set('order_quantity_shares', quantityText);
  box.textContent = `正在評估 ${symbol} 的官方成交量、成交額與即時價差…`;
  try {
    const payload = await api(uiDataApi(`/market/${encodeURIComponent(symbol)}/liquidity?${query}`));
    renderLiquidityAssessment(payload);
    return payload;
  } catch (error) {
    box.innerHTML = renderEmptyBlock('流動性評估失敗', error.message);
    return null;
  }
}

function anomalyMetric(value, suffix = '') {
  if (value === null || value === undefined || value === '') return '無資料';
  const number = Number(value);
  return Number.isFinite(number) ? `${number.toLocaleString(undefined, { maximumFractionDigits: 2 })}${suffix}` : '無資料';
}

function renderTradingAnomalies(payload) {
  const list = $('tradingAnomalyList');
  const status = $('tradingAnomalyStatus');
  if (!list || !status) return;
  const items = payload?.items || payload?.events || [];
  status.textContent = items.length
    ? `已保存 ${items.length} 個事件；規則 ${payload?.detector_version || 'stock_ai.daily_trading_anomaly_detector.v1'}。`
    : '目前沒有已保存的異常事件；可按「掃描近半年」查詢官方未復權日 K。';
  if (!items.length) {
    list.innerHTML = renderEmptyBlock('尚無異常交易事件', '掃描不會用假行情或模型推測補值。');
    return;
  }
  const labels = { open: '待追蹤', acknowledged: '已確認', resolved: '已解除' };
  list.innerHTML = items.map((item) => {
    const tracking = item.tracking || {};
    const metrics = item.metrics || {};
    const nextButton = tracking.status === 'resolved'
      ? `<button type="button" data-anomaly-event="${escapeHtml(item.event_id)}" data-anomaly-status="reopened">重新開啟</button>`
      : tracking.status === 'acknowledged'
        ? `<button type="button" data-anomaly-event="${escapeHtml(item.event_id)}" data-anomaly-status="resolved">標記解除</button>`
        : `<button type="button" data-anomaly-event="${escapeHtml(item.event_id)}" data-anomaly-status="acknowledged">確認追蹤</button>`;
    return `<div class="event" role="listitem">
      <h4>${escapeHtml(item.trading_date)} · ${escapeHtml(item.title)} <span class="chip">${escapeHtml(labels[tracking.status] || tracking.status || '待追蹤')}</span></h4>
      <p>${escapeHtml(item.summary)}</p>
      <p>價格 ${anomalyMetric(metrics.price_change_percent, '%')}；跳空 ${anomalyMetric(metrics.gap_percent, '%')}；量比 ${anomalyMetric(metrics.volume_ratio, ' 倍')}</p>
      <small>來源 ${escapeHtml((item.source_ids || []).join('、') || '無')} · 觀察 ${escapeHtml(String(tracking.observation_count || 0))} 次 · ${escapeHtml(item.detector_version || '')}</small>
      <div class="condition-row">${nextButton}</div>
    </div>`;
  }).join('');
}

async function loadTradingAnomalies(symbol) {
  const list = $('tradingAnomalyList');
  if (!symbol || !list) return null;
  try {
    const payload = await api(uiDataApi(`/market/${encodeURIComponent(symbol)}/anomalies?limit=100`));
    renderTradingAnomalies(payload);
    return payload;
  } catch (error) {
    list.innerHTML = renderEmptyBlock('異常事件載入失敗', error.message);
    return null;
  }
}

async function scanTradingAnomalies(symbol) {
  const button = $('scanTradingAnomalies');
  const status = $('tradingAnomalyStatus');
  if (!symbol || !button || !status) return null;
  button.disabled = true;
  status.textContent = `正在用官方未復權日 K 掃描 ${symbol} 近半年…`;
  try {
    const payload = await api(uiDataApi(`/market/${encodeURIComponent(symbol)}/anomalies/scan`), {
      method: 'POST',
      body: JSON.stringify({ refresh: true }),
    });
    renderTradingAnomalies(payload);
    status.textContent = `掃描完成：${payload.history_point_count || 0} 根日 K，偵測 ${payload.detected_count || 0} 件，新保存 ${payload.created_count || 0} 件。`;
    return payload;
  } catch (error) {
    status.textContent = `掃描失敗：${error.message}`;
    return null;
  } finally {
    button.disabled = false;
  }
}

async function trackTradingAnomaly(eventId, status) {
  const symbol = String(state.currentEntity?.symbol || state.symbol || '').trim();
  if (!symbol || !eventId) return;
  await api(uiDataApi(`/market/${encodeURIComponent(symbol)}/anomalies/${encodeURIComponent(eventId)}/tracking`), {
    method: 'POST',
    body: JSON.stringify({ status }),
  });
  await loadTradingAnomalies(symbol);
}

async function loadDailyHistory(options = {}) {
  const activeSymbol = String(state.currentEntity?.symbol || state.symbol || '').trim().toUpperCase();
  // Keep the historical series, all individual-stock tabs, and Agent context
  // on the one shared selected security.  The former second input permitted
  // them to silently diverge.
  const symbol = activeSymbol;
  if (!symbol) {
    renderIntradayCandleStatus(null, '請先選擇股票。');
    return null;
  }
  const range = ensureDailyHistoryDates();
  if (!range?.start || !range?.end) {
    renderIntradayCandleStatus(null, '請選擇日 K 起日與迄日。');
    return null;
  }
  renderIntradayCandleStatus(null, `正在查詢 ${symbol} ${range.start} ～ ${range.end} 的完整日 K…`);
  let cursor = null;
  let refresh = options.refresh !== false;
  let refreshAdjustments = options.refresh !== false;
  let page = null;
  let firstPage = null;
  let fallbackCount = 0;
  let turnoverCount = 0;
  const sourceIds = new Set();
  const allPoints = [];
  do {
    page = await api(dailyHistoryQueryUrl(symbol, {
      cursor,
      refresh,
      refreshAdjustments,
      limit: 5000,
    }));
    if (!firstPage) firstPage = page;
    allPoints.push(...(page.points || []));
    fallbackCount += Number(page.fallback_count || 0);
    turnoverCount += Number(page.field_coverage?.turnover || 0);
    (page.source_ids || []).forEach(sourceId => sourceIds.add(sourceId));
    cursor = page.next_cursor || null;
    refresh = false;
    refreshAdjustments = false;
  } while (page?.has_more && cursor);
  const key = symbolKey(symbol);
  state.historySeries[key] = allPoints;
  state.historyMeta[key] = {
    ...firstPage,
    ...page,
    requested_start: range.start,
    requested_end: range.end,
    points: undefined,
    point_count: allPoints.length,
    total_point_count: allPoints.length,
    source_ids: [...sourceIds].sort(),
    fallback_count: fallbackCount,
    is_fallback: fallbackCount > 0,
    turnover_complete: allPoints.length > 0 && turnoverCount === allPoints.length,
    field_coverage: {
      ...(page?.field_coverage || {}),
      open: allPoints.length,
      high: allPoints.length,
      low: allPoints.length,
      close: allPoints.length,
      volume: allPoints.length,
      turnover: turnoverCount,
    },
  };
  state.intradayTimeframe = 'D';
  state.activeIntradayKey = null;
  renderDailyHistoryStatus(state.historyMeta[key], allPoints.length);
  const ent = state.currentEntity || { symbol, name: symbol };
  const chartName = ent.name && ent.name !== ent.symbol
    ? `${ent.name} ${ent.symbol}`
    : ent.symbol || symbol;
  const basisLabel = {
    unadjusted: '原始未復權',
    forward_adjusted: '前復權',
    backward_adjusted: '後復權',
  }[state.historyMeta[key].price_basis] || '原始未復權';
  $('chartTitle').textContent = `${chartName} · ${range.start} ～ ${range.end} · ${basisLabel}日 K`;
  renderCurrentChart();
  return state.historyMeta[key];
}

function syncHistoryControls() {
  const daily = String($('intradayTimeframe')?.value || state.intradayTimeframe || 'D') === 'D';
  $$('.daily-history-control').forEach(element => { element.hidden = !daily; });
  const tradeDate = $('intradayTradeDate')?.closest('.intraday-control');
  if (tradeDate) tradeDate.hidden = daily;
  if ($('loadIntradayCandles')) $('loadIntradayCandles').hidden = daily;
}

function updateIntradayChartTitle(payload) {
  const ent = state.currentEntity || { symbol: state.symbol, name: state.symbol };
  const symbol = String(ent.symbol || state.symbol || '').trim();
  const name = String(ent.name || '').trim();
  const chartName = name && name !== symbol ? `${name} ${symbol}` : symbol;
  const title = $('chartTitle');
  if (title) title.textContent = `${chartName} · ${payload.trading_date} · ${payload.timeframe_minutes} 分 K`;
}

async function loadIntradayCandles(options = {}) {
  const symbol = String(state.currentEntity?.symbol || state.symbol || '').trim();
  const selector = $('intradayTimeframe');
  const dateInput = $('intradayTradeDate');
  const rawTimeframe = String(selector?.value || state.intradayTimeframe || 'D');
  state.intradayTimeframe = rawTimeframe;
  if (!symbol) {
    renderIntradayCandleStatus(null, '請先選擇股票。');
    return null;
  }
  if (rawTimeframe === 'D') {
    state.activeIntradayKey = null;
    syncHistoryControls();
    return loadDailyHistory({ refresh: options.refresh !== false });
  }
  const timeframe = Number(rawTimeframe);
  const selectedDate = String(dateInput?.value || '').trim();
  const query = new URLSearchParams({
    timeframe: String(timeframe),
    refresh: options.refresh === false ? 'false' : 'true',
  });
  if (selectedDate) query.set('date', selectedDate);
  if (!options.silent) {
    renderIntradayCandleStatus(
      null,
      `正在載入 ${symbol} ${selectedDate || '最新已保存交易日'} ${timeframe} 分 K…`,
    );
  }
  const payload = await api(
    uiDataApi(`/intraday/candles/${encodeURIComponent(symbol)}?${query.toString()}`),
  );
  const key = intradaySeriesKey(symbol, payload.trading_date, timeframe);
  state.intradaySeries[key] = payload.points || [];
  state.intradayMeta[key] = payload;
  state.activeIntradayKey = key;
  if (dateInput && payload.trading_date) dateInput.value = payload.trading_date;
  renderIntradayCandleStatus(payload);
  updateIntradayChartTitle(payload);
  if (payload.points?.length) {
    drawChart(payload.points, {
      intradayOnly: true,
      timeframeMinutes: timeframe,
      intradayMeta: payload,
      historyMeta: payload,
    });
  } else {
    renderChartMessage(
      `${payload.trading_date} 查無 ${timeframe} 分 K`,
      payload.refresh?.error
        || '此交易日尚未進入本機的一分 K 修訂庫；系統不會用日 K 或補值資料冒充。',
    );
  }
  return payload;
}

function scheduleIntradayCandleRefresh() {
  if (state.intradayTimeframe === 'D') return;
  clearTimeout(state.intradayRefreshTimer);
  state.intradayRefreshTimer = setTimeout(() => {
    loadIntradayCandles({ refresh: false, silent: true }).catch(() => {});
  }, 800);
}

function initIntradayCandleControls() {
  const selector = $('intradayTimeframe');
  const dateInput = $('intradayTradeDate');
  const loadButton = $('loadIntradayCandles');
  if (!selector || !loadButton) return;
  selector.addEventListener('change', () => {
    state.intradayTimeframe = selector.value;
    syncHistoryControls();
    loadIntradayCandles({ refresh: selector.value !== 'D' }).catch(err => {
      renderIntradayCandleStatus(null, `K 線載入失敗：${err.message}`);
    });
  });
  dateInput?.addEventListener('change', () => {
    if (selector.value !== 'D') {
      loadIntradayCandles({ refresh: false }).catch(err => {
        renderIntradayCandleStatus(null, `指定交易日載入失敗：${err.message}`);
      });
    }
  });
  loadButton.addEventListener('click', () => {
    loadIntradayCandles({ refresh: selector.value !== 'D' }).catch(err => {
      renderIntradayCandleStatus(null, `分 K 載入失敗：${err.message}`);
    });
  });
  $('loadDailyHistory')?.addEventListener('click', () => {
    loadDailyHistory({ refresh: true }).catch(err => {
      renderIntradayCandleStatus(null, `歷史日 K 查詢失敗：${err.message}`);
    });
  });
  ensureDailyHistoryDates();
  syncHistoryControls();
}

function renderCurrentChart() {
  syncChartThemeFromDom($('priceChart'));
  if (state.intradayTimeframe !== 'D' && state.activeIntradayKey) {
    const intraday = state.intradaySeries[state.activeIntradayKey] || [];
    const meta = state.intradayMeta[state.activeIntradayKey] || null;
    if (intraday.length && meta) {
      drawChart(intraday, {
        intradayOnly: true,
        timeframeMinutes: meta.timeframe_minutes,
        intradayMeta: meta,
        historyMeta: meta,
      });
      return;
    }
  }
  const key = symbolKey(state.realtimeQuote?.symbol || state.currentEntity?.symbol || state.symbol);
  const candles = mergedChartSeries(key);
  const historyMeta = state.historyMeta[key] || null;
  debugReport('A', 'features/market-chart.js:renderCurrentChart', 'render-current-chart', {
    stateSymbol: state.symbol,
    currentEntitySymbol: state.currentEntity?.symbol || null,
    realtimeQuoteSymbol: state.realtimeQuote?.symbol || null,
    key,
    candleCount: candles.length,
  });
  if (candles.length >= 5) {
    drawChart(candles, { realtimeOnly: !!state.realtimeQuote, latest: state.realtimeQuote, historyMeta });
    return;
  }
  const ent = state.currentEntity || { symbol: state.symbol, name: state.symbol };
  if (candles.length) {
    renderChartMessage(
      `歷史 K 線不足（目前 ${candles.length} 根）`,
      historyMeta?.note || `${ent.name || ''} ${ent.symbol || state.symbol} 暫時沒有足夠日 K，價格資訊仍顯示於下方資料卡。`,
    );
    return;
  }
  renderChartMessage(`等待圖表資料 ${ent.name || ''} ${ent.symbol || state.symbol}`, '先載入歷史日 K，再用盤中即時報價更新最新一根 K 線與資訊卡。');
}

function setStockEventsOpen(open) {
  const layout = $('stockPrimaryLayout');
  const panel = $('stockEventPanel');
  const toggle = $('stockEventToggle');
  if (!layout || !panel || !toggle) return;
  const isOpen = Boolean(open);
  layout.dataset.eventsOpen = String(isOpen);
  toggle.setAttribute('aria-expanded', String(isOpen));
  panel.setAttribute('aria-hidden', String(!isOpen));
  panel.inert = !isOpen;
  const label = toggle.querySelector('[data-i18n]');
  const key = isOpen ? 'stock.events.hide' : 'stock.events.open';
  if (label) {
    label.dataset.i18n = key;
    label.textContent = UI_TEXT[uiSettings.language]?.[key] || (isOpen ? '關閉個股事件' : '開啟個股事件');
  }
}

function ensureStockEventListEnd(list) {
  if (!list) return null;
  const currentEnd = list.lastElementChild;
  if (currentEnd?.classList.contains('event-list-end')) return currentEnd;
  const end = document.createElement('div');
  end.className = 'event-list-end';
  end.setAttribute('aria-hidden', 'true');
  list.append(end);
  return end;
}

function syncStockEventPanelViewport(panel) {
  if (!panel) return;
  const viewportInset = 20;
  const panelTop = Math.max(panel.getBoundingClientRect().top, viewportInset);
  const availableHeight = Math.max(180, window.innerHeight - panelTop - viewportInset);
  panel.style.setProperty('--stock-event-panel-viewport-height', `${Math.floor(availableHeight)}px`);
}

function initStockEventPanel() {
  const toggle = $('stockEventToggle');
  const panel = $('stockEventPanel');
  const list = $('eventList');
  const corporateActionSync = $('corporateActionSync');
  const corporateActionSyncStatus = $('corporateActionSyncStatus');
  if (!toggle || !panel || !list) return;
  ensureStockEventListEnd(list);
  new MutationObserver(() => ensureStockEventListEnd(list)).observe(list, { childList: true });
  let viewportSyncFrame = 0;
  const scheduleViewportSync = () => {
    cancelAnimationFrame(viewportSyncFrame);
    viewportSyncFrame = requestAnimationFrame(() => syncStockEventPanelViewport(panel));
  };
  scheduleViewportSync();
  window.addEventListener('resize', scheduleViewportSync, { passive: true });
  window.addEventListener('scroll', scheduleViewportSync, { passive: true, capture: true });
  setStockEventsOpen(false);
  toggle.addEventListener('click', () => {
    setStockEventsOpen(toggle.getAttribute('aria-expanded') !== 'true');
    scheduleViewportSync();
  });
  corporateActionSync?.addEventListener('click', async () => {
    corporateActionSync.disabled = true;
    if (corporateActionSyncStatus) {
      corporateActionSyncStatus.dataset.state = 'loading';
      corporateActionSyncStatus.textContent = `正在同步 ${state.symbol} 的到期公司行動…`;
    }
    try {
      const result = await api('/api/open-stock-ai/paper-account/corporate-actions/sync', {
        method: 'POST',
        body: JSON.stringify({ symbol: state.symbol }),
      });
      if (corporateActionSyncStatus) {
        corporateActionSyncStatus.dataset.state = 'success';
        corporateActionSyncStatus.textContent = `完成：新套用 ${result.applied_count}、已處理 ${result.idempotent_count}、略過 ${result.skipped_count}`;
      }
      await loadDashboardSymbolDetails(state.symbol);
    } catch (error) {
      if (corporateActionSyncStatus) {
        corporateActionSyncStatus.dataset.state = 'error';
        corporateActionSyncStatus.textContent = `同步失敗：${error?.message || '請稍後再試'}`;
      }
    } finally {
      corporateActionSync.disabled = false;
    }
  });
  panel.addEventListener('wheel', (event) => {
    if (list.scrollHeight <= list.clientHeight || event.deltaY === 0) return;
    const deltaScale = event.deltaMode === 1
      ? 18
      : event.deltaMode === 2
        ? Math.max(list.clientHeight, 1)
        : 1;
    const delta = event.deltaY * deltaScale;
    const atTop = list.scrollTop <= 0;
    const atBottom = list.scrollTop + list.clientHeight >= list.scrollHeight - 1;
    if ((delta < 0 && atTop) || (delta > 0 && atBottom)) return;
    event.preventDefault();
    event.stopPropagation();
    list.scrollTop += delta;
  }, { passive: false });
  panel.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    event.preventDefault();
    setStockEventsOpen(false);
    toggle.focus({ preventScroll: true });
  });
}

function makeRealtimePoint(data) {
  const price = currentDisplayPrice(data);
  if (price === null || price === undefined || !data.symbol) return null;
  const quoteTime = data.time || new Date().toLocaleTimeString('zh-TW', {hour12:false});
  const quoteDate = normalizeQuoteDate(data);
  return {
    date: `${quoteDate} ${quoteTime}`,
    time: quoteTime,
    price,
    bid: toFiniteNumber(data.bids?.[0]?.price),
    ask: toFiniteNumber(data.asks?.[0]?.price),
    cumulativeVolumeLots: toFiniteNumber(data.total_volume_lots),
    raw: data,
  };
}

function updateRealtimeCandle(key, point) {
  state.realtimeCandles[key] = state.realtimeCandles[key] || [];
  const candles = state.realtimeCandles[key];
  const bucket = point.date.slice(0, 16); // one-minute realtime candle bucket
  const last = candles[candles.length - 1];
  if (last && last.date === bucket) {
    last.high = Math.max(last.high, point.price);
    last.low = Math.min(last.low, point.price);
    last.close = point.price;
    if (point.cumulativeVolumeLots !== null) {
      last.volume = Math.max(0, point.cumulativeVolumeLots - (last.startVolumeLots ?? point.cumulativeVolumeLots));
      last.cumulativeVolumeLots = point.cumulativeVolumeLots;
    }
    last.lastQuoteTime = point.time;
  } else {
    const prevCum = candles.length ? candles[candles.length - 1].cumulativeVolumeLots : point.cumulativeVolumeLots;
    candles.push({
      date: bucket,
      open: point.price,
      high: point.price,
      low: point.price,
      close: point.price,
      volume: point.cumulativeVolumeLots !== null && prevCum !== null ? Math.max(0, point.cumulativeVolumeLots - prevCum) : 0,
      startVolumeLots: prevCum,
      cumulativeVolumeLots: point.cumulativeVolumeLots,
      source: 'realtime_intraday_sample',
      lastQuoteTime: point.time,
    });
  }
  if (candles.length > 240) candles.splice(0, candles.length - 240);
  return candles;
}

function pushRealtimePoint(data) {
  const point = makeRealtimePoint(data);
  if (!point) return;
  const key = symbolKey(data.symbol);
  if (!key) return;
  state.realtimeSeries[key] = state.realtimeSeries[key] || [];
  const arr = state.realtimeSeries[key];
  const last = arr[arr.length - 1];
  if (last && last.time === point.time) Object.assign(last, point);
  else arr.push(point);
  if (arr.length > 240) arr.splice(0, arr.length - 240);
  const candles = updateRealtimeCandle(key, point);
  debugReport('C', 'features/market-chart.js:pushRealtimePoint', 'push-realtime-point', {
    payloadSymbol: data.symbol || null,
    key,
    seriesLength: arr.length,
    candleLength: candles.length,
    latestBucket: candles[candles.length - 1]?.date || null,
  });
  if (state.intradayTimeframe === 'D') {
    drawChart(mergedChartSeries(key), { realtimeOnly: true, latest: data, historyMeta: state.historyMeta[key] || null });
  }
}

function drawRealtimeChart(series, latest) {
  const canvas = $('priceChart');
  if (!canvas) return;
  syncChartThemeFromDom(canvas);
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 1040, cssH = 620;
  canvas.width = Math.floor(cssW * dpr); canvas.height = Math.floor(cssH * dpr);
  ctx.setTransform(dpr,0,0,dpr,0,0); ctx.clearRect(0,0,cssW,cssH);
  const left=64,right=92,top=34,priceBottom=395,volumeTop=425,volumeBottom=520;
  const plotW=cssW-left-right, h=priceBottom-top;
  const visible=series.slice(-120);
  if (!visible.length) {
    ctx.fillStyle=CHART_THEME.background; ctx.fillRect(0,0,cssW,cssH);
    ctx.fillStyle=CHART_THEME.text; ctx.font='700 18px Segoe UI'; ctx.fillText(`等待盤中即時報價 ${latest?.name || ''} ${latest?.symbol || ''}`,64,60);
    ctx.fillStyle=CHART_THEME.muted; ctx.font='13px Segoe UI'; ctx.fillText('收到 TWSE MIS 報價後，價格軸、時間軸、Bid/Ask、盤中均線、BOLL 都會由即時資料更新。',64,88);
    syncCanvasToLiquidTexture(canvas, 'realtime-chart-message');
    return;
  }
  const values=[];
  visible.forEach(p=>{ values.push(p.price); if(p.bid) values.push(p.bid); if(p.ask) values.push(p.ask); });
  if (latest?.high) values.push(latest.high); if (latest?.low) values.push(latest.low);
  const minRaw=Math.min(...values), maxRaw=Math.max(...values), pad=Math.max((maxRaw-minRaw)*0.12, maxRaw*0.001);
  const min=minRaw-pad, max=maxRaw+pad;
  const xFor=i=>left+(visible.length<=1?0:i*(plotW/(visible.length-1)));
  const yFor=v=>priceBottom-(v-min)/Math.max(1e-9,max-min)*h;
  ctx.fillStyle=CHART_THEME.background; ctx.fillRect(0,0,cssW,cssH);
  ctx.fillStyle=CHART_THEME.panel; ctx.fillRect(left,top,plotW,h);
  ctx.fillStyle=CHART_THEME.panelMuted; ctx.fillRect(left,volumeTop,plotW,volumeBottom-volumeTop);
  ctx.strokeStyle=CHART_THEME.grid; ctx.fillStyle=CHART_THEME.muted; ctx.font='12px Segoe UI'; ctx.textAlign='left'; ctx.textBaseline='middle';
  for(let i=0;i<=6;i++){ const y=top+h*i/6, val=max-(max-min)*i/6; ctx.beginPath(); ctx.moveTo(left,y); ctx.lineTo(left+plotW,y); ctx.stroke(); ctx.fillText(niceNumber(val),left+plotW+10,y); }
  ctx.textAlign='center'; ctx.textBaseline='top';
  const labelCount=Math.min(7,visible.length);
  for(let i=0;i<labelCount;i++){ const idx=Math.round(i*(visible.length-1)/Math.max(1,labelCount-1)); const x=xFor(idx); ctx.strokeStyle=CHART_THEME.gridSoft; ctx.beginPath(); ctx.moveTo(x,top); ctx.lineTo(x,volumeBottom); ctx.stroke(); ctx.fillStyle=CHART_THEME.muted; ctx.fillText(visible[idx]?.time || '',x,volumeBottom+8); }
  const plotLine=(vals,color,width=1.5,dash=[])=>{ ctx.save(); ctx.setLineDash(dash); ctx.strokeStyle=color; ctx.lineWidth=width; ctx.beginPath(); let started=false; vals.forEach((v,i)=>{ if(v===null||v===undefined||Number.isNaN(v)) return; const x=xFor(i), y=yFor(v); if(!started){ctx.moveTo(x,y); started=true;} else ctx.lineTo(x,y); }); ctx.stroke(); ctx.restore(); };
  plotLine(visible.map(p=>p.price),CHART_THEME.price,2.2);
  plotLine(visible.map(p=>p.bid),CHART_THEME.bid,1.2,[4,4]);
  plotLine(visible.map(p=>p.ask),CHART_THEME.ask,1.2,[4,4]);
  [5,10,20,60].forEach((n,idx)=>{ const vals=movingAverage(visible.map(p=>({close:p.price})),n); plotLine(vals,[CHART_THEME.ma5,CHART_THEME.ma10,CHART_THEME.ma20,CHART_THEME.ma60][idx],1.2); });
  const bb=bollinger(visible.map(p=>({close:p.price})),20,2); plotLine(bb.upper,CHART_THEME.boll,1); plotLine(bb.lower,CHART_THEME.boll,1);
  ctx.fillStyle=CHART_THEME.muted; ctx.textAlign='left'; ctx.fillText('盤中累計量',left,volumeTop-15);
  const vols=visible.map(p=>p.volume||0); const v0=vols[0]||0; const vmax=Math.max(...vols.map(v=>Math.max(0,v-v0)),1);
  visible.forEach((p,i)=>{ const prev=i?visible[i-1].volume||p.volume||0:v0; const dv=Math.max(0,(p.volume||prev)-prev); const barH=Math.max(1,dv/vmax*(volumeBottom-volumeTop-8)); ctx.fillStyle=p.price >= (i?visible[i-1].price:p.price)?CHART_THEME.upSoft:CHART_THEME.downSoft; ctx.fillRect(xFor(i)-3,volumeBottom-barH,6,barH); });
  const macdTop=545, macdBottom=595;
  ctx.fillStyle=CHART_THEME.panelMuted; ctx.fillRect(left,macdTop,plotW,macdBottom-macdTop);
  const md=macd(visible.map(p=>({close:p.price})));
  const macdVals=[...md.line,...md.signal,...md.hist].filter(v=>Number.isFinite(v));
  const macdMax=Math.max(...macdVals.map(v=>Math.abs(v)),1e-9);
  const yMacd=v=>(macdTop+macdBottom)/2-v/macdMax*((macdBottom-macdTop)*0.42);
  ctx.strokeStyle=CHART_THEME.grid; ctx.beginPath(); ctx.moveTo(left,yMacd(0)); ctx.lineTo(left+plotW,yMacd(0)); ctx.stroke();
  md.hist.forEach((v,i)=>{ const y0=yMacd(0), yh=yMacd(v); ctx.fillStyle=v>=0?CHART_THEME.upSoft:CHART_THEME.downSoft; ctx.fillRect(xFor(i)-3,Math.min(y0,yh),6,Math.max(1,Math.abs(yh-y0))); });
  const plotMacdLine=(vals,color)=>{ ctx.strokeStyle=color; ctx.lineWidth=1.1; ctx.beginPath(); let started=false; vals.forEach((v,i)=>{ if(!Number.isFinite(v)) return; const x=xFor(i), y=yMacd(v); if(!started){ctx.moveTo(x,y); started=true;} else ctx.lineTo(x,y); }); ctx.stroke(); };
  plotMacdLine(md.line,CHART_THEME.macd); plotMacdLine(md.signal,CHART_THEME.signal);
  ctx.fillStyle=CHART_THEME.muted; ctx.textAlign='left'; ctx.fillText('MACD(12,26,9) 即時樣本',left,macdTop-12);
  ctx.fillStyle=CHART_THEME.text; ctx.font='700 15px Segoe UI'; ctx.textAlign='left'; ctx.textBaseline='alphabetic';
  ctx.fillText(`盤中即時圖 ${latest?.name || ''} ${latest?.symbol || ''}｜價格/委買委賣/均線/布林通道全由即時報價更新`,left,22);
  ctx.font='12px Segoe UI'; let lx=left+420; [['成交/中價','#fff'],['Bid','#cfcfd4'],['Ask','#8f8f96'],['MA5/10/20/60','#d8d8de'],['BOLL','#a7a7ad']].forEach(([n,c])=>{ctx.fillStyle=c;ctx.fillRect(lx,12,18,3);ctx.fillStyle='#d8d8de';ctx.fillText(n,lx+24,16);lx+=105;});
  ctx.fillStyle=CHART_THEME.muted; ctx.textAlign='right'; ctx.fillText('價格',left+plotW+64,top-10); ctx.fillText('時間',left+plotW,volumeBottom+30);
  syncCanvasToLiquidTexture(canvas, 'realtime-chart');
}

function handleRealtimeData(data) {
  const payloadKey = symbolKey(data?.symbol);
  if (!payloadKey || payloadKey !== state.activeRealtimeSymbolKey) return;
  state.realtimeQuote = data;
  debugReport('B', 'features/market-chart.js:handleRealtimeData', 'handle-realtime-data', {
    payloadSymbol: data?.symbol || null,
    hasLastPrice: data?.last_price !== null && data?.last_price !== undefined,
    bidCount: data?.bids?.length || 0,
    askCount: data?.asks?.length || 0,
  });
  updateRealtimeSummary(data);
  pushRealtimePoint(data);
  renderMonitorSignals(data.symbol || state.symbol, data, []);
}

document.querySelectorAll('.sidebar .nav-btn[data-view]').forEach(btn => btn.addEventListener('click', async () => {
  const view = btn.dataset.view;
  setView(view);
  // A view loader may outlive a subsequent click on a workspace tab.  Remember
  // the tab selected for this navigation so a late loader cannot restore the
  // workspace's previously persisted tab over the user's newer choice.
  const requestedTab = document.documentElement.dataset.workspaceTab || '';
  await loadViewData(view).catch(() => {});
  // Shared instrument chart loaders may refresh data asynchronously; keep the
  // selected workspace highlighted after those loaders finish.  Do this only
  // when the same tab is still active; otherwise an older, slower route would
  // pull the user back from a newer tab choice.
  if (window.__activeWorkspace === view
      && document.documentElement.dataset.workspaceTab === requestedTab) {
    setView(view);
  }
}));

function movingAverage(points, window) {
  const out = Array(points.length).fill(null);
  let sum = 0;
  for (let i = 0; i < points.length; i++) {
    sum += points[i].close;
    if (i >= window) sum -= points[i - window].close;
    if (i >= window - 1) out[i] = sum / window;
  }
  return out;
}

function bollinger(points, window = 20, k = 2) {
  const mid = movingAverage(points, window);
  const upper = Array(points.length).fill(null);
  const lower = Array(points.length).fill(null);
  for (let i = window - 1; i < points.length; i++) {
    const slice = points.slice(i - window + 1, i + 1).map(p => p.close);
    const avg = mid[i];
    const variance = slice.reduce((acc, x) => acc + Math.pow(x - avg, 2), 0) / window;
    const sd = Math.sqrt(variance);
    upper[i] = avg + k * sd;
    lower[i] = avg - k * sd;
  }
  return { mid, upper, lower };
}

function ema(values, span) {
  const out = Array(values.length).fill(null);
  const alpha = 2 / (span + 1);
  let prev = null;
  for (let i = 0; i < values.length; i++) {
    const v = values[i];
    prev = prev === null ? v : alpha * v + (1 - alpha) * prev;
    out[i] = prev;
  }
  return out;
}

function macd(points) {
  const closes = points.map(p => p.close);
  const ema12 = ema(closes, 12);
  const ema26 = ema(closes, 26);
  const line = closes.map((_, i) => ema12[i] - ema26[i]);
  const signal = ema(line, 9);
  const hist = line.map((v, i) => v - signal[i]);
  return { line, signal, hist };
}

function niceNumber(v) {
  if (!Number.isFinite(v)) return '-';
  if (Math.abs(v) >= 1000) return v.toFixed(0);
  if (Math.abs(v) >= 100) return v.toFixed(1);
  return v.toFixed(2);
}

function toFiniteNumber(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function normalizeChartPoints(points) {
  if (!Array.isArray(points)) return [];
  return points.map(p => {
    const open = toFiniteNumber(p.open), high = toFiniteNumber(p.high), low = toFiniteNumber(p.low), close = toFiniteNumber(p.close);
    if (open === null || high === null || low === null || close === null) return null;
    const fixedHigh = Math.max(high, open, close, low);
    const fixedLow = Math.min(low, open, close, high);
    return { ...p, date: String(p.date || ''), open, high: fixedHigh, low: fixedLow, close, volume: Math.max(0, toFiniteNumber(p.volume) ?? 0) };
  }).filter(Boolean);
}

function renderChartMessage(title, detail = '') {
  const canvas = $('priceChart');
  if (!canvas) return;
  window.StockChartInteractions?.clearRenderModel();
  syncChartThemeFromDom(canvas);
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 1040;
  const cssH = 620;
  canvas.width = Math.floor(cssW * dpr);
  canvas.height = Math.floor(cssH * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);
  ctx.fillStyle = CHART_THEME.background; ctx.fillRect(0, 0, cssW, cssH);
  ctx.fillStyle = CHART_THEME.text; ctx.font = '700 18px Segoe UI';
  ctx.fillText(title, 64, 64);
  if (detail) {
    ctx.fillStyle = CHART_THEME.muted; ctx.font = '13px Segoe UI';
    ctx.fillText(String(detail).slice(0, 140), 64, 94);
  }
  syncCanvasToLiquidTexture(canvas, 'chart-message');
}

function drawLine(ctx, values, visibleStart, xFor, yFor, color, width = 1.5, visibleEnd = values.length) {
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.beginPath();
  let started = false;
  for (let vi = 0; vi < visibleEnd - visibleStart; vi++) {
    const idx = visibleStart + vi;
    const val = values[idx];
    if (val === null || val === undefined || Number.isNaN(val)) continue;
    const x = xFor(vi);
    const y = yFor(val);
    if (!started) { ctx.moveTo(x, y); started = true; }
    else ctx.lineTo(x, y);
  }
  ctx.stroke();
}

function heikinAshiSeries(points) {
  const transformed = [];
  points.forEach((point, index) => {
    const close = (point.open + point.high + point.low + point.close) / 4;
    const previous = transformed[index - 1];
    const open = previous
      ? (previous.open + previous.close) / 2
      : (point.open + point.close) / 2;
    transformed.push({
      ...point,
      open,
      close,
      high: Math.max(point.high, open, close),
      low: Math.min(point.low, open, close),
    });
  });
  return transformed;
}

function drawChart(points, options = {}) {
  const canvas = $('priceChart');
  if (!canvas) return;
  syncChartThemeFromDom(canvas);
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 1040;
  const cssH = 620;
  canvas.width = Math.floor(cssW * dpr);
  canvas.height = Math.floor(cssH * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);

  points = normalizeChartPoints(points);
  debugReport('A', 'features/market-chart.js:drawChart', 'draw-chart-input', {
    pointCount: points.length,
    realtimeOnly: !!options.realtimeOnly,
    latestSymbol: options.latest?.symbol || null,
  });
  if (!points.length) {
    debugReport('A', 'features/market-chart.js:drawChart', 'draw-chart-empty', {
      stateSymbol: state.symbol,
      realtimeQuoteSymbol: state.realtimeQuote?.symbol || null,
    });
    renderChartMessage('等待 K 線資料', '系統會先載入歷史日 K，再用盤中即時報價更新最新一根 K 線。');
    return;
  }

  const viewport = window.StockChartInteractions?.resolveViewport(points.length)
    || { visibleStart: Math.max(0, points.length - Math.min(120, points.length)), visibleEnd: points.length };
  const visibleStart = viewport.visibleStart;
  const visibleEnd = viewport.visibleEnd;
  const visible = points.slice(visibleStart, visibleEnd);
  const chartType = window.StockChartInteractions?.getChartType() || 'candles';
  const displayedPoints = chartType === 'heikin' ? heikinAshiSeries(points) : points;
  const displayedVisible = displayedPoints.slice(visibleStart, visibleEnd);
  const mas = { ma5: movingAverage(points, 5), ma10: movingAverage(points, 10), ma20: movingAverage(points, 20), ma60: movingAverage(points, 60) };
  const bb = bollinger(points, 20, 2);
  const md = macd(points);
  const indicators = window.StockChartInteractions?.getIndicators?.() || {
    ma5: true, ma10: true, ma20: true, ma60: true, boll: true, volume: true, macd: true,
  };
  const showVolume = indicators.volume !== false;
  const showMacd = indicators.macd !== false;

  const left = 64, right = 92, top = 112;
  let priceBottom = 550;
  let volumeTop = 0, volumeBottom = 0, macdTop = 0, macdBottom = 0;
  if (showVolume && showMacd) {
    priceBottom = 350;
    volumeTop = 380; volumeBottom = 468;
    macdTop = 500; macdBottom = 585;
  } else if (showVolume) {
    priceBottom = 420;
    volumeTop = 452; volumeBottom = 550;
  } else if (showMacd) {
    priceBottom = 420;
    macdTop = 452; macdBottom = 550;
  }
  const interactionBottom = showMacd ? macdBottom : (showVolume ? volumeBottom : priceBottom);
  const plotW = cssW - left - right;
  const candleSlot = plotW / Math.max(1, visible.length);
  const candleW = clamp(candleSlot * 0.58, 3, 11);
  const xFor = (i) => left + i * candleSlot + candleSlot / 2;

  const visibleIndicators = [];
  for (let i = visibleStart; i < visibleEnd; i++) {
    ['ma5','ma10','ma20','ma60'].forEach(k => { if (indicators[k] !== false && mas[k][i] !== null) visibleIndicators.push(mas[k][i]); });
    if (indicators.boll !== false && bb.upper[i] !== null) visibleIndicators.push(bb.upper[i], bb.lower[i]);
  }
  const rawMin = Math.min(...displayedVisible.map(p => p.low), ...visibleIndicators);
  const rawMax = Math.max(...displayedVisible.map(p => p.high), ...visibleIndicators);
  const pad = Math.max((rawMax - rawMin) * 0.08, rawMax * 0.003);
  const priceMin = rawMin - pad;
  const priceMax = rawMax + pad;
  const priceH = priceBottom - top;
  const yPrice = v => priceBottom - (v - priceMin) / Math.max(1e-9, priceMax - priceMin) * priceH;

  // Background panels
  ctx.fillStyle = CHART_THEME.background; ctx.fillRect(0, 0, cssW, cssH);
  ctx.fillStyle = CHART_THEME.panel; ctx.fillRect(left, top, plotW, priceH);
  if (showVolume) ctx.fillStyle = CHART_THEME.panelMuted, ctx.fillRect(left, volumeTop, plotW, volumeBottom - volumeTop);
  if (showMacd) ctx.fillStyle = CHART_THEME.panelMuted, ctx.fillRect(left, macdTop, plotW, macdBottom - macdTop);

  // Price grid and right Y-axis labels
  ctx.strokeStyle = CHART_THEME.grid; ctx.lineWidth = 1;
  ctx.fillStyle = CHART_THEME.muted; ctx.font = '12px Segoe UI'; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
  for (let i = 0; i <= 6; i++) {
    const y = top + (priceH * i / 6);
    const price = priceMax - (priceMax - priceMin) * i / 6;
    ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(left + plotW, y); ctx.stroke();
    ctx.fillText(niceNumber(price), left + plotW + 10, y);
  }
  ctx.strokeStyle = CHART_THEME.border;
  ctx.beginPath(); ctx.moveTo(left, top); ctx.lineTo(left, priceBottom); ctx.lineTo(left + plotW, priceBottom); ctx.lineTo(left + plotW, top); ctx.stroke();

  // Time grid and X-axis date labels
  ctx.textAlign = 'center'; ctx.textBaseline = 'top'; ctx.fillStyle = CHART_THEME.muted;
  const labelCount = Math.min(7, visible.length);
  for (let i = 0; i < labelCount; i++) {
    const idx = Math.round(i * (visible.length - 1) / Math.max(1, labelCount - 1));
    const x = xFor(idx);
    ctx.strokeStyle = CHART_THEME.gridSoft; ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, interactionBottom); ctx.stroke();
    const d = String(visible[idx].date || '').includes(' ') ? String(visible[idx].date).split(' ').slice(-1)[0] : String(visible[idx].date).slice(5);
    ctx.fillText(d, x, interactionBottom + 8);
  }

  if (indicators.boll !== false) {
    // Bollinger band fill + lines
    ctx.beginPath();
    let started = false;
    for (let vi = 0; vi < visible.length; vi++) {
      const idx = visibleStart + vi;
      if (bb.upper[idx] === null) continue;
      const x = xFor(vi), y = yPrice(bb.upper[idx]);
      if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
    }
    for (let vi = visible.length - 1; vi >= 0; vi--) {
      const idx = visibleStart + vi;
      if (bb.lower[idx] === null) continue;
      ctx.lineTo(xFor(vi), yPrice(bb.lower[idx]));
    }
    ctx.closePath(); ctx.fillStyle = CHART_THEME.bollFill; ctx.fill();
    drawLine(ctx, bb.upper, visibleStart, xFor, yPrice, CHART_THEME.boll, 1, visibleEnd);
    drawLine(ctx, bb.lower, visibleStart, xFor, yPrice, CHART_THEME.boll, 1, visibleEnd);
  }

  // The same verified OHLC series can be viewed without changing the underlying source values.
  if (chartType === 'candles' || chartType === 'heikin') {
    displayedVisible.forEach((p, i) => {
      const x = xFor(i);
      const up = p.close >= p.open;
      const color = up ? CHART_THEME.up : CHART_THEME.down;
      ctx.strokeStyle = color; ctx.fillStyle = color;
      ctx.lineWidth = 1.2;
      ctx.beginPath(); ctx.moveTo(x, yPrice(p.high)); ctx.lineTo(x, yPrice(p.low)); ctx.stroke();
      const yOpen = yPrice(p.open), yClose = yPrice(p.close);
      const bodyTop = Math.min(yOpen, yClose), bodyH = Math.max(2, Math.abs(yOpen - yClose));
      ctx.fillRect(x - candleW / 2, bodyTop, candleW, bodyH);
    });
  } else if (chartType === 'bars') {
    displayedVisible.forEach((p, i) => {
      const x = xFor(i);
      const color = p.close >= p.open ? CHART_THEME.up : CHART_THEME.down;
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.4;
      ctx.beginPath();
      ctx.moveTo(x, yPrice(p.high));
      ctx.lineTo(x, yPrice(p.low));
      ctx.moveTo(x - candleW / 2, yPrice(p.open));
      ctx.lineTo(x, yPrice(p.open));
      ctx.moveTo(x, yPrice(p.close));
      ctx.lineTo(x + candleW / 2, yPrice(p.close));
      ctx.stroke();
    });
  } else {
    const lineColor = CHART_THEME.ma10;
    ctx.beginPath();
    visible.forEach((p, i) => {
      const x = xFor(i), y = yPrice(p.close);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    if (chartType === 'area') {
      ctx.lineTo(xFor(visible.length - 1), priceBottom);
      ctx.lineTo(xFor(0), priceBottom);
      ctx.closePath();
      const fill = ctx.createLinearGradient(0, top, 0, priceBottom);
      fill.addColorStop(0, `${lineColor}66`);
      fill.addColorStop(1, `${lineColor}08`);
      ctx.fillStyle = fill;
      ctx.fill();
      ctx.beginPath();
      visible.forEach((p, i) => {
        const x = xFor(i), y = yPrice(p.close);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
    }
    ctx.strokeStyle = lineColor;
    ctx.lineWidth = 2.4;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.stroke();
  }

  // Moving averages
  if (indicators.ma5 !== false) drawLine(ctx, mas.ma5, visibleStart, xFor, yPrice, CHART_THEME.ma5, 1.7, visibleEnd);
  if (indicators.ma10 !== false) drawLine(ctx, mas.ma10, visibleStart, xFor, yPrice, CHART_THEME.ma10, 1.5, visibleEnd);
  if (indicators.ma20 !== false) drawLine(ctx, mas.ma20, visibleStart, xFor, yPrice, CHART_THEME.ma20, 1.5, visibleEnd);
  if (indicators.ma60 !== false) drawLine(ctx, mas.ma60, visibleStart, xFor, yPrice, CHART_THEME.ma60, 1.4, visibleEnd);
  if (indicators.boll !== false) drawLine(ctx, bb.mid, visibleStart, xFor, yPrice, CHART_THEME.boll, 1, visibleEnd);

  if (showVolume) {
    const maxVol = Math.max(...visible.map(p => p.volume), 1);
    const vH = volumeBottom - volumeTop;
    ctx.strokeStyle = CHART_THEME.grid; ctx.beginPath(); ctx.moveTo(left, volumeBottom); ctx.lineTo(left + plotW, volumeBottom); ctx.stroke();
    ctx.textAlign = 'left'; ctx.fillStyle = CHART_THEME.muted; ctx.fillText('成交量', left, volumeTop - 18);
    ctx.fillText(maxVol.toLocaleString(), left + plotW + 10, volumeTop + 8);
    visible.forEach((p, i) => { const x = xFor(i); ctx.fillStyle = p.close >= p.open ? CHART_THEME.upSoft : CHART_THEME.downSoft; const h = Math.max(1, p.volume / maxVol * (vH - 8)); ctx.fillRect(x - candleW / 2, volumeBottom - h, candleW, h); });
  }
  if (showMacd) {
    const macdVals = [];
    for (let i = visibleStart; i < visibleEnd; i++) macdVals.push(md.line[i], md.signal[i], md.hist[i]);
    const macdMaxAbs = Math.max(...macdVals.map(v => Math.abs(v || 0)), 1e-9);
    const yMacd = v => (macdTop + macdBottom) / 2 - v / macdMaxAbs * ((macdBottom - macdTop) * 0.42);
    ctx.strokeStyle = CHART_THEME.grid; ctx.beginPath(); ctx.moveTo(left, yMacd(0)); ctx.lineTo(left + plotW, yMacd(0)); ctx.stroke();
    ctx.fillStyle = CHART_THEME.muted; ctx.textAlign = 'left'; ctx.fillText('MACD(12,26,9)', left, macdTop - 18);
    visible.forEach((p, vi) => { const idx = visibleStart + vi; const x = xFor(vi), y0 = yMacd(0), yh = yMacd(md.hist[idx]); ctx.fillStyle = md.hist[idx] >= 0 ? CHART_THEME.upSoft : CHART_THEME.downSoft; ctx.fillRect(x - Math.max(1, candleW * .36), Math.min(y0, yh), Math.max(1, candleW * .72), Math.max(1, Math.abs(yh - y0))); });
    drawLine(ctx, md.line, visibleStart, xFor, yMacd, CHART_THEME.macd, 1.3, visibleEnd);
    drawLine(ctx, md.signal, visibleStart, xFor, yMacd, CHART_THEME.signal, 1.3, visibleEnd);
  }

  // Titles, axis names, latest OHLC legend
  const last = visible[visible.length - 1];
  const dataMode = options.intradayOnly
    ? `${options.timeframeMinutes} 分 K · 指定交易日重建`
    : options.realtimeOnly
      ? '歷史日K + 盤中即時最新一根'
      : '歷史日K';
  const chartTypeLabel = {
    line: '收盤折線',
    area: '收盤面積',
    bars: '美國線 OHLC',
    heikin: 'Heikin-Ashi 平均 K 線',
    candles: '蠟燭 K 線',
  }[chartType] || '蠟燭 K 線';
  const chartMode = `${dataMode} · ${chartTypeLabel}`;
  const latestTimeLabel = String(last.date || '');
  const baseFreshness = options.intradayOnly
    ? `${options.intradayMeta?.trading_date || ''}；由 ${options.intradayMeta?.source_one_minute_count ?? points.length} 根持久化 1 分 K 重建；${options.intradayMeta?.reconstruction_status || 'unknown'}`
    : options.realtimeOnly
      ? describeRealtimeFreshness(latestTimeLabel)
      : `最新 K 線時間 ${latestTimeLabel}`;
  const historyCount = Number(options.historyMeta?.point_count ?? points.length);
  const limitedHistory = historyCount < 60 ? `；歷史 ${historyCount} 根，僅顯示資料量足夠的指標` : '';
  const freshnessDetail = `${baseFreshness}${limitedHistory}`;
  const chartDataStatus = $('chartDataStatus');
  if (chartDataStatus) chartDataStatus.textContent = freshnessDetail;
  ctx.textAlign = 'left'; ctx.textBaseline = 'alphabetic';
  ctx.font = '700 15px Segoe UI'; ctx.fillStyle = CHART_THEME.text;
  ctx.fillText(`K線圖模式：${chartMode}`, left, 22);
  ctx.font = '12px Segoe UI'; ctx.fillStyle = CHART_THEME.muted;
  ctx.fillText(`最新K：${latestTimeLabel}  O ${niceNumber(last.open)}  H ${niceNumber(last.high)}  L ${niceNumber(last.low)}  C ${niceNumber(last.close)}`, left, 42);
  ctx.font = '12px Segoe UI'; ctx.fillStyle = CHART_THEME.muted;
  ctx.fillText(freshnessDetail, left, 60);
  const legends = [
    ['MA5', CHART_THEME.ma5, 5, 'ma5'], ['MA10', CHART_THEME.ma10, 10, 'ma10'], ['MA20', CHART_THEME.ma20, 20, 'ma20'],
    ['MA60', CHART_THEME.ma60, 60, 'ma60'], ['BOLL(20,2)', CHART_THEME.boll, 20, 'boll'],
  ].filter(([, , minimum, key]) => indicators[key] !== false && points.length >= minimum);
  let lx = left;
  const legendY = 82;
  legends.forEach(([name, color]) => { ctx.fillStyle = color; ctx.fillRect(lx, legendY - 4, 18, 3); ctx.fillStyle = CHART_THEME.muted; ctx.fillText(name, lx + 24, legendY); lx += 92; });
  ctx.fillStyle = CHART_THEME.muted; ctx.textAlign = 'right';
  ctx.fillText('價格', left + plotW + 64, top - 10);
  ctx.fillText('時間', left + plotW, interactionBottom + 30);
  syncCanvasToLiquidTexture(canvas, 'candlestick-chart');
  window.StockChartInteractions?.setRenderModel({
    points,
    visibleStart,
    visibleEnd,
    options,
    bounds: {
      cssW,
      cssH,
      left,
      right,
      top,
      priceBottom,
      volumeTop,
      volumeBottom,
      macdTop,
      macdBottom: interactionBottom,
      plotW,
      candleSlot,
      priceMin,
      priceMax,
    },
  });
  document.dispatchEvent(new CustomEvent('stock-chart-data-updated'));
}

function isMarketIndexSymbol(symbol) {
  return String(symbol || '').startsWith('^') || String(symbol || '').toUpperCase() === 'TX=F';
}

async function loadMarketIndexSummary(symbol, options = {}) {
  const requestedSymbol = String(symbol || '^TWII').trim().toUpperCase();
  const labels = {
    '^TWII': '加權指數',
    '^TWOII': '櫃買指數',
    'TX=F': '台指期',
    '^DJI': '道瓊指數',
    '^IXIC': 'NASDAQ',
    '^GSPC': 'S&P 500',
    '^SOX': '費城半導體',
  };
  const name = labels[requestedSymbol] || requestedSymbol;
  const key = symbolKey(requestedSymbol);
  state.symbol = requestedSymbol;
  state.currentEntity = {
    symbol: requestedSymbol,
    name,
    entity_type: 'index',
    market: requestedSymbol === '^TWII' || requestedSymbol === '^TWOII' ? 'taiwan' : 'global',
    exchange: 'INDEX',
  };
  state.detailSummary = null;
  state.activeRealtimeSymbolKey = key;
  state.realtimeQuote = null;
  state.realtimeSource?.close?.();
  state.realtimeSource = null;
  state.historySeries = { ...state.historySeries, [key]: state.historySeries[key] || [] };
  state.historyMeta = { ...state.historyMeta, [key]: state.historyMeta[key] || null };
  syncWorkspaceControls();

  const payload = await StockWorkspaceCache.singleFlight(
    `chart:index:${requestedSymbol}`,
    () => api(`/api/instruments/${encodeURIComponent(requestedSymbol)}/chart`),
    { ttlMs: 60_000 },
  );
  const points = Array.isArray(payload?.points) ? payload.points : [];
  if (points.length) {
    state.historySeries[key] = points;
    state.historyMeta[key] = payload;
  }
  renderCurrentChart();
  $('chartTitle').textContent = `${name} ${requestedSymbol} K線圖 / 指數歷史日K`;
  const latest = points.at(-1);
  const previous = points.at(-2) || latest;
  const change = latest && previous?.close
    ? ((Number(latest.close) - Number(previous.close)) / Number(previous.close)) * 100
    : null;
  $('summaryCards').innerHTML = points.length
    ? `
      <div class="mini-card"><span>市場基準</span><strong>${escapeHtml(name)}<br/>${escapeHtml(requestedSymbol)}</strong></div>
      <div class="mini-card"><span>最新可用指數</span><strong>${escapeHtml(niceNumber(Number(latest.close)))}</strong></div>
      <div class="mini-card"><span>日變動</span><strong class="${twColor(change || 0)}">${change == null ? '-' : `${change >= 0 ? '+' : ''}${change.toFixed(2)}%`}</strong></div>
      <div class="mini-card"><span>資料時間</span><strong>${escapeHtml(latest.date || '-')}</strong></div>
      <div class="mini-card"><span>資料來源</span><strong>${escapeHtml(payload.source || '指數歷史來源')}</strong></div>`
    : `<div class="mini-card"><span>指數圖表</span><strong>目前沒有可用歷史資料</strong></div>`;
  renderRealtimePanel(`${name} 為市場指數；不會送出個股即時五檔、財報、籌碼或估值請求。`);
  if ($('dailyHistorySymbol')) $('dailyHistorySymbol').value = requestedSymbol;
  if (options.navigate !== false) setView(window.__activeWorkspace === 'instrument' ? 'instrument' : 'home');
  return payload;
}

async function loadSummary(symbol = state.symbol, options = {}) {
  const requestedSymbol = String(symbol || '').trim().toUpperCase();
  if (!requestedSymbol) {
    state.symbol = null;
    state.currentEntity = null;
    state.detailSummary = null;
    const chartTitle = $('chartTitle');
    if (chartTitle) chartTitle.textContent = '尚未選擇股票';
    return null;
  }
  if (isMarketIndexSymbol(requestedSymbol)) {
    return loadMarketIndexSummary(requestedSymbol, options);
  }
  symbol = requestedSymbol;
  state.symbol = symbol;
  syncWorkspaceControls();
  const key = symbolKey(symbol);
  state.activeRealtimeSymbolKey = key;
  debugReport('A', 'features/market-chart.js:loadSummary', 'load-summary-start', {
    inputSymbol: symbol,
    key,
    existingQuoteSymbol: state.realtimeQuote?.symbol || null,
    existingCandleCount: state.realtimeCandles[key]?.length || 0,
  });
  state.realtimeSeries = { ...state.realtimeSeries, [key]: state.realtimeSeries[key] || [] };
  state.realtimeCandles = { ...state.realtimeCandles, [key]: state.realtimeCandles[key] || [] };
  state.historySeries = { ...state.historySeries, [key]: state.historySeries[key] || [] };
  state.historyMeta = { ...state.historyMeta, [key]: state.historyMeta[key] || null };
  state.currentEntity = { symbol, name: symbol };
  const [summaryResult, historyResult] = await Promise.allSettled([
    api(uiDataApi(`/market/${encodeURIComponent(symbol)}/summary`)),
    api(dailyHistoryQueryUrl(symbol, { refresh: true, limit: 5000 })),
  ]);
  if (summaryResult.status === 'fulfilled') {
    const summary = summaryResult.value;
    const entity = summary.entity && typeof summary.entity === 'object' ? summary.entity : {};
    const entitySymbol = String(entity.symbol || symbol).trim().toUpperCase();
    const entityName = String(entity.name || entitySymbol).trim();
    state.currentEntity = { ...entity, symbol: entitySymbol, name: entityName };
    state.detailSummary = summary;
    debugReport('E', 'features/market-chart.js:loadSummary', 'load-summary-success', {
      inputSymbol: symbol,
      entitySymbol: summary.entity?.symbol || null,
      entityName: summary.entity?.name || null,
    });
  } else {
    state.detailSummary = null;
    debugReport('E', 'features/market-chart.js:loadSummary', 'load-summary-error', {
      inputSymbol: symbol,
      error: summaryResult.reason?.message || String(summaryResult.reason || ''),
    });
    console.warn('summary context failed', summaryResult.reason);
  }
  if (historyResult.status === 'fulfilled') {
    state.historySeries[key] = historyResult.value.points || [];
    state.historyMeta[key] = historyResult.value;
    if (state.intradayTimeframe === 'D') renderDailyHistoryStatus(historyResult.value);
  } else {
    state.historySeries[key] = [];
    state.historyMeta[key] = null;
  }
  const ent = state.currentEntity;
  syncWorkspaceControls();
  const existingQuoteKey = symbolKey(state.realtimeQuote?.symbol);
  if (existingQuoteKey !== key) state.realtimeQuote = null;
  renderCurrentChart();
  const entitySymbol = String(ent?.symbol || symbol).trim() || symbol;
  const entityName = String(ent?.name || '').trim();
  const chartName = entityName && entityName !== entitySymbol
    ? `${entityName} ${entitySymbol}`
    : entitySymbol;
  if (state.intradayTimeframe === 'D') {
    $('chartTitle').textContent = `${chartName} K線圖 / 歷史日K + 盤中即時`;
  }
  const symbolSelect = $('symbolSelect');
  if (symbolSelect) {
    const nextSymbol = ent.symbol || symbol;
    // A task can select a valid security before the full symbol catalogue is
    // loaded. Keep the control truthful in that interval instead of showing
    // the placeholder while the chart is already rendering that security.
    if (![...symbolSelect.options].some(option => option.value === nextSymbol)) {
      const option = document.createElement('option');
      option.value = nextSymbol;
      option.textContent = `${nextSymbol} · ${entityName || nextSymbol}`;
      option.dataset.contextSelection = 'true';
      const placeholder = [...symbolSelect.options].find(option => !option.value);
      if (placeholder) placeholder.after(option);
      else symbolSelect.append(option);
    }
    symbolSelect.value = nextSymbol;
  }
  if ($('dailyHistorySymbol')) $('dailyHistorySymbol').value = entitySymbol;
  if (state.intradayTimeframe !== 'D') {
    await loadIntradayCandles({ refresh: true });
  }
  if (state.realtimeQuote) updateRealtimeSummary(state.realtimeQuote);
  else if (state.detailSummary) {
    const detail = state.detailSummary;
    const point = detail.latest_price || {};
    const change = Number(detail.change_percent || 0);
    $('summaryCards').innerHTML = `
      <div class="mini-card"><span>最新可用價格</span><strong>${escapeHtml(niceNumber(point.close))}</strong></div>
      <div class="mini-card"><span>漲跌幅</span><strong class="${change >= 0 ? 'up' : 'down'}">${change >= 0 ? '+' : ''}${change.toFixed(2)}%</strong></div>
      <div class="mini-card"><span>資料時間</span><strong>${escapeHtml(point.date || detail.data_timestamp || '-')}</strong></div>
      <div class="mini-card"><span>資料來源</span><strong>${escapeHtml(detail.data_source || '官方最新可用資料')}</strong></div>`;
  } else $('summaryCards').innerHTML = `
    <div class="mini-card"><span>報價狀態</span><strong>暫時無法取得</strong></div>
    <div class="mini-card"><span>處理方式</span><strong>請稍後重新整理，圖表仍顯示已取得的歷史資料</strong></div>`;
  loadDashboardSymbolDetails(ent.symbol || symbol).catch(err => {
    $('fundamentalsBox').innerHTML = renderEmptyBlock('Phase 1 資料載入失敗', err.message);
  });
  loadNotificationCenter(ent.symbol || symbol).catch(() => {});
  loadLiquidityAssessment(ent.symbol || symbol).catch(() => {});
  if (['portfolio', 'research', 'instrument'].includes(currentViewId())) {
    loadViewData(currentViewId()).catch(() => {});
  }
  startRealtime(ent.symbol || symbol);
  if (options.navigate !== false) setView('instrument');
}

$('assessLiquidity')?.addEventListener('click', () => {
  const symbol = String(state.currentEntity?.symbol || state.symbol || '').trim();
  loadLiquidityAssessment(symbol).catch(() => {});
});
$('scanTradingAnomalies')?.addEventListener('click', () => {
  const symbol = String(state.currentEntity?.symbol || state.symbol || '').trim();
  scanTradingAnomalies(symbol).catch(() => {});
});
$('tradingAnomalyList')?.addEventListener('click', (event) => {
  const button = event.target.closest('[data-anomaly-event]');
  if (!button) return;
  button.disabled = true;
  trackTradingAnomaly(button.dataset.anomalyEvent, button.dataset.anomalyStatus)
    .catch(error => {
      const status = $('tradingAnomalyStatus');
      if (status) status.textContent = `追蹤狀態更新失敗：${error.message}`;
    })
    .finally(() => { button.disabled = false; });
});

$('chartFocusMode')?.addEventListener('click', () => {
  const enabled = document.documentElement.dataset.chartFocus !== 'true';
  document.documentElement.dataset.chartFocus = String(enabled);
  document.dispatchEvent(new CustomEvent('stock-ai:chart-focus-change', { detail: { enabled } }));
  $('chartFocusMode').textContent = enabled ? '離開聚焦' : '聚焦圖表';
  $('priceChartOverlay')?.focus({ preventScroll: true });
});
document.addEventListener('keydown', event => {
  if (event.key !== 'Escape' || document.documentElement.dataset.chartFocus !== 'true') return;
  document.documentElement.dataset.chartFocus = 'false';
  document.dispatchEvent(new CustomEvent('stock-ai:chart-focus-change', { detail: { enabled: false } }));
  if ($('chartFocusMode')) $('chartFocusMode').textContent = '聚焦圖表';
});
