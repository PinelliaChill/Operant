/**
 * 协作模式主视图（v5 §2.2；v6 包C：运行监控更名运行进度 + 模板发布封锁）。
 * 结构：页头（标题 + 分段 chips：总览 / 编排画布 / 运行进度，?view=home|canvas|runs）
 * → 总览（默认）：WorkflowOverview 模板为主的双栏视图（模板列表 / 实例管理 / 只读画布）
 * → 画布 tab：工具行（草稿名/删除/校验/发布版本/保存草稿）+ GraphCanvas
 * → 运行 tab：MonitorPanel 运行进度
 *
 * 数据流：本地编辑态 { nodes, edges, draftName } 初始化自选中草稿（深拷贝）；
 * 仅"保存草稿"回写 client（saveGraphDraft）；切换草稿直接从 client 重载（丢弃未保存编辑）。
 * StatusBar 经 Outlet context 的 setCollabStatus 上抛：画布 Tab=草稿摘要，其余 Tab=草稿/运行计数。
 * v6 发布封锁：当前编辑草稿经 getWorkflowDirectory 映射到已发布目录条目（draftId 匹配），
 * 该模板存在活跃实例（canPublishTemplate.ok===false）时点击发布仅弹 warn 通知、不打开 Modal，
 * 并在发布按钮旁以小字提示封锁原因；从未发布过的新草稿无匹配条目，不封锁。
 * v4：多 Agent 群聊迁移为工作流群聊会话（conv_wf_delivery），旧"协作讨论"面板已移除。
 */

import React, { useCallback, useEffect, useState } from 'react';
import { useOutletContext, useSearchParams } from 'react-router-dom';
import { FileCheck, Menu, PanelLeftOpen, Save, Trash2 } from 'lucide-react';
import type {
  GraphCompilerDiagnostic,
  GraphDraft,
  GraphEdge,
  GraphNode,
  WorkflowRun,
} from '@operant/sdk';
import { useOperant } from '../../context/ClientContext';
import { useDemo } from '../../demo/DemoContext';
import type { DemoWorkflowDirectoryItem } from '../../demo/types';
import type { RailOutletContext } from '../../app/RailLayout';
import { Modal } from '../../components/Modal';
import { GraphCanvas } from './canvas/GraphCanvas';
import { removeNodeWithEdges } from './canvas/graph-utils';
import { WorkflowOverview } from './WorkflowOverview';
import { MonitorPanel } from './MonitorPanel';
import { GRAPH_DRAFTS_CHANGED_EVENT } from './CollabSidebar';
import { LiveGraphTeamView } from './LiveGraphTeamView';

type CollabTab = 'home' | 'canvas' | 'runs';

export const CollabView: React.FC = () => {
  const { clientMode } = useOperant();
  if (clientMode === 'live') return <LiveCollabView />;
  return <DemoCollabView />;
};

const LiveCollabView: React.FC = () => {
  const [searchParams, setSearchParams] = useSearchParams();
  const viewParam = searchParams.get('view');
  const activeTab: CollabTab = viewParam === 'canvas' || viewParam === 'runs' ? viewParam : 'home';

  const setTab = (tab: CollabTab) => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.set('view', tab);
      return next;
    });
  };

  return (
    <div className="section-view">
      <header className="section-header">
        <div
          style={{
            display: 'flex',
            alignItems: 'flex-start',
            justifyContent: 'space-between',
            gap: 12,
            flexWrap: 'wrap',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 }}>
            <div style={{ minWidth: 0 }}>
              <h1 className="section-title">协作工作台</h1>
              <p className="section-sub">实时 Graph 工作流与 Team 协作运行监控（Phase 2/3）</p>
            </div>
          </div>
        </div>

        <div className="section-chips" role="group" aria-label="协作视图切换">
          <button
            type="button"
            className={`section-chip${activeTab === 'home' ? ' active' : ''}`}
            aria-pressed={activeTab === 'home'}
            onClick={() => setTab('home')}
          >
            总览
          </button>
          <button
            type="button"
            className={`section-chip${activeTab === 'canvas' ? ' active' : ''}`}
            aria-pressed={activeTab === 'canvas'}
            onClick={() => setTab('canvas')}
          >
            编排画布
          </button>
          <button
            type="button"
            className={`section-chip${activeTab === 'runs' ? ' active' : ''}`}
            aria-pressed={activeTab === 'runs'}
            onClick={() => setTab('runs')}
          >
            运行进度
          </button>
        </div>
      </header>

      <div className="collab-main">
        <div className="collab-tab-body">
          <LiveGraphTeamView activeTab={activeTab} />
        </div>
      </div>
    </div>
  );
};

