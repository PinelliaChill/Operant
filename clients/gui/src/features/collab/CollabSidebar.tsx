/**
 * 协作分区情境侧栏（设计基线 §3.1 + v12 侧栏项目树与分组架构）
 * 自上而下：
 * 1. 搜索框与全局新建项目 / 折叠侧栏把手
 * 2. 草稿 (Drafts)（支持折叠/展开）
 * 3. 模板 (Templates)（支持折叠/展开，点击直达模板总览）
 * 4. 项目工作区实例树（遍历 projects，可折叠，hover 快捷「+」新建实例，含活跃实例与已归档折叠子组）
 * 5. 运行进度 (Runs)（全局 Run 列表）
 */

import React, { useEffect, useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { ChevronRight, PanelLeftClose, Plus, Search, Check } from 'lucide-react';
import type { GraphDraft, WorkflowRun } from '@operant/sdk';
import { useOperant } from '../../context/ClientContext';
import { useDemo } from '../../demo/DemoContext';
import { StatusBadge } from '../../components/StatusBadge';
import { Modal } from '../../components/Modal';
import { InstanceRow } from './InstanceRow';
import type { DemoWorkflowDirectoryItem } from '../../demo/types';

/** 草稿保存后广播，侧栏监听以刷新列表（CollabView 保存草稿时 dispatch） */
export const GRAPH_DRAFTS_CHANGED_EVENT = 'operant:graph-drafts-changed';

/** 预置项目标识颜色 */
const PRESET_COLORS = ['#2563eb', '#ea580c', '#059669', '#7c3aed', '#db2777', '#0891b2'];

interface CollabSidebarProps {
  /** 行点击跳转后回调（移动端用于关闭覆盖抽屉） */
  onNavigate?: () => void;
  /** 收起侧栏回调（v6 §3：RailLayout 全宽度注入——桌面收起内联侧栏为把手，移动端关闭抽屉） */
  onCollapse?: () => void;
}

export const CollabSidebar: React.FC<CollabSidebarProps> = ({ onNavigate, onCollapse }) => {
  const navigate = useNavigate();
  const { client, activeWorkspace } = useOperant();
  const {
    conversations,
    getWorkflowDirectory,
    instancesOfWorkflow,
    projects,
    createProject,
    createWorkflowInstance,
  } = useDemo();
  const [searchParams] = useSearchParams();
  const activeDraftId = searchParams.get('draft');
  const activeTplId = searchParams.get('tpl');

  const [query, setQuery] = useState('');
  const defaultProjectId =
    projects.find((p) => p.isDefault)?.id || projects[0]?.id || 'proj_workspace';

  // 折叠状态（草稿、模板）
  const [draftsOpen, setDraftsOpen] = useState(true);
  const [templatesOpen, setTemplatesOpen] = useState(true);

  // 项目实例折叠状态（持久化于 localStorage.operant.collab.collapsed_projects）
  const [collapsedProjects, setCollapsedProjects] = useState<string[]>(() => {
    try {
      const raw = localStorage.getItem('operant.collab.collapsed_projects');
      return raw ? JSON.parse(raw) : [];
    } catch {
      return [];
    }
  });

  const toggleProjectCollapse = (projectId: string) => {
    setCollapsedProjects((prev) => {
      const next = prev.includes(projectId)
        ? prev.filter((id) => id !== projectId)
        : [...prev, projectId];
      try {
        localStorage.setItem('operant.collab.collapsed_projects', JSON.stringify(next));
      } catch {
        /* ignore localStorage error */
      }
      return next;
    });
  };

  // 各项目已归档实例子组展开状态（默认收起）
  const [collapsedArchived, setCollapsedArchived] = useState<Record<string, boolean>>({});
  const toggleProjectArchived = (projectId: string) => {
    setCollapsedArchived((prev) => ({ ...prev, [projectId]: !prev[projectId] }));
  };

  // 新建项目 Modal 状态
  const [newProjectModalOpen, setNewProjectModalOpen] = useState(false);
  const [newProjName, setNewProjName] = useState('');
  const [newProjColor, setNewProjColor] = useState('#2563eb');
  const [newProjPath, setNewProjPath] = useState('');
  const [pathCustomized, setPathCustomized] = useState(false);

  // 新建工作流实例 Modal 状态
  const [newInstanceModalOpen, setNewInstanceModalOpen] = useState(false);
  const [newInstanceTitle, setNewInstanceTitle] = useState('');
  const [newInstanceTplId, setNewInstanceTplId] = useState<string>('');
  const [newInstanceProjectId, setNewInstanceProjectId] = useState<string>(defaultProjectId);

  useEffect(() => {
    if (!pathCustomized && newProjName.trim()) {
      const slug = newProjName.trim().toLowerCase().replace(/\s+/g, '-');
      setNewProjPath(`/Users/operant/workspace/${slug}`);
    }
  }, [newProjName, pathCustomized]);

  const [drafts, setDrafts] = useState<GraphDraft[]>([]);
  const [templates, setTemplates] = useState<DemoWorkflowDirectoryItem[]>([]);
  const [runs, setRuns] = useState<WorkflowRun[]>([]);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const [draftList, runList, templateList] = await Promise.all([
          client.listGraphDrafts(activeWorkspace),
          client.listWorkflowRuns(activeWorkspace),
          getWorkflowDirectory(),
        ]);
        if (cancelled) return;
        setDrafts(draftList);
        setRuns(runList);
        setTemplates(templateList);
      } catch (err: unknown) {
        console.error('Failed to load collab sidebar data:', err);
      }
    };
    void load();
    window.addEventListener(GRAPH_DRAFTS_CHANGED_EVENT, load);
    return () => {
      cancelled = true;
      window.removeEventListener(GRAPH_DRAFTS_CHANGED_EVENT, load);
    };
  }, [client, activeWorkspace, getWorkflowDirectory]);

  const normalizedQuery = query.trim().toLowerCase();

  const filteredDrafts = useMemo(() => {
    if (!normalizedQuery) return drafts;
    return drafts.filter((d) => d.name.toLowerCase().includes(normalizedQuery));
  }, [drafts, normalizedQuery]);

  const filteredTemplates = useMemo(() => {
    if (!normalizedQuery) return templates;
    return templates.filter((t) => t.name.toLowerCase().includes(normalizedQuery));
  }, [templates, normalizedQuery]);

  /** 活跃实例（跨模板，按更新时间倒序）：绑定工作流目录条目的群聊会话 */
  const activeInstances = useMemo(() => {
    return conversations
      .filter((c) => c.workflowId && c.lifecycle !== 'archived')
      .filter((c) => !normalizedQuery || c.title.toLowerCase().includes(normalizedQuery))
      .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
  }, [conversations, normalizedQuery]);

  /** 已归档实例（跨模板，按更新时间倒序） */
  const archivedInstances = useMemo(() => {
    return conversations
      .filter((c) => c.workflowId && c.lifecycle === 'archived')
      .filter((c) => !normalizedQuery || c.title.toLowerCase().includes(normalizedQuery))
      .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
  }, [conversations, normalizedQuery]);

  // 项目列表按默认工作区优先排序
  const sortedProjects = useMemo(
    () => [...projects].sort((a, b) => (a.isDefault ? -1 : b.isDefault ? 1 : 0)),
    [projects]
  );

  const handleCreateProjectSubmit = () => {
    if (!newProjName.trim()) return;
    createProject({
      name: newProjName.trim(),
      color: newProjColor,
      path:
        newProjPath.trim() ||
        `/Users/operant/workspace/${newProjName.trim().toLowerCase().replace(/\s+/g, '-')}`,
    });
    setNewProjectModalOpen(false);
    setNewProjName('');
    setNewProjPath('');
    setPathCustomized(false);
  };

  const openNewInstanceModal = (projectId?: string) => {
    setNewInstanceTitle('');
    setNewInstanceProjectId(projectId || defaultProjectId);
    setNewInstanceTplId(templates[0]?.id || '');
    setNewInstanceModalOpen(true);
  };

  const handleCreateInstanceSubmit = () => {
    if (!newInstanceTplId) return;
    const newId = createWorkflowInstance(
      newInstanceTplId,
      newInstanceTitle.trim() || undefined,
      newInstanceProjectId
    );
    setNewInstanceModalOpen(false);
    navigate(`/workflow/${newInstanceTplId}/s/${newId}`);
    onNavigate?.();
  };

  return (
    <div className="rail-sidebar-inner">
      {/* 1. 搜索框与新建项目按钮 */}
      <div className="rail-sidebar-header">
        <div className="rail-sidebar-search">
          <Search size={14} aria-hidden="true" />
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索草稿、模板与实例"
            aria-label="搜索草稿、模板与实例"
            className="rail-sidebar-search-input"
          />
        </div>
        <button
          type="button"
          className="btn btn-ghost btn-icon"
          onClick={() => {
            setNewProjName('');
            setNewProjPath('');
            setNewProjColor('#2563eb');
            setPathCustomized(false);
            setNewProjectModalOpen(true);
          }}
          aria-label="新建项目"
          title="新建项目"
        >
          <Plus size={16} />
        </button>
        {onCollapse && (
          <button
            className="btn btn-ghost btn-icon"
            onClick={onCollapse}
            aria-label="收起侧栏"
            title="收起侧栏"
          >
            <PanelLeftClose size={15} />
          </button>
        )}
      </div>

      <div className="rail-sidebar-scroll">
        {/* 1. 草稿组：点击载入选中到画布（支持折叠/展开） */}
        <div className="rail-sidebar-group">
          <div
            className="rail-sidebar-section-header"
            role="button"
            tabIndex={0}
            onClick={() => setDraftsOpen((v) => !v)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                setDraftsOpen((v) => !v);
              }
            }}
            aria-expanded={draftsOpen}
          >
            <span className="rail-sidebar-section-title">草稿</span>
            <span className="rail-sidebar-project-count">{filteredDrafts.length}</span>
            <ChevronRight
              size={12}
              className={`rail-sidebar-project-chevron${draftsOpen ? ' expanded' : ''}`}
              aria-hidden="true"
            />
          </div>
          {draftsOpen && (
            filteredDrafts.length > 0 ? (
              filteredDrafts.map((d) => (
                <button
                  key={d.id}
                  className={`rail-sidebar-row${d.id === activeDraftId ? ' active' : ''}`}
                  onClick={() => {
                    navigate(`/collab?view=canvas&draft=${d.id}`);
                    onNavigate?.();
                  }}
                  aria-current={d.id === activeDraftId ? 'page' : undefined}
                >
                  <span className="rail-sidebar-row-main">
                    <span className="rail-sidebar-row-title">{d.name}</span>
                    <span className="rail-sidebar-row-sub">
                      {d.nodes.length} 节点 · {d.edges.length} 连线
                    </span>
                  </span>
                </button>
              ))
            ) : (
              <div className="rail-sidebar-empty">暂无图草稿</div>
            )
          )}
        </div>

        {/* 2. 模板组（支持折叠/展开，点击直达模板总览） */}
        <div className="rail-sidebar-group">
          <div
            className="rail-sidebar-section-header"
            role="button"
            tabIndex={0}
            onClick={() => setTemplatesOpen((v) => !v)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                setTemplatesOpen((v) => !v);
              }
            }}
            aria-expanded={templatesOpen}
          >
            <span className="rail-sidebar-section-title">模板</span>
            <span className="rail-sidebar-project-count">{filteredTemplates.length}</span>
            <ChevronRight
              size={12}
              className={`rail-sidebar-project-chevron${templatesOpen ? ' expanded' : ''}`}
              aria-hidden="true"
            />
          </div>
          {templatesOpen && (
            filteredTemplates.length > 0 ? (
              filteredTemplates.map((t) => {
                const isSelected = t.id === activeTplId;
                const activeCount = instancesOfWorkflow(t.id).active.length;
                return (
                  <button
                    key={t.id}
                    className={`rail-sidebar-row${isSelected ? ' active' : ''}`}
                    onClick={() => {
                      navigate(`/collab?view=home&tpl=${t.id}`);
                      onNavigate?.();
                    }}
                    aria-current={isSelected ? 'page' : undefined}
                  >
                    <span className="rail-sidebar-row-main">
                      <span
                        className="rail-sidebar-row-title"
                        style={{ display: 'flex', alignItems: 'center', gap: 6 }}
                      >
                        <span
                          style={{
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            whiteSpace: 'nowrap',
                          }}
                        >
                          {t.name}
                        </span>
                        <span
                          className="badge"
                          style={{
                            fontSize: '10px',
                            padding: '1px 5px',
                            backgroundColor: 'var(--accent-subtle)',
                            color: 'var(--accent-on-subtle)',
                            flexShrink: 0,
                          }}
                        >
                          {t.kind === 'published' ? `v${t.version}` : '另存'}
                        </span>
                      </span>
                      <span className="rail-sidebar-row-sub">{activeCount} 个活跃实例</span>
                    </span>
                  </button>
                );
              })
            ) : (
              <div className="rail-sidebar-empty">暂无模板</div>
            )
          )}
        </div>

        {/* 3. 按项目工作区分组的实例树 */}
        <div className="rail-sidebar-group">
          <div className="rail-sidebar-group-title">项目实例</div>
          {sortedProjects.map((p) => {
            const isCollapsed = collapsedProjects.includes(p.id);
            const projectActive = activeInstances.filter(
              (c) => c.projectId === p.id || (!c.projectId && p.isDefault)
            );
            const projectArchived = archivedInstances.filter(
              (c) => c.projectId === p.id || (!c.projectId && p.isDefault)
            );

            return (
              <div key={p.id} className="rail-sidebar-project-group">
                <div
                  className="rail-sidebar-project-header"
                  role="button"
                  tabIndex={0}
                  onClick={() => toggleProjectCollapse(p.id)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault();
                      toggleProjectCollapse(p.id);
                    }
                  }}
                  aria-expanded={!isCollapsed}
                  aria-label={`项目 ${p.name}，${projectActive.length} 个活跃实例`}
                >
                  <span
                    className="rail-sidebar-dot"
                    style={{ backgroundColor: p.color }}
                    aria-hidden="true"
                  />
                  <div className="rail-sidebar-project-info">
                    <div className="rail-sidebar-project-name-row">
                      <span className="rail-sidebar-project-name">{p.name}</span>
                      {p.isDefault && (
                        <span className="rail-sidebar-project-default-badge">默认</span>
                      )}
                    </div>
                    <span className="rail-sidebar-project-path" title={p.path}>
                      {p.path}
                    </span>
                  </div>
                  <div className="rail-sidebar-project-actions">
                    <span
                      className="rail-sidebar-project-count"
                      title={`${projectActive.length} 个活跃实例`}
                    >
                      {projectActive.length}
                    </span>
                    <button
                      type="button"
                      className="rail-sidebar-project-add"
                      onClick={(e) => {
                        e.stopPropagation();
                        openNewInstanceModal(p.id);
                      }}
                      title={`在 ${p.name} 中新建实例`}
                      aria-label={`在 ${p.name} 中新建实例`}
                    >
                      <Plus size={12} />
                    </button>
                    <ChevronRight
                      size={12}
                      className={`rail-sidebar-project-chevron${!isCollapsed ? ' expanded' : ''}`}
                      aria-hidden="true"
                    />
                  </div>
                </div>

                {!isCollapsed && (
                  <div className="rail-sidebar-project-children">
                    {projectActive.length > 0 ? (
                      projectActive.map((c) => (
                        <InstanceRow
                          key={c.id}
                          conversation={c}
                          showIdleBadge
                          onNavigate={onNavigate}
                        />
                      ))
                    ) : (
                      <div className="rail-sidebar-empty">
                        {normalizedQuery ? '无匹配活跃实例' : '暂无活跃实例'}
                      </div>
                    )}

                    {projectArchived.length > 0 && (
                      <div style={{ marginTop: 4 }}>
                        <button
                          type="button"
                          className="rail-sidebar-group-title inst-arch-toggle"
                          onClick={() => toggleProjectArchived(p.id)}
                          aria-expanded={Boolean(collapsedArchived[p.id])}
                        >
                          <ChevronRight
                            size={11}
                            className={`inst-arch-toggle-chevron${
                              collapsedArchived[p.id] ? ' expanded' : ''
                            }`}
                            aria-hidden="true"
                          />
                          <span>已归档（{projectArchived.length}）</span>
                        </button>
                        {collapsedArchived[p.id] &&
                          projectArchived.map((c) => (
                            <InstanceRow
                              key={c.id}
                              conversation={c}
                              archived
                              direction="up"
                              onNavigate={onNavigate}
                            />
                          ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>

        {/* 4. 运行进度组：点击进 /runs/:id 流水线详情 */}
        <div className="rail-sidebar-group">
          <div className="rail-sidebar-group-title">运行进度</div>
          {runs.length > 0 ? (
            runs.map((r) => (
              <button
                key={r.id}
                className="rail-sidebar-row"
                onClick={() => {
                  navigate(`/runs/${r.id}`);
                  onNavigate?.();
                }}
              >
                <span className="rail-sidebar-row-main">
                  <span className="rail-sidebar-row-title">{r.task}</span>
                  <span className="rail-sidebar-row-sub">{r.id}</span>
                </span>
                <StatusBadge status={r.status} size="sm" />
              </button>
            ))
          ) : (
            <div className="rail-sidebar-empty">暂无运行进度</div>
          )}
        </div>
      </div>

      {/* 新建项目 Modal */}
      <Modal
        isOpen={newProjectModalOpen}
        onClose={() => setNewProjectModalOpen(false)}
        title="新建项目工作区"
        footer={
          <>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => setNewProjectModalOpen(false)}
            >
              取消
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={handleCreateProjectSubmit}
              disabled={!newProjName.trim()}
            >
              <Plus size={14} />
              <span>创建项目</span>
            </button>
          </>
        }
      >
        <form
          onSubmit={(e) => {
            e.preventDefault();
            handleCreateProjectSubmit();
          }}
          style={{ display: 'flex', flexDirection: 'column', gap: 14 }}
        >
          <div>
            <label
              style={{
                fontSize: '11px',
                fontWeight: 600,
                color: 'var(--text-muted)',
                display: 'block',
                marginBottom: 4,
              }}
            >
              项目名称
            </label>
            <input
              type="text"
              className="input"
              placeholder="例如：数据分析工作区"
              value={newProjName}
              onChange={(e) => setNewProjName(e.target.value)}
              autoFocus
            />
          </div>

          <div>
            <label
              style={{
                fontSize: '11px',
                fontWeight: 600,
                color: 'var(--text-muted)',
                display: 'block',
                marginBottom: 4,
              }}
            >
              工作区绑定目录路径
            </label>
            <input
              type="text"
              className="input"
              placeholder="/Users/operant/workspace/..."
              value={newProjPath}
              onChange={(e) => {
                setNewProjPath(e.target.value);
                setPathCustomized(true);
              }}
            />
            <span
              style={{
                fontSize: '11px',
                color: 'var(--text-muted)',
                marginTop: 4,
                display: 'block',
              }}
            >
              绑定本地文件系统 workspace 路径，工作流与实例将归属该工作区。
            </span>
          </div>

          <div>
            <label
              style={{
                fontSize: '11px',
                fontWeight: 600,
                color: 'var(--text-muted)',
                display: 'block',
                marginBottom: 6,
              }}
            >
              标识颜色
            </label>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              {PRESET_COLORS.map((c) => (
                <button
                  key={c}
                  type="button"
                  onClick={() => setNewProjColor(c)}
                  style={{
                    width: 26,
                    height: 26,
                    borderRadius: '50%',
                    backgroundColor: c,
                    border:
                      newProjColor === c
                        ? '2px solid var(--text-primary)'
                        : '2px solid transparent',
                    cursor: 'pointer',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    color: '#ffffff',
                  }}
                  aria-label={`选择颜色 ${c}`}
                >
                  {newProjColor === c && <Check size={14} />}
                </button>
              ))}
            </div>
          </div>
        </form>
      </Modal>

      {/* 新建工作流实例 Modal */}
      <Modal
        isOpen={newInstanceModalOpen}
        onClose={() => setNewInstanceModalOpen(false)}
        title="新建工作流实例"
        footer={
          <>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => setNewInstanceModalOpen(false)}
            >
              取消
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={handleCreateInstanceSubmit}
              disabled={!newInstanceTplId}
            >
              <Plus size={14} />
              <span>创建实例</span>
            </button>
          </>
        }
      >
        <form
          onSubmit={(e) => {
            e.preventDefault();
            handleCreateInstanceSubmit();
          }}
          style={{ display: 'flex', flexDirection: 'column', gap: 14 }}
        >
          <div>
            <label
              style={{
                fontSize: '12px',
                fontWeight: 600,
                color: 'var(--text-primary)',
                display: 'block',
                marginBottom: 6,
              }}
            >
              选择工作流模板
            </label>
            <select
              className="select"
              value={newInstanceTplId}
              onChange={(e) => setNewInstanceTplId(e.target.value)}
              autoFocus
            >
              {templates.length > 0 ? (
                templates.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name} ({t.kind === 'published' ? `v${t.version}` : '另存模板'}) ·{' '}
                    {t.nodeCount} 节点
                  </option>
                ))
              ) : (
                <option value="">暂无可用工作流模板</option>
              )}
            </select>
            {templates.length === 0 && (
              <span
                style={{
                  fontSize: '11px',
                  color: 'var(--status-warn-text, #ea580c)',
                  marginTop: 4,
                  display: 'block',
                }}
              >
                提示：请先在协作画布中发布工作流模板。
              </span>
            )}
          </div>

          <div>
            <label
              style={{
                fontSize: '12px',
                fontWeight: 600,
                color: 'var(--text-primary)',
                display: 'block',
                marginBottom: 6,
              }}
            >
              归属项目工作区
            </label>
            <select
              className="select"
              value={newInstanceProjectId}
              onChange={(e) => setNewInstanceProjectId(e.target.value)}
            >
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} {p.isDefault ? '（默认工作区）' : ''}
                </option>
              ))}
            </select>
            <span
              style={{
                fontSize: '11px',
                color: 'var(--text-muted)',
                marginTop: 4,
                display: 'block',
              }}
            >
              实例将归属该项目工作区，并在该目录上下文下运行。
            </span>
          </div>

          <div>
            <label
              style={{
                fontSize: '12px',
                fontWeight: 600,
                color: 'var(--text-primary)',
                display: 'block',
                marginBottom: 6,
              }}
            >
              实例名称（可选）
            </label>
            <input
              type="text"
              className="input"
              value={newInstanceTitle}
              onChange={(e) => setNewInstanceTitle(e.target.value)}
              placeholder="留空自动生成（例如：自主功能交付图 · 实例 2）"
            />
          </div>
        </form>
      </Modal>
    </div>
  );
};
