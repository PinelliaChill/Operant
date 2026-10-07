import { B24ContextInspector } from "./B24ContextInspector";
import { LiveWorkbenchPanel } from './LiveWorkbenchPanel';
import { LiveFilePreview } from './LiveFilePreview';
import { LiveTerminalPanel, TERMINAL_CLEANUP_EVENT, readTerminalCleanupUnknown } from './LiveTerminalPanel';
import { workbenchClient } from '../../live/workbenchClient';
import type { WorkbenchCommandRegistry, WorkbenchReference } from '../../live/workbenchClient';
import { bindPendingConversationSend, draftAfterAccepted, pendingSendMatchesSelection, shouldQueuePendingSend } from '../../live/pendingConversationSend';
import { deepLinkLookupDecision } from '../../live/deepLinkLookup';
import './b2-chat-layout.css';
import './ui-refine-chat.css';
import { historyItemLabel } from './chatPresentation';
import { systemEventLabel, toolActionLabel, toolResultLabel } from './historyPresentation';
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
import { StatusBadge } from '../../components/StatusBadge';
import { PathInput } from '../../components/PathInput';
import { SearchSelect } from '../../components/SearchSelect';
import type * as B2 from '../../../../../sdk/typescript-client/b2.generated';
import { useOperant } from '../../context/ClientContext';
import { approvalId, outcomeNeedsReconciliation, requestCode, requestError, requiredText } from '../extensions/localProjection';
import { parseExtensionArguments } from './extensionCommandArguments';
import { useLive, liveThreadTitle } from '../../live/LiveContext';
import { useOnboarding } from '../../live/OnboardingContext';
import type { LiveApproval, LiveEvent } from '../../live/liveState';
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
    return '审批状态已更新';
  }
  if (event.event_type.startsWith('tool.')) return '操作状态已更新';
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
  if (event.event_type.startsWith('agent.')) return '成员状态已更新';
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
}> = ({ approval, busy, onDecide }) => {
  const action = approval.detail.match(/(?:^|[\s=])(run_command|file_write|network_access|read_file|write_file)(?:\s|$)/)?.[1] || approval.category;
  const actionName: Record<string, string> = {
    run_command: '运行命令', file_write: '修改文件', write_file: '修改文件',
    read_file: '读取文件', network_access: '访问网络', security_policy: '执行受保护操作',
  };
  const statusName: Record<string, string> = { pending: '等待你的决定', approved_once: '已允许一次', approved_for_run: '本次运行已允许', rejected: '已拒绝', expired: '已过期' };
  return (
  <article className="live-approval-card">
    <div className="live-approval-head">
      <span className="live-approval-icon" aria-hidden="true"><ShieldCheck size={16} /></span>
      <div>
        <h3>助手请求{actionName[action] || '执行操作'}</h3>
        <p>{statusName[approval.status] || statusLabel(approval.status)}</p>
      </div>
      <StatusBadge status={approval.status} label={statusName[approval.status] || statusLabel(approval.status)} size="sm" />
    </div>
    <details className="live-approval-details"><summary>查看操作与审批详情</summary><p>{approval.detail || '服务未提供更多动作说明'}</p><dl className="live-approval-meta">
      <div><dt>审批编号</dt><dd><code>{approval.id}</code></dd></div>
      <div><dt>操作编号</dt><dd><code>{approval.toolCallId}</code></dd></div>
      <div><dt>到期时间</dt><dd>{approval.expiresAt}</dd></div>
    </dl></details>
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
};

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
  let summary: React.ReactNode;
  if (payload.type === 'user_message' || payload.type === 'agent_message') {
    summary = <p>{payload.text}</p>;
  } else if (payload.type === 'tool_call') {
    summary = <p>{toolActionLabel(payload.tool_name)}</p>;
  } else if (payload.type === 'tool_result_ref') {
    summary = <p>{toolResultLabel(payload.summary, payload.outcome)}</p>;
  } else if (payload.type === 'artifact_ref') {
    summary = <p>{payload.label || '已生成内容'}</p>;
  } else if (payload.type === 'approval_link') {
    summary = <p>等待你确认一项操作</p>;
  } else if (payload.type === 'steering') {
    summary = <p>{payload.text}</p>;
  } else if (payload.type === 'system_event') {
    summary = <p>{systemEventLabel(payload.event_type, payload.summary)}</p>;
  } else {
    summary = <p>收到一条运行记录</p>;
  }
  const details = <details className="live-history-details"><summary>查看记录详情</summary><dl>
    <div><dt>记录类型</dt><dd>{type}</dd></div>
    {item.cursor !== null && item.cursor !== undefined && <div><dt>进度位置</dt><dd>{String(item.cursor)}</dd></div>}
    <div><dt>原始记录</dt><dd><pre>{safeJson(payload)}</pre></dd></div>
  </dl></details>;
  const itemKey = item.id || `${item.thread_id}:${item.turn_id}:${item.position ?? index}`;
  if (type === 'system_event') {
    return <details className="ui-chat-system-event" data-payload-type={type} key={itemKey}>
      <summary>{payload.type === 'system_event' ? systemEventLabel(payload.event_type, payload.summary) : '运行记录'}</summary>
      {details}
    </details>;
  }
  return (
    <article className="live-message b2-history-item" data-payload-type={type} key={itemKey}>
      <div className="live-message-meta"><strong>{historyItemLabel(type)}</strong></div>
      {summary}
      {type !== 'user_message' && type !== 'agent_message' && details}
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
    approvals,
    selectedProjectId,
    selectedThreadId,
    selectedThread,
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
    cancelCommandAvailable,
    selectProject,
    resolveDeepLink,
    refresh,
    reconnect,
    sendMessage,
    cancelSession,
    decideApproval,
    loadFiles,
    clearError,
  } = useLive();
  const { setup, setupLoading, setupError, metadata, draft, setDraft, getDraftRevision, getRouteRevision, pendingSend, setPendingSend, bootstrap, initializeConversation, renameConversation, refreshMetadata, createBusy, createOutcomeUnknown, renameOutcomeUnknown, createError, recoveredConversationId, clearCreateUncertainty } = useOnboarding();
  const projectSelectRef = useRef<HTMLButtonElement>(null);
  const [showFiles, setShowFiles] = useState(false);
  const [showTerminal, setShowTerminal] = useState(false);
  const [workbenchMounted, setWorkbenchMounted] = useState(false);
  const [terminalCleanupId, setTerminalCleanupId] = useState<string | null>(null);
  const [showEvents, setShowEvents] = useState(false);
  const [filePath, setFilePath] = useState('');
  const [filesLoading, setFilesLoading] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [titleDraft, setTitleDraft] = useState('');
  const [renameBusy, setRenameBusy] = useState(false);
  const pendingSendInFlight = useRef(false);
  const deepLinkAttempted = useRef<string | null>(null);
  const deepLinkLookupEpoch = useRef(0);
  const [deepLinkLookup, setDeepLinkLookup] = useState<'idle' | 'loading' | 'failed'>('idle');
  const [references, setReferencesState] = useState<WorkbenchReference[]>([]);
  const referencesRevision = useRef(0);
  const setReferences = useCallback<React.Dispatch<React.SetStateAction<WorkbenchReference[]>>>((value) => {
    referencesRevision.current += 1;
    setReferencesState(value);
  }, []);
  const [referenceKind, setReferenceKind] = useState<ReferenceKind>('file');
  const [referenceTarget, setReferenceTarget] = useState('');
  const [referencePickerOpen, setReferencePickerOpen] = useState(false);
  const [referenceBusy, setReferenceBusy] = useState(false);
  const referenceRequest = useRef(0);
  const referenceScope = useRef('');
  referenceScope.current = `${selectedThreadId}:${selectedThread?.workspaceRef || ''}`;
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
    && !createBusy
    && !createOutcomeUnknown
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
    setReferenceTarget('');
    setReferencePickerOpen(false);
    setReferenceBusy(false);
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
    return () => { ++referenceRequest.current; };
  }, [selectedThreadId, selectedThread?.workspaceRef]);

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
    if (selectedThread?.sessionId) void phase56Client.listSkillCommands(selectedThreadId).then((value) => {
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
  }, [selectedThreadId, selectedThread?.sessionId, workbenchConnected, phase56Client]);

  const refreshMissingDeepLink = useCallback(async () => {
    const epoch = ++deepLinkLookupEpoch.current;
    const route = window.location.href;
    setDeepLinkLookup('loading');
    let refreshed = false;
    try { refreshed = await refresh(); } catch { refreshed = false; }
    if (epoch !== deepLinkLookupEpoch.current || window.location.href !== route) return;
    setDeepLinkLookup(refreshed ? 'idle' : 'failed');
  }, [refresh]);

  // An uncached deep link gets one formal projection read before showing a
  // not-found state. It never creates a replacement conversation.
  React.useEffect(() => {
    if (clientMode !== 'live') return;
    const target = conversationId ?? null;
    resolveDeepLink(target);
    const decision = deepLinkLookupDecision(target, threads.some((thread) => thread.id === target), connectionStatus === 'connected', deepLinkAttempted.current);
    if (decision === 'empty' || decision === 'found') {
      deepLinkAttempted.current = null;
      setDeepLinkLookup('idle');
      return;
    }
    if (decision === 'disconnected') {
      deepLinkAttempted.current = null;
      setDeepLinkLookup('failed');
      return;
    }
    if (decision === 'await_result' || !target) return;
    deepLinkAttempted.current = target;
    void refreshMissingDeepLink();
  }, [clientMode, connectionStatus, conversationId, refreshMissingDeepLink, resolveDeepLink, threads]);
  React.useEffect(() => () => { deepLinkLookupEpoch.current += 1; }, []);

  const retryDeepLink = () => {
    if (!conversationId) return;
    if (connectionStatus !== 'connected') { void reconnect(); return; }
    deepLinkAttempted.current = conversationId;
    void refreshMissingDeepLink();
  };

  const loadLiveFiles = useCallback(async (path = '') => {
    if (!selectedProject) return;
    setFilesLoading(true);
    setFilePath(path);
    await loadFiles(selectedProject.id, path);
    setFilesLoading(false);
  }, [loadFiles, selectedProject]);

  const handleSubmit = async () => {
    if (!selectedThread || !draft.trim() || pendingSendInFlight.current) return;
    if (pendingSend) setPendingSend(null);
    if (!selectedThread.sessionId) {
      const clickedText = draft.trim();
      const clickedReferences = references.map((item) => ({ ...item.reference }));
      const clickedReferencesRevision = referencesRevision.current;
      const clickedRevision = getDraftRevision();
      const clickedWorkspaceId = selectedProjectId || setup?.default_workspace_id || '';
      const clickedRouteVersion = getRouteRevision();
      const clickedLocation = window.location.href;
      if (!setup?.ready) {
        if (!setup?.default_model_profile_id) { navigate('/settings?section=models'); return; }
        if (!await bootstrap(setup.default_model_profile_id)) return;
      }
      const result = await initializeConversation({ thread_id: selectedThread.id });
      if (result && getRouteRevision() === clickedRouteVersion && window.location.href === clickedLocation) {
        if (clickedWorkspaceId && result.workspace_id !== clickedWorkspaceId) { setWorkbenchError('新对话的工作区与发送时选择的不一致。请打开对话核对后手动发送。'); return; }
        if (shouldQueuePendingSend(true, clickedText, clickedWorkspaceId)) setPendingSend(bindPendingConversationSend({ threadId: result.thread_id, workspaceId: clickedWorkspaceId, workspaceRef: selectedThread.workspaceRef || '', sourceConversationId: conversationId ?? null, text: clickedText, references: clickedReferences, referencesRevision: clickedReferencesRevision, draftRevision: clickedRevision }));
        navigate(`/chat/${encodeURIComponent(result.thread_id)}`);
      }
      return;
    }
    if (!canSend) return;
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
            : '；请查看会话记录确认结果';
          if (result.status === 'failed') { setWorkbenchError(`${extension.command} 已失败${skillFeedback}`); return; }
          setWorkbenchNotice(extension.kind === 'skill'
            ? `${extension.command} 已完成${skillFeedback}`
            : `${extension.command} 已完成，结果已写入会话记录。`);
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
      if (!definition) { setWorkbenchError(`没有找到命令 ${name}。请从命令列表选择。`); return; }
      if (argumentParts.length > 0 && !['review.run', 'plan.generate'].includes(definition.command_kind)) {
        setWorkbenchError(`${definition.canonical_name} 不接受参数；草稿已保留。`);
        return;
      }
      if (reviewCommand && !effectiveReviewerRoleId) {
        setWorkbenchError('运行 /review 需要严格只读的 Reviewer 角色：仅允许 read_file、search_files、git_diff。请先在模型与角色设置中配置。');
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
    const clickedRevision = getDraftRevision();
    const clickedReferences = references.map((item) => ({ ...item.reference }));
    const clickedReferencesRevision = referencesRevision.current;
    const completed = await sendMessage(message, clickedReferences);
    if (completed) {
      const currentRevision = getDraftRevision();
      setDraft((current) => draftAfterAccepted(current, currentRevision, { text: message, draftRevision: clickedRevision }));
      if (referencesRevision.current === clickedReferencesRevision) setReferences([]);
    }
  };

  const addReference = async (target = referenceTarget, kind = referenceKind) => {
    if (!selectedThread || !workbenchConnected || !target.trim() || referenceBusy) return;
    const request = ++referenceRequest.current;
    const scope = referenceScope.current;
    const isCurrent = () => request === referenceRequest.current && scope === referenceScope.current;
    setReferenceBusy(true); setWorkbenchError('');
    try {
      const identity = `reference:${selectedThread.id}:${kind}:${target.trim()}:${referenceMaxTokens}`;
      const value = await workbenchClient.createReference(selectedThread.id, { kind, target: target.trim(), max_tokens: referenceMaxTokens }, keyForWorkbenchAction(identity));
      workbenchKeys.current.delete(identity);
      if (!isCurrent()) return;
      setReferences((current) => [...current, value]);
      if (target === referenceTarget) setReferenceTarget('');
      setReferencePickerOpen(false);
      setWorkbenchNotice(`已附加 ${value.source} 的摘要快照，正文按需读取。`);
    } catch (error) { if (isCurrent()) setWorkbenchError(error instanceof Error ? error.message : '引用创建失败'); }
    finally { if (isCurrent()) setReferenceBusy(false); }
  };

  const beginConversation = async (sendClickedDraft = false) => {
    if (pendingSendInFlight.current) return;
    if (pendingSend) setPendingSend(null);
    const clickedText = draft.trim();
    const clickedReferences = references.map((item) => ({ ...item.reference }));
    const clickedReferencesRevision = referencesRevision.current;
    const clickedRevision = getDraftRevision();
    const clickedWorkspaceId = selectedProjectId || setup?.default_workspace_id || '';
    const clickedWorkspaceRef = selectedProject?.workspaceRef || '';
    const clickedRouteVersion = getRouteRevision();
    const clickedLocation = window.location.href;
    if (!setup?.ready) {
      if (!setup?.default_model_profile_id) { navigate('/settings?section=models'); return; }
      const ready = await bootstrap(setup.default_model_profile_id);
      if (!ready) { navigate(setup.missing_steps?.includes('skills') ? '/settings?section=tools' : '/settings?section=models'); return; }
    }
    const result = await initializeConversation(selectedProjectId ? { workspace_id: selectedProjectId } : {});
    if (result && getRouteRevision() === clickedRouteVersion && window.location.href === clickedLocation) {
      if (clickedWorkspaceId && result.workspace_id !== clickedWorkspaceId) { setWorkbenchError('新对话的工作区与选择的不一致。请刷新对话列表核对。'); return; }
      if (shouldQueuePendingSend(sendClickedDraft, clickedText, clickedWorkspaceId)) setPendingSend(bindPendingConversationSend({ threadId: result.thread_id, workspaceId: clickedWorkspaceId, workspaceRef: clickedWorkspaceRef, sourceConversationId: conversationId ?? null, text: clickedText, references: clickedReferences, referencesRevision: clickedReferencesRevision, draftRevision: clickedRevision }));
      navigate(`/chat/${encodeURIComponent(result.thread_id)}`);
    }
  };
  useEffect(() => {
    if (!pendingSend || !selectedThread?.sessionId || !pendingSendMatchesSelection(pendingSend, conversationId ?? null, selectedThread.id, selectedThread.workspaceRef)) return;
    if (selectedProjectId && pendingSend.workspaceId !== selectedProjectId) { setPendingSend(null); setWorkbenchError('工作区已变化，自动发送已取消。草稿仍在输入框中。'); return; }
    if (busy || phase !== 'ready' || pendingSendInFlight.current || extensionApproval || extensionOutcomeUnknown) return;
    pendingSendInFlight.current = true;
    const submitted = pendingSend;
    setPendingSend(null);
    void sendMessage(submitted.text, submitted.references).then((accepted) => {
      if (accepted) {
        const currentRevision = getDraftRevision();
        setDraft((current) => draftAfterAccepted(current, currentRevision, submitted));
        if (referencesRevision.current === submitted.referencesRevision) setReferences([]);
      }
    }).finally(() => { pendingSendInFlight.current = false; });
  }, [pendingSend, conversationId, selectedThread?.id, selectedThread?.sessionId, selectedThread?.workspaceRef, selectedProjectId, busy, phase, extensionApproval, extensionOutcomeUnknown, getDraftRevision, sendMessage, setDraft, setPendingSend]);

  const saveTitle = async () => {
    if (!selectedThread || !titleDraft.trim() || renameBusy) return;
    setRenameBusy(true);
    const saved = await renameConversation(selectedThread.id, titleDraft.trim());
    setRenameBusy(false);
    if (saved) setRenaming(false);
  };
  const refreshConversation = async () => { await refresh(); await refreshMetadata(); };

  const topError = lastError || stream.error || (command.status === 'awaiting_projection' ? command.error : undefined);
  const connectionMessage = phase === 'ready'
    ? manualReconcileRequired
      ? '需要人工核对，已阻止自动重试'
      : stream.status === 'replaying' ? '正在重新连接并同步状态' : '已连接'
    : phase === 'connecting' ? '正在连接…'
      : phase === 'error' ? '连接失败，实时信息未加载'
        : '等待连接';

  if (phase !== 'ready' && !selectedThread && !topError) {
    return (
      <div className="live-chat-view live-chat-empty">
        <div className="live-connection-state" role="status" aria-live="polite">
          <Loader2 size={22} className="animate-spin" aria-hidden="true" />
          <h1>{connectionMessage}</h1>
          <p>连接后会显示项目会话。</p>
        </div>
      </div>
    );
  }

  return (
    <div className="live-chat-view ui-chat" data-client-mode="live">
      <header className="live-chat-header">
        <div className="live-header-leading">
          {showSidebarOpenBtn && (
            <button type="button" className="btn btn-secondary btn-icon" onClick={openSidebar} aria-label="打开侧栏" title="打开侧栏">
              {isMobile ? <PanelLeftOpen size={16} aria-hidden="true" /> : <PanelLeftOpen size={16} aria-hidden="true" />}
            </button>
          )}
          <div>
            <div className="live-kicker"><span className="live-kicker-dot" aria-hidden="true" />对话</div>
            <h1>{selectedThread ? metadata[selectedThread.id]?.title || (selectedThread.title && selectedThread.title !== selectedThread.id ? selectedThread.title : '新对话') : '新对话'}</h1>
          </div>
        </div>
        <div className="live-header-actions">
          {selectedThread && <button type="button" className="btn btn-ghost btn-sm" onClick={() => { setTitleDraft(metadata[selectedThread.id]?.title || selectedThread.title || ''); setRenaming(true); }}>改名</button>}
          <StatusBadge
            status={phase === 'ready' ? (stream.status === 'replaying' ? 'pending' : 'connected') : phase === 'error' ? 'disconnected' : 'pending'}
            label={connectionMessage}
            size="sm"
            pulse={phase === 'connecting' || stream.status === 'replaying'}
          />
          <button type="button" className="btn btn-ghost btn-icon" onClick={() => void refreshConversation()} aria-label="刷新对话" title="刷新对话" disabled={phase === 'connecting'}>
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
      {createError && selectedThread && <div className="live-alert live-alert-error" role="alert">{createError}{(createOutcomeUnknown || renameOutcomeUnknown) && <button type="button" className="btn btn-secondary btn-sm" onClick={() => void clearCreateUncertainty()}>刷新并核对</button>}{recoveredConversationId && <button type="button" className="btn btn-primary btn-sm" onClick={() => navigate(`/chat/${encodeURIComponent(recoveredConversationId)}`)}>打开已创建对话</button>}</div>}
      {renaming && selectedThread && <div className="live-rename"><label>对话名称<input className="input" value={titleDraft} onChange={(event) => setTitleDraft(event.target.value)} maxLength={100} autoFocus onKeyDown={(event) => { if (event.key === 'Enter') void saveTitle(); if (event.key === 'Escape') setRenaming(false); }} /></label><button type="button" className="btn btn-primary btn-sm" disabled={renameBusy || !titleDraft.trim()} onClick={() => void saveTitle()}>保存名称</button><button type="button" className="btn btn-ghost btn-sm" onClick={() => setRenaming(false)}>取消</button></div>}
      {workbenchNotice && <div className="live-workbench-notice" role="status">{workbenchNotice}</div>}
      {terminalCleanupId && <div className="live-alert live-alert-error" role="alert"><AlertTriangle size={16} aria-hidden="true" /><span>终端 {terminalCleanupId} 的清理结果未确认；请人工核查。打开终端面板可刷新状态。本提示不改变审计记录。</span></div>}

      {manualReconcileRequired && (
        <div className="live-alert live-alert-warn" role="alert">
          <AlertTriangle size={17} aria-hidden="true" />
          <div className="live-alert-content">
            <strong>需要人工核对</strong>
            <span>{manualReconcileReason || '结果未知，请刷新并核对运行状态。系统不会自动重试，也不会猜测运行结果。'}</span>
          </div>
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => void refreshConversation()} disabled={phase !== 'ready'}>
            刷新并核对状态
          </button>
        </div>
      )}

      <div className="live-chat-toolbar ui-chat-project-bar">
        <SearchSelect label="工作项目" buttonRef={projectSelectRef} value={selectedProjectId ?? ''} onChange={(value) => selectProject(value || null)} placeholder="选择项目" options={projects.filter((project) => project.readable).map((project) => ({ value: project.id, label: project.name, detail: project.workspaceRef }))} />
        <button type="button" className="btn btn-secondary btn-sm" onClick={() => void beginConversation()}
          disabled={createBusy || createOutcomeUnknown || phase !== 'ready'}>
          <PlusIcon />{createBusy ? '创建中…' : '新建对话'}
        </button>
      </div>
      {selectedThread && <details className="ui-chat-setup"><summary>对话详情</summary><dl className="ui-thread-detail-body"><div><dt>对话编号</dt><dd><code>{selectedThread.id}</code></dd></div><div><dt>工作区</dt><dd>{selectedThread.workspaceRef}</dd></div><div><dt>助手</dt><dd>{history?.session.role_snapshot?.role_name || '通用助手'}</dd></div></dl></details>}

      {projectionStale && (
        <div className="live-projection-note" role="status">
          <RefreshCw size={13} aria-hidden="true" /> 当前状态可能尚未同步，请刷新确认。
        </div>
      )}

      {!selectedThread ? (
        <div className="live-chat-empty-content live-chat-welcome">
          <MessageSquare size={28} aria-hidden="true" />
          <h2>{conversationId ? deepLinkLookup === 'loading' ? '正在查找这段对话…' : deepLinkLookup === 'failed' ? '暂时无法确认这段对话' : deepLinkNotFound ? '找不到这段对话' : '正在打开对话…' : setup?.ready ? '想先做什么？' : '先连接一个模型'}</h2>
          <p>{conversationId ? deepLinkLookup === 'failed' ? '连接或列表读取失败。请刷新并查找，当前没有创建新对话。' : deepLinkLookup === 'loading' ? '正在从服务端读取最新对话列表。' : deepLinkNotFound ? '最新列表中暂未找到该对话。你可以再刷新一次核对。' : '正在同步对话内容。' : setupError || (setup?.ready ? '写下你的问题或任务，通用助手会开始处理。' : '连接模型后就能直接开始对话。')}</p>
          {conversationId && <button type="button" className="btn btn-secondary" onClick={retryDeepLink} disabled={deepLinkLookup === 'loading'}>{connectionStatus === 'connected' ? '刷新并查找' : '重新连接并查找'}</button>}
          {createError && <div className="live-alert live-alert-error" role="alert">{createError}</div>}
          {(createOutcomeUnknown || renameOutcomeUnknown) && <button type="button" className="btn btn-secondary" onClick={() => void clearCreateUncertainty()}>刷新并核对原请求</button>}
          {recoveredConversationId && <button type="button" className="btn btn-primary" onClick={() => navigate(`/chat/${encodeURIComponent(recoveredConversationId)}`)}>打开已创建对话</button>}
          {!conversationId && <><textarea className="textarea" value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="描述你想完成的工作…" aria-label="对话草稿" rows={3} /><button type="button" className="btn btn-primary" onClick={() => void beginConversation(true)} disabled={createBusy || createOutcomeUnknown || phase !== 'ready' || setupLoading}>{setup?.ready ? (createBusy ? '正在准备…' : '开始对话') : '连接模型'}</button></>}
        </div>
      ) : (
        <>


          {!selectedThread.sessionId && <div className="live-alert live-alert-warn" role="status">这段旧对话还未准备好助手。发送消息时会自动准备，草稿会保留。</div>}

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
                </div>
                <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                  <button type="button" className="btn btn-ghost btn-sm" onClick={() => { void refreshHistory(); void refreshMetadata(); }} disabled={historyLoading || phase !== 'ready' || !selectedThread.sessionId} aria-label="刷新对话记录">
                    <RefreshCw size={13} aria-hidden="true" />刷新历史
                  </button>
                {selectedThread.sessionId && (
                  <button type="button" className="btn btn-secondary btn-sm" onClick={() => void cancelSession()} disabled={!cancelCommandAvailable || manualReconcileRequired || phase !== 'ready' || connectionStatus !== 'connected'} title="取消后请刷新确认运行状态">
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
                  {pickerVisible && <div className="live-reference-picker"><SearchSelect label="引用类型" value={referenceKind} onChange={(value) => { setReferenceKind(value as ReferenceKind); setReferenceTarget(''); }} disabled={!workbenchConnected || referenceBusy} options={[{ value: 'file', label: '文件' }, { value: 'thread', label: '对话摘要' }, { value: 'artifact', label: '可引用内容' }]} />
                    {referenceKind === 'thread' ? <SearchSelect label="目标对话" value={referenceTarget} onChange={setReferenceTarget} placeholder="选择对话" disabled={!workbenchConnected || referenceBusy} options={threads.filter((item) => item.id !== selectedThread.id && item.workspaceRef === selectedThread.workspaceRef).map((item) => ({ value: item.id, label: metadata[item.id]?.title || item.title || '新对话', detail: item.id }))} />
                      : referenceKind === 'artifact' ? <><SearchSelect label="可引用内容" value={referenceTarget} onChange={setReferenceTarget} placeholder="选择内容" disabled={!workbenchConnected || referenceBusy || artifactBusy} options={artifactChoices.map((item) => ({ value: item.id, label: item.source, detail: item.summary }))} />
                        {artifactBusy && <span role="status">正在读取可引用工件…</span>}{artifactError && <><span role="alert">{artifactError}</span><button type="button" className="btn btn-ghost btn-sm" onClick={() => { setArtifactLoaded(false); setArtifactChoices([]); setArtifactCursor(null); setArtifactReload((current) => current + 1); }} disabled={artifactBusy}>重试读取</button></>}{artifactCursor !== null && <button type="button" className="btn btn-ghost btn-sm" onClick={() => void loadMoreArtifacts()} disabled={artifactBusy}>加载更多工件</button>}
                        {artifactChoices.filter((item) => item.id === referenceTarget).map((item) => <p className="live-artifact-choice" key={item.id}>{item.summary} · {item.media_type} · {item.size_bytes.toLocaleString()} B · SHA-256 {item.content_hash.slice(0, 16)}…</p>)}</>
                        : <><PathInput key={`${selectedThreadId}:${selectedThread?.workspaceRef}`} label="项目内文件" kind="file" relativeTo={selectedThread?.workspaceRef || ''} value={referenceTarget} onChange={setReferenceTarget} list="live-reference-files" placeholder="输入或选择文件路径" disabled={!workbenchConnected || referenceBusy || !selectedThread?.workspaceRef} /><datalist id="live-reference-files">{files.filter((item) => item.kind === 'file').map((item) => <option key={item.path} value={item.path} />)}</datalist></>}
                    <label>摘要 Token 上限<input className="input" type="number" min={128} max={4096} value={referenceMaxTokens} onChange={(event) => setReferenceMaxTokens(Number(event.target.value))} disabled={referenceBusy} /></label>
                    <button type="button" className="btn btn-secondary btn-sm" onClick={() => void addReference()} disabled={!workbenchConnected || !referenceTarget.trim() || referenceBusy || referenceMaxTokens < 128 || referenceMaxTokens > 4096}>附加摘要</button></div>}
                  {commandSuggestions.length > 0 && <div className="live-command-suggestions" role="listbox" aria-label="可用命令">{commandSuggestions.map((item) => <button type="button" role="option" aria-selected={draft.trim() === item.canonical_name} key={item.canonical_name} onClick={() => setDraft(item.canonical_name)}><span>{item.canonical_name}{item.aliases?.length ? ` · ${item.aliases.join('、')}` : ''}</span><small>{item.command_kind} · {item.execution_mode || 'json'} </small></button>)}</div>}
                  {extensionSuggestions.length > 0 && <div className="live-command-suggestions" role="listbox" aria-label="可用扩展与 Skill 命令">{extensionSuggestions.map((item) => <button type="button" role="option" aria-selected={draft.trim() === item.command} key={item.command} onClick={() => setDraft(item.command)}><span>{item.command} · {item.description}</span><small>{item.kind === 'skill' ? `${item.pluginId} · 已授权 Skill` : `${item.pluginId} v${item.version} · 已授权扩展`}</small></button>)}</div>}
                  {selectedExtensionCommand && <label className="live-extension-arguments">{selectedExtensionCommand.kind === 'skill' ? 'Skill 命令参数' : '扩展命令参数'}（JSON 对象）<textarea className="input" value={extensionArguments} onChange={(event) => setExtensionArguments(event.target.value)} rows={3} aria-describedby="extension-argument-hint" /><small id="extension-argument-hint">{selectedExtensionCommand.parameters ? `参数 Schema：${JSON.stringify(selectedExtensionCommand.parameters)}` : '无参数 Schema。'} 输入值仅用于执行当前命令。</small></label>}
                  {extensionApproval && <div className="live-alert" role="group" aria-label="动态命令审批"><span>命令等待人工审批：<code>{extensionApproval.approvalId}</code></span><button type="button" className="btn btn-primary btn-sm" disabled={commandBusy || !workbenchConnected} onClick={() => { const pending = extensionApproval; setCommandBusy(true); void phase45Client.decidePhase45Approval(pending.approvalId, { approved: true }).then(() => { setExtensionApproval(undefined); return pending.run(); }).catch((error: unknown) => { if (outcomeNeedsReconciliation(error)) setExtensionOutcomeUnknown(true); setWorkbenchError(`审批或原命令失败：${requestError(error)}`); }).finally(() => setCommandBusy(false)); }}>允许并提交</button><button type="button" className="btn btn-secondary btn-sm" disabled={commandBusy || !workbenchConnected} onClick={() => { const pending = extensionApproval; setCommandBusy(true); void phase45Client.decidePhase45Approval(pending.approvalId, { approved: false }).then(() => { setExtensionApproval(undefined); setWorkbenchNotice('已拒绝命令。'); }).catch((error: unknown) => setWorkbenchError(`拒绝失败：${requestError(error)}`)).finally(() => setCommandBusy(false)); }}>拒绝</button></div>}
                  {extensionOutcomeUnknown && <div className="live-alert live-alert-error" role="alert">命令仍在进行或结果未知，请先查看会话记录和审计；不要重复提交。<button type="button" className="btn btn-secondary btn-sm" onClick={() => setExtensionOutcomeUnknown(false)}>已人工核对</button></div>}
                  {reviewCommand && <div className="live-reviewer-role"><SearchSelect label="审查角色" value={effectiveReviewerRoleId} onChange={setReviewerRoleId} placeholder="选择只读角色" disabled={commandBusy || reviewerRoles.length === 0} options={reviewerRoles.map((role) => ({ value: role.id!, label: role.name, detail: role.id }))} />{reviewerRoles.length === 0 && <span>需要先配置只读角色。</span>}</div>}
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
                  placeholder={manualReconcileRequired ? '需要人工核对，确认状态后才能发送' : '描述你想完成的工作，Enter 发送，Shift+Enter 换行'}
                  aria-label="向会话发送消息"
                  disabled={!selectedThread.workspaceRef || (selectedThread.sessionId ? busy : connectionStatus !== 'connected') || commandBusy || phase !== 'ready' || createBusy || createOutcomeUnknown}
                  rows={2}
                />
                <button type="button" className="btn btn-primary btn-icon" onClick={() => void handleSubmit()} disabled={!(selectedThread.sessionId ? canSend : Boolean(draft.trim()) && !createBusy && !createOutcomeUnknown && phase === 'ready' && connectionStatus === 'connected') || commandBusy} aria-label={draft.startsWith('/') ? '执行命令' : '发送消息'} title={draft.startsWith('/') ? '执行命令' : '发送消息'}>
                  {command.status === 'sending' ? <Loader2 size={16} className="animate-spin" aria-hidden="true" /> : <SendHorizontal size={16} aria-hidden="true" />}
                </button>
              </div>
              <details className="live-composer-note"><summary>输入与引用说明</summary><p>输入 / 查看命令，输入 @ 添加引用。文件变更后请重新添加引用；引用失效时消息会被拒绝，草稿仍会保留。</p></details>
            {history && history.next_cursor !== null && <button type="button" className="btn btn-secondary" disabled={historyLoading} onClick={() => void loadMoreHistory()}>加载更多历史</button>}
              </section>

            {selectedThread.sessionId && <details className="live-workbench-entry" onToggle={(event) => { if (event.currentTarget.open) setWorkbenchMounted(true); }}><summary>协作与高级工具</summary>{workbenchMounted && <LiveWorkbenchPanel threadId={selectedThread.id} connected={workbenchConnected} onChanged={async () => { await refresh(); }} />}</details>}
            <details className="live-inspector-column ui-chat-inspector" onToggle={(event) => { if (!event.currentTarget.open) setShowTerminal(false); }}><summary>运行详情与文件</summary><div className="ui-chat-inspector-body" aria-label="运行详情">
          <details className="live-thread-summary ui-thread-details"><summary>会话信息 <StatusBadge status={selectedThread.status} label={statusLabel(selectedThread.status)} size="sm" /></summary><div className="ui-thread-detail-body" aria-label="会话信息">
            <div className="live-thread-summary-main">
              <span className="live-thread-icon" aria-hidden="true"><MessageSquare size={16} /></span>
              <div>
                <strong>{liveThreadTitle(selectedThread)}</strong>
                <span>{selectedThread.workspaceRef || '未选择工作区'} · {selectedThread.id}</span>
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
                <p>会话、运行和审批记录已从本地服务读取。</p>

              </section>
            </div></details>
          </div>
        </>
      )}
    </div>
  );
};

const PlusIcon: React.FC = () => <span aria-hidden="true"><span className="live-plus-icon">+</span></span>;
