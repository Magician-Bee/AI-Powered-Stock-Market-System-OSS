(() => {
  'use strict';

  function loadAgentTradingWorkspace() {
    if (!document.querySelector('link[data-agent-trading-workspace-style]')) {
      const link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = '/static/agent-trading-workspace.css?v=20260722-agent-spacing-v4';
      link.dataset.agentTradingWorkspaceStyle = 'true';
      document.head.appendChild(link);
    }
    if (!document.querySelector('script[data-agent-trading-workspace-script]')) {
      const script = document.createElement('script');
      script.src = '/static/agent-trading-workspace.js?v=20260722-agent-provider-v1';
      script.async = false;
      script.dataset.agentTradingWorkspaceScript = 'true';
      document.head.appendChild(script);
    }
  }

  loadAgentTradingWorkspace();

  const byId = (id) => document.getElementById(id);
  const escapeHtml = (value) => String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
  const number = (value) => Number(value || 0);
  const money = (value) => number(value).toLocaleString('zh-TW', { maximumFractionDigits: 2 });
  const currency = (value) => `NT$ ${money(value)}`;
  const signed = (value) => `${number(value) >= 0 ? '+' : ''}${money(value)}`;
  const percent = (value, digits = 2) => `${number(value).toFixed(digits)}%`;
  const orderSideLabel = (side) => ({ buy: '買進', sell: '賣出', short_sell: '融券放空', buy_to_cover: '買回回補' })[side] || String(side || '-');

  // Paper trading is a portfolio capability, not a seventh top-level workspace.
  const PAPER_VIEW_ID = 'portfolio';
  const AUTO_MARK_INTERVAL_MS = 5 * 60 * 1000;
  const LEGACY_BROKER_STYLE_TEST_TOKEN = '/static/paper-trading-broker-v4.css?v=20260715-paper-broker-v4';
  let activeEpisodeId = null;
  let autoMarkTimer = null;
  let accountCache = null;
  let previewCache = null;
  let navigationSyncFrame = 0;
  let accountRequestVersion = 0;
  let resetInFlight = false;
  let lastVerifiedAccount = null;
  let autonomousStatusRequest = null;
  let autonomousStatusTimer = null;

  function ensureStyles() {
    const styles = [
      ['/static/paper-trading-view.css?v=20260715-paper-trading-v3', 'paperTradingStyle'],
      ['/static/paper-trading-broker-v4.css?v=20260718-moving-switcher-v7', 'paperTradingBrokerStyle'],
    ];
    void LEGACY_BROKER_STYLE_TEST_TOKEN;
    styles.forEach(([href, key]) => {
      const selector = `link[data-${key}]`;
      const existing = document.querySelector(selector);
      if (existing) {
        if (existing.href !== new URL(href, window.location.href).href) existing.href = href;
        return;
      }
      const link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = href;
      link.dataset[key] = 'true';
      document.head.appendChild(link);
    });
  }

  function paperLabel() {
    return document.documentElement.lang === 'en' ? 'Paper Trading' : '模擬交易';
  }

  function uncachedPath(path) {
    const separator = path.includes('?') ? '&' : '?';
    return `${path}${separator}_paper_ui_ts=${Date.now()}`;
  }

  function request(path, options = {}) {
    const method = String(options.method || 'GET').toUpperCase();
    const url = method === 'GET' ? uncachedPath(path) : path;
    return fetch(url, {
      cache: 'no-store',
      credentials: 'same-origin',
      ...options,
      headers: {
        'Content-Type': 'application/json',
        'Cache-Control': 'no-cache, no-store, max-age=0',
        Pragma: 'no-cache',
        ...(options.headers || {}),
      },
    }).then(async (response) => {
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || result.error || `HTTP ${response.status}`);
      return result;
    });
  }

  function card(label, value, note = '', tone = '') {
    return `<div class="mini-card ${tone}"><span>${escapeHtml(label)}</span><strong>${value}</strong>${note ? `<small>${escapeHtml(note)}</small>` : ''}</div>`;
  }

  function setStatus(text, tone = '') {
    const node = byId('paperTrainingStatus');
    if (!node) return;
    node.textContent = text;
    node.dataset.tone = tone;
  }

  function currentActiveViewId() {
    return document.querySelector('body > .app-shell main .view.active')?.id || '';
  }

  function decorateNavigationButton(button) {
    if (!button) return;
    button.classList.remove('glass-button', 'glass-refract-control', 'ff-glass-button');
    button.classList.add('stock-nav-option');
    button.dataset.glassComponent = 'NavigationOption';
    delete button.dataset.glassRefraction;
    button.querySelectorAll(':scope > .glass-control-lens').forEach((lens) => lens.remove());
  }

  function syncNavigationMaterial() {
    const activeView = currentActiveViewId();
    const buttons = [...document.querySelectorAll('.sidebar .nav-btn[data-view]')];
    buttons.forEach((button) => {
      decorateNavigationButton(button);
      const selected = Boolean(activeView) && button.dataset.view === activeView;
      if (button.classList.contains('active') !== selected) button.classList.toggle('active', selected);
      if (selected) {
        button.dataset.selected = 'true';
        button.setAttribute('aria-current', 'page');
      } else {
        button.removeAttribute('data-selected');
        button.removeAttribute('aria-current');
      }
    });
  }

  function scheduleNavigationSync() {
    if (navigationSyncFrame) window.cancelAnimationFrame(navigationSyncFrame);
    navigationSyncFrame = window.requestAnimationFrame(() => {
      navigationSyncFrame = 0;
      syncNavigationMaterial();
    });
  }

  function initializeStatefulGlass() {
    const nav = document.querySelector('.sidebar .nav-menu');
    const main = document.querySelector('main.main');
    if (nav) {
      new MutationObserver(scheduleNavigationSync).observe(nav, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ['class', 'data-view'],
      });
    }
    if (main) {
      new MutationObserver(scheduleNavigationSync).observe(main, {
        subtree: true,
        attributes: true,
        attributeFilter: ['class'],
      });
    }
    new MutationObserver(scheduleNavigationSync).observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-ui-theme', 'data-ui-backdrop'],
    });
    document.addEventListener('click', (event) => {
      const navButton = event.target.closest('.sidebar .nav-btn[data-view]');
      if (navButton) {
        window.setTimeout(scheduleNavigationSync, 0);
        window.setTimeout(scheduleNavigationSync, 80);
      }
      const jump = event.target.closest('.settings-jumpbar button');
      if (jump) {
        jump.parentElement?.querySelectorAll('button').forEach((button) => {
          button.classList.toggle('active', button === jump);
        });
      }
    }, true);
    syncNavigationMaterial();
  }

  function buildPanel(panel) {
    if (panel.dataset.paperLayout === 'broker-v5') return;
    panel.dataset.paperLayout = 'broker-v5';
    panel.innerHTML = `
      <div class="panel-head">
        <div>
          <h3>模擬交易</h3>
          <p class="panel-copy">用完整委託流程管理虛擬帳戶、持倉、未成交委託與策略績效。</p>
        </div>
        <span class="chip">模擬帳戶</span>
      </div>

      <section id="autonomousPaperMonitor" class="paper-broker-card" aria-labelledby="autonomousPaperHeading">
        <div class="paper-broker-head">
          <div>
            <h4 id="autonomousPaperHeading">AI 自主紙上帳戶</h4>
            <small>追蹤 AI 的交易計畫、掛單與剩餘持倉；下方手動模擬交易使用另一個帳戶。</small>
          </div>
          <button id="autonomousPaperRefresh" type="button">更新自主帳戶狀態</button>
        </div>
        <p id="autonomousPaperStatus" class="paper-training-status" role="status" aria-live="polite">正在讀取自主帳戶…</p>
        <div id="autonomousPaperSummary" class="summary-cards paper-training-grid"></div>
        <div id="autonomousPaperReview" class="event-list"></div>
        <h4>自主交易計畫</h4>
        <div id="autonomousPaperPlans" class="table compact paper-position-table"></div>
        <h4>自主帳戶掛單</h4>
        <div id="autonomousPaperOrders" class="table compact paper-position-table"></div>
        <h4>自主帳戶實際持倉</h4>
        <div id="autonomousPaperPositions" class="table compact paper-position-table"></div>
        <div id="autonomousPaperAlerts" class="event-list" role="status" aria-live="polite"></div>
      </section>

      <h4>手動模擬帳戶</h4>
      <div id="paperTrainingSummary" class="summary-cards paper-training-grid"></div>

      <div class="paper-broker-layout">
        <div>
          <section class="paper-broker-card paper-order-ticket">
            <div class="paper-broker-head">
              <h4>委託下單</h4>
              <small>買進、賣出與空頭回補共用同一張委託單；融券放空必須附可驗證的借券 locate，沒有憑證會被拒絕。</small>
            </div>

            <div class="paper-side-switch" role="group" aria-label="買賣別">
            <button id="paperTrainingBuySide" type="button" data-side="buy" class="active">買進</button>
            <button id="paperTrainingSellSide" type="button" data-side="sell">賣出</button>
            <button id="paperTrainingShortSellSide" type="button" data-side="short_sell">融券放空</button>
            <button id="paperTrainingCoverSide" type="button" data-side="buy_to_cover">買回回補</button>
            </div>
            <input id="paperTrainingSide" type="hidden" value="buy" />

            <div class="paper-ticket-grid">
              <label class="paper-ticket-wide">股票代號
                <input id="paperTrainingSymbol" value="" placeholder="選擇或輸入股票代號" />
              </label>
              <label>委託種類
                <select id="paperTrainingOrderType">
                  <option value="market">市價</option>
                  <option value="limit">限價</option>
                  <option value="stop">停損</option>
                  <option value="stop_limit">停損限價</option>
                </select>
              </label>
              <label>交易單位
                <select id="paperTrainingLotType">
                  <option value="board_lot">整股／整張</option>
                  <option value="odd_lot">零股</option>
                </select>
              </label>
              <label><span id="paperTrainingQuantityLabel">張數</span>
                <input id="paperTrainingQuantity" type="number" min="1" step="1" value="1" />
              </label>
              <label>委託效期
                <select id="paperTrainingTimeInForce">
                  <option value="rod">ROD 當日有效</option>
                  <option value="ioc">IOC 立即成交否則取消</option>
                  <option value="fok">FOK 全部成交否則取消</option>
                </select>
              </label>
              <label>交易時段
                <select id="paperTrainingSession">
                  <option value="regular">盤中</option>
                  <option value="after_hours">盤後</option>
                </select>
              </label>
              <p id="paperTrainingExchangeRuleHint" class="paper-ticket-wide">整股盤中：一張 1,000 股；價格須符合交易所 tick。</p>
              <label class="paper-ticket-wide">到期時間（選填，ISO 8601 + 時區）
                <input id="paperTrainingExpiresAt" placeholder="2026-08-20T13:30:00+08:00" />
              </label>
              <label id="paperTrainingBorrowReceiptField" class="paper-ticket-wide" hidden>借券 locate 憑證（僅融券放空；JSON）
                <textarea id="paperTrainingBorrowReceipt" rows="5" placeholder='{"receipt_id":"…","source":"broker verified locate","verified_at":"…","expires_at":"…","available_quantity":1000,"annual_fee_bps":1600}'></textarea>
                <small>必須是帳戶可驗證的借券憑證；公開借券歷史不能當作真實可借量。</small>
              </label>
              <label id="paperTrainingLimitField" class="paper-price-field" hidden>限價
                <input id="paperTrainingLimitPrice" type="number" min="0.000001" step="0.01" placeholder="輸入限價" />
              </label>
              <label id="paperTrainingStopField" class="paper-price-field" hidden>停損觸發價
                <input id="paperTrainingStopPrice" type="number" min="0.000001" step="0.01" placeholder="輸入觸發價" />
              </label>
            </div>

            <div class="paper-preview-command">
              <button id="paperTrainingPreview" type="button">更新委託試算</button>
            </div>
            <div id="paperTrainingBrokerStatus" class="event-list"></div>
            <div id="paperTrainingPreviewCards" class="summary-cards paper-ticket-preview"></div>
            <div id="paperTrainingEstimateBox" class="event-list"></div>
            <label class="paper-training-policy">委託理由
              <textarea id="paperTrainingRationale" rows="4" placeholder="記錄進出場依據、預期與失效條件。"></textarea>
            </label>
            <div class="paper-order-actions">
              <button id="paperTrainingSubmitOrder" class="primary" type="button">送出買進委託</button>
              <button id="paperTrainingRefreshPrices" type="button">更新行情與撮合掛單</button>
            </div>
          </section>

          <section class="paper-broker-card">
            <div class="paper-broker-head">
              <h4>未成交委託</h4>
              <small>限價與停損單會顯示狀態，可隨時撤單。</small>
            </div>
            <div id="paperTrainingOpenOrders" class="table compact paper-order-table"></div>
          </section>

          <section class="paper-broker-card">
            <div class="paper-broker-head">
              <h4>成交紀錄</h4>
              <small>保留成交價、數量、費用、稅與現金變化。</small>
            </div>
            <div id="paperTrainingFills" class="table compact paper-fill-table"></div>
          </section>
        </div>

        <div>
          <section class="paper-broker-card">
            <div class="paper-broker-head">
              <h4>風險與資產連動</h4>
              <small>風險結果會記錄為學習證據，不阻止策略實驗。</small>
            </div>
            <div id="paperTrainingLinkedSummary" class="summary-cards paper-risk-summary"></div>
            <div id="paperTrainingPositions" class="table compact paper-position-table"></div>
            <div id="paperTrainingMeta" class="event-list"></div>
          </section>

          <section class="paper-broker-card paper-account-card">
            <div class="paper-broker-head">
              <h4>模擬帳戶</h4>
              <small>資金、委託、成交、持倉與損益會持久保存。</small>
            </div>
            <div class="paper-account-grid">
              <label>初始資金
                <input id="paperTrainingInitialCash" type="number" min="1" step="1000" value="10000" />
              </label>
              <button id="paperTrainingReset" type="button">重設帳戶</button>
              <label>回合 ID
                <input id="paperTrainingEpisodeId" readonly placeholder="尚未開始" />
              </label>
              <button id="paperTrainingStartEpisode" type="button">開始策略回合</button>
            </div>
            <label class="paper-training-policy">策略目標
              <input id="paperTrainingObjective" value="測試選股、進出場、委託與持倉管理" />
            </label>
            <div id="paperTrainingStatus" class="paper-training-status">正在讀取帳戶…</div>
          </section>

          <section class="paper-broker-card paper-governance-card">
            <div class="paper-broker-head">
              <h4>策略與模型治理</h4>
              <small>只顯示本機 SQLite 已核准 artifact；回滾必須選擇同一類別、留下原因與核准者。</small>
            </div>
            <div id="paperTrainingGovernanceStatus" class="event-list"></div>
            <div class="paper-governance-grid">
              <label>Artifact 類別
                <select id="paperTrainingRollbackScope">
                  <option value="strategy">策略</option>
                  <option value="model">模型</option>
                </select>
              </label>
              <label>核准者
                <input id="paperTrainingRollbackApprover" maxlength="200" placeholder="帳戶持有人或人工審核者" />
              </label>
              <label class="paper-governance-wide">回滾原因
                <input id="paperTrainingRollbackReason" maxlength="500" placeholder="例如：Shadow 回撤超過已核准門檻" />
              </label>
              <button id="paperTrainingRollbackArtifact" type="button">回滾上一個已核准版本</button>
            </div>
          </section>

          <section class="paper-broker-card">
            <div class="paper-broker-head">
              <h4>績效與學習紀錄</h4>
              <small>每次成功、失敗、撤單與未成交都保留在回合紀錄。</small>
            </div>
            <div class="paper-learning-layout">
              <div id="paperTrainingLearning" class="paper-training-ledger"></div>
              <label class="paper-training-policy">回合檢討
                <textarea id="paperTrainingReflection" rows="6" placeholder="記錄有效條件、錯誤判斷與下一輪調整。"></textarea>
              </label>
            </div>
            <div class="paper-learning-actions">
              <button id="paperTrainingEvaluate" type="button">計算本回合績效</button>
              <button id="paperTrainingCloseEpisode" type="button">結束回合</button>
              <button id="paperTrainingSaveReflection" type="button">保存檢討</button>
            </div>
          </section>
        </div>
      </div>`;
  }

  function ensureWorkspace() {
    ensureStyles();
    const panel = byId('paperTrainingPanel');
    const assetsView = byId('assets');
    if (!panel || !assetsView) return null;
    buildPanel(panel);
    return assetsView;
  }

  async function activateView() {
    window.setView?.('portfolio');
    window.setWorkspaceTab?.('portfolio', 'simulated');
    syncNavigationMaterial();
    const main = document.querySelector('main.main');
    if (main) main.scrollTop = 0;
    window.scrollTo({ top: 0, left: 0, behavior: 'auto' });
    window.__liquidGLRenderer__?.render?.();
    window.__glassSamplerController?.scheduleCapture?.(40);
    void loadAutonomousStatus();
    try {
      await loadAccount();
      await loadArtifactGovernance({ preserveStatus: true });
      if (hasOrderSymbol()) {
        await loadOrderPreview({ preserveStatus: true });
        setStatus('模擬帳戶與委託試算已載入。', 'ok');
      } else {
        setStatus('模擬帳戶已載入；請先選擇股票再進行委託試算。', 'ok');
      }
    } catch (error) {
      setStatus(error.message || String(error), 'error');
    }
  }

  function setSide(side) {
    const normalized = ['buy', 'sell', 'short_sell', 'buy_to_cover'].includes(side) ? side : 'buy';
    const hidden = byId('paperTrainingSide');
    if (hidden) hidden.value = normalized;
    ['buy', 'sell', 'short_sell', 'buy_to_cover'].forEach((item) => {
      const id = { buy: 'paperTrainingBuySide', sell: 'paperTrainingSellSide', short_sell: 'paperTrainingShortSellSide', buy_to_cover: 'paperTrainingCoverSide' }[item];
      byId(id)?.classList.toggle('active', normalized === item);
    });
    const submit = byId('paperTrainingSubmitOrder');
    if (submit) {
      const labels = { buy: '送出買進委託', sell: '送出賣出委託', short_sell: '送出融券放空委託', buy_to_cover: '送出買回回補委託' };
      submit.textContent = labels[normalized];
      submit.classList.toggle('sell-order', ['sell', 'short_sell'].includes(normalized));
    }
    const borrowField = byId('paperTrainingBorrowReceiptField');
    if (borrowField) borrowField.hidden = normalized !== 'short_sell';
    previewCache = null;
  }

  function updateTicketFields() {
    const orderTypeControl = byId('paperTrainingOrderType');
    const timeInForceControl = byId('paperTrainingTimeInForce');
    const sessionControl = byId('paperTrainingSession');
    const lotType = byId('paperTrainingLotType')?.value || 'board_lot';
    const session = sessionControl?.value || 'regular';
    const requiresLimitRod = lotType === 'odd_lot' || session === 'after_hours';
    if (requiresLimitRod) {
      if (orderTypeControl) orderTypeControl.value = 'limit';
      if (timeInForceControl) timeInForceControl.value = 'rod';
    }
    if (orderTypeControl) orderTypeControl.disabled = requiresLimitRod;
    if (timeInForceControl) timeInForceControl.disabled = requiresLimitRod;
    const orderType = orderTypeControl?.value || 'market';
    const limitField = byId('paperTrainingLimitField');
    const stopField = byId('paperTrainingStopField');
    if (limitField) limitField.hidden = !['limit', 'stop_limit'].includes(orderType);
    if (stopField) stopField.hidden = !['stop', 'stop_limit'].includes(orderType);
    if (byId('paperTrainingQuantityLabel')) {
      byId('paperTrainingQuantityLabel').textContent = lotType === 'odd_lot' ? '股數' : '張數';
    }
    const ruleHint = byId('paperTrainingExchangeRuleHint');
    if (ruleHint) {
      ruleHint.textContent = lotType === 'odd_lot'
        ? '台股零股：限價單、ROD；僅在零股集合競價回報時撮合。'
        : session === 'after_hours'
          ? '盤後定價：限價 ROD；等待盤後定價撮合回報。'
          : '整股盤中：一張 1,000 股；價格須符合交易所 tick。';
    }
    previewCache = null;
  }

  function currentOrderRequest() {
    const symbol = String(byId('paperTrainingSymbol')?.value || '').trim();
    if (!symbol) throw new Error('請輸入股票代號。');
    const selectedSide = byId('paperTrainingSide')?.value;
    const side = ['buy', 'sell', 'short_sell', 'buy_to_cover'].includes(selectedSide) ? selectedSide : 'buy';
    const orderType = byId('paperTrainingOrderType')?.value || 'market';
    const lotType = byId('paperTrainingLotType')?.value || 'board_lot';
    const quantity = Math.max(1, number(byId('paperTrainingQuantity')?.value));
    const requestBody = {
      symbol,
      side,
      order_type: orderType,
      time_in_force: byId('paperTrainingTimeInForce')?.value || 'rod',
      lot_type: lotType,
      session: byId('paperTrainingSession')?.value || 'regular',
      episode_id: byId('paperTrainingEpisodeId')?.value?.trim() || activeEpisodeId || null,
      actor: 'user',
      rationale: byId('paperTrainingRationale')?.value?.trim() || '',
    };
    if (lotType === 'odd_lot') requestBody.quantity_shares = quantity;
    else requestBody.quantity_lots = quantity;
    if (['limit', 'stop_limit'].includes(orderType)) {
      const limitPrice = number(byId('paperTrainingLimitPrice')?.value);
      if (limitPrice <= 0) throw new Error('請輸入限價。');
      requestBody.limit_price = limitPrice;
    }
    if (['stop', 'stop_limit'].includes(orderType)) {
      const stopPrice = number(byId('paperTrainingStopPrice')?.value);
      if (stopPrice <= 0) throw new Error('請輸入停損觸發價。');
      requestBody.stop_price = stopPrice;
    }
    const expiresAt = String(byId('paperTrainingExpiresAt')?.value || '').trim();
    if (expiresAt) requestBody.expires_at = expiresAt;
    if (side === 'short_sell') {
      const rawReceipt = String(byId('paperTrainingBorrowReceipt')?.value || '').trim();
      if (!rawReceipt) throw new Error('融券放空需要貼上帳戶可驗證的借券 locate 憑證。');
      try {
        requestBody.borrow_receipt = JSON.parse(rawReceipt);
      } catch (_error) {
        throw new Error('借券 locate 憑證必須是有效 JSON。');
      }
    }
    return requestBody;
  }

  function syncOfficialTradingControls(requestBody) {
    if (byId('tradingSymbol')) byId('tradingSymbol').value = requestBody.symbol;
    if (byId('tradingSide')) byId('tradingSide').value = requestBody.side;
    if (byId('tradingLots') && requestBody.quantity_lots) {
      byId('tradingLots').value = String(requestBody.quantity_lots);
    }
  }

  function orderTypeLabel(value) {
    return { market: '市價', limit: '限價', stop: '停損', stop_limit: '停損限價' }[value] || value || '-';
  }

  function statusLabel(value) {
    return {
      submitted: '已送出',
      acknowledged: '已確認',
      open: '掛單中',
      triggered: '已觸發',
      partially_filled: '部分成交',
      filled: '已成交',
      canceled: '已撤銷',
      replaced: '已替換',
      expired: '已到期',
      rejected: '已拒絕',
    }[value] || value || '-';
  }

  function renderPositionTable(account) {
    const box = byId('paperTrainingPositions');
    if (!box) return;
    const rows = (account?.positions || []).map((item) => `
      <div class="row">
        <div>${escapeHtml(item.symbol || '-')}<br/><small>${escapeHtml(item.market || '')}</small></div>
        <div>${money(item.quantity)} 股<br/><small>成本 ${currency(item.average_cost)}</small></div>
        <div>${currency(item.market_value)}<br/><small>${percent(item.position_size_pct || 0)}</small></div>
        <div class="${number(item.unrealized_pnl) >= 0 ? 'tw-red' : 'tw-green'}">${signed(item.unrealized_pnl)}<br/><small>已實現 ${signed(item.realized_pnl)}</small></div>
      </div>`).join('');
    box.innerHTML = `<div class="row header"><div>部位</div><div>股數 / 成本</div><div>市值 / 比例</div><div>損益</div></div>${rows || '<div class="event"><h4>尚無持倉</h4><p>買進成交後，持倉會顯示在這裡。</p></div>'}`;
  }

  function renderOpenOrders(items) {
    const box = byId('paperTrainingOpenOrders');
    if (!box) return;
    const rows = (items || []).filter((item) => item.is_open).map((item) => `
      <div class="row">
        <div>${escapeHtml(item.symbol)}<br/><small>${orderSideLabel(item.side)} · ${orderTypeLabel(item.order_type)}</small></div>
        <div>${money(item.filled_quantity || 0)} / ${money(item.requested_quantity)} 股<br/><small>剩餘 ${money(item.remaining_quantity || 0)} · ${escapeHtml(item.lot_type || '-')}</small></div>
        <div>${item.limit_price ? `限 ${money(item.limit_price)}` : '-'}<br/><small>${item.stop_price ? `停 ${money(item.stop_price)}` : ''}</small></div>
        <div><span class="paper-order-status ${escapeHtml(item.status)}">${statusLabel(item.status)}</span><br/><small>${escapeHtml(item.time_in_force || '')}${item.expires_at ? ` · 到期 ${escapeHtml(item.expires_at)}` : ''}</small></div>
        <div>${item.can_cancel ? `<button type="button" data-cancel-paper-order="${escapeHtml(item.order_id)}">撤單</button>` : ''}${item.can_replace && ['limit', 'stop_limit'].includes(item.order_type) ? `<button type="button" data-replace-paper-order="${escapeHtml(item.order_id)}" data-replace-paper-price="${escapeHtml(item.limit_price)}">改價</button>` : ''}</div>
      </div>`).join('');
    box.innerHTML = `<div class="row header"><div>委託</div><div>數量</div><div>價格</div><div>狀態</div><div>操作</div></div>${rows || '<div class="event"><h4>沒有未成交委託</h4><p>限價單或停損單尚未成交時會顯示在這裡。</p></div>'}`;
    box.querySelectorAll('[data-cancel-paper-order]').forEach((button) => {
      button.addEventListener('click', () => cancelOrder(button.dataset.cancelPaperOrder));
    });
    box.querySelectorAll('[data-replace-paper-order]').forEach((button) => {
      button.addEventListener('click', () => replaceOrder(button.dataset.replacePaperOrder, button.dataset.replacePaperPrice));
    });
  }

  function renderFills(items) {
    const box = byId('paperTrainingFills');
    if (!box) return;
    const rows = (items || []).map((item) => `
      <div class="row">
        <div>${escapeHtml(item.symbol)}<br/><small>${orderSideLabel(item.side)} · ${orderTypeLabel(item.order_type)}</small></div>
        <div>${money(item.quantity)} 股</div>
        <div>${currency(item.fill_price)}</div>
        <div>${currency(item.commission)}<br/><small>稅 ${currency(item.tax)}</small></div>
        <div class="${number(item.net_cash_delta) >= 0 ? 'tw-red' : 'tw-green'}">${signed(item.net_cash_delta)}<br/><small>${item.settlement_due_at ? `T+2 ${escapeHtml(item.settlement_status === 'settled' ? '已交割' : '待交割')}` : '即時紙上現金'}</small></div>
      </div>`).join('');
    box.innerHTML = `<div class="row header"><div>成交</div><div>數量</div><div>成交價</div><div>費稅</div><div>現金變化</div></div>${rows || '<div class="event"><h4>尚無成交</h4><p>成交後會保留完整費用與現金紀錄。</p></div>'}`;
  }

  function renderLearning(account) {
    const learning = account?.learning || {};
    const learningBox = byId('paperTrainingLearning');
    if (!learningBox) return;
    learningBox.innerHTML = `
      <div class="event">
        <h4>回合摘要</h4>
        <p>共 ${learning.episode_count || 0} 回合 · 已評估 ${learning.evaluated_episode_count || 0} · 正報酬 ${learning.positive_episode_count || 0} · 負報酬 ${learning.negative_episode_count || 0}</p>
        <p>累積績效 ${signed(learning.total_reward)} · 平均 ${signed(learning.average_reward)}</p>
      </div>`;
    const latest = (learning.episodes || [])[0];
    if (latest?.episode_id) {
      activeEpisodeId = latest.episode_id;
      if (byId('paperTrainingEpisodeId')) byId('paperTrainingEpisodeId').value = activeEpisodeId;
    }
  }

  function renderAccount(account, { forceInitialCashInput = false } = {}) {
    if (!account) return;
    accountCache = account;
    lastVerifiedAccount = account;
    const summary = byId('paperTrainingSummary');
    if (summary) {
      summary.innerHTML = [
        card('總資產', currency(account.total_equity), `初始 ${currency(account.initial_cash)}`),
        card('可用現金', currency(account.available_cash ?? account.cash_balance), `已結算 ${currency(account.settled_cash_balance ?? account.cash_balance)} · 待付 ${currency(account.unsettled_payable)}`),
        card('累積報酬', percent(account.total_return_pct || 0), `損益 ${signed(number(account.total_equity) - number(account.initial_cash))}`),
        card('待收交割', currency(account.unsettled_receivable), `${account.pending_settlement_count || 0} 筆 T+2 待交割`),
        card('委託 / 成交', `${account.order_count || 0} / ${account.fill_count || 0}`, `持倉 ${account.position_count || 0} 檔`),
      ].join('');
    }
    const initialCashInput = byId('paperTrainingInitialCash');
    if (initialCashInput && (forceInitialCashInput || initialCashInput.dataset.dirty !== 'true')) {
      initialCashInput.value = String(number(account.initial_cash));
      initialCashInput.dataset.dirty = 'false';
    }
    renderPositionTable(account);
    renderOpenOrders(account.open_orders || account.recent_orders || []);
    renderFills(account.recent_fills || []);
    renderLearning(account);
  }

  const autonomousValue = (value, formatter = money) => value == null || value === '' || !Number.isFinite(Number(value))
    ? '未提供' : formatter(value);
  const autonomousQuantity = (value) => autonomousValue(value, (amount) => `${money(amount)} 股`);
  const autonomousStateLabel = (state) => ({
    planned: '等待進場', waiting: '等待條件', entry_dispatching: '進場提交待確認', entry_submitted: '進場委託中', entry_partial: '進場部分成交',
    open: '持倉管理中', active: '持倉管理中', holding: '持倉管理中', exit_dispatching: '出場提交待確認', exit_submitted: '出場委託中', exit_partial: '出場部分成交',
    reconciliation_required: '需要對帳', closed: '已確認平倉', cancelled: '已取消', expired: '已到期',
    invalidated: '已失效', rejected: '已拒絕', completed: '已完成', partially_completed: '執行未完成',
    failed: '執行失敗', receipt_unavailable: '無法取得執行收據', submission_unknown: '提交結果待確認',
    dispatching: '正在提交', running: '執行中', queued: '等待執行',
  })[state] || state || '未提供';

  const autonomousWaitLabel = (reason) => ({
    exit_fill_overdue: '出場單等待過久，仍有持倉風險',
    exit_fill_timeout: '出場單等待過久，仍有持倉風險',
    exit_replacement_limit_reached: '已達重掛上限，仍有持倉風險',
    exit_price_floor_reached: '行情已低於允許的最低賣價',
    exit_cancellation_limit_reached: '多次撤單仍未確認，需要查明原單狀態',
    awaiting_exit_cancellation: '等待原出場單撤單確認',
    awaiting_entry_cancellation: '等待未成交買單撤單確認',
    awaiting_exit_fill: '等待出場成交',
    awaiting_entry_fill: '等待進場成交',
    exit_policy_not_configured: '出場單等待過久，尚未設定重掛政策',
    quote_ineligible: '行情不足以執行委託',
    exit_quote_ineligible: '行情不足以安全重掛出場單',
    invalid_broker_cost_estimate: '券商成本估計無效，尚未送出委託',
    awaiting_new_market_observation: '等待新的有效行情',
    broker_preview_or_frozen_budget: '委託條件或原資金預算尚未通過',
    risk_admission: '等待風險條件通過',
    monitoring_exit: '持續監控退出條件',
    waiting_time: '等待進場時間', waiting_price: '等待進場價格',
    limit_price_below_daily_limit: '限價低於當日允許價格範圍',
    limit_price_above_daily_limit: '限價高於當日允許價格範圍',
    limit_price_is_off_tick: '限價不符合市場價格跳動單位',
  })[reason] || reason || '';

  function renderAutonomousStatus(snapshot) {
    const account = snapshot?.account;
    const accountId = snapshot?.account_id;
    const plans = snapshot?.plans;
    const orders = account?.open_order_reservations;
    const positions = account?.positions;
    // A status payload must bind every displayed ledger row to the campaign account.
    // Never fall back to accountCache, which belongs to manual paper trading.
    if (!accountId || !account || account.account_id !== accountId || snapshot.mode !== 'paper'
      || !Array.isArray(plans) || !Array.isArray(orders) || !Array.isArray(positions)
      || [...plans, ...orders, ...positions].some((row) => row.account_id && row.account_id !== accountId)) {
      throw new Error('自主帳戶收據不完整或帳戶識別不一致，已停止顯示數值。');
    }
    const setHtml = (id, html) => { if (byId(id)) byId(id).innerHTML = html; };
    const amount = (value) => autonomousValue(value, (v) => `${account.base_currency || 'TWD'} ${money(v)}`);
    const activePlans = plans.filter((plan) => !['closed', 'cancelled', 'expired', 'invalidated', 'rejected'].includes(plan.state?.status || plan.status));
    const review = snapshot.model_review || {};
    const coverage = snapshot.security_research_coverage || {};
    const coverageReady = coverage.status === 'available';
    const deepCoverage = coverage.deep_research_status_counts || {};
    const domainCoverage = coverage.data_domain_counts || {};
    const latest = Array.isArray(review.reviews) ? review.reviews[0] : null;
    const selection = review.provider_model_selection || {};
    const enabled = snapshot.enabled === true ? '新進場已啟用' : snapshot.enabled === false ? '新進場已停止' : '啟用狀態未提供';
    const observed = snapshot.account_observed_at ? new Date(snapshot.account_observed_at) : null;
    const observedLabel = observed && !Number.isNaN(observed.getTime()) ? observed.toLocaleString('zh-TW') : '時間未提供';
    const status = byId('autonomousPaperStatus');
    if (status) {
      status.textContent = `${accountId} · ${enabled} · 帳戶快照 ${observedLabel}`;
      status.dataset.tone = 'ok';
    }
    setHtml('autonomousPaperSummary', [
      card('自主帳戶權益', escapeHtml(amount(account.total_equity)), '紙上資產'),
      card('自主可用現金', escapeHtml(amount(account.available_cash)), `帳面現金 ${amount(account.cash_balance)}`),
      card('管理中計畫 / 已載入計畫', `${activePlans.length} / ${plans.length}`, '計畫建立與成交分別追蹤'),
      card('掛單 / 持倉', `${orders.length} / ${positions.length}`, `已成交 ${autonomousValue(account.fill_count)} 筆`),
      card('逐檔覆蓋帳本', coverageReady ? `${autonomousValue(coverage.security_count)} 檔` : '尚未建立',
        coverageReady ? `可新增部位 ${autonomousValue(coverage.new_entry_eligible_count)} 檔` : '下次全市場研究會建立'),
    ].join(''));
    const reviewEnabled = review.enabled === true ? '已啟用' : review.enabled === false ? '已停止' : '未提供';
    setHtml('autonomousPaperReview', `<div class="event"><h4>模型複查 ${escapeHtml(reviewEnabled)}</h4>
      <p>${escapeHtml([selection.provider || review.driver, selection.model, selection.reasoning_effort].filter(Boolean).join(' · ') || '模型設定未提供')}</p>
      <p>${escapeHtml(review.budget_day || '日期未提供')} · 本日已用 ${escapeHtml(autonomousValue(review.used_today))} / ${escapeHtml(autonomousValue(review.daily_limit))} 次 · 剩餘 ${escapeHtml(autonomousValue(review.remaining_today))} 次 · 每次最多 ${escapeHtml(autonomousValue(review.max_steps))} 步</p>
      <p>${latest ? `最近複查：${escapeHtml(autonomousStateLabel(latest.status))}${latest.error || latest.reconciliation_error ? ` · ${escapeHtml(latest.error || latest.reconciliation_error)}` : ''}` : '尚無模型複查紀錄'}</p>
      ${latest?.run_id ? `<small>執行紀錄 ${escapeHtml(latest.run_id)}</small>` : ''}</div>
      <div class="event"><h4>逐檔研究與資料有效性</h4>
      ${coverageReady ? `<p>快照內深入 ${escapeHtml(autonomousValue(deepCoverage.current))} · 過期 ${escapeHtml(autonomousValue(deepCoverage.stale))} · 失敗 ${escapeHtml(autonomousValue(deepCoverage.failed))} · 尚未深入 ${escapeHtml(autonomousValue(deepCoverage.never_researched))}</p>
      <p>每日行情待更新 ${escapeHtml(autonomousValue(domainCoverage.daily_price?.needs_update))} · 歷史行情待更新 ${escapeHtml(autonomousValue(domainCoverage.price_history?.needs_update))} · 財務待更新 ${escapeHtml(autonomousValue(domainCoverage.financials?.needs_update))} · 新聞事件待更新 ${escapeHtml(autonomousValue(domainCoverage.news_events?.needs_update))}</p>
      <small>帳本時間 ${escapeHtml(coverage.observed_at || '未提供')} · 快照有效至 ${escapeHtml(coverage.universe_expires_at || '未提供')} · 有狀態不代表資料齊全或策略已驗證</small>`
      : '<p>尚無逐檔覆蓋快照；下次全市場研究會依官方身分建立，未選入深入研究者仍會保留尚未深入狀態。</p>'}</div>`);
    const planRows = plans.map((plan) => {
      const state = plan.state || {};
      const definition = plan.definition || {};
      return `<div class="row"><div>${escapeHtml(plan.symbol || definition.symbol || '未提供')}<br/><small>${escapeHtml(plan.plan_id)}</small></div>
        <div>${escapeHtml(autonomousStateLabel(state.status || plan.status))}<br/><small>${escapeHtml(autonomousWaitLabel(state.wait_reason))}</small></div>
        <div>剩餘 ${escapeHtml(autonomousQuantity(state.remaining_quantity))}<br/><small>已進場 ${escapeHtml(autonomousQuantity(state.filled_quantity))}</small></div>
        <div>停損 ${escapeHtml(amount(definition.stop_loss))}<br/><small>${definition.exit_not_after ? `最晚退出 ${escapeHtml(definition.exit_not_after)}` : `最長持有 ${escapeHtml(autonomousValue(definition.max_holding_seconds, (seconds) => `${money(Number(seconds) / 3600)} 小時`))}`}</small>
        ${definition.exit_order_policy ? `<br/><small>等待 ${escapeHtml(autonomousValue(definition.exit_order_policy.wait_seconds))} 秒後，最多重掛 ${escapeHtml(autonomousValue(definition.exit_order_policy.max_replacements))} 次；最低賣價 ${escapeHtml(amount(definition.exit_order_policy.minimum_limit_price))}</small>` : ''}</div></div>`;
    }).join('');
    setHtml('autonomousPaperPlans', `<div class="row header"><div>標的 / 計畫</div><div>執行狀態</div><div>已成交持倉</div><div>退出政策</div></div>${planRows || '<div class="event"><h4>尚未建立自主交易計畫</h4><p>目前帳本沒有計畫；研究與模型複查狀態列於上方。</p></div>'}`);
    const orderRows = orders.map((order) => `<div class="row"><div>${escapeHtml(order.symbol)}<br/><small>${escapeHtml(order.order_id)}</small></div>
      <div>${escapeHtml(orderSideLabel(order.side))}<br/><small>${escapeHtml(statusLabel(order.status))}</small></div>
      <div>待成交 ${escapeHtml(autonomousQuantity(order.remaining_quantity))}</div>
      <div>預占參考價 ${escapeHtml(amount(order.reservation_price))}<br/><small>${escapeHtml((order.blockers || []).join('、'))}</small></div></div>`).join('');
    setHtml('autonomousPaperOrders', `<div class="row header"><div>標的 / 委託</div><div>方向 / 狀態</div><div>剩餘股數</div><div>資金預占</div></div>${orderRows || '<div class="event"><p>自主帳戶目前沒有未成交掛單。</p></div>'}`);
    const positionRows = positions.map((position) => `<div class="row"><div>${escapeHtml(position.symbol)}</div>
      <div>${escapeHtml(autonomousQuantity(position.quantity))}</div><div>成本 ${escapeHtml(amount(position.average_cost))}</div>
      <div>市值 ${escapeHtml(amount(position.market_value))}</div></div>`).join('');
    setHtml('autonomousPaperPositions', `<div class="row header"><div>標的</div><div>實際持有股數</div><div>平均成本</div><div>持倉市值</div></div>${positionRows || '<div class="event"><p>自主帳戶目前沒有持倉。</p></div>'}`);
    const alerts = plans.filter((plan) => plan.state?.exit_alert).map((plan) => {
      const alert = plan.state.exit_alert;
      return `<div class="event"><h4>出場需要注意 · ${escapeHtml(plan.symbol || plan.definition?.symbol)}</h4>
        <p>${escapeHtml(autonomousWaitLabel(alert.reason) || '出場狀態需要確認')} · 剩餘 ${escapeHtml(autonomousQuantity(alert.remaining_quantity ?? plan.state.remaining_quantity))}</p>
        <p>委託 ${escapeHtml(alert.order_id || '尚未取得委託')} · 已重掛 ${escapeHtml(autonomousValue(alert.replacement_count))} 次</p>
        ${Array.isArray(alert.preview_blockers) && alert.preview_blockers.length ? `<p>阻擋原因：${escapeHtml(alert.preview_blockers.map(autonomousWaitLabel).join('、'))}</p>` : ''}</div>`;
    });
    if (snapshot.automatic_research?.error) alerts.push(`<div class="event"><h4>自主研究需要處理</h4><p>${escapeHtml(snapshot.automatic_research.error)}</p></div>`);
    setHtml('autonomousPaperAlerts', alerts.join(''));
  }

  function loadAutonomousStatus() {
    if (!byId('autonomousPaperMonitor')) return Promise.resolve();
    if (autonomousStatusRequest) return autonomousStatusRequest;
    const refresh = byId('autonomousPaperRefresh');
    if (refresh) refresh.disabled = true;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 15_000);
    autonomousStatusRequest = request('/agent/autonomy/status', { signal: controller.signal }).then(renderAutonomousStatus).catch((error) => {
      ['Summary', 'Review', 'Plans', 'Orders', 'Positions', 'Alerts'].forEach((part) => {
        const node = byId(`autonomousPaper${part}`);
        if (node) node.innerHTML = '';
      });
      const status = byId('autonomousPaperStatus');
      if (status) {
        status.textContent = `自主帳戶讀取失敗，數值暫不顯示：${error.message || String(error)}`;
        status.dataset.tone = 'error';
      }
    }).finally(() => {
      window.clearTimeout(timeout);
      autonomousStatusRequest = null;
      if (refresh) refresh.disabled = false;
    });
    return autonomousStatusRequest;
  }

  function startAutonomousStatusUpdates() {
    if (autonomousStatusTimer) window.clearInterval(autonomousStatusTimer);
    const refreshVisible = () => {
      if (!document.hidden && byId(PAPER_VIEW_ID)?.classList.contains('active')
        && byId('autonomousPaperMonitor')?.getClientRects().length) void loadAutonomousStatus();
    };
    autonomousStatusTimer = window.setInterval(refreshVisible, 30_000);
    document.addEventListener('visibilitychange', refreshVisible);
  }

  function renderOrderPreview(preview) {
    previewCache = preview;
    const ticket = preview.ticket || {};
    const market = preview.market || {};
    const costs = preview.estimated_costs || {};
    const costEvidence = preview.cost_evidence || {};
    const risk = preview.risk_advisory || {};
    const validation = preview.validation || {};
    const restrictions = preview.trading_restrictions || {};
    const settlement = preview.settlement || {};

    const status = byId('paperTrainingBrokerStatus');
    if (status) {
      status.innerHTML = `<div class="event"><h4>${escapeHtml(market.name || ticket.symbol || '委託試算')}</h4><p>${orderSideLabel(ticket.side)} ${money(ticket.requested_quantity)} 股 · ${orderTypeLabel(ticket.order_type)} · ${escapeHtml(ticket.time_in_force || '')}</p></div>`;
    }
    const cards = byId('paperTrainingPreviewCards');
    if (cards) {
      cards.innerHTML = [
        card('目前價格 / 預估成交', `${currency(market.price)}<br/>${currency(preview.estimated_fill_price)}`, market.name || ticket.symbol || ''),
        card('成交金額', currency(costs.gross_amount), `${money(ticket.requested_quantity)} 股`),
        card('費用與稅', currency(number(costs.commission) + number(costs.tax)), `本機紙上假設 · 手續費 ${currency(costs.commission)} / 稅 ${currency(costs.tax)}`),
        card('成本證據', costEvidence.execution_evidence_eligible ? '已驗證' : '本機模擬', costEvidence.execution_evidence_eligible ? '帳戶費率與校準收據已綁定' : '非券商帳戶費率或實盤成交證據'),
        card('預估收付', currency(costs.estimated_total), ['sell', 'short_sell'].includes(ticket.side) ? '預估入帳' : '預估扣款'),
        card('交割', settlement.enforced ? 'T+2' : '即時模擬', settlement.settlement_due_at ? `預計 ${escapeHtml(settlement.settlement_due_at)}` : '沒有交易所結算收據'),
      ].join('');
    }
    const expectation = {
      fill_now: '目前條件可直接成交。',
      rest_as_open_order: '委託會進入未成交委託，等待價格條件。',
      wait_for_stop_trigger: '委託會等待停損觸發價。',
      cancel_if_not_immediately_marketable: '若不能立即成交，IOC／FOK 會取消。',
      blocked_by_trading_restriction: '因官方交易限制，這筆委託無法送出或成交。',
      wait_for_exchange_session: '委託已通過帳戶檢查，等待正確的交易時段或集合競價成交回報。',
    }[preview.execution_expectation] || '';
    const estimate = byId('paperTrainingEstimateBox');
    if (estimate) {
      const limitText = restrictions.price_limits?.limit_up || restrictions.price_limits?.limit_down
        ? `漲停 ${currency(restrictions.price_limits?.limit_up)} · 跌停 ${currency(restrictions.price_limits?.limit_down)}`
        : '行情未提供當日漲跌停界線';
      const marketRules = preview.market_rules || {};
      const exchangeText = marketRules.session?.name
        ? `交易規則：${escapeHtml(marketRules.session.name)} · ${marketRules.matching_allowed ? '可撮合' : '等待撮合'} · tick ${escapeHtml(marketRules.tick_size || '-')}`
        : '交易規則：尚未取得交易所時段收據';
      const assumptions = costEvidence.assumptions || {};
      const missing = (costEvidence.blockers || []).join('、');
      const costText = costEvidence.execution_evidence_eligible
        ? '成本已綁定帳戶費率與市場衝擊校準收據。'
        : `成本僅為本機紙上設定：手續費 ${number(assumptions.commission_bps)} bps、賣出稅 ${number(assumptions.sell_tax_bps)} bps、滑價 ${number(assumptions.slippage_bps)} bps；非券商帳戶費率或實盤成交證據。`;
      estimate.innerHTML = `<div class="event"><h4>委託結果預估</h4><p>${escapeHtml(expectation)}</p><p>${validation.valid ? '帳戶、持倉與交易限制檢查通過。' : `無法送單：${escapeHtml(validation.reason || '委託內容不完整')}`}</p><p>${escapeHtml(costText)}</p>${missing ? `<p>成本證據缺口：${escapeHtml(missing)}</p>` : ''}<p>${escapeHtml(limitText)}</p><p>${exchangeText}</p></div>`;
    }
    const linked = byId('paperTrainingLinkedSummary');
    if (linked) {
      linked.innerHTML = [
        card('可用現金', currency(preview.account?.available_cash ?? preview.account?.cash_balance), `已結算 ${currency(preview.account?.settled_cash_balance ?? preview.account?.cash_balance)}`),
        card('帳戶檢查', validation.valid ? '可送委託' : '無法送單', validation.reason || '現金與持倉足夠'),
        card('單一股票曝險', percent(risk.symbol_exposure_percent || 0), `上限 ${percent(risk.max_symbol_exposure_percent || 0)}`),
        card('總曝險', percent(risk.total_exposure_percent || 0), `上限 ${percent(risk.max_total_exposure_percent || 0)}`),
      ].join('');
    }
    const meta = byId('paperTrainingMeta');
    if (meta) {
      const alerts = risk.alerts || [];
      const restrictionLabels = {
        attention_stock: '注意股票',
        disposition_stock: '處置股票',
        halt_trading: '停止交易',
        resume_trading: '恢復交易',
        price_limit: '漲跌停限制',
      };
      const restrictionCards = (restrictions.active_restrictions || []).map((item) => `
        <div class="event">
          <h4>${escapeHtml(restrictionLabels[item.restriction_type] || item.restriction_type || '交易限制')}</h4>
          <p>${escapeHtml(item.effective_from || '')}${item.effective_until ? ` 至 ${escapeHtml(item.effective_until)}` : ''}</p>
          <p><a href="${escapeHtml(item.source_url || '#')}" target="_blank" rel="noopener">官方來源</a></p>
        </div>`).join('');
      const blockerCards = (restrictions.blockers || []).map((item) => `
        <div class="event"><h4>交易限制阻擋</h4><p>${escapeHtml(item.code || item.message || '')}</p></div>`).join('');
      const warningCards = (restrictions.warnings || []).map((item) => `
        <div class="event"><h4>交易限制提醒</h4><p>${escapeHtml(item.code || item.message || '')}</p></div>`).join('');
      const riskCards = alerts.map((alert) => `<div class="event"><h4>${escapeHtml(alert.title || '風險提示')}</h4><p>${escapeHtml(alert.message || '')}</p></div>`).join('');
      meta.innerHTML = restrictionCards || blockerCards || warningCards || riskCards
        ? `${blockerCards}${warningCards}${restrictionCards}${riskCards}`
        : '<div class="event"><h4>風險摘要</h4><p>目前委託沒有額外風險或交易限制提示。</p></div>';
    }
  }

  async function loadAccount({ refreshPrices = false, preserveStatus = false } = {}) {
    const requestVersion = accountRequestVersion;
    if (!preserveStatus) setStatus(refreshPrices ? '正在更新行情並撮合掛單…' : '正在讀取模擬帳戶…');
    const account = refreshPrices
      ? (await request('/api/open-stock-ai/agent/paper-training/mark-to-market', { method: 'POST', body: '{}' })).account
      : await request('/api/open-stock-ai/agent/paper-training/account');
    if (requestVersion !== accountRequestVersion || resetInFlight) return account;
    renderAccount(account);
    if (!preserveStatus) {
      setStatus(refreshPrices ? '行情、掛單與帳戶市值已更新。' : '模擬帳戶已載入。', 'ok');
    }
    document.dispatchEvent(new CustomEvent('stock-ai-paper-account-updated', { detail: account }));
    return account;
  }

  function renderArtifactGovernance(governance) {
    const node = byId('paperTrainingGovernanceStatus');
    if (!node) return;
    const scopes = governance?.scopes || {};
    const currentScope = byId('paperTrainingRollbackScope')?.value || 'strategy';
    const strategy = scopes.strategy || {};
    const model = scopes.model || {};
    const active = scopes[currentScope] || {};
    node.innerHTML = [
      `<div class="event"><h4>策略</h4><p>${escapeHtml(strategy.current_artifact_id || '尚無已啟用策略版本')}</p><p>${Number(strategy.activation_count || 0)} 次啟用紀錄</p></div>`,
      `<div class="event"><h4>模型</h4><p>${escapeHtml(model.current_artifact_id || '尚無已啟用模型版本')}</p><p>${Number(model.activation_count || 0)} 次啟用紀錄</p></div>`,
      `<div class="event"><h4>目前可回滾類別</h4><p>${escapeHtml(currentScope === 'model' ? '模型' : '策略')}：${escapeHtml(active.current_artifact_id || '沒有可回滾版本')}</p><p>回滾收據 ${Number(governance?.receipt_count || 0)} 筆</p></div>`,
    ].join('');
    const button = byId('paperTrainingRollbackArtifact');
    if (button) button.disabled = Number(active.activation_count || 0) < 2;
  }

  async function loadArtifactGovernance({ preserveStatus = false } = {}) {
    const governance = await request('/api/open-stock-ai/agent/paper-training/governance/artifacts');
    renderArtifactGovernance(governance);
    if (!preserveStatus) setStatus('已讀取策略與模型的本機治理版本。', 'ok');
    return governance;
  }

  async function rollbackArtifact() {
    const artifactScope = byId('paperTrainingRollbackScope')?.value || 'strategy';
    const approvedBy = byId('paperTrainingRollbackApprover')?.value?.trim();
    const reason = byId('paperTrainingRollbackReason')?.value?.trim();
    if (!approvedBy) throw new Error('請輸入人工核准者。');
    if (!reason) throw new Error('請輸入回滾原因。');
    const label = artifactScope === 'model' ? '模型' : '策略';
    if (!window.confirm(`確定回滾${label}至上一個已核准版本？此操作會留下不可變回滾收據。`)) return;
    const result = await request('/api/open-stock-ai/agent/paper-training/governance/artifacts/rollback', {
      method: 'POST',
      body: JSON.stringify({ artifact_scope: artifactScope, approved_by: approvedBy, reason }),
    });
    renderArtifactGovernance(result.governance);
    setStatus(`${label}已回滾至 ${result.receipt?.to_artifact_id || '上一個已核准版本'}。`, 'ok');
  }

  async function loadOrderPreview({ preserveStatus = false } = {}) {
    const requestBody = currentOrderRequest();
    syncOfficialTradingControls(requestBody);
    const preview = await request('/api/open-stock-ai/agent/paper-training/preview', {
      method: 'POST',
      body: JSON.stringify(requestBody),
    });
    renderOrderPreview(preview);
    if (!preserveStatus) setStatus('委託試算已更新。', 'ok');
    return preview;
  }

  function hasOrderSymbol() {
    return Boolean(String(byId('paperTrainingSymbol')?.value || '').trim());
  }

  async function resetAccount() {
    const input = byId('paperTrainingInitialCash');
    const button = byId('paperTrainingReset');
    const amount = number(input?.value);
    if (!Number.isFinite(amount) || amount <= 0) throw new Error('請輸入大於 0 的初始資金。');
    if (!window.confirm(`確定將模擬帳戶重設為 ${currency(amount)}？所有委託、成交與學習紀錄都會清除。`)) return;

    const resetVersion = ++accountRequestVersion;
    resetInFlight = true;
    if (button) button.disabled = true;
    setStatus(`正在把模擬帳戶重設為 ${currency(amount)}…`);
    try {
      const result = await request('/api/open-stock-ai/agent/paper-training/reset', {
        method: 'POST',
        body: JSON.stringify({ initial_cash: amount }),
      });
      if (resetVersion !== accountRequestVersion) return;
      const returnedAccount = result.account || {};
      if (Math.abs(number(returnedAccount.initial_cash) - amount) > 0.005
        || Math.abs(number(returnedAccount.cash_balance) - amount) > 0.005
        || Math.abs(number(returnedAccount.total_equity) - amount) > 0.005) {
        throw new Error(`伺服器沒有完成資金重設：初始 ${currency(returnedAccount.initial_cash)}、現金 ${currency(returnedAccount.cash_balance)}、總資產 ${currency(returnedAccount.total_equity)}`);
      }

      activeEpisodeId = result.episode?.episode_id || null;
      if (byId('paperTrainingEpisodeId')) byId('paperTrainingEpisodeId').value = activeEpisodeId || '';
      if (input) {
        input.value = String(amount);
        input.dataset.dirty = 'false';
      }
      previewCache = null;
      renderAccount(returnedAccount, { forceInitialCashInput: true });

      const verifiedAccount = await request('/api/open-stock-ai/agent/paper-training/account');
      if (resetVersion !== accountRequestVersion) return;
      if (Math.abs(number(verifiedAccount.initial_cash) - amount) > 0.005
        || Math.abs(number(verifiedAccount.cash_balance) - amount) > 0.005
        || Math.abs(number(verifiedAccount.total_equity) - amount) > 0.005) {
        throw new Error('帳戶重設後的 SQLite 驗證失敗，拒絕顯示舊資金。');
      }
      renderAccount(verifiedAccount, { forceInitialCashInput: true });
      resetInFlight = false;
      await loadOrderPreview({ preserveStatus: true });
      renderAccount(verifiedAccount, { forceInitialCashInput: true });
      setStatus(`模擬帳戶已重設為 ${currency(amount)}，總資產與可用現金已同步。`, 'ok');
      document.dispatchEvent(new CustomEvent('stock-ai-paper-account-updated', { detail: verifiedAccount }));
    } finally {
      resetInFlight = false;
      if (button) button.disabled = false;
    }
  }

  async function startEpisode() {
    const objective = byId('paperTrainingObjective')?.value?.trim() || 'Paper trading strategy round';
    const result = await request('/api/open-stock-ai/agent/paper-training/episode', {
      method: 'POST',
      body: JSON.stringify({ objective, actor: 'user' }),
    });
    activeEpisodeId = result.episode_id;
    if (byId('paperTrainingEpisodeId')) byId('paperTrainingEpisodeId').value = activeEpisodeId;
    await loadAccount({ preserveStatus: true });
    setStatus(`策略回合 ${activeEpisodeId} 已開始。`, 'ok');
  }

  async function submitOrder() {
    const requestBody = currentOrderRequest();
    if (!previewCache) await loadOrderPreview({ preserveStatus: true });
    requestBody.episode_id = byId('paperTrainingEpisodeId')?.value?.trim() || activeEpisodeId || null;
    const sideLabels = { buy: '買進', sell: '賣出', short_sell: '融券放空', buy_to_cover: '買回回補' };
    setStatus(`正在送出${sideLabels[requestBody.side] || '模擬'}委託…`);
    const result = await request('/api/open-stock-ai/agent/paper-training/order', {
      method: 'POST',
      body: JSON.stringify(requestBody),
    });
    activeEpisodeId = result.episode_id;
    if (byId('paperTrainingEpisodeId')) byId('paperTrainingEpisodeId').value = activeEpisodeId;
    renderAccount(result.account);
    const order = result.broker?.order || {};
    const fill = order.fill || result.oms?.fill || {};
    const messages = {
      filled: `委託已成交：${requestBody.symbol} · ${money(fill.quantity)} 股 · ${currency(fill.fill_price)}`,
      open: `委託已掛單：${requestBody.symbol} · 等待價格條件`,
      triggered: `停損條件已觸發：${requestBody.symbol} · 等待限價成交`,
      canceled: `委託已取消：${order.rejection_reason || '未立即成交'}`,
      partially_filled: `委託部分成交：剩餘 ${money(order.remaining_quantity)} 股`,
      expired: '委託已到期。',
      rejected: `委託被拒絕：${order.rejection_reason || '請檢查帳戶與委託內容'}`,
    };
    const finalMessage = messages[order.status] || '委託已送出。';
    const finalTone = ['filled', 'open', 'triggered'].includes(order.status) ? 'ok' : 'warn';
    previewCache = null;
    await loadAccount({ preserveStatus: true });
    await loadOrderPreview({ preserveStatus: true });
    setStatus(finalMessage, finalTone);
  }

  async function cancelOrder(orderId) {
    if (!orderId) return;
    if (!window.confirm('確定撤銷這筆未成交委託？')) return;
    setStatus('正在撤單…');
    const result = await request(`/api/open-stock-ai/agent/paper-training/orders/${encodeURIComponent(orderId)}/cancel`, {
      method: 'POST',
      body: JSON.stringify({ reason: 'user_requested' }),
    });
    renderAccount(result.account);
    await loadOrderPreview({ preserveStatus: true });
    setStatus('委託已撤銷。', 'ok');
  }

  async function replaceOrder(orderId, currentPrice) {
    if (!orderId) return;
    const input = window.prompt('輸入新的限價；原單會保留所有既有成交並標記為已替換。', currentPrice || '');
    if (input === null) return;
    const limitPrice = number(input);
    if (!Number.isFinite(limitPrice) || limitPrice <= 0) throw new Error('請輸入大於 0 的新限價。');
    setStatus('正在替換未成交委託…');
    const result = await request(`/api/open-stock-ai/agent/paper-training/orders/${encodeURIComponent(orderId)}/replace`, {
      method: 'POST',
      body: JSON.stringify({ limit_price: limitPrice, actor: 'user', rationale: 'user_requested_price_replace' }),
    });
    renderAccount(result.account);
    await loadOrderPreview({ preserveStatus: true });
    setStatus(`委託已替換為 ${currency(limitPrice)}；原單成交與事件已保留。`, 'ok');
  }

  async function evaluateEpisode(close = false) {
    const episodeId = byId('paperTrainingEpisodeId')?.value?.trim() || activeEpisodeId;
    if (!episodeId) throw new Error('尚未建立策略回合。');
    await loadAccount({ refreshPrices: true, preserveStatus: true });
    const result = await request('/api/open-stock-ai/agent/paper-training/evaluate', {
      method: 'POST',
      body: JSON.stringify({ episode_id: episodeId, close }),
    });
    await loadAccount({ preserveStatus: true });
    setStatus(`本回合績效 ${signed(result.reward)}，報酬率 ${number(result.return_pct).toFixed(3)}%。`, 'ok');
  }

  async function saveReflection() {
    const episodeId = byId('paperTrainingEpisodeId')?.value?.trim() || activeEpisodeId;
    const summary = byId('paperTrainingReflection')?.value?.trim();
    if (!episodeId) throw new Error('尚未建立策略回合。');
    if (!summary) throw new Error('請先輸入回合檢討。');
    const lessons = summary.split(/\n|；|;/).map((item) => item.trim()).filter(Boolean).slice(0, 20);
    await request('/api/open-stock-ai/agent/paper-training/reflection', {
      method: 'POST',
      body: JSON.stringify({
        episode_id: episodeId,
        summary,
        lessons,
        next_rules: lessons.map((item) => `下一輪：${item}`),
      }),
    });
    await loadAccount({ preserveStatus: true });
    setStatus('回合檢討已保存。', 'ok');
  }

  function startAutomaticMarketMarks() {
    if (autoMarkTimer) window.clearInterval(autoMarkTimer);
    autoMarkTimer = window.setInterval(() => {
      if (document.hidden || resetInFlight) return;
      if (!byId(PAPER_VIEW_ID)?.classList.contains('active')) return;
      loadAccount({ refreshPrices: true }).catch((error) => setStatus(error.message || String(error), 'error'));
    }, AUTO_MARK_INTERVAL_MS);
    document.addEventListener('visibilitychange', () => {
      if (document.hidden || resetInFlight) return;
      if (!byId(PAPER_VIEW_ID)?.classList.contains('active')) return;
      loadAccount({ refreshPrices: true }).catch((error) => setStatus(error.message || String(error), 'error'));
    });
  }

  function bind(id, handler) {
    const node = byId(id);
    if (!node || node.dataset.paperBound === 'true') return;
    node.dataset.paperBound = 'true';
    node.addEventListener('click', () => {
      Promise.resolve(handler()).catch((error) => setStatus(error.message || String(error), 'error'));
    });
  }

  async function initialize() {
    ensureWorkspace();
    initializeStatefulGlass();
    if (!byId('paperTrainingPanel')) return;

    const officialSide = byId('tradingSide')?.value;
    const officialLots = byId('tradingLots')?.value;
    if (officialSide) setSide(officialSide);
    if (officialLots && byId('paperTrainingQuantity')) byId('paperTrainingQuantity').value = officialLots;

    bind('paperTrainingBuySide', async () => {
      setSide('buy');
      await loadOrderPreview();
    });
    bind('paperTrainingSellSide', async () => {
      setSide('sell');
      await loadOrderPreview();
    });
    bind('paperTrainingShortSellSide', async () => {
      setSide('short_sell');
      await loadOrderPreview();
    });
    bind('paperTrainingCoverSide', async () => {
      setSide('buy_to_cover');
      await loadOrderPreview();
    });
    byId('paperTrainingBorrowReceipt')?.addEventListener('input', () => { previewCache = null; });
    bind('paperTrainingPreview', loadOrderPreview);
    bind('autonomousPaperRefresh', loadAutonomousStatus);
    bind('paperTrainingSubmitOrder', submitOrder);
    bind('paperTrainingRefreshPrices', async () => {
      await loadAccount({ refreshPrices: true });
      if (hasOrderSymbol()) {
        await loadOrderPreview({ preserveStatus: true });
        setStatus('行情、掛單與帳戶市值已更新。', 'ok');
      } else {
        setStatus('帳戶已更新；輸入股票代號後可進行委託試算。', 'ok');
      }
    });
    bind('paperTrainingReset', resetAccount);
    bind('paperTrainingStartEpisode', startEpisode);
    bind('paperTrainingEvaluate', () => evaluateEpisode(false));
    bind('paperTrainingCloseEpisode', () => evaluateEpisode(true));
    bind('paperTrainingSaveReflection', saveReflection);
    bind('paperTrainingRollbackArtifact', rollbackArtifact);
    byId('paperTrainingRollbackScope')?.addEventListener('change', () => {
      loadArtifactGovernance({ preserveStatus: true }).catch((error) => setStatus(error.message || String(error), 'error'));
    });

    byId('paperTrainingInitialCash')?.addEventListener('input', (event) => {
      event.currentTarget.dataset.dirty = 'true';
    });
    [
      'paperTrainingSymbol',
      'paperTrainingOrderType',
      'paperTrainingLotType',
      'paperTrainingQuantity',
      'paperTrainingTimeInForce',
      'paperTrainingSession',
      'paperTrainingLimitPrice',
      'paperTrainingStopPrice',
    ].forEach((id) => {
      byId(id)?.addEventListener('change', updateTicketFields);
    });

    updateTicketFields();
    startAutomaticMarketMarks();
    startAutonomousStatusUpdates();
    void loadAutonomousStatus();
    try {
      await loadAccount();
      await loadArtifactGovernance({ preserveStatus: true });
      if (hasOrderSymbol()) {
        await loadOrderPreview({ preserveStatus: true });
        setStatus('模擬帳戶與委託試算已載入。', 'ok');
      } else {
        setStatus('模擬帳戶已載入；請先選擇股票再進行委託試算。', 'ok');
      }
    } catch (error) {
      setStatus(error.message || String(error), 'error');
    }

    window.__paperTradingUI = {
      loadAccount,
      loadOrderPreview,
      loadArtifactGovernance,
      resetAccount,
      syncNavigationMaterial,
      activateView,
    };
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initialize, { once: true });
  } else {
    initialize();
  }
})();
