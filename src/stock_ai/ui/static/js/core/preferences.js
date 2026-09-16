const UI_SETTINGS_KEY = 'stock-ai-ui-settings-v1';
const DEFAULT_UI_SETTINGS = Object.freeze({
  theme: 'exchange',
  backdrop: 'constellation',
  language: 'zh-Hant',
  density: 'comfortable',
  fontScale: 100,
  glassTransparency: 70,
});

const UI_TEXT = {
  'zh-Hant': {
    'nav.customize': '介面自訂',
    'settings.title': '設定與連線中心', 'settings.subtitle': 'Codex 帳號、Skills、MCP、資料 API 與介面偏好統一在這裡管理。',
    'settings.themeTitle': '介面風格', 'settings.themeCopy': '依照工作環境選擇整套配色；文字對比、玻璃明暗與市場訊號會同步調整。',
    'theme.exchange': '交易所夜色', 'theme.exchangeCopy': '適合長時間盯盤，清楚辨識多色市場訊號',
    'theme.graphite': '石墨黑', 'theme.graphiteCopy': '降低色彩干擾，專注閱讀數據與報表',
    'theme.clarity': '清透玻璃', 'theme.clarityCopy': '提升折射與高光，呈現更清楚的玻璃層次',
    'theme.terminal': '終端綠', 'theme.terminalCopy': '以綠色焦點突出系統狀態與密集資訊',
    'theme.aurora': '極光紫', 'theme.auroraCopy': '以紫藍色調區分研究內容與市場訊號',
    'theme.pearl': '珍珠白', 'theme.pearlCopy': '溫暖淺色介面，適合明亮環境與長篇閱讀',
    'theme.daylight': '日光藍', 'theme.daylightCopy': '冷白藍色對比，適合日間分析工作',
    'settings.backdropTitle': '背景光學', 'settings.backdropCopy': '選擇光線穿過玻璃時的焦點與空間深度；只改變視覺氣氛，不影響資料與功能。',
    'backdrop.constellation': '柔光焦點', 'backdrop.constellationCopy': '左上冷白柔光與右下青色光域',
    'backdrop.grid': '稜鏡折射', 'backdrop.gridCopy': '斜向透明折射面與冷暖分光',
    'backdrop.waves': '景深流光', 'backdrop.wavesCopy': '中央保持清晰，四周逐步加深',
    'backdrop.bands': '色散微光', 'backdrop.bandsCopy': '藍、紫、金三色柔光分散畫面',
    'backdrop.tiles': '市場聚光', 'backdrop.tilesCopy': '中央亮區搭配明顯暗角收斂',
    'backdrop.none': '原生壁紙', 'backdrop.noneCopy': '只保留主題壁紙與玻璃折射',
    'settings.displayTitle': '顯示與語言', 'settings.displayCopy': '調整介面語言、資訊密度與閱讀比例。',
    'settings.language': '介面語言', 'settings.languageCopy': '切換導覽、標題與設定介面的語言',
    'settings.density': '資訊密度', 'settings.densityCopy': '控制卡片、表格與導覽間距',
    'density.comfortable': '舒適', 'density.compact': '緊湊',
    'settings.fontScale': '文字比例', 'settings.fontScaleCopy': '放大主要資料與操作文字',
    'settings.glassTransparency': '玻璃透明度', 'settings.glassTransparencyCopy': '向右增加通透感，向左提高內容後方的遮蔽',
    'stock.events.open': '開啟個股事件', 'stock.events.hide': '關閉個股事件', 'stock.events.copy': '公司公告與相關新聞',
    'settings.preview': '正在即時預覽，套用後會保存到這台裝置。', 'settings.reset': '恢復預設', 'settings.save': '套用設定',
    saved: '設定已保存。', reset: '已恢復預設設定。', reload: '正在重新載入玻璃渲染品質…',
  },
  en: {
    'nav.customize': 'CUSTOMIZE',
    'settings.title': 'Settings & Connections', 'settings.subtitle': 'Manage the Codex account, Skills, MCP, data APIs, and interface preferences in one place.',
    'settings.themeTitle': 'Interface Style', 'settings.themeCopy': 'Choose a complete palette for your work environment; text contrast, glass brightness, and market signals adjust together.',
    'theme.exchange': 'Exchange Night', 'theme.exchangeCopy': 'Made for long market sessions with clearly separated signals',
    'theme.graphite': 'Graphite', 'theme.graphiteCopy': 'Reduces color distraction for focused data and report reading',
    'theme.clarity': 'Clear Glass', 'theme.clarityCopy': 'Brighter refraction and highlights for stronger glass depth',
    'theme.terminal': 'Terminal Green', 'theme.terminalCopy': 'Green focus accents emphasize system status and dense data',
    'theme.aurora': 'Aurora Violet', 'theme.auroraCopy': 'Violet-blue tones distinguish research from market signals',
    'theme.pearl': 'Pearl White', 'theme.pearlCopy': 'Warm light interface for bright rooms and long-form reading',
    'theme.daylight': 'Daylight Blue', 'theme.daylightCopy': 'Cool white-blue contrast for daytime analysis',
    'settings.backdropTitle': 'Backdrop Optics', 'settings.backdropCopy': 'Choose the focus and spatial depth of light through the glass; visuals change without affecting data or features.',
    'backdrop.constellation': 'Soft Focus', 'backdrop.constellationCopy': 'Cool-white light at upper left and cyan at lower right',
    'backdrop.grid': 'Prism Refraction', 'backdrop.gridCopy': 'Diagonal transparent planes with cool-warm splitting',
    'backdrop.waves': 'Depth Glow', 'backdrop.wavesCopy': 'A clear center with progressively deeper edges',
    'backdrop.bands': 'Chromatic Sheen', 'backdrop.bandsCopy': 'Blue, violet, and gold light dispersed across the view',
    'backdrop.tiles': 'Market Spotlight', 'backdrop.tilesCopy': 'A bright center with a clearly restrained vignette',
    'backdrop.none': 'Native Wallpaper', 'backdrop.noneCopy': 'Keep only the theme wallpaper and glass refraction',
    'settings.displayTitle': 'Display & Language', 'settings.displayCopy': 'Adjust interface language, information density, and reading scale.',
    'settings.language': 'Interface Language', 'settings.languageCopy': 'Switch navigation, titles, and settings language',
    'settings.density': 'Information Density', 'settings.densityCopy': 'Control spacing in cards, tables, and navigation',
    'density.comfortable': 'Comfortable', 'density.compact': 'Compact',
    'settings.fontScale': 'Text Scale', 'settings.fontScaleCopy': 'Scale primary data and control text',
    'settings.glassTransparency': 'Glass Transparency', 'settings.glassTransparencyCopy': 'Move right for more transparency or left for stronger content separation',
    'stock.events.open': 'Open Stock Events', 'stock.events.hide': 'Close Stock Events', 'stock.events.copy': 'Company filings and related news',
    'settings.preview': 'Previewing live. Apply to save these settings on this device.', 'settings.reset': 'Restore Defaults', 'settings.save': 'Apply Settings',
    saved: 'Settings saved.', reset: 'Default settings restored.', reload: 'Reloading the glass render quality…',
  },
};

