/**
 * 编排画布（设计基线 §2）：节点面板 + 1600×800 点阵滚动表面 + 节点配置面板。
 *
 * 交互状态机（全部 Pointer Events，合成事件可测）：
 * - idle：节点 pointerdown → 选中 + drag；输出端口 pointerdown → connect(拖拽)；
 *   边 click → 选中边；空白 pointerdown → 清空选中；Delete/Backspace → 删除选中；
 *   节点方向键 → 微移 4px；面板点击 → 级联添加；面板 pointerdown → palette。
 * - drag：pointermove 实时更新节点 position（clamp 表面内）；pointerup 落定回 idle。
 * - connect（armed=false 拖拽连线）：pointermove 更新预览虚线；pointerup 落在其他节点
 *   输入端口 → 校验（禁自环/禁重复）创建边；位移 <6px 视为点击 → 进入 armed 待命；
 *   落空 ≥6px 或 Esc → 取消。
 * - connect（armed=true 点击连线后备）：源端口 accent 高亮 + aria-label"选择目标输入端口"；
 *   点击（含键盘 Enter）目标输入端口 → 创建边；键盘 Enter 源端口可取消/换源；
 *   空白 pointerdown / Esc → 取消；预览虚线持续跟随光标。
 * - palette（面板拖入）：位移 >6px 在指针落点生成节点并转入 drag；未超阈值 pointerup
 *   回 idle（随后 click 由面板按钮走级联添加）。
 * - 面板折叠（v6 §4）：左侧节点面板与右侧节点配置面板均可折叠（展开状态经
 *   localStorage operant.panel.nodePalette / operant.panel.nodeConfig 持久化，默认展开）；
 *   收起后画布对应侧缘留细竖把手（点击/Enter 展开），画布表面宽度自适应；
 *   配置面板仍仅选中节点时出现，折叠状态独立于选中逻辑。
 * - readOnly（v4 §3.3）：隐藏节点面板与配置面板（含折叠把手），节点仅可聚焦/选中
 *   （不可拖、不可连、不可删），供工作流界面画布 Tab 只读预览复用。
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import type {
  GraphCompilerDiagnostic,
  GraphEdge,
  GraphNode,
  GraphNodeType,
} from '@operant/sdk';
import { useOperant } from '../../../context/ClientContext';
import { PanelCollapseHandle, usePanelExpanded } from '../../../components/CollapsiblePanel';
import { GraphNodeCard } from './GraphNodeCard';
import { GraphEdgeLayer } from './GraphEdgeLayer';
import { NodePalette } from './NodePalette';
import { NodeConfigPanel } from './NodeConfigPanel';
import {
  CANVAS_HEIGHT,
  CANVAS_WIDTH,
  NODE_WIDTH,
  clampPosition,
  createNodeSkeleton,
  getPortCenter,
  hasDuplicateEdge,
  removeNodeWithEdges,
  type PortRef,
} from './graph-utils';

type Interaction =
  | { kind: 'idle' }
  | {
      kind: 'drag';
      nodeId: string;
      startClientX: number;
      startClientY: number;
      origX: number;
      origY: number;
    }
  | {
      kind: 'connect';
      source: PortRef;
      startClientX: number;
      startClientY: number;
      cursor: { x: number; y: number };
      /** true = 点击连线待命态；false = 拖拽连线中 */
      armed: boolean;
    }
  | { kind: 'palette'; type: GraphNodeType; startClientX: number; startClientY: number };

const IDLE: Interaction = { kind: 'idle' };

interface GraphCanvasProps {
  nodes: GraphNode[];
  edges: GraphEdge[];
  diagnostics: GraphCompilerDiagnostic[];
  selectedNodeId: string | null;
  selectedEdgeId: string | null;
  onSelectNode: (id: string | null) => void;
  onSelectEdge: (id: string | null) => void;
  onChangeNodes: (nodes: GraphNode[]) => void;
  onChangeEdges: (edges: GraphEdge[]) => void;
  /** 只读预览（v4 §3.3 工作流界面画布 Tab）：隐藏节点面板与配置面板，节点不可拖、端口不可连、禁用删除 */
  readOnly?: boolean;
}

