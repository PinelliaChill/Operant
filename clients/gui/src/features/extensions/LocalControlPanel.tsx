import React, { useCallback, useEffect, useRef, useState } from 'react';
import { AlertTriangle, Eye, Globe, Hand, Monitor, Play, RefreshCw, Square } from 'lucide-react';
import { useOperant } from '../../context/ClientContext';
import { Modal } from '../../components/Modal';
import { SearchSelect } from '../../components/SearchSelect';
import { useOnboarding } from '../../live/OnboardingContext';
import type { LocalApplication } from '../../../../../sdk/typescript-client/onboarding';
import { localStateLabel } from '../../lib/statusCopy';
import { approvalId, idempotencyKey, LOCAL_EXTENSION_CHANGE_EVENT, optionalText, outcomeNeedsReconciliation, projection, projectionItems, requestCode, requestError, requiredText } from './localProjection';

type Session = {
  sessionId: string;
  pluginId: string;
  targetId: string;
  state: string;
  lastError?: string;
  lastJobId?: string;
  observation?: Record<string, unknown>;
};

type PendingAction = { run: () => Promise<unknown>; after?: (result: Record<string, unknown>) => void; approvalId: string; label: string };
type ExtensionDriver = { name: string; pluginId: string; description: string; parameters?: Record<string, unknown> };
type UnknownJob = { jobId: string; targetId: string; operation: string; pluginId: string };

const operations: Record<string, Array<{ value: string; label: string; hint: string }>> = {
  browser: [
    { value: 'navigate', label: '打开网址', hint: '{"url":"https://example.com"}' },
    { value: 'click', label: '点击元素', hint: '{"selector":"button"}' },
    { value: 'fill', label: '填写输入框', hint: '{"selector":"input","value":"文本"}' },
    { value: 'capture_viewport', label: '页面截图', hint: '{}' },
  ],
  computer: [
    { value: 'click_button', label: '点击按钮', hint: '{"button_name":"确定"}' },
    { value: 'type_text', label: '输入文本', hint: '{"element_name":"搜索框","value":"文本"}' },
    { value: 'press_key', label: '按键', hint: '{"key":"Enter"}' },
    { value: 'capture_window', label: '窗口截图', hint: '{}' },
    { value: 'read_clipboard', label: '读取剪贴板证据', hint: '{}' },
    { value: 'write_clipboard', label: '写入剪贴板', hint: '{"value":"文本"}' },
  ],
};

function mapSession(value: Record<string, unknown>): Session {
  return {
    sessionId: requiredText(value.session_id, 'session_id'),
    pluginId: requiredText(value.plugin_id, 'plugin_id'),
    targetId: requiredText(value.target_id, 'target_id'),
    state: requiredText(value.state, 'state'),
    lastError: optionalText(value.last_error),
    lastJobId: optionalText(value.last_job_id),
    observation: value.observation && typeof value.observation === 'object' ? projection(value.observation, 'observation') : undefined,
  };
}

