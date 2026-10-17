import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { OnboardingClient, type ConversationInitialization, type ConversationMetadata, type ConversationStart, type SetupState } from '../../../../sdk/typescript-client/onboarding';
import { useOperant } from '../context/ClientContext';
import { currentBrowserOrigin } from '../lib/liveBaseUrl';
import { nativeCoreTransport } from '../lib/nativeCoreTransport';
import { useLive } from './LiveContext';
import { metadataRefreshKey } from './metadataRefreshKey';
import { readConversationMetadata, type MetadataReadResult } from './metadataRead';
import type { PendingConversationSend } from './pendingConversationSend';
import { confirmedCreateMetadata, createRequestStorageKey, decodeCreateRequest, encodeCreateRequest, type PendingCreateRequest, isConversationInitialization, mergeConversationMetadata, renameIsConfirmed } from './createRequestRecovery';
import { requestErrorCopy } from '../lib/requestErrorCopy';
import { isWriteOutcomeUnknown } from '../lib/writeOutcome';
import { ConversationDrafts, draftScope, draftThreadFromPath, draftThreadForSelection, isNewConversationDraft, restoreConfirmedConversationDraft, type DraftSnapshot } from './conversationDrafts';

interface OnboardingContextValue {
  client: OnboardingClient;
  setup: SetupState | null;
  setupLoading: boolean;
  setupError: string;
  metadata: Record<string, ConversationMetadata>;
  draft: string;
  setDraft: React.Dispatch<React.SetStateAction<string>>;
  getDraftRevision: () => number;
  captureDraft: () => DraftSnapshot;
  getDraftScope: () => string;
  draftReturnPath: string;
  acceptDraft: (snapshot: DraftSnapshot) => void;
  moveDraftToThread: (snapshot: DraftSnapshot, threadId: string) => DraftSnapshot;
  draftWarning: string;
  recoveredDraftConflict: string;
  getRouteRevision: () => number;
  noteRouteChange: (pathname?: string) => void;
  pendingSend: PendingConversationSend | null;
  setPendingSend: (value: PendingConversationSend | null) => void;
  createBusy: boolean;
  createOutcomeUnknown: boolean;
  renameOutcomeUnknown: boolean;
  createError: string;
  metadataWarning: string;
  recoveredConversationId: string | null;
  refreshSetup: () => Promise<SetupState | null>;
  refreshMetadata: () => Promise<MetadataReadResult['status']>;
  bootstrap: (modelProfileId?: string) => Promise<boolean>;
  initializeConversation: (input?: ConversationStart) => Promise<ConversationInitialization | null>;
  renameConversation: (threadId: string, title: string) => Promise<boolean>;
  clearCreateUncertainty: () => Promise<void>;
  dismissRecoveredCreateNotice: (threadId: string) => void;
}

const Context = createContext<OnboardingContextValue | null>(null);
const message = requestErrorCopy;

