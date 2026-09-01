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
  LiveStreamState,
  LiveThread,
  LiveWorkspaceFile,
  containsManualReconcile,
  createIdempotencyKey,
  emptyEventAccumulator,
  isApprovalProjectionEvent,
  isTerminalEvent,
  reduceEvent,
} from './liveState';

export type LiveProjectionPhase = 'idle' | 'connecting' | 'ready' | 'error';
export type LiveApprovalDecision = 'approve' | 'reject';

export interface LiveContextValue {
  phase: LiveProjectionPhase;
  adapter: LiveClientAdapter;
  projects: LiveProjectProjection[];
  threads: LiveThread[];
  /** Only sessions returned by the generated createSession Command are cached. */
  sessions: LiveSession[];
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
  selectProject: (projectId: string | null) => void;
  selectThread: (threadId: string | null) => void;
  selectSession: (sessionId: string | null) => void;
  resolveDeepLink: (threadId: string | null) => void;
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
    || error.code.includes('outcome_unknown');
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
  const accumulatorRef = useRef<EventAccumulator>(emptyEventAccumulator());
  const cursorTrackerRef = useRef(new Phase1E.ScopedCursorTracker());
  const selectedThreadIdRef = useRef<string | null>(null);
  const selectedSessionIdRef = useRef<string | null>(null);
  const selectedProjectIdRef = useRef<string | null>(null);
  const deepLinkTargetRef = useRef<string | null>(null);
  const pendingRunRef = useRef<PendingRun | null>(null);
  const approvalKeysRef = useRef(new Map<string, string>());
  const createSessionKeyRef = useRef<string | null>(null);
  const createSessionInputRef = useRef<string | null>(null);
  const pendingApprovalRef = useRef<PendingApprovalDecision | null>(null);
  const commandDispatchingRef = useRef(false);
  const approvalDispatchingRef = useRef(false);
  const createSessionDispatchingRef = useRef(false);
  const manualReconcileRef = useRef(false);
  const previousConnectionRef = useRef(connectionStatus);

  selectedThreadIdRef.current = selectedThreadId;
  selectedSessionIdRef.current = selectedSessionId;
  selectedProjectIdRef.current = selectedProjectId;
  deepLinkTargetRef.current = deepLinkTargetId;

  const selectedThread = threads.find((thread) => thread.id === selectedThreadId);
  const selectedSession = sessions.find((session) => session.id === selectedSessionId);

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

  const selectProject = useCallback((projectId: string | null) => {
    setSelectedProjectId(projectId);
    if (!projectId) return;
    const project = projects.find((item) => item.id === projectId);
    const thread = project ? threads.find((item) => projectHasThread(project, item.id)) : undefined;
    setSelectedThreadId(thread?.id ?? null);
    setSelectedSessionId(thread?.sessionId ?? null);
    setDeepLinkTargetId(null);
    setDeepLinkNotFound(false);
  }, [projects, threads]);

  const selectThread = useCallback((threadId: string | null) => {
    const thread = threads.find((item) => item.id === threadId);
    setSelectedThreadId(thread?.id ?? null);
    setSelectedSessionId(thread?.sessionId ?? null);
    setDeepLinkTargetId(null);
    setDeepLinkNotFound(false);
    if (thread) {
      const project = projects.find((item) => item.threadIds.includes(thread.id));
      setSelectedProjectId(project?.id ?? null);
    }
  }, [projects, threads]);

  const selectSession = useCallback((sessionId: string | null) => {
    const thread = threads.find((item) => item.sessionId === sessionId);
    setSelectedSessionId(sessionId);
    if (thread) {
      setSelectedThreadId(thread.id);
      setSelectedProjectId(projects.find((project) => project.threadIds.includes(thread.id))?.id ?? null);
    }
  }, [projects, threads]);

