import './b2-task-visual.css';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ArrowRight, ListTodo, RefreshCw, ShieldAlert } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import type * as B2 from '../../../../../sdk/typescript-client/b2.generated';
import { EmptyState } from '../../components/EmptyState';
import { StatusBadge } from '../../components/StatusBadge';
import { useOperant } from '../../context/ClientContext';
import { B2LiveAdapter, normalizeB2Error } from '../../live/b2Adapter';
import { createIdempotencyKey } from '../../live/liveState';

type TaskFilter = 'all' | 'active' | 'done';

const ACTION_LABELS: Record<B2.TaskAction['action'], string> = {
  inspect: '查看详情',
  cancel: '取消',
  resume: '恢复',
  approve: '审批',
  archive: '归档',
};

const SOURCE_LABELS: Record<B2.TaskSource['source_type'], string> = {
  session: 'Session',
  workflow_run: 'WorkflowRun',
  team_task: 'TeamTask',
};

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    active: '活跃',
    running: '运行中',
    waiting_approval: '等待审批',
    completed: '已完成',
    succeeded: '已完成',
    failed: '已失败',
    cancelled: '已取消',
    archived: '已归档',
  };
  return labels[status] || status;
}

export const LiveTasksView: React.FC = () => {
  const { b2Client, connectionStatus } = useOperant();
  const navigate = useNavigate();
  const adapter = useMemo(() => new B2LiveAdapter(b2Client), [b2Client]);
  const [tasks, setTasks] = useState<B2.B2Task[]>([]);
  const [filter, setFilter] = useState<TaskFilter>('all');
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState<string | null>(null);
  const [error, setError] = useState<{ code: string; message: string; recovery: string } | null>(null);
  const loadEpoch = useRef(0);

  const load = useCallback(async () => {
    const epoch = loadEpoch.current + 1;
    loadEpoch.current = epoch;
    setLoading(true);
    setError(null);
    try {
      await adapter.connect();
      const page = await adapter.listTasks();
      if (epoch !== loadEpoch.current) return;
      setTasks(page.items);
    } catch (caught: unknown) {
      if (epoch !== loadEpoch.current) return;
      setTasks([]);
      setError(normalizeB2Error(caught).detail);
    } finally {
      if (epoch === loadEpoch.current) setLoading(false);
    }
  }, [adapter]);

  useEffect(() => {
    void load();
  }, [load]);

  const filteredTasks = useMemo(() => tasks.filter((task) => {
    if (filter === 'all') return true;
    const done = ['completed', 'succeeded', 'failed', 'cancelled', 'archived'].includes(task.source_status);
    return filter === 'done' ? done : !done;
  }), [filter, tasks]);

  const inspectTask = (task: B2.B2Task) => {
    if (task.source.source_type === 'session' && task.thread_id) {
      navigate(`/chat/${encodeURIComponent(task.thread_id)}`);
      return;
    }
    if (task.source.source_type === 'workflow_run') {
      navigate(`/runs/${encodeURIComponent(task.source.source_id)}?source_type=workflow_run`);
      return;
    }
    setError({ code: 'task_inspect_unsupported', message: 'Core 返回的 TeamTask 没有本批 GUI 详情入口。', recovery: 'none' });
  };

  const executeAction = async (task: B2.B2Task, action: B2.TaskAction) => {
    if (action.availability !== 'available') return;
    if (action.action === 'inspect' || action.action === 'approve') {
      inspectTask(task);
      return;
    }
    if (action.action === 'resume' || action.action === 'archive') {
      setError({ code: `task_${action.action}_unsupported`, message: `Core 暂未提供 ${ACTION_LABELS[action.action]} Command；不会把“查看详情”冒充该动作。`, recovery: 'open_details' });
      inspectTask(task);
      return;
    }
    if (action.action !== 'cancel') return;
    if (task.source.source_type !== 'session') {
      setError({ code: 'task_cancel_requires_graph_ui', message: 'WorkflowRun 取消请进入 Graph 运行详情，由对应服务端协议裁决。', recovery: 'open_details' });
      inspectTask(task);
      return;
    }
    setWorking(task.source.source_id);
    setError(null);
    try {
      const result = await adapter.cancelSession(task.source.source_id, createIdempotencyKey());
      const accepted = result.accepted;
      await load();
      setError(accepted
        ? { code: 'session_cancel_projection_pending', message: 'Core 已接受取消请求；是否完成仍以刷新后的 Task Projection 为准。', recovery: 'refresh' }
        : { code: 'session_cancel_rejected', message: 'Core 未接受取消请求，任务状态仍以服务端 Projection 为准。', recovery: 'refresh' });
    } catch (caught: unknown) {
      setError(normalizeB2Error(caught).detail);
    } finally {
      setWorking(null);
    }
  };

  return (
    <div className="section-view b2-task-page" data-client-mode="live">
      <header className="section-header">
        <div>
          <h1 className="section-title">任务</h1>
          <p className="section-sub">B2 Core Task Projection · Session 与 WorkflowRun 保持来源语义</p>
        </div>
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => void load()} disabled={loading || connectionStatus !== 'connected'}>
          <RefreshCw size={14} aria-hidden="true" />刷新
        </button>
        <div className="section-chips b2-task-filter" role="group" aria-label="任务过滤">
          {([['all', '全部'], ['active', '进行中'], ['done', '已结束']] as const).map(([key, label]) => (
            <button type="button" key={key} className={`section-chip${filter === key ? ' active' : ''}`} aria-pressed={filter === key} onClick={() => setFilter(key)}>{label}</button>
          ))}
        </div>
      </header>
      {error && (
        <div className="live-alert live-alert-error b2-task-error" role="alert">
          <ShieldAlert size={16} aria-hidden="true" />
          <div className="live-alert-content"><strong>{error.code}</strong><span>{error.message}</span><span className="live-alert-recovery">恢复：{error.recovery}</span></div>
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => void load()} disabled={loading}><RefreshCw size={13} aria-hidden="true" />重试</button>
        </div>
      )}
      <div className="section-scroll">
        <div className="section-inner">
          {loading ? (
            <div className="live-panel-loading b2-task-loading" role="status"><RefreshCw size={16} className="animate-spin" />正在读取 Core Task Projection…</div>
          ) : filteredTasks.length === 0 ? (
            <div className="b2-task-empty"><EmptyState icon={ListTodo} title="暂无 Core 任务" description={tasks.length === 0 ? 'Core 没有返回任务 Projection。Live 不会用演示任务补齐。' : '当前过滤条件没有匹配的 Core 任务。'} /></div>
          ) : (
            <ul className="chat-row-list b2-task-list">
              {filteredTasks.map((task) => (
                <li className="chat-row chat-row-wrap b2-task-row" data-source-type={task.source.source_type} data-status={task.source_status} key={`${task.source.source_type}:${task.source.source_id}`}>
                  <span className="b2-task-identity"><strong className="b2-task-source-type">{SOURCE_LABELS[task.source.source_type]}</strong><span className="b2-task-source-id section-mono">{task.source.source_id}</span></span>
                  <span className="chat-row-main"><span className="chat-row-title">{task.title}</span><span className="b2-task-scope">{task.project_id || '无 Project'} · {task.workspace_id || '无 Workspace'}</span></span>
                  <span className="b2-task-status"><StatusBadge status={task.source_status} label={statusLabel(task.source_status)} size="sm" /></span>
                  <span className="b2-task-actions">
                    {task.actions.map((action) => (
                      <button type="button" className="btn btn-secondary btn-sm b2-task-action" data-action={action.action} data-availability={action.availability} key={action.action} disabled={action.availability !== 'available' || working === task.source.source_id} title={action.availability === 'available' ? undefined : action.reason_code || `Core 未允许${ACTION_LABELS[action.action]}`} onClick={() => void executeAction(task, action)}>
                        {working === task.source.source_id && action.action === 'cancel' ? <RefreshCw size={12} className="animate-spin" /> : null}{ACTION_LABELS[action.action]}{action.action === 'inspect' && <ArrowRight size={12} aria-hidden="true" />}
                      </button>
                    ))}
                  </span>
                  <time className="chat-row-meta" dateTime={task.created_at}>{task.created_at}</time>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
};
