import React, { useCallback, useEffect, useState } from 'react';
import { ArrowUpRight, RefreshCw } from 'lucide-react';
import { sessionControlClient, type BtwRun } from '../../live/sessionControlClient';

export const LiveBtwPanel: React.FC<{ sessionId: string; threadId: string; workspace: string; connected: boolean; onPromoted: () => Promise<void> }> = ({ sessionId, threadId, workspace, connected, onPromoted }) => {
  const storageKey = `operant-btw:${threadId}`;
  const [prompt, setPrompt] = useState('');
  const [runId, setRunId] = useState(() => sessionStorage.getItem(storageKey) || '');
  const [run, setRun] = useState<BtwRun | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const refresh = useCallback(async (id = runId) => {
    if (!connected || !id) return;
    try { setRun(await sessionControlClient.getBtw(id)); setError(''); }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'BTW 查询失败'); }
  }, [connected, runId]);
  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    if (!connected || !runId || run?.status !== 'running') return;
    const timer = window.setInterval(() => { void refresh(); }, 3000);
    return () => window.clearInterval(timer);
  }, [connected, refresh, run?.status, runId]);

  const start = async () => {
    if (!connected || !prompt.trim() || busy) return;
    setBusy(true); setError(''); setNotice(''); setRun(null);
    try {
      const result = await sessionControlClient.startBtw({ session_id: sessionId, thread_id: threadId, workspace, prompt: prompt.trim() }, (id) => { setRunId(id); sessionStorage.setItem(storageKey, id); });
      setRun(result);
      setPrompt('');
      setNotice(result.status === 'completed' ? '只读结果已由 Core 保存。需要加入主线时，请明确点击提升。' : `Core 返回状态：${result.status}`);
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'BTW 调用失败'); if (runId) await refresh(); }
    finally { setBusy(false); }
  };
  const promote = async () => {
    if (!run || run.status !== 'completed' || busy || !window.confirm('将此 BTW 只读结果加入主线会话？')) return;
    setBusy(true); setError(''); setNotice('');
    try { const updated = await sessionControlClient.promoteBtw(run.id); setRun(updated); await onPromoted(); setNotice('Core 已确认提升，主线会话已刷新。'); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '提升失败，请刷新 Core 状态核对'); }
    finally { setBusy(false); }
  };

  return <details className="live-btw-panel"><summary>BTW 独立提问 <span>只读模型调用 · 不自动写入主线</span></summary>
    <div className="live-btw-content"><p>在当前 Session 中单独提问。结果先保存在 Sidecar；只有点击“提升到主线”才会加入会话。</p>
      <label>提问内容<textarea className="input" value={prompt} onChange={(event) => setPrompt(event.target.value)} disabled={!connected || busy} placeholder="输入需要独立分析的问题" /></label>
      <div className="live-btw-actions"><button type="button" className="btn btn-secondary btn-sm" disabled={!connected || !prompt.trim() || busy} onClick={() => void start()}>{busy ? '处理中…' : '独立提问'}</button>{runId && <button type="button" className="btn btn-ghost btn-sm" disabled={!connected || busy} onClick={() => void refresh()}><RefreshCw size={13} />刷新结果</button>}</div>
      {error && <div className="live-alert live-alert-error" role="alert">{error}</div>}{notice && <div className="live-alert" role="status">{notice}</div>}
      {run && <article className="live-btw-result"><strong>Core 状态：{run.status}</strong><small>Sidecar ID：{run.id}</small>{run.error_code && <p role="alert">失败原因：{run.error_code}</p>}{run.response && <pre>{run.response}</pre>}{run.status === 'completed' && <button type="button" className="btn btn-primary btn-sm" onClick={() => void promote()} disabled={busy || !connected}><ArrowUpRight size={13} />提升到主线</button>}{run.status === 'promoted' && <p>已提升：{run.promoted_item_id || 'Core 已确认'}</p>}</article>}
    </div>
  </details>;
};
