function explorerEnglishUi() {
  return document.documentElement.lang === 'en';
}

function explorerText(zh, en) {
  return explorerEnglishUi() ? en : zh;
}

async function loadEntities(q='') {
  const table = $('entityTable');
  const status = $('entityPageStatus');
  const hint = $('entitySearchHint');
  const query = String(q || '').trim();
  if (!table) return null;
  if (status) status.textContent = explorerText('搜尋中…', 'Searching…');
  if (hint) hint.textContent = query
    ? explorerText(`正在搜尋「${query}」…`, `Searching for “${query}”…`)
    : explorerText('正在載入可搜尋的證券主檔…', 'Loading the searchable securities master…');
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 12_000);
  try {
    const data = await api(uiDataApi(`/entities/search?q=${encodeURIComponent(query)}`), { signal: controller.signal });
    const items = Array.isArray(data?.items) ? data.items : [];
    const count = Number(data?.count ?? items.length);
    if ($('entityCount')) $('entityCount').textContent = String(count);
    if (status) status.textContent = `${count.toLocaleString()} ${explorerText('筆結果', 'results')}`;
    if (hint) hint.textContent = query
      ? explorerText(`「${query}」找到 ${count.toLocaleString()} 筆；點選列可開啟個股圖表。`, `Found ${count.toLocaleString()} matches for “${query}”. Select a row to open its chart.`)
      : explorerText(`共 ${count.toLocaleString()} 筆可搜尋證券；輸入代號或名稱可縮小範圍。`, `${count.toLocaleString()} securities are available. Enter a symbol or name to narrow the list.`);
    const rows = items.map((entity) => {
      const symbol = String(entity?.symbol || '').trim();
      const name = String(entity?.name || '-');
      const market = [entity?.market, entity?.exchange].filter(Boolean).join('/') || '-';
      return `<div class="row clickable" data-symbol="${escapeHtml(symbol)}">
        <div>${escapeHtml(symbol)}</div><div>${escapeHtml(name)}</div><div>${escapeHtml(market)}</div><div><button data-symbol="${escapeHtml(symbol)}" class="open-detail" type="button">${explorerText('查看K線', 'Open Chart')}</button></div>
      </div>`;
    }).join('');
    table.innerHTML = `<div class="row header"><div>${explorerText('代號', 'Symbol')}</div><div>${explorerText('名稱', 'Name')}</div><div>${explorerText('市場', 'Market')}</div><div>${explorerText('操作', 'Action')}</div></div>` + (rows || `<div class="event"><h4>${explorerText('查無資料', 'No Results')}</h4><p>${explorerText('請改輸入交易所/資料源支援的代號或名稱，例如 2330、0050、AAPL、NVDA。', 'Try a symbol or name supported by the exchange or data source, such as 2330, 0050, AAPL, or NVDA.')}</p></div>`);
    table.querySelectorAll('.open-detail').forEach(button => button.addEventListener('click', (event) => {
      event.stopPropagation();
      const symbol = event.currentTarget.dataset.symbol;
      if (symbol) loadSummary(symbol).catch(error => alert(error.message));
    }));
    table.querySelectorAll('.row.clickable').forEach(row => row.addEventListener('click', (event) => {
      const symbol = event.currentTarget.dataset.symbol;
      if (symbol) loadSummary(symbol).catch(error => alert(error.message));
    }));
    return data;
  } catch (error) {
    const timedOut = error?.name === 'AbortError';
    if (status) status.textContent = explorerText('讀取失敗', 'Unavailable');
    if (hint) hint.textContent = timedOut
      ? explorerText('證券來源回應逾時，請稍後重新搜尋。', 'The securities source timed out. Please try the search again shortly.')
      : explorerText('證券主檔暫時無法讀取，可稍後重新搜尋。', 'The securities master is temporarily unavailable. Try the search again shortly.');
    const detail = timedOut
      ? explorerText('官方證券來源在 12 秒內沒有回應；沒有以假資料取代結果。', 'The official securities source did not respond within 12 seconds; no synthetic result was substituted.')
      : error?.message || explorerText('請稍後再試。', 'Please try again shortly.');
    table.innerHTML = `<div class="event"><h4>${explorerText('證券資料暫時無法載入', 'Securities data is temporarily unavailable')}</h4><p>${escapeHtml(detail)}</p></div>`;
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

function bindEntitySearch() {
  const form = document.getElementById('entitySearchForm');
  if (!form || form.dataset.entitySearchBound === 'true') return;
  form.dataset.entitySearchBound = 'true';
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    loadEntities($('entitySearchInput')?.value).catch(error => console.warn('Entity search is unavailable', error));
  });
  document.getElementById('entitySearchReset')?.addEventListener('click', () => {
    if ($('entitySearchInput')) $('entitySearchInput').value = '';
    loadEntities('').catch(error => console.warn('Entity search is unavailable', error));
  });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bindEntitySearch, { once: true });
else bindEntitySearch();

async function askQuestion() {
  $('answerBox').textContent = explorerText('查詢資料中...', 'Querying data...');
  try {
    const data = await api(uiDataApi('/query'), { method: 'POST', body: JSON.stringify({ question: $('questionInput').value }) });
    $('answerBox').textContent = explorerEnglishUi()
      ? `Route: ${data.route}\n\n${data.answer}\n\nSources:\n- ${data.sources.join('\n- ')}`
      : `【路由】${data.route}\n\n${data.answer}\n\n【Sources】\n- ${data.sources.join('\n- ')}`;
  } catch (err) {
    $('answerBox').textContent = explorerEnglishUi()
      ? `Query failed: ${err.message}\nThe system will not substitute synthetic quote data.`
      : `查詢失敗：${err.message}\n系統不會用補值行情替代。`;
  }
}

function selectedScreenerConditions() {
  const selected = [...document.querySelectorAll('[data-screener-condition]:checked')]
    .map((input) => String(input.dataset.screenerCondition || '').trim())
    .filter(Boolean);
  const custom = String($('screenerCustomConditions')?.value || '')
    .split(/\r?\n/)
    .map((item) => item.trim())
    .filter(Boolean);
  return [...new Set([...selected, ...custom])];
}

function screenerReceiptText(receipt) {
  return (receipt || []).map((item) => {
    const observed = item.observed === null || item.observed === undefined ? '無資料' : item.observed;
    return `${item.condition}（觀測值：${observed}）`;
  });
}

