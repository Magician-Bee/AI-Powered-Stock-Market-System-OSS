(function () {
  'use strict';
  const F = () => window.AgentDockFormatters;
  function versionRecords(state, artifact) {
    return Object.values(state?.artifact_versions || {})
      .filter(item => String(item?.artifact_id || '') === String(artifact.artifact_id || ''))
      .sort((left, right) => Number(left.version || 0) - Number(right.version || 0));
  }

  function versionRecord(state, artifact) {
    const versions = versionRecords(state, artifact);
    const selected = state?.active_selection;
    const selectedVersion = String(selected?.artifact_id || '') === String(artifact.artifact_id || '')
      ? Number(selected?.artifact_version || 0)
      : 0;
    return versions.find(item => Number(item.version || 0) === selectedVersion)
      || versions.at(-1)
      || null;
  }

  function selectionFor(artifact, version) {
    return {
      artifact_id: artifact.artifact_id,
      artifact_version: version,
      target_type: 'artifact',
      branch_id: artifact.branch_id,
      node_id: artifact.step_id,
      path: artifact.path || `Artifacts ＞ ${artifact.title || artifact.name || artifact.artifact_id}`,
    };
  }

  function editableText(version) {
    const value = version?.content;
    if (value && typeof value === 'object' && typeof value.content === 'string') return value.content;
    return typeof value === 'string' ? value : '';
  }

  function editableContent(version, text) {
    const value = version?.content;
    return value && typeof value === 'object' ? { ...value, content: text } : text;
  }

  function appendRevisionForm(card, artifact, version, artifactVersion, title, draft = null) {
    if (card.querySelector('.agent-artifact-revision-form')) return;
    const form = document.createElement('form');
    const label = document.createElement('label');
    const content = document.createElement('textarea');
    const reason = document.createElement('input');
    const status = document.createElement('small');
    const submit = document.createElement('button');
    form.className = 'agent-artifact-revision-form';
    label.textContent = `局部修改內容（目前 v${artifactVersion}）`;
    content.value = editableText(version);
    content.required = true;
    content.setAttribute('aria-label', `修改 ${title.textContent} 的內容`);
    content.placeholder = '輸入修改後的完整文字內容';
    reason.required = true;
    reason.maxLength = 2000;
    reason.placeholder = '說明修改原因，例如：由單日改為三日累計';
    reason.setAttribute('aria-label', `修改 ${title.textContent} 的原因`);
    submit.type = 'submit';
    submit.textContent = '檢查並確認局部修改';
    status.className = 'agent-artifact-revision-status';
    const draftMatchesVersion = Number(draft?.artifact_version || 0) === Number(artifactVersion);
    status.textContent = draftMatchesVersion && draft?.status
      ? draft.status
      : '會先驗證版本與相依節點，再要求你確認。';
    form.addEventListener('submit', async event => {
      event.preventDefault();
      submit.disabled = true;
      status.textContent = '正在檢查版本、相依節點與修改內容…';
      try {
        const result = await window.AgentDockController?.proposeArtifactChange?.(artifact.artifact_id, {
          expected_version: artifactVersion,
          content: editableContent(version, content.value),
          reason: reason.value,
          affected_node_ids: artifact.step_id ? [artifact.step_id] : [],
        });
            status.textContent = result?.cancelled
              ? '已取消；尚未建立新版本。'
              : `已建立 Artifact v${result?.version || artifactVersion + 1}。`;
            if (!result?.cancelled && result?.version) {
              window.AgentDockStore?.set?.({
                artifact_revision_draft: {
                  artifact_id: artifact.artifact_id,
                  artifact_version: Number(result.version),
                  status: status.textContent,
                },
              });
            }
            if (result?.cancelled) submit.disabled = false;
      } catch (error) {
        status.textContent = `修改未套用：${error?.message || '未知錯誤'}`;
        submit.disabled = false;
      }
    });
    label.append(content);
    form.append(label, reason, submit, status);
    card.append(form);
    content.focus();
  }

  function create(artifact, state = {}) {
    const card = document.createElement('article');
    card.className = 'agent-artifact-card';
    card.dataset.artifactId = artifact.artifact_id || '';
    const version = versionRecord(state, artifact);
    const artifactVersion = Number(version?.version || artifact.version || artifact.artifact_version || 1);
    const history = versionRecords(state, artifact);
    const latestVersion = Number(history.at(-1)?.version || artifactVersion);
    card.dataset.artifactVersion = String(artifactVersion);
    const title = document.createElement('strong');
    const meta = document.createElement('small');
    const summary = document.createElement('p');
    const source = document.createElement('small');
    const actions = document.createElement('div');
    title.textContent = artifact.title || artifact.name || 'Artifact';
    meta.textContent = [
      artifact.type || artifact.kind || 'file',
      artifact.mime_type || artifact.media_type || '',
      `目前選取 v${artifactVersion}`,
      artifactVersion < latestVersion ? `最新 v${latestVersion}` : '最新版本',
    ].filter(Boolean).join(' · ');
    summary.textContent = artifact.summary || artifact.metadata?.summary || '';
    const uri = artifact.uri || (
      artifact.run_id && artifact.artifact_id
        ? `/api/agents/runs/${encodeURIComponent(artifact.run_id)}/artifacts/${encodeURIComponent(artifact.artifact_id)}`
        : ''
    );
    source.textContent = [
      artifact.step_id ? `來源 Step：${artifact.step_id}` : '',
      `Artifact：${artifact.artifact_id || '—'}`,
      uri ? `URI：${uri}` : '',
    ].filter(Boolean).join(' · ');
    actions.className = 'agent-artifact-actions';
    const select = document.createElement('button');
    select.type = 'button';
    select.textContent = '選取 Artifact';
    select.addEventListener('click', () => window.AgentArtifactSelection?.select?.(selectionFor(artifact, artifactVersion)));
    actions.append(select);
    if (history.length) {
      const versions = document.createElement('nav');
      versions.className = 'agent-artifact-version-picker';
      versions.setAttribute('aria-label', `${title.textContent} 版本`);
      history.forEach(record => {
        const button = document.createElement('button');
        const number = Number(record.version || 1);
        button.type = 'button';
        button.textContent = `v${number}`;
        button.className = number === artifactVersion ? 'is-current' : '';
        button.setAttribute('aria-pressed', String(number === artifactVersion));
        button.title = [
          record.changed_by || record.author || '',
          record.reason || record.summary || '',
          record.validation_result || '',
        ].filter(Boolean).join(' · ') || `選取 v${number}`;
        button.addEventListener('click', () => window.AgentArtifactSelection?.select?.(
          selectionFor(artifact, number),
        ));
        versions.append(button);
      });
      actions.append(versions);
    }
    if ((artifact.mime_type || artifact.media_type || '').startsWith('text/')) {
      const revise = document.createElement('button');
      revise.type = 'button';
      revise.textContent = '提出局部修改';
      revise.setAttribute('aria-label', `修改 Artifact：${title.textContent}`);
      revise.addEventListener('click', () => {
        const draft = {
          artifact_id: artifact.artifact_id,
          artifact_version: artifactVersion,
        };
        window.AgentArtifactSelection?.select?.(selectionFor(artifact, artifactVersion));
        window.AgentDockStore?.set?.({ artifact_revision_draft: draft });
        // Render immediately for simple embedded consumers; the state above
        // makes the same form reappear after the next full Dock render.
        appendRevisionForm(card, artifact, version, artifactVersion, title, draft);
      });
      actions.append(revise);
    }
    if (uri) {
      const open = document.createElement('a');
      const download = document.createElement('a');
      open.href = uri;
      open.target = '_blank';
      open.rel = 'noopener';
      open.textContent = '開啟 Artifact';
      download.href = `${uri}${uri.includes('?') ? '&' : '?'}download=true`;
      download.download = artifact.name || artifact.artifact_id || 'artifact';
      download.textContent = '下載／匯出';
      actions.append(open, download);
    }
    if (artifact.step_id) {
      const step = document.createElement('button');
      step.type = 'button';
      step.textContent = '查看來源 Step';
      step.addEventListener('click', () => window.AgentDockController.openTask(artifact.step_id));
      actions.append(step);
    }
    const evidence = artifact.evidence_ids || artifact.metadata?.evidence_ids || [];
    if (evidence.length) {
      const details = document.createElement('details');
      const toggle = document.createElement('summary');
      const body = document.createElement('pre');
      toggle.textContent = `查看 Evidence（${evidence.length}）`;
      body.textContent = F().text(evidence);
      details.append(toggle, body);
      card.append(title, meta, summary, source, actions, details);
    } else {
      card.append(title, meta, summary, source, actions);
    }
    const draft = state?.artifact_revision_draft;
    if (String(draft?.artifact_id || '') === String(artifact.artifact_id || '')) {
      appendRevisionForm(card, artifact, version, artifactVersion, title, draft);
    }
    return card;
  }
  function render(container, state) {
    if (!container) return;
    container.replaceChildren();
    const runId = window.AgentPlanView.activeRunId(state);
    const values = Object.values(state.artifacts).filter(item => (
      (!state.active_session_id || item.session_id === state.active_session_id)
      && (!runId || item.run_id === runId)
    ));
    if (!values.length) {
      const empty = document.createElement('p');
      empty.className = 'agent-empty-state';
      empty.textContent = '目前沒有產物；通過 Host 驗證的報告、CSV、圖表與 Evidence 會顯示在這裡。';
      container.append(empty);
      return;
    }
    values.forEach(item => container.append(create(item, state)));
  }
  window.AgentArtifactView = { create, render, selectionFor, versionRecord, versionRecords };
}());
