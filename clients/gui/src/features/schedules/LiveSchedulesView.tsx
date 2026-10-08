import { PathInput } from '../../components/PathInput';
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
import { requestErrorCopy } from '../../lib/requestErrorCopy';
import {
  mapRunRequest,
  mapRunRequests,
  mapSchedule,
  mapSchedules,
  mapWatchStatus,
  schedulerError,
  type RunRequestStatus,
  type ScheduleStatus,
  type TriggerKind,
  type LiveRunRequest,
  type LiveWatchStatus,
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
  dead_letter: '已失败',
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
  cancel?: () => void;
  busy?: boolean;
}> = ({ request, replay, cancel, busy = false }) => (
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
        <div><dt>定时任务</dt><dd>{request.scheduleId} · v{request.scheduleVersion}</dd></div>
        <div><dt>尝试</dt><dd>{request.attemptCount}/{request.maxAttempts}</dd></div>
        <div><dt>可执行时间</dt><dd>{formatDateTime(request.availableAt)}</dd></div>
        {request.lastErrorCode && <div><dt>最后错误</dt><dd>{request.lastErrorCode}</dd></div>}
        {request.replayOfRequestId && <div><dt>原失败请求</dt><dd>{request.replayOfRequestId}</dd></div>}
      </dl>
    </div>
    {replay && (
      <button type="button" className="btn btn-danger btn-sm" onClick={replay} disabled={busy}>
        <RotateCcw size={13} aria-hidden="true" />
        {busy ? '提交中…' : '重新提交'}
      </button>
    )}
    {cancel && <button type="button" className="btn btn-secondary btn-sm" onClick={cancel} disabled={busy}>取消请求</button>}
  </article>
);

