import React, { useMemo, useState } from 'react';
import { useMatch, useNavigate } from 'react-router-dom';
import { ChevronRight, FolderKanban, PanelLeftClose, Plus, Search } from 'lucide-react';
import { Modal } from '../../components/Modal';
import { StatusBadge } from '../../components/StatusBadge';
import { useOperant } from '../../context/ClientContext';
import { useLive } from '../../live/LiveContext';
import type { LiveProjectProjection } from '../../live/liveState';
import { formatRelativeDay } from '../../lib/format';
import { visibleThreadTree } from './liveThreadTree';

interface LiveSidebarProps {
  onNavigate?: () => void;
  onCollapse?: () => void;
}

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
    canCreateSession,
    createSessionUnavailableReason,
  } = useLive();
  const [query, setQuery] = useState('');
  const [collapsedProjects, setCollapsedProjects] = useState<string[]>([]);
  const [collapsedThreads, setCollapsedThreads] = useState<string[]>([]);
  const [newSessionOpen, setNewSessionOpen] = useState(false);
  const [creatingSession, setCreatingSession] = useState(false);
  const [newRoleName, setNewRoleName] = useState('');
  const [newRolePrompt, setNewRolePrompt] = useState('');
  const [newRoleModelId, setNewRoleModelId] = useState('');

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

  const handleCreateSession = async () => {
    if (!canCreateSession || !newRoleName.trim() || !newRolePrompt.trim() || !newRoleModelId.trim()) return;
    setCreatingSession(true);
    const session = await createSession({
      threadId: selectedThreadId || '',
      newRole: {
        name: newRoleName.trim(),
        system_prompt: newRolePrompt.trim(),
        model_profile_id: newRoleModelId.trim(),
      },
    });
    setCreatingSession(false);
    if (session) {
      setNewSessionOpen(false);
      setNewRoleName('');
      setNewRolePrompt('');
      setNewRoleModelId('');
    }
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
          onClick={() => setNewSessionOpen(true)}
          aria-label="新建会话"
          title="为当前会话创建运行"
          disabled={!canCreateSession || creatingSession}
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
            <strong>{lastError.code}</strong>
            <span>{lastError.message}</span>
          </div>
        )}

        {phase === 'ready' && filteredProjects.length === 0 && (
          <div className="rail-sidebar-empty live-sidebar-empty">
            {normalizedQuery ? '没有匹配的项目或会话' : '尚未添加可访问的项目'}
          </div>
        )}

        {filteredProjects.map((project) => {
          const isCollapsed = collapsedProjects.includes(project.id);
          const allProjectThreads = projectThreads(project, threads);
          const projectThreadList = visibleThreadTree(allProjectThreads, normalizedQuery, collapsedThreads);
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
                        aria-label={`${normalizedQuery || !collapsedThreads.includes(thread.id) ? '折叠' : '展开'} ${thread.title || thread.id} 的子会话`}
                      ><ChevronRight size={13} aria-hidden="true" className={normalizedQuery || !collapsedThreads.includes(thread.id) ? 'expanded' : ''} /></button>
                        : <span className="live-thread-tree-spacer" aria-hidden="true" />}
                      <button
                        type="button"
                        className={`rail-sidebar-row${thread.id === (selectedThreadId || activeThreadId) ? ' active' : ''}`}
                        onClick={() => goThread(thread.id)}
                        aria-current={thread.id === (selectedThreadId || activeThreadId) ? 'page' : undefined}
                        aria-label={`${depth > 0 ? `第 ${depth} 层子会话，` : ''}${thread.title || thread.id}，${thread.status}`}
                      >
                        <span className="rail-sidebar-row-main">
                          <span className="rail-sidebar-row-title">{thread.title || thread.id}</span>
                          <span className="rail-sidebar-row-sub">{thread.status}</span>
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
            {phase === 'connecting' ? '正在读取 Core Projection…' : '尚未建立 Core 连接'}
          </div>
        )}
        {command.status === 'awaiting_projection' && (
          <div className="live-sidebar-empty" role="status">
            请求已接受，正在同步进度…
          </div>
        )}
      </div>

      <div className="rail-sidebar-footer">
        <div className="rail-sidebar-mode-card live-mode-card">
          <div className="rail-sidebar-mode-text">
            <span className="rail-sidebar-mode-title">实时连接</span>
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
        title="新建会话"
        footer={(
          <>
            <button type="button" className="btn btn-secondary" onClick={() => setNewSessionOpen(false)}>
              取消
            </button>
            <button type="button" className="btn btn-primary" onClick={() => void handleCreateSession()} disabled={!canCreateSession || !newRoleName.trim() || !newRolePrompt.trim() || !newRoleModelId.trim() || creatingSession} title={createSessionUnavailableReason || '请填写完整角色信息'}>
              {creatingSession ? '提交中…' : '创建会话'}
            </button>
          </>
        )}
      >
        <div className="live-modal-form">
          <label className="live-select-label"><span>角色名称</span><input className="input" value={newRoleName} onChange={(event) => setNewRoleName(event.target.value)} placeholder="例如：编码助手" /></label>
          <label className="live-select-label"><span>模型配置编号</span><input className="input" value={newRoleModelId} onChange={(event) => setNewRoleModelId(event.target.value)} placeholder="已配置的模型编号" /></label>
          <label className="live-select-label"><span>系统提示词</span><textarea className="textarea" value={newRolePrompt} onChange={(event) => setNewRolePrompt(event.target.value)} rows={3} placeholder="描述这个角色的职责" /></label>
        </div>
        {createSessionUnavailableReason && <p className="live-modal-copy">{createSessionUnavailableReason}</p>}
      </Modal>
    </div>
  );
};