export const LocalControlPanel: React.FC = () => {
  const { phase56Client, phase45Client, connectionStatus } = useOperant();
  const { client: onboardingClient } = useOnboarding();
  const [localApps, setLocalApps] = useState<LocalApplication[]>([]);
  const [localAppsError, setLocalAppsError] = useState('');
  const [plugins, setPlugins] = useState<Record<string, unknown>[]>([]);
  const [drivers, setDrivers] = useState<ExtensionDriver[]>([]);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [unknownJobs, setUnknownJobs] = useState<UnknownJob[]>([]);
  const [unknownLoaded, setUnknownLoaded] = useState(false);
  const [reviewOutcome, setReviewOutcome] = useState<Record<string, 'applied' | 'not_applied' | ''>>({});
  const [reviewNote, setReviewNote] = useState<Record<string, string>>({});
  const [reviewConfirm, setReviewConfirm] = useState<Record<string, boolean>>({});
  const [pluginId, setPluginId] = useState('');
  const [computerBundleId, setComputerBundleId] = useState('');
  const [websiteUrl, setWebsiteUrl] = useState('');
  const [sessionId, setSessionId] = useState('');
  const [operation, setOperation] = useState('navigate');
  const [argumentsText, setArgumentsText] = useState('{}');
  const [driverName, setDriverName] = useState('');
  const [driverArguments, setDriverArguments] = useState('{}');
  const [jobId, setJobId] = useState('');
  const [job, setJob] = useState<Record<string, unknown>>();
  const [observations, setObservations] = useState<Record<string, Record<string, unknown> | null>>({});
  const [observationTick, setObservationTick] = useState(0);
  const [artifact, setArtifact] = useState<{ mediaType: string; base64: string }>();
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<PendingAction>();
  const [confirmCloseId, setConfirmCloseId] = useState('');
  const disconnected = connectionStatus !== 'connected';
  useEffect(() => {
    if (disconnected) return;
    let active = true;
    void onboardingClient.listLocalApplications().then((page) => { if (active) { setLocalApps(page.items); setLocalAppsError(''); } })
      .catch((reason: unknown) => { if (active) setLocalAppsError(`应用列表读取失败：${requestError(reason)}`); });
    return () => { active = false; };
  }, [disconnected, onboardingClient]);
  useEffect(() => { if (disconnected) setConfirmCloseId(''); }, [disconnected]);
  const selectedPlugin = plugins.find((item) => item.plugin_id === pluginId);
  const allowedTargets = Array.isArray(selectedPlugin?.allowed_targets) ? selectedPlugin.allowed_targets.filter((item): item is string => typeof item === 'string') : [];
  const computerTarget = pluginId === 'operant.macos.computer'
    ? (allowedTargets.length === 1 ? allowedTargets[0] : computerBundleId)
    : undefined;
  const selected = sessions.find((item) => item.sessionId === sessionId);
  const selectedPluginEnabled = Boolean(selected && plugins.some((item) => item.plugin_id === selected.pluginId && item.state === 'enabled'));
  const privateContentAllowed = !disconnected && selected?.state === 'active' && selectedPluginEnabled;
  const privateAccessRef = useRef({ allowed: privateContentAllowed, sessionId, jobId });
  privateAccessRef.current = { allowed: privateContentAllowed, sessionId, jobId };
  useEffect(() => {
    if (privateContentAllowed) return;
    setArgumentsText('{}');
    setDriverArguments('{}');
    setArtifact(undefined);
    setPending(undefined);
  }, [privateContentAllowed]);
  const kind = selected?.pluginId.includes('browser') ? 'browser' : 'computer';
  const availableOperations = operations[kind];
  const effectiveOperation = availableOperations.some((item) => item.value === operation) ? operation : availableOperations[0].value;
  const currentObservation = selected ? (observations[selected.sessionId] === undefined ? selected.observation : observations[selected.sessionId] ?? undefined) : undefined;
  const observationExpiresAt = optionalText(currentObservation?.expires_at);
  const observationHash = observationExpiresAt && Date.parse(observationExpiresAt) > Date.now() ? optionalText(currentObservation?.observation_hash) : undefined;
  useEffect(() => {
    if (!observationExpiresAt) return;
    const remaining = Date.parse(observationExpiresAt) - Date.now();
    if (!Number.isFinite(remaining) || remaining <= 0) return;
    const timer = window.setTimeout(() => setObservationTick((value) => value + 1), remaining + 20);
    return () => window.clearTimeout(timer);
  }, [observationExpiresAt, observationTick]);
  const jobResult = job?.result && typeof job.result === 'object' && !Array.isArray(job.result) ? projection(job.result, '作业详情') : undefined;
  const artifactAvailable = Boolean(optionalText(jobResult?.artifact_ref));

  const refresh = useCallback(async () => {
    setUnknownLoaded(false);
    const [pluginPage, sessionPage, driverPage, unknownPage] = await Promise.all([phase56Client.listLocalCapabilityPlugins(), phase56Client.listLocalControlSessions(), phase56Client.listExtensionDrivers(), phase56Client.listUnknownLocalControlJobs()]);
    setPlugins(projectionItems(pluginPage, '本机驱动'));
    const driverSource = projection(driverPage, '扩展驱动');
    if (!Array.isArray(driverSource.drivers)) throw new Error('扩展驱动列表格式无效。');
    setDrivers(driverSource.drivers.map((raw) => {
      const item = projection(raw, '扩展驱动');
      return { name: requiredText(item.name, 'name'), pluginId: requiredText(item.plugin_id, 'plugin_id'), description: optionalText(item.description) ?? '', parameters: item.parameters && typeof item.parameters === 'object' && !Array.isArray(item.parameters) ? projection(item.parameters, 'parameters') : undefined };
    }));
    const next = projectionItems(sessionPage, '本机会话').map(mapSession);
    setSessions(next);
    const unknown = projection(unknownPage, '结果未知作业');
    if (!Array.isArray(unknown.jobs)) throw new Error('结果未知作业列表格式无效。');
    setUnknownJobs(unknown.jobs.map((raw) => {
      const item = projection(raw, '结果未知作业');
      return { jobId: requiredText(item.job_id, 'job_id'), targetId: requiredText(item.target_id, 'target_id'), operation: requiredText(item.operation, 'operation'), pluginId: requiredText(item.plugin_id, 'plugin_id') };
    }));
    setError('');
    setUnknownLoaded(true);
    setSessionId((current) => current && next.some((item) => item.sessionId === current) ? current : next[0]?.sessionId ?? '');
  }, [phase56Client]);

  useEffect(() => { void refresh().catch((reason: unknown) => setError(requestError(reason))); }, [refresh]);
  useEffect(() => {
    const onChange = () => { void refresh().catch((reason: unknown) => setError(requestError(reason))); };
    window.addEventListener(LOCAL_EXTENSION_CHANGE_EVENT, onChange);
    return () => window.removeEventListener(LOCAL_EXTENSION_CHANGE_EVENT, onChange);
  }, [refresh]);

  const action = async (label: string, run: () => Promise<unknown>, after?: (result: Record<string, unknown>) => void) => {
    if (disconnected || busy) return;
    setBusy(true); setError(''); setNotice(''); setPending(undefined);
    try {
      if (!unknownLoaded && !label.startsWith('人工核对')) throw new Error('本机投影尚未确认，请先刷新。');
      const result = projection(await run(), label);
      after?.(result);
      if (label === '执行操作') setArgumentsText('{}');
      if (label === '扩展驱动操作') setDriverArguments('{}');
      if (['开启会话', '人工接管', '恢复控制', '关闭会话', '执行操作', '扩展驱动操作'].includes(label)) setObservations((current) => ({ ...current, [sessionId]: null }));
      setNotice(`${label}已提交。`);
      await refresh();
    } catch (reason: unknown) {
      const approval = approvalId(reason);
      if (requestCode(reason) === 'approval_required' && approval) {
        setPending({ run, after, approvalId: approval, label });
        setError(`${label}需要人工审批。批准后才会使用原幂等键提交同一操作。`);
      } else {
        if (['观测', '执行操作', '扩展驱动操作'].includes(label)) setObservations((current) => ({ ...current, [sessionId]: null }));
        if (outcomeNeedsReconciliation(reason)) setUnknownLoaded(false);
        setError(`${label}失败：${requestError(reason)} 若结果未知，请先在 运行记录或目标应用核对，勿重复提交。`);
      }
    } finally { setBusy(false); }
  };

  const decide = async (approved: boolean) => {
    if (!pending || busy || disconnected) return;
    setBusy(true); setError('');
    const retry = pending;
    try {
      await phase45Client.decidePhase45Approval(retry.approvalId, { approved });
      setPending(undefined);
      if (approved) {
        const result = projection(await retry.run(), retry.label);
        retry.after?.(result);
        if (retry.label === '执行操作') setArgumentsText('{}');
        if (retry.label === '扩展驱动操作') setDriverArguments('{}');
        if (['开启会话', '人工接管', '恢复控制', '关闭会话', '执行操作', '扩展驱动操作'].includes(retry.label)) setObservations((current) => ({ ...current, [sessionId]: null }));
        setNotice(`${retry.label}已获批准并提交；请查看作业结果。`);
        await refresh();
      } else setNotice('已拒绝操作。');
    } catch (reason: unknown) {
      const nextApproval = approvalId(reason);
      if (approved && requestCode(reason) === 'approval_required' && nextApproval) {
        setPending({ ...retry, approvalId: nextApproval });
        setError(`${retry.label}还需要下一项人工审批。每次批准后仅提交同一幂等请求。`);
      } else {
        if (['观测', '执行操作', '扩展驱动操作'].includes(retry.label)) setObservations((current) => ({ ...current, [sessionId]: null }));
        if (outcomeNeedsReconciliation(reason)) setUnknownLoaded(false);
        setError(`审批或原操作失败：${requestError(reason)} 请人工核对后处理。`);
      }
    }
    finally { setBusy(false); }
  };

  const submitJob = (result: Record<string, unknown>) => {
    const submittedJob = requiredText(result.job_id, 'job_id');
    setJobId(submittedJob); setJob(undefined); setArtifact(undefined);
    setObservations((current) => ({ ...current, [sessionId]: null }));
  };

  const readJob = async () => {
    if (!jobId || busy) return;
    setBusy(true); setError('');
    try {
      const result = projection(await phase56Client.getRemoteTargetJobResult(jobId), '作业结果');
      setJob(result);
      if (selected && result.target_id === selected.targetId && result.status === 'succeeded' && result.observation && typeof result.observation === 'object') {
        setObservations((current) => ({ ...current, [selected.sessionId]: projection(result.observation, '观测结果') }));
      }
    }
    catch (reason: unknown) { setError(`作业结果读取失败：${requestError(reason)}`); }
    finally { setBusy(false); }
  };

  const readArtifact = async () => {
    if (!jobId || busy || !privateContentAllowed) return;
    const requestedSessionId = sessionId;
    const requestedJobId = jobId;
    await action('读取截图或剪贴板', () => phase56Client.getLocalControlArtifact(requestedJobId), (data) => {
      const access = privateAccessRef.current;
      if (!access.allowed || access.sessionId !== requestedSessionId || access.jobId !== requestedJobId) return;
      setArtifact({ mediaType: requiredText(data.media_type, 'media_type'), base64: requiredText(data.base64, 'base64') });
    });
  };

  const act = () => {
    if (!selected || !observationHash) return;
    if (!observationExpiresAt || Date.parse(observationExpiresAt) <= Date.now()) { setObservations((current) => ({ ...current, [selected.sessionId]: null })); setError('观测已过期，请重新观测。'); return; }
    try {
      const args = JSON.parse(argumentsText) as unknown;
      if (!args || typeof args !== 'object' || Array.isArray(args)) throw new Error('参数必须是 JSON 对象。');
      const key = idempotencyKey();
      void action('执行操作', () => phase56Client.actLocalControlSession(selected.sessionId, { operation: effectiveOperation, observation_hash: observationHash, arguments: args as Record<string, unknown>, idempotency_key: key }, { idempotencyKey: key }), submitJob);
    } catch (reason: unknown) { setError(requestError(reason)); }
  };

  const drive = () => {
    if (!selected || !observationHash || !driverName) return;
    if (!observationExpiresAt || Date.parse(observationExpiresAt) <= Date.now()) { setObservations((current) => ({ ...current, [selected.sessionId]: null })); setError('观测已过期，请重新观测。'); return; }
    try {
      const args = JSON.parse(driverArguments) as unknown;
      if (!args || typeof args !== 'object' || Array.isArray(args)) throw new Error('扩展驱动参数必须是 JSON 对象。');
      const key = idempotencyKey();
      void action('扩展驱动操作', () => phase56Client.driveLocalControlSession(selected.sessionId, { driver_name: driverName, observation_hash: observationHash, arguments: args as Record<string, unknown>, idempotency_key: key }, { idempotencyKey: key }), submitJob);
    } catch (reason: unknown) { setError(requestError(reason)); }
  };

  const reconcile = (unknown: UnknownJob) => {
    const observedOutcome = reviewOutcome[unknown.jobId];
    const evidenceNote = reviewNote[unknown.jobId]?.trim() ?? '';
    if (!observedOutcome || evidenceNote.length < 5 || evidenceNote.length > 1000 || !reviewConfirm[unknown.jobId]) return;
    const key = idempotencyKey();
    void action('人工核对未知作业', () => phase56Client.reconcileUnknownLocalControlJob(unknown.jobId, {
      idempotency_key: key, observed_outcome: observedOutcome, evidence_note: evidenceNote,
    }, { idempotencyKey: key }), () => {
      setReviewConfirm((current) => ({ ...current, [unknown.jobId]: false }));
    });
  };

  return <section className="local-panel" aria-labelledby="local-control-title">
    <div className="local-panel-heading"><div><h2 id="local-control-title">本机能力</h2><p>选择要使用的能力，确认目标后开始。</p></div><button type="button" className="btn btn-secondary" onClick={() => void refresh().catch((reason: unknown) => setError(requestError(reason)))} disabled={busy}><RefreshCw size={14} aria-hidden="true" />刷新</button></div>
    <div className="local-capability-grid" aria-label="可用能力">
      {([['operant.macos.computer', '操作本机应用', '选择已授权的应用，在操作前查看当前窗口。', Monitor], ['operant.chrome.browser', '操作网站', '填写网址，在浏览器中查看和操作网页。', Globe]] as const).map(([id, title, description, Icon]) => {
        const item = plugins.find((plugin) => plugin.plugin_id === id);
        const available = item?.state === 'enabled';
        return <article key={id} className={`local-capability-card${pluginId === id ? ' selected' : ''}`}><Icon size={20} aria-hidden="true" /><strong>{title}</strong><span>{description}</span><small>{available ? '已启用' : item ? '未启用' : '尚未安装'}</small><div className="local-capability-actions">{available ? <button type="button" className="btn btn-secondary btn-sm" aria-pressed={pluginId === id} disabled={disconnected} onClick={() => { setPluginId(id); setComputerBundleId(''); }}>选择能力</button> : <a className="btn btn-primary btn-sm" href="#local-driver-setup">{item ? '查看授权并启用' : '安装并授权'}</a>}</div></article>;
      })}
    </div>
    {pluginId === 'operant.chrome.browser' && <label className="local-target-field">网站地址<input className="input" type="url" value={websiteUrl} onChange={(event) => { const next = event.target.value; setWebsiteUrl(next); setOperation('navigate'); setArgumentsText(JSON.stringify({ url: next })); }} placeholder="https://example.com" autoComplete="url" /><small>打开能力后，先观测再提交打开网址操作。</small></label>}
    {disconnected && <p className="live-alert live-alert-error" role="alert">连接已断开，本机操作暂停。恢复连接后请刷新。</p>}
    {error && <p className="live-alert live-alert-error" role="alert"><AlertTriangle size={14} aria-hidden="true" />{error}</p>}
    {notice && <p className="live-alert" role="status">{notice}</p>}
    {pending && <div className="local-approval" role="group" aria-label="本机操作审批"><span>审批 ID：<code>{pending.approvalId}</code></span><div className="local-actions"><button type="button" className="btn btn-primary" disabled={busy || disconnected} onClick={() => void decide(true)}>允许并提交</button><button type="button" className="btn btn-danger" disabled={busy || disconnected} onClick={() => void decide(false)}>拒绝</button></div></div>}
    {unknownJobs.length > 0 && <div className="local-unknown-stack" role="alert" aria-label="待人工核对的结果未知作业"><strong>有 {unknownJobs.length} 项结果未知，新的本机操作已暂停</strong><p>先到目标应用和 Core 作业证据中核对实际结果。这里仅记录人工判断，不重放原作业或修改它的终态。</p>{unknownJobs.map((unknown) => <div className="local-unknown-card" key={unknown.jobId}><div className="local-meta"><span>作业：<code>{unknown.jobId}</code></span><span>驱动：{unknown.pluginId}</span><span>目标：<code>{unknown.targetId}</code></span><span>操作：{unknown.operation}</span></div><label htmlFor={`unknown-outcome-${unknown.jobId}`}>人工核对结果</label><select id={`unknown-outcome-${unknown.jobId}`} className="select" value={reviewOutcome[unknown.jobId] ?? ''} onChange={(event) => { setReviewOutcome((current) => ({ ...current, [unknown.jobId]: event.target.value as 'applied' | 'not_applied' | '' })); setReviewConfirm((current) => ({ ...current, [unknown.jobId]: false })); }}><option value="">选择实际结果</option><option value="applied">已执行</option><option value="not_applied">未执行</option></select><label htmlFor={`unknown-note-${unknown.jobId}`}>核对依据（5–1000 字）</label><textarea id={`unknown-note-${unknown.jobId}`} className="input" minLength={5} maxLength={1000} rows={3} value={reviewNote[unknown.jobId] ?? ''} onChange={(event) => { setReviewNote((current) => ({ ...current, [unknown.jobId]: event.target.value })); setReviewConfirm((current) => ({ ...current, [unknown.jobId]: false })); }} /><label className="local-confirm"><input type="checkbox" checked={reviewConfirm[unknown.jobId] === true} onChange={(event) => setReviewConfirm((current) => ({ ...current, [unknown.jobId]: event.target.checked }))} />我已在目标应用和作业证据中核对，不要求重放原作业</label><button type="button" className="btn btn-primary" disabled={busy || disconnected || !reviewOutcome[unknown.jobId] || (reviewNote[unknown.jobId]?.trim().length ?? 0) < 5 || (reviewNote[unknown.jobId]?.trim().length ?? 0) > 1000 || !reviewConfirm[unknown.jobId]} onClick={() => reconcile(unknown)}>记录人工核对</button></div>)}</div>}
    <div className="local-form-row">{pluginId === 'operant.macos.computer' && <><SearchSelect label="要操作的应用" value={computerTarget ?? ''} onChange={setComputerBundleId} placeholder="选择已授权应用" options={allowedTargets.map((target) => ({ value: target, label: localApps.find((app) => app.bundle_id === target)?.name || target.split('.').pop() || target, detail: target }))} />{localAppsError && <small role="alert">{localAppsError}</small>}<small id="local-computer-target-help">只会激活所选且正在运行的应用。</small></>}<button type="button" className="btn btn-primary" disabled={busy || disconnected || !unknownLoaded || unknownJobs.length > 0 || !pluginId || (pluginId === 'operant.macos.computer' && !computerTarget)} onClick={() => { const key = idempotencyKey(); void action('开启会话', () => phase56Client.openLocalControlSession({ plugin_id: pluginId as 'operant.chrome.browser' | 'operant.macos.computer', ...(computerTarget ? { computer_bundle_id: computerTarget } : {}), idempotency_key: key }, { idempotencyKey: key }), (result) => setSessionId(requiredText(result.session_id, 'session_id'))); }}>开始使用</button></div>
    <div className="local-form-row"><SearchSelect label="正在使用的能力" value={sessionId} onChange={(value) => { setSessionId(value); setJobId(''); setJob(undefined); setArtifact(undefined); setArgumentsText('{}'); setDriverArguments('{}'); setPending(undefined); }} placeholder="选择已开始的能力" options={sessions.filter((item) => item.state !== 'closed').map((item) => ({ value: item.sessionId, label: item.pluginId.includes('browser') ? '操作网站' : '操作本机应用', detail: `${localStateLabel(item.state)} · ${item.sessionId.slice(0, 8)}` }))} /></div>
    {selected && <div className="local-session-card"><div className="local-meta"><span>状态：<strong>{localStateLabel(selected.state)}</strong></span><details><summary>目标详情</summary><code>{selected.targetId}</code></details>{selected.lastError && <span>操作失败：{selected.lastError}。请刷新并核对目标状态。</span>}</div><div className="local-actions">
      <button type="button" className="btn btn-secondary" disabled={busy || disconnected || !unknownLoaded || unknownJobs.length > 0 || selected.state !== 'active'} onClick={() => { const key = idempotencyKey(); void action('观测', () => phase56Client.observeLocalControlSession(sessionId, { idempotency_key: key }, { idempotencyKey: key }), submitJob); }}><Eye size={14} aria-hidden="true" />观测</button>
      {selected.state === 'active' && <button type="button" className="btn btn-secondary" disabled={busy || disconnected} onClick={() => { const key = idempotencyKey(); void action('人工接管', () => phase56Client.takeoverLocalControlSession(sessionId, { idempotency_key: key }, { idempotencyKey: key })); }}><Hand size={14} aria-hidden="true" />人工接管</button>}
      {selected.state === 'human_control' && <button type="button" className="btn btn-secondary" disabled={busy || disconnected} onClick={() => { const key = idempotencyKey(); void action('恢复控制', () => phase56Client.resumeLocalControlSession(sessionId, { idempotency_key: key }, { idempotencyKey: key })); }}><Play size={14} aria-hidden="true" />恢复控制</button>}
      <button type="button" className="btn btn-ghost" disabled={busy || disconnected || selected.state === 'closed'} onClick={() => setConfirmCloseId(selected.sessionId)}><Square size={14} aria-hidden="true" />关闭</button></div>
      {currentObservation && <details><summary>观测详情</summary><pre className="local-evidence">{JSON.stringify(currentObservation, null, 2)}</pre></details>}
      {selected.state === 'human_control' && <p className="local-help">人工已接管。恢复后先重新观测，再执行操作。</p>}
      <details className="local-advanced"><summary>手动操控台与原始参数</summary>
      {selected.state === 'active' && <div className="local-act"><label htmlFor="local-operation">操作</label><select id="local-operation" className="select" value={effectiveOperation} onChange={(event) => { setOperation(event.target.value); setArgumentsText('{}'); }}>{availableOperations.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select><label htmlFor="local-arguments">参数（JSON）</label><textarea id="local-arguments" className="input" rows={3} value={argumentsText} onChange={(event) => setArgumentsText(event.target.value)} aria-describedby="local-argument-help" /><small id="local-argument-help">{availableOperations.find((item) => item.value === effectiveOperation)?.hint ?? availableOperations[0].hint}。编辑和待审批期间暂存于本页；成功提交后清除，不写入本地持久存储。</small><button type="button" className="btn btn-primary" disabled={busy || disconnected || !unknownLoaded || unknownJobs.length > 0 || !observationHash} onClick={act}>执行操作</button>{!observationHash && <small>{currentObservation ? '当前观测已过期，请重新观测。' : '请先观测，取得最新 observation_hash。'}</small>}</div>}
      {selected.state === 'active' && drivers.length > 0 && <div className="local-act"><h3>扩展驱动</h3><label htmlFor="local-driver">已授权驱动</label><select id="local-driver" className="select" value={driverName} onChange={(event) => setDriverName(event.target.value)}><option value="">选择扩展驱动</option>{drivers.map((driver) => <option key={driver.name} value={driver.name}>{driver.pluginId} · {driver.name}</option>)}</select><label htmlFor="local-driver-arguments">驱动参数（JSON 对象）</label><textarea id="local-driver-arguments" className="input" rows={3} value={driverArguments} onChange={(event) => setDriverArguments(event.target.value)} /><small>{drivers.find((driver) => driver.name === driverName)?.description}{driverName && ` 参数 Schema：${JSON.stringify(drivers.find((driver) => driver.name === driverName)?.parameters ?? {})}`}</small><button type="button" className="btn btn-secondary" disabled={busy || disconnected || !unknownLoaded || unknownJobs.length > 0 || !observationHash || !driverName} onClick={drive}>调用扩展驱动</button></div>}
      </details>
    </div>}
    {jobId && <div className="local-job"><div className="local-panel-heading"><h3>作业结果 <code>{jobId}</code></h3><div className="local-actions"><button type="button" className="btn btn-secondary" disabled={busy} onClick={() => void readJob()}>读取结果</button><button type="button" className="btn btn-secondary" disabled={busy || !artifactAvailable || !privateContentAllowed} onClick={() => void readArtifact()}>查看截图或剪贴板</button></div></div>{job && <pre className="local-evidence">{JSON.stringify(job, null, 2)}</pre>}{artifact && artifact.mediaType.startsWith('image/') && <img className="local-artifact" src={`data:${artifact.mediaType};base64,${artifact.base64}`} alt="本机操作截图证据" />}{artifact && artifact.mediaType.startsWith('text/') && <pre className="local-evidence" aria-label="剪贴板读取内容">{new TextDecoder().decode(Uint8Array.from(atob(artifact.base64), (character) => character.charCodeAt(0))).slice(0, 4000)}</pre>}</div>}
    <Modal isOpen={Boolean(confirmCloseId)} portal title="确认关闭本机操控会话" onClose={() => { if (!busy) setConfirmCloseId(''); }} footer={<><button type="button" className="btn btn-secondary" disabled={busy} onClick={() => setConfirmCloseId('')}>取消</button><button type="button" className="btn btn-danger" disabled={busy || disconnected || !sessions.some((item) => item.sessionId === confirmCloseId && item.state !== 'closed')} onClick={() => { if (!confirmCloseId || busy || disconnected) return; const closingId = confirmCloseId; setConfirmCloseId(''); const key = idempotencyKey(); void action('关闭会话', () => phase56Client.closeLocalControlSession(closingId, { idempotency_key: key }, { idempotencyKey: key })); }}>确认关闭</button></>}><p>关闭会话 <code>{confirmCloseId}</code>？关闭后将撤销此处的控制权限。</p></Modal>
  </section>;
};
