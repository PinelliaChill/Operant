import React, { useCallback, useEffect, useRef, useState } from 'react';
import { workbenchClient } from '../../live/workbenchClient';
import type { ResourceCleanupPreview, ResourceInventory } from '../../../../../sdk/typescript-client/workbench.generated';
import './live-resources.css';

const sizeLabel = (bytes: number) => bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`;
const kindLabels: Record<string, string> = {
  context_reference_snapshot: '临时引用快照', artifact: '工件', context_revision: '模型输入快照',
  compaction: '压缩记录', thread_history: '对话历史', browser_profile: '浏览器环境',
  memory: '长期记忆', approval_audit: '审批与审计', run_state: '运行记录',
  tool_snapshot: '工具快照', terminal: '交互终端',
};
const retentionLabels: Record<string, string> = {
  context_reference_snapshot: '临时引用正文；清理前核对引用、Pin 和运行状态',
  artifact: '工件可能包含成果或执行证据，按原保留策略管理',
  context_revision: '保存模型实际输入与恢复依据', compaction: '保存压缩后的任务事实与来源',
  thread_history: '对话历史持续保留', browser_profile: '浏览器环境按独立策略管理',
  memory: '长期记忆按版本和来源管理', approval_audit: '审批与审计持续保留',
  run_state: '运行状态与恢复依据持续保留', tool_snapshot: '工具快照与回执持续保留',
  terminal: '终端由运行状态和关闭流程管理',
};
function reasonLabel(reason: string): string {
  const labels: Record<string, string> = {
    eligible: '满足清理条件', pinned: '已 Pin', canonical_history: '对话历史',
    context_revision: '模型输入或引用快照', compaction: '压缩记录',
    legacy_snapshot_policy: '原策略禁止物理删除', authoritative_artifact: '成果或执行证据',
    active_or_recoverable_run: '活动或可恢复运行', thread_owner_unverified: '会话归属尚未核验',
    ttl_not_due_or_protected_resource: '尚未到期或存在保留锁',
  };
  if (reason.includes('unknown') || reason.includes('reconciliation')) return '结果尚未确认，需要人工核对';
  return reason.split(',').map((part) => labels[part] ?? part).join('、');
}
function checkedCursors(value: Record<string, unknown> = {}): Record<string, number> {
  const result: Record<string, number> = {};
  for (const name of ['after_artifact', 'after_revision', 'after_compaction']) {
    const cursor = value[name];
    if (cursor === undefined) continue;
    if (typeof cursor !== 'number' || !Number.isSafeInteger(cursor) || cursor < 0) throw new Error('Core 返回了无效的资源分页位置。');
    result[name] = cursor;
  }
  return result;
}

export const LiveResourcesPanel: React.FC<{ threadId: string; connected: boolean }> = ({ threadId, connected }) => {
  const [inventory, setInventory] = useState<ResourceInventory | null>(null);
  const [preview, setPreview] = useState<ResourceCleanupPreview | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [completedHours, setCompletedHours] = useState('1');
  const [unansweredHours, setUnansweredHours] = useState('72');
  const epoch = useRef(0);
  const keys = useRef(new Map<string, string>());
  const cursors = useRef<Record<string, number>>({});
  const refresh = useCallback(async (more = false) => {
    const version = ++epoch.current;
    if (!connected) return;
    setBusy(true);
    try {
      const value = await workbenchClient.listResources(threadId, more ? cursors.current : {});
      if (version !== epoch.current) return;
      if (more && value.truncated && JSON.stringify(value.next_cursor) === JSON.stringify(cursors.current)) throw new Error('资源分页没有推进，请刷新核对。');
      cursors.current = checkedCursors(value.next_cursor);
      setInventory((old) => more && old ? { ...value, resources: [...new Map([...old.resources, ...value.resources].map((item) => [item.id, item])).values()] } : value);
      setPreview(null); setError('');
      if (!more) {
        setSelected([]);
        setCompletedHours(String((value.policy.completed_ttl_seconds ?? 3600) / 3600));
        setUnansweredHours(String((value.policy.unanswered_ttl_seconds ?? 259200) / 3600));
      }
    } catch (cause) { if (version === epoch.current) setError(String(cause)); }
    finally { if (version === epoch.current) setBusy(false); }
  }, [connected, threadId]);
  useEffect(() => {
    setInventory(null); setPreview(null); setSelected([]); setBusy(false); setNotice(''); setError('');
    void refresh();
    return () => { ++epoch.current; };
  }, [refresh]);
  async function mutate(identity: string, operation: (key: string) => Promise<unknown>, message: string) {
    if (!connected || busy) return;
    const version = epoch.current;
    setBusy(true); setError(''); setNotice('');
    const fingerprint = `${threadId}:${identity}`;
    const key = keys.current.get(fingerprint) ?? crypto.randomUUID();
    keys.current.set(fingerprint, key);
    try {
      const result = await operation(key); keys.current.delete(fingerprint);
      if (version !== epoch.current) return;
      const skipped = result && typeof result === 'object' && 'skipped' in result && Array.isArray(result.skipped) ? result.skipped : [];
      setNotice(message + (skipped.length ? ` ${skipped.length} 项未清理：${skipped.map((item: { reason: string }) => reasonLabel(item.reason)).join('；')}` : '')); await refresh();
    } catch (cause) { if (version === epoch.current) setError(String(cause)); }
    finally { if (version === epoch.current || version + 1 === epoch.current) setBusy(false); }
  }
  async function previewCleanup() {
    if (!connected || busy || !selected.length) return;
    const version = epoch.current;
    setBusy(true); setError(''); setNotice('');
    try {
      const value = await workbenchClient.previewResourceCleanup(threadId, selected);
      if (version === epoch.current) setPreview(value);
    } catch (cause) { if (version === epoch.current) setError(String(cause)); }
    finally { if (version === epoch.current) setBusy(false); }
  }
  const disabled = !connected || busy;
  const cleanupIds = preview?.items.filter((item) => item.eligible).map((item) => item.resource_id) ?? [];
  return <details className="live-resources-panel"><summary>临时资源与保留策略</summary><div className="live-resources-content">
    <p>查看资源大小、归属和保留原因。清理前先预览，Core 会再次检查 Pin 和运行状态。</p>
    {!connected && <p role="alert">Core 未连接，资源信息可能已过期。</p>}
    {error && <p role="alert" className="live-workbench-error">{error}</p>}
    {notice && <p role="status" className="live-workbench-notice">{notice}</p>}
    <button type="button" className="btn btn-ghost btn-sm" disabled={disabled} onClick={() => void refresh()}>刷新资源</button>
    {inventory ? <><p>{inventory.resources.length} 项 · {sizeLabel(inventory.resources.reduce((sum, item) => sum + item.size_bytes, 0))}</p>
      <ul className="live-resource-list">{inventory.resources.map((item) => <li key={item.id}>
        <label><input type="checkbox" checked={selected.includes(item.id)} disabled={disabled} onChange={(event) => {
          setSelected((old) => event.target.checked ? [...old, item.id] : old.filter((id) => id !== item.id)); setPreview(null);
        }} /><span><strong>{kindLabels[item.kind] ?? item.kind}</strong> · {sizeLabel(item.size_bytes)}<small>{item.id}</small></span></label>
        <p>{retentionLabels[item.kind] ?? item.retention_reason}{item.hold_reason ? ` · 保留锁：${reasonLabel(item.hold_reason)}` : ''}</p>
        <small>归属：{item.owner_thread_id || '未确认'} · {item.state}{item.due_at ? ` · 到期 ${new Date(item.due_at).toLocaleString()}` : ''}</small>
        {['context_reference_snapshot', 'artifact'].includes(item.kind) && item.state === 'active' && item.hold_reason !== 'legacy_snapshot_policy' && <button type="button" className="btn btn-ghost btn-sm" disabled={disabled} onClick={() => void mutate(`pin:${item.id}:${!item.pinned}`, (key) => workbenchClient.pinResource(threadId, item.id, !item.pinned, key), item.pinned ? '已取消 Pin。' : '已 Pin，自动清理会保留此资源。')}>{item.pinned ? '取消 Pin' : 'Pin 保留'}</button>}
      </li>)}</ul>
      {inventory.truncated && <><p>当前盘点尚未全部读取。</p><button type="button" className="btn btn-secondary btn-sm" disabled={disabled} onClick={() => void refresh(true)}>读取更多资源</button></>}
      <fieldset disabled={disabled} className="live-resource-policy"><legend>临时资源 TTL</legend>
        <label>确认完成后保留（小时）<input type="number" className="input" min="1" step="0.01" value={completedHours} onChange={(event) => setCompletedHours(event.target.value)} /></label>
        <label>未回复时保留（小时）<input type="number" className="input" min="1" step="0.01" value={unansweredHours} onChange={(event) => setUnansweredHours(event.target.value)} /></label>
        <button type="button" className="btn btn-secondary btn-sm" onClick={() => {
          const complete = Number(completedHours), unanswered = Number(unansweredHours);
          if (!completedHours || !unansweredHours || !Number.isFinite(complete) || !Number.isFinite(unanswered) || complete < 1 || unanswered < 1 || complete > 8760 || unanswered > 8760) { setError('请填写 1～8760 小时的保留时间。'); return; }
          void mutate(`policy:${complete}:${unanswered}`, (key) => workbenchClient.updateResourcePolicy(threadId, { completed_ttl_seconds: Math.round(complete * 3600), unanswered_ttl_seconds: Math.round(unanswered * 3600) }, key), '保留策略已保存。');
        }}>保存保留时间</button>
      </fieldset>
      <p>默认确认完成后 1 小时、未回复 3 天。聊天、长期记忆、审批审计和仍可恢复的运行会保留。</p>
      <div className="live-workbench-actions">
        <button type="button" className="btn btn-secondary btn-sm" disabled={disabled} onClick={() => void mutate('confirm:true', (key) => workbenchClient.confirmResources(threadId, true, key), '已确认完成，临时资源按 TTL 保留。')}>确认任务完成</button>
        <button type="button" className="btn btn-ghost btn-sm" disabled={disabled} onClick={() => void mutate('confirm:false', (key) => workbenchClient.confirmResources(threadId, false, key), '已撤回完成确认。')}>撤回完成确认</button>
        <button type="button" className="btn btn-secondary btn-sm" disabled={disabled || !selected.length} onClick={() => void previewCleanup()}>预览选中资源清理</button>
      </div>
      {preview && <div className="live-resource-preview" role="region" aria-label="资源清理预览"><p>可清理 {cleanupIds.length} 项，{sizeLabel(preview.total_bytes)}。确认后删除这些临时资源。</p>
        <ul>{preview.items.map((item) => <li key={item.resource_id}>{item.resource_id}：{item.eligible ? '可清理' : '保留'} · {reasonLabel(item.reason)}</li>)}</ul>
        <div className="live-workbench-actions"><button type="button" className="btn btn-secondary btn-sm" disabled={disabled || !cleanupIds.length} onClick={() => void mutate(`cleanup:${cleanupIds.join(',')}`, (key) => workbenchClient.cleanupResources(threadId, cleanupIds, key), '清理已结束；状态变化的资源由 Core 保留，列表已刷新。')}>确认清理临时资源</button><button type="button" className="btn btn-ghost btn-sm" disabled={busy} onClick={() => setPreview(null)}>取消清理</button></div>
      </div>}
    </> : <p>{connected ? '正在读取资源…' : '连接恢复后刷新资源。'}</p>}
  </div></details>;
};
