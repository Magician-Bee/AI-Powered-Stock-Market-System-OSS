(function () {
  'use strict';

  function isTerminalRun(run) {
    if (window.AgentEventReducer?.isTerminalRun) return window.AgentEventReducer.isTerminalRun(run);
    // Do not regress to the old unconditional check:
    // ['completed', 'partially_completed', 'max_steps_reached', 'failed', 'cancelled', 'interrupted'].includes(status)
    // because recoverable partial/max-step checkpoints must keep streaming.
    return ['completed', 'partially_completed', 'max_steps_reached', 'failed', 'cancelled', 'interrupted']
      .includes(String(run?.status || '')) && run?.recovery_pending !== true;
  }

  class AgentStreamController {
    constructor(store) {
      this.store = store;
      this.controller = null;
      this.runId = null;
      this.retryCount = 0;
      this.stopped = false;
      this.generation = 0;
      this.reconcileTimer = null;
    }

    stop() {
      this.generation += 1;
      this.stopped = true;
      if (this.reconcileTimer) clearTimeout(this.reconcileTimer);
      this.reconcileTimer = null;
      this.controller?.abort();
      this.controller = null;
      this.runId = null;
    }

    async connect(runId) {
      this.stop();
      this.stopped = false;
      this.runId = runId;
      this.retryCount = 0;
      this.scheduleReconcile(this.generation);
      return this.open(this.generation);
    }

    scheduleReconcile(generation) {
      if (this.reconcileTimer) clearTimeout(this.reconcileTimer);
      this.reconcileTimer = setTimeout(() => this.reconcile(generation), 3500);
    }

    async reconcile(generation) {
      const runId = this.runId;
      if (!runId || this.stopped || generation !== this.generation) return;
      try {
        const last = Number(this.store.getState().last_sequence_by_run[runId] || 0);
        const response = await (window.AgentRuntimeFetch || fetch)(
          `/api/agents/runs/${encodeURIComponent(runId)}/events?after_sequence=${last}`,
          { headers: { Accept: 'application/json' } },
        );
        if (!response.ok) throw new Error(`Agent event reconciliation HTTP ${response.status}`);
        const payload = await response.json();
        (payload.items || [])
          .slice()
          .sort((a, b) => Number(a.sequence || 0) - Number(b.sequence || 0))
          .forEach(event => this.store.dispatch(event));
        const run = this.store.getState().runs[runId];
        if (isTerminalRun(run)) {
          this.store.set({ connection_state: 'offline' });
          this.reconcileTimer = null;
          return;
        }
      } catch {
        // The durable SSE stream remains authoritative. Polling is a WebKit
        // recovery path and will retry without interrupting the live run.
      }
      if (!this.stopped && generation === this.generation && runId === this.runId) {
        this.scheduleReconcile(generation);
      }
    }

    async open(generation = this.generation) {
      const runId = this.runId;
      if (!runId || this.stopped || generation !== this.generation) return null;
      const controller = new AbortController();
      this.controller = controller;
      const last = Number(this.store.getState().last_sequence_by_run[runId] || 0);
      this.store.set({ connection_state: this.retryCount ? 'reconnecting' : 'connecting' });
      try {
        const response = await (window.AgentRuntimeFetch || fetch)(
          `/api/agents/runs/${encodeURIComponent(runId)}/stream?after_sequence=${last}`,
          { signal: controller.signal, headers: { Accept: 'text/event-stream, application/x-ndjson' } },
        );
        if (generation !== this.generation || runId !== this.runId) return null;
        if (!response.ok || !response.body) throw new Error(`Agent stream HTTP ${response.status}`);
        this.store.set({ connection_state: 'streaming' });
        const terminal = await this.consume(response, runId, generation);
        this.retryCount = 0;
        if (
          !terminal
          && !this.stopped
          && generation === this.generation
          && runId === this.runId
        ) {
          const run = this.store.getState().runs[runId];
          if (!isTerminalRun(run)) {
            this.store.set({ connection_state: 'reconnecting' });
            this.retryCount += 1;
            await new Promise(resolve => setTimeout(resolve, Math.min(2000, 250 * this.retryCount)));
            return this.open(generation);
          }
        }
        return terminal;
      } catch (error) {
        if (
          this.stopped
          || error.name === 'AbortError'
          || controller.signal.aborted
          || generation !== this.generation
          || runId !== this.runId
        ) return null;
        this.store.set({ connection_state: 'reconnecting' });
        this.retryCount += 1;
        const delay = Math.min(15000, 500 * (2 ** Math.min(this.retryCount, 5)));
        await new Promise(resolve => setTimeout(resolve, delay));
        return this.open(generation);
      }
    }

    async consume(response, runId, generation) {
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      const sse = String(response.headers.get('content-type') || '').includes('text/event-stream');
      let buffer = '';
      let dataLines = [];
      let terminal = null;
      const accept = payload => {
        if (generation !== this.generation || runId !== this.runId) return;
        if (payload.event?.run_id && payload.event.run_id !== runId) return;
        if (payload.type === 'activity' && payload.event) this.store.dispatch(payload.event);
        if (payload.type === 'result') {
          const result = payload.result || {};
          const hasContinuation = result.recovery_pending === true
            && ['partially_completed', 'max_steps_reached'].includes(String(result.status || ''));
          if (hasContinuation) {
            const current = this.store.getState();
            this.store.set({
              active_run_id: runId,
              connection_state: 'streaming',
              runs: {
                ...(current.runs || {}),
                [runId]: {
                  ...(current.runs?.[runId] || {}),
                  status: 'recovery_pending',
                  checkpoint_status: result.status,
                  recovery_pending: true,
                },
              },
            });
          } else terminal = result;
        }
        if (payload.type === 'error' || payload.type === 'cancelled') terminal = payload;
        if (['waiting_user_input', 'waiting_decision', 'waiting_approval', 'suspended'].includes(payload.type)) {
          this.store.set({ active_run_id: payload.run_id });
          terminal = payload;
        }
      };
      while (
        !terminal
        && !this.stopped
        && generation === this.generation
        && runId === this.runId
      ) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        const lines = buffer.split('\n');
        buffer = done ? '' : lines.pop();
        for (const line of lines) {
          if (sse) {
            if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart());
            if (!line.trim() && dataLines.length) {
              accept(JSON.parse(dataLines.join('\n')));
              dataLines = [];
            }
          } else if (line.trim()) accept(JSON.parse(line));
        }
        if (done) break;
      }
      if (dataLines.length) accept(JSON.parse(dataLines.join('\n')));
      if (terminal && generation === this.generation && runId === this.runId) {
        this.store.set({ connection_state: 'offline' });
      }
      return terminal;
    }
  }

  window.AgentStreamController = AgentStreamController;
  window.AgentStreamIsTerminalRun = isTerminalRun;
}());
