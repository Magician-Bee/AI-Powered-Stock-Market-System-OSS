function openStockTone(action) {
  const key = String(action || '').toLowerCase();
  if (['sell', 'reduce'].includes(key)) return 'warn';
  return '';
}

function openStockCount(items) {
  return Array.isArray(items) ? items.length : 0;
}

function openStockPercent(value, digits = 0) {
  if (value === null || value === undefined) return '-';
  const amount = Number(value);
  if (!Number.isFinite(amount)) return '-';
  return `${(amount * 100).toFixed(digits)}%`;
}

function openStockPointPercent(value, digits = 1) {
  if (value === null || value === undefined) return '-';
  const amount = Number(value);
  if (!Number.isFinite(amount)) return '-';
  return `${amount.toFixed(digits)}%`;
}

function contractLabel(value, fallback = '契約 v1') {
  const raw = String(value || '');
  const version = raw.match(/\.v(\d+)/i)?.[1];
  return version ? `契約 v${version}` : fallback;
}

function sourceDisplayName(value) {
  const raw = String(value || '').toLowerCase();
  if (raw.includes('tradingagents')) return '外部策略流程';
  if (raw.includes('ai-trader') || raw.includes('ai_trader')) return '外部訊號格式';
  if (raw.includes('fingpt')) return '情緒預測模組';
  if (raw.includes('finrobot')) return '研究報告模組';
  if (raw.includes('finrl')) return '策略回測模組';
  if (raw.includes('qlib')) return '因子研究模組';
  return value || '外部研究來源';
}

function methodDisplayName(value) {
  const raw = String(value || '').toLowerCase();
  if (raw.includes('keyword_sentiment')) return '本地情緒與新聞摘要';
  if (raw.includes('revenue_snapshot')) return '本地營收與報告摘要';
  if (raw.includes('technical')) return '技術面摘要';
  if (raw.includes('metadata')) return '本地結構讀取';
  if (raw.includes('placeholder')) return '待接資料';
  return neutralResearchText(value || '本地轉接');
}

function controlStatusLabel(value) {
  const labels = {
    enforced: '已落實',
    policy_declared: '已列入政策',
    partially_enforced: '部分落實',
    enforced_for_current_mode: '目前模式已落實',
    preview_enforced: '預覽模式已落實',
  };
  return labels[String(value || '')] || value || '-';
}

function neutralResearchText(value) {
  return String(value || '')
    .replace(/OpenStockAIEngine/g, '本地策略模組')
    .replace(/Open Stock AI/g, '本地策略模組')
    .replace(/TradingAgents/g, '外部策略流程')
    .replace(/AI-Trader/g, '外部訊號格式')
    .replace(/ai_trader/gi, 'external_signal')
    .replace(/FinGPT/g, '情緒預測模組')
    .replace(/FinRobot/g, '研究報告模組')
    .replace(/\bmulti-agent\b/gi, '多流程')
    .replace(/\bheavy model inference\b/gi, '深度推論')
    .replace(/source verified:/gi, '來源已驗證：')
    .replace(/\d+\s+news items scored as bullish\s*\([^)]+\);?/gi, '多則新聞評為偏多；')
    .replace(/\d+\s+news items scored as bearish\s*\([^)]+\);?/gi, '多則新聞評為偏空；')
    .replace(/news items scored as neutral/gi, '則新聞評為中性')
    .replace(/Research or backtest validation failed\.?/gi, '研究或回測驗證未通過')
    .replace(/Rejected by research validation\.?/gi, '未通過研究驗證')
    .replace(/blocked_by_risk/gi, '風控阻擋')
    .replace(/validation failed\.?/gi, '驗證未通過')
    .replace(/remains behind the adapter boundary/gi, '保留在轉接器邊界內')
    .replace(/fundamentals loaded=True/gi, '基本面已載入')
    .replace(/fundamentals loaded=False/gi, '基本面未載入')
    .replace(/view=positive/gi, '觀點=正向')
    .replace(/view=negative/gi, '觀點=偏空')
    .replace(/view=neutral/gi, '觀點=中性')
    .replace(/report generation\/runtime stays isolated behind the adapter/gi, '報告產生流程保留在轉接器內')
    .replace(/technical view is uptrend/gi, '技術面為上升趨勢')
    .replace(/technical view is downtrend/gi, '技術面為下降趨勢')
    .replace(/technical view is neutral/gi, '技術面為中性')
    .replace(/graph output is evidence only and cannot bypass/gi, '流程輸出僅作證據，不可繞過')
    .replace(/Unified strategy score/gi, '統一策略分數')
    .replace(/forecast: flat/gi, '預測：持平')
    .replace(/Portfolio rating/gi, '投組評級')
    .replace(/Trader action/gi, '交易動作')
    .replace(/Research or backtest/gi, '研究或回測')
    .replace(/\bfingpt\b/gi, '情緒預測模組')
    .replace(/\bfinrobot\b/gi, '研究報告模組')
    .replace(/\btradingagents\b/gi, '外部策略流程')
    .replace(/\bstrategy_engine\b/gi, '策略模組')
    .replace(/\bsignal\b/gi, '訊號')
    .replace(/validated_locally_no_remote_publish/gi, '本地驗證，不遠端發布')
    .replace(/read_only_skill_route_no_remote_external_signal_publish/gi, '唯讀路由，不遠端發布外部訊號')
    .replace(/read_only_skill_route_no_remote_ai_trader_publish/gi, '唯讀路由，不遠端發布外部訊號');
}

function sanitizeTechnicalValue(value) {
  if (Array.isArray(value)) return value.map(sanitizeTechnicalValue);
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, sanitizeTechnicalValue(item)]));
  }
  if (typeof value === 'string') return neutralResearchText(value);
  return value;
}

