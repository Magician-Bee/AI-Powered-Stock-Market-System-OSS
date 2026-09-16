(function () {
  'use strict';

  const STORAGE_KEY = 'stockAiAgentDockV2';
  const persisted = (() => {
    try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}'); } catch { return {}; }
  })();
  const persistedDockWidth = Number(persisted.dock_width || 320);
  const compactDockWidth = Math.min(persistedDockWidth, 320);
  // v5 allowed the navigation button to persist a full-workspace Agent
  // overlay.  The only supported desktop presentation is now the resizable
  // right dock, so old localStorage values must not resurrect that overlay.
  if (persisted.dock_maximized === true) {
    delete persisted.dock_maximized;
  }
  const initial = {
    active_session_id: persisted.active_session_id || null,
    active_run_id: persisted.active_run_id || null,
    sessions: {}, messages: {}, runs: {}, plans: {}, plan_revisions: {}, steps: {}, tool_calls: {},
    approvals: {}, artifacts: {}, artifact_versions: {}, artifact_selections: {}, skills: {}, events: {},
    forests: {}, branches: {}, interactions: {}, automations: {}, evidence: {},
    observability: null,
    ordered_message_ids: [], ordered_event_ids: [], last_sequence_by_run: {},
    active_selection: null,
    // Artifact revisions are a user-visible, local draft.  Keep it in Dock
    // state so a normal render (selection, stream update, or resize) cannot
    // silently discard the form the user is editing.
    artifact_revision_draft: null,
    collapsed_task_nodes: {},
    technical_view: persisted.technical_view === true,
    rebuilt_goal_run_ids: persisted.rebuilt_goal_run_ids || {},
    environment_snapshot: null,
    dock_open: persisted.dock_open !== false,
    dock_width: Math.min(640, Math.max(300, compactDockWidth)),
    dock_maximized: false,
    active_tab: persisted.active_tab || 'chat',
    // This is the Agent event-stream state, not the model-provider health.
    // A newly-created Session has no stream yet and must not label a healthy
    // configured provider as offline.
    connection_state: 'idle',
  };
  let current = initial;
  const listeners = new Set();
  let scheduled = false;

  function notify() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      listeners.forEach(listener => listener(current));
    });
  }
  function persist() {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      active_session_id: current.active_session_id,
      active_run_id: current.active_run_id,
      dock_open: current.dock_open,
      dock_width: current.dock_width,
      active_tab: current.active_tab,
      technical_view: current.technical_view,
      rebuilt_goal_run_ids: current.rebuilt_goal_run_ids,
      layout_version: 6,
    }));
  }
  function set(patch) {
    current = { ...current, ...patch };
    persist();
    notify();
    return current;
  }
  function dispatch(event) {
    current = window.AgentEventReducer.reduce(current, event);
    persist();
    notify();
    return current;
  }
  function snapshotItems(value) {
    if (Array.isArray(value)) return value;
    if (value && typeof value === 'object') return Object.values(value);
    return [];
  }
  function artifactVersionItems(value) {
    const items = [];
    if (Array.isArray(value)) {
      value.forEach(item => items.push(...artifactVersionItems(item)));
      return items;
    }
    if (!value || typeof value !== 'object') return items;
    if (value.artifact_id && value.version != null) return [value];
    Object.entries(value).forEach(([artifactId, history]) => {
      const versions = Array.isArray(history) ? history : [history];
      versions.forEach(version => {
        if (!version || typeof version !== 'object' || version.version == null) return;
        items.push({ artifact_id: version.artifact_id || artifactId, ...version });
      });
    });
    return items;
  }
  function applySnapshot(snapshot) {
    if (!snapshot?.run) return current;
    const snapshotRun = snapshot.run;
    // A snapshot can arrive after replaying model.session.configured. Keep
    // that Run's actual SDK receipt even while its final result is pending.
    const resolvedModel = snapshotRun.result?.provider_model_metadata
      || snapshot.final_result?.provider_model_metadata
      || snapshotRun.provider_model_metadata
      || current.runs[snapshotRun.run_id]?.provider_model_metadata;
    const run = resolvedModel ? {
      ...snapshotRun,
      provider_model_metadata: { ...resolvedModel },
      model: resolvedModel.model || null,
      reasoning_effort: resolvedModel.reasoning_effort || null,
    } : snapshotRun;
    const checkpointStatus = String(run.status || '');
    const recoveryCheckpoint = ['partially_completed', 'max_steps_reached'].includes(checkpointStatus);
    const snapshotRecoveryPending = recoveryCheckpoint && Boolean(
      run.recovery_pending
      || run.result?.recovery_pending
      || snapshot.result?.recovery_pending,
    );
    const projectedRun = snapshotRecoveryPending
      ? {
        ...run,
        status: 'recovery_pending',
        checkpoint_status: checkpointStatus,
        recovery_pending: true,
        terminal: false,
      }
      : run;
    const snapshotInteractions = snapshotItems(snapshot.interactions);
    const hasRecoveryEscalation = snapshotInteractions.some(item => (
      item
      && !['answered', 'resolved', 'cancelled', 'expired'].includes(String(item.status || ''))
      && item.interaction_purpose === 'recovery_escalation'
    ));
    const patch = {
      runs: { ...current.runs, [run.run_id]: projectedRun },
      active_run_id: projectedRun.terminal ? null : run.run_id,
      active_session_id: run.session_id || current.active_session_id,
      // A P40 L8 decision belongs beside its Task Forest and Fishbone, not
      // below an arbitrary amount of historical chat after a desktop restart.
      dock_open: hasRecoveryEscalation ? true : current.dock_open,
      active_tab: hasRecoveryEscalation ? 'tasks' : current.active_tab,
      environment_snapshot: snapshot.environment_snapshot || current.environment_snapshot,
      last_sequence_by_run: {
        ...current.last_sequence_by_run,
        [run.run_id]: Number(snapshot.last_sequence || 0),
      },
    };
    patch.plans = { ...current.plans };
    if (snapshot.current_plan?.plan_id) {
      patch.plans[window.AgentEventReducer.scopedKey(run.run_id, snapshot.current_plan.plan_id)] = {
        ...snapshot.current_plan,
        run_id: run.run_id,
        session_id: run.session_id,
      };
    }
    patch.plan_revisions = {
      ...current.plan_revisions,
      [run.run_id]: snapshotItems(snapshot.plan_revisions).map(item => ({
        ...item,
        run_id: run.run_id,
        session_id: run.session_id,
      })),
    };
    patch.steps = { ...current.steps };
    // A reconnect can start from persisted Dock state that belongs to an old
    // PlanGraph revision.  Replace this Run's projected steps with the
    // durable snapshot so transient numeric turn IDs (for example "8") never
    // survive as fake Task Forest / DAG nodes.
    const snapshotSteps = snapshotItems(snapshot.steps);
    const planNodes = new Map(snapshotItems(snapshot.current_plan?.nodes).map(node => [node.node_id, node]));
    Object.entries(patch.steps).forEach(([key, step]) => {
      if (step?.run_id === run.run_id) delete patch.steps[key];
    });
    snapshotSteps.forEach(step => {
      const id = step.node_id || step.step_id;
      const planNode = planNodes.get(id);
      patch.steps[window.AgentEventReducer.scopedKey(run.run_id, id)] = {
        ...step,
        ...(planNode ? {
          status: planNode.status || step.status,
          metadata: { ...(step.metadata || {}), ...(planNode.metadata || {}) },
        } : {}),
        run_id: run.run_id, session_id: run.session_id,
      };
    });
    patch.tool_calls = { ...current.tool_calls };
    snapshotItems(snapshot.tool_calls).forEach(call => {
      patch.tool_calls[window.AgentEventReducer.scopedKey(run.run_id, call.tool_call_id)] = {
        ...call, run_id: run.run_id, session_id: run.session_id,
      };
    });
    patch.approvals = { ...current.approvals };
    snapshotItems(snapshot.approvals).forEach(item => {
      patch.approvals[window.AgentEventReducer.scopedKey(run.run_id, item.approval_id)] = {
        ...item, run_id: run.run_id, session_id: run.session_id,
      };
    });
    patch.artifacts = { ...current.artifacts };
    snapshotItems(snapshot.artifacts).forEach(item => {
      patch.artifacts[window.AgentEventReducer.scopedKey(run.run_id, item.artifact_id)] = {
        ...item, run_id: run.run_id, session_id: run.session_id,
        step_id: item.step_id || item.metadata?.step_id,
      };
    });
    patch.forests = { ...current.forests };
    const forests = [...snapshotItems(snapshot.forests)];
    if (snapshot.forest && !forests.some(item => item.forest_id === snapshot.forest.forest_id)) {
      forests.push(snapshot.forest);
    }
    forests.forEach(item => {
      const id = item.forest_id || item.task_forest_id;
      if (id) patch.forests[id] = { ...item, run_id: run.run_id, session_id: run.session_id };
    });
    patch.branches = { ...current.branches };
    const branches = [...snapshotItems(snapshot.branches)];
    forests.forEach(forest => snapshotItems(forest.branches).forEach(branch => {
      if (!branches.some(item => item.branch_id === branch.branch_id)) branches.push(branch);
    }));
    branches.forEach(item => {
      const id = item.branch_id;
      if (id) patch.branches[id] = { ...item, run_id: run.run_id, session_id: run.session_id };
      snapshotItems(item.steps).forEach((step, index) => {
        const stepId = step.node_id || step.step_id;
        if (!stepId) return;
        patch.steps[window.AgentEventReducer.scopedKey(run.run_id, stepId)] = {
          ...step,
          node_id: stepId,
          parent_node_id: step.parent_node_id || step.parent_id || id,
          order_index: Number(step.order_index ?? step.position ?? index),
          run_id: run.run_id,
          session_id: run.session_id,
        };
      });
    });
    patch.interactions = { ...current.interactions };
    snapshotInteractions.forEach(item => {
      if (item.interaction_id) patch.interactions[item.interaction_id] = {
        ...item,
        // Session snapshots include historical checkpoints from other Runs.
        // Preserve their owner so the Decision Card cannot re-label an old
        // question as an action for the current completed Run.
        run_id: item.run_id || run.run_id,
        session_id: item.session_id || run.session_id,
      };
    });
    patch.artifact_versions = { ...current.artifact_versions };
    artifactVersionItems(snapshot.artifact_versions).forEach(item => {
      if (item.artifact_id && item.version != null) {
        patch.artifact_versions[`${item.artifact_id}:v${item.version}`] = {
          ...item, run_id: item.run_id || run.run_id, session_id: item.session_id || run.session_id,
        };
      }
    });
    patch.artifact_selections = { ...current.artifact_selections };
    snapshotItems(snapshot.artifact_selections).forEach(item => {
      if (item.selection_id) patch.artifact_selections[item.selection_id] = item;
    });
    // Automation records are a durable session projection.  Unlike append-
    // only event history, a fresh snapshot is authoritative: retain other
    // Sessions for their own tabs, but replace this Session's records so an
    // archived Automation cannot be resurrected from an old browser cache.
    patch.automations = { ...current.automations };
    Object.entries(patch.automations).forEach(([id, automation]) => {
      if (automation?.session_id === run.session_id) delete patch.automations[id];
    });
    snapshotItems(snapshot.automations).forEach(item => {
      // Do not turn an account-wide or stale Automation into this Run's
      // Automation just because it arrived in a reconnect snapshot.  The API
      // is Session-scoped; retaining that ownership check keeps the UI
      // fail-closed if a future endpoint accidentally returns a broad list.
      if (item.automation_id && item.session_id === run.session_id) {
        patch.automations[item.automation_id] = { ...item };
      }
    });
    patch.evidence = { ...current.evidence };
    snapshotItems(snapshot.evidence).forEach(item => {
      if (item.evidence_id) patch.evidence[item.evidence_id] = { ...item, session_id: run.session_id };
    });
    return set(patch);
  }

  window.AgentDockStore = {
    getState: () => current,
    set,
    dispatch,
    applySnapshot,
    artifactVersionItems,
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); },
  };
}());
