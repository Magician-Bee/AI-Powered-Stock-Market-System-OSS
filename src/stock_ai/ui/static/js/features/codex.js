// Production market-summary diagnostics use normal application logging only.
function marketSummaryDebugReport() {}
const CODEX_SKIP_STORAGE_KEY = 'stock-ai-codex-login-skipped';

function codexLoginWasSkipped() {
  return window.localStorage.getItem(CODEX_SKIP_STORAGE_KEY) === 'true';
}

function continueWithoutCodex() {
  window.localStorage.setItem(CODEX_SKIP_STORAGE_KEY, 'true');
  if ($('authGate')) $('authGate').hidden = true;
  document.documentElement.dataset.codexAuth = 'skipped';
  if ($('agentSettingsStatus')) {
    $('agentSettingsStatus').textContent = '尚未登入 Codex；可在設定中連接 Codex 或其他模型。';
  }
}

const UI_DATA_API_PREFIX = '/api/data/ui/v1';

function uiDataApi(path) {
  const suffix = String(path || '');
  return `${UI_DATA_API_PREFIX}${suffix.startsWith('/') ? suffix : `/${suffix}`}`;
}

async function api(path, options = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return await res.json();
}

function codexRequest(path, payload = {}) {
  return api(path, { method: 'POST', body: JSON.stringify(payload) });
}

function renderCodexAccount(account) {
  state.codexAccount = account;
  const authenticated = account?.authenticated === true;
  const skipped = codexLoginWasSkipped();
  const codexRequired = state.agentSettings?.default_driver === 'codex';
  if ($('authGate')) $('authGate').hidden = !codexRequired || authenticated || skipped;
  document.documentElement.dataset.codexAuth = authenticated
    ? 'authenticated'
    : (skipped ? 'skipped' : (codexRequired ? 'required' : 'not-selected'));
  const plan = String(account?.plan_type || '').toUpperCase();
  // This top-bar position identifies the active model operator on every
  // platform. Account identity remains available in Settings.
  if ($('accountLabel') && !state.agentSettings?.default_driver) $('accountLabel').textContent = 'Codex';
  if ($('accountAvatar') && !state.agentSettings?.default_driver) $('accountAvatar').textContent = 'C';
  if ($('codexRuntimeChip')) $('codexRuntimeChip').textContent = authenticated ? `${plan || 'ChatGPT'} · 已連線` : '尚未登入';
  if ($('authState')) $('authState').textContent = authenticated ? '登入完成，正在開啟系統…' : '請使用 ChatGPT 帳號登入 Codex。';
  if ($('settingsCodexStatus')) $('settingsCodexStatus').textContent = authenticated ? '已連線' : '未登入';
  if ($('settingsCodexAccount')) $('settingsCodexAccount').textContent = account?.email || (authenticated ? `${plan || 'ChatGPT'} 帳號` : '需要 ChatGPT 登入');
  if ($('settingsCodexIdentity')) $('settingsCodexIdentity').textContent = account?.email || (authenticated ? plan || 'ChatGPT' : '尚未登入');
  if ($('settingsCodexRuntime')) $('settingsCodexRuntime').textContent = account?.runtime || 'Codex App Server';
  if ($('settingsCodexProject')) $('settingsCodexProject').textContent = account?.project_root || '完整工作目錄';
  const badge = $('settingsCodexBadge');
  if (badge) {
    badge.textContent = authenticated ? '已連線' : '需要登入';
    badge.classList.toggle('ready', authenticated);
    badge.classList.toggle('warn', !authenticated);
  }
  if ($('settingsLoginCodex')) $('settingsLoginCodex').hidden = authenticated;
  if ($('logoutCodex')) $('logoutCodex').hidden = !authenticated;
  if ($('syncCodexProject')) $('syncCodexProject').disabled = !authenticated;
}

function renderSettingsStatusRows(items, emptyTitle, emptyDetail) {
  if (!items?.length) {
    return `<div class="settings-status-row"><span><strong>${escapeHtml(emptyTitle)}</strong><small>${escapeHtml(emptyDetail)}</small></span><i class="settings-status-dot warn"></i></div>`;
  }
  return items.map(item => `
    <div class="settings-status-row">
      <span><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.detail || item.source || '')}</small></span>
      <i class="settings-status-dot ${item.ready === false ? 'warn' : 'ready'}"></i>
    </div>`).join('');
}

