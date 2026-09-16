'use strict';

const AGENT_CODEX_EFFORT_LABELS = {
  none: '無', minimal: '最低', low: '低', medium: '中', high: '高',
  xhigh: '特高', max: '最大', ultra: '極高',
};

function agentCodexEffortLabel(value) {
  return AGENT_CODEX_EFFORT_LABELS[value] ? `${AGENT_CODEX_EFFORT_LABELS[value]}（${value}）` : value;
}

function agentCodexModelLabel(item) {
  const name = item?.display_name || item?.model || '';
  return name.replace(/^(GPT-\d+(?:\.\d+)?)[ -](.+)$/i, (_, family, suffix) => `${family.toUpperCase()} ${suffix.replaceAll('-', ' ')}`);
}

function agentCodexCatalogModel(model) {
  const items = state.agentCodexModels || [];
  return model
    ? items.find(item => item.model === model || item.id === model)
    : items.find(item => item.model === state.agentCodexDefaults?.model) || items.find(item => item.is_default === true);
}

function agentCodexOption(value, label, unavailable = false) {
  const option = document.createElement('option');
  option.value = value;
  option.textContent = label;
  option.disabled = unavailable;
  return option;
}

function renderAgentCodexModels(selection = {}) {
  const modelPicker = $('agentCodexModel');
  const effortPicker = $('agentCodexReasoningEffort');
  if (!modelPicker || !effortPicker) return;
  const model = selection.model ?? modelPicker.value ?? '';
  const effort = selection.reasoningEffort ?? effortPicker.value ?? '';
  modelPicker.replaceChildren(agentCodexOption('', '沿用 Codex 預設模型'));
  (state.agentCodexModels || []).forEach(item => {
    modelPicker.append(agentCodexOption(item.model, `${agentCodexModelLabel(item)}${item.is_default ? ' · 預設' : ''}`));
  });
  if (model && !(state.agentCodexModels || []).some(item => item.model === model)) {
    modelPicker.append(agentCodexOption(model, `${model}（目前不可用，保留此值）`, true));
  }
  modelPicker.value = model;
  const selected = agentCodexCatalogModel(model);
  const efforts = selected?.supported_reasoning_efforts || [];
  effortPicker.replaceChildren(agentCodexOption('', model ? '沿用模型預設推理強度' : '沿用 Codex 預設推理強度'));
  efforts.forEach(item => effortPicker.append(agentCodexOption(item.reasoning_effort, agentCodexEffortLabel(item.reasoning_effort))));
  if (effort && !efforts.some(item => item.reasoning_effort === effort)) {
    effortPicker.append(agentCodexOption(effort, `${agentCodexEffortLabel(effort)}（目前不可用，保留此值）`, true));
  }
  effortPicker.value = effort;
  const description = $('agentCodexModelDescription');
  if (description) description.textContent = selected?.description || (model ? '模型清單尚未確認此設定；已保留原值，可重新整理或選擇其他模型。' : '留空會使用 Codex 的預設設定。');
  const effortDescription = $('agentCodexEffortDescription');
  if (effortDescription) {
    const selectedEffort = efforts.find(item => item.reasoning_effort === effort);
    effortDescription.textContent = selectedEffort?.description
      || (effort && !selectedEffort ? '目前無法確認此推理強度是否可用，已保留原值。'
        : selected?.default_reasoning_effort ? `此模型建議的預設強度：${agentCodexEffortLabel(selected.default_reasoning_effort)}。` : '可用推理強度會隨模型更新。');
  }
  updateAgentCodexPickerLabels();
}

