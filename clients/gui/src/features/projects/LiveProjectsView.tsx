import React, { useCallback, useState } from 'react';
import { Folder, FolderKanban, File, Loader2, PanelLeftOpen, RefreshCw } from 'lucide-react';
import { useNavigate, useOutletContext } from 'react-router-dom';
import { EmptyState } from '../../components/EmptyState';
import { StatusBadge } from '../../components/StatusBadge';
import { useLive } from '../../live/LiveContext';
import type { LiveProjectProjection, LiveWorkspaceFile } from '../../live/liveState';
import type { RailOutletContext } from '../../app/RailLayout';

const ProjectFiles: React.FC<{
  files: LiveWorkspaceFile[];
  loading: boolean;
  onLoad: () => void;
}> = ({ files, loading, onLoad }) => (
  <div className="live-project-files">
    <div className="live-project-files-heading">
      <span>可读目录</span>
      <button type="button" className="btn btn-ghost btn-sm" onClick={onLoad} disabled={loading}>
        {loading ? <Loader2 size={12} className="animate-spin" aria-hidden="true" /> : <RefreshCw size={12} aria-hidden="true" />}
        {loading ? '读取中…' : '刷新文件'}
      </button>
    </div>
    {files.length === 0 ? (
      <p className="live-project-files-empty">未加载目录 metadata；Core 不提供正文下载。</p>
    ) : (
      <ul>
        {files.slice(0, 8).map((file) => (
          <li key={file.path}>
            {file.kind === 'directory' ? <Folder size={14} aria-hidden="true" /> : <File size={14} aria-hidden="true" />}
            <span>{file.path}</span>
          </li>
        ))}
      </ul>
    )}
  </div>
);

export const LiveProjectsView: React.FC = () => {
  const navigate = useNavigate();
  const { showSidebarOpenBtn, openSidebar } = useOutletContext<RailOutletContext>();
  const {
    phase,
    projects,
    threads,
    selectedProjectId,
    selectProject,
    loadFiles,
    files,
    filesWorkspaceId,
    lastError,
    refresh,
    reconnect,
  } = useLive();
  const [filesLoadingId, setFilesLoadingId] = useState<string | null>(null);

  const getProjectThreads = useCallback((project: LiveProjectProjection) => (
    threads.filter((thread) => project.threadIds.includes(thread.id))
  ), [threads]);

  const showProjectFiles = async (project: LiveProjectProjection) => {
    setFilesLoadingId(project.id);
    await loadFiles(project.id);
    setFilesLoadingId(null);
  };

  if (phase !== 'ready' && projects.length === 0) {
    return (
      <div className="live-route-state" role={lastError ? 'alert' : 'status'}>
        <div className="live-route-state-icon"><FolderKanban size={22} aria-hidden="true" /></div>
        <h1>{lastError ? 'Core Project Projection 不可用' : '正在读取 Workspace Project…'}</h1>
        <p>{lastError?.message ?? 'Live 模式不会用演示项目填充此页面。'}</p>
        <div className="live-empty-actions">
          <button type="button" className="btn btn-primary" onClick={() => void reconnect()}><RefreshCw size={14} aria-hidden="true" />重连 Core</button>
          <button type="button" className="btn btn-secondary" onClick={() => void refresh()}>重新查询</button>
        </div>
      </div>
    );
  }

  return (
    <div className="live-route-view">
      <header className="live-route-header">
        <div className="live-route-heading">
          {showSidebarOpenBtn && <button type="button" className="btn btn-secondary btn-icon" onClick={openSidebar} aria-label="打开 Core 侧栏" title="打开 Core 侧栏"><PanelLeftOpen size={16} aria-hidden="true" /></button>}
          <div><span className="live-kicker"><span className="live-kicker-dot" aria-hidden="true" />实时 Core · phase1e.v1</span><h1>Workspace Projects</h1></div>
        </div>
        <div className="live-route-header-actions"><StatusBadge status={phase === 'ready' ? 'connected' : 'pending'} label={phase === 'ready' ? 'Core 已连接' : '读取中'} size="sm" /><button type="button" className="btn btn-ghost btn-icon" onClick={() => void refresh()} aria-label="刷新 Project Projection" title="刷新 Project Projection"><RefreshCw size={15} aria-hidden="true" /></button></div>
      </header>
      {lastError && <div className="live-alert live-alert-error" role="alert"><span>{lastError.code}：{lastError.message}</span></div>}
      <p className="live-route-description">以下内容来自已注册 Workspace 的服务端只读 Projection。项目不支持本阶段 CRUD、默认标记或删除。</p>
      <div className="live-project-grid">
        {projects.filter((project) => project.readable).map((project) => {
          const projectThreads = getProjectThreads(project);
          const isSelected = project.id === selectedProjectId;
          return (
            <article key={project.id} className={`live-project-card${isSelected ? ' selected' : ''}`}>
              <button type="button" className="live-project-card-main" onClick={() => selectProject(project.id)} aria-pressed={isSelected}>
                <span className="live-project-card-icon" aria-hidden="true"><FolderKanban size={18} /></span>
                <span className="live-project-card-copy"><strong>{project.name}</strong><span>{project.workspaceRef}</span></span>
                <StatusBadge status={project.writable ? 'active' : 'paused'} label={project.writable ? '可写' : '只读'} size="sm" />
              </button>
              <div className="live-project-card-meta"><span>{projectThreads.length} 个 Thread</span><span>{project.runIds.length} 个 Run 摘要</span></div>
              {isSelected && <ProjectFiles files={filesWorkspaceId === project.id ? files : []} loading={filesLoadingId === project.id} onLoad={() => void showProjectFiles(project)} />}
              <div className="live-project-card-actions">
                <button type="button" className="btn btn-secondary btn-sm" onClick={() => { selectProject(project.id); navigate('/chat'); }}>查看 Thread</button>
                <button type="button" className="btn btn-ghost btn-sm" onClick={() => void showProjectFiles(project)} disabled={filesLoadingId === project.id}>浏览文件</button>
              </div>
            </article>
          );
        })}
      </div>
      {projects.filter((project) => project.readable).length === 0 && (
        <EmptyState icon={FolderKanban} title="暂无可读 Workspace Project" description="Core 没有返回可读的服务端 Workspace 投影。" />
      )}
    </div>
  );
};
