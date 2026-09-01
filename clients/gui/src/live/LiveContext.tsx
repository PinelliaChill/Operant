import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import type {
  AnyOperantEvent,
  ApprovalCard,
  ApprovalDecision,
  CanonicalAgentMessage,
  Session,
  Thread,
} from '@operant/sdk';
import { useOperant } from '../context/ClientContext';
import {
  LiveClientAdapter,
  eventPayload,
  normalizeLiveError,
} from './liveAdapter';
import {
  EventAccumulator,
  LiveActionState,
  LiveCommandState,
  LiveError,
  LiveProjectProjection,
  LiveStreamState,
  LiveWorkspaceFile,
  emptyEventAccumulator,
  containsManualReconcile,
  isApprovalProjectionEvent,
  isTerminalEvent,
  reduceEvent,
  threadNeedsManualReconcile,
} from './liveState';

export type LiveProjectionPhase = 'idle' | 'connecting' | 'ready' | 'error';

export interface LiveContextValue {
  phase: LiveProjectionPhase;
  adapter: LiveClientAdapter;
  projects: LiveProjectProjection[];
  threads: Thread[];
  sessions: Session[];
  approvals: ApprovalCard[];
  selectedProjectId: string | null;
  selectedThreadId: string | null;
  selectedSessionId: string | null;
  selectedThread: Thread | undefined;
  selectedSession: Session | undefined;
  messages: CanonicalAgentMessage[];
  files: LiveWorkspaceFile[];
  filesWorkspaceId: string | null;
  stream: LiveStreamState;
  projectionStale: boolean;
  lastError?: LiveError;
  command: LiveCommandState;
  approvalAction: LiveActionState;
  manualReconcileRequired: boolean;
  selectProject: (projectId: string | null) => void;
  selectThread: (threadId: string | null) => void;
  selectSession: (sessionId: string | null) => void;
  refresh: () => Promise<void>;
  reconnect: () => Promise<void>;
  createSession: (options?: Parameters<LiveClientAdapter['createSession']>[0]) => Promise<Session | undefined>;
  sendMessage: (message: string) => Promise<void>;
  cancelSession: () => Promise<void>;
  decideApproval: (approval: ApprovalCard, decision: ApprovalDecision['decision']) => Promise<void>;
  loadFiles: (workspaceId: string, relativePath?: string) => Promise<void>;
  clearError: () => void;
}

const LiveContext = createContext<LiveContextValue | null>(null);

function initialStream(): LiveStreamState {
  return {
    status: 'idle',
    cursor: { sequence: 0 },
    events: [],
  };
}

function projectMatchesThread(project: LiveProjectProjection, thread: Thread): boolean {
  return project.threadIds.includes(thread.id) || project.workspaceRef === thread.workspace;
}

function readableThreadTitle(thread: Thread | undefined): string {
  return thread?.title || thread?.id || '未命名 Thread';
}

