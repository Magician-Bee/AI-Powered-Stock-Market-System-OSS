(function () {
  'use strict';

  function activeInteractions(state) {
    // A terminal Run deliberately clears active_run_id, but the Dock still
    // renders its newest Run as the foreground result.  Without using that
    // same foreground Run here, an unresolved checkpoint left by an older
    // Run in the same session is rendered above a newer completed answer.
    // That looks like the completed Agent has started asking the same
    // question again.  Keep historical interactions durable, but do not
    // present them as actions for the foreground Run.
    const visibleRunId = state.active_run_id
      || window.AgentPlanView?.activeRunId?.(state)
      || null;
    const candidates = Object.values(state.interactions || {})
      .filter(item => !state.active_session_id || item.session_id === state.active_session_id)
      .filter(item => !['answered', 'resolved', 'cancelled', 'expired'].includes(String(item.status || '')))
      .filter(item => !visibleRunId || item.run_id === visibleRunId)
      .sort((left, right) => String(
        left.updated_at || left.created_at || left.event_timestamp || '',
      ).localeCompare(String(
        right.updated_at || right.created_at || right.event_timestamp || '',
      )));

    // P83 permits a Decision Checkpoint to contain several related questions,
    // but it must still be one current checkpoint. A recovery can leave old,
    // unresolved cards in the durable history; rendering all of them stacks
    // conflicting buttons above the same Run. Keep the newest pending card
    // for each visible Run. Older cards remain durable/auditable and are not
    // discarded from state.
    const newestByRun = new Map();
    candidates.forEach(interaction => {
      newestByRun.set(String(interaction.run_id || interaction.interaction_id), interaction);
    });
    return [...newestByRun.values()];
  }

  function artifactAnchor(interaction) {
    return {
      artifact_id: interaction.artifact_id || `decision:${interaction.interaction_id}`,
      artifact_version: Number(interaction.artifact_version || interaction.version || 1) || 1,
    };
  }

  function values(value) {
    if (Array.isArray(value)) return value.filter(Boolean).map(String);
    return value ? [String(value)] : [];
  }

  async function respondAndClose(card, interaction, answer) {
    const responder = window.AgentDockController?.respondInteraction;
    if (typeof responder !== 'function') {
      throw new Error('Agent interaction controller is unavailable');
    }
    await responder(interaction.interaction_id, answer);

    // The durable response is authoritative, but a native WebKit repaint can
    // race the Dock store update.  Replace the stale actionable controls as
    // soon as the response succeeds so a completed Run never looks like it is
    // still waiting for the same external decision.
    const notice = document.createElement('p');
    notice.className = 'agent-decision-resolved';
    const selected = String(answer.option_id || '').toLowerCase();
    notice.textContent = selected === 'skip'
      ? '已略過此建議；目前結果保持完成，沒有建立新的 Run。'
      : '已收到你的回覆，正在更新 Agent 結果。';
    card.replaceChildren(notice);
  }

  function create(interaction, state) {
    const card = document.createElement('article');
    card.className = 'agent-decision-card';
    card.dataset.interactionId = interaction.interaction_id;
    const eyebrow = document.createElement('small');
    eyebrow.textContent = interaction.interaction_purpose === 'post_answer_proposal'
      ? 'Agent 建議的下一步（目前回答已完成）'
      : interaction.interaction_purpose === 'automation_confirmation'
        ? 'Automation 提案（確認前不執行）'
        : interaction.interaction_purpose === 'proposal_evaluation'
          ? 'Agent 已依證據評估你的提案'
          : interaction.kind === 'proposal'
            ? '方案確認'
      : interaction.tentative_judgment
        ? 'Agent 已完成初步判斷，需你的決定'
        : '需要你的決定';
    const title = document.createElement('strong');
    title.textContent = interaction.title || interaction.question || interaction.prompt || '請選擇下一步';
    const description = document.createElement('p');
    description.textContent = interaction.description || interaction.agent_recommendation || interaction.agent_view || '';
    description.hidden = !description.textContent;
    const questionBudget = document.createElement('small');
    const questions = interaction.questions || [interaction.question || interaction.prompt].filter(Boolean);
    questionBudget.className = 'agent-decision-question-budget';
    questionBudget.textContent = `Reflection Checkpoint · ${Math.min(3, Math.max(1, questions.length))} 題`;
    const reflection = document.createElement('section');
    reflection.className = 'agent-decision-reflection';
    reflection.setAttribute('aria-label', '初步判斷與未確定事項');
    const reflectionTitle = document.createElement('strong');
    const tentative = document.createElement('p');
    const evidence = document.createElement('p');
    const details = document.createElement('p');
    reflectionTitle.textContent = 'Agent 暫定建議';
    tentative.textContent = interaction.tentative_judgment
      || interaction.agent_view
      || interaction.agent_recommendation
      || 'Agent 已先形成暫定方向，請比較下列方案。';
    const mainEvidence = values(
      interaction.main_evidence || interaction.primary_evidence || interaction.evidence_summary,
    );
    const unknowns = values(interaction.unknowns || interaction.unknown_information);
    const risks = values(interaction.important_risks || interaction.risks);
    evidence.textContent = mainEvidence.length ? `主要依據：${mainEvidence.join('、')}` : '';
    details.textContent = [
      unknowns.length ? `仍未知：${unknowns.join('、')}` : '',
      risks.length ? `重要風險：${risks.join('、')}` : '',
    ].filter(Boolean).join(' · ');
    evidence.hidden = !evidence.textContent;
    details.hidden = !details.textContent;
    reflection.append(reflectionTitle, tentative, evidence, details);
    const options = document.createElement('div');
    options.className = 'agent-decision-options';
    const preferredOption = String(interaction.preferred_option || interaction.agent_preferred_option || '');
    (interaction.options || []).forEach((option, index) => {
      const normalized = typeof option === 'string' ? { label: option, value: option } : option;
      const button = document.createElement('button');
      button.type = 'button';
      button.dataset.optionId = normalized.option_id || normalized.id || String(index);
      const isPreferred = normalized.preferred === true
        || normalized.recommended === true
        || button.dataset.optionId === preferredOption;
      button.className = isPreferred ? 'primary is-preferred' : '';
      const optionLabel = normalized.label || normalized.title || normalized.value || `選項 ${index + 1}`;
      const missingLegacyRecoveryModel = interaction.interaction_purpose === 'recovery_escalation'
        && button.dataset.optionId === 'continue_alternative_recovery'
        && !normalized.recovery_driver_id;
      const label = document.createElement('strong');
      const badge = document.createElement('span');
      const reason = document.createElement('small');
      label.textContent = missingLegacyRecoveryModel
        ? '尚未設定替代模型（請開啟模型設定）'
        : isPreferred ? `${optionLabel} ` : optionLabel;
      badge.textContent = isPreferred ? '· Agent 建議' : `替代方案 ${index + 1}`;
      reason.textContent = missingLegacyRecoveryModel
        ? '請先設定另一個 provider，避免以相同模型重試。'
        : normalized.reason || normalized.tradeoff || '可選方案；送出前仍可比較其他選項。';
      button.append(label, badge, reason);
      if (normalized.reason || missingLegacyRecoveryModel) {
        button.title = missingLegacyRecoveryModel
          ? '這是舊版 L8 互動，未保存可切換模型。請先設定另一個 provider，避免以相同模型重試。'
          : normalized.reason;
      }
      button.disabled = missingLegacyRecoveryModel;
      button.addEventListener('click', async () => {
        window.AgentArtifactSelection?.select?.({
          ...artifactAnchor(interaction),
          target_type: 'decision_option',
          branch_id: interaction.branch_id,
          node_id: button.dataset.optionId,
          path: `${title.textContent} ＞ ${optionLabel}`,
        });
        try {
          await respondAndClose(card, interaction, {
            option_id: button.dataset.optionId,
            value: normalized.value ?? normalized.label,
            artifact_version: artifactAnchor(interaction).artifact_version,
          });
        } catch (error) {
          button.setAttribute('aria-invalid', 'true');
          button.title = error?.message || '無法送出回覆，請再試一次。';
        }
      });
      options.append(button);
    });
    const form = document.createElement('form');
    form.className = 'agent-decision-free-text';
    const input = document.createElement('input');
    input.type = 'text';
    input.placeholder = interaction.free_text_placeholder || '或直接輸入你的偏好';
    input.setAttribute('aria-label', `回答：${title.textContent}`);
    const send = document.createElement('button');
    send.type = 'submit';
    send.textContent = '回答';
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const value = input.value.trim();
      if (!value) return;
      window.AgentArtifactSelection?.select?.({
        ...artifactAnchor(interaction),
        target_type: 'decision_free_text',
        branch_id: interaction.branch_id,
        node_id: 'free_text',
        path: `${title.textContent} ＞ 自由文字回答`,
      });
      try {
        await respondAndClose(card, interaction, {
          free_text: value,
          artifact_version: artifactAnchor(interaction).artifact_version,
        });
      } catch (error) {
        input.setAttribute('aria-invalid', 'true');
        input.title = error?.message || '無法送出回覆，請再試一次。';
      }
    });
    form.append(input, send);
    if (interaction.allow_free_text === false) form.hidden = true;
    card.append(eyebrow, questionBudget, title, description, reflection, options, form);
    return card;
  }

  function render(container, state) {
    if (!container) return;
    container.replaceChildren();
    const interactions = activeInteractions(state);
    container.hidden = interactions.length === 0;
    interactions.forEach(item => container.append(create(item, state)));
  }

  window.AgentDecisionCard = { activeInteractions, artifactAnchor, create, render };
}());
