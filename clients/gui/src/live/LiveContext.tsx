import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { Phase1E } from '@operant/sdk';
import { useOperant } from '../context/ClientContext';
import {
  LiveClientAdapter,
  UnsupportedLiveCapabilityError,
  eventPayload,
  mapSseFrame,
  normalizeLiveError,
} from './liveAdapter';
import {
  Cursor,
  EventAccumulator,
  LiveActionState,
  LiveApproval,
  LiveCommandState,
  LiveCreateSessionInput,
  LiveError,
  LiveEvent,
  LiveMessage,
  LiveProjectProjection,
  LiveSession,
  LiveSessionOption,
  LiveStreamState,
  LiveThread,
  LiveWorkspaceFile,
  containsManualReconcile,
  connectionLossState,
  createIdempotencyKey,
  emptyEventAccumulator,
  approvalActionKey,
  approvalProjectionResolved,
  isCurrentProjectionResponse,
  isThreadSelectionLocked,
  canBindSessionToThread,
  canDecideApproval,
  commandStateAfterTerminalEvent,
  eventNeedsManualReconcile,
  isApprovalProjectionEvent,
  isTerminalEvent,
  projectionGeneration,
  reduceEvent,
  shouldReleaseApprovalPending,
  streamEndedBeforeTerminalError,
  terminalEventError,
  terminalRunOutcome,
  threadForSession,
  sessionOptionsFor,
} from './liveState';

export type LiveProjectionPhase = 'idle' | 'connecting' | 'ready' | 'error';
export type LiveApprovalDecision = 'approve' | 'reject';

export interface LiveContextValue {
  phase: LiveProjectionPhase;
  adapter: LiveClientAdapter;
  projects: LiveProjectProjection[];
  threads: LiveThread[];
  /** Session details returned by the generated createSession Command. */
  sessions: LiveSession[];
  /** Selectable Session IDs merged with authoritative Thread bindings. */
  sessionOptions: LiveSessionOption[];
  approvals: LiveApproval[];
  selectedProjectId: string | null;
  selectedThreadId: string | null;
  selectedSessionId: string | null;
  selectedThread: LiveThread | undefined;
  selectedSession: LiveSession | undefined;
  /** Phase 1E has no message Query; this is always empty in live mode. */
  messages: LiveMessage[];
  files: LiveWorkspaceFile[];
  filesWorkspaceId: string | null;
  stream: LiveStreamState;
  projectionStale: boolean;
  lastError?: LiveError;
  command: LiveCommandState;
  approvalAction: LiveActionState;
  manualReconcileRequired: boolean;
  manualReconcileReason?: string;
  deepLinkTargetId: string | null;
  deepLinkNotFound: boolean;
  canCreateSession: boolean;
  createSessionUnavailableReason?: string;
  messageQueryAvailable: false;
  cancelCommandAvailable: false;
  selectProject: (projectId: string | null) => boolean;
  selectThread: (threadId: string | null) => boolean;
  selectSession: (sessionId: string | null) => boolean;
  resolveDeepLink: (threadId: string | null) => boolean;
  refresh: () => Promise<void>;
  reconnect: () => Promise<void>;
  createSession: (input: LiveCreateSessionInput) => Promise<LiveSession | undefined>;
  sendMessage: (message: string) => Promise<void>;
  cancelSession: () => Promise<void>;
  decideApproval: (approval: LiveApproval, decision: LiveApprovalDecision) => Promise<void>;
  loadFiles: (workspaceId: string, relativePath?: string) => Promise<void>;
  clearError: () => void;
}

const LiveContext = createContext<LiveContextValue | null>(null);

interface PendingRun {
  sessionId: string;
  threadId: string;
  workspace: string;
  message: string;
  idempotencyKey: string;
}

interface PendingApprovalDecision {
  sessionId: string;
  approvalId: string;
}

interface PendingSessionCreation {
  sessionId: string;
  idempotencyKey: string;
}

function initialStream(): LiveStreamState {
  return { status: 'idle', cursor: 0, events: [] };
}

