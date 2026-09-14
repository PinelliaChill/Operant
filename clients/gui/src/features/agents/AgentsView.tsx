/**
 * Agent 分区（设计基线 §6，中深页；v6 §角色预设并入 Agent 页）
 * Agent 卡网格（彩色方块头像首字、名称、model 等宽字体、roleDesc、
 * 状态徽章 在线/忙碌/离线、capabilities chips）；
 * 点击卡片打开详情 Drawer（复用 components/Drawer：完整描述、能力清单、模型、状态）。
 * v6：设置「角色预设」字段（名称/模型/系统提示词/工具权限/最大轮次）并入本页——
 * 详情抽屉补充只读展示并在底部提供「编辑」切入编辑态表单；页头「+ 新建 Agent」
 * 弹出 Modal 表单；保存统一经 DemoContext.upsertAgent（按 id 更新或追加）。
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Bot, Pencil, Plus } from 'lucide-react';
import { Drawer } from '../../components/Drawer';
import { EmptyState } from '../../components/EmptyState';
import { Modal } from '../../components/Modal';
import { useDemo } from '../../demo/DemoContext';
import type { DemoAgent } from '../../demo/types';
import { useOperant } from '../../context/ClientContext';
import { LiveAgentsView } from './LiveAgentsView';

/** Agent 状态 → 中文标签（online/busy/offline → 在线/忙碌/离线） */
const AGENT_STATUS_LABELS: Record<DemoAgent['status'], string> = {
  online: '在线',
  busy: '忙碌',
  offline: '离线',
};

const AgentStatusPill: React.FC<{ status: DemoAgent['status'] }> = ({ status }) => (
  <span className={`agent-status ${status}`}>
    <span className="agent-status-dot" aria-hidden="true" />
    {AGENT_STATUS_LABELS[status]}
  </span>
);

/** 新建 Agent 头像取色板（口径对齐现有卡片用色，按 Agent 数量轮转取色） */
const AGENT_COLOR_PALETTE = ['#2563eb', '#7c3aed', '#ea580c', '#059669'];

/** 可用工具选项（口径对齐演示种子 toolPolicy.allowedTools 的并集） */
const AGENT_TOOL_OPTIONS = ['read_file', 'search_files', 'apply_patch', 'run_command', 'git_diff'];

/** 需审批动作类别选项（口径对齐演示种子 toolPolicy.approvalRequired 的并集） */
const AGENT_APPROVAL_OPTIONS = ['git_write', 'destructive', 'privileged'];

/** 新建 Agent 默认最大轮次 */
const DEFAULT_MAX_TURNS = 10;

/** 表单字段标签统一样式 */
const FIELD_LABEL_STYLE: React.CSSProperties = {
  fontSize: '11px',
  fontWeight: 600,
  color: 'var(--text-muted)',
  display: 'block',
  marginBottom: 4,
};

/** 行内错误提示样式 */
const FIELD_ERROR_STYLE: React.CSSProperties = {
  margin: '4px 0 0',
  fontSize: '11px',
  color: 'var(--status-error-text)',
};

/** 复选组容器样式 */
const CHECKBOX_GROUP_STYLE: React.CSSProperties = {
  display: 'flex',
  flexWrap: 'wrap',
  gap: '6px 14px',
};

const CHECKBOX_LABEL_STYLE: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: 4,
  fontSize: 12,
  color: 'var(--text-secondary)',
  cursor: 'pointer',
};

/** Agent 表单值（maxTurns 以字符串承载，提交时校验解析） */
interface AgentFormValue {
  name: string;
  model: string;
  roleDesc: string;
  systemPrompt: string;
  workspaceWrite: boolean;
  commandExecution: boolean;
  allowedTools: string[];
  approvalRequired: string[];
  maxTurns: string;
}

interface AgentFormErrors {
  name?: string;
  model?: string;
  maxTurns?: string;
}

type ValidatedField = 'name' | 'model' | 'maxTurns';
const VALIDATED_FIELDS: ValidatedField[] = ['name', 'model', 'maxTurns'];

/** 编辑入口：DemoAgent → 表单值（缺省字段按演示口径兜底） */
function toFormValue(agent: DemoAgent): AgentFormValue {
  return {
    name: agent.name,
    model: agent.model,
    roleDesc: agent.roleDesc,
    systemPrompt: agent.systemPrompt ?? '',
    workspaceWrite: agent.toolPolicy?.workspaceWrite ?? false,
    commandExecution: agent.toolPolicy?.commandExecution ?? false,
    allowedTools: agent.toolPolicy?.allowedTools ?? [],
    approvalRequired: agent.toolPolicy?.approvalRequired ?? [],
    maxTurns: agent.maxTurns !== undefined ? String(agent.maxTurns) : String(DEFAULT_MAX_TURNS),
  };
}

