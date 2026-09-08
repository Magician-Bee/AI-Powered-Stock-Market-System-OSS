'use strict';

function agentControlApi(path) {
  if (window.location.hostname !== '127.0.0.1') return path;
  const url = new URL(path, window.location.origin);
  // Keep model discovery out of the busy dashboard stream connection pool.
  url.hostname = 'localhost';
  return url.toString();
}

function agentControlHeaders() {
  const token = document.querySelector('meta[name="stock-ai-runtime-session"]')?.content || '';
  return {
    'Content-Type': 'application/json',
    ...(token ? { 'X-Stock-AI-Session': token } : {}),
  };
}

async function discoverAgentModels() {
  const baseUrl = $('agentOpenAIBaseUrl')?.value?.trim() || '';
  if (!baseUrl) throw new Error('請先輸入 OpenAI Compatible 或 Ollama 位址。');
  const result = await api(agentControlApi('/api/agents/providers/openai-compatible/models'), {
    method: 'POST',
    headers: agentControlHeaders(),
    body: JSON.stringify({ base_url: baseUrl }),
  });
  const models = (Array.isArray(result.items)
    ? result.items
    : (Array.isArray(result.models) ? result.models : []))
    .map((model) => model?.id || model?.name || String(model || ''))
    .map((model) => model.trim())
    .filter(Boolean)
    .filter((model, index, all) => all.indexOf(model) === index);
  const picker = $('agentDiscoveredModels');
  const pickerField = $('agentDiscoveredModelsField');
  if (picker) {
    picker.replaceChildren();
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = models.length ? '選擇要使用的模型' : '此服務沒有回傳模型';
    placeholder.disabled = true;
    placeholder.selected = true;
    picker.append(placeholder);
    models.forEach((model) => {
      const option = document.createElement('option');
      option.value = option.textContent = model;
      picker.append(option);
    });
    const currentModel = $('agentOpenAIModel')?.value?.trim() || '';
    picker.disabled = models.length === 0;
    picker.size = Math.min(Math.max(models.length, 3), 6);
    if (currentModel && models.includes(currentModel)) picker.value = currentModel;
  }
  if (pickerField) pickerField.hidden = false;
  if ($('agentModelDiscoveryState')) {
    $('agentModelDiscoveryState').textContent = models.length
      ? `已找到 ${models.length} 個模型；從下方清單選擇後會帶入模型名稱。`
      : '服務可連線，但沒有回傳可用模型。';
  }
  return result;
}

function selectDiscoveredAgentModel() {
  const selected = $('agentDiscoveredModels')?.value || '';
  if (!selected) return;
  if ($('agentOpenAIModel')) $('agentOpenAIModel').value = selected;
  if ($('agentModelDiscoveryState')) $('agentModelDiscoveryState').textContent = `已選擇 ${selected}。`;
}
