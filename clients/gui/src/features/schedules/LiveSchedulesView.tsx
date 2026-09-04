import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  CalendarClock,
  Clock,
  Loader2,
  Pause,
  Play,
  RefreshCw,
  RotateCcw,
} from 'lucide-react';
import { EmptyState } from '../../components/EmptyState';
import { Modal } from '../../components/Modal';
import { StatusBadge } from '../../components/StatusBadge';
import { useOperant } from '../../context/ClientContext';
import { formatDateTime } from '../../lib/format';
import {
  mapRunRequest,
  mapRunRequests,
  mapSchedule,
  mapSchedules,
  schedulerError,
  type RunRequestStatus,
  type ScheduleStatus,
  type TriggerKind,
  type LiveRunRequest,
} from '../../live45/schedulerAdapter';
import { createSchedulerClient } from '../../live45/createSchedulerClient';
import type { ScheduleCreateInput } from '../../live45/schedulerClient';
import {
  SchedulerIdempotencyKeys,
  beginSchedulerLoad,
  emptySchedulerSnapshot,
  failSchedulerLoad,
  finishSchedulerLoad,
  replaceSchedule,
} from '../../live45/schedulerState';
import './live-schedules.css';

const REQUEST_LABELS: Record<RunRequestStatus, string> = {
  queued: '排队中',
  leased: '执行中',
  retry_wait: '等待重试',
  succeeded: '已完成',
  cancelled: '已取消',
  dead_letter: 'Dead Letter',
  manual_reconcile_required: '需人工核对',
};