const NAV_TEXT = {
  'zh-Hant': { home:'首頁',market:'市場',instrument:'個股',portfolio:'投資組合',research:'研究',system:'系統' },
  en: { home:'Home',market:'Market',instrument:'Instrument',portfolio:'Portfolio',research:'Research',system:'System' },
};

const SHELL_TEXT = {
  'zh-Hant': { brand:'股市AI系統',subbrand:'市場操作台',eyebrow:'行情資料 · 策略研究 · 風控執行 · 投資工作區',placeholder:'交給 AI Agent：查行情、找資料、分析風險或操作專案…',search:'傳送',sourceStatus:'資料源狀態、即時行情、新聞事件與研究工作區整合在同一個操作面板。',strip:['行情熱度','籌碼追蹤','風險雷達','策略回測','資產監控'] },
  en: { brand:'Stock AI System',subbrand:'Market Workstation',eyebrow:'Market Data · Strategy Research · Risk Execution · Investment Workspace',placeholder:'Ask AI Agent to research, analyze risk, or operate the project…',search:'Send',sourceStatus:'Source status, realtime quotes, news events, and research tools are unified in one workstation.',strip:['Market Pulse','Flow Tracking','Risk Radar','Strategy Tests','Asset Monitor'] },
};

// Exact translations for the complete application shell and workspace UI. Market/news
// content is intentionally not machine-translated; only known interface copy is replaced.
const FULL_UI_TEXT_EN = Object.freeze({
  '股市AI系統': 'Stock AI System',
  '市場操作台': 'Market Workstation',
  '股市AI系統 | 市場操作台': 'Stock AI System | Market Workstation',
  'Codex 核心登入': 'Codex Core Sign-in',
  '登入後開始市場決策': 'Sign in to start market decisions',
  '使用 ChatGPT 帳號連接 Codex。系統不會要求你貼上 API 金鑰，也不會在頁面中接觸密碼。': 'Connect Codex with your ChatGPT account. The system never asks you to paste an API key and never handles your password in the page.',
  '正在檢查登入狀態…': 'Checking sign-in status…',
  '使用 ChatGPT 登入': 'Sign in with ChatGPT',
  '改用裝置代碼': 'Use a device code instead',
  '裝置代碼': 'Device code',
  '開啟驗證頁': 'Open verification page',
  '主功能導覽': 'Primary navigation',
  '與 AI Agent 對話': 'Chat with AI Agent',
  '傳送': 'Send',
  '活動': 'Activity',
  'Agent 即時活動': 'Live Agent Activity',
  '等待指令': 'Waiting for instructions',
  '輸入任務後，這裡會顯示可稽核的執行摘要。': 'After you enter a task, an auditable execution summary will appear here.',
  '開啟完整工作台': 'Open full workspace',
  'Codex 帳號': 'Codex account',
  '現在該做什麼': 'What to do now',
  '正在整合市場資料與策略訊號…': 'Combining market data and strategy signals…',
  '顯示理由': 'Show rationale',
  '更新判斷': 'Refresh decision',
  '本輪動作': 'Current action',
  '等待市場資料': 'Waiting for market data',
  '完成後會顯示下一步與重估時間': 'The next step and reassessment time will appear when ready',
  '內建市場雷達決策': 'Built-in market radar decisions',
  '現在可買': 'Buy now',
  '現在可賣': 'Sell now',
  '完整行動清單': 'Complete action plan',
  '直接交給 AI Agent': 'Delegate directly to AI Agent',
  '使用同一套 Durable Agent Runtime 分析市場、理解專案與操作目前 UI。': 'Use the same Durable Agent Runtime to analyze markets, understand the project, and operate the current UI.',
  '連線中': 'Connecting',
  '快速指令': 'Quick commands',
  '市場決策': 'Market decision',
  '找買點': 'Find entries',
  '找賣點': 'Find exits',
  '操作 UI': 'Operate UI',
  '直接列出現在可買、可賣與需要等待的股票。': 'List the stocks to buy, sell, or wait on now.',
  '找出目前最接近買點的股票，只有結論與時間。': 'Find the stocks closest to an entry point; return only the conclusion and timing.',
  '找出目前應減碼或等待賣點的股票，只有結論與時間。': 'Find stocks that should be reduced or are waiting for an exit; return only the conclusion and timing.',
  '檢查目前頁面並操作到個股分析。': 'Inspect the current page and navigate to Stock Analysis.',
  '直接下指令，例如：分析 2330 現在該買、賣還是等': 'Enter an instruction, for example: analyze whether to buy, sell, or wait on 2330',
  '使用 UI Bridge 操作介面': 'Use UI Bridge to operate the interface',
  '執行': 'Run',
  'AI Agent 工作台': 'AI Agent Workspace',
  '在上方常駐對話列輸入任務；這裡顯示 Agent 的計畫摘要、可稽核執行事件、資料來源、工具／技能／套件、參數與操作結果。': 'Enter tasks in the persistent composer above. This workspace shows the Agent plan, auditable execution events, sources, tools, skills, packages, parameters, and results.',
  '載入中': 'Loading',
  'Agent 任務控制': 'Agent task controls',
  '目前任務': 'Current task',
  '尚未收到任務': 'No task received',
  '自主層級': 'Autonomy level',
  'Agent 自主層級': 'Agent autonomy level',
  '分析與預覽，不下單': 'Analysis and preview only; no orders',
  '允許終端機與專案修改': 'Allow terminal and project changes',
  '允許瀏覽器、通知與外部服務操作': 'Allow browser, notifications, and external services',
  '允許模擬下單': 'Allow paper orders',
  '允許全部（含破壞性外部操作與模擬下單）': 'Allow all actions, including destructive external actions and paper orders',
  '目前操作員': 'Active operator',
  '設定': 'Settings',
  '真實券商下單固定關閉；終端機、專案修改與模擬委託只會在明確選擇對應層級後啟用。': 'Live brokerage orders are permanently disabled. Terminal access, project changes, and paper orders activate only after the corresponding level is explicitly selected.',
  '執行活動': 'Execution activity',
  '等待任務': 'Waiting for a task',
  'Agent 活動統計': 'Agent activity statistics',
  '工具': 'Tools',
  '技能': 'Skills',
  '套件': 'Packages',
  '排程': 'Schedules',
  'Agent 執行後會依序顯示計畫摘要、工具參數、技能／套件、結果與排程。': 'After the Agent runs, the plan, tool parameters, skills/packages, results, and schedules appear in order.',
  '證券資料': 'Securities',
  '市場指數': 'Market indexes',
  '台股、美股、半導體與匯率觀察': 'Taiwan, U.S., semiconductor, and FX coverage',
  '資料源': 'Data sources',
  '啟用 / 部分 / 規劃中': 'Active / Partial / Planned',
  '系統狀態': 'System status',
  '線上': 'Online',
  '檢查中...': 'Checking…',
  '檢查中': 'Checking',
  '每日市場摘要': 'Daily Market Brief',
  '儀表板': 'Dashboard',
  '重大新聞與事件': 'Major News & Events',
  '新聞與事件': 'News & Events',
  '跨市場指數': 'Cross-market Indexes',
  '籌碼排行': 'Positioning Leaders',
  '法人籌碼': 'Institutional Flow',
  '資料源註冊表': 'Data Source Registry',
  '來源註冊': 'Source Registry',
  '自選群組': 'Watchlist Groups',
  '自選清單': 'Watchlist',
  '提醒規則': 'Alert Rules',
  '提醒框架': 'Alert Framework',
  '個股 K 線與技術指標': 'Stock Candles & Technical Indicators',
  '時間橫軸 價格縱軸': 'Time on the horizontal axis; price on the vertical axis',
  'K 線、均線、布林通道與 MACD 圖表': 'Candles, moving averages, Bollinger Bands, and MACD chart',
  '個股事件流': 'Stock Event Stream',
  '個股新聞': 'Stock News',
  '基本面與籌碼': 'Fundamentals & Positioning',
  'AI 策略草案': 'AI Strategy Draft',
  '訊號規劃': 'Signal Planning',
  '即時行情監控': 'Realtime Market Monitor',
  '即時資料': 'Realtime Data',
  '監控訊號': 'Monitoring Signals',
  '證券資料庫搜尋': 'Security Database Search',
  '證券主檔': 'Security Master',
  '條件選股': 'Stock Screener',
  '執行篩選': 'Run Screen',
  '動能向上': 'Positive momentum',
  '籌碼偏多': 'Bullish positioning',
  '基本面穩定': 'Stable fundamentals',
  '風險較低': 'Lower risk',
  '新聞中心': 'News Center',
  '市場與個股': 'Markets & Stocks',
  '籌碼與融資券': 'Institutional & Margin Data',
  '法人與融資券': 'Institutions & Margin',
  '基本面中心': 'Fundamentals Center',
  '營收與財務': 'Revenue & Financials',
  'I. 交易功能': 'I. Trading',
  '僅供預覽': 'Preview Only',
  '股票代號，例如 2330.TW': 'Ticker, e.g. 2330.TW',
  '買進': 'Buy',
  '賣出': 'Sell',
  '更新交易預覽': 'Refresh Trade Preview',
  '風險與資產連動': 'Risk & Portfolio Linkage',
  '風控與資產': 'Risk & Portfolio',
  '模型研究': 'Model Research',
  '日報、自選與個股': 'Daily Brief, Watchlist & Stocks',
  '更新規則參考': 'Refresh Rule Reference',
  '通知與自選脈絡': 'Notifications & Watchlist Context',
  '通知與自選': 'Notifications & Watchlist',
  '量化策略研究': 'Quant Strategy Research',
  '策略與轉接器': 'Strategies & Adapters',
  '波段': 'Swing',
  '當沖': 'Intraday',
  '週線': 'Weekly',
  '月線': 'Monthly',
  '執行研究': 'Run Research',
  '盤前觀察': 'Pre-market Review',
  '盤中提醒': 'Intraday Alerts',
  '收盤檢討': 'Post-market Review',
  '執行批次': 'Run Batch',
  '策略決策流程': 'Strategy Decision Flow',
  '研究來源與證據': 'Research Sources & Evidence',
  '資料轉接器': 'Data Adapters',
  '技術細節': 'Technical Details',
  'K. 風控功能': 'K. Risk Control',
  '風險摘要': 'Risk Summary',
  '更新風險摘要': 'Refresh Risk Summary',
  '持倉快照': 'Position Snapshot',
  '部位快照': 'Position Snapshot',
  'L. 帳務與資產': 'L. Accounts & Assets',
  '本地預覽': 'Local Preview',
  '本區整合 I/J/K 的交易、建議、風控脈絡，並呈現本地資產預覽。': 'This area combines trading, advice, and risk context from I/J/K and presents a local portfolio preview.',
  '更新資產摘要': 'Refresh Portfolio Summary',
  '持倉明細': 'Position Details',
  '部位': 'Positions',
  'AI 虛擬資金訓練場': 'AI Virtual Capital Training Lab',
  '使用者與 Codex 都可用真實市場價格進行紙上買賣；研究與風控只提示、不阻擋實驗。': 'Both the user and Codex can paper trade using real market prices. Research and risk controls provide guidance without blocking experiments.',
  '初始虛擬資金': 'Initial Virtual Capital',
  '真實股票代號': 'Real Ticker',
  '例如 2330.TW': 'e.g. 2330.TW',
  '動作': 'Action',
  '加碼': 'Add',
  '全部賣出': 'Sell All',
  '減碼百分比': 'Reduce by Percentage',
  '買進金額 / 減碼 %': 'Buy Amount / Reduction %',
  '回合 ID': 'Episode ID',
  '系統自動建立': 'Created automatically',
  '實驗目標': 'Experiment Objective',
  '讓 Codex 測試選股、進出場與持倉管理假設': 'Let Codex test stock selection, entry/exit, and position-management hypotheses',
  '買賣理由': 'Trade Rationale',
  '在成交前留下原始理由，避免事後改寫。': 'Record the original rationale before execution to prevent hindsight rewriting.',
  '重設虛擬資金': 'Reset Virtual Capital',
  '開始學習回合': 'Start Learning Episode',
  '用真實價格模擬成交': 'Simulate Fill at Real Price',
  '真實行情重新估值': 'Revalue with Real Quotes',
  '計算本回合 Reward': 'Calculate Episode Reward',
  '結束回合': 'Close Episode',
  '正在讀取虛擬帳戶…': 'Loading virtual account…',
  '回合反思': 'Episode Reflection',
  '記錄成功、失敗、資料缺口、下一次要驗證的規則。': 'Record successes, failures, data gaps, and the rules to validate next.',
  '保存反思': 'Save Reflection',
  '只有真實存在的標的與已驗證的最後成交價／官方收盤價才能成交。Codex 不受研究或風控 Gate 阻擋，但不能超支、不能無券賣出，也不能自行填入價格。': 'Only real instruments with a verified last price or official close can be filled. Codex is not blocked by research or risk gates, but cannot overspend, short without holdings, or enter prices manually.',
  'M. 通知通道': 'M. Notification Channels',
  '訊息預覽': 'Message Preview',
  '發送 dry-run 檢查': 'Send Dry-run Check',
  '關聯推演': 'Linkage Analysis',
  '知識圖譜原型': 'Knowledge Graph Prototype',
  '影響': 'affects',
  '推演關聯': 'Analyze Linkage',
  '系統規格覆蓋': 'System Specification Coverage',
  '規格覆蓋': 'Specification Coverage',
  '資料目錄': 'Data Catalog',
  '資料目錄 JSON': 'Data Catalog JSON',
  '設定與連線中心': 'Settings & Connections',
  'Codex 帳號、Skills、MCP、資料 API 與介面偏好統一在這裡管理。': 'Manage the Codex account, Skills, MCP, data APIs, and interface preferences in one place.',
  '設定分類': 'Settings categories',
  '帳號與連線': 'Accounts & Connections',
  'Agent、模型與 API': 'Agent, Models & APIs',
  '介面風格': 'Interface Style',
  '背景光學': 'Backdrop Optics',
  '顯示與語言': 'Display & Language',
  '帳號、AI 與外部連線': 'Accounts, AI & External Connections',
  '登入、專案同步、Skills、MCP 與資料 API 集中管理；所有憑證只留在本機後端。': 'Manage sign-in, project sync, Skills, MCP, and data APIs in one place. All credentials remain in the local backend.',
  '重新檢查': 'Recheck',
  '連線摘要': 'Connection summary',
  '正在讀取帳號': 'Reading account',
  '本機可用能力': 'Locally available capabilities',
  '工具伺服器': 'Tool servers',
  '資料與通知連線': 'Data & notification connections',
  'Codex 帳號與專案': 'Codex Account & Project',
  'ChatGPT 登入、執行核心與工作目錄': 'ChatGPT sign-in, runtime, and working directory',
  '執行核心': 'Runtime',
  '登入帳號': 'Signed-in Account',
  '專案範圍': 'Project Scope',
  '完整工作目錄': 'Full Working Directory',
  'UI 操作': 'UI Control',
  '專案已由目前工作目錄載入。': 'The project is loaded from the current working directory.',
  '重新同步專案': 'Resync Project',
  '登入 Codex': 'Sign in to Codex',
  '登出 Codex': 'Sign Out of Codex',
  'Codex 與專案本機技能': 'Codex and project-local skills',
  '正在掃描本機 Skills…': 'Scanning local Skills…',
  'MCP 工具連線': 'MCP Tool Connections',
  '由 Codex App Server 管理': 'Managed by Codex App Server',
  '正在檢查 MCP 設定…': 'Checking MCP configuration…',
  '資料 API 與通知': 'Data APIs & Notifications',
  '只顯示連線狀態，不顯示金鑰': 'Shows connection status only; keys are never displayed',
  '正在檢查 API 狀態…': 'Checking API status…',
  'Stock AI Agent 執行核心': 'Stock AI Agent Runtime',
  'Stock AI Agent Runtime 保留相同工具、權限與稽核層；你可以替換負責規劃的模型或外部 Agent。': 'Stock AI Agent Runtime keeps the same tools, permissions, and audit layer while letting you replace the planning model or external Agent.',
  '目前統一由已登入的 Codex 驅動。FinGPT 與其他本地／外部模型不會下載、載入或啟動。': 'The signed-in Codex instance is the sole active driver. FinGPT and other local or external models are not downloaded, loaded, or started.',
  '模型提供者': 'Model Provider',
  'Codex App Server': 'Codex App Server',
  'OpenAI 相容／本地模型': 'OpenAI-compatible / Local Model',
  '外部 Agent Framework': 'External Agent Framework',
  'Codex（唯一啟用）': 'Codex (Only Active Provider)',
  '原生能力': 'Native Capabilities',
  '保留': 'Preserved',
  '真實交易': 'Live Trading',
  '關閉': 'Disabled',
  'Codex 只負責規劃與推理；專案、終端機、網站、Skills、MCP、UI 與排程由 Stock AI Agent Runtime 在本介面直接執行並留下事件紀錄。': 'Codex handles planning and reasoning. The Stock AI Agent Runtime directly executes project, terminal, website, Skills, MCP, UI, and scheduling actions in this interface and records the events.',
  'OpenAI 相容／本地模型連線': 'OpenAI-compatible / Local Model Connection',
  '模型名稱': 'Model Name',
  '本機服務不需要金鑰': 'Local service does not require a key',
  '逾時秒數': 'Timeout (seconds)',
  '金鑰只保存於 macOS Keychain，不會回傳到頁面。': 'The key is stored only in macOS Keychain and is never returned to the page.',
  'Bearer Token': 'Bearer Token',
  'Token 只保存於 macOS Keychain，不會回傳到頁面。': 'The token is stored only in macOS Keychain and is never returned to the page.',
  '被選定的模型只負責規劃與推理；專案、終端機、網站、Skills、MCP、UI、風控與排程仍由 Stock AI Agent Runtime 執行並留下相同事件紀錄。Codex 原生能力不會因切換模型而移除。': 'The selected model handles planning and reasoning only. Stock AI Agent Runtime still executes project, terminal, web, Skills, MCP, UI, risk, and scheduling actions with the same audit trail. Switching models never removes native Codex capabilities.',
  '支援的連接方式': 'Supported Connection Methods',
  'OpenAI-compatible 可連接相容雲端 API、Ollama gateway、vLLM 或其他本機服務；外部 Agent protocol 可連接 Hermes、LangGraph、AutoGen、CrewAI 或自訂 Agent server。Anthropic／Gemini 可透過相容 gateway 或外部 Agent 接入。': 'OpenAI-compatible connects to compatible cloud APIs, Ollama gateways, vLLM, or other local services. The external Agent protocol connects Hermes, LangGraph, AutoGen, CrewAI, or custom Agent servers. Anthropic and Gemini can connect through compatible gateways or an external Agent.',
  '測試目前連線': 'Test Current Connection',
  '儲存 Agent 設定': 'Save Agent Settings',
  '正在載入模型提供者設定。': 'Loading model provider settings.',
  '未啟用的模型介面': 'Inactive Model Interfaces',
  'OpenAI 相容、本地模型、外部 Agent、Anthropic 與 Gemini 目前只保留未來相容契約；程序未啟動、模型未載入、請求數為 0。': 'OpenAI-compatible, local-model, external-Agent, Anthropic, and Gemini interfaces are retained only as future compatibility contracts. No process is running, no model is loaded, and the request count is zero.',
  '查看主系統工具分類': 'View Main System Tool Categories',
  'Codex 已設為固定模型提供者。': 'Codex is configured as the fixed model provider.',
  '不使用 Codex 繼續': 'Continue without Codex',
  '可先使用市場與研究功能，之後再到「設定 → Agent、模型與 API」登入 Codex 或連接其他模型。': 'Use market and research features now, then sign in to Codex or connect another model later under Settings → Agent, Models & APIs.',
  '背景與介面風格': 'Backdrop and interface style',
  '繁體中文': 'Traditional Chinese',
  '舒適': 'Comfortable',
  '緊湊': 'Compact',
  '正在即時預覽，套用後會保存到這台裝置。': 'Previewing live. Apply to save these settings on this device.',
  '恢復預設': 'Restore Defaults',
  '套用設定': 'Apply Settings',
  '目前僅提供唯讀預覽。': 'Read-only preview is currently available.',
  '未接券商 API 前，僅可檢視預覽與成本估算。': 'Until a brokerage API is connected, only previews and cost estimates are available.',
  '委託流程目前固定為預覽模式': 'The order workflow is currently locked to preview mode',
  '本機預覽資產摘要': 'Local preview portfolio summary',
  '部位、已實現損益與股利收入目前為預覽或推估值': 'Positions, realized P&L, and dividend income are currently previewed or estimated values',
  'K線圖模式': 'Candlestick chart mode',
  '最新K：': 'Latest candle:',
  '價格': 'Price',
  '時間': 'Time',
  '成交量': 'Volume',
  '盤中即時行情': 'Realtime intraday quotes',
  '無資料': 'No data',
  '暫無資料': 'No data available',
  '查無資料': 'No matching data',
  '讀取失敗': 'Failed to load',
  '載入失敗': 'Failed to load',
  '執行失敗': 'Execution failed',
  '連線失敗': 'Connection failed',
  '更新失敗': 'Update failed',
  '成功': 'Success',
  '失敗': 'Failed',
  '已完成': 'Completed',
  '完成': 'Complete',
  '執行中': 'Running',
  '準備中': 'Preparing',
  '待命': 'Standby',
  '離線': 'Offline',
  '未設定': 'Not configured',
  '未連線': 'Not connected',
  '已連線': 'Connected',
  '已啟用': 'Enabled',
  '未啟用': 'Disabled',
  '可用': 'Available',
  '不可用': 'Unavailable',
  '重新整理': 'Refresh',
  '更新': 'Update',
  '取消': 'Cancel',
  '確認': 'Confirm',
  '儲存': 'Save',
  '保存': 'Save',
  '刪除': 'Delete',
  '詳細資料': 'Details',
  '查看': 'View',
  '開啟': 'Open',
  '返回': 'Back',
  '下一步': 'Next Step',
  '理由': 'Rationale',
  '結論': 'Conclusion',
  '建議': 'Recommendation',
  '風險': 'Risk',
  '來源': 'Sources',
  '狀態': 'Status',
  '名稱': 'Name',
  '代號': 'Ticker',
  '市場': 'Market',
  '日期': 'Date',
  '類型': 'Type',
  '數量': 'Quantity',
  '成本': 'Cost',
  '損益': 'P&L',
  '報酬率': 'Return',
  '說明': 'Description',
});

