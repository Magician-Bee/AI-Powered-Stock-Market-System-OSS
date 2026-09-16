(function () {
  'use strict';
  const F = () => window.AgentDockFormatters;
  const focusedPendingApprovals = new Set();

  function create(approval) {
    const card = document.createElement('article');
    card.className = 'agent-approval-card';
    card.tabIndex = -1;
    card.dataset.approvalId = approval.approval_id;
    const title = document.createElement('strong');
    title.textContent = approval.status === 'pending' ? '需要批准' : `批准狀態：${approval.status}`;
    const summary = document.createElement('p');
    summary.textContent = `${approval.tool_name || '操作'} · 風險 ${approval.risk_class || '—'}`;
    const meta = document.createElement('small');
    meta.textContent = `Approval ${approval.approval_id} · 到期 ${F().time(approval.expires_at)} · Digest ${String(approval.argument_digest || '').slice(0, 12)}`;
    const details = document.createElement('details');
    const detailsTitle = document.createElement('summary');
    const pre = document.createElement('pre');
    detailsTitle.textContent = '查看遮蔽後參數與資源範圍';
    pre.textContent = F().text({
      arguments: approval.payload?.arguments,
      resource_scope: approval.resource_scope,
    });
    details.append(detailsTitle, pre);
    card.append(title, summary, meta, details);
    if (approval.status === 'pending') {
      const actions = document.createElement('div');
      actions.className = 'agent-approval-actions';
      const deny = document.createElement('button');
      const approve = document.createElement('button');
      deny.type = approve.type = 'button';
      deny.textContent = '拒絕';
      approve.textContent = '批准並繼續';
      approve.className = 'primary';
      deny.addEventListener('click', () => window.AgentDockController.resolveApproval(approval.approval_id, false));
      approve.addEventListener('click', () => window.AgentDockController.resolveApproval(approval.approval_id, true));
      actions.append(deny, approve);
      card.append(actions);
      if (!focusedPendingApprovals.has(approval.approval_id)) {
        focusedPendingApprovals.add(approval.approval_id);
        queueMicrotask(() => {
          if (card.isConnected) card.focus({ preventScroll: true });
        });
      }
    }
    return card;
  }
  window.AgentApprovalView = { create, focusedPendingApprovals };
}());
