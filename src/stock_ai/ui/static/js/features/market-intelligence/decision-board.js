(() => {
  const byId = id => document.getElementById(id);

  function qualityLabel(item) {
    const status = item?.data_quality?.status || 'insufficient';
    return {
      ready: '資料完整',
      partial: '部分資料',
      insufficient: '資料不足',
      conflict: '來源衝突',
    }[status] || status;
  }

  function sortItems(items, mode) {
    return [...items].sort((a, b) => {
      if (mode === 'trigger') return Number(a.distance_to_trigger_percent ?? 9999) - Number(b.distance_to_trigger_percent ?? 9999);
      if (mode === 'risk') return ({ high: 0, medium: 1, low: 2, unknown: 3 }[a.risk_level] ?? 3) - ({ high: 0, medium: 1, low: 2, unknown: 3 }[b.risk_level] ?? 3);
      if (mode === 'quality') return Number(b.data_quality?.score || 0) - Number(a.data_quality?.score || 0);
      if (mode === 'volume') return Number(b.trade_value || 0) - Number(a.trade_value || 0);
      return Number(a.rank || 999999) - Number(b.rank || 999999);
    });
  }

  function trustBadges(item) {
    const receipt = item?.data_quality_receipt;
    const badges = [
      `<span class="decision-trust rule">${item.model_status === 'succeeded' ? 'AI VERIFIED' : item.model_status === 'queued' ? 'AI ANALYZING' : 'SYSTEM SCANNED'}</span>`,
      item.host_risk_status === 'passed'
        ? '<span class="decision-trust verified">HOST VERIFIED</span>'
        : '<span class="decision-trust blocked">RISK BLOCKED</span>',
      `<span class="decision-trust quality">${escapeHtml(qualityLabel(item).toUpperCase())}</span>`,
    ];
    if (receipt?.receipt_id) {
      badges.push(`<span class="decision-trust quality">DQ RECEIPT ${escapeHtml(String(receipt.certification_status || 'partial').toUpperCase())}</span>`);
    }
    if (item.model_status === 'succeeded' && item.model_receipt?.status === 'succeeded') badges.push('<span class="decision-trust model">MODEL RECEIPT</span>');
    return badges.join('');
  }

  function factorEvidence(item) {
    const labels = {
      fundamental_score: '基本面',
      valuation_score: '估值',
      chip_score: '籌碼',
      event_score: '事件',
      relative_strength_score: '相對強弱',
    };
    const available = Object.entries(labels).map(([key, label]) => {
      const factor = item?.factor_scores?.[key];
      if (factor?.value === null || factor?.value === undefined) return null;
      const status = factor.historical_pit_eligible === true ? 'PIT' : factor.status === 'partial' ? '目前資料' : String(factor.status || '已驗證');
      const source = factor.source ? ` · ${factor.source}` : '';
      return `<span class="decision-factor ${escapeHtml(String(factor.status || 'unavailable'))}" title="${escapeHtml(`${label}：${source || '來源未提供'}${factor.reason ? `；${factor.reason}` : ''}`)}">${escapeHtml(label)} ${escapeHtml(niceNumber(Number(factor.value)))} <small>${escapeHtml(status)}</small></span>`;
    }).filter(Boolean);
    return available.length
      ? `<div class="decision-card-factors" aria-label="因子資料來源">${available.join('')}</div>`
      : '<div class="decision-card-factors empty">因子資料：目前僅使用可驗證行情；其他資料尚未可用。</div>';
  }

  function card(item, { near = false } = {}) {
    const change = item.change_percent == null ? '-' : `${Number(item.change_percent) >= 0 ? '+' : ''}${Number(item.change_percent).toFixed(2)}%`;
    const triggerDistance = item.distance_to_trigger_percent == null ? '-' : `${Number(item.distance_to_trigger_percent).toFixed(2)}%`;
    return `
      <article class="market-decision-card ${near ? 'near-condition' : ''}" data-decision-symbol="${escapeHtml(item.symbol)}">
        <button class="decision-card-main" type="button" data-workspace-action="select" data-symbol="${escapeHtml(item.symbol)}">
          <span class="decision-card-rank">#${Number(item.rank || 0)}</span>
          <span class="decision-card-symbol"><strong>${escapeHtml(item.name || item.symbol)}</strong><small>${escapeHtml(item.symbol)}</small></span>
          <span class="decision-card-price"><strong>${item.latest_price == null ? '-' : escapeHtml(niceNumber(item.latest_price))}</strong><small>${escapeHtml(change)}</small></span>
          <span class="decision-card-label">${escapeHtml(near ? '接近條件' : item.decision_label)}</span>
        </button>
        <div class="decision-card-trust">${trustBadges(item)}</div>
        ${factorEvidence(item)}
        <dl class="decision-card-facts">
          <div><dt>目前原因</dt><dd>${escapeHtml(item.primary_reason)}</dd></div>
          <div><dt>下一觸發</dt><dd>${escapeHtml(item.trigger)}${item.trigger_price == null ? '' : ` · ${escapeHtml(niceNumber(item.trigger_price))}`}</dd></div>
          <div><dt>觸發事件</dt><dd>${escapeHtml(item.trigger_event || '官方價格、成交量或事件更新')}</dd></div>
          <div><dt>進出場區間</dt><dd>${item.trigger_price == null ? '-' : escapeHtml(niceNumber(item.trigger_price))} ～ ${item.invalidation_price == null ? '-' : escapeHtml(niceNumber(item.invalidation_price))}</dd></div>
          <div><dt>最強反方</dt><dd>${escapeHtml(item.contrary_reasons?.[0] || '目前沒有額外反方證據')}</dd></div>
          <div><dt>失效條件</dt><dd>${escapeHtml(item.invalidation)}</dd></div>
          <div><dt>距離觸發</dt><dd>${escapeHtml(triggerDistance)}</dd></div>
          <div><dt>下次重估</dt><dd>${escapeHtml(item.next_review || '下一個可驗證資料事件')}</dd></div>
          <div><dt>AI 狀態</dt><dd>${escapeHtml(item.model_status === 'succeeded' && item.model_receipt?.status === 'succeeded' ? 'AI 已驗證' : '僅系統掃描')}</dd></div>
        </dl>
        <div class="decision-card-actions" aria-label="${escapeHtml(item.symbol)} 操作">
          <button type="button" data-workspace-action="select" data-symbol="${escapeHtml(item.symbol)}">開啟圖表</button>
          <button type="button" data-workspace-action="watch" data-symbol="${escapeHtml(item.symbol)}">加入自選</button>
          <button type="button" data-workspace-action="alert" data-symbol="${escapeHtml(item.symbol)}">設定提醒</button>
          <button type="button" data-workspace-action="agent" data-symbol="${escapeHtml(item.symbol)}">詢問 Agent</button>
          <details>
            <summary>更多</summary>
            <div>
              <button type="button" data-workspace-action="compare" data-symbol="${escapeHtml(item.symbol)}">加入比較</button>
              <button type="button" data-workspace-action="evidence" data-symbol="${escapeHtml(item.symbol)}">查看證據</button>
              <button type="button" data-workspace-action="paper" data-symbol="${escapeHtml(item.symbol)}">模擬交易</button>
            </div>
          </details>
        </div>
      </article>`;
  }

  function empty(title, detail) {
    return `<div class="market-decision-empty"><strong>${escapeHtml(title)}</strong><p>${escapeHtml(detail)}</p></div>`;
  }

  function detailsFor(snapshot, symbols) {
    return (symbols || []).map(symbol => snapshot.candidate_details?.[symbol]).filter(Boolean);
  }

  function render(bootstrap, sortMode = 'rank') {
    const snapshot = bootstrap?.market_snapshot;
    if (!snapshot) {
      ['actionableDecisionList', 'avoidDecisionList', 'watchDecisionList', 'futureDecisionList', 'portfolioDecisionList'].forEach(id => {
        if (byId(id)) byId(id).innerHTML = empty('全市場快照初始化中', '正在讀取官方批次資料；不會用假資料填入決策卡。');
      });
      return;
    }
    const details = snapshot.candidate_details || {};
    const decisionPayload = bootstrap.market_decisions || {};
    const marketRows = (name) => (decisionPayload[name] || []).map(item => details[item.symbol] || item).filter(Boolean);
    const actionable = marketRows('buy_now');
    const avoid = [...marketRows('avoid_now'), ...marketRows('insufficient_data')];
    const watch = marketRows('watch');
    const future = marketRows('future_buy');
    const positionSymbols = Object.values(snapshot.portfolio_actions || {}).flat();
    const positions = positionSymbols.map(symbol => details[symbol]).filter(Boolean);
    const classification = snapshot.classification_counts || {};
    byId('actionableCount').textContent = String(classification.BUY_NOW ?? actionable.length);
    byId('avoidCount').textContent = String((classification.AVOID_NOW || 0) + (classification.INSUFFICIENT_DATA || 0));
    byId('watchCount').textContent = String(classification.WATCH ?? watch.length);
    byId('futureBuyCount').textContent = String(classification.FUTURE_BUY ?? future.length);
    byId('portfolioActionCount').textContent = String(positions.length);
    byId('actionableDecisionList').innerHTML = actionable.length
      ? sortItems(actionable, sortMode).slice(0, 5).map(item => card(item)).join('')
      : empty('目前沒有股票通過立即買進門檻', '最接近條件的股票會留在觀察或未來可買分類。');
    byId('avoidDecisionList').innerHTML = avoid.length
      ? sortItems(avoid, sortMode).slice(0, 5).map(item => card(item)).join('')
      : empty('目前沒有暫不介入標的', '排除分類會列出風險證據與解除條件。');
    byId('watchDecisionList').innerHTML = watch.length
      ? sortItems(watch, sortMode).slice(0, 5).map(item => card(item)).join('')
      : empty('目前沒有持續觀察標的', '資料更新後會重新分類。');
    byId('futureDecisionList').innerHTML = future.length
      ? sortItems(future, sortMode).slice(0, 5).map(item => card(item)).join('')
      : empty('目前沒有未來可買標的', '等待回檔、突破、量能或市場環境條件。');
    byId('portfolioDecisionList').innerHTML = positions.length
      ? sortItems(positions, sortMode).slice(0, 5).map(item => card(item)).join('')
      : empty('目前沒有持倉，因此沒有可賣或減碼標的', '未持有股票不會被放入投資組合行動。');

    const counts = snapshot.ranking_counts || {};
    const model = snapshot.model_overlay || {};
    byId('marketDecisionSummary').textContent = model.status === 'failed'
      ? `AI 深度分析失敗；全市場 ${snapshot.universe.resolved_count} 檔分類仍保留${model.previous_success_generated_at ? `，上一份成功分析 ${new Date(model.previous_success_generated_at).toLocaleString('zh-TW')}` : ''}。`
      : model.status === 'running'
        ? `可投資 ${snapshot.universe.resolved_count} 檔 · 已分類 ${snapshot.universe.classified_count ?? snapshot.universe.resolved_count} · AI 深度分析執行中。`
        : `可投資 ${snapshot.universe.resolved_count} 檔 · 已分類 ${snapshot.universe.classified_count ?? snapshot.universe.resolved_count} · AI 深度分析 ${model.analyzed_symbols?.length || 0} 檔。`;
  }

  function unavailableDetail(title, item, fields) {
    return `
      <div class="workspace-detail-unavailable">
        <strong>${escapeHtml(title)}目前沒有足夠的可引用欄位</strong>
        <p>本輪快照不以規則分數或空值冒充 ${escapeHtml(fields)}。資料品質：${escapeHtml(qualityLabel(item))}；來源：${escapeHtml(item.data_quality?.source || '未提供')}。</p>
        <p>下次重估：${escapeHtml(item.next_review || '官方資料更新時')}</p>
      </div>`;
  }

  function renderDetail(item, tab = 'overview', supplemental = {}) {
    const target = byId('workspaceDecisionDetail');
    if (!target || !item) return;
    const summary = supplemental.summary || {};
    const fundamentals = summary.fundamentals || null;
    const flow = summary.flow || null;
    const events = Array.isArray(summary.events) ? summary.events : [];
    const evidence = (item.evidence || []).map(entry => `
      <li><strong>${escapeHtml(entry.source)}</strong> ${escapeHtml(entry.statement)}<small>${escapeHtml(entry.observed_at || '-')} · ${entry.fallback ? 'FALLBACK' : '原始來源'}</small></li>
    `).join('');
    const qualityReceipt = item.data_quality_receipt || summary.data_quality_receipt;
    const qualityReceiptDetail = qualityReceipt?.receipt_id
      ? `<div><span>品質收據</span><strong>${escapeHtml(qualityReceipt.certification_status || 'partial')}</strong><p>${escapeHtml(qualityReceipt.receipt_id)} · freshness ${escapeHtml(qualityReceipt.freshness_status || 'unknown')} · completeness ${escapeHtml(qualityReceipt.completeness_status || 'unknown')} · confidence ${escapeHtml(qualityReceipt.confidence_status || 'unknown')}</p></div>`
      : `<div><span>品質收據</span><strong>legacy / 未建立</strong><p>此歷史快照建立時尚未有逐筆決策品質收據。</p></div>`;
    const eventRows = events.map(event => `
      <li><strong>${escapeHtml(event.title || event.event_type || '事件')}</strong><p>${escapeHtml(event.summary || '')}</p><small>${escapeHtml(event.event_time || '')} · ${escapeHtml(event.source_url || '來源未提供')}</small></li>
    `).join('');
    const sections = {
      overview: `
        <div class="workspace-detail-grid">
          <div><span>目前行動</span><strong>${escapeHtml(item.decision_label)}</strong><p>${escapeHtml(item.primary_reason)}</p></div>
          <div><span>下一步</span><strong>${escapeHtml(item.trigger)}</strong><p>失效：${escapeHtml(item.invalidation)}</p></div>
          <div><span>資料完整度</span><strong>${escapeHtml(qualityLabel(item))}</strong><p>${escapeHtml((item.data_quality?.missing_fields || []).join('、') || '關鍵行情欄位已具備')}</p></div>
          ${qualityReceiptDetail}
        </div>`,
      technical: `<p>規則分數 ${Number(item.score || 0).toFixed(3)}；日內收盤位置 ${Number(item.range_position || 0) * 100}%；觸發價 ${item.trigger_price == null ? '-' : escapeHtml(niceNumber(item.trigger_price))}；失效價 ${item.invalidation_price == null ? '-' : escapeHtml(niceNumber(item.invalidation_price))}。</p>`,
      chips: flow
        ? `<div class="workspace-detail-grid"><div><span>外資淨買賣</span><strong>${escapeHtml(niceNumber(flow.foreign_net_buy))}</strong></div><div><span>投信淨買賣</span><strong>${escapeHtml(niceNumber(flow.investment_trust_net_buy))}</strong></div><div><span>自營商淨買賣</span><strong>${escapeHtml(niceNumber(flow.dealer_net_buy))}</strong></div><div><span>融資餘額變化</span><strong>${escapeHtml(niceNumber(flow.margin_balance_change))}</strong></div></div>`
        : unavailableDetail('籌碼', item, '法人買賣超與融資券'),
      financials: fundamentals
        ? `<div class="workspace-detail-grid"><div><span>營收年增</span><strong>${escapeHtml(niceNumber(fundamentals.revenue_yoy))}%</strong></div><div><span>EPS</span><strong>${escapeHtml(niceNumber(fundamentals.eps))}</strong></div><div><span>毛利率</span><strong>${escapeHtml(niceNumber(fundamentals.gross_margin))}%</strong></div><div><span>ROE</span><strong>${escapeHtml(niceNumber(fundamentals.roe))}%</strong></div></div>`
        : unavailableDetail('財務', item, '營收、EPS、毛利率與 ROE'),
      valuation: fundamentals
        ? `<div class="workspace-detail-grid"><div><span>本益比</span><strong>${escapeHtml(niceNumber(fundamentals.pe))}</strong></div><div><span>股價淨值比</span><strong>${escapeHtml(niceNumber(fundamentals.pb))}</strong></div><div><span>估值限制</span><strong>官方欄位摘要</strong><p>不把單一倍數寫成目標價或投資結論。</p></div></div>`
        : unavailableDetail('估值', item, 'PE 與 PB'),
      news: events.length
        ? `<ul class="workspace-evidence-list">${eventRows}</ul>`
        : unavailableDetail('新聞', item, '具來源網址與時間的相關新聞'),
      events: events.length
        ? `<ul class="workspace-evidence-list">${eventRows}</ul>`
        : unavailableDetail('事件', item, '可驗證的公司或市場事件'),
      risk: `<p>風險等級：${escapeHtml(item.risk_level)}；Host 風控：${escapeHtml(item.host_risk_status)}；流動性：${escapeHtml(item.liquidity_status)}；Portfolio Fit：${escapeHtml(item.portfolio_fit)}。</p>`,
      evidence: `${qualityReceipt?.receipt_id ? `<p><strong>決策品質收據：</strong>${escapeHtml(qualityReceipt.receipt_id)} · ${escapeHtml(qualityReceipt.certification_status || 'partial')}；來源衝突 ${escapeHtml(qualityReceipt.source_disagreement_status || 'not_observed')}。</p>` : ''}<ul class="workspace-evidence-list">${evidence || '<li>目前沒有可引用證據。</li>'}</ul>`,
    };
    target.innerHTML = sections[tab] || unavailableDetail(tab, item, '該分頁所需資料');
  }

  window.MarketDecisionBoard = { render, renderDetail };
})();