function renderSkillsSettings(data) {
  const skills = data?.skills || {};
  const mcp = data?.mcp || {};
  const skillItems = (skills.items || []).map(item => ({ name: item.name, detail: `${item.source || '本機'} Skill`, ready: true }));
  const mcpItems = (mcp.items || []).map(name => ({ name, detail: `${mcp.transport || 'Codex'} · 已在本機設定`, ready: true }));
  if ($('settingsSkillCount')) $('settingsSkillCount').textContent = String(skills.count ?? 0);
  if ($('settingsMcpCount')) $('settingsMcpCount').textContent = String(mcp.count ?? 0);
  if ($('settingsSkillsBox')) $('settingsSkillsBox').innerHTML = renderSettingsStatusRows(skillItems, '尚未找到額外 Skill', 'Codex 仍可使用內建能力；新增 Skill 後會在這裡顯示。');
  if ($('settingsMcpBox')) $('settingsMcpBox').innerHTML = renderSettingsStatusRows(
    mcpItems,
    mcp.bridge_ready ? 'MCP 橋接已就緒' : 'MCP 尚未就緒',
    mcp.config_detected ? '設定檔已讀取，目前沒有列出伺服器。' : '尚未偵測到 MCP 伺服器設定。',
  );
  const badges = [
    ['settingsSkillsBadge', `${skills.count ?? 0} 個`, Number(skills.count || 0) > 0],
    ['settingsMcpBadge', mcp.bridge_ready ? `${mcp.count ?? 0} 個 · 橋接就緒` : '未就緒', mcp.bridge_ready === true],
  ];
  badges.forEach(([id, label, ready]) => {
    const badge = $(id);
    if (!badge) return;
    badge.textContent = label;
    badge.classList.toggle('ready', ready);
    badge.classList.toggle('warn', !ready);
  });
}

function renderConnectionSettings(data) {
  const apis = data?.apis || [];
  const apiItems = apis.map(item => ({ name: item.name, detail: `${item.provider || 'API'} · ${item.detail || ''}`, ready: item.configured === true }));
  if ($('settingsApiCount')) $('settingsApiCount').textContent = `${apis.filter(item => item.configured).length}/${apis.length}`;
  if ($('settingsApiBox')) $('settingsApiBox').innerHTML = renderSettingsStatusRows(apiItems, '尚無 API 狀態', '請重新檢查本機後端。');
  const badge = $('settingsApiBadge');
  if (badge) {
    const ready = apis.some(item => item.configured);
    badge.textContent = `${apis.filter(item => item.configured).length}/${apis.length} 已連線`;
    badge.classList.toggle('ready', ready);
    badge.classList.toggle('warn', !ready);
  }
}

function boundedSystemStatus(promise, milliseconds = 6000) {
  let timer = 0;
  return Promise.race([
    Promise.resolve(promise),
    new Promise((_, reject) => {
      timer = window.setTimeout(() => reject(new Error(`狀態服務未在 ${Math.round(milliseconds / 1000)} 秒內回應。`)), milliseconds);
    }),
  ]).finally(() => window.clearTimeout(timer));
}

async function loadSystemAgentPage(refresh = false) {
  const button = $('refreshAgentStatus');
  if (button) { button.disabled = true; button.textContent = '檢查中…'; }
  try {
    const [capabilityResult, accountResult] = await Promise.allSettled([
      boundedSystemStatus(api('/api/codex/capabilities')),
      boundedSystemStatus(loadCodexAccount(refresh)),
    ]);
    if (capabilityResult.status === 'fulfilled' && $('settingsComputerUse')) {
      $('settingsComputerUse').textContent = capabilityResult.value.computer_use_ready ? 'Computer Use 已就緒' : 'Computer Use 未就緒';
    }
    if (accountResult.status === 'rejected') {
      if ($('settingsCodexBadge')) $('settingsCodexBadge').textContent = '暫時無法檢查';
      if ($('codexProjectSummary')) $('codexProjectSummary').textContent = accountResult.reason?.message || 'Codex 帳號狀態暫時無法取得。';
    }
  } finally {
    if (button) { button.disabled = false; button.textContent = '重新檢查'; }
  }
}