const CONTROL_VALUE_TRANSLATIONS = Object.freeze({
  questionInput: {},
  paperTrainingObjective: {
    '讓 Codex 測試選股、進出場與持倉管理假設': 'Let Codex test stock selection, entry/exit, and position-management hypotheses',
  },
});

const ORIGINAL_TEXT = new WeakMap();
const ORIGINAL_ATTRIBUTES = new WeakMap();
const ORIGINAL_CONTROL_VALUES = new WeakMap();
const TRANSLATABLE_ATTRIBUTES = ['placeholder', 'title', 'aria-label', 'data-title'];
const HAN_TEXT_PATTERN = /[\u3400-\u9fff]/;
let uiTranslationObserver = null;
let applyingUiTranslation = false;

function loadUiSettings() {
  try {
    const saved = JSON.parse(localStorage.getItem(UI_SETTINGS_KEY) || '{}');
    return {
      ...DEFAULT_UI_SETTINGS,
      theme: saved.theme || DEFAULT_UI_SETTINGS.theme,
      backdrop: saved.backdrop || (saved.backdropPattern === false ? 'none' : DEFAULT_UI_SETTINGS.backdrop),
      language: saved.language || DEFAULT_UI_SETTINGS.language,
      density: saved.density || DEFAULT_UI_SETTINGS.density,
      fontScale: normalizeFontScale(saved.fontScale),
      glassTransparency: normalizeGlassTransparency(saved.glassTransparency),
    };
  } catch {
    return { ...DEFAULT_UI_SETTINGS };
  }
}

