import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { type ConnectionCreate, type OAuthAttempt, type ProviderConnection } from '../../../../../sdk/typescript-client/onboarding';
import { useOnboarding } from '../../live/OnboardingContext';
import { isWriteOutcomeUnknown } from '../../lib/writeOutcome';
import { requestErrorCopy } from '../../lib/requestErrorCopy';
import { ModelConnectionsView } from './ModelConnectionsView';
import { sameOAuthAttempt, startOAuthStatusPolling } from './oauthStatusPolling';
import { currentBrowserOrigin } from '../../lib/liveBaseUrl';
import { codexLoginRequestHasEnded, decodeModelRequest, encodeModelRequest, modelRequestAfterReconciliation, modelRequestIsConfirmed, modelRequestResultCopy, modelRequestStorageKey, type PendingModelRequest } from './modelRequestRecovery';

const errorText = (value: unknown) => value instanceof Error ? value.message : '请求失败，请刷新后核对。';

export const LiveModelConnections: React.FC = () => {
  const { client, bootstrap, refreshSetup, draft } = useOnboarding();
  const navigate = useNavigate();
  const [connections, setConnections] = useState<ProviderConnection[]>([]);
  const [attempt, setAttempt] = useState<OAuthAttempt | null>(null);
  const [knownCodexAttemptId, setKnownCodexAttemptId] = useState<string | null>(null);
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
    if (!attempt || attempt.status !== 'pending') return;
    return startOAuthStatusPolling({
      read: () => client.getModelOAuthStatus(attempt.attempt_id),
      retryOnError: attempt.provider === 'codex',
      onStatus: (next) => {
        if (!sameOAuthAttempt(attempt, next)) {
          setError('登录状态与当前请求不一致，请核对原请求。');
          setErrorDetail('');
          if (attempt.provider === 'codex') setWriteUncertain(true);
          return;
        }
        if (attempt.provider === 'codex') setError((current) => current === 'Codex 登录状态暂时读不到，恢复连接后会自动重试。' ? '' : current);
        setAttempt(next);
        if (next.status !== 'pending') void refresh();
        if (next.status === 'connected' || next.status === 'ready') void refreshSetup();
      },
      onError: (cause) => {
        setError(attempt.provider === 'codex' ? 'Codex 登录状态暂时读不到，恢复连接后会自动重试。' : '登录状态读取失败，请刷新连接列表核对。');
        setErrorDetail(errorText(cause));
      },
    });
  }, [attempt, client, refresh, refreshSetup]);

  const storeRequest = useCallback((value: PendingModelRequest | null) => {
    if (value) localStorage.setItem(storageKey, encodeModelRequest(value));
    else localStorage.removeItem(storageKey);
    setPendingRequest(value);
  }, [storageKey]);

  useEffect(() => {
    if (!writeUncertain && codexLoginRequestHasEnded(pendingRequest, attempt, knownCodexAttemptId)) {
      try { storeRequest(null); }
      catch { setWriteUncertain(true); setError('无法清除本地登录记录，请先核对原请求。'); }
    }
  }, [attempt, knownCodexAttemptId, pendingRequest, storeRequest, writeUncertain]);
  useEffect(() => {
    if (attempt?.provider === 'codex' && attempt.status !== 'pending') setReconcileMessage('');
  }, [attempt?.provider, attempt?.status]);

  const write = useCallback(async <T,>(label: string, run: () => Promise<T>, tracked?: PendingModelRequest, allowDuringCodexLogin = false, restoreOnKnownFailure?: PendingModelRequest | null): Promise<T | null> => {
    if (inFlight.current || writeUncertain) return null;
    if (!allowDuringCodexLogin && pendingRequest !== 'invalid' && pendingRequest?.kind === 'codex_oauth' && attempt?.status === 'pending') {
      setError('Codex 登录正在进行。请完成或取消当前登录后继续。');
      return null;
    }
    inFlight.current = true; setBusy(true); setError(''); setErrorDetail(''); setReconcileMessage('');
    let dispatched = false;
    try {
      if (tracked) storeRequest(tracked);
      dispatched = true;
      const result = await run();
      if (tracked && tracked.kind !== 'codex_oauth' && tracked.kind !== 'codex_cancel') storeRequest(null);
      return result;
    }
    catch (cause: unknown) {
      const uncertain = dispatched && isWriteOutcomeUnknown(cause);
      setError(!dispatched ? '无法保存请求编号，已停止提交。请检查本地存储权限。' : uncertain ? `${label}结果尚未确认，请核对原请求。` : requestErrorCopy(cause));
      setErrorDetail(errorText(cause));
      if (uncertain) setWriteUncertain(true);
      else if (tracked && dispatched) {
        try { storeRequest(restoreOnKnownFailure ?? null); } catch { setWriteUncertain(true); }
      }
      return null;
    }
    finally { inFlight.current = false; setBusy(false); }
  }, [attempt?.status, pendingRequest, storeRequest, writeUncertain]);

  const createKeyConnection = async (input: ConnectionCreate) => {
    const requestId = crypto.randomUUID();
    const saved = await write('保存模型连接', () => client.createModelConnection(input, { idempotencyKey: requestId }), { requestId, kind: 'api_key' });
    if (!saved) return false;
    await refresh(); await refreshSetup();
    return true;
  };
  const startOAuth = (provider: 'chatgpt' | 'gemini' | 'codex', connectionId?: string, projectId?: string, clientId?: string, clientSecret?: string) => {
    void (async () => {
      const requestId = crypto.randomUUID();
      const next = await write('启动账号登录', () => client.startModelOAuth({ provider, ...(connectionId ? { connection_id: connectionId } : {}), ...(projectId ? { project_id: projectId } : {}), ...(clientId ? { client_id: clientId } : {}), ...(clientSecret ? { client_secret: clientSecret } : {}) }, { idempotencyKey: requestId }), { requestId, kind: provider === 'codex' ? 'codex_oauth' : 'oauth' });
      if (next) {
        if (next.provider !== provider) {
          setWriteUncertain(true);
          setError('登录响应与请求不一致，请核对原请求。');
          return;
        }
        if (provider === 'codex') setKnownCodexAttemptId(next.attempt_id);
        setAttempt(next); await refresh();
      }
    })();
  };
  const cancelOAuth = (attemptId: string) => {
    void (async () => {
      const cancelRequestId = crypto.randomUUID();
      const tracked = attempt?.provider === 'codex' ? { requestId: cancelRequestId, kind: 'codex_cancel' as const, attemptId } : undefined;
      const previous = pendingRequest && pendingRequest !== 'invalid' ? pendingRequest : null;
      const next = await write('取消账号登录', () => client.cancelModelOAuth(attemptId, { idempotencyKey: cancelRequestId }), tracked, true, previous);
      if (next) {
        if (tracked && (next.provider !== 'codex' || next.attempt_id !== attemptId || next.status === 'pending')) {
          setWriteUncertain(true);
          setError('取消登录响应与请求不一致，请核对原请求。');
          return;
        }
        if (tracked) {
          try { storeRequest(null); }
          catch { setWriteUncertain(true); setError('无法清除本地取消记录，请核对原请求。'); return; }
        }
        setAttempt(next); await refresh();
      }
    })();
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
          if (!modelRequestAfterReconciliation(pendingRequest, result)) storeRequest(null);
          setConfirmedRequestId(pendingRequest.requestId);
          setWriteUncertain(false);
          if ((pendingRequest.kind === 'codex_oauth' || pendingRequest.kind === 'codex_cancel') && result.oauth_attempt) {
            if (pendingRequest.kind === 'codex_oauth') setKnownCodexAttemptId(result.oauth_attempt.attempt_id);
            setAttempt(result.oauth_attempt);
          }
          await refresh(); await refreshSetup();
        }
      } catch (cause: unknown) {
        setReconcileMessage('原请求暂时无法核对。请恢复连接后再查询，暂时不要重复提交。');
        setErrorDetail(errorText(cause));
      } finally { inFlight.current = false; setBusy(false); }
    })();
  };

  useEffect(() => {
    if (!writeUncertain || !pendingRequest || pendingRequest === 'invalid' || !['codex_oauth', 'codex_cancel'].includes(pendingRequest.kind)) return;
    let active = true;
    let timer: number;
    const reconcile = () => {
      acknowledge();
      if (active) timer = window.setTimeout(reconcile, 5000);
    };
    reconcile();
    return () => { active = false; window.clearTimeout(timer); };
  }, [pendingRequest, writeUncertain, client]);

  return <ModelConnectionsView connections={connections} loading={loading} busy={busy || selectionBusy || loading} error={error} errorDetail={errorDetail} writeUncertain={writeUncertain} reconcileMessage={reconcileMessage} confirmedRequestId={confirmedRequestId} pendingRequestId={pendingRequest && pendingRequest !== 'invalid' ? pendingRequest.requestId : undefined} attempt={attempt} onRefresh={() => { void refresh(); }} onCreateKeyConnection={createKeyConnection} onStartOAuth={startOAuth} onCancelOAuth={cancelOAuth} onDiscover={discover} onSelectModel={selectModel} onDisconnect={disconnect} onAcknowledge={acknowledge} />;
};
