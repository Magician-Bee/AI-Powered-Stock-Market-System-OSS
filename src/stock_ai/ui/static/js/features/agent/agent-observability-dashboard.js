(function () {
  'use strict';

  const fields = [
    ['active_sessions', 'Active Sessions', value => String(value || 0)],
    ['active_branches', 'Active Branches', value => String(value || 0)],
    // `tool_latency_ms` / `model_latency_ms` inside a window are durable
    // totals.  The operator cards must show the measured per-call average,
    // otherwise a longer history is incorrectly rendered as one enormous
    // single request latency.
    ['average_tool_latency_ms', 'Tool Latency', value => `${Math.round(Number(value || 0))} ms`],
    ['average_model_latency_ms', 'Model Latency', value => `${Math.round(Number(value || 0))} ms`],
    ['error_rate', 'Error Rate', percent],
    ['tool_error_rate', 'Tool Error Rate', percent],
    ['repair_success_rate', 'Repair Success', percent],
    ['repeated_failure_rate', 'Repeated Failure', percent],
    ['token_usage', 'Token Usage', value => Math.round(Number(value || 0)).toLocaleString()],
    ['web_research_cost', 'Web Research Cost', value => Number(value || 0).toFixed(2)],
    ['automation_count', 'Active Automations', value => String(value || 0)],
    ['notification_count', 'Notifications', value => String(value || 0)],
    ['false_automation_notification_rate', 'False Trigger', percent],
    ['task_completion_rate', 'Task Completion', percent],
    ['branch_recovery_rate', 'Branch Recovery', percent],
    ['user_intervention_rate', 'User Intervention', percent],
    ['average_tool_calls', 'Average Tool Calls', value => Number(value || 0).toFixed(1)],
    ['average_token_cost', 'Average Token Cost', value => Number(value || 0).toFixed(4)],
    ['research_source_diversity', 'Research Source Diversity', value => String(value || 0)],
    ['evidence_freshness', 'Evidence Freshness', percent],
    ['user_correction_incorporation_rate', 'Correction Incorporation', percent],
    ['automation_duplicate_rate', 'Automation Duplicate', percent],
    ['session_context_retrieval_precision', 'Context Retrieval Precision', percent],
    ['average_questions_per_checkpoint', 'Questions / Checkpoint', value => Number(value || 0).toFixed(1)],
    ['question_budget_breach_rate', 'Question Budget Breach', percent],
  ];

  const windowLabels = { '24h': '24 hours', '7d': '7 days', '30d': '30 days', all: 'All time' };
  let activeWindow = '';

  function percent(value) { return `${(Number(value || 0) * 100).toFixed(1)}%`; }

  function bytes(value) {
    const amount = Math.max(0, Number(value || 0));
    if (amount >= 1024 ** 3) return `${(amount / (1024 ** 3)).toFixed(2)} GB`;
    if (amount >= 1024 ** 2) return `${(amount / (1024 ** 2)).toFixed(1)} MB`;
    if (amount >= 1024) return `${(amount / 1024).toFixed(1)} KB`;
    return `${Math.round(amount)} B`;
  }

  function metricText(metrics, key, formatter) {
    if (metrics.metric_status?.[key] === 'no_data') return '—';
    return formatter(metrics[key]);
  }

  function render(container, state) {
    if (!container) return;
    const dashboard = state.observability;
    container.replaceChildren();
    if (!dashboard) {
      container.hidden = true;
      return;
    }
    container.hidden = false;
    const availableWindows = dashboard.available_windows || Object.keys(dashboard.windows || {});
    if (!activeWindow || !availableWindows.includes(activeWindow)) {
      activeWindow = dashboard.default_window || availableWindows[0] || 'all';
    }
    const metrics = dashboard.windows?.[activeWindow] || dashboard;
    const title = document.createElement('strong');
    title.textContent = 'Observability';
    const controls = document.createElement('label');
    controls.textContent = ' KPI Window ';
    const selector = document.createElement('select');
    selector.setAttribute('aria-label', 'KPI Window');
    availableWindows.forEach(key => {
      const option = document.createElement('option');
      option.value = key;
      option.textContent = windowLabels[key] || key;
      option.selected = key === activeWindow;
      selector.append(option);
    });
    selector.addEventListener('change', () => {
      activeWindow = selector.value;
      render(container, state);
    });
    controls.append(selector);
    const grid = document.createElement('dl');
    grid.className = 'agent-observability-grid';
    fields.forEach(([key, label, formatter]) => {
      const item = document.createElement('div');
      const term = document.createElement('dt');
      term.textContent = label;
      const definition = document.createElement('dd');
      definition.textContent = metricText(metrics, key, formatter);
      item.append(term, definition);
      grid.append(item);
    });
    const samples = document.createElement('small');
    const counts = metrics.sample_counts || {};
    samples.textContent = `Samples: ${Number(counts.tasks || 0)} tasks · ${Number(counts.execution_attempts || 0)} executions · ${Number(counts.failure_events || 0)} failure events`;
    const storage = dashboard.storage;
    const storageStatus = document.createElement('small');
    if (storage?.healthy) {
      const database = storage.database || {};
      const runtimeEvents = storage.runtime_events || {};
      const maintenance = storage.maintenance || {};
      const state = maintenance.ready ? 'ready for explicit maintenance' : 'maintenance deferred';
      storageStatus.textContent = `Runtime storage: ${bytes(database.total_bytes)} · ${bytes(database.reclaimable_bytes)} reclaimable · ${Number(runtimeEvents.pending || 0)} pending events · ${state}`;
    } else if (storage) {
      storageStatus.textContent = 'Runtime storage: health metadata unavailable; maintenance is blocked.';
    }
    const operational = dashboard.operational_alerts;
    const operationalDetails = document.createElement('details');
    const operationalSummary = document.createElement('summary');
    const currentAlerts = Array.isArray(operational?.current_alerts) ? operational.current_alerts : [];
    operationalSummary.textContent = `Operational alerts · ${currentAlerts.length} active`;
    const operationalList = document.createElement('ul');
    if (!operational) {
      const empty = document.createElement('li');
      empty.textContent = 'Operational alert runtime is unavailable.';
      operationalList.append(empty);
    } else {
      currentAlerts.forEach(alert => {
        const item = document.createElement('li');
        item.textContent = `${alert.severity || 'UNKNOWN'} · ${alert.code || 'alert'} · ${alert.message || ''}`;
        operationalList.append(item);
      });
      const coverage = Object.entries(operational.coverage || {});
      coverage.filter(([, value]) => value?.enabled === false).forEach(([scope, value]) => {
        const item = document.createElement('li');
        item.textContent = `${scope}: not configured (${value.blocker || 'no runtime signal'})`;
        operationalList.append(item);
      });
      const chaos = operational.chaos_recovery || {};
      const chaosItem = document.createElement('li');
      const chaosCount = Number(chaos.scenario_count || 0);
      chaosItem.textContent = `Chaos recovery: ${chaos.status || 'unknown'} · ${chaosCount}/4 scenarios${chaos.reason ? ` (${chaos.reason})` : ''}`;
      operationalList.append(chaosItem);
      const delivery = operational.delivery || {};
      const deliveryItem = document.createElement('li');
      deliveryItem.textContent = `On-call delivery: ${delivery.status || 'unknown'}${delivery.blocker ? ` (${delivery.blocker})` : ''}`;
      operationalList.append(deliveryItem);
      const gate = operational.order_gate?.live_submission || {};
      const gateItem = document.createElement('li');
      gateItem.textContent = `Live order gate: ${gate.blocked ? 'blocked' : 'open'}${(gate.reasons || []).length ? ` (${gate.reasons.join(', ')})` : ''}`;
      operationalList.append(gateItem);
      if (!operationalList.children.length) {
        const empty = document.createElement('li');
        empty.textContent = 'No active operational alerts.';
        operationalList.append(empty);
      }
    }
    operationalDetails.append(operationalSummary, operationalList);
    const slo = dashboard.slo;
    const sloEvidence = document.createElement('details');
    const sloSummary = document.createElement('summary');
    sloSummary.textContent = 'SLO evidence';
    const sloList = document.createElement('ul');
    const reports = Array.isArray(slo?.reports) ? slo.reports : [];
    const latestReports = new Map();
    reports.forEach(report => latestReports.set(String(report.service || ''), report));
    if (!latestReports.size) {
      const empty = document.createElement('li');
      empty.textContent = 'No SLO observations recorded';
      sloList.append(empty);
    } else {
      latestReports.forEach(report => {
        const item = document.createElement('li');
        const samples = Number(report.samples || 0);
        const latency = report.p95_latency_ms == null ? '—' : `${Math.round(Number(report.p95_latency_ms))} ms`;
        item.textContent = `${report.service}: ${report.status} · ${samples} samples · p95 ${latency}`;
        sloList.append(item);
      });
    }
    sloEvidence.append(sloSummary, sloList);
    const failures = document.createElement('details');
    const failureSummary = document.createElement('summary');
    failureSummary.textContent = 'Failure classification';
    const failureList = document.createElement('ul');
    const classified = Object.entries(metrics.failure_classification || {});
    if (!classified.length) {
      const empty = document.createElement('li');
      empty.textContent = 'No classified failure samples';
      failureList.append(empty);
    } else {
      classified.forEach(([category, count]) => {
        const item = document.createElement('li');
        item.textContent = `${category}: ${count}`;
        failureList.append(item);
      });
    }
    failures.append(failureSummary, failureList);
    container.append(title, controls, samples, storageStatus, grid, operationalDetails, sloEvidence, failures);
  }

  window.AgentObservabilityDashboard = { render };
}());