let uiSettings = loadUiSettings();

function normalizeFontScale(value) {
  const raw = Number(value || DEFAULT_UI_SETTINGS.fontScale);
  const percent = raw > 0 && raw <= 3 ? raw * 100 : raw;
  return Math.min(115, Math.max(90, Math.round(percent * 10) / 10));
}

function normalizeGlassTransparency(value) {
  const raw = Number(value ?? DEFAULT_UI_SETTINGS.glassTransparency);
  return Math.min(95, Math.max(0, Math.round(raw * 10) / 10));
}

function applyGlassTransparency(value) {
  const transparency = normalizeGlassTransparency(value);
  const visibleTint = 1 - transparency / 100;
  const materialOpacity = 0.075 + visibleTint * 0.36;
  const chartDark = 0.025 + visibleTint * 0.16;
  const chartLight = 0.035 + visibleTint * 0.20;
  const root = document.documentElement;
  root.style.setProperty('--ui-glass-material-opacity', materialOpacity.toFixed(4));
  root.style.setProperty('--ui-glass-chart-dark-alpha', chartDark.toFixed(4));
  root.style.setProperty('--ui-glass-chart-light-alpha', chartLight.toFixed(4));
  root.dataset.uiGlassTransparency = String(transparency);
}

function preserveOuterWhitespace(source, translated) {
  const leading = source.match(/^\s*/)?.[0] || '';
  const trailing = source.match(/\s*$/)?.[0] || '';
  return `${leading}${translated}${trailing}`;
}