function renderOpenStockFlow(decision) {
  const snapshot = decision.market_snapshot || {};
  const intelligence = decision.intelligence || {};
  const signal = decision.signal || {};
  const research = decision.research || {};
  const performanceMetrics = research.performance_metrics || research.raw?.finrl?.performance_metrics || {};
  const significance = research.statistical_significance || research.raw?.finrl?.statistical_significance || {};
  const benchmarkMetrics = research.benchmark_metrics || research.raw?.finrl?.benchmark_metrics || {};
  const monteCarloTailRisk = research.monte_carlo_tail_risk || research.raw?.finrl?.monte_carlo_tail_risk || {};
  const regimeRobustness = research.regime_robustness || research.raw?.finrl?.regime_robustness || {};
  const historicalUniverse = research.historical_universe || research.raw?.finrl?.historical_universe || {};
  const risk = decision.risk || {};
  const execution = decision.execution || {};
  const steps = [
    {
      name: '行情資料中樞',
      status: snapshot.price === null || snapshot.price === undefined ? '部分資料' : '就緒',
      tone: snapshot.price === null || snapshot.price === undefined ? 'warn' : '',
      detail: `${openStockCount(snapshot.ohlcv)} 筆 OHLCV · ${openStockCount(snapshot.news)} 則新聞`,
    },
    {
      name: '市場情報中樞',
      status: intelligence.summary ? '就緒' : '部分資料',
      tone: intelligence.summary ? '' : 'warn',
      detail: `${openStockStatusLabel(intelligence.sentiment_label || 'neutral')} · ${openStockCount(intelligence.evidence)} 筆證據`,
    },
    {
      name: '策略研究',
      status: openStockStatusLabel(signal.action || 'hold'),
      tone: openStockTone(signal.action),
      detail: `${openStockPercent(signal.confidence)} 信心分數 · ${openStockCount(signal.source_modules)} 個模組`,
    },
    {
      name: '研究驗證',
      status: research.passed ? '通過' : '需檢視',
      tone: research.passed ? '' : 'warn',
      detail: `Sharpe ${research.sharpe ?? '-'} · 最大回撤 ${research.max_drawdown_pct ?? '-'} · Sortino ${performanceMetrics.sortino ?? '-'} · DSR ${significance.deflated_sharpe?.probability ?? '-'} · Alpha ${benchmarkMetrics.alpha_annualized_pct ?? '-'}% · Beta ${benchmarkMetrics.beta ?? '-'} · IR ${benchmarkMetrics.information_ratio ?? '-'} · CVaR ${monteCarloTailRisk.conditional_value_at_risk_loss_pct ?? '-'}% · Regime ${regimeRobustness.passed ? '通過' : '需檢視'} · Universe ${historicalUniverse.passed ? '通過' : '需檢視'}`,
    },
    {
      name: '風控閘門',
      status: risk.approved ? '已核准' : '已阻擋',
      tone: risk.approved ? '' : 'block',
      detail: neutralResearchText(risk.reason || '-'),
    },
    {
      name: '模擬執行',
      status: execution.executed ? '模擬委託' : '待命',
      tone: execution.executed ? '' : 'warn',
      detail: neutralResearchText(execution.order_id || execution.reason || execution.mode || '-'),
    },
  ];
  $('openStockFlowBox').innerHTML = steps.map(step => `
    <div class="process-step ${step.tone}">
      <span>${escapeHtml(step.status)}</span>
      <strong>${escapeHtml(step.name)}</strong>
      <small>${escapeHtml(step.detail)}</small>
    </div>`).join('');
}