function initialDeepLinkTarget(): string | null {
  if (typeof window === 'undefined') return null;
  const path = window.location.hash.replace(/^#/, '').split('?', 1)[0];
  if (!path.startsWith('/chat/')) return null;
  const encodedId = path.slice('/chat/'.length).split('/', 1)[0];
  if (!encodedId) return null;
  try {
    return decodeURIComponent(encodedId);
  } catch {
    return encodedId;
  }
}

function projectHasThread(project: LiveProjectProjection, threadId: string | null): boolean {
  return Boolean(threadId && project.threadIds.includes(threadId));
}

function roleIdForSession(session: LiveSession | undefined): string | undefined {
  const roleId = session?.role_snapshot.role_id;
  return roleId && roleId.trim() ? roleId : undefined;
}

function errorNeedsManualReconcile(error: LiveError): boolean {
  return error.recovery === 'manual_reconcile'
    || error.code.includes('manual_reconcile')
    || error.code.includes('outcome_unknown')
    || containsManualReconcile(error.detail);
}

function manualReconcileError(value: unknown, fallback: string): LiveError {
  if (typeof value === 'object' && value !== null && !Array.isArray(value)) {
    const candidate = value as { code?: unknown; message?: unknown; detail?: unknown };
    return {
      code: typeof candidate.code === 'string' ? candidate.code : 'manual_reconcile_required',
      message: typeof candidate.message === 'string' ? candidate.message : fallback,
      retryable: false,
      recovery: 'manual_reconcile',
      detail: candidate.detail ?? value,
    };
  }
  return {
    code: 'manual_reconcile_required',
    message: fallback,
    retryable: false,
    recovery: 'manual_reconcile',
    detail: value,
  };
}

function threadSelectionLockedError(): LiveError {
  return {
    code: 'thread_switch_blocked',
    message: '当前 Thread 仍有运行、Session 或 Approval 命令等待 Core 终态/Projection；请等待结果后再切换。',
    retryable: false,
    recovery: 'wait_for_projection',
  };
}

function frameHasTerminalProjection(event: LiveEvent): boolean {
  return isTerminalEvent(event) || containsManualReconcile(eventPayload(event));
}

export const LiveProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { phase1eClient, clientMode, connectionStatus } = useOperant();
  const adapter = useMemo(() => new LiveClientAdapter(phase1eClient), [phase1eClient]);
  const [phase, setPhase] = useState<LiveProjectionPhase>('idle');
  const [projects, setProjects] = useState<LiveProjectProjection[]>([]);
  const [threads, setThreads] = useState<LiveThread[]>([]);
  const [sessions, setSessions] = useState<LiveSession[]>([]);
  const [approvals, setApprovals] = useState<LiveApproval[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(null);
  const [selectedThreadId, setSelectedThreadId] = useState<string | null>(null);
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [messages] = useState<LiveMessage[]>([]);
  const [files, setFiles] = useState<LiveWorkspaceFile[]>([]);
  const [filesWorkspaceId, setFilesWorkspaceId] = useState<string | null>(null);
  const [stream, setStream] = useState<LiveStreamState>(initialStream);
  const [projectionStale, setProjectionStale] = useState(false);
  const [lastError, setLastError] = useState<LiveError | undefined>();
  const [command, setCommand] = useState<LiveCommandState>({ status: 'idle' });
  const [approvalAction, setApprovalAction] = useState<LiveActionState>({ status: 'idle' });
  const [manualReconcileRequired, setManualReconcileRequired] = useState(false);
  const [manualReconcileReason, setManualReconcileReason] = useState<string | undefined>();
  const [deepLinkTargetId, setDeepLinkTargetId] = useState<string | null>(initialDeepLinkTarget);
  const [deepLinkNotFound, setDeepLinkNotFound] = useState(false);
  const lifecycleRef = useRef(0);
  const projectionRequestSequenceRef = useRef(0);
  const accumulatorRef = useRef<EventAccumulator>(emptyEventAccumulator());
  const cursorTrackerRef = useRef(new Phase1E.ScopedCursorTracker());
  const selectedThreadIdRef = useRef<string | null>(null);
  const selectedSessionIdRef = useRef<string | null>(null);
  const selectedProjectIdRef = useRef<string | null>(null);
  const deepLinkTargetRef = useRef<string | null>(null);
  const pendingRunRef = useRef<PendingRun | null>(null);
  const pendingSessionRef = useRef<PendingSessionCreation | null>(null);
  const approvalKeysRef = useRef(new Map<string, string>());
  const createSessionKeyRef = useRef<string | null>(null);
  const createSessionInputRef = useRef<string | null>(null);
  const pendingApprovalRef = useRef<PendingApprovalDecision | null>(null);
  const terminalErrorRef = useRef<LiveError | undefined>(undefined);
  const commandRef = useRef<LiveCommandState>({ status: 'idle' });
  const approvalActionRef = useRef<LiveActionState>({ status: 'idle' });
  const commandDispatchingRef = useRef(false);
  const approvalDispatchingRef = useRef(false);
  const createSessionDispatchingRef = useRef(false);
  const manualReconcileRef = useRef(false);
  const previousConnectionRef = useRef(connectionStatus);
  const phaseRef = useRef(phase);
  const connectionStatusRef = useRef(connectionStatus);

  selectedThreadIdRef.current = selectedThreadId;
  selectedSessionIdRef.current = selectedSessionId;
  selectedProjectIdRef.current = selectedProjectId;
  deepLinkTargetRef.current = deepLinkTargetId;
  phaseRef.current = phase;
  connectionStatusRef.current = connectionStatus;
  commandRef.current = command;
  approvalActionRef.current = approvalAction;

  const selectedThread = threads.find((thread) => thread.id === selectedThreadId);
  const selectedSession = sessions.find((session) => session.id === selectedSessionId);
  const sessionOptions = useMemo(() => sessionOptionsFor(threads, sessions), [sessions, threads]);

  const markManualReconcile = useCallback((reason: string) => {
    manualReconcileRef.current = true;
    setManualReconcileRequired(true);
    setManualReconcileReason((current) => current || reason);
  }, []);

  const applyError = useCallback((error: unknown, stale = true): LiveError => {
    const normalized = normalizeLiveError(error).detail;
    setLastError(normalized);
    setProjectionStale(stale);
    if (errorNeedsManualReconcile(normalized)) markManualReconcile(normalized.message);
    return normalized;
  }, [markManualReconcile]);

  const rejectThreadSelection = useCallback((nextThreadId: string | null): boolean => {
    if (!isThreadSelectionLocked(
      selectedThreadIdRef.current,
      nextThreadId,
      pendingRunRef.current !== null,
      pendingSessionRef.current !== null,
      commandRef.current.status,
      pendingApprovalRef.current !== null,
      approvalActionRef.current.status,
    )) return false;
    setLastError(threadSelectionLockedError());
    return true;
  }, []);

  const commitSelection = useCallback((
    projectId: string | null,
    threadId: string | null,
    sessionId: string | null,
  ) => {
    selectedProjectIdRef.current = projectId;
    selectedThreadIdRef.current = threadId;
    selectedSessionIdRef.current = sessionId;
    setSelectedProjectId(projectId);
    setSelectedThreadId(threadId);
    setSelectedSessionId(sessionId);
  }, []);

  const selectProject = useCallback((projectId: string | null): boolean => {
    const project = projectId ? projects.find((item) => item.id === projectId) : undefined;
    const thread = project ? threads.find((item) => projectHasThread(project, item.id)) : undefined;
    const nextThreadId = thread?.id ?? null;
    if (rejectThreadSelection(nextThreadId)) return false;
    commitSelection(projectId, nextThreadId, thread?.sessionId ?? null);
    deepLinkTargetRef.current = null;
    setDeepLinkTargetId(null);
    setDeepLinkNotFound(false);
    return true;
  }, [commitSelection, projects, rejectThreadSelection, threads]);

  const selectThread = useCallback((threadId: string | null): boolean => {
    const thread = threads.find((item) => item.id === threadId);
    const nextThreadId = thread?.id ?? null;
    if (rejectThreadSelection(nextThreadId)) return false;
    const project = thread ? projects.find((item) => item.threadIds.includes(thread.id)) : undefined;
    commitSelection(project?.id ?? null, nextThreadId, thread?.sessionId ?? null);
    deepLinkTargetRef.current = null;
    setDeepLinkTargetId(null);
    setDeepLinkNotFound(false);
    return true;
  }, [commitSelection, projects, rejectThreadSelection, threads]);

  const selectSession = useCallback((sessionId: string | null): boolean => {
    const selectedThread = selectedThreadIdRef.current
      ? threads.find((item) => item.id === selectedThreadIdRef.current)
      : undefined;
    if (!sessionId) {
      // A bound Thread is authoritative; the placeholder option cannot clear
      // its Session and leave the select out of sync with the run target.
      commitSelection(
        selectedProjectIdRef.current,
        selectedThread?.id ?? null,
        selectedThread?.sessionId ?? null,
      );
      return true;
    }
    const thread = threadForSession(threads, sessionId);
    if (!thread) {
      // A page-local cached Session has no binding until Core projects it.
      // Ignore it instead of pairing the current Thread with the wrong ID.
      return false;
    }
    if (rejectThreadSelection(thread.id)) return false;
    commitSelection(
      projects.find((project) => project.threadIds.includes(thread.id))?.id ?? null,
      thread.id,
      thread.sessionId,
    );
    return true;
  }, [commitSelection, projects, rejectThreadSelection, threads]);

  const resolveDeepLink = useCallback((threadId: string | null): boolean => {
    if (!threadId) {
      if (rejectThreadSelection(null)) return false;
      deepLinkTargetRef.current = null;
      setDeepLinkTargetId(null);
      setDeepLinkNotFound(false);
      return true;
    }
    const thread = threads.find((item) => item.id === threadId);
    if (!thread) {
      if (rejectThreadSelection(null)) return false;
      // An unknown deep link is a real empty state, never the first item.
      deepLinkTargetRef.current = threadId;
      setDeepLinkTargetId(threadId);
      commitSelection(null, null, null);
      setDeepLinkNotFound(true);
      return true;
    }
    if (rejectThreadSelection(thread.id)) return false;
    deepLinkTargetRef.current = threadId;
    setDeepLinkTargetId(threadId);
    setDeepLinkNotFound(false);
    commitSelection(
      projects.find((project) => project.threadIds.includes(thread.id))?.id ?? null,
      thread.id,
      thread.sessionId,
    );
    return true;
  }, [commitSelection, projects, rejectThreadSelection, threads]);

  const loadProjection = useCallback(async (generation: number): Promise<boolean> => {
    const requestSequence = projectionRequestSequenceRef.current + 1;
    projectionRequestSequenceRef.current = requestSequence;
    const responseIsCurrent = () => isCurrentProjectionResponse(
      requestSequence,
      projectionRequestSequenceRef.current,
      generation,
      lifecycleRef.current,
    );
    let nextProjects: LiveProjectProjection[];
    let nextThreads: LiveThread[];
    try {
      [nextProjects, nextThreads] = await Promise.all([
        adapter.listProjects(),
        adapter.listThreads(),
      ]);
    } catch (error: unknown) {
      if (!responseIsCurrent()) return false;
      throw error;
    }
    if (!responseIsCurrent()) return false;
    if (connectionStatusRef.current !== 'connected' && phaseRef.current !== 'connecting') return false;

    const sessionIds = [...new Set(nextThreads.map((thread) => thread.sessionId).filter((id): id is string => Boolean(id)))];
    let approvalPages: LiveApproval[][];
    try {
      approvalPages = await Promise.all(sessionIds.map((sessionId) => adapter.listPendingApprovals(sessionId)));
    } catch (error: unknown) {
      if (!responseIsCurrent()) return false;
      throw error;
    }
    if (!responseIsCurrent()) return false;
    if (connectionStatusRef.current !== 'connected' && phaseRef.current !== 'connecting') return false;

    const nextApprovals = approvalPages.flat();
    const nextSessions = adapter.listSessions();

    const requestedThreadId = deepLinkTargetRef.current;
    const requestedThread = requestedThreadId
      ? nextThreads.find((thread) => thread.id === requestedThreadId)
      : undefined;
    const retainedThread = selectedThreadIdRef.current
      ? nextThreads.find((thread) => thread.id === selectedThreadIdRef.current)
      : undefined;
    const nextThread = requestedThreadId ? requestedThread : retainedThread || nextThreads[0];
    const requestedThreadIdForSelection = nextThread?.id ?? null;
    const selectionLocked = isThreadSelectionLocked(
      selectedThreadIdRef.current,
      requestedThreadIdForSelection,
      pendingRunRef.current !== null,
      pendingSessionRef.current !== null,
      commandRef.current.status,
      pendingApprovalRef.current !== null,
      approvalActionRef.current.status,
    );
    if (selectionLocked && selectedThreadIdRef.current !== null && !retainedThread) {
      // Do not replace the visible projection with a list that dropped the
      // Thread owning an in-flight command.  Its outcome is still unknown;
      // retaining the prior view is safer than silently selecting another
      // Thread or treating the missing row as completion.
      setLastError(threadSelectionLockedError());
      setProjectionStale(true);
      return false;
    }
    const effectiveThread = selectionLocked ? retainedThread : nextThread;
    const nextThreadId = effectiveThread?.id ?? null;
    const unknownDeepLink = Boolean(requestedThreadId && !requestedThread);
    if (!selectionLocked) {
      selectedThreadIdRef.current = nextThreadId;
      selectedSessionIdRef.current = effectiveThread?.sessionId ?? null;
      setSelectedThreadId(nextThreadId);
      setSelectedSessionId(effectiveThread?.sessionId ?? null);
      setDeepLinkNotFound(unknownDeepLink);
    } else {
      setLastError(threadSelectionLockedError());
    }

    const currentProject = nextProjects.find((project) => project.id === selectedProjectIdRef.current);
    const threadProject = nextProjects.find((project) => project.threadIds.includes(nextThreadId ?? ''));
    const nextProjectId = threadProject?.id ?? (unknownDeepLink
      ? null
      : nextThreadId ? currentProject?.id ?? null : nextProjects[0]?.id ?? null);

    setProjects(nextProjects);
    setThreads(nextThreads);
    setSessions(nextSessions);
    setApprovals(nextApprovals);
    setProjectionStale(false);
    if (!selectionLocked) {
      selectedProjectIdRef.current = nextProjectId;
      setSelectedProjectId(nextProjectId);
      setLastError((current) => manualReconcileRef.current ? current : terminalErrorRef.current);
    }

    const pendingSession = pendingSessionRef.current;
    const pendingSessionThread = pendingSession ? threadForSession(nextThreads, pendingSession.sessionId) : undefined;
    if (pendingSessionThread) {
      pendingSessionRef.current = null;
      createSessionKeyRef.current = null;
      createSessionInputRef.current = null;
      selectedThreadIdRef.current = pendingSessionThread.id;
      selectedSessionIdRef.current = pendingSessionThread.sessionId;
      selectedProjectIdRef.current = nextProjects.find((project) => project.threadIds.includes(pendingSessionThread.id))?.id ?? null;
      setCommand({ status: 'idle' });
      setSelectedThreadId(pendingSessionThread.id);
      setSelectedSessionId(pendingSessionThread.sessionId);
      setSelectedProjectId(nextProjects.find((project) => project.threadIds.includes(pendingSessionThread.id))?.id ?? null);
    } else if (pendingSession) {
      setCommand({
        status: 'awaiting_projection',
        idempotencyKey: pendingSession.idempotencyKey,
        error: {
          code: 'session_thread_projection_pending',
          message: `Core 已创建 Session ${pendingSession.sessionId}，但 ThreadProjection.legacy_refs 尚未返回对应 session ref；不会重复创建。`,
          retryable: false,
          recovery: 'none',
        },
      });
    }

    const pendingApproval = pendingApprovalRef.current;
    if (pendingApproval) {
      if (approvalProjectionResolved(
        pendingApproval.sessionId,
        pendingApproval.approvalId,
        nextApprovals,
      )) {
        pendingApprovalRef.current = null;
        setApprovalAction({ status: 'idle' });
      }
    }

    return true;
  }, [adapter]);

  const refresh = useCallback(async () => {
    if (clientMode !== 'live') return;
    if (connectionStatusRef.current !== 'connected' && phaseRef.current !== 'connecting') return;
    const generation = projectionGeneration(lifecycleRef.current, true);
    lifecycleRef.current = generation;
    try {
      const committed = await loadProjection(generation);
      if (committed && generation === lifecycleRef.current && connectionStatusRef.current === 'connected') {
        phaseRef.current = 'ready';
        setPhase('ready');
      }
    } catch (error: unknown) {
      if (generation !== lifecycleRef.current) return;
      phaseRef.current = 'error';
      setPhase((current) => (current === 'ready' ? current : 'error'));
      applyError(error);
    }
  }, [applyError, clientMode, loadProjection]);

  const correctProjection = useCallback(async () => {
    if (clientMode !== 'live' || manualReconcileRef.current) return;
    // A projection correction must not invalidate the generation captured by
    // the active AsyncIterable. Only an explicit refresh/reconnect supersedes
    // in-flight stream work.
    const generation = projectionGeneration(lifecycleRef.current, false);
    try {
      await loadProjection(generation);
    } catch (error: unknown) {
      if (generation === lifecycleRef.current) applyError(error);
    }
  }, [applyError, clientMode, loadProjection]);

  const connectAndRefresh = useCallback(async (): Promise<boolean> => {
    if (clientMode !== 'live') return false;
    const generation = projectionGeneration(lifecycleRef.current, true);
    lifecycleRef.current = generation;
    phaseRef.current = 'connecting';
    setPhase('connecting');
    setStream((current) => ({
      ...current,
      status: 'connecting',
      error: manualReconcileRef.current ? current.error : undefined,
    }));
    try {
      await adapter.connect();
      if (generation !== lifecycleRef.current) return false;
      const committed = await loadProjection(generation);
      if (!committed || generation !== lifecycleRef.current) return false;
      if (connectionStatusRef.current === 'disconnected' || connectionStatusRef.current === 'reconnecting') return false;
      phaseRef.current = 'ready';
      setPhase('ready');
      if (manualReconcileRef.current) {
        setStream((current) => ({
          ...current,
          status: 'error',
          error: current.error || manualReconcileError(undefined, '需要人工核对，Live 不会自动重试。'),
        }));
      } else {
        setStream((current) => ({ ...current, status: 'connected', error: undefined }));
      }
      return true;
    } catch (error: unknown) {
      if (generation !== lifecycleRef.current) return false;
      const detail = applyError(error, false);
      phaseRef.current = 'error';
      setPhase('error');
      setStream((current) => ({ ...current, status: 'error', error: detail }));
      return false;
    }
  }, [adapter, applyError, clientMode, loadProjection]);

  const handleFrame = useCallback((frame: Phase1E.SseFrame, threadId: string, generation: number) => {
    if (generation !== lifecycleRef.current || selectedThreadIdRef.current !== threadId) return;
    if (manualReconcileRef.current) return;
    const event = mapSseFrame(frame, pendingRunRef.current?.sessionId || selectedSessionIdRef.current || 'unknown');
    const requiresManualReconcile = eventNeedsManualReconcile(event) || containsManualReconcile(frame.data);
    if (requiresManualReconcile) {
      // Detect and persist this before any stream state can be marked
      // connected. The raw error/detail remains on the mapped event.
      const detail = manualReconcileError(
        event.error,
        'SSE 返回 manual_reconcile_required / outcome_unknown，必须人工核对。',
      );
      markManualReconcile(detail.message);
      const accepted = cursorTrackerRef.current.accept(frame);
      const next = accepted
        ? reduceEvent(accumulatorRef.current, `${event.resource_scope}\0${event.stream_kind}`, event)
        : accumulatorRef.current;
      accumulatorRef.current = next;
      setLastError(detail);
      setProjectionStale(true);
      setCommand((current) => ({ status: 'error', error: detail, idempotencyKey: current.idempotencyKey }));
      setApprovalAction((current) => ({ status: 'error', error: detail, idempotencyKey: current.idempotencyKey }));
      setStream((current) => ({
        ...current,
        status: 'error',
        cursor: next.cursor,
        events: next.events,
        lastEvent: accepted ? event : current.lastEvent,
        error: detail,
      }));
      return;
    }
    const accepted = cursorTrackerRef.current.accept(frame);
    if (!accepted) return;
    if (event.thread_id && event.thread_id !== threadId) return;
    const scope = `${event.resource_scope}\0${event.stream_kind}`;
    const next = reduceEvent(accumulatorRef.current, scope, event);
    if (next === accumulatorRef.current) return;
    accumulatorRef.current = next;
    setStream((current) => ({
      ...current,
      status: 'connected',
      cursor: next.cursor,
      events: next.events,
      lastEvent: event,
      error: undefined,
    }));
    const terminalOutcome = terminalRunOutcome(event);
    if (terminalOutcome !== null) {
      const pendingRun = pendingRunRef.current;
      if (pendingRun?.threadId === threadId) pendingRunRef.current = null;
      setCommand((current) => commandStateAfterTerminalEvent(event, current));
      const terminalError = terminalEventError(event);
      terminalErrorRef.current = terminalError;
      if (terminalError) {
        setLastError(terminalError);
        setStream((current) => ({ ...current, error: terminalError }));
      }
    }
    if (isApprovalProjectionEvent(event) || frameHasTerminalProjection(event)) void correctProjection();
  }, [correctProjection, markManualReconcile]);

  const consumeRunStream = useCallback(async (
    run: PendingRun,
    replay: boolean,
    generation: number,
  ): Promise<boolean> => {
    const lastEventId: Cursor | undefined = replay && accumulatorRef.current.cursor !== null
      ? accumulatorRef.current.cursor
      : undefined;
    setStream((current) => ({ ...current, status: replay ? 'replaying' : 'connecting', error: undefined }));
    const streamResult = await adapter.runSessionStream(
      run.sessionId,
      {
        message: run.message,
        workspace: run.workspace,
        thread_id: run.threadId,
      },
      {
        idempotencyKey: run.idempotencyKey,
        lastEventId,
      },
    );
    if (generation !== lifecycleRef.current) return false;

    if (streamResult.receipt) {
      if (streamResult.receipt.recovery === 'manual_reconcile' || containsManualReconcile(streamResult.receipt)) {
        const detail = manualReconcileError(
          streamResult.receipt,
          'Core Receipt 要求 manual_reconcile，必须人工核对。',
        );
        markManualReconcile(detail.message);
        setLastError(detail);
        setProjectionStale(true);
        setCommand({ status: 'error', error: detail, idempotencyKey: run.idempotencyKey });
        setStream((current) => ({ ...current, status: 'error', error: detail }));
        return false;
      }
      // 202 is only idempotency/command admission evidence, never terminal.
      setCommand({ status: 'awaiting_projection', idempotencyKey: run.idempotencyKey });
      setStream((current) => ({ ...current, status: 'connected', error: undefined }));
      return true;
    }

    setCommand({ status: 'awaiting_projection', idempotencyKey: run.idempotencyKey });
    let terminalSeen = false;
    for await (const frame of streamResult.events) {
      const event = mapSseFrame(frame, run.sessionId);
      if (
        selectedThreadIdRef.current === run.threadId
        && (!event.thread_id || event.thread_id === run.threadId)
        && terminalRunOutcome(event) !== null
      ) {
        terminalSeen = true;
      }
      handleFrame(frame, run.threadId, generation);
    }
    if (generation !== lifecycleRef.current || manualReconcileRef.current) return false;
    const runStillPending = pendingRunRef.current?.idempotencyKey === run.idempotencyKey;
    if (!terminalSeen || runStillPending) {
      const detail = streamEndedBeforeTerminalError();
      setLastError(detail);
      setProjectionStale(true);
      setCommand({ status: 'error', error: detail, idempotencyKey: run.idempotencyKey });
      setStream((current) => ({ ...current, status: 'error', error: detail }));
      // Keep pendingRunRef and its original idempotency key. An explicit
      // reconnect can replay this exact logical command without guessing EOF
      // to be a successful completion.
      return false;
    }
    setStream((current) => ({ ...current, status: 'connected', error: undefined }));
    return true;
  }, [adapter, handleFrame, markManualReconcile]);

  const replayThenCorrect = useCallback(async () => {
    if (clientMode !== 'live' || manualReconcileRequired || connectionStatusRef.current !== 'connected') return;
    const generation = lifecycleRef.current;
    const pendingRun = pendingRunRef.current;
    try {
      if (pendingRun && pendingRun.threadId === selectedThreadIdRef.current) {
        const replayCompleted = await consumeRunStream(pendingRun, true, generation);
        if (!replayCompleted) return;
      }
      // Replay is followed by authoritative projection queries.
      await refresh();
      if (!manualReconcileRef.current) setStream((current) => ({ ...current, status: 'connected', error: undefined }));
    } catch (error: unknown) {
      const detail = applyError(error);
      setStream((current) => ({ ...current, status: 'error', error: detail }));
      setCommand((current) => ({ status: 'error', error: detail, idempotencyKey: current.idempotencyKey }));
    }
  }, [applyError, clientMode, consumeRunStream, manualReconcileRequired, phase, refresh]);

  const reconnect = useCallback(async () => {
    if (clientMode !== 'live') return;
    if (manualReconcileRequired) {
      // Manual reconcile allows read-only reconnection/query only. Never replay.
      await connectAndRefresh();
      return;
    }
    if (phase !== 'ready' || connectionStatus !== 'connected') {
      const established = await connectAndRefresh();
      if (established && !manualReconcileRef.current) await replayThenCorrect();
      return;
    }
    await replayThenCorrect();
  }, [clientMode, connectAndRefresh, connectionStatus, manualReconcileRequired, phase, replayThenCorrect]);

  useEffect(() => {
    previousConnectionRef.current = connectionStatus;
    if (clientMode === 'live') {
      void connectAndRefresh();
      return () => {
        lifecycleRef.current = projectionGeneration(lifecycleRef.current, true);
      };
    }
    lifecycleRef.current = projectionGeneration(lifecycleRef.current, true);
    phaseRef.current = 'idle';
    setPhase('idle');
    setProjects([]);
    setThreads([]);
    setSessions([]);
    setApprovals([]);
    setFiles([]);
    setFilesWorkspaceId(null);
    setSelectedProjectId(null);
    setSelectedThreadId(null);
    setSelectedSessionId(null);
    setDeepLinkTargetId(null);
    setDeepLinkNotFound(false);
    setManualReconcileRequired(false);
    manualReconcileRef.current = false;
    setManualReconcileReason(undefined);
    setLastError(undefined);
    setProjectionStale(false);
    setCommand({ status: 'idle' });
    setApprovalAction({ status: 'idle' });
    selectedProjectIdRef.current = null;
    selectedThreadIdRef.current = null;
    selectedSessionIdRef.current = null;
    deepLinkTargetRef.current = null;
    commandRef.current = { status: 'idle' };
    approvalActionRef.current = { status: 'idle' };
    accumulatorRef.current = emptyEventAccumulator();
    cursorTrackerRef.current = new Phase1E.ScopedCursorTracker();
    pendingRunRef.current = null;
    pendingSessionRef.current = null;
    pendingApprovalRef.current = null;
    terminalErrorRef.current = undefined;
    approvalKeysRef.current.clear();
    createSessionKeyRef.current = null;
    createSessionInputRef.current = null;
    commandDispatchingRef.current = false;
    approvalDispatchingRef.current = false;
    createSessionDispatchingRef.current = false;
    setStream(initialStream());
  }, [clientMode, connectAndRefresh]);

  useEffect(() => {
    if (clientMode !== 'live' || phase !== 'ready') return;
    accumulatorRef.current = emptyEventAccumulator();
    cursorTrackerRef.current = new Phase1E.ScopedCursorTracker();
    setStream((current) => ({ ...current, status: 'connected', cursor: 0, events: [], lastEvent: undefined, error: undefined }));
  }, [clientMode, phase, selectedThreadId]);

  useEffect(() => {
    const previous = previousConnectionRef.current;
    previousConnectionRef.current = connectionStatus;
    if (clientMode !== 'live') return;
    if (connectionStatus === 'disconnected' || connectionStatus === 'reconnecting') {
      const lost = connectionLossState(connectionStatus);
      phaseRef.current = lost.phase;
      setPhase(lost.phase);
      setProjectionStale(lost.projectionStale);
      setLastError(lost.error);
      setStream((current) => ({ ...current, status: lost.streamStatus, error: lost.error }));
      return;
    }
    if (connectionStatus === 'connected' && previous !== 'connected') {
      void (async () => {
        const established = await connectAndRefresh();
        if (established && !manualReconcileRef.current) await replayThenCorrect();
      })();
    }
  }, [clientMode, connectAndRefresh, connectionStatus, replayThenCorrect]);

  const createSession = useCallback(async (input: LiveCreateSessionInput) => {
    if (clientMode !== 'live' || phase !== 'ready' || manualReconcileRequired || command.status !== 'idle') return undefined;
    if (stream.status === 'replaying' || stream.status === 'error' || connectionStatus !== 'connected') return undefined;
    const selectedThread = selectedThreadIdRef.current
      ? threads.find((thread) => thread.id === selectedThreadIdRef.current)
      : undefined;
    if (!selectedThread || !canBindSessionToThread(selectedThread)) return undefined;
    if (input.threadId !== selectedThread.id) {
      const detail: LiveError = {
        code: 'thread_selection_changed',
        message: '当前选中的 Thread 已变化，请重新选择后再创建 Session。',
        retryable: false,
        recovery: 'refresh_and_retry',
      };
      setLastError(detail);
      setCommand({ status: 'error', error: detail });
      return undefined;
    }
    const roleIdAvailable = typeof input?.roleId === 'string' && input.roleId.trim().length > 0;
    const newRole = input?.newRole;
    const validNewRole = newRole
      && newRole.name.trim().length > 0
      && newRole.system_prompt.trim().length > 0
      && newRole.model_profile_id.trim().length > 0
      ? newRole
      : undefined;
    const newRoleAvailable = validNewRole !== undefined;
    if (Number(roleIdAvailable) + Number(newRoleAvailable) !== 1) return undefined;
    if (createSessionDispatchingRef.current) return undefined;
    createSessionDispatchingRef.current = true;
    try {
      const inputFingerprint = JSON.stringify(input);
      const sameInput = createSessionInputRef.current === inputFingerprint;
      const idempotencyKey = sameInput && createSessionKeyRef.current
        ? createSessionKeyRef.current
        : createIdempotencyKey();
      createSessionInputRef.current = inputFingerprint;
      createSessionKeyRef.current = idempotencyKey;
      pendingSessionRef.current = { sessionId: '', idempotencyKey };
      setCommand({ status: 'sending', idempotencyKey });
      const session = await adapter.createSession(input, idempotencyKey);
      pendingSessionRef.current = { sessionId: session.id, idempotencyKey };
      setSessions(adapter.listSessions());
      setSelectedSessionId(session.id);
      setCommand({
        status: 'awaiting_projection',
        idempotencyKey,
        error: {
          code: 'session_thread_projection_pending',
          message: `Core 已创建 Session ${session.id}，等待 ThreadProjection.legacy_refs 返回对应 session ref。`,
          retryable: false,
          recovery: 'none',
        },
      });
      await refresh();
      return session;
    } catch (error: unknown) {
      const detail = applyError(error, false);
      if (!detail.retryable && !errorNeedsManualReconcile(detail)) {
        // A typed non-retryable rejection proves the Session command did not
        // remain in an unknown state, so it must not strand Thread selection.
        pendingSessionRef.current = null;
      }
      setCommand({ status: 'error', error: detail, idempotencyKey: createSessionKeyRef.current ?? undefined });
      return undefined;
    } finally {
      createSessionDispatchingRef.current = false;
    }
  }, [adapter, applyError, clientMode, command.status, connectionStatus, manualReconcileRequired, phase, refresh, stream.status, threads]);

  const sendMessage = useCallback(async (message: string) => {
    const trimmed = message.trim();
    const thread = selectedThreadIdRef.current ? threads.find((item) => item.id === selectedThreadIdRef.current) : undefined;
    if (!trimmed || clientMode !== 'live' || phase !== 'ready' || !thread?.sessionId || !thread.workspaceRef) return;
    if (manualReconcileRequired || command.status !== 'idle' || connectionStatus !== 'connected' || stream.status !== 'connected') return;
    if (commandDispatchingRef.current) return;
    commandDispatchingRef.current = true;
    terminalErrorRef.current = undefined;
    setLastError(undefined);
    setStream((current) => ({ ...current, error: undefined }));
    const previousRun = pendingRunRef.current;
    const commandKey = previousRun
      && previousRun.sessionId === thread.sessionId
      && previousRun.threadId === thread.id
      && previousRun.message === trimmed
      ? previousRun.idempotencyKey
      : createIdempotencyKey();
    const run: PendingRun = {
      sessionId: thread.sessionId,
      threadId: thread.id,
      workspace: thread.workspaceRef,
      message: trimmed,
      idempotencyKey: commandKey,
    };
    pendingRunRef.current = run;
    setCommand({ status: 'sending', idempotencyKey: commandKey });
    try {
      const streamCompleted = await consumeRunStream(run, false, lifecycleRef.current);
      if (clientMode === 'live' && streamCompleted) await refresh();
    } catch (error: unknown) {
      const detail = applyError(error, false);
      if (!detail.retryable && !errorNeedsManualReconcile(detail)) {
        // A definitive command rejection is not an in-flight run.  Retryable
        // transport failures intentionally keep the pending key so reconnect
        // can safely replay it without allowing a cross-Thread race.
        pendingRunRef.current = null;
      }
      setStream((current) => ({ ...current, status: 'error', error: detail }));
      setCommand({ status: 'error', error: detail, idempotencyKey: commandKey });
    } finally {
      commandDispatchingRef.current = false;
    }
  }, [applyError, clientMode, command.status, connectionStatus, consumeRunStream, manualReconcileRequired, phase, refresh, stream.status, threads]);

  const cancelSession = useCallback(async () => {
    if (clientMode !== 'live') return;
    const error = new UnsupportedLiveCapabilityError('Session cancel Command');
    const detail = applyError(error, false);
    setCommand({ status: 'error', error: detail });
  }, [applyError, clientMode]);

  const decideApproval = useCallback(async (approval: LiveApproval, decision: LiveApprovalDecision) => {
    if (
      clientMode !== 'live'
      || phase !== 'ready'
      || !canDecideApproval(
        approval,
        approvalAction,
        connectionStatus,
        stream.status,
        manualReconcileRequired,
      )
      || approvalDispatchingRef.current
    ) return;
    approvalDispatchingRef.current = true;
    const keyId = approvalActionKey(approval.sessionId, approval.id, decision);
    const idempotencyKey = approvalKeysRef.current.get(keyId) || createIdempotencyKey();
    approvalKeysRef.current.set(keyId, idempotencyKey);
    pendingApprovalRef.current = { sessionId: approval.sessionId, approvalId: approval.id };
    setApprovalAction({ status: 'sending', idempotencyKey });
    try {
      await adapter.submitApproval(approval, decision === 'approve', idempotencyKey);
      // Do not clear pending locally. Only the next scoped Query can confirm
      // that the target approval is no longer pending.
      setApprovalAction({ status: 'awaiting_projection', idempotencyKey });
      // Keep the active run stream generation so its terminal frame is still accepted.
      await correctProjection();
    } catch (error: unknown) {
      const detail = applyError(error, false);
      if (shouldReleaseApprovalPending(detail)
        && pendingApprovalRef.current?.sessionId === approval.sessionId
        && pendingApprovalRef.current?.approvalId === approval.id) {
        pendingApprovalRef.current = null;
      }
      setApprovalAction({ status: 'error', error: detail, idempotencyKey });
    } finally {
      approvalDispatchingRef.current = false;
    }
  }, [adapter, approvalAction.status, applyError, clientMode, connectionStatus, correctProjection, manualReconcileRequired, phase, stream.status]);

  const loadFiles = useCallback(async (workspaceId: string, relativePath = '') => {
    if (clientMode !== 'live' || phase !== 'ready' || connectionStatus !== 'connected') return;
    try {
      const nextFiles = await adapter.listWorkspaceFiles(workspaceId, relativePath);
      setFilesWorkspaceId(workspaceId);
      setFiles(nextFiles);
      setLastError(undefined);
      setProjectionStale(false);
    } catch (error: unknown) {
      setFiles([]);
      setFilesWorkspaceId(workspaceId);
      applyError(error);
    }
  }, [adapter, applyError, clientMode, connectionStatus, phase]);

  const clearError = useCallback(() => {
    terminalErrorRef.current = undefined;
    setLastError(undefined);
    setStream((current) => ({ ...current, error: undefined }));
    // Manual reconcile is intentionally not cleared: this prevents a
    // dismissed banner from becoming permission to retry an unknown action.
    setCommand((current) => current.status === 'error' && !manualReconcileRequired ? { status: 'idle' } : current);
    setApprovalAction((current) => current.status === 'error' && !manualReconcileRequired ? { status: 'idle' } : current);
  }, [manualReconcileRequired]);

  const canCreateSession = clientMode === 'live'
    && phase === 'ready'
    && connectionStatus === 'connected'
    && stream.status === 'connected'
    && command.status === 'idle'
    && !manualReconcileRequired
    && canBindSessionToThread(selectedThread);
  const createSessionUnavailableReason = manualReconcileRequired
    ? '需要人工核对，不能创建新命令。'
    : command.status === 'awaiting_projection'
      ? command.error?.message || '等待 Core ThreadProjection 校正，不能重复创建 Session。'
    : !selectedThread
      ? '请先选择一个 Core Thread；Session 必须绑定到明确的 Thread。'
    : selectedThread.status !== 'active'
      ? '当前 Thread 不是 active，Core 不允许绑定新的 Session。'
    : selectedThread.sessionId
      ? '当前 Thread 已绑定 Session，不能再创建第二个 Session。'
    : connectionStatus !== 'connected' || phase !== 'ready'
      ? 'Core 尚未连接或协议尚未协商。'
      : stream.status !== 'connected'
        ? 'SSE 正在回放或发生错误。'
        : undefined;

  const value = useMemo<LiveContextValue>(() => ({
    phase,
    adapter,
    projects,
    threads,
    sessions,
    sessionOptions,
    approvals,
    selectedProjectId,
    selectedThreadId,
    selectedSessionId,
    selectedThread,
    selectedSession,
    messages,
    files,
    filesWorkspaceId,
    stream,
    projectionStale,
    lastError,
    command,
    approvalAction,
    manualReconcileRequired,
    manualReconcileReason,
    deepLinkTargetId,
    deepLinkNotFound,
    canCreateSession,
    createSessionUnavailableReason,
    messageQueryAvailable: false,
    cancelCommandAvailable: false,
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
  }), [
    adapter,
    approvalAction,
    approvals,
    canCreateSession,
    cancelSession,
    clearError,
    command,
    createSession,
    createSessionUnavailableReason,
    deepLinkNotFound,
    deepLinkTargetId,
    decideApproval,
    files,
    filesWorkspaceId,
    lastError,
    loadFiles,
    manualReconcileReason,
    manualReconcileRequired,
    messages,
    phase,
    projects,
    projectionStale,
    reconnect,
    refresh,
    resolveDeepLink,
    selectProject,
    selectSession,
    selectThread,
    selectedProjectId,
    selectedSession,
    selectedSessionId,
    selectedThread,
    selectedThreadId,
    sendMessage,
    sessions,
    sessionOptions,
    stream,
    threads,
  ]);

  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>;
};

export function useLive(): LiveContextValue {
  const context = useContext(LiveContext);
  if (!context) throw new Error('useLive must be used within a LiveProvider');
  return context;
}

export function liveThreadTitle(thread: LiveThread | undefined): string {
  return thread?.title || thread?.id || '未命名 Thread';
}

export function sessionRoleId(session: LiveSession | undefined): string | undefined {
  return roleIdForSession(session);
}
