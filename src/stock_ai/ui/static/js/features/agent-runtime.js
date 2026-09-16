'use strict';

const AGENT_DRIVER_LABELS = {
  codex: 'Stock AI Agent · Codex',
  'openai-compatible': 'Stock AI Agent · OpenAI Compatible',
  'external-agent': 'Stock AI Agent · 外部 Agent',
};

function agentDriverLabel(driver) {
  const value = typeof driver === 'string' ? { id: driver } : (driver || {});
  return value.model
    ? `${AGENT_DRIVER_LABELS[value.id] || value.id} · ${value.model}`
    : AGENT_DRIVER_LABELS[value.id] || value.id || 'Stock AI Agent';
}

function activeAgentSettingsIdentity(settings) {
  const id = settings?.default_driver || 'codex';
  const config = id === 'openai-compatible'
    ? settings?.openai_compatible
    : id === 'external-agent'
      ? settings?.external_agent
      : settings?.codex;
  return { id, model: config?.model || '', reasoning_effort: config?.reasoning_effort || '' };
}

function applyAgentSettingsIdentity(settings) {
  const identity = activeAgentSettingsIdentity(settings);
  window.__stockAIActiveAgent = { ...identity, label: agentDriverLabel(identity) };
  document.dispatchEvent(new CustomEvent('stock-ai-active-agent-changed', { detail: window.__stockAIActiveAgent }));
  if ($('activeAgentLabel')) $('activeAgentLabel').textContent = agentDriverLabel(identity);
  updateAgentCodexPickerLabels();
  return identity;
}

function markAgentSettingsSaved() {
  state.agentSettingsRevision = (state.agentSettingsRevision || 0) + 1;
}

async function loadAgentRuntimeSettings() {
  const revision = state.agentSettingsRevision || 0;
  const requestId = (state.agentSettingsLoadRequestId || 0) + 1;
  state.agentSettingsLoadRequestId = requestId;
  const isCurrent = () => revision === (state.agentSettingsRevision || 0)
    && requestId === state.agentSettingsLoadRequestId;
  const currentSettings = () => ({ runtime: state.agentRuntime, settings: state.agentSettings });
  let runtime;
  let settings;
  try {
    [runtime, settings] = await Promise.all([api('/api/agents'), api('/api/agents/settings')]);
  } catch (error) {
    if (!isCurrent()) return currentSettings();
    if ($('settingsAgentBadge')) $('settingsAgentBadge').textContent = '載入失敗';
    if ($('agentSettingsStatus')) $('agentSettingsStatus').textContent = `模型提供者設定無法載入：${error.message || '請稍後再試。'}`;
    throw error;
  }
  // A successful save from either picker invalidates earlier reads. Their
  // stale data or errors must not relabel the model selected for the next Run.
  if (!isCurrent()) return currentSettings();
  state.agentRuntime = runtime;
  state.agentSettings = settings;
  // Opening System starts an asynchronous refresh.  A reply that started
  // before the user chose a provider must not replace that in-progress form.
  if (state.agentSettingsFormDirty) {
    updateAgentProviderPanels();
    return { runtime, settings };
  }
  const identity = applyAgentSettingsIdentity(settings);
  if ($('agentDefaultDriver')) $('agentDefaultDriver').value = settings.default_driver || 'codex';
  const openai = settings.openai_compatible || {};
  const external = settings.external_agent || {};
  renderAgentCodexModels({ model: settings.codex?.model || '', reasoningEffort: settings.codex?.reasoning_effort || '' });
  if ($('agentOpenAIBaseUrl')) $('agentOpenAIBaseUrl').value = openai.base_url || '';
  if ($('agentOpenAIModel')) $('agentOpenAIModel').value = openai.model || '';
  if ($('agentOpenAITimeout')) $('agentOpenAITimeout').value = String(openai.timeout_seconds || 120);
  if ($('agentOpenAINoKey')) $('agentOpenAINoKey').checked = openai.no_key === true;
  if ($('agentExternalEndpoint')) $('agentExternalEndpoint').value = external.endpoint || '';
  if ($('agentExternalFramework')) $('agentExternalFramework').value = external.framework || 'custom';
  if ($('agentExternalModel')) $('agentExternalModel').value = external.model || '';
  updateAgentProviderPanels();
  if ($('agentSettingsStatus')) $('agentSettingsStatus').textContent = '模型提供者設定已載入。';
  if ($('settingsAgentBadge')) $('settingsAgentBadge').textContent = `${agentDriverLabel(identity)} 已選用`;
  if (identity.id === 'codex' && !state.agentCodexModelStatus) void discoverAgentCodexModels();
  return { runtime, settings };
}

