import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Bot, Database, Pencil, Plus, RefreshCw, Search, ShieldCheck } from 'lucide-react';
import type * as B2 from '../../../../../sdk/typescript-client/b2.generated';
import { EmptyState } from '../../components/EmptyState';
import { Modal } from '../../components/Modal';
import { StatusBadge } from '../../components/StatusBadge';
import { useOperant } from '../../context/ClientContext';
import { B2LiveAdapter, normalizeB2Error } from '../../live/b2Adapter';
import { createIdempotencyKey } from '../../live/liveState';
import { policyWithRoleTools, ROLE_TOOL_GROUPS, selectedRoleTools } from './roleTools';
import type { SelectableRoleTool } from './roleTools';
import './ui-refine-agents.css';

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
  selectedTools: SelectableRoleTool[];
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
  selectedTools: [],
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
    selectedTools: selectedRoleTools(role.tool_policy),
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
  const [instances, setInstances] = useState<B2.AgentInstance[]>([]);
  const [instanceOffset, setInstanceOffset] = useState(0);
  const [nextInstanceOffset, setNextInstanceOffset] = useState<number | null>(null);
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
      const [nextModels, nextRoles, agentPage] = await Promise.all([adapter.listModels(), adapter.listRoles(), adapter.listAgents(instanceOffset)]);
      if (epoch !== loadEpoch.current) return;
      setModels(nextModels);
      setRoles(nextRoles);
      setInstances(agentPage.items);
      setNextInstanceOffset(agentPage.next_offset);
    } catch (caught: unknown) {
      if (epoch !== loadEpoch.current) return;
      setModels([]);
      setRoles([]);
      setInstances([]);
      setNextInstanceOffset(null);
      setError(normalizeB2Error(caught).detail);
    } finally {
      if (epoch === loadEpoch.current) setLoading(false);
    }
  }, [adapter, instanceOffset]);

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
    const model = models.find((item) => item.id && item.enabled !== false);
    setRoleForm({ ...DEFAULT_ROLE_FORM, modelProfileId: model?.id || '', effort: model?.default_effort || 'medium' });
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
        budget: { ...(editingRole?.budget ?? {}), ...(maxTurns ? { max_turns: maxTurns } : {}) },
        tool_policy: policyWithRoleTools(editingRole?.tool_policy, roleForm.selectedTools),
      } satisfies B2.CreateRoleRequest;
      const key = createIdempotencyKey();
      const saved = editingRole?.id
        ? await adapter.updateRole(editingRole.id, base, key)
        : await adapter.createRole(base, key);
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
    <div className="section-view b2-agents-page" data-client-mode="live">
      <header className="section-header b2-agents-header">
        <div className="b2-agents-header-copy">
          <span className="b2-agents-kicker">Agent 配置</span>
          <h1 className="section-title">模型与角色</h1>
          <p className="section-sub">先配置可用模型，再编辑角色预设；运行实例保留为只读快照。</p>
        </div>
        <div className="b2-agents-header-actions">
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => void load()} disabled={loading || connectionStatus !== 'connected'}>
            <RefreshCw size={14} aria-hidden="true" />刷新
          </button>
          <button type="button" className="btn btn-secondary btn-sm" onClick={openNewModel} disabled={loading}>
            <Plus size={14} aria-hidden="true" />添加模型
          </button>
          <button type="button" className="btn btn-primary btn-sm" onClick={openNewRole} disabled={loading || models.length === 0}>
            <Plus size={14} aria-hidden="true" />新建角色
          </button>
        </div>
      </header>
      {error && <LiveErrorBanner error={error} onRetry={() => void load()} />}
      <div className="section-scroll">
        <div className="section-inner b2-agents-inner">
          {loading ? (
            <div className="live-panel-loading" role="status"><RefreshCw size={16} className="animate-spin" />正在读取模型与角色配置…</div>
          ) : (
            <>
              <section className="b2-agents-section" aria-labelledby="b2-models-title">
                <div className="b2-agents-section-heading">
                  <div className="b2-agents-section-title">
                    <span className="b2-agents-section-icon"><Database size={17} aria-hidden="true" /></span>
                    <div>
                      <h2 id="b2-models-title">模型配置</h2>
                      <p>配置模型连接、能力和默认预算。</p>
                    </div>
                  </div>
                  <div className="b2-agents-section-meta"><span className="b2-agents-count">{models.length} 个</span><span>凭据仅显示引用名</span></div>
                </div>
                {models.length === 0 ? (
                  <div className="b2-agents-empty-shell">
                    <EmptyState icon={Database} title="暂无模型配置" description="先添加一个模型，或在表单中使用 Provider Discovery 选择 Core 返回的精确模型 ID。" />
                  </div>
                ) : (
                  <div className="b2-agents-grid">
                    {models.map((model) => (
                      <article className="card b2-agents-card" key={model.id || model.model_id}>
                        <div className="b2-agents-card-header">
                          <div className="b2-agents-card-heading">
                            <span className="b2-agents-card-kicker">模型配置</span>
                            <h3>{model.name}</h3>
                            <p className="b2-agents-card-subtitle">{model.provider || '兼容 Provider'} · 默认推理强度 {(model.default_effort || 'medium').toUpperCase()}</p>
                          </div>
                          <StatusBadge status={model.enabled === false ? 'inactive' : 'active'} size="sm" />
                        </div>
                        <dl className="b2-agents-card-summary">
                          <div><dt>模型 ID</dt><dd><code>{model.model_id}</code></dd></div>
                          <div><dt>上下文窗口</dt><dd>{model.context_window ? `${model.context_window.toLocaleString()} tokens` : '未设置'}</dd></div>
                          <div><dt>默认预算</dt><dd>{model.default_token_budget ? `${model.default_token_budget.toLocaleString()} tokens` : '未设置'}</dd></div>
                        </dl>
                        <details className="b2-agents-details">
                          <summary>查看连接与能力详情</summary>
                          <dl className="b2-agents-detail-grid">
                            <div><dt>ModelProfile ID</dt><dd><code>{model.id || 'Core 尚未返回 ID'}</code></dd></div>
                            <div><dt>Provider</dt><dd>{model.provider || 'openai-compatible'}</dd></div>
                            <div><dt>Base URL</dt><dd><code>{model.base_url}</code></dd></div>
                            <div><dt>凭据引用</dt><dd><code>{model.secret_ref}</code></dd></div>
                            <div><dt>可用推理强度</dt><dd>{model.supported_efforts?.join('、') || 'Core 未返回'}</dd></div>
                            <div><dt>默认推理强度</dt><dd>{model.default_effort || 'Core 未返回'}</dd></div>
                            <div><dt>输入价格</dt><dd>{model.input_usd_per_million_tokens === undefined || model.input_usd_per_million_tokens === null ? '未设置' : `$${model.input_usd_per_million_tokens}/百万 tokens`}</dd></div>
                            <div><dt>输出价格</dt><dd>{model.output_usd_per_million_tokens === undefined || model.output_usd_per_million_tokens === null ? '未设置' : `$${model.output_usd_per_million_tokens}/百万 tokens`}</dd></div>
                            <div><dt>Provider 参数</dt><dd><code>{model.effort_parameter || '未设置'}</code></dd></div>
                            <div><dt>强度映射</dt><dd>{model.effort_mapping?.map((item) => `${item.effort} → ${item.provider_value}`).join('、') || '未设置'}</dd></div>
                          </dl>
                        </details>
                        <button type="button" className="btn btn-ghost btn-sm b2-agents-card-action" onClick={() => openEditModel(model)} disabled={!model.id}><Pencil size={13} aria-hidden="true" />编辑模型配置</button>
                      </article>
                    ))}
                  </div>
                )}
              </section>

              <section className="b2-agents-section" aria-labelledby="b2-roles-title">
                <div className="b2-agents-section-heading">
                  <div className="b2-agents-section-title">
                    <span className="b2-agents-section-icon"><Bot size={17} aria-hidden="true" /></span>
                    <div>
                      <h2 id="b2-roles-title">角色预设</h2>
                      <p>编辑可复用的角色；每次运行保留创建时的角色快照。</p>
                    </div>
                  </div>
                  <div className="b2-agents-section-meta"><span className="b2-agents-count">{roles.length} 个</span><span>版本由 Core 维护</span></div>
                </div>
                {roles.length === 0 ? (
                  <div className="b2-agents-empty-shell">
                    <EmptyState icon={Bot} title="暂无角色预设" description="请先配置模型，再创建可用于 Session 的角色预设。" />
                  </div>
                ) : (
                  <div className="b2-agents-grid">
                    {roles.map((role) => (
                      <article className="card b2-agents-card" key={`${role.id || role.name}:${role.version || 1}`}>
                        <div className="b2-agents-card-header">
                          <div className="b2-agents-card-heading">
                            <span className="b2-agents-card-kicker">可编辑配置</span>
                            <h3>{role.name}</h3>
                            <p className="b2-agents-card-subtitle">绑定模型：{modelName(role.model_profile_id)} · v{role.version || 1}</p>
                          </div>
                          <StatusBadge status={role.status || 'active'} size="sm" />
                        </div>
                        <dl className="b2-agents-card-summary">
                          <div><dt>推理强度</dt><dd>{(role.effort || 'medium').toUpperCase()}</dd></div>
                          <div><dt>工具能力</dt><dd>{role.tool_policy?.allowed_tools?.length || 0} 个允许工具</dd></div>
                          <div><dt>执行权限</dt><dd>Workspace 写入 {role.tool_policy?.workspace_write ? '已配置' : '未配置'} · 命令执行 {role.tool_policy?.command_execution ? '已配置' : '未配置'}</dd></div>
                          <div><dt>提示词摘要</dt><dd>{role.system_prompt.slice(0, 100)}{role.system_prompt.length > 100 ? '…' : ''}</dd></div>
                        </dl>
                        <details className="b2-agents-details">
                          <summary>查看角色与权限详情</summary>
                          <dl className="b2-agents-detail-grid">
                            <div><dt>RolePreset ID</dt><dd><code>{role.id || 'Core 尚未返回 Role ID'}</code></dd></div>
                            <div><dt>版本</dt><dd>v{role.version || 1}</dd></div>
                            <div><dt>ModelProfile ID</dt><dd><code>{role.model_profile_id}</code></dd></div>
                            <div><dt>记忆范围</dt><dd><code>{role.memory_scope || '未设置'}</code></dd></div>
                            <div><dt>允许工具</dt><dd>{role.tool_policy?.allowed_tools?.join('、') || '无'}</dd></div>
                            <div><dt>需审批能力</dt><dd>{role.tool_policy?.approval_required?.join('、') || '无'}</dd></div>
                            <div><dt>Workspace 写入</dt><dd>{role.tool_policy?.workspace_write ? '已配置' : '未配置'}</dd></div>
                            <div><dt>命令执行</dt><dd>{role.tool_policy?.command_execution ? '已配置' : '未配置'}</dd></div>
                            <div><dt>最大轮次</dt><dd>{role.budget?.max_turns ? `${role.budget.max_turns} 轮` : '未设置'}</dd></div>
                            <div><dt>其他预算</dt><dd>{role.budget?.timeout_seconds ? `超时 ${role.budget.timeout_seconds}s` : '未设置'}</dd></div>
                            <div className="b2-agents-detail-wide"><dt>完整系统提示词</dt><dd className="b2-agents-long-copy">{role.system_prompt}</dd></div>
                          </dl>
                        </details>
                        <button type="button" className="btn btn-ghost btn-sm b2-agents-card-action" onClick={() => openEditRole(role)} disabled={!role.id}><Pencil size={13} aria-hidden="true" />编辑角色预设</button>
                      </article>
                    ))}
                  </div>
                )}
              </section>

              <section className="b2-agents-section" aria-labelledby="b2-instances-title">
                <div className="b2-agents-section-heading">
                  <div className="b2-agents-section-title">
                    <span className="b2-agents-section-icon"><Bot size={17} aria-hidden="true" /></span>
                    <div>
                      <h2 id="b2-instances-title">运行实例</h2>
                      <p>查看已创建的运行实例及当时使用的模型与角色。</p>
                    </div>
                  </div>
                  <div className="b2-agents-section-meta"><span className="b2-agents-count">{instances.length} 条</span><span>按创建时间倒序</span></div>
                </div>
                {instances.length === 0 ? (
                  <div className="b2-agents-empty-inline" role="status">
                    <Bot size={18} aria-hidden="true" />
                    <div><strong>暂无运行实例</strong><span>创建并运行任务后，Core 会在此展示只读实例。</span></div>
                  </div>
                ) : (
                  <div className="b2-agents-grid">
                    {instances.map((agent) => {
                      const snapshot = agent.role_snapshot;
                      return (
                        <article className="card b2-agents-card b2-agents-instance-card" key={agent.id}>
                          <div className="b2-agents-card-header">
                            <div className="b2-agents-card-heading">
                              <span className="b2-agents-card-kicker">只读运行实例</span>
                              <h3>{snapshot.role_name}</h3>
                              <p className="b2-agents-card-subtitle">{snapshot.model_profile_name} · {snapshot.model_id}</p>
                            </div>
                            <StatusBadge status={agent.status || 'created'} label={({ created: '已创建', running: '运行中', completed: '已完成', failed: '已失败', cancelled: '已取消', timed_out: '已超时' } as Record<string, string>)[agent.status || 'created']} size="sm" />
                          </div>
                          <dl className="b2-agents-card-summary">
                            <div><dt>会话</dt><dd><code>{agent.session_id}</code></dd></div>
                            <div><dt>模型</dt><dd>{snapshot.model_id}</dd></div>
                            <div><dt>角色版本</dt><dd>v{snapshot.role_version}</dd></div>
                            <div><dt>权限</dt><dd>Workspace 写入 {snapshot.tool_policy.workspace_write ? '已配置' : '未配置'} · 命令执行 {snapshot.tool_policy.command_execution ? '已配置' : '未配置'}</dd></div>
                          </dl>
                          <details className="b2-agents-details">
                            <summary>查看不可变 RoleSnapshot</summary>
                            <dl className="b2-agents-detail-grid b2-agents-snapshot-grid">
                              <div><dt>Agent ID</dt><dd><code>{agent.id || 'Core 尚未返回 Agent ID'}</code></dd></div>
                              <div><dt>Session ID</dt><dd><code>{agent.session_id}</code></dd></div>
                              <div><dt>Role ID</dt><dd><code>{snapshot.role_id}</code></dd></div>
                              <div><dt>Role 版本</dt><dd>v{snapshot.role_version}</dd></div>
                              <div><dt>ModelProfile</dt><dd>{snapshot.model_profile_name} · <code>{snapshot.model_profile_id}</code></dd></div>
                              <div><dt>Provider</dt><dd>{snapshot.provider}</dd></div>
                              <div><dt>模型 ID</dt><dd><code>{snapshot.model_id}</code></dd></div>
                              <div><dt>Base URL</dt><dd><code>{snapshot.base_url}</code></dd></div>
                              <div><dt>凭据引用</dt><dd><code>{snapshot.secret_ref}</code></dd></div>
                              <div><dt>上下文窗口</dt><dd>{snapshot.context_window ? `${snapshot.context_window.toLocaleString()} tokens` : '未设置'}</dd></div>
                              <div><dt>推理强度</dt><dd>{snapshot.effort}</dd></div>
                              <div><dt>Provider 参数</dt><dd><code>{snapshot.provider_effort_parameter || '未设置'}</code> · {snapshot.provider_effort_value || '未设置'}</dd></div>
                              <div><dt>记忆范围</dt><dd><code>{snapshot.memory_scope}</code></dd></div>
                              <div><dt>允许工具</dt><dd>{snapshot.tool_policy.allowed_tools?.join('、') || '无'}</dd></div>
                              <div><dt>需审批能力</dt><dd>{snapshot.tool_policy.approval_required?.join('、') || '无'}</dd></div>
                              <div><dt>Workspace 写入</dt><dd>{snapshot.tool_policy.workspace_write ? '已配置' : '未配置'}</dd></div>
                              <div><dt>命令执行</dt><dd>{snapshot.tool_policy.command_execution ? '已配置' : '未配置'}</dd></div>
                              <div><dt>最大轮次</dt><dd>{snapshot.budget.max_turns ? `${snapshot.budget.max_turns} 轮` : '未设置'}</dd></div>
                              <div><dt>预算超时</dt><dd>{snapshot.budget.timeout_seconds ? `${snapshot.budget.timeout_seconds}s` : '未设置'}</dd></div>
                              {snapshot.captured_at && <div><dt>快照时间</dt><dd>{snapshot.captured_at}</dd></div>}
                              {snapshot.overrides && <div><dt>会话覆盖</dt><dd>{snapshot.overrides.budget_fields?.join('、') || '无'}</dd></div>}
                              <div className="b2-agents-detail-wide"><dt>快照系统提示词</dt><dd className="b2-agents-long-copy">{snapshot.system_prompt}</dd></div>
                            </dl>
                          </details>
                        </article>
                      );
                    })}
                  </div>
                )}
                <div className="live-empty-actions b2-agents-pagination">
                  <button type="button" className="btn btn-secondary" disabled={loading || instanceOffset === 0} onClick={() => setInstanceOffset(Math.max(0, instanceOffset - 100))}>上一页</button>
                  <button type="button" className="btn btn-secondary" disabled={loading || nextInstanceOffset === null} onClick={() => { if (nextInstanceOffset !== null) setInstanceOffset(nextInstanceOffset); }}>下一页</button>
                </div>
              </section>
            </>
          )}
        </div>
      </div>

      <Modal portal isOpen={modelModalOpen} onClose={() => setModelModalOpen(false)} title={editingModel ? '编辑模型配置' : '添加模型配置'} footer={(
        <><button type="button" className="btn btn-ghost" onClick={() => setModelModalOpen(false)}>取消</button><button type="button" className="btn btn-primary" onClick={() => void saveModel()} disabled={saving || !modelForm.name.trim() || !modelForm.modelId.trim() || !modelForm.baseUrl.trim() || !modelForm.secretRef.trim()}>{saving ? '保存中…' : '保存配置'}</button></>
      )}>
        <div className="b2-agents-modal-content b2-agents-form">
          <fieldset className="b2-agents-fieldset">
            <legend>连接信息</legend>
            <div className="b2-agents-field-grid">
              <div className="b2-agents-field">
                <label htmlFor="b2-model-name">配置名称</label>
                <input id="b2-model-name" className="input" value={modelForm.name} onChange={(event) => setModelForm((current) => ({ ...current, name: event.target.value }))} />
                <p>用于在角色和任务中识别这份模型配置。</p>
              </div>
              <div className="b2-agents-field">
                <label htmlFor="b2-model-id">Provider Model ID</label>
                <input id="b2-model-id" className="input section-mono" value={modelForm.modelId} onChange={(event) => setModelForm((current) => ({ ...current, modelId: event.target.value }))} placeholder="填写 Discovery 返回的精确 ID" />
                <p>保持 Provider Discovery 返回的原始模型 ID，不要手动改写。</p>
              </div>
              <div className="b2-agents-field">
                <label htmlFor="b2-model-url">Base URL</label>
                <input id="b2-model-url" className="input" value={modelForm.baseUrl} onChange={(event) => setModelForm((current) => ({ ...current, baseUrl: event.target.value }))} />
                <p>用于读取模型目录并发送正式请求。</p>
              </div>
              <div className="b2-agents-field">
                <label htmlFor="b2-model-secret">Secret Ref（环境变量名）</label>
                <input id="b2-model-secret" className="input section-mono" value={modelForm.secretRef} onChange={(event) => setModelForm((current) => ({ ...current, secretRef: event.target.value }))} />
                <p>只填写环境变量名；GUI 不读取、显示或保存凭据值。</p>
              </div>
            </div>
          </fieldset>
          <fieldset className="b2-agents-fieldset">
            <legend>资源默认值</legend>
            <div className="b2-agents-field-grid b2-agents-field-grid-2">
              <div className="b2-agents-field">
                <label htmlFor="b2-model-context">上下文窗口</label>
                <input id="b2-model-context" className="input" type="number" min={1} value={modelForm.contextWindow} onChange={(event) => setModelForm((current) => ({ ...current, contextWindow: event.target.value }))} />
                <p>留空表示不指定上下文窗口。</p>
              </div>
              <div className="b2-agents-field">
                <label htmlFor="b2-model-budget">默认 Token 预算</label>
                <input id="b2-model-budget" className="input" type="number" min={1} value={modelForm.tokenBudget} onChange={(event) => setModelForm((current) => ({ ...current, tokenBudget: event.target.value }))} />
                <p>角色未另行设置预算时使用。</p>
              </div>
            </div>
          </fieldset>
          <section className="b2-agents-discovery" aria-labelledby="b2-model-discovery-title">
            <div className="b2-agents-discovery-head">
              <div>
                <h3 id="b2-model-discovery-title">发现可用模型</h3>
                <p>从当前连接读取 Core 返回的模型 ID，点击后填入表单。</p>
              </div>
              <button type="button" className="btn btn-secondary btn-sm" onClick={() => void discover()} disabled={discovering || !modelForm.baseUrl.trim() || !modelForm.secretRef.trim()}><Search size={13} aria-hidden="true" />{discovering ? '发现中…' : 'Provider Discovery'}</button>
            </div>
            {discovery && <div className="b2-agents-discovery-result"><strong>Core 返回的模型 ID</strong>{discovery.length === 0 ? <span>没有返回模型 ID</span> : discovery.map((id) => <button type="button" className="b2-agents-discovery-id" key={id} onClick={() => setModelForm((current) => ({ ...current, modelId: id }))}>{id}</button>)}</div>}
          </section>
          <p className="b2-agents-form-note">Core 只保存 <code>secret_ref</code> 引用名；GUI 不读取、显示或保存凭据值。</p>
        </div>
      </Modal>

      <Modal portal isOpen={roleModalOpen} onClose={() => setRoleModalOpen(false)} title={editingRole ? '编辑角色预设' : '新建角色预设'} footer={(
        <><button type="button" className="btn btn-ghost" onClick={() => setRoleModalOpen(false)}>取消</button><button type="button" className="btn btn-primary" onClick={() => void saveRole()} disabled={saving || !roleForm.name.trim() || !roleForm.systemPrompt.trim() || !roleForm.modelProfileId.trim()}>{saving ? '保存中…' : '保存预设'}</button></>
      )}>
        <div className="b2-agents-modal-content b2-agents-form">
          <fieldset className="b2-agents-fieldset">
            <legend>角色身份</legend>
            <div className="b2-agents-field-grid">
              <div className="b2-agents-field">
                <label htmlFor="b2-role-name">角色名称</label>
                <input id="b2-role-name" className="input" value={roleForm.name} onChange={(event) => setRoleForm((current) => ({ ...current, name: event.target.value }))} />
                <p>用于任务选择和运行实例识别。</p>
              </div>
              <div className="b2-agents-field">
                <label htmlFor="b2-role-model">绑定模型配置</label>
                <select id="b2-role-model" className="select" value={roleForm.modelProfileId} onChange={(event) => setRoleForm((current) => ({ ...current, modelProfileId: event.target.value, effort: models.find((model) => model.id === event.target.value)?.default_effort || current.effort }))}><option value="">选择模型配置</option>{models.filter((model) => model.id && model.enabled !== false).map((model) => <option key={model.id} value={model.id}>{model.name} · {model.model_id}</option>)}</select>
                <p>只显示 Core 返回且仍启用的 ModelProfile。</p>
              </div>
            </div>
          </fieldset>
          <fieldset className="b2-agents-fieldset">
            <legend>行为说明</legend>
            <div className="b2-agents-field">
              <label htmlFor="b2-role-prompt">系统提示词</label>
              <textarea id="b2-role-prompt" className="textarea" rows={5} value={roleForm.systemPrompt} onChange={(event) => setRoleForm((current) => ({ ...current, systemPrompt: event.target.value }))} />
              <p>定义角色的职责和输出边界；运行后会保存到不可变 RoleSnapshot。</p>
            </div>
          </fieldset>
          <fieldset className="b2-agents-fieldset">
            <legend>执行参数</legend>
            <div className="b2-agents-field-grid b2-agents-field-grid-2">
              <div className="b2-agents-field">
                <label htmlFor="b2-role-effort">推理强度（Effort）</label>
                <select id="b2-role-effort" className="select" value={roleForm.effort} onChange={(event) => setRoleForm((current) => ({ ...current, effort: event.target.value as B2.Effort }))}>{(models.find((model) => model.id === roleForm.modelProfileId)?.supported_efforts ?? []).map((effort) => <option key={effort} value={effort}>{effort}</option>)}</select>
                <p>可选项来自绑定 ModelProfile 的能力声明。</p>
              </div>
              <div className="b2-agents-field">
                <label htmlFor="b2-role-turns">最大轮次</label>
                <input id="b2-role-turns" className="input" type="number" min={1} value={roleForm.maxTurns} onChange={(event) => setRoleForm((current) => ({ ...current, maxTurns: event.target.value }))} />
                <p>达到上限后由 Core 停止该角色的继续执行。</p>
              </div>
            </div>
          </fieldset>
          <fieldset className="b2-agents-fieldset b2-agents-tools-fieldset">
            <legend>允许的工具</legend>
            <p>默认不授予工具。只勾选此角色需要的能力；文件工具为只读，协作工具可创建和联系子 Agent。</p>
            {ROLE_TOOL_GROUPS.map((group) => <div className="b2-agents-tool-group" key={group.title}>
              <h3>{group.title}</h3>
              <div className="b2-agents-tool-options">{group.tools.map((tool) => <label key={tool.id}>
                <input type="checkbox" checked={roleForm.selectedTools.includes(tool.id)} onChange={(event) => setRoleForm((current) => ({
                  ...current,
                  selectedTools: event.target.checked
                    ? [...current.selectedTools, tool.id]
                    : current.selectedTools.filter((value) => value !== tool.id),
                }))} />
                <span>{tool.label} <code>{tool.id}</code></span>
              </label>)}</div>
            </div>)}
          </fieldset>
          <p className="b2-agents-form-note">权限变更只进入新建 Session 的不可变快照；已有运行不会改变。编辑时其他工具、Workspace 写入、命令执行、审批和执行器设置保持原值。</p>
        </div>
      </Modal>
    </div>
  );
};
