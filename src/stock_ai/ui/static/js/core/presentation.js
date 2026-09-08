function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function signalLabel(signal) {
  if (signal === 'buy') return '<span class="tag positive">買進候選</span>';
  if (signal === 'watch') return '<span class="tag neutral">觀察</span>';
  return '<span class="tag negative">保守</span>';
}

function renderEmptyBlock(title, detail) {
  return `<div class="event"><h4>${escapeHtml(title)}</h4><p>${escapeHtml(detail)}</p></div>`;
}

function safeUrl(url) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:') return parsed.toString();
  } catch {}
  return '';
}

function compactText(value, maxLength = 160) {
  const text = String(value || '')
    .replace(/&lt;a\s+[^&]*&gt;([\s\S]*?)&lt;\/a&gt;/gi, ' $1 ')
    .replace(/<a\s+[^>]*>([\s\S]*?)<\/a>/gi, ' $1 ')
    .replace(/https?:\/\/\S+/gi, '')
    .replace(/<[^>]+>/g, ' ')
    .replace(/&lt;[^&]*&gt;/gi, ' ')
    .replace(/\b[A-Za-z0-9.-]+\.(?:com|tw|net|org)\b\s*(?:--|[-:：])?\s*/gi, ' ')
    .replace(/(?:Yahoo\s*財經|Yahoo股市|news\.cnyes\.com|sinotrade\.com\.tw|Google News|MoneyDJ|工商時報|經濟日報|鉅亨網|udn\.com|ctee\.com\.tw)/gi, ' ')
    .replace(/\s*(?:[-|｜]|來源[:：])\s*(?:Yahoo\s*財經|Yahoo股市|news\.cnyes\.com|sinotrade\.com\.tw|Google News|MoneyDJ|工商時報|經濟日報|鉅亨網|udn\.com|ctee\.com\.tw)\s*$/gi, ' ')
    .replace(/\s*(?:[-|｜]|來源[:：])\s*[\u4e00-\u9fffA-Za-z0-9.&'()\/-]{2,24}\.(?:com|tw|net|org)\s*$/gi, ' ')
    .replace(/\s*(?:[-|｜]|來源[:：])\s*[\u4e00-\u9fffA-Za-z0-9.&'()\/-]{2,24}(?:新聞網|新聞|財經|股市|日報|時報|News|Information)\s*$/gi, ' ')
    .replace(/\s*[\u4e00-\u9fffA-Za-z0-9.&'()\/-]{2,24}\.(?:com|tw|net|org)\s*$/gi, ' ')
    .replace(/\s*[\u4e00-\u9fffA-Za-z0-9.&'()\/-]{2,24}(?:新聞網|新聞|財經|股市|日報|時報|News|Information)\s*$/gi, ' ')
    .replace(/\s*來源[:：]\s*$/gi, ' ')
    .replace(/&nbsp;/gi, ' ')
    .replace(/&amp;/gi, '&')
    .replace(/\s+/g, ' ')
    .trim();
  if (/<a\s+href|&lt;a\s+href/i.test(text)) return '';
  if (!text) return '';
  return text.length > maxLength ? `${text.slice(0, maxLength - 1)}...` : text;
}

function renderNewsSummaryLink(summary, url, fallback = '閱讀全文') {
  const text = compactText(summary || '', 180) || compactText(fallback || '', 180) || '閱讀全文';
  const href = safeUrl(url);
  if (!href) return escapeHtml(text);
  return `<a class="news-link" href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(text)}</a>`;
}

function displaySourceLabel(value) {
  const raw = String(value || '').trim();
  if (!raw) return '';
  const url = safeUrl(raw);
  if (url) {
    try {
      const host = new URL(url).hostname.replace(/^www\./i, '').toLowerCase();
      if (host.includes('twse.com.tw')) return 'TWSE 官方';
      if (host.includes('mops.twse.com.tw')) return 'MOPS';
      if (host.includes('news.google.com')) return 'Google News 摘要';
      if (host.includes('finance.yahoo.com') || host.includes('yahoo.com')) return 'Yahoo 新聞摘要';
      if (host.includes('cnyes.com')) return '鉅亨新聞摘要';
      if (host.includes('moneydj.com')) return 'MoneyDJ 新聞摘要';
      if (host.includes('udn.com')) return '經濟日報摘要';
      if (host.includes('ctee.com.tw')) return '工商時報摘要';
      return '新聞/公告來源';
    } catch {}
  }
  return compactText(raw, 40).replace(/The Information/gi, '').trim() || '資料來源';
}

function eventTypeLabel(eventType) {
  const key = String(eventType || '').toLowerCase();
  if (key === 'material_event') return '重大訊息';
  if (key === 'announcement') return '公告';
  if (key === 'news') return '新聞';
  if (key === 'industry') return '產業';
  if (key === 'macro') return '總經';
  return key || '事件';
}