async function discoverAgentCodexModels() {
  const requestId = (state.agentCodexModelRequestId || 0) + 1;
  state.agentCodexModelRequestId = requestId;
  state.agentCodexModelStatus = 'loading';
  const button = $('refreshAgentCodexModels');
  const status = $('agentCodexModelState');
  if (button) button.disabled = true;
  if (status) status.textContent = '正在讀取此 Codex 帳號的可用模型…';
  $('agentCodexModel')?.setAttribute('aria-busy', 'true');
  try {
    const result = await api(agentControlApi('/api/agents/providers/codex/models'), { headers: agentControlHeaders() });
    if (requestId !== state.agentCodexModelRequestId) return result;
    if (!Array.isArray(result.items)) throw new Error('服務未回傳有效模型清單。');
    state.agentCodexDefaults = result.defaults || {};
    const seen = new Set();
    state.agentCodexModels = result.items.flatMap(item => {
      const model = String(item?.model || item?.id || '').trim();
      if (!model || seen.has(model)) return [];
      seen.add(model);
      const efforts = new Set();
      return [{ ...item, model, supported_reasoning_efforts: (Array.isArray(item.supported_reasoning_efforts) ? item.supported_reasoning_efforts : []).filter(value => {
        if (!value?.reasoning_effort || efforts.has(value.reasoning_effort)) return false;
        efforts.add(value.reasoning_effort);
        return true;
      }) }];
    });
    state.agentCodexModelStatus = 'ready';
    renderAgentCodexModels();
    if (status) status.textContent = state.agentCodexModels.length
      ? `已載入 ${state.agentCodexModels.length} 個模型。變更後請儲存 Agent 設定。`
      : '此帳號目前沒有回傳可用模型；已保留原設定，請稍後重新整理。';
    return result;
  } catch (error) {
    if (requestId !== state.agentCodexModelRequestId) return null;
    state.agentCodexModels = [];
    state.agentCodexModelStatus = 'error';
    renderAgentCodexModels();
    if (status) status.textContent = `Codex 模型清單載入失敗：${error.message || '請稍後再試。'} 已保留目前設定。`;
    return null;
  } finally {
    if (requestId === state.agentCodexModelRequestId) {
      if (button) button.disabled = false;
      $('agentCodexModel')?.setAttribute('aria-busy', 'false');
      renderAgentCodexPopover();
    }
  }
}

function changeAgentCodexModel() {
  state.agentSettingsFormDirty = true;
  const effort = $('agentCodexReasoningEffort')?.value || '';
  const selected = agentCodexCatalogModel($('agentCodexModel')?.value || '');
  const resetEffort = selected && effort && !selected.supported_reasoning_efforts.some(item => item.reasoning_effort === effort);
  const recommended = selected?.default_reasoning_effort || '';
  const replacement = selected?.supported_reasoning_efforts.some(item => item.reasoning_effort === recommended) ? recommended : '';
  renderAgentCodexModels(resetEffort ? { reasoningEffort: replacement } : {});
  if (resetEffort && $('agentCodexModelState')) {
    $('agentCodexModelState').textContent = `所選模型不支援 ${agentCodexEffortLabel(effort)}；推理強度已改為${replacement ? agentCodexEffortLabel(replacement) : '沿用 Codex 預設'}，儲存後套用。`;
  }
}

let agentCodexPopover = null;
let agentCodexPopoverAnchor = null;
let agentCodexPopoverKind = 'model';
let agentCodexPopoverSurface = 'settings';
let agentCodexPickerBound = false;

function agentCodexPickerSelection(surface) {
  return surface === 'composer'
    ? { model: state.agentSettings?.codex?.model || '', effort: state.agentSettings?.codex?.reasoning_effort || '' }
    : { model: $('agentCodexModel')?.value || '', effort: $('agentCodexReasoningEffort')?.value || '' };
}

