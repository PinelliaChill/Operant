import { PathInput } from '../../components/PathInput';
import React, { useEffect, useRef, useState } from 'react';
import type { Phase23, Phase56, Phase56Client } from '@operant/sdk';
import type { MergeRunView, WriterArtifactView, WriterWorkspaceView } from '../../live23/multiwriterAdapter';
import { mergeSources } from './graph-merge-model';

interface Props {
  client: Phase56Client;
  runId: string;
  definition: Phase23.WorkflowDefinition;
  artifacts: WriterArtifactView[];
  workspaces: WriterWorkspaceView[];
  merges: MergeRunView[];
  disabled: boolean;
  onChanged: () => Promise<void>;
  onWarning: (message: string) => void;
}

function isLease(value: unknown): value is Phase56.WriterLease {
  if (!value || typeof value !== 'object') return false;
  const lease = value as Record<string, unknown>;
  return typeof lease.writer_workspace_id === 'string' && typeof lease.owner === 'string'
    && typeof lease.token === 'string' && typeof lease.fencing === 'number'
    && typeof lease.expires_at === 'string';
}

export const GraphMergeControls: React.FC<Props> = ({ client, runId, definition, artifacts, workspaces, merges, disabled, onChanged, onWarning }) => {
  const nodes = definition.nodes.filter((node) => node.node_kind === 'merge');
  const [nodeId, setNodeId] = useState(nodes[0]?.node_id ?? '');
  const [artifactIds, setArtifactIds] = useState<string[]>([]);
  const [targetRef, setTargetRef] = useState('');
  const [mergeId, setMergeId] = useState('');
  const [reviewApproved, setReviewApproved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [leaseCount, setLeaseCount] = useState(0);
  const leases = useRef<Phase56.WriterLease[]>([]);
  const actionKeys = useRef(new Map<string, string>());
  const scopeEpoch = useRef(0);
  const busyRef = useRef(false);

  const releaseHeld = (updateState = true) => {
    const held = leases.current;
    leases.current = [];
    if (updateState) setLeaseCount(0);
    if (!held.length) return;
    void Promise.allSettled(held.map((lease) => client.releaseWriterLease(
      lease.writer_workspace_id, { lease },
      { idempotencyKey: key(`release:${lease.writer_workspace_id}:${lease.fencing}`) },
    ))).then((results) => {
      if (results.some((result) => result.status === 'rejected')) onWarning('部分临时写入授权释放失败，请刷新核对；授权最长 5 分钟后失效。');
    });
  };

  useEffect(() => {
    scopeEpoch.current += 1;
    actionKeys.current.clear();
    setLeaseCount(0); setArtifactIds([]); setMergeId(''); setNodeId(nodes[0]?.node_id ?? '');
    setTargetRef(''); setReviewApproved(false); setError(null);
    return () => { scopeEpoch.current += 1; releaseHeld(false); };
  }, [runId, definition.workflow_id, definition.version]);

  const node = nodes.find((item) => item.node_id === nodeId);
  const knownMerge = merges.find((item) => item.mergeRunId === mergeId);
  const key = (action: string) => {
    const existing = actionKeys.current.get(action);
    if (existing) return existing;
    const created = crypto.randomUUID();
    actionKeys.current.set(action, created);
    return created;
  };
  const perform = async (action: () => Promise<void>) => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true); setError(null);
    try { await action(); }
    catch (caught) { setError(caught instanceof Error ? caught.message : '合并请求失败，请刷新后核对。'); }
    finally { busyRef.current = false; setBusy(false); }
  };
  const sources = () => {
    if (!node) throw new Error('请选择合并步骤。');
    return mergeSources(node, artifactIds, artifacts, workspaces);
  };
  const selectedLeases = (ids: string[]) => {
    const selected = ids.map((id) => leases.current.find((lease) => lease.writer_workspace_id === id));
    if (selected.some((lease) => !lease || Date.parse(lease.expires_at) <= Date.now() + 10_000)) {
      throw new Error('临时写入授权缺失或即将到期，请重新获取后核对。');
    }
    return selected as Phase56.WriterLease[];
  };

  const acquire = () => void perform(async () => {
    const plan = sources();
    if (leases.current.some((lease) => Date.parse(lease.expires_at) > Date.now() + 10_000)) throw new Error('当前临时写入授权仍有效，请先释放。');
    const epoch = scopeEpoch.current;
    const owner = `operant-gui-merge:${crypto.randomUUID()}`;
    const acquired: Phase56.WriterLease[] = [];
    try {
      for (const workspaceId of plan.workspaceIds) {
        const response = await client.acquireWriterLease(workspaceId, { owner, ttl_seconds: 300 }, { idempotencyKey: key(`lease:${owner}:${workspaceId}`) });
        if (!isLease(response)) throw new Error('返回的临时写入授权不完整，请重新获取。');
        acquired.push(response);
        if (epoch !== scopeEpoch.current) throw new Error('已切换运行，正在释放刚取得的临时写入授权。');
      }
    } catch (caught) {
      const releases = await Promise.allSettled(acquired.map((lease) => client.releaseWriterLease(lease.writer_workspace_id, { lease }, { idempotencyKey: key(`release:${lease.writer_workspace_id}:${lease.fencing}`) })));
      if (releases.some((result) => result.status === 'rejected')) onWarning('部分临时写入授权释放失败，请刷新核对；授权最长 5 分钟后失效。');
      throw caught;
    }
    leases.current = acquired;
    setLeaseCount(acquired.length);
  });

  const create = () => void perform(async () => {
    const epoch = scopeEpoch.current;
    const plan = sources();
    if (!node?.merge_policy || !targetRef.startsWith('/')) throw new Error('请选择用于合并的独立文件夹。');
    if (workspaces.some((item) => item.isolationRef === targetRef.trim())) throw new Error('合并目标不能与来源文件夹相同。');
    const action = JSON.stringify(['create', runId, node.node_id, plan.artifactIds, node.merge_policy.strategy, targetRef.trim(), plan.baseRevision]);
    const idempotencyKey = key(action);
    const requestedMergeId = `merge_run_${idempotencyKey.replaceAll('-', '')}`;
    if (merges.some((item) => item.mergeRunId === requestedMergeId)) {
      actionKeys.current.delete(action);
      setMergeId(requestedMergeId);
      return;
    }
    const response = await client.createMergeRun({
      merge_run: {
        merge_run_id: requestedMergeId, graph_run_id: runId, merge_node_id: node.node_id, artifact_ids: plan.artifactIds,
        strategy: node.merge_policy.strategy ?? 'three_way', target_isolation_ref: targetRef.trim(), base_revision: plan.baseRevision,
      },
      leases: selectedLeases(plan.workspaceIds),
    }, { idempotencyKey });
    if (response.merge_run_id !== requestedMergeId) throw new Error('返回的合并记录与请求不符，请刷新后核对。');
    actionKeys.current.delete(action);
    if (epoch !== scopeEpoch.current) return;
    setMergeId(response.merge_run_id);
    await onChanged();
  });

  const finalize = () => void perform(async () => {
    const epoch = scopeEpoch.current;
    if (!mergeId || !knownMerge || !reviewApproved) throw new Error('请先选择合并记录并完成审阅确认。');
    if (!node) throw new Error('请选择合并步骤。');
    const plan = mergeSources(node, knownMerge.artifactIds, artifacts, workspaces);
    if (['running', 'succeeded', 'failed', 'rolled_back', 'outcome_unknown'].includes(knownMerge.status)) throw new Error('该合并当前无法重复执行。');
    await client.finalizeMergeRun(mergeId, { leases: selectedLeases(plan.workspaceIds), review_approved: true }, { idempotencyKey: key(`finalize:${mergeId}`) });
    actionKeys.current.delete(`finalize:${mergeId}`);
    releaseHeld();
    if (epoch !== scopeEpoch.current) return;
    setReviewApproved(false);
    await onChanged();
  });

  if (!nodes.length) return null;
  return <section className="b24-card b24-card-subtle" aria-label="合并操作">
    <h3>合并操作</h3>
    <p className="b24-field-help">选择产物，核对冲突和结果后再完成合并。</p>
    <label className="b24-field"><span className="b24-field-label">合并步骤</span><select value={nodeId} onChange={(event) => { releaseHeld(); setNodeId(event.target.value); setArtifactIds([]); setMergeId(''); }} disabled={disabled || busy}>{nodes.map((item) => <option key={item.node_id} value={item.node_id}>{item.node_id}</option>)}</select></label>
    <fieldset className="b24-fieldset"><legend>来源产物</legend>{artifacts.map((item) => <label className="b24-check-row" key={item.writerArtifactId}><input type="checkbox" checked={artifactIds.includes(item.writerArtifactId)} onChange={(event) => { releaseHeld(); setArtifactIds((current) => event.target.checked ? [...current, item.writerArtifactId] : current.filter((id) => id !== item.writerArtifactId)); }} disabled={disabled || busy} /><span>{item.writerArtifactId} · {item.baseRevision}</span></label>)}</fieldset>
    <PathInput label="合并到独立文件夹" value={targetRef} onChange={setTargetRef} disabled={disabled || busy} />
    <p className="b24-field-help">已取得 {leaseCount} 项临时授权，切换运行时自动释放。</p>
    <div className="b24-actions"><button type="button" className="btn btn-secondary btn-sm" onClick={acquire} disabled={disabled || busy || artifactIds.length < 2}>获取临时授权</button><button type="button" className="btn btn-secondary btn-sm" onClick={() => releaseHeld()} disabled={busy || leaseCount === 0}>释放授权</button><button type="button" className="btn btn-secondary btn-sm" onClick={create} disabled={disabled || busy || leaseCount < 2 || !targetRef.trim() || Boolean(mergeId)}>开始合并</button></div>
    {merges.length > 0 && <label className="b24-field"><span className="b24-field-label">待完成的合并</span><select value={mergeId} onChange={(event) => { releaseHeld(); const selected = merges.find((item) => item.mergeRunId === event.target.value); setMergeId(event.target.value); setArtifactIds(selected?.artifactIds ?? []); setTargetRef(selected?.targetIsolationRef ?? ''); setReviewApproved(false); }} disabled={disabled || busy}><option value="">选择合并记录</option>{merges.filter((item) => item.mergeNodeId === nodeId).map((item) => <option key={item.mergeRunId} value={item.mergeRunId}>{item.mergeRunId} · {item.status}</option>)}</select></label>}
    {knownMerge && <><p>当前状态：{knownMerge.status}{knownMerge.errorCode ? ` · ${knownMerge.errorCode}` : ''}</p><p className="b24-field-help">冻结来源：{knownMerge.artifactIds.join('、')} · 基线：{knownMerge.baseRevision} · 隔离目标：{knownMerge.targetIsolationRef}</p><label className="b24-check-row"><input type="checkbox" checked={reviewApproved} onChange={(event) => setReviewApproved(event.target.checked)} disabled={disabled || busy} /><span>已核对冻结来源、目标、冲突和实际差异，确认执行合并</span></label><button type="button" className="btn btn-primary btn-sm" onClick={finalize} disabled={disabled || busy || !reviewApproved || !['created', 'conflicted', 'review_required'].includes(knownMerge.status)}>完成合并</button></>}
    {error && <p className="b24-error" role="alert">{error}</p>}
  </section>;
};
