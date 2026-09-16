const BROKER_UI = Object.freeze({
  taishin: { name: '台新證券', short: 'TS', preferred: true },
  fubon: { name: '富邦證券', short: 'FB' },
  sinopac: { name: '永豐金證券', short: 'SP' },
  yuanta: { name: '元大證券', short: 'YT' },
  masterlink: { name: '元富證券', short: 'ML' },
});

const BROKER_ACTION_LABELS = Object.freeze({
  open_account: '完成開戶',
  sign_api_agreement: '簽署 API 使用同意',
  apply_certificate: '申請／更新憑證',
  create_api_key: '建立 API Key',
  complete_api_test: '完成券商測試',
  configure_fixed_ip: '設定固定 IP',
  contact_broker_representative: '聯絡券商服務人員',
});

function brokerStateLabel(status) {
  return {
    not_started: '尚未開始',
    requires_user_action: '需要本人操作',
    ready_for_readonly_probe: '等待唯讀驗證',
    readonly_verified: '唯讀已驗證',
    live_permission_recorded: '已記錄交易權限',
  }[status] || '狀態未知';
}

function renderBrokerConnections(connections, capabilities, health, runtime) {
  const grid = $('brokerConnectionGrid');
  if (!grid) return;
  const profiles = new Map((capabilities?.profiles || []).map(item => [item.broker_id, item]));
  const workers = new Map((health?.workers || []).map(item => [item.broker_id, item]));
  const runtimeMetrics = new Map((runtime?.metrics || []).map(item => [item.broker_id, item]));
  const brokers = connections?.brokers || [];
  grid.innerHTML = brokers.map(item => {
    const ui = BROKER_UI[item.broker_id] || { name: item.broker_id, short: 'API' };
    const profile = profiles.get(item.broker_id) || {};
    const worker = workers.get(item.broker_id) || {};
    const metrics = runtimeMetrics.get(item.broker_id) || {};
    const verified = item.state === 'readonly_verified' || item.state === 'live_permission_recorded';
    const actions = (item.required_actions || [])
      .map(action => BROKER_ACTION_LABELS[action] || action)
      .join('、') || '回到本頁進行唯讀能力探測';
    const sdkVersion = profile.sdk_version || worker.sdk_version;
    const sdk = sdkVersion && sdkVersion !== 'unverified'
      ? `SDK ${sdkVersion}`
      : 'SDK 尚未驗證';
    const primaryClass = ui.preferred ? ' is-preferred' : '';
    const preferredBadge = ui.preferred ? '<span class="broker-preferred-badge">首選接入</span>' : '';
    const readonlyPlanAction = ui.preferred
      ? '<button type="button" class="primary" data-show-taishin-readonly-plan>查看台新唯讀接入步驟</button>'
      : '';
    return `
      <article class="broker-connection-card${primaryClass}" data-broker-id="${escapeHtml(item.broker_id)}">
        <div class="settings-card-head">
          <span class="settings-card-icon">${escapeHtml(ui.short)}</span>
          <div><h5>${escapeHtml(ui.name)}${preferredBadge}</h5><p>${escapeHtml(sdk)} · 隔離程序 ${worker.process_running === true && worker.process_isolated === true ? '執行中' : '尚未啟動'}</p></div>
          <span class="settings-state-badge ${verified ? 'ready' : 'warn'}">${escapeHtml(brokerStateLabel(item.state))}</span>
        </div>
        <dl class="broker-connection-facts">
          <div><dt>目前狀態</dt><dd>${escapeHtml(brokerStateLabel(worker.state || item.state))}</dd></div>
          <div><dt>下一步</dt><dd>${escapeHtml(actions)}</dd></div>
          <div><dt>行情／帳務／交易</dt><dd>${verified ? '依真實探測結果顯示' : '全部保持未驗證'}</dd></div>
          <div><dt>Feed／延遲</dt><dd>${metrics.websocket_connected === true ? `已連線 · ${metrics.market_latency_ms == null ? '延遲未取得' : `${escapeHtml(String(metrics.market_latency_ms))} ms`}` : '尚無真實 Feed'}</dd></div>
        </dl>
        <div class="settings-card-actions">
          ${readonlyPlanAction}
          <button type="button" data-open-broker="${escapeHtml(item.broker_id)}">開啟官方申請頁</button>
        </div>
      </article>`;
  }).join('');
  grid.querySelectorAll('[data-open-broker]').forEach(button => {
    button.addEventListener('click', () => openBrokerOfficialPage(button.dataset.openBroker, button));
  });
  grid.querySelectorAll('[data-show-taishin-readonly-plan]').forEach(button => {
    button.addEventListener('click', () => showTaishinReadonlyPlan(button));
  });
  const verifiedCount = brokers.filter(item => ['readonly_verified', 'live_permission_recorded'].includes(item.state)).length;
  if ($('brokerGatewayStatus')) {
    $('brokerGatewayStatus').textContent = verifiedCount
      ? `${verifiedCount}/${brokers.length} 家已完成真實唯讀驗證；真實交易仍保持關閉。`
      : `0/${brokers.length} 家完成唯讀驗證；請由帳戶本人開始官方申請流程。`;
  }
}