function renderOpenStockSources(payload, storage = null) {
  const audit = arguments[2] || null;
  const signals = arguments[3] || null;
  const paperOrders = arguments[4] || null;
  const paperExposure = arguments[5] || null;
  const brokerImport = arguments[6] || null;
  const optionalSources = arguments[7] || null;
  const projects = payload?.projects || {};
  const contracts = payload?.contracts || {};
  const sourceLock = payload?.source_lock || audit?.external_source_lock || {};
  const optionalExternalSources = optionalSources || audit?.optional_external_sources || {};
  const licenseFootprint = audit?.external_license_footprint || {};
  const evidenceLineage = audit?.external_evidence_lineage || {};
  const aiTraderValidated = (paperOrders?.items || []).filter(item => item.ai_trader_valid === true).length;
  const aiTraderSignalValidated = (signals?.items || []).filter(item => item.ai_trader_valid === true).length;
  const tradingAgentsContract = contracts.tradingagents || {};
  const aiTraderContract = contracts.ai_trader || {};
  const finGptContract = contracts.fingpt || {};
  const finRlContract = contracts.finrl || {};
  const finRobotContract = contracts.finrobot || {};
  const qlibContract = contracts.qlib || {};
  const finGptForecast = audit?.fingpt_forecast_projection || {};
  const finRobotReport = audit?.finrobot_report_projection || {};
  const finRlBacktest = audit?.finrl_backtest_projection || {};
  const qlibFactor = audit?.qlib_factor_projection || {};
  const aiTraderInterop = audit?.ai_trader_interop_projection || {};
  const aiTraderSkillRoute = audit?.ai_trader_skill_route || {};
  const reflectionReplay = audit?.reflection_replay || {};
  const portfolioAttribution = audit?.portfolio_attribution || {};
  const runtimeConnectorGovernance = audit?.runtime_connector_governance || {};
  const brokerImportGovernance = brokerImport || audit?.broker_import_governance || {};
  const contributionMatrix = audit?.external_project_contribution_matrix || {};
  const outcomeAttribution = audit?.outcome_attribution || {};
  const requirementMatrix = audit?.requirement_matrix || {};
  const designSystemContract = audit?.design_system_contract || {};
  const cards = [
    renderWorkspaceCard('外部來源', `${payload?.verified_count ?? 0}/${payload?.count ?? 0}<br/>已驗證`, payload?.all_verified ? '已驗證所有 clone 來源' : '部分外部 repo 需要檢視', payload?.all_verified ? '' : 'warn'),
    renderWorkspaceCard(
      '來源鎖定',
      `${sourceLock?.lock_verified_count ?? payload?.lock_verified_count ?? 0}/${sourceLock?.count ?? payload?.count ?? 0}<br/>已鎖定`,
      `${contractLabel(sourceLock?.schema_version)} / git origin + HEAD`,
      sourceLock?.all_locked ?? payload?.all_locked ? '' : 'warn',
    ),
    renderWorkspaceCard(
      '選用來源',
      `${optionalExternalSources.excluded_count ?? 0}/${optionalExternalSources.optional_count ?? 0}<br/>已排除`,
      `${contractLabel(optionalExternalSources.schema_version)} / 未預期 ${optionalExternalSources.unexpected_clone_count ?? 0}`,
      optionalExternalSources.available ? '' : 'warn',
    ),
    renderWorkspaceCard(
      '設計系統',
      `${designSystemContract.satisfied_count ?? 0}/${designSystemContract.component_count ?? 0}<br/>契約`,
      `${contractLabel(designSystemContract.schema_version)} / 本地 UI`,
      designSystemContract.available ? '' : 'warn',
    ),
    renderWorkspaceCard(
      '儲存狀態',
      `${storage?.signals ?? 0}<br/>筆訊號`,
      `${contractLabel(storage?.schema_version)} / ${storage?.decision_logs ?? 0} 筆決策紀錄`,
    ),
    renderWorkspaceCard(
      '外部訊號',
      `${aiTraderSignalValidated}/${openStockCount(signals?.items)}<br/>有效`,
      `${contractLabel(signals?.schema_version)} / ${contractLabel(signals?.items?.[0]?.schema_version, '列資料 v1')}`,
    ),
    renderWorkspaceCard(
      '證據溯源',
      `${evidenceLineage.traced_count ?? 0}/${evidenceLineage.expected_count ?? 0}<br/>來源`,
      `${contractLabel(evidenceLineage.schema_version)} / 產物 ${evidenceLineage.artifact_count ?? 0}`,
      evidenceLineage.available ? '' : 'warn',
    ),
    renderWorkspaceCard(
      '訊號互通',
      `${aiTraderInterop.signal_valid ? '有效' : '需檢視'}<br/>訊號`,
      `${contractLabel(aiTraderInterop.schema_version)} / 模擬投影 ${aiTraderInterop.paper_order_projection_count ?? 0}`,
      aiTraderInterop.available ? '' : 'warn',
    ),
    renderWorkspaceCard(
      '策略路由',
      `${aiTraderSkillRoute.route_ready ? '就緒' : '需檢視'}<br/>${aiTraderSkillRoute.selected_skill_count ?? 0} 條路由`,
      `${contractLabel(aiTraderSkillRoute.schema_version)} / 來源路由`,
      aiTraderSkillRoute.available ? '' : 'warn',
    ),
    renderWorkspaceCard(
      '反思回放',
      `${reflectionReplay.available ? '就緒' : '需檢視'}<br/>${escapeHtml(neutralResearchText(reflectionReplay.dominant_cohort || '-'))}`,
      `${contractLabel(reflectionReplay.schema_version)} / ${reflectionReplay.risk_debator_count ?? 0} 個風險觀點`,
      reflectionReplay.available ? '' : 'warn',
    ),
    renderWorkspaceCard(
      '投組歸因',
      `${portfolioAttribution.available ? '就緒' : '需檢視'}<br/>${portfolioAttribution.symbol_count ?? 0} 檔標的`,
      `${contractLabel(portfolioAttribution.schema_version)} / 最高權重 ${escapeHtml(portfolioAttribution.top_symbol || '-')}`,
      portfolioAttribution.available ? '' : 'warn',
    ),
    renderWorkspaceCard(
      '結果歸因',
      `${outcomeAttribution.available ? '就緒' : '需檢視'}<br/>${outcomeAttribution.evaluated_count ?? 0} 筆評估`,
      `${contractLabel(outcomeAttribution.schema_version)} / 正向 ${openStockPercent(outcomeAttribution.positive_rate ?? 0)}`,
      outcomeAttribution.available ? '' : 'warn',
    ),
    renderWorkspaceCard(
      '資料來源',
      `${audit?.data_source_schema?.source_count ?? 0}<br/>封包`,
      `${contractLabel(audit?.data_source_schema?.schema_version)} / 品質 v1`,
      audit?.data_source_schema?.available ? '' : 'warn',
    ),
    renderWorkspaceCard(
      '整合稽核',
      audit?.ready ? '就緒' : '需檢視',
      `${audit?.invariants_passed ?? 0}/${audit?.invariants_total ?? 0} 項不變條件 / ${audit?.runtime?.paper_only ? '僅模擬交易' : '執行需檢視'}`,
      audit?.ready ? '' : 'warn',
    ),
    renderWorkspaceCard(
      '需求矩陣',
      `${requirementMatrix.satisfied_count ?? 0}/${requirementMatrix.requirement_count ?? 0}<br/>需求`,
      `${contractLabel(requirementMatrix.schema_version)} / 檢視 ${requirementMatrix.review_item_count ?? 0}`,
      requirementMatrix.available ? '' : 'warn',
    ),
    renderWorkspaceCard(
      '授權足跡',
      `${licenseFootprint.licensed_count ?? 0}/${licenseFootprint.project_count ?? 0}<br/>授權`,
      `${contractLabel(licenseFootprint.schema_version)} / 檢視 ${licenseFootprint.missing_license_count ?? 0}`,
      licenseFootprint.available ? '' : 'warn',
    ),
    renderWorkspaceCard(
      '執行期連接器',
      `${runtimeConnectorGovernance.blocked_count ?? 0}/${runtimeConnectorGovernance.connector_count ?? 0}<br/>已阻擋`,
      `${contractLabel(runtimeConnectorGovernance.schema_version)} / 遠端下單 ${runtimeConnectorGovernance.remote_order_submission_allowed ? '允許' : '關閉'}`,
      runtimeConnectorGovernance.available ? '' : 'warn',
    ),
    renderWorkspaceCard(
      '券商匯入',
      `${brokerImportGovernance.blocked_count ?? 0}/${brokerImportGovernance.connector_count ?? 0}<br/>已阻擋`,
      `${contractLabel(brokerImportGovernance.schema_version)} / 憑證 ${brokerImportGovernance.credential_count ?? 0}`,
      brokerImportGovernance.available ? '' : 'warn',
    ),
    renderWorkspaceCard(
      '外部貢獻',
      `${contributionMatrix.traced_count ?? 0}/${contributionMatrix.project_count ?? 0}<br/>專案`,
      `${contractLabel(contributionMatrix.schema_version)} / 繞過 ${contributionMatrix.remote_order_bypass_count ?? 0}`,
      contributionMatrix.available ? '' : 'warn',
    ),
    renderWorkspaceCard(
      '模擬交易帳本',
      `${paperOrders?.count ?? storage?.trades ?? 0}<br/>筆委託`,
      `${aiTraderValidated}/${openStockCount(paperOrders?.items)} 訊號結構 / ${contractLabel(paperOrders?.row_schema_version, '列資料 v1')}`,
    ),
    renderWorkspaceCard(
      '模擬曝險',
      `${openStockPointPercent(paperExposure?.total_position_size_pct ?? 0)}<br/>總計`,
      `${paperExposure?.symbol_count ?? 0} 檔標的 / ${contractLabel(paperExposure?.schema_version)}`,
    ),
    renderWorkspaceCard(
      '外部策略流程',
      projects.tradingagents?.head ? projects.tradingagents.head.slice(0, 12) : '-',
      `${tradingAgentsContract.schema_contract?.class_count ?? 0} 個結構 / ${tradingAgentsContract.graph_contract?.node_count ?? 0} 個圖節點 / ${tradingAgentsContract.risk_memory_contract?.risk_debator_count ?? 0} 個風險觀點`,
    ),
    renderWorkspaceCard(
      '外部研究模型',
      `${projects.fingpt?.head ? projects.fingpt.head.slice(0, 8) : '-'} / ${projects.finrobot?.head ? projects.finrobot.head.slice(0, 8) : '-'}`,
      `${finGptContract.benchmark_contract?.sentiment_template_count ?? 0} 個模板 / ${finGptContract.forecaster_contract?.prompt_functions?.length ?? 0} 個預測函式 / ${finRobotContract.agent_roles?.count ?? 0} 個角色 / ${finRobotContract.report_analysis_tools?.count ?? 0} 個工具`,
    ),
    renderWorkspaceCard(
      '情緒預測',
      `${openStockStatusHtml(finGptForecast.direction || '-')}<br/>${escapeHtml(finGptForecast.bin_label || '-')}`,
      `${contractLabel(finGptForecast.schema_version)} / 分數 ${finGptForecast.forecast_score ?? '-'}`,
      finGptForecast.available ? '' : 'warn',
    ),
    renderWorkspaceCard(
      '研究報告',
      `${openStockStatusHtml(finRobotReport.report_view || '-')}<br/>風險 ${openStockStatusHtml(finRobotReport.risk_view || '-')}`,
      `${contractLabel(finRobotReport.schema_version)} / 段落 ${finRobotReport.section_count ?? 0}`,
      finRobotReport.available ? '' : 'warn',
    ),
    renderWorkspaceCard(
      '回測與因子',
      `${projects.finrl?.head ? projects.finrl.head.slice(0, 8) : '-'} / ${projects.qlib?.head ? projects.qlib.head.slice(0, 8) : '-'}`,
      `${finRlContract.finrl_contract?.paper_example_count ?? 0} 個模擬範例 / ${finRlContract.finrl_trading_contract?.adaptive_rotation_file_count ?? 0} 個輪動檔 / ${qlibContract.workflow_contract?.workflow_count ?? 0} 個 qlib 工作流`,
    ),
    renderWorkspaceCard(
      '策略回測',
      `${finRlBacktest.passed ? '通過' : '需檢視'}<br/>Sharpe ${finRlBacktest.sharpe ?? '-'}`,
      `${contractLabel(finRlBacktest.schema_version)} / 回撤 ${finRlBacktest.max_drawdown_pct ?? '-'}`,
      finRlBacktest.available ? '' : 'warn',
    ),
    renderWorkspaceCard(
      '因子模型',
      `${qlibFactor.passed ? '通過' : '需檢視'}<br/>模型 ${qlibFactor.model_score ?? '-'}`,
      `${contractLabel(qlibFactor.schema_version)} / 排名 ${qlibFactor.rank_ic_proxy ?? '-'}`,
      qlibFactor.available ? '' : 'warn',
    ),
    renderWorkspaceCard(
      '外部交易訊號',
      projects.ai_trader?.head ? projects.ai_trader.head.slice(0, 12) : '-',
      `${aiTraderContract.schema_contract?.schema_count ?? 0} 個結構 / ${aiTraderContract.skill_contract?.skill_count ?? 0} 條路由`,
    ),
  ];
  $('openStockSourcesBox').innerHTML = cards.join('');
}

