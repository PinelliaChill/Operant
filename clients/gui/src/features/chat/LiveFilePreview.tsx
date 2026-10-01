import React, { useEffect, useRef, useState } from 'react';
import { AlertTriangle, FileCode2, GitCompareArrows, Loader2 } from 'lucide-react';
import { workbenchClient } from '../../live/workbenchClient';
import type { LiveWorkspaceFile } from '../../live/liveState';

type Preview = { path: string; content: string; content_hash: string; size_bytes: number; truncated: boolean; language?: string | null };
type Diff = { path: string; diff: string; truncated: boolean };

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : 'Core 文件读取失败';
}

function languageLabel(path: string): string {
  const extension = path.split('.').pop()?.toLowerCase();
  return ({ py: 'Python', ts: 'TypeScript', tsx: 'TypeScript JSX', js: 'JavaScript', jsx: 'JavaScript JSX',
    rs: 'Rust', go: 'Go', java: 'Java', sh: 'Shell', css: 'CSS', html: 'HTML', json: 'JSON', md: 'Markdown' } as Record<string, string>)[extension || ''] || '文本';
}

/** File bytes and diff are always queried from Core for the selected workspace. */
export const LiveFilePreview: React.FC<{
  workspaceId: string;
  files: LiveWorkspaceFile[];
  currentPath: string;
  onLoad: (path: string) => void;
  onReference: (path: string) => void;
  canReference: boolean;
  loading: boolean;
}> = ({ workspaceId, files, currentPath, onLoad, onReference, canReference, loading }) => {
  const [preview, setPreview] = useState<Preview | null>(null);
  const [diff, setDiff] = useState<Diff | null>(null);
  const [selectedPath, setSelectedPath] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const sequence = useRef(0);
  useEffect(() => { ++sequence.current; setPreview(null); setDiff(null); setSelectedPath(''); setError(''); setBusy(false); }, [workspaceId, currentPath]);
  useEffect(() => () => { ++sequence.current; }, []);

  const open = async (path: string) => {
    const request = ++sequence.current;
    setSelectedPath(path); setPreview(null); setDiff(null); setError(''); setBusy(true);
    try {
      const value = await workbenchClient.getFileContent(workspaceId, path, 65_536);
      if (request === sequence.current) setPreview(value);
    } catch (cause) { if (request === sequence.current) setError(errorText(cause)); }
    finally { if (request === sequence.current) setBusy(false); }
  };

  const loadDiff = async () => {
    if (!selectedPath || busy) return;
    const request = ++sequence.current;
    setBusy(true); setError('');
    try {
      const value = await workbenchClient.getFileDiff(workspaceId, selectedPath, 65_536);
      if (request === sequence.current) setDiff(value);
    } catch (cause) { if (request === sequence.current) setError(errorText(cause)); }
    finally { if (request === sequence.current) setBusy(false); }
  };

  const parentPath = currentPath.split('/').filter(Boolean).slice(0, -1).join('/');
  return <section className="live-panel live-files-panel" aria-labelledby="live-files-title">
    <div className="live-panel-heading"><div><h2 id="live-files-title">工作区文件</h2><p>正文与差异由 Core 按路径和权限读取，单次最多 64 KiB</p></div>
      {currentPath && <button type="button" className="btn btn-ghost btn-sm" onClick={() => onLoad(parentPath)} disabled={loading}>返回上级</button>}
    </div>
    <div className="live-file-path" aria-label="当前目录路径"><span>{currentPath ? `/${currentPath}` : '/'}</span><span className="live-file-project-id">Workspace ID: {workspaceId}</span></div>
    {loading ? <div className="live-panel-loading" role="status"><Loader2 size={16} className="animate-spin" aria-hidden="true" />正在读取目录…</div>
      : files.length === 0 ? <p className="live-panel-empty">当前目录没有可读条目。</p>
        : <ul className="live-file-list">{files.map((file) => <li key={file.path}>
          {file.kind === 'directory' ? <button type="button" className="live-file-link" onClick={() => onLoad(file.path)} disabled={busy}>{file.name}/</button>
            : <button type="button" className="live-file-link" onClick={() => void open(file.path)} aria-current={selectedPath === file.path ? 'true' : undefined}>{file.name}</button>}
          <span className="live-file-kind">{file.kind === 'directory' ? '目录' : '文件'}</span>
          {file.size !== null && <span className="live-file-size">{file.size.toLocaleString()} B</span>}
        </li>)}</ul>}
    {error && <p className="live-file-error" role="alert"><AlertTriangle size={14} aria-hidden="true" />{error}</p>}
    {busy && <p role="status"><Loader2 size={14} className="animate-spin" aria-hidden="true" />正在读取 {selectedPath}…</p>}
    {preview && <div className="live-file-preview"><div className="live-file-preview-heading"><strong><FileCode2 size={15} aria-hidden="true" />{preview.path}</strong><div className="live-file-preview-actions"><button type="button" className="btn btn-secondary btn-sm" onClick={() => onReference(preview.path)} disabled={!canReference}>@ 引用此文件</button><button type="button" className="btn btn-secondary btn-sm" onClick={() => void loadDiff()} disabled={busy}><GitCompareArrows size={13} aria-hidden="true" />查看 Diff</button></div></div>
      <p>{preview.language || languageLabel(preview.path)} · {preview.size_bytes.toLocaleString()} B · 预览片段哈希 {preview.content_hash.slice(0, 16)}…{preview.truncated ? ' · 预览已截断' : ''}</p>
      <pre aria-label="文件正文"><code>{preview.content}</code></pre>
    </div>}
    {diff && <div className="live-file-preview"><strong>工作区 Diff · {diff.path}</strong><p>{diff.truncated ? '差异已截断；请缩小范围核对。' : 'Core 返回的当前差异'}</p><pre aria-label="文件 Diff"><code>{diff.diff || '没有可显示的差异。'}</code></pre></div>}
  </section>;
};
