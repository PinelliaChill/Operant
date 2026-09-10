import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate, useOutletContext, useParams } from 'react-router-dom';
import {
  AlertTriangle,
  ChevronRight,
  File,
  Folder,
  Loader2,
  MessageSquare,
  PanelLeftOpen,
  RefreshCw,
  SendHorizontal,
  ShieldCheck,
  Square,
  Wifi,
  X,
} from 'lucide-react';
import { EmptyState } from '../../components/EmptyState';
import { StatusBadge } from '../../components/StatusBadge';
import type * as B2 from '../../../../../sdk/typescript-client/b2.generated';
import { useOperant } from '../../context/ClientContext';
import { useLive, liveThreadTitle } from '../../live/LiveContext';
import type { LiveApproval, LiveEvent, LiveSessionOption, LiveWorkspaceFile } from '../../live/liveState';
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
        {busy ? '提交中…' : '批准一次'}
      </button>
    </div>
  </article>
);

const LiveFileBrowser: React.FC<{
  projectId: string;
  workspaceRef: string;
  files: LiveWorkspaceFile[];
  currentPath: string;
  loading: boolean;
  onLoad: (path: string) => void;
}> = ({ projectId, workspaceRef, files, currentPath, loading, onLoad }) => {
  const parentPath = currentPath.split('/').filter(Boolean).slice(0, -1).join('/');
  return (
    <section className="live-panel live-files-panel" aria-labelledby="live-files-title">
      <div className="live-panel-heading">
        <div>
          <h2 id="live-files-title">Workspace 文件</h2>
          <p>{workspaceRef} · 只读目录 metadata</p>
        </div>
        {currentPath && (
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => onLoad(parentPath)} disabled={loading}>
            返回上级
          </button>
        )}
      </div>
      <div className="live-file-path" aria-label="当前目录路径">
        <span>{currentPath ? `/${currentPath}` : '/'}</span>
        <span className="live-file-project-id">Workspace ID: {projectId}</span>
      </div>
      {loading ? (
        <div className="live-panel-loading" role="status"><Loader2 size={16} className="animate-spin" />正在读取 Core 文件投影…</div>
      ) : files.length === 0 ? (
        <p className="live-panel-empty">当前目录没有可读条目，或 Core 未返回目录内容。</p>
      ) : (
        <ul className="live-file-list">
          {files.map((file) => (
            <li key={file.path}>
              {file.kind === 'directory' ? <Folder size={15} aria-hidden="true" /> : <File size={15} aria-hidden="true" />}
              {file.kind === 'directory' ? (
                <button type="button" className="live-file-link" onClick={() => onLoad(file.path)}>{file.name}</button>
              ) : (
                <span className="live-file-name">{file.name}</span>
              )}
              <span className="live-file-kind">{file.kind === 'directory' ? '目录' : '文件'}</span>
              {file.size !== null && <span className="live-file-size">{formatCursor(file.size)} B</span>}
            </li>
          ))}
        </ul>
      )}
    </section>
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
  return (
    <article className="live-message b2-history-item" data-payload-type={type} key={itemKey}>
      <div className="live-message-meta">
        <strong>{type}</strong>
        <span>{item.cursor === null || item.cursor === undefined ? 'canonical' : `Cursor ${String(item.cursor)}`}</span>
      </div>
      {body}
    </article>
  );
};