function translateUiString(source) {
  if (typeof source !== 'string' || !HAN_TEXT_PATTERN.test(source)) return source;
  const trimmed = source.trim();
  const exact = FULL_UI_TEXT_EN[trimmed];
  if (exact) return preserveOuterWhitespace(source, exact);

  const patterns = [
    [/^(\d+)\s*檔$/, (_, count) => `${count} stocks`],
    [/^(\d+)\s*檔立即$/, (_, count) => `${count} immediate`],
    [/^(\d+)\s*項$/, (_, count) => `${count} items`],
    [/^(\d+)\s*筆$/, (_, count) => `${count} records`],
    [/^(\d+)\s*則$/, (_, count) => `${count} entries`],
    [/^共\s*(\d+)\s*檔$/, (_, count) => `${count} stocks total`],
    [/^更新時間[：:]\s*(.+)$/, (_, value) => `Updated: ${value}`],
    [/^資料時間[：:]\s*(.+)$/, (_, value) => `Data time: ${value}`],
    [/^來源[：:]\s*(.+)$/, (_, value) => `Source: ${value}`],
    [/^狀態[：:]\s*(.+)$/, (_, value) => `Status: ${value}`],
    [/^錯誤[：:]\s*(.+)$/, (_, value) => `Error: ${value}`],
    [/^第\s*(\d+)\s*回合$/, (_, value) => `Episode ${value}`],
  ];
  for (const [pattern, replacer] of patterns) {
    if (pattern.test(trimmed)) return preserveOuterWhitespace(source, trimmed.replace(pattern, replacer));
  }
  return source;
}

