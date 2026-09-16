(function () {
  'use strict';

  const F = () => window.AgentDockFormatters;

  function activeRunId(state) {
    return window.AgentPlanView.activeRunId(state);
  }

  function stepsForRun(state, runId) {
    const rawSteps = Object.values(state.steps || {})
      .filter(step => step.run_id === runId)
      .sort((left, right) => (
        Number(left.order_index || 0) - Number(right.order_index || 0)
        || String(left.node_id || '').localeCompare(String(right.node_id || ''))
      ));
    // A durable node can arrive through plan, recovery, and lifecycle events.
    // They must remain one visible step; otherwise the same failed tool is
    // rendered once per event and only one copy receives recovered_by.
    const stepBySemanticKey = new Map();
    rawSteps.forEach(step => {
      const isBranch = step.type === 'branch' || step.node_type === 'branch';
      const semanticKey = !isBranch && String(step.title || '').trim()
        ? `step-title:${String(step.title || '').trim()}`
        : `${isBranch ? 'branch' : 'step'}:${String(step.node_id || step.step_id || '')}`;
      const prior = stepBySemanticKey.get(semanticKey);
      if (!prior) {
        stepBySemanticKey.set(semanticKey, step);
        return;
      }
      const priorLinks = prior.metadata?.recovered_by || [];
      const nextLinks = step.metadata?.recovered_by || [];
      const links = [...priorLinks];
      nextLinks.forEach(link => {
        if (!links.some(item => JSON.stringify(item) === JSON.stringify(link))) links.push(link);
      });
      stepBySemanticKey.set(semanticKey, {
        ...prior,
        ...step,
        status: links.length && ['failed', 'blocked', 'cancelled'].includes(String(prior.status || step.status))
          ? 'failed' : (step.status || prior.status),
        metadata: { ...(prior.metadata || {}), ...(step.metadata || {}), recovered_by: links },
      });
    });
    const steps = [...stepBySemanticKey.values()];
    const stepIds = new Set(steps.map(step => String(step.node_id || step.step_id)));
    const stepTitles = new Set(
      steps
        .filter(step => step.type !== 'branch' && step.node_type !== 'branch')
        .map(step => String(step.title || '').trim())
        .filter(Boolean),
    );
    const branches = Object.values(state.branches || {})
      .filter(branch => {
        if (branch.run_id !== runId || stepIds.has(String(branch.branch_id))) return false;
        // A branch created from a durable Plan node is a scheduling
        // projection of that same node, not an extra user-visible task.  If
        // we render both records, every tool step appears twice and the DAG
        // looks as if the Agent retried work that it actually ran once.
        const sourceNodeId = String(
          branch.source_plan_node_id || branch.source_node_id || '',
        );
        if (sourceNodeId && stepIds.has(sourceNodeId)) return false;
        const branchTitle = String(branch.title || branch.objective || '').trim();
        return !branchTitle || !stepTitles.has(branchTitle);
      })
      .map((branch, index) => ({
        ...branch,
        node_id: branch.branch_id,
        parent_node_id: branch.parent_branch_id || branch.parent_id || null,
        title: branch.title || branch.objective || branch.branch_id,
        type: 'branch',
        order_index: Number(branch.order_index ?? index),
      }));
    return [...branches, ...steps].sort((left, right) => (
      Number(left.order_index || 0) - Number(right.order_index || 0)
      || String(left.node_id || '').localeCompare(String(right.node_id || ''))
    ));
  }

  function buildTree(steps) {
    const byId = new Map(steps.map(step => [String(step.node_id || step.step_id), step]));
    const children = new Map();
    const roots = [];
    steps.forEach(step => {
      const id = String(step.node_id || step.step_id);
      const parent = String(step.parent_node_id || step.parent_id || '');
      if (parent && parent !== id && byId.has(parent)) {
        if (!children.has(parent)) children.set(parent, []);
        children.get(parent).push(step);
      } else {
        roots.push(step);
      }
    });
    return { byId, children, roots };
  }

  function revisionDiff(previous, current) {
    const prior = new Map(
      (previous?.plan?.nodes || []).map(node => [String(node.node_id), JSON.stringify(node)]),
    );
    const next = new Map(
      (current?.plan?.nodes || []).map(node => [String(node.node_id), JSON.stringify(node)]),
    );
    const added = [...next.keys()].filter(id => !prior.has(id)).length;
    const removed = [...prior.keys()].filter(id => !next.has(id)).length;
    const changed = [...next.keys()].filter(id => prior.has(id) && prior.get(id) !== next.get(id)).length;
    return { added, removed, changed };
  }

  function renderReflection(container, state, runId) {
    const reflectionEvent = Object.values(state.events || {})
      .filter(event => event.run_id === runId && event.type === 'reflection.completed')
      .sort((left, right) => Number(left.sequence || 0) - Number(right.sequence || 0))
      .at(-1);
    if (!reflectionEvent) return;
    const reflection = reflectionEvent.payload?.reflection || reflectionEvent.reflection || {};
    const section = document.createElement('section');
    section.className = 'agent-public-reflection';
    const heading = document.createElement('strong');
    heading.textContent = 'Agent 反思';
    const summary = document.createElement('p');
    summary.textContent = `偏好方案：${reflection.preferred_option || '尚未形成偏好方案'}`;
    section.append(heading, summary);
    [
      ['替代方案', reflection.alternatives],
      ['未知事項', reflection.unknowns],
      ['重要風險', reflection.important_risks],
    ].forEach(([label, values]) => {
      if (!Array.isArray(values) || !values.length) return;
      const item = document.createElement('p');
      item.textContent = `${label}：${values.join('；')}`;
      section.append(item);
    });
    container.append(section);
  }

  function tag(text, tone = '') {
    const item = document.createElement('span');
    item.className = `agent-task-tag ${tone}`;
    item.textContent = text;
    return item;
  }

  function dependencyIds(step) {
    return (step.dependency_ids || step.dependencies || [])
      .map(value => (typeof value === 'object'
        ? value.node_id || value.step_id || value.id
        : value))
      .filter(Boolean)
      .map(String);
  }

  function artifactAnchor(step, state = {}) {
    const runId = step.run_id || activeRunId(state);
    const forest = Object.values(state.forests || {}).find(item => item.run_id === runId) || {};
    const plan = Object.values(state.plans || {}).find(item => item.run_id === runId) || {};
    const artifactId = step.artifact_id
      || forest.artifact_id
      || plan.artifact_id
      || forest.forest_id
      || plan.plan_id
      || runId;
    const artifactVersion = Number(
      step.artifact_version
      || forest.artifact_version
      || forest.version
      || plan.artifact_version
      || plan.revision_number
      || plan.revision
      || 1,
    );
    return { artifact_id: artifactId || null, artifact_version: artifactVersion || 1 };
  }

  function selectNode(step, title, state = {}, targetType = null) {
    const anchor = artifactAnchor(step, state);
    window.AgentArtifactSelection?.select?.({
      ...anchor,
      target_type: targetType
        || (step.type === 'branch' || step.node_type === 'branch' ? 'branch' : 'step'),
      branch_id: step.branch_id || (step.type === 'branch' ? step.node_id : null),
      node_id: step.node_id || step.step_id,
      path: step.path || (String(title).includes('＞') ? String(title) : `Task Forest ＞ ${title}`),
    });
  }

  function createNode(step, tree, siblings, visited, state) {
    const id = String(step.node_id || step.step_id);
    const status = F().status(step.status);
    const recoveredBy = step.metadata?.recovered_by || [];
    const isRecoveredFailure = ['failed', 'blocked', 'cancelled'].includes(String(step.status))
      && recoveredBy.length > 0;
    const item = document.createElement('li');
    item.className = `agent-task-node is-${status.key}`;
    item.dataset.nodeId = id;
    const card = document.createElement('article');
    card.className = 'agent-task-node-card';
    const selected = String(state.active_selection?.node_id || '') === id;
    card.classList.toggle('is-selected', selected);
    card.tabIndex = 0;
    card.setAttribute('role', 'button');
    card.setAttribute('aria-pressed', String(selected));
    card.setAttribute('aria-label', `選取 ${step.title || id}`);
    const header = document.createElement('header');
    const title = document.createElement('strong');
    const statusLabel = document.createElement('span');
    title.textContent = step.title || id;
    statusLabel.textContent = isRecoveredFailure
      ? `✓ 已修復（保留${status.label}收據）`
      : `${status.icon} ${status.label}`;
    header.append(title, statusLabel);
    const tags = document.createElement('div');
    tags.className = 'agent-task-tags';
    tags.append(tag(step.type || step.node_type || 'task'));
    if (step.assigned_agent) tags.append(tag(`Agent：${step.assigned_agent}`, 'is-agent'));
    if (siblings.length > 1) tags.append(tag('平行分支', 'is-parallel'));
    if (step.capability) tags.append(tag(step.capability));
    if (isRecoveredFailure) {
      const recoveryTools = recoveredBy
        .map(link => link.recovery_tool || link.recovery_call_id)
        .filter(Boolean)
        .join('、');
      tags.append(tag(`替代證據：${recoveryTools || '已連結'}`, 'is-complete'));
    }
    const description = document.createElement('p');
    description.textContent = step.description || step.reason_summary || '';
    description.hidden = !description.textContent;
    card.append(header, tags, description);

    const dependencies = dependencyIds(step);
    if (dependencies.length) {
      const dependencyList = document.createElement('div');
      dependencyList.className = 'agent-task-dependencies';
      const label = document.createElement('b');
      label.textContent = '依賴';
      dependencyList.append(label);
      dependencies.forEach(dependencyId => {
        const dependency = tree.byId.get(dependencyId);
        const dependencyStatus = F().status(dependency?.status || 'pending');
        dependencyList.append(tag(
          `${dependencyStatus.icon} ${dependency?.title || dependencyId}`,
          dependency?.status === 'completed' ? 'is-complete' : 'is-blocking',
        ));
      });
      card.append(dependencyList);
    }

    const toolIds = step.tool_call_ids || [];
    if (toolIds.length) {
      const tools = document.createElement('div');
      tools.className = 'agent-task-tools';
      tools.append(tag(`Tool Call：${toolIds.join('、')}`, 'is-tool'));
      card.append(tools);
    }

    const blockedReason = step.blocked_reason
      || step.error_summary
      || step.metadata?.blocked_reason;
    if (blockedReason && ['blocked', 'failed'].includes(String(step.status))) {
      const blocked = document.createElement('p');
      blocked.className = 'agent-task-blocked-reason';
      blocked.textContent = `阻塞原因：${blockedReason}`;
      card.append(blocked);
    }
    if (step.result_summary) {
      const result = document.createElement('p');
      result.className = 'agent-task-result';
      result.textContent = `結果：${step.result_summary}`;
      card.append(result);
    }
    item.append(card);

    if (visited.has(id)) return item;
    visited.add(id);
    const childSteps = tree.children.get(id) || [];
    if (childSteps.length) {
      const list = document.createElement('ol');
      list.className = 'agent-task-children';
      const collapsed = Boolean(state.collapsed_task_nodes?.[id]);
      list.hidden = collapsed;
      card.setAttribute('aria-expanded', String(!collapsed));
      card.setAttribute('aria-controls', `agent-task-children-${id}`);
      list.id = `agent-task-children-${id}`;
      childSteps.forEach(child => list.append(createNode(child, tree, childSteps, visited, state)));
      item.append(list);
      const activate = () => {
        selectNode(step, title.textContent, state);
        list.hidden = !list.hidden;
        card.setAttribute('aria-expanded', String(!list.hidden));
        const store = window.AgentDockStore;
        if (store) store.set({
          collapsed_task_nodes: {
            ...(store.getState().collapsed_task_nodes || {}),
            [id]: list.hidden,
          },
        });
      };
      card.addEventListener('click', activate);
      card.addEventListener('keydown', event => {
        if (!['Enter', ' '].includes(event.key)) return;
        event.preventDefault();
        activate();
      });
    } else {
      const activate = () => selectNode(step, title.textContent, state);
      card.addEventListener('click', activate);
      card.addEventListener('keydown', event => {
        if (!['Enter', ' '].includes(event.key)) return;
        event.preventDefault();
        activate();
      });
    }
    return item;
  }

  function renderCriteria(container, plan) {
    const criteria = plan?.completion_criteria || [];
    if (!criteria.length) return;
    const section = document.createElement('section');
    section.className = 'agent-task-criteria';
    const title = document.createElement('strong');
    const list = document.createElement('ul');
    title.textContent = '完成標準';
    criteria.forEach(value => {
      const item = document.createElement('li');
      item.textContent = value;
      list.append(item);
    });
    section.append(title, list);
    container.append(section);
  }

  function selectableNode(step, label, path, state = {}, targetType = null) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'agent-schema-node';
    button.textContent = label;
    button.dataset.nodeId = step.node_id || step.step_id || '';
    button.addEventListener('click', () => selectNode(step, path || label, state, targetType));
    return button;
  }

  function dagLayout(steps) {
    const byId = new Map(steps.map(step => [String(step.node_id || step.step_id), step]));
    const levels = new Map();
    const visiting = new Set();
    function levelFor(step) {
      const id = String(step.node_id || step.step_id);
      if (levels.has(id)) return levels.get(id);
      if (visiting.has(id)) return 0;
      visiting.add(id);
      const parents = dependencyIds(step).map(value => byId.get(value)).filter(Boolean);
      const level = parents.length ? Math.max(...parents.map(levelFor)) + 1 : 0;
      visiting.delete(id);
      levels.set(id, level);
      return level;
    }
    steps.forEach(levelFor);
    const columns = new Map();
    steps.forEach(step => {
      const level = levels.get(String(step.node_id || step.step_id)) || 0;
      if (!columns.has(level)) columns.set(level, []);
      columns.get(level).push(step);
    });
    const nodeWidth = 156;
    const nodeHeight = 52;
    const columnGap = 62;
    const rowGap = 26;
    const positions = new Map();
    columns.forEach((items, level) => items.forEach((step, index) => {
      positions.set(String(step.node_id || step.step_id), {
        x: 16 + level * (nodeWidth + columnGap),
        y: 16 + index * (nodeHeight + rowGap),
      });
    }));
    const maxLevel = Math.max(0, ...columns.keys());
    const maxRows = Math.max(1, ...[...columns.values()].map(items => items.length));
    return {
      byId, columns, positions, nodeWidth, nodeHeight,
      width: 32 + ((maxLevel + 1) * nodeWidth) + (maxLevel * columnGap),
      height: 32 + (maxRows * nodeHeight) + ((maxRows - 1) * rowGap),
    };
  }

  function svgElement(name, attributes = {}) {
    const node = document.createElementNS('http://www.w3.org/2000/svg', name);
    Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
    return node;
  }

  function renderPlanDag(container, steps, state = {}) {
    if (!steps.length) return;
    const section = document.createElement('section');
    section.className = 'agent-schema-visual agent-plan-dag';
    const title = document.createElement('strong');
    title.textContent = 'Plan 依賴圖（DAG）';
    const viewport = document.createElement('div');
    viewport.className = 'agent-dag-viewport';
    const graph = document.createElement('div');
    graph.className = 'agent-dag-graph';
    graph.setAttribute('role', 'group');
    graph.setAttribute('aria-label', 'Plan 節點與依賴連線');
    const layout = dagLayout(steps);
    graph.style.width = `${layout.width}px`;
    graph.style.height = `${layout.height}px`;
    const edges = svgElement('svg', {
      class: 'agent-dag-edges', viewBox: `0 0 ${layout.width} ${layout.height}`,
      width: layout.width, height: layout.height, 'aria-hidden': 'true',
    });
    steps.forEach(step => {
      const id = String(step.node_id || step.step_id);
      const destination = layout.positions.get(id);
      dependencyIds(step).forEach(dependencyId => {
        const source = layout.positions.get(dependencyId);
        if (!source || !destination) return;
        const x1 = source.x + layout.nodeWidth;
        const y1 = source.y + (layout.nodeHeight / 2);
        const x2 = destination.x;
        const y2 = destination.y + (layout.nodeHeight / 2);
        const midpoint = x1 + ((x2 - x1) / 2);
        const edge = svgElement('path', {
          d: `M ${x1} ${y1} C ${midpoint} ${y1}, ${midpoint} ${y2}, ${x2} ${y2}`,
          class: 'agent-dag-edge',
          'data-from': dependencyId,
          'data-to': id,
        });
        edges.append(edge);
      });
    });
    graph.append(edges);
    steps.forEach(step => {
      const id = String(step.node_id || step.step_id);
      const position = layout.positions.get(id);
      const status = F().status(step.status);
      const node = selectableNode(
        step,
        `${status.icon} ${step.title || id}`,
        `Plan DAG ＞ ${step.title || id}`,
        state,
        step.type === 'branch' || step.node_type === 'branch' ? 'branch' : 'step',
      );
      node.classList.add('agent-dag-node', `is-${status.key}`);
      node.style.left = `${position.x}px`;
      node.style.top = `${position.y}px`;
      node.style.width = `${layout.nodeWidth}px`;
      node.style.minHeight = `${layout.nodeHeight}px`;
      node.title = dependencyIds(step).length
        ? `依賴：${dependencyIds(step).map(value => layout.byId.get(value)?.title || value).join('、')}`
        : '起始節點';
      graph.append(node);
    });
    viewport.append(graph);
    section.append(title, viewport);
    container.append(section);
  }

  function hasParallelBranches(steps) {
    const byId = new Map(steps.map(step => [String(step.node_id || step.step_id), step]));
    const startingLanes = new Set();
    steps.forEach(step => {
      if (step.type === 'branch' || step.node_type === 'branch') return;
      const hasExecutableCapability = Boolean(
        step.capability || step.tool_name || (step.tool_call_ids || []).length,
      );
      if (!hasExecutableCapability) return;
      const hasPlanDependency = dependencyIds(step).some(dependencyId => byId.has(dependencyId));
      if (hasPlanDependency) return;
      startingLanes.add(String(step.parent_node_id || step.parent_id || step.branch_id || 'main'));
    });
    // Branch records are also used to record each sequential tool turn.  A
    // lane diagram is meaningful only when two executable lanes can start
    // without waiting for one another; otherwise it repeats a linear plan.
    return startingLanes.size >= 2;
  }

  function renderSwimlanes(container, steps, state = {}) {
    if (!hasParallelBranches(steps)) return;
    const lanes = new Map();
    steps.forEach(step => {
      const lane = String(step.parent_node_id || step.parent_id || step.branch_id || 'main');
      if (!lanes.has(lane)) lanes.set(lane, []);
      lanes.get(lane).push(step);
    });
    if (lanes.size < 2) return;
    const section = document.createElement('section');
    section.className = 'agent-schema-visual agent-branch-swimlanes';
    const title = document.createElement('strong');
    title.textContent = '平行 Branch（Swimlane）';
    const laneWrap = document.createElement('div');
    laneWrap.className = 'agent-swimlane-wrap';
    lanes.forEach((items, laneId) => {
      const lane = document.createElement('section');
      const heading = document.createElement('small');
      const list = document.createElement('div');
      lane.className = 'agent-swimlane';
      heading.textContent = laneId === 'main' ? '主線' : `Branch：${laneId}`;
      items.forEach(step => list.append(selectableNode(
        step, step.title || step.node_id, `Swimlane ＞ ${step.title || step.node_id}`, state,
      )));
      lane.append(heading, list);
      laneWrap.append(lane);
    });
    section.append(title, laneWrap);
    container.append(section);
  }

  function renderRecoveryFishbone(container, steps, state = {}) {
    const failures = steps.filter(step => (
      step.type !== 'branch'
      && step.node_type !== 'branch'
      && ['failed', 'blocked', 'cancelled'].includes(String(step.status))
    ));
    if (!failures.length) return;
    const section = document.createElement('section');
    section.className = 'agent-schema-visual agent-recovery-fishbone';
    const title = document.createElement('strong');
    title.textContent = '錯誤根因與恢復路徑（Fishbone）';
    const diagram = document.createElement('div');
    diagram.className = 'agent-fishbone-diagram';
    diagram.setAttribute('role', 'group');
    diagram.setAttribute('aria-label', '失敗根因、恢復策略與未完成結果');
    const axis = document.createElement('span');
    axis.className = 'agent-fishbone-axis';
    axis.setAttribute('aria-hidden', 'true');
    const causes = document.createElement('div');
    causes.className = 'agent-fishbone-causes';
    failures.forEach((step, index) => {
      const item = document.createElement('article');
      item.className = index % 2 ? 'is-lower' : 'is-upper';
      const required = step.metadata?.required_recovery || step.required_recovery || {};
      const recovered = step.metadata?.recovered_by || [];
      const reason = step.error_summary || step.error?.message || required.error?.message || 'Host 正在保留失敗收據。';
      const strategy = required.acceptable_alternatives?.length
        ? `下一層：${required.acceptable_alternatives.join('、')}`
        : step.metadata?.recovery?.action || '等待 Host 選擇下一層恢復策略';
      const status = recovered.length
        ? `已由 ${recovered.map(link => link.recovery_tool || link.recovery_call_id).join('、')} 取得替代證據`
        : '尚未取得替代證據，不能標示為完成';
      const name = selectableNode(
        step, step.title || step.node_id, `Fishbone ＞ ${step.title || step.node_id}`, state, 'condition',
      );
      const cause = document.createElement('p');
      const action = document.createElement('p');
      const evidence = document.createElement('p');
      cause.textContent = `原因：${reason}`;
      action.textContent = strategy;
      evidence.textContent = status;
      item.append(name, cause, action, evidence);
      causes.append(item);
    });
    const effect = document.createElement('strong');
    effect.className = 'agent-fishbone-effect';
    const unresolved = failures.filter(step => !(step.metadata?.recovered_by || []).length);
    effect.textContent = unresolved.length
      ? 'Run 尚未完成'
      : '失敗已由替代證據修復；原始收據保留';
    diagram.append(axis, causes, effect);
    section.append(title, diagram);
    container.append(section);
  }

  function renderSchemaVisualizations(container, steps, state = {}) {
    renderPlanDag(container, steps, state);
    renderSwimlanes(container, steps, state);
    renderRecoveryFishbone(container, steps, state);
  }

  function renderRevisions(container, revisions) {
    if (!revisions.length) return;
    const details = document.createElement('details');
    details.className = 'agent-task-revisions';
    const summary = document.createElement('summary');
    const list = document.createElement('ol');
    summary.textContent = `Revision History · ${revisions.length} 版`;
    revisions.forEach((revision, index) => {
      const diff = revisionDiff(revisions[index - 1], revision);
      const item = document.createElement('li');
      const title = document.createElement('strong');
      const reason = document.createElement('p');
      title.textContent = `Revision ${revision.revision} · +${diff.added} / −${diff.removed} / Δ${diff.changed}`;
      reason.textContent = revision.reason_summary || '未提供修改原因';
      item.append(title, reason);
      list.append(item);
    });
    details.append(summary, list);
    container.append(details);
  }

  function render(container, state) {
    if (!container) return;
    container.classList.add('agent-task-tree');
    container.replaceChildren();
    const runId = activeRunId(state);
    const steps = stepsForRun(state, runId);
    const forest = Object.values(state.forests || {}).find(item => item.run_id === runId);
    const plan = Object.values(state.plans).find(item => (
      item.run_id === runId && item.plan_id === state.runs[runId]?.plan_id
    )) || Object.values(state.plans).find(item => item.run_id === runId);
    if (forest) {
      const heading = document.createElement('header');
      heading.className = 'agent-task-forest-heading';
      const title = document.createElement('strong');
      const meta = document.createElement('small');
      title.textContent = forest.title || forest.objective || 'Task Forest';
      const workCount = (plan?.nodes || []).length || steps.length;
      meta.textContent = `${workCount} 個工作項目`;
      heading.append(title, meta);
      container.append(heading);
    }
    renderCriteria(container, plan);
    renderReflection(container, state, runId);
    renderSchemaVisualizations(container, steps, state);
    if (!steps.length) {
      const empty = document.createElement('p');
      empty.className = 'agent-empty-state';
      empty.textContent = '目前 Run 尚未建立可顯示的階層任務。';
      container.append(empty);
      renderRevisions(container, state.plan_revisions?.[runId] || []);
      return;
    }
    const tree = buildTree(steps);
    const list = document.createElement('ol');
    list.className = 'agent-task-roots';
    const visited = new Set();
    tree.roots.forEach(step => list.append(createNode(step, tree, tree.roots, visited, state)));
    // Defensive fallback for malformed imported plans: never hide an orphan or
    // cycle merely because it cannot be reached from a root.
    steps.filter(step => !visited.has(String(step.node_id || step.step_id)))
      .forEach(step => list.append(createNode(step, tree, [step], visited, state)));
    container.append(list);
    renderRevisions(container, state.plan_revisions?.[runId] || []);
  }
  window.AgentTaskTreeView = {
    activeRunId, artifactAnchor, buildTree, dagLayout, dependencyIds, revisionDiff, render, renderPlanDag,
    hasParallelBranches, renderRecoveryFishbone, renderReflection, renderSchemaVisualizations, renderSwimlanes,
    stepsForRun,
  };
}());