export const LiveChatView: React.FC = () => {
  const { conversationId } = useParams<{ conversationId: string }>();
  const navigate = useNavigate();
  const { clientMode, connectionStatus } = useOperant();
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
  const [showFiles, setShowFiles] = useState(false);
  const [showEvents, setShowEvents] = useState(false);
  const [filePath, setFilePath] = useState('');
  const [filesLoading, setFilesLoading] = useState(false);
  const [creatingSession, setCreatingSession] = useState(false);
  const [selectedRoleId, setSelectedRoleId] = useState('');

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
    && phase === 'ready';

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
    if (!canSend) return;
    const message = draft.trim();
    setDraft('');
    await sendMessage(message);
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
    <div className="live-chat-view" data-client-mode="live">
      <header className="live-chat-header">
        <div className="live-header-leading">
          {showSidebarOpenBtn && (
            <button type="button" className="btn btn-secondary btn-icon" onClick={openSidebar} aria-label="打开 Core 侧栏" title="打开 Core 侧栏">
              {isMobile ? <PanelLeftOpen size={16} aria-hidden="true" /> : <PanelLeftOpen size={16} aria-hidden="true" />}
            </button>
          )}
          <div>
            <div className="live-kicker"><span className="live-kicker-dot" aria-hidden="true" />实时 Core · phase1e.v1</div>
            <h1>{liveThreadTitle(selectedThread)}</h1>
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

      {manualReconcileRequired && (
        <div className="live-alert live-alert-warn" role="alert">
          <AlertTriangle size={17} aria-hidden="true" />
          <div className="live-alert-content">
            <strong>需要人工核对</strong>
            <span>{manualReconcileReason || 'Core 返回了 manual_reconcile_required / outcome_unknown。GUI 不会自动重放或猜测运行终态。'}</span>
          </div>
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => void refresh()} disabled={phase !== 'ready'}>
            重新查询 Projection
          </button>
        </div>
      )}

      <div className="live-chat-toolbar">
        <label className="live-select-label">
          <span>Project / Workspace</span>
          <select
            className="select"
            value={selectedProjectId ?? ''}
            onChange={(event) => selectProject(event.target.value || null)}
            aria-label="选择 Core Project Workspace"
          >
            <option value="">未选择 Project</option>
            {projects.filter((project) => project.readable).map((project) => (
              <option key={project.id} value={project.id}>{project.name} · {project.workspaceRef}</option>
            ))}
          </select>
        </label>
        <label className="live-select-label">
          <span>Thread</span>
          <select
            className="select"
            value={selectedThreadId ?? ''}
            onChange={(event) => {
              const id = event.target.value || null;
              if (selectThread(id) && id) navigate(`/chat/${id}`);
            }}
            aria-label="选择 Core Thread"
          >
            <option value="">未选择 Thread</option>
            {threads.map((thread) => <option key={thread.id} value={thread.id}>{thread.title || thread.id}</option>)}
          </select>
        </label>
        <label className="live-select-label live-session-select">
          <span>Session</span>
          <select
            className="select"
            value={selectedSessionId ?? ''}
            onChange={(event) => selectSession(event.target.value || null)}
            aria-label="选择 Core Session"
          >
            <option value="" disabled={Boolean(selectedThread?.sessionId)}>未绑定 Session</option>
            {sessionOptions.map((option) => (
              <option key={option.id} value={option.id} disabled={option.boundThreadId === null}>
                {sessionLabel(option)}
              </option>
            ))}
          </select>
        </label>
        <label className="live-select-label">
          <span>RolePreset</span>
          <select
            className="select"
            value={selectedRoleId}
            onChange={(event) => setSelectedRoleId(event.target.value)}
            aria-label="选择 Core RolePreset"
            disabled={roles.length === 0}
          >
            <option value="">未选择 RolePreset</option>
            {roles.filter((role) => role.status !== 'inactive' && role.id).map((role) => (
              <option key={role.id} value={role.id}>{role.name} · {role.id}</option>
            ))}
          </select>
        </label>
        <button type="button" className="btn btn-secondary btn-sm" onClick={() => void createThread()}
          disabled={!canCreateThread} title={createThreadUnavailableReason}>
          <PlusIcon />{threadCreationStatus === 'sending' ? '创建中…' : '新建会话'}
        </button>
        <button type="button" className="btn btn-secondary btn-sm" onClick={() => void handleCreateSession()} disabled={!canCreateSession || !selectedRoleId || creatingSession} title={createSessionUnavailableReason || '需要一个明确的 RolePreset'}>
          <PlusIcon />
          {creatingSession ? '创建中…' : '创建 Session'}
        </button>
      </div>

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
            title={deepLinkNotFound ? '未找到该 Core Thread' : phase === 'error' ? '无法显示 Core Thread' : '选择一个 Core Thread'}
            description={deepLinkNotFound ? `Core Projection 中不存在 Thread ${conversationId}。Live 不会用第一条或演示数据替代它。` : phase === 'error' ? '连接失败不会回退为演示数据。请修复 Core 或协议协商后重试。' : '从侧栏或上方选择服务端 Projection 中的 Thread。'}
            action={(
              <div className="live-empty-actions">
                <button type="button" className="btn btn-primary" onClick={() => void reconnect()}>
                  <RefreshCw size={14} aria-hidden="true" />重连 Core
                </button>
                <button type="button" className="btn btn-secondary" onClick={() => void handleCreateSession()} disabled={!canCreateSession || !selectedRoleId || creatingSession}>
                  创建 Session
                </button>
              </div>
            )}
          />
        </div>
      ) : (
        <>
          <section className="live-thread-summary" aria-label="Core Thread Projection">
            <div className="live-thread-summary-main">
              <span className="live-thread-icon" aria-hidden="true"><MessageSquare size={16} /></span>
              <div>
                <strong>{liveThreadTitle(selectedThread)}</strong>
                <span>{selectedThread.workspaceRef || '未绑定 Workspace'} · {selectedThread.id}</span>
              </div>
            </div>
            <StatusBadge status={selectedThread.status} label={statusLabel(selectedThread.status)} size="sm" />
          </section>

          {!selectedThread.sessionId && (
            <div className="live-alert live-alert-warn" role="alert">
              <AlertTriangle size={17} aria-hidden="true" />
              <div className="live-alert-content">
                <strong>Thread 未绑定 Session</strong>
                <span>该 Thread 的 legacy_refs 没有 source_type=session。Live 不会按 workspace 或第一条 Session 猜测，运行命令已禁用。</span>
              </div>
            </div>
          )}

          {visibleApprovals.length > 0 && (
            <section className="live-approvals-section" aria-labelledby="live-approvals-title">
              <div className="live-section-heading">
                <h2 id="live-approvals-title"><ShieldCheck size={16} aria-hidden="true" />待处理 Approval</h2>
                <span>{visibleApprovals.length} 项 · 由 Core Projection 提供</span>
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
                  <h2 id="live-messages-title">Session History</h2>
                  <p>B2 返回 canonical Item.payload；运行状态来自 Projection / SSE。</p>
                </div>
                <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                  <button type="button" className="btn btn-ghost btn-sm" onClick={() => void refreshHistory()} disabled={historyLoading || phase !== 'ready' || !selectedThread.sessionId} aria-label="刷新 Session history">
                    <RefreshCw size={13} aria-hidden="true" />刷新历史
                  </button>
                {selectedThread.sessionId && (
                  <button type="button" className="btn btn-secondary btn-sm" onClick={() => void cancelSession()} disabled={!cancelCommandAvailable || manualReconcileRequired || phase !== 'ready' || connectionStatus !== 'connected'} title="取消由 Core B2 Command 接收，按钮状态等待服务端 Projection 校正">
                    <Square size={12} aria-hidden="true" />取消 Session
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
                  <div className="live-panel-loading" role="status"><Loader2 size={15} className="animate-spin" />正在读取 Core canonical history…</div>
                ) : history?.items.length ? (
                  history.items.map((item, index) => <CanonicalHistoryItem item={item} index={index} key={item.id || `${item.thread_id}:${item.turn_id}:${item.position ?? index}`} />)
                ) : (
                  <p className="live-panel-empty">Core 尚未返回该 Session 的 canonical history。</p>
                )}
              </div>
              <div className="live-composer">
                <textarea
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                      event.preventDefault();
                      void handleSubmit();
                    }
                  }}
                  placeholder={manualReconcileRequired ? '需要人工核对，完成 Projection 校正后才能发送' : selectedThread?.sessionId ? '向当前 Session 发送消息，Enter 发送' : '该 Thread 没有 Session legacy ref，不能运行'}
                  aria-label="向 Core Session 发送消息"
                  disabled={!selectedThread?.sessionId || !selectedThread.workspaceRef || busy || phase !== 'ready'}
                  rows={2}
                />
                <button type="button" className="btn btn-primary btn-icon" onClick={() => void handleSubmit()} disabled={!canSend} aria-label="发送消息" title="发送消息">
                  {command.status === 'sending' ? <Loader2 size={16} className="animate-spin" aria-hidden="true" /> : <SendHorizontal size={16} aria-hidden="true" />}
                </button>
              </div>
              <p className="live-composer-note">发送请求只表示命令已提交；GUI 等待 Core 的 Receipt / Projection，不把网络送达当成完成。断线、回放或错误期间会禁用命令。</p>
            {history && history.next_cursor !== null && <button type="button" className="btn btn-secondary" disabled={historyLoading} onClick={() => void loadMoreHistory()}>加载更多历史</button>}
              </section>

            <aside className="live-inspector-column" aria-label="Core 实时检查器">
              <div className="live-inspector-actions">
                <button type="button" className={`btn btn-secondary btn-sm${showEvents ? ' active' : ''}`} onClick={() => setShowEvents((current) => !current)}>
                  <Wifi size={13} aria-hidden="true" />{showEvents ? '隐藏 SSE' : '查看 SSE'}
                </button>
                <button type="button" className={`btn btn-secondary btn-sm${showFiles ? ' active' : ''}`} onClick={() => {
                  const next = !showFiles;
                  setShowFiles(next);
                  if (next) void loadLiveFiles();
                }} disabled={!selectedProject}>
                  <Folder size={13} aria-hidden="true" />{showFiles ? '隐藏文件' : '浏览文件'}
                </button>
              </div>
              {showEvents && <LiveEventTimeline events={stream.events} cursor={stream.cursor} status={stream.status} />}
              {showFiles && selectedProject && (
                <LiveFileBrowser
                  projectId={selectedProject.id}
                  workspaceRef={selectedProject.workspaceRef}
                  files={files}
                  currentPath={filePath}
                  loading={filesLoading}
                  onLoad={(path) => void loadLiveFiles(path)}
                />
              )}
              <section className="live-panel live-scope-panel">
                <div className="live-panel-heading"><h2>本阶段边界</h2><ChevronRight size={15} aria-hidden="true" /></div>
                <p>Live 已接入 Core 连接、Workspace/Project、Thread、Session/Run、SSE Cursor 回放、Approval 与类型化错误。</p>
                <p className="live-not-connected">OAuth PKCE 由 Core 部署配置启用；PWA、TUI 与 Tauri 均复用生成 Client，远程连接仍以服务端验收状态为准。</p>
              </section>
            </aside>
          </div>
        </>
      )}
    </div>
  );
};

const PlusIcon: React.FC = () => <span aria-hidden="true"><span className="live-plus-icon">+</span></span>;
