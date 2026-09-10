import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Bot, Database, Pencil, Plus, RefreshCw, Search, ShieldCheck } from 'lucide-react';
import type * as B2 from '../../../../../sdk/typescript-client/b2.generated';
import { EmptyState } from '../../components/EmptyState';
import { Modal } from '../../components/Modal';
import { StatusBadge } from '../../components/StatusBadge';
import { useOperant } from '../../context/ClientContext';
import { B2LiveAdapter, normalizeB2Error } from '../../live/b2Adapter';
import { createIdempotencyKey } from '../../live/liveState';

type ModelForm = {
  name: string;
  modelId: string;
  baseUrl: string;
  secretRef: string;
  contextWindow: string;
  tokenBudget: string;
};

type RoleForm = {
  name: string;
  systemPrompt: string;
  modelProfileId: string;
  effort: B2.Effort;
  maxTurns: string;
};

const DEFAULT_MODEL_FORM: ModelForm = {
  name: '',
  modelId: '',
  baseUrl: 'https://api.openai.com/v1',
  secretRef: 'OPERANT_API_KEY',
  contextWindow: '',
  tokenBudget: '',
};

const DEFAULT_ROLE_FORM: RoleForm = {
  name: '',
  systemPrompt: '',
  modelProfileId: '',
  effort: 'medium',
  maxTurns: '10',
};

const fieldLabel: React.CSSProperties = {
  display: 'block',
  marginBottom: 4,
  color: 'var(--text-muted)',
  fontSize: 11,
  fontWeight: 600,
};

function positiveNumber(value: string): number | undefined {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : undefined;
}

function modelToForm(model: B2.ModelProfile): ModelForm {
  return {
    name: model.name,
    modelId: model.model_id,
    baseUrl: model.base_url,
    secretRef: model.secret_ref,
    contextWindow: model.context_window ? String(model.context_window) : '',
    tokenBudget: model.default_token_budget ? String(model.default_token_budget) : '',
  };
}

function roleToForm(role: B2.RolePreset): RoleForm {
  return {
    name: role.name,
    systemPrompt: role.system_prompt,
    modelProfileId: role.model_profile_id,
    effort: role.effort || 'medium',
    maxTurns: role.budget?.max_turns ? String(role.budget.max_turns) : '10',
  };
}

const LiveErrorBanner: React.FC<{
  error: { code: string; message: string; recovery?: string };
  onRetry: () => void;
}> = ({ error, onRetry }) => (
  <div className="live-alert live-alert-error" role="alert">
    <ShieldCheck size={16} aria-hidden="true" />
    <div className="live-alert-content">
      <strong>{error.code}</strong>
      <span>{error.message}</span>
      {error.recovery && <span className="live-alert-recovery">恢复：{error.recovery}</span>}
    </div>
    <button type="button" className="btn btn-secondary btn-sm" onClick={onRetry}>
      <RefreshCw size={13} aria-hidden="true" />重试
    </button>
  </div>
);

