/**
 * 画布 SVG 边层：位于节点卡片下方。
 * 每条边 = 透明加宽命中路径（可点击选中）+ 可见三次贝塞尔路径（2px 灰，选中 accent，带箭头 marker）。
 * 连线中另渲染一条 accent 虚线预览（源端口 → 光标）。
 */

import React from 'react';
import type { GraphEdge, GraphNode } from '@operant/sdk';
import { edgePath, getPortCenter } from './graph-utils';

interface EdgePreview {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

interface GraphEdgeLayerProps {
  nodes: GraphNode[];
  edges: GraphEdge[];
  selectedEdgeId: string | null;
  onSelectEdge: (id: string) => void;
  preview: EdgePreview | null;
}

export const GraphEdgeLayer: React.FC<GraphEdgeLayerProps> = ({
  nodes,
  edges,
  selectedEdgeId,
  onSelectEdge,
  preview,
}) => {
  const nodeMap = new Map(nodes.map((n) => [n.id, n]));

  const renderEdge = (edge: GraphEdge) => {
    const sourceNode = nodeMap.get(edge.source_node_id);
    const targetNode = nodeMap.get(edge.target_node_id);
    if (!sourceNode || !targetNode) return null;
    const p1 = getPortCenter(sourceNode, edge.source_port_id, 'output');
    const p2 = getPortCenter(targetNode, edge.target_port_id, 'input');
    if (!p1 || !p2) return null;

    const selected = edge.id === selectedEdgeId;
    const d = edgePath(p1.x, p1.y, p2.x, p2.y);

    return (
      <g key={edge.id}>
        {/* 命中路径：透明加宽，承接点击选中 */}
        <path
          d={d}
          fill="none"
          stroke="transparent"
          strokeWidth={12}
          style={{ pointerEvents: 'stroke', cursor: 'pointer' }}
          onPointerDown={(e) => e.stopPropagation()}
          onClick={() => onSelectEdge(edge.id)}
        >
          {edge.condition_label ? <title>{edge.condition_label}</title> : null}
        </path>
        <path
          d={d}
          fill="none"
          stroke={selected ? 'var(--accent-action)' : 'var(--border-strong)'}
          strokeWidth={selected ? 2.5 : 2}
          markerEnd={selected ? 'url(#graph-arrow-active)' : 'url(#graph-arrow)'}
          className={selected ? undefined : 'graph-edge-line'}
          style={{ pointerEvents: 'none' }}
        />
      </g>
    );
  };

  return (
    <svg
      className="graph-edge-layer"
      width="100%"
      height="100%"
      style={{ position: 'absolute', inset: 0, pointerEvents: 'none', zIndex: 1 }}
      aria-hidden="true"
    >
      <defs>
        <marker
          id="graph-arrow"
          viewBox="0 0 10 10"
          refX="9"
          refY="5"
          markerWidth="7"
          markerHeight="7"
          orient="auto-start-reverse"
        >
          <path d="M 0 1 L 9 5 L 0 9 z" fill="var(--border-strong)" className="graph-arrow-default" />
        </marker>
        <marker
          id="graph-arrow-active"
          viewBox="0 0 10 10"
          refX="9"
          refY="5"
          markerWidth="7"
          markerHeight="7"
          orient="auto-start-reverse"
        >
          <path d="M 0 1 L 9 5 L 0 9 z" fill="var(--accent-action)" />
        </marker>
      </defs>

      {edges.map(renderEdge)}

      {preview && (
        <path
          d={edgePath(preview.x1, preview.y1, preview.x2, preview.y2)}
          fill="none"
          stroke="var(--accent-action)"
          strokeWidth={2}
          strokeDasharray="5 4"
          style={{ pointerEvents: 'none' }}
        />
      )}
    </svg>
  );
};