function localDateTime(minutesFromNow = 60): string {
  const date = new Date(Date.now() + minutesFromNow * 60_000);
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

function requestBadgeStatus(status: RunRequestStatus): string {
  if (status === 'succeeded') return 'succeeded';
  if (status === 'leased') return 'running';
  if (status === 'dead_letter' || status === 'manual_reconcile_required') return 'failed';
  if (status === 'cancelled') return 'cancelled';
  return 'pending';
}

const RequestCard: React.FC<{
  request: LiveRunRequest;
  replay?: () => void;
  busy?: boolean;
}> = ({ request, replay, busy = false }) => (
  <article className="scheduler-request-card">
    <div className="scheduler-request-main">
      <div className="scheduler-card-title">
        <strong>{request.workflowId}</strong>
        <StatusBadge
          status={requestBadgeStatus(request.status)}
          label={REQUEST_LABELS[request.status]}
          size="sm"
        />
      </div>
      <dl className="scheduler-meta">
        <div><dt>请求</dt><dd>{request.id}</dd></div>
        <div><dt>调度</dt><dd>{request.scheduleId} · v{request.scheduleVersion}</dd></div>
        <div><dt>尝试</dt><dd>{request.attemptCount}/{request.maxAttempts}</dd></div>
        <div><dt>可执行时间</dt><dd>{formatDateTime(request.availableAt)}</dd></div>
        {request.lastErrorCode && <div><dt>最后错误</dt><dd>{request.lastErrorCode}</dd></div>}
        {request.replayOfRequestId && <div><dt>Replay 来源</dt><dd>{request.replayOfRequestId}</dd></div>}
      </dl>
    </div>
    {replay && (
      <button type="button" className="btn btn-danger btn-sm" onClick={replay} disabled={busy}>
        <RotateCcw size={13} aria-hidden="true" />
        {busy ? '提交中…' : '显式 Replay'}
      </button>
    )}
  </article>
);

export const LiveSchedulesView: React.FC = () => {
  const { connectionStatus, addNotification } = useOperant();
  const client = useMemo(createSchedulerClient, []);
  const keys = useRef(new SchedulerIdempotencyKeys());
  const queryEpoch = useRef(0);
  const [snapshot, setSnapshot] = useState(emptySchedulerSnapshot);
  const [error, setError] = useState<ReturnType<typeof schedulerError> | null>(null);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [replayRequest, setReplayRequest] = useState<LiveRunRequest | null>(null);
  const [replayConfirmed, setReplayConfirmed] = useState(false);
  const [name, setName] = useState('');
  const [triggerKind, setTriggerKind] = useState<TriggerKind>('cron');
  const [cronExpression, setCronExpression] = useState('0 3 * * *');
  const [timerAt, setTimerAt] = useState(localDateTime);
  const [workflowId, setWorkflowId] = useState('');
  const [workflowVersion, setWorkflowVersion] = useState('1');

  const refresh = useCallback(async (quiet = false) => {
    const epoch = ++queryEpoch.current;
    if (!quiet) setSnapshot((current) => beginSchedulerLoad(current));
    setError(null);
    try {
      await client.negotiateProtocol();
      const [schedulesValue, queueValue, deadLetterValue] = await Promise.all([
        client.listSchedules(),
        client.listQueue(),
        client.listDeadLetter(),
      ]);
      if (epoch !== queryEpoch.current) return;
      setSnapshot(finishSchedulerLoad(
        mapSchedules(schedulesValue),
        mapRunRequests(queueValue),
        mapRunRequests(deadLetterValue),
      ));
    } catch (caught) {
      if (epoch !== queryEpoch.current) return;
      setSnapshot((current) => failSchedulerLoad(current));
      setError(schedulerError(caught));
    }
  }, [client]);

  useEffect(() => {
    void refresh();
    const interval = window.setInterval(() => void refresh(true), 15_000);
    return () => {
      queryEpoch.current += 1;
      window.clearInterval(interval);
    };
  }, [refresh]);

  const mutationsDisabled = connectionStatus === 'disconnected'
    || connectionStatus === 'reconnecting'
    || snapshot.phase !== 'ready'
    || busyAction !== null;
  const parsedWorkflowVersion = Number(workflowVersion);
  const timerDate = new Date(timerAt);
  const createInputValid = Boolean(name.trim() && workflowId.trim())
    && Number.isSafeInteger(parsedWorkflowVersion)
    && parsedWorkflowVersion >= 1
    && (triggerKind === 'cron' ? Boolean(cronExpression.trim()) : !Number.isNaN(timerDate.getTime()));

  const updateStatus = async (scheduleId: string, version: number, status: ScheduleStatus) => {
    const action = `status:${scheduleId}:v${version}:${status}`;
    const key = keys.current.acquire(action);
    setBusyAction(action);
    setError(null);
    try {
      const value = await client.setScheduleStatus(
        scheduleId,
        { status, expected_version: version },
        key,
      );
      keys.current.release(action);
      const updated = mapSchedule(value);
      setSnapshot((current) => ({
        ...current,
        schedules: replaceSchedule(current.schedules, updated),
      }));
      addNotification('success', status === 'paused' ? `已暂停「${updated.name}」` : `已恢复「${updated.name}」`);
      await refresh(true);
    } catch (caught) {
      const nextError = schedulerError(caught);
      if (nextError.recovery === 'use_new_idempotency_key') keys.current.release(action);
      setError(nextError);
    } finally {
      setBusyAction(null);
    }
  };

  const triggerNow = async (scheduleId: string, scheduleName: string) => {
    const action = `trigger:${scheduleId}`;
    const key = keys.current.acquire(action);
    setBusyAction(action);
    setError(null);
    try {
      const request = mapRunRequest(await client.triggerSchedule(scheduleId, key));
      keys.current.release(action);
      addNotification('success', `「${scheduleName}」已进入队列；重复点击已由幂等键保护`);
      setSnapshot((current) => ({ ...current, queue: [request, ...current.queue.filter((item) => item.id !== request.id)] }));
      await refresh(true);
    } catch (caught) {
      const nextError = schedulerError(caught);
      if (nextError.recovery === 'use_new_idempotency_key') keys.current.release(action);
      setError(nextError);
    } finally {
      setBusyAction(null);
    }
  };

  const createSchedule = async () => {
    if (!createInputValid) return;
    const action = 'create';
    setBusyAction(action);
    setError(null);
    try {
      const request: ScheduleCreateInput = {
        name: name.trim(),
        trigger_kind: triggerKind,
        cron_expression: triggerKind === 'cron' ? cronExpression.trim() : null,
        timer_at: triggerKind === 'timer' ? timerDate.toISOString() : null,
        timezone_name: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
        workflow_id: workflowId.trim(),
        workflow_version: parsedWorkflowVersion,
      };
      const action = `create:${JSON.stringify(request)}`;
      const key = keys.current.acquire(action);
      const created = mapSchedule(await client.createSchedule(request, key));
      keys.current.release(action);
      setSnapshot((current) => ({ ...current, schedules: replaceSchedule(current.schedules, created) }));
      setCreateOpen(false);
      setName('');
      addNotification('success', `已创建调度「${created.name}」`);
      await refresh(true);
    } catch (caught) {
      const nextError = schedulerError(caught);
      if (nextError.recovery === 'use_new_idempotency_key') {
        for (const action of keys.current.actions('create:')) keys.current.release(action);
      }
      setError(nextError);
    } finally {
      setBusyAction(null);
    }
  };

  const replay = async () => {
    if (!replayRequest || !replayConfirmed) return;
    const requestId = replayRequest.id;
    const action = `replay:${requestId}`;
    const key = keys.current.acquire(action);
    setBusyAction(action);
    setError(null);
    try {
      const queued = mapRunRequest(await client.replayDeadLetter(requestId, key));
      keys.current.release(action);
      setReplayRequest(null);
      setReplayConfirmed(false);
      setSnapshot((current) => ({
        ...current,
        queue: [queued, ...current.queue.filter((item) => item.id !== queued.id)],
      }));
      addNotification('success', `Dead Letter ${requestId} 已显式 Replay；重复提交已由幂等键保护`);
      await refresh(true);
    } catch (caught) {
      const nextError = schedulerError(caught);
      if (nextError.recovery === 'use_new_idempotency_key') keys.current.release(action);
      setError(nextError);
    } finally {
      setBusyAction(null);
    }
  };

  const firstLoad = snapshot.phase === 'loading' && snapshot.schedules.length === 0
    && snapshot.queue.length === 0 && snapshot.deadLetter.length === 0;
  if (firstLoad) {
    return (
      <div className="live-route-state" role="status">
        <div className="live-route-state-icon"><Loader2 size={22} className="animate-spin" aria-hidden="true" /></div>
        <h1>正在读取 Scheduler Projection…</h1>
        <p>Live 模式只展示本地 Core 的 phase45.v1 数据，不会混入演示调度。</p>
      </div>
    );
  }

  return (
    <div className="section-view live-scheduler-view">
      <header className="section-header scheduler-header">
        <div>
          <span className="live-kicker"><span className="live-kicker-dot" aria-hidden="true" />实时 Core · phase45.v1</span>
          <h1 className="section-title">调度中心</h1>
          <p className="section-sub">单 Core 本地 Scheduler · {snapshot.schedules.length} 个调度</p>
        </div>
        <div className="scheduler-header-actions">
          <StatusBadge
            status={snapshot.phase === 'ready' && connectionStatus === 'connected' ? 'connected' : 'disconnected'}
            label={snapshot.phase === 'ready' && connectionStatus === 'connected' ? 'Core 已连接' : '连接不可用'}
            size="sm"
          />
          <span className="scheduler-connection-announcement" role="status" aria-live="polite">
            {connectionStatus === 'connected' ? 'Core 已连接' : 'Core 连接已断开，调度修改暂不可用'}
          </span>
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => void refresh()} disabled={snapshot.phase === 'loading'}>
            <RefreshCw size={13} className={snapshot.phase === 'loading' ? 'animate-spin' : undefined} aria-hidden="true" />
            刷新
          </button>
          <button type="button" className="btn btn-primary btn-sm" onClick={() => setCreateOpen(true)} disabled={mutationsDisabled}>
            新建调度
          </button>
        </div>
      </header>

      <div className="section-scroll">
        <div className="section-inner scheduler-content">
          {error && (
            <div className="live-alert live-alert-error scheduler-alert" role="alert">
              <AlertTriangle size={16} aria-hidden="true" />
              <span><strong>{error.code}</strong>：{error.message} {snapshot.stale && '当前内容可能已过期。'}</span>
              <button type="button" className="btn btn-secondary btn-sm" onClick={() => void refresh()}>重试</button>
            </div>
          )}

          <section aria-labelledby="live-schedules-heading">
            <div className="scheduler-section-heading">
              <div><h2 id="live-schedules-heading">Schedules</h2><p>暂停/恢复使用版本检查；手动触发使用一次一键一幂等键。</p></div>
            </div>
            {snapshot.schedules.length === 0 ? (
              <EmptyState icon={CalendarClock} title="暂无服务端调度" description="Core 尚未返回 ScheduleDefinition。可创建一个绑定已发布 Workflow 的调度。" />
            ) : (
              <div className="scheduler-card-list">
                {snapshot.schedules.map((schedule) => {
                  const statusBusy = busyAction?.startsWith(`status:${schedule.id}:`) ?? false;
                  const triggerBusy = busyAction === `trigger:${schedule.id}`;
                  return (
                    <article className="scheduler-schedule-card" key={schedule.id}>
                      <span className="scheduler-kind-icon" aria-hidden="true">
                        {schedule.triggerKind === 'cron' ? <CalendarClock size={17} /> : <Clock size={17} />}
                      </span>
                      <div className="scheduler-card-body">
                        <div className="scheduler-card-title">
                          <strong>{schedule.name}</strong>
                          <StatusBadge status={schedule.status === 'enabled' ? 'active' : schedule.status} size="sm" />
                        </div>
                        <p className="scheduler-rule">
                          {schedule.triggerKind === 'cron' ? schedule.cronExpression : `单次 ${formatDateTime(schedule.timerAt ?? '')}`}
                          <span> · {schedule.timezoneName}</span>
                        </p>
                        <p className="scheduler-target">{schedule.workflowId} · Workflow v{schedule.workflowVersion} · 调度 v{schedule.version}</p>
                      </div>
                      <div className="scheduler-card-actions">
                        <button
                          type="button"
                          className="btn btn-secondary btn-sm"
                          onClick={() => void triggerNow(schedule.id, schedule.name)}
                          disabled={mutationsDisabled || schedule.status !== 'enabled'}
                        >
                          {triggerBusy ? <Loader2 size={13} className="animate-spin" aria-hidden="true" /> : <Play size={13} aria-hidden="true" />}
                          {triggerBusy ? '提交中…' : '立即运行'}
                        </button>
                        {schedule.status !== 'cancelled' && (
                          <button
                            type="button"
                            className="btn btn-ghost btn-sm"
                            onClick={() => void updateStatus(schedule.id, schedule.version, schedule.status === 'enabled' ? 'paused' : 'enabled')}
                            disabled={mutationsDisabled}
                          >
                            {statusBusy ? <Loader2 size={13} className="animate-spin" aria-hidden="true" /> : schedule.status === 'enabled' ? <Pause size={13} aria-hidden="true" /> : <Play size={13} aria-hidden="true" />}
                            {statusBusy ? '保存中…' : schedule.status === 'enabled' ? '暂停' : '恢复'}
                          </button>
                        )}
                      </div>
                    </article>
                  );
                })}
              </div>
            )}
          </section>

          <div className="scheduler-columns">
            <section aria-labelledby="scheduler-queue-heading">
              <div className="scheduler-section-heading"><div><h2 id="scheduler-queue-heading">运行请求队列</h2><p>显示已提交请求的权威状态，不推断后台仍在运行。</p></div><strong>{snapshot.queue.length}</strong></div>
              <div className="scheduler-request-list">
                {snapshot.queue.length === 0 ? <p className="scheduler-inline-empty">队列中暂无运行请求。</p> : snapshot.queue.map((request) => <RequestCard key={request.id} request={request} />)}
              </div>
            </section>
            <section aria-labelledby="scheduler-dead-heading">
              <div className="scheduler-section-heading"><div><h2 id="scheduler-dead-heading">Dead Letter</h2><p>只有人工确认后才创建新的 replay 请求；原记录不会被覆盖。</p></div><strong>{snapshot.deadLetter.length}</strong></div>
              <div className="scheduler-request-list">
                {snapshot.deadLetter.length === 0 ? <p className="scheduler-inline-empty">暂无 Dead Letter。</p> : snapshot.deadLetter.map((request) => (
                  <RequestCard
                    key={request.id}
                    request={request}
                    busy={mutationsDisabled}
                    replay={() => { setReplayRequest(request); setReplayConfirmed(false); }}
                  />
                ))}
              </div>
            </section>
          </div>
          <p className="section-footnote">本页面只连接本地单 Core、单 Scheduler Leader 与单 Runtime Writer；不表示 Remote、Relay、OAuth 或多 Writer 已实现。</p>
        </div>
      </div>

      <Modal
        isOpen={createOpen}
        onClose={() => { if (busyAction !== 'create') setCreateOpen(false); }}
        title="新建服务端调度"
        footer={<>
          <button type="button" className="btn btn-ghost" onClick={() => setCreateOpen(false)} disabled={busyAction === 'create'}>取消</button>
          <button type="button" className="btn btn-primary" onClick={() => void createSchedule()} disabled={busyAction !== null || !createInputValid}>{busyAction === 'create' ? '创建中…' : '创建调度'}</button>
        </>}
      >
        <div className="scheduler-form">
          {error && <div className="scheduler-form-error" role="alert">{error.code}：{error.message}</div>}
          <label>调度名称<input className="input" value={name} onChange={(event) => setName(event.target.value)} autoFocus /></label>
          <fieldset><legend>触发类型</legend><div className="scheduler-segmented">
            <button type="button" className={`btn btn-sm ${triggerKind === 'cron' ? 'btn-primary' : 'btn-secondary'}`} aria-pressed={triggerKind === 'cron'} onClick={() => setTriggerKind('cron')}>Cron</button>
            <button type="button" className={`btn btn-sm ${triggerKind === 'timer' ? 'btn-primary' : 'btn-secondary'}`} aria-pressed={triggerKind === 'timer'} onClick={() => setTriggerKind('timer')}>单次 Timer</button>
          </div></fieldset>
          {triggerKind === 'cron' ? <label>Cron 表达式<input className="input" value={cronExpression} onChange={(event) => setCronExpression(event.target.value)} /></label> : <label>触发时间<input type="datetime-local" className="input" value={timerAt} onChange={(event) => setTimerAt(event.target.value)} /></label>}
          <label>已发布 Workflow ID<input className="input" value={workflowId} onChange={(event) => setWorkflowId(event.target.value)} placeholder="例如 builtin.coding-review" /></label>
          <label>Workflow 版本<input type="number" min="1" step="1" className="input" value={workflowVersion} onChange={(event) => setWorkflowVersion(event.target.value)} /></label>
          <p className="scheduler-form-note">live 契约只接受已发布 Workflow；Prompt 和系统巡检仍仅保留在演示模式。</p>
        </div>
      </Modal>

      <Modal
        isOpen={replayRequest !== null}
        onClose={() => { if (!busyAction?.startsWith('replay:')) { setReplayRequest(null); setReplayConfirmed(false); } }}
        title="确认显式 Replay"
        footer={<>
          <button type="button" className="btn btn-ghost" onClick={() => { setReplayRequest(null); setReplayConfirmed(false); }} disabled={busyAction?.startsWith('replay:')}>取消</button>
          <button type="button" className="btn btn-danger" onClick={() => void replay()} disabled={!replayConfirmed || mutationsDisabled}>{busyAction?.startsWith('replay:') ? '提交中…' : '确认创建 Replay 请求'}</button>
        </>}
      >
        <div className="scheduler-replay-confirm">
          {error && <div className="scheduler-form-error" role="alert">{error.code}：{error.message}</div>}
          <div className="scheduler-danger-copy" role="alert"><AlertTriangle size={18} aria-hidden="true" /><p>Replay 会创建一个新的运行请求，可能再次执行 Workflow 副作用。原 Dead Letter 会保留，不会被覆盖。</p></div>
          <dl className="scheduler-meta"><div><dt>请求</dt><dd>{replayRequest?.id}</dd></div><div><dt>Workflow</dt><dd>{replayRequest?.workflowId} · v{replayRequest?.workflowVersion}</dd></div><div><dt>最后错误</dt><dd>{replayRequest?.lastErrorCode ?? '未提供'}</dd></div></dl>
          <label className="scheduler-confirm-check"><input type="checkbox" checked={replayConfirmed} onChange={(event) => setReplayConfirmed(event.target.checked)} />我已核对该请求，并确认创建一次显式 Replay。</label>
        </div>
      </Modal>
    </div>
  );
};