export const GraphCanvas: React.FC<GraphCanvasProps> = (props) => {
  const {
    nodes,
    edges,
    diagnostics,
    selectedNodeId,
    selectedEdgeId,
    onSelectNode,
    onSelectEdge,
    onChangeNodes,
    onChangeEdges,
    readOnly = false,
  } = props;
  const { addNotification } = useOperant();

  // 面板折叠状态（v6 §4）：localStorage 持久化、默认展开，与选中逻辑互相独立
  const [paletteExpanded, setPaletteExpanded] = usePanelExpanded('operant.panel.nodePalette');
  const [configExpanded, setConfigExpanded] = usePanelExpanded('operant.panel.nodeConfig');

  const surfaceRef = useRef<HTMLDivElement>(null);
  const [interaction, setInteractionState] = useState<Interaction>(IDLE);

  // ---- refs：window 级监听器闭包内读取最新状态 ----
  const nodesRef = useRef(nodes);
  nodesRef.current = nodes;
  const edgesRef = useRef(edges);
  edgesRef.current = edges;
  const interactionRef = useRef(interaction);
  interactionRef.current = interaction;
  const selectionRef = useRef({ selectedNodeId, selectedEdgeId });
  selectionRef.current = { selectedNodeId, selectedEdgeId };
  const readOnlyRef = useRef(readOnly);
  readOnlyRef.current = readOnly;
  const callbacksRef = useRef({ onChangeNodes, onChangeEdges, onSelectNode, onSelectEdge, addNotification });
  callbacksRef.current = { onChangeNodes, onChangeEdges, onSelectNode, onSelectEdge, addNotification };

  const setInteraction = useCallback((next: Interaction) => {
    interactionRef.current = next;
    setInteractionState(next);
  }, []);

  /** client 坐标 → 画布表面坐标（表面滚动时 getBoundingClientRect 实时反映） */
  const clientToSurface = (clientX: number, clientY: number): { x: number; y: number } => {
    const rect = surfaceRef.current?.getBoundingClientRect();
    if (!rect) return { x: 0, y: 0 };
    return { x: clientX - rect.left, y: clientY - rect.top };
  };

  /** 命中检测：指针位置是否落在某输入端口圆点上 */
  const inputPortFromPoint = (clientX: number, clientY: number): PortRef | null => {
    const el = document.elementFromPoint(clientX, clientY);
    const portEl = el?.closest('[data-direction="input"][data-node-id][data-port-id]');
    if (!(portEl instanceof HTMLElement)) return null;
    return { nodeId: portEl.dataset.nodeId!, portId: portEl.dataset.portId! };
  };

  /** 创建边：禁自环、禁完全重复；成功后选中新边 */
  const tryConnect = (source: PortRef, target: PortRef) => {
    if (source.nodeId === target.nodeId) {
      callbacksRef.current.addNotification('warn', '不能连接到同一节点（已阻止自环）。');
      return;
    }
    if (hasDuplicateEdge(edgesRef.current, source, target)) {
      callbacksRef.current.addNotification('warn', '这两个端口之间已存在连线（已阻止重复连线）。');
      return;
    }
    const edge: GraphEdge = {
      id: `edge_${Date.now()}`,
      source_node_id: source.nodeId,
      source_port_id: source.portId,
      target_node_id: target.nodeId,
      target_port_id: target.portId,
      edge_type: 'data',
    };
    callbacksRef.current.onChangeEdges([...edgesRef.current, edge]);
    callbacksRef.current.onSelectEdge(edge.id);
    callbacksRef.current.onSelectNode(null);
  };

  /** 删除当前选中（节点联动删边）；Delete/Backspace 与工具行删除按钮共用 */
  const deleteSelection = useCallback(() => {
    const { selectedNodeId: nodeId, selectedEdgeId: edgeId } = selectionRef.current;
    if (nodeId) {
      const result = removeNodeWithEdges(nodesRef.current, edgesRef.current, nodeId);
      callbacksRef.current.onChangeNodes(result.nodes);
      callbacksRef.current.onChangeEdges(result.edges);
      callbacksRef.current.onSelectNode(null);
    } else if (edgeId) {
      callbacksRef.current.onChangeEdges(edgesRef.current.filter((e) => e.id !== edgeId));
      callbacksRef.current.onSelectEdge(null);
    }
  }, []);

  // ---- window 级 pointer 监听：仅交互激活时挂载 ----
  const interactionActive = interaction.kind !== 'idle';
  useEffect(() => {
    if (!interactionActive) return;

    const onMove = (e: PointerEvent) => {
      const cur = interactionRef.current;
      if (cur.kind === 'drag') {
        const dx = e.clientX - cur.startClientX;
        const dy = e.clientY - cur.startClientY;
        callbacksRef.current.onChangeNodes(
          nodesRef.current.map((n) =>
            n.id === cur.nodeId ? { ...n, position: clampPosition(cur.origX + dx, cur.origY + dy) } : n
          )
        );
      } else if (cur.kind === 'connect') {
        setInteraction({ ...cur, cursor: clientToSurface(e.clientX, e.clientY) });
      } else if (cur.kind === 'palette') {
        const moved = Math.hypot(e.clientX - cur.startClientX, e.clientY - cur.startClientY);
        if (moved > 6) {
          // 在指针落点生成节点并转入节点拖动
          const pos = clientToSurface(e.clientX, e.clientY);
          const node = createNodeSkeleton(
            cur.type,
            clampPosition(pos.x - NODE_WIDTH / 2, pos.y - 20)
          );
          callbacksRef.current.onChangeNodes([...nodesRef.current, node]);
          callbacksRef.current.onSelectNode(node.id);
          setInteraction({
            kind: 'drag',
            nodeId: node.id,
            startClientX: e.clientX,
            startClientY: e.clientY,
            origX: node.position.x,
            origY: node.position.y,
          });
        }
      }
    };

    const onUp = (e: PointerEvent) => {
      const cur = interactionRef.current;
      if (cur.kind === 'drag' || cur.kind === 'palette') {
        setInteraction(IDLE);
        return;
      }
      if (cur.kind === 'connect') {
        if (cur.armed) return; // 待命态：创建走输入端口 click，取消走空白 pointerdown / Esc
        const target = inputPortFromPoint(e.clientX, e.clientY);
        if (target) {
          tryConnect(cur.source, target);
          setInteraction(IDLE);
          return;
        }
        const moved = Math.hypot(e.clientX - cur.startClientX, e.clientY - cur.startClientY);
        // 原地松手视为点击 → 进入点连待命态；拖动落空 → 取消
        setInteraction(moved < 6 ? { ...cur, armed: true } : IDLE);
      }
    };

    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    return () => {
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [interactionActive, setInteraction]);

  // ---- 全局键盘：Esc 取消交互；Delete/Backspace 删除选中（输入控件内不拦截） ----
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)
      ) {
        return;
      }
      if (e.key === 'Escape') {
        if (interactionRef.current.kind !== 'idle') setInteraction(IDLE);
        return;
      }
      if (readOnlyRef.current) return; // 只读预览禁用删除
      if (e.key === 'Delete' || e.key === 'Backspace') {
        const { selectedNodeId: nodeId, selectedEdgeId: edgeId } = selectionRef.current;
        if (nodeId || edgeId) {
          e.preventDefault();
          deleteSelection();
        }
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [deleteSelection, setInteraction]);

  // ---- 事件处理器 ----

  const handleSurfacePointerDown = () => {
    // 节点 / 端口 / 边均已 stopPropagation，到达这里即空白区域
    onSelectNode(null);
    onSelectEdge(null);
    if (interactionRef.current.kind === 'connect') setInteraction(IDLE);
  };

  const handleNodePointerDown = (node: GraphNode, e: React.PointerEvent) => {
    e.stopPropagation();
    onSelectNode(node.id);
    onSelectEdge(null);
    if (readOnly) return; // 只读预览：可选中查看，不可拖动
    if (e.button !== 0) return;
    setInteraction({
      kind: 'drag',
      nodeId: node.id,
      startClientX: e.clientX,
      startClientY: e.clientY,
      origX: node.position.x,
      origY: node.position.y,
    });
  };

  const handleNodeKeyDown = (node: GraphNode, e: React.KeyboardEvent) => {
    if (readOnly) return; // 只读预览：方向键微移禁用
    const STEP = 4;
    let dx = 0;
    let dy = 0;
    if (e.key === 'ArrowLeft') dx = -STEP;
    else if (e.key === 'ArrowRight') dx = STEP;
    else if (e.key === 'ArrowUp') dy = -STEP;
    else if (e.key === 'ArrowDown') dy = STEP;
    else return;
    e.preventDefault();
    e.stopPropagation();
    onChangeNodes(
      nodesRef.current.map((n) =>
        n.id === node.id
          ? { ...n, position: clampPosition(n.position.x + dx, n.position.y + dy) }
          : n
      )
    );
  };

  const handleOutputPortPointerDown = (node: GraphNode, portId: string, e: React.PointerEvent) => {
    e.stopPropagation();
    if (readOnly) return; // 只读预览：端口不可连
    if (e.button !== 0) return;
    const center = getPortCenter(node, portId, 'output');
    setInteraction({
      kind: 'connect',
      source: { nodeId: node.id, portId },
      startClientX: e.clientX,
      startClientY: e.clientY,
      cursor: center ?? { x: 0, y: 0 },
      armed: false,
    });
  };

  /** 键盘点连后备：Enter/Space 激活输出端口（click detail === 0）进入/取消待命 */
  const handleOutputPortClick = (node: GraphNode, portId: string, e: React.MouseEvent) => {
    if (readOnly) return; // 只读预览：端口不可连
    if (e.detail !== 0) return; // 指针点击已由 pointerdown/pointerup 流程处理
    const cur = interactionRef.current;
    const isSameArmed =
      cur.kind === 'connect' &&
      cur.armed &&
      cur.source.nodeId === node.id &&
      cur.source.portId === portId;
    if (isSameArmed) {
      setInteraction(IDLE);
      return;
    }
    const center = getPortCenter(node, portId, 'output');
    setInteraction({
      kind: 'connect',
      source: { nodeId: node.id, portId },
      startClientX: 0,
      startClientY: 0,
      cursor: center ?? { x: 0, y: 0 },
      armed: true,
    });
  };

  const handleInputPortClick = (node: GraphNode, portId: string) => {
    if (readOnly) return; // 只读预览：端口不可连
    const cur = interactionRef.current;
    if (cur.kind === 'connect' && cur.armed) {
      tryConnect(cur.source, { nodeId: node.id, portId });
      setInteraction(IDLE);
    }
  };

  const handleAddNode = (type: GraphNodeType) => {
    const count = nodesRef.current.length;
    const node = createNodeSkeleton(type, clampPosition(40 + count * 24, 40 + count * 24));
    onChangeNodes([...nodesRef.current, node]);
    onSelectNode(node.id);
    onSelectEdge(null);
  };

  const handlePaletteDragStart = (type: GraphNodeType, e: React.PointerEvent) => {
    if (e.button !== 0) return;
    setInteraction({ kind: 'palette', type, startClientX: e.clientX, startClientY: e.clientY });
  };

  const handleSelectEdge = (edgeId: string) => {
    onSelectEdge(edgeId);
    onSelectNode(null);
    if (interactionRef.current.kind === 'connect') setInteraction(IDLE);
  };

  const selectedNode = nodes.find((n) => n.id === selectedNodeId) ?? null;
  const connecting = interaction.kind === 'connect';
  const armedSource = connecting && interaction.armed ? interaction.source : null;
  const preview = connecting
    ? (() => {
        const sourceNode = nodes.find((n) => n.id === interaction.source.nodeId);
        const center = sourceNode
          ? getPortCenter(sourceNode, interaction.source.portId, 'output')
          : null;
        if (!center) return null;
        return { x1: center.x, y1: center.y, x2: interaction.cursor.x, y2: interaction.cursor.y };
      })()
    : null;

  return (
    <div className={`collab-canvas-layout${readOnly ? ' collab-canvas-layout-readonly' : ''}`}>
      {!readOnly &&
        (paletteExpanded ? (
          <NodePalette
            onAddNode={handleAddNode}
            onDragStart={handlePaletteDragStart}
            onCollapse={() => setPaletteExpanded(false)}
          />
        ) : (
          <PanelCollapseHandle
            side="left"
            label="展开节点面板"
            onExpand={() => setPaletteExpanded(true)}
          />
        ))}

      <div className="collab-canvas-center">
        <div className="collab-canvas-scroll">
          <div
            ref={surfaceRef}
            className={`collab-canvas-surface${connecting ? ' connecting' : ''}`}
            style={{ width: CANVAS_WIDTH, height: CANVAS_HEIGHT }}
            role="application"
            aria-label="工作流编排画布"
            onPointerDown={handleSurfacePointerDown}
          >
            <GraphEdgeLayer
              nodes={nodes}
              edges={edges}
              selectedEdgeId={selectedEdgeId}
              onSelectEdge={handleSelectEdge}
              preview={preview}
            />
            {nodes.map((node) => (
              <GraphNodeCard
                key={node.id}
                node={node}
                selected={node.id === selectedNodeId}
                dragging={interaction.kind === 'drag' && interaction.nodeId === node.id}
                armedOutputPortId={armedSource?.nodeId === node.id ? armedSource.portId : null}
                onPointerDown={(e) => handleNodePointerDown(node, e)}
                onKeyDown={(e) => handleNodeKeyDown(node, e)}
                onFocusSelect={() => {
                  onSelectNode(node.id);
                  onSelectEdge(null);
                }}
                onOutputPortPointerDown={(portId, e) => handleOutputPortPointerDown(node, portId, e)}
                onOutputPortClick={(portId, e) => handleOutputPortClick(node, portId, e)}
                onInputPortClick={(portId) => handleInputPortClick(node, portId)}
              />
            ))}
          </div>
        </div>

        {/* 编译器诊断条（复用原 WorkflowView 诊断列表） */}
        {diagnostics.length > 0 && (
          <div className="collab-diagnostics">
            <div className="collab-diagnostics-title">编译器诊断（{diagnostics.length}）</div>
            {diagnostics.map((d, i) => (
              <div
                key={i}
                style={{
                  fontSize: '11px',
                  color:
                    d.level === 'error' ? 'var(--status-error-text)' : 'var(--status-warn-text)',
                  marginBottom: 2,
                }}
              >
                [{d.rule_code}] {d.message} {d.node_id ? `（节点：${d.node_id}）` : ''}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 配置面板仍仅选中节点时出现；折叠时右缘留细把手，折叠状态独立于选中逻辑（v6 §4） */}
      {!readOnly &&
        selectedNode &&
        (configExpanded ? (
          <NodeConfigPanel
            node={selectedNode}
            onChange={(patch) =>
              onChangeNodes(
                nodesRef.current.map((n) => (n.id === selectedNode.id ? { ...n, ...patch } : n))
              )
            }
            onCollapse={() => setConfigExpanded(false)}
          />
        ) : (
          <PanelCollapseHandle
            side="right"
            label="展开配置面板"
            onExpand={() => setConfigExpanded(true)}
          />
        ))}
    </div>
  );
};
