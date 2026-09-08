(function () {
  'use strict';
  const FOREGROUND_RUN_CANDIDATE_LIMIT = 12;
  function runtimeUrl(path) {
    // Agent mutations must remain same-origin so the server-issued session
    // token and Origin policy apply.  Switching between 127.0.0.1 and
    // localhost looks equivalent to a person but is cross-origin to a browser
    // and turns every control request into a rejected CORS preflight.
    return new URL(path, window.location.origin).toString();
  }

  function runtimeOptions(options = {}) {
    const headers = new Headers(options.headers || {});
    const token = document.querySelector('meta[name="stock-ai-runtime-session"]')?.content || '';
    if (token) headers.set('X-Stock-AI-Session', token);
    return { ...options, headers };
  }

  async function runtimeFetch(path, options = {}) {
    return fetch(runtimeUrl(path), runtimeOptions(options));
  }

  async function runtimeApi(path, options = {}) {
    // Keep the controller independently testable and compatible with the
    // server-rendered/mock environment, where the shared API helper is the
    // only available transport.
    if (typeof document === 'undefined' && typeof api === 'function') return api(path, options);
    const response = await runtimeFetch(path, options);
    if (!response.ok) {
      let detail = '';
      try { detail = (await response.json()).detail || ''; } catch { /* use status below */ }
      throw new Error(detail || `Agent control HTTP ${response.status}`);
    }
    return response.status === 204 ? null : response.json();
  }

  function objectiveKey(value) {
    // Recovery belongs in the foreground only while it is still the newest
    // attempt for that exact user request.  The Host may add a routing hint,
    // but that must not make two otherwise identical objectives look unlike.
    return String(value || '')
      .replace(/\[MODEL_TASK_KIND:[^\]]+\]/gi, '')
      .replace(/\s+/g, ' ')
      .trim()
      .toLocaleLowerCase();
  }

  function timestampOf(value) {
    const timestamp = Date.parse(String(value || ''));
    return Number.isFinite(timestamp) ? timestamp : null;
  }

  function isStaleRecovery(run, candidate, latestCompleted) {
    // A recoverable checkpoint remains available in Session history, but it is
    // not live work after a newer Run has already completed. Treating every
    // historical checkpoint (including waiting decisions) as foreground work
    // makes a desktop restart reopen an old question and hide the latest answer.
    if (!run?.status) return false;
    const recoveryTime = timestampOf(candidate?.updated_at || run?.updated_at);
    const completedTime = timestampOf(
      latestCompleted?.candidate?.updated_at || latestCompleted?.run?.updated_at,
    );
    return recoveryTime !== null && completedTime !== null && recoveryTime < completedTime;
  }

  window.AgentRuntimeApi = runtimeApi;
  window.AgentRuntimeFetch = runtimeFetch;

  class AgentSessionController {
    constructor(store) { this.store = store; }
    async list() {
      const payload = await runtimeApi('/api/agents/sessions?limit=100');
      const sessions = {};
      (payload.items || []).forEach(item => { sessions[item.session_id] = item; });
      this.store.set({ sessions });
      return payload.items || [];
    }
    async ensure() {
      const id = this.store.getState().active_session_id;
      if (id) {
        try {
          return (await this.select(id)).session_id;
        } catch { /* create below */ }
      }
      return (await this.create()).session_id;
    }
    async restoreForeground() {
      // A native desktop restart keeps the last UI-selected Session in
      // localStorage.  That may be an empty draft while the durable Host has
      // already resumed a recoverable Run in another Session.  Ask the Host
      // which Session owns live/recoverable work before falling back to that
      // stale client preference, so the user can actually watch the recovery.
      const sessions = await this.list();
      const recoverable = new Set([
        'queued', 'planning', 'running', 'recovery_pending', 'repairing', 'suspended', 'interrupted',
        'waiting_user_input', 'waiting_decision', 'waiting_approval',
        'partially_completed', 'max_steps_reached',
      ]);
      // Session results are already ordered by their durable update time. A
      // large historical workspace previously fetched up to 100 Run records
      // one by one here, leaving the native Dock blank while the server was
      // healthy. Inspect only the recent foreground candidates concurrently.
      const candidates = sessions.filter(item => item?.active_run_id)
        .slice(0, FOREGROUND_RUN_CANDIDATE_LIMIT);
      const resolved = (await Promise.all(candidates.map(async candidate => {
        try {
          const run = await runtimeApi(`/api/agents/runs/${encodeURIComponent(candidate.active_run_id)}`);
          return { candidate, run };
        } catch {
          // A pruned historical Run must not prevent opening a new Session.
          return null;
        }
      }))).filter(Boolean);
      const selectResolvedRun = async item => {
        const session = await this.select(item.candidate.session_id);
        const runId = String(item.run?.run_id || item.candidate.active_run_id || '');
        if (runId) {
          // `select()` intentionally clears the transient Run projection.
          // Keep this small durable summary until the Dock hydrates the full
          // snapshot, otherwise a completed foreground Run has no symbols at
          // desktop startup and cannot restore the matching workspace.
          this.store.set({
            runs: { ...this.store.getState().runs, [runId]: item.run },
            // Keep the resolved Run foreground until the Dock applies its
            // canonical snapshot. The reducer then clears terminal Runs in
            // the normal way, after their symbol and final evidence exist.
            active_run_id: runId,
          });
        }
        return session.session_id;
      };
      const latestCompleted = resolved.find(item => (
        String(item.run?.status || '') === 'completed'
      ));
      for (let index = 0; index < resolved.length; index += 1) {
        const current = resolved[index];
        if (!recoverable.has(String(current.run?.status || ''))) continue;
        if (isStaleRecovery(current.run, current.candidate, latestCompleted)) continue;
        const key = objectiveKey(current.run?.objective);
        const completedReplacement = key && resolved.slice(0, index).find(item => (
          String(item.run?.status || '') === 'completed'
          && objectiveKey(item.run?.objective) === key
        ));
        if (completedReplacement) {
          // Keep the failed Run in its Session for audit, but do not revive it
          // over a newer successful execution of the identical objective.
          return selectResolvedRun(completedReplacement);
        }
        return selectResolvedRun(current);
      }
      // A completed Run is still the most useful foreground state after a
      // native-app restart: it is the user's latest evidence, not an empty
      // draft.  The sessions API is ordered by last update, so the first
      // completed Run preserves the Host's recency order without inventing a
      // client-side timestamp.  Prefer active/recoverable work above; this is
      // only the fallback when there is nothing left to resume.
      if (latestCompleted) {
        return selectResolvedRun(latestCompleted);
      }
      return this.ensure();
    }
    async create(title = 'Stock AI Agent 對話') {
      const session = await runtimeApi('/api/agents/sessions', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title }),
      });
      this.store.set({
        active_session_id: session.session_id,
        active_run_id: null,
        sessions: { ...this.store.getState().sessions, [session.session_id]: session },
        messages: {},
        ordered_message_ids: [],
        runs: {},
        plans: {},
        plan_revisions: {},
        steps: {},
        tool_calls: {},
        approvals: {},
        artifacts: {},
        artifact_versions: {},
        artifact_selections: {},
        forests: {},
        branches: {},
        interactions: {},
        automations: {},
        evidence: {},
        active_selection: null,
        skills: {},
        events: {},
        ordered_event_ids: [],
        environment_snapshot: null,
        connection_state: 'idle',
      });
      return session;
    }
    async select(sessionId) {
      const session = await runtimeApi(`/api/agents/sessions/${encodeURIComponent(sessionId)}`);
      const messages = {};
      const ordered = [];
      (session.messages || []).forEach(item => { messages[item.message_id] = item; ordered.push(item.message_id); });
      this.store.set({
        active_session_id: sessionId,
        active_run_id: session.active_run_id || null,
        sessions: { ...this.store.getState().sessions, [sessionId]: session },
        messages,
        ordered_message_ids: ordered,
        runs: {},
        plans: {},
        plan_revisions: {},
        steps: {},
        tool_calls: {},
        approvals: {},
        artifacts: {},
        artifact_versions: {},
        artifact_selections: {},
        forests: {},
        branches: {},
        interactions: {},
        automations: {},
        evidence: {},
        active_selection: null,
        skills: {},
        events: {},
        ordered_event_ids: [],
        environment_snapshot: null,
        connection_state: 'idle',
      });
      return session;
    }
    async sendMessage(sessionId, content, context = null) {
      const message = await runtimeApi(`/api/agents/sessions/${encodeURIComponent(sessionId)}/messages`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content, artifact_context_selection: context }),
      });
      const items = Array.isArray(message?.messages) ? message.messages : [message?.message || message];
      const messages = { ...this.store.getState().messages };
      const ordered = [...this.store.getState().ordered_message_ids];
      items.filter(item => item?.message_id).forEach(item => {
        messages[item.message_id] = item;
        if (!ordered.includes(item.message_id)) ordered.push(item.message_id);
      });
      const interactions = { ...this.store.getState().interactions };
      const interaction = message?.interaction;
      if (interaction?.interaction_id) {
        interactions[interaction.interaction_id] = {
          ...interaction,
          kind: interaction.kind || 'proposal',
          status: interaction.status || 'waiting_decision',
          session_id: interaction.session_id || sessionId,
        };
      }
      this.store.set({ messages, ordered_message_ids: ordered, interactions, dock_open: true });
      return message;
    }
    async archive(sessionId) {
      const session = await runtimeApi(`/api/agents/sessions/${encodeURIComponent(sessionId)}/archive`, { method: 'POST' });
      this.store.set({ sessions: { ...this.store.getState().sessions, [sessionId]: session } });
      return session;
    }
  }
  window.AgentSessionController = AgentSessionController;
}());
