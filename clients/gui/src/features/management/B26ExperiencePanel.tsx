import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { B26Client, type B26State } from '../../../../../sdk/typescript-client/b2_6.generated';
import { B25Client, type GovernanceState } from '../../../../../sdk/typescript-client/b2_5.generated';
import type { ManagementState } from '../../../../../sdk/typescript-client/b2_3.generated';
import { currentBrowserOrigin } from '../../lib/liveBaseUrl';
import { B26Presentation } from './B26Presentation';
import { buildExperienceView } from './b26-view';

export const B26ExperiencePanel: React.FC<{
  management: ManagementState; projectId: string; connectionStatus: string; onMutation?: () => void;
}> = ({ management, projectId, connectionStatus, onMutation }) => {
  const client = useMemo(() => new B26Client(currentBrowserOrigin()), []);
  const gc = useMemo(() => new B25Client(currentBrowserOrigin()), []);
  const [state, setState] = useState<B26State | null>(null);
  const [governance, setGovernance] = useState<GovernanceState | null>(null);
  const [loading, setLoading] = useState(false), [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null), [notice, setNotice] = useState<string | null>(null);
  const [generation, setGeneration] = useState(0);
  const epoch = useRef(0), writing = useRef(false), activeProject = useRef(projectId);
  activeProject.current = projectId;
  const refresh = useCallback(async () => {
    if (!projectId || connectionStatus !== 'connected' || writing.current) return;
    const token = ++epoch.current;
    setLoading(true);
    try {
      await Promise.all([client.negotiateProtocol(), gc.negotiateProtocol()]);
      const [next, knowledge] = await Promise.all([client.getExperience(projectId), gc.getGovernance(projectId)]);
      if (token !== epoch.current || activeProject.current !== projectId) return;
      if (next.project_id !== projectId || knowledge.project_id !== projectId) throw new Error('Core 返回了其他项目的投影。');
      setState(next); setGovernance(knowledge); setGeneration(v => v + 1); setError(null);
      if (next.unresolved_command_ids?.length) setNotice(`待人工核对命令：${next.unresolved_command_ids.join('、')}。请检查当前对象，不要自动重发。`);
    } catch (e) {
      if (token === epoch.current) setError(e instanceof Error ? e.message : '状态读取失败，请检查连接并刷新。');
    } finally { if (token === epoch.current) setLoading(false); }
  }, [client, gc, projectId, connectionStatus]);
  useEffect(() => {
    ++epoch.current; setState(null); setGovernance(null); setError(null); setNotice(null); setGeneration(v => v + 1);
    return () => { ++epoch.current; };
  }, [projectId]);
  useEffect(() => { void refresh(); }, [refresh]);
  const view = useMemo(() => state && governance && state.project_id === projectId
    ? buildExperienceView(state, governance, management, generation) : null,
  [state, governance, management, generation, projectId]);
  const submit = async (id: string, values: Record<string, string>) => {
    if (!view || writing.current || connectionStatus !== 'connected' || error) return;
    const build = view.commands.get(id);
    if (!build) { setNotice('投影已变化，请重新打开操作并核对。'); return; }
    let command;
    try { command = build(values); } catch (e) { setNotice(e instanceof Error ? e.message : '请核对输入。'); return; }
    if (command.project_id !== projectId) { setNotice('项目已变化，请刷新。'); return; }
    writing.current = true; setBusy(true); ++epoch.current;
    try {
      const result = await client.execute(command, { idempotencyKey: crypto.randomUUID() });
      if (activeProject.current !== projectId) return;
      if (result.state.project_id !== projectId) throw new Error('Core 返回了其他项目的结果。');
      setState(result.state); setGeneration(v => v + 1); setNotice(result.message); onMutation?.();
    } catch (e) {
      if (activeProject.current === projectId) setError(`${e instanceof Error ? e.message : '结果未确认'}。请刷新核对；不会自动重发。`);
    } finally { writing.current = false; setBusy(false); }
  };
  if (!projectId) return <p className="b2-memory-warning">请选择项目后查看经验与授权。</p>;
  return <section aria-label="经验与授权"><B26Presentation sections={view?.sections ?? []} loading={loading} busy={busy}
    readOnly={connectionStatus !== 'connected' || !!error || !view}
    error={error ?? (connectionStatus !== 'connected' ? 'Core 已断开；保留内容只读，重连后请刷新。' : null)}
    notice={notice} onRefresh={() => void refresh()} onAction={(id, values) => void submit(id, values)} />
    <p className="section-footnote"><Link to="/remote">查看远程任务的派发与完成结果</Link>；Target 上传的候选仍需在管理中心“知识”页审阅。</p>
  </section>;
};
