import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ArrowRight, ListTodo, RefreshCw, Square } from 'lucide-react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import type * as B2 from '../../../../../sdk/typescript-client/b2.generated';
import type * as Phase23 from '../../../../../sdk/typescript-client/phase23.generated';
import { EmptyState } from '../../components/EmptyState';
import { StatusBadge } from '../../components/StatusBadge';
import { useOperant } from '../../context/ClientContext';
import { B2LiveAdapter, normalizeB2Error } from '../../live/b2Adapter';
import { createIdempotencyKey } from '../../live/liveState';
import { CanonicalHistoryItem } from '../chat/LiveChatView';

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    active: '活跃', running: '运行中', waiting_approval: '等待审批', completed: '已完成',
    succeeded: '已完成', failed: '已失败', cancelled: '已取消', interrupted: '已中断',
    manual_reconcile_required: '需要人工核对',
  };
  return labels[status] || status;
}

export const LiveRunDetailView: React.FC = () => {
  const { id = '' } = useParams<{ id: string }>();
  const [searchParams] = useSearchParams();
  const sourceTypeParam = searchParams.get('source_type');
  const sourceType = sourceTypeParam === 'session' || sourceTypeParam === 'workflow_run' ? sourceTypeParam : undefined;
  const { b2Client, phase23Client, connectionStatus } = useOperant();
  const b2Adapter = useMemo(() => new B2LiveAdapter(b2Client), [b2Client]);
  const [task, setTask] = useState<B2.B2Task | null>(null);
  const [history, setHistory] = useState<B2.B2SessionHistory | null>(null);
  const [graphRun, setGraphRun] = useState<Phase23.GraphWorkflowRun | null>(null);
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<{ code: string; message: string; recovery: string } | null>(null);
  const loadEpoch = useRef(0);

  const load = useCallback(async () => {
    if (!id.trim()) return;
    const epoch = loadEpoch.current + 1;
    loadEpoch.current = epoch;
    setLoading(true);
    setError(null);
    setTask(null);
    setHistory(null);
    setGraphRun(null);
    try {
      await b2Adapter.connect();
      if (epoch !== loadEpoch.current) return;
      const found = await b2Adapter.getTask(id, sourceType);
      if (epoch !== loadEpoch.current) return;
      setTask(found);
      if (found.source.source_type === 'session') {
        const nextHistory = await b2Adapter.getSessionHistory(found.source.source_id);
        if (epoch !== loadEpoch.current) return;
        setHistory(nextHistory);
      } else if (found.source.source_type === 'workflow_run') {
        try {
          const nextGraphRun = await phase23Client.getGraphRunByLegacyWorkflow(found.source.source_id);
          if (epoch !== loadEpoch.current) return;
          setGraphRun(nextGraphRun);
        } catch (caught: unknown) {
          if (epoch !== loadEpoch.current) return;
          const detail = normalizeB2Error(caught).detail;
          setError({ code: detail.code, message: `WorkflowRun 详情读取失败：${detail.message}`, recovery: detail.recovery });
        }
      }
    } catch (caught: unknown) {
      if (epoch !== loadEpoch.current) return;
      setError(normalizeB2Error(caught).detail);
    } finally {
      if (epoch === loadEpoch.current) setLoading(false);
    }
  }, [b2Adapter, id, phase23Client, sourceType]);

  useEffect(() => {
    void load();
  }, [load]);

  const cancel = async () => {
    if (!task || task.source.source_type !== 'session') return;
    const action = task.actions.find((entry) => entry.action === 'cancel');
    if (!action || action.availability !== 'available') return;
    setWorking(true);
    setError(null);
    try {
      const result = await b2Adapter.cancelSession(task.source.source_id, createIdempotencyKey());
      const accepted = result.accepted;
      await load();
      setError(accepted
        ? { code: 'session_cancel_projection_pending', message: 'Core 已接受取消请求；是否完成仍以刷新后的 Projection 为准。', recovery: 'refresh' }
        : { code: 'session_cancel_rejected', message: 'Core 未接受取消请求；当前状态仍以 Projection 为准。', recovery: 'refresh' });
    } catch (caught: unknown) {
      setError(normalizeB2Error(caught).detail);
    } finally {
      setWorking(false);
    }
  };

  const canCancel = Boolean(task?.actions.some((action) => action.action === 'cancel' && action.availability === 'available'));

  return (
    <div className="section-view b2-run-detail" data-client-mode="live">
      <header className="section-header">
        <div><h1 className="section-title">运行详情</h1><p className="section-sub section-mono">{id}</p></div>
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => void load()} disabled={loading || connectionStatus !== 'connected'}><RefreshCw size={14} aria-hidden="true" />刷新</button>
      </header>
      {error && <div className="live-alert live-alert-error" role="alert"><ListTodo size={16} aria-hidden="true" /><div className="live-alert-content"><strong>{error.code}</strong><span>{error.message}</span><span className="live-alert-recovery">恢复：{error.recovery}</span></div><button type="button" className="btn btn-secondary btn-sm" onClick={() => void load()} disabled={loading}>重试</button></div>}
      <div className="section-scroll"><div className="section-inner">
        {loading ? <div className="live-panel-loading" role="status"><RefreshCw size={16} className="animate-spin" />正在读取 Core 运行详情…</div> : !task ? <EmptyState icon={ListTodo} title="没有可显示的运行详情" description="Live 只显示 Core 返回的 B2 Task Projection。" /> : (
          <>
            <section className="card b2-run-summary" aria-label="Core Task Projection">
              <div><span className="b2-run-source">{task.source.source_type}</span><h2>{task.title}</h2><p className="section-mono">source_id={task.source.source_id}</p></div>
              <StatusBadge status={task.source_status} label={statusLabel(task.source_status)} />
              {canCancel && <button type="button" className="btn btn-danger btn-sm" onClick={() => void cancel()} disabled={working}><Square size={13} aria-hidden="true" />{working ? '取消中…' : '取消 Session'}</button>}
            </section>
            <dl className="b2-run-meta card"><div><dt>Project</dt><dd>{task.project_id || '—'}</dd></div><div><dt>Workspace</dt><dd>{task.workspace_id || '—'}</dd></div><div><dt>Thread</dt><dd>{task.thread_id || '—'}</dd></div><div><dt>Revision</dt><dd>{task.revision}</dd></div></dl>
            {task.source.source_type === 'session' && history && <section className="card b2-run-agents"><div className="live-section-heading"><h2>AgentInstance</h2><span>{history.agents.length} 个 · 使用不可变 RoleSnapshot</span></div>{history.agents.length === 0 ? <p className="live-panel-empty">Core 尚未返回 AgentInstance。</p> : <ul className="chat-row-list">{history.agents.map((agent, index) => <li className="chat-row chat-row-wrap" key={agent.id || `${agent.session_id}:${index}`}><span className="chat-row-main"><span className="chat-row-title">{agent.role_snapshot.role_name}</span><span className="chat-row-meta">{agent.id || 'AgentInstance'} · role v{agent.role_snapshot.role_version} · {agent.role_snapshot.model_id}</span></span><StatusBadge status={agent.status || 'created'} size="sm" /></li>)}</ul>}</section>}
            {task.source.source_type === 'session' && history && <section className="card b2-run-history"><div className="live-section-heading"><h2>Canonical Session History</h2><span>{history.items.length} items · Cursor {history.next_cursor ?? '—'}</span></div>{history.items.length === 0 ? <p className="live-panel-empty">Core 尚未返回 canonical items。</p> : history.items.map((item, index) => <CanonicalHistoryItem item={item} index={index} key={item.id || `${item.thread_id}:${item.turn_id}:${item.position ?? index}`} />)}</section>}
            {task.source.source_type === 'session' && task.thread_id && <Link className="btn btn-secondary" to={`/chat/${encodeURIComponent(task.thread_id)}`}>打开 Session 会话 <ArrowRight size={13} aria-hidden="true" /></Link>}
            {task.source.source_type === 'workflow_run' && <section className="card b2-run-graph"><div className="live-section-heading"><h2>Graph WorkflowRun</h2>{graphRun && <StatusBadge status={graphRun.status} label={statusLabel(graphRun.status)} size="sm" />}</div>{graphRun ? <><p className="section-mono">Graph Run {graphRun.id} · revision {graphRun.revision}</p><p>当前节点：{graphRun.current_node_ids.join(', ') || '无'}</p><Link className="btn btn-secondary btn-sm" to={`/collab?view=runs&legacyWorkflowRunId=${encodeURIComponent(id)}`}>进入 Graph 运行监控 <ArrowRight size={13} aria-hidden="true" /></Link></> : <p className="live-panel-empty">等待 Phase23 Graph Projection。</p>}</section>}
            {task.source.source_type === 'team_task' && <p className="live-panel-empty">Core 返回了 TeamTask 来源；本批不合成 TeamTask 详情。</p>}
          </>
        )}
      </div></div>
    </div>
  );
};