function shouldSkipTranslationNode(node) {
  const parent = node.nodeType === Node.TEXT_NODE ? node.parentElement : node;
  if (!parent) return true;
  return Boolean(parent.closest('script,style,noscript,code,pre,textarea,[contenteditable="true"],[data-i18n-skip],.json-box,.answer'));
}

function translateTextNode(node, language) {
  if (shouldSkipTranslationNode(node)) return;
  if (language === 'en') {
    const current = node.nodeValue || '';
    if (!HAN_TEXT_PATTERN.test(current)) return;
    const translated = translateUiString(current);
    if (translated === current) return;
    ORIGINAL_TEXT.set(node, current);
    node.nodeValue = translated;
    return;
  }
  const original = ORIGINAL_TEXT.get(node);
  if (typeof original === 'string') node.nodeValue = original;
}

function translateElementAttributes(element, language) {
  if (!(element instanceof Element) || element.closest('[data-i18n-skip]')) return;
  let originals = ORIGINAL_ATTRIBUTES.get(element);
  if (!originals) originals = {};
  for (const attribute of TRANSLATABLE_ATTRIBUTES) {
    if (!element.hasAttribute(attribute)) continue;
    if (language === 'en') {
      const current = element.getAttribute(attribute) || '';
      if (!HAN_TEXT_PATTERN.test(current)) continue;
      const translated = translateUiString(current);
      if (translated === current) continue;
      originals[attribute] = current;
      element.setAttribute(attribute, translated);
    } else if (Object.prototype.hasOwnProperty.call(originals, attribute)) {
      element.setAttribute(attribute, originals[attribute]);
    }
  }
  if (Object.keys(originals).length) ORIGINAL_ATTRIBUTES.set(element, originals);
}

