import { B24ContextInspector } from "./B24ContextInspector";
import { LiveWorkbenchPanel } from './LiveWorkbenchPanel';
import { LiveFilePreview } from './LiveFilePreview';
import { LiveTerminalPanel, TERMINAL_CLEANUP_EVENT, readTerminalCleanupUnknown } from './LiveTerminalPanel';
import { workbenchClient } from '../../live/workbenchClient';
import type { WorkbenchCommandRegistry, WorkbenchReference } from '../../live/workbenchClient';
import './b2-chat-layout.css';
import './ui-refine-chat.css';
import { chatEmptyPresentation, historyItemLabel } from './chatPresentation';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useOutletContext, useParams } from 'react-router-dom';
import {
  AlertTriangle,
  ChevronRight,
  Folder,
  Loader2,
  MessageSquare,
  PanelLeftOpen,
  RefreshCw,
  SendHorizontal,
  ShieldCheck,
  Square,
  Terminal,
  Wifi,
  X,
} from 'lucide-react';
import { EmptyState } from '../../components/EmptyState';
import { StatusBadge } from '../../components/StatusBadge';
import type * as B2 from '../../../../../sdk/typescript-client/b2.generated';
import { useOperant } from '../../context/ClientContext';
import { approvalId, outcomeNeedsReconciliation, requestCode, requestError, requiredText } from '../extensions/localProjection';
import { parseExtensionArguments } from './extensionCommandArguments';
import { useLive, liveThreadTitle } from '../../live/LiveContext';
import type { LiveApproval, LiveEvent, LiveSessionOption } from '../../live/liveState';
import { approvalsForSession, canDecideApproval, formatCursor } from '../../live/liveState';
import type { RailOutletContext } from '../../app/RailLayout';

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    idle: '空闲',
    connecting: '连接中',
    connected: '已连接',
    replaying: '回放中',
    error: '连接错误',
    active: '活跃',
    waiting_approval: '等待审批',
    paused: '已暂停',
    completed: '已完成',
    failed: '已失败',
    cancelled: '已取消',
    pending: '待处理',
    approved_once: '已批准',
    approved_for_run: '本次运行已批准',
    rejected: '已拒绝',
    expired: '已过期',
  };
  return labels[status] ?? status;
}

const REVIEW_TOOLS = ['read_file', 'search_files', 'git_diff'];
type ReferenceKind = 'file' | 'thread' | 'artifact';
type ArtifactChoice = { id: string; source: string; summary: string; media_type: string; content_hash: string; size_bytes: number };
type ExtensionCommand = { kind: 'extension' | 'skill'; command: string; pluginId: string; version: string; description: string; parameters?: Record<string, unknown> };

function isReadOnlyReviewer(role: B2.RolePreset): boolean {
  const policy = role.tool_policy;
  const allowed = policy?.allowed_tools ?? [];
  return role.status !== 'inactive'
    && Boolean(role.id)
    && allowed.length === REVIEW_TOOLS.length
    && REVIEW_TOOLS.every((tool) => allowed.includes(tool))
    && !policy?.workspace_write
    && !policy?.command_execution
    && (policy?.approval_required?.length ?? 0) === 0;
}

function eventSummary(event: LiveEvent): string {
  const payload = event.payload;
  if (event.event_type === 'model.delta' && typeof payload === 'object' && payload !== null && 'delta_text' in payload) {
    const delta = (payload as { delta_text?: unknown }).delta_text;
    return typeof delta === 'string' ? delta : '模型增量已提交';
  }
  if (event.event_type.startsWith('approval') || event.event_type.includes('approval')) {
    return 'Approval Projection 已更新';
  }
  if (event.event_type.startsWith('tool.')) return 'Action Gateway Projection 已更新';
  if (event.event_type === 'agent.failed') {
    const message = event.payload.message;
    return typeof message === 'string' && message.trim() ? `Agent 失败：${message}` : 'Agent 运行失败';
  }
  if (event.event_type === 'agent.cancelled') {
    const reason = event.payload.reason;
    return typeof reason === 'string' && reason.trim() ? `Agent 已取消：${reason}` : 'Agent 已取消';
  }
  if (event.event_type === 'agent.timed_out') return 'Agent 运行超时';
  if (event.event_type === 'budget.exhausted') {
    const reason = event.payload.reason;
    return typeof reason === 'string' && reason.trim() ? `运行预算已耗尽：${reason}` : '运行预算已耗尽';
  }
  if (event.event_type === 'agent.no_progress') return 'Agent 因连续无进展而停止';
  if (event.event_type === 'agent.max_turns') return 'Agent 已达到最大轮次';
  if (event.event_type === 'session.run_failed') return 'Session 运行失败';
  if (event.event_type.startsWith('agent.')) return 'Agent Projection 已更新';
  return '收到已提交事件';
}

const LiveErrorBanner: React.FC<{
  code: string;
  message: string;
  recovery?: string;
  onRetry?: () => void;
  onClear?: () => void;
}> = ({ code, message, recovery, onRetry, onClear }) => (
  <div className="live-alert live-alert-error" role="alert">
    <AlertTriangle size={17} aria-hidden="true" />
    <div className="live-alert-content">
      <strong>{code}</strong>
      <span>{message}</span>
      {recovery && <span className="live-alert-recovery">{recovery}</span>}
    </div>
    <div className="live-alert-actions">
      {onRetry && (
        <button type="button" className="btn btn-secondary btn-sm" onClick={onRetry}>
          <RefreshCw size={13} aria-hidden="true" />
          重试
        </button>
      )}
      {onClear && (
        <button type="button" className="btn btn-ghost btn-icon" onClick={onClear} aria-label="关闭错误提示" title="关闭错误提示">
          <X size={15} aria-hidden="true" />
        </button>
      )}
    </div>
  </div>
);

export const LiveApprovalCard: React.FC<{
  approval: LiveApproval;
  busy: boolean;
  onDecide: (decision: 'approve' | 'reject') => void;
}> = ({ approval, busy, onDecide }) => (
  <article className="live-approval-card">
    <div className="live-approval-head">
      <span className="live-approval-icon" aria-hidden="true"><ShieldCheck size={16} /></span>
      <div>
        <h3>{approval.category}</h3>
        <p>{approval.detail || 'Core 未提供动作详情'}</p>
      </div>
      <StatusBadge status={approval.status} size="sm" />
    </div>
    <dl className="live-approval-meta">
      <div><dt>Approval ID</dt><dd>{approval.id}</dd></div>
      <div><dt>Tool Call</dt><dd>{approval.toolCallId}</dd></div>
      <div><dt>过期时间</dt><dd>{approval.expiresAt}</dd></div>
    </dl>
    <div className="live-approval-actions">
      <button type="button" className="btn btn-secondary btn-sm" onClick={() => onDecide('reject')} disabled={busy || approval.status !== 'pending'}>
        拒绝
      </button>
      <button type="button" className="btn btn-primary btn-sm" onClick={() => onDecide('approve')} disabled={busy || approval.status !== 'pending'}>
        {busy ? '暂不可操作' : '批准一次'}
      </button>
    </div>
  </article>
);

