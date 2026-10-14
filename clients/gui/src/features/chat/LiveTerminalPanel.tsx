import React, { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { AlertTriangle, Loader2, Square, Terminal } from 'lucide-react';
import { workbenchClient } from '../../live/workbenchClient';

const OUTPUT_LIMIT = 120_000;
const CLEANUP_UNKNOWN = '终端清理未确认，可能仍有本机进程运行；请人工核对后再继续。';
const CLEANUP_EVENT = 'operant-terminal-cleanup-state';

function cleanupKey(threadId: string): string { return `operant:terminal-cleanup-unknown:${threadId}`; }
export function readTerminalCleanupUnknown(threadId: string): string | null {
  try { return sessionStorage.getItem(cleanupKey(threadId)); } catch { return null; }
}
function setTerminalCleanupUnknown(threadId: string, terminalId: string | null): void {
  try { if (terminalId) sessionStorage.setItem(cleanupKey(threadId), terminalId); else sessionStorage.removeItem(cleanupKey(threadId)); }
  catch { /* The visible panel still shows the unknown result. */ }
  if (typeof window !== 'undefined') window.dispatchEvent(new Event(CLEANUP_EVENT));
}
export const TERMINAL_CLEANUP_EVENT = CLEANUP_EVENT;

function pendingKey(threadId: string): string { return `operant:terminal-approval:${threadId}`; }
function readPending(threadId: string): { key: string; approvalId: string } | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(pendingKey(threadId)) || 'null') as unknown;
    if (typeof value === 'object' && value !== null && 'key' in value && 'approvalId' in value
      && typeof value.key === 'string' && typeof value.approvalId === 'string') return { key: value.key, approvalId: value.approvalId };
  } catch { /* The terminal can still be used without storage; route replay is then unavailable. */ }
  return null;
}
function storePending(threadId: string, value: { key: string; approvalId: string } | null): void {
  try { if (value) sessionStorage.setItem(pendingKey(threadId), JSON.stringify(value)); else sessionStorage.removeItem(pendingKey(threadId)); }
  catch { /* Keep the in-memory key for this mounted panel. */ }
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : '终端连接失败';
}