  const resolveDeepLink = useCallback((threadId: string | null) => {
    setDeepLinkTargetId(threadId);
    if (!threadId) {
      setDeepLinkNotFound(false);
      return;
    }
    const thread = threads.find((item) => item.id === threadId);
    if (!thread) {
      // An unknown deep link is a real empty state, never the first item.
      setSelectedThreadId(null);
      setSelectedSessionId(null);
      setSelectedProjectId(null);
      setDeepLinkNotFound(true);
      return;
    }
    setDeepLinkNotFound(false);
    setSelectedThreadId(thread.id);
    setSelectedSessionId(thread.sessionId);
    setSelectedProjectId(projects.find((project) => project.threadIds.includes(thread.id))?.id ?? null);
  }, [projects, threads]);

  const loadProjection = useCallback(async (generation: number) => {
    const [nextProjects, nextThreads] = await Promise.all([
      adapter.listProjects(),
      adapter.listThreads(),
    ]);
    if (generation !== lifecycleRef.current) return;

    const sessionIds = [...new Set(nextThreads.map((thread) => thread.sessionId).filter((id): id is string => Boolean(id)))];
    const approvalPages = await Promise.all(sessionIds.map((sessionId) => adapter.listPendingApprovals(sessionId)));
    if (generation !== lifecycleRef.current) return;

    const nextApprovals = approvalPages.flat();
    const nextSessions = adapter.listSessions();
    setProjects(nextProjects);
    setThreads(nextThreads);
    setSessions(nextSessions);
    setApprovals(nextApprovals);
    setProjectionStale(false);
    setLastError(undefined);

    const requestedThreadId = deepLinkTargetRef.current;
    const requestedThread = requestedThreadId
      ? nextThreads.find((thread) => thread.id === requestedThreadId)
      : undefined;
    const retainedThread = selectedThreadIdRef.current
      ? nextThreads.find((thread) => thread.id === selectedThreadIdRef.current)
      : undefined;
    const nextThread = requestedThreadId ? requestedThread : retainedThread || nextThreads[0];
    const nextThreadId = nextThread?.id ?? null;
    const unknownDeepLink = Boolean(requestedThreadId && !requestedThread);
    setSelectedThreadId(nextThreadId);
    setSelectedSessionId(nextThread?.sessionId ?? null);
    setDeepLinkNotFound(unknownDeepLink);

    const currentProject = nextProjects.find((project) => project.id === selectedProjectIdRef.current);
    const threadProject = nextProjects.find((project) => project.threadIds.includes(nextThreadId ?? ''));
    setSelectedProjectId(threadProject?.id ?? (unknownDeepLink
      ? null
      : nextThreadId ? currentProject?.id ?? null : nextProjects[0]?.id ?? null));

    const pendingApproval = pendingApprovalRef.current;
    if (pendingApproval) {
      const remainsPending = nextApprovals.some((approval) => (
        approval.sessionId === pendingApproval.sessionId
        && approval.id === pendingApproval.approvalId
        && approval.status === 'pending'
      ));
      if (!remainsPending) {
        pendingApprovalRef.current = null;
        setApprovalAction({ status: 'idle' });
      }
    }

    // A thread Query status is the only available Phase 1E run projection.
    // It can release the awaiting state; a 202 receipt alone cannot.
    const run = pendingRunRef.current;
    const projectedRunThread = run && nextThreads.find((thread) => thread.id === run.threadId);
    if (run && projectedRunThread && projectedRunThread.status !== 'active') {
      pendingRunRef.current = null;
      setCommand({ status: 'idle' });
    }
  }, [adapter]);

  const refresh = useCallback(async () => {
    if (clientMode !== 'live') return;
    const generation = ++lifecycleRef.current;
    try {
      await loadProjection(generation);
      if (generation === lifecycleRef.current) setPhase('ready');
    } catch (error: unknown) {
      if (generation !== lifecycleRef.current) return;
      setPhase((current) => (current === 'ready' ? current : 'error'));
      applyError(error);
    }
  }, [applyError, clientMode, loadProjection]);

