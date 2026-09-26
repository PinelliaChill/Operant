import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { AlertTriangle, ArrowUpRight, RefreshCw, SendHorizontal, Square } from 'lucide-react';
import { useLive } from '../../live/LiveContext';
import { workbenchClient } from '../../live/workbenchClient';
import type { ChildAgentView, WorkbenchContext, WorkbenchMessage } from '../../live/workbenchClient';
import { StatusBadge } from '../../components/StatusBadge';
import { messageDeliveryLabel } from './workbenchPresentation';

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : 'Core 工作台请求失败';
}

const childStatusLabels: Record<ChildAgentView['status'], string> = {
  queued: '排队中', running: '运行中', completed: '已完成', failed: '失败',
  cancelled: '已取消', interrupted: '已中断',
};

export const LiveWorkbenchPanel: React.FC<{ threadId: string; connected: boolean; onChanged: () => Promise<void> }> = ({ threadId, connected, onChanged }) => {
  const navigate = useNavigate();
  const { selectedThread, threads, roles, models, selectThread, stream } = useLive();
  const [children, setChildren] = useState<ChildAgentView[]>([]);
  const [messages, setMessages] = useState<WorkbenchMessage[]>([]);
  const [context, setContext] = useState<WorkbenchContext | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [queryError, setQueryError] = useState('');
  const [notice, setNotice] = useState('');
  const [task, setTask] = useState('');
  const [roleId, setRoleId] = useState('');
  const [modelId, setModelId] = useState('');
  const [recipient, setRecipient] = useState('');
  const [message, setMessage] = useState('');
  const [replyTo, setReplyTo] = useState('');
  const [contextCommandBusy, setContextCommandBusy] = useState(false);
  const mutationKeys = useRef(new Map<string, string>());
  const requestSequence = useRef(0);
  const keyFor = (scope: string, fingerprint: string) => {
    const identity = `${scope}:${fingerprint}`;
    const existing = mutationKeys.current.get(identity);
    if (existing) return existing;
    const key = crypto.randomUUID();
    mutationKeys.current.set(identity, key);
    return key;
  };

  const refresh = useCallback(async () => {
    if (!connected) return;
    const sequence = ++requestSequence.current;
    setLoading(true);
    const results = await Promise.allSettled([
      workbenchClient.listChildren(threadId),
      workbenchClient.listMessages(threadId),
      workbenchClient.getContext(threadId),
    ]);
    if (sequence !== requestSequence.current) return;
    if (results[0].status === 'fulfilled') setChildren(results[0].value);
    if (results[1].status === 'fulfilled') setMessages(results[1].value);
    if (results[2].status === 'fulfilled') setContext(results[2].value);
    const failures = results.filter((result): result is PromiseRejectedResult => result.status === 'rejected');
    setQueryError(failures.length ? failures.map((failure) => errorText(failure.reason)).join('；') : '');
    setLoading(false);
  }, [connected, threadId]);

  useEffect(() => {
    ++requestSequence.current;
    setChildren([]);
    setMessages([]);
    setContext(null);
    setRecipient('');
    setNotice('');
    setError('');
    setQueryError('');
    void refresh();
    return () => { ++requestSequence.current; };
  }, [refresh]);

  useEffect(() => {
    if (!connected || !stream.lastEvent) return;
    if (stream.lastEvent.thread_id === threadId || stream.lastEvent.session_id === selectedThread?.sessionId) void refresh();
  }, [connected, refresh, selectedThread?.sessionId, stream.lastEvent, threadId]);

  useEffect(() => {
    if (!connected || !children.some((item) => item.status === 'queued' || item.status === 'running')) return;
    const timer = window.setInterval(() => { void refresh(); }, 3000);
    return () => window.clearInterval(timer);
  }, [children, connected, refresh]);

  const recipients = useMemo(() => {
    const parent = selectedThread?.parentThreadId ? threads.find((item) => item.id === selectedThread.parentThreadId) : undefined;
    const siblings = selectedThread?.parentThreadId
      ? threads.filter((item) => item.id !== threadId && item.parentThreadId === selectedThread.parentThreadId)
      : [];
    return [parent && { id: parent.id, label: `父会话 ${parent.title}` },
      ...siblings.map((item) => ({ id: item.id, label: `同级会话 ${item.title}` })),
      ...children.map((child) => ({ id: child.thread_id, label: `子会话 ${child.role_id} · ${child.thread_id.slice(0, 8)}` }))]
      .filter((item): item is { id: string; label: string } => Boolean(item));
  }, [children, selectedThread?.parentThreadId, threadId, threads]);

  const createChild = async () => {
    if (!connected || !selectedThread?.sessionId || !task.trim() || busy) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const input = {
        task: task.trim(),
        ...(roleId ? { role_id: roleId } : {}),
        ...(modelId ? { model_profile_id: modelId } : {}),
      };
      const identity = `child:${threadId}:${JSON.stringify(input)}`;
      const child = await workbenchClient.createChild(threadId, input, keyFor('child', `${threadId}:${JSON.stringify(input)}`));
      mutationKeys.current.delete(identity);
      setTask('');
      setNotice(`已创建子会话 ${child.thread_id.slice(0, 8)}；状态由 Core 回读。`);
      await onChanged();
      await refresh();
    } catch (cause) { setError(errorText(cause)); }
    finally { setBusy(false); }
  };

  const cancelChild = async (child: ChildAgentView) => {
    if (!connected || busy) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const identity = `cancel:${child.thread_id}`;
      const result = await workbenchClient.cancelChild(child.thread_id, keyFor('cancel', child.thread_id));
      mutationKeys.current.delete(identity);
      setNotice(result ? `Core 已返回 ${result.status}，请核对最终结果。` : 'Core 已接受取消请求，请刷新核对子会话结果。');
      await onChanged();
      await refresh();
    } catch (cause) { setError(errorText(cause)); }
    finally { setBusy(false); }
  };

  const sendDirected = async () => {
    if (!connected || busy || !recipient || !message.trim()) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const input = { recipient_thread_id: recipient, body: message.trim(), ...(replyTo ? { reply_to: replyTo } : {}) };
      const identity = `message:${threadId}:${JSON.stringify(input)}`;
      const sent = await workbenchClient.sendMessage(threadId, { ...input, idempotency_key: keyFor('message', `${threadId}:${JSON.stringify(input)}`) });
      mutationKeys.current.delete(identity);
      setMessage(''); setReplyTo('');
      setNotice(`发给 ${sent.recipient_thread_id.slice(0, 8)} 的消息：${messageDeliveryLabel(sent)}。`);
      await refresh();
    } catch (cause) { setError(errorText(cause)); }
    finally { setBusy(false); }
  };

  const navigateChild = (id: string) => {
    if (selectThread(id)) navigate(`/chat/${encodeURIComponent(id)}`);
    else setError('Core Projection 尚未包含此子会话，请刷新后进入。');
  };

  const runContextCommand = async (text: '/compact-context' | '/clear-context') => {
    if (!connected || contextCommandBusy) return;
    setContextCommandBusy(true); setError(''); setNotice('');
    try {
      const identity = `command:${threadId}:${text}`;
      const result = await workbenchClient.executeCommand(threadId, { text }, keyFor('command', `${threadId}:${text}`));
      mutationKeys.current.delete(identity);
      setNotice(`${result.command}：${result.message}`);
      await refresh();
    } catch (cause) { setError(errorText(cause)); }
    finally { setContextCommandBusy(false); }
  };

  const usage = context?.token_estimate != null && context?.context_window
    ? Math.min(100, Math.round(context.token_estimate / context.context_window * 100)) : null;
  return <section className="live-workbench" aria-label="多 Agent 工作台">
    <div className="live-panel-heading"><div><h2>协作与上下文</h2><p>子会话、定向消息和当前会话的上下文占用</p></div>
      <button type="button" className="btn btn-ghost btn-sm" onClick={() => void refresh()} disabled={!connected || loading}><RefreshCw size={14} aria-hidden="true" />刷新</button>
    </div>
    {!connected && <p className="live-workbench-error" role="alert">Core 未连接，协作状态可能已过期。</p>}
    {error && <p className="live-workbench-error" role="alert"><AlertTriangle size={14} aria-hidden="true" />{error}</p>}
    {queryError && <p className="live-workbench-error" role="alert"><AlertTriangle size={14} aria-hidden="true" />{queryError}</p>}
    {notice && <p className="live-workbench-notice" role="status">{notice}</p>}
    <div className="live-workbench-section">
      <h3>子 Agent <span>{children.length}</span></h3>
      {children.length === 0 ? <p className="live-panel-empty">当前会话还没有子 Agent。</p> : <ul className="live-workbench-children">{children.map((child) => <li key={child.thread_id}>
        <div><strong>{child.role_id || '继承父角色'}</strong><StatusBadge status={child.status} label={childStatusLabels[child.status]} size="sm" /><p>{child.task}</p>{child.result && <p className="live-workbench-result">结果：{child.result}</p>}{child.recovery && <p>恢复：{child.recovery}</p>}
          <details className="live-workbench-child-scope"><summary>模型、工作区与权限</summary><dl>
            <div><dt>模型配置</dt><dd>{child.model_profile_id}</dd></div>
            <div><dt>工作区</dt><dd>{child.workspace_ref}</dd></div>
            <div><dt>允许工具</dt><dd>{child.tool_policy.allowed_tools?.join('、') || '无'}</dd></div>
            <div><dt>写入权限</dt><dd>{child.tool_policy.workspace_write ? '允许' : '只读'}</dd></div>
            <div><dt>轮次预算</dt><dd>{child.budget.max_turns ?? '未限制'}</dd></div>
          </dl></details></div>
        <div className="live-workbench-actions"><button type="button" className="btn btn-secondary btn-sm" onClick={() => navigateChild(child.thread_id)}><ArrowUpRight size={13} aria-hidden="true" />进入</button>
          {['queued', 'running', 'interrupted'].includes(child.status) && <button type="button" className="btn btn-ghost btn-sm" onClick={() => void cancelChild(child)} disabled={!connected || busy}><Square size={12} aria-hidden="true" />取消</button>}</div>
      </li>)}</ul>}
      <div className="live-workbench-form"><label>任务描述<textarea className="textarea" value={task} onChange={(event) => setTask(event.target.value)} rows={2} placeholder="交给子 Agent 的具体任务" disabled={!connected || !selectedThread?.sessionId || busy} /></label>
        <div className="live-workbench-form-row"><label>角色<select className="select" value={roleId} onChange={(event) => setRoleId(event.target.value)} disabled={!connected || busy}><option value="">继承父角色</option>{roles.filter((role) => role.status !== 'inactive').map((role) => <option key={role.id} value={role.id}>{role.name}</option>)}</select></label>
          <label>模型配置<select className="select" value={modelId} onChange={(event) => setModelId(event.target.value)} disabled={!connected || busy}><option value="">继承角色配置</option>{models.map((model) => <option key={model.id} value={model.id}>{model.name || model.id}</option>)}</select></label></div>
        <p className="live-workbench-hint">未覆盖时继承当前会话的角色、模型、权限、工作区与预算；创建后的有效配置以 Core 回读为准。</p>
        <button type="button" className="btn btn-secondary btn-sm" onClick={() => void createChild()} disabled={!connected || !selectedThread?.sessionId || !task.trim() || busy}>创建子 Agent</button>
      </div>
    </div>
    <div className="live-workbench-section"><h3>定向消息</h3>
      {messages.length === 0 ? <p className="live-panel-empty">没有发给此会话或由此会话发出的消息。</p> : <ol className="live-workbench-messages">{messages.map((item) => <li key={item.message_id}>
        <small>{item.sender_thread_id.slice(0, 8)} → {item.recipient_thread_id.slice(0, 8)} · {messageDeliveryLabel(item)}{item.reply_to ? ` · 回复 ${item.reply_to.slice(0, 8)}` : ''}</small>
        <p>{item.body}</p><button type="button" className="ui-text-action" onClick={() => { setRecipient(item.sender_thread_id); setReplyTo(item.message_id); }} disabled={item.sender_thread_id === threadId}>回复</button>
      </li>)}</ol>}
      <div className="live-workbench-form"><label>收件会话<select className="select" value={recipient} onChange={(event) => { setRecipient(event.target.value); setReplyTo(''); }} disabled={!connected || busy}><option value="">选择收件人</option>{recipients.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
        <label>消息<textarea className="textarea" value={message} onChange={(event) => setMessage(event.target.value)} rows={2} placeholder="仅发给选中的会话" disabled={!connected || busy} /></label>
        {replyTo && <button type="button" className="ui-text-action" onClick={() => setReplyTo('')}>取消回复关联</button>}
        <button type="button" className="btn btn-secondary btn-sm" onClick={() => void sendDirected()} disabled={!connected || !recipient || !message.trim() || busy}><SendHorizontal size={13} aria-hidden="true" />发送给收件人</button>
      </div>
    </div>
    <div className="live-workbench-section"><h3>上下文占用</h3>
      {context ? <><p>{context.token_estimate == null ? 'Core 尚未给出 Token 估算' : `${context.token_estimate.toLocaleString()} Token`}{context.context_window ? ` / ${context.context_window.toLocaleString()} · ${usage}%` : ''} · {context.watermark}</p>
        {usage !== null && <progress value={usage} max={100} aria-label="上下文占用百分比" />}
        {context.compaction_id && <p>最近压缩：{context.compaction_id.slice(0, 12)}{context.pre_compaction_token_estimate != null ? ` · 压缩前 ${context.pre_compaction_token_estimate.toLocaleString()} Token` : ''}</p>}
        {context.baseline_operation && <p>当前基线：{context.baseline_operation} · 下轮生效</p>}
        <details><summary>上下文详情</summary><p>{context.blocks?.length ?? 0} 个区块 · {context.references?.length ?? 0} 个引用</p>
          <ul>{(context.blocks ?? []).map((block, index) => <li key={index}>{String(block.type ?? `区块 ${index + 1}`)}{typeof block.token_estimate === 'number' ? ` · ${block.token_estimate} Token` : ''}</li>)}</ul>
          {(context.references?.length ?? 0) > 0 && <><strong>已绑定引用</strong><ul>{(context.references ?? []).map((reference, index) => <li key={index}>{String(reference.ref_type ?? '引用')} · {String(reference.target_id ?? reference.source_ref ?? '来源已记录')}</li>)}</ul></>}
        </details>
        <div className="live-workbench-actions"><button type="button" className="btn btn-secondary btn-sm" onClick={() => void runContextCommand('/compact-context')} disabled={!connected || contextCommandBusy}>压缩上下文</button>
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => void runContextCommand('/clear-context')} disabled={!connected || contextCommandBusy}>清理上下文</button></div>
      </> : <p className="live-panel-empty">{loading ? '正在读取上下文…' : '暂无上下文详情'}</p>}
    </div>
  </section>;
};
