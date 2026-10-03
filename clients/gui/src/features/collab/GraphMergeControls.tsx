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
      if (results.some((result) => result.status === 'rejected')) onWarning('部分 Writer Lease 释放失败；请回读 Core，Lease 最多 5 分钟过期。');
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
    catch (caught) { setError(caught instanceof Error ? caught.message : 'Merge 请求失败，请刷新 Core 投影。'); }
    finally { busyRef.current = false; setBusy(false); }
  };
  const sources = () => {
    if (!node) throw new Error('请选择 Merge 节点。');
    return mergeSources(node, artifactIds, artifacts, workspaces);
  };
  const selectedLeases = (ids: string[]) => {
    const selected = ids.map((id) => leases.current.find((lease) => lease.writer_workspace_id === id));
    if (selected.some((lease) => !lease || Date.parse(lease.expires_at) <= Date.now() + 10_000)) {
      throw new Error('一次性 Writer Lease 缺失或即将到期，请重新获取后核对。');
    }
    return selected as Phase56.WriterLease[];
  };

  const acquire = () => void perform(async () => {
    const plan = sources();
    if (leases.current.some((lease) => Date.parse(lease.expires_at) > Date.now() + 10_000)) throw new Error('当前 Lease 仍有效，请先释放。');
    const epoch = scopeEpoch.current;
    const owner = `operant-gui-merge:${crypto.randomUUID()}`;
    const acquired: Phase56.WriterLease[] = [];
    try {
      for (const workspaceId of plan.workspaceIds) {
        const response = await client.acquireWriterLease(workspaceId, { owner, ttl_seconds: 300 }, { idempotencyKey: key(`lease:${owner}:${workspaceId}`) });
        if (!isLease(response)) throw new Error('Core 返回的 Writer Lease 不完整。');
        acquired.push(response);
        if (epoch !== scopeEpoch.current) throw new Error('已切换 Graph Run；新取得的 Lease 正在释放。');
      }
    } catch (caught) {
      const releases = await Promise.allSettled(acquired.map((lease) => client.releaseWriterLease(lease.writer_workspace_id, { lease }, { idempotencyKey: key(`release:${lease.writer_workspace_id}:${lease.fencing}`) })));
      if (releases.some((result) => result.status === 'rejected')) onWarning('部分 Writer Lease 释放失败；请回读 Core，Lease 最多 5 分钟过期。');
      throw caught;
    }
    leases.current = acquired;
    setLeaseCount(acquired.length);
  });

  const create = () => void perform(async () => {
    const epoch = scopeEpoch.current;
    const plan = sources();
    if (!node?.merge_policy || !targetRef.startsWith('/')) throw new Error('请填写隔离目标的绝对路径。');
    if (workspaces.some((item) => item.isolationRef === targetRef.trim())) throw new Error('合并目标不能是来源 Writer Workspace。');
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
    if (response.merge_run_id !== requestedMergeId) throw new Error('Core 返回的 MergeRun ID 与请求不符，请刷新投影核对。');
    actionKeys.current.delete(action);
    if (epoch !== scopeEpoch.current) return;
    setMergeId(response.merge_run_id);
    await onChanged();
  });

  const finalize = () => void perform(async () => {
    const epoch = scopeEpoch.current;
    if (!mergeId || !knownMerge || !reviewApproved) throw new Error('请先核对 MergeRun 和审阅确认。');
    if (!node) throw new Error('请选择 Merge 节点。');
    const plan = mergeSources(node, knownMerge.artifactIds, artifacts, workspaces);
    if (['running', 'succeeded', 'failed', 'rolled_back', 'outcome_unknown'].includes(knownMerge.status)) throw new Error('该 MergeRun 状态不允许再次 Finalize。');
    await client.finalizeMergeRun(mergeId, { leases: selectedLeases(plan.workspaceIds), review_approved: true }, { idempotencyKey: key(`finalize:${mergeId}`) });
    actionKeys.current.delete(`finalize:${mergeId}`);
    releaseHeld();
    if (epoch !== scopeEpoch.current) return;
    setReviewApproved(false);
    await onChanged();
  });

  if (!nodes.length) return null;
  return <section className="b24-card b24-card-subtle" aria-label="Merge 节点操作">
    <h3>Merge 节点操作</h3>
    <p className="b24-field-help">选择冻结的 Writer 工件，获取一次性 Lease，在隔离目标创建 MergeRun；核对状态后再 Finalize。Core 裁决权限、冲突和未知结果。</p>
    <label className="b24-field"><span className="b24-field-label">Merge 节点</span><select value={nodeId} onChange={(event) => { releaseHeld(); setNodeId(event.target.value); setArtifactIds([]); setMergeId(''); }} disabled={disabled || busy}>{nodes.map((item) => <option key={item.node_id} value={item.node_id}>{item.node_id}</option>)}</select></label>
    <fieldset className="b24-fieldset"><legend>来源工件</legend>{artifacts.map((item) => <label className="b24-check-row" key={item.writerArtifactId}><input type="checkbox" checked={artifactIds.includes(item.writerArtifactId)} onChange={(event) => { releaseHeld(); setArtifactIds((current) => event.target.checked ? [...current, item.writerArtifactId] : current.filter((id) => id !== item.writerArtifactId)); }} disabled={disabled || busy} /><span>{item.writerArtifactId} · {item.baseRevision}</span></label>)}</fieldset>
    <label className="b24-field"><span className="b24-field-label">隔离合并目标绝对路径</span><input value={targetRef} onChange={(event) => setTargetRef(event.target.value)} disabled={disabled || busy} /></label>
    <p className="b24-field-help">已取得 {leaseCount} 个一次性 Lease；令牌不会显示，切换运行时会释放并清除。活跃 Lease 不会被接管。</p>
    <div className="b24-actions"><button type="button" className="btn btn-secondary btn-sm" onClick={acquire} disabled={disabled || busy || artifactIds.length < 2}>获取一次性 Lease</button><button type="button" className="btn btn-secondary btn-sm" onClick={() => releaseHeld()} disabled={busy || leaseCount === 0}>释放 Lease</button><button type="button" className="btn btn-secondary btn-sm" onClick={create} disabled={disabled || busy || leaseCount < 2 || !targetRef.trim() || Boolean(mergeId)}>创建 MergeRun</button></div>
    {merges.length > 0 && <label className="b24-field"><span className="b24-field-label">待完成 MergeRun</span><select value={mergeId} onChange={(event) => { releaseHeld(); const selected = merges.find((item) => item.mergeRunId === event.target.value); setMergeId(event.target.value); setArtifactIds(selected?.artifactIds ?? []); setTargetRef(selected?.targetIsolationRef ?? ''); setReviewApproved(false); }} disabled={disabled || busy}><option value="">选择 Core MergeRun</option>{merges.filter((item) => item.mergeNodeId === nodeId).map((item) => <option key={item.mergeRunId} value={item.mergeRunId}>{item.mergeRunId} · {item.status}</option>)}</select></label>}
    {knownMerge && <><p>当前状态：{knownMerge.status}{knownMerge.errorCode ? ` · ${knownMerge.errorCode}` : ''}</p><p className="b24-field-help">冻结来源：{knownMerge.artifactIds.join('、')} · 基线：{knownMerge.baseRevision} · 隔离目标：{knownMerge.targetIsolationRef}</p><label className="b24-check-row"><input type="checkbox" checked={reviewApproved} onChange={(event) => setReviewApproved(event.target.checked)} disabled={disabled || busy} /><span>已核对冻结来源、目标、冲突和实际差异，确认执行合并</span></label><button type="button" className="btn btn-primary btn-sm" onClick={finalize} disabled={disabled || busy || !reviewApproved || !['created', 'conflicted', 'review_required'].includes(knownMerge.status)}>Finalize MergeRun</button></>}
    {error && <p className="b24-error" role="alert">{error}</p>}
  </section>;
};
