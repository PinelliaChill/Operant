import React, { useMemo, useState } from 'react';
import { useMatch, useNavigate } from 'react-router-dom';
import { ChevronRight, FolderKanban, PanelLeftClose, Plus, Search } from 'lucide-react';
import { Modal } from '../../components/Modal';
import { StatusBadge } from '../../components/StatusBadge';
import { useOperant } from '../../context/ClientContext';
import { useLive } from '../../live/LiveContext';
import type { LiveProjectProjection } from '../../live/liveState';
import { formatRelativeDay } from '../../lib/format';

interface LiveSidebarProps {
  onNavigate?: () => void;
  onCollapse?: () => void;
}

function projectThreads(project: LiveProjectProjection, threads: ReturnType<typeof useLive>['threads']) {
  return threads
    .filter((thread) => project.threadIds.includes(thread.id) || thread.workspace === project.workspaceRef)
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at));
}

/**
 * Live-only conversation sidebar.  It intentionally has no DemoContext
 * dependency: an unavailable Core produces an empty/error state rather than a
 * list that looks like a successful connection.
 */
export const LiveSidebar: React.FC<LiveSidebarProps> = ({ onNavigate, onCollapse }) => {
  const navigate = useNavigate();
  const chatMatch = useMatch('/chat/:conversationId');
  const activeThreadId = chatMatch?.params.conversationId;
  const { setClientMode } = useOperant();
  const {
    phase,
    projects,
    threads,
    selectedProjectId,
    selectedThreadId,
    selectProject,
    selectThread,
    createSession,
    lastError,
    projectionStale,
    command,
  } = useLive();
  const [query, setQuery] = useState('');
  const [collapsedProjects, setCollapsedProjects] = useState<string[]>([]);
  const [newSessionOpen, setNewSessionOpen] = useState(false);
  const [creatingSession, setCreatingSession] = useState(false);

  const normalizedQuery = query.trim().toLowerCase();
  const visibleProjects = useMemo(
    () => projects.filter((project) => project.readable),
    [projects]
  );
  const filteredProjects = useMemo(
    () => visibleProjects.filter((project) => {
      if (!normalizedQuery) return true;
      if (project.name.toLowerCase().includes(normalizedQuery)) return true;
      if (project.workspaceRef.toLowerCase().includes(normalizedQuery)) return true;
      return projectThreads(project, threads).some((thread) => thread.title.toLowerCase().includes(normalizedQuery));
    }),
    [normalizedQuery, threads, visibleProjects]
  );

  const toggleProject = (projectId: string) => {
    setCollapsedProjects((current) => current.includes(projectId)
      ? current.filter((id) => id !== projectId)
      : [...current, projectId]);
  };

  const goThread = (threadId: string) => {
    selectThread(threadId);
    navigate(`/chat/${threadId}`);
    onNavigate?.();
  };

  const handleCreateSession = async () => {
    setCreatingSession(true);
    const session = await createSession({});
    setCreatingSession(false);
    if (session) setNewSessionOpen(false);
  };

  const phaseLabel = phase === 'ready'
    ? 'Core 已连接'
    : phase === 'connecting'
      ? '正在连接 Core'
      : phase === 'error'
        ? 'Core 连接失败'
        : '等待连接';

  return (
    <div className="rail-sidebar-inner live-sidebar" data-client-mode="live">
      <div className="rail-sidebar-header">
        <div className="rail-sidebar-search">
          <Search size={14} aria-hidden="true" />
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索 Core Thread"
            aria-label="搜索 Core Thread"
            className="rail-sidebar-search-input"
          />
        </div>
        <button
          type="button"
          className="btn btn-ghost btn-icon"
          onClick={() => setNewSessionOpen(true)}
          aria-label="创建 Core Session"
          title="创建 Core Session"
          disabled={phase !== 'ready' || creatingSession}
        >
          <Plus size={16} aria-hidden="true" />
        </button>
        {onCollapse && (
          <button
            type="button"
            className="btn btn-ghost btn-icon"
            onClick={onCollapse}
            aria-label="收起侧栏"
            title="收起侧栏"
          >
            <PanelLeftClose size={15} aria-hidden="true" />
          </button>
        )}
      </div>

      <div className="live-sidebar-status" role="status" aria-live="polite">
        <StatusBadge
          status={phase === 'ready' ? 'connected' : phase === 'error' ? 'disconnected' : 'pending'}
          label={phaseLabel}
          size="sm"
          pulse={phase === 'connecting'}
        />
        {projectionStale && <span className="live-sidebar-stale">Projection 待校正</span>}
      </div>

      <div className="rail-sidebar-scroll">
        {lastError && (
          <div className="live-sidebar-error" role="alert">
            <strong>{lastError.code}</strong>
            <span>{lastError.message}</span>
          </div>
        )}

        {phase === 'ready' && filteredProjects.length === 0 && (
          <div className="rail-sidebar-empty live-sidebar-empty">
            {normalizedQuery ? '没有匹配的 Core Project/Thread' : 'Core 暂无可读 Workspace Project'}
          </div>
        )}

        {filteredProjects.map((project) => {
          const isCollapsed = collapsedProjects.includes(project.id);
          const projectThreadList = projectThreads(project, threads).filter((thread) => (
            !normalizedQuery || thread.title.toLowerCase().includes(normalizedQuery)
          ));
          const isSelectedProject = project.id === selectedProjectId;
          return (
            <section key={project.id} className="rail-sidebar-project-group">
              <div className={`rail-sidebar-project-header${isSelectedProject ? ' active' : ''}`}>
                <button
                  type="button"
                  className="live-sidebar-project-button"
                  onClick={() => {
                    selectProject(project.id);
                    toggleProject(project.id);
                  }}
                  aria-expanded={!isCollapsed}
                  aria-label={`Project ${project.name}，${projectThreadList.length} 个 Thread`}
                >
                  <FolderKanban size={13} aria-hidden="true" />
                  <span className="rail-sidebar-project-info">
                    <span className="rail-sidebar-project-name-row">
                      <span className="rail-sidebar-project-name">{project.name}</span>
                      {!project.writable && <span className="live-readonly-badge">只读</span>}
                    </span>
                    <span className="rail-sidebar-project-path" title={project.workspaceRef}>
                      {project.workspaceRef}
                    </span>
                  </span>
                  <span className="rail-sidebar-project-actions" aria-hidden="true">
                    <span className="rail-sidebar-project-count">{projectThreadList.length}</span>
                    <ChevronRight size={12} className={`rail-sidebar-project-chevron${!isCollapsed ? ' expanded' : ''}`} />
                  </span>
                </button>
              </div>
              {!isCollapsed && (
                <div className="rail-sidebar-project-children">
                  {projectThreadList.length > 0 ? projectThreadList.map((thread) => (
                    <button
                      type="button"
                      key={thread.id}
                      className={`rail-sidebar-row${thread.id === (selectedThreadId || activeThreadId) ? ' active' : ''}`}
                      onClick={() => goThread(thread.id)}
                      aria-current={thread.id === (selectedThreadId || activeThreadId) ? 'page' : undefined}
                    >
                      <span className="rail-sidebar-row-main">
                        <span className="rail-sidebar-row-title">{thread.title || thread.id}</span>
                        <span className="rail-sidebar-row-sub">{thread.status}</span>
                      </span>
                      <span className="rail-sidebar-time">{formatRelativeDay(thread.updated_at)}</span>
                    </button>
                  )) : (
                    <div className="rail-sidebar-empty">{normalizedQuery ? '无匹配 Thread' : '暂无 Thread'}</div>
                  )}
                </div>
              )}
            </section>
          );
        })}

        {phase !== 'ready' && !lastError && (
          <div className="live-sidebar-empty" role="status">
            {phase === 'connecting' ? '正在读取 Core Projection…' : '尚未建立 Core 连接'}
          </div>
        )}
        {command.status === 'awaiting_projection' && (
          <div className="live-sidebar-empty" role="status">
            Session/Run 已接受，等待 Core Projection 更新…
          </div>
        )}
      </div>

      <div className="rail-sidebar-footer">
        <div className="rail-sidebar-mode-card live-mode-card">
          <div className="rail-sidebar-mode-text">
            <span className="rail-sidebar-mode-title">实时 Core</span>
            <span className="rail-sidebar-mode-desc">只显示服务端 Projection</span>
          </div>
          <button
            type="button"
            role="switch"
            aria-checked={false}
            aria-label="切换到演示模式"
            className="switch"
            onClick={() => setClientMode('mock')}
          />
        </div>
      </div>

      <Modal
        isOpen={newSessionOpen}
        onClose={() => setNewSessionOpen(false)}
        title="创建 Core Session"
        footer={(
          <>
            <button type="button" className="btn btn-secondary" onClick={() => setNewSessionOpen(false)}>
              取消
            </button>
            <button type="button" className="btn btn-primary" onClick={() => void handleCreateSession()} disabled={creatingSession}>
              {creatingSession ? '提交中…' : '创建并等待 Projection'}
            </button>
          </>
        )}
      >
        <p className="live-modal-copy">
          请求会交给当前 Core 的 Session Client。GUI 不会本地生成 Session、Thread 或运行状态；创建成功后等待服务端投影绑定。
        </p>
        <div className="live-modal-boundary">
          <span>当前阶段接入</span>
          <strong>Workspace / Project · Thread · Session / Run</strong>
        </div>
      </Modal>
    </div>
  );
};
