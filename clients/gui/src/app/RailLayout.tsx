/**
 * RailLayout：Shell v2 壳层（取代 ShellLayout）
 * 结构：IconRail 64px + ContextSidebar 280px（随分区切换内容）+ Main + StatusBar 32px。
 * 响应式：
 * - <960px：IconRail 隐藏改底部 TabBar（会话/任务/项目/设置）；ContextSidebar 变覆盖抽屉
 *   （复用 components/Drawer：遮罩 + Esc + 焦点管理 + 焦点还原）；StatusBar 隐藏。
 * - >=960px（所有桌面宽度）：ContextSidebar 内联且可收起为细竖把手（v6 §3），展开状态按
 *   模式持久化 localStorage（operant.panel.chatSidebar / operant.panel.collabSidebar），
 *   ChatSidebar 与 CollabSidebar 互不影响；rail 不再设"会话"项（v6 §2），
 *   顶部模式切换钮（v6 §1）为桌面端对话/协作唯一入口。
 */

import React, { useState } from 'react';
import { NavLink, Outlet, useLocation, useMatch } from 'react-router-dom';
import {
  MessageSquare,
  ListTodo,
  ShieldCheck,
  CalendarClock,
  FolderKanban,
  Bot,
  Puzzle,
  Sparkles,
  Settings,
  Sun,
  Moon,
  X,
} from 'lucide-react';
import type { ContextRevision } from '@operant/sdk';
import { useOperant } from '../context/ClientContext';
import { useDemo } from '../demo/DemoContext';
import { Drawer } from '../components/Drawer';
import { ModeSwitchButton } from '../components/ModeSwitchButton';
import { PanelCollapseHandle, usePanelExpanded } from '../components/CollapsiblePanel';
import { StatusBadge } from '../components/StatusBadge';
import { ContextRing } from '../components/ContextRing';
import { ChatSidebar } from '../features/chat/ChatSidebar';
import { getWorkflowDisplayName } from '../features/chat/chatUtils';
import { CollabSidebar } from '../features/collab/CollabSidebar';
import { APPROVAL_MODE_LABELS } from '../demo/DemoContext';
import { useLive } from '../live/LiveContext';
import { LiveUnavailableView } from '../live/LiveUnavailableView';

/** 监听 CSS 媒体查询，用于壳层响应式行为（内联/覆盖抽屉切换） */
function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState<boolean>(() => window.matchMedia(query).matches);

  React.useEffect(() => {
    const mql = window.matchMedia(query);
    const handler = (e: MediaQueryListEvent) => setMatches(e.matches);
    mql.addEventListener('change', handler);
    setMatches(mql.matches);
    return () => mql.removeEventListener('change', handler);
  }, [query]);

  return matches;
}

/** 顶部分区 + 底部设置（v6 §2：移除"会话"项——对话/协作经顶部模式切换钮进入，底部 TabBar 保留会话） */
const RAIL_SECTIONS = [
  { to: '/tasks', label: '任务', icon: ListTodo },
  { to: '/approvals', label: '审批', icon: ShieldCheck },
  { to: '/schedules', label: '调度', icon: CalendarClock },
  { to: '/projects', label: '项目', icon: FolderKanban },
  { to: '/agents', label: 'Agent', icon: Bot },
  { to: '/extensions', label: '插件与MCP', icon: Puzzle },
  { to: '/skills', label: '技能', icon: Sparkles },
] as const;

const TABBAR_ITEMS = [
  { to: '/chat', label: '会话', icon: MessageSquare },
  { to: '/tasks', label: '任务', icon: ListTodo },
  { to: '/projects', label: '项目', icon: FolderKanban },
  { to: '/settings', label: '设置', icon: Settings },
] as const;

/** 分区 → document.title 中文名（runs 为运行详情内容路由；/workflow 深链归协作分区；未知分区回退"会话"） */
const SECTION_TITLES: Record<string, string> = {
  chat: '会话',
  collab: '协作',
  // /workflow/* 保留深链（群聊 / Agent 个人界面）与 StatusBar 一致归协作分区
  workflow: '协作',
  tasks: '任务',
  approvals: '审批',
  schedules: '调度',
  projects: '项目',
  agents: 'Agent',
  extensions: '插件与MCP',
  skills: '技能',
  settings: '设置',
  runs: '运行进度',
};