function screenerQualityReceiptText(receipt) {
  if (!receipt) return [];
  const status = String(receipt.certification_status || 'blocked').toUpperCase();
  const freshness = String(receipt.freshness_status || 'unknown');
  const completeness = String(receipt.completeness_status || 'failed');
  const anomaly = String(receipt.anomaly_status || 'failed');
  const confidence = String(receipt.confidence_status || 'failed');
  return [`資料品質收據：${status} · 新鮮度 ${freshness} · 完整性 ${completeness} · 異常 ${anomaly} · 信心 ${confidence}`];
}

function screenerFieldReceiptText(receipt) {
  const entries = Object.entries(receipt || {});
  if (!entries.length) return [];
  return entries.map(([field, detail]) => {
    const status = String(detail?.status || 'unavailable');
    const source = Array.isArray(detail?.source) ? detail.source.join(', ') : detail?.source;
    const asOf = detail?.data_as_of;
    const reason = detail?.reason;
    return explorerEnglishUi()
      ? `${field}: ${status}${source ? ` · Source ${source}` : ''}${asOf ? ` · As of ${asOf}` : ''}${reason ? ` · ${reason}` : ''}`
      : `${field}：${status}${source ? ` · 來源 ${source}` : ''}${asOf ? ` · 資料時間 ${asOf}` : ''}${reason ? ` · ${reason}` : ''}`;
  });
}

