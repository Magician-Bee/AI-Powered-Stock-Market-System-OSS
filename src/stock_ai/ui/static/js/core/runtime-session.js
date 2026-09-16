(() => {
  const token = document.querySelector('meta[name="stock-ai-runtime-session"]')?.content || '';
  const nativeFetch = window.fetch.bind(window);
  const requiresRuntimeSession = path => ['/api', '/agent/autonomy']
    .some(prefix => path === prefix || path.startsWith(`${prefix}/`));
  window.fetch = async (input, init = {}) => {
    const rawUrl = input instanceof Request ? input.url : String(input);
    const url = new URL(rawUrl, window.location.href);
    if (!token || url.origin !== window.location.origin || !requiresRuntimeSession(url.pathname)) {
      return nativeFetch(input, init);
    }
    const headers = new Headers(input instanceof Request ? input.headers : undefined);
    new Headers(init.headers || {}).forEach((value, key) => headers.set(key, value));
    headers.set('X-Stock-AI-Session', token);
    const response = await nativeFetch(input, { ...init, headers });
    if (response.status === 403) {
      const lastReload = Number(sessionStorage.getItem('stockAiSessionReloadAt') || 0);
      if (Date.now() - lastReload > 10_000) {
        sessionStorage.setItem('stockAiSessionReloadAt', String(Date.now()));
        window.location.reload();
      }
    }
    return response;
  };
})();
