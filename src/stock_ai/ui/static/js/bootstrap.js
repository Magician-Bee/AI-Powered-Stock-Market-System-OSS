const STOCK_AI_I18N_VERSION = '20260718-full-i18n-v4';
const STOCK_AI_I18N_EXACT_FILES = Array.from(
  { length: 10 },
  (_, index) => `/static/i18n/en-dynamic-${String(index + 1).padStart(2, '0')}.json?v=${STOCK_AI_I18N_VERSION}`,
);
const STOCK_AI_I18N_SEGMENT_FILES = [
  `/static/i18n/en-segments.json?v=${STOCK_AI_I18N_VERSION}`,
  `/static/i18n/en-segments-extra.json?v=${STOCK_AI_I18N_VERSION}`,
];
const STOCK_AI_I18N_CONTROL_FILE = `/static/i18n/en-controls.json?v=${STOCK_AI_I18N_VERSION}`;

async function loadStockAiJson(url) {
  const response = await fetch(url, { cache: 'no-store' });
  if (!response.ok) throw new Error(`i18n ${response.status}: ${url}`);
  return response.json();
}

async function loadStockAiI18nCatalogs() {
  const [exactResults, segmentResults, controlsResult] = await Promise.all([
    Promise.allSettled(STOCK_AI_I18N_EXACT_FILES.map(loadStockAiJson)),
    Promise.allSettled(STOCK_AI_I18N_SEGMENT_FILES.map(loadStockAiJson)),
    loadStockAiJson(STOCK_AI_I18N_CONTROL_FILE).catch((error) => {
      console.error('English localization controls failed', error);
      return {};
    }),
  ]);
  const exact = {};
  const segments = {};
  exactResults.forEach((result) => {
    if (result.status === 'fulfilled') Object.assign(exact, result.value || {});
    else console.error('English localization catalog failed', result.reason);
  });
  segmentResults.forEach((result) => {
    if (result.status === 'fulfilled') Object.assign(segments, result.value || {});
    else console.error('English localization segments failed', result.reason);
  });
  return { exact, segments, controls: controlsResult || {} };
}