function renderOpenStockDecision(decision) {
  const review = arguments[1] || null;
  const request = decision.request || {};
  const snapshot = decision.market_snapshot || {};
  const intelligence = decision.intelligence || {};
  const signal = decision.signal || {};
  const research = decision.research || {};
  const risk = decision.risk || {};
  const execution = decision.execution || {};
  const intelligenceRaw = intelligence.raw || {};
  const researchRaw = research.raw || {};
  const benchmarkMetrics = research.benchmark_metrics || researchRaw.finrl?.benchmark_metrics || {};
  const monteCarloTailRisk = research.monte_carlo_tail_risk || researchRaw.finrl?.monte_carlo_tail_risk || {};
  const regimeRobustness = research.regime_robustness || researchRaw.finrl?.regime_robustness || {};
  const historicalUniverse = research.historical_universe || researchRaw.finrl?.historical_universe || {};
  const researchArtifacts = research.artifacts || researchRaw.artifacts || {};
  const unifiedAdapterResults = [
    ...(intelligence.adapter_results || []),
    ...(research.adapter_results || []),
  ];
  const decisionSchema = signal.decision_schema || {};
  const modelProvenance = decisionSchema.model_provenance || {};
  const scoreCalibration = decisionSchema.score_calibration || {};
  const modelRegistry = researchRaw.model_registry || {};
  const modelReproducibility = modelRegistry.experiment?.reproducibility || {};
  const signalValidation = signal.ai_trader_validation || {};
  const fingptProjection = intelligenceRaw?.fingpt?.forecast_projection || {};
  const finrobotProjection = intelligenceRaw?.finrobot?.report_projection || {};
  const finrlProjection = researchRaw?.finrl?.backtest_projection || {};
  const qlibProjection = researchRaw?.qlib?.factor_projection || {};
  const aiTraderInteropProjection = signal.ai_trader_interop_projection || {};
  const aiTraderSkillRoute = signal.ai_trader_skill_route || {};
  const reflectionReplay = review?.reflection_replay || {};
  const portfolioAttribution = review?.portfolio_attribution || {};

  $('openStockDecisionCards').innerHTML = [
    renderWorkspaceCard('請求', `${escapeHtml(request.symbol || '-')}<br/>${escapeHtml(request.market || '-')} / ${escapeHtml(request.horizon || '-')}`, '策略研究'),
    renderWorkspaceCard('決策結構', contractLabel(decision.schema_version), `${contractLabel(signal.schema_version)} / ${contractLabel(execution.schema_version)}`),
    renderWorkspaceCard('市場價格', snapshot.price === null || snapshot.price === undefined ? '-' : niceNumber(snapshot.price), `${openStockCount(snapshot.ohlcv)} 筆 OHLCV / ${openStockCount(snapshot.news)} 則新聞`),
    renderWorkspaceCard('交易訊號', `${openStockStatusHtml(signal.action || '-')}<br/>${openStockPercent(signal.confidence)}`, neutralResearchText(signal.reason || '-'), openStockTone(signal.action)),
    renderWorkspaceCard(
      '情緒預測',
      `${openStockStatusHtml(fingptProjection.direction || '-')}<br/>${escapeHtml(fingptProjection.bin_label || '-')}`,
      `${contractLabel(fingptProjection.schema_version)} / 分數 ${fingptProjection.forecast_score ?? '-'}`,
      fingptProjection.schema_version ? '' : 'warn',
    ),
    renderWorkspaceCard(
      '研究報告',
      `${openStockStatusHtml(finrobotProjection.report_view || '-')}<br/>風險 ${openStockStatusHtml(finrobotProjection.risk_view || '-')}`,
      `${contractLabel(finrobotProjection.schema_version)} / ${escapeHtml(finrobotProjection.valuation_view || '-')}`,
      finrobotProjection.schema_version ? '' : 'warn',
    ),
    renderWorkspaceCard(
      '研究驗證',
      research.passed ? '通過' : '需檢視',
      `Sharpe ${research.sharpe ?? '-'} / Sortino ${(research.performance_metrics || researchRaw.finrl?.performance_metrics)?.sortino ?? '-'} / DSR ${(research.statistical_significance || researchRaw.finrl?.statistical_significance)?.deflated_sharpe?.probability ?? '-'} / Alpha ${benchmarkMetrics.alpha_annualized_pct ?? '-'}% / Beta ${benchmarkMetrics.beta ?? '-'} / IR ${benchmarkMetrics.information_ratio ?? '-'} / CVaR ${monteCarloTailRisk.conditional_value_at_risk_loss_pct ?? '-'}% / Regime ${regimeRobustness.passed ? '通過' : '需檢視'} / Universe ${historicalUniverse.passed ? '通過' : '需檢視'} / CAGR ${(research.performance_metrics || researchRaw.finrl?.performance_metrics)?.cagr_pct ?? '-'}% / 轉接器 ${openStockCount(research.adapter_results)}`,
      research.passed ? '' : 'warn',
    ),
    renderWorkspaceCard(
      '策略回測',
      `${finrlProjection.passed ? '通過' : '需檢視'}<br/>Sharpe ${finrlProjection.metrics?.sharpe ?? '-'}`,
      `${contractLabel(finrlProjection.schema_version)} / 回撤 ${finrlProjection.metrics?.max_drawdown_pct ?? '-'}`,
      finrlProjection.schema_version ? '' : 'warn',
    ),
    renderWorkspaceCard(
      '因子模型',
      `${qlibProjection.passed ? '通過' : '需檢視'}<br/>模型 ${qlibProjection.model_score ?? '-'}`,
      `${contractLabel(qlibProjection.schema_version)} / 排名 ${qlibProjection.rank_ic_proxy ?? '-'}`,
      qlibProjection.schema_version ? '' : 'warn',
    ),
    renderWorkspaceCard(
      '模型版本',
      `${openStockCount(modelProvenance.model_version_ids)}<br/>${modelProvenance.runtime_status === 'executed' ? '已建立版本' : '未執行 runtime'}`,
      `${contractLabel(modelProvenance.schema_version)} / 實驗 ${escapeHtml(String(modelProvenance.experiment_id || '-').slice(0, 22))} / ${modelReproducibility.level === 'environment_captured' ? '環境已記錄' : '收據待補'} / 校準 ${scoreCalibration.status === 'calibrated' ? '已校準' : '未校準'} / 漂移 ${modelProvenance.drift_monitor_status || '未設定'} / 部署 ${modelProvenance.deployment_mode || 'none'}`,
      modelProvenance.runtime_status === 'executed' && modelProvenance.model_version_ids?.length ? '' : 'warn',
    ),
    renderWorkspaceCard(
      '研究產物',
      `${openStockCount(researchArtifacts.artifacts)}<br/>檔案`,
      contractLabel(researchArtifacts.schema_version),
    ),
    renderWorkspaceCard('風控閘門', risk.approved ? '已核准' : '已阻擋', neutralResearchText(risk.reason || '-'), risk.approved ? '' : 'warn'),
    renderWorkspaceCard('執行結果', execution.executed ? '模擬委託' : '未執行', neutralResearchText(execution.reason || execution.mode || '-')),
    renderWorkspaceCard(
      '決策回放',
      `${review?.total ?? 0}<br/>紀錄`,
      `${contractLabel(review?.schema_version)} / 核准率 ${openStockPercent(review?.approval_rate ?? 0)}`,
    ),
    renderWorkspaceCard(
      '反思回放',
      `${reflectionReplay.memory_ready ? '就緒' : '需檢視'}<br/>${escapeHtml(neutralResearchText(reflectionReplay.dominant_cohort || '-'))}`,
      `${contractLabel(reflectionReplay.schema_version)} / 經驗 ${reflectionReplay.lesson_count ?? 0}`,
      reflectionReplay.schema_version ? '' : 'warn',
    ),
    renderWorkspaceCard(
      '投組歸因',
      `${portfolioAttribution.available ? '就緒' : '需檢視'}<br/>${portfolioAttribution.symbol_count ?? 0} 檔標的`,
      `${contractLabel(portfolioAttribution.schema_version)} / 決策 ${portfolioAttribution.decision_count ?? 0}`,
      portfolioAttribution.schema_version ? '' : 'warn',
    ),
    renderWorkspaceCard(
      '訊號互通',
      `${aiTraderInteropProjection.valid ? '有效' : '需檢視'}<br/>${escapeHtml(neutralResearchText(aiTraderInteropProjection.artifact_type || '訊號'))}`,
      `${contractLabel(aiTraderInteropProjection.schema_version)} / 欄位 ${aiTraderInteropProjection.row_field_count ?? 0}`,
      aiTraderInteropProjection.schema_version ? '' : 'warn',
    ),
    renderWorkspaceCard(
      '策略路由',
      `${aiTraderSkillRoute.route_ready ? '就緒' : '需檢視'}<br/>${aiTraderSkillRoute.selected_skill_count ?? 0} 條路由`,
      `${contractLabel(aiTraderSkillRoute.schema_version)} / 來源路由`,
      aiTraderSkillRoute.schema_version ? '' : 'warn',
    ),
  ].join('');
  renderOpenStockFlow(decision);

  $('openStockSignalBox').innerHTML = `
    <div class="event">
      <h4>交易訊號 <span class="tag ${signal.action === 'buy' ? 'positive' : signal.action === 'hold' ? 'neutral' : 'negative'}">${openStockStatusHtml(signal.action || '-')}</span></h4>
      <p>${escapeHtml(neutralResearchText(signal.reason || '未回傳訊號原因。'))}</p>
      <p>評級 ${escapeHtml(decisionSchema.rating || '-')} / 交易員 ${openStockStatusHtml(decisionSchema.action || '-')} / 部位 ${openStockPointPercent(signal.position_size_pct)}</p>
      <p>外部訊號結構 ${signalValidation.valid ? '有效' : '需檢視'} / ${escapeHtml(signalValidation.valid ? '訊號列資料' : '需檢視結構')}</p>
      <p>訊號互通 ${aiTraderInteropProjection.valid ? '有效' : '需檢視'} / ${contractLabel(aiTraderInteropProjection.schema_version)} / 發布邊界 ${escapeHtml(neutralResearchText(aiTraderInteropProjection.publishing_boundary || '-'))}</p>
      <p>策略路由 ${aiTraderSkillRoute.route_ready ? '就緒' : '需檢視'} / ${aiTraderSkillRoute.selected_skill_count ?? 0} 條路由 / 邊界 ${escapeHtml(neutralResearchText(aiTraderSkillRoute.execution_boundary || '-'))}</p>
      <p>模型版本 ${openStockCount(modelProvenance.model_version_ids)} / runtime ${escapeHtml(modelProvenance.runtime_status || 'not_requested')} / 實驗 ${escapeHtml(String(modelProvenance.experiment_id || '-'))} / 校準 ${escapeHtml(scoreCalibration.status || 'unavailable')} / 漂移 ${escapeHtml(modelProvenance.drift_monitor_status || 'not_configured')} / 推廣 ${escapeHtml(modelProvenance.promotion_status || 'research_only_unpromoted')} / 部署 ${escapeHtml(modelProvenance.deployment_mode || 'none')} / 執行權限 ${escapeHtml(modelProvenance.execution_authority || 'none')}</p>
      <p>反思回放 ${reflectionReplay.memory_ready ? '就緒' : '需檢視'} / ${contractLabel(reflectionReplay.schema_version)} / ${escapeHtml(neutralResearchText(reflectionReplay.dominant_cohort || '-'))}</p>
      <p>投組歸因 ${portfolioAttribution.available ? '就緒' : '需檢視'} / ${contractLabel(portfolioAttribution.schema_version)} / 標的 ${portfolioAttribution.symbol_count ?? 0}</p>
      <p>進場 ${snapshot.price === null || snapshot.price === undefined ? '-' : niceNumber(signal.entry_price ?? snapshot.price)} / 目標 ${signal.target_price === null || signal.target_price === undefined ? '-' : niceNumber(signal.target_price)} / 停損 ${signal.stop_loss === null || signal.stop_loss === undefined ? '-' : niceNumber(signal.stop_loss)}</p>
      <p>模組 ${escapeHtml((signal.source_modules || []).map(sourceDisplayName).join('、') || '策略模組')}</p>
    </div>
    <div class="event">
      <h4>市場研判</h4>
      <p>${escapeHtml(neutralResearchText(intelligence.summary || '-'))}</p>
      <p>情緒 ${openStockStatusHtml(intelligence.sentiment_label || '-')} / 分數 ${intelligence.sentiment_score === null || intelligence.sentiment_score === undefined ? '-' : niceNumber(intelligence.sentiment_score)}</p>
      <p>情緒預測 ${openStockStatusHtml(fingptProjection.direction || '-')} / ${escapeHtml(fingptProjection.bin_label || '-')} / ${contractLabel(fingptProjection.schema_version)}</p>
      <p>研究報告 ${openStockStatusHtml(finrobotProjection.report_view || '-')} / 風險 ${openStockStatusHtml(finrobotProjection.risk_view || '-')} / ${contractLabel(finrobotProjection.schema_version)}</p>
    </div>`;

  $('openStockRiskBox').innerHTML = `
    <div class="event workspace-alert ${risk.approved ? 'info' : 'block'}">
      <h4>風控決策</h4>
      <p>${escapeHtml(neutralResearchText(risk.reason || '-'))}</p>
      <p>最大部位 ${openStockPointPercent(risk.max_position_size_pct)} / 調整後 ${openStockPointPercent(risk.adjusted_position_size_pct)}</p>
      <p>${contractLabel(risk.schema_version)} / 閘門 ${openStockCount(risk.gate_checks)}</p>
      ${renderSimpleList((risk.risk_notes || []).map(neutralResearchText), '未回傳風險備註。')}
    </div>
    <div class="event">
      <h4>執行結果</h4>
      <p>${escapeHtml(neutralResearchText(execution.reason || '-'))}</p>
      <p>模式 ${escapeHtml(neutralResearchText(execution.mode || '-'))} / 委託 ${escapeHtml(neutralResearchText(execution.order_id || '-'))}</p>
    </div>`;

  const adapterBlocks = unifiedAdapterResults.map((adapterResult) => {
    const projects = adapterResult.external_projects || {};
    const project = Object.values(projects)[0] || {};
    const files = (project.capability_files || []).slice(0, 3);
    const adapterModel = adapterResult.metrics?.model_provenance || {};
    return `
      <div class="event">
        <h4>${escapeHtml(sourceDisplayName(adapterResult.source_name || adapterResult.source_key || '轉接器'))}</h4>
        <p>${escapeHtml(neutralResearchText(adapterResult.summary || '轉接邊界已就緒。'))}</p>
        <p>證據 ${openStockCount(adapterResult.evidence)} / 狀態 ${openStockStatusHtml(adapterResult.status || 'placeholder')} / commit ${escapeHtml(project.head ? String(project.head).slice(0, 12) : '-')}</p>
        <p>方法 ${escapeHtml(methodDisplayName(adapterResult.method || 'metadata_only'))} / ${contractLabel(adapterResult.schema_version, '既有格式')}</p>
        ${adapterModel.provider ? `<p>模型輸出 ${adapterModel.model_output ? '已驗證 runtime' : '非模型輸出'} / runtime ${escapeHtml(adapterModel.runtime_status || '-')} / 執行權限 ${escapeHtml(adapterModel.execution_authority || 'none')}</p>` : ''}
        <p>鎖定 ${adapterResult.lock_verified ? '已驗證' : '需檢視'} / 預期 ${escapeHtml(project.expected_head ? String(project.expected_head).slice(0, 12) : '-')}</p>
        <p>${escapeHtml(project.origin || project.path ? `來源已鎖定並可追溯；${files.length} 個參考檔案` : '外部來源尚未驗證')}</p>
      </div>`;
  }).join('');
  $('openStockAdapterBox').innerHTML = adapterBlocks || renderEmptyBlock('尚無轉接器結果', '統一轉接器結果尚未提供。');

  $('openStockRawBox').textContent = JSON.stringify(sanitizeTechnicalValue({
    request,
    market_snapshot: {
      symbol: snapshot.symbol,
      market: snapshot.market,
      price: snapshot.price,
      ohlcv_count: openStockCount(snapshot.ohlcv),
      news_count: openStockCount(snapshot.news),
      announcements_count: openStockCount(snapshot.announcements),
    },
    intelligence,
    signal,
    research,
    risk,
    execution,
  }), null, 2);
}

