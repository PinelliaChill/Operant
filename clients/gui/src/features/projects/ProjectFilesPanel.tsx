import React, { useCallback, useEffect, useRef, useState } from 'react';
import { SearchSelect } from '../../components/SearchSelect';
import { useOperant } from '../../context/ClientContext';
import { useLive } from '../../live/LiveContext';
import { appendWorkspaceFiles } from '../../live/liveAdapter';
import type { LiveWorkspaceFile } from '../../live/liveState';
import { requestErrorCopy } from '../../lib/requestErrorCopy';
import { LiveFilePreview } from '../chat/LiveFilePreview';

interface DirectoryState {
  scope: string;
  path: string;
  files: LiveWorkspaceFile[];
  snapshot: string | null;
  nextPageToken: string | null;
}

const emptyDirectory = (scope: string, path: string): DirectoryState => ({
  scope, path, files: [], snapshot: null, nextPageToken: null,
});

/** Browse the project's registered workspace through the existing formal client. */
export const ProjectFilesPanel: React.FC = () => {
  const { connectionStatus } = useOperant();
  const { adapter, phase, projects, projectNames, selectedProjectId } = useLive();
  const [choice, setChoice] = useState(selectedProjectId || '');
  const readable = projects.filter((project) => project.readable);
  const project = choice ? readable.find((item) => item.id === choice) : readable[0];
  const scope = project ? JSON.stringify([project.id, project.workspaceRef]) : '';
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const sequence = useRef(0);
  const [directory, setDirectory] = useState<DirectoryState>(emptyDirectory('', ''));
  const [busy, setBusy] = useState<'initial' | 'more' | null>(null);
  const [error, setError] = useState('');
  const connected = phase === 'ready' && connectionStatus === 'connected';
  const workspaceId = project?.id || '';
  const load = useCallback(async (path = '') => {
    const request = ++sequence.current;
    setError('');
    if (!connected || !workspaceId) { setBusy(null); return; }
    setDirectory(emptyDirectory(scope, path));
    setBusy('initial');
    try {
      const page = await adapter.listWorkspaceFilePage(workspaceId, path);
      if (request === sequence.current && scopeRef.current === scope) {
        setDirectory({ scope, path, ...page });
      }
    } catch (cause) {
      if (request === sequence.current && scopeRef.current === scope) {
        setDirectory(emptyDirectory(scope, path));
        setError(requestErrorCopy(cause instanceof Error && 'detail' in cause ? cause.detail : cause));
      }
    } finally {
      if (request === sequence.current && scopeRef.current === scope) setBusy(null);
    }
  }, [adapter, connected, scope, workspaceId]);
  const loadMore = useCallback(async () => {
    const { path, nextPageToken, snapshot } = directory;
    if (!connected || !workspaceId || directory.scope !== scope || !nextPageToken || busy) return;
    const request = ++sequence.current;
    setError('');
    setBusy('more');
    try {
      const page = await adapter.listWorkspaceFilePage(workspaceId, path, nextPageToken);
      if (request === sequence.current && scopeRef.current === scope) {
        if (page.snapshot !== snapshot || page.nextPageToken === nextPageToken) {
          throw new Error('目录在读取期间发生变化，请刷新文件后重试。');
        }
        setDirectory((current) => current.scope === scope && current.path === path
          && current.nextPageToken === nextPageToken
          ? { ...current, files: appendWorkspaceFiles(current.files, page.files), nextPageToken: page.nextPageToken }
          : current);
      }
    } catch (cause) {
      if (request === sequence.current && scopeRef.current === scope) {
        setError(requestErrorCopy(cause instanceof Error && 'detail' in cause ? cause.detail : cause));
      }
    } finally {
      if (request === sequence.current && scopeRef.current === scope) setBusy(null);
    }
  }, [adapter, busy, connected, directory, scope, workspaceId]);
  useEffect(() => {
    void load();
    return () => { ++sequence.current; };
  }, [load]);
  return <section className="b2-memory" aria-label="项目文件">
    <div className="b2-memory-card">
      <SearchSelect label="项目" value={project?.id || ''} onChange={setChoice}
        options={readable.map((item) => ({ value: item.id, label: projectNames[item.id] || item.name }))}
        disabled={!connected} />
      <button type="button" className="btn btn-secondary btn-sm" disabled={!connected || !project || busy !== null}
        onClick={() => void load(directory.scope === scope ? directory.path : '')}>刷新文件</button>
    </div>
    {!connected && <p role="status">连接恢复后可查看项目文件。</p>}
    {connected && !project && <p>{choice ? '项目不可用。请选择其他项目。' : '先添加一个项目，再查看文件。'}</p>}
    {error && <p className="live-alert live-alert-error" role="alert">{error}</p>}
    {connected && project && <LiveFilePreview key={scope} workspaceId={workspaceId}
      files={directory.scope === scope ? directory.files : []}
      currentPath={directory.scope === scope ? directory.path : ''}
      onLoad={(path) => void load(path)} canReference={false} loading={busy === 'initial'} />}
    {connected && project && directory.scope === scope && directory.nextPageToken && <div>
      <p role="status">已显示 {directory.files.length} 项，目录还有更多文件。</p>
      <button type="button" className="btn btn-secondary btn-sm" disabled={busy !== null}
        onClick={() => void loadMore()}>{busy === 'more' ? '正在加载更多…' : '继续加载文件'}</button>
    </div>}
  </section>;
};
