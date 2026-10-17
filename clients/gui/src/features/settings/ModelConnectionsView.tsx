import React, { useEffect, useRef, useState } from 'react';
import { ExternalLink, KeyRound, RefreshCw, Trash2 } from 'lucide-react';
import { SearchSelect } from '../../components/SearchSelect';
import { modelReadiness } from './modelReadiness';
import type { SetupState } from '../../../../../sdk/typescript-client/onboarding';
import type { ConnectionCreate } from '../../../../../sdk/typescript-client/onboarding';
import { canContinueOAuthRegistration, canDiscoverConnectionModels, canReauthorizeModelConnection, knownModelConnectionErrorLabel, modelConnectionErrorLabel, modelConnectionStatusLabel, oauthStatusLabel, offersApiKeyAlternative } from '../../lib/statusCopy';
import { modelDisplayName } from './modelName';
import { groupModelConnections, pendingOAuthUrl } from './modelConnectionPresentation';
import './model-connections.css';

export type ConnectionProvider = 'openai-compatible' | 'chatgpt' | 'gemini' | 'codex';
export interface ConnectionItem {
  connection_id: string;
  name: string;
  provider: ConnectionProvider;
  auth_method: 'api_key' | 'oauth';
  status: 'needs_auth' | 'connected' | 'ready' | 'error';
  base_url: string;
  model_ids?: string[];
  model_names?: Record<string, unknown>;
  profile_ids?: string[];
  account_label?: string | null;
  error?: string | null;
}
export interface OAuthAttemptView {
  attempt_id: string;
  connection_id: string;
  provider: 'chatgpt' | 'gemini' | 'codex';
  status: 'pending' | 'connected' | 'ready' | 'cancelled' | 'expired' | 'error';
  authorization_url?: string | null;
  message?: string | null;
}

interface ModelConnectionsProps {
  connections: ConnectionItem[];
  setup?: SetupState | null;
  setupUnavailable?: boolean;
  readFailed?: boolean;
  loading: boolean;
  busy: boolean;
  error: string;
  errorDetail?: string;
  writeUncertain: boolean;
  reconcileMessage?: string;
  pendingRequestId?: string;
  confirmedRequestId?: string;
  attempt?: OAuthAttemptView | null;
  onRefresh: () => void;
  onCreateKeyConnection: (input: ConnectionCreate) => Promise<boolean>;
  onStartOAuth: (provider: 'chatgpt' | 'gemini' | 'codex', connectionId?: string, projectId?: string, clientId?: string, clientSecret?: string) => void;
  onCancelOAuth: (attemptId: string) => void;
  onDiscover: (connectionId: string) => void;
  onSelectModel: (connectionId: string, modelId: string) => void;
  onDisconnect: (connectionId: string) => void;
  onAcknowledge: () => void;
}

