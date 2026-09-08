function renderRequirementContract(contract) {
  const coverage = contract?.coverage || {};
  const cards = [
    ['資料來源分層', `${coverage.source_tier_count ?? '-'} 層`, '即時/交易、官方公開、輔助資料'],
    ['資料模組', `${coverage.data_module_count ?? '-'} 個`, '主檔、即時、盤中分 K、歷史 K 線、法人、籌碼、基本面、事件、新聞、資產、委託、訊號'],
    ['功能視圖', `${coverage.app_feature_count ?? '-'} 個`, 'A-M 首頁、自選、個股、監控、選股、新聞、籌碼、財報、交易、輔助、風控、資產、通知'],
    ['交易邊界', coverage.live_ordering_enabled ? '實單啟用' : '實單關閉', coverage.broker_api_connected ? '已接券商 API' : '未接券商 API，僅預覽/紙上交易'],
  ];
  $('requirementContractBox').innerHTML = cards.map(([label, value, detail]) => `
    <div class="mini-card">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
      <small>${escapeHtml(detail)}</small>
    </div>`).join('');
  const safety = contract?.safety_limits || [];
  $('requirementSafetyBox').innerHTML = safety.slice(0, 7).map(item => `
    <div class="event">
      <h4>${escapeHtml(item.rule || item.code)}</h4>
      <p>${escapeHtml(controlStatusLabel(item.status))} / ${(item.enforced_by || []).length} 項控管</p>
    </div>`).join('') || renderEmptyBlock('尚無安全限制', '系統需求契約尚未載入。');
}

function renderScheduleGuard(schedule) {
  const activePhase = (schedule?.phases || []).find(item => item.active) || {};
  const blockedCount = (schedule?.phases || []).reduce((sum, phase) => sum + Number(phase.blocked_task_count || 0), 0);
  const cards = [
    ['目前排程', schedule?.active_phase || '-', `${schedule?.timezone || 'Asia/Taipei'} / ${schedule?.as_of || '-'}`],
    ['可執行任務', `${activePhase.allowed_task_count ?? 0} 個`, activePhase.window || '目前非交易資料更新窗'],
    ['阻擋任務', `${blockedCount} 個`, schedule?.authorized_realtime_feed ? '已授權即時資料' : '未授權即時 streaming'],
    ['實單下單', schedule?.live_ordering_allowed ? '允許' : '關閉', schedule?.broker_api_connected ? '已接券商 API' : '未接券商 API'],
  ];
  $('scheduleGuardBox').innerHTML = cards.map(([label, value, detail]) => `
    <div class="mini-card">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
      <small>${escapeHtml(detail)}</small>
    </div>`).join('');
}

function renderSourcePolicy(policy) {
  const guardrails = policy?.guardrails_enforced || {};
  const samplePrice = policy?.sample_auxiliary_only_price || {};
  const sampleDecision = policy?.sample_complete_decision || {};
  const cards = [
    ['輔助行情限制', guardrails.auxiliary_source_cannot_be_sole_price_source ? '已阻擋' : '需檢視', 'Yahoo/Google/新聞不可作為唯一價格來源'],
    ['完整決策證據', guardrails.complete_decision_evidence_allowed ? '已通過' : '需補齊', '技術、籌碼、基本面、事件、風險與進出場條件'],
    ['資料衝突標記', guardrails.source_conflict_requires_note ? '已要求' : '需檢視', '來源衝突時必須留下資料衝突說明'],
    ['來源分層樣本', `${samplePrice?.source_summary?.tier_3 ?? 0} 個輔助源`, samplePrice?.price_policy?.allowed ? '價格樣本未阻擋' : '輔助來源單獨價格已阻擋'],
    ['決策樣本', sampleDecision?.overall_allowed ? '允許' : '阻擋', `${sampleDecision?.source_summary?.source_count ?? 0} 個來源 / ${sampleDecision?.decision_policy?.missing_fields?.length ?? 0} 個缺漏`],
  ];
  $('sourcePolicyBox').innerHTML = cards.map(([label, value, detail]) => `
    <div class="mini-card">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
      <small>${escapeHtml(detail)}</small>
    </div>`).join('');
}

