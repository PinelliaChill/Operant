import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { OnboardingClient, OnboardingError, type ConversationInitialization, type ConversationMetadata, type ConversationStart, type SetupState } from '../../../../sdk/typescript-client/onboarding';
import { useOperant } from '../context/ClientContext';
import { currentBrowserOrigin } from '../lib/liveBaseUrl';
import { useLive } from './LiveContext';
import { metadataRefreshKey } from './metadataRefreshKey';
import type { PendingConversationSend } from './pendingConversationSend';
import { confirmedCreateMetadata, createRequestStorageKey, mergeConversationMetadata, renameIsConfirmed } from './createRequestRecovery';

interface OnboardingContextValue {
  client: OnboardingClient;
  setup: SetupState | null;
  setupLoading: boolean;
  setupError: string;
  metadata: Record<string, ConversationMetadata>;
  draft: string;
  setDraft: React.Dispatch<React.SetStateAction<string>>;
  getDraftRevision: () => number;
  getRouteRevision: () => number;
  noteRouteChange: () => void;
  pendingSend: PendingConversationSend | null;
  setPendingSend: (value: PendingConversationSend | null) => void;
  createBusy: boolean;
  createOutcomeUnknown: boolean;
  renameOutcomeUnknown: boolean;
  createError: string;
  recoveredConversationId: string | null;
  refreshSetup: () => Promise<SetupState | null>;
  refreshMetadata: () => Promise<boolean>;
  bootstrap: (modelProfileId?: string) => Promise<boolean>;
  initializeConversation: (input?: ConversationStart) => Promise<ConversationInitialization | null>;
  renameConversation: (threadId: string, title: string) => Promise<boolean>;
  clearCreateUncertainty: () => Promise<void>;
}

const Context = createContext<OnboardingContextValue | null>(null);
const message = (error: unknown) => error instanceof Error ? error.message : '请求失败，请刷新后重试。';
const uncertain = (error: unknown) => !(error instanceof OnboardingError) || error.code === 'transport_unavailable' || error.recovery === 'manual_reconcile' || error.recovery === 'retry_same_idempotency_key';