/** 协作模式上抛给 StatusBar 的摘要：canvas=画布草稿/只读工作流摘要；summary=总览/运行 Tab 的草稿与运行计数 */
export type CollabStatusInfo =
  | { kind: 'canvas'; draftName: string; nodeCount: number; edgeCount: number }
  | { kind: 'summary'; draftCount: number; runCount: number };

/** Outlet context：向会话/协作分区传递侧栏开启入口与 StatusBar 数据通道 */
export interface RailOutletContext {
  /** 是否显示"打开侧栏"按钮（会话/协作分区：移动端抽屉收起或桌面侧栏收起为把手时） */
  showSidebarOpenBtn: boolean;
  openSidebar: () => void;
  isMobile: boolean;
  /** 协作模式（CollabView）上报画布摘要；离开协作模式时传 null 清除 */
  setCollabStatus: (info: CollabStatusInfo | null) => void;
}

/** Mock 状态栏：仅在明确演示模式渲染 Demo Projection。 */
const DemoRailStatusBar: React.FC<{ collabStatus: CollabStatusInfo | null }> = ({ collabStatus }) => {
  const { activeWorkspace, connectionStatus } = useOperant();
  const { conversations, agents, getConversationStats, approvalPolicy, pendingApprovals } =
    useDemo();
  const location = useLocation();
  const chatMatch = useMatch('/chat/:conversationId');
  const isChatSection = location.pathname.startsWith('/chat');
  const isApprovalsSection = location.pathname.startsWith('/approvals');
  // /workflow/* 深链（群聊 / Agent 个人界面）保持协作分支文案
  const isCollabSection =
    location.pathname.startsWith('/collab') || location.pathname.startsWith('/workflow');

  if (isApprovalsSection) {
    // 审批分区：左栏分区名"审批"，中栏显示 审批 · {当前策略模式名} · 待处理 N 项（随策略切换即时更新）
    return (
      <footer className="rail-statusbar">
        <div className="rail-statusbar-side">
          <span className="rail-statusbar-text">审批</span>
        </div>
        <div className="rail-statusbar-side">
          <span className="rail-statusbar-text">
            审批 · {APPROVAL_MODE_LABELS[approvalPolicy.mode]} · 待处理 {pendingApprovals.length} 项
          </span>
        </div>
      </footer>
    );
  }

  if (isCollabSection) {
    return (
      <footer className="rail-statusbar">
        <div className="rail-statusbar-side">
          <span className="rail-statusbar-text">协作工作台</span>
        </div>
        <div className="rail-statusbar-side">
          {collabStatus?.kind === 'canvas' && (
            <>
              <span className="rail-statusbar-text">{collabStatus.draftName}</span>
              <span aria-hidden="true">•</span>
              <span className="rail-statusbar-text">
                {collabStatus.nodeCount} 节点 · {collabStatus.edgeCount} 连线
              </span>
            </>
          )}
          {collabStatus?.kind === 'summary' && (
            <span className="rail-statusbar-text">
              {collabStatus.draftCount} 个草稿 · {collabStatus.runCount} 个运行进度
            </span>
          )}
        </div>
      </footer>
    );
  }

  if (isChatSection) {
    // 未选中会话时回退到首个置顶会话，保证状态栏始终有演示上下文
    const conversationId =
      chatMatch?.params.conversationId ??
      conversations.find((c) => c.pinned)?.id ??
      conversations[0]?.id;
    const conversation = conversations.find((c) => c.id === conversationId);
    const isGroupChat = Boolean(conversation?.workflowId);
    // 对话模式纯净化：左侧仅主助手名 · 模型（无多 Agent 彩点连排）；
    // v4 §2：群聊会话左侧改显"群聊 · {workflow 名称}"
    const leadAgentId = conversation?.agentIds[0];
    const leadAgent = leadAgentId ? agents.find((a) => a.id === leadAgentId) : undefined;
    const stats = getConversationStats(conversationId ?? '');
    const tokensText =
      stats.tokens >= 1000 ? `${(stats.tokens / 1000).toFixed(1)}k` : `${stats.tokens}`;

    // 仅环的小号 ContextRing（文字明细由 CSS 隐藏）
    const demoRevision: ContextRevision = {
      revision_id: 'demo_rev_statusbar',
      thread_id: conversationId ?? 'demo',
      total_tokens: stats.tokens,
      context_window_limit: 128000,
      components: [],
      created_at: new Date().toISOString(),
    };

    return (
      <footer className="rail-statusbar">
        <div className="rail-statusbar-side">
          <span className="rail-statusbar-text">
            {isGroupChat
              ? `群聊 · ${getWorkflowDisplayName(conversation?.workflowId ?? '')}`
              : leadAgent
                ? `${leadAgent.name} · ${leadAgent.model}`
                : '暂无主助手'}
          </span>
        </div>

        <div className="rail-statusbar-side">
          <span className="rail-statusbar-text">{stats.toolCount} 个工具</span>
          <span aria-hidden="true">•</span>
          <span className="rail-statusbar-ring">
            <ContextRing revision={demoRevision} size={18} strokeWidth={3} hideCacheMetric />
          </span>
          <span className="rail-statusbar-text">{tokensText} tokens</span>
        </div>
      </footer>
    );
  }

  return (
    <footer className="rail-statusbar">
      <div className="rail-statusbar-side">
        <span className="rail-statusbar-text">
          工作区：{activeWorkspace.split('/').pop() || activeWorkspace}
        </span>
      </div>
      <div className="rail-statusbar-side">
        <StatusBadge status={connectionStatus} size="sm" pulse={connectionStatus === 'connected'} />
      </div>
    </footer>
  );
};