function updateAgentCodexPickerLabels() {
  ['settings', 'composer'].forEach(surface => {
    const selection = agentCodexPickerSelection(surface);
    const model = agentCodexCatalogModel(selection.model);
    const modelButton = $(surface === 'settings' ? 'agentCodexModelButton' : 'agentComposerModelButton');
    const effortButton = $(surface === 'settings' ? 'agentCodexEffortButton' : 'agentComposerEffortButton');
    if (modelButton) {
      modelButton.textContent = `${selection.model ? agentCodexModelLabel(model) || selection.model : '預設模型'} ⌄`;
      modelButton.disabled = state.agentCodexPickerSaving === true;
    }
    if (effortButton) {
      effortButton.textContent = `${selection.effort ? agentCodexEffortLabel(selection.effort) : selection.model ? '模型預設' : '預設推理'} ⌄`;
      effortButton.disabled = state.agentCodexPickerSaving === true;
    }
  });
  if ($('agentComposerCodexSettings')) $('agentComposerCodexSettings').hidden = state.agentSettings?.default_driver !== 'codex';
}

function closeAgentCodexPopover(restoreFocus = true) {
  if (!agentCodexPopover) return;
  agentCodexPopover.hidden = true;
  agentCodexPopoverAnchor?.setAttribute('aria-expanded', 'false');
  if (restoreFocus) agentCodexPopoverAnchor?.focus();
  agentCodexPopoverAnchor = null;
}

function positionAgentCodexPopover() {
  if (!agentCodexPopoverAnchor || !agentCodexPopover || agentCodexPopover.hidden) return;
  const rect = agentCodexPopoverAnchor.getBoundingClientRect();
  const viewport = window.visualViewport;
  const leftEdge = viewport?.offsetLeft || 0;
  const topEdge = viewport?.offsetTop || 0;
  const width = viewport?.width || window.innerWidth;
  const height = viewport?.height || window.innerHeight;
  // AppKit paints its toolbar above WKWebView, using this same DOM rect.
  // A web z-index cannot cover that native view, so reserve its visible area.
  const nativeToolbar = document.documentElement?.dataset.nativeLiquidGlass === 'appkit'
    ? document.querySelector('.topbar')?.getBoundingClientRect() : null;
  const safeTop = nativeToolbar?.height > 0 ? Math.max(topEdge, nativeToolbar.bottom) : topEdge;
  const safeBottom = topEdge + height;
  const menuWidth = Math.min(340, width - 16);
  const below = Math.max(0, safeBottom - Math.max(rect.bottom, safeTop) - 14);
  const above = Math.max(0, Math.min(rect.top, safeBottom) - safeTop - 14);
  const useBelow = below >= Math.min(420, above);
  const maxHeight = Math.max(0, Math.min(560, safeBottom - safeTop - 16, useBelow ? below : above));
  agentCodexPopover.style.width = `${menuWidth}px`;
  agentCodexPopover.style.maxHeight = `${maxHeight}px`;
  agentCodexPopover.style.left = `${Math.max(leftEdge + 8, Math.min(rect.left, leftEdge + width - menuWidth - 8))}px`;
  const menuHeight = Math.min(agentCodexPopover.getBoundingClientRect().height, maxHeight);
  agentCodexPopover.style.top = `${Math.max(safeTop + 8, Math.min(useBelow ? rect.bottom + 6 : rect.top - menuHeight - 6, safeBottom - menuHeight - 8))}px`;
}

