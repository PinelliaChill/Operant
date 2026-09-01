/**
 * 会话分区情境侧栏（设计基线 §3.1 + v12 侧栏项目树与分组架构）
 * 自上而下：搜索框与全局新建 / 📌 置顶（若有）/ 按项目工作区分组的会话树 /
 * 临时工作流（tempWorkflows 非空时显示）/ 演示模式开关卡。
 * 项目分组 Header：标识色圆点 + 项目名 + 默认标记 + 路径 + 会话数徽标 + 折叠 Chevron + hover 快捷「+」新建；
 * 折叠状态持久化于 localStorage.operant.chat.collapsed_projects。
 */

import React, { useState } from 'react';
import { useMatch, useNavigate } from 'react-router-dom';
import { ChevronRight, GitBranch, PanelLeftClose, Plus, Search } from 'lucide-react';
import { useOperant } from '../../context/ClientContext';
import { useDemo } from '../../demo/DemoContext';
import { Modal } from '../../components/Modal';
import type { DemoConversation } from '../../demo/types';
import { formatRelativeDay } from '../../lib/format';

/** 临时工作流详情 Modal 的演示节点名清单（5 节点演示值，与 store 的 nodeCount 一致） */
const TEMP_WF_NODE_NAMES = [
  '规划节点 · 拆解会话目标',
  '探索节点 · 收集上下文',
  '编码节点 · 独占写入实现',
  '评审节点 · 只读裁决',
  '汇总节点 · 输出结论',
];

interface ChatSidebarProps {
  /** 行点击跳转后回调（移动端用于关闭覆盖抽屉） */
  onNavigate?: () => void;
  /** 收起侧栏回调（v6 §3：RailLayout 全宽度注入——桌面收起内联侧栏为把手，移动端关闭抽屉） */
  onCollapse?: () => void;
}

