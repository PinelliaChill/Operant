import type { Phase23 } from '@operant/sdk';

type Node = Phase23.NodeSpec;
type Edge = Phase23.EdgeSpec;
type Kind = Node['node_kind'];
type Point = { x: number; y: number };

export function hasOutputPort(nodes: Node[], source: { node: string; port: string } | null): boolean {
  if (!source) return false;
  return Boolean(nodes.find((node) => node.node_id === source.node)?.output_ports?.some((port) => port.name === source.port));
}

function sameJson(left: unknown, right: unknown): boolean {
  if (left === right) return true;
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left) && Array.isArray(right)
      && left.length === right.length
      && left.every((value, index) => sameJson(value, right[index]));
  }
  if (left && right && typeof left === 'object' && typeof right === 'object') {
    const leftObject = left as Record<string, unknown>;
    const rightObject = right as Record<string, unknown>;
    const leftKeys = Object.keys(leftObject).sort();
    const rightKeys = Object.keys(rightObject).sort();
    return sameJson(leftKeys, rightKeys)
      && leftKeys.every((key) => sameJson(leftObject[key], rightObject[key]));
  }
  return false;
}

export function nodePosition(node: Node, index: number): Point {
  const value = node.metadata?.canvas_position;
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    const point = value as Record<string, unknown>;
    if (typeof point.x === 'number' && Number.isFinite(point.x) && typeof point.y === 'number' && Number.isFinite(point.y)) return { x: point.x, y: point.y };
  }
  return { x: 70 + (index % 4) * 270, y: 65 + Math.floor(index / 4) * 160 };
}

export function newNode(kind: Kind, index: number): Node {
  const base = `${kind}_${crypto.randomUUID().slice(0, 8)}`;
  const needsTimeout = kind === 'human_input' || kind === 'approval';
  return {
    node_id: base,
    node_kind: kind,
    input_ports: kind === 'artifact' ? [
      { name: 'artifact_id', value_type: 'any', required: false },
      { name: 'content', value_type: 'any', required: false },
    ] : [{ name: 'input', value_type: 'any', required: false }],
    output_ports: kind === 'condition' ? [
      { name: 'true', value_type: 'any', required: false },
      { name: 'false', value_type: 'any', required: false },
    ] : [{ name: kind === 'approval' ? 'approved' : kind === 'subworkflow' ? 'child_run_id' : kind === 'artifact' ? 'artifact_id' : kind === 'merge' ? 'result_artifact_ref' : 'output', value_type: 'any', required: false }],
    metadata: {
      canvas_position: { x: 70 + (index % 4) * 270, y: 65 + Math.floor(index / 4) * 160 },
      ...(['timer', 'wait'].includes(kind) ? { delay_seconds: 0 } : {}),
      ...(kind === 'artifact' ? { media_type: 'text/plain' } : {}),
    },
    ...(kind === 'artifact' ? { idempotency_class: 'idempotent' as const } : {}),
    ...(needsTimeout ? { timeout_policy: { timeout_seconds: 300, on_timeout_node_id: null } } : {}),
    ...(kind === 'subworkflow' ? { subworkflow_id: '', subworkflow_version: 1 } : {}),
    ...(kind === 'merge' ? {
      writes_workspace: true,
      idempotency_class: 'non_idempotent' as const,
      merge_policy: { strategy: 'three_way', source_writer_keys: [], require_review: true, rollback_on_failure: true },
    } : {}),
    ...(kind === 'loop' ? { loop_policy: { max_iterations: 3, max_wall_seconds: 300, max_output_tokens: 2000, max_cost_usd: 1, max_subagents: 0, max_recursion_depth: 1, exit_expression: 'False', on_limit_node_id: base } } : {}),
  };
}

export function graphDifference(base: Phase23.WorkflowDefinition | null, nodes: Node[], edges: Edge[]): { added: string[]; removed: string[]; changed: string[]; edgeAdded: string[]; edgeRemoved: string[] } {
  if (!base) return { added: nodes.map((n) => n.node_id), removed: [], changed: [], edgeAdded: edges.map((e) => e.edge_id), edgeRemoved: [] };
  const oldNodes = new Map(base.nodes.map((n) => [n.node_id, n]));
  const newNodes = new Map(nodes.map((n) => [n.node_id, n]));
  const oldEdges = new Map((base.edges || []).map((e) => [e.edge_id, e]));
  const newEdges = new Map(edges.map((e) => [e.edge_id, e]));
  return {
    added: nodes.filter((n) => !oldNodes.has(n.node_id)).map((n) => n.node_id),
    removed: base.nodes.filter((n) => !newNodes.has(n.node_id)).map((n) => n.node_id),
    changed: nodes.filter((n) => oldNodes.has(n.node_id) && !sameJson(oldNodes.get(n.node_id), n)).map((n) => n.node_id),
    edgeAdded: edges.filter((e) => !oldEdges.has(e.edge_id) || !sameJson(oldEdges.get(e.edge_id), e)).map((e) => e.edge_id),
    edgeRemoved: (base.edges || []).filter((e) => !newEdges.has(e.edge_id)).map((e) => e.edge_id),
  };
}
