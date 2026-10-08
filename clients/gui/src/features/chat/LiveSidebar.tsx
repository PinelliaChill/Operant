import React, { useMemo, useState } from 'react';
import { useMatch, useNavigate } from 'react-router-dom';
import { ChevronRight, FolderKanban, PanelLeftClose, Plus, Search } from 'lucide-react';
import { useOnboarding } from '../../live/OnboardingContext';
import { StatusBadge } from '../../components/StatusBadge';
import { liveThreadTitle, useLive } from '../../live/LiveContext';
import type { LiveProjectProjection } from '../../live/liveState';
import { formatRelativeDay } from '../../lib/format';
import { visibleThreadTree } from './liveThreadTree';
import { requestErrorCopy } from '../../lib/requestErrorCopy';
import { visibleOnboardingError } from '../../live/createRequestRecovery';

interface LiveSidebarProps {
  onNavigate?: () => void;
  onCollapse?: () => void;
}

const threadStatusLabel = (status: string) => ({ active: '可用', running: '运行中', waiting_approval: '等待审批', completed: '已完成', failed: '已失败', cancelled: '已取消', paused: '已暂停', idle: '尚未开始' })[status] || status;

function projectThreads(project: LiveProjectProjection, threads: ReturnType<typeof useLive>['threads']) {
  return threads
    .filter((thread) => project.threadIds.includes(thread.id))
    .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
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
  const {
    phase,
    projects,
    threads,
    selectedProjectId,
    selectedThreadId,
    selectProject,
    selectThread,
    lastError,
    projectionStale,
    command,
  } = useLive();
  const { setup, metadata, bootstrap, initializeConversation, getRouteRevision, createBusy, createOutcomeUnknown, renameOutcomeUnknown, createError, metadataWarning, recoveredConversationId, clearCreateUncertainty } = useOnboarding();
  const onboardingError = visibleOnboardingError(createError, metadataWarning);
  const [query, setQuery] = useState('');
  const [collapsedProjects, setCollapsedProjects] = useState<string[]>([]);
  const [collapsedThreads, setCollapsedThreads] = useState<string[]>([]);

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
      return projectThreads(project, threads).some((thread) => (metadata[thread.id]?.title || liveThreadTitle(thread)).toLowerCase().includes(normalizedQuery));
    }),
    [metadata, normalizedQuery, threads, visibleProjects]
  );

  const toggleProject = (projectId: string) => {
    setCollapsedProjects((current) => current.includes(projectId)
      ? current.filter((id) => id !== projectId)
      : [...current, projectId]);
  };
  const toggleThread = (threadId: string) => {
    setCollapsedThreads((current) => current.includes(threadId)
      ? current.filter((id) => id !== threadId)
      : [...current, threadId]);
  };

  const goThread = (threadId: string) => {
    if (!selectThread(threadId)) return;
    navigate(`/chat/${threadId}`);
    onNavigate?.();
  };

  const handleCreateConversation = async () => {
    const routeRevision = getRouteRevision();
    const locationHref = window.location.href;
    const stillHere = () => getRouteRevision() === routeRevision && window.location.href === locationHref;
    if (!setup?.ready) {
      if (!setup?.default_model_profile_id) { navigate('/settings?section=models'); onNavigate?.(); return; }
      const ready = await bootstrap(setup.default_model_profile_id);
      if (!stillHere()) return;
      if (!ready) { navigate(setup.missing_steps?.includes('skills') ? '/settings?section=tools' : '/settings?section=models'); onNavigate?.(); return; }
    }
    const result = await initializeConversation(selectedProjectId ? { workspace_id: selectedProjectId } : {});
    if (result && stillHere()) { navigate(`/chat/${encodeURIComponent(result.thread_id)}`); onNavigate?.(); }
  };

  const phaseLabel = phase === 'ready'
    ? '已连接'
    : phase === 'connecting'
      ? '连接中'
      : phase === 'error'
        ? '连接失败'
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
            placeholder="搜索项目或会话"
            aria-label="搜索项目或会话"
            className="rail-sidebar-search-input"
          />
        </div>
        <button
          type="button"
          className="btn btn-ghost btn-icon"
          onClick={() => void handleCreateConversation()}
          aria-label="新建对话"
          title={setup?.ready ? '新建对话' : '先连接模型'}
          disabled={createBusy || createOutcomeUnknown || phase !== 'ready'}
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

      {(phase !== 'ready' || projectionStale) && <div className="live-sidebar-status" role="status" aria-live="polite">
        <StatusBadge
          status={phase === 'ready' ? 'connected' : phase === 'error' ? 'disconnected' : 'pending'}
          label={phaseLabel}
          size="sm"
          pulse={phase === 'connecting'}
        />
        {projectionStale && <span className="live-sidebar-stale">正在同步最新状态</span>}
      </div>}

      <div className="rail-sidebar-scroll">
        {lastError && (
          <div className="live-sidebar-error" role="alert">
            <strong>{requestErrorCopy(lastError)}</strong>
            <details><summary>查看错误详情</summary><code>{lastError.code}</code><p>{lastError.message}</p>{lastError.recovery && <code>{lastError.recovery}</code>}</details>
          </div>
        )}
        {onboardingError && <div className="live-sidebar-error" role="alert"><span>{onboardingError}</span>{(createOutcomeUnknown || renameOutcomeUnknown) && <button type="button" className="btn btn-secondary btn-sm" onClick={() => void clearCreateUncertainty()}>刷新并核对</button>}{recoveredConversationId && <button type="button" className="btn btn-primary btn-sm" onClick={() => navigate(`/chat/${encodeURIComponent(recoveredConversationId)}`)}>打开已创建对话</button>}</div>}

        {phase === 'ready' && filteredProjects.length === 0 && (
          <div className="rail-sidebar-empty live-sidebar-empty">
            {normalizedQuery ? '没有匹配的项目或会话' : '尚未添加可访问的项目'}
          </div>
        )}

        {filteredProjects.map((project) => {
          const isCollapsed = collapsedProjects.includes(project.id);
          const allProjectThreads = projectThreads(project, threads);
          const projectThreadList = visibleThreadTree(allProjectThreads, normalizedQuery, collapsedThreads, Object.fromEntries(Object.entries(metadata).map(([id, item]) => [id, item.title])));
          const isSelectedProject = project.id === selectedProjectId;
          return (
            <section key={project.id} className="rail-sidebar-project-group">
              <div className={`rail-sidebar-project-header${isSelectedProject ? ' active' : ''}`}>
                <button
                  type="button"
                  className="live-sidebar-project-button"
                  onClick={() => {
                    if (selectProject(project.id)) toggleProject(project.id);
                  }}
                  aria-expanded={!isCollapsed}
                  aria-label={`项目 ${project.name}，${projectThreadList.length} 个会话`}
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
                  {projectThreadList.length > 0 ? projectThreadList.map(({ thread, depth, hasChildren }) => (
                    <div key={thread.id} className="live-thread-tree-row" style={{ paddingInlineStart: `${depth * 14}px` }}>
                      {hasChildren ? <button
                        type="button"
                        className="live-thread-tree-toggle"
                        onClick={() => toggleThread(thread.id)}
                        aria-expanded={normalizedQuery ? true : !collapsedThreads.includes(thread.id)}
                        aria-label={`${normalizedQuery || !collapsedThreads.includes(thread.id) ? '折叠' : '展开'} ${metadata[thread.id]?.title || liveThreadTitle(thread)} 的子对话`}
                      ><ChevronRight size={13} aria-hidden="true" className={normalizedQuery || !collapsedThreads.includes(thread.id) ? 'expanded' : ''} /></button>
                        : <span className="live-thread-tree-spacer" aria-hidden="true" />}
                      <button
                        type="button"
                        className={`rail-sidebar-row${thread.id === (selectedThreadId || activeThreadId) ? ' active' : ''}`}
                        onClick={() => goThread(thread.id)}
                        aria-current={thread.id === (selectedThreadId || activeThreadId) ? 'page' : undefined}
                        aria-label={`${depth > 0 ? `第 ${depth} 层子对话，` : ''}${metadata[thread.id]?.title || liveThreadTitle(thread)}，${threadStatusLabel(thread.status)}`}
                      >
                        <span className="rail-sidebar-row-main">
                          <span className="rail-sidebar-row-title">{metadata[thread.id]?.title || liveThreadTitle(thread)}</span>
                          <span className="rail-sidebar-row-sub">{threadStatusLabel(thread.status)}</span>
                        </span>
                        <span className="rail-sidebar-time">{formatRelativeDay(thread.updatedAt)}</span>
                      </button>
                    </div>
                  )) : (
                    <div className="rail-sidebar-empty">{normalizedQuery ? '没有匹配的会话' : '暂无会话'}</div>
                  )}
                </div>
              )}
            </section>
          );
        })}

        {phase !== 'ready' && !lastError && (
          <div className="live-sidebar-empty" role="status">
            {phase === 'connecting' ? '正在读取对话…' : '尚未连接'}
          </div>
        )}
        {command.status === 'awaiting_projection' && (
          <div className="live-sidebar-empty" role="status">
            请求已接受，正在同步进度…
          </div>
        )}
      </div>

    </div>
  );
};