export const OnboardingProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { clientMode, connectionStatus } = useOperant();
  const live = useLive();
  const client = useMemo(() => new OnboardingClient(currentBrowserOrigin(), nativeCoreTransport), []);
  const createStorageKey = useMemo(() => createRequestStorageKey(currentBrowserOrigin()), []);
  const [setup, setSetup] = useState<SetupState | null>(null);
  const [setupLoading, setSetupLoading] = useState(false);
  const [setupError, setSetupError] = useState('');
  const [metadata, setMetadata] = useState<Record<string, ConversationMetadata>>({});
  const [draftStore] = useState(() => new ConversationDrafts(() => localStorage));
  const [, renderDraft] = useState(0);
  const [hash, setHash] = useState(() => window.location.hash);
  useEffect(() => {
    const changed = () => setHash(window.location.hash);
    window.addEventListener('hashchange', changed);
    return () => window.removeEventListener('hashchange', changed);
  }, []);
  const chatPath = hash.replace(/^#/, '').split('?')[0];
  const routeThread = draftThreadFromPath(chatPath);
  const lastDraftScope = useRef(draftScope(currentBrowserOrigin(), '', null));
  const lastDraftPath = useRef('/chat');
  if (chatPath === '/chat' || routeThread) {
    lastDraftPath.current = chatPath;
    lastDraftScope.current = draftScope(currentBrowserOrigin(), live.selectedProjectId || setup?.default_workspace_id || '', draftThreadForSelection(chatPath, live.selectedThreadId));
  }
  const scope = lastDraftScope.current;
  const draftReturnPath = lastDraftPath.current;
  const getDraftScope = useCallback(() => lastDraftScope.current, []);
  const draft = draftStore.read(scope).text;
  const draftWarning = draftStore.warning;
  const setDraft = useCallback<React.Dispatch<React.SetStateAction<string>>>((value) => {
    const current = draftStore.read(scope);
    draftStore.write(scope, typeof value === 'function' ? value(current.text) : value);
    renderDraft((revision) => revision + 1);
  }, [draftStore, scope]);
  const getDraftRevision = useCallback(() => draftStore.read(scope).revision, [draftStore, scope]);
  const captureDraft = useCallback(() => draftStore.read(scope), [draftStore, scope]);
  const acceptDraft = useCallback((snapshot: DraftSnapshot) => {
    draftStore.accept(snapshot); renderDraft((revision) => revision + 1);
  }, [draftStore]);
  const moveDraftToThread = useCallback((snapshot: DraftSnapshot, threadId: string) => {
    const moved = draftStore.move(snapshot, draftScope(currentBrowserOrigin(), '', threadId));
    renderDraft((revision) => revision + 1);
    return moved;
  }, [draftStore]);
  const routeRevision = useRef(0);
  const getRouteRevision = useCallback(() => routeRevision.current, []);
  const noteRouteChange = useCallback((pathname?: string) => {
    routeRevision.current += 1;
    if (pathname) setHash(`#${pathname}`);
  }, []);
  const [pendingSend, setPendingSend] = useState<PendingConversationSend | null>(null);
  const [createBusy, setCreateBusy] = useState(false);
  const [pendingCreateRequest, setPendingCreateRequest] = useState<PendingCreateRequest | null>(() => {
    try { return decodeCreateRequest(localStorage.getItem(createStorageKey), currentBrowserOrigin()); } catch { return null; }
  });
  const pendingCreateKey = pendingCreateRequest?.requestId || null;
  const [recoveredDraftConflict, setRecoveredDraftConflict] = useState('');
  const [createOutcomeUnknown, setCreateOutcomeUnknown] = useState(Boolean(pendingCreateKey));
  const [createError, setCreateError] = useState(pendingCreateKey ? '上次新建对话的结果尚未确认。请刷新并核对原请求。' : '');
  const [metadataWarning, setMetadataWarning] = useState('');
  const [recoveredConversationId, setRecoveredConversationId] = useState<string | null>(null);
  const createInFlight = useRef(false);
  const metadataReadEpoch = useRef(0);
  const actionInFlight = useRef(false);
  const renameInFlight = useRef(false);
  const [renameOutcomeUnknown, setRenameOutcomeUnknown] = useState(false);
  const [renamePendingTarget, setRenamePendingTarget] = useState<{ threadId: string; title: string } | null>(null);
  const storeCreateKey = useCallback((key: string | null, draftScope?: string) => {
    const request = key ? { requestId: key, ...(draftScope ? { draftScope } : {}) } : null;
    if (request) localStorage.setItem(createStorageKey, encodeCreateRequest(request));
    else localStorage.removeItem(createStorageKey);
    setPendingCreateRequest(request);
  }, [createStorageKey]);
  const showMetadataWarning = useCallback((warning: string) => {
    setMetadataWarning(warning);
  }, []);
  const clearMetadataWarning = useCallback(() => {
    setMetadataWarning('');
  }, []);

  const setupReadEpoch = useRef(0);
  const refreshSetup = useCallback(async () => {
    const epoch = ++setupReadEpoch.current;
    if (clientMode !== 'live' || connectionStatus !== 'connected') return null;
    setSetupLoading(true);
    try {
      const next = await client.getSetupState();
      if (epoch !== setupReadEpoch.current) return null;
      setSetup(next);
      setSetupError('');
      return next;
    } catch (error: unknown) { if (epoch === setupReadEpoch.current) setSetupError(message(error)); return null; }
    finally { if (epoch === setupReadEpoch.current) setSetupLoading(false); }
  }, [client, clientMode, connectionStatus]);
  const refreshMetadata = useCallback(async () => {
    if (clientMode !== 'live' || connectionStatus !== 'connected') return 'failed' as const;
    const epoch = ++metadataReadEpoch.current;
    const result = await readConversationMetadata(client, live.selectedThreadId, () => epoch === metadataReadEpoch.current);
    if (result.status === 'superseded') return 'superseded';
    if (result.items) setMetadata((current) => mergeConversationMetadata(current, result.items!));
    if (result.status === 'failed') {
      showMetadataWarning(`${result.scope === 'selected' ? '当前对话标题' : '标题'}暂时无法读取，请刷新核对。`);
    } else clearMetadataWarning();
    return result.status;
  }, [client, clientMode, connectionStatus, live.selectedThreadId, showMetadataWarning, clearMetadataWarning]);

  const titleRefreshKey = metadataRefreshKey(live.threads, live.selectedThreadId, live.stream.events);

  useEffect(() => {
    if (clientMode !== 'live') return;
    if (connectionStatus === 'connected') void refreshSetup();
    else { ++setupReadEpoch.current; setSetup(null); setSetupError(''); setSetupLoading(false); }
  }, [clientMode, connectionStatus, refreshSetup]);
  useEffect(() => {
    if (clientMode === 'live' && connectionStatus === 'connected' && live.phase === 'ready') void refreshMetadata();
  }, [clientMode, connectionStatus, live.phase, titleRefreshKey, refreshMetadata]);

  const bootstrap = useCallback(async (modelProfileId?: string) => {
    if (actionInFlight.current) return false;
    actionInFlight.current = true;
    ++setupReadEpoch.current;
    setSetupLoading(false);
    try {
      const next = await client.bootstrapSetup(modelProfileId ? { model_profile_id: modelProfileId } : {}, { idempotencyKey: crypto.randomUUID() });
      setSetup(next); setSetupError('');
      await live.refresh();
      return next.ready;
    } catch (error: unknown) { setSetupError(message(error)); return false; }
    finally { actionInFlight.current = false; }
  }, [client, live]);

  const initializeConversation = useCallback(async (input: ConversationStart = {}) => {
    if (createInFlight.current || createOutcomeUnknown || connectionStatus !== 'connected') return null;
    const workspace = input.workspace_id || (input.thread_id ? live.threads.find((thread) => thread.id === input.thread_id)?.workspaceRef : live.selectedProjectId || setup?.default_workspace_id);
    const archived = live.archivedProjectIds.some((id) => id === workspace || live.projects.find((project) => project.id === id)?.workspaceRef === workspace);
    if (archived) { setCreateError('此项目已归档。请选择活跃项目后新建对话，已有文件和记录仍可查看。'); return null; }
    createInFlight.current = true; setCreateBusy(true); setCreateError('');
    setRecoveredConversationId(null);
    const requestKey = crypto.randomUUID();
    let stored = false;
    try {
      const source = captureDraft();
      storeCreateKey(requestKey, isNewConversationDraft(source) ? source.scope : undefined);
      stored = true;
      const result = await client.initializeConversation(input, { idempotencyKey: requestKey });
      if (!isConversationInitialization(result)) throw new Error('创建对话的响应缺少必要信息，请核对原请求。');
      storeCreateKey(null);
      setCreateOutcomeUnknown(false);
      try {
        await live.refresh();
        if (await refreshMetadata() === 'failed') showMetadataWarning('对话已创建，但标题暂时无法读取。请刷新对话列表核对。');
      } catch { showMetadataWarning('对话已创建，但列表暂时无法同步。请刷新后打开。'); }
      return result;
    } catch (error: unknown) {
      if (!stored) {
        setCreateError('无法保存创建请求的核对标识，已停止新建。请检查本地存储权限后重试。');
      } else if (isWriteOutcomeUnknown(error)) {
        setCreateOutcomeUnknown(true);
        setCreateError('没有收到创建结果。请刷新并核对原请求，暂时不要重试。');
      } else {
        try { storeCreateKey(null); } catch { /* A stale key remains a safe recovery barrier. */ }
        setCreateError(message(error));
      }
      return null;
    } finally { createInFlight.current = false; setCreateBusy(false); }
  }, [client, connectionStatus, createOutcomeUnknown, live, refreshMetadata, showMetadataWarning, storeCreateKey, setup?.default_workspace_id, captureDraft]);

  const renameConversation = useCallback(async (threadId: string, title: string) => {
    if (renameInFlight.current || renameOutcomeUnknown) return false;
    renameInFlight.current = true;
    const current = metadata[threadId];
    try {
      const next = await client.renameConversation(threadId, { title, ...(current ? { expected_revision: current.revision } : {}) }, { idempotencyKey: crypto.randomUUID() });
      setMetadata((items) => ({ ...items, [threadId]: next }));
      setRenamePendingTarget(null); setRenameOutcomeUnknown(false);
      return true;
    } catch (error: unknown) {
      if (isWriteOutcomeUnknown(error)) {
        setCreateError('改名结果尚未确认。请刷新标题核对原请求，暂时不要再次改名。');
        setRenameOutcomeUnknown(true); setRenamePendingTarget({ threadId, title });
      } else setCreateError(message(error));
      return false;
    } finally { renameInFlight.current = false; }
  }, [client, metadata, renameOutcomeUnknown]);

  const clearCreateUncertainty = useCallback(async () => {
    if (connectionStatus !== 'connected') { setCreateError('连接已断开。恢复连接后再刷新核对原请求。'); return; }
    try {
      if (pendingCreateKey) {
        const page = await client.listConversationMetadata({ requestId: pendingCreateKey });
        const confirmed = confirmedCreateMetadata(page.items);
        if (!confirmed) {
          setCreateError('尚未查到原请求的对话。请稍后再刷新，或到运行记录中人工核对；不要再次创建。');
          return;
        }
        setMetadata((items) => mergeConversationMetadata(items, [confirmed]));
        setRecoveredConversationId(confirmed.thread_id);
        const conflict = restoreConfirmedConversationDraft(draftStore, pendingCreateRequest?.draftScope, draftScope(currentBrowserOrigin(), '', confirmed.thread_id));
        renderDraft((revision) => revision + 1);
        setRecoveredDraftConflict(conflict);
        if (conflict) {
          setCreateOutcomeUnknown(true);
          setCreateError('已找到原请求的对话，但该对话已有草稿。原草稿保留在下方，请先复制保存或合并，再清空目标输入框并刷新核对。没有自动发送。');
          await live.refresh();
          return;
        }
        storeCreateKey(null);
        setCreateOutcomeUnknown(false);
        setCreateError('已找到先前创建的对话，可直接打开。');
        try {
          await live.refresh();
          if (await refreshMetadata() === 'failed') showMetadataWarning('已确认创建，但对话列表暂时无法同步。可直接打开该对话。');
        } catch { showMetadataWarning('已确认创建，但对话列表暂时无法同步。可直接打开该对话。'); }
      }
      if (renameOutcomeUnknown && renamePendingTarget) {
        const current = await client.getConversationMetadata(renamePendingTarget.threadId);
        setMetadata((items) => mergeConversationMetadata(items, [current]));
        if (!renameIsConfirmed(current, renamePendingTarget.title)) {
          setCreateError('改名结果尚未确认。请查看当前标题并继续人工核对，暂不要再次改名。');
          return;
        }
        setRenamePendingTarget(null); setRenameOutcomeUnknown(false);
        if (!pendingCreateKey) setCreateError('标题已按原请求更新。');
      }
    } catch {
      setCreateError('原请求暂时无法核对。请恢复连接后刷新，暂时不要重复提交。');
    }
  }, [client, connectionStatus, live, pendingCreateKey, refreshMetadata, renameOutcomeUnknown, renamePendingTarget, showMetadataWarning, storeCreateKey, pendingCreateRequest, draftStore]);

  const dismissRecoveredCreateNotice = useCallback((threadId: string) => {
    if (createOutcomeUnknown || pendingCreateKey || recoveredConversationId !== threadId) return;
    setRecoveredConversationId(null);
    setCreateError((current) => current === '已找到先前创建的对话，可直接打开。' ? '' : current);
  }, [createOutcomeUnknown, pendingCreateKey, recoveredConversationId]);

  return <Context.Provider value={{ client, setup, setupLoading, setupError, metadata, draft, setDraft, getDraftRevision, captureDraft, getDraftScope, draftReturnPath, acceptDraft, moveDraftToThread, draftWarning, recoveredDraftConflict, getRouteRevision, noteRouteChange, pendingSend, setPendingSend, createBusy, createOutcomeUnknown, renameOutcomeUnknown, createError, metadataWarning, recoveredConversationId, refreshSetup, refreshMetadata, bootstrap, initializeConversation, renameConversation, clearCreateUncertainty, dismissRecoveredCreateNotice }}>{children}</Context.Provider>;
};

export function useOnboarding(): OnboardingContextValue {
  const value = useContext(Context);
  if (!value) throw new Error('OnboardingProvider 未挂载');
  return value;
}