function updateAgentProviderPanels() {
  const selected = $('agentDefaultDriver')?.value || 'codex';
  document.querySelectorAll('[data-agent-provider-panel]').forEach(panel => {
    panel.hidden = panel.dataset.agentProviderPanel !== selected;
  });
}

async function saveAgentRuntimeSettings() {
  const payload = {
    default_driver: $('agentDefaultDriver')?.value || 'codex',
    codex_model: $('agentCodexModel')?.value || '',
    codex_reasoning_effort: $('agentCodexReasoningEffort')?.value || '',
    openai_base_url: $('agentOpenAIBaseUrl')?.value?.trim() || '',
    openai_model: $('agentOpenAIModel')?.value?.trim() || '',
    openai_timeout_seconds: Number($('agentOpenAITimeout')?.value || 120),
    openai_no_key: $('agentOpenAINoKey')?.checked === true,
    clear_openai_api_key: $('agentOpenAIClearKey')?.checked === true,
    external_endpoint: $('agentExternalEndpoint')?.value?.trim() || '',
    external_framework: $('agentExternalFramework')?.value?.trim() || 'custom',
    external_model: $('agentExternalModel')?.value?.trim() || '',
    clear_external_token: $('agentExternalClearToken')?.checked === true,
  };
  const openaiKey = $('agentOpenAIKey')?.value?.trim();
  const externalToken = $('agentExternalToken')?.value?.trim();
  if (openaiKey) payload.openai_api_key = openaiKey;
  if (externalToken) payload.external_token = externalToken;
  await api('/api/agents/settings', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  markAgentSettingsSaved();
  state.agentSettingsFormDirty = false;
  const result = await loadAgentRuntimeSettings();
  if ($('agentSettingsStatus')) $('agentSettingsStatus').textContent = 'Agent 設定已儲存，將套用於新任務。';
  return result;
}

async function testAgentProvider() {
  const selected = $('agentDefaultDriver')?.value || 'codex';
  const health = await api(`/api/agents/providers/${encodeURIComponent(selected)}/health`);
  if ($('agentSettingsStatus')) {
    const unavailable = health.reachable === false
      || health.configured === false
      || health.authenticated === false
      || health.healthy === false;
    const detail = typeof health.error === 'string' && health.error.trim()
      ? `：${health.error.trim()}`
      : '';
    $('agentSettingsStatus').textContent = unavailable
      ? `模型連線異常${detail || '；請確認服務位址、模型與驗證設定。'}`
      : '模型連線正常。';
  }
  return health;
}

function setGlobalAgentPopover(open) {
  if (window.AgentDockController) {
    open ? window.AgentDockController.open() : window.AgentDockController.close();
  }
  const legacy = $('globalAgentPopover');
  if (legacy) legacy.hidden = true;
  $('agentConsoleNav')?.setAttribute('aria-expanded', String(Boolean(open)));
}

async function runAutonomousAgent(objective) {
  if (!window.AgentDockController) throw new Error('Stock AI Agent Dock 尚未載入。');
  return window.AgentDockController.submit({ objective, source: 'runtime_adapter' });
}

window.AgentRuntimeAPI = {
  loadSettings: loadAgentRuntimeSettings,
  saveSettings: saveAgentRuntimeSettings,
  discoverModels: discoverAgentModels,
  discoverCodexModels: discoverAgentCodexModels,
  waitSettingsSaved: waitForAgentCodexSettingsSave,
  testProvider: testAgentProvider,
  submit: input => window.AgentDockController.submit(input),
};