/** Live 状态栏：只显示 LiveProvider 的 Core/SSE 状态，不读取演示统计。 */
const LiveRailStatusBar: React.FC = () => {
  const { activeWorkspace } = useOperant();
  const { phase, stream, selectedThread, projectionStale } = useLive();
  const status = phase === 'error' ? 'disconnected' : phase === 'ready' && stream.status !== 'replaying' ? 'connected' : 'pending';
  const label = phase === 'error'
    ? 'Core 连接失败'
    : stream.status === 'replaying'
      ? 'SSE 回放中'
      : phase === 'ready'
        ? 'Core 已连接'
        : 'Core 连接中';
  return (
    <footer className="rail-statusbar" data-client-mode="live">
      <div className="rail-statusbar-side">
        <span className="rail-statusbar-text">Workspace：{selectedThread?.workspace || activeWorkspace}</span>
      </div>
      <div className="rail-statusbar-side">
        {projectionStale && <span className="rail-statusbar-text">Projection 待校正 · </span>}
        <StatusBadge status={status} label={label} size="sm" pulse={status === 'pending'} />
      </div>
    </footer>
  );
};

/** 状态栏根据显式客户端模式分支，避免 Live 页面混入 Demo 统计。 */
const RailStatusBar: React.FC<{ collabStatus: CollabStatusInfo | null }> = (props) => {
  const { clientMode } = useOperant();
  return clientMode === 'live' ? <LiveRailStatusBar /> : <DemoRailStatusBar {...props} />;
};

/**
 * Mobile keeps the context sidebar in a closed drawer, so its Demo mode card
 * is not a persistent signal. Keep this status marker outside the drawer and
 * toast tray; desktop layout remains unchanged while narrow screens always
 * expose the active data source.
 */
const DemoModeIndicator: React.FC = () => (
  <div
    className="demo-mode-indicator"
    data-client-mode="mock"
    role="status"
    aria-live="polite"
    aria-label="当前使用演示数据"
  >
    <span className="demo-mode-indicator-dot" aria-hidden="true" />
    <span>演示数据</span>
  </div>
);

