import React, { useEffect, useRef, useState } from 'react';
import { ExternalLink, KeyRound, RefreshCw, Trash2 } from 'lucide-react';
import { SearchSelect } from '../../components/SearchSelect';
import type { ConnectionCreate } from '../../../../../sdk/typescript-client/onboarding';
import { canDiscoverConnectionModels, canReauthorizeModelConnection, knownModelConnectionErrorLabel, modelConnectionErrorLabel, modelConnectionStatusLabel, oauthStatusLabel, offersApiKeyAlternative } from '../../lib/statusCopy';
import { modelDisplayName } from './modelName';
import './model-connections.css';

export type ConnectionProvider = 'openai-compatible' | 'chatgpt' | 'gemini';
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
  provider: 'chatgpt' | 'gemini';
  status: 'pending' | 'connected' | 'ready' | 'cancelled' | 'expired' | 'error';
  authorization_url?: string | null;
  message?: string | null;
}

interface ModelConnectionsProps {
  connections: ConnectionItem[];
  loading: boolean;
  busy: boolean;
  error: string;
  errorDetail?: string;
  writeUncertain: boolean;
  attempt?: OAuthAttemptView | null;
  onRefresh: () => void;
  onCreateKeyConnection: (input: ConnectionCreate) => Promise<boolean>;
  onStartOAuth: (provider: 'chatgpt' | 'gemini', connectionId?: string, projectId?: string, clientId?: string, clientSecret?: string) => void;
  onCancelOAuth: (attemptId: string) => void;
  onDiscover: (connectionId: string) => void;
  onSelectModel: (connectionId: string, modelId: string) => void;
  onDisconnect: (connectionId: string) => void;
  onAcknowledge: () => void;
}