/** 新建入口：表单默认值（status 固定 online、capabilities 空由保存时补充） */
function createEmptyFormValue(): AgentFormValue {
  return {
    name: '',
    model: '',
    roleDesc: '',
    systemPrompt: '',
    workspaceWrite: false,
    commandExecution: false,
    allowedTools: [],
    approvalRequired: [],
    maxTurns: String(DEFAULT_MAX_TURNS),
  };
}

/** 提交/失焦校验：名称必填非空、模型必填、最大轮次为 ≥1 整数 */
function validateForm(form: AgentFormValue): AgentFormErrors {
  const errors: AgentFormErrors = {};
  if (form.name.trim().length === 0) {
    errors.name = '请输入 Agent 名称';
  }
  if (form.model.trim().length === 0) {
    errors.model = '请输入模型 ID';
  }
  const turns = Number(form.maxTurns);
  if (!Number.isInteger(turns) || turns < 1) {
    errors.maxTurns = '最大轮次需为不小于 1 的整数';
  }
  return errors;
}

/** 变更表单值；已报错的字段随输入即时复核，未报错字段不提前暴露错误 */
function applyFormPatch(
  form: AgentFormValue,
  errors: AgentFormErrors,
  patch: Partial<AgentFormValue>
): { form: AgentFormValue; errors: AgentFormErrors } {
  const nextForm = { ...form, ...patch };
  const allErrors = validateForm(nextForm);
  const nextErrors: AgentFormErrors = { ...errors };
  VALIDATED_FIELDS.forEach((field) => {
    if (field in patch && nextErrors[field] !== undefined) {
      nextErrors[field] = allErrors[field];
    }
  });
  return { form: nextForm, errors: nextErrors };
}

/** 失焦校验单个字段（只更新该字段错误，不影响其他字段） */
function blurValidateField(
  form: AgentFormValue,
  errors: AgentFormErrors,
  field: ValidatedField
): AgentFormErrors {
  const nextErrors: AgentFormErrors = { ...errors };
  nextErrors[field] = validateForm(form)[field];
  return nextErrors;
}

/** 复选列表项切换 */
function toggleListItem(list: string[], item: string, checked: boolean): string[] {
  return checked ? [...list, item] : list.filter((t) => t !== item);
}

/** 校验通过的表单值 → DemoAgent 可写字段（id/color/capabilities/status 由调用方补充） */
function buildAgentFields(form: AgentFormValue) {
  return {
    name: form.name.trim(),
    model: form.model.trim(),
    roleDesc: form.roleDesc.trim(),
    systemPrompt: form.systemPrompt,
    toolPolicy: {
      allowedTools: form.allowedTools,
      workspaceWrite: form.workspaceWrite,
      commandExecution: form.commandExecution,
      approvalRequired: form.approvalRequired,
    },
    maxTurns: Number(form.maxTurns),
  };
}