export const LiveProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { client, clientMode, connectionStatus } = useOperant();
  const adapter = useMemo(() => new LiveClientAdapter(client), [client]);
  const [phase, setPhase] = useState<LiveProjectionPhase>('idle');
  const [projects, setProjects] = useState<LiveProjectProjection[]>([]);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [approvals, setApprovals] = useState<ApprovalCard[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(null);
  const [selectedThreadId, setSelectedThreadId] = useState<string | null>(null);
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<CanonicalAgentMessage[]>([]);
  const [files, setFiles] = useState<LiveWorkspaceFile[]>([]);
  const [filesWorkspaceId, setFilesWorkspaceId] = useState<string | null>(null);
  const [stream, setStream] = useState<LiveStreamState>(initialStream);
  const [projectionStale, setProjectionStale] = useState(false);
  const [lastError, setLastError] = useState<LiveError | undefined>();
  const [command, setCommand] = useState<LiveCommandState>({ status: 'idle' });
  const [approvalAction, setApprovalAction] = useState<LiveActionState>({ status: 'idle' });
  const [refreshTick, setRefreshTick] = useState(0);
  const lifecycleRef = useRef(0);
  const accumulatorRef = useRef<EventAccumulator>(emptyEventAccumulator());
  const streamUnsubscribeRef = useRef<(() => void) | undefined>(undefined);
  const runUnsubscribeRef = useRef<(() => void) | undefined>(undefined);
  const selectedThreadIdRef = useRef<string | null>(null);
  const selectedSessionIdRef = useRef<string | null>(null);
  const previousConnectionRef = useRef(connectionStatus);

  selectedThreadIdRef.current = selectedThreadId;
  selectedSessionIdRef.current = selectedSessionId;

  const selectedThread = threads.find((thread) => thread.id === selectedThreadId);
  const selectedSession = sessions.find((session) => session.id === selectedSessionId);
  const manualReconcileRequired = threadNeedsManualReconcile(selectedThread) || stream.events.some((event) => containsManualReconcile(eventPayload(event)));

  const applyError = useCallback((error: unknown, stale = true) => {
    const normalized = normalizeLiveError(error).detail;
    setLastError(normalized);
    setProjectionStale(stale);
    return normalized;
  }, []);

  const selectProject = useCallback((projectId: string | null) => {
    setSelectedProjectId(projectId);
    if (!projectId) return;
    const project = projects.find((item) => item.id === projectId);
    const thread = threads.find((item) => project && projectMatchesThread(project, item));
    if (thread) {
      setSelectedThreadId(thread.id);
      setSelectedSessionId(thread.session_id);
    } else {
      setSelectedThreadId(null);
      setSelectedSessionId(null);
    }
  }, [projects, threads]);

  const selectThread = useCallback((threadId: string | null) => {
    setSelectedThreadId(threadId);
    const thread = threads.find((item) => item.id === threadId);
    setSelectedSessionId(thread?.session_id ?? null);
  }, [threads]);

  const selectSession = useCallback((sessionId: string | null) => {
    setSelectedSessionId(sessionId);
    const thread = threads.find((item) => item.session_id === sessionId);
    setSelectedThreadId(thread?.id ?? null);
  }, [threads]);

  const loadThread = useCallback(async (threadId: string | null, generation: number) => {
    if (!threadId) {
      setMessages([]);
      return;
    }
    try {
      const nextMessages = await adapter.listThreadMessages(threadId);
      if (generation !== lifecycleRef.current) return;
      setMessages(nextMessages);
    } catch (error: unknown) {
      if (generation !== lifecycleRef.current) return;
      applyError(error);
    }
  }, [adapter, applyError]);

  const loadProjection = useCallback(async (generation: number) => {
    const [nextProjects, nextThreads, nextSessions, nextApprovals] = await Promise.all([
      adapter.listProjects(),
      adapter.listThreads(),
      adapter.listSessions(),
      adapter.listPendingApprovals(),
    ]);
    if (generation !== lifecycleRef.current) return;

    setProjects(nextProjects);
    setThreads(nextThreads);
    setSessions(nextSessions);
    setApprovals(nextApprovals);
    setProjectionStale(false);
    setLastError(undefined);
    setCommand((current) => current.status === 'awaiting_projection' ? { status: 'idle' } : current);
    setApprovalAction((current) => current.status === 'awaiting_projection' ? { status: 'idle' } : current);

    const existingThread = nextThreads.find((thread) => thread.id === selectedThreadIdRef.current);
    const firstThread = existingThread || nextThreads[0];
    const nextThreadId = firstThread?.id ?? null;
    setSelectedThreadId(nextThreadId);
    setSelectedSessionId(firstThread?.session_id ?? null);
    const project = nextProjects.find((item) => firstThread && projectMatchesThread(item, firstThread));
    setSelectedProjectId(project?.id ?? nextProjects[0]?.id ?? null);
    await loadThread(nextThreadId, generation);
  }, [adapter, loadThread]);

  const refresh = useCallback(async () => {
    if (clientMode !== 'live') return;
    const generation = ++lifecycleRef.current;
    setPhase((current) => (current === 'ready' ? current : 'connecting'));
    try {
      await loadProjection(generation);
      if (generation === lifecycleRef.current) setPhase('ready');
    } catch (error: unknown) {
      if (generation !== lifecycleRef.current) return;
      setPhase((current) => (current === 'ready' ? current : 'error'));
      applyError(error);
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

  const handleEvent = useCallback((event: AnyOperantEvent) => {
    const activeThreadId = selectedThreadIdRef.current;
    if (activeThreadId && event.thread_id && event.thread_id !== activeThreadId) return;
    const activeSessionId = selectedSessionIdRef.current;
    if (activeSessionId && event.session_id && event.session_id !== activeSessionId) return;
    const scope = event.thread_id || event.session_id || activeThreadId || 'global';
    const next = reduceEvent(accumulatorRef.current, scope, event);
    if (next === accumulatorRef.current) return;
    accumulatorRef.current = next;
    setStream(() => ({
      status: 'connected',
      cursor: next.cursor,
      events: next.events,
      lastEvent: event,
      error: undefined,
    }));
    if (isApprovalProjectionEvent(event) || isTerminalEvent(event)) {
      // Events are evidence, not final state.  Always correct from Query
      // Projection after the event is observed.
      setRefreshTick((value) => value + 1);
    }
  }, []);

  const replayThenCorrect = useCallback(async () => {
    if (clientMode !== 'live' || !selectedThreadId || phase !== 'ready') return;
    setStream((current) => ({ ...current, status: 'replaying', error: undefined }));
    const cursor = accumulatorRef.current.cursor;
    try {
      streamUnsubscribeRef.current?.();
      streamUnsubscribeRef.current = adapter.subscribeThreadEvents(selectedThreadId, cursor, handleEvent);
      // subscribeEvents is intentionally callback-based in the generated SDK;
      // yielding once lets committed replay callbacks run before projection
      // correction.  Query remains authoritative if no replay is available.
      await Promise.resolve();
      await refresh();
      setStream((current) => ({ ...current, status: 'connected', error: undefined }));
    } catch (error: unknown) {
      const detail = applyError(error);
      setStream((current) => ({ ...current, status: 'error', error: detail }));
    }
  }, [adapter, applyError, clientMode, handleEvent, phase, refresh, selectedThreadId]);

  const reconnect = useCallback(async () => {
    if (clientMode !== 'live') return;
    if (phase !== 'ready') {
      await connectAndRefresh();
      return;
    }
    await replayThenCorrect();
  }, [clientMode, connectAndRefresh, phase, replayThenCorrect]);

  useEffect(() => {
    if (clientMode === 'live') {
      void connectAndRefresh();
    } else {
      lifecycleRef.current += 1;
      setPhase('idle');
      setProjects([]);
      setThreads([]);
      setSessions([]);
      setApprovals([]);
      setMessages([]);
      setFiles([]);
      setSelectedProjectId(null);
      setSelectedThreadId(null);
      setSelectedSessionId(null);
      accumulatorRef.current = emptyEventAccumulator();
      setStream(initialStream());
      setLastError(undefined);
      setProjectionStale(false);
      streamUnsubscribeRef.current?.();
      streamUnsubscribeRef.current = undefined;
      runUnsubscribeRef.current?.();
      runUnsubscribeRef.current = undefined;
    }
    return () => {
      streamUnsubscribeRef.current?.();
      streamUnsubscribeRef.current = undefined;
      runUnsubscribeRef.current?.();
      runUnsubscribeRef.current = undefined;
    };
  }, [clientMode, connectAndRefresh]);

  useEffect(() => {
    if (clientMode !== 'live' || phase !== 'ready' || !selectedThreadId) return;
    accumulatorRef.current = emptyEventAccumulator();
    setStream((current) => ({
      ...current,
      status: 'connecting',
      cursor: { sequence: 0 },
      events: [],
      lastEvent: undefined,
      error: undefined,
    }));
    void loadThread(selectedThreadId, lifecycleRef.current);
    streamUnsubscribeRef.current?.();
    streamUnsubscribeRef.current = adapter.subscribeThreadEvents(
      selectedThreadId,
      accumulatorRef.current.cursor,
      handleEvent
    );
    return () => {
      streamUnsubscribeRef.current?.();
      streamUnsubscribeRef.current = undefined;
    };
  }, [adapter, clientMode, handleEvent, loadThread, phase, selectedThreadId]);

  useEffect(() => {
    if (refreshTick === 0 || clientMode !== 'live') return;
    void refresh();
  }, [clientMode, refresh, refreshTick]);

  useEffect(() => {
    const previous = previousConnectionRef.current;
    previousConnectionRef.current = connectionStatus;
    if (clientMode !== 'live') return;
    if (connectionStatus === 'disconnected' || connectionStatus === 'reconnecting') {
      setStream((current) => ({ ...current, status: 'replaying' }));
      return;
    }
    if (connectionStatus === 'connected' && previous !== 'connected') {
      void replayThenCorrect();
    }
  }, [clientMode, connectionStatus, replayThenCorrect]);

  const createSession = useCallback(async (options: Parameters<LiveClientAdapter['createSession']>[0] = {}) => {
    if (clientMode !== 'live') return undefined;
    try {
      const session = await adapter.createSession(options);
      setSelectedSessionId(session.id);
      setCommand({ status: 'awaiting_projection' });
      await refresh();
      const projectedThreads = await adapter.listThreads();
      const createdThread = projectedThreads.find((thread) => thread.session_id === session.id);
      if (createdThread) setSelectedThreadId(createdThread.id);
      return session;
    } catch (error: unknown) {
      const detail = applyError(error, false);
      setCommand({ status: 'error', error: detail });
      return undefined;
    }
  }, [adapter, applyError, clientMode, refresh]);

  const sendMessage = useCallback(async (message: string) => {
    const trimmed = message.trim();
    if (!trimmed || clientMode !== 'live' || !selectedThread) return;
    setCommand({ status: 'sending' });
    runUnsubscribeRef.current?.();
    try {
      runUnsubscribeRef.current = adapter.runSessionStream(
        selectedThread.session_id,
        trimmed,
        selectedThread.workspace,
        handleEvent
      );
      // Network acceptance is not a run state.  Only Projection/Events can
      // move the UI beyond this awaiting state.
      setCommand({ status: 'awaiting_projection' });
      await Promise.resolve();
    } catch (error: unknown) {
      const detail = applyError(error, false);
      setCommand({ status: 'error', error: detail });
    }
  }, [adapter, applyError, clientMode, handleEvent, selectedThread]);

  const cancelSession = useCallback(async () => {
    if (clientMode !== 'live' || !selectedSession) return;
    setCommand({ status: 'sending' });
    try {
      await adapter.cancelSession(selectedSession.id);
      setCommand({ status: 'awaiting_projection' });
      await refresh();
    } catch (error: unknown) {
      const detail = applyError(error, false);
      setCommand({ status: 'error', error: detail });
    }
  }, [adapter, applyError, clientMode, refresh, selectedSession]);

  const decideApproval = useCallback(async (approval: ApprovalCard, decision: ApprovalDecision['decision']) => {
    if (clientMode !== 'live') return;
    setApprovalAction({ status: 'sending' });
    try {
      await adapter.submitApproval(approval.session_id, approval.id, {
        approval_id: approval.id,
        decision,
        decided_by: 'gui',
        decided_at: new Date().toISOString(),
      });
      // A command response is not a decision projection.  Keep the action in
      // awaiting state until the next authoritative approval query arrives.
      setApprovalAction({ status: 'awaiting_projection' });
      await refresh();
    } catch (error: unknown) {
      const detail = applyError(error, false);
      setApprovalAction({ status: 'error', error: detail });
    }
  }, [adapter, applyError, clientMode, refresh]);

  const loadFiles = useCallback(async (workspaceId: string, relativePath = '') => {
    if (clientMode !== 'live') return;
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
  }, [adapter, applyError, clientMode]);

  const clearError = useCallback(() => {
    setLastError(undefined);
    setStream((current) => ({ ...current, error: undefined }));
    setCommand((current) => current.status === 'error' ? { status: 'idle' } : current);
    setApprovalAction((current) => current.status === 'error' ? { status: 'idle' } : current);
  }, []);

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
    selectProject,
    selectThread,
    selectSession,
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
    cancelSession,
    clearError,
    command,
    createSession,
    decideApproval,
    files,
    filesWorkspaceId,
    lastError,
    loadFiles,
    manualReconcileRequired,
    messages,
    phase,
    projects,
    projectionStale,
    reconnect,
    refresh,
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

export function liveThreadTitle(thread: Thread | undefined): string {
  return readableThreadTitle(thread);
}
