import React, { useCallback, useEffect, useRef, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { useOperant } from '../../context/ClientContext';
import { usePhase45 } from '../../live45/Phase45Context';

interface AskApproval {
  approval_id: string; action_hash: string; status: string;
  decided_by: string | null; reason_code: string | null;
  requested_at: string; expires_at: string;
}
function parseApproval(value: Record<string, unknown>): AskApproval {
  if (typeof value.approval_id !== 'string' || typeof value.action_hash !== 'string' || typeof value.status !== 'string') throw new Error('Core ASK Approval 格式无效');
  return {
    approval_id: value.approval_id, action_hash: value.action_hash, status: value.status,
    decided_by: typeof value.decided_by === 'string' ? value.decided_by : null,
    reason_code: typeof value.reason_code === 'string' ? value.reason_code : null,
    requested_at: typeof value.requested_at === 'string' ? value.requested_at : '',
    expires_at: typeof value.expires_at === 'string' ? value.expires_at : '',
  };
}

export const LiveAskAudit: React.FC = () => {
  const { phase45Client, connectionStatus } = useOperant();
  const { mcpIntervention, loadSecurityAudit, auditsByAction, auditError, auditLoadingActionHash } = usePhase45();
  const [id, setId] = useState(mcpIntervention?.approvalId || '');
  const [approval, setApproval] = useState<AskApproval | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [confirmation, setConfirmation] = useState<boolean | null>(null);
  const confirmButtonRef = useRef<HTMLButtonElement>(null);
  const approveButtonRef = useRef<HTMLButtonElement>(null);
  const rejectButtonRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (mcpIntervention?.approvalId) {
      setId(mcpIntervention.approvalId);
      setApproval(null);
      setConfirmation(null);
    }
  }, [mcpIntervention?.approvalId]);
  useEffect(() => { if (confirmation !== null) confirmButtonRef.current?.focus(); }, [confirmation]);
  const load = useCallback(async () => {
    if (!id.trim() || connectionStatus !== 'connected') return;
    setBusy(true); setError(''); setConfirmation(null);
    try {
      const next = parseApproval(await phase45Client.getPhase45Approval(id.trim()));
      setApproval(next);
      await loadSecurityAudit(next.action_hash);
    } catch (cause) { setApproval(null); setError(cause instanceof Error ? cause.message : 'ASK 审批查询失败'); }
    finally { setBusy(false); }
  }, [connectionStatus, id, loadSecurityAudit, phase45Client]);
  const decide = async (approved: boolean) => {
    if (!approval || approval.status !== 'pending' || busy || confirmation !== approved || connectionStatus !== 'connected') return;
    setConfirmation(null); setBusy(true); setError(''); setNotice('');
    try {
      await phase45Client.decidePhase45Approval(approval.approval_id, { approved, reason_code: approved ? 'human_approved' : 'human_denied' }, { idempotencyKey: crypto.randomUUID() });
      const next = parseApproval(await phase45Client.getPhase45Approval(approval.approval_id));
      setApproval(next); await loadSecurityAudit(next.action_hash);
      setNotice(`Core 已确认：${next.status}；人工决定已写入审计。`);
    } catch (cause) { setError(cause instanceof Error ? cause.message : '决定未确认，请重新查询 Core 状态'); }
    finally { setBusy(false); }
  };
  const cancelConfirmation = () => {
    const previous = confirmation;
    setConfirmation(null);
    requestAnimationFrame(() => (previous ? approveButtonRef.current : rejectButtonRef.current)?.focus());
  };
  const facts = approval ? auditsByAction[approval.action_hash] || [] : [];
  const approvalExpired = approval?.status === 'pending' && !!approval.expires_at && Date.parse(approval.expires_at) <= Date.now();
  return <section className="config-card" aria-labelledby="ask-audit-title"><h2 id="ask-audit-title">ASK 审批审计与人工接管</h2><p className="config-hint">输入 Core 返回的审批 ID，核对自动审核结论或延期原因。模型没有可靠结论时，申请保持待处理，可由人工决定。</p>
    <div className="live-ask-lookup"><label>审批 ID<input className="input" value={id} onChange={(event) => { setId(event.target.value); setApproval(null); setConfirmation(null); setNotice(''); }} placeholder="Core Approval ID" /></label><button type="button" className="btn btn-secondary btn-sm" onClick={() => void load()} disabled={!id.trim() || busy || connectionStatus !== 'connected'}><RefreshCw size={13} />查询</button></div>
    {error && <div className="live-alert live-alert-error" role="alert">{error}</div>}{notice && <div className="live-alert" role="status">{notice}</div>}
    {approval && <div className="live-ask-result"><p><strong>状态：{approval.status}</strong> · 决定者：{approval.decided_by || '尚未决定'} · 原因：{approval.reason_code || '无'}</p><p>请求：{approval.requested_at} · 过期：{approval.expires_at}</p><p>Action Hash：<code>{approval.action_hash}</code></p>
      {approvalExpired && <p role="alert">此申请已过期，请返回会话重新发起。</p>}
      {approval.status === 'pending' && !approvalExpired && (confirmation === null
        ? <div className="config-actions"><button ref={approveButtonRef} type="button" className="btn btn-primary btn-sm" onClick={() => setConfirmation(true)} disabled={busy || connectionStatus !== 'connected'}>人工批准</button><button ref={rejectButtonRef} type="button" className="btn btn-secondary btn-sm" onClick={() => setConfirmation(false)} disabled={busy || connectionStatus !== 'connected'}>人工拒绝</button></div>
        : <div role="group" aria-label="确认 ASK 审批决定"><p>确认{confirmation ? '批准' : '拒绝'}申请 <code>{approval.approval_id}</code>？决定将写入 Core 审计。</p><div className="config-actions"><button ref={confirmButtonRef} type="button" className={confirmation ? 'btn btn-primary btn-sm' : 'btn btn-secondary btn-sm'} onClick={() => void decide(confirmation)} disabled={busy || connectionStatus !== 'connected'}>确认{confirmation ? '批准' : '拒绝'}</button><button type="button" className="btn btn-ghost btn-sm" onClick={cancelConfirmation} disabled={busy}>取消</button></div></div>)}
      <h3>安全审计事实</h3>{auditError && <p role="alert">{auditError.code}：{auditError.message}</p>}{auditLoadingActionHash && <p role="status">正在读取审计…</p>}{!auditLoadingActionHash && !auditError && facts.length === 0 && <p>暂无审计事实。</p>}{facts.length > 0 && <ol>{facts.map((fact) => <li key={fact.eventId}>{fact.eventType} · {fact.decision || '事实'} · {fact.createdAt}{fact.reasonCode ? ` · 原因 ${fact.reasonCode}` : ''}{fact.decidedBy ? ` · 决定者 ${fact.decidedBy}` : ''}{fact.ruleIds.length ? ` · 规则 ${fact.ruleIds.join('、')}` : ''}</li>)}</ol>}
    </div>}
  </section>;
};