const DemoCollabView: React.FC = () => {
  const { client, clientMode, activeWorkspace, addNotification } = useOperant();
  const { canPublishTemplate, getWorkflowDirectory } = useDemo();
  const { showSidebarOpenBtn, openSidebar, isMobile, setCollabStatus } =
    useOutletContext<RailOutletContext>();
  const [searchParams, setSearchParams] = useSearchParams();

  const viewParam = searchParams.get('view');
  const activeTab: CollabTab = viewParam === 'canvas' || viewParam === 'runs' ? viewParam : 'home';
  const draftParam = searchParams.get('draft');

  const [drafts, setDrafts] = useState<GraphDraft[]>([]);
  const [runs, setRuns] = useState<WorkflowRun[]>([]);
  /** 工作流目录（发布版本 + 另存临时工作流）：发布封锁的 draftId → 模板条目映射用 */
  const [directory, setDirectory] = useState<DemoWorkflowDirectoryItem[]>([]);

  // 本地编辑态（深拷贝自选中草稿；仅保存时回写）
  const [loadedDraftId, setLoadedDraftId] = useState<string | null>(null);
  const [nodes, setNodes] = useState<GraphNode[]>([]);
  const [edges, setEdges] = useState<GraphEdge[]>([]);
  const [draftName, setDraftName] = useState('');
  const [draftDescription, setDraftDescription] = useState<string | undefined>(undefined);

  const [diagnostics, setDiagnostics] = useState<GraphCompilerDiagnostic[]>([]);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [publishModalOpen, setPublishModalOpen] = useState(false);
  const [publishDescription, setPublishDescription] = useState('');

  const loadLists = useCallback(async () => {
    try {
      const [draftList, runList] = await Promise.all([
        client.listGraphDrafts(activeWorkspace),
        client.listWorkflowRuns(activeWorkspace),
      ]);
      setDrafts(draftList);
      setRuns(runList);
    } catch (err: unknown) {
      console.error('Failed to load collab data:', err);
    }
  }, [client, activeWorkspace]);

  useEffect(() => {
    void loadLists();
  }, [loadLists]);

  /** 工作流目录刷新（草稿保存/发布版本后同步目录名与条目） */
  const refreshDirectory = useCallback(() => {
    void getWorkflowDirectory().then((list) => setDirectory(list));
  }, [getWorkflowDirectory]);

  useEffect(() => {
    refreshDirectory();
  }, [refreshDirectory]);

  // 草稿初始化 / 切换：URL draft 参数或首个草稿；切换即从 client 重载（丢弃未保存编辑）
  useEffect(() => {
    const targetId = draftParam ?? drafts[0]?.id ?? null;
    if (!targetId || targetId === loadedDraftId) return;
    let cancelled = false;
    client
      .getGraphDraft(targetId)
      .then((draft) => {
        if (cancelled) return;
        const copy = JSON.parse(JSON.stringify(draft)) as GraphDraft;
        setLoadedDraftId(copy.id);
        setNodes(copy.nodes);
        setEdges(copy.edges);
        setDraftName(copy.name);
        setDraftDescription(copy.description);
        setDiagnostics([]);
        setSelectedNodeId(null);
        setSelectedEdgeId(null);
      })
      .catch(() => {
        /* 草稿不存在（如深链过期）：等待列表或参数变化 */
      });
    return () => {
      cancelled = true;
    };
  }, [draftParam, drafts, loadedDraftId, client]);

  // StatusBar 数据通道：画布 Tab 显示 草稿名 + N 节点 · M 连线；总览/运行 Tab 显示草稿与运行计数
  useEffect(() => {
    if (activeTab === 'canvas' && loadedDraftId) {
      setCollabStatus({ kind: 'canvas', draftName, nodeCount: nodes.length, edgeCount: edges.length });
      return;
    }
    setCollabStatus({ kind: 'summary', draftCount: drafts.length, runCount: runs.length });
  }, [
    activeTab,
    loadedDraftId,
    draftName,
    nodes.length,
    edges.length,
    drafts.length,
    runs.length,
    setCollabStatus,
  ]);

  useEffect(() => () => setCollabStatus(null), [setCollabStatus]);

  const setTab = (tab: CollabTab) => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.set('view', tab);
      return next;
    });
  };

  const handleSelectNode = useCallback((id: string | null) => {
    setSelectedNodeId(id);
    if (id) setSelectedEdgeId(null);
  }, []);

  const handleSelectEdge = useCallback((id: string | null) => {
    setSelectedEdgeId(id);
    if (id) setSelectedNodeId(null);
  }, []);

  // v6 发布封锁：当前编辑草稿 → getWorkflowDirectory 中 draftId 匹配的已发布目录条目
  // （模板级视图下每草稿仅保留最新版本一条，匹配结果唯一）；无匹配（从未发布过的新草稿）
  // 不封锁；有活跃实例时禁止发布新版本（活跃实例经 canPublishTemplate 按模板跨版本聚合）
  const publishedEntryOfDraft = loadedDraftId
    ? directory.find((item) => item.kind === 'published' && item.draftId === loadedDraftId)
    : undefined;
  const publishBlock = publishedEntryOfDraft
    ? canPublishTemplate(publishedEntryOfDraft.id)
    : { ok: true, activeCount: 0 };

  /** 发布入口封锁：有活跃实例时仅弹 warn 通知（数据层口径），不打开发布 Modal */
  const openPublishModal = () => {
    if (!publishBlock.ok) {
      addNotification(
        'warn',
        `该模板下有 ${publishBlock.activeCount} 个活跃实例，请先在总览中归档或删除后再发布`
      );
      return;
    }
    setPublishModalOpen(true);
  };

  const handleSave = async () => {
    if (!loadedDraftId) return;
    try {
      await client.saveGraphDraft({
        id: loadedDraftId,
        workspace: activeWorkspace,
        name: draftName,
        description: draftDescription,
        nodes,
        edges,
      });
      addNotification('success', '草稿已保存（演示）');
      void loadLists();
      refreshDirectory();
      window.dispatchEvent(new CustomEvent(GRAPH_DRAFTS_CHANGED_EVENT));
    } catch {
      addNotification('error', '保存失败');
    }
  };

  const handleCompile = async () => {
    if (!loadedDraftId) return;
    try {
      const res = await client.compileGraphDraft(loadedDraftId);
      setDiagnostics(res.diagnostics);
      if (res.is_valid) {
        addNotification('success', '校验通过：图 IR 有效，可以发布。');
      } else {
        addNotification('warn', `校验发现 ${res.diagnostics.length} 个诊断问题。`);
      }
    } catch {
      addNotification('error', '校验执行失败');
    }
  };

  const handlePublish = async () => {
    if (!loadedDraftId) return;
    // 确认前复检封锁（Modal 打开期间实例状态可能变化）：拦截口径与入口一致
    if (!publishBlock.ok) {
      addNotification(
        'warn',
        `该模板下有 ${publishBlock.activeCount} 个活跃实例，请先在总览中归档或删除后再发布`
      );
      setPublishModalOpen(false);
      return;
    }
    try {
      const rev = await client.publishGraphDraft(loadedDraftId, publishDescription);
      setPublishModalOpen(false);
      setPublishDescription('');
      addNotification('success', `已发布工作流定义 v${rev.version}。`);
      void loadLists();
      refreshDirectory();
      setTab('home');
    } catch (err: unknown) {
      addNotification('error', `发布失败：${err instanceof Error ? err.message : '未知错误'}`);
    }
  };

  const handleDeleteSelection = () => {
    if (selectedNodeId) {
      const result = removeNodeWithEdges(nodes, edges, selectedNodeId);
      setNodes(result.nodes);
      setEdges(result.edges);
      setSelectedNodeId(null);
    } else if (selectedEdgeId) {
      setEdges(edges.filter((e) => e.id !== selectedEdgeId));
      setSelectedEdgeId(null);
    }
  };

  const sidebarOpenBtn = showSidebarOpenBtn ? (
    <button
      className="btn btn-secondary btn-icon chat-header-sidebar-btn"
      onClick={openSidebar}
      aria-label="打开侧栏"
      title="打开侧栏"
    >
      {isMobile ? <Menu size={16} /> : <PanelLeftOpen size={16} />}
    </button>
  ) : undefined;

  return (
    <div className="section-view">
      <header className="section-header">
        <div
          style={{
            display: 'flex',
            alignItems: 'flex-start',
            justifyContent: 'space-between',
            gap: 12,
            flexWrap: 'wrap',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 }}>
            {sidebarOpenBtn}
            <div style={{ minWidth: 0 }}>
              <h1 className="section-title">协作</h1>
              <p className="section-sub">工作流总览、多智能体编排画布与运行进度</p>
            </div>
          </div>
        </div>

        <div className="section-chips" role="group" aria-label="协作视图切换">
          <button
            type="button"
            className={`section-chip${activeTab === 'home' ? ' active' : ''}`}
            aria-pressed={activeTab === 'home'}
            onClick={() => setTab('home')}
          >
            总览
          </button>
          <button
            type="button"
            className={`section-chip${activeTab === 'canvas' ? ' active' : ''}`}
            aria-pressed={activeTab === 'canvas'}
            onClick={() => setTab('canvas')}
          >
            编排画布
          </button>
          <button
            type="button"
            className={`section-chip${activeTab === 'runs' ? ' active' : ''}`}
            aria-pressed={activeTab === 'runs'}
            onClick={() => setTab('runs')}
          >
            运行进度{clientMode === 'mock' ? `（${runs.length}）` : ''}
          </button>
        </div>
      </header>

      <div className="collab-main">
        <div className="collab-tab-body">
          {activeTab === 'home' && <WorkflowOverview />}

          {activeTab === 'canvas' && (
            <>
              {/* 工具行：草稿名（可改名）| 删除 | 校验 | 发布版本 | 保存草稿 */}
              <div className="collab-toolbar">
                <input
                  type="text"
                  className="input collab-draft-name"
                  value={draftName}
                  onChange={(e) => setDraftName(e.target.value)}
                  placeholder="草稿名称…"
                  aria-label="草稿名称"
                />
                <button
                  type="button"
                  className="btn btn-ghost btn-icon btn-danger"
                  onClick={handleDeleteSelection}
                  disabled={drafts.length <= 1}
                  aria-label="删除草稿"
                  title="删除草稿"
                >
                  <Trash2 size={14} aria-hidden="true" />
                </button>
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  onClick={handleCompile}
                  aria-label="校验工作流"
                >
                  <FileCheck size={14} aria-hidden="true" />
                  校验
                </button>
                <button
                  type="button"
                  className={`btn btn-sm ${publishBlock.ok ? 'btn-primary' : 'btn-secondary'}`}
                  onClick={openPublishModal}
                  disabled={!loadedDraftId}
                >
                  <FileCheck size={13} />
                  <span>发布版本</span>
                </button>
                {!publishBlock.ok && (
                  <span
                    style={{ fontSize: 11, color: 'var(--status-warn-text)', whiteSpace: 'nowrap' }}
                    title="该模板下存在活跃实例，归档或删除后才能发布新版本"
                  >
                    有 {publishBlock.activeCount} 个活跃实例，发布已锁定
                  </span>
                )}
                <button
                  type="button"
                  onClick={handleSave}
                  className="btn btn-secondary btn-sm"
                  disabled={!loadedDraftId}
                >
                  <Save size={13} />
                  <span>保存草稿</span>
                </button>
              </div>

              <GraphCanvas
                nodes={nodes}
                edges={edges}
                diagnostics={diagnostics}
                selectedNodeId={selectedNodeId}
                selectedEdgeId={selectedEdgeId}
                onSelectNode={handleSelectNode}
                onSelectEdge={handleSelectEdge}
                onChangeNodes={setNodes}
                onChangeEdges={setEdges}
              />
                </>
              )}

              {activeTab === 'runs' && <MonitorPanel runs={runs} />}
            </div>
          </div>

      {/* 发布版本 Modal（复用原 WorkflowView 逻辑与文案） */}
      {clientMode === 'mock' && <Modal
        isOpen={publishModalOpen}
        onClose={() => setPublishModalOpen(false)}
        title="发布工作流定义"
        footer={
          <>
            <button onClick={() => setPublishModalOpen(false)} className="btn btn-ghost">
              取消
            </button>
            <button onClick={handlePublish} className="btn btn-primary">
              <FileCheck size={14} />
              <span>确认并发布版本</span>
            </button>
          </>
        }
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <p style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
            发布将创建此图定义的不可变快照，活跃的触发器与执行将绑定到该发布版本 ID。
          </p>
          <div>
            <label
              style={{
                fontSize: '12px',
                fontWeight: 600,
                color: 'var(--text-primary)',
                display: 'block',
                marginBottom: 4,
              }}
            >
              发布说明 / 描述
            </label>
            <textarea
              className="textarea"
              rows={3}
              placeholder="例如：包含更新的评审循环约束的生产发布"
              value={publishDescription}
              onChange={(e) => setPublishDescription(e.target.value)}
            />
          </div>
        </div>
      </Modal>}
    </div>
  );
};