function installStockAiDynamicI18n({ exact, segments, controls }) {
  const legacyTranslateUiString = typeof translateUiString === 'function'
    ? translateUiString
    : (value) => value;
  const legacyTranslateUiTree = typeof translateUiTree === 'function'
    ? translateUiTree
    : () => {};
  const legacyTranslateControlDefaults = typeof translateControlDefaults === 'function'
    ? translateControlDefaults
    : () => {};
  const dynamicControlOriginals = new WeakMap();
  const dynamicChromeOriginals = new WeakMap();
  const segmentEntries = Object.entries(segments).sort((a, b) => b[0].length - a[0].length);
  const HAN_TEXT_PATTERN = /[\u3400-\u9fff]/;
  let dynamicChromeObserver = null;
  let dynamicChromeApplying = false;

  const UI_CHROME_SELECTOR = [
    'button',
    'label',
    'legend',
    'summary',
    'option',
    '.chip',
    '.tag',
    '.meta-pill',
    '.filter-chip',
    '.panel-head h3',
    '.panel-head h4',
    '.panel-head .panel-copy',
    '.paper-broker-head h4',
    '.paper-broker-head small',
    '.mini-card > span',
    '.mini-card > small',
    '.row.header > div',
    '.process-step > span',
    '.process-step > strong',
    '.process-step > small',
    '.event > h3',
    '.event > h4',
    '.event > p',
    '.settings-status-row strong',
    '.settings-status-row small',
    '.agent-decision-head span',
    '.agent-decision-head small',
    '.agent-run-meta span',
    '.agent-tool-trace span',
    '.agent-activity-item > strong',
    '.agent-activity-item > p',
    '.paper-order-status',
    '.home-status-bar h3',
    '.decision-lane-head h3',
    '.decision-lane-head small',
    '.global-agent-popover-head strong',
    '.global-agent-popover-head span',
    '[data-i18n-dynamic]',
  ].join(',');

  function isEnglish() {
    return document.documentElement.lang === 'en';
  }

  function preserveOuterWhitespace(source, translated) {
    const leading = source.match(/^\s*/)?.[0] || '';
    const trailing = source.match(/\s*$/)?.[0] || '';
    return `${leading}${translated}${trailing}`;
  }

  function normalizeTranslatedPunctuation(value) {
    return String(value)
      .replaceAll('：', ': ')
      .replaceAll('；', '; ')
      .replaceAll('，', ', ')
      .replaceAll('、', ', ')
      .replaceAll('。', '.')
      .replaceAll('？', '?')
      .replaceAll('／', '/')
      .replaceAll('｜', ' | ')
      .replace(/:([^\s/])/g, ': $1')
      .replace(/(\d):\s+(\d)/g, '$1:$2')
      .replace(/;([^\s])/g, '; $1')
      .replace(/(\d)(stocks|items|records|lots|shares|sources|modules|signals|orders|instruments|roles|templates|evaluations|notifications|candidates|watched)\b/gi, '$1 $2')
      .replace(/\s{2,}/g, ' ')
      .replace(/\s+([,.;:%?])/g, '$1')
      .trim();
  }

  function translatePatternEnglish(trimmed) {
    const patterns = [
      [/^(\d+)\s*檔$/, (_, n) => `${n} stocks`],
      [/^(\d+)\s*檔立即執行$/, (_, n) => `${n} immediate`],
      [/^(\d+)\s*檔立即\s*·\s*(\d+)\s*檔候選$/, (_, a, b) => `${a} immediate · ${b} candidates`],
      [/^(\d+)\s*檔立即\s*·\s*(\d+)\s*檔監控$/, (_, a, b) => `${a} immediate · ${b} watched`],
      [/^(\d+)\s*檔\s*·\s*每檔都有下一步$/, (_, n) => `${n} stocks · each has a next step`],
      [/^(\d+)\s*個工具可用$/, (_, n) => `${n} tools available`],
      [/^(\d+)\s*個工具$/, (_, n) => `${n} tools`],
      [/^(\d+)\s*個$/, (_, n) => `${n} items`],
      [/^(\d+)\s*項$/, (_, n) => `${n} items`],
      [/^(\d+)\s*筆$/, (_, n) => `${n} records`],
      [/^(\d+)\s*則$/, (_, n) => `${n} items`],
      [/^(\d+)\s*張$/, (_, n) => `${n} lots`],
      [/^(\d+)\s*股$/, (_, n) => `${n} shares`],
      [/^(\d+)\/(\d+)\s*個通道已設定；金鑰只由後端環境讀取。$/, (_, ready, total) => `${ready}/${total} channels configured; keys are read only from the backend environment.`],
      [/^尚未支援 provider:\s*(.+)$/, (_, provider) => `Unsupported provider: ${provider}`],
      [/^共\s*(\d+)\s*回合\s*·\s*已評估\s*(\d+)\s*·\s*正報酬\s*(\d+)\s*·\s*負報酬\s*(\d+)$/, (_, a, b, c, d) => `${a} episodes · ${b} evaluated · ${c} positive · ${d} negative`],
      [/^目前個股\s+(.+)$/, (_, value) => `Current stock ${value}`],
      [/^帳戶版本\s+(.+)$/, (_, value) => `Account revision ${value}`],
      [/^Agent Context 已同步：(.+)$/, (_, value) => `Agent context synchronized: ${value}`],
      [/^策略回合\s+(.+)\s+已開始。$/, (_, value) => `Strategy episode ${value} started.`],
      [/^本回合績效\s+(.+)，報酬率\s+(.+)%。$/, (_, reward, pct) => `Episode performance ${reward}, return ${pct}%.`],
      [/^委託已成交：(.+)\s*·\s*(.+)\s*股\s*·\s*(.+)$/, (_, symbol, qty, price) => `Order filled: ${symbol} · ${qty} shares · ${price}`],
      [/^委託已掛單：(.+)\s*·\s*等待價格條件$/, (_, symbol) => `Order opened: ${symbol} · waiting for the price condition`],
      [/^停損條件已觸發：(.+)\s*·\s*等待限價成交$/, (_, symbol) => `Stop triggered: ${symbol} · waiting for the limit fill`],
      [/^確定將模擬帳戶重設為\s+(.+)？所有委託、成交與學習紀錄都會清除。$/, (_, amount) => `Reset the paper account to ${amount}? All orders, fills, and learning records will be deleted.`],
      [/^正在把模擬帳戶重設為\s+(.+)…$/, (_, amount) => `Resetting the paper account to ${amount}…`],
      [/^模擬帳戶已重設為\s+(.+)，總資產與可用現金已同步。$/, (_, amount) => `The paper account was reset to ${amount}. Total assets and available cash are synchronized.`],
      [/^伺服器沒有完成資金重設：初始\s+(.+)、現金\s+(.+)、總資產\s+(.+)$/, (_, a, b, c) => `The server did not complete the capital reset: initial ${a}, cash ${b}, total assets ${c}`],
      [/^(.+)\s+K線圖\s*\/\s*歷史日K\s*\+\s*盤中即時$/, (_, name) => `${name} Candlestick Chart / Daily History + Realtime Intraday`],
      [/^等待圖表資料\s+(.+)$/, (_, value) => `Waiting for chart data ${value}`],
      [/^等待盤中即時報價\s+(.+)$/, (_, value) => `Waiting for realtime intraday quotes ${value}`],
      [/^K線圖模式：(.+)$/, (_, value) => `Candlestick mode: ${translateDynamicEnglish(value)}`],
      [/^最新K：(.+)$/, (_, value) => `Latest candle: ${value}`],
      [/^最新 K 線時間\s+(.+)$/, (_, value) => `Latest candle time ${value}`],
      [/^最新有效交易時間\s+(.+)；系統現在時間\s+(.+)$/, (_, a, b) => `Latest valid trading time ${a}; system time ${b}`],
      [/^台股已收盤，最新有效交易時間\s+(.+)；系統現在時間\s+(.+)$/, (_, a, b) => `Taiwan market is closed; latest valid trading time ${a}; system time ${b}`],
      [/^台股未開盤，最新有效交易時間\s+(.+)；系統現在時間\s+(.+)$/, (_, a, b) => `Taiwan market has not opened; latest valid trading time ${a}; system time ${b}`],
      [/^盤中即時更新中，最新有效交易時間\s+(.+)；系統現在時間\s+(.+)$/, (_, a, b) => `Updating realtime intraday; latest valid trading time ${a}; system time ${b}`],
      [/^盤中即時圖\s+(.+)｜價格\/委買委賣\/均線\/布林通道全由即時報價更新$/, (_, value) => `Realtime intraday chart ${value} | price, bid/ask, moving averages, and Bollinger Bands update from realtime quotes`],
      [/^(.+)\s+已連線$/, (_, value) => `${value} connected`],
      [/^(.+)\s+未登入$/, (_, value) => `${value} not signed in`],
      [/^(.+)\s+已完成$/, (_, value) => `${value} completed`],
      [/^(.+)\s+需要使用者批准$/, (_, value) => `${value} requires user approval`],
    ];
    for (const [pattern, replacement] of patterns) {
      if (pattern.test(trimmed)) return trimmed.replace(pattern, replacement);
    }
    return '';
  }

  function translateExactEnglish(source) {
    if (typeof source !== 'string' || !HAN_TEXT_PATTERN.test(source)) return source;
    const legacy = legacyTranslateUiString(source);
    if (legacy !== source) return legacy;
    const trimmed = source.trim();
    const translated = exact[trimmed] || translatePatternEnglish(trimmed);
    return translated ? preserveOuterWhitespace(source, translated) : source;
  }

  function translateDynamicEnglish(source) {
    const exactResult = translateExactEnglish(source);
    if (exactResult !== source || typeof source !== 'string' || !HAN_TEXT_PATTERN.test(source)) return exactResult;
    const leading = source.match(/^\s*/)?.[0] || '';
    const trailing = source.match(/\s*$/)?.[0] || '';
    let translated = source.trim();
    let changed = false;
    for (const [zh, en] of segmentEntries) {
      if (!translated.includes(zh)) continue;
      translated = translated.split(zh).join(en);
      changed = true;
    }
    if (!changed) return source;
    return `${leading}${normalizeTranslatedPunctuation(translated)}${trailing}`;
  }

  translateUiString = translateExactEnglish;
  window.stockAiTranslateExactEnglish = translateExactEnglish;
  window.stockAiTranslateDynamicEnglish = translateDynamicEnglish;
  window.stockAiText = (source) => isEnglish()
    ? translateDynamicEnglish(String(source ?? ''))
    : String(source ?? '');
  window.stockAiLocale = () => isEnglish() ? 'en-US' : 'zh-TW';

  function applyDynamicControlTranslations(language) {
    for (const [id, pairs] of Object.entries(controls)) {
      const control = document.getElementById(id);
      if (!control || !('value' in control)) continue;
      if (!dynamicControlOriginals.has(control)) dynamicControlOriginals.set(control, control.value);
      for (const [zh, en] of pairs) {
        if (language === 'en' && (control.value === zh || control.value === en)) {
          control.value = en;
          break;
        }
        if (language !== 'en' && (control.value === en || control.value === zh)) {
          control.value = zh;
          break;
        }
      }
    }
  }

  function shouldSkipChromeElement(element) {
    if (!(element instanceof Element)) return true;
    return Boolean(element.closest(
      'script,style,noscript,pre,code,textarea,[contenteditable="true"],[data-i18n-skip],.json-box,'
      + '#newsCenterBox,#overviewNewsBox,#eventList',
    ));
  }

  function translateChromeTextNode(node, language) {
    if (!(node instanceof Text) || shouldSkipChromeElement(node.parentElement)) return;
    if (language === 'en') {
      const current = node.nodeValue || '';
      if (!HAN_TEXT_PATTERN.test(current)) return;
      const translated = translateDynamicEnglish(current);
      if (translated === current) return;
      if (!dynamicChromeOriginals.has(node)) dynamicChromeOriginals.set(node, current);
      node.nodeValue = translated;
      return;
    }
    const original = dynamicChromeOriginals.get(node);
    if (typeof original === 'string') node.nodeValue = original;
  }

  function translateChromeElement(element, language) {
    if (!(element instanceof Element) || shouldSkipChromeElement(element)) return;
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    let node = walker.nextNode();
    while (node) {
      translateChromeTextNode(node, language);
      node = walker.nextNode();
    }
  }

  function translateProtectedUiChrome(root, language) {
    if (!root) return;
    const candidates = [];
    if (root instanceof Element && root.matches(UI_CHROME_SELECTOR)) candidates.push(root);
    if (root.querySelectorAll) candidates.push(...root.querySelectorAll(UI_CHROME_SELECTOR));
    candidates.forEach((element) => translateChromeElement(element, language));
  }

  translateControlDefaults = function stockAiTranslateControlDefaults(language) {
    legacyTranslateControlDefaults(language);
    applyDynamicControlTranslations(language);
  };

  translateUiTree = function stockAiTranslateUiTree(root, language) {
    legacyTranslateUiTree(root, language);
    translateProtectedUiChrome(root, language);
    applyDynamicControlTranslations(language);
  };

  function startDynamicChromeObserver() {
    if (dynamicChromeObserver || !document.body) return;
    dynamicChromeObserver = new MutationObserver((mutations) => {
      if (dynamicChromeApplying) return;
      dynamicChromeApplying = true;
      try {
        const language = isEnglish() ? 'en' : 'zh-Hant';
        mutations.forEach((mutation) => {
          if (mutation.type === 'characterData') {
            const parent = mutation.target.parentElement;
            if (parent?.closest(UI_CHROME_SELECTOR)) translateChromeElement(parent.closest(UI_CHROME_SELECTOR), language);
            return;
          }
          mutation.addedNodes.forEach((node) => translateProtectedUiChrome(node, language));
        });
      } finally {
        dynamicChromeApplying = false;
      }
    });
    dynamicChromeObserver.observe(document.body, { subtree: true, childList: true, characterData: true });
  }

  if (!window.__stockAiCanvasI18nPatched && typeof CanvasRenderingContext2D !== 'undefined') {
    window.__stockAiCanvasI18nPatched = true;
    const originalFillText = CanvasRenderingContext2D.prototype.fillText;
    const originalStrokeText = CanvasRenderingContext2D.prototype.strokeText;
    CanvasRenderingContext2D.prototype.fillText = function stockAiFillText(text, ...args) {
      return originalFillText.call(this, window.stockAiText(text), ...args);
    };
    CanvasRenderingContext2D.prototype.strokeText = function stockAiStrokeText(text, ...args) {
      return originalStrokeText.call(this, window.stockAiText(text), ...args);
    };
  }

  if (!window.__stockAiDialogI18nPatched) {
    window.__stockAiDialogI18nPatched = true;
    const nativeConfirm = window.confirm.bind(window);
    const nativeAlert = window.alert.bind(window);
    const nativePrompt = window.prompt.bind(window);
    window.confirm = (message) => nativeConfirm(window.stockAiText(message));
    window.alert = (message) => nativeAlert(window.stockAiText(message));
    window.prompt = (message, defaultValue) => nativePrompt(window.stockAiText(message), defaultValue);
  }

  window.stockAiFindUntranslatedUi = function stockAiFindUntranslatedUi() {
    if (!document.body || !isEnglish()) return [];
    const unresolved = [];
    document.querySelectorAll(UI_CHROME_SELECTOR).forEach((element) => {
      if (shouldSkipChromeElement(element)) return;
      const text = String(element.textContent || '').replace(/\s+/g, ' ').trim();
      if (text && HAN_TEXT_PATTERN.test(text)) unresolved.push(text);
    });
    return [...new Set(unresolved)];
  };

  window.stockAiTranslateExistingDynamicUi = function stockAiTranslateExistingDynamicUi() {
    const language = isEnglish() ? 'en' : 'zh-Hant';
    if (document.body) translateUiTree(document.body, language);
    applyDynamicControlTranslations(language);
    const answer = document.getElementById('answerBox');
    if (answer && language === 'en') {
      answer.textContent = answer.textContent
        .replaceAll('【路由】', '【Route】')
        .replaceAll('查詢失敗：', 'Query failed: ')
        .replaceAll('系統不會用補值行情替代。', 'The system will not substitute imputed quotes.');
    }
    const unresolved = window.stockAiFindUntranslatedUi();
    if (unresolved.length) console.warn('Untranslated English UI strings', unresolved.slice(0, 50));
  };

  startDynamicChromeObserver();
}

