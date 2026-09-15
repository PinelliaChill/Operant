import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import {
  Check,
  CircleStop,
  FileCheck,
  Link as LinkIcon,
  Pause,
  Play,
  RefreshCw,
  RotateCcw,
  Send,
  ShieldAlert,
  SquarePen,
} from 'lucide-react';
import { Link, useSearchParams } from 'react-router-dom';
import type { Phase23 } from '@operant/sdk';
import type * as B2 from '../../../../../sdk/typescript-client/b2.generated';
import type * as B24 from '../../../../../sdk/typescript-client/b2_4.generated';
import { useOperant } from '../../context/ClientContext';
import {
  consumeIteratorForEpoch,
  cursorForScope,
  eventsForScope,
  graphRunIsTerminal,
  IdempotencyKeyRegistry,
  emptyRuntimeEventState,
  reduceRuntimeEvent,
} from '../../live23/runtimeState';
import { mapRuntimeFrame } from '../../live23/phase23Adapter';
import {
  loadMultiWriterProjection,
  type MergeRunView,
  type WriterArtifactView,
  type WriterConflictView,
  type WriterWorkspaceView,
} from '../../live23/multiwriterAdapter';
import { B24Empty, B24Section, B24Status } from './B24Presentation';
import {
  commandResourceId,
  canPrepareTeamForGraph,
  currentRoster,
  filterGraphRunsByWorkspace,
  graphRunSummaryFromProjection,
  mergeGraphRunPages,
  normalizeCollaborationDirectory,
  roleLabel,
  teamKey,
  teamLabel,
  type CollaborationDirectory,
  type CollaborationGraphRun,
  type CollaborationTeam,
} from './b24-client';
import './live-graph-team.css';

type LiveTab = 'home' | 'canvas' | 'runs';
type Tone = 'neutral' | 'success' | 'warning' | 'danger';
type Audience = 'direct' | 'team';
type TaskStatus = Phase23.TeamTask['status'];

const MESSAGE_KINDS = [
  'TaskAssignment', 'Finding', 'ArtifactPublished', 'Decision', 'Blocker',
  'Question', 'ApprovalRequested', 'StatusUpdate', 'Completion',
] as const;
const TASK_STATUSES: TaskStatus[] = ['open', 'in_progress', 'blocked', 'completed', 'cancelled'];

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Core 请求失败，请检查本地服务后重试。';
}

function workflowIdentity(workflow: B24.WorkflowDefinition | undefined): string | null {
  if (!workflow?.workflow_id || !Number.isInteger(workflow.version) || Number(workflow.version) < 1) return null;
  return `${workflow.workflow_id}:v${workflow.version}`;
}

function teamIdentity(team: CollaborationTeam | undefined): string | null {
  if (!team?.team_id || !Number.isInteger(team.version) || Number(team.version) < 1) return null;
  return teamKey({ team_id: team.team_id, version: team.version });
}

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    created: '已创建', queued: '排队中', running: '运行中', active: '协作中',
    waiting_input: '等待输入', waiting_approval: '等待审批', waiting: '等待中',
    interrupted: '已中断', manual_reconcile_required: '需要人工核对', completed: '已完成',
    succeeded: '已完成', failed: '已失败', cancelled: '已取消', pending: '待处理',
    ready: '就绪', retry_wait: '等待重试', skipped: '已跳过', invited: '已邀请',
    idle: '空闲', delivered: '已送达', acked: '已确认', expired: '已过期',
    open: '待处理', in_progress: '进行中', blocked: '已阻塞', draft: '草稿',
    published: '已发布', disconnected: '已断开', connecting: '连接中', live: '实时',
  };
  return labels[status] ?? status;
}

function toneFor(status: string): Tone {
  if (['completed', 'succeeded', 'published', 'acked'].includes(status)) return 'success';
  if (['running', 'active', 'ready', 'in_progress', 'delivered', 'live', 'connecting'].includes(status)) return 'warning';
  if (['failed', 'cancelled', 'blocked', 'manual_reconcile_required', 'expired', 'disconnected'].includes(status)) return 'danger';
  return 'neutral';
}

function payloadText(item: Phase23.TeamMessage): string {
  const text = item.payload?.text;
  if (typeof text === 'string' && text.trim()) return text;
  try { return JSON.stringify(item.payload); } catch { return '消息内容无法展示。'; }
}

function unique(values: string[]): string[] {
  return [...new Set(values.filter((value) => value.trim()))];
}

function rolesIn(workflow: B24.WorkflowDefinition | undefined): string[] {
  if (!workflow) return [];
  return unique(workflow.nodes.map((node) => node.metadata?.role_id).filter((id): id is string => typeof id === 'string'));
}

function memberLabel(member: Phase23.TeamRosterMember, team: CollaborationTeam | undefined): string {
  const definition = team?.members.find((candidate) => candidate.member_id === member.member_id);
  return definition?.role || definition?.agent_definition_id || member.member_id;
}

function commandOptions(idempotencyKey: string): { idempotencyKey: string } {
  return { idempotencyKey };
}

