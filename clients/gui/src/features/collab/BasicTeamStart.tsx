import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { OnboardingError, type CollaborationStarted, type CollaborationTemplateView } from '../../../../../sdk/typescript-client/onboarding';
import { SearchSelect } from '../../components/SearchSelect';
import { useOperant } from '../../context/ClientContext';
import { useOnboarding } from '../../live/OnboardingContext';
import { useLive } from '../../live/LiveContext';
import { collaborationStatusLabel } from '../../lib/statusCopy';
import { graphRunIsTerminal } from '../../live23/runtimeState';
import { graphRunHref } from './graphRunLink';
import { refreshThenOpenTeamReply } from './teamReplyNavigation';
import './basic-team.css';

interface Props { onAdvanced: () => void }

export const BasicTeamStart: React.FC<Props> = ({ onAdvanced }) => {
  const { client, setup } = useOnboarding();
  const { phase23Client } = useOperant();
  const { refresh: refreshLive } = useLive();
  const navigate = useNavigate();
  const [templates, setTemplates] = useState<CollaborationTemplateView[]>([]);
  const [templateId, setTemplateId] = useState('');
  const [task, setTask] = useState('');
  const [result, setResult] = useState<CollaborationStarted | null>(null);
  const [observedStatus, setObservedStatus] = useState('');
  const [statusError, setStatusError] = useState('');
  const [statusRefresh, setStatusRefresh] = useState(0);
  const [reviewerThreadId, setReviewerThreadId] = useState('');
  const [reviewerError, setReviewerError] = useState('');
  const [reviewerRefresh, setReviewerRefresh] = useState(0);
  const [replyBusy, setReplyBusy] = useState(false);
  const [replyError, setReplyError] = useState('');
  const replyDispatching = useRef(false);
  const activeRunId = useRef<string | undefined>(undefined);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [unknown, setUnknown] = useState(false);
  const dispatching = useRef(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try { const page = await client.listTeamTemplates(); setTemplates(page.items); setTemplateId((id) => page.items.some((item) => item.template_id === id) ? id : page.items[0]?.template_id || ''); setError(''); }
    catch (cause: unknown) { setError(cause instanceof Error ? cause.message : '团队模板读取失败。'); }
    finally { setLoading(false); }
  }, [client]);
  useEffect(() => { void refresh(); }, [refresh]);

  const resultRunId = result?.graph_run_id;
  activeRunId.current = resultRunId;
  useEffect(() => {
    if (!resultRunId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const readStatus = async () => {
      try {
        const run = await phase23Client.getGraphRun(resultRunId);
        if (cancelled) return;
        setObservedStatus(run.status);
        setStatusError('');
        if (!graphRunIsTerminal(run.status)) timer = setTimeout(() => void readStatus(), 3000);
      } catch {
        if (!cancelled) setStatusError('暂时无法更新进度。请刷新状态，或打开运行进度核对。');
      }
    };
    void readStatus();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [phase23Client, resultRunId, statusRefresh]);

  const teamRunId = result?.team_run_id;
  useEffect(() => {
    if (observedStatus !== 'completed' || !resultRunId || !teamRunId) return;
    let cancelled = false;
    void phase23Client.getTeamRun(teamRunId).then((team) => {
      if (cancelled) return;
      if (team.workflow_run_id !== resultRunId) throw new Error('团队运行与当前任务不一致');
      const reviewer = team.roster.find((member) => member.member_id === 'reviewer');
      if (!reviewer?.thread_id) throw new Error('尚未找到审查成员的回复');
      setReviewerThreadId(reviewer.thread_id); setReviewerError('');
    }).catch(() => { if (!cancelled) setReviewerError('暂时无法读取团队回复。请刷新后重试，或查看运行进度。'); });
    return () => { cancelled = true; };
  }, [observedStatus, phase23Client, resultRunId, teamRunId, reviewerRefresh]);

  const start = async () => {
    if (dispatching.current || unknown || !task.trim() || !templateId) return;
    dispatching.current = true; setBusy(true); setError('');
    try {
      const next = await client.startTemplateTeam({ template_id: templateId, task: task.trim(), ...(setup?.default_workspace_id ? { workspace_id: setup.default_workspace_id } : {}) }, { idempotencyKey: crypto.randomUUID() });
      setResult(next); setObservedStatus(next.status); setStatusError(''); setReviewerThreadId(''); setReviewerError(''); setReplyError(''); setReplyBusy(false);
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : '团队启动失败。');
      if (!(cause instanceof OnboardingError) || cause.code === 'transport_unavailable' || cause.recovery === 'manual_reconcile') setUnknown(true);
    } finally { dispatching.current = false; setBusy(false); }
  };

  const openReply = async () => {
    if (!reviewerThreadId || !resultRunId || replyDispatching.current) return;
    const target = reviewerThreadId;
    const runId = resultRunId;
    const route = window.location.href;
    replyDispatching.current = true; setReplyBusy(true); setReplyError('');
    try {
      const outcome = await refreshThenOpenTeamReply(
        refreshLive,
        () => activeRunId.current === runId && window.location.href === route,
        () => navigate(`/chat/${encodeURIComponent(target)}`),
      );
      if (outcome === 'refresh_failed') setReplyError('团队回复暂时无法载入。请检查连接后重试刷新。');
    } finally { replyDispatching.current = false; if (activeRunId.current === runId && window.location.href === route) setReplyBusy(false); }
  };

  return <div className="basic-team">
    <header><h1>协作</h1><p>选择一个团队模板，描述任务，即可开始。</p></header>
    {error && <div className="live-alert live-alert-error" role="alert">{error}</div>}
    {unknown && <div className="live-alert live-alert-warn" role="alert">启动结果尚未确认。请先查看任务列表，避免重复创建。<button type="button" className="btn btn-secondary btn-sm" onClick={() => { setUnknown(false); void refresh(); }}>已核对，刷新模板</button></div>}
    <section className="basic-team-card"><h2>开始团队任务</h2>{loading ? <p role="status">正在读取模板…</p> : templates.length ? <><SearchSelect label="团队模板" value={templateId} onChange={setTemplateId} options={templates.map((item) => ({ value: item.template_id, label: item.name, detail: `${item.member_count} 名成员 · ${item.description}` }))} /><p>{templates.find((item) => item.template_id === templateId)?.description}</p><label>任务内容<textarea className="textarea" rows={4} value={task} onChange={(event) => setTask(event.target.value)} placeholder="描述希望团队完成的工作" /></label><button type="button" className="btn btn-primary" disabled={busy || unknown || !task.trim()} onClick={() => void start()}>{busy ? '正在启动…' : '开始协作'}</button></> : <p>当前没有可用模板。请刷新或打开高级配置。</p>}</section>
    {result && <section className="basic-team-card" role="status"><h2>{collaborationStatusLabel(observedStatus || result.status)}</h2>{statusError && <p role="alert">{statusError}</p>}{reviewerError && <p role="alert">{reviewerError}<button type="button" className="btn btn-ghost btn-sm" onClick={() => setReviewerRefresh((value) => value + 1)}>重试读取</button></p>}{replyError && <p role="alert">{replyError}<button type="button" className="btn btn-secondary btn-sm" onClick={() => void openReply()}>重试载入回复</button></p>}<div className="basic-team-actions">{observedStatus === 'completed' && reviewerThreadId && <button type="button" className="btn btn-primary" disabled={replyBusy} onClick={() => void openReply()}>{replyBusy ? '正在载入回复…' : '查看团队回复'}</button>}<Link className="btn btn-secondary" to={graphRunHref(result.graph_run_id)}>查看运行进度</Link><button type="button" className="btn btn-ghost" onClick={() => setStatusRefresh((value) => value + 1)}>刷新状态</button></div><details><summary>运行详情</summary><code>{result.graph_run_id}</code></details></section>}
    <button type="button" className="btn btn-ghost" onClick={onAdvanced}>高级：编辑团队与流程</button>
  </div>;
};