  const correctProjection = useCallback(async () => {
    if (clientMode !== 'live') return;
    // A projection correction must not invalidate the generation captured by
    // the active AsyncIterable. Only an explicit refresh/reconnect supersedes
    // in-flight stream work.
    const generation = lifecycleRef.current;
    try {
      await loadProjection(generation);
    } catch (error: unknown) {
      if (generation === lifecycleRef.current) applyError(error);
    }
  }, [applyError, clientMode, loadProjection]);

  const connectAndRefresh = useCallback(async () => {
    if (clientMode !== 'live') return;
    const generation = ++lifecycleRef.current;
    setPhase('connecting');
    setStream((current) => ({ ...current, status: 'connecting', error: undefined }));
    try {
      await adapter.connect();
      if (generation !== lifecycleRef.current) return;
      await loadProjection(generation);
      if (generation !== lifecycleRef.current) return;
      setPhase('ready');
      setStream((current) => ({ ...current, status: 'connected', error: undefined }));
    } catch (error: unknown) {
      if (generation !== lifecycleRef.current) return;
      const detail = applyError(error, false);
      setPhase('error');
      setStream((current) => ({ ...current, status: 'error', error: detail }));
    }
  }, [adapter, applyError, clientMode, loadProjection]);

  const handleFrame = useCallback((frame: Phase1E.SseFrame, threadId: string, generation: number) => {
    if (generation !== lifecycleRef.current || selectedThreadIdRef.current !== threadId) return;
    if (!cursorTrackerRef.current.accept(frame)) return;
    const event = mapSseFrame(frame, pendingRunRef.current?.sessionId || selectedSessionIdRef.current || 'unknown');
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
    if (containsManualReconcile(frame.data)) {
      markManualReconcile('SSE 返回 manual_reconcile_required / outcome_unknown，必须人工核对。');
    }
    if (isApprovalProjectionEvent(event) || frameHasTerminalProjection(event)) void correctProjection();
  }, [correctProjection, markManualReconcile]);

  const consumeRunStream = useCallback(async (
    run: PendingRun,
    replay: boolean,
    generation: number,
  ): Promise<void> => {
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
    if (generation !== lifecycleRef.current) return;

    if (streamResult.receipt) {
      if (streamResult.receipt.recovery === 'manual_reconcile' || containsManualReconcile(streamResult.receipt)) {
        markManualReconcile('Core Receipt 要求 manual_reconcile，必须人工核对。');
      }
      // 202 is only idempotency/command admission evidence, never terminal.
      setCommand({ status: 'awaiting_projection', idempotencyKey: run.idempotencyKey });
      setStream((current) => ({ ...current, status: 'connected', error: undefined }));
      return;
    }

    setCommand({ status: 'awaiting_projection', idempotencyKey: run.idempotencyKey });
    for await (const frame of streamResult.events) {
      handleFrame(frame, run.threadId, generation);
    }
    if (generation === lifecycleRef.current) setStream((current) => ({ ...current, status: 'connected', error: undefined }));
  }, [adapter, handleFrame, markManualReconcile]);

  const replayThenCorrect = useCallback(async () => {
    if (clientMode !== 'live' || phase !== 'ready' || manualReconcileRequired) return;
    const generation = lifecycleRef.current;
    const pendingRun = pendingRunRef.current;
    try {
      if (pendingRun && pendingRun.threadId === selectedThreadIdRef.current) {
        await consumeRunStream(pendingRun, true, generation);
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
      await connectAndRefresh();
      return;
    }
    await replayThenCorrect();
  }, [clientMode, connectAndRefresh, connectionStatus, manualReconcileRequired, phase, replayThenCorrect]);

  useEffect(() => {
    if (clientMode === 'live') {
      void connectAndRefresh();
      return () => {
        lifecycleRef.current += 1;
      };
    }
    lifecycleRef.current += 1;
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
    accumulatorRef.current = emptyEventAccumulator();
    cursorTrackerRef.current = new Phase1E.ScopedCursorTracker();
    pendingRunRef.current = null;
    pendingApprovalRef.current = null;
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
      setStream((current) => ({ ...current, status: 'replaying' }));
      return;
    }
    if (connectionStatus === 'connected' && previous !== 'connected') void replayThenCorrect();
  }, [clientMode, connectionStatus, replayThenCorrect]);