function translateControlDefaults(language) {
  Object.entries(CONTROL_VALUE_TRANSLATIONS).forEach(([id, translations]) => {
    const control = $(id);
    if (!control) return;
    const source = Object.keys(translations)[0];
    const english = translations[source];
    if (!ORIGINAL_CONTROL_VALUES.has(control)) ORIGINAL_CONTROL_VALUES.set(control, source);
    if (language === 'en' && (control.value === source || control.value === english)) control.value = english;
    if (language !== 'en' && (control.value === source || control.value === english)) control.value = ORIGINAL_CONTROL_VALUES.get(control) || source;
  });
}

function translateUiTree(root, language) {
  if (!root) return;
  const rootElement = root.nodeType === Node.ELEMENT_NODE ? root : root.parentElement;
  if (root.nodeType === Node.TEXT_NODE) translateTextNode(root, language);
  if (rootElement instanceof Element) translateElementAttributes(rootElement, language);

  const textWalker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let textNode = textWalker.nextNode();
  while (textNode) {
    translateTextNode(textNode, language);
    textNode = textWalker.nextNode();
  }

  if (root.querySelectorAll) {
    root.querySelectorAll(TRANSLATABLE_ATTRIBUTES.map((attribute) => `[${attribute}]`).join(','))
      .forEach((element) => translateElementAttributes(element, language));
  }
}

function startUiTranslationObserver() {
  if (uiTranslationObserver || !document.body) return;
  uiTranslationObserver = new MutationObserver((mutations) => {
    if (applyingUiTranslation || uiSettings.language !== 'en') return;
    applyingUiTranslation = true;
    try {
      mutations.forEach((mutation) => {
        if (mutation.type === 'characterData') {
          translateTextNode(mutation.target, 'en');
          return;
        }
        if (mutation.type === 'attributes') {
          translateElementAttributes(mutation.target, 'en');
          return;
        }
        mutation.addedNodes.forEach((node) => translateUiTree(node, 'en'));
      });
    } finally {
      applyingUiTranslation = false;
    }
  });
  uiTranslationObserver.observe(document.body, {
    subtree: true,
    childList: true,
    characterData: true,
    attributes: true,
    attributeFilter: TRANSLATABLE_ATTRIBUTES,
  });
}

function translateInterface(language = uiSettings.language) {
  const lang = UI_TEXT[language] ? language : 'zh-Hant';
  const text = UI_TEXT[lang];
  const shell = SHELL_TEXT[lang];
  applyingUiTranslation = true;
  try {
    document.documentElement.lang = lang;
    document.title = lang === 'en' ? FULL_UI_TEXT_EN['股市AI系統 | 市場操作台'] : '股市AI系統 | 市場操作台';
    document.querySelectorAll('[data-i18n]').forEach((element) => {
      const value = text[element.dataset.i18n];
      if (value) element.textContent = value;
    });
    document.querySelector('.brand h1').textContent = shell.brand;
    document.querySelector('.brand p').textContent = shell.subbrand;
    document.querySelector('.side-card small').textContent = shell.sourceStatus;
    document.querySelector('.eyebrow').textContent = shell.eyebrow;
    if ($('globalAgentPrompt')) $('globalAgentPrompt').placeholder = shell.placeholder;
    if ($('globalAgentSend')) $('globalAgentSend').textContent = shell.search;
    document.querySelectorAll('.design-strip > span').forEach((element, index) => { element.textContent = shell.strip[index] || ''; });
    document.querySelectorAll('.sidebar .nav-btn').forEach((button) => {
      const label = NAV_TEXT[lang][button.dataset.view] || button.dataset.view;
      const textNode = [...button.childNodes].find((node) => node.nodeType === Node.TEXT_NODE && node.textContent.trim());
      if (textNode) textNode.nodeValue = label;
      button.dataset.title = label;
      if (button.hasAttribute('title')) button.title = label;
    });
    const current = currentViewId();
    if ($('viewTitle')) $('viewTitle').textContent = NAV_TEXT[lang][current] || current;
    translateUiTree(document.body, lang);
    translateControlDefaults(lang);
  } finally {
    applyingUiTranslation = false;
  }
}

function updateSettingsOutputs(settings) {
  const formatPercent = (value) => Number(value).toLocaleString(undefined, { maximumFractionDigits: 1 });
  if ($('uiFontScaleValue')) $('uiFontScaleValue').value = `${formatPercent(settings.fontScale)}%`;
  if ($('uiGlassTransparencyValue')) $('uiGlassTransparencyValue').value = `${formatPercent(settings.glassTransparency)}%`;
  const themeName = UI_TEXT[settings.language]?.[`theme.${settings.theme}`] || settings.theme;
  const backdropName = UI_TEXT[settings.language]?.[`backdrop.${settings.backdrop}`] || settings.backdrop;
  // Theme and backdrop are meaningful only on the dedicated 介面設定 tab.
  // Avoid leaking appearance state into Agent／模型, Skills, or API pages.
  const isSettingsTab = document.documentElement?.dataset?.workspace === 'system'
    && document.documentElement?.dataset?.workspaceTab === 'interface';
  if ($('settingsProfileChip') && isSettingsTab) $('settingsProfileChip').textContent = `${themeName} / ${backdropName}`;
}