export const RailLayout: React.FC = () => {
  const { theme, toggleTheme, notifications, removeNotification, clientMode } = useOperant();
  const location = useLocation();
  const section = location.pathname.split('/')[1] || 'chat';

  // Phase 1E surfaces plus the additive Phase 2/3 collaboration runtime may
  // render in live mode. Other routes stay explicitly demo-only.
  const liveSupportedSection =
    section === 'chat' ||
    section === 'projects' ||
    section === 'approvals' ||
    section === 'collab';

  const isMobile = useMediaQuery('(max-width: 959px)');

  // 桌面（>=960 所有宽度）情境侧栏展开状态：按模式持久化（v6 §3），默认展开、互不影响
  const [chatSidebarExpanded, setChatSidebarExpanded] = usePanelExpanded(
    'operant.panel.chatSidebar'
  );
  const [collabSidebarExpanded, setCollabSidebarExpanded] = usePanelExpanded(
    'operant.panel.collabSidebar'
  );
  // 移动端（<960）覆盖抽屉开关：不持久化，维持抽屉行为
  const [drawerOpen, setDrawerOpen] = useState(false);

  const isCollabSection = section === 'collab';
  const sidebarExpanded = isCollabSection ? collabSidebarExpanded : chatSidebarExpanded;

  // 侧栏宽度可拖拽调整（180px ~ 480px，<160px 自动折叠收起，持久化至 operant.sidebar.width）
  const [sidebarWidth, setSidebarWidth] = useState<number>(() => {
    const saved = localStorage.getItem('operant.sidebar.width');
    if (saved) {
      const parsed = parseInt(saved, 10);
      if (!Number.isNaN(parsed) && parsed >= 180 && parsed <= 480) {
        return parsed;
      }
    }
    return 280;
  });
  const [isResizing, setIsResizing] = useState(false);
  const isResizingRef = React.useRef(false);

  const persistSidebarWidth = React.useCallback((nextWidth: number): number => {
    const clamped = Math.min(480, Math.max(180, nextWidth));
    setSidebarWidth(clamped);
    localStorage.setItem('operant.sidebar.width', clamped.toString());
    return clamped;
  }, []);

  /** 收起当前分区侧栏：移动端=关闭抽屉；桌面=收起内联侧栏（按模式持久化） */
  const collapseSidebar = React.useCallback(() => {
    if (isMobile) {
      setDrawerOpen(false);
    } else if (isCollabSection) {
      setCollabSidebarExpanded(false);
    } else {
      setChatSidebarExpanded(false);
    }
  }, [isMobile, isCollabSection, setCollabSidebarExpanded, setChatSidebarExpanded]);

  /** 拖拽开始：监听 window mousemove 与 mouseup */
  const handleResizerMouseDown = React.useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      isResizingRef.current = true;
      setIsResizing(true);
      const startX = e.clientX;
      const startWidth = sidebarWidth;

      const handleMouseMove = (moveEvent: MouseEvent) => {
        if (!isResizingRef.current) return;
        const delta = moveEvent.clientX - startX;
        const newWidth = startWidth + delta;

        if (newWidth < 160) {
          // 向左拖动小于 160px 时，自动触发收起折叠
          isResizingRef.current = false;
          setIsResizing(false);
          window.removeEventListener('mousemove', handleMouseMove);
          window.removeEventListener('mouseup', handleMouseUp);
          collapseSidebar();
          return;
        }

        persistSidebarWidth(newWidth);
      };

      const handleMouseUp = () => {
        isResizingRef.current = false;
        setIsResizing(false);
        window.removeEventListener('mousemove', handleMouseMove);
        window.removeEventListener('mouseup', handleMouseUp);
      };

      window.addEventListener('mousemove', handleMouseMove);
      window.addEventListener('mouseup', handleMouseUp);
    },
    [collapseSidebar, persistSidebarWidth, sidebarWidth]
  );

  /** 侧栏处于收起折叠状态下，在 Rail 右边缘按住向右拖拽直接展开侧栏 */
  const handleExpandResizerMouseDown = React.useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      const startX = e.clientX;
      let hasExpanded = false;
      isResizingRef.current = true;
      setIsResizing(true);

      const handleMouseMove = (moveEvent: MouseEvent) => {
        if (!isResizingRef.current) return;
        const delta = moveEvent.clientX - startX;

        // 向右拖动位移 > 60px 时，自动触发展开
        if (!hasExpanded && delta > 60) {
          hasExpanded = true;
          if (isCollabSection) {
            setCollabSidebarExpanded(true);
          } else {
            setChatSidebarExpanded(true);
          }
        }

        if (hasExpanded) {
          persistSidebarWidth(delta);
        }
      };

      const handleMouseUp = () => {
        isResizingRef.current = false;
        setIsResizing(false);
        window.removeEventListener('mousemove', handleMouseMove);
        window.removeEventListener('mouseup', handleMouseUp);
      };

      window.addEventListener('mousemove', handleMouseMove);
      window.addEventListener('mouseup', handleMouseUp);
    },
    [isCollabSection, persistSidebarWidth, setCollabSidebarExpanded, setChatSidebarExpanded]
  );

  /** 展开当前分区侧栏（与头部"打开侧栏"按钮共用：移动端开抽屉 / 桌面展开内联侧栏） */
  const openSidebar = React.useCallback(() => {
    if (isMobile) {
      setDrawerOpen(true);
    } else if (isCollabSection) {
      setCollabSidebarExpanded(true);
    } else {
      setChatSidebarExpanded(true);
    }
  }, [isMobile, isCollabSection, setCollabSidebarExpanded, setChatSidebarExpanded]);

  const handleResizerKeyDown = React.useCallback((event: React.KeyboardEvent<HTMLDivElement>) => {
    const step = event.shiftKey ? 40 : 16;
    let nextWidth: number | undefined;
    if (event.key === 'ArrowLeft') nextWidth = sidebarWidth - step;
    if (event.key === 'ArrowRight') nextWidth = sidebarWidth + step;
    if (event.key === 'Home') nextWidth = 180;
    if (event.key === 'End') nextWidth = 480;
    if (nextWidth === undefined) return;
    event.preventDefault();
    persistSidebarWidth(nextWidth);
  }, [persistSidebarWidth, sidebarWidth]);

  const handleExpandResizerKeyDown = React.useCallback((event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== 'ArrowRight' && event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    openSidebar();
  }, [openSidebar]);

  // 协作模式画布摘要（CollabView 经 Outlet context 上抛，供 StatusBar 消费）
  const [collabStatus, setCollabStatus] = useState<CollabStatusInfo | null>(null);

  // 窄屏路由切换时自动收起侧栏抽屉：仅移动断点生效（桌面内联侧栏不误收）；
  // 依赖 pathname 而非 location 整体，同路径的查询/Tab 参数变化（如 ?view=canvas）不重复打扰
  React.useEffect(() => {
    if (isMobile) setDrawerOpen(false);
  }, [location.pathname, isMobile]);

  // 跨到桌面断点时兜底复位抽屉开关（桌面由按模式持久化的展开状态接管）
  React.useEffect(() => {
    if (!isMobile) setDrawerOpen(false);
  }, [isMobile]);

  // 页面 title 随分区变化（SPA 持续有效，卸载无需还原）
  React.useEffect(() => {
    document.title = `${SECTION_TITLES[section] ?? SECTION_TITLES.chat} — Operant`;
  }, [section]);

  // Live 仅保留 Core Thread 侧栏；协作草稿侧栏属于明确的 Demo-only 面。
  const hasSidebar = clientMode === 'live' ? section === 'chat' : section === 'chat' || section === 'collab';
  // >=960 所有宽度内联且可收起为把手（v6 §3）；<960 维持覆盖抽屉
  const sidebarInline = hasSidebar && !isMobile && sidebarExpanded;
  const showSidebarHandle = hasSidebar && !isMobile && !sidebarExpanded;
  const sidebarDrawer = hasSidebar && isMobile && drawerOpen;
  // Keep the mobile trigger mounted while the drawer is open so Esc/close can
  // restore keyboard focus to a live element instead of an unmounted button.
  const showSidebarOpenBtn = hasSidebar && (isMobile ? true : !sidebarExpanded);
  // onCollapse 全宽度注入（v6 §3）：桌面收起内联侧栏 / 移动端关闭抽屉；
  // onNavigate 供抽屉内行点击关闭抽屉（桌面内联为幂等空操作）
  const contextSidebar = isCollabSection ? (
    <CollabSidebar onCollapse={collapseSidebar} onNavigate={() => setDrawerOpen(false)} />
  ) : (
    <ChatSidebar onCollapse={collapseSidebar} onNavigate={() => setDrawerOpen(false)} />
  );

  return (
    <div className="rail-container">
      {/* 跳转链接：仅键盘焦点时可见 */}
      <a href="#main-content" className="skip-link">
        跳到主内容
      </a>

      <div className="rail-body">
        {/* IconRail 64px（<960px 隐藏，改底部 TabBar） */}
        <nav className="rail-icon-rail" aria-label="分区导航">
          {/* 唯一模式切换钮（v6 §1）："会话"图标上方，恒显当前模式图标 + 菜单切换 */}
          <ModeSwitchButton />
          {RAIL_SECTIONS.map((item) => {
            const Icon = item.icon;
            return (
              <NavLink
                key={item.to}
                to={item.to}
                className={({ isActive }) => `rail-nav-item${isActive ? ' active' : ''}`}
                aria-label={item.label}
                title={item.label}
              >
                <Icon size={20} />
              </NavLink>
            );
          })}

          <div className="rail-icon-rail-spacer" />

          <button
            className="rail-nav-item"
            onClick={toggleTheme}
            aria-label={theme === 'light' ? '切换到深色主题' : '切换到浅色主题'}
            title={theme === 'light' ? '切换到深色主题' : '切换到浅色主题'}
          >
            {theme === 'light' ? <Moon size={20} /> : <Sun size={20} />}
          </button>
          <NavLink
            to="/settings"
            className={({ isActive }) => `rail-nav-item${isActive ? ' active' : ''}`}
            aria-label="设置"
            title="设置"
          >
            <Settings size={20} />
          </NavLink>
        </nav>

        {/* ContextSidebar：>=960 内联（可拖拽调整宽度，可收起为细把手）；<960 走覆盖抽屉 */}
        {sidebarInline && (
          <aside
            className={`rail-sidebar${isResizing ? ' resizing' : ''}`}
            style={{
              width: `${sidebarWidth}px`,
              minWidth: `${sidebarWidth}px`,
              maxWidth: `${sidebarWidth}px`,
            }}
            aria-label="情境侧栏"
          >
            {contextSidebar}
            <div
              className="rail-sidebar-resizer"
              onMouseDown={handleResizerMouseDown}
              onKeyDown={handleResizerKeyDown}
              role="separator"
              aria-orientation="vertical"
              aria-valuemin={180}
              aria-valuemax={480}
              aria-valuenow={sidebarWidth}
              aria-valuetext={`${sidebarWidth}px`}
              tabIndex={0}
              aria-label="拖拽调整侧栏宽度"
              title="拖拽调整侧栏宽度（向左拖至 160px 以下自动折叠收起）"
            />
          </aside>
        )}
        {showSidebarHandle && (
          <>
            <div
              className={`rail-sidebar-expand-resizer${isResizing ? ' resizing' : ''}`}
              onMouseDown={handleExpandResizerMouseDown}
              onKeyDown={handleExpandResizerKeyDown}
              role="separator"
              aria-orientation="vertical"
              aria-valuemin={180}
              aria-valuemax={480}
              aria-valuenow={sidebarWidth}
              aria-valuetext={`${sidebarWidth}px；按 Enter 或向右键展开`}
              tabIndex={0}
              aria-label="向右拖拽展开侧栏"
              title="向右拖拽直接展开侧栏（位移超过 60px 自动展开并随光标缩放宽度）"
            />
            <PanelCollapseHandle side="left" label="展开侧栏" onExpand={openSidebar} />
          </>
        )}

        {/* 主区：侧栏开启按钮由各分区经 Outlet context 静态嵌入页头，避免浮动遮挡 */}
        <main id="main-content" tabIndex={-1} className="rail-main">
          {clientMode === 'live' && !liveSupportedSection ? (
            <LiveUnavailableView section={section} />
          ) : (
            <Outlet
              context={{
                showSidebarOpenBtn,
                openSidebar,
                isMobile,
                setCollabStatus,
              }}
            />
          )}
        </main>

        {/* <960px：侧栏覆盖抽屉（Drawer 自带遮罩 / Esc / 焦点管理与还原）；
            抽屉顶部放同一个模式切换钮（一处，所有页面共用抽屉） */}
        <Drawer
          isOpen={sidebarDrawer}
          onClose={() => setDrawerOpen(false)}
          title={isCollabSection ? '协作' : '会话'}
          side="left"
          width={280}
          bodyStyle={{
            padding: 0,
            display: 'flex',
            flexDirection: 'column',
            overflow: 'hidden',
            minHeight: 0,
          }}
        >
          <div className="rail-drawer-mode">
            <ModeSwitchButton onNavigate={() => setDrawerOpen(false)} />
            <span className="rail-drawer-mode-label">
              {/* 与 RailStatusBar 口径一致：/workflow 深链同属协作语境（v6 修复轮） */}
              {location.pathname.startsWith('/collab') || location.pathname.startsWith('/workflow')
                ? '切换到对话模式'
                : '切换到协作模式'}
            </span>
          </div>
          {/* 移动端审批入口（v5 把审批中心移到 rail 一级分区后，<960px TabBar 无审批项）：
              模式钮下方一行"审批"，复用侧栏行样式，点击进 /approvals 并关闭抽屉 */}
          <div className="rail-drawer-approvals">
            <NavLink
              to="/approvals"
              className="rail-sidebar-row"
              onClick={() => setDrawerOpen(false)}
            >
              <ShieldCheck size={14} className="rail-drawer-approvals-icon" aria-hidden="true" />
              <span className="rail-sidebar-row-title">审批</span>
            </NavLink>
          </div>
          {contextSidebar}
        </Drawer>
      </div>

      {/* StatusBar 32px（<960px 隐藏） */}
      <RailStatusBar collabStatus={collabStatus} />

      {clientMode === 'mock' && <DemoModeIndicator />}

      {/* 底部 Tab Bar（<960px 显示） */}
      <nav className="bottom-tabbar" aria-label="底部导航">
        {TABBAR_ITEMS.map((item) => {
          const Icon = item.icon;
          return (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => `bottom-tabbar-item${isActive ? ' active' : ''}`}
            >
              <Icon size={18} />
              <span>{item.label}</span>
            </NavLink>
          );
        })}
      </nav>

      {/* 浮动通知托盘 */}
      {notifications.length > 0 && (
        <div className="toast-tray">
          {notifications.map((n) => (
            <div
              key={n.id}
              className="toast"
              style={{
                padding: '10px 14px',
                borderRadius: 'var(--radius-md)',
                backgroundColor: 'var(--bg-card)',
                border: `1px solid ${
                  n.type === 'error'
                    ? 'var(--status-error-border)'
                    : n.type === 'warn'
                      ? 'var(--status-warn-border)'
                      : n.type === 'success'
                        ? 'var(--status-safe-border)'
                        : 'var(--status-info-border)'
                }`,
                boxShadow: 'var(--shadow-lg)',
                fontSize: '12px',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                gap: 8,
              }}
            >
              <span>{n.message}</span>
              <button
                onClick={() => removeNotification(n.id)}
                className="btn btn-ghost btn-icon"
                style={{ padding: 2 }}
                aria-label="关闭通知"
              >
                <X size={12} />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};
