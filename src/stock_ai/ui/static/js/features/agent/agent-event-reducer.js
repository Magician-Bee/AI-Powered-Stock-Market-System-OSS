(function () {
  'use strict';

  function payloadOf(event) {
    return event?.payload && typeof event.payload === 'object' ? event.payload : event || {};
  }

  function scopedKey(runId, id) {
    return `${String(runId || 'no-run')}:${String(id || 'unknown')}`;
  }

  // Execution turns and PlanGraph nodes have different identifiers.  The
  // durable node ID is authoritative whenever an event supplies it; using a
  // numeric turn step first creates a ghost Task Forest node such as "5".
  function nodeIdOf(event, payload) {
    return String(event?.node_id || payload?.node_id || event?.step_id || payload?.step_id || '');
  }

  function collection(state, name) {
    return { ...(state[name] || {}) };
  }

  const TERMINAL_RUN_STATUSES = new Set([
    'completed', 'partially_completed', 'max_steps_reached', 'failed', 'cancelled', 'interrupted',
  ]);
  const RECOVERY_CONTINUATION_TYPES = new Set([
    'recovery.continuation_scheduled', 'run.continuation_requested', 'run.resumed',
  ]);

  function recoveryPending(eventType, payload, previousRun = {}) {
    if (payload.recovery_pending === false) return false;
    if (payload.recovery_pending === true) return true;
    if (RECOVERY_CONTINUATION_TYPES.has(eventType)) return true;
    return Boolean(previousRun.recovery_pending)
      && !['run.completed', 'run.failed', 'run.cancelled'].includes(eventType);
  }

  function isTerminalRun(run) {
    return TERMINAL_RUN_STATUSES.has(String(run?.status || '')) && run?.recovery_pending !== true;
  }

  function verifiedFinalCompletion(payload) {
    const validation = payload.completion_validation;
    const checks = validation?.checks;
    if (payload.status !== 'completed' || payload.recovery_pending !== false
      || validation?.validator !== 'completion_evaluator' || validation.passed !== true
      || !Array.isArray(checks) || !checks.length || checks.some(check => check?.passed !== true)) return false;
    if (['pending_recovery_node_ids', 'goal_completion_gaps'].some(key => (
      payload[key] != null && (!Array.isArray(payload[key]) || payload[key].length)
    ))) return false;
    const failures = checks.find(check => check.name === 'failed_plan_nodes_are_nonblocking');
    const unfinished = checks.find(check => check.name === 'plan_nodes_finished');
    return Array.isArray(failures?.unresolved_failed_nodes) && !failures.unresolved_failed_nodes.length
      && Array.isArray(unfinished?.unfinished) && !unfinished.unfinished.length;
  }

  function upsertPlan(state, event, payload) {
    const plan = payload.plan || payload.current_plan;
    if (!plan?.plan_id || !event.run_id) return;
    const planKey = scopedKey(event.run_id, plan.plan_id);
    state.plans[planKey] = {
      ...(state.plans[planKey] || {}), ...plan,
      run_id: event.run_id, session_id: event.session_id,
    };
    const run = state.runs[event.run_id] || {};
    state.runs[event.run_id] = {
      ...run, plan_id: plan.plan_id,
      plan_revision: plan.revision_number || event.plan_revision || 1,
    };
    const currentNodeIds = new Set(
      (plan.nodes || []).map(node => String(node.node_id || '')).filter(Boolean),
    );
    Object.entries(state.steps).forEach(([stepKey, step]) => {
      if (step.run_id === event.run_id && step.plan_id === plan.plan_id
        && !currentNodeIds.has(String(step.node_id || step.step_id || ''))) delete state.steps[stepKey];
    });
    (plan.nodes || []).forEach((node, index) => {
      const stepKey = scopedKey(event.run_id, node.node_id);
      state.steps[stepKey] = {
        ...(state.steps[stepKey] || {}), ...node,
        type: node.type || node.node_type,
        parent_node_id: node.parent_node_id || node.parent_id || null,
        dependency_ids: node.dependency_ids || node.dependencies || [],
        order_index: Number(node.order_index ?? index),
        run_id: event.run_id, session_id: event.session_id, plan_id: plan.plan_id,
      };
    });
  }

  function reduce(state, event) {
    if (!event?.type) return state;
    const payload = payloadOf(event);
    const type = String(event.type);
    const actualRunId = event.run_id || payload.run_id || null;
    const sessionId = event.session_id || payload.session_id
      || (actualRunId ? state.runs?.[actualRunId]?.session_id : null) || state.active_session_id || null;
    const runId = String(actualRunId || `session:${sessionId || 'global'}`);
    const eventId = String(event.event_id || `${runId}:${event.sequence || 0}:${type}`);
    const sequence = Number(event.sequence || 0);
    if (state.events?.[eventId]) return state;
    if (sequence && sequence <= Number(state.last_sequence_by_run?.[runId] || 0)) return state;

    const next = {
      ...state,
      events: { ...(state.events || {}), [eventId]: { ...event, session_id: sessionId } },
      ordered_event_ids: [...(state.ordered_event_ids || []), eventId],
      last_sequence_by_run: {
        ...(state.last_sequence_by_run || {}),
        [runId]: Math.max(sequence, Number(state.last_sequence_by_run?.[runId] || 0)),
      },
      runs: collection(state, 'runs'), plans: collection(state, 'plans'),
      steps: collection(state, 'steps'), tool_calls: collection(state, 'tool_calls'),
      approvals: collection(state, 'approvals'), artifacts: collection(state, 'artifacts'),
      artifact_versions: collection(state, 'artifact_versions'),
      artifact_selections: collection(state, 'artifact_selections'),
      messages: collection(state, 'messages'), skills: collection(state, 'skills'),
      sessions: collection(state, 'sessions'), forests: collection(state, 'forests'),
      branches: collection(state, 'branches'), interactions: collection(state, 'interactions'),
      automations: collection(state, 'automations'), evidence: collection(state, 'evidence'),
      ordered_message_ids: [...(state.ordered_message_ids || [])],
    };

    if (actualRunId) {
      next.active_run_id = String(actualRunId);
      const explicitStatus = String(payload.status || event.status || '');
      const previousRun = state.runs?.[actualRunId] || {};
      const pendingRecovery = recoveryPending(type, payload, previousRun);
      const confirmedFinal = type === 'result.final' && verifiedFinalCompletion(payload);
      let runStatus = {
        'run.queued': 'queued', 'run.started': 'running', 'run.resumed': 'running',
        'run.continuation_requested': 'queued', 'run.paused': 'suspended',
        'run.completed': explicitStatus || 'completed', 'run.partially_completed': 'partially_completed',
        'run.reclassified': 'partially_completed',
        'run.max_steps_reached': 'max_steps_reached',
        'run.failed': 'failed', 'run.cancelled': 'cancelled',
        'recovery.continuation_scheduled': 'repairing',
      }[type];
      if (confirmedFinal) runStatus = 'completed';
      if (
        pendingRecovery
        && ['partially_completed', 'max_steps_reached'].includes(String(runStatus || explicitStatus))
      ) runStatus = 'recovery_pending';
      if (type === 'result.final' && pendingRecovery) runStatus = 'recovery_pending';
      const recoveryFinished = confirmedFinal || ['run.completed', 'run.failed', 'run.cancelled'].includes(type)
        && !['partially_completed', 'max_steps_reached'].includes(String(explicitStatus));
      next.runs[actualRunId] = {
        ...previousRun, run_id: String(actualRunId), session_id: sessionId,
        status: runStatus || previousRun.status, last_sequence: sequence,
        checkpoint_status: confirmedFinal ? null : ['partially_completed', 'max_steps_reached'].includes(explicitStatus)
          ? explicitStatus
          : previousRun.checkpoint_status,
        recovery_pending: recoveryFinished ? false : pendingRecovery,
        recovery_summary: confirmedFinal ? null : type === 'recovery.continuation_scheduled'
          ? payload.summary || previousRun.recovery_summary
          : previousRun.recovery_summary,
        provider: payload.provider || previousRun.provider,
        autonomy: payload.autonomy || previousRun.autonomy,
      };
    }

    if (actualRunId && type === 'model.session.configured') {
      next.runs[actualRunId] = {
        ...next.runs[actualRunId],
        model: payload.model || null,
        reasoning_effort: payload.reasoning_effort || null,
        model_resolution_source: payload.resolution_source || null,
        provider_model_metadata: { ...payload },
      };
    }

    if (type === 'session.created' || type === 'session.title.updated') {
      const session = payload.session || payload;
      if (sessionId) next.sessions[sessionId] = { ...(next.sessions[sessionId] || {}), ...session, session_id: sessionId };
    }
    if (type.startsWith('plan.')) upsertPlan(next, event, payload);
    if (type.startsWith('forest.')) {
      const forest = payload.forest || payload;
      const id = String(event.forest_id || forest.forest_id || forest.task_forest_id || '');
      if (id) next.forests[id] = { ...(next.forests[id] || {}), ...forest, forest_id: id, run_id: actualRunId, session_id: sessionId };
    }
    if (type.startsWith('branch.')) {
      const branch = payload.branch || payload;
      const id = String(event.branch_id || branch.branch_id || '');
      const status = {
        'branch.created': 'queued', 'branch.started': 'running', 'branch.paused': 'paused',
        'branch.resumed': 'running', 'branch.completed': 'completed', 'branch.failed': 'failed',
        'branch.repaired': 'running', 'branch.cancelled': 'cancelled',
      }[type] || branch.status;
      if (id) next.branches[id] = {
        ...(next.branches[id] || {}), ...branch, branch_id: id, status,
        run_id: actualRunId, session_id: sessionId,
      };
    }
    if (type.startsWith('step.')) {
      const id = nodeIdOf(event, payload);
      if (id) {
        const stepKey = scopedKey(actualRunId, id);
        const status = {
          'step.proposed': 'proposed', 'step.ready': 'ready', 'step.started': 'running',
          'step.waiting_approval': 'waiting_approval', 'step.completed': 'completed',
          'step.failed': 'failed', 'step.blocked': 'blocked', 'step.skipped': 'skipped',
          'step.cancelled': 'cancelled',
        }[type] || 'pending';
        next.steps[stepKey] = {
          ...(next.steps[stepKey] || {}), ...payload, step_id: id, node_id: id, status,
          branch_id: event.branch_id || payload.branch_id,
          run_id: actualRunId, session_id: sessionId,
        };
      }
    }
    if (type === 'recovery.started') {
      const id = nodeIdOf(event, payload);
      if (id) {
        const stepKey = scopedKey(actualRunId, id);
        next.steps[stepKey] = {
          ...(next.steps[stepKey] || {}), ...payload, step_id: id, node_id: id,
          status: 'failed', run_id: actualRunId, session_id: sessionId,
          metadata: {
            ...(next.steps[stepKey]?.metadata || {}),
            recovery: payload.recovery || null,
            required_recovery: payload.required_recovery || null,
          },
        };
      }
    }
    if (type === 'recovery.linked') {
      const id = nodeIdOf(event, payload);
      if (id) {
        const stepKey = scopedKey(actualRunId, id);
        next.steps[stepKey] = {
          ...(next.steps[stepKey] || {}), ...payload, step_id: id, node_id: id,
          run_id: actualRunId, session_id: sessionId,
          metadata: {
            ...(next.steps[stepKey]?.metadata || {}),
            recovery_for: payload.recovery_for || [],
          },
        };
      }
      (payload.recovery_for || []).forEach(link => {
        const failedNodeId = String(link?.failed_node_id || '');
        if (!failedNodeId) return;
        const signature = JSON.stringify(link);
        const failedKey = scopedKey(actualRunId, failedNodeId);
        if (!next.steps[failedKey]) return;
        next.steps[failedKey] = {
          ...next.steps[failedKey],
          metadata: {
            ...(next.steps[failedKey].metadata || {}),
            recovered_by: (() => {
              const existing = next.steps[failedKey].metadata?.recovered_by || [];
              return existing.some(item => JSON.stringify(item) === signature)
                ? existing
                : [...existing, link];
            })(),
          },
        };
        Object.entries(next.branches).forEach(([branchId, branch]) => {
          if (branch.run_id !== actualRunId) return;
          const ownsFailedNode = String(branch.source_plan_node_id || '') === failedNodeId
            || String(branch.source_node_id || '') === failedNodeId
            || String(next.steps[failedKey].branch_id || '') === branchId;
          if (!ownsFailedNode) return;
          next.branches[branchId] = {
            ...branch,
            status: 'partially_completed',
            metadata: {
              ...(branch.metadata || {}),
              recovered_by: [
                ...(branch.metadata?.recovered_by || []),
                ...(branch.metadata?.recovered_by || []).some(item => JSON.stringify(item) === signature)
                  ? [] : [link],
              ],
            },
          };
        });
      });
    }
    if (type.startsWith('tool.')) {
      const id = String(event.tool_call_id || payload.tool_call_id || payload.call_id || '');
      if (id) {
        const toolKey = scopedKey(actualRunId, id);
        const status = {
          'tool.requested': 'queued', 'tool.queued': 'queued', 'tool.started': 'running',
          'tool.progress': 'running', 'tool.retrying': 'retrying', 'tool.completed': 'completed',
          'tool.failed': 'failed', 'tool.cancelled': 'cancelled',
        }[type] || next.tool_calls[toolKey]?.status || 'queued';
        next.tool_calls[toolKey] = {
          ...(next.tool_calls[toolKey] || {}), ...payload, tool_call_id: id,
          step_id: event.step_id || payload.node_id, branch_id: event.branch_id || payload.branch_id,
          tool_name: payload.tool, status, run_id: actualRunId, session_id: sessionId,
        };
      }
    }
    if (type.startsWith('validation.')) {
      const id = String(event.tool_call_id || payload.call_id || '');
      const toolKey = scopedKey(actualRunId, id);
      if (id && next.tool_calls[toolKey]) next.tool_calls[toolKey] = {
        ...next.tool_calls[toolKey], validation_status: type.slice('validation.'.length),
        validation: payload.validation,
        evidence_ids: payload.evidence_ids || next.tool_calls[toolKey].evidence_ids || [],
      };
    }
    if (type.startsWith('skill.') || type === 'package.loaded' || type === 'mcp.tool.selected') {
      const rawId = String(payload.skill_id || payload.package_id || payload.mcp_server || eventId);
      const stepId = String(event.step_id || payload.step_id || payload.node_id || 'no-step');
      const toolCallId = String(event.tool_call_id || payload.tool_call_id || payload.call_id || 'no-call');
      const id = [runId, stepId, toolCallId, type, rawId].join(':');
      next.skills[id] = {
        ...(next.skills[id] || {}), ...payload, id, audit_key: id, run_id: actualRunId,
        session_id: sessionId, step_id: stepId, tool_call_id: toolCallId,
        kind: type === 'package.loaded' ? 'package' : type === 'mcp.tool.selected' ? 'mcp' : 'skill',
        status: type.split('.').pop(),
      };
    }
    if (type.startsWith('approval.')) {
      const approval = payload.approval || payload;
      const rawId = String(event.approval_id || approval.approval_id || '');
      const id = scopedKey(actualRunId, rawId);
      if (rawId) next.approvals[id] = {
        ...(next.approvals[id] || {}), ...approval, approval_id: rawId,
        run_id: actualRunId, session_id: sessionId,
        step_id: event.step_id || approval.step_id || payload.node_id,
      };
    }
    if (type.startsWith('interaction.') || type.startsWith('proposal.')) {
      const interaction = payload.interaction || payload.proposal || payload;
      const id = String(event.interaction_id || interaction.interaction_id || interaction.proposal_id || '');
      const status = {
        'interaction.proposed': 'proposed', 'interaction.awaiting_user': 'awaiting_user',
        'interaction.requested': 'awaiting_user',
        'interaction.user_answered': 'answered', 'proposal.received': 'proposed',
        'proposal.evaluated': 'awaiting_user', 'proposal.accepted': 'answered',
        'proposal.modified': 'answered', 'proposal.rejected': 'answered',
      }[type] || interaction.status;
      if (id) next.interactions[id] = {
        ...(next.interactions[id] || {}), ...interaction, interaction_id: id,
        title: interaction.title || interaction.question || interaction.prompt,
        question: interaction.question || interaction.prompt,
        kind: type.startsWith('proposal.') ? 'proposal' : interaction.kind,
        status, run_id: actualRunId, session_id: sessionId,
        branch_id: event.branch_id || interaction.branch_id,
      };
      if (
        type === 'interaction.requested'
        && interaction.interaction_purpose === 'recovery_escalation'
      ) {
        // Keep L8 visible: its question is pinned above the tabs, while the
        // selected Task tab exposes the affected Forest and Fishbone.
        next.dock_open = true;
        next.active_tab = 'tasks';
      }
    }
    if (type.startsWith('artifact.')) {
      const artifact = payload.artifact || payload;
      const rawId = String(event.artifact_id || artifact.artifact_id || '');
      const id = scopedKey(actualRunId, rawId);
      if (rawId) {
        next.artifacts[id] = {
          ...(next.artifacts[id] || {}), ...artifact, artifact_id: rawId,
          run_id: actualRunId, session_id: sessionId,
          step_id: event.step_id || artifact.step_id || artifact.metadata?.step_id,
        };
        const version = Number(artifact.version || artifact.artifact_version || 0);
        if (version) next.artifact_versions[`${rawId}:v${version}`] = {
          ...artifact, artifact_id: rawId, version, session_id: sessionId,
        };
      }
      if (type === 'artifact.selected') {
        const selection = payload.selection || payload;
        const selectionId = String(selection.selection_id || event.selection_id || eventId);
        next.active_selection = { ...selection, selection_id: selectionId, artifact_id: rawId || selection.artifact_id };
        next.artifact_selections[selectionId] = next.active_selection;
      }
    }
    if (type === 'research.evidence_added' || type.startsWith('evidence.')) {
      const evidence = payload.evidence || payload;
      const id = String(event.evidence_id || evidence.evidence_id || '');
      if (id) next.evidence[id] = {
        ...(next.evidence[id] || {}), ...evidence, evidence_id: id,
        run_id: actualRunId, session_id: sessionId, branch_id: event.branch_id || evidence.branch_id,
      };
    }
    if (type.startsWith('automation.')) {
      const automation = payload.automation || payload;
      const id = String(event.automation_id || automation.automation_id || '');
      const status = type.slice('automation.'.length);
      if (id) next.automations[id] = {
        ...(next.automations[id] || {}), ...automation, automation_id: id,
        status: ['intent_created', 'compiling', 'validating', 'testing', 'publishing', 'active', 'paused', 'archived', 'failed', 'expired'].includes(status)
          ? status : automation.status,
        run_id: actualRunId, session_id: sessionId, branch_id: event.branch_id || automation.branch_id,
      };
    }
    if (type === 'message.created' || type.startsWith('assistant.message.')) {
      const message = payload.message || payload;
      const id = String(type.startsWith('assistant.')
        ? message.message_id || `assistant:${actualRunId}`
        : message.message_id || `${eventId}:message`);
      const prior = next.messages[id] || {};
      next.messages[id] = {
        ...prior, ...message, message_id: id, run_id: actualRunId, session_id: sessionId,
        role: type.startsWith('assistant.') ? 'assistant' : message.role || 'user',
        content: payload.content || message.content || payload.summary || event.summary || '',
        status: type.endsWith('.started') ? 'streaming'
          : type.endsWith('.completed') ? message.status || payload.status || 'completed' : message.status,
        created_at: message.created_at || event.timestamp,
      };
      if (!next.ordered_message_ids.includes(id)) next.ordered_message_ids.push(id);
    }
    if (type === 'context.snapshot.created') next.environment_snapshot = payload.snapshot || null;
    // Never expose a green Run while a real branch still has a failure.
    // This mirrors the Host completion gate and protects reconnects from a
    // stale/misclassified run.completed event.
    if (type === 'run.completed' && actualRunId) {
      const failedSteps = Object.values(next.steps).some(step => (
        step.run_id === actualRunId && ['failed', 'blocked'].includes(String(step.status || ''))
      ));
      const failedTools = Object.values(next.tool_calls).some(call => (
        call.run_id === actualRunId && String(call.status || '') === 'failed'
      ));
      if (failedSteps || failedTools) {
        next.runs[actualRunId] = {
          ...(next.runs[actualRunId] || {}),
          status: 'partially_completed',
          checkpoint_status: 'partially_completed',
          recovery_pending: true,
          recovery_summary: 'Run 收到完成事件但仍有失敗分支，保留失敗現場並等待修復。',
        };
      }
    }
    if (actualRunId && isTerminalRun(next.runs[actualRunId])) next.active_run_id = null;
    return next;
  }

  window.AgentEventReducer = { isTerminalRun, recoveryPending, reduce, scopedKey };
}());
