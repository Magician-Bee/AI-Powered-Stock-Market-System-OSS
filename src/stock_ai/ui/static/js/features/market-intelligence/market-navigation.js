(() => {
  const listLabels = {
    buy_now: '現在可買',
    sell_reduce: '持倉可賣／減碼',
    watch: '持續觀察',
    future_buy: '未來可買',
    avoid_now: '暫不介入',
    watchlist: '自選清單',
  };
  // This is a whole-market discovery surface.  Existing-position actions are
  // useful, but must not hide the populated WATCH universe when no new BUY
  // candidate has cleared the quality gate.
  const decisionListPriority = ['buy_now', 'watch', 'future_buy', 'avoid_now', 'sell_reduce'];
  const PAGE_SIZE = 40;
  let bootstrapPayload = null;
  let activeList = 'buy_now';
  let visibleLimit = PAGE_SIZE;

  function candidateRow(item) {
    if (item.kind === 'industry') {
      const change = Number(item.average_change_percent || 0);
      return `
        <button class="market-navigation-row industry" type="button" role="listitem" data-workspace-action="select" data-symbol="${escapeHtml(item.leader_symbol || '')}">
          <span><strong title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</strong><small>${Number(item.member_count || 0)} 檔有效樣本</small></span>
          <span><strong class="${twColor(change)}">${change >= 0 ? '+' : ''}${change.toFixed(2)}%</strong><small>產業平均</small></span>
          <span>代表股 ${escapeHtml(item.leader_symbol || '-')}</span>
          <small>官方全市場快照聚合</small>
        </button>`;
    }
    const change = item.change_percent == null ? '-' : `${Number(item.change_percent) >= 0 ? '+' : ''}${Number(item.change_percent).toFixed(2)}%`;
    const volume = item.volume == null ? '成交量 -' : `量 ${Number(item.volume).toLocaleString()}`;
    const freshness = item.data_quality?.data_as_of || item.updated_at || item.added_at || '';
    const classification = item.decision_label || item.alert_type || (item.kind === 'index' ? '市場基準' : '');
    return `
      <button class="market-navigation-row" type="button" role="listitem" data-workspace-action="select" data-symbol="${escapeHtml(item.symbol)}">
        <span><strong title="${escapeHtml(item.symbol)}">${escapeHtml(item.symbol)}</strong><small title="${escapeHtml(item.name || '')}">${escapeHtml(item.name || '')}</small></span>
        <span><strong>${item.latest_price == null ? '-' : escapeHtml(niceNumber(item.latest_price))}</strong><small>${escapeHtml(change)}</small></span>
        <span class="market-navigation-meta"><strong title="${escapeHtml(classification)}">${escapeHtml(classification)}</strong><small title="${escapeHtml(volume)}">${escapeHtml(volume)}</small></span>
        <small>${item.rank ? `排名 ${Number(item.rank)}${item.rank_change == null ? '' : ` · ${Number(item.rank_change) >= 0 ? '↑' : '↓'}${Math.abs(Number(item.rank_change))}`}` : '已保存'}${freshness ? ` · ${escapeHtml(String(freshness).slice(0, 16))}` : ''}</small>
      </button>`;
  }

  function message(title, detail) {
    return `<div class="market-navigation-empty"><strong>${escapeHtml(title)}</strong><p>${escapeHtml(detail)}</p></div>`;
  }

  function allCompactDetails() {
    return Object.values(bootstrapPayload?.market_snapshot?.candidate_details || {});
  }

  function itemsFor(name) {
    const snapshot = bootstrapPayload?.market_snapshot;
    const navigation = bootstrapPayload?.market_navigation || {};
    const details = allCompactDetails();
    if (!snapshot) return [];
    const decisions = bootstrapPayload?.market_decisions || {};
    if (['buy_now', 'sell_reduce', 'watch', 'future_buy', 'avoid_now'].includes(name)) return decisions[name] || [];
    if (name === 'watchlist') return navigation.watchlist || [];
    if (name === 'positions') return Object.values(bootstrapPayload?.portfolio_actions || {}).flat();
    if (name === 'volume') return navigation.volume || [];
    if (name === 'movers') return navigation.movers || [];
    if (name === 'indices') return navigation.indices || [];
    if (name === 'industries') return navigation.industries || [];
    if (name === 'institutional') return navigation.institutional || [];
    if (name === 'anomalies') return navigation.anomalies || [];
    if (name === 'alerts') return navigation.alerts || [];
    return [];
  }

  function render(name = activeList) {
    const listChanged = name !== activeList;
    activeList = name;
    if (listChanged) visibleLimit = PAGE_SIZE;
    const target = document.getElementById('marketNavigationList');
    if (!target) return;
    document.querySelectorAll('[data-market-list]').forEach(button => button.classList.toggle('is-active', button.dataset.marketList === name));
    document.getElementById('marketNavigationTitle').textContent = listLabels[name] || name;
    const items = itemsFor(name);
    if (items.length) {
      const visibleItems = items.slice(0, visibleLimit);
      target.innerHTML = visibleItems.map(candidateRow).join('');
      updateProgress(visibleItems.length, items.length);
      return;
    }
    updateProgress(0, 0);
    const emptyStates = {
      watchlist: ['尚未載入自選股', '可從任一決策卡直接加入自選；加入後不需離開首頁。'],
      institutional: ['法人排行目前不可用', bootstrapPayload?.market_navigation?.institutional_status?.message || '不會以規則分數冒充法人買賣超。'],
      anomalies: ['本輪沒有已載入異常事件', '官方異常事件更新後會在同一股票 Context 顯示。'],
      alerts: ['目前沒有監控提醒', '可從決策卡的「設定提醒」建立價格或觸發條件。'],
      industries: ['產業排行資料不足', '完整產業強弱仍可在市場 Regime 摘要查看。'],
    };
    const copy = emptyStates[name] || ['目前沒有項目', '資料更新後會保留舊內容並無縫替換。'];
    target.innerHTML = message(copy[0], copy[1]);
  }

  function updateProgress(visibleCount, totalCount) {
    const progress = document.getElementById('marketNavigationProgress');
    const count = document.getElementById('marketNavigationCount');
    const more = document.getElementById('marketNavigationMore');
    if (!progress || !count || !more) return;
    const hasItems = totalCount > 0;
    progress.hidden = !hasItems;
    count.textContent = hasItems ? `顯示 ${visibleCount.toLocaleString()} / ${totalCount.toLocaleString()} 檔` : '';
    more.hidden = visibleCount >= totalCount;
    more.setAttribute('aria-label', `再顯示最多 ${Math.min(PAGE_SIZE, Math.max(0, totalCount - visibleCount))} 檔`);
  }

  function preferredListAfterHydration() {
    // Do not leave the primary market workspace blank merely because the
    // stricter data-quality gate correctly withheld every BUY_NOW candidate.
    // Preserve an existing non-empty selection; otherwise show the strongest
    // remaining decision surface (for example WATCH / 待交叉驗證).
    if (itemsFor(activeList).length) return activeList;
    return decisionListPriority.find(name => itemsFor(name).length) || activeList;
  }

  function hydrate(payload) {
    bootstrapPayload = payload;
    visibleLimit = PAGE_SIZE;
    render(preferredListAfterHydration());
  }

  function init() {
    document.querySelectorAll('[data-market-list]').forEach(button => {
      button.addEventListener('click', () => render(button.dataset.marketList));
    });
    document.getElementById('marketNavigationMore')?.addEventListener('click', () => {
      visibleLimit += PAGE_SIZE;
      render(activeList);
    });
  }

  window.MarketWorkspaceNavigation = { init, hydrate, render };
})();
