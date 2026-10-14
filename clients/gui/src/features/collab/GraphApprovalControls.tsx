import React, { useEffect, useRef, useState } from 'react';
import type { Phase23, Phase23Client } from '@operant/sdk';

interface Props {
  client: Phase23Client;
  runId: string;
  node: Phase23.NodeRun;
  disabled: boolean;
  onChanged: () => Promise<void>;
}

export const GraphApprovalControls: React.FC<Props> = ({ client, runId, node, disabled, onChanged }) => {
  const [approval, setApproval] = useState<Phase23.GraphNodeApproval | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [accepted, setAccepted] = useState(false);
  const epoch = useRef(0);
  const busyRef = useRef(false);
  const keys = useRef(new Map<string, string>());

  useEffect(() => {
    const current = ++epoch.current;
    setApproval(null); setError(null); setAccepted(false);
    if (node.status !== 'waiting_approval' || !node.wait_token) return;
    void client.getGraphNodeApproval(runId, node.node_id).then((result) => {
      if (current !== epoch.current) return;
      if (result.graph_run_id !== runId || result.node_run_id !== node.id || result.wait_token !== node.wait_token) {
        throw new Error('审批与当前等待节点不匹配，请刷新 Core 投影。');
      }
      setApproval(result);
    }).catch((caught) => { if (current === epoch.current) setError(caught instanceof Error ? caught.message : '审批读取失败。'); });
    return () => { epoch.current += 1; keys.current.clear(); busyRef.current = false; };
  }, [client, runId, node.id, node.node_id, node.status, node.wait_token]);

  const decide = async (approved: boolean) => {
    if (busyRef.current || disabled || accepted || !approval || approval.status !== 'pending' || approval.wait_token !== node.wait_token) return;
    if (approved && !window.confirm(`确认批准 ${approval.category}？\n${approval.detail}\nAction Hash: ${approval.action_hash}`)) return;
    if (busyRef.current) return;
    busyRef.current = true;
    const current = epoch.current;
    const action = `${approval.approval_id}:${approved}`;
    const idempotencyKey = keys.current.get(action) ?? crypto.randomUUID();
    keys.current.set(action, idempotencyKey);
    setBusy(true); setError(null);
    let commandAccepted = false;
    try {
      await client.decideGraphNodeApproval(runId, node.node_id, {
        approval_id: approval.approval_id, wait_token: approval.wait_token, approved,
      }, { idempotencyKey });
      commandAccepted = true;
      if (current !== epoch.current) return;
      keys.current.delete(action);
      setAccepted(true);
      await onChanged();
      if (current !== epoch.current) return;
      const latest = await client.getGraphNodeApproval(runId, node.node_id);
      if (current !== epoch.current) return;
      if (latest.graph_run_id !== runId || latest.node_run_id !== node.id || latest.wait_token !== node.wait_token) throw new Error('审批与当前等待节点不匹配，请刷新 Core 投影。');
      setApproval(latest);
    } catch (caught) { if (current === epoch.current) setError(`${commandAccepted ? '审批请求已被 Core 接收，但投影读取失败；请刷新运行。' : '审批结果未知，请回读 Core 后用原请求重试。'} ${caught instanceof Error ? caught.message : ''}`); }
    finally { if (current === epoch.current) { busyRef.current = false; setBusy(false); } }
  };

  return <div className="b24-form" aria-label={`节点 ${node.node_id} 的流程审批`}>
    {approval && <><p>类别：{approval.category} · 状态：{approval.status}</p><p>{approval.detail}</p><p className="b24-field-help">Action Hash：{approval.action_hash} · 截止：{approval.expires_at}</p></>}
    {approval?.status === 'pending' && <div className="b24-actions"><button className="btn btn-primary btn-sm" type="button" onClick={() => void decide(true)} disabled={disabled || busy || accepted}>批准流程</button><button className="btn btn-danger btn-sm" type="button" onClick={() => void decide(false)} disabled={disabled || busy || accepted}>拒绝流程</button></div>}
    {error && <p className="b24-error" role="alert">{error}</p>}
  </div>;
};
