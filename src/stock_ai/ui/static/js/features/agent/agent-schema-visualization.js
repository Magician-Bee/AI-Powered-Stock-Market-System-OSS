(function () {
  'use strict';

  const RENDERERS = new Set([
    'table', 'chart', 'graph', 'canvas', 'map', 'evidence_graph', 'task_forest',
    'dag', 'swimlane', 'workflow', 'timeline', 'gantt', 'fishbone',
    'decision_matrix', 'risk_table', 'risk_bar', 'risk_radar',
  ]);

  function items(value) {
    return Array.isArray(value) ? value.filter(item => item && typeof item === 'object') : [];
  }

  function schemaFor(artifact) {
    const structured = artifact?.renderer && Object.prototype.hasOwnProperty.call(artifact || {}, 'document')
      ? {
          ...(artifact.document && typeof artifact.document === 'object' && !Array.isArray(artifact.document)
            ? artifact.document : { items: artifact.document }),
          renderer: artifact.renderer,
          schema_version: artifact.schema_version,
        }
      : null;
    const versionContent = artifact?.content
      && typeof artifact.content === 'object'
      && artifact.content.renderer
      && Object.prototype.hasOwnProperty.call(artifact.content, 'document')
      ? {
          ...(artifact.content.document && typeof artifact.content.document === 'object'
            && !Array.isArray(artifact.content.document)
            ? artifact.content.document : { items: artifact.content.document }),
          renderer: artifact.content.renderer,
          schema_version: artifact.content.schema_version,
        }
      : null;
    const candidates = [
      structured,
      versionContent,
      artifact?.visualization,
      artifact?.metadata?.visualization,
      artifact?.content?.visualization,
      artifact?.schema,
    ];
    const schema = candidates.find(value => value && typeof value === 'object' && !Array.isArray(value));
    if (!schema) return null;
    const renderer = String(schema.renderer || schema.type || '').toLowerCase();
    return RENDERERS.has(renderer) ? { ...schema, renderer } : null;
  }

  function textNode(tag, value, className = '') {
    const node = document.createElement(tag);
    node.textContent = String(value ?? '');
    if (className) node.className = className;
    return node;
  }

  function artifactAnchor(artifact) {
    return {
      artifact_id: artifact.artifact_id || artifact.id || null,
      artifact_version: Number(artifact.artifact_version || artifact.version || 1) || 1,
    };
  }

  function itemId(item, index = 0) {
    return String(item?.node_id || item?.id || item?.option_id || item?.key || index);
  }

  function itemLabel(item, fallback = 'Node') {
    return String(item?.label || item?.title || item?.name || item?.id || fallback);
  }

  function selectVisual(artifact, item, targetType, path, extra = {}) {
    window.AgentArtifactSelection?.select?.({
      ...artifactAnchor(artifact),
      target_type: targetType,
      branch_id: item?.branch_id || artifact.branch_id || null,
      node_id: itemId(item),
      path,
      ...extra,
    });
  }

  function visualButton(artifact, item, targetType, path, label = null) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'agent-schema-selectable';
    button.dataset.nodeId = itemId(item);
    button.textContent = label || itemLabel(item);
    button.addEventListener('click', () => selectVisual(artifact, item, targetType, path));
    return button;
  }

  function dependencyIds(item) {
    return (Array.isArray(item?.dependencies) ? item.dependencies : [])
      .map(value => (typeof value === 'object' ? value.id || value.node_id || value.label : value))
      .filter(Boolean)
      .map(String);
  }

  function dagCoordinates(nodes) {
    const byId = new Map(nodes.map((node, index) => [itemId(node, index), node]));
    const levels = new Map();
    const visiting = new Set();
    function visit(node, index = 0) {
      const id = itemId(node, index);
      if (levels.has(id)) return levels.get(id);
      if (visiting.has(id)) return 0;
      visiting.add(id);
      const parents = dependencyIds(node).map(value => byId.get(value)).filter(Boolean);
      const level = parents.length ? Math.max(...parents.map(parent => visit(parent))) + 1 : 0;
      visiting.delete(id);
      levels.set(id, level);
      return level;
    }
    nodes.forEach(visit);
    const columns = new Map();
    nodes.forEach((node, index) => {
      const level = levels.get(itemId(node, index)) || 0;
      if (!columns.has(level)) columns.set(level, []);
      columns.get(level).push({ node, index });
    });
    const width = 148;
    const height = 50;
    const positions = new Map();
    columns.forEach((column, level) => column.forEach(({ node, index }, row) => {
      positions.set(itemId(node, index), { x: 14 + (level * 208), y: 14 + (row * 74) });
    }));
    return {
      byId, positions, nodeWidth: width, nodeHeight: height,
      width: 28 + ((Math.max(0, ...columns.keys()) + 1) * width) + (Math.max(0, ...columns.keys()) * 60),
      height: 28 + (Math.max(1, ...[...columns.values()].map(column => column.length)) * height)
        + ((Math.max(1, ...[...columns.values()].map(column => column.length)) - 1) * 24),
    };
  }

  function renderDag(body, schema, artifact) {
    body.className = 'agent-schema-dag agent-diagram-viewport';
    const nodes = items(schema.nodes);
    const layout = dagCoordinates(nodes);
    const canvas = document.createElement('div');
    canvas.className = 'agent-schema-dag-canvas';
    canvas.style.width = `${layout.width}px`;
    canvas.style.height = `${layout.height}px`;
    nodes.forEach((item, index) => {
      const destinationId = itemId(item, index);
      const destination = layout.positions.get(destinationId);
      dependencyIds(item).forEach(parentId => {
        const source = layout.positions.get(parentId);
        if (!source || !destination) return;
        const x1 = source.x + layout.nodeWidth;
        const y1 = source.y + (layout.nodeHeight / 2);
        const x2 = destination.x;
        const y2 = destination.y + (layout.nodeHeight / 2);
        const mid = x1 + ((x2 - x1) / 2);
        const edge = document.createElement('span');
        edge.className = 'agent-dom-dag-edge';
        edge.dataset.from = parentId;
        edge.dataset.to = destinationId;
        edge.setAttribute('aria-hidden', 'true');
        const first = document.createElement('i');
        const vertical = document.createElement('i');
        const last = document.createElement('i');
        first.className = 'is-horizontal';
        first.style.left = `${x1}px`;
        first.style.top = `${y1}px`;
        first.style.width = `${Math.max(0, mid - x1)}px`;
        vertical.className = 'is-vertical';
        vertical.style.left = `${mid}px`;
        vertical.style.top = `${Math.min(y1, y2)}px`;
        vertical.style.height = `${Math.abs(y2 - y1)}px`;
        last.className = 'is-horizontal';
        last.style.left = `${mid}px`;
        last.style.top = `${y2}px`;
        last.style.width = `${Math.max(0, x2 - mid)}px`;
        edge.append(first, vertical, last);
        canvas.append(edge);
      });
    });
    nodes.forEach((item, index) => {
      const id = itemId(item, index);
      const position = layout.positions.get(id);
      const button = visualButton(
        artifact, item, 'visualization_node',
        `${schema.title || 'DAG'} ＞ ${itemLabel(item)}`,
      );
      button.classList.add('agent-dag-node');
      button.style.left = `${position.x}px`;
      button.style.top = `${position.y}px`;
      button.style.width = `${layout.nodeWidth}px`;
      button.style.minHeight = `${layout.nodeHeight}px`;
      button.dataset.status = String(item.status || 'pending');
      canvas.append(button);
    });
    body.append(canvas);
  }

  function renderSwimlane(body, schema, artifact) {
    body.className = 'agent-schema-swimlane';
    items(schema.lanes).forEach(lane => {
      const column = document.createElement('section');
      column.append(textNode('strong', lane.label || lane.title || lane.id || 'Branch'));
      const list = document.createElement('ol');
      items(lane.steps || lane.items).forEach(step => {
        const row = document.createElement('li');
        row.dataset.status = String(step.status || 'pending');
        row.append(visualButton(
          artifact, step, 'visualization_node',
          `${schema.title || 'Swimlane'} ＞ ${itemLabel(lane, 'Branch')} ＞ ${itemLabel(step, 'Step')}`,
        ));
        list.append(row);
      });
      column.append(list);
      body.append(column);
    });
  }

  function renderTimeline(body, schema, artifact, gantt = false) {
    body.className = gantt ? 'agent-schema-gantt' : 'agent-schema-timeline';
    const rows = items(schema.tasks || schema.events || schema.items);
    const starts = rows.map(item => Number(item.start ?? item.offset ?? 0));
    const ends = rows.map((item, index) => Number(item.end ?? (starts[index] + Number(item.duration || 1))));
    const minimum = starts.length ? Math.min(...starts) : 0;
    const maximum = ends.length ? Math.max(...ends) : 1;
    const span = Math.max(1, maximum - minimum);
    rows.forEach((item, index) => {
      const row = document.createElement('div');
      const label = itemLabel(item, gantt ? 'Task' : 'Event');
      row.append(visualButton(
        artifact, item, gantt ? 'schedule' : 'timeline_event',
        `${schema.title || (gantt ? 'Gantt' : 'Timeline')} ＞ ${label}`,
        label,
      ));
      const rail = document.createElement('span');
      rail.className = 'agent-schema-time-rail';
      const bar = document.createElement('i');
      const start = starts[index];
      const end = ends[index];
      bar.style.setProperty('--start', `${Math.max(0, ((start - minimum) / span) * 100)}%`);
      bar.style.setProperty('--size', `${Math.max(2, ((end - start) / span) * 100)}%`);
      rail.append(bar);
      row.append(rail);
      body.append(row);
    });
  }

  function renderFishbone(body, schema, artifact) {
    body.className = 'agent-schema-fishbone';
    const axis = document.createElement('span');
    axis.className = 'agent-fishbone-axis';
    axis.setAttribute('aria-hidden', 'true');
    const causes = document.createElement('div');
    causes.className = 'agent-fishbone-causes';
    items(schema.causes || schema.categories).forEach((cause, index) => {
      const branch = document.createElement('section');
      branch.className = index % 2 ? 'is-lower' : 'is-upper';
      const label = cause.label || cause.category || 'Cause';
      branch.append(visualButton(
        artifact, cause, 'condition', `${schema.title || 'Fishbone'} ＞ ${label}`, label,
      ));
      const list = document.createElement('ul');
      const reasons = Array.isArray(cause.items) ? cause.items : (Array.isArray(cause.causes) ? cause.causes : []);
      reasons.forEach((reason, reasonIndex) => {
        const row = document.createElement('li');
        const item = typeof reason === 'object' ? reason : { id: `${itemId(cause, index)}:${reasonIndex}`, label: reason };
        const reasonLabel = item.label || item.text || String(reason);
        row.append(visualButton(
          artifact, item, 'condition', `${schema.title || 'Fishbone'} ＞ ${label} ＞ ${reasonLabel}`, reasonLabel,
        ));
        list.append(row);
      });
      branch.append(list);
      causes.append(branch);
    });
    const effect = visualButton(
      artifact,
      { id: 'effect', label: schema.effect || schema.problem || '錯誤結果' },
      'visualization_effect',
      `${schema.title || 'Fishbone'} ＞ ${schema.effect || schema.problem || '錯誤結果'}`,
      schema.effect || schema.problem || '錯誤結果',
    );
    effect.classList.add('agent-fishbone-effect');
    body.append(axis, causes, effect);
  }

  function renderDecisionMatrix(body, schema, artifact) {
    body.className = 'agent-schema-decision-matrix';
    const table = document.createElement('table');
    const criteria = Array.isArray(schema.criteria) ? schema.criteria : [];
    const head = document.createElement('tr');
    head.append(textNode('th', '方案'));
    criteria.forEach(item => head.append(textNode('th', item.label || item.name || item)));
    head.append(textNode('th', '總分'));
    table.append(head);
    items(schema.options).forEach(option => {
      const row = document.createElement('tr');
      const optionName = option.label || option.name || option.id || 'Option';
      const name = document.createElement('th');
      name.append(visualButton(
        artifact, option, 'decision_option', `${schema.title || 'Decision Matrix'} ＞ ${optionName}`, optionName,
      ));
      row.append(name);
      const scores = Array.isArray(option.scores) ? option.scores : [];
      criteria.forEach((criterion, index) => {
        const key = criterion.id || criterion.name || criterion.label;
        row.append(textNode('td', scores[index] ?? option.values?.[key] ?? '—'));
      });
      row.append(textNode('td', option.total ?? option.score ?? '—'));
      table.append(row);
    });
    body.append(table);
  }

  function renderRisk(body, schema, artifact) {
    const factors = items(schema.factors || schema.items);
    body.className = `agent-schema-${schema.renderer}`;
    if (schema.renderer === 'risk_table') {
      const table = document.createElement('table');
      factors.forEach(item => {
        const row = document.createElement('tr');
        const label = item.label || item.name || item.id || 'Risk';
        const heading = document.createElement('th');
        heading.append(visualButton(
          artifact, item, 'chart_region', `${schema.title || 'Risk'} ＞ ${label}`, label,
        ));
        row.append(heading);
        row.append(textNode('td', item.value ?? item.score ?? '—'));
        table.append(row);
      });
      body.append(table);
      return;
    }
    if (schema.renderer === 'risk_radar') {
      const radar = document.createElement('div');
      radar.className = 'agent-schema-radar-shape';
      radar.style.setProperty('--risk-level', String(Math.max(0, Math.min(1,
        factors.reduce((sum, item) => sum + Number(item.value ?? item.score ?? 0), 0) / Math.max(1, factors.length),
      ))));
      body.append(radar);
    }
    factors.forEach(item => {
      const row = document.createElement('div');
      const label = item.label || item.name || item.id || 'Risk';
      row.append(visualButton(
        artifact, item, 'chart_region', `${schema.title || 'Risk'} ＞ ${label}`, label,
      ));
      const meter = document.createElement('meter');
      meter.min = 0;
      meter.max = Number(item.max || 1);
      meter.value = Number(item.value ?? item.score ?? 0);
      row.append(meter, textNode('strong', item.value ?? item.score ?? '—'));
      body.append(row);
    });
  }

  function renderWorkflow(body, schema, artifact) {
    body.className = 'agent-schema-workflow';
    const list = document.createElement('ol');
    items(schema.steps || schema.nodes).forEach(step => {
      const row = document.createElement('li');
      row.dataset.status = String(step.status || 'pending');
      row.append(visualButton(
        artifact, step, 'automation_node', `${schema.title || 'Workflow'} ＞ ${itemLabel(step, 'Step')}`,
      ));
      list.append(row);
    });
    body.append(list);
  }

  function renderCollection(body, schema, artifact) {
    const rows = items(schema.rows || schema.items || schema.nodes || schema.features);
    body.className = `agent-schema-${schema.renderer}`;
    if (schema.renderer === 'table') {
      const table = document.createElement('table');
      const columns = Array.isArray(schema.columns) ? schema.columns : [];
      if (columns.length) {
        const head = document.createElement('tr');
        columns.forEach(column => head.append(textNode('th', column.label || column.name || column.key || column)));
        table.append(head);
      }
      rows.forEach((item, index) => {
        const row = document.createElement('tr');
        const values = columns.length
          ? columns.map(column => item[column.key || column.name || column])
          : Object.values(item);
        values.forEach((value, valueIndex) => {
          const cell = document.createElement(valueIndex === 0 ? 'th' : 'td');
          if (valueIndex === 0) cell.append(visualButton(
            artifact, item, 'visualization_node',
            `${schema.title || 'Table'} ＞ ${itemLabel(item, String(value ?? index + 1))}`,
            value,
          ));
          else cell.textContent = String(value ?? '—');
          row.append(cell);
        });
        table.append(row);
      });
      body.append(table);
      return;
    }
    rows.forEach((item, index) => {
      const card = document.createElement('article');
      card.append(visualButton(
        artifact, item, schema.renderer === 'chart' ? 'chart_region' : 'visualization_node',
        `${schema.title || schema.renderer} ＞ ${itemLabel(item, `Item ${index + 1}`)}`,
      ));
      const detail = item.summary || item.description || item.value;
      if (detail !== undefined) card.append(textNode('small', detail));
      body.append(card);
    });
  }

  function renderOne(artifact) {
    const schema = schemaFor(artifact);
    if (!schema) return null;
    const section = document.createElement('section');
    section.className = 'agent-schema-visualization';
    section.dataset.renderer = schema.renderer;
    section.append(textNode('strong', schema.title || artifact.title || artifact.name || 'Visualization'));
    const body = document.createElement('div');
    if (schema.renderer === 'dag') renderDag(body, schema, artifact);
    else if (schema.renderer === 'swimlane') renderSwimlane(body, schema, artifact);
    else if (schema.renderer === 'workflow') renderWorkflow(body, schema, artifact);
    else if (schema.renderer === 'timeline') renderTimeline(body, schema, artifact);
    else if (schema.renderer === 'gantt') renderTimeline(body, schema, artifact, true);
    else if (schema.renderer === 'fishbone') renderFishbone(body, schema, artifact);
    else if (schema.renderer === 'decision_matrix') renderDecisionMatrix(body, schema, artifact);
    else if (schema.renderer.startsWith('risk_')) renderRisk(body, schema, artifact);
    else renderCollection(body, schema, artifact);
    section.append(body);
    return section;
  }

  function render(container, state) {
    if (!container) return;
    container.replaceChildren();
    Object.values(state.artifacts || {})
      .filter(item => !state.active_session_id || item.session_id === state.active_session_id)
      .map(renderOne)
      .filter(Boolean)
      .forEach(node => container.append(node));
    container.hidden = container.childElementCount === 0;
  }

  window.AgentSchemaVisualization = {
    RENDERERS, artifactAnchor, dagCoordinates, renderOne, render, schemaFor, selectVisual,
  };
}());