function renderOpenStockSession(payload) {
  const items = payload?.items || [];
  const portfolio = payload?.portfolio_construction || {};
  const approved = items.filter(item => item?.risk?.approved === true).length;
  const executed = items.filter(item => item?.execution?.executed === true).length;
  const symbols = items.map(item => item?.request?.symbol).filter(Boolean).join(', ') || '-';
  const optimizer = portfolio.portfolio_optimization || {};
  const optimizerReady = optimizer.status === 'verified';
  const portfolioRisk = portfolio.portfolio_risk_constraints || {};
  const portfolioRiskReady = portfolioRisk.status === 'verified';
  const optimizerBlockers = [...(optimizer.blockers || []), ...(portfolioRisk.blockers || [])].slice(0, 2).join('、');
  const sharedRiskContext = payload?.portfolio_risk_context || {};
  const sharedRiskBlockers = (sharedRiskContext.blockers || []).slice(0, 2).join('、');
  const missingSharedRiskInput = sharedRiskBlockers.includes('portfolio_risk_materialization');
  const riskContextSummary = items.length === 0
    ? '資料待補：尚未設定批次觀察清單'
    : sharedRiskContext.status === 'withheld'
    ? `資料待補：${missingSharedRiskInput ? '缺少共享 PIT 投組風險資料' : sharedRiskBlockers || '共享 PIT 投組風險收據未通過驗證'}${sharedRiskBlockers ? `（${sharedRiskBlockers}）` : ''}`
    : optimizerReady && portfolioRiskReady
      ? '集中度、流動性與尾端風險已驗證'
      : `PIT 風控未就緒${optimizerBlockers ? `：${optimizerBlockers}` : ''}`;
  $('openStockSessionBox').innerHTML = [
    renderWorkspaceCard('批次工作階段', escapeHtml(sessionLabel(payload?.session) || '-'), payload?.schema_version || 'open_stock_ai.batch_session.v1'),
    renderWorkspaceCard('批次標的', `${openStockCount(items)}<br/>項目`, symbols),
    renderWorkspaceCard('批次風控', `${approved}/${openStockCount(items)}<br/>已核准`, `${executed}/${openStockCount(items)} 已執行`, approved === openStockCount(items) ? '' : 'warn'),
    renderWorkspaceCard(
      '投組建構',
      `${openStockPointPercent(portfolio.proposed_total_weight_pct ?? 0)}<br/>建議權重`,
      `${portfolio.schema_version || 'open_stock_ai.portfolio_construction.v5'} / ${escapeHtml(riskContextSummary)}`,
      portfolio.available && optimizerReady && portfolioRiskReady ? '' : 'warn',
    ),
  ].join('');
}