function renderOfficialDerivatives(status) {
  const tdcc = status?.contracts?.tdcc_holding_distribution || {};
  const taifex = status?.contracts?.taifex_derivatives_summary || {};
  const cards = [
    ['TDCC 集保', tdcc.connected ? '快取可用' : '契約就緒', tdcc.source?.csv_import_endpoint ? '支援 JSON/CSV 匯入；週資料只做籌碼集中度分析' : (tdcc.source?.guardrail || '週資料只做籌碼集中度分析')],
    ['TAIFEX 期權', taifex.connected ? '快取可用' : '契約就緒', taifex.source?.csv_import_endpoint ? '支援 JSON/CSV 匯入；盤後公開資料只做風險背景' : (taifex.source?.guardrail || '盤後公開資料只做風險背景')],
    ['官方來源數', `${status?.connected_source_count ?? 0}/${status?.required_source_count ?? 0}`, '未接 live feed 時不偽造期權/集保資料'],
    ['交易邊界', status?.live_trading_source ? '需檢視' : '非即時交易源', '期貨/選擇權實單仍需券商 API 與授權行情'],
  ];
  $('officialDerivativesBox').innerHTML = cards.map(([label, value, detail]) => `
    <div class="mini-card">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
      <small>${escapeHtml(detail)}</small>
    </div>`).join('');
}

function renderOfficialEvents(status) {
  const mops = status?.mops_company_events || {};
  const source = status?.sources?.[0] || {};
  const cards = [
    ['MOPS 重大訊息', mops.available ? '快取可用' : '尚未匯入', source.csv_import_endpoint ? '支援 JSON/CSV 匯入，來源層級為第 2 層官方公開資料。' : source.guardrail || '官方重大訊息快取尚未設定。'],
    ['事件筆數', `${mops.count ?? 0}`, mops.latest_event_time ? `最新事件 ${mops.latest_event_time}` : (mops.blocking_reason || '等待官方資料匯入。')],
    ['官方來源數', `${status?.connected_source_count ?? 0}/${status?.required_source_count ?? 0}`, '重大訊息會優先進入個股事件與新聞中心。'],
    ['交易邊界', status?.live_trading_source ? '需檢查' : '非即時交易源', 'MOPS 是公告/財報來源，不可當秒級報價或自動下單來源。'],
  ];
  $('officialEventsBox').innerHTML = cards.map(([label, value, detail]) => `
    <div class="mini-card">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
      <small>${escapeHtml(detail)}</small>
    </div>`).join('');
}

function renderUpdateRunner(plan) {
  const selected = (plan?.phases || []).find(item => item.selected) || {};
  const cards = [
    ['更新任務乾跑', plan?.dry_run ? '啟用' : '未啟用', '只產生任務計畫，不寫資料、不下單'],
    ['選定時段', selected.phase || plan?.active_phase || '-', selected.window || '目前非排程窗'],
    ['總任務', `${plan?.job_count ?? 0} 個`, `${plan?.phase_count ?? 0} 個排程時段`],
    ['本次可執行', `${plan?.ready_selected_job_count ?? 0} 個`, `阻擋 ${plan?.blocked_selected_job_count ?? 0} 個`],
    ['安全邊界', plan?.live_ordering_enabled ? '需檢視' : '實單關閉', plan?.broker_api_connected ? '已接券商 API' : '未接券商 API'],
  ];
  $('updateRunnerBox').innerHTML = cards.map(([label, value, detail]) => `
    <div class="mini-card">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
      <small>${escapeHtml(detail)}</small>
    </div>`).join('');
}

function bindSymbolOpeners(selector) {
  document.querySelectorAll(selector).forEach(el => el.addEventListener('click', (event) => {
    if (event.target.closest('a')) return;
    const sym = el.dataset.symbol;
    if (sym) loadSummary(sym).catch(err => alert(err.message));
  }));
}