/** A PTY is created and owned by Core. The browser only carries its input and output. */
export const LiveTerminalPanel: React.FC<{ threadId: string; connected: boolean }> = ({ threadId, connected }) => {
  const navigate = useNavigate();
  const [status, setStatus] = useState<'idle' | 'opening' | 'running' | 'closed' | 'cleanup_unknown'>(() => readTerminalCleanupUnknown(threadId) ? 'cleanup_unknown' : 'idle');
  const [output, setOutput] = useState('');
  const [error, setError] = useState(() => readTerminalCleanupUnknown(threadId) ? CLEANUP_UNKNOWN : '');
  const [input, setInput] = useState('');
  const [approvalId, setApprovalId] = useState(() => readPending(threadId)?.approvalId || '');
  const socket = useRef<WebSocket | null>(null);
  const terminalId = useRef<string | null>(null);
  const createKey = useRef<string | null>(readPending(threadId)?.key || null);
  const generation = useRef(0);
  const outputRef = useRef<HTMLPreElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const lastSize = useRef('');

  useEffect(() => {
    const unresolved = readTerminalCleanupUnknown(threadId);
    if (unresolved) {
      let active = true;
      void workbenchClient.getTerminal(unresolved).then((view) => {
        if (!active) return;
        if (view.status === 'terminated' || view.status === 'exited') {
          setTerminalCleanupUnknown(threadId, null);
          setStatus('closed'); setError('Core 已确认此前终端结束。');
        } else {
          setStatus('cleanup_unknown');
          setError(`${CLEANUP_UNKNOWN} Core 当前状态：${view.status}。`);
        }
      }).catch(() => { if (active) { setStatus('cleanup_unknown'); setError(CLEANUP_UNKNOWN); } });
      return () => { active = false; };
    }
  }, [threadId]);

  useEffect(() => {
    return () => {
      ++generation.current;
      socket.current?.close();
      socket.current = null;
      const id = terminalId.current;
      terminalId.current = null;
      if (id) void workbenchClient.closeTerminal(id).then((closed) => {
        if (closed.status === 'cleanup_unknown') setTerminalCleanupUnknown(threadId, id);
      }).catch(() => setTerminalCleanupUnknown(threadId, id));
    };
  }, [threadId]);

  useEffect(() => {
    if (!connected && status === 'running') {
      socket.current?.close();
      setStatus('closed');
      setError('Core 连接已中断；终端输入已停止，请核对服务端状态。');
    }
  }, [connected, status]);

  useEffect(() => { outputRef.current?.scrollTo({ top: outputRef.current.scrollHeight }); }, [output]);
  useEffect(() => { if (status === 'running') inputRef.current?.focus(); }, [status]);
  useEffect(() => {
    if (status !== 'running' || !outputRef.current || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect;
      if (!rect || socket.current?.readyState !== WebSocket.OPEN) return;
      const cols = Math.max(20, Math.min(300, Math.floor(rect.width / 8)));
      const rows = Math.max(5, Math.min(150, Math.floor(rect.height / 16)));
      const size = `${cols}:${rows}`;
      if (size === lastSize.current) return;
      lastSize.current = size;
      socket.current.send(JSON.stringify({ type: 'resize', cols, rows }));
    });
    observer.observe(outputRef.current);
    return () => observer.disconnect();
  }, [status]);

  const append = (value: string) => setOutput((current) => (current + value).slice(-OUTPUT_LIMIT));

  const start = async () => {
    if (!connected || status === 'opening' || status === 'running' || status === 'cleanup_unknown') return;
    const attempt = ++generation.current;
    setStatus('opening'); setError(''); setOutput('');
    try {
      const previous = terminalId.current;
      if (previous) {
        const closed = await workbenchClient.closeTerminal(previous);
        if (closed.status === 'cleanup_unknown') {
          setTerminalCleanupUnknown(threadId, previous);
          setStatus('cleanup_unknown'); setError(CLEANUP_UNKNOWN);
          return;
        }
        terminalId.current = null;
      }
      const key = createKey.current ?? crypto.randomUUID();
      createKey.current = key;
      const terminal = await workbenchClient.createTerminal(threadId, { cols: 80, rows: 24 }, key);
      createKey.current = null;
      storePending(threadId, null);
      setApprovalId('');
      if (attempt !== generation.current) {
        await workbenchClient.closeTerminal(terminal.terminal_id);
        return;
      }
      terminalId.current = terminal.terminal_id;
      const ws = new WebSocket(workbenchClient.terminalStreamUrl(terminal.terminal_id), workbenchClient.terminalStreamProtocols(terminal.stream_token));
      socket.current = ws;
      ws.onopen = () => {
        if (attempt !== generation.current) return;
        if (ws.protocol !== 'operant.terminal.v1') {
          setError('Core 未协商终端子协议，输入已停止。');
          ws.close();
          return;
        }
        setStatus('running');
      };
      ws.onmessage = (event) => {
        if (attempt !== generation.current) return;
        try {
          const frame = JSON.parse(String(event.data)) as { type?: string; data?: string; exit_code?: number | null; message?: string };
          if (frame.type === 'output' && typeof frame.data === 'string') append(frame.data);
          else if (frame.type === 'exit') {
            append(`\n[进程结束${frame.exit_code == null ? '' : ` · code ${frame.exit_code}`}]\n`);
            setStatus('closed');
          } else if (frame.type === 'error') setError(frame.message || '终端执行失败');
        } catch { setError('终端返回了无法解析的数据，输入已停止。'); ws.close(); }
      };
      ws.onerror = () => { if (attempt === generation.current) setError('终端连接失败，请核对 Core 状态与权限。'); };
      ws.onclose = () => {
        if (attempt === generation.current) {
          setStatus('closed');
          if (terminalId.current === terminal.terminal_id) {
            terminalId.current = null;
            void workbenchClient.closeTerminal(terminal.terminal_id).then((closed) => {
              if (closed.status === 'cleanup_unknown') {
                setTerminalCleanupUnknown(threadId, terminal.terminal_id);
                if (attempt === generation.current) { setStatus('cleanup_unknown'); setError(CLEANUP_UNKNOWN); }
              }
            }).catch(() => {
              setTerminalCleanupUnknown(threadId, terminal.terminal_id);
              if (attempt === generation.current) { setStatus('cleanup_unknown'); setError(CLEANUP_UNKNOWN); }
            });
          }
        }
      };
    } catch (cause) {
      if (attempt === generation.current) {
        const detail = typeof cause === 'object' && cause !== null && 'detail' in cause ? cause.detail : null;
        const approvalId = typeof detail === 'object' && detail !== null && 'approval_id' in detail ? String(detail.approval_id) : null;
        if (approvalId && createKey.current) {
          storePending(threadId, { key: createKey.current, approvalId });
          setApprovalId(approvalId);
        } else if (typeof cause === 'object' && cause !== null && 'recovery' in cause && cause.recovery === 'none') {
          createKey.current = null;
          storePending(threadId, null);
          setApprovalId('');
        }
        if (terminalId.current && !approvalId) {
          setTerminalCleanupUnknown(threadId, terminalId.current);
          setError(`${CLEANUP_UNKNOWN} ${errorText(cause)}`);
          setStatus('cleanup_unknown');
        } else {
          setError(approvalId ? `需要先处理审批 ${approvalId}，批准后点击“批准后重试”按同一请求重试。` : errorText(cause));
          setStatus('closed');
        }
      }
    }
  };

  const stop = async () => {
    ++generation.current;
    socket.current?.close(); socket.current = null;
    const id = terminalId.current;
    terminalId.current = null;
    createKey.current = null;
    storePending(threadId, null);
    setApprovalId('');
    setStatus('closed');
    setInput('');
    if (id) {
      try {
        const closed = await workbenchClient.closeTerminal(id);
        if (closed.status === 'cleanup_unknown') { setTerminalCleanupUnknown(threadId, id); setStatus('cleanup_unknown'); setError(CLEANUP_UNKNOWN); }
      } catch (cause) { setTerminalCleanupUnknown(threadId, id); setStatus('cleanup_unknown'); setError(`终端关闭结果未确认，需人工核对：${errorText(cause)}`); }
    }
  };

  const send = () => {
    if (status !== 'running' || socket.current?.readyState !== WebSocket.OPEN || !input) return;
    socket.current.send(JSON.stringify({ type: 'input', data: `${input}\n` }));
    setInput('');
  };
  const interrupt = () => {
    if (status === 'running' && socket.current?.readyState === WebSocket.OPEN) {
      socket.current.send(JSON.stringify({ type: 'input', data: '\u0003' }));
    }
  };

  return <section className="live-panel live-terminal-panel" aria-label="交互终端">
    <div className="live-panel-heading"><div><h2><Terminal size={16} aria-hidden="true" /> 交互终端</h2><p>在当前工作区以本机用户权限运行；批准创建后，本会话的输入不逐条审批。关闭面板或离开会话会终止进程，输入不可重放。</p></div>
      {status === 'running' || status === 'opening'
        ? <button type="button" className="btn btn-secondary btn-sm" onClick={() => void stop()}><Square size={12} aria-hidden="true" />关闭终端</button>
        : <button type="button" className="btn btn-secondary btn-sm" onClick={() => void start()} disabled={!connected || status === 'cleanup_unknown'}>{status === 'cleanup_unknown' ? '需人工核对' : approvalId ? '批准后重试' : status === 'closed' ? '新建终端' : '打开终端'}</button>}
    </div>
    {error && <p className="live-terminal-error" role="alert"><AlertTriangle size={14} aria-hidden="true" />{error}</p>}
    {status === 'cleanup_unknown' && <button type="button" className="btn btn-ghost btn-sm" onClick={() => { setTerminalCleanupUnknown(threadId, null); setStatus('closed'); setError('已清除本浏览器的提示；Core 审计记录未改变。'); }}>已人工核查，清除提示</button>}
    {approvalId && <div className="live-terminal-approval"><span>申请 {approvalId} 待核对；批准后返回并按同一请求重试。</span><button type="button" className="btn btn-secondary btn-sm" onClick={() => navigate('/approvals')}>前往审批</button><button type="button" className="btn btn-ghost btn-sm" onClick={() => { createKey.current = null; storePending(threadId, null); setApprovalId(''); setError('已放弃本次终端申请。'); }}>放弃申请</button></div>}
    {status === 'opening' && <p role="status"><Loader2 size={14} className="animate-spin" aria-hidden="true" />正在等待 Core 创建终端…</p>}
    <pre ref={outputRef} className="live-terminal-output" aria-label="终端输出" role="log">{output || (status === 'idle' ? '打开终端后显示输出。' : '')}</pre>
    <div className="live-terminal-input"><input ref={inputRef} className="input" type="text" value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => { if (event.ctrlKey && event.key.toLowerCase() === 'c') { event.preventDefault(); interrupt(); } else if (event.key === 'Enter' && !event.nativeEvent.isComposing) { event.preventDefault(); send(); } }} aria-label="终端输入" placeholder="输入命令并按 Enter" disabled={status !== 'running'} /><button type="button" className="btn btn-secondary btn-sm" onClick={send} disabled={status !== 'running' || !input}>发送</button><button type="button" className="btn btn-ghost btn-sm" onClick={interrupt} disabled={status !== 'running'} title="向 PTY 发送 Ctrl+C">中断命令</button></div>
  </section>;
};