const LiveEventTimeline: React.FC<{ events: LiveEvent[]; cursor: LiveEvent['sequence']; status: string }> = ({ events, cursor, status }) => (
  <section className="live-panel live-events-panel" aria-labelledby="live-events-title">
    <div className="live-panel-heading">
      <div>
        <h2 id="live-events-title">SSE 回放与连接</h2>
        <p>已提交 Cursor：{formatCursor(cursor)} · {statusLabel(status)}</p>
      </div>
      <Wifi size={16} aria-hidden="true" className={status === 'connected' ? 'live-icon-ok' : undefined} />
    </div>
    {events.length === 0 ? (
      <p className="live-panel-empty">尚未收到该 Thread 的已提交事件。</p>
    ) : (
      <ol className="live-event-list" aria-live="polite">
        {events.slice(-12).map((event) => (
          <li key={`${event.id}:${event.sequence}`}>
            <span className="live-event-sequence">#{event.sequence}</span>
            <span className="live-event-type">{event.event_type}</span>
            <span className="live-event-summary">{eventSummary(event)}</span>
          </li>
        ))}
      </ol>
    )}
  </section>
);

function sessionLabel(option: LiveSessionOption): string {
  if (option.details) {
    const role = option.details.role_snapshot?.role_name || 'Session';
    return `${role} · ${option.id.slice(0, 12)}`;
  }
  return `服务端 Session ${option.id.slice(0, 12)} · 详情未查询`;
}

function safeJson(value: unknown): string {
  try {
    return JSON.stringify(value, (_key, nested) => (
      typeof nested === 'bigint' ? nested.toString() : nested
    ), 2);
  } catch {
    return '[无法显示该 canonical payload]';
  }
}

/** Render canonical Item.payload fields without turning them into synthetic messages. */
export const CanonicalHistoryItem: React.FC<{ item: B2.Item; index: number }> = ({ item, index }) => {
  const payload = item.payload;
  const type = payload.type || 'canonical_payload';
  let body: React.ReactNode;
  if (payload.type === 'user_message' || payload.type === 'agent_message') {
    body = <p>{payload.text}</p>;
  } else if (payload.type === 'tool_call') {
    body = (
      <dl className="b2-history-payload">
        <div><dt>工具</dt><dd>{payload.tool_name}</dd></div>
        <div><dt>Tool Call</dt><dd>{payload.tool_call_id}</dd></div>
        {payload.detail_summary && <div><dt>详情</dt><dd>{payload.detail_summary}</dd></div>}
        {payload.action_hash && <div><dt>Action Hash</dt><dd>{payload.action_hash}</dd></div>}
      </dl>
    );
  } else if (payload.type === 'tool_result_ref') {
    body = (
      <dl className="b2-history-payload">
        <div><dt>结果</dt><dd>{payload.outcome}</dd></div>
        <div><dt>Artifact</dt><dd>{payload.artifact_id}</dd></div>
        <div><dt>Tool Call</dt><dd>{payload.tool_call_id}</dd></div>
        {payload.summary && <div><dt>摘要</dt><dd>{payload.summary}</dd></div>}
      </dl>
    );
  } else if (payload.type === 'artifact_ref') {
    body = <p>Artifact：{payload.artifact_id}{payload.label ? ` · ${payload.label}` : ''}</p>;
  } else if (payload.type === 'approval_link') {
    body = <p>Approval：{payload.approval_id}</p>;
  } else if (payload.type === 'steering') {
    body = <p>{payload.text} · mode={payload.mode || 'steer'}</p>;
  } else if (payload.type === 'system_event') {
    body = <p>{payload.summary} · event={payload.event_type}{payload.source_ref ? ` · source=${payload.source_ref}` : ''}</p>;
  } else {
    body = <pre>{safeJson(payload)}</pre>;
  }
  const itemKey = item.id || `${item.thread_id}:${item.turn_id}:${item.position ?? index}`;
  if (type === 'system_event') {
    return <details className="ui-chat-system-event" data-payload-type={type}>
      <summary>运行记录 <span>{item.cursor == null ? 'canonical' : `Cursor ${String(item.cursor)}`}</span></summary>
      {body}
    </details>;
  }
  return (
    <article className="live-message b2-history-item" data-payload-type={type} key={itemKey}>
      <div className="live-message-meta">
        <strong>{historyItemLabel(type)}</strong>
        <span>{item.cursor === null || item.cursor === undefined ? 'canonical' : `Cursor ${String(item.cursor)}`}</span>
      </div>
      {body}
    </article>
  );
};