/** 编辑/新建共用的受控表单字段（标签经 htmlFor 关联，错误提示行内展示） */
const AgentFormFields: React.FC<{
  idPrefix: string;
  value: AgentFormValue;
  errors: AgentFormErrors;
  toolOptions: string[];
  approvalOptions: string[];
  onChange: (patch: Partial<AgentFormValue>) => void;
  onBlurField: (field: ValidatedField) => void;
}> = ({ idPrefix, value, errors, toolOptions, approvalOptions, onChange, onBlurField }) => (
  <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
    <div>
      <label style={FIELD_LABEL_STYLE} htmlFor={`${idPrefix}-name`}>
        名称
      </label>
      <input
        id={`${idPrefix}-name`}
        type="text"
        className="input"
        value={value.name}
        autoFocus
        onChange={(e) => onChange({ name: e.target.value })}
        onBlur={() => onBlurField('name')}
        aria-invalid={errors.name ? true : undefined}
        aria-describedby={errors.name ? `${idPrefix}-name-error` : undefined}
      />
      {errors.name && (
        <p id={`${idPrefix}-name-error`} style={FIELD_ERROR_STYLE}>
          {errors.name}
        </p>
      )}
    </div>

    <div>
      <label style={FIELD_LABEL_STYLE} htmlFor={`${idPrefix}-model`}>
        模型
      </label>
      <input
        id={`${idPrefix}-model`}
        type="text"
        className="input section-mono"
        value={value.model}
        placeholder="Provider Discovery 返回的模型 ID"
        onChange={(e) => onChange({ model: e.target.value })}
        onBlur={() => onBlurField('model')}
        aria-invalid={errors.model ? true : undefined}
        aria-describedby={errors.model ? `${idPrefix}-model-error` : undefined}
      />
      {errors.model && (
        <p id={`${idPrefix}-model-error`} style={FIELD_ERROR_STYLE}>
          {errors.model}
        </p>
      )}
    </div>

    <div>
      <label style={FIELD_LABEL_STYLE} htmlFor={`${idPrefix}-role-desc`}>
        职责简述
      </label>
      <input
        id={`${idPrefix}-role-desc`}
        type="text"
        className="input"
        value={value.roleDesc}
        onChange={(e) => onChange({ roleDesc: e.target.value })}
      />
    </div>

    <div>
      <label style={FIELD_LABEL_STYLE} htmlFor={`${idPrefix}-system-prompt`}>
        系统提示词
      </label>
      <textarea
        id={`${idPrefix}-system-prompt`}
        className="textarea"
        rows={4}
        value={value.systemPrompt}
        placeholder="描述该角色的行为边界与工作方式"
        onChange={(e) => onChange({ systemPrompt: e.target.value })}
      />
    </div>

    <div>
      <span id={`${idPrefix}-policy-label`} style={FIELD_LABEL_STYLE}>
        工具权限
      </span>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {/* 写入权限：双选（允许写入 / 只读），对齐原角色预设「写入权限」口径 */}
        <div role="group" aria-label="写入权限" style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 12, color: 'var(--text-secondary)' }}>写入权限</span>
          <div style={{ display: 'inline-flex', gap: 6 }}>
            <button
              type="button"
              className={`section-chip${value.workspaceWrite ? ' active' : ''}`}
              aria-pressed={value.workspaceWrite}
              onClick={() => onChange({ workspaceWrite: true })}
            >
              允许写入
            </button>
            <button
              type="button"
              className={`section-chip${value.workspaceWrite ? '' : ' active'}`}
              aria-pressed={!value.workspaceWrite}
              onClick={() => onChange({ workspaceWrite: false })}
            >
              只读
            </button>
          </div>
        </div>

        {/* 命令执行：双选（允许执行 / 禁止执行） */}
        <div role="group" aria-label="命令执行" style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 12, color: 'var(--text-secondary)' }}>命令执行</span>
          <div style={{ display: 'inline-flex', gap: 6 }}>
            <button
              type="button"
              className={`section-chip${value.commandExecution ? ' active' : ''}`}
              aria-pressed={value.commandExecution}
              onClick={() => onChange({ commandExecution: true })}
            >
              允许执行
            </button>
            <button
              type="button"
              className={`section-chip${value.commandExecution ? '' : ' active'}`}
              aria-pressed={!value.commandExecution}
              onClick={() => onChange({ commandExecution: false })}
            >
              禁止执行
            </button>
          </div>
        </div>

        {/* 可用工具：复选组（编辑时并入 Agent 现有工具名，避免丢失未知工具） */}
        <div>
          <span id={`${idPrefix}-tools-label`} style={{ ...FIELD_LABEL_STYLE, marginBottom: 0 }}>
            可用工具
          </span>
          <div role="group" aria-labelledby={`${idPrefix}-tools-label`} style={CHECKBOX_GROUP_STYLE}>
            {toolOptions.map((tool) => (
              <label key={tool} htmlFor={`${idPrefix}-tool-${tool}`} style={CHECKBOX_LABEL_STYLE}>
                <input
                  id={`${idPrefix}-tool-${tool}`}
                  type="checkbox"
                  checked={value.allowedTools.includes(tool)}
                  onChange={(e) =>
                    onChange({ allowedTools: toggleListItem(value.allowedTools, tool, e.target.checked) })
                  }
                />
                <span className="section-mono">{tool}</span>
              </label>
            ))}
          </div>
        </div>

        {/* 需审批动作：复选组（编辑时并入 Agent 现有类别，避免丢失未知类别） */}
        <div>
          <span id={`${idPrefix}-approval-label`} style={{ ...FIELD_LABEL_STYLE, marginBottom: 0 }}>
            需审批动作
          </span>
          <div role="group" aria-labelledby={`${idPrefix}-approval-label`} style={CHECKBOX_GROUP_STYLE}>
            {approvalOptions.map((kind) => (
              <label key={kind} htmlFor={`${idPrefix}-approval-${kind}`} style={CHECKBOX_LABEL_STYLE}>
                <input
                  id={`${idPrefix}-approval-${kind}`}
                  type="checkbox"
                  checked={value.approvalRequired.includes(kind)}
                  onChange={(e) =>
                    onChange({
                      approvalRequired: toggleListItem(value.approvalRequired, kind, e.target.checked),
                    })
                  }
                />
                <span className="section-mono">{kind}</span>
              </label>
            ))}
          </div>
        </div>
      </div>
    </div>

    <div>
      <label style={FIELD_LABEL_STYLE} htmlFor={`${idPrefix}-max-turns`}>
        最大轮次
      </label>
      <input
        id={`${idPrefix}-max-turns`}
        type="number"
        className="input"
        min={1}
        step={1}
        value={value.maxTurns}
        onChange={(e) => onChange({ maxTurns: e.target.value })}
        onBlur={() => onBlurField('maxTurns')}
        aria-invalid={errors.maxTurns ? true : undefined}
        aria-describedby={errors.maxTurns ? `${idPrefix}-max-turns-error` : undefined}
      />
      {errors.maxTurns && (
        <p id={`${idPrefix}-max-turns-error`} style={FIELD_ERROR_STYLE}>
          {errors.maxTurns}
        </p>
      )}
    </div>
  </div>
);