async function showTaishinReadonlyPlan(button) {
  const panel = $('taishinReadonlyPlan');
  if (!panel) return;
  const original = button.textContent;
  button.disabled = true;
  button.textContent = '讀取步驟…';
  try {
    const plan = await api('/api/brokers/taishin/readonly-onboarding');
    const steps = (plan.steps || []).map((step, index) => `
      <li>
        <strong>${index + 1}. ${escapeHtml(step.title)}</strong>
        <span>${escapeHtml(step.detail)}</span>
        ${step.official_url ? `<a href="${escapeHtml(step.official_url)}" target="_blank" rel="noreferrer">官方說明</a>` : ''}
      </li>`).join('');
    panel.hidden = false;
    panel.innerHTML = `
      <div class="taishin-readonly-plan-head"><div><span class="broker-preferred-badge">台新優先路徑</span><h5>第一階段：唯讀行情接入</h5></div><span class="settings-state-badge warn">交易永遠關閉</span></div>
      <p>此設定只準備唯讀行情驗證；不讀取帳務、不建立委託、不送單，亦不接受任何帳密、OTP 或憑證內容。</p>
      <ol>${steps}</ol>`;
    panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    if ($('brokerGatewayStatus')) $('brokerGatewayStatus').textContent = '台新唯讀接入步驟已展開；需要本人操作的項目會明確停在官方頁與 macOS Keychain。';
  } catch (error) {
    if ($('brokerGatewayStatus')) $('brokerGatewayStatus').textContent = `無法讀取台新接入步驟：${error.message || '請稍後再試'}`;
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

async function loadBrokerConnections() {
  const button = $('refreshBrokerConnections');
  if (button) {
    button.disabled = true;
    button.textContent = '檢查中…';
  }
  try {
    const [connections, capabilities, health, runtime] = await Promise.all([
      api('/api/brokers/connections'),
      api('/api/brokers/capabilities'),
      api('/api/brokers/health'),
      api('/api/brokers/runtime'),
    ]);
    renderBrokerConnections(connections, capabilities, health, runtime);
  } catch (error) {
    if ($('brokerConnectionGrid')) {
      $('brokerConnectionGrid').innerHTML = `
        <article class="broker-connection-card is-error">
          <div class="settings-card-head"><span class="settings-card-icon">!</span><div><h5>券商閘道無法連線</h5><p>${escapeHtml(error.message || '請稍後再試')}</p></div><span class="settings-state-badge warn">讀取失敗</span></div>
        </article>`;
    }
    if ($('brokerGatewayStatus')) $('brokerGatewayStatus').textContent = '券商狀態讀取失敗；系統未嘗試任何登入或交易。';
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = '重新檢查';
    }
  }
}

async function openBrokerOfficialPage(brokerId, button) {
  if (!BROKER_UI[brokerId]) return;
  const original = button.textContent;
  button.disabled = true;
  button.textContent = '正在開啟…';
  try {
    const result = await api(`/api/brokers/${encodeURIComponent(brokerId)}/authorization/open`, {
      method: 'POST',
      body: JSON.stringify({ user_requested: true }),
    });
    if ($('brokerGatewayStatus')) {
      $('brokerGatewayStatus').textContent = result.opened
        ? `已開啟 ${BROKER_UI[brokerId].name} 官方頁面；請由帳戶本人完成要求，完成前系統不會自動繼續。`
        : `無法自動開啟 ${BROKER_UI[brokerId].name} 官方頁面，請重新嘗試。`;
    }
  } catch (error) {
    if ($('brokerGatewayStatus')) $('brokerGatewayStatus').textContent = `開啟失敗：${error.message || '請稍後再試'}`;
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

if ($('refreshBrokerConnections')) {
  $('refreshBrokerConnections').addEventListener('click', () => loadBrokerConnections());
}

window.loadBrokerConnections = loadBrokerConnections;
