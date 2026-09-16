(() => {
  let stream = null;

  function bootstrap({ force = false } = {}) {
    return StockWorkspaceCache.singleFlight(
      'workspace:bootstrap',
      () => api('/api/workspace/bootstrap'),
      { ttlMs: 30_000, force },
    );
  }

  async function refresh(trigger = {}) {
    const response = await api('/api/intelligence/scans?wait=true', {
      method: 'POST',
      body: JSON.stringify({
        event_type: 'user_requested',
        mode: 'deterministic_whole_market',
        llm_per_symbol: false,
        ...trigger,
      }),
    });
    StockWorkspaceCache.invalidate('workspace:');
    return response.snapshot;
  }

  function connect(onSnapshot) {
    stream?.close();
    stream = openSecuredEventStream(
      '/api/workspace/stream',
      new Set(['snapshot.updated']),
      event => {
        try {
          const detail = JSON.parse(event.data);
          StockWorkspaceCache.invalidate('workspace:');
          onSnapshot?.(detail);
        } catch (error) {
          console.error('Workspace snapshot event failed', error);
        }
      },
      () => {
        // Authenticated fetch streaming reconnects without clearing the snapshot.
      },
    );
    return stream;
  }

  window.MarketIntelligenceSnapshotService = { bootstrap, refresh, connect };
})();
