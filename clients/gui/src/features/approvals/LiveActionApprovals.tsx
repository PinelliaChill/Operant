import React, { useCallback, useEffect, useRef, useState } from 'react';
import type { ActionApprovalView } from '../../../../../sdk/typescript-client/onboarding.generated.ts';
import { useOperant } from '../../context/ClientContext';
import { useOnboarding } from '../../live/OnboardingContext';
import { currentBrowserOrigin } from '../../lib/liveBaseUrl';
import { readApprovalDecision, submitApprovalDecision } from '../../lib/actionApprovalDecision';

const pendingKey = `operant.action-approval.pending.v1:${currentBrowserOrigin()}`;
const actionLabel = (item: ActionApprovalView) => item.tool === 'skill_source'
  ? item.operation === 'add' ? '添加技能目录' : '移除技能目录' : '待确认操作';

export const LiveActionApprovals: React.FC = () => {
  const { client } = useOnboarding();
  const { phase45Client, connectionStatus } = useOperant();
  const [items, setItems] = useState<ActionApprovalView[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [confirmation, setConfirmation] = useState<{ item: ActionApprovalView; approved: boolean } | null>(null);
  const [pendingId, setPendingId] = useState<string | null>(() => {
    try { return localStorage.getItem(pendingKey); } catch { return null; }
  });
  const [storageReady, setStorageReady] = useState(() => {
    try { localStorage.getItem(pendingKey); return true; } catch { return false; }
  });
  const revision = useRef(0);
  const dispatching = useRef(false);
  const confirmationButton = useRef<HTMLButtonElement>(null);
  const cardButtons = useRef(new Map<string, HTMLButtonElement>());
  const load = useCallback(async (after?: string) => {
    if (connectionStatus !== 'connected' || dispatching.current) return;
    const current = ++revision.current;
    setBusy(true); setError('');
    try {
      const page = await client.listActionApprovals({ limit: 50, afterCursor: after });
      if (revision.current !== current) return;
      setItems(previous => after ? [...previous, ...page.items.filter(item => !previous.some(old => old.approval_id === item.approval_id))] : page.items);
      setCursor(page.next_cursor ?? null);
    } catch { if (revision.current === current) setError('审批列表读取失败，请恢复连接后刷新。'); }
    finally { if (revision.current === current) setBusy(false); }
  }, [client, connectionStatus]);
  useEffect(() => { void load(); return () => { revision.current += 1; }; }, [load]);
  useEffect(() => { confirmationButton.current?.focus(); }, [confirmation]);
  const settle = async (id: string, status: Awaited<ReturnType<typeof readApprovalDecision>>) => {
    if (!status) { setError('决定尚未确认。请核对原审批，暂时不要再次提交。'); return; }
    try { localStorage.removeItem(pendingKey); setPendingId(null); }
    catch { setStorageReady(false); setError('无法保存审批状态，请检查浏览器存储后核对。'); return; }
    setItems(previous => previous.filter(item => item.approval_id !== id));
    setNotice(status === 'approved' || status === 'consumed' ? '已批准。请回到原客户端继续操作。' : status === 'denied' ? '已拒绝。' : '审批已过期。');
  };
  const decide = async () => {
    if (!confirmation || dispatching.current || pendingId || !storageReady || connectionStatus !== 'connected') return;
    dispatching.current = true; revision.current += 1; setBusy(true); setError(''); setNotice('');
    const { item, approved } = confirmation;
    try {
      localStorage.setItem(pendingKey, item.approval_id);
      setPendingId(item.approval_id); setConfirmation(null);
      await settle(item.approval_id, await submitApprovalDecision(phase45Client, item.approval_id, approved));
    } catch { setStorageReady(false); setError('无法保存本次审批记录，未提交决定。请检查浏览器存储。'); }
    finally { dispatching.current = false; setBusy(false); }
  };
  const reconcile = async () => {
    if (!pendingId || dispatching.current || connectionStatus !== 'connected') return;
    dispatching.current = true; setBusy(true); setError('');
    try { await settle(pendingId, await readApprovalDecision(phase45Client, pendingId)); }
    finally { dispatching.current = false; setBusy(false); }
  };
  const cancel = () => {
    const id = confirmation?.item.approval_id;
    setConfirmation(null);
    requestAnimationFrame(() => { if (id) cardButtons.current.get(id)?.focus(); });
  };
  return <section className="config-card" aria-labelledby="action-approvals-title">
    <div className="config-field-head"><h2 id="action-approvals-title">本机操作</h2><button className="btn btn-secondary btn-sm" disabled={busy || connectionStatus !== 'connected'} onClick={() => void load()}>刷新本机审批</button></div>
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {!storageReady && <p role="alert">浏览器存储不可用，暂时不能提交审批决定。</p>}
    {pendingId && <div role="alert"><p>有一项决定尚未确认。</p><button className="btn btn-secondary" disabled={busy || connectionStatus !== 'connected'} onClick={() => void reconcile()}>核对原审批</button></div>}
    {!items.length && !busy && !error && <p>没有待处理的本机操作。</p>}
    {busy && <p role="status">正在读取审批…</p>}
    {items.map(item => <article className="config-card" key={item.approval_id}>
      <h3>{actionLabel(item)}</h3><p style={{ overflowWrap: 'anywhere' }}>{item.target_label}</p>{item.device_name && <p>客户端：{item.device_name}</p>}
      {confirmation?.item.approval_id === item.approval_id ? <div role="group" aria-label="确认审批决定"><p>确认{confirmation.approved ? '批准' : '拒绝'}这次操作？</p><button ref={confirmationButton} className="btn btn-primary" disabled={busy || Boolean(pendingId)} onClick={() => void decide()}>确认{confirmation.approved ? '批准' : '拒绝'}</button><button className="btn btn-secondary" disabled={busy} onClick={cancel}>取消</button></div>
      : <div className="config-actions"><button ref={element => { if (element) cardButtons.current.set(item.approval_id, element); else cardButtons.current.delete(item.approval_id); }} className="btn btn-primary" disabled={busy || !storageReady || Boolean(pendingId) || connectionStatus !== 'connected'} onClick={() => setConfirmation({ item, approved: true })}>批准</button><button className="btn btn-secondary" disabled={busy || !storageReady || Boolean(pendingId) || connectionStatus !== 'connected'} onClick={() => setConfirmation({ item, approved: false })}>拒绝</button></div>}
      <details><summary>查看详情</summary><p>操作：{item.tool} · {item.operation}</p><p>审批编号：{item.approval_id}</p><p>申请时间：{item.requested_at} · 有效期至：{item.expires_at}</p></details>
    </article>)}
    {cursor && <button className="btn btn-secondary" disabled={busy} onClick={() => void load(cursor)}>加载更多</button>}
  </section>;
};