export const OnboardingProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { clientMode, connectionStatus } = useOperant();
  const live = useLive();
  const client = useMemo(() => new OnboardingClient(currentBrowserOrigin()), []);
  const createStorageKey = useMemo(() => createRequestStorageKey(currentBrowserOrigin()), []);
  const [setup, setSetup] = useState<SetupState | null>(null);
  const [setupLoading, setSetupLoading] = useState(false);
  const [setupError, setSetupError] = useState('');
  const [metadata, setMetadata] = useState<Record<string, ConversationMetadata>>({});
  const [draft, setDraftState] = useState('');
  const draftRevision = useRef(0);
  const setDraft = useCallback<React.Dispatch<React.SetStateAction<string>>>((value) => {
    draftRevision.current += 1;
    setDraftState(value);
  }, []);
  const getDraftRevision = useCallback(() => draftRevision.current, []);
  const routeRevision = useRef(0);
  const getRouteRevision = useCallback(() => routeRevision.current, []);
  const noteRouteChange = useCallback(() => { routeRevision.current += 1; }, []);
  const [pendingSend, setPendingSend] = useState<PendingConversationSend | null>(null);
  const [createBusy, setCreateBusy] = useState(false);
  const [pendingCreateKey, setPendingCreateKey] = useState<string | null>(() => {
    try { return localStorage.getItem(createStorageKey) || null; } catch { return null; }
  });
  const [createOutcomeUnknown, setCreateOutcomeUnknown] = useState(Boolean(pendingCreateKey));
  const [createError, setCreateError] = useState(pendingCreateKey ? '上次新建对话的结果尚未确认。请刷新并核对原请求。' : '');
  const [recoveredConversationId, setRecoveredConversationId] = useState<string | null>(null);
  const createInFlight = useRef(false);
  const metadataReadEpoch = useRef(0);
  const actionInFlight = useRef(false);
  const renameInFlight = useRef(false);
  const [renameOutcomeUnknown, setRenameOutcomeUnknown] = useState(false);
  const [renamePendingTarget, setRenamePendingTarget] = useState<{ threadId: string; title: string } | null>(null);
  const storeCreateKey = useCallback((key: string | null) => {
    if (key) localStorage.setItem(createStorageKey, key);
    else localStorage.removeItem(createStorageKey);
    setPendingCreateKey(key);
  }, [createStorageKey]);

  const refreshSetup = useCallback(async () => {
    if (clientMode !== 'live' || connectionStatus !== 'connected') return null;
    setSetupLoading(true);
    try {
      const next = await client.getSetupState();
      setSetup(next);
      setSetupError('');
      return next;
    } catch (error: unknown) { setSetupError(message(error)); return null; }
    finally { setSetupLoading(false); }
  }, [client, clientMode, connectionStatus]);
  const refreshMetadata = useCallback(async () => {
    if (clientMode !== 'live' || connectionStatus !== 'connected') return false;
    const epoch = ++metadataReadEpoch.current;
    try {
      const page = await client.listConversationMetadata({ limit: 500 });
      let selected: ConversationMetadata | null = null;
      let selectedError: unknown = null;
      if (live.selectedThreadId) {
        try { selected = await client.getConversationMetadata(live.selectedThreadId); }
        catch (error: unknown) { selectedError = error; }
      }
      if (epoch !== metadataReadEpoch.current) return false;
      setMetadata((current) => mergeConversationMetadata(current, selected ? [...page.items, selected] : page.items));
      if (selectedError) setCreateError(`当前对话标题读取失败：${message(selectedError)}。请刷新后核对。`);
      return !selectedError;
    } catch (error: unknown) { if (epoch === metadataReadEpoch.current) setCreateError(`标题读取失败：${message(error)}。请刷新后核对。`); return false; }
  }, [client, clientMode, connectionStatus, live.selectedThreadId]);

  const titleRefreshKey = metadataRefreshKey(live.threads, live.selectedThreadId, live.stream.events);

  useEffect(() => {
    if (clientMode !== 'live') return;
    if (connectionStatus === 'connected') void refreshSetup();
    else { setSetup(null); setSetupError(''); }
  }, [clientMode, connectionStatus, refreshSetup]);
  useEffect(() => {
    if (clientMode === 'live' && connectionStatus === 'connected' && live.phase === 'ready') void refreshMetadata();
  }, [clientMode, connectionStatus, live.phase, titleRefreshKey, refreshMetadata]);

  const bootstrap = useCallback(async (modelProfileId?: string) => {
    if (actionInFlight.current) return false;
    actionInFlight.current = true;
    try {
      const next = await client.bootstrapSetup(modelProfileId ? { model_profile_id: modelProfileId } : {}, { idempotencyKey: crypto.randomUUID() });
      setSetup(next); setSetupError('');
      await live.refresh();
      return next.ready;
    } catch (error: unknown) { setSetupError(`${message(error)} 请刷新后核对设置状态。`); return false; }
    finally { actionInFlight.current = false; }
  }, [client, live]);

  const initializeConversation = useCallback(async (input: ConversationStart = {}) => {
    if (createInFlight.current || createOutcomeUnknown || connectionStatus !== 'connected') return null;
    createInFlight.current = true; setCreateBusy(true); setCreateError('');
    setRecoveredConversationId(null);
    const requestKey = crypto.randomUUID();
    let stored = false;
    try {
      storeCreateKey(requestKey);
      stored = true;
      const result = await client.initializeConversation(input, { idempotencyKey: requestKey });
      storeCreateKey(null);
      setCreateOutcomeUnknown(false);
      try {
        await live.refresh();
        if (!await refreshMetadata()) setCreateError('对话已创建，但标题暂时无法读取。请刷新对话列表核对。');
      } catch { setCreateError('对话已创建，但列表暂时无法同步。请刷新后打开。'); }
      return result;
    } catch (error: unknown) {
      if (!stored) {
        setCreateError('无法保存创建请求的核对标识，已停止新建。请检查本地存储权限后重试。');
      } else if (uncertain(error)) {
        setCreateOutcomeUnknown(true);
        setCreateError(`新建结果尚未确认：${message(error)}。请刷新并核对原请求，不要重复创建。`);
      } else {
        try { storeCreateKey(null); } catch { /* A stale key remains a safe recovery barrier. */ }
        setCreateError(message(error));
      }
      return null;
    } finally { createInFlight.current = false; setCreateBusy(false); }
  }, [client, connectionStatus, createOutcomeUnknown, live, refreshMetadata, storeCreateKey]);

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
      setCreateError(`${message(error)} 请刷新标题后核对。`);
      if (uncertain(error)) { setRenameOutcomeUnknown(true); setRenamePendingTarget({ threadId, title }); }
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
        storeCreateKey(null);
        setCreateOutcomeUnknown(false);
        setCreateError('已找到先前创建的对话，可直接打开。');
        try {
          await live.refresh();
          if (!await refreshMetadata()) setCreateError('已确认创建，但对话列表暂时无法同步。可直接打开该对话。');
        } catch { setCreateError('已确认创建，但对话列表暂时无法同步。可直接打开该对话。'); }
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
    } catch (error: unknown) {
      setCreateError(`原请求核对失败：${message(error)}。请恢复连接后重试；不要重复提交。`);
    }
  }, [client, connectionStatus, live, pendingCreateKey, refreshMetadata, renameOutcomeUnknown, renamePendingTarget, storeCreateKey]);

  return <Context.Provider value={{ client, setup, setupLoading, setupError, metadata, draft, setDraft, getDraftRevision, getRouteRevision, noteRouteChange, pendingSend, setPendingSend, createBusy, createOutcomeUnknown, renameOutcomeUnknown, createError, recoveredConversationId, refreshSetup, refreshMetadata, bootstrap, initializeConversation, renameConversation, clearCreateUncertainty }}>{children}</Context.Provider>;
};

export function useOnboarding(): OnboardingContextValue {
  const value = useContext(Context);
  if (!value) throw new Error('OnboardingProvider 未挂载');
  return value;
}
