(() => {
  const MAX_ENTRIES = 50;
  const entries = new Map();
  const pending = new Map();

  function get(key) {
    const entry = entries.get(key);
    if (!entry) return null;
    if (entry.expiresAt && entry.expiresAt < Date.now()) {
      entries.delete(key);
      return null;
    }
    entries.delete(key);
    entries.set(key, entry);
    return entry.value;
  }

  function set(key, value, ttlMs = 5 * 60 * 1000) {
    entries.delete(key);
    entries.set(key, { value, expiresAt: ttlMs > 0 ? Date.now() + ttlMs : 0 });
    while (entries.size > MAX_ENTRIES) entries.delete(entries.keys().next().value);
    return value;
  }

  async function singleFlight(key, loader, { ttlMs = 5 * 60 * 1000, force = false } = {}) {
    if (!force) {
      const cached = get(key);
      if (cached !== null) return cached;
    }
    if (pending.has(key)) return pending.get(key);
    const request = Promise.resolve()
      .then(loader)
      .then(value => set(key, value, ttlMs))
      .finally(() => pending.delete(key));
    pending.set(key, request);
    return request;
  }

  function invalidate(prefix = '') {
    [...entries.keys()].forEach(key => {
      if (!prefix || String(key).startsWith(prefix)) entries.delete(key);
    });
  }

  window.StockWorkspaceCache = { get, set, singleFlight, invalidate };
})();