export const LiveChatView: React.FC = () => {
  const { conversationId } = useParams<{ conversationId: string }>();
  const navigate = useNavigate();
  const { clientMode, connectionStatus, phase56Client, phase45Client } = useOperant();
  const { showSidebarOpenBtn, openSidebar, isMobile } = useOutletContext<RailOutletContext>();
  const {
    phase,
    projects,
    threads,
    sessionOptions,
    approvals,
    selectedProjectId,
    selectedThreadId,
    selectedSessionId,
    selectedThread,
    selectedSession,
    roles,
    history,
    historyLoading,
    historyError,
    refreshHistory,
    loadMoreHistory,
    files,
    stream,
    projectionStale,
    lastError,
    command,
    approvalAction,
    manualReconcileRequired,
    manualReconcileReason,
    deepLinkNotFound,
    canCreateThread,
    createThreadUnavailableReason,
    threadCreationStatus,
    createThread,
    canCreateSession,
    createSessionUnavailableReason,
    cancelCommandAvailable,
    selectProject,
    selectThread,
    selectSession,
    resolveDeepLink,
    refresh,
    reconnect,
    createSession,
    sendMessage,
    cancelSession,
    decideApproval,
    loadFiles,
    clearError,
  } = useLive();
  const [draft, setDraft] = useState('');
  const projectSelectRef = useRef<HTMLSelectElement>(null);
  const [showFiles, setShowFiles] = useState(false);
  const [showTerminal, setShowTerminal] = useState(false);
  const [terminalCleanupId, setTerminalCleanupId] = useState<string | null>(null);
  const [showEvents, setShowEvents] = useState(false);
  const [filePath, setFilePath] = useState('');
  const [filesLoading, setFilesLoading] = useState(false);
  const [creatingSession, setCreatingSession] = useState(false);
  const [selectedRoleId, setSelectedRoleId] = useState('');
  const [references, setReferences] = useState<WorkbenchReference[]>([]);
  const [referenceKind, setReferenceKind] = useState<ReferenceKind>('file');
  const [referenceTarget, setReferenceTarget] = useState('');
  const [referencePickerOpen, setReferencePickerOpen] = useState(false);
  const [referenceBusy, setReferenceBusy] = useState(false);
  const [referenceMaxTokens, setReferenceMaxTokens] = useState(1200);
  const [artifactChoices, setArtifactChoices] = useState<ArtifactChoice[]>([]);
  const [artifactCursor, setArtifactCursor] = useState<number | null>(null);
  const [artifactLoaded, setArtifactLoaded] = useState(false);
  const [artifactBusy, setArtifactBusy] = useState(false);
  const [artifactError, setArtifactError] = useState('');
  const [artifactReload, setArtifactReload] = useState(0);
  const artifactRequest = useRef(0);
  const [workbenchError, setWorkbenchError] = useState('');
  const [workbenchNotice, setWorkbenchNotice] = useState('');
  const [registry, setRegistry] = useState<WorkbenchCommandRegistry | null>(null);
  const [extensionCommands, setExtensionCommands] = useState<ExtensionCommand[]>([]);
  const [extensionArguments, setExtensionArguments] = useState('{}');
  const [extensionApproval, setExtensionApproval] = useState<{ approvalId: string; run: () => Promise<void> }>();
  const [extensionOutcomeUnknown, setExtensionOutcomeUnknown] = useState(false);
  const [commandBusy, setCommandBusy] = useState(false);
  const [reviewerRoleId, setReviewerRoleId] = useState('');
  const workbenchKeys = useRef(new Map<string, string>());
  const keyForWorkbenchAction = (identity: string) => {
    const prior = workbenchKeys.current.get(identity);
    if (prior) return prior;
    const key = crypto.randomUUID();
    workbenchKeys.current.set(identity, key);
    return key;
  };

  const selectedProject = projects.find((project) => project.id === selectedProjectId);
  useEffect(() => {
    if (!roles.some((role) => role.id === selectedRoleId && role.status !== 'inactive')) {
      setSelectedRoleId(roles.find((role) => role.status !== 'inactive')?.id || '');
    }
  }, [roles, selectedRoleId]);
  const visibleApprovals = useMemo(
    () => approvalsForSession(approvals, selectedThread?.sessionId ?? null),
    [approvals, selectedThread?.sessionId],
  );
  const busy = command.status === 'sending'
    || command.status === 'awaiting_projection'
    || manualReconcileRequired
    || connectionStatus !== 'connected'
    || stream.status !== 'connected';
  const canSend = Boolean(selectedThread?.sessionId && selectedThread.workspaceRef && draft.trim())
    && !busy
    && !extensionApproval
    && !extensionOutcomeUnknown
    && phase === 'ready';
  const workbenchConnected = phase === 'ready' && connectionStatus === 'connected' && !projectionStale;
  const pickerVisible = referencePickerOpen || draft.includes('@');
  const commandSuggestions = useMemo(() => draft.startsWith('/') && registry
    ? registry.commands.filter((item) => [item.canonical_name, ...(item.aliases ?? [])]
      .some((alias) => alias.toLowerCase().startsWith(draft.trim().split(/\s/, 1)[0].toLowerCase())))
    : [], [draft, registry]);
  const extensionSuggestions = useMemo(() => draft.startsWith('/')
    ? extensionCommands.filter((item) => item.command.toLowerCase().startsWith(draft.trim().split(/\s/, 1)[0].toLowerCase()))
    : [], [draft, extensionCommands]);
  const selectedExtensionCommand = extensionCommands.find((item) => item.command.toLowerCase() === draft.trim().toLowerCase());
  const reviewCommand = /^\/(review|审查)(\s|$)/i.test(draft.trim());
  const reviewerRoles = useMemo(() => roles.filter(isReadOnlyReviewer), [roles]);
  const effectiveReviewerRoleId = reviewerRoles.some((role) => role.id === reviewerRoleId)
    ? reviewerRoleId : reviewerRoles[0]?.id ?? '';

  useEffect(() => {
    setReferences([]);
    setReferencePickerOpen(false);
    setWorkbenchError('');
    setWorkbenchNotice('');
    setExtensionApproval(undefined);
    setShowTerminal(false);
    ++artifactRequest.current;
    setArtifactChoices([]);
    setArtifactCursor(null);
    setArtifactLoaded(false);
    setArtifactBusy(false);
    setArtifactError('');
  }, [selectedThreadId]);

  useEffect(() => {
    if (!pickerVisible || referenceKind !== 'artifact' || !selectedThreadId || !workbenchConnected || artifactLoaded) return;
    const request = ++artifactRequest.current;
    setArtifactBusy(true);
    setArtifactError('');
    void workbenchClient.listReferenceArtifacts(selectedThreadId).then((page) => {
      if (request !== artifactRequest.current) return;
      setArtifactChoices(page.items);
      setArtifactCursor(page.next_cursor ?? null);
      setArtifactLoaded(true);
    }).catch((error: unknown) => {
      if (request === artifactRequest.current) setArtifactError(error instanceof Error ? error.message : '可引用工件列表读取失败');
    }).finally(() => { if (request === artifactRequest.current) setArtifactBusy(false); });
  }, [pickerVisible, referenceKind, selectedThreadId, workbenchConnected, artifactLoaded, artifactReload]);

  const loadMoreArtifacts = async () => {
    if (!selectedThreadId || artifactCursor === null || artifactBusy) return;
    const request = ++artifactRequest.current;
    setArtifactBusy(true); setArtifactError('');
    try {
      const page = await workbenchClient.listReferenceArtifacts(selectedThreadId, artifactCursor);
      if (request === artifactRequest.current) {
        setArtifactChoices((current) => [...current, ...page.items.filter((item) => !current.some((existing) => existing.id === item.id))]);
        setArtifactCursor(page.next_cursor ?? null);
      }
    } catch (error) { if (request === artifactRequest.current) setArtifactError(error instanceof Error ? error.message : '后续工件读取失败'); }
    finally { if (request === artifactRequest.current) setArtifactBusy(false); }
  };

  useEffect(() => {
    setShowFiles(false);
    setFilePath('');
  }, [selectedProjectId]);

  useEffect(() => {
    const refreshCleanup = () => setTerminalCleanupId(selectedThreadId ? readTerminalCleanupUnknown(selectedThreadId) : null);
    refreshCleanup();
    window.addEventListener(TERMINAL_CLEANUP_EVENT, refreshCleanup);
    return () => window.removeEventListener(TERMINAL_CLEANUP_EVENT, refreshCleanup);
  }, [selectedThreadId]);

  useEffect(() => {
    if (pickerVisible && workbenchConnected) return;
    ++artifactRequest.current;
    setArtifactChoices([]);
    setArtifactCursor(null);
    setArtifactLoaded(false);
    setArtifactBusy(false);
    setArtifactError('');
  }, [pickerVisible, workbenchConnected]);

  useEffect(() => {
    if (!workbenchConnected || !selectedThreadId) { setRegistry(null); setExtensionCommands([]); return; }
    let active = true;
    setExtensionCommands([]);
    void workbenchClient.listCommands().then((value) => { if (active) setRegistry(value); })
      .catch((error: unknown) => { if (active) setWorkbenchError(error instanceof Error ? error.message : '命令列表读取失败'); });
    void phase56Client.listExtensionCommands().then((value) => {
      if (!active) return;
      const page = value as Record<string, unknown>;
      if (!Array.isArray(page.commands)) throw new Error('扩展命令列表格式无效。');
      const commands = page.commands.map((raw) => {
        const item = raw as Record<string, unknown>;
        return {
          kind: 'extension' as const,
          command: `/${requiredText(item.name, 'name')}`,
          pluginId: requiredText(item.plugin_id, 'plugin_id'),
          version: requiredText(item.plugin_version, 'plugin_version'),
          description: typeof item.description === 'string' ? item.description : '',
          parameters: item.parameters && typeof item.parameters === 'object' && !Array.isArray(item.parameters) ? item.parameters as Record<string, unknown> : undefined,
        };
      });
      setExtensionCommands((current) => [...current.filter((item) => item.kind !== 'extension'), ...commands]);
    }).catch((error: unknown) => { if (active) setWorkbenchError(`扩展命令读取失败：${requestError(error)}`); });
    void phase56Client.listSkillCommands(selectedThreadId).then((value) => {
      if (!active) return;
      setExtensionCommands((current) => [
        ...current.filter((item) => item.kind !== 'skill'),
        ...value.commands.map((item) => ({
          kind: 'skill' as const,
          command: `/${requiredText(item.command, 'command')}`,
          pluginId: requiredText(item.skill_id, 'skill_id'),
          version: '',
          description: item.description,
          parameters: item.parameters,
        })),
      ]);
    }).catch((error: unknown) => { if (active) setWorkbenchError(`Skill 命令读取失败：${requestError(error)}`); });
    return () => { active = false; };
  }, [selectedThreadId, workbenchConnected, phase56Client]);

  // A deep link is resolved against the server projection.  Unknown IDs stay
  // visible as an empty live state; they are never replaced with Demo data.
  React.useEffect(() => {
    if (clientMode !== 'live') return;
    resolveDeepLink(conversationId ?? null);
  }, [clientMode, conversationId, resolveDeepLink]);

  const loadLiveFiles = useCallback(async (path = '') => {
    if (!selectedProject) return;
    setFilesLoading(true);
    setFilePath(path);
    await loadFiles(selectedProject.id, path);
    setFilesLoading(false);
  }, [loadFiles, selectedProject]);

  const handleSubmit = async () => {
    if (!canSend || !selectedThread) return;
    const message = draft.trim();
    if (message.startsWith('/')) {
      const [extensionName] = message.split(/\s+/);
      const extension = extensionCommands.find((item) => item.command.toLowerCase() === extensionName.toLowerCase());
      if (extension) {
        if (message !== extensionName) { setWorkbenchError('命令参数请填写在下方 JSON 输入框，草稿已保留。'); return; }
        let args: Record<string, unknown>;
        try { args = parseExtensionArguments(extensionArguments, extension.parameters); }
        catch (error: unknown) { setWorkbenchError(requestError(error)); return; }
        const identity = `${extension.kind}:${selectedThread.id}:${extension.command}:${extensionArguments}`;
        const key = keyForWorkbenchAction(identity);
        const run = async () => {
          const result = extension.kind === 'skill'
            ? await phase56Client.executeSkillCommand(selectedThread.id, { command: extension.command.slice(1), arguments: { prompt: requiredText(args.prompt, 'prompt') }, idempotency_key: key }, { idempotencyKey: key })
            : await phase56Client.executeExtensionCommand(selectedThread.id, { command: extension.command.slice(1), arguments: args, idempotency_key: key }, { idempotencyKey: key });
          workbenchKeys.current.delete(identity);
          await refreshHistory();
          const skillFeedback = extension.kind === 'skill' && typeof result.result === 'string'
            ? `：${result.result.slice(0, 500)}（资源 ${'resource_id' in result ? result.resource_id : '未知'}）`
            : '；请查看 Core 历史及结果';
          if (result.status === 'failed') { setWorkbenchError(`${extension.command} 已失败${skillFeedback}`); return; }
          setWorkbenchNotice(extension.kind === 'skill'
            ? `${extension.command} 已完成${skillFeedback}`
            : `${extension.command} 已完成，结果已写入 Core 历史。`);
          setDraft(''); setExtensionArguments('{}');
        };
        setCommandBusy(true); setWorkbenchError(''); setWorkbenchNotice('');
        try { await run(); }
        catch (error: unknown) {
          const approval = approvalId(error);
          if (requestCode(error) === 'approval_required' && approval) { setExtensionApproval({ approvalId: approval, run }); setWorkbenchError('命令需要人工审批。'); }
          else {
            if (outcomeNeedsReconciliation(error)) setExtensionOutcomeUnknown(true);
            setWorkbenchError(`命令失败：${requestError(error)} 若结果未知，请人工核对后处理。`);
          }
        } finally { setCommandBusy(false); }
        return;
      }
      if (!registry) { setWorkbenchError('命令列表尚未加载，无法校验命令。'); return; }
      const [name, ...argumentParts] = message.split(/\s+/);
      const definition = registry.commands.find((item) => [item.canonical_name, ...(item.aliases ?? [])]
        .some((alias) => alias.toLowerCase() === name.toLowerCase()));
      if (!definition) { setWorkbenchError(`Core 未注册命令 ${name}。请从命令列表选择。`); return; }
      if (argumentParts.length > 0 && !['review.run', 'plan.generate'].includes(definition.command_kind)) {
        setWorkbenchError(`${definition.canonical_name} 不接受参数；草稿已保留。`);
        return;
      }
      if (reviewCommand && !effectiveReviewerRoleId) {
        setWorkbenchError('运行 /review 需要严格只读的 Reviewer 角色：仅允许 read_file、search_files、git_diff。请先在 Agent 设置中配置。');
        return;
      }
      setCommandBusy(true); setWorkbenchError(''); setWorkbenchNotice('');
      try {
        const identity = `command:${selectedThread.id}:${registry.registry_version}:${message}:${reviewCommand ? effectiveReviewerRoleId : ''}`;
        const result = await workbenchClient.executeCommand(selectedThread.id, {
          text: message, registry_version: registry.registry_version,
          ...(reviewCommand ? { reviewer_role_id: effectiveReviewerRoleId } : {}),
        }, keyForWorkbenchAction(identity));
        workbenchKeys.current.delete(identity);
        setWorkbenchNotice(`${result.command}：${result.message}`);
        setDraft('');
        await refreshHistory();
      } catch (error) { setWorkbenchError(error instanceof Error ? error.message : '命令执行失败'); }
      finally { setCommandBusy(false); }
      return;
    }
    const completed = await sendMessage(message, references.map((item) => item.reference));
    if (completed) { setDraft(''); setReferences([]); }
  };

  const addReference = async (target = referenceTarget, kind = referenceKind) => {
    if (!selectedThread || !workbenchConnected || !target.trim() || referenceBusy) return;
    setReferenceBusy(true); setWorkbenchError('');
    try {
      const identity = `reference:${selectedThread.id}:${kind}:${target.trim()}:${referenceMaxTokens}`;
      const value = await workbenchClient.createReference(selectedThread.id, { kind, target: target.trim(), max_tokens: referenceMaxTokens }, keyForWorkbenchAction(identity));
      workbenchKeys.current.delete(identity);
      setReferences((current) => [...current, value]);
      if (target === referenceTarget) setReferenceTarget('');
      setReferencePickerOpen(false);
      setWorkbenchNotice(`已附加 ${value.source} 的摘要快照，正文按需读取。`);
    } catch (error) { setWorkbenchError(error instanceof Error ? error.message : '引用创建失败'); }
    finally { setReferenceBusy(false); }
  };

  const handleCreateSession = async () => {
    const roleId = selectedRoleId || selectedSession?.role_snapshot.role_id;
    if (!canCreateSession || !roleId || !selectedThread) return;
    setCreatingSession(true);
    const session = await createSession({ roleId, threadId: selectedThread.id });
    setCreatingSession(false);
    if (session) {
      const projectedThread = threads.find((thread) => thread.sessionId === session.id);
      if (projectedThread) navigate(`/chat/${projectedThread.id}`);
    }
  };

  const emptyPresentation = chatEmptyPresentation({
    failed: phase === 'error' || connectionStatus === 'disconnected',
    deepLinkNotFound,
    hasProjects: projects.some((project) => project.readable),
    hasProject: Boolean(selectedProject),
  });
  const handleEmptyAction = () => {
    switch (emptyPresentation.action) {
      case 'projects': navigate('/projects'); break;
      case 'select-project': projectSelectRef.current?.focus(); break;
      case 'create-thread': if (canCreateThread) void createThread(); break;
      case 'reconnect': void reconnect(); break;
    }
  };

  const topError = lastError || stream.error || (command.status === 'awaiting_projection' ? command.error : undefined);
  const connectionMessage = phase === 'ready'
    ? manualReconcileRequired
      ? '需要人工核对，已阻止自动重试'
      : stream.status === 'replaying' ? 'Core 正在重建连接，Projection 待校正' : 'Core 已连接'
    : phase === 'connecting' ? '正在连接 Core 并协商 phase1e.v1…'
      : phase === 'error' ? 'Core 连接失败，实时数据未加载'
        : '等待 Core 连接';

  if (phase !== 'ready' && !selectedThread && !topError) {
    return (
      <div className="live-chat-view live-chat-empty">
        <div className="live-connection-state" role="status" aria-live="polite">
          <Loader2 size={22} className="animate-spin" aria-hidden="true" />
          <h1>{connectionMessage}</h1>
          <p>Live 模式只等待 Core Projection，不会显示演示会话。</p>
        </div>
      </div>
    );
  }

  return (
    <div className="live-chat-view ui-chat" data-client-mode="live">
      <header className="live-chat-header">
        <div className="live-header-leading">
          {showSidebarOpenBtn && (
            <button type="button" className="btn btn-secondary btn-icon" onClick={openSidebar} aria-label="打开 Core 侧栏" title="打开 Core 侧栏">
              {isMobile ? <PanelLeftOpen size={16} aria-hidden="true" /> : <PanelLeftOpen size={16} aria-hidden="true" />}
            </button>
          )}
          <div>
            <div className="live-kicker"><span className="live-kicker-dot" aria-hidden="true" />对话</div>
            <h1>{selectedThread ? (selectedThread.title && selectedThread.title !== selectedThread.id ? selectedThread.title : '项目会话') : '开始新任务'}</h1>
          </div>
        </div>
        <div className="live-header-actions">
          <StatusBadge
            status={phase === 'ready' ? (stream.status === 'replaying' ? 'pending' : 'connected') : phase === 'error' ? 'disconnected' : 'pending'}
            label={connectionMessage}
            size="sm"
            pulse={phase === 'connecting' || stream.status === 'replaying'}
          />
          <button type="button" className="btn btn-ghost btn-icon" onClick={() => void refresh()} aria-label="刷新 Core Projection" title="刷新 Core Projection" disabled={phase === 'connecting'}>
            <RefreshCw size={15} aria-hidden="true" />
          </button>
        </div>
      </header>

      {topError && (
        <LiveErrorBanner
          code={topError.code}
          message={topError.message}
          recovery={topError.recovery}
          onRetry={topError.retryable ? () => void reconnect() : undefined}
          onClear={clearError}
        />
      )}
      {workbenchError && <div className="live-alert live-alert-error" role="alert"><AlertTriangle size={16} aria-hidden="true" /><span>{workbenchError}</span></div>}
      {workbenchNotice && <div className="live-workbench-notice" role="status">{workbenchNotice}</div>}
      {terminalCleanupId && <div className="live-alert live-alert-error" role="alert"><AlertTriangle size={16} aria-hidden="true" /><span>终端 {terminalCleanupId} 的清理结果未确认；请人工核查。打开终端面板可回读 Core 状态。本提示不改变 Core 审计。</span></div>}

      {manualReconcileRequired && (
        <div className="live-alert live-alert-warn" role="alert">
          <AlertTriangle size={17} aria-hidden="true" />
          <div className="live-alert-content">
            <strong>需要人工核对</strong>
            <span>{manualReconcileReason || 'Core 返回了 manual_reconcile_required / outcome_unknown。GUI 不会自动重放或猜测运行终态。'}</span>
          </div>
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => void refresh()} disabled={phase !== 'ready'}>
            刷新并核对状态
          </button>
        </div>
      )}

      <div className="live-chat-toolbar ui-chat-project-bar">
        <label className="live-select-label">
          <span>工作项目</span>
          <select
            className="select"
            ref={projectSelectRef}
            value={selectedProjectId ?? ''}
            onChange={(event) => selectProject(event.target.value || null)}
            aria-label="选择 Core Project Workspace"
          >
            <option value="">选择项目</option>
            {projects.filter((project) => project.readable).map((project) => (
              <option key={project.id} value={project.id}>{project.name} · {project.workspaceRef}</option>
            ))}
          </select>
        </label>
        <button type="button" className="btn btn-secondary btn-sm" onClick={() => void createThread()}
          disabled={!canCreateThread} title={createThreadUnavailableReason}>
          <PlusIcon />{threadCreationStatus === 'sending' ? '创建中…' : '新建会话'}
        </button>
      </div>
      <details className="ui-chat-setup" open={Boolean(selectedThread && !selectedThread.sessionId)}>
        <summary>会话与运行设置<span>{selectedSession?.role_snapshot.role_name || history?.session.role_snapshot?.role_name || '选择会话与角色'}</span></summary>
        <div className="ui-chat-setup-fields">
        <label className="live-select-label">
          <span>当前会话</span>
          <select
            className="select"
            value={selectedThreadId ?? ''}
            onChange={(event) => {
              const id = event.target.value || null;
              if (selectThread(id) && id) navigate(`/chat/${id}`);
            }}
            aria-label="选择 Core Thread"
          >
            <option value="">选择已有会话</option>
            {threads.map((thread) => <option key={thread.id} value={thread.id}>{thread.title || thread.id}</option>)}
          </select>
        </label>
        <label className="live-select-label live-session-select">
          <span>绑定运行</span>
          <select
            className="select"
            value={selectedSessionId ?? ''}
            onChange={(event) => selectSession(event.target.value || null)}
            aria-label="选择 Core Session"
          >
            <option value="" disabled={Boolean(selectedThread?.sessionId)}>尚未绑定运行</option>
            {sessionOptions.map((option) => (
              <option key={option.id} value={option.id} disabled={option.boundThreadId === null}>
                {history?.session.id === option.id ? `${history.session.role_snapshot?.role_name || option.id} · ${option.id.slice(0, 12)}` : sessionLabel(option)}
              </option>
            ))}
          </select>
        </label>
        <label className="live-select-label">
          <span>角色预设</span>
          <select
            className="select"
            value={selectedRoleId}
            onChange={(event) => setSelectedRoleId(event.target.value)}
            aria-label="选择 Core RolePreset"
            disabled={roles.length === 0}
          >
            <option value="">选择角色</option>
            {roles.filter((role) => role.status !== 'inactive' && role.id).map((role) => (
              <option key={role.id} value={role.id}>{role.name} · {role.id}</option>
            ))}
          </select>
        </label>
        <button type="button" className="btn btn-secondary btn-sm" onClick={() => void handleCreateSession()} disabled={!canCreateSession || !selectedRoleId || creatingSession} title={createSessionUnavailableReason || '需要一个明确的 RolePreset'}>
          <PlusIcon />
          {creatingSession ? '正在准备…' : '使用此角色'}
        </button>
        </div>
        {roles.length === 0 && <p className="ui-chat-setup-note">还没有可用角色。<button type="button" className="ui-text-action" onClick={() => navigate('/agents')}>配置模型与角色</button></p>}
      </details>

      {projectionStale && (
        <div className="live-projection-note" role="status">
          <RefreshCw size={13} aria-hidden="true" /> 当前显示可能落后于 Core，等待 Query Projection 校正。
        </div>
      )}

      {!selectedThread ? (
        <div className="live-chat-empty-content">
          <EmptyState
            icon={MessageSquare}
            titleAs="h4"
            title={emptyPresentation.title}
            description={emptyPresentation.description}
            action={emptyPresentation.action === 'none' ? undefined : (
              <button type="button" className="btn btn-primary" onClick={handleEmptyAction}
                disabled={emptyPresentation.action === 'create-thread' && !canCreateThread}>
                {emptyPresentation.label}
              </button>
            )}
          />
        </div>
      ) : (
        <>


          {!selectedThread.sessionId && (
            <div className="live-alert live-alert-warn" role="alert">
              <AlertTriangle size={17} aria-hidden="true" />
              <div className="live-alert-content">
                <strong>选择角色以开始工作</strong>
                <span>当前会话尚未绑定运行。请在“会话与运行设置”中选择角色并确认；绑定完成前不能发送消息。</span>
              </div>
            </div>
          )}

          {visibleApprovals.length > 0 && (
            <section className="live-approvals-section" aria-labelledby="live-approvals-title">
              <div className="live-section-heading">
                <h2 id="live-approvals-title"><ShieldCheck size={16} aria-hidden="true" />待处理审批</h2>
                <span>{visibleApprovals.length} 项 · 等待你的决定</span>
              </div>
              <div className="live-approval-list">
                {visibleApprovals.map((approval) => (
                  <LiveApprovalCard
                    key={approval.id}
                    approval={approval}
                    busy={phase !== 'ready' || !canDecideApproval(
                      approval,
                      approvalAction,
                      connectionStatus,
                      stream.status,
                      manualReconcileRequired,
                    )}
                    onDecide={(decision) => void decideApproval(approval, decision)}
                  />
                ))}
              </div>
            </section>
          )}

          <div className="live-content-grid">
            <section className="live-messages-panel" aria-labelledby="live-messages-title">
              <div className="live-panel-heading">
                <div>
                  <h2 id="live-messages-title">对话记录</h2>
                  <p>任务内容与执行结果保存在当前会话中。</p>
                </div>
                <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                  <button type="button" className="btn btn-ghost btn-sm" onClick={() => void refreshHistory()} disabled={historyLoading || phase !== 'ready' || !selectedThread.sessionId} aria-label="刷新 Session history">
                    <RefreshCw size={13} aria-hidden="true" />刷新历史
                  </button>
                {selectedThread.sessionId && (
                  <button type="button" className="btn btn-secondary btn-sm" onClick={() => void cancelSession()} disabled={!cancelCommandAvailable || manualReconcileRequired || phase !== 'ready' || connectionStatus !== 'connected'} title="取消由 Core B2 Command 接收，按钮状态等待服务端 Projection 校正">
                    <Square size={12} aria-hidden="true" />取消运行
                  </button>
                )}
                </div>
              </div>

              <div className="live-message-list" aria-live="polite">
                {historyError && (
                  <div className="live-alert live-alert-error" role="alert">
                    <AlertTriangle size={15} aria-hidden="true" />
                    <span>{historyError.code} · {historyError.message}</span>
                  </div>
                )}
                {historyLoading ? (
                  <div className="live-panel-loading" role="status"><Loader2 size={15} className="animate-spin" />正在读取历史记录…</div>
                ) : history?.items.length ? (
                  history.items.map((item, index) => <CanonicalHistoryItem item={item} index={index} key={item.id || `${item.thread_id}:${item.turn_id}:${item.position ?? index}`} />)
                ) : (
                  <p className="live-panel-empty">还没有消息，在下方描述你想完成的工作。</p>
                )}
              </div>
              <div className="live-composer">
                <div className="live-composer-extras">
                  <button type="button" className="btn btn-ghost btn-sm live-reference-toggle" aria-expanded={pickerVisible} onClick={() => setReferencePickerOpen((current) => !current)} disabled={!workbenchConnected || !selectedThread.sessionId}>@ 添加引用</button>
                  {references.length > 0 && <ul className="live-reference-chips" aria-label="待发送引用">{references.map((item, index) => <li key={`${item.content_hash}:${index}`}><details><summary>{item.source} · {item.size_bytes.toLocaleString()} B · {item.content_hash.slice(0, 8)}{item.truncated ? ' · 摘要已截断' : ''}</summary><p>{item.summary}</p><small>SHA-256 {item.content_hash}</small></details><button type="button" aria-label={`移除引用 ${item.source}`} onClick={() => setReferences((current) => current.filter((_, position) => position !== index))}>×</button></li>)}</ul>}
                  {pickerVisible && <div className="live-reference-picker"><label>引用类型<select className="select" value={referenceKind} onChange={(event) => { setReferenceKind(event.target.value as ReferenceKind); setReferenceTarget(''); }} disabled={!workbenchConnected || referenceBusy}><option value="file">文件</option><option value="thread">会话摘要</option><option value="artifact">可引用工件</option></select></label>
                    {referenceKind === 'thread' ? <label>目标会话<select className="select" value={referenceTarget} onChange={(event) => setReferenceTarget(event.target.value)} disabled={!workbenchConnected || referenceBusy}><option value="">选择会话</option>{threads.filter((item) => item.id !== selectedThread.id && item.workspaceRef === selectedThread.workspaceRef).map((item) => <option key={item.id} value={item.id}>{item.title || item.id}</option>)}</select></label>
                      : referenceKind === 'artifact' ? <><label>当前会话可引用工件<select className="select" value={referenceTarget} onChange={(event) => setReferenceTarget(event.target.value)} disabled={!workbenchConnected || referenceBusy || artifactBusy}><option value="">选择 Core 已授权的工件</option>{artifactChoices.map((item) => <option key={item.id} value={item.id}>{item.source} · {item.id.slice(0, 12)}</option>)}</select></label>
                        {artifactBusy && <span role="status">正在读取可引用工件…</span>}{artifactError && <><span role="alert">{artifactError}</span><button type="button" className="btn btn-ghost btn-sm" onClick={() => { setArtifactLoaded(false); setArtifactChoices([]); setArtifactCursor(null); setArtifactReload((current) => current + 1); }} disabled={artifactBusy}>重试读取</button></>}{artifactCursor !== null && <button type="button" className="btn btn-ghost btn-sm" onClick={() => void loadMoreArtifacts()} disabled={artifactBusy}>加载更多工件</button>}
                        {artifactChoices.filter((item) => item.id === referenceTarget).map((item) => <p className="live-artifact-choice" key={item.id}>{item.summary} · {item.media_type} · {item.size_bytes.toLocaleString()} B · SHA-256 {item.content_hash.slice(0, 16)}…</p>)}</>
                        : <label>工作区相对路径<input className="input" value={referenceTarget} onChange={(event) => setReferenceTarget(event.target.value)} list="live-reference-files" placeholder="输入或选择文件路径" disabled={!workbenchConnected || referenceBusy} /><datalist id="live-reference-files">{files.filter((item) => item.kind === 'file').map((item) => <option key={item.path} value={item.path} />)}</datalist></label>}
                    <label>摘要 Token 上限<input className="input" type="number" min={128} max={4096} value={referenceMaxTokens} onChange={(event) => setReferenceMaxTokens(Number(event.target.value))} disabled={referenceBusy} /></label>
                    <button type="button" className="btn btn-secondary btn-sm" onClick={() => void addReference()} disabled={!workbenchConnected || !referenceTarget.trim() || referenceBusy || referenceMaxTokens < 128 || referenceMaxTokens > 4096}>附加摘要</button></div>}
                  {commandSuggestions.length > 0 && <div className="live-command-suggestions" role="listbox" aria-label="可用命令">{commandSuggestions.map((item) => <button type="button" role="option" aria-selected={draft.trim() === item.canonical_name} key={item.canonical_name} onClick={() => setDraft(item.canonical_name)}><span>{item.canonical_name}{item.aliases?.length ? ` · ${item.aliases.join('、')}` : ''}</span><small>{item.command_kind} · {item.execution_mode || 'json'} · Core</small></button>)}</div>}
                  {extensionSuggestions.length > 0 && <div className="live-command-suggestions" role="listbox" aria-label="可用扩展与 Skill 命令">{extensionSuggestions.map((item) => <button type="button" role="option" aria-selected={draft.trim() === item.command} key={item.command} onClick={() => setDraft(item.command)}><span>{item.command} · {item.description}</span><small>{item.kind === 'skill' ? `${item.pluginId} · 已授权 Skill` : `${item.pluginId} v${item.version} · 已授权扩展`}</small></button>)}</div>}
                  {selectedExtensionCommand && <label className="live-extension-arguments">{selectedExtensionCommand.kind === 'skill' ? 'Skill 命令参数' : '扩展命令参数'}（JSON 对象）<textarea className="input" value={extensionArguments} onChange={(event) => setExtensionArguments(event.target.value)} rows={3} aria-describedby="extension-argument-hint" /><small id="extension-argument-hint">{selectedExtensionCommand.parameters ? `参数 Schema：${JSON.stringify(selectedExtensionCommand.parameters)}` : '无参数 Schema。'} 输入值仅发送给 Core。</small></label>}
                  {extensionApproval && <div className="live-alert" role="group" aria-label="动态命令审批"><span>命令等待人工审批：<code>{extensionApproval.approvalId}</code></span><button type="button" className="btn btn-primary btn-sm" disabled={commandBusy || !workbenchConnected} onClick={() => { const pending = extensionApproval; setCommandBusy(true); void phase45Client.decidePhase45Approval(pending.approvalId, { approved: true }).then(() => { setExtensionApproval(undefined); return pending.run(); }).catch((error: unknown) => { if (outcomeNeedsReconciliation(error)) setExtensionOutcomeUnknown(true); setWorkbenchError(`审批或原命令失败：${requestError(error)}`); }).finally(() => setCommandBusy(false)); }}>允许并提交</button><button type="button" className="btn btn-secondary btn-sm" disabled={commandBusy || !workbenchConnected} onClick={() => { const pending = extensionApproval; setCommandBusy(true); void phase45Client.decidePhase45Approval(pending.approvalId, { approved: false }).then(() => { setExtensionApproval(undefined); setWorkbenchNotice('已拒绝命令。'); }).catch((error: unknown) => setWorkbenchError(`拒绝失败：${requestError(error)}`)).finally(() => setCommandBusy(false)); }}>拒绝</button></div>}
                  {extensionOutcomeUnknown && <div className="live-alert live-alert-error" role="alert">命令仍在进行或结果未知，请先在 Core 历史和审计中核对。<button type="button" className="btn btn-secondary btn-sm" onClick={() => setExtensionOutcomeUnknown(false)}>已人工核对</button></div>}
                  {reviewCommand && <label className="live-reviewer-role">审查角色<select className="select" value={effectiveReviewerRoleId} onChange={(event) => setReviewerRoleId(event.target.value)} disabled={commandBusy || reviewerRoles.length === 0}><option value="" disabled>选择严格只读角色</option>{reviewerRoles.map((role) => <option key={role.id} value={role.id}>{role.name} · {role.id}</option>)}</select>{reviewerRoles.length === 0 && <span>需要仅允许 read_file、search_files、git_diff 的角色。</span>}</label>}
                </div>
                <textarea
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                      event.preventDefault();
                      void handleSubmit();
                    }
                  }}
                  placeholder={manualReconcileRequired ? '需要人工核对，完成 Projection 校正后才能发送' : selectedThread?.sessionId ? '描述你想完成的工作，Enter 发送，Shift+Enter 换行' : '请先选择角色并绑定运行'}
                  aria-label="向 Core Session 发送消息"
                  disabled={!selectedThread?.sessionId || !selectedThread.workspaceRef || busy || commandBusy || phase !== 'ready'}
                  rows={2}
                />
                <button type="button" className="btn btn-primary btn-icon" onClick={() => void handleSubmit()} disabled={!canSend || commandBusy} aria-label={draft.startsWith('/') ? '执行命令' : '发送消息'} title={draft.startsWith('/') ? '执行命令' : '发送消息'}>
                  {command.status === 'sending' ? <Loader2 size={16} className="animate-spin" aria-hidden="true" /> : <SendHorizontal size={16} aria-hidden="true" />}
                </button>
              </div>
              <p className="live-composer-note">输入 / 可发现 Core 命令，输入 @ 可附加有版本摘要。文件引用保留创建时的快照；源文件变更后可重附加，用哈希区分版本。权限或快照失效时 Core 会拒绝，草稿与引用保留供核对。</p>
            {history && history.next_cursor !== null && <button type="button" className="btn btn-secondary" disabled={historyLoading} onClick={() => void loadMoreHistory()}>加载更多历史</button>}
              </section>

            {selectedThread.sessionId && <LiveWorkbenchPanel threadId={selectedThread.id} connected={workbenchConnected} onChanged={refresh} />}
            <details className="live-inspector-column ui-chat-inspector" onToggle={(event) => { if (!event.currentTarget.open) setShowTerminal(false); }}><summary>运行详情与文件</summary><div className="ui-chat-inspector-body" aria-label="Core 实时检查器">
          <details className="live-thread-summary ui-thread-details"><summary>会话信息 <StatusBadge status={selectedThread.status} label={statusLabel(selectedThread.status)} size="sm" /></summary><div className="ui-thread-detail-body" aria-label="Core Thread Projection">
            <div className="live-thread-summary-main">
              <span className="live-thread-icon" aria-hidden="true"><MessageSquare size={16} /></span>
              <div>
                <strong>{liveThreadTitle(selectedThread)}</strong>
                <span>{selectedThread.workspaceRef || '未绑定 Workspace'} · {selectedThread.id}</span>
              </div>
            </div>
            </div>
          </details>
              {selectedThread.sessionId && <B24ContextInspector sessionId={selectedThread.sessionId} busy={busy} />}
              <div className="live-inspector-actions">
                <button type="button" className={`btn btn-secondary btn-sm${showEvents ? ' active' : ''}`} onClick={() => setShowEvents((current) => !current)}>
                  <Wifi size={13} aria-hidden="true" />{showEvents ? '隐藏事件' : '运行事件'}
                </button>
                <button type="button" className={`btn btn-secondary btn-sm${showFiles ? ' active' : ''}`} onClick={() => {
                  const next = !showFiles;
                  setShowFiles(next);
                  if (next) void loadLiveFiles();
                }} disabled={!selectedProject} aria-expanded={showFiles}>
                  <Folder size={13} aria-hidden="true" />{showFiles ? '隐藏文件' : '浏览文件'}
                </button>
                <button type="button" className={`btn btn-secondary btn-sm${showTerminal ? ' active' : ''}`} onClick={() => setShowTerminal((current) => !current)} disabled={!selectedThread.sessionId} aria-expanded={showTerminal}>
                  <Terminal size={13} aria-hidden="true" />{showTerminal ? '隐藏终端' : '交互终端'}
                </button>
              </div>
              {showEvents && <LiveEventTimeline events={stream.events} cursor={stream.cursor} status={stream.status} />}
              {showFiles && selectedProject && (
                <LiveFilePreview
                  workspaceId={selectedProject.id}
                  files={files}
                  currentPath={filePath}
                  loading={filesLoading}
                  onLoad={(path) => void loadLiveFiles(path)}
                  onReference={(path) => void addReference(path, 'file')}
                  canReference={workbenchConnected && Boolean(selectedThread.sessionId) && !referenceBusy && referenceMaxTokens >= 128 && referenceMaxTokens <= 4096}
                />
              )}
              {showTerminal && selectedThread.sessionId && <LiveTerminalPanel key={selectedThread.id} threadId={selectedThread.id} connected={workbenchConnected} />}
              <section className="live-panel live-scope-panel">
                <div className="live-panel-heading"><h2>连接与协议</h2><ChevronRight size={15} aria-hidden="true" /></div>
                <p>当前使用本地 Core 的真实状态。会话、运行、审批和事件记录均由服务端提供，协议为 phase1e.v1 / B2。</p>
                <p className="live-not-connected">OAuth PKCE 由 Core 部署配置启用；PWA、TUI 与 Tauri 均复用生成 Client，远程连接仍以服务端验收状态为准。</p>
              </section>
            </div></details>
          </div>
        </>
      )}
    </div>
  );
};

const PlusIcon: React.FC = () => <span aria-hidden="true"><span className="live-plus-icon">+</span></span>;