  const createSession = useCallback(async (input: LiveCreateSessionInput) => {
    if (clientMode !== 'live' || phase !== 'ready' || manualReconcileRequired) return undefined;
    if (stream.status === 'replaying' || stream.status === 'error' || connectionStatus !== 'connected') return undefined;
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
      setCommand({ status: 'sending', idempotencyKey });
      const session = await adapter.createSession(input, idempotencyKey);
      setSessions(adapter.listSessions());
      setSelectedSessionId(session.id);
      setCommand({ status: 'awaiting_projection', idempotencyKey });
      await refresh();
      const projectedThreads = await adapter.listThreads();
      const createdThread = projectedThreads.find((thread) => thread.sessionId === session.id);
      if (createdThread && lifecycleRef.current > 0) {
        setSelectedThreadId(createdThread.id);
        setSelectedProjectId(projects.find((project) => project.threadIds.includes(createdThread.id))?.id ?? null);
      }
      createSessionKeyRef.current = null;
      return session;
    } catch (error: unknown) {
      const detail = applyError(error, false);
      setCommand({ status: 'error', error: detail });
      return undefined;
    } finally {
      createSessionDispatchingRef.current = false;
    }
  }, [adapter, applyError, clientMode, connectionStatus, manualReconcileRequired, phase, projects, refresh, stream.status]);

  const sendMessage = useCallback(async (message: string) => {
    const trimmed = message.trim();
    const thread = selectedThreadIdRef.current ? threads.find((item) => item.id === selectedThreadIdRef.current) : undefined;
    if (!trimmed || clientMode !== 'live' || phase !== 'ready' || !thread?.sessionId || !thread.workspaceRef) return;
    if (manualReconcileRequired || command.status !== 'idle' || connectionStatus !== 'connected' || stream.status !== 'connected') return;
    if (commandDispatchingRef.current) return;
    commandDispatchingRef.current = true;
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
      await consumeRunStream(run, false, lifecycleRef.current);
      if (clientMode === 'live') await refresh();
    } catch (error: unknown) {
      const detail = applyError(error, false);
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
      || manualReconcileRequired
      || approval.status !== 'pending'
      || connectionStatus !== 'connected'
      || stream.status !== 'connected'
      || approvalAction.status !== 'idle'
      || approvalDispatchingRef.current
    ) return;
    approvalDispatchingRef.current = true;
    const keyId = `${approval.sessionId}\0${approval.id}`;
    const idempotencyKey = approvalKeysRef.current.get(keyId) || createIdempotencyKey();
    approvalKeysRef.current.set(keyId, idempotencyKey);
    pendingApprovalRef.current = { sessionId: approval.sessionId, approvalId: approval.id };
    setApprovalAction({ status: 'sending', idempotencyKey });
    try {
      await adapter.submitApproval(approval, decision === 'approve', idempotencyKey);
      // Do not clear pending locally. Only the next scoped Query can confirm
      // that the target approval is no longer pending.
      setApprovalAction({ status: 'awaiting_projection', idempotencyKey });
      await refresh();
    } catch (error: unknown) {
      const detail = applyError(error, false);
      setApprovalAction({ status: 'error', error: detail, idempotencyKey });
    } finally {
      approvalDispatchingRef.current = false;
    }
  }, [adapter, approvalAction.status, applyError, clientMode, connectionStatus, manualReconcileRequired, phase, refresh, stream.status]);

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
    && !manualReconcileRequired;
  const createSessionUnavailableReason = manualReconcileRequired
    ? '需要人工核对，不能创建新命令。'
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
