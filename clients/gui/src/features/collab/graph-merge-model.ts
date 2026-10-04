import type { Phase23 } from '@operant/sdk';
import type { WriterArtifactView, WriterWorkspaceView } from '../../live23/multiwriterAdapter';

export function mergeSources(
  node: Phase23.NodeSpec,
  artifactIds: string[],
  artifacts: WriterArtifactView[],
  workspaces: WriterWorkspaceView[],
): { artifactIds: string[]; workspaceIds: string[]; baseRevision: string } {
  if (node.node_kind !== 'merge' || !node.merge_policy) throw new Error('请选择已发布的 Merge 节点。');
  const selected = artifactIds.map((id) => artifacts.find((item) => item.writerArtifactId === id));
  if (selected.some((item) => !item)) throw new Error('来源工件已变化，请刷新运行投影。');
  const sources = selected as WriterArtifactView[];
  const keys = sources.map((item) => workspaces.find((workspace) => workspace.writerWorkspaceId === item.writerWorkspaceId)?.writerKey);
  if (keys.some((key) => !key) || new Set(keys).size !== sources.length) throw new Error('每个来源需要独立的 Writer Workspace。');
  if (keys.length < 2 || new Set(keys).size !== node.merge_policy.source_writer_keys.length
    || node.merge_policy.source_writer_keys.some((key) => !keys.includes(key))) throw new Error('来源工件必须覆盖 Merge 节点冻结的 Writer 集合。');
  const revisions = new Set(sources.map((item) => item.baseRevision));
  if (revisions.size !== 1) throw new Error('来源工件的基线版本不一致。');
  return { artifactIds, workspaceIds: sources.map((item) => item.writerWorkspaceId), baseRevision: sources[0].baseRevision };
}