async function loadOpenStockAIView() {
  const symbol = normalizeWorkspaceSymbol($('openStockSymbol')?.value || state.symbol);
  const market = $('openStockMarket')?.value || 'TW';
  const horizon = $('openStockHorizon')?.value || 'swing';
  if ($('openStockSymbol')) $('openStockSymbol').value = symbol;
  if (!symbol) {
    renderWorkspaceError(
      ['openStockSignalBox', 'openStockRiskBox', 'openStockAdapterBox'],
      '請先輸入台股代號',
      '策略研究需要明確的 .TW 或 .TWO 股票代號。',
    );
    return;
  }
  updateTradeState({ symbol });
  renderWorkspaceError(['openStockSignalBox', 'openStockRiskBox', 'openStockAdapterBox'], '載入中', '正在執行策略研究與資料轉接...');
  $('openStockDecisionCards').innerHTML = '';
  if ($('openStockSessionBox')) $('openStockSessionBox').innerHTML = '';
  if ($('openStockSourcesBox')) $('openStockSourcesBox').innerHTML = '';
  if ($('openStockFlowBox')) $('openStockFlowBox').innerHTML = '';
  if ($('openStockRawBox')) $('openStockRawBox').textContent = '';
  // The core research result is the user's requested operation.  Auxiliary
  // evidence cards may involve optional external project inventory; they must
  // not keep the full native research workspace on a loading screen when one
  // of those non-critical calls is slow or unavailable.
  const decision = await api(
    `/api/open-stock-ai/analyze?symbol=${encodeURIComponent(symbol)}&market=${encodeURIComponent(market)}&horizon=${encodeURIComponent(horizon)}`,
  );
  try {
    renderOpenStockDecision(decision, {});
  } catch (error) {
    console.error('Open Stock AI core research render failed', error);
    renderWorkspaceError(
      ['openStockSignalBox', 'openStockRiskBox', 'openStockAdapterBox'],
      '研究結果無法顯示',
      `核心研究已完成，但結果卡渲染失敗：${error?.message || '未知前端錯誤'}`,
    );
    return;
  }
  const supplementary = await Promise.allSettled([
    api('/api/open-stock-ai/external-sources'),
    api('/api/open-stock-ai/storage'),
    api(`/api/open-stock-ai/decision-review?symbol=${encodeURIComponent(symbol)}&limit=50`),
    api(`/api/open-stock-ai/integration-audit?symbol=${encodeURIComponent(symbol)}&limit=50`),
    api('/api/open-stock-ai/signals?limit=20'),
    api('/api/open-stock-ai/paper-orders?limit=20'),
    api('/api/open-stock-ai/paper-exposure'),
    api('/api/open-stock-ai/broker-import-governance'),
    api('/api/open-stock-ai/optional-external-sources'),
  ]);
  const [sources, storage, review, audit, signals, paperOrders, paperExposure, brokerImport, optionalSources] = supplementary.map(
    item => item.status === 'fulfilled' ? item.value : {},
  );
  renderOpenStockSources(sources, storage, audit, signals, paperOrders, paperExposure, brokerImport, optionalSources);
  try {
    renderOpenStockDecision(decision, review);
  } catch (error) {
    console.error('Open Stock AI supplementary research render failed', error);
  }
}

async function loadOpenStockAISession() {
  const session = $('openStockSession')?.value || 'pre_market';
  if ($('openStockSessionBox')) {
    $('openStockSessionBox').innerHTML = renderWorkspaceCard('批次工作階段', '執行中', sessionLabel(session), 'warn');
  }
  const payload = await api(`/api/open-stock-ai/session/${encodeURIComponent(session)}`);
  renderOpenStockSession(payload);
}