export const ModelConnectionsView: React.FC<ModelConnectionsProps> = ({ connections, loading, busy, error, errorDetail, writeUncertain, attempt, onRefresh, onCreateKeyConnection, onStartOAuth, onCancelOAuth, onDiscover, onSelectModel, onDisconnect, onAcknowledge }) => {
  const [service, setService] = useState<'openai' | 'gemini' | 'custom'>('openai');
  const [name, setName] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [apiKey, setApiKey] = useState('');
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
  const geminiConnection = connections.find((connection) => connection.provider === 'gemini' && connection.auth_method === 'oauth');
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

  return <div className="model-connections">
    <div className="model-connections-heading"><div><h1>模型连接</h1><p>连接模型后，就可以开始对话。</p></div><button type="button" className="btn btn-secondary btn-sm" onClick={onRefresh} disabled={busy || loading}><RefreshCw size={14} aria-hidden="true" />刷新</button></div>
    {error && <div className="live-alert live-alert-error" role="alert">{(errorDetail && knownModelConnectionErrorLabel(errorDetail)) || error}<details><summary>查看原因</summary>{errorDetail || error}</details></div>}
    {writeUncertain && <div className="live-alert live-alert-error" role="alert">连接操作的结果尚未确认。请刷新列表并核对状态后继续。<button type="button" className="btn btn-secondary btn-sm" onClick={onAcknowledge}>我已核对</button></div>}
    <div className="model-connections-options">
      <section className="model-connections-card"><h2>ChatGPT</h2><p>使用账号登录并选择可用模型。</p>{(!chatgptConnection || canReauthorizeModelConnection(chatgptConnection.status, chatgptConnection.error)) ? <button type="button" className="btn btn-primary" onClick={() => onStartOAuth('chatgpt', chatgptConnection?.connection_id)} disabled={busy || writeUncertain}>{chatgptConnection ? '重新授权 ChatGPT' : '连接 ChatGPT'}</button> : <p>账号已添加，请查看下方连接状态。</p>}</section>
      <section className="model-connections-card"><h2>Gemini</h2><p>使用 Google 账号登录并选择可用模型。</p><label>Google Cloud 项目编号<input className="input" value={geminiProjectId} onChange={(event) => setGeminiProjectId(event.target.value)} placeholder="你的项目 ID" /></label><details><summary>高级：使用自己的桌面 OAuth 客户端</summary><label>客户端 ID<input className="input" value={geminiClientId} onChange={(event) => setGeminiClientId(event.target.value)} autoComplete="off" /></label><label>客户端密钥<input className="input" type="password" value={geminiClientSecret} onChange={(event) => setGeminiClientSecret(event.target.value)} autoComplete="off" /></label></details>{(!geminiConnection || canReauthorizeModelConnection(geminiConnection.status, geminiConnection.error)) ? <button type="button" className="btn btn-primary" onClick={() => onStartOAuth('gemini', geminiConnection?.connection_id, geminiProjectId.trim(), geminiClientId.trim() || undefined, geminiClientSecret || undefined)} disabled={busy || writeUncertain || !geminiProjectId.trim()}>{geminiConnection ? '重新登录 Gemini' : '连接 Gemini'}</button> : <p>账号已添加，请查看下方连接状态。</p>}</section>
      <section className="model-connections-card"><h2><KeyRound size={17} aria-hidden="true" />API 密钥</h2><p>选择服务商并填写密钥。模型会从服务端发现。</p><form onSubmit={(event) => void submitKey(event)}><SearchSelect label="服务商" value={service} onChange={(value) => setService(value as 'openai' | 'gemini' | 'custom')} options={[{ value: 'openai', label: 'OpenAI' }, { value: 'gemini', label: 'Gemini API' }, { value: 'custom', label: '兼容接口 / 自定义' }]} />{service === 'custom' && <label>服务地址<input className="input" type="url" value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder="https://…/v1" autoComplete="url" /></label>}<label>API 密钥<input ref={apiKeyRef} className="input" type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} autoComplete="new-password" /></label><details><summary>高级选项</summary><label>连接名称（可选）<input className="input" value={name} onChange={(event) => setName(event.target.value)} /></label></details>{formError && <p className="live-alert live-alert-error" role="alert">{formError}</p>}<button type="submit" className="btn btn-primary" disabled={busy || writeUncertain}>保存连接</button></form></section>
    </div>
    {attempt && <section className="model-connections-card" role="status" aria-live="polite"><h2>账号授权</h2><p>{attempt.status === 'error' && attempt.message ? modelConnectionErrorLabel(attempt.message) : oauthStatusLabel(attempt.status)}</p>{attempt.authorization_url && attempt.status === 'pending' && <><a className="btn btn-primary" href={attempt.authorization_url} target="_blank" rel="noopener noreferrer" onClick={(event) => void openOAuth(event, attempt.authorization_url!)}>打开登录页面 <ExternalLink size={14} aria-hidden="true" /></a>{oauthOpenError && <p role="alert">{oauthOpenError}</p>}<details><summary>手动打开登录页面</summary><label>登录地址<input ref={oauthUrlRef} className="input" type="text" readOnly value={attempt.authorization_url} onFocus={(event) => event.currentTarget.select()} /></label><button type="button" className="btn btn-secondary btn-sm" onClick={() => void copyOAuthUrl(attempt.authorization_url!)}>{oauthCopied ? '已复制地址' : '复制登录地址'}</button></details></>}{attempt.status === 'pending' && <button type="button" className="btn btn-secondary" onClick={() => onCancelOAuth(attempt.attempt_id)} disabled={busy}>取消登录</button>}{attempt.status === 'error' && attempt.message && <details><summary>查看失败原因</summary><code>{attempt.message}</code></details>}</section>}
    <section className="model-connections-list" aria-label="已连接的模型服务"><h2>已连接的服务</h2>{loading ? <p role="status">正在读取连接…</p> : connections.length === 0 ? <p>还没有连接模型。</p> : connections.map((connection) => <article className="model-connections-card" key={connection.connection_id}><div className="model-connections-row"><div><h3>{connection.name}</h3><p>{connection.account_label || (connection.auth_method === 'oauth' ? '账号登录' : 'API 密钥')}</p></div><span className="badge">{modelConnectionStatusLabel(connection.status, connection.error)}</span></div>{connection.error && <div className="live-alert live-alert-error" role="alert">{modelConnectionErrorLabel(connection.error)}{offersApiKeyAlternative(connection.error) && <button type="button" className="btn btn-secondary btn-sm" onClick={focusApiKey}>连接 API 密钥</button>}<details><summary>查看原因</summary><code>{connection.error}</code></details></div>}<div className="model-connections-actions">{connection.auth_method === 'oauth' && canReauthorizeModelConnection(connection.status, connection.error) && <button type="button" className="btn btn-secondary btn-sm" onClick={() => onStartOAuth(connection.provider as 'chatgpt' | 'gemini', connection.connection_id, connection.provider === 'gemini' ? geminiProjectId.trim() : undefined, geminiClientId.trim() || undefined, geminiClientSecret || undefined)} disabled={busy || writeUncertain || (connection.provider === 'gemini' && !geminiProjectId.trim())}>{connection.error === 'chatgpt_plan_usage_disabled' ? '重新授权套餐' : '重新登录'}</button>}<button type="button" className="btn btn-secondary btn-sm" onClick={() => onDiscover(connection.connection_id)} disabled={busy || writeUncertain || !canDiscoverConnectionModels(connection.status, connection.error)}>查找模型</button><button type="button" className="btn btn-ghost btn-sm" onClick={() => onDisconnect(connection.connection_id)} disabled={busy || writeUncertain}><Trash2 size={13} aria-hidden="true" />{connection.error === 'revocation_unconfirmed' ? '重试断开' : '断开连接'}</button></div>{(connection.model_ids?.length ?? 0) > 0 && <div className="model-connections-select"><SearchSelect label="选择模型" value={selectedModels[connection.connection_id] || ''} onChange={(value) => setSelectedModels((current) => ({ ...current, [connection.connection_id]: value }))} placeholder="选择发现的模型" options={(connection.model_ids ?? []).map((modelId) => ({ value: modelId, label: modelDisplayName(modelId, connection.model_names), detail: modelId }))} /><button type="button" className="btn btn-primary btn-sm" disabled={busy || writeUncertain || !selectedModels[connection.connection_id]} onClick={() => onSelectModel(connection.connection_id, selectedModels[connection.connection_id])}>使用此模型</button></div>}<details className="model-connections-detail"><summary>连接详情</summary><dl><div><dt>服务地址</dt><dd>{connection.base_url}</dd></div><div><dt>连接编号</dt><dd><code>{connection.connection_id}</code></dd></div></dl></details></article>)}</section>
  </div>;
};