export const AgentsView: React.FC = () => {
  const { clientMode } = useOperant();
  if (clientMode === 'live') {
    return <LiveAgentsView />;
  }
  return <DemoAgentsView />;
};

const DemoAgentsView: React.FC = () => {
  const { agents, upsertAgent } = useDemo();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected = agents.find((a) => a.id === selectedId);

  // 详情抽屉编辑态
  const [editing, setEditing] = useState(false);
  const [editForm, setEditForm] = useState<AgentFormValue | null>(null);
  const [editErrors, setEditErrors] = useState<AgentFormErrors>({});

  // 新建 Agent Modal
  const [createOpen, setCreateOpen] = useState(false);
  const [createForm, setCreateForm] = useState<AgentFormValue>(createEmptyFormValue);
  const [createErrors, setCreateErrors] = useState<AgentFormErrors>({});

  // 编辑表单卸载（保存/取消/关闭）后把焦点还给「编辑」按钮，保持键盘可达；
  // 仅在「编辑态 → 详情态」切换时触发，不抢占抽屉打开时的初始焦点
  const editButtonRef = useRef<HTMLButtonElement | null>(null);
  const wasEditingRef = useRef(false);
  useEffect(() => {
    if (selected && wasEditingRef.current && !editing) {
      editButtonRef.current?.focus();
    }
    wasEditingRef.current = editing;
  }, [selected, editing]);

  // 编辑态选项 = 已知选项 ∪ 该 Agent 现有取值（避免保存时丢失未知工具/类别）
  const editToolOptions = useMemo(() => {
    const current = selected?.toolPolicy?.allowedTools ?? [];
    return Array.from(new Set([...AGENT_TOOL_OPTIONS, ...current]));
  }, [selected]);

  const editApprovalOptions = useMemo(() => {
    const current = selected?.toolPolicy?.approvalRequired ?? [];
    return Array.from(new Set([...AGENT_APPROVAL_OPTIONS, ...current]));
  }, [selected]);

  const changeEditForm = (patch: Partial<AgentFormValue>) => {
    if (!editForm) return;
    const next = applyFormPatch(editForm, editErrors, patch);
    setEditForm(next.form);
    setEditErrors(next.errors);
  };

  const blurEditField = (field: ValidatedField) => {
    if (!editForm) return;
    setEditErrors(blurValidateField(editForm, editErrors, field));
  };

  const startEdit = () => {
    if (!selected) return;
    setEditForm(toFormValue(selected));
    setEditErrors({});
    setEditing(true);
  };

  // 退出编辑态并丢弃未保存改动（setState 引用稳定，可安全作为 onClose 依赖）
  const exitEdit = useCallback(() => {
    setEditing(false);
    setEditForm(null);
    setEditErrors({});
  }, []);

  const saveEdit = () => {
    if (!selected || !editForm) return;
    const errors = validateForm(editForm);
    if (errors.name || errors.model || errors.maxTurns) {
      setEditErrors(errors);
      return;
    }
    upsertAgent({ ...selected, ...buildAgentFields(editForm) });
    exitEdit();
  };

  // onClose 必须引用稳定：useDialogA11y 的 effect 依赖 [isOpen, onClose]，
  // 若每次渲染传入新闭包，编辑表单每敲一个键都会触发 effect 重跑并抢走输入焦点
  const closeDrawer = useCallback(() => {
    setSelectedId(null);
    exitEdit();
  }, [exitEdit]);

  const closeCreate = useCallback(() => setCreateOpen(false), []);

  const changeCreateForm = (patch: Partial<AgentFormValue>) => {
    const next = applyFormPatch(createForm, createErrors, patch);
    setCreateForm(next.form);
    setCreateErrors(next.errors);
  };

  const blurCreateField = (field: ValidatedField) => {
    setCreateErrors(blurValidateField(createForm, createErrors, field));
  };

  const openCreate = () => {
    setCreateForm(createEmptyFormValue());
    setCreateErrors({});
    setCreateOpen(true);
  };

  const saveCreate = () => {
    const errors = validateForm(createForm);
    if (errors.name || errors.model || errors.maxTurns) {
      setCreateErrors(errors);
      return;
    }
    upsertAgent({
      id: `agent_${Date.now()}`,
      color: AGENT_COLOR_PALETTE[agents.length % AGENT_COLOR_PALETTE.length],
      capabilities: [],
      status: 'online',
      ...buildAgentFields(createForm),
    });
    setCreateOpen(false);
  };

  return (
    <div className="section-view">
      <header className="section-header">
        <div
          style={{
            display: 'flex',
            alignItems: 'flex-start',
            justifyContent: 'space-between',
            gap: 12,
            flexWrap: 'wrap',
          }}
        >
          <div>
            <h1 className="section-title">Agent</h1>
            <p className="section-sub">共 {agents.length} 位 Agent</p>
          </div>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            onClick={openCreate}
            aria-haspopup="dialog"
          >
            <Plus size={13} aria-hidden="true" />
            <span>新建 Agent</span>
          </button>
        </div>
      </header>

      <div className="section-scroll">
        <div className="section-inner">
          {agents.length === 0 ? (
            <div className="section-empty-wrap">
              <EmptyState
                icon={Bot}
                title="暂无 Agent"
                description="演示数据中还没有任何 Agent。"
              />
            </div>
          ) : (
            <div className="section-card-grid">
              {agents.map((a) => (
                <button
                  key={a.id}
                  type="button"
                  className="section-card"
                  onClick={() => setSelectedId(a.id)}
                  aria-haspopup="dialog"
                  aria-label={`查看 Agent ${a.name} 详情`}
                >
                  <span className="section-card-head">
                    <span
                      className="section-card-icon"
                      style={{ backgroundColor: a.color }}
                      aria-hidden="true"
                    >
                      {a.name.charAt(0)}
                    </span>
                    <span className="section-card-title">{a.name}</span>
                    <span className="section-card-status">
                      <AgentStatusPill status={a.status} />
                    </span>
                  </span>
                  <span className="section-card-meta section-mono">{a.model}</span>
                  <span className="section-card-desc">{a.roleDesc}</span>
                  <span className="section-chip-list">
                    {a.capabilities.map((cap) => (
                      <span key={cap} className="chat-source-tag">
                        {cap}
                      </span>
                    ))}
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>

      <Drawer
        isOpen={Boolean(selected)}
        onClose={closeDrawer}
        title={selected ? (editing ? `编辑 Agent：${selected.name}` : selected.name) : 'Agent 详情'}
      >
        {selected && !editing && (
          <div className="agent-detail">
            <div className="agent-detail-head">
              <span
                className="section-detail-icon"
                style={{ backgroundColor: selected.color }}
                aria-hidden="true"
              >
                {selected.name.charAt(0)}
              </span>
              <div className="section-detail-main">
                <div className="section-card-title">{selected.name}</div>
                <div className="section-card-meta section-mono">{selected.model}</div>
              </div>
              <AgentStatusPill status={selected.status} />
            </div>

            <div className="agent-detail-field">
              <span className="agent-detail-label">角色职责</span>
              <p className="agent-detail-value">{selected.roleDesc}</p>
            </div>

            {selected.systemPrompt && (
              <div className="agent-detail-field">
                <span className="agent-detail-label">系统提示词</span>
                <p className="agent-detail-value">{selected.systemPrompt}</p>
              </div>
            )}

            <div className="agent-detail-field">
              <span className="agent-detail-label">模型</span>
              <span className="agent-detail-value section-mono">{selected.model}</span>
            </div>

            <div className="agent-detail-field">
              <span className="agent-detail-label">状态</span>
              <div className="agent-detail-value">
                <AgentStatusPill status={selected.status} />
              </div>
            </div>

            {selected.toolPolicy && (
              <div className="agent-detail-field">
                <span className="agent-detail-label">工具权限</span>
                <div className="agent-detail-value" style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                  <span>写入权限：{selected.toolPolicy.workspaceWrite ? '允许写入' : '只读'}</span>
                  <span>命令执行：{selected.toolPolicy.commandExecution ? '允许' : '禁止'}</span>
                  {selected.toolPolicy.allowedTools.length > 0 && (
                    <span style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                      可用工具：
                      {selected.toolPolicy.allowedTools.map((tool) => (
                        <span key={tool} className="chat-source-tag section-mono">
                          {tool}
                        </span>
                      ))}
                    </span>
                  )}
                  {selected.toolPolicy.approvalRequired.length > 0 && (
                    <span style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                      需审批动作：
                      {selected.toolPolicy.approvalRequired.map((kind) => (
                        <span key={kind} className="chat-source-tag section-mono">
                          {kind}
                        </span>
                      ))}
                    </span>
                  )}
                </div>
              </div>
            )}

            {selected.maxTurns !== undefined && (
              <div className="agent-detail-field">
                <span className="agent-detail-label">最大轮次</span>
                <span className="agent-detail-value">{selected.maxTurns} 轮</span>
              </div>
            )}

            <div className="agent-detail-field">
              <span className="agent-detail-label">能力清单</span>
              <div className="section-chip-list">
                {selected.capabilities.map((cap) => (
                  <span key={cap} className="chat-source-tag">
                    {cap}
                  </span>
                ))}
              </div>
            </div>

            <div
              style={{
                borderTop: '1px solid var(--border-subtle)',
                paddingTop: 12,
                display: 'flex',
                justifyContent: 'flex-end',
              }}
            >
              <button
                ref={editButtonRef}
                type="button"
                className="btn btn-secondary btn-sm"
                onClick={startEdit}
              >
                <Pencil size={13} aria-hidden="true" />
                <span>编辑</span>
              </button>
            </div>
          </div>
        )}

        {selected && editing && editForm && (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              saveEdit();
            }}
            noValidate
            aria-label={`编辑 Agent ${selected.name}`}
            style={{ display: 'flex', flexDirection: 'column', gap: 16 }}
          >
            <AgentFormFields
              idPrefix="agent-edit"
              value={editForm}
              errors={editErrors}
              toolOptions={editToolOptions}
              approvalOptions={editApprovalOptions}
              onChange={changeEditForm}
              onBlurField={blurEditField}
            />
            <div
              style={{
                display: 'flex',
                justifyContent: 'flex-end',
                gap: 8,
                borderTop: '1px solid var(--border-subtle)',
                paddingTop: 12,
              }}
            >
              <button type="button" className="btn btn-secondary" onClick={exitEdit}>
                取消
              </button>
              <button type="submit" className="btn btn-primary">
                保存
              </button>
            </div>
          </form>
        )}
      </Drawer>

      <Modal isOpen={createOpen} onClose={closeCreate} title="新建 Agent">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            saveCreate();
          }}
          noValidate
          aria-label="新建 Agent"
          style={{ display: 'flex', flexDirection: 'column', gap: 16 }}
        >
          <AgentFormFields
            idPrefix="agent-new"
            value={createForm}
            errors={createErrors}
            toolOptions={AGENT_TOOL_OPTIONS}
            approvalOptions={AGENT_APPROVAL_OPTIONS}
            onChange={changeCreateForm}
            onBlurField={blurCreateField}
          />
          <div
            style={{
              display: 'flex',
              justifyContent: 'flex-end',
              gap: 8,
              borderTop: '1px solid var(--border-subtle)',
              paddingTop: 12,
            }}
          >
            <button type="button" className="btn btn-secondary" onClick={closeCreate}>
              取消
            </button>
            <button type="submit" className="btn btn-primary">
              保存
            </button>
          </div>
        </form>
      </Modal>
    </div>
  );
};