export const LiveAgentsView: React.FC = () => {
  const { b2Client, connectionStatus, addNotification } = useOperant();
  const adapter = useMemo(() => new B2LiveAdapter(b2Client), [b2Client]);
  const [models, setModels] = useState<B2.ModelProfile[]>([]);
  const [roles, setRoles] = useState<B2.RolePreset[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<{ code: string; message: string; retryable: boolean; recovery: string } | null>(null);
  const [modelModalOpen, setModelModalOpen] = useState(false);
  const [roleModalOpen, setRoleModalOpen] = useState(false);
  const [editingModel, setEditingModel] = useState<B2.ModelProfile | null>(null);
  const [editingRole, setEditingRole] = useState<B2.RolePreset | null>(null);
  const [modelForm, setModelForm] = useState<ModelForm>(DEFAULT_MODEL_FORM);
  const [roleForm, setRoleForm] = useState<RoleForm>(DEFAULT_ROLE_FORM);
  const [saving, setSaving] = useState(false);
  const [discovering, setDiscovering] = useState(false);
  const [discovery, setDiscovery] = useState<string[] | null>(null);
  const loadEpoch = useRef(0);

  const load = useCallback(async () => {
    const epoch = loadEpoch.current + 1;
    loadEpoch.current = epoch;
    setLoading(true);
    setError(null);
    try {
      await adapter.connect();
      const [nextModels, nextRoles] = await Promise.all([adapter.listModels(), adapter.listRoles()]);
      if (epoch !== loadEpoch.current) return;
      setModels(nextModels);
      setRoles(nextRoles);
    } catch (caught: unknown) {
      if (epoch !== loadEpoch.current) return;
      setModels([]);
      setRoles([]);
      setError(normalizeB2Error(caught).detail);
    } finally {
      if (epoch === loadEpoch.current) setLoading(false);
    }
  }, [adapter]);

  useEffect(() => {
    void load();
  }, [load]);

  const openNewModel = () => {
    setEditingModel(null);
    setModelForm(DEFAULT_MODEL_FORM);
    setDiscovery(null);
    setModelModalOpen(true);
  };

  const openEditModel = (model: B2.ModelProfile) => {
    setEditingModel(model);
    setModelForm(modelToForm(model));
    setDiscovery(null);
    setModelModalOpen(true);
  };

  const openNewRole = () => {
    setEditingRole(null);
    setRoleForm({ ...DEFAULT_ROLE_FORM, modelProfileId: models.find((model) => model.id && model.enabled !== false)?.id || '' });
    setRoleModalOpen(true);
  };

  const openEditRole = (role: B2.RolePreset) => {
    setEditingRole(role);
    setRoleForm(roleToForm(role));
    setRoleModalOpen(true);
  };

  const saveModel = async () => {
    if (!modelForm.name.trim() || !modelForm.modelId.trim() || !modelForm.baseUrl.trim() || !modelForm.secretRef.trim()) return;
    setSaving(true);
    try {
      const key = createIdempotencyKey();
      const contextWindow = positiveNumber(modelForm.contextWindow);
      const tokenBudget = positiveNumber(modelForm.tokenBudget);
      const saved = editingModel?.id
        ? await adapter.updateModel(editingModel.id, {
          name: modelForm.name.trim(),
          model_id: modelForm.modelId.trim(),
          base_url: modelForm.baseUrl.trim(),
          secret_ref: modelForm.secretRef.trim(),
          context_window: contextWindow ?? null,
          default_token_budget: tokenBudget ?? null,
        }, key)
        : await adapter.createModel({
          name: modelForm.name.trim(),
          model_id: modelForm.modelId.trim(),
          base_url: modelForm.baseUrl.trim(),
          secret_ref: modelForm.secretRef.trim(),
          context_window: contextWindow,
          default_token_budget: tokenBudget,
          supported_efforts: ['low', 'medium', 'high'],
          default_effort: 'medium',
        }, key);
      loadEpoch.current += 1;
      setModels((current) => editingModel?.id
        ? current.map((model) => model.id === saved.id ? saved : model)
        : [...current, saved]);
      setModelModalOpen(false);
      addNotification('success', `模型配置已${editingModel ? '更新' : '创建'}：${saved.name}`);
    } catch (caught: unknown) {
      setError(normalizeB2Error(caught).detail);
    } finally {
      setSaving(false);
    }
  };

  const discover = async () => {
    if (!modelForm.baseUrl.trim() || !modelForm.secretRef.trim()) return;
    setDiscovering(true);
    setError(null);
    try {
      setDiscovery(await adapter.discoverModels({ base_url: modelForm.baseUrl.trim(), secret_ref: modelForm.secretRef.trim() }, createIdempotencyKey()));
    } catch (caught: unknown) {
      setError(normalizeB2Error(caught).detail);
    } finally {
      setDiscovering(false);
    }
  };

  const saveRole = async () => {
    if (!roleForm.name.trim() || !roleForm.systemPrompt.trim() || !roleForm.modelProfileId.trim()) return;
    setSaving(true);
    try {
      const maxTurns = positiveNumber(roleForm.maxTurns);
      const base = {
        name: roleForm.name.trim(),
        system_prompt: roleForm.systemPrompt.trim(),
        model_profile_id: roleForm.modelProfileId.trim(),
        effort: roleForm.effort,
        budget: maxTurns ? { max_turns: maxTurns } : {},
      } satisfies B2.CreateRoleRequest;
      const key = createIdempotencyKey();
      const saved = editingRole?.id
        ? await adapter.updateRole(editingRole.id, base, key)
        : await adapter.createRole({
          ...base,
          // New roles start with no tools and no workspace/process authority.
          tool_policy: { allowed_tools: [], workspace_write: false, command_execution: false },
        }, key);
      loadEpoch.current += 1;
      setRoles((current) => editingRole?.id
        ? current.map((role) => role.id === saved.id ? saved : role)
        : [...current, saved]);
      setRoleModalOpen(false);
      addNotification('success', `RolePreset 已${editingRole ? '更新' : '创建'}：${saved.name}`);
    } catch (caught: unknown) {
      setError(normalizeB2Error(caught).detail);
    } finally {
      setSaving(false);
    }
  };

  const modelName = (id: string) => models.find((model) => model.id === id)?.name || id;

  return (
    <div className="section-view" data-client-mode="live">
      <header className="section-header">
        <div>
          <h1 className="section-title">Agent 与模型配置</h1>
          <p className="section-sub">B2 Core 配置投影 · RolePreset 可编辑，Session/AgentInstance 只读快照</p>
        </div>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => void load()} disabled={loading || connectionStatus !== 'connected'}>
            <RefreshCw size={14} aria-hidden="true" />刷新
          </button>
          <button type="button" className="btn btn-secondary btn-sm" onClick={openNewModel} disabled={loading}>
            <Plus size={14} aria-hidden="true" />添加模型
          </button>
          <button type="button" className="btn btn-primary btn-sm" onClick={openNewRole} disabled={loading || models.length === 0}>
            <Plus size={14} aria-hidden="true" />新建 RolePreset
          </button>
        </div>
      </header>
      {error && <LiveErrorBanner error={error} onRetry={() => void load()} />}
      <div className="section-scroll">
        <div className="section-inner">
          {loading ? (
            <div className="live-panel-loading" role="status"><RefreshCw size={16} className="animate-spin" />正在读取 B2 配置…</div>
          ) : (
            <>
              <section className="b2-config-section" aria-labelledby="b2-models-title">
                <div className="live-section-heading">
                  <h2 id="b2-models-title"><Database size={16} aria-hidden="true" />ModelProfile</h2>
                  <span>{models.length} 个 · Secret 只显示引用名</span>
                </div>
                {models.length === 0 ? (
                  <EmptyState icon={Database} title="暂无模型配置" description="先添加配置，或在表单中通过 Provider Discovery 选择 Core 返回的模型 ID。" />
                ) : (
                  <div className="b2-config-grid">
                    {models.map((model) => (
                      <article className="card b2-config-card" key={model.id || model.model_id}>
                        <div className="b2-config-card-head">
                          <div><h3>{model.name}</h3><p className="section-mono">{model.model_id}</p></div>
                          <StatusBadge status={model.enabled === false ? 'inactive' : 'active'} size="sm" />
                        </div>
                        <dl className="b2-config-meta">
                          <div><dt>Provider</dt><dd>{model.provider || 'openai-compatible'}</dd></div>
                          <div><dt>Base URL</dt><dd>{model.base_url}</dd></div>
                          <div><dt>Secret Ref</dt><dd>{model.secret_ref}</dd></div>
                          {model.context_window && <div><dt>上下文</dt><dd>{model.context_window.toLocaleString()} tokens</dd></div>}
                        </dl>
                        <button type="button" className="btn btn-ghost btn-sm" onClick={() => openEditModel(model)} disabled={!model.id}><Pencil size={13} aria-hidden="true" />编辑配置</button>
                      </article>
                    ))}
                  </div>
                )}
              </section>

              <section className="b2-config-section" aria-labelledby="b2-roles-title">
                <div className="live-section-heading">
                  <h2 id="b2-roles-title"><Bot size={16} aria-hidden="true" />RolePreset</h2>
                  <span>{roles.length} 个 · 版本由 Core 维护</span>
                </div>
                {roles.length === 0 ? (
                  <EmptyState icon={Bot} title="暂无 RolePreset" description="请先配置 ModelProfile，再创建一个可用于 Session 的角色预设。" />
                ) : (
                  <div className="b2-config-grid">
                    {roles.map((role) => (
                      <article className="card b2-config-card" key={`${role.id || role.name}:${role.version || 1}`}>
                        <div className="b2-config-card-head">
                          <div><h3>{role.name}</h3><p>{role.id || 'Core 尚未返回 Role ID'} · v{role.version || 1}</p></div>
                          <StatusBadge status={role.status || 'active'} size="sm" />
                        </div>
                        <dl className="b2-config-meta">
                          <div><dt>模型</dt><dd>{modelName(role.model_profile_id)}</dd></div>
                          <div><dt>Effort</dt><dd>{(role.effort || 'medium').toUpperCase()}</dd></div>
                          <div><dt>工具</dt><dd>{role.tool_policy?.allowed_tools?.length || 0} 个允许工具</dd></div>
                          <div><dt>权限</dt><dd>Workspace 写入 {role.tool_policy?.workspace_write ? '已配置' : '未配置'} · 命令执行 {role.tool_policy?.command_execution ? '已配置' : '未配置'}</dd></div>
                          <div><dt>提示词</dt><dd>{role.system_prompt.slice(0, 100)}{role.system_prompt.length > 100 ? '…' : ''}</dd></div>
                        </dl>
                        <button type="button" className="btn btn-ghost btn-sm" onClick={() => openEditRole(role)} disabled={!role.id}><Pencil size={13} aria-hidden="true" />编辑预设</button>
                      </article>
                    ))}
                  </div>
                )}
              </section>
            </>
          )}
        </div>
      </div>

      <Modal isOpen={modelModalOpen} onClose={() => setModelModalOpen(false)} title={editingModel ? '编辑 ModelProfile' : '添加 ModelProfile'} footer={(
        <><button type="button" className="btn btn-ghost" onClick={() => setModelModalOpen(false)}>取消</button><button type="button" className="btn btn-primary" onClick={() => void saveModel()} disabled={saving || !modelForm.name.trim() || !modelForm.modelId.trim() || !modelForm.baseUrl.trim() || !modelForm.secretRef.trim()}>{saving ? '保存中…' : '保存配置'}</button></>
      )}>
        <div className="b2-config-form">
          <label style={fieldLabel} htmlFor="b2-model-name">配置名称</label><input id="b2-model-name" className="input" value={modelForm.name} onChange={(event) => setModelForm((current) => ({ ...current, name: event.target.value }))} />
          <label style={fieldLabel} htmlFor="b2-model-id">Provider Model ID</label><input id="b2-model-id" className="input section-mono" value={modelForm.modelId} onChange={(event) => setModelForm((current) => ({ ...current, modelId: event.target.value }))} placeholder="从 Discovery 返回中选择" />
          <label style={fieldLabel} htmlFor="b2-model-url">Base URL</label><input id="b2-model-url" className="input" value={modelForm.baseUrl} onChange={(event) => setModelForm((current) => ({ ...current, baseUrl: event.target.value }))} />
          <label style={fieldLabel} htmlFor="b2-model-secret">Secret Ref（环境变量名）</label><input id="b2-model-secret" className="input section-mono" value={modelForm.secretRef} onChange={(event) => setModelForm((current) => ({ ...current, secretRef: event.target.value }))} />
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}><div><label style={fieldLabel} htmlFor="b2-model-context">上下文窗口</label><input id="b2-model-context" className="input" type="number" min={1} value={modelForm.contextWindow} onChange={(event) => setModelForm((current) => ({ ...current, contextWindow: event.target.value }))} /></div><div><label style={fieldLabel} htmlFor="b2-model-budget">默认 Token 预算</label><input id="b2-model-budget" className="input" type="number" min={1} value={modelForm.tokenBudget} onChange={(event) => setModelForm((current) => ({ ...current, tokenBudget: event.target.value }))} /></div></div>
          <div className="b2-config-discovery"><button type="button" className="btn btn-secondary btn-sm" onClick={() => void discover()} disabled={discovering || !modelForm.baseUrl.trim() || !modelForm.secretRef.trim()}><Search size={13} aria-hidden="true" />{discovering ? '发现中…' : 'Provider Discovery'}</button>{discovery && <div className="b2-discovery-result"><strong>Core 返回的模型 ID</strong>{discovery.length === 0 ? <span>没有返回模型 ID</span> : discovery.map((id) => <button type="button" className="b2-discovery-id" key={id} onClick={() => setModelForm((current) => ({ ...current, modelId: id }))}>{id}</button>)}</div>}</div>
          <p className="section-sub">Core 只保存 <code>secret_ref</code> 引用名；GUI 不读取、显示或保存凭据值。</p>
        </div>
      </Modal>

      <Modal isOpen={roleModalOpen} onClose={() => setRoleModalOpen(false)} title={editingRole ? '编辑 RolePreset' : '新建 RolePreset'} footer={(
        <><button type="button" className="btn btn-ghost" onClick={() => setRoleModalOpen(false)}>取消</button><button type="button" className="btn btn-primary" onClick={() => void saveRole()} disabled={saving || !roleForm.name.trim() || !roleForm.systemPrompt.trim() || !roleForm.modelProfileId.trim()}>{saving ? '保存中…' : '保存预设'}</button></>
      )}>
        <div className="b2-config-form">
          <label style={fieldLabel} htmlFor="b2-role-name">名称</label><input id="b2-role-name" className="input" value={roleForm.name} onChange={(event) => setRoleForm((current) => ({ ...current, name: event.target.value }))} />
          <label style={fieldLabel} htmlFor="b2-role-model">ModelProfile</label><select id="b2-role-model" className="select" value={roleForm.modelProfileId} onChange={(event) => setRoleForm((current) => ({ ...current, modelProfileId: event.target.value }))}><option value="">选择模型配置</option>{models.filter((model) => model.id && model.enabled !== false).map((model) => <option key={model.id} value={model.id}>{model.name} · {model.model_id}</option>)}</select>
          <label style={fieldLabel} htmlFor="b2-role-prompt">系统提示词</label><textarea id="b2-role-prompt" className="textarea" rows={5} value={roleForm.systemPrompt} onChange={(event) => setRoleForm((current) => ({ ...current, systemPrompt: event.target.value }))} />
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}><div><label style={fieldLabel} htmlFor="b2-role-effort">Effort</label><select id="b2-role-effort" className="select" value={roleForm.effort} onChange={(event) => setRoleForm((current) => ({ ...current, effort: event.target.value as B2.Effort }))}><option value="low">low</option><option value="medium">medium</option><option value="high">high</option></select></div><div><label style={fieldLabel} htmlFor="b2-role-turns">最大轮次</label><input id="b2-role-turns" className="input" type="number" min={1} value={roleForm.maxTurns} onChange={(event) => setRoleForm((current) => ({ ...current, maxTurns: event.target.value }))} /></div></div>
          <p className="section-sub">新建角色默认没有工具、Workspace 写入或命令执行权限；编辑既有角色只提交名称、提示词、模型、Effort 和预算，已有 ToolPolicy 保持由 Core 管理。</p>
        </div>
      </Modal>
    </div>
  );
};