async function runScreener() {
  const conditions = selectedScreenerConditions();
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 30_000);
  let data;
  try {
    data = await api(uiDataApi('/screener'), {
      method: 'POST',
      signal: controller.signal,
      body: JSON.stringify({
        market: 'taiwan',
        conditions,
        universe_source: 'top_by_volume',
        filters: { market: 'taiwan' },
        limit: 12,
      }),
    });
  } catch (error) {
    if (error?.name === 'AbortError') {
      throw new Error(explorerText(
        '官方即時行情在 30 秒內沒有完成；系統未以假資料取代結果，請稍後重試。',
        'The official realtime scan did not finish within 30 seconds; no synthetic data was substituted. Please try again shortly.',
      ));
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
  const english = explorerEnglishUi();
  const table = $('screenerTable');
  if (!data.items.length) {
    table.innerHTML = `<div class="event"><h4>${english ? 'No matching realtime quotes' : '沒有符合條件的即時行情'}</h4><p>${english ? 'The official-volume universe was resolved, but no available realtime quote passed every selected condition.' : '已解析官方成交量 Universe，但目前沒有可用即時行情同時通過所有條件。'}</p><p>${escapeHtml(conditions.join('；') || (english ? 'No conditions selected' : '尚未選擇條件'))}</p></div>`;
    return data;
  }
  table.innerHTML = `<div class="row header"><div>${english ? 'Stock' : '股票'}</div><div>${english ? 'Realtime-source Change' : '即時源漲跌'}</div><div>${english ? 'Rationale' : '理由'}</div><div>${english ? 'Verified Conditions' : '已驗證條件'}</div></div>` + data.items.map(r => {
    const pct = r.metrics.change_percent;
    const pctText = Number.isFinite(Number(pct)) ? `${niceNumber(Number(pct))}%` : '-';
    const reasons = english
      ? [
          `Realtime source: ${r.metrics?.source || '-'}`,
          `Quote time: ${r.metrics?.quote_time || '-'}`,
          'Only fields present in the realtime payload are displayed.',
          `Realtime-source change is ${Number(pct) > 0 ? 'positive' : Number(pct) < 0 ? 'negative' : 'flat'}.`,
        ]
      : r.reasons;
    const verified = [
      ...screenerReceiptText(r.metrics?.condition_receipt),
      ...screenerFieldReceiptText(r.metrics?.condition_field_receipt),
      ...screenerQualityReceiptText(r.data_quality_receipt),
    ];
    return `<div class="row clickable" data-symbol="${escapeHtml(r.symbol)}"><div>${escapeHtml(r.name)}<br/><small>${escapeHtml(r.symbol)}</small></div><div><span class="${twColor(Number(pct) || 0)}">${pctText}</span></div><div>${reasons.map(item => escapeHtml(item)).join('<br/>')}</div><div>${verified.map(item => escapeHtml(item)).join('<br/>') || (english ? 'No condition receipt' : '沒有條件收據')}</div></div>`;
  }).join('');
  document.querySelectorAll('#screenerTable .row.clickable').forEach(el => el.addEventListener('click', () => loadSummary(el.dataset.symbol)));
  return data;
}

// Keep the control binding explicit. The market workspace is route-switched
// without recreating the static button, so relying on one early dashboard
// listener can silently leave the control inert after a workspace refresh.
function bindScreenerControls() {
  const button = $('runScreener');
  if (!button || button.dataset.screenerBound === 'true') return;
  button.dataset.screenerBound = 'true';
  button.addEventListener('click', async (event) => {
    event.preventDefault();
    button.disabled = true;
    try {
      await runScreener();
    } catch (error) {
      const table = $('screenerTable');
      if (table) {
        table.innerHTML = `<div class="event"><h4>${escapeHtml(explorerText('選股資料暫時無法載入', 'Screener data is temporarily unavailable'))}</h4><p>${escapeHtml(error?.message || explorerText('請稍後再試。', 'Please try again shortly.'))}</p></div>`;
      }
    } finally {
      button.disabled = false;
    }
  });
}

window.runScreener = runScreener;
window.bindScreenerControls = bindScreenerControls;

function linkageDirectionEnglish(value) {
  const key = String(value || '').toLowerCase();
  return { mixed: 'Mixed', positive: 'Positive', negative: 'Negative', unknown: 'Unknown' }[key] || value || 'Unknown';
}

function linkageEnglishContent(source, target) {
  const key = String(source || '').trim().toUpperCase();
  if (['SOX', '^SOX', '費半'].includes(key)) {
    return {
      mechanism: 'The PHLX Semiconductor Index reflects global semiconductor risk appetite. It may influence capital flows and sentiment in Taiwan semiconductor stocks, while company fundamentals and same-day events remain decisive.',
      path: ['SOX / PHLX Semiconductor', 'Global semiconductor risk appetite', 'Taiwan electronics / semiconductors', target],
      evidence: ['Industry-index linkage rule', 'Realtime quantitative validation is not yet complete'],
    };
  }
  if (['US10Y', '美債', '利率'].includes(key)) {
    return {
      mechanism: 'U.S. Treasury yields affect discount rates and foreign-investor risk appetite. They may influence high-valuation technology stocks, but the direction still depends on growth expectations and prevailing market sentiment.',
      path: ['US10Y', 'Discount rate / risk appetite', 'Growth-stock valuation', target],
      evidence: ['Macroeconomic transmission rule', 'Realtime quantitative validation is not yet complete'],
    };
  }
  return {
    mechanism: 'Only a rule-based explanation is currently available. A historically validated stock-linkage model has not yet been completed, and the system does not present correlation as causation.',
    path: [source, 'Market variable', target],
    evidence: ['Rule template only', 'Historical correlation and event-study validation are still required'],
  };
}

async function runLinkage() {
  const source = $('sourceFactor').value, target = $('targetSymbol').value;
  const l = await api(uiDataApi(`/linkage?source=${encodeURIComponent(source)}&target=${encodeURIComponent(target)}`));
  if (!explorerEnglishUi()) {
    $('linkageResult').innerHTML = `<h3>${l.source} → ${l.target}</h3><p><strong>方向：</strong>${l.direction}　<strong>未校準規則分數：</strong>${Number(l.confidence).toFixed(2)}（0–1）</p><p>${l.mechanism}</p><div class="path">${l.path.map(n => `<span class="node">${n}</span>`).join('<span>→</span>')}</div><p><strong>依據：</strong>${l.evidence.join('；')}</p>`;
    return;
  }
  const english = linkageEnglishContent(source, l.target);
  $('linkageResult').innerHTML = `
    <h3>${escapeHtml(l.source)} → ${escapeHtml(l.target)}</h3>
    <p><strong>Direction:</strong> ${escapeHtml(linkageDirectionEnglish(l.direction))}&nbsp;&nbsp; <strong>Uncalibrated rule score:</strong> ${Number(l.confidence).toFixed(2)} (0–1)</p>
    <p>${escapeHtml(english.mechanism)}</p>
    <div class="path">${english.path.map(n => `<span class="node">${escapeHtml(n)}</span>`).join('<span>→</span>')}</div>
    <p><strong>Evidence:</strong> ${english.evidence.map(item => escapeHtml(item)).join('; ')}</p>`;
}

async function loadCatalog() {
  const [catalog, contract, sourcePolicy, officialEvents, officialDerivatives, updatePlan, dataStatus, dataSources, dataApiContract, newsHistoryCoverage] = await Promise.all([
    api(uiDataApi('/catalog')),
    api('/api/system/requirements'),
    api('/api/system/source-policy'),
    api('/api/system/official-events'),
    api('/api/system/official-derivatives'),
    api('/api/system/update-plan'),
    api('/api/data/status?summary=true'),
    api('/api/data/sources'),
    api('/api/data/ui/v1/contract'),
    api(uiDataApi('/news/history-coverage')),
  ]);
  $('catalogBox').textContent = JSON.stringify(catalog, null, 2);
  renderRequirementContract(contract);
  const schedule = await api('/api/system/schedule');
  renderScheduleGuard(schedule);
  renderSourcePolicy(sourcePolicy);
  renderOfficialEvents(officialEvents);
  renderOfficialDerivatives(officialDerivatives);
  renderUpdateRunner(updatePlan);
  await renderDataPlatform(dataStatus, dataSources, dataApiContract, newsHistoryCoverage);
}

async function renderDataPlatform(status, registry, dataApiContract, newsHistoryCoverage) {
  const warehouse = status?.warehouse || {};
  const rawLake = warehouse?.raw_data_lake || {};
  const standardWarehouse = warehouse?.standard_warehouse || {};
  const incrementalLoader = warehouse?.incremental_loader || {};
  const revisionHistory = warehouse?.revision_history || {};
  const dataLineage = warehouse?.data_lineage || {};
  const dataQuality = warehouse?.data_quality || {};
  const cachePolicy = warehouse?.cache_policy || {};
  const sourceFailover = warehouse?.source_failover || {};
  const sourceObservability = warehouse?.source_observability || {};
  const latestFailover = sourceFailover?.latest_run || {};
  const reconciliation = warehouse?.reconciliation || {};
  const latestReconciliation = reconciliation?.latest_run?.summary || {};
  const reconciliationConflicts = await api('/api/data/reconciliation/conflicts?status=open&limit=20');
  const standardDomains = standardWarehouse?.domains || {};
  const entityRegistry = status?.entity_registry || {};
  const lifecycle = status?.security_lifecycle || {};
  const tables = warehouse?.tables || {};
  const datasets = warehouse?.datasets || [];
  const checkpoints = warehouse?.checkpoints || [];
  const policies = Object.fromEntries((registry?.cache_policies || []).map(item => [item.dataset, item]));
  const registeredDatasets = registry?.datasets || [];
  const failedCheckpoints = checkpoints.filter(item => item.status === 'failed');
  const unifiedDataApi = dataApiContract || status?.unified_data_api || {};
  const cards = [
    ['唯一讀取入口', status?.single_read_path || '-', 'API、Agent 與研究 Pipeline 共用'],
    [
      '來源註冊中心',
      `${registry?.count ?? 0} 個來源 / ${registry?.dataset_count ?? 0} 個資料集`,
      `${registry?.cache_policies?.length ?? 0} 組 TTL · 端點、授權、欄位與失敗策略集中管理`,
    ],
    [
      'Unified Data API',
      `${unifiedDataApi.route_count ?? 0} 條路由 / ${unifiedDataApi.consumer_count ?? 0} 個頁面`,
      `${unifiedDataApi.prefix || '-'} · UI 直接存取 connector ${unifiedDataApi.ui_connector_access ? '需修正' : '0'} · ${unifiedDataApi.status || 'unknown'}`,
    ],
    ['Entity / Revision', `${tables.entities ?? 0} / ${tables.revisions ?? 0}`, `${tables.raw_payloads ?? 0} 份不可變原始資料`],
    [
      'Market Warehouse',
      `${standardWarehouse.record_count ?? 0} 筆標準紀錄`,
      `價格 ${standardDomains.prices?.record_count ?? 0} · 財務 ${standardDomains.financials?.record_count ?? 0} · 籌碼 ${standardDomains.flows?.record_count ?? 0} · 事件 ${standardDomains.events?.record_count ?? 0} · 總體 ${standardDomains.macro?.record_count ?? 0}`,
    ],
    [
      'Incremental Loader',
      `${incrementalLoader.run_count ?? 0} 次更新 / ${incrementalLoader.batch_count ?? 0} 批`,
      `${incrementalLoader.resumable_checkpoint_count ?? 0} 個可續傳 checkpoint · 僅從已提交 cursor 拉取缺少或更新資料`,
    ],
    [
      'Revision History',
      `${revisionHistory.revision_count ?? 0} 個版本 / ${revisionHistory.snapshot_count ?? 0} 份快照`,
      `修訂標的 ${revisionHistory.corrected_observation_count ?? 0} · 版本鏈 ${revisionHistory.status || '尚未驗證'} · 不可變 ${revisionHistory.immutable ? '是' : '否'}`,
    ],
    [
      '完整資料血緣',
      `${dataLineage.artifact_count ?? 0} 個衍生結果 / ${dataLineage.artifact_edge_count ?? 0} 條關聯`,
      `${dataLineage.status || 'not_run'} · 寫入政策 ${dataLineage.write_policy || '尚未驗證'} · 不可變 ${dataLineage.artifacts_immutable ? '是' : '否'}`,
    ],
    [
      'Data Quality',
      `${dataQuality.report_count ?? 0} 份每日報告 / ${dataQuality.issue_count ?? 0} 個問題`,
      `涵蓋 ${dataQuality.dataset_count ?? 0} 個資料集 · 最新 ${dataQuality.latest_report?.report_date || '尚未執行'} · ${dataQuality.status || 'not_run'}`,
    ],
    [
      'Cache Policy',
      `${cachePolicy.policy_count ?? 0} 組規則 / ${cachePolicy.entry_count ?? 0} 個狀態`,
      `fresh ${cachePolicy.state_counts?.fresh ?? 0} · stale ${cachePolicy.state_counts?.stale_while_revalidate ?? 0} · expired ${cachePolicy.state_counts?.expired ?? 0} · invalidated ${cachePolicy.state_counts?.invalidated ?? 0} · refresh ${cachePolicy.active_refresh_count ?? 0}`,
    ],
    [
      'Source Failover',
      `${sourceFailover.failover_count ?? 0} 次備援 / ${sourceFailover.run_count ?? 0} 次執行`,
      latestFailover.run_id
        ? `${latestFailover.requested_source_id} → ${latestFailover.selected_source_id || '失敗'} · 實際來源身分${sourceFailover.source_identity_preserved ? '已保留' : '需檢查'}`
        : '尚未執行 · 僅允許註冊且審查過的備援路徑',
    ],
    [
      'Source Observability',
      `${sourceObservability.alert_count ?? 0} 個告警 / ${sourceObservability.source_count ?? 0} 個來源資料集`,
      `SLO、延遲、缺少分區與修訂異常 · ${sourceObservability.status || 'not_run'}`,
    ],
    [
      'Reconciliation Engine',
      `${reconciliation.open_conflict_count ?? 0} 個未解衝突 / ${reconciliation.run_count ?? 0} 次校驗`,
      latestReconciliation.run_id
        ? `${latestReconciliation.dataset} · ${latestReconciliation.status} · ${latestReconciliation.comparison_count ?? 0} 項跨來源比較`
        : '尚未執行 · 價格、財務與事件採各自校驗規則',
    ],
    [
      'Raw Data Lake',
      `${rawLake.object_count ?? tables.raw_objects ?? 0} 個原始物件`,
      `${rawLake.stored_bytes ?? tables.raw_bytes ?? 0} bytes · 重跑 ${rawLake.reprocessing_run_count ?? tables.raw_reprocessing_runs ?? 0} 次 · ${rawLake.immutable ? '不可變保護' : '尚未驗證'}`,
    ],
    [
      'Envelope 欄位追溯',
      tables.traced_fields === null || tables.traced_fields === undefined
        ? '完整掃描延後'
        : `${tables.traced_fields} 個值`,
      tables.untraced_revisions === null || tables.untraced_revisions === undefined
        ? '大型本機歷史改於完整稽核時掃描，不阻塞操作面板'
        : `${tables.untraced_revisions} 筆 revision 未完整追溯`,
    ],
    [
      'Temporal Contract',
      `${tables.temporal_contract_revisions ?? 0} 筆`,
      `交易日 ${tables.trade_date_revisions ?? 0} · 財務期間 ${tables.fiscal_period_revisions ?? 0} · 違規 ${tables.temporal_contract_violations ?? 0}`,
    ],
    [
      'Entity Registry',
      `${entityRegistry.identifier_count ?? tables.identifiers ?? 0} 個識別碼`,
      `品質 ${entityRegistry.status || '尚未驗證'} · ${entityRegistry.cross_source_entity_count ?? 0} 個跨來源實體 · 目前代號歧義 ${entityRegistry.ambiguous_current_display_symbol_count ?? 0}`,
    ],
    [
      '證券生命週期',
      `${lifecycle.event_count ?? 0} 個事件`,
      `生命週期品質 ${lifecycle.quality?.status || '尚未驗證'} · 股票 ${lifecycle.by_entity_type?.stock ?? 0} · ETF ${lifecycle.by_entity_type?.etf ?? 0} · 權證 ${lifecycle.by_entity_type?.warrant ?? 0} · 指數 ${lifecycle.by_entity_type?.index ?? 0}`,
    ],
    [
      '掛牌範圍',
      `上市 ${lifecycle.by_listing_type?.listed ?? 0} · 上櫃 ${lifecycle.by_listing_type?.otc ?? 0} · 興櫃 ${lifecycle.by_listing_type?.emerging ?? 0}`,
      `下市 ${lifecycle.by_status?.delisted ?? 0} · 到期 ${lifecycle.by_status?.expired ?? 0}`,
    ],
    ['來源衝突', `${tables.open_conflicts ?? 0} 個`, failedCheckpoints.length ? `${failedCheckpoints.length} 個更新失敗` : '衝突只標記、不覆寫來源 · checkpoint 無失敗'],
  ];
  $('dataPlatformSummary').innerHTML = cards.map(([label, value, detail]) => `
    <div class="mini-card">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
      <small>${escapeHtml(detail)}</small>
    </div>`).join('');
  $('dataPlatformStatusChip').textContent = failedCheckpoints.length ? '需處理' : '可查詢';
  $('entityRegistryStatusChip').textContent = entityRegistry.status === 'passed'
    ? '解析正常'
    : `需檢查 · ${entityRegistry.status || '尚未驗證'}`;

  const coverageBox = $('newsHistoryCoverage');
  if (coverageBox) {
    const coverageSources = newsHistoryCoverage?.sources || [];
    const providerWideBlocker = newsHistoryCoverage?.provider_wide_blocker || 'reviewed_provider_wide_historical_coverage_receipt_missing';
    coverageBox.innerHTML = `
      <div class="event">
        <h4>歷史新聞 PIT 回放：${Number(newsHistoryCoverage?.verified_source_count || 0)}/${Number(newsHistoryCoverage?.source_count || 0)} 個來源通過最近匯入審計</h4>
        <p>只有原始發布時間、不可變 coverage receipt 與該批完整匯入審計都成立時，新聞才可用於歷史回放；新聞中心看得到的內容不等於 PIT 證據。</p>
        <small>Provider 全歷史：未認證 · ${escapeHtml(providerWideBlocker)}</small>
      </div>
      ${coverageSources.map(item => {
        const receipt = item.receipt || {};
        const audit = item.latest_ingestion_audit || {};
        const eligible = item.latest_ingestion_pit_eligible === true;
        return `<div class="event">
          <h4>${escapeHtml(item.source_id)} · ${eligible ? '最近匯入可作 PIT 回放' : '最近匯入不可作 PIT 回放'}</h4>
          <p>範圍 ${escapeHtml(receipt.coverage_start || '-')} ～ ${escapeHtml(receipt.coverage_end || '-')} · receipt ${escapeHtml(receipt.receipt_id || '未保存')}</p>
          <p>審計 ${escapeHtml(audit.audit_sha256 || '未保存')} · 匯入 ${Number(audit.unique_ingested_event_count || 0)}/${Number(audit.provider_event_count || 0)} 筆</p>
          <small>${escapeHtml((item.blockers || []).join(' · ') || '最近 immutable audit 與 receipt 已相符；仍不代表 provider 全歷史已認證。')}</small>
        </div>`;
      }).join('') || renderEmptyBlock('尚無新聞歷史 coverage receipt', '目前新聞僅可作即時資訊；匯入並審計具原始發布時間的 provider 歷史資料後，才會在此顯示 PIT 回放資格。')}`;
  }

  $('dataPlatformDatasets').innerHTML = datasets.map(item => {
    const checkpoint = checkpoints.find(row => row.dataset === item.dataset);
    const ttl = policies[item.dataset]?.ttl_seconds;
    const datasetCache = (cachePolicy.items || []).filter(row => row.dataset === item.dataset);
    const cacheStates = [...new Set(datasetCache.map(row => row.state))];
    return `<div class="event">
      <h4>${escapeHtml(item.dataset)}</h4>
      <p>${item.revision_count ?? 0} revisions · ${item.entity_count ?? 0} entities</p>
      <small>最新取得 ${escapeHtml(item.latest_acquired_at || '-')} · checkpoint ${escapeHtml(checkpoint?.status || '尚未建立')} · TTL ${ttl ?? '來源預設'} 秒 · cache ${escapeHtml(cacheStates.join('/') || 'empty')}</small>
      <button type="button" data-cache-invalidate="${escapeHtml(item.dataset)}">使快取失效</button>
      <small data-cache-result="${escapeHtml(item.dataset)}"></small>
      <button type="button" data-quality-dataset="${escapeHtml(item.dataset)}">執行品質檢查</button>
      <small data-quality-result="${escapeHtml(item.dataset)}"></small>
      ${reconciliation.configured_datasets?.includes(item.dataset) ? `
        <button type="button" data-reconciliation-dataset="${escapeHtml(item.dataset)}">執行跨來源校驗</button>
        <small data-reconciliation-result="${escapeHtml(item.dataset)}"></small>
      ` : ''}
      <button type="button" data-snapshot-dataset="${escapeHtml(item.dataset)}">建立目前版本快照</button>
      <small data-snapshot-result="${escapeHtml(item.dataset)}"></small>
    </div>`;
  }).join('') || renderEmptyBlock('尚無標準化資料', '完成來源載入後會在此顯示 revision、Entity、checkpoint 與 TTL。');

  $('dataPlatformSources').innerHTML = (registry?.items || []).map(item => {
    const sourceDatasets = registeredDatasets.filter(dataset => dataset.source_id === item.source_id);
    const strategies = [...new Set(sourceDatasets.map(dataset => dataset.failure_strategy?.on_exhausted).filter(Boolean))];
    const latestFailoverRole = latestFailover.requested_source_id === item.source_id
      ? `最近主來源執行：${latestFailover.status || '-'}`
      : latestFailover.selected_source_id === item.source_id
        ? `最近實際備援來源：${latestFailover.selected_dataset_id || '-'}`
        : '';
    return `
      <div class="event">
        <h4>${escapeHtml(item.display_name)}</h4>
        <p>${escapeHtml(item.source_id)} · 可靠度 Tier ${item.reliability_tier} · ${sourceDatasets.length} 個資料集 · 每 ${item.update_frequency_seconds} 秒更新</p>
        <small>${escapeHtml(item.authority)} · ${escapeHtml(item.license_status)} · ${(item.domains || []).map(domain => escapeHtml(domain)).join(', ')}</small>
        <small>失敗策略：${strategies.map(value => escapeHtml(value)).join('、') || '未宣告'} · 備援：${(item.failover_source_ids || []).map(value => escapeHtml(value)).join('、') || '無'}</small>
        ${latestFailoverRole ? `<small>${escapeHtml(latestFailoverRole)} · run ${escapeHtml(latestFailover.run_id || '-')}</small>` : ''}
        <details>
          <summary>檢視欄位與端點契約</summary>
          ${sourceDatasets.map(dataset => `
            <p><strong>${escapeHtml(dataset.dataset_id)}</strong> · ${escapeHtml(dataset.transport)} · 必填 ${(dataset.required_fields || []).map(value => escapeHtml(value)).join(', ') || '無'}<br/>
            <small>${escapeHtml(dataset.endpoint_template)} · ${escapeHtml(dataset.failure_strategy?.on_exhausted || '未宣告')}</small></p>
          `).join('') || '<small>此來源目前沒有啟用的資料集端點。</small>'}
        </details>
      </div>`;
  }).join('') || renderEmptyBlock('來源註冊表為空', '系統不會改用未註冊來源。');

  const observabilityItems = sourceObservability?.sources || [];
  const observabilityAlerts = sourceObservability?.alerts || [];
  $('dataSourceObservability').innerHTML = observabilityItems.map(item => {
    const rate = item.success_rate === null || item.success_rate === undefined
      ? '尚無嘗試'
      : `${(Number(item.success_rate) * 100).toFixed(1)}%`;
    const lag = item.lag_seconds === null || item.lag_seconds === undefined
      ? '尚無成功資料'
      : `${Math.round(Number(item.lag_seconds))} 秒`;
    const missing = (item.missing_partitions || []).map(partition => `${partition.partition_key} (${partition.status})`).join('、');
    return `<div class="event">
      <h4>${escapeHtml(item.source_display_name || item.source_id)} · ${escapeHtml(item.dataset)}</h4>
      <p>SLO ${escapeHtml(rate)} · ${escapeHtml(item.slo_status)} · 延遲 ${escapeHtml(lag)} · ${escapeHtml(item.lag_status)}</p>
      <small>嘗試 ${item.window_attempt_count ?? 0} / 成功 ${item.window_success_count ?? 0} · 修訂更正 ${item.revision_correction_count ?? 0} · ${escapeHtml(item.revision_status || 'passed')}</small>
      ${missing ? `<small>待處理分區：${escapeHtml(missing)}</small>` : '<small>已宣告分區均有成功 checkpoint，或尚未設定分區契約。</small>'}
    </div>`;
  }).join('') || renderEmptyBlock('尚無來源可觀測性資料', '完成資料載入或設定分區契約後，會顯示來源 SLO、延遲與版本異常。');
  if (observabilityAlerts.length) {
    const deliveryCopy = sourceObservability.notification_delivery_configured
      ? '桌面通知出口已啟用；按「評估並通知」後才會投遞，每筆結果均保存不可變收據。'
      : '通知出口尚未配置；系統不會把本機告警評估誤報為已投遞。';
    $('dataSourceObservability').insertAdjacentHTML('afterbegin', `<div class="event"><h4>自動告警 · ${observabilityAlerts.length} 項</h4><p>${observabilityAlerts.map(item => escapeHtml(`${item.source_id}/${item.dataset}: ${item.code}`)).join('；')}</p><small>${escapeHtml(deliveryCopy)}</small></div>`);
  }
  const notifyObservability = $('notifySourceObservability');
  if (notifyObservability) {
    notifyObservability.onclick = async () => {
      const resultNode = $('sourceObservabilityActionResult');
      notifyObservability.disabled = true;
      if (resultNode) resultNode.textContent = '正在評估資料新鮮度並投遞目前告警…';
      try {
        const receipt = await api('/api/data/observability/notify', { method: 'POST' });
        const slo = receipt?.slo_observation || {};
        const evidence = slo?.evidence || {};
        await loadCatalog();
        const refreshedResult = $('sourceObservabilityActionResult');
        if (refreshedResult) {
          const report = slo?.observation || {};
          const detail = evidence.data_at
            ? `最舊實際成功時間 ${evidence.data_at}`
            : `缺少或逾時來源：${(evidence.unavailable_sources || []).join('、') || '未取得任何來源成功時間'}`;
          const deliveryCounts = receipt?.notification_status_counts || {};
          const delivered = Number(deliveryCounts.delivered || 0);
          const failed = Number(deliveryCounts.failed || 0);
          const unconfigured = Number(deliveryCounts.not_configured || 0);
          const receiptHashes = (receipt?.notification_receipts || [])
            .map(item => item.receipt_sha256)
            .filter(Boolean)
            .slice(0, 3)
            .join(' · ');
          refreshedResult.innerHTML = `<div class="event"><h4>資料新鮮度 SLO · ${escapeHtml(slo.status || 'unknown')}</h4><p>${escapeHtml(report.status || '未寫入報告')} · ${escapeHtml(detail)}</p><p>通知：已投遞 ${delivered} · 失敗 ${failed} · 未設定 ${unconfigured}</p><small>SLO ${escapeHtml(report.report_sha256 || '未取得收據')} · 通知 ${escapeHtml(receiptHashes || '本次沒有告警收據')}</small></div>`;
        }
      } catch (error) {
        if (resultNode) resultNode.textContent = `資料新鮮度評估失敗：${error.message}`;
      } finally {
        const refreshedButton = $('notifySourceObservability');
        if (refreshedButton) refreshedButton.disabled = false;
      }
    };
  }

  $('dataReconciliationConflicts').innerHTML = (reconciliationConflicts?.items || []).map(item => `
    <div class="event">
      <h4>${escapeHtml(item.dataset)} · ${escapeHtml(item.field_name)}</h4>
      <p>${escapeHtml(item.left_source_id || '-')}：${escapeHtml(JSON.stringify(item.left_value))} ↔ ${escapeHtml(item.right_source_id || '-')}：${escapeHtml(JSON.stringify(item.right_value))}</p>
      <small>${escapeHtml(item.entity_id)} · ${escapeHtml(item.observation_key)} · ${escapeHtml(item.comparison_method || 'exact')} · conflict ${escapeHtml(item.conflict_id)}</small>
    </div>
  `).join('') || renderEmptyBlock('目前沒有未解來源衝突', '校驗一致時不會改寫任一來源的 revision。');

  document.querySelectorAll('[data-quality-dataset]').forEach(button => button.addEventListener('click', async () => {
    const resultNode = document.querySelector(`[data-quality-result="${CSS.escape(button.dataset.qualityDataset)}"]`);
    button.disabled = true;
    if (resultNode) resultNode.textContent = '正在產生今日 point-in-time 品質報告…';
    try {
      const report = await api(`/api/data/quality/${encodeURIComponent(button.dataset.qualityDataset)}`, {
        method: 'POST',
        body: JSON.stringify({ partition_key: 'all' }),
      });
      button.textContent = `品質 ${report.status} · ${report.report_date}`;
      if (resultNode) {
        resultNode.textContent = `缺漏 ${report.missing_count} · 異常 ${report.anomaly_count} · 時間錯位 ${report.time_misalignment_count} · 重複 ${report.duplicate_count} · 來源衝突 ${report.conflict_count}`;
      }
    } catch (error) {
      button.textContent = `品質檢查失敗：${error.message}`;
      if (resultNode) resultNode.textContent = '';
    } finally {
      button.disabled = false;
    }
  }));
  document.querySelectorAll('[data-reconciliation-dataset]').forEach(button => button.addEventListener('click', async () => {
    const dataset = button.dataset.reconciliationDataset;
    const resultNode = document.querySelector(`[data-reconciliation-result="${CSS.escape(dataset)}"]`);
    button.disabled = true;
    if (resultNode) resultNode.textContent = '正在以各來源最新 point-in-time revision 進行校驗…';
    try {
      const report = await api('/api/data/reconciliation/runs', {
        method: 'POST',
        body: JSON.stringify({ dataset }),
      });
      button.textContent = `校驗 ${report.status} · ${report.comparison_count} 項`;
      if (resultNode) {
        resultNode.textContent = `來源 ${report.source_count} · 衝突 ${report.conflict_count} · 已解 ${report.resolved_count} · 原始來源資料已保留`;
      }
    } catch (error) {
      button.textContent = `跨來源校驗失敗：${error.message}`;
      if (resultNode) resultNode.textContent = '';
    } finally {
      button.disabled = false;
    }
  }));
  document.querySelectorAll('[data-cache-invalidate]').forEach(button => button.addEventListener('click', async () => {
    const dataset = button.dataset.cacheInvalidate;
    const resultNode = document.querySelector(`[data-cache-result="${CSS.escape(dataset)}"]`);
    button.disabled = true;
    if (resultNode) resultNode.textContent = '正在依規則記錄失效事件…';
    try {
      const result = await api(`/api/data/cache/${encodeURIComponent(dataset)}/invalidate`, {
        method: 'POST',
        body: JSON.stringify({ reason: 'manual_refresh' }),
      });
      button.textContent = `已失效 ${result.count ?? 0} 個 cache entry`;
      if (resultNode) resultNode.textContent = `${result.reason} · ${result.invalidated_at}`;
    } catch (error) {
      button.textContent = `快取失效失敗：${error.message}`;
      if (resultNode) resultNode.textContent = '';
    } finally {
      button.disabled = false;
    }
  }));
  document.querySelectorAll('[data-snapshot-dataset]').forEach(button => button.addEventListener('click', async () => {
    const resultNode = document.querySelector(`[data-snapshot-result="${CSS.escape(button.dataset.snapshotDataset)}"]`);
    button.disabled = true;
    if (resultNode) resultNode.textContent = '正在固定 point-in-time revision manifest…';
    try {
      const response = await api('/api/data/snapshots', {
        method: 'POST',
        body: JSON.stringify({ dataset: button.dataset.snapshotDataset }),
      });
      const snapshot = response?.snapshot || {};
      button.textContent = `快照 ${snapshot.item_count ?? 0} 筆`;
      if (resultNode) {
        resultNode.textContent = `${snapshot.snapshot_id || '-'} · integrity ${snapshot.integrity?.status || '-'}`;
      }
    } catch (error) {
      if (resultNode) resultNode.textContent = `快照失敗：${error.message}`;
    } finally {
      button.disabled = false;
    }
  }));

  const latestLineageArtifact = dataLineage?.latest_artifact || null;
  if (latestLineageArtifact?.artifact_id) {
    const graph = await api(`/api/data/lineage/${encodeURIComponent(latestLineageArtifact.artifact_id)}`);
    const completeness = graph?.completeness || {};
    const artifactNodes = (graph?.nodes || []).filter(node => node.type === 'artifact');
    const revisionNodes = (graph?.nodes || []).filter(node => node.type === 'revision');
    $('dataPlatformLineage').innerHTML = `
      <div class="event">
        <h4>完整資料血緣 · ${escapeHtml(latestLineageArtifact.artifact_type)}</h4>
        <p>${escapeHtml(latestLineageArtifact.name)} · ${escapeHtml(JSON.stringify(latestLineageArtifact.value))}</p>
        <small>${escapeHtml(latestLineageArtifact.entity_id || '-')} · artifact ${escapeHtml(latestLineageArtifact.artifact_id)}</small>
        <small>狀態 ${escapeHtml(completeness.status || 'incomplete')} · 來源 ${completeness.source_count ?? 0} · raw ${completeness.raw_payload_count ?? 0} · revision ${revisionNodes.length} · 衍生結果 ${artifactNodes.length} · 深度 ${completeness.max_depth ?? 0}</small>
        <small>來源：${(graph.sources || []).map(value => escapeHtml(value)).join('、') || '-'} · 完整性政策：缺少原始資料即拒絕寫入</small>
        <details open>
          <summary>原始來源 → revision → 指標 → 結論</summary>
          <p><strong>轉換鏈</strong><br/><small>${(graph.transformation_ids || []).map(value => escapeHtml(value)).join(' → ') || '-'}</small></p>
          <p><strong>原始 payload</strong><br/><small>${(graph.raw_payload_ids || []).map(value => escapeHtml(value)).join('、') || '-'}</small></p>
          ${(graph.edges || []).filter(edge => edge.edge_type === 'derived_from').map(edge => `
            <p><strong>${escapeHtml(edge.input_role || 'input')}</strong> · ${escapeHtml(edge.from)} → ${escapeHtml(edge.to)}<br/>
            <small>${escapeHtml(edge.transformation_id || '-')} · fields ${(edge.input_fields || []).map(value => escapeHtml(value)).join(', ') || '/'}</small></p>
          `).join('')}
        </details>
      </div>`;
    return;
  }

  if (!datasets.length) {
    $('dataPlatformLineage').innerHTML = renderEmptyBlock('尚無血緣資料', '資料載入後可從 revision 反查原始 payload 與轉換版本。');
    return;
  }
  let revision = null;
  for (const candidate of datasets) {
    const query = await api('/api/data/query', {
      method: 'POST',
      body: JSON.stringify({ dataset: candidate.dataset, limit: 1 }),
    });
    revision = query?.items?.[0] || null;
    if (revision) break;
  }
  if (!revision) {
    $('dataPlatformLineage').innerHTML = renderEmptyBlock('尚無血緣資料', '查無可顯示的 revision。');
    return;
  }
  const lineage = await api(`/api/data/revisions/${encodeURIComponent(revision.revision_id)}/lineage`);
  const fieldProvenance = Object.entries(revision.field_provenance || {}).slice(0, 6);
  $('dataPlatformLineage').innerHTML = `
    <div class="event">
      <h4>${escapeHtml(revision.dataset)} · ${escapeHtml(revision.entity_id)}</h4>
      <p>Revision ${revision.revision} · ${escapeHtml(revision.source_id)} · ${escapeHtml(revision.quality_status)}</p>
      <small>basis ${escapeHtml(revision.temporal?.time_basis || '-')} · trade date ${escapeHtml(revision.temporal?.trade_date || '-')} · fiscal period ${escapeHtml(revision.temporal?.fiscal_period || '-')}</small>
      <small>published ${escapeHtml(revision.temporal?.published_at || '-')} · available ${escapeHtml(revision.temporal?.available_at || '-')} · acquired ${escapeHtml(revision.temporal?.acquired_at || '-')} · effective ${escapeHtml(revision.temporal?.effective_at || '-')}</small>
      <small>raw ${escapeHtml(lineage?.raw_payload?.raw_payload_id || '-')} · parsed SHA-256 ${escapeHtml(lineage?.raw_payload?.payload_hash || '-')}</small>
      <small>object ${escapeHtml(lineage?.raw_payload?.raw_object_id || '-')} · wire SHA-256 ${escapeHtml(lineage?.raw_payload?.wire_hash || '-')} · ${lineage?.raw_payload?.byte_length ?? 0} bytes · integrity ${escapeHtml(lineage?.raw_payload?.integrity_status || '-')}</small>
      ${lineage?.raw_payload?.raw_payload_id ? `
        <button type="button" data-reprocess-raw="${escapeHtml(lineage.raw_payload.raw_payload_id)}">從原始物件重跑清洗</button>
        <small data-reprocess-result></small>
      ` : ''}
      <details>
        <summary>檢視欄位級來源、時間與品質</summary>
        ${fieldProvenance.map(([pointer, provenance]) => `
          <p><strong>${escapeHtml(pointer)}</strong> · ${escapeHtml(provenance.source_id || '-')} · ${escapeHtml(provenance.quality_status || '-')}<br/>
          <small>basis ${escapeHtml(provenance.temporal?.time_basis || '-')} · trade date ${escapeHtml(provenance.temporal?.trade_date || '-')} · fiscal period ${escapeHtml(provenance.temporal?.fiscal_period || '-')}</small><br/>
          <small>published ${escapeHtml(provenance.temporal?.published_at || '-')} · available ${escapeHtml(provenance.temporal?.available_at || '-')} · acquired ${escapeHtml(provenance.temporal?.acquired_at || '-')} · effective ${escapeHtml(provenance.temporal?.effective_at || '-')} · updated ${escapeHtml(provenance.updated_at || '-')}</small><br/>
          <small>raw ${escapeHtml(provenance.raw_payload_id || '-')} · path ${escapeHtml(provenance.raw_json_pointer || '-')}</small></p>
        `).join('') || '<small>此 revision 沒有欄位追溯資料。</small>'}
      </details>
    </div>`;
  const reprocessButton = document.querySelector('[data-reprocess-raw]');
  if (reprocessButton) {
    reprocessButton.addEventListener('click', async () => {
      const resultNode = reprocessButton.parentElement.querySelector('[data-reprocess-result]');
      reprocessButton.disabled = true;
      if (resultNode) resultNode.textContent = '正在驗證原始 SHA-256 並重跑清洗…';
      try {
        const replay = await api(`/api/data/raw/${encodeURIComponent(reprocessButton.dataset.reprocessRaw)}/reprocess`, {
          method: 'POST',
          body: JSON.stringify({ dataset: revision.dataset }),
        });
        if (resultNode) {
          resultNode.textContent = `重跑 ${replay.status} · ${replay.output_record_count} 筆 · output ${replay.output_payload_hash}`;
        }
      } catch (error) {
        if (resultNode) resultNode.textContent = `重跑失敗：${error.message}`;
      } finally {
        reprocessButton.disabled = false;
      }
    });
  }
}

async function resolveEntityIdentifier() {
  const input = $('entityIdentifierInput');
  const resultBox = $('entityResolutionResult');
  const identifier = String(input?.value || '').trim();
  if (!identifier) {
    resultBox.innerHTML = renderEmptyBlock('請輸入代號', '可輸入交易所代號、正式顯示代號、統編或完整 ENT ID。');
    return;
  }
  resultBox.innerHTML = renderEmptyBlock('解析中', '正在查詢 Point-in-time Entity Registry。');
  try {
    const resolution = await api(`/api/data/entity-registry/resolve?identifier=${encodeURIComponent(identifier)}`);
    const candidates = resolution?.candidates || [];
    if (resolution.status === 'unavailable') {
      resultBox.innerHTML = renderEmptyBlock('查無標的', 'Registry 不會為未知代號建立假資料。');
      return;
    }
    const rows = candidates.map(candidate => {
      const identifiers = candidate.matched_identifiers || candidate.identifiers || [];
      const aliases = [...new Set(
        identifiers.map(item => `${item.identifier_value} · ${item.source_id}`),
      )].join('；');
      const selected = resolution.entity?.entity_id === candidate.entity_id;
      return `<div class="event">
        <h4>${selected ? '已解析 · ' : '候選 · '}${escapeHtml(candidate.canonical_name || candidate.entity_id)}</h4>
        <p>${escapeHtml(candidate.entity_id)} · ${escapeHtml(candidate.exchange || '-')} · ${escapeHtml(candidate.lifecycle_status || '-')}</p>
        <small>${escapeHtml(aliases || '以內部 Entity ID 直接解析')}</small>
      </div>`;
    }).join('');
    const warning = (resolution.warnings || []).includes('historical_identifier_reuse')
      ? '<div class="event"><h4>歷史代號曾被重用</h4><p>目前只選取唯一仍有效的實體；歷史研究請傳入 as_of。</p></div>'
      : '';
    resultBox.innerHTML = `${warning}${rows}`;
  } catch (error) {
    resultBox.innerHTML = renderEmptyBlock('解析失敗', error.message);
  }
}

if ($('refreshDataPlatform')) {
  $('refreshDataPlatform').addEventListener('click', () => loadCatalog().catch(error => {
    $('dataPlatformStatusChip').textContent = `載入失敗：${error.message}`;
  }));
}

if ($('resolveEntityIdentifier')) {
  $('resolveEntityIdentifier').addEventListener('click', () => resolveEntityIdentifier());
  $('entityIdentifierInput').addEventListener('keydown', event => {
    if (event.key === 'Enter') {
      event.preventDefault();
      resolveEntityIdentifier();
    }
  });
}