function renderAgentCodexPopover() {
  if (!agentCodexPopoverAnchor || !agentCodexPopover || agentCodexPopover.hidden) return;
  const restoreFocus = agentCodexPopover.contains(document.activeElement);
  const selection = agentCodexPickerSelection(agentCodexPopoverSurface);
  const isModel = agentCodexPopoverKind === 'model';
  const selected = agentCodexCatalogModel(selection.model);
  const current = isModel ? selection.model : selection.effort;
  const heading = document.createElement('div');
  heading.className = 'agent-codex-picker-heading';
  heading.textContent = isModel ? '選取模型' : '選取推理強度';
  const list = document.createElement('div');
  list.setAttribute('role', 'listbox');
  list.setAttribute('aria-label', heading.textContent);
  const choices = [{ value: '', label: '預設', description: isModel ? '推薦的模型組合' : selection.model ? '沿用所選模型的預設推理強度' : '沿用 Codex 預設設定' }];
  if (isModel) (state.agentCodexModels || []).forEach(item => choices.push({ value: item.model, label: agentCodexModelLabel(item) }));
  else (selected?.supported_reasoning_efforts || []).forEach(item => choices.push({ value: item.reasoning_effort, label: agentCodexEffortLabel(item.reasoning_effort), description: item.description }));
  if (current && !choices.some(choice => choice.value === current)) choices.push({ value: current, label: current, description: '目前不可用，保留此值', disabled: true });
  choices.forEach(choice => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'agent-codex-picker-option';
    button.setAttribute('role', 'option');
    button.setAttribute('aria-selected', String(choice.value === current));
    button.disabled = Boolean(choice.disabled);
    button.tabIndex = choice.value === current ? 0 : -1;
    const text = document.createElement('span');
    text.textContent = choice.label;
    button.append(text);
    const check = document.createElement('span');
    check.className = 'agent-codex-picker-check';
    check.setAttribute('aria-hidden', 'true');
    check.textContent = choice.value === current ? '✓' : '';
    button.append(check);
    if (choice.description) {
      const small = document.createElement('small');
      small.textContent = choice.description;
      button.append(small);
    }
    button.addEventListener('click', () => selectAgentCodexPickerValue(choice.value));
    list.append(button);
  });
  const footer = document.createElement('div');
  footer.className = 'agent-codex-picker-footer';
  const status = document.createElement('span');
  status.setAttribute('role', 'status');
  status.textContent = state.agentCodexModelStatus === 'loading' ? '正在載入模型…'
    : state.agentCodexModelStatus === 'error' ? '清單載入失敗，已保留設定。'
      : '只套用新任務';
  const refresh = document.createElement('button');
  refresh.type = 'button';
  refresh.textContent = '重新整理';
  refresh.disabled = state.agentCodexModelStatus === 'loading';
  refresh.addEventListener('click', () => { void discoverAgentCodexModels(); renderAgentCodexPopover(); });
  footer.append(status, refresh);
  agentCodexPopover.replaceChildren(heading, list, footer);
  positionAgentCodexPopover();
  if (restoreFocus) (agentCodexPopover.querySelector('[aria-selected="true"]:not(:disabled)') || agentCodexPopover.querySelector('[role="option"]:not(:disabled)'))?.focus();
}

async function selectAgentCodexPickerValue(value) {
  const surface = agentCodexPopoverSurface;
  const kind = agentCodexPopoverKind;
  const selection = agentCodexPickerSelection(surface);
  closeAgentCodexPopover();
  if (surface === 'settings') {
    if (kind === 'model') { $('agentCodexModel').value = value; changeAgentCodexModel(); }
    else { $('agentCodexReasoningEffort').value = value; state.agentSettingsFormDirty = true; renderAgentCodexModels(); }
    return;
  }
  if (kind === 'model') {
    selection.model = value;
    const selected = agentCodexCatalogModel(value);
    const effectiveEffort = selection.effort || state.agentCodexDefaults?.reasoning_effort;
    if (selected && effectiveEffort && !selected.supported_reasoning_efforts.some(item => item.reasoning_effort === effectiveEffort)) {
      selection.effort = selected.default_reasoning_effort || '';
    }
  } else selection.effort = value;
  state.agentCodexPickerSaving = true;
  let settleSave;
  let saveOutcome = { saved: false, error: 'Codex 模型設定尚未儲存。' };
  state.agentCodexSettingsSavePromise = new Promise(resolve => { settleSave = resolve; });
  updateAgentCodexPickerLabels();
  const status = $('agentComposerModelStatus');
  if (status) status.textContent = '正在儲存模型設定…';
  try {
    const settings = await api('/api/agents/settings', {
      method: 'POST', headers: agentControlHeaders(),
      body: JSON.stringify({ default_driver: 'codex', codex_model: selection.model, codex_reasoning_effort: selection.effort }),
    });
    markAgentSettingsSaved();
    state.agentSettings = settings;
    applyAgentSettingsIdentity(settings);
    renderAgentCodexModels({ model: settings.codex?.model || '', reasoningEffort: settings.codex?.reasoning_effort || '' });
    if (status) status.textContent = '已儲存，套用於新任務。';
    saveOutcome = { saved: true };
  } catch (error) {
    saveOutcome = { saved: false, error: error.message || 'Codex 模型設定儲存失敗。' };
    if (status) status.textContent = `儲存失敗：${error.message || '請稍後再試。'}`;
  } finally {
    state.agentCodexPickerSaving = false;
    settleSave(saveOutcome);
    state.agentCodexSettingsSavePromise = null;
    updateAgentCodexPickerLabels();
  }
}