async function safeInitStep(label, fn) {
  try {
    await fn();
  } catch (err) {
    console.error(`${label} failed`, err);
  }
}

(async function init(){
  const catalogs = await loadStockAiI18nCatalogs();
  installStockAiDynamicI18n(catalogs);
  initUiSettings();
  window.HomeMarketWorkspace?.render?.();
  initStockEventPanel();
  initIntradayCandleControls();
  window.StockChartInteractions?.init();
  window.stockAiTranslateExistingDynamicUi();
  syncWorkspaceControls();
  initOpenSourceGlassUI();
  initFreeFrontendLiquidGlass();
  await safeInitStep('codex-account', async () => {
    await loadCodexAccount(false);
  });
  await safeInitStep('system-agent-page', async () => loadSystemAgentPage(false));
  await safeInitStep('system-skills-page', async () => loadSystemSkillsPage());
  await safeInitStep('system-connections-page', async () => loadSystemConnectionsPage());
  await safeInitStep('broker-connections', async () => loadBrokerConnections());
  await safeInitStep('agent-runtime', async () => loadAgentRuntimeSettings());
  await safeInitStep('market-intelligence-workspace', async () => window.HomeMarketWorkspace?.init?.());
  await safeInitStep('health', async () => { const health = await api('/health'); $('healthText').textContent = health.status; });
  await safeInitStep('dashboard-overview', async () => loadDashboardOverview());
  await safeInitStep('news-center', async () => loadNewsCenterView());
  await safeInitStep('entities', async () => loadEntities(''));
  await safeInitStep('summary', async () => {
    if (currentViewId() === 'instrument' && String(state.symbol || '').trim()) {
      await loadSummary(state.symbol, { navigate: false });
    }
  });
  await safeInitStep('screener', async () => runScreener());
  await safeInitStep('linkage', async () => runLinkage());
  await safeInitStep('catalog', async () => loadCatalog());
  await safeInitStep('notifications', async () => loadNotificationCenter());
  await safeInitStep('open-source-glass-ui-refresh', async () => applyOpenSourceGlassClasses());
  window.stockAiTranslateExistingDynamicUi();
  if (!document.querySelector('body > .app-shell .view.active')) setView('home');
})();
