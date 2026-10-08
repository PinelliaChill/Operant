import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { type ConnectionCreate, type OAuthAttempt, type ProviderConnection } from '../../../../../sdk/typescript-client/onboarding';
import { useOnboarding } from '../../live/OnboardingContext';
import { isWriteOutcomeUnknown } from '../../lib/writeOutcome';
import { ModelConnectionsView } from './ModelConnectionsView';
import { currentBrowserOrigin } from '../../lib/liveBaseUrl';
import { decodeModelRequest, encodeModelRequest, modelRequestIsConfirmed, modelRequestResultCopy, modelRequestStorageKey, type PendingModelRequest } from './modelRequestRecovery';

const errorText = (value: unknown) => value instanceof Error ? value.message : '请求失败，请刷新后核对。';

export const LiveModelConnections: React.FC = () => {
  const { client, bootstrap, refreshSetup, draft } = useOnboarding();
  const navigate = useNavigate();
  const [connections, setConnections] = useState<ProviderConnection[]>([]);
  const [attempt, setAttempt] = useState<OAuthAttempt | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [errorDetail, setErrorDetail] = useState('');
  const storageKey = modelRequestStorageKey(currentBrowserOrigin());
  const [pendingRequest, setPendingRequest] = useState<PendingModelRequest | 'invalid' | null>(() => {
    try { return decodeModelRequest(localStorage.getItem(storageKey)); } catch { return 'invalid'; }
  });
  const [writeUncertain, setWriteUncertain] = useState(Boolean(pendingRequest));
  const [reconcileMessage, setReconcileMessage] = useState('');
  const [confirmedRequestId, setConfirmedRequestId] = useState<string>();
  const inFlight = useRef(false);
  const finalizingSelection = useRef(false);
  const [selectionBusy, setSelectionBusy] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try { const page = await client.listModelConnections(); setConnections(page.items); setError(''); setErrorDetail(''); return true; }
    catch (cause: unknown) { setError('连接列表读取失败，请检查服务并刷新。'); setErrorDetail(errorText(cause)); return false; }
    finally { setLoading(false); }
  }, [client]);
  useEffect(() => { void refresh(); }, [refresh]);

  useEffect(() => {
    if (!attempt || !['pending', 'connected'].includes(attempt.status)) return;
    let active = true;
    const timer = window.setTimeout(() => {
      void client.getModelOAuthStatus(attempt.attempt_id).then((next) => {
        if (!active) return;
        setAttempt(next);
        if (next.status === 'connected' || next.status === 'ready') { void refresh(); void refreshSetup(); }
      }).catch((cause: unknown) => { if (active) { setError('登录状态读取失败，请刷新连接列表核对。'); setErrorDetail(errorText(cause)); } });
    }, 2500);
    return () => { active = false; window.clearTimeout(timer); };
  }, [attempt, client, refresh, refreshSetup]);

  const storeRequest = useCallback((value: PendingModelRequest | null) => {
    if (value) localStorage.setItem(storageKey, encodeModelRequest(value));
    else localStorage.removeItem(storageKey);
    setPendingRequest(value);
  }, [storageKey]);

  const write = useCallback(async <T,>(label: string, run: () => Promise<T>, tracked?: PendingModelRequest): Promise<T | null> => {
    if (inFlight.current || writeUncertain) return null;
    inFlight.current = true; setBusy(true); setError(''); setErrorDetail(''); setReconcileMessage('');
    let dispatched = false;
    try {
      if (tracked) storeRequest(tracked);
      dispatched = true;
      const result = await run();
      if (tracked) storeRequest(null);
      return result;
    }
    catch (cause: unknown) {
      setError(dispatched ? `${label}失败，请核对原请求结果。` : '无法保存请求编号，已停止提交。请检查本地存储权限。');
      setErrorDetail(errorText(cause));
      if (dispatched && isWriteOutcomeUnknown(cause)) setWriteUncertain(true);
      else if (tracked && dispatched) {
        try { storeRequest(null); } catch { setWriteUncertain(true); }
      }
      return null;
    }
    finally { inFlight.current = false; setBusy(false); }
  }, [storeRequest, writeUncertain]);

  const createKeyConnection = async (input: ConnectionCreate) => {
    const requestId = crypto.randomUUID();
    const saved = await write('保存模型连接', () => client.createModelConnection(input, { idempotencyKey: requestId }), { requestId, kind: 'api_key' });
    if (!saved) return false;
    await refresh(); await refreshSetup();
    return true;
  };
  const startOAuth = (provider: 'chatgpt' | 'gemini', connectionId?: string, projectId?: string, clientId?: string, clientSecret?: string) => {
    void (async () => {
      const requestId = crypto.randomUUID();
      const next = await write('启动账号登录', () => client.startModelOAuth({ provider, ...(connectionId ? { connection_id: connectionId } : {}), ...(projectId ? { project_id: projectId } : {}), ...(clientId ? { client_id: clientId } : {}), ...(clientSecret ? { client_secret: clientSecret } : {}) }, { idempotencyKey: requestId }), { requestId, kind: 'oauth' });
      if (next) { setAttempt(next); await refresh(); }
    })();
  };
  const cancelOAuth = (attemptId: string) => {
    void (async () => { const next = await write('取消账号登录', () => client.cancelModelOAuth(attemptId, { idempotencyKey: crypto.randomUUID() })); if (next) setAttempt(next); })();
  };
  const discover = (connectionId: string) => {
    void (async () => { const result = await write('查找模型', () => client.discoverConnectionModels(connectionId, { idempotencyKey: crypto.randomUUID() })); if (result) await refresh(); })();
  };
  const selectModel = (connectionId: string, modelId: string) => {
    if (finalizingSelection.current) return;
    finalizingSelection.current = true; setSelectionBusy(true);
    void (async () => {
      try {
        const result = await write('选择模型', () => client.selectConnectionModel(connectionId, { model_id: modelId }, { idempotencyKey: crypto.randomUUID() }));
        if (!result) return;
        const ready = await bootstrap(result.model_profile_id);
        if (!ready) setError('模型已保存，但助手准备未完成。请刷新设置状态后重试。');
        await refresh(); await refreshSetup();
        if (ready && draft.trim()) navigate('/chat');
      } finally { finalizingSelection.current = false; setSelectionBusy(false); }
    })();
  };
  const disconnect = (connectionId: string) => {
    if (!window.confirm('断开此模型连接？已有对话仍保留原有记录。')) return;
    void (async () => { const result = await write('断开连接', () => client.deleteModelConnection(connectionId, { idempotencyKey: crypto.randomUUID() })); if (result) { await refresh(); await refreshSetup(); } })();
  };
  const acknowledge = () => {
    if (inFlight.current) return;
    void (async () => {
      if (pendingRequest === 'invalid') { setReconcileMessage('本地核对记录不可用，请先恢复本地存储或人工核对运行记录。提交仍已暂停。'); return; }
      if (!pendingRequest) { if (await refresh()) setWriteUncertain(false); return; }
      inFlight.current = true; setBusy(true);
      try {
        const result = await client.getModelConnectionRequest(pendingRequest.requestId);
        setReconcileMessage(modelRequestResultCopy(pendingRequest, result));
        if (modelRequestIsConfirmed(pendingRequest, result)) {
          storeRequest(null);
          setConfirmedRequestId(pendingRequest.requestId);
          setWriteUncertain(false);
          await refresh(); await refreshSetup();
        }
      } catch (cause: unknown) {
        setReconcileMessage('原请求暂时无法核对。请恢复连接后再查询，暂时不要重复提交。');
        setErrorDetail(errorText(cause));
      } finally { inFlight.current = false; setBusy(false); }
    })();
  };

  return <ModelConnectionsView connections={connections} loading={loading} busy={busy || selectionBusy} error={error} errorDetail={errorDetail} writeUncertain={writeUncertain} reconcileMessage={reconcileMessage} confirmedRequestId={confirmedRequestId} pendingRequestId={pendingRequest && pendingRequest !== 'invalid' ? pendingRequest.requestId : undefined} attempt={attempt} onRefresh={() => { void refresh(); }} onCreateKeyConnection={createKeyConnection} onStartOAuth={startOAuth} onCancelOAuth={cancelOAuth} onDiscover={discover} onSelectModel={selectModel} onDisconnect={disconnect} onAcknowledge={acknowledge} />;
};