function fillSettingsForm(settings) {
  $$('input[name="uiTheme"]').forEach((input) => { input.checked = input.value === settings.theme; });
  $$('input[name="uiBackdrop"]').forEach((input) => { input.checked = input.value === settings.backdrop; });
  if ($('uiLanguage')) $('uiLanguage').value = settings.language;
  if ($('uiDensity')) $('uiDensity').value = settings.density;
  if ($('uiFontScale')) $('uiFontScale').value = settings.fontScale;
  if ($('uiGlassTransparency')) $('uiGlassTransparency').value = settings.glassTransparency;
  updateSettingsOutputs(settings);
}

function readSettingsForm() {
  return {
    theme: appShell().querySelector('input[name="uiTheme"]:checked')?.value || 'exchange',
    backdrop: appShell().querySelector('input[name="uiBackdrop"]:checked')?.value || 'constellation',
    language: $('uiLanguage')?.value || 'zh-Hant',
    density: $('uiDensity')?.value || 'comfortable',
    fontScale: Number($('uiFontScale')?.value || 100),
    glassTransparency: Number($('uiGlassTransparency')?.value ?? DEFAULT_UI_SETTINGS.glassTransparency),
  };
}

function applyUiSettings(settings, { syncRenderer = true } = {}) {
  uiSettings = {
    ...DEFAULT_UI_SETTINGS,
    ...settings,
    fontScale: normalizeFontScale(settings.fontScale),
    glassTransparency: normalizeGlassTransparency(settings.glassTransparency),
  };
  const root = document.documentElement;
  root.dataset.uiTheme = uiSettings.theme;
  applyChartTheme(uiSettings.theme);
  root.dataset.uiBackdrop = uiSettings.backdrop;
  root.dataset.uiDensity = uiSettings.density;
  root.style.setProperty('--ui-font-scale', String(uiSettings.fontScale / 100));
  applyGlassTransparency(uiSettings.glassTransparency);
  translateInterface(uiSettings.language);
  updateSettingsOutputs(uiSettings);
  if ($('priceChart')) renderCurrentChart();
  window.HomeMarketWorkspace?.syncChartPreview?.();
  if (syncRenderer) {
    initDynamicGlassInteractions({ pointerMotion: !window.matchMedia('(prefers-reduced-motion: reduce)').matches });
    window.__liquidGLRenderer__?.render?.();
    window.__glassSamplerController?.scheduleCapture?.(40);
  }
}

function initUiSettings() {
  fillSettingsForm(uiSettings);
  applyUiSettings(uiSettings, { syncRenderer: false });
  startUiTranslationObserver();
  const preview = () => applyUiSettings(readSettingsForm());
  const smoothRanges = [$('uiFontScale'), $('uiGlassTransparency')].filter(Boolean);
  let rangePreviewFrame = 0;
  const previewRange = () => {
    cancelAnimationFrame(rangePreviewFrame);
    rangePreviewFrame = requestAnimationFrame(() => {
      const next = readSettingsForm();
      document.documentElement.style.setProperty('--ui-font-scale', String(normalizeFontScale(next.fontScale) / 100));
      applyGlassTransparency(next.glassTransparency);
      updateSettingsOutputs(next);
    });
  };
  smoothRanges.forEach((control) => {
    control.addEventListener('input', previewRange, { passive: true });
    control.addEventListener('change', preview);
  });
  $$('#settings input:not([type="range"]),#settings select').forEach((control) => control.addEventListener('input', preview));
  $('saveSettingsBtn')?.addEventListener('click', () => {
    const next = readSettingsForm();
    localStorage.setItem(UI_SETTINGS_KEY, JSON.stringify(next));
    applyUiSettings(next);
    $('settingsStatus').textContent = UI_TEXT[next.language].saved;
  });
  $('resetSettingsBtn')?.addEventListener('click', () => {
    const defaults = { ...DEFAULT_UI_SETTINGS };
    localStorage.setItem(UI_SETTINGS_KEY, JSON.stringify(defaults));
    fillSettingsForm(defaults);
    applyUiSettings(defaults);
    $('settingsStatus').textContent = UI_TEXT[defaults.language].reset;
  });
  new MutationObserver(() => {
    syncChartThemeFromDom($('priceChart'));
    renderCurrentChart();
  }).observe(document.documentElement, { attributes: true, attributeFilter: ['data-ui-theme'] });
}

function configureGlassPreferences() {
  const params = new URLSearchParams(window.location.search);
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const reducedTransparency = window.matchMedia('(prefers-reduced-transparency: reduce)').matches;
  const highContrast = window.matchMedia('(prefers-contrast: more), (forced-colors: active)').matches;
  const mobile = window.matchMedia('(max-width: 1060px)').matches;
  const forceCss = ['css', 'off'].includes(params.get('glass'));
  const requestedQuality = params.get('glass-quality') || 'ultra';
  const constrainedDevice = (navigator.hardwareConcurrency && navigator.hardwareConcurrency <= 4)
    || (navigator.deviceMemory && navigator.deviceMemory <= 4);
  const quality = requestedQuality === 'low' || mobile || constrainedDevice
    ? 1
    : requestedQuality === 'balanced' ? 2 : 3;
  const root = document.documentElement;
  const platform = String(navigator.userAgentData?.platform || navigator.platform || navigator.userAgent || '').toLowerCase();
  root.dataset.uiPlatform = root.dataset.nativeLiquidGlass === 'appkit'
    ? 'macos'
    : platform.includes('win') ? 'windows'
      : platform.includes('mac') ? 'macos-web'
        : platform.includes('linux') ? 'linux' : 'web';
  root.dataset.glassMotion = reducedMotion ? 'reduced' : 'full';
  root.dataset.glassTransparency = reducedTransparency || highContrast ? 'reduced' : 'full';
  root.dataset.glassQuality = quality === 1 ? 'low' : quality === 2 ? 'high' : 'ultra';
  root.dataset.glassMode = forceCss || mobile || highContrast ? 'css' : 'pending';
  return {
    allowWebGL: !(forceCss || mobile || highContrast),
    pointerMotion: !reducedMotion,
    quality,
  };
}