export const ModelConnectionsView: React.FC<ModelConnectionsProps> = ({ connections, setup, setupUnavailable, readFailed, loading, busy: operationBusy, error, errorDetail, writeUncertain, reconcileMessage, pendingRequestId, confirmedRequestId, attempt, onRefresh, onCreateKeyConnection, onStartOAuth, onCancelOAuth, onDiscover, onSelectModel, onDisconnect, onAcknowledge }) => {
  const busy = operationBusy || Boolean(readFailed);
  const readiness = modelReadiness(setup, connections, Boolean(setupUnavailable || readFailed || loading || writeUncertain));
  const [service, setService] = useState<'openai' | 'gemini' | 'custom'>('openai');
  const [name, setName] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [apiKey, setApiKey] = useState('');
  useEffect(() => {
    if (confirmedRequestId) { setApiKey(''); setName(''); setBaseUrl(''); setFormError(''); }
  }, [confirmedRequestId]);
  const apiKeyRef = useRef<HTMLInputElement>(null);
  const [geminiProjectId, setGeminiProjectId] = useState('');
  const [geminiClientId, setGeminiClientId] = useState('');
  const [geminiClientSecret, setGeminiClientSecret] = useState('');
  const [selectedModels, setSelectedModels] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState('');
  const [oauthOpenError, setOAuthOpenError] = useState('');
  const [oauthCopied, setOAuthCopied] = useState(false);
  const oauthUrlRef = useRef<HTMLInputElement>(null);
  const chatgptConnection = connections.find((connection) => connection.provider === 'chatgpt' && connection.auth_method === 'oauth');
  const codexConnection = connections.find((connection) => connection.provider === 'codex' && connection.auth_method === 'oauth');
  const geminiConnection = connections.find((connection) => connection.provider === 'gemini' && connection.auth_method === 'oauth');
  const { visible: visibleConnections, legacy: legacyConnections } = groupModelConnections(connections);
  const authorizationUrl = pendingOAuthUrl(attempt);
  const codexLoginPending = attempt?.provider === 'codex' && attempt.status === 'pending';
  useEffect(() => { setOAuthOpenError(''); setOAuthCopied(false); }, [attempt?.attempt_id]);
  const openOAuth = async (event: React.MouseEvent<HTMLAnchorElement>, url: string) => {
    if (!Boolean((window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__)) return;
    event.preventDefault();
    setOAuthOpenError('');
    try {
      const { invoke } = await import('@tauri-apps/api/core');
      await invoke('open_model_oauth', { url });
    } catch {
      setOAuthOpenError('无法打开系统浏览器。请展开下方地址，复制后在浏览器中打开。');
    }
  };
  const copyOAuthUrl = async (url: string) => {
    try { await navigator.clipboard.writeText(url); setOAuthCopied(true); setOAuthOpenError(''); }
    catch { oauthUrlRef.current?.focus(); oauthUrlRef.current?.select(); setOAuthOpenError('无法自动复制。地址已选中，请手动复制。'); }
  };
  const focusApiKey = () => {
    setService('openai');
    apiKeyRef.current?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    apiKeyRef.current?.focus();
  };
  const submitKey = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!apiKey.trim() || (service === 'custom' && !baseUrl.trim())) { setFormError('请填写密钥；自定义服务还需填写地址。'); return; }
    setFormError('');
    const saved = await onCreateKeyConnection({ provider: service === 'gemini' ? 'gemini' : 'openai-compatible', ...(name.trim() || service !== 'custom' ? { name: name.trim() || (service === 'openai' ? 'OpenAI' : service === 'gemini' ? 'Gemini API' : '自定义服务') } : {}), ...(service === 'custom' ? { base_url: baseUrl.trim() } : {}), api_key: apiKey });
    if (saved) { setName(''); setBaseUrl(''); setApiKey(''); }
  };

  const reauthorize = (connection: ConnectionItem) => {
    if (connection.provider === 'gemini') onStartOAuth('gemini', connection.connection_id, geminiProjectId.trim(), geminiClientId.trim() || undefined, geminiClientSecret || undefined);
    else if (connection.provider === 'chatgpt' || connection.provider === 'codex') onStartOAuth(connection.provider, connection.connection_id);
  };

  const connectionCard = (connection: ConnectionItem) => <article className="model-connections-card" key={connection.connection_id}>
    <div className="model-connections-row"><div><h3>{connection.name === 'API connection' ? 'API连接' : connection.provider === 'codex' && connection.name === 'Codex' ? 'ChatGPT（Codex）' : connection.name}</h3><p>{connection.account_label || (connection.auth_method === 'oauth' ? '账号登录' : 'API 密钥')}</p></div><span className="badge">{modelConnectionStatusLabel(connection.status, connection.error)}</span></div>
    {connection.error && <div className="live-alert live-alert-error" role="alert">{modelConnectionErrorLabel(connection.error)}{offersApiKeyAlternative(connection.error) && <button type="button" className="btn btn-secondary btn-sm" onClick={focusApiKey}>连接 API 密钥</button>}<details><summary>查看原因</summary><code>{connection.error}</code></details></div>}
    <div className="model-connections-actions">
      {connection.auth_method === 'oauth' && connection.provider !== 'openai-compatible' && canReauthorizeModelConnection(connection.status, connection.error) && <button type="button" className="btn btn-secondary btn-sm" onClick={() => reauthorize(connection)} disabled={busy || writeUncertain || (connection.provider === 'gemini' && !geminiProjectId.trim()) || (connection.provider === 'codex' && codexLoginPending)}>{connection.error === 'chatgpt_plan_usage_disabled' ? '重新授权套餐' : '重新登录'}</button>}
      <button type="button" className="btn btn-secondary btn-sm" onClick={() => onDiscover(connection.connection_id)} disabled={busy || writeUncertain || !canDiscoverConnectionModels(connection.status, connection.error)}>查找模型</button>
      <button type="button" className="btn btn-ghost btn-sm" onClick={() => onDisconnect(connection.connection_id)} disabled={busy || writeUncertain}><Trash2 size={13} aria-hidden="true" />{connection.error === 'revocation_unconfirmed' ? '重试断开' : '断开连接'}</button>
    </div>
    {(connection.model_ids?.length ?? 0) > 0 && <div className="model-connections-select"><SearchSelect label="选择模型" value={selectedModels[connection.connection_id] || ''} onChange={(value) => setSelectedModels((current) => ({ ...current, [connection.connection_id]: value }))} placeholder="选择发现的模型" options={(connection.model_ids ?? []).map((modelId) => ({ value: modelId, label: modelDisplayName(modelId, connection.model_names), detail: modelId }))} /><button type="button" className="btn btn-primary btn-sm" disabled={busy || writeUncertain || !selectedModels[connection.connection_id]} onClick={() => onSelectModel(connection.connection_id, selectedModels[connection.connection_id])}>使用此模型</button></div>}
    <details className="model-connections-detail"><summary>连接详情</summary><dl><div><dt>服务地址</dt><dd>{connection.base_url}</dd></div><div><dt>连接编号</dt><dd><code>{connection.connection_id}</code></dd></div></dl></details>
  </article>;

  const authorizationAttempt = attempt && <section className="model-connections-card" role="status" aria-live="polite">
    <h2>账号授权</h2>
    <p>{canContinueOAuthRegistration(attempt.provider, attempt.status, attempt.message) ? '授权码未被接受。请点击下方按钮继续连接。' : attempt.status === 'error' && attempt.message ? modelConnectionErrorLabel(attempt.message) : attempt.provider === 'codex' && attempt.status === 'pending' ? '等待 Codex 登录。请在浏览器完成授权，状态会自动更新。' : oauthStatusLabel(attempt.status)}</p>
    {authorizationUrl && <><a className="btn btn-primary" href={authorizationUrl} target="_blank" rel="noopener noreferrer" onClick={(event) => void openOAuth(event, authorizationUrl)}>打开登录页面 <ExternalLink size={14} aria-hidden="true" /></a>{oauthOpenError && <p role="alert">{oauthOpenError}</p>}<details><summary>手动打开登录页面</summary><label>登录地址<input ref={oauthUrlRef} className="input" type="text" readOnly value={authorizationUrl} onFocus={(event) => event.currentTarget.select()} /></label><button type="button" className="btn btn-secondary btn-sm" onClick={() => void copyOAuthUrl(authorizationUrl)}>{oauthCopied ? '已复制地址' : '复制登录地址'}</button></details></>}
    {attempt.status === 'pending' && <button type="button" className="btn btn-secondary" onClick={() => onCancelOAuth(attempt.attempt_id)} disabled={busy || writeUncertain}>取消登录</button>}
    {canContinueOAuthRegistration(attempt.provider, attempt.status, attempt.message) && <button type="button" className="btn btn-primary" disabled={busy || writeUncertain} onClick={() => onStartOAuth('chatgpt', attempt.connection_id)}>继续连接 ChatGPT</button>}
    {attempt.status === 'error' && attempt.message && <details><summary>查看失败原因</summary><code>{attempt.message}</code></details>}
  </section>;

  return <div className="model-connections">
    <div className="model-connections-heading"><div><h1>模型连接</h1><p>连接模型后，就可以开始对话。</p></div><button type="button" className="btn btn-secondary btn-sm" onClick={onRefresh} disabled={operationBusy || loading}><RefreshCw size={14} aria-hidden="true" />刷新</button></div>
    <section className="model-connections-card" aria-label="当前模型与准备状态" aria-live="polite">
      <h2>当前默认模型</h2>
      <p>{readiness.unconfirmed ? '默认模型信息暂时无法读取，请刷新核对。' : readiness.profile ? `${modelDisplayName(readiness.profile.model_id)} · ${readiness.profile.name}` : setup?.default_model_profile_id ? '默认模型信息暂时无法确认，请刷新核对。' : '尚未选择默认模型'}</p>
      <ol><li>模型服务：{readiness.serviceLabel}</li><li>默认模型：{readiness.selectionLabel}</li><li>对话准备：{readiness.conversationLabel}</li></ol>
      {!readiness.unconfirmed && !readiness.ready && setup?.missing_steps?.length ? <p>还需完成：{setup.missing_steps.map((step) => ({ workspace: '选择工作项目', model: '选择默认模型', role: '准备助手角色', assistant: '准备助手角色', skills: '准备技能' })[step] || '刷新核对设置').join('、')}</p> : null}
    </section>
    {error && <div className="live-alert live-alert-error" role="alert">{(errorDetail && knownModelConnectionErrorLabel(errorDetail)) || error}<details><summary>查看原因</summary>{errorDetail || error}</details></div>}
    {writeUncertain && <div className="live-alert live-alert-error model-request-recovery" role="alert"><p>{reconcileMessage || '连接操作的结果尚未确认，请先核对，暂时不要重复提交。'}</p><div className="model-request-recovery-actions"><button type="button" className="btn btn-secondary btn-sm" onClick={onAcknowledge} disabled={busy || loading}>{pendingRequestId ? '核对原请求' : '刷新核对'}</button>{pendingRequestId && <details><summary>核对详情</summary><p>请求编号：<code>{pendingRequestId}</code></p></details>}</div></div>}
    {!writeUncertain && reconcileMessage && <p role="status">{reconcileMessage}</p>}
    <div className="model-connections-options">
      <section className="model-connections-card">
        <h2>ChatGPT（Codex）</h2>
        <p>使用 ChatGPT 账号连接 Codex。登录由官方 Codex CLI 打开浏览器，可用模型以连接后发现的结果为准。</p>
        {!codexConnection || canReauthorizeModelConnection(codexConnection.status, codexConnection.error)
          ? <button type="button" className="btn btn-primary" onClick={() => onStartOAuth('codex', codexConnection?.connection_id)} disabled={busy || writeUncertain || codexLoginPending}>{codexLoginPending ? '等待登录…' : codexConnection ? '重新登录 Codex' : '连接 Codex'}</button>
          : <p>账号已添加，请查看下方连接状态。</p>}
      </section>
      <section className="model-connections-card"><h2>Gemini</h2><p>使用 Google 账号连接 Gemini API，额度和费用由 Cloud 项目决定。</p><label>Google Cloud 项目 ID<input className="input" value={geminiProjectId} onChange={(event) => setGeminiProjectId(event.target.value)} placeholder="你的项目 ID" autoCapitalize="none" autoCorrect="off" spellCheck={false} /></label><details><summary>高级：使用自己的桌面 OAuth 客户端</summary><label>客户端 ID<input className="input" value={geminiClientId} onChange={(event) => setGeminiClientId(event.target.value)} autoComplete="off" /></label><label>客户端密钥<input className="input" type="password" value={geminiClientSecret} onChange={(event) => setGeminiClientSecret(event.target.value)} autoComplete="off" /></label></details>{(!geminiConnection || canReauthorizeModelConnection(geminiConnection.status, geminiConnection.error)) ? <button type="button" className="btn btn-primary" onClick={() => onStartOAuth('gemini', geminiConnection?.connection_id, geminiProjectId.trim(), geminiClientId.trim() || undefined, geminiClientSecret || undefined)} disabled={busy || writeUncertain || !geminiProjectId.trim()}>{geminiConnection ? '重新登录 Gemini' : '连接 Gemini'}</button> : <p>账号已添加，请查看下方连接状态。</p>}</section>
      <section className="model-connections-card"><h2><KeyRound size={17} aria-hidden="true" />API 密钥</h2><p>选择服务商并填写密钥。模型会从服务端发现。</p><form onSubmit={(event) => void submitKey(event)}><SearchSelect label="服务商" value={service} onChange={(value) => setService(value as 'openai' | 'gemini' | 'custom')} options={[{ value: 'openai', label: 'OpenAI' }, { value: 'gemini', label: 'Gemini API' }, { value: 'custom', label: '兼容接口 / 自定义' }]} />{service === 'custom' && <label>服务地址<input className="input" type="url" value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder="https://…/v1" autoComplete="url" /></label>}<label>API 密钥<input ref={apiKeyRef} className="input" type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} autoComplete="new-password" /></label><details><summary>高级选项</summary><label>连接名称（可选）<input className="input" value={name} onChange={(event) => setName(event.target.value)} /></label></details>{formError && <p className="live-alert live-alert-error" role="alert">{formError}</p>}<button type="submit" className="btn btn-primary" disabled={busy || writeUncertain}>保存连接</button></form></section>
    </div>
    {attempt?.provider !== 'chatgpt' && authorizationAttempt}
    <section className="model-connections-list" aria-label="已连接的模型服务"><h2>已连接的服务</h2>{loading ? <p role="status">正在读取连接…</p> : readFailed ? <p role="status">连接信息暂时无法读取，恢复后请刷新核对。</p> : connections.length === 0 ? <p>还没有连接模型。</p> : visibleConnections.length === 0 ? <p>其他连接显示在下方高级设置中。</p> : visibleConnections.map(connectionCard)}</section>
    <details className="model-connections-advanced">
      <summary>高级设置：旧版 ChatGPT 连接{legacyConnections.length > 0 ? `（${legacyConnections.length}）` : ''}</summary>
      <div className="model-connections-card"><h2>旧版 ChatGPT 连接</h2><p>保留原有第三方账号授权方式。已有连接不会自动转为 Codex。</p>{(!chatgptConnection || canReauthorizeModelConnection(chatgptConnection.status, chatgptConnection.error)) ? <button type="button" className="btn btn-secondary" onClick={() => onStartOAuth('chatgpt', chatgptConnection?.connection_id)} disabled={busy || writeUncertain}>{chatgptConnection ? '重新授权旧版 ChatGPT' : '连接旧版 ChatGPT'}</button> : <p>账号已添加，请查看下方连接状态。</p>}</div>
      {attempt?.provider === 'chatgpt' && authorizationAttempt}
      {!readFailed && legacyConnections.map(connectionCard)}
    </details>
  </div>;
};