async function loadSystemSkillsPage() {
  const button = $('refreshSkillsStatus');
  if (button) { button.disabled = true; button.textContent = '檢查中…'; }
  try {
    renderSkillsSettings(await boundedSystemStatus(api('/api/system/settings/skills')));
  } catch (error) {
    ['settingsSkillsBox', 'settingsMcpBox'].forEach(id => {
      if ($(id)) $(id).innerHTML = renderSettingsStatusRows([], '工具狀態讀取失敗', error.message || '請稍後再試。');
    });
  } finally {
    if (button) { button.disabled = false; button.textContent = '重新檢查'; }
  }
}

async function loadSystemConnectionsPage() {
  const button = $('refreshApiStatus');
  if (button) { button.disabled = true; button.textContent = '檢查中…'; }
  try {
    renderConnectionSettings(await boundedSystemStatus(api('/api/system/settings/connections')));
  } catch (error) {
    if ($('settingsApiBox')) $('settingsApiBox').innerHTML = renderSettingsStatusRows([], 'API 狀態讀取失敗', error.message || '請稍後再試。');
  } finally {
    if (button) { button.disabled = false; button.textContent = '重新檢查'; }
  }
}

async function loadCodexAccount(refresh = false) {
  const account = await api(`/api/codex/account${refresh ? '?refresh=true' : ''}`);
  renderCodexAccount(account);
  return account;
}

async function startCodexLogin(flow = 'browser') {
  window.localStorage.removeItem(CODEX_SKIP_STORAGE_KEY);
  if ($('authState')) $('authState').textContent = '正在建立安全登入工作階段…';
  const login = await codexRequest('/api/codex/login', { flow });
  state.codexLoginId = login.login_id;
  if (flow === 'device_code') {
    $('deviceCodeBox').hidden = false;
    $('deviceUserCode').textContent = login.user_code || '-';
    $('deviceVerificationLink').href = login.verification_url || '#';
    window.open(login.verification_url, '_blank', 'noopener');
  } else {
    window.open(login.auth_url, '_blank', 'noopener');
  }
  if ($('authState')) $('authState').textContent = '請在瀏覽器完成 ChatGPT 登入。';
  await pollCodexLogin(login.login_id);
}

async function pollCodexLogin(loginId) {
  for (let attempt = 0; attempt < 240; attempt += 1) {
    await new Promise(resolve => setTimeout(resolve, 1000));
    const status = await api(`/api/codex/login/${encodeURIComponent(loginId)}`);
    if (status.status === 'completed') {
      window.localStorage.removeItem(CODEX_SKIP_STORAGE_KEY);
      await loadCodexAccount(true);
      window.HomeMarketWorkspace?.load?.({ force: true, selectDefault: false }).catch(() => {});
      return;
    }
    if (['failed', 'cancelled', 'unknown'].includes(status.status)) {
      throw new Error(status.error || '登入未完成。');
    }
  }
  throw new Error('登入等待逾時，請重新嘗試。');
}

async function runCodexPrompt(promptOverride = null, computerUseOverride = null) {
  const prompt = String(promptOverride || $('codexPrompt')?.value || '').trim();
  if (!prompt) return;
  const computerUse = computerUseOverride ?? ($('codexComputerUse')?.checked === true);
  if ($('codexPrompt')) $('codexPrompt').value = prompt;
  const objective = computerUse ? `請使用 ui.* command bridge 直接操作目前介面並驗證結果：${prompt}` : prompt;
  if ($('globalAgentPrompt')) $('globalAgentPrompt').value = objective;
  if ($('homeAgentDockStatus')) {
    $('homeAgentDockStatus').textContent = computerUse
      ? '已送至右側 Agent Dock，正在透過 UI Bridge 操作。'
      : '已送至右側 Agent Dock 執行。';
  }
  window.AgentDockController.open();
  const result = await window.AgentDockController.submit({ objective, source: 'research_question' });
  if ($('homeAgentDockStatus')) {
    $('homeAgentDockStatus').textContent = '執行結果已顯示在右側 Agent Dock。';
  }
  return result;
}

async function syncCodexProject() {
  $('codexProjectSummary').textContent = 'Stock AI Agent 正在重新讀取完整專案…';
  const result = await window.AgentDockController.submit({
    objective: '完整讀取目前專案的 README、pyproject、config、src、tests 與 docs，回報主入口、資料、策略、風控、執行與 UI 的最新結構；不要修改檔案。',
    autonomy: 'advisory',
    source: 'project_sync',
  });
  $('codexProjectSummary').textContent = result.summary || '專案同步完成。';
}

async function logoutCodex() {
  await codexRequest('/api/codex/logout');
  renderCodexAccount({ authenticated: false });
}
