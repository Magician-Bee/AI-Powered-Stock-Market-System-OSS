(function () {
  'use strict';

  const labels = {
    proposed: '提議',
    pending: '待執行',
    idle: '待命',
    ready: '可執行',
    queued: '排隊中',
    planning: '規劃中',
    running: '執行中',
    recovery_pending: '已保留進度，準備自動修復',
    repairing: '正在從 Checkpoint 修復',
    retrying: '重試中',
    waiting_user_input: '等待補充資訊',
    waiting_decision: '等待使用者決策',
    waiting_approval: '等待批准',
    paused: '已暫停',
    suspended: '已暫停',
    completed: '已完成',
    partially_completed: '部分完成，仍在等待修復',
    max_steps_reached: '已達步驟上限，尚未完成',
    failed: '失敗',
    blocked: '阻塞',
    skipped: '略過',
    cancelled: '已取消',
  };
  const icons = {
    proposed: '◌', pending: '○', idle: '○', ready: '◉', queued: '◷', planning: '◷',
    running: '●', recovery_pending: '↻', repairing: '↻', retrying: '↻', waiting_user_input: '？', waiting_decision: '◇',
    waiting_approval: '🔒', paused: 'Ⅱ',
    suspended: 'Ⅱ', completed: '✓', partially_completed: '⚠', max_steps_reached: '⚠', failed: '!', blocked: '⚠',
    skipped: '–', cancelled: '×',
  };
  const secretParts = [
    'password', 'secret', 'token', 'api_key', 'authorization', 'cookie',
    'credential', 'private_key',
  ];

  function redact(value) {
    if (Array.isArray(value)) return value.map(redact);
    if (!value || typeof value !== 'object') return value;
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [
      key,
      secretParts.some(part => key.toLowerCase().includes(part)) ? '[redacted]' : redact(item),
    ]));
  }

  function text(value, fallback = '') {
    if (value === null || value === undefined) return fallback;
    if (typeof value === 'string') return value;
    try { return JSON.stringify(redact(value), null, 2); } catch { return fallback; }
  }

  function time(value) {
    if (!value) return '';
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime())
      ? String(value)
      : new Intl.DateTimeFormat('zh-TW', {
        hour: '2-digit', minute: '2-digit', second: '2-digit',
      }).format(parsed);
  }

  function status(value) {
    const key = String(value || 'pending');
    return { key, label: labels[key] || key, icon: icons[key] || '•' };
  }

  function duration(ms) {
    const value = Number(ms || 0);
    if (!value) return '';
    return value < 1000 ? `${value} ms` : `${(value / 1000).toFixed(1)} 秒`;
  }

  window.AgentDockFormatters = { duration, redact, status, text, time };
}());
