/**
 * 工作流只读画布（/collab/:wfId/canvas，v5 §2.2）：协作总览卡片"打开画布"目标。
 * 头部：← 返回总览 / 名称 / 徽章 /（published 且有来源草稿时）"在工作台打开"。
 * published 用发布版本真实 nodes/edges；persisted 无真实 GraphDraft 时用演示节点数据；
 * readOnly 下仅选中态变化，说明条"只读预览，编辑请在工作台打开草稿"。
 * StatusBar 经 setCollabStatus 上抛名称 + 节点/连线数（与工作台画布 Tab 同一形式）。
 * id 不存在 → 空态 + 返回总览。
 */

import React, { useEffect, useState } from 'react';
import { useNavigate, useOutletContext, useParams } from 'react-router-dom';
import { ArrowLeft, ExternalLink, GitBranch } from 'lucide-react';
import type { GraphEdge, GraphNode, GraphNodeType } from '@operant/sdk';
import { EmptyState } from '../../components/EmptyState';
import { useOperant } from '../../context/ClientContext';
import { useDemo } from '../../demo/DemoContext';
import type { RailOutletContext } from '../../app/RailLayout';
import type { DemoWorkflowDirectoryItem } from '../../demo/types';
import { GraphCanvas } from './canvas/GraphCanvas';

/** persisted 类条目无真实 GraphDraft：按 nodeCount/edgeCount 生成确定性演示节点链 */
function buildPersistedDemoGraph(item: DemoWorkflowDirectoryItem): {
  nodes: GraphNode[];
  edges: GraphEdge[];
} {
  const types: GraphNodeType[] = ['agent', 'tool', 'agent', 'approval', 'agent', 'join', 'script'];
  const labels = ['规划', '上下文收集', '编码执行', '审批闸门', '评审', '汇合', '构建产物'];
  const nodeCount = Math.max(1, item.nodeCount);
  const nodes: GraphNode[] = [];
  for (let i = 0; i < nodeCount; i++) {
    nodes.push({
      id: `${item.id}_demo_node_${i}`,
      label: labels[i % labels.length],
      type: types[i % types.length],
      position: { x: 60 + (i % 4) * 320, y: 120 + Math.floor(i / 4) * 220 },
      inputs: [{ id: 'in_1', name: '输入', type: 'any', direction: 'input' }],
      outputs: [{ id: 'out_1', name: '输出', type: 'any', direction: 'output' }],
    });
  }
  const edges: GraphEdge[] = [];
  for (let i = 0; i < Math.min(item.edgeCount, nodeCount - 1); i++) {
    edges.push({
      id: `${item.id}_demo_edge_${i}`,
      source_node_id: nodes[i].id,
      source_port_id: 'out_1',
      target_node_id: nodes[i + 1].id,
      target_port_id: 'in_1',
      edge_type: 'data',
    });
  }
  return { nodes, edges };
}

export const CollabWorkflowCanvas: React.FC = () => {
  const { wfId = '' } = useParams<{ wfId: string }>();
  const navigate = useNavigate();
  const { client } = useOperant();
  const { getWorkflowDirectory } = useDemo();
  const { setCollabStatus } = useOutletContext<RailOutletContext>();

  /** undefined=加载中；null=不存在 */
  const [item, setItem] = useState<DemoWorkflowDirectoryItem | null | undefined>(undefined);
  // 画布本地态：published=发布版本真实图；persisted=演示节点数据；readOnly 下仅选中态变化
  const [canvasNodes, setCanvasNodes] = useState<GraphNode[]>([]);
  const [canvasEdges, setCanvasEdges] = useState<GraphEdge[]>([]);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getWorkflowDirectory().then((list) => {
      if (!cancelled) setItem(list.find((i) => i.id === wfId) ?? null);
    });
    return () => {
      cancelled = true;
    };
  }, [getWorkflowDirectory, wfId]);

  // StatusBar 数据通道：与工作台画布 Tab 同一形式（名称 + 节点/连线数），卸载时清除
  useEffect(() => {
    if (item) {
      setCollabStatus({
        kind: 'canvas',
        draftName: item.name,
        nodeCount: item.nodeCount,
        edgeCount: item.edgeCount,
      });
    } else {
      setCollabStatus(null);
    }
    return () => setCollabStatus(null);
  }, [item, setCollabStatus]);

  // 画布数据：published 取发布版本 nodes/edges；persisted 用演示节点数据
  useEffect(() => {
    if (!item) return;
    setSelectedNodeId(null);
    setSelectedEdgeId(null);
    if (item.kind === 'persisted') {
      const demo = buildPersistedDemoGraph(item);
      setCanvasNodes(demo.nodes);
      setCanvasEdges(demo.edges);
      return;
    }
    let cancelled = false;
    client
      .listGraphRevisions()
      .then((revisions) => {
        if (cancelled) return;
        const rev = revisions.find((r) => r.id === item.id);
        setCanvasNodes(rev?.nodes ?? []);
        setCanvasEdges(rev?.edges ?? []);
      })
      .catch(() => {
        if (!cancelled) {
          setCanvasNodes([]);
          setCanvasEdges([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [item, client]);

  // 加载中
  if (item === undefined) {
    return (
      <div className="section-view">
        <div className="section-scroll">
          <div className="section-inner">
            <p className="section-footnote">正在加载工作流…</p>
          </div>
        </div>
      </div>
    );
  }

  // id 不存在：空态 + 返回总览（页面级空态，标题即本页唯一 h1）
  if (item === null) {
    return (
      <div className="section-view chat-view-empty">
        <EmptyState
          icon={GitBranch}
          titleAs="h1"
          title="未找到该工作流"
          description="它可能尚未发布或已被移除，请返回协作总览选择。"
          action={
            <button className="btn btn-secondary" onClick={() => navigate('/collab')}>
              <ArrowLeft size={14} aria-hidden="true" />
              返回协作总览
            </button>
          }
        />
      </div>
    );
  }

  return (
    <div className="section-view">
      <header className="section-header">
        <div className="wf-detail-head">
          <button
            className="btn btn-secondary btn-icon"
            onClick={() => navigate('/collab')}
            aria-label="返回协作总览"
            title="返回协作总览"
          >
            <ArrowLeft size={16} aria-hidden="true" />
          </button>
          <div className="wf-detail-title">
            <h1 className="section-title">{item.name}</h1>
            <p className="section-sub">
              {item.nodeCount} 节点 · {item.edgeCount} 连线
            </p>
          </div>
          <span className="wf-detail-badges">
            {item.kind === 'published' ? (
              <span className="wf-badge published">已发布 v{item.version}</span>
            ) : (
              <span className="wf-badge persisted">另存</span>
            )}
          </span>
          <div style={{ flex: 1 }} />
          {item.kind === 'published' && item.draftId && (
            <button
              className="btn btn-secondary"
              onClick={() => navigate(`/collab?view=canvas&draft=${item.draftId}`)}
            >
              <ExternalLink size={14} aria-hidden="true" />
              在工作台打开
            </button>
          )}
        </div>
      </header>

      <div className="wf-tab-body">
        <div className="wf-readonly-note" role="note">
          只读预览，编辑请在工作台打开草稿
        </div>
        <GraphCanvas
          nodes={canvasNodes}
          edges={canvasEdges}
          diagnostics={[]}
          selectedNodeId={selectedNodeId}
          selectedEdgeId={selectedEdgeId}
          onSelectNode={setSelectedNodeId}
          onSelectEdge={setSelectedEdgeId}
          onChangeNodes={setCanvasNodes}
          onChangeEdges={setCanvasEdges}
          readOnly
        />
      </div>
    </div>
  );
};
