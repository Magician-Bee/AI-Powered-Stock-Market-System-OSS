(function () {
  'use strict';
  const F = () => window.AgentDockFormatters;

  function inlineText(value) {
    return String(value || '')
      .replace(/\*\*/g, '')
      .replace(/`/g, '')
      .trim();
  }

  function tableCells(line) {
    return String(line || '')
      .trim()
      .replace(/^\|/, '')
      .replace(/\|$/, '')
      .split('|')
      .map(inlineText);
  }

  function isTableDivider(line) {
    const cells = tableCells(line);
    return cells.length > 1 && cells.every(cell => /^:?-{3,}:?$/.test(cell.replace(/\s/g, '')));
  }

  function assistantAnswer(content) {
    const root = document.createElement('div');
    root.className = 'agent-answer-content';
    const lines = F().text(content).replace(/\r/g, '').split('\n');
    let index = 0;

    while (index < lines.length) {
      const line = lines[index].trim();
      if (!line) {
        index += 1;
        continue;
      }

      const heading = line.match(/^#{1,4}\s+(.+)$/);
      if (heading) {
        const title = document.createElement('h3');
        title.textContent = inlineText(heading[1]);
        root.append(title);
        index += 1;
        continue;
      }

      if (line.includes('|') && isTableDivider(lines[index + 1] || '')) {
        const wrap = document.createElement('div');
        const table = document.createElement('table');
        const head = document.createElement('thead');
        const headRow = document.createElement('tr');
        tableCells(line).forEach(cell => {
          const column = document.createElement('th');
          column.textContent = cell;
          headRow.append(column);
        });
        head.append(headRow);
        table.append(head);
        index += 2;
        const body = document.createElement('tbody');
        while (index < lines.length && lines[index].trim().includes('|')) {
          const row = document.createElement('tr');
          tableCells(lines[index]).forEach(cell => {
            const column = document.createElement('td');
            column.textContent = cell;
            row.append(column);
          });
          body.append(row);
          index += 1;
        }
        table.append(body);
        wrap.className = 'agent-answer-table-wrap';
        wrap.append(table);
        root.append(wrap);
        continue;
      }

      const listItem = line.match(/^(\d+)[.)]\s+(.+)$/) || line.match(/^[-*]\s+(.+)$/);
      if (listItem) {
        const ordered = Boolean(line.match(/^\d+[.)]\s+/));
        const list = document.createElement(ordered ? 'ol' : 'ul');
        while (index < lines.length) {
          const current = lines[index].trim();
          const match = ordered
            ? current.match(/^\d+[.)]\s+(.+)$/)
            : current.match(/^[-*]\s+(.+)$/);
          if (!match) break;
          const item = document.createElement('li');
          item.textContent = inlineText(match[1]);
          list.append(item);
          index += 1;
        }
        root.append(list);
        continue;
      }

      const paragraphLines = [line];
      index += 1;
      while (index < lines.length) {
        const next = lines[index].trim();
        if (
          !next
          || /^#{1,4}\s+/.test(next)
          || /^\d+[.)]\s+/.test(next)
          || /^[-*]\s+/.test(next)
          || (next.includes('|') && isTableDivider(lines[index + 1] || ''))
        ) break;
        paragraphLines.push(next);
        index += 1;
      }
      const paragraph = document.createElement('p');
      paragraph.textContent = inlineText(paragraphLines.join(' '));
      root.append(paragraph);
    }
    return root;
  }

  function userVisibleText(content) {
    return F().text(content)
      .replace(/^(?:\s*\[(?:MODEL_TASK_KIND:[a-z_]+|MODEL_OUTPUT_SCHEMA:[a-z_]+)\]\s*)+/i, '')
      .trim();
  }

  function bubble(role, content, timestamp) {
    const item = document.createElement('article');
    item.className = `agent-message agent-message-${role}`;
    const label = document.createElement('strong');
    const text = role === 'assistant'
      ? assistantAnswer(content)
      : document.createElement('p');
    const time = document.createElement('time');
    label.textContent = role === 'user' ? '使用者' : role === 'assistant' ? 'Agent' : 'Runtime';
    if (role !== 'assistant') text.textContent = role === 'user'
      ? userVisibleText(content)
      : F().text(content);
    time.textContent = F().time(timestamp);
    item.append(label, text, time);
    return item;
  }

  function previewText(value) {
    const text = F().text(value);
    return text.length > 84 ? `${text.slice(0, 84)}…` : text;
  }

  function runtimeCard(title, summary, tone = '', fullContent = summary) {
    const card = document.createElement('article');
    card.className = `agent-runtime-card agent-operation-card ${tone}`;
    const head = document.createElement('header');
    const heading = document.createElement('strong');
    const preview = document.createElement('span');
    const disclosure = document.createElement('details');
    const toggle = document.createElement('summary');
    const text = document.createElement('p');
    heading.textContent = title;
    preview.className = 'agent-operation-preview';
    preview.textContent = previewText(summary);
    toggle.textContent = '顯示完整資訊';
    text.textContent = F().text(fullContent);
    head.append(heading, preview);
    disclosure.className = 'agent-operation-disclosure';
    disclosure.append(toggle, text);
    card.append(head, disclosure);
    return card;
  }

  function feedEntries(state) {
    const entries = [];
    const keyed = new Map();
    state.ordered_message_ids.forEach(messageId => {
      const message = state.messages[messageId];
      if (!message) return;
      if (
        state.active_session_id
        && message.session_id
        && message.session_id !== state.active_session_id
      ) return;
      const key = message.role === 'assistant' && message.run_id
        ? `assistant-run:${message.run_id}`
        : `message:${messageId}`;
      if (!keyed.has(key)) {
        keyed.set(key, entries.length);
        entries.push({ kind: 'stored-message', id: messageId, key, message });
      } else {
        entries[keyed.get(key)] = { kind: 'stored-message', id: messageId, key, message };
      }
    });
    state.ordered_event_ids.forEach(eventId => {
      const event = state.events[eventId];
      if (!event || event.visibility === 'debug') return;
      if (
        state.active_session_id
        && event.session_id
        && event.session_id !== state.active_session_id
      ) return;
      const payload = event.payload || event;
      const type = String(event.type);
      const eventRunId = String(event.run_id || 'no-run');
      if (type.startsWith('tool.')) {
        const rawId = event.tool_call_id || payload.tool_call_id || payload.call_id;
        if (!rawId) return;
        const id = window.AgentEventReducer.scopedKey(eventRunId, rawId);
        const key = `tool:${id}`;
        if (!keyed.has(key)) {
          keyed.set(key, entries.length);
          entries.push({ kind: 'tool', id, key, event });
        } else entries[keyed.get(key)].event = event;
        return;
      }
      if (type.startsWith('skill.') || type === 'package.loaded' || type === 'mcp.tool.selected') {
        const rawId = payload.skill_id || payload.package_id || payload.mcp_server || eventId;
        const stepId = String(event.step_id || payload.step_id || payload.node_id || 'no-step');
        const toolCallId = String(event.tool_call_id || payload.tool_call_id || payload.call_id || 'no-call');
        const id = [eventRunId, stepId, toolCallId, type, rawId].join(':');
        const key = `skill:${id}`;
        if (!keyed.has(key)) {
          keyed.set(key, entries.length);
          entries.push({ kind: 'skill', id, key, event });
        } else entries[keyed.get(key)].event = event;
        return;
      }
      if (type.startsWith('approval.')) {
        const rawId = event.approval_id || payload.approval?.approval_id || payload.approval_id;
        const id = window.AgentEventReducer.scopedKey(eventRunId, rawId);
        const key = `approval:${id}`;
        if (!keyed.has(key)) {
          keyed.set(key, entries.length);
          entries.push({ kind: 'approval', id, key, event });
        } else entries[keyed.get(key)].event = event;
        return;
      }
      if (type === 'message.created' || type.startsWith('assistant.message.')) return;
      if ([
        'reasoning.summary',
        'reasoning.next_step', 'evidence.gap', 'replan.reason', 'plan.proposed',
        'plan.revised', 'plan.step.progress', 'plan.step.reported',
        'validation.passed', 'validation.failed',
        'artifact.created', 'run.failed', 'run.cancelled', 'model.session.configured',
      ].includes(type)) entries.push({ kind: type, key: `event:${eventId}`, event });
    });
    entries.forEach((entry, index) => { entry.insertionOrder = index; });
    entries.sort((left, right) => {
      const leftTime = Date.parse(left.message?.created_at || left.event?.timestamp || '') || 0;
      const rightTime = Date.parse(right.message?.created_at || right.event?.timestamp || '') || 0;
      if (leftTime !== rightTime) return leftTime - rightTime;
      const leftSequence = Number(left.event?.sequence || 0);
      const rightSequence = Number(right.event?.sequence || 0);
      return leftSequence - rightSequence || left.insertionOrder - right.insertionOrder;
    });
    return entries;
  }

  function entryNode(entry, state) {
    const event = entry.event || {};
    const payload = event.payload || event;
    if (entry.kind === 'stored-message') {
      const message = entry.message;
      return bubble(
        message.role || 'assistant',
        message.content?.objective || message.content?.summary || message.content?.text || message.content,
        message.created_at,
      );
    }
    if (entry.kind === 'tool') {
      const tool = state.tool_calls[entry.id] || { ...payload, tool_call_id: entry.id };
      return window.AgentToolView.create(tool);
    }
    if (entry.kind === 'skill') {
      return window.AgentSkillView.create(state.skills[entry.id] || payload);
    }
    if (entry.kind === 'approval') {
      const approval = state.approvals[entry.id] || payload.approval || payload;
      return window.AgentApprovalView.create(approval);
    }
    if (entry.kind === 'message.created') {
      const message = payload.message || payload;
      return bubble(
        message.role || 'user',
        message.content?.objective || message.content?.summary || message.content?.text || message.content || payload.summary,
        event.timestamp,
      );
    }
    if (entry.kind === 'assistant.message.completed') {
      return bubble('assistant', payload.content || payload.summary, event.timestamp);
    }
    if (entry.kind === 'reasoning.summary') {
      return runtimeCard('規劃摘要', payload.reason_summary || payload.summary);
    }
    if (entry.kind === 'model.session.configured') {
      return runtimeCard('本次模型', `模型：${payload.model || 'SDK 未回報'} · 推理：${payload.reasoning_effort || 'SDK 未回報'}`);
    }
    if (entry.kind === 'reasoning.next_step') {
      return runtimeCard('下一步理由', payload.summary);
    }
    if (entry.kind === 'evidence.gap') {
      return runtimeCard('證據缺口', payload.summary, 'is-warning');
    }
    if (entry.kind === 'replan.reason') {
      return runtimeCard('重新規劃', payload.summary);
    }
    if (entry.kind === 'plan.step.progress') {
      return runtimeCard(
        `執行中：${payload.title || payload.node_id || '計畫步驟'}`,
        payload.description || '正在取得並驗證這一步需要的證據。',
        'is-running',
        {
          step: payload.title || payload.node_id,
          expected_result: payload.description || null,
          expected_tool_call_ids: payload.expected_tool_call_ids || [],
        },
      );
    }
    if (entry.kind === 'plan.step.reported') {
      const gaps = payload.remaining_gaps || [];
      return runtimeCard(
        `步驟完成：${payload.title || payload.node_id || '計畫步驟'}`,
        payload.result_summary || '本步驟已完成。',
        gaps.length ? 'is-warning' : 'is-success',
        {
          result: payload.result_summary || '本步驟已完成。',
          remaining_gaps: gaps,
          next_step: payload.next_step || '整理並回覆最終結論',
          host_results: payload.host_results || [],
        },
      );
    }
    if (entry.kind.startsWith('plan.')) {
      const publicPlanSummary = userVisibleText(
        payload.reason_summary || payload.summary || payload.plan?.objective,
      );
      return runtimeCard(
        entry.kind === 'plan.revised' ? '執行計畫已更新' : '執行計畫',
        publicPlanSummary,
      );
    }
    if (entry.kind.startsWith('validation.')) {
      return runtimeCard(
        entry.kind.endsWith('passed') ? '✓ 驗證通過' : '驗證失敗',
        payload.validation || payload.summary,
        entry.kind.endsWith('failed') ? 'is-error' : 'is-success',
      );
    }
    if (entry.kind === 'artifact.created') {
      return window.AgentArtifactView.create(payload.artifact || payload);
    }
    return runtimeCard(
      entry.kind === 'run.cancelled' ? '任務已取消' : '任務失敗',
      payload.error || payload.summary,
      'is-error',
    );
  }

  function signature(entry, state) {
    if (entry.kind === 'tool') return JSON.stringify(state.tool_calls[entry.id] || entry.event);
    if (entry.kind === 'skill') return JSON.stringify(state.skills[entry.id] || entry.event);
    if (entry.kind === 'approval') return JSON.stringify(state.approvals[entry.id] || entry.event);
    if (entry.kind === 'stored-message') return JSON.stringify(entry.message);
    return JSON.stringify(entry.event);
  }

  function loadEarlierButton(container, hiddenCount) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'agent-feed-load-earlier';
    button.textContent = `載入較早紀錄（尚有 ${hiddenCount} 筆）`;
    button.addEventListener('click', () => {
      const priorHeight = container.scrollHeight;
      const priorTop = container.scrollTop;
      container.dataset.loadingEarlier = 'true';
      container.dataset.windowSize = String(Number(container.dataset.windowSize || 120) + 120);
      render(container, window.AgentDockStore.getState());
      container.scrollTop = priorTop + (container.scrollHeight - priorHeight);
      delete container.dataset.loadingEarlier;
    });
    return button;
  }

  function idleContext() {
    const context = window.WorkspaceContextStore?.get?.() || {};
    const workspace = context.route?.workspace || 'home';
    const tab = context.route?.tab || 'overview';
    const workspaceLabel = (window.WORKSPACES?.[workspace]?.title || workspace || '首頁').replace(/^AI 全市場決策中心$/, '首頁');
    const tabLabel = (window.WORKSPACES?.[workspace]?.tabs || []).find(([key]) => key === tab)?.[1];
    const symbol = String(context.selection?.symbol || '').trim().toUpperCase();
    return { workspace, workspaceLabel, tabLabel, symbol };
  }

  function idleActionButton(label, objective, symbol) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'agent-idle-action';
    button.textContent = label;
    button.dataset.agentIdleAction = label;
    button.addEventListener('click', () => {
      const controller = window.AgentDockController;
      if (!controller?.submit) return;
      button.disabled = true;
      controller.submit({ objective, source: 'idle-action', symbols: symbol ? [symbol] : [] })
        .catch(error => controller.showError?.(error))
        .finally(() => { button.disabled = false; });
    });
    return button;
  }

  function idleActions(context) {
    if (context.symbol && !String(context.symbol).startsWith('^')) return [
      ['分析目前股票', `請分析${context.symbol}，整理目前趨勢、關鍵風險、觸發條件與下一步。`, context.symbol],
      ['標記支撐壓力', `請判讀${context.symbol}的支撐與壓力，提出可標記的關鍵價格與依據。`, context.symbol],
      ['比較同產業', `請比較${context.symbol}與同產業可比較標的，說明相對強弱、風險與資料限制。`, context.symbol],
      ['建立價格提醒', `請為${context.symbol}提出價格提醒條件，列出觸發價、失效條件與檢查頻率。`, context.symbol],
      ['建立模擬交易', `請為${context.symbol}建立一份僅供預覽的模擬交易提案，包含部位、風險限制與不執行原因。`, context.symbol],
    ];
    const general = {
      home: [
        ['整理市場機會', '請從目前全市場快照整理值得進一步研究的候選、正反證據與資料限制。'],
        ['檢查市場風險', '請檢查目前市場寬度、異常事件、資料品質與可能的系統性風險。'],
      ],
      market: [
        ['整理市場強弱', '請依目前市場頁的資料整理強弱族群、量價異常、法人動向與資料限制。'],
        ['檢查監控條件', '請檢查目前市場監控與提醒條件，列出缺口與下一步。'],
      ],
      instrument: [
        ['說明選股需求', '目前尚未明確選取股票；請說明完成個股分析前需要選擇哪些資料與標的。'],
      ],
      portfolio: [
        ['檢查持倉風險', '請依目前投資組合頁整理持倉曝險、集中度、損益與待補資料。'],
        ['整理模擬委託', '請檢查目前模擬委託與成交狀態，列出風險限制與下一步。'],
      ],
      research: [
        ['建立研究計畫', '請依目前研究頁建立動態研究計畫，說明每一步結果、缺口與下一步。'],
        ['整理研究缺口', '請檢查目前研究資料與產物，列出證據缺口和可驗證的後續工作。'],
      ],
      system: [
        ['檢查模型連線', '請檢查目前 Agent 與模型頁的 Provider、模型、連線狀態與待處理問題。'],
        ['檢查資料來源', '請檢查系統資料平台的來源、品質、時間與降級狀態。'],
        ['檢查工具整合', '請檢查目前 Skills、MCP 與工具整合狀態，列出可用能力與缺口。'],
      ],
    };
    return (general[context.workspace] || general.home)
      .map(([label, objective]) => [label, objective, '']);
  }

  function idleState() {
    const root = document.createElement('section');
    root.className = 'agent-empty-state agent-idle-state';
    root.dataset.feedKey = 'empty';
    const context = idleContext();
    const heading = document.createElement('strong');
    heading.textContent = '準備協助目前工作區';
    const location = document.createElement('p');
    location.className = 'agent-idle-context';
    location.textContent = `目前位置：${context.workspaceLabel}${context.tabLabel ? `／${context.tabLabel}` : ''}${context.symbol ? ` · ${context.symbol}` : ''}`;
    const copy = document.createElement('p');
    copy.textContent = '選擇一項常用操作，或直接在下方輸入自己的問題。每一項都會先建立可稽核的分析 Run。';
    const actions = document.createElement('div');
    actions.className = 'agent-idle-actions';
    idleActions(context).forEach(([label, objective, symbol]) => {
      actions.append(idleActionButton(label, objective, symbol));
    });
    root.append(heading, location, copy, actions);
    return root;
  }

  function render(container, state) {
    if (!container) return;
    const nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 120;
    const entries = feedEntries(state);
    const windowSize = Math.max(60, Number(container.dataset.windowSize || 120));
    const visibleEntries = entries.slice(-windowSize);
    const hiddenCount = Math.max(0, entries.length - visibleEntries.length);
    const finalAnswer = entries
      .filter(entry => {
        if (entry.kind !== 'stored-message' || entry.message?.role !== 'assistant') return false;
        const status = state.runs[entry.message.run_id]?.status;
        return ['completed', 'partially_completed', 'max_steps_reached', 'failed', 'cancelled'].includes(String(status || ''));
      })
      .at(-1);
    const finalAnswerKey = finalAnswer
      ? `${finalAnswer.message.run_id || ''}:${finalAnswer.id}:${F().text(finalAnswer.message.content).length}`
      : '';
    const receivedFinalAnswer = Boolean(
      finalAnswerKey && container.dataset.finalAnswerKey !== finalAnswerKey
    );
    const desired = [];
    if (!entries.length) {
      desired.push(idleState());
    }
    const nodes = container._agentFeedNodes || new Map();
    container._agentFeedNodes = nodes;
    if (hiddenCount) {
      const key = 'load-earlier';
      let node = nodes.get(key);
      const label = `載入較早紀錄（尚有 ${hiddenCount} 筆）`;
      if (!node || node.textContent !== label) {
        const replacement = loadEarlierButton(container, hiddenCount);
        replacement.dataset.feedKey = key;
        if (node?.isConnected) node.replaceWith(replacement);
        node = replacement;
        nodes.set(key, node);
      }
      desired.push(node);
    }
    visibleEntries.forEach(entry => {
      const key = entry.key || `${entry.kind}:${entry.id || entry.event?.event_id}`;
      const nextSignature = signature(entry, state);
      let node = nodes.get(key);
      if (!node || node.dataset.feedSignature !== nextSignature) {
        const replacement = entryNode(entry, state);
        replacement.dataset.feedKey = key;
        replacement.dataset.feedSignature = nextSignature;
        if (node?.isConnected) node.replaceWith(replacement);
        node = replacement;
        nodes.set(key, node);
      }
      desired.push(node);
    });
    const desiredSet = new Set(desired);
    [...container.children].forEach(node => {
      if (!desiredSet.has(node)) node.remove();
    });
    let cursor = container.firstElementChild;
    desired.forEach(node => {
      if (node !== cursor) container.insertBefore(node, cursor);
      cursor = node.nextElementSibling;
    });
    [...nodes.entries()].forEach(([key, node]) => {
      if (!desiredSet.has(node)) nodes.delete(key);
    });
    container.dataset.finalAnswerKey = finalAnswerKey;
    if (
      !container.dataset.loadingEarlier
      && (nearBottom || (receivedFinalAnswer && nearBottom))
    ) container.scrollTop = container.scrollHeight;
  }

  window.AgentConversationView = {
    assistantAnswer, feedEntries, entryNode, signature, render,
    refreshIdle() {
      const container = document.getElementById('agentChatFeed');
      const state = window.AgentDockStore?.getState?.() || {};
      if (container && !feedEntries(state).length) render(container, state);
    },
  };
}());