export const ChatSidebar: React.FC<ChatSidebarProps> = ({ onNavigate, onCollapse }) => {
  const navigate = useNavigate();
  const { clientMode, setClientMode } = useOperant();
  const { conversations, projects, agents, tempWorkflows, persistTempWorkflow, createConversation } =
    useDemo();
  const chatMatch = useMatch('/chat/:conversationId');
  const activeConversationId = chatMatch?.params.conversationId;
  const [query, setQuery] = useState('');

  // 折叠项目 ID 集合（持久化于 localStorage.operant.chat.collapsed_projects）
  const [collapsedProjects, setCollapsedProjects] = useState<string[]>(() => {
    try {
      const raw = localStorage.getItem('operant.chat.collapsed_projects');
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
        localStorage.setItem('operant.chat.collapsed_projects', JSON.stringify(next));
      } catch {
        /* ignore localStorage quota/disabled errors */
      }
      return next;
    });
  };

  // 临时工作流详情 Modal（行点击打开；Esc/遮罩关闭、焦点还原由 Modal 自带）
  const [tempModalId, setTempModalId] = useState<string | null>(null);
  const tempModal = tempWorkflows.find((t) => t.id === tempModalId);

  // 新建会话 Modal 状态
  const [newChatModalOpen, setNewChatModalOpen] = useState(false);
  const [newChatTitle, setNewChatTitle] = useState('');
  const defaultProjectId = projects.find((p) => p.isDefault)?.id || projects[0]?.id || 'proj_workspace';
  const [newChatProjectId, setNewChatProjectId] = useState(defaultProjectId);
  const [newChatAgentId, setNewChatAgentId] = useState('agent_assistant');

  const normalizedQuery = query.trim().toLowerCase();
  const matchesQuery = (c: DemoConversation) =>
    !normalizedQuery || c.title.toLowerCase().includes(normalizedQuery);

  // 已归档实例不出现在会话侧栏（归档实例只在协作总览的归档区查看）
  const visibleConversations = conversations.filter((c) => c.lifecycle !== 'archived');
  const pinnedList = visibleConversations.filter((c) => c.pinned && matchesQuery(c));

  // 项目列表按默认工作区优先排序
  const sortedProjects = [...projects].sort((a, b) => (a.isDefault ? -1 : b.isDefault ? 1 : 0));

  const goConversation = (id: string) => {
    navigate(`/chat/${id}`);
    onNavigate?.();
  };

  const openNewChatModal = (projectId?: string) => {
    setNewChatTitle('');
    setNewChatProjectId(projectId || defaultProjectId);
    setNewChatAgentId('agent_assistant');
    setNewChatModalOpen(true);
  };

  const renderConversationRow = (c: DemoConversation, subtitleTime: boolean) => (
    <button
      key={c.id}
      className={`rail-sidebar-row${c.id === activeConversationId ? ' active' : ''}`}
      onClick={() => goConversation(c.id)}
      aria-current={c.id === activeConversationId ? 'page' : undefined}
    >
      <span className="rail-sidebar-row-main">
        <span className="rail-sidebar-row-title">{c.title}</span>
        {subtitleTime && (
          <span className="rail-sidebar-row-sub">{formatRelativeDay(c.updatedAt)}</span>
        )}
      </span>
      {c.workflowId && <span className="rail-sidebar-badge-group">群聊</span>}
      {c.unread !== undefined && c.unread > 0 && (
        <span className="rail-sidebar-unread" aria-label={`${c.unread} 条未读`}>
          {c.unread}
        </span>
      )}
      {!subtitleTime && (
        <span className="rail-sidebar-time">{formatRelativeDay(c.updatedAt)}</span>
      )}
    </button>
  );

  return (
    <div className="rail-sidebar-inner">
      {/* 1. 搜索框与新建会话按钮 */}
      <div className="rail-sidebar-header">
        <div className="rail-sidebar-search">
          <Search size={14} aria-hidden="true" />
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索对话"
            aria-label="搜索对话"
            className="rail-sidebar-search-input"
          />
        </div>
        <button
          type="button"
          className="btn btn-ghost btn-icon"
          onClick={() => openNewChatModal(defaultProjectId)}
          aria-label="新建会话"
          title="新建会话"
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
        {/* 2. 置顶组 */}
        {pinnedList.length > 0 && (
          <div className="rail-sidebar-group">
            <div className="rail-sidebar-group-title">📌 置顶</div>
            {pinnedList.map((c) => renderConversationRow(c, true))}
          </div>
        )}

        {/* 3. 按项目工作区分组的树形会话列表 */}
        <div className="rail-sidebar-group" style={{ marginTop: pinnedList.length > 0 ? 12 : 6 }}>
          <div className="rail-sidebar-group-title">项目工作区</div>
          {sortedProjects.map((p) => {
            const isCollapsed = collapsedProjects.includes(p.id);
            const projectConversations = visibleConversations
              .filter(
                (c) =>
                  !c.pinned &&
                  (c.projectId === p.id || (!c.projectId && p.isDefault)) &&
                  matchesQuery(c)
              )
              .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));

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
                  aria-label={`项目 ${p.name}，${projectConversations.length} 个会话`}
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
                      title={`${projectConversations.length} 个会话`}
                    >
                      {projectConversations.length}
                    </span>
                    <button
                      type="button"
                      className="rail-sidebar-project-add"
                      onClick={(e) => {
                        e.stopPropagation();
                        openNewChatModal(p.id);
                      }}
                      title={`在 ${p.name} 中新建会话`}
                      aria-label={`在 ${p.name} 中新建会话`}
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
                    {projectConversations.length > 0 ? (
                      projectConversations.map((c) => renderConversationRow(c, false))
                    ) : (
                      <div className="rail-sidebar-empty">
                        {normalizedQuery ? '无匹配会话' : '暂无会话'}
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>

        {/* 4. 临时工作流组（展示在项目列表下方） */}
        {tempWorkflows.length > 0 && (
          <div className="rail-sidebar-group">
            <div className="rail-sidebar-group-title">临时工作流</div>
            {tempWorkflows.map((t) => (
              <div
                key={t.id}
                className="rail-sidebar-row"
                role="button"
                tabIndex={0}
                onClick={() => setTempModalId(t.id)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    setTempModalId(t.id);
                  }
                }}
                aria-label={`查看临时工作流 ${t.name}`}
              >
                <GitBranch size={14} className="rail-sidebar-temp-icon" aria-hidden="true" />
                <span className="rail-sidebar-row-main">
                  <span className="rail-sidebar-row-title">{t.name}</span>
                  <span className="rail-sidebar-row-sub">{t.nodeCount} 节点</span>
                </span>
                <span
                  className={`rail-sidebar-badge-temp${t.status === 'saved_as' ? ' saved' : ''}`}
                >
                  {t.status === 'saved_as' ? '已另存' : '临时'}
                </span>
                {t.status === 'temp' && (
                  <button
                    className="rail-sidebar-temp-persist"
                    onClick={(e) => {
                      e.stopPropagation();
                      persistTempWorkflow(t.id);
                    }}
                  >
                    持久化
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 5. 底部演示模式开关卡 */}
      <div className="rail-sidebar-footer">
        <div className="rail-sidebar-mode-card">
          <div className="rail-sidebar-mode-text">
            <span className="rail-sidebar-mode-title">演示模式</span>
            <span className="rail-sidebar-mode-desc">
              {clientMode === 'mock' ? '使用内置演示数据' : '已连接 Core 后端'}
            </span>
          </div>
          <button
            role="switch"
            aria-checked={clientMode === 'mock'}
            aria-label="演示模式开关"
            className="switch"
            onClick={() => setClientMode(clientMode === 'mock' ? 'live' : 'mock')}
          />
        </div>
      </div>

      {/* 临时工作流详情 Modal */}
      <Modal
        isOpen={tempModal !== undefined}
        onClose={() => setTempModalId(null)}
        title={tempModal?.name ?? '临时工作流'}
        footer={
          <>
            {tempModal?.status === 'temp' && (
              <button
                className="btn btn-primary"
                onClick={() => {
                  persistTempWorkflow(tempModal.id);
                  setTempModalId(null);
                }}
              >
                持久化
              </button>
            )}
            <button className="btn btn-secondary" onClick={() => setTempModalId(null)}>
              关闭
            </button>
          </>
        }
      >
        <p className="temp-wf-note">
          临时工作流仅绑定本会话；持久化后将另存为正式工作流并进入工作流目录，会话绑定保持不变。
        </p>
        <div className="temp-wf-node-title">
          节点清单（{tempModal?.nodeCount ?? TEMP_WF_NODE_NAMES.length} 节点 ·{' '}
          {tempModal?.edgeCount ?? 4} 连线）
        </div>
        <ul className="temp-wf-node-list">
          {TEMP_WF_NODE_NAMES.map((name) => (
            <li key={name}>
              <GitBranch size={12} aria-hidden="true" />
              <span>{name}</span>
            </li>
          ))}
        </ul>
      </Modal>

      {/* 新建会话 Modal */}
      <Modal
        isOpen={newChatModalOpen}
        onClose={() => setNewChatModalOpen(false)}
        title="新建会话"
        footer={
          <>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => setNewChatModalOpen(false)}
            >
              取消
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => {
                const title = newChatTitle.trim() || '新会话';
                const newId = createConversation(title, newChatProjectId, newChatAgentId);
                setNewChatModalOpen(false);
                navigate(`/chat/${newId}`);
                onNavigate?.();
              }}
            >
              创建并进入
            </button>
          </>
        }
      >
        <form
          onSubmit={(e) => {
            e.preventDefault();
            const title = newChatTitle.trim() || '新会话';
            const newId = createConversation(title, newChatProjectId, newChatAgentId);
            setNewChatModalOpen(false);
            navigate(`/chat/${newId}`);
            onNavigate?.();
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
              会话标题
            </label>
            <input
              type="text"
              className="input"
              value={newChatTitle}
              onChange={(e) => setNewChatTitle(e.target.value)}
              placeholder="例如：分析前端性能架构与优化"
              autoFocus
            />
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
              value={newChatProjectId}
              onChange={(e) => setNewChatProjectId(e.target.value)}
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
              会话将绑定该项目的上下文文件与知识库范围。
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
              主交互助手
            </label>
            <select
              className="select"
              value={newChatAgentId}
              onChange={(e) => setNewChatAgentId(e.target.value)}
            >
              {agents.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name} ({a.model})
                </option>
              ))}
            </select>
          </div>
        </form>
      </Modal>
    </div>
  );
};