export const LiveSchedulesView: React.FC = () => {
  const { connectionStatus, activeWorkspace, addNotification } = useOperant();
  const client = useMemo(createSchedulerClient, []);
  const keys = useRef(new SchedulerIdempotencyKeys());
  const queryEpoch = useRef(0);
  const [snapshot, setSnapshot] = useState(emptySchedulerSnapshot);
  const [watchStatuses, setWatchStatuses] = useState<Record<string, LiveWatchStatus>>({});
  const [error, setError] = useState<ReturnType<typeof schedulerError> | null>(null);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [replayRequest, setReplayRequest] = useState<LiveRunRequest | null>(null);
  const [replayConfirmed, setReplayConfirmed] = useState(false);
  const [name, setName] = useState('');
  const [triggerKind, setTriggerKind] = useState<TriggerKind>('cron');
  const [hookEventType, setHookEventType] = useState<'application.signal' | 'file.changed' | 'git.head.changed'>('application.signal');
  const [watchPath, setWatchPath] = useState('');
  const [cronExpression, setCronExpression] = useState('0 3 * * *');
  const [timerAt, setTimerAt] = useState(localDateTime);
  const [hookEventIds, setHookEventIds] = useState<Record<string, string>>({});
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
      const schedules = mapSchedules(schedulesValue);
      const watcherEntries = await Promise.all(schedules.filter((item) => item.triggerKind === 'hook' && item.hookEventType !== 'application.signal').map(async (item) => [item.id, mapWatchStatus(await client.getWatchStatus(item.id))] as const));
      if (epoch !== queryEpoch.current) return;
      setWatchStatuses(Object.fromEntries(watcherEntries));
      setSnapshot(finishSchedulerLoad(
        schedules,
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
  const createInputValid = Boolean(name.trim() && workflowId.trim() && activeWorkspace.trim())
    && Number.isSafeInteger(parsedWorkflowVersion)
    && parsedWorkflowVersion >= 1
    && (triggerKind === 'cron' ? Boolean(cronExpression.trim()) : triggerKind === 'timer' ? !Number.isNaN(timerDate.getTime()) : hookEventType === 'application.signal' || (watchPath.startsWith('/') && watchPath !== activeWorkspace.trim()));

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
      addNotification('success', `「${scheduleName}」已进入队列`);
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

  const signalHook = async (scheduleId: string, scheduleName: string) => {
    const eventId = hookEventIds[scheduleId]?.trim();
    if (!eventId) return;
    const action = `hook:${scheduleId}:${eventId}`;
    const key = keys.current.acquire(action);
    setBusyAction(action);
    setError(null);
    try {
      const request = mapRunRequest(await client.signalHook(scheduleId, eventId, key));
      keys.current.release(action);
      setSnapshot((current) => ({ ...current, queue: [request, ...current.queue.filter((item) => item.id !== request.id)] }));
      addNotification('success', `「${scheduleName}」事件 ${eventId} 已接收。`);
      await refresh(true);
    } catch (caught) {
      const nextError = schedulerError(caught);
      if (nextError.recovery === 'use_new_idempotency_key') keys.current.release(action);
      setError(nextError);
    } finally {
      setBusyAction(null);
    }
  };

  const cancelRequest = async (requestId: string) => {
    const action = `cancel:${requestId}`;
    const key = keys.current.acquire(action);
    setBusyAction(action);
    setError(null);
    try {
      const request = mapRunRequest(await client.cancelRunRequest(requestId, key));
      keys.current.release(action);
      setSnapshot((current) => ({ ...current, queue: [request, ...current.queue.filter((item) => item.id !== request.id)] }));
      addNotification('success', `运行请求 ${requestId} 正在取消。`);
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
      const request: ScheduleCreateInput & { watch_path?: string | null } = {
        name: name.trim(),
        trigger_kind: triggerKind,
        cron_expression: triggerKind === 'cron' ? cronExpression.trim() : null,
        timer_at: triggerKind === 'timer' ? timerDate.toISOString() : null,
        hook_event_type: triggerKind === 'hook' ? hookEventType : null,
        watch_path: triggerKind === 'hook' && hookEventType !== 'application.signal' ? watchPath.trim() : null,
        timezone_name: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
        workflow_id: workflowId.trim(),
        workflow_version: parsedWorkflowVersion,
        workflow_input: { workspace_or_target: activeWorkspace.trim() },
      };
      const action = `create:${JSON.stringify(request)}`;
      const key = keys.current.acquire(action);
      const created = mapSchedule(await client.createSchedule(request, key));
      keys.current.release(action);
      setSnapshot((current) => ({ ...current, schedules: replaceSchedule(current.schedules, created) }));
      setCreateOpen(false);
      setName('');
      addNotification('success', `已创建定时任务「${created.name}」`);
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
      addNotification('success', `失败请求 ${requestId} 已重新提交。`);
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
        <h1>正在读取定时任务…</h1>

      </div>
    );
  }

  return (
    <div className="section-view live-scheduler-view">
      <header className="section-header scheduler-header">
        <div>

          <h1 className="section-title">定时任务</h1>
          <p className="section-sub">{snapshot.schedules.length} 个定时任务</p>
        </div>
        <div className="scheduler-header-actions">
          <StatusBadge
            status={snapshot.phase === 'ready' && connectionStatus === 'connected' ? 'connected' : 'disconnected'}
            label={snapshot.phase === 'ready' && connectionStatus === 'connected' ? '已连接' : '连接不可用'}
            size="sm"
          />
          <span className="scheduler-connection-announcement" role="status" aria-live="polite">
            {connectionStatus === 'connected' ? '已连接' : '连接已断开，暂时无法修改定时任务'}
          </span>
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => void refresh()} disabled={snapshot.phase === 'loading'}>
            <RefreshCw size={13} className={snapshot.phase === 'loading' ? 'animate-spin' : undefined} aria-hidden="true" />
            刷新
          </button>
          <button type="button" className="btn btn-primary btn-sm" onClick={() => setCreateOpen(true)} disabled={mutationsDisabled}>
            新建定时任务
          </button>
        </div>
      </header>

      <div className="section-scroll">
        <div className="section-inner scheduler-content">
          {error && (
            <div className="live-alert live-alert-error scheduler-alert" role="alert">
              <AlertTriangle size={16} aria-hidden="true" />
              <div>{requestErrorCopy(error)} {snapshot.stale && '当前内容可能已过期。'}<details><summary>错误详情</summary><pre>{error.code}：{error.message}</pre></details></div>
              <button type="button" className="btn btn-secondary btn-sm" onClick={() => void refresh()}>重试</button>
            </div>
          )}

          <section aria-labelledby="live-schedules-heading">
            <div className="scheduler-section-heading">
              <div><h2 id="live-schedules-heading">定时任务列表</h2></div>
            </div>
            {snapshot.schedules.length === 0 ? (
              <EmptyState icon={CalendarClock} title="暂无定时任务" />
            ) : (
              <div className="scheduler-card-list">
                {snapshot.schedules.map((schedule) => {
                  const statusBusy = busyAction?.startsWith(`status:${schedule.id}:`) ?? false;
                  const triggerBusy = busyAction === `trigger:${schedule.id}`;
                  return (
                    <article className="scheduler-schedule-card" key={schedule.id}>
                      <span className="scheduler-kind-icon" aria-hidden="true">
                        {schedule.triggerKind === 'cron' ? <CalendarClock size={17} /> : schedule.triggerKind === 'timer' ? <Clock size={17} /> : <Play size={17} />}
                      </span>
                      <div className="scheduler-card-body">
                        <div className="scheduler-card-title">
                          <strong>{schedule.name}</strong>
                          <StatusBadge status={schedule.status === 'enabled' ? 'active' : schedule.status} size="sm" />
                        </div>
                        <p className="scheduler-rule">
                          {schedule.triggerKind === 'cron' ? schedule.cronExpression : schedule.triggerKind === 'timer' ? `单次 ${formatDateTime(schedule.timerAt ?? '')}` : schedule.hookEventType === 'file.changed' ? `文件变化 · ${schedule.watchPath}` : schedule.hookEventType === 'git.head.changed' ? `Git 版本变化 · ${schedule.watchPath}` : '本地应用信号 · application.signal'}
                          <span> · {schedule.timezoneName}</span>
                        </p>
                        <p className="scheduler-target">{schedule.workflowId} · 流程 v{schedule.workflowVersion} · 定时任务 v{schedule.version}</p>
                        {watchStatuses[schedule.id] && <p className="scheduler-target" role="status">Watcher：{watchStatuses[schedule.id].errorCode ? `错误 ${watchStatuses[schedule.id].errorCode}` : watchStatuses[schedule.id].initialized ? `已初始化 · generation ${watchStatuses[schedule.id].generation}` : '等待初始化'}{watchStatuses[schedule.id].observedAt ? ` · 观察于 ${formatDateTime(watchStatuses[schedule.id].observedAt ?? '')}` : ''}</p>}
                      </div>
                      <div className="scheduler-card-actions">
                        {schedule.triggerKind === 'hook' && schedule.hookEventType === 'application.signal' ? <>
                          <label className="scheduler-hook-event">事件 ID<input className="input" value={hookEventIds[schedule.id] ?? ''} onChange={(event) => setHookEventIds((current) => ({ ...current, [schedule.id]: event.target.value }))} placeholder="应用事件的稳定 ID" maxLength={200} disabled={mutationsDisabled || schedule.status !== 'enabled'} /></label>
                          <button type="button" className="btn btn-secondary btn-sm" onClick={() => void signalHook(schedule.id, schedule.name)} disabled={mutationsDisabled || schedule.status !== 'enabled' || !hookEventIds[schedule.id]?.trim()}>{busyAction?.startsWith(`hook:${schedule.id}:`) ? '提交中…' : '发送应用信号'}</button>
                        </> : <button
                          type="button"
                          className="btn btn-secondary btn-sm"
                          onClick={() => void triggerNow(schedule.id, schedule.name)}
                          disabled={mutationsDisabled || schedule.status !== 'enabled'}
                        >
                          {triggerBusy ? <Loader2 size={13} className="animate-spin" aria-hidden="true" /> : <Play size={13} aria-hidden="true" />}
                          {triggerBusy ? '提交中…' : '立即运行'}
                        </button>}
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
              <div className="scheduler-section-heading"><div><h2 id="scheduler-queue-heading">运行队列</h2></div><strong>{snapshot.queue.length}</strong></div>
              <div className="scheduler-request-list">
                {snapshot.queue.length === 0 ? <p className="scheduler-inline-empty">队列中暂无运行请求。</p> : snapshot.queue.map((request) => <RequestCard key={request.id} request={request} busy={mutationsDisabled} cancel={['queued', 'leased', 'retry_wait'].includes(request.status) ? () => void cancelRequest(request.id) : undefined} />)}
              </div>
            </section>
            <section aria-labelledby="scheduler-dead-heading">
              <div className="scheduler-section-heading"><div><h2 id="scheduler-dead-heading">失败记录</h2></div><strong>{snapshot.deadLetter.length}</strong></div>
              <div className="scheduler-request-list">
                {snapshot.deadLetter.length === 0 ? <p className="scheduler-inline-empty">暂无失败记录。</p> : snapshot.deadLetter.map((request) => (
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

        </div>
      </div>

      <Modal
        isOpen={createOpen}
        onClose={() => { if (busyAction !== 'create') setCreateOpen(false); }}
        title="新建定时任务"
        footer={<>
          <button type="button" className="btn btn-ghost" onClick={() => setCreateOpen(false)} disabled={busyAction === 'create'}>取消</button>
          <button type="button" className="btn btn-primary" onClick={() => void createSchedule()} disabled={busyAction !== null || !createInputValid}>{busyAction === 'create' ? '创建中…' : '创建定时任务'}</button>
        </>}
      >
        <div className="scheduler-form">
          {error && <div className="scheduler-form-error" role="alert">{requestErrorCopy(error)}<details><summary>错误详情</summary><pre>{error.code}：{error.message}</pre></details></div>}
          <label>定时任务名称<input className="input" value={name} onChange={(event) => setName(event.target.value)} autoFocus /></label>
          <fieldset><legend>触发类型</legend><div className="scheduler-segmented">
            <button type="button" className={`btn btn-sm ${triggerKind === 'cron' ? 'btn-primary' : 'btn-secondary'}`} aria-pressed={triggerKind === 'cron'} onClick={() => setTriggerKind('cron')}>重复执行</button>
            <button type="button" className={`btn btn-sm ${triggerKind === 'timer' ? 'btn-primary' : 'btn-secondary'}`} aria-pressed={triggerKind === 'timer'} onClick={() => setTriggerKind('timer')}>执行一次</button>
            <button type="button" className={`btn btn-sm ${triggerKind === 'hook' ? 'btn-primary' : 'btn-secondary'}`} aria-pressed={triggerKind === 'hook'} onClick={() => setTriggerKind('hook')}>事件触发</button>
          </div></fieldset>
          {triggerKind === 'cron' ? <details><summary>执行规则（高级） · {cronExpression === '0 3 * * *' ? '每天 03:00' : '自定义'}</summary><label>Cron 表达式<input className="input" value={cronExpression} onChange={(event) => setCronExpression(event.target.value)} /></label></details> : triggerKind === 'timer' ? <label>触发时间<input type="datetime-local" className="input" value={timerAt} onChange={(event) => setTimerAt(event.target.value)} /></label> : <><label>事件来源<select className="input" value={hookEventType} onChange={(event) => setHookEventType(event.target.value as typeof hookEventType)}><option value="application.signal">应用信号</option><option value="file.changed">文件变化</option><option value="git.head.changed">Git 版本变化</option></select></label>{hookEventType === 'application.signal' ? <p className="scheduler-form-note">相同事件不会重复触发任务。</p> : <div><PathInput key={hookEventType} label="监控文件或文件夹" kind={hookEventType === 'file.changed' ? 'file' : 'directory'} value={watchPath} onChange={setWatchPath} within={activeWorkspace} placeholder={hookEventType === 'file.changed' ? '选择项目内的文件' : '选择项目内的 Git 仓库文件夹'} disabled={Boolean(busyAction)} /><small className="scheduler-form-note">请选择当前项目内的文件或文件夹。</small></div>}</>}
          <label>已发布流程编号<input className="input" value={workflowId} onChange={(event) => setWorkflowId(event.target.value)} placeholder="填写已发布的流程编号" /></label>
          <details><summary>高级选项</summary><label>流程版本<input type="number" min="1" step="1" className="input" value={workflowVersion} onChange={(event) => setWorkflowVersion(event.target.value)} /></label></details>
          <p className="scheduler-form-note">项目文件夹：{activeWorkspace || '请先在会话页选择项目'}</p>
        </div>
      </Modal>

      <Modal
        isOpen={replayRequest !== null}
        onClose={() => { if (!busyAction?.startsWith('replay:')) { setReplayRequest(null); setReplayConfirmed(false); } }}
        title="确认重新提交"
        footer={<>
          <button type="button" className="btn btn-ghost" onClick={() => { setReplayRequest(null); setReplayConfirmed(false); }} disabled={busyAction?.startsWith('replay:')}>取消</button>
          <button type="button" className="btn btn-danger" onClick={() => void replay()} disabled={!replayConfirmed || mutationsDisabled}>{busyAction?.startsWith('replay:') ? '提交中…' : '确认重新提交'}</button>
        </>}
      >
        <div className="scheduler-replay-confirm">
          {error && <div className="scheduler-form-error" role="alert">{requestErrorCopy(error)}<details><summary>错误详情</summary><pre>{error.code}：{error.message}</pre></details></div>}
          <div className="scheduler-danger-copy" role="alert"><AlertTriangle size={18} aria-hidden="true" /><p>重新提交会创建新的运行请求，可能再次修改文件或执行外部操作。原失败记录会保留。</p></div>
          <dl className="scheduler-meta"><div><dt>请求</dt><dd>{replayRequest?.id}</dd></div><div><dt>流程</dt><dd>{replayRequest?.workflowId} · v{replayRequest?.workflowVersion}</dd></div><div><dt>最后错误</dt><dd>{replayRequest?.lastErrorCode ?? '未提供'}</dd></div></dl>
          <label className="scheduler-confirm-check"><input type="checkbox" checked={replayConfirmed} onChange={(event) => setReplayConfirmed(event.target.checked)} />我已核对该请求，并确认重新提交。</label>
        </div>
      </Modal>
    </div>
  );
};
