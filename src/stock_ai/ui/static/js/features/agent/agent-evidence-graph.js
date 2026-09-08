(function () {
  'use strict';

  function evidenceForSession(state) {
    return Object.values(state.evidence || {})
      .filter(item => !state.active_session_id || item.session_id === state.active_session_id);
  }

  function formatEvidenceClaim(value, fallback) {
    if (typeof value === 'string' && value.trim()) {
      const text = value.trim();
      if (text.startsWith('{')) {
        try { return formatEvidenceClaim(JSON.parse(text), fallback); } catch (_error) { /* plain text */ }
      }
      return text;
    }
    if (value && typeof value === 'object') {
      const symbol = String(value.symbol || '').trim();
      if (value.schema_version === 'open_stock_ai.web_research.v1') {
        const query = String(value.query || '').trim();
        const count = Number(value.source_count || 0);
        return `外部研究已取得 ${count ? `${count} 個來源` : '外部來源'}${query ? `：${query}` : '。'}`;
      }
      const bucket = String(value.recommendation_bucket || '').trim();
      if (value.schema_version === 'open_stock_ai.agent_workspace.v1') {
        const marketState = {
          buy: '可研究的候選標的',
          sell: '風險或減碼候選',
          watch: '持續觀察',
          data_blocked: '資料不足，尚無法形成可靠判斷',
        }[bucket.toLowerCase()] || '市場研究摘要';
        const permission = String(value.execution_permission || '').trim().toLowerCase();
        const boundary = permission === 'blocked'
          ? '執行已阻擋'
          : permission
            ? `執行權限：${permission}`
            : '';
        return [symbol || '市場研究', marketState, boundary].filter(Boolean).join(' · ');
      }
      if (bucket.toLowerCase() === 'data_blocked') {
        return `${symbol ? `${symbol}：` : ''}資料不足，尚無法形成可靠判斷。`;
      }
      const semantic = [value.summary, value.message, value.interpretation, value.status]
        .find(item => typeof item === 'string' && item.trim());
      if (semantic) return `${symbol ? `${symbol}：` : ''}${semantic.trim()}`;
      // Evidence nodes are navigational controls, not raw-data viewers.  A
      // serialized tool payload can be several kilobytes long and turns a
      // compact Dock into an unreadable JSON button.  Selecting the node still
      // opens the artifact/detail view where the complete receipt is retained.
      const kind = String(value.tool_name || value.capability || value.type || value.schema_version || '')
        .replace(/^open_stock_ai\./, '')
        .replace(/\.v\d+$/, '')
        .replace(/_/g, ' ')
        .trim();
      return [symbol || 'Evidence', kind || '已取得可檢視證據'].join(' · ');
    }
    return String(fallback || 'Evidence');
  }

  function artifactAnchor(item) {
    return {
      artifact_id: item.artifact_id || `evidence:${item.evidence_id}`,
      artifact_version: Number(item.artifact_version || item.version || 1) || 1,
    };
  }

  function relationItems(item) {
    return [
      ...(item.supports || []).map(target => ({ kind: 'support', target: String(target) })),
      ...(item.contradicts || []).map(target => ({ kind: 'contradict', target: String(target) })),
    ];
  }

  function svgElement(name, attributes = {}) {
    const node = document.createElementNS('http://www.w3.org/2000/svg', name);
    Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
    return node;
  }

  function selectEvidence(item, claim, targetType = 'evidence', path = null) {
    window.AgentArtifactSelection?.select?.({
      ...artifactAnchor(item),
      target_type: targetType,
      branch_id: item.branch_id,
      node_id: item.node_id || item.evidence_id,
      evidence_id: item.evidence_id,
      path: path || item.path || `Evidence Graph ＞ ${claim}`,
    });
  }

  function evidenceButton(item, claim) {
    const node = document.createElement('button');
    node.type = 'button';
    node.className = 'agent-evidence-node';
    node.dataset.evidenceId = item.evidence_id;
    const title = document.createElement('strong');
    title.textContent = claim;
    const source = document.createElement('small');
    source.textContent = [item.source_type, item.source, item.freshness].filter(Boolean).join(' · ');
    const confidence = document.createElement('span');
    confidence.textContent = Number.isFinite(Number(item.confidence))
      ? `信心 ${Math.round(Number(item.confidence) * 100)}%` : 'Evidence';
    const version = document.createElement('b');
    version.textContent = `v${artifactAnchor(item).artifact_version}`;
    node.append(title, source, confidence, version);
    node.addEventListener('click', () => selectEvidence(item, claim));
    return node;
  }

  function render(container, state, options = {}) {
    if (!container) return;
    container.replaceChildren();
    const evidence = evidenceForSession(state);
    if (!evidence.length) {
      container.hidden = true;
      return;
    }
    container.hidden = false;
    const section = document.createElement('section');
    section.className = `agent-evidence-graph${options.compact ? ' is-compact' : ''}`;
    const header = document.createElement('header');
    const heading = document.createElement('strong');
    const count = document.createElement('small');
    heading.textContent = 'Evidence Graph';
    count.textContent = `${evidence.length} 個可選 Evidence Node`;
    header.append(heading, count);
    const viewport = document.createElement('div');
    viewport.className = 'agent-evidence-viewport';
    const canvas = document.createElement('div');
    canvas.className = 'agent-evidence-canvas';
    canvas.setAttribute('role', 'group');
    canvas.setAttribute('aria-label', 'Evidence 支持與衝突關係圖');
    const relationTargets = [...new Set(evidence.flatMap(item => relationItems(item).map(link => link.target)))];
    const sourceWidth = 190;
    const targetWidth = 170;
    const rowHeight = 74;
    const canvasWidth = relationTargets.length ? 470 : 230;
    const canvasHeight = Math.max(92, 18 + (Math.max(evidence.length, relationTargets.length) * rowHeight));
    canvas.style.width = `${canvasWidth}px`;
    canvas.style.height = `${canvasHeight}px`;
    const sourcePositions = new Map();
    const targetPositions = new Map();
    evidence.forEach((item, index) => sourcePositions.set(String(item.evidence_id), {
      x: 12, y: 12 + (index * rowHeight),
    }));
    relationTargets.forEach((target, index) => targetPositions.set(target, {
      x: 288, y: 12 + (index * rowHeight),
    }));
    const edges = svgElement('svg', {
      class: 'agent-evidence-edges', viewBox: `0 0 ${canvasWidth} ${canvasHeight}`,
      width: canvasWidth, height: canvasHeight, 'aria-hidden': 'true',
    });
    evidence.forEach(item => relationItems(item).forEach(link => {
      const source = sourcePositions.get(String(item.evidence_id));
      const target = targetPositions.get(link.target);
      if (!source || !target) return;
      const x1 = source.x + sourceWidth;
      const y1 = source.y + 27;
      const x2 = target.x;
      const y2 = target.y + 24;
      const mid = x1 + ((x2 - x1) / 2);
      edges.append(svgElement('path', {
        class: `agent-evidence-edge is-${link.kind}`,
        d: `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`,
        'data-evidence-id': item.evidence_id, 'data-target': link.target,
      }));
      const label = svgElement('text', {
        class: `agent-evidence-edge-label is-${link.kind}`,
        x: mid, y: ((y1 + y2) / 2) - 4, 'text-anchor': 'middle',
      });
      label.textContent = link.kind === 'support' ? '支持' : '衝突';
      edges.append(label);
    }));
    canvas.append(edges);
    evidence.forEach(item => {
      const position = sourcePositions.get(String(item.evidence_id));
      const claim = formatEvidenceClaim(item.claim || item.title, item.evidence_id);
      const node = evidenceButton(item, claim);
      node.style.left = `${position.x}px`;
      node.style.top = `${position.y}px`;
      node.style.width = `${sourceWidth}px`;
      canvas.append(node);
    });
    relationTargets.forEach((target, index) => {
      const position = targetPositions.get(target);
      const owner = evidence.find(item => relationItems(item).some(link => link.target === target)) || evidence[0];
      const node = document.createElement('button');
      node.type = 'button';
      node.className = 'agent-evidence-claim-node';
      node.dataset.nodeId = target;
      node.textContent = target;
      node.style.left = `${position.x}px`;
      node.style.top = `${position.y}px`;
      node.style.width = `${targetWidth}px`;
      node.addEventListener('click', () => selectEvidence(
        owner, target, 'evidence_claim', `Evidence Graph ＞ 結論 ＞ ${target}`,
      ));
      canvas.append(node);
    });
    viewport.append(canvas);
    section.append(header, viewport);
    container.append(section);
  }

  window.AgentEvidenceGraph = {
    artifactAnchor, evidenceForSession, formatEvidenceClaim, relationItems, render, selectEvidence,
  };
}());
