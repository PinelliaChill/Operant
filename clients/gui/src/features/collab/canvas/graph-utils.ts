/**
 * 编排画布共享常量、类型与纯函数。
 * 端口坐标的派生依赖固定几何：卡片宽 220、头部高 40、端口区顶内边距 6、端口行高 16，
 * 与 layout.css 中 .graph-node* 样式一一对应，改动需同步。
 */

import type { LucideIcon } from 'lucide-react';
import {
  Bot,
  Wrench,
  FileCode,
  GitFork,
  Merge,
  ShieldCheck,
  Hand,
  GitBranch,
} from 'lucide-react';
import type { GraphEdge, GraphNode, GraphNodeType, GraphPort } from '@operant/sdk';

export const CANVAS_WIDTH = 1600;
export const CANVAS_HEIGHT = 800;
export const NODE_WIDTH = 220;
export const NODE_HEADER_HEIGHT = 40;
export const PORTS_PADDING_TOP = 6;
export const PORT_ROW_HEIGHT = 16;

/** 节点类型 → 中文标签（含 sdk 兜底类型 subworkflow） */
export const NODE_TYPE_LABELS: Record<string, string> = {
  agent: '代理',
  tool: '工具',
  script: '脚本',
  condition: '条件',
  join: '汇合',
  approval: '审批',
  human_input: '人工输入',
  subworkflow: '子工作流',
};

/** 节点类型 → 类型图标（面板与节点卡片共用） */
export const NODE_TYPE_ICONS: Record<string, LucideIcon> = {
  agent: Bot,
  tool: Wrench,
  script: FileCode,
  condition: GitFork,
  join: Merge,
  approval: ShieldCheck,
  human_input: Hand,
  subworkflow: GitBranch,
};

/** 节点面板可选的 7 种类型（不含 subworkflow） */
export const PALETTE_TYPES: GraphNodeType[] = [
  'agent',
  'tool',
  'script',
  'condition',
  'join',
  'approval',
  'human_input',
];

/** 端口引用（连线端点） */
export interface PortRef {
  nodeId: string;
  portId: string;
}

/** 端口圆点中心在画布表面坐标系中的位置（圆点半径 5px 压在卡片左右边缘上） */
export function getPortCenter(
  node: GraphNode,
  portId: string,
  direction: 'input' | 'output'
): { x: number; y: number } | null {
  const list = direction === 'input' ? node.inputs : node.outputs;
  const index = list.findIndex((p) => p.id === portId);
  if (index < 0) return null;
  return {
    x: node.position.x + (direction === 'input' ? 0 : NODE_WIDTH),
    y:
      node.position.y +
      NODE_HEADER_HEIGHT +
      PORTS_PADDING_TOP +
      index * PORT_ROW_HEIGHT +
      PORT_ROW_HEIGHT / 2,
  };
}

/** 三次贝塞尔边路径（水平出、水平进） */
export function edgePath(x1: number, y1: number, x2: number, y2: number): string {
  const dx = Math.max(40, Math.abs(x2 - x1) * 0.5);
  return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
}

/** 新节点骨架（设计基线 §2.3；condition 双 output 真/假，join 双 input） */
export function createNodeSkeleton(
  type: GraphNodeType,
  position: { x: number; y: number }
): GraphNode {
  const inputs: GraphPort[] = [{ id: 'in_1', name: '输入', type: 'any', direction: 'input' }];
  const outputs: GraphPort[] = [{ id: 'out_1', name: '输出', type: 'any', direction: 'output' }];
  if (type === 'condition') {
    outputs.splice(
      0,
      outputs.length,
      { id: 'out_true', name: '真', type: 'any', direction: 'output' },
      { id: 'out_false', name: '假', type: 'any', direction: 'output' }
    );
  }
  if (type === 'join') {
    inputs.push({ id: 'in_2', name: '输入', type: 'any', direction: 'input' });
  }
  return {
    id: `node_${Date.now()}`,
    label: NODE_TYPE_LABELS[type] ?? type,
    type,
    position,
    inputs,
    outputs,
  };
}

/** 把节点位置限制在画布表面内 */
export function clampPosition(x: number, y: number): { x: number; y: number } {
  return {
    x: Math.min(Math.max(x, 0), CANVAS_WIDTH - NODE_WIDTH),
    y: Math.min(Math.max(y, 0), CANVAS_HEIGHT - 60),
  };
}

/** 四元组完全相同的边视为重复 */
export function hasDuplicateEdge(edges: GraphEdge[], source: PortRef, target: PortRef): boolean {
  return edges.some(
    (e) =>
      e.source_node_id === source.nodeId &&
      e.source_port_id === source.portId &&
      e.target_node_id === target.nodeId &&
      e.target_port_id === target.portId
  );
}

/** 删除节点并联动删除其所有边 */
export function removeNodeWithEdges(
  nodes: GraphNode[],
  edges: GraphEdge[],
  nodeId: string
): { nodes: GraphNode[]; edges: GraphEdge[] } {
  return {
    nodes: nodes.filter((n) => n.id !== nodeId),
    edges: edges.filter((e) => e.source_node_id !== nodeId && e.target_node_id !== nodeId),
  };
}
