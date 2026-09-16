import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { B25Client, type ContextMemoryImpact, type MemoryVersionRef } from '../../../../../sdk/typescript-client/b2_5.generated';
import { currentBrowserOrigin } from '../../lib/liveBaseUrl';
import { useOperant } from '../../context/ClientContext';
import { B24Empty, B24Section, B24Status } from '../collab/B24Presentation';
import type { B24Command, InspectedContextRevision } from '../../../../../sdk/typescript-client/b2_4.generated';

type InspectionRevision = InspectedContextRevision;
export function B24ContextInspector({ sessionId, busy }: { sessionId: string; busy: boolean }) {
  const { b24Client, connectionStatus } = useOperant();
  const [revisions, setRevisions] = useState<InspectionRevision[]>([]);
  const [selected, setSelected] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [working, setWorking] = useState(false);
  const [impacts, setImpacts] = useState<ContextMemoryImpact[]>([]);
  const [impactError, setImpactError] = useState('');
  const b25Client = useMemo(() => new B25Client(currentBrowserOrigin()), []);
  const epoch = useRef(0);
  const load = useCallback(async () => {
    const version = ++epoch.current;
    try {
      await b24Client.negotiateProtocol();
      const result = await b24Client.inspectContext(sessionId);
      if (version !== epoch.current) return;
      const items = result.revisions;
      setRevisions(items);
      setSelected((old) => items.some((r) => r.id === old) ? old : (items.at(-1)?.id ?? ''));
      setError('');
      try {
        await b25Client.negotiateProtocol();
        const impact = await b25Client.getContextImpact(sessionId);
        if (version === epoch.current) { setImpacts(impact.entries); setImpactError(''); }
      } catch {
        if (version === epoch.current) setImpactError('无法读取当前治理状态；保留上次投影，不能据此确认来源仍有效。');
      }
    } catch (e) {
      if (version !== epoch.current) return;
      setRevisions([]); setError(e instanceof Error ? e.message : '无法读取实际上下文');
    }
  }, [b24Client, b25Client, sessionId]);
  useEffect(() => { setSelected(''); setRevisions([]); setImpacts([]); setImpactError(''); void load(); return () => { epoch.current++; }; }, [load, busy]);
  const revision = revisions.find((r) => r.id === selected);
  const inspection = revision?.memory_inspection;
  function governanceNote(ref: MemoryVersionRef) {
    const impact = impacts.find((item) => item.ref.dataset_id === ref.dataset_id && item.ref.record_id === ref.record_id && item.ref.version === ref.version && item.ref.content_digest === ref.content_digest);
    if (!impact) return null;
    return <p className={!impact.source_and_time_valid ? 'b24-error' : undefined}>
      当前来源与时效：{impact.source_and_time_valid ? '检查通过，发送前仍由 Core 复核' : '已失效或不可用；已发送内容保留，后续请求可能被阻止'}
      {(impact.conflict_proposal_ids ?? []).length > 0 && <> · 有 {(impact.conflict_proposal_ids ?? []).length} 项冲突候选待人工核对</>}
    </p>;
  }
  async function execute(action: B24Command['action'], recordId?: string) {
    setWorking(true); setError(''); setNotice('');
    try {
      await b24Client.execute({ action, session_id: sessionId, record_id: recordId }, { idempotencyKey: crypto.randomUUID() });
      setNotice('已更新后续请求的记忆选择；已发送的上下文记录保留。'); await load();
    } catch (e) { setError(e instanceof Error ? e.message : '上下文操作失败'); }
    finally { setWorking(false); }
  }
  return <details className="b24-inspector">
    <summary>上下文检查器</summary>
    <B24Section title="实际发送的上下文" description="记录每次请求的模型、预算和记忆来源。已发送内容无法收回；撤销污染会阻止后续请求。" actions={<button type="button" onClick={() => void load()} disabled={working || connectionStatus !== 'connected'}>重新读取</button>}>
      {error && <p role="alert" className="b24-error">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      {impactError && <p role="alert" className="b24-error">{impactError}</p>}
      {revisions.length === 0 ? <B24Empty title="暂无已发送的上下文" /> : <>
        <label className="b24-field">请求记录<select value={selected} onChange={(e) => setSelected(e.target.value)}>{revisions.map((r, i) => <option key={r.id} value={r.id}>请求 {i + 1} · {r.model_id}</option>)}</select></label>
        {revision && <p>模型：{revision.model_id} · 输入：{revision.watermark.input_token_estimate ?? '未知'} / {revision.watermark.available_input_tokens ?? '未知'} Token <B24Status label={revision.watermark.estimation_method ?? '估算方式未知'} /></p>}
        {inspection ? <>
          <p>知识截止点：{inspection.pack.knowledge_cutoff} · 记忆预算：{inspection.pack.token_count} / {inspection.pack.total_token_budget} Token（{inspection.counting_method}）</p>
          <button type="button" disabled={busy || working || connectionStatus !== 'connected'} onClick={() => void execute('memory_refresh')}>刷新后续请求的记忆</button>
          <div className="b24-list">{inspection.entries.map((entry) => <article className="b24-card" key={entry.memory.ref.record_id}>
            <p>{entry.memory.content}</p>{governanceNote(entry.memory.ref)}<p>版本 {entry.memory.ref.version} · {entry.token_count} Token · 选择原因：{entry.reason}</p>
            <details><summary>来源与适用条件</summary><pre>{JSON.stringify({ sources: entry.memory.sources, conditions: entry.memory.conditions }, null, 2)}</pre></details>
            <button type="button" disabled={busy || working || connectionStatus !== 'connected'} onClick={() => void execute('memory_exclude', entry.memory.ref.record_id)}>移出本次后续请求</button>
          </article>)}</div>
          {inspection.entries.length === 0 && <B24Empty title="本次未使用记忆" description="没有满足权限、条件、相关性和预算的条目。" />}
        </> : <B24Empty title="本次没有记忆清单" description="记忆未启用或该记录来自旧版本。" />}
      </>}
    </B24Section>
  </details>;
}