export const LiveGraphTeamView: React.FC<{ activeTab: LiveTab }> = ({ activeTab }) => {
  const {
    b24Client, b2Client, phase23Client, phase56Client, connectionStatus,
    activeWorkspace, addNotification,
  } = useOperant();
  const [searchParams] = useSearchParams();
  const legacyParam = searchParams.get('legacyWorkflowRunId') || '';
  const teamRunParam = searchParams.get('teamRunId') || '';

  const keys = useRef(new IdempotencyKeyRegistry());
  const streamEpoch = useRef(0);
  const streamIterator = useRef<AsyncIterator<Phase23.SseFrame> | null>(null);
  const graphScopeEpoch = useRef(0);
  const graphQueryEpoch = useRef(0);
  const teamScopeEpoch = useRef(0);
  const teamQueryEpoch = useRef(0);
  const directoryEpoch = useRef(0);
  const busyEpoch = useRef(0);
  const workspaceEpoch = useRef(0);
  const previousWorkspace = useRef(activeWorkspace);
  const resolvedLegacy = useRef('');

  const [directory, setDirectory] = useState<CollaborationDirectory | null>(null);
  const [directoryLoading, setDirectoryLoading] = useState(false);
  const [directoryError, setDirectoryError] = useState<string | null>(null);
  const [workflowSelection, setWorkflowSelection] = useState('');
  const [teamSelection, setTeamSelection] = useState('');
  const [roleIds, setRoleIds] = useState<string[]>([]);
  const [memberIds, setMemberIds] = useState<string[]>([]);
  const [graphName, setGraphName] = useState('');
  const [graphDescription, setGraphDescription] = useState('');
  const [graphTask, setGraphTask] = useState('');
  const [workflowDefinition, setWorkflowDefinition] = useState<B24.WorkflowDefinition | null>(null);
  const [legacyWorkflowRunId, setLegacyWorkflowRunId] = useState('');
  const [graphRunId, setGraphRunId] = useState('');
  const [graphRun, setGraphRun] = useState<Phase23.GraphWorkflowRun | null>(null);
  const [knownGraphRuns, setKnownGraphRuns] = useState<CollaborationGraphRun[]>([]);
  const [graphRunsHasMore, setGraphRunsHasMore] = useState(false);
  const [graphRunsCursor, setGraphRunsCursor] = useState<string | null>(null);
  const [nodeRuns, setNodeRuns] = useState<Phase23.NodeRun[]>([]);
  const [writerWorkspaces, setWriterWorkspaces] = useState<WriterWorkspaceView[]>([]);
  const [writerArtifacts, setWriterArtifacts] = useState<WriterArtifactView[]>([]);
  const [writerConflicts, setWriterConflicts] = useState<WriterConflictView[]>([]);
  const [mergeRuns, setMergeRuns] = useState<MergeRunView[]>([]);
  const [events, setEvents] = useState(emptyRuntimeEventState);
  const [streamStatus, setStreamStatus] = useState<'idle' | 'connecting' | 'live' | 'disconnected'>('idle');
  const [teamRunId, setTeamRunId] = useState('');
  const [viewerAgentId, setViewerAgentId] = useState('');
  const [recipientAgentId, setRecipientAgentId] = useState('');
  const [messageAudience, setMessageAudience] = useState<Audience>('direct');
  const [messageKind, setMessageKind] = useState<(typeof MESSAGE_KINDS)[number]>('StatusUpdate');
  const [teamTask, setTeamTask] = useState('');
  const [teamRun, setTeamRun] = useState<Phase23.TeamRunProjection | null>(null);
  const [messages, setMessages] = useState<Phase23.TeamMessage[]>([]);
  const [mailbox, setMailbox] = useState<Phase23.MailboxDelivery[]>([]);
  const [tasks, setTasks] = useState<Phase23.TeamTask[]>([]);
  const [artifacts, setArtifacts] = useState<Phase23.ArtifactBoardItem[]>([]);
  const [outgoing, setOutgoing] = useState('');
  const [artifactToPublish, setArtifactToPublish] = useState('');
  const [artifactTitle, setArtifactTitle] = useState('');
  const [agentInstances, setAgentInstances] = useState<B2.AgentInstance[]>([]);
  const [agentHistory, setAgentHistory] = useState<B2.B2SessionHistory | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const selectedWorkflow = useMemo(
    () => directory?.workflows.find((workflow) => workflowIdentity(workflow) === workflowSelection),
    [directory, workflowSelection],
  );
  const selectedTeam = useMemo(
    () => directory?.teams.find((team) => teamIdentity(team) === teamSelection),
    [directory, teamSelection],
  );
  const workflowId = selectedWorkflow?.workflow_id || '';
  const workflowVersion = Number(selectedWorkflow?.version || 1);
  const teamId = selectedTeam?.team_id || '';
  const teamVersion = Number(selectedTeam?.version || 1);
  const roster = currentRoster(teamRun?.roster ?? []);
  const chosenRecipients = messageAudience === 'team' ? roster : roster.filter((member) => member.agent_instance_id === recipientAgentId);
  const messageRecipientsActive = chosenRecipients.length > 0 && chosenRecipients.every((member) => member.status === 'active');
  const selectedViewer = roster.find((member) => member.agent_instance_id === viewerAgentId);
  const graphScope = `graph_run:${graphRunId.trim()}`;
  const scopedEvents = eventsForScope(events, graphScope);
  const artifactCandidates = useMemo(() => {
    const result = new Map<string, { title: string; sourceMessageId?: string | null; revision: number }>();
    artifacts.forEach((artifact) => result.set(artifact.artifact_id, { title: artifact.title, sourceMessageId: artifact.source_message_id, revision: artifact.revision }));
    tasks.forEach((task) => task.artifact_refs.forEach((id) => { if (!result.has(id)) result.set(id, { title: id, sourceMessageId: task.source_message_id, revision: 0 }); }));
    return [...result.entries()].map(([artifactId, item]) => ({ artifactId, ...item }));
  }, [artifacts, tasks]);

  const run = async (operation: () => Promise<unknown>, isCurrent = () => true) => {
    const epoch = ++busyEpoch.current;
    setBusy(true); setError(null);
    try { await operation(); }
    catch (caught) { if (epoch === busyEpoch.current && isCurrent()) setError(errorMessage(caught)); }
    finally { if (epoch === busyEpoch.current && isCurrent()) setBusy(false); }
  };

  const loadDirectory = useCallback(async (cursor?: string) => {
    const epoch = ++directoryEpoch.current;
    setDirectoryLoading(true); setDirectoryError(null);
    if (!cursor) {
      invalidateGraph({ clearRunId: true, clearEvents: true }); clearTeam(false, true);
      setKnownGraphRuns([]); setGraphRunsHasMore(false); setGraphRunsCursor(null);
    }
    try {
      const next = normalizeCollaborationDirectory(await b24Client.getCollaborationDirectory({ workspace: activeWorkspace.trim() || undefined, cursor }));
      if (epoch !== directoryEpoch.current) return next;
      setDirectory(next);
      const workspaceRuns = filterGraphRunsByWorkspace(next.graph_runs, activeWorkspace);
      setKnownGraphRuns((current) => cursor ? mergeGraphRunPages(current, workspaceRuns) : workspaceRuns);
      setGraphRunsHasMore(next.graph_runs_has_more);
      setGraphRunsCursor(next.graph_runs_next_cursor);
      if (cursor) return next;
      setWorkflowSelection((current) => current && next.workflows.some((item) => workflowIdentity(item) === current)
        ? current
        : workflowIdentity(next.workflows.filter((item) => item.status === 'published').sort((a, b) => Number(b.version || 0) - Number(a.version || 0))[0] || next.workflows[0]) || '');
      setTeamSelection((current) => current && next.teams.some((item) => teamIdentity(item) === current) ? current : teamIdentity(next.teams[0]) || '');
      setRoleIds((current) => { const ids = new Set(next.roles.map((role) => role.id)); const keep = current.filter((id) => ids.has(id)); return keep.length ? keep : next.roles.slice(0, 3).map((role) => role.id); });
      return next;
    } catch (caught) {
      if (epoch !== directoryEpoch.current) return;
      setDirectoryError(errorMessage(caught));
      if (cursor) return;
      setDirectory(null); setKnownGraphRuns([]); setGraphRunsHasMore(false); setGraphRunsCursor(null);
      invalidateGraph({ clearRunId: true, clearEvents: true, cancelPending: true }); clearTeam(true, true);
    } finally { if (epoch === directoryEpoch.current) setDirectoryLoading(false); }
  }, [activeWorkspace, b24Client]);

  const loadAgents = useCallback(async () => {
    try {
      const page = await b2Client.listAgents({ limit: 100 });
      setAgentInstances(page.items);
    } catch (caught) {
      setError(`Agent 目录读取失败：${errorMessage(caught)}`);
    }
  }, [b2Client]);

  useEffect(() => { void loadDirectory(); void loadAgents(); }, [loadAgents, loadDirectory]);

  useEffect(() => {
    if (!selectedWorkflow) return;
    setGraphName((current) => current || selectedWorkflow.name);
    setGraphDescription((current) => current || selectedWorkflow.description || '');
    const ids = rolesIn(selectedWorkflow);
    if (ids.length) setRoleIds((current) => unique([...ids, ...current]));
  }, [selectedWorkflow]);

  useEffect(() => {
    const ids = selectedTeam?.members.map((member) => member.member_id) ?? [];
    setMemberIds((current) => { const keep = current.filter((id) => ids.includes(id)); return keep.length ? keep : ids; });
  }, [selectedTeam]);

  useEffect(() => {
    const id = viewerAgentId.trim();
    const agent = agentInstances.find((item) => item.id === id);
    if (!agent) { setAgentHistory(null); return; }
    let cancelled = false;
    void b2Client.getSessionHistory(agent.session_id, { limit: 50 }).then((history) => { if (!cancelled) setAgentHistory(history); }).catch(() => { if (!cancelled) setAgentHistory(null); });
    return () => { cancelled = true; };
  }, [agentInstances, b2Client, viewerAgentId]);

  const safelyReturnIterator = (iterator: AsyncIterator<Phase23.SseFrame> | null) => {
    if (iterator?.return) void Promise.resolve(iterator.return()).catch(() => undefined);
  };
  const stopStream = () => { streamEpoch.current += 1; safelyReturnIterator(streamIterator.current); streamIterator.current = null; setStreamStatus('idle'); };
  const invalidateGraph = ({ clearRunId = false, clearEvents = false, cancelPending = false } = {}) => {
    stopStream(); graphScopeEpoch.current += 1; graphQueryEpoch.current += 1;
    if (cancelPending) { busyEpoch.current += 1; setBusy(false); setError(null); }
    if (clearRunId) setGraphRunId('');
    if (clearEvents) setEvents(emptyRuntimeEventState());
    setGraphRun(null); setNodeRuns([]); setWriterWorkspaces([]); setWriterArtifacts([]); setWriterConflicts([]); setMergeRuns([]);
    return graphScopeEpoch.current;
  };
  const clearTeam = (cancelPending = false, clearRunId = false) => {
    teamScopeEpoch.current += 1; teamQueryEpoch.current += 1;
    if (cancelPending) { busyEpoch.current += 1; setBusy(false); setError(null); }
    if (clearRunId) setTeamRunId('');
    setTeamRun(null); setMessages([]); setMailbox([]); setTasks([]); setArtifacts([]); setViewerAgentId(''); setRecipientAgentId('');
    setArtifactToPublish(''); setArtifactTitle(''); setAgentHistory(null);
    return teamScopeEpoch.current;
  };

  const refreshGraph = async (runId = graphRunId, expectedScope = graphScopeEpoch.current, current = () => true) => {
    const id = runId.trim(); if (!id) throw new Error('请先选择一个 Graph Run。');
    const epoch = ++graphQueryEpoch.current;
    const [nextRun, nextNodes, multiWriter] = await Promise.all([phase23Client.getGraphRun(id), phase23Client.listNodeRuns(id), loadMultiWriterProjection(phase56Client, id)]);
    if (epoch !== graphQueryEpoch.current || expectedScope !== graphScopeEpoch.current || !current()) return null;
    if (nextRun.workspace_or_target && nextRun.workspace_or_target !== activeWorkspace.trim()) throw new Error('Core 返回了其他工作区的 Graph Run。');
    setGraphRun(nextRun);
    const summary = graphRunSummaryFromProjection(nextRun);
    if (summary.workspace_or_target === activeWorkspace.trim()) {
      setKnownGraphRuns((items) => [summary, ...items.filter((item) => item.id !== summary.id)]);
    }
    setNodeRuns(nextNodes);
    setWriterWorkspaces(multiWriter.workspaces); setWriterArtifacts(multiWriter.artifacts); setWriterConflicts(multiWriter.conflicts); setMergeRuns(multiWriter.merges);
    return nextRun;
  };

  const loadDefinition = () => {
    const expectedWorkspace = workspaceEpoch.current;
    return run(async () => {
      if (!workflowId) throw new Error('请先从 Core 目录选择工作流模板。');
      const definition = await phase23Client.getWorkflowDefinition(workflowId, workflowVersion);
      if (expectedWorkspace !== workspaceEpoch.current) return;
      setWorkflowDefinition(definition); setGraphName(definition.name); setGraphDescription(definition.description || '');
    }, () => expectedWorkspace === workspaceEpoch.current);
  };

  const resolveLegacy = (requested = legacyWorkflowRunId) => {
    const scope = invalidateGraph({ clearRunId: true });
    return run(async () => {
      if (!requested.trim()) throw new Error('请填写旧 Coding Workflow Run ID。');
      const linked = await phase23Client.getGraphRunByLegacyWorkflow(requested.trim());
      const nodes = await phase23Client.listNodeRuns(linked.id);
      if (scope !== graphScopeEpoch.current) return;
      setGraphRunId(linked.id); setGraphRun(linked); setNodeRuns(nodes);
      const summary = graphRunSummaryFromProjection(linked);
      if (summary.workspace_or_target === activeWorkspace.trim()) {
        setKnownGraphRuns((items) => [summary, ...items.filter((item) => item.id !== summary.id)]);
      }
    }, () => scope === graphScopeEpoch.current);
  };

  useEffect(() => {
    if (activeTab !== 'runs' || !legacyParam || resolvedLegacy.current === legacyParam) return;
    resolvedLegacy.current = legacyParam; setLegacyWorkflowRunId(legacyParam); void resolveLegacy(legacyParam);
  }, [activeTab, legacyParam]);
  useEffect(() => { if (teamRunParam && teamRunParam !== teamRunId) setTeamRunId(teamRunParam); }, [teamRunId, teamRunParam]);

  const createGraph = () => run(async () => {
    if (!graphName.trim()) throw new Error('请填写工作流名称。');
    if (!roleIds.length) throw new Error('请至少选择一个 RolePreset。');
    const logical = `graph-create:${workflowId || 'new'}:${graphName}:${graphDescription}:${roleIds.join(',')}:${activeWorkspace}`;
    const result = await b24Client.execute({ action: 'graph_create_from_roles', name: graphName.trim(), description: graphDescription.trim() || undefined, role_ids: roleIds, workflow_id: workflowId || undefined, workspace_or_target: activeWorkspace.trim() || undefined, task: graphTask.trim() || undefined }, commandOptions(keys.current.get(logical)));
    keys.current.release(logical);
    const id = commandResourceId(result, 'Graph 编排');
    const nextDirectory = normalizeCollaborationDirectory(result.directory); setDirectory(nextDirectory);
    const created = nextDirectory.workflows.filter((item) => item.workflow_id === id).sort((a, b) => Number(b.version || 0) - Number(a.version || 0))[0];
    if (created) { setWorkflowSelection(workflowIdentity(created) || ''); setWorkflowDefinition(null); }
    addNotification('success', 'Graph 草稿已由 Core 创建；请校验并发布后运行。');
  });

  const compileGraph = () => run(async () => {
    if (!workflowId) throw new Error('请先选择工作流模板。');
    const logical = `graph-compile:${workflowId}:${workflowVersion}`;
    const result = await phase23Client.compileWorkflowDraft(workflowId, { definition_version: workflowVersion }, commandOptions(keys.current.get(logical)));
    keys.current.release(logical);
    if (!result.valid) throw new Error(result.diagnostics.map((item) => `${item.code}: ${item.message}`).join('；') || 'Graph 校验未通过。');
    addNotification('success', `Graph 校验通过：${workflowId} v${workflowVersion}。`);
  });

  const publishGraph = () => run(async () => {
    if (!workflowId) throw new Error('请先选择工作流模板。');
    if (selectedWorkflow?.status === 'published') throw new Error('当前版本已经发布；如需编辑，请创建新的草稿版本。');
    const logical = `graph-publish:${workflowId}:${workflowVersion}`;
    const receipt = await phase23Client.publishWorkflow(workflowId, { draft_version: workflowVersion }, commandOptions(keys.current.get(logical)));
    keys.current.release(logical); if (!receipt.accepted) throw new Error('Core 未接受发布请求。');
    const nextDirectory = await loadDirectory();
    const published = nextDirectory?.workflows
      .filter((item) => item.workflow_id === workflowId && item.status === 'published')
      .sort((left, right) => Number(right.version || 0) - Number(left.version || 0))[0];
    if (published) setWorkflowSelection(workflowIdentity(published) || workflowSelection);
    addNotification('success', '工作流版本已提交发布；状态以 Core 目录刷新结果为准。');
  });

  const startGraph = () => {
    const scope = invalidateGraph({ clearRunId: true }); const expectedWorkspace = workspaceEpoch.current; const current = () => scope === graphScopeEpoch.current && expectedWorkspace === workspaceEpoch.current;
    return run(async () => {
      if (!workflowId) throw new Error('请先选择工作流模板。');
      if (selectedWorkflow?.status !== 'published') throw new Error('只有 Core 目录中的已发布版本可以运行。');
      const logical = `start-graph:${workflowId}:${workflowVersion}:${activeWorkspace}`;
      const receipt = await phase23Client.startGraphRun({ workflow_id: workflowId, definition_version: workflowVersion, input: graphTask.trim() ? { task: graphTask.trim() } : {}, workspace_or_target: activeWorkspace.trim() || undefined }, commandOptions(keys.current.get(logical)));
      keys.current.release(logical); if (!receipt.resource_id) throw new Error('Core 未返回 Graph Run ID。'); if (!current()) return;
      setGraphRunId(receipt.resource_id); const runProjection = await refreshGraph(receipt.resource_id, scope, current); if (runProjection) addNotification('success', `Graph Run 已创建：${runProjection.id}。`);
    }, current);
  };

  const monitorGraph = async () => {
    setError(null); const id = graphRunId.trim(); const expectedScope = graphScopeEpoch.current; let epoch = streamEpoch.current; let iterator: AsyncIterator<Phase23.SseFrame> | null = null;
    try {
      if (!id) throw new Error('请先选择一个 Graph Run。');
      stopStream(); epoch = ++streamEpoch.current; const current = () => epoch === streamEpoch.current && expectedScope === graphScopeEpoch.current; setStreamStatus('connecting'); const scope = `graph_run:${id}`;
      const stream = await phase23Client.streamGraphRunEvents(id, { lastEventId: cursorForScope(events, scope) }); iterator = stream.events[Symbol.asyncIterator]();
      if (!current()) { safelyReturnIterator(iterator); return; } streamIterator.current = iterator; setStreamStatus('live');
      const completion = await consumeIteratorForEpoch(iterator, current, (frame) => setEvents((state) => reduceRuntimeEvent(state, mapRuntimeFrame(frame, scope))));
      if (streamIterator.current === iterator) streamIterator.current = null; if (completion === 'stale' || !current()) return;
      const corrected = await refreshGraph(id, expectedScope, current); if (corrected && graphRunIsTerminal(corrected.status)) setStreamStatus('idle'); else { setStreamStatus('disconnected'); setError('事件流已结束，但 Graph Run 尚未终止。已保留 Cursor，可点击“从 Cursor 重连”。'); }
    } catch (caught) {
      if (epoch !== streamEpoch.current || expectedScope !== graphScopeEpoch.current) return; if (streamIterator.current === iterator) streamIterator.current = null; setStreamStatus('disconnected'); setError(`${errorMessage(caught)} 已保留 Cursor，点击“从 Cursor 重连”继续。`);
    }
  };

  const refreshTeamProjection = async (expectedScope = teamScopeEpoch.current, requestedViewer = viewerAgentId, requestedRunId = teamRunId) => {
    const id = requestedRunId.trim(); if (!id) throw new Error('请先启动 Team Run 或打开已有 Team Run。'); const epoch = ++teamQueryEpoch.current; const nextRun = await phase23Client.getTeamRun(id);
    if (epoch !== teamQueryEpoch.current || expectedScope !== teamScopeEpoch.current) return;
    const viewer = currentRoster(nextRun.roster).find((item) => item.agent_instance_id === requestedViewer)?.agent_instance_id || currentRoster(nextRun.roster)[0]?.agent_instance_id;
    if (!viewer) throw new Error('Core 返回的 Team Roster 没有可用 Agent。');
    const [nextMessages, nextMailbox, nextTasks, nextArtifacts] = await Promise.all([phase23Client.listTeamMessages(id, { viewerId: viewer }), phase23Client.getMailbox(id, viewer), phase23Client.getTaskBoard(id), phase23Client.getArtifactBoard(id, { viewerId: viewer })]);
    if (epoch !== teamQueryEpoch.current || expectedScope !== teamScopeEpoch.current) return;
    setTeamSelection(`${nextRun.team_id}:v${nextRun.team_version}`);
    setTeamRun(nextRun); setViewerAgentId(viewer); setRecipientAgentId((current) => currentRoster(nextRun.roster).some((item) => item.agent_instance_id === current) ? current : currentRoster(nextRun.roster)[0]?.agent_instance_id || ''); setMessages(nextMessages); setMailbox(nextMailbox.deliveries); setTasks(nextTasks.tasks); setArtifacts(nextArtifacts.artifacts);
    setArtifactToPublish((current) => current || nextArtifacts.artifacts[0]?.artifact_id || ''); setArtifactTitle((current) => current || nextArtifacts.artifacts[0]?.title || '');
  };
  const refreshTeam = () => { const scope = teamScopeEpoch.current; return run(() => refreshTeamProjection(scope), () => scope === teamScopeEpoch.current); };

  const changeViewer = (viewer: string) => {
    const scope = ++teamScopeEpoch.current;
    teamQueryEpoch.current += 1;
    busyEpoch.current += 1;
    setViewerAgentId(viewer);
    setAgentHistory(null);
    setMessages([]); setMailbox([]); setTasks([]); setArtifacts([]);
    setArtifactToPublish(''); setArtifactTitle('');
    void run(() => refreshTeamProjection(scope, viewer), () => scope === teamScopeEpoch.current);
  };

  const selectGraphRun = (id: string) => {
    const graphEpoch = invalidateGraph({ clearRunId: true, clearEvents: true, cancelPending: true });
    const teamEpoch = clearTeam(true, true);
    if (!id) return;
    if (!knownGraphRuns.some((item) => item.id === id && item.workspace_or_target === activeWorkspace.trim())) {
      setError('请选择当前工作区的 Core Graph Run。'); return;
    }
    setGraphRunId(id);
    const current = () => graphEpoch === graphScopeEpoch.current && teamEpoch === teamScopeEpoch.current;
    void run(async () => {
      const selected = await refreshGraph(id, graphEpoch, current);
      if (!selected || !current()) return;
      if (selected.team_run_id) {
        setTeamRunId(selected.team_run_id);
        await refreshTeamProjection(teamEpoch, '', selected.team_run_id);
      }
    }, current);
  };

  const startTeam = () => {
    if (!canPrepareTeamForGraph(graphRun)) return;
    const scope = clearTeam(true); const graphId = graphRunId.trim();
    return run(async () => {
      if (!teamId) throw new Error('请先从 Core 目录选择 Team 模板。'); if (!graphId) throw new Error('请先启动或选择一个 Graph Run。'); if (!selectedTeam || memberIds.length !== selectedTeam.members.length) throw new Error('该 Team 需要选择完整 Roster。');
      const logical = `team-start:${teamId}:${teamVersion}:${graphId}:${memberIds.join(',')}:${activeWorkspace}`;
      const result = await b24Client.execute({ action: 'team_start_from_roles', team_id: teamId, team_version: teamVersion, workflow_run_id: graphId, member_ids: memberIds, workspace_or_target: activeWorkspace.trim() || undefined, task: teamTask.trim() || undefined }, commandOptions(keys.current.get(logical)));
      keys.current.release(logical); const id = commandResourceId(result, 'Team 协作'); if (scope !== teamScopeEpoch.current) return; setTeamRunId(id); await loadAgents(); await refreshTeamProjection(scope, '', id); addNotification('success', `Team Run 已创建：${id}。`);
    }, () => scope === teamScopeEpoch.current);
  };

  const acknowledge = (deliveryId: string) => { const scope = teamScopeEpoch.current; const logical = `ack:${teamRunId}:${viewerAgentId}:${deliveryId}`; return run(async () => { await phase23Client.acknowledgeMailboxDelivery(teamRunId.trim(), viewerAgentId.trim(), deliveryId, commandOptions(keys.current.get(logical))); keys.current.release(logical); await refreshTeamProjection(scope); }, () => scope === teamScopeEpoch.current); };
  const sendMessage = () => {
    const scope = teamScopeEpoch.current; const text = outgoing.trim(); const recipients = messageAudience === 'team' ? unique(roster.map((item) => item.agent_instance_id)) : recipientAgentId.trim() ? [recipientAgentId.trim()] : []; const logical = `message:${teamRunId}:${viewerAgentId}:${messageAudience}:${recipients.join(',')}:${messageKind}:${text}`;
    return run(async () => { if (!teamRunId.trim()) throw new Error('请先启动 Team Run。'); if (!viewerAgentId.trim() || !recipients.length || !text) throw new Error('请选择发送方、接收方并填写消息内容。'); if (!messageRecipientsActive) throw new Error('接收成员已结束，不能接收新消息。'); await phase23Client.sendTeamMessage(teamRunId.trim(), { sender_id: viewerAgentId.trim(), recipient_ids: recipients, audience: messageAudience, message_kind: messageKind, payload: { text }, requires_ack: true }, commandOptions(keys.current.get(logical))); keys.current.release(logical); setOutgoing((current) => current.trim() === text ? '' : current); await refreshTeamProjection(scope); }, () => scope === teamScopeEpoch.current);
  };
  const updateTask = (task: Phase23.TeamTask, status: TaskStatus) => { const scope = teamScopeEpoch.current; const logical = `task:${teamRunId}:${task.task_id}:${task.revision}:${status}`; return run(async () => { await phase23Client.updateTeamTask(teamRunId.trim(), task.task_id, { title: task.title, summary: task.summary, assignee_ids: task.assignee_ids, status, artifact_refs: task.artifact_refs, source_message_id: task.source_message_id, expected_revision: task.revision }, commandOptions(keys.current.get(logical))); keys.current.release(logical); await refreshTeamProjection(scope); }, () => scope === teamScopeEpoch.current); };
  const publishArtifact = () => { const scope = teamScopeEpoch.current; const candidate = artifactCandidates.find((item) => item.artifactId === artifactToPublish); const logical = `artifact:${teamRunId}:${artifactToPublish}:${artifactTitle}:${candidate?.revision || 0}`; return run(async () => { if (!candidate) throw new Error('请选择 Core 已返回的 Artifact 引用。'); if (!artifactTitle.trim()) throw new Error('请填写产物标题。'); await phase23Client.publishArtifactBoardItem(teamRunId.trim(), { artifact_id: candidate.artifactId, title: artifactTitle.trim(), publisher_id: viewerAgentId.trim(), recipient_ids: messageAudience === 'team' ? roster.map((item) => item.agent_instance_id) : [viewerAgentId.trim()], source_message_id: candidate.sourceMessageId, expected_revision: candidate.revision }, commandOptions(keys.current.get(logical))); keys.current.release(logical); await refreshTeamProjection(scope); addNotification('success', 'Artifact 发布请求已由 Core 接收。'); }, () => scope === teamScopeEpoch.current); };

  useLayoutEffect(() => { if (previousWorkspace.current === activeWorkspace) return; previousWorkspace.current = activeWorkspace; workspaceEpoch.current += 1; directoryEpoch.current += 1; invalidateGraph({ clearRunId: true, clearEvents: true, cancelPending: true }); clearTeam(true, true); setWorkflowDefinition(null); setKnownGraphRuns([]); setGraphRunsHasMore(false); setGraphRunsCursor(null); }, [activeWorkspace]);
  useEffect(() => () => { directoryEpoch.current += 1; streamEpoch.current += 1; graphScopeEpoch.current += 1; graphQueryEpoch.current += 1; teamScopeEpoch.current += 1; teamQueryEpoch.current += 1; busyEpoch.current += 1; safelyReturnIterator(streamIterator.current); streamIterator.current = null; }, []);

  const connected = connectionStatus === 'connected';
  const renderDirectoryError = directoryError && <div className="b24-error" role="alert"><ShieldAlert size={16} aria-hidden="true" /><span>协作目录读取失败：{directoryError}</span><button type="button" className="btn btn-secondary btn-sm" onClick={() => void loadDirectory()} disabled={directoryLoading}>重试</button></div>;
  return (
    <div className="section-scroll"><div className="section-inner live23-shell b24-layout" data-client-mode="live">
      <div className="live23-status b24-card" role="status" aria-live="polite"><B24Status label={connectionStatus === 'connected' ? '已连接本地 Core' : '本地 Core 未连接'} tone={connected ? 'success' : connectionStatus === 'reconnecting' ? 'warning' : 'danger'} /><span className="live23-muted">选择成员并发布工作流，运行状态由本地服务同步。</span></div>
      {graphRunsHasMore && <div className="b24-actions"><span className="b24-field-help">当前工作区还有更早的运行。</span><button type="button" className="btn btn-secondary btn-sm" onClick={() => void loadDirectory(graphRunsCursor || undefined)} disabled={directoryLoading || !graphRunsCursor}>加载更早运行</button></div>}
      {error && <div className="live23-error b24-error" role="alert"><ShieldAlert size={16} aria-hidden="true" /><span>{error}</span></div>}{renderDirectoryError}

      {activeTab === 'canvas' && <div className="b24-layout-split"><div className="b24-layout-main"><B24Section title="Graph 编排" description="从 Core 返回的 RolePreset 选择成员，编辑后创建新的版本草稿。已发布定义保持不可变。" actions={<div className="b24-actions"><button type="button" className="btn btn-secondary btn-sm" onClick={() => void loadDirectory()} disabled={busy || directoryLoading}><RefreshCw size={14} aria-hidden="true" />刷新目录</button><button type="button" className="btn btn-secondary btn-sm" onClick={() => void loadDefinition()} disabled={busy || !workflowId}><RefreshCw size={14} aria-hidden="true" />读取定义</button></div>}>
        <form className="b24-form" onSubmit={(event) => { event.preventDefault(); void createGraph(); }}><div className="b24-grid b24-grid-2"><label className="b24-field"><span className="b24-field-label">模板版本<span className="b24-field-required">*</span></span><select value={workflowSelection} onChange={(event) => { setWorkflowSelection(event.target.value); setWorkflowDefinition(null); }}><option value="">选择 Core 模板</option>{directory?.workflows.map((workflow) => { const id = workflowIdentity(workflow); return id ? <option key={id} value={id}>{workflow.name} · v{workflow.version} · {statusLabel(workflow.status || 'draft')}</option> : null; })}</select><span className="b24-field-help">选择已有版本，或填写名称新建工作流。</span></label><label className="b24-field"><span className="b24-field-label">工作流名称<span className="b24-field-required">*</span></span><input value={graphName} onChange={(event) => setGraphName(event.target.value)} placeholder="例如：代码评审协作" /></label></div><label className="b24-field"><span className="b24-field-label">描述</span><textarea rows={2} value={graphDescription} onChange={(event) => setGraphDescription(event.target.value)} placeholder="说明这个编排适用的任务条件。" /></label><label className="b24-field"><span className="b24-field-label">本次任务提示</span><textarea rows={2} value={graphTask} onChange={(event) => setGraphTask(event.target.value)} placeholder="运行 Graph 时传给节点的任务提示。" /></label><fieldset className="b24-fieldset b24-card"><legend>RolePreset 成员</legend><div className="b24-list b24-list-grid">{directory?.roles.map((role) => <label className="b24-check-row" key={role.id}><input type="checkbox" checked={roleIds.includes(role.id)} onChange={(event) => setRoleIds((current) => event.target.checked ? unique([...current, role.id]) : current.filter((id) => id !== role.id))} /><span><strong>{roleLabel(role)}</strong><small>推理强度：{role.effort}</small></span></label>)}{directory?.roles.length === 0 && <B24Empty title="Core 尚未返回 RolePreset" description="请先在 Agent 设置中配置可用角色。" />}</div></fieldset><div className="b24-actions"><button type="submit" className="btn btn-primary" disabled={busy || !connected || !directory || !roleIds.length}><SquarePen size={14} aria-hidden="true" />保存为新草稿版本</button><button type="button" className="btn btn-secondary" onClick={() => void compileGraph()} disabled={busy || !connected || !workflowId}><FileCheck size={14} aria-hidden="true" />校验当前版本</button><button type="button" className="btn btn-secondary" onClick={() => void publishGraph()} disabled={busy || !connected || !workflowId || selectedWorkflow?.status === 'published'}><Check size={14} aria-hidden="true" />发布当前草稿</button><button type="button" className="btn btn-primary" onClick={() => void startGraph()} disabled={busy || !connected || !workflowId || selectedWorkflow?.status !== 'published'}><Play size={14} aria-hidden="true" />启动 Graph Run</button></div></form>
        {workflowDefinition && <div className="b24-card b24-card-subtle"><strong>{workflowDefinition.name} · v{workflowDefinition.version}</strong><span>{workflowDefinition.nodes.length} 节点 · {workflowDefinition.edges?.length || 0} 连线</span><ol className="b24-list">{workflowDefinition.nodes.map((node) => <li key={node.node_id}><span>{node.node_id}</span><B24Status label={node.node_kind} /></li>)}</ol></div>}
      </B24Section></div><aside className="b24-layout-aside"><B24Section title="当前目录" description="只显示 Core 已确认的工作流、Team 和角色。">{directoryLoading && <p className="b24-field-help">正在读取 Core 协作目录…</p>}{!directoryLoading && !directory && <B24Empty title="协作目录不可用" description="Live 请求失败后不会回退演示数据。" />}{directory && <dl className="b24-detail-list"><div><dt>工作流</dt><dd>{directory.workflows.length}</dd></div><div><dt>Team</dt><dd>{directory.teams.length}</dd></div><div><dt>RolePreset</dt><dd>{directory.roles.length}</dd></div></dl>}</B24Section></aside></div>}

      {activeTab === 'runs' && <B24Section title="Graph 运行进度" description="选择已有运行或使用编排页启动。事件流结束但运行未终止时，会明确保留 Cursor 重连。" actions={<div className="b24-actions"><button className="btn btn-secondary btn-sm" disabled={busy || !graphRunId} onClick={() => void run(() => refreshGraph())}><RefreshCw size={14} aria-hidden="true" />刷新投影</button><button className="btn btn-primary btn-sm" disabled={busy || streamStatus === 'live' || !graphRunId} onClick={() => void monitorGraph()}><RotateCcw size={14} aria-hidden="true" />{streamStatus === 'disconnected' ? '从 Cursor 重连' : '监控事件'}</button><button className="btn btn-secondary btn-sm" disabled={streamStatus === 'idle'} onClick={stopStream}><CircleStop size={14} aria-hidden="true" />停止监控</button></div>}><div className="b24-form"><div className="b24-grid b24-grid-2"><label className="b24-field"><span className="b24-field-label">Graph Run</span><select value={graphRunId} onChange={(event) => selectGraphRun(event.target.value)}><option value="">选择 Graph Run</option>{knownGraphRuns.map((item) => <option key={item.id} value={item.id}>{item.id} · {statusLabel(item.status)}</option>)}</select><span className="b24-field-help">运行由 Core 返回并自动加入列表。</span></label><div className="b24-field"><span className="b24-field-label">运行控制</span><div className="b24-actions"><button className="btn btn-primary btn-sm" disabled={busy || !connected || !graphRunId} onClick={() => void (async () => { const scope = graphScopeEpoch.current; await run(async () => { await phase23Client.resumeGraphRun(graphRunId, { allow_unknown_side_effect_replay: false }, commandOptions(keys.current.get(`resume:${graphRunId}`))); await refreshGraph(graphRunId, scope); addNotification('success', '恢复请求已由 Core 接收。'); }, () => scope === graphScopeEpoch.current); })()}><Play size={13} aria-hidden="true" />恢复</button><button className="btn btn-secondary btn-sm" disabled title="phase23.v1 未提供 Graph Pause Command"><Pause size={13} aria-hidden="true" />暂停（协议待补）</button><button className="btn btn-danger btn-sm" disabled={busy || !connected || !graphRunId} onClick={() => void (async () => { if (!window.confirm(`确认取消 Graph Run ${graphRunId}？`)) return; const scope = graphScopeEpoch.current; await run(async () => { await phase23Client.cancelGraphRun(graphRunId, commandOptions(keys.current.get(`cancel:${graphRunId}`))); await refreshGraph(graphRunId, scope); addNotification('success', '取消请求已由 Core 接收。'); }, () => scope === graphScopeEpoch.current); })()}><CircleStop size={13} aria-hidden="true" />取消运行</button></div></div></div><p className="b24-field-help">流状态：{statusLabel(streamStatus)} · Cursor {cursorForScope(events, graphScope).toString()}</p><details className="b24-compat"><summary>兼容旧 Coding Workflow 深链</summary><div className="b24-form-row"><label className="b24-field"><span className="b24-field-label">旧 Run ID</span><input value={legacyWorkflowRunId} onChange={(event) => setLegacyWorkflowRunId(event.target.value)} placeholder="仅用于已有深链恢复" /></label><button type="button" className="btn btn-secondary btn-sm" onClick={() => void resolveLegacy()} disabled={busy}>解析旧 Workflow</button></div></details>{graphRun && <div className="b24-card b24-card-subtle"><div className="b24-card-heading"><strong>{statusLabel(graphRun.status)}</strong><B24Status label={`revision ${graphRun.revision}`} /></div><span>当前节点：{graphRun.current_node_ids.join('、') || '无'}</span></div>}</div><div className="b24-grid b24-grid-2"><B24Section title="节点"><ul className="b24-list">{nodeRuns.map((node) => <li key={node.id}><span><strong>{node.node_id}</strong><small>{node.id}</small></span><B24Status label={statusLabel(node.status)} tone={toneFor(node.status)} /></li>)}{nodeRuns.length === 0 && <B24Empty title="暂无节点投影" description="选择运行并刷新后查看。" />}</ul></B24Section><B24Section title="最近事件"><ul className="b24-list">{scopedEvents.slice().reverse().map((event) => <li key={`${event.resourceScope}:${event.id}`}><span>{event.eventType}</span><code>{event.cursor?.toString() || '—'}</code></li>)}{scopedEvents.length === 0 && <B24Empty title="暂无事件" description="启动事件监控后查看。" />}</ul></B24Section></div><div className="b24-grid b24-grid-2"><B24Section title="Writer Workspace / Lease"><ul className="b24-list">{writerWorkspaces.map((item) => <li key={item.writerWorkspaceId}><span>{item.writerKey}<small>{item.isolationKind} · {item.isolationRef}</small></span><B24Status label={item.lease === null ? '无 Lease' : item.lease.releasedAt ? '已释放' : `fence ${item.lease.fencing}`} tone={item.lease?.releasedAt ? 'neutral' : 'warning'} /></li>)}{!writerWorkspaces.length && <B24Empty title="暂无 Writer Projection" />}</ul></B24Section><B24Section title="Patch / Commit Artifact"><ul className="b24-list">{writerArtifacts.map((item) => <li key={item.writerArtifactId}><span>{item.artifactKind}<small>{item.changedPaths.join('、')}</small></span><B24Status label={`${item.testEvidenceRefs.length} 份测试证据`} /></li>)}{!writerArtifacts.length && <B24Empty title="暂无 Writer Artifact" />}</ul></B24Section><B24Section title="Conflict"><ul className="b24-list">{writerConflicts.map((item) => <li key={item.conflictId}><span>{item.paths.join('、')}</span><B24Status label={item.status} tone={toneFor(item.status)} /></li>)}{!writerConflicts.length && <B24Empty title="暂无 Conflict" />}</ul></B24Section><B24Section title="Merge Node"><ul className="b24-list">{mergeRuns.map((item) => <li key={item.mergeRunId}><span>{item.mergeNodeId}<small>{item.resultArtifactRef || item.errorCode || '等待结果'}</small></span><B24Status label={item.status} tone={toneFor(item.status)} /></li>)}{!mergeRuns.length && <B24Empty title="暂无 Merge Node" />}</ul></B24Section></div></B24Section>}

      {activeTab === 'home' && <div className="b24-layout-split"><div className="b24-layout-main"><B24Section title="Team 协作" description="从 Core 目录选择 Team 与成员。启动命令由 Core 创建 Graph 绑定、Session、Agent 和 Thread，页面不要求手填内部 ID。" actions={<div className="b24-actions"><button className="btn btn-secondary btn-sm" onClick={() => void loadDirectory()} disabled={busy || directoryLoading}><RefreshCw size={14} aria-hidden="true" />刷新目录</button><button className="btn btn-secondary btn-sm" onClick={() => void refreshTeam()} disabled={busy || !teamRunId}><RefreshCw size={14} aria-hidden="true" />刷新 Team</button></div>}><form className="b24-form" onSubmit={(event) => event.preventDefault()}><div className="b24-grid b24-grid-2"><label className="b24-field"><span className="b24-field-label">Team 模板<span className="b24-field-required">*</span></span><select value={teamSelection} onChange={(event) => setTeamSelection(event.target.value)}><option value="">选择 Core Team</option>{directory?.teams.map((team) => { const id = teamIdentity(team); return id ? <option key={id} value={id}>{teamLabel(team)}</option> : null; })}</select></label><label className="b24-field"><span className="b24-field-label">绑定 Graph Run</span><select value={graphRunId} onChange={(event) => selectGraphRun(event.target.value)}><option value="">选择已知 Graph Run</option>{knownGraphRuns.map((item) => <option key={item.id} value={item.id}>{item.id} · {statusLabel(item.status)}</option>)}</select></label></div><fieldset className="b24-fieldset b24-card"><legend>Roster 成员</legend>{selectedTeam ? <div className="b24-list b24-list-grid">{selectedTeam.members.map((member) => <label className="b24-check-row" key={member.member_id}><input type="checkbox" checked={memberIds.includes(member.member_id)} onChange={(event) => setMemberIds((current) => event.target.checked ? unique([...current, member.member_id]) : current.filter((id) => id !== member.member_id))} /><span><strong>{member.role}</strong><small>{member.agent_definition_id}{member.can_coordinate ? ' · 可协调' : ''}</small></span></label>)}</div> : <B24Empty title="请先选择 Team" description="Team 成员会在这里显示。" />}<p className="b24-field-help">当前 Team 服务端要求完整 Roster；成员身份由 Core 目录提供。</p></fieldset><label className="b24-field"><span className="b24-field-label">协作任务</span><textarea rows={2} value={teamTask} onChange={(event) => setTeamTask(event.target.value)} placeholder="描述本次 Team 要共同完成的任务。" /></label><div className="b24-actions"><button type="button" className="btn btn-primary" onClick={() => void startTeam()} disabled={busy || !connected || !teamId || !graphRunId || !canPrepareTeamForGraph(graphRun) || !selectedTeam || memberIds.length !== selectedTeam.members.length}><Play size={14} aria-hidden="true" />启动 Team Run</button>{teamRunId && <B24Status label={`当前 Run ${teamRunId}`} tone="success" />}</div></form>{teamRun && <div className="b24-card b24-card-subtle"><div className="b24-card-heading"><strong>{statusLabel(teamRun.status)}</strong><B24Status label={`${teamRun.roster.length} 名成员`} /></div><span>绑定 Graph Run：{teamRun.workflow_run_id}</span></div>}</B24Section>
      {teamRun && <><B24Section title="Agent 个人页" description="查看当前 Team Roster 中一个 Agent 的身份、状态和会话入口。"><label className="b24-field"><span className="b24-field-label">当前 Agent</span><select value={viewerAgentId} onChange={(event) => changeViewer(event.target.value)}><option value="">选择 Agent</option>{roster.map((item) => <option key={item.agent_instance_id} value={item.agent_instance_id}>{memberLabel(item, selectedTeam)} · {statusLabel(item.status || 'invited')}</option>)}</select></label>{selectedViewer ? <article className="b24-agent-profile"><div className="b24-agent-avatar" aria-hidden="true">{memberLabel(selectedViewer, selectedTeam).slice(0, 1)}</div><div className="b24-agent-profile-main"><h3>{memberLabel(selectedViewer, selectedTeam)}</h3><B24Status label={statusLabel(selectedViewer.status || 'invited')} tone={toneFor(selectedViewer.status || 'invited')} /><dl className="b24-detail-list"><div><dt>Agent Instance</dt><dd><code>{selectedViewer.agent_instance_id}</code></dd></div><div><dt>Thread</dt><dd>{selectedViewer.thread_id ? <Link to={`/chat/${encodeURIComponent(selectedViewer.thread_id)}`}><LinkIcon size={13} aria-hidden="true" />打开会话</Link> : 'Core 未返回 Thread'}</dd></div><div><dt>历史条目</dt><dd>{agentHistory?.items.length ?? '—'}</dd></div></dl></div></article> : <B24Empty title="请选择当前 Agent" description="消息和 Mailbox 会按当前 Agent 权限读取。" />}</B24Section><div className="b24-grid b24-grid-2"><B24Section title="消息与 Mailbox" description="群聊和定向消息沿用服务端可见性；消息正文作为不可信上下文显示。"><div className="b24-form"><div className="b24-grid b24-grid-2"><label className="b24-field"><span className="b24-field-label">消息范围</span><select value={messageAudience} onChange={(event) => setMessageAudience(event.target.value as Audience)}><option value="direct">定向给一名 Agent</option><option value="team">发送到 Team</option></select></label><label className="b24-field"><span className="b24-field-label">消息类型</span><select value={messageKind} onChange={(event) => setMessageKind(event.target.value as (typeof MESSAGE_KINDS)[number])}>{MESSAGE_KINDS.map((kind) => <option key={kind} value={kind}>{kind}</option>)}</select></label></div>{messageAudience === 'direct' && <label className="b24-field"><span className="b24-field-label">接收 Agent</span><select value={recipientAgentId} onChange={(event) => setRecipientAgentId(event.target.value)}><option value="">选择接收 Agent</option>{roster.map((item) => <option key={item.agent_instance_id} value={item.agent_instance_id}>{memberLabel(item, selectedTeam)}</option>)}</select></label>}<label className="b24-field"><span className="b24-field-label">消息内容</span><textarea rows={3} value={outgoing} onChange={(event) => setOutgoing(event.target.value)} placeholder="发送持久消息…" /></label><div className="b24-actions"><button type="button" className="btn btn-primary" onClick={() => void sendMessage()} disabled={busy || !viewerAgentId || !messageRecipientsActive}><Send size={14} aria-hidden="true" />发送持久消息</button>{!messageRecipientsActive && <p className="b24-helper-text">请选择仍在协作中的接收成员。</p>}</div></div><ul className="b24-list b24-message-list">{messages.map((item) => <li className="b24-message" key={item.message_id}><div className="b24-message-meta"><strong>{item.message_kind}</strong><span>{item.sender_id === viewerAgentId ? '我' : item.sender_id}</span><B24Status label={item.audience === 'team' ? 'Team' : '定向'} /></div><p>{payloadText(item)}</p></li>)}{!messages.length && <B24Empty title="暂无可见消息" description="Core 会按当前 Agent 的可见性投影消息。" />}</ul><h3>Mailbox</h3><ul className="b24-list">{mailbox.map((item) => <li key={item.delivery_id}><span><strong>{item.message.message_kind}</strong><small>{payloadText(item.message)}</small></span><div className="b24-actions"><B24Status label={statusLabel(item.status)} tone={toneFor(item.status)} />{item.status !== 'acked' && <button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void acknowledge(item.delivery_id)}>确认</button>}</div></li>)}{!mailbox.length && <B24Empty title="Mailbox 为空" description="暂无发给当前 Agent 的待确认消息。" />}</ul></B24Section><B24Section title="Task Board" description="状态更新带上 Core revision，冲突会显式失败并要求刷新。"><ul className="b24-list">{tasks.map((item) => <li className="b24-board-item" key={item.task_id}><div><strong>{item.title}</strong><small>{item.summary || '无摘要'}{item.assignee_ids.length ? ` · ${item.assignee_ids.length} 名负责人` : ''}</small></div><div className="b24-actions"><select aria-label={`${item.title} 状态`} value={item.status} onChange={(event) => void updateTask(item, event.target.value as TaskStatus)} disabled={busy}>{TASK_STATUSES.map((status) => <option key={status} value={status}>{statusLabel(status)}</option>)}</select><B24Status label={`rev ${item.revision}`} /></div></li>)}{!tasks.length && <B24Empty title="暂无任务" description="Task 由 Core Projection 或协作消息产生。" />}</ul></B24Section></div><B24Section title="Artifact Board" description="页面只操作 Core 已返回的 Artifact 引用；文件内容和权限由服务端校验。"><div className="b24-form-row"><label className="b24-field"><span className="b24-field-label">已有 Artifact 引用</span><select value={artifactToPublish} onChange={(event) => { setArtifactToPublish(event.target.value); const candidate = artifactCandidates.find((item) => item.artifactId === event.target.value); if (candidate) setArtifactTitle(candidate.title); }}><option value="">选择 Artifact</option>{artifactCandidates.map((item) => <option key={item.artifactId} value={item.artifactId}>{item.title} · rev {item.revision}</option>)}</select></label><label className="b24-field"><span className="b24-field-label">发布标题</span><input value={artifactTitle} onChange={(event) => setArtifactTitle(event.target.value)} placeholder="Artifact 在板上的标题" /></label><button type="button" className="btn btn-secondary btn-sm" onClick={() => void publishArtifact()} disabled={busy || !artifactToPublish || !viewerAgentId}><Send size={13} aria-hidden="true" />发布引用</button></div><ul className="b24-list">{artifacts.map((item) => <li key={item.artifact_id}><span><strong>{item.title}</strong><small>{item.media_type} · publisher {item.publisher_id} · revision {item.revision}</small></span><B24Status label={item.visibility} /></li>)}{!artifacts.length && <B24Empty title="暂无 Artifact" description="Core 尚未返回当前 Agent 可见的产物。" />}</ul></B24Section></>}{!teamRun && <B24Empty title="尚未加载 Team Run" description="选择 Team、绑定已知 Graph Run 后启动协作。" />}</div><aside className="b24-layout-aside"><B24Section title="Team 目录" description="Team 定义是版本化协作契约.">{directory?.teams.map((team) => <article className={`b24-card b24-card-subtle${teamIdentity(team) === teamSelection ? ' b24-card-selected' : ''}`} key={teamIdentity(team) || team.default_coordinator}><div className="b24-card-heading"><strong>{teamLabel(team)}</strong><B24Status label={`${team.members.length} 名成员`} /></div><p>{team.members.map((member) => member.role).join('、')}</p></article>)}{(!directory || !directory.teams.length) && <B24Empty title="Core 尚未返回 Team" description="请先创建可用 Team 定义。" />}</B24Section></aside></div>}
    </div></div>
  );
};