async function waitForAgentCodexSettingsSave() {
  const pending = state.agentCodexSettingsSavePromise;
  if (!pending) return;
  const outcome = await pending;
  if (!outcome.saved) throw new Error(`模型設定尚未儲存，任務未送出：${outcome.error}`);
}

function openAgentCodexPopover(anchor, kind, surface) {
  if (agentCodexPopoverAnchor === anchor && !agentCodexPopover.hidden) { closeAgentCodexPopover(); return; }
  closeAgentCodexPopover(false);
  if (!agentCodexPopover) {
    agentCodexPopover = document.createElement('div');
    agentCodexPopover.id = 'agentCodexPickerPopover';
    agentCodexPopover.className = 'agent-codex-picker-popover';
    agentCodexPopover.addEventListener('keydown', event => {
      const choices = [...agentCodexPopover.querySelectorAll('[role="option"]:not(:disabled)')];
      const index = choices.indexOf(document.activeElement);
      let next;
      if (event.key === 'ArrowDown') next = choices[(index + 1) % choices.length];
      if (event.key === 'ArrowUp') next = choices[(index - 1 + choices.length) % choices.length];
      if (event.key === 'Home') next = choices[0];
      if (event.key === 'End') next = choices.at(-1);
      if (next) { event.preventDefault(); next.focus(); }
    });
    document.body.append(agentCodexPopover);
  }
  agentCodexPopoverAnchor = anchor;
  agentCodexPopoverKind = kind;
  agentCodexPopoverSurface = surface;
  agentCodexPopover.hidden = false;
  anchor.setAttribute('aria-expanded', 'true');
  if (!state.agentCodexModelStatus) void discoverAgentCodexModels();
  renderAgentCodexPopover();
  (agentCodexPopover.querySelector('[aria-selected="true"]:not(:disabled)') || agentCodexPopover.querySelector('[role="option"]:not(:disabled)'))?.focus();
}

function bindAgentCodexPickers() {
  if (agentCodexPickerBound) return;
  agentCodexPickerBound = true;
  [['agentCodexModelButton', 'model', 'settings'], ['agentCodexEffortButton', 'effort', 'settings'], ['agentComposerModelButton', 'model', 'composer'], ['agentComposerEffortButton', 'effort', 'composer']].forEach(([id, kind, surface]) => {
    const button = $(id);
    button?.addEventListener('click', () => openAgentCodexPopover(button, kind, surface));
    button?.addEventListener('keydown', event => {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); openAgentCodexPopover(button, kind, surface); }
    });
  });
  document.addEventListener('pointerdown', event => {
    if (agentCodexPopoverAnchor && !agentCodexPopover.contains(event.target) && !agentCodexPopoverAnchor.contains(event.target)) closeAgentCodexPopover(false);
  });
  document.addEventListener('keydown', event => {
    if (!agentCodexPopoverAnchor) return;
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closeAgentCodexPopover(); }
    if (event.key === 'Tab') closeAgentCodexPopover(false);
  }, true);
  window.addEventListener('resize', positionAgentCodexPopover);
  window.visualViewport?.addEventListener('resize', positionAgentCodexPopover);
  document.addEventListener('scroll', positionAgentCodexPopover, true);
}
