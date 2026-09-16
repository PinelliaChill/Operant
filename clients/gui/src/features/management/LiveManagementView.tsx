import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  Archive,
  Check,
  Download,
  ExternalLink,
  FolderKanban,
  Link2,
  Loader2,
  PackageOpen,
  PanelLeftOpen,
  Pencil,
  Plus,
  Power,
  RefreshCw,
  Search,
  Settings2,
  Sparkles,
  Trash2,
  Unplug,
  X,
} from 'lucide-react';
import { Link, useOutletContext, useSearchParams } from 'react-router-dom';
import { EmptyState } from '../../components/EmptyState';
import { StatusBadge } from '../../components/StatusBadge';
import { useOperant } from '../../context/ClientContext';
import { currentBrowserOrigin } from '../../lib/liveBaseUrl';
import { B25Client } from '../../../../../sdk/typescript-client/b2_5.generated';
import {
  B23AdapterError,
  B23ManagementAdapter,
  type B23Action,
  type B23ManagedArtifact,
  type B23ManagedDataset,
  type B23ManagedInstallation,
  type B23ManagedMemory,
  type B23ManagedProject,
  type B23ManagedSkill,
  type B23ManagementCommand,
  type B23ManagementResult,
  type B23ManagementState,
  type B23UiError,
  blocksB23Management,
  normalizeB23Error,
} from '../../live/b23Adapter';
import type { RailOutletContext } from '../../app/RailLayout';
import { B25GovernancePanel } from './B25GovernancePanel';
import type { B25ClientLike, B2ModelClientLike } from './b25-state';

export type ManagementTab = 'projects' | 'knowledge' | 'plugins' | 'settings' | 'skills' | 'retention';

const TAB_LABELS: Record<ManagementTab, string> = {
  projects: '项目',
  knowledge: '知识',
  plugins: '插件',
  settings: '记忆设置',
  skills: '技能',
  retention: '保留与审计',
};

const TABS: ManagementTab[] = ['projects', 'knowledge', 'plugins', 'settings', 'skills', 'retention'];

type ManagedSkillView = B23ManagedSkill & {
  project_ids?: string[];
  trust_status?: string;
};

type ManagementStateView = B23ManagementState;

type ArtifactAuditFindingView = {
  finding_type: string;
  artifact_id: string | null;
  content_hash: string | null;
  expected_size_bytes: number | null;
  observed_size_bytes: number | null;
  finding_hash: string;
  repairable: boolean;
};

type ArtifactAuditReportView = {
  findings: ArtifactAuditFindingView[];
  scanned_database_references: number;
  scanned_blobs: number;
};

function isRecordValue(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function readRequiredAuditText(source: Record<string, unknown>, key: string): string | undefined {
  const value = source[key];
  return typeof value === 'string' && value.trim().length > 0 ? value : undefined;
}

function readNullableAuditText(source: Record<string, unknown>, key: string): string | null | undefined {
  if (!(key in source)) return undefined;
  const value = source[key];
  if (value === null) return null;
  return typeof value === 'string' && value.trim().length > 0 ? value : undefined;
}

function readNullableAuditNumber(source: Record<string, unknown>, key: string): number | null | undefined {
  if (!(key in source)) return undefined;
  const value = source[key];
  if (value === null) return null;
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : undefined;
}

function parseArtifactAuditExport(value: unknown): ArtifactAuditReportView | undefined {
  if (!isRecordValue(value)) return undefined;
  const report = value.artifact_audit;
  if (!isRecordValue(report)) return undefined;
  const references = report.scanned_database_references;
  const blobs = report.scanned_blobs;
  if (
    typeof references !== 'number' || !Number.isFinite(references) || references < 0
    || typeof blobs !== 'number' || !Number.isFinite(blobs) || blobs < 0
    || !Array.isArray(report.findings)
  ) return undefined;
  const findings: ArtifactAuditFindingView[] = [];
  for (const valueEntry of report.findings) {
    if (!isRecordValue(valueEntry)) return undefined;
    const findingType = readRequiredAuditText(valueEntry, 'finding_type');
    const findingHash = readRequiredAuditText(valueEntry, 'finding_hash');
    const artifactId = readNullableAuditText(valueEntry, 'artifact_id');
    const contentHash = readNullableAuditText(valueEntry, 'content_hash');
    const expectedSize = readNullableAuditNumber(valueEntry, 'expected_size_bytes');
    const observedSize = readNullableAuditNumber(valueEntry, 'observed_size_bytes');
    if (
      !findingType || !findingHash || artifactId === undefined || contentHash === undefined
      || expectedSize === undefined || observedSize === undefined
      || typeof valueEntry.repairable !== 'boolean'
    ) return undefined;
    findings.push({
      finding_type: findingType,
      artifact_id: artifactId,
      content_hash: contentHash,
      expected_size_bytes: expectedSize,
      observed_size_bytes: observedSize,
      finding_hash: findingHash,
      repairable: valueEntry.repairable,
    });
  }
  return {
    findings,
    scanned_database_references: references,
    scanned_blobs: blobs,
  };
}

function managementArtifacts(state: B23ManagementState): B23ManagedArtifact[] {
  const value = (state as ManagementStateView).artifacts;
  return Array.isArray(value) ? value : [];
}

function isManagementTab(value: string | null): value is ManagementTab {
  return value !== null && TABS.includes(value as ManagementTab);
}

function queryTab(value: string | null, fallback: ManagementTab): ManagementTab {
  if (value === 'memory') return 'knowledge';
  if (value === 'plugin' || value === 'extensions') return 'plugins';
  if (value === 'skill') return 'skills';
  return isManagementTab(value) ? value : fallback;
}

function valueText(value: unknown): string {
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2) ?? String(value);
  } catch {
    return String(value);
  }
}

function redactValue(value: unknown, key?: string): unknown {
  const numericBudget = key && /^(max_output_tokens|max_tokens|token_budget|recall_token_budget)$/.test(key) && typeof value === 'number';
  if (key && !numericBudget && /(secret|token|password|api[_-]?key|credential)/i.test(key)) return '[已隐藏]';
  if (Array.isArray(value)) return value.map((item) => redactValue(item));
  if (typeof value === 'object' && value !== null) {
    return Object.fromEntries(Object.entries(value).map(([entryKey, entryValue]) => [entryKey, redactValue(entryValue, entryKey)]));
  }
  return value;
}

function safeJson(value: unknown): string {
  return valueText(redactValue(value));
}

function statusLabel(value: string): string {
  const labels: Record<string, string> = {
    active: '已启用',
    enabled: '已启用',
    disabled: '已停用',
    installed: '已安装',
    closing: '关闭中',
    uninstalling: '卸载中',
    failed: '失败',
    restart_required: '需要重启',
    retained: '数据已保留',
    deleted: '已删除',
    published: '已发布',
    unpublished: '未发布',
    inactive: '已停用',
    revoked: '已撤销',
    pinned: '已固定',
    scheduled: '已安排清理',
    trashed: '已移入回收站',
    archived_artifact: '已归档',
    active_artifact: '可用',
    accepted: '已确认',
    blocked: '已阻止',
    partial: '部分完成',
    archived: '已归档',
    proposed: '待确认',
    pending: '待确认',
    conflict: '发生冲突',
    rejected: '已拒绝',
    confirmed: '已确认',
    deactivated: '已停用',
  };
  return labels[value] ?? value;
}

function statusKind(value: string): 'connected' | 'active' | 'paused' | 'denied' | 'pending' {
  if (['active', 'enabled', 'installed', 'confirmed', 'accepted', 'published', 'bound', 'valid'].includes(value)) return 'connected';
  if (['disabled', 'deactivated', 'archived', 'retained', 'deleted', 'inactive', 'revoked', 'uninstalled'].includes(value)) return 'paused';
  if (['failed', 'blocked', 'conflict', 'rejected', 'error'].includes(value)) return 'denied';
  return 'pending';
}

function absolutePath(value: string): boolean {
  return value.trim().startsWith('/');
}

function makeAdapterUnavailableError(): B23AdapterError {
  return new B23AdapterError({
    code: 'b2_3_client_unavailable',
    message: 'B2-3 生成 Client 尚未接入，Live 管理页面未提交任何命令。',
    retryable: false,
    recovery: 'none',
    outcomeUnknown: false,
  });
}

type ExecuteCommand = (
  command: B23ManagementCommand,
  label: string,
) => Promise<B23ManagementResult | undefined>;

interface ManagementPanelProps {
  state: B23ManagementState;
  execute: ExecuteCommand;
  busy: boolean;
  onTab: (tab: ManagementTab) => void;
  auditReport?: ArtifactAuditReportView;
  b25Client: B25ClientLike | null;
  b2Client: B2ModelClientLike | null;
  connectionStatus: string;
  refresh: () => Promise<void>;
}

const FormError: React.FC<{ message: string; id?: string }> = ({ message, id = 'b2-memory-form-error' }) => (
  <div id={id} className="b2-memory-warning live-alert live-alert-error" role="alert" tabIndex={-1}>
    <AlertTriangle size={15} aria-hidden="true" />
    <span>{message}</span>
  </div>
);

const ActionButton: React.FC<{
  label: string;
  icon?: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
  tone?: 'primary' | 'secondary' | 'ghost';
  type?: 'button' | 'submit';
}> = ({ label, icon, onClick, disabled, tone = 'secondary', type = 'button' }) => (
  <button type={type} className={`btn btn-${tone} btn-sm b2-memory-action`} onClick={onClick} disabled={disabled}>
    {icon}
    <span>{label}</span>
  </button>
);

const ProjectsPanel: React.FC<ManagementPanelProps> = ({ state, execute, busy, onTab }) => {
  const [name, setName] = useState('');
  const [workspacePath, setWorkspacePath] = useState('');
  const [formError, setFormError] = useState('');
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingName, setEditingName] = useState('');
  const [archiveId, setArchiveId] = useState<string | null>(null);
  const [detachId, setDetachId] = useState<string | null>(null);

  const create = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const trimmedName = name.trim();
    const trimmedPath = workspacePath.trim();
    if (!trimmedName) {
      setFormError('请填写项目名称。');
      return;
    }
    if (!absolutePath(trimmedPath)) {
      setFormError('workspace 路径必须是绝对路径，例如 /Users/you/workspace/project。');
      return;
    }
    setFormError('');
    const result = await execute({ action: 'project_create', name: trimmedName, workspace_path: trimmedPath }, '创建项目');
    if (result) {
      setName('');
      setWorkspacePath('');
    }
  };

  const rename = async (project: B23ManagedProject) => {
    const trimmedName = editingName.trim();
    if (!trimmedName) {
      setFormError('项目名称不能为空。');
      return;
    }
    setFormError('');
    const result = await execute({ action: 'project_update', project_id: project.project_id, name: trimmedName }, '重命名项目');
    if (result) {
      setEditingId(null);
      setEditingName('');
    }
  };

  const archive = async (project: B23ManagedProject) => {
    if (archiveId !== project.project_id) {
      setArchiveId(project.project_id);
      return;
    }
    setArchiveId(null);
    await execute({ action: 'project_archive', project_id: project.project_id, confirmed: true }, '归档项目');
  };

  const detach = async (project: B23ManagedProject) => {
    if (detachId !== project.project_id) {
      setDetachId(project.project_id);
      return;
    }
    setDetachId(null);
    await execute({ action: 'project_detach', project_id: project.project_id, confirmed: true }, '解除项目关联');
  };

  return (
    <div className="b2-memory" data-panel="projects">
      <section className="b2-memory-card">
        <div className="b2-memory-card-header">
          <div>
            <h2>新建项目</h2>
            <p>项目绑定一个绝对 workspace 目录，后续记忆与插件选择都使用这个项目范围。</p>
          </div>
          <FolderKanban size={18} aria-hidden="true" />
        </div>
        <form className="b2-memory-form" onSubmit={(event) => void create(event)} noValidate>
          {formError && <FormError message={formError} id="project-form-error" />}
          <label htmlFor="b2-project-name">项目名称</label>
          <input id="b2-project-name" className="input" value={name} onChange={(event) => setName(event.target.value)} aria-describedby={formError ? 'project-form-error' : undefined} required />
          <label htmlFor="b2-project-workspace">Workspace 绝对路径</label>
          <input id="b2-project-workspace" className="input" value={workspacePath} onChange={(event) => setWorkspacePath(event.target.value)} placeholder="/Users/you/workspace/project" aria-describedby={formError ? 'project-form-error' : undefined} required />
          <div className="b2-memory-actions">
            <ActionButton label="创建项目" tone="primary" icon={<Plus size={14} aria-hidden="true" />} onClick={() => undefined} disabled={busy || !name.trim() || !workspacePath.trim()} type="submit" />
          </div>
        </form>
      </section>

      <section aria-labelledby="b2-project-list-title">
        <div className="b2-memory-card-header">
          <div><h2 id="b2-project-list-title">项目列表</h2><p>{state.projects.length} 个项目，ID 和绑定状态来自服务端。</p></div>
          <StatusBadge status="active" label={`${state.projects.filter((project) => !project.archived).length} 个活跃`} size="sm" />
        </div>
        {state.projects.length === 0 ? (
          <EmptyState icon={FolderKanban} title="暂无项目" description="Live 模式不会用演示项目填充此页面。" />
        ) : (
          <div className="b2-memory-grid">
            {state.projects.map((project) => (
              <article key={project.project_id} className="b2-memory-card" data-state={project.archived ? 'archived' : 'active'}>
        <div className="b2-memory-card-header">
                  <div className="b2-memory-card-title">
                    <FolderKanban size={17} aria-hidden="true" />
                    {editingId === project.project_id ? (
                      <input className="input" value={editingName} onChange={(event) => setEditingName(event.target.value)} aria-label={`编辑项目名称 ${project.name}`} />
                    ) : <h3>{project.name}</h3>}
                  </div>
                  <StatusBadge status={statusKind(project.archived ? 'archived' : 'active')} label={statusLabel(project.archived ? 'archived' : 'active')} size="sm" />
                </div>
                <div className="b2-memory-meta">
                  <span>Project ID <code>{project.project_id}</code></span>
                  <span>Workspace <code>{project.workspace_id}</code></span>
                  <span>记忆：{project.memory_enabled ? '已开启' : '已关闭'}</span>
                  {project.installation_id && <span>插件安装：<code>{project.installation_id}</code></span>}
                </div>
                <div className="b2-memory-actions">
                  {!project.archived && (editingId === project.project_id ? (
                    <>
                      <ActionButton label="保存名称" tone="primary" onClick={() => void rename(project)} disabled={busy} icon={<Check size={13} aria-hidden="true" />} />
                      <ActionButton label="取消" tone="ghost" onClick={() => { setEditingId(null); setEditingName(''); }} disabled={busy} icon={<X size={13} aria-hidden="true" />} />
                    </>
                  ) : <ActionButton label="重命名" onClick={() => { setEditingId(project.project_id); setEditingName(project.name); }} disabled={busy} icon={<Pencil size={13} aria-hidden="true" />} />)}
                  <ActionButton label="管理知识" onClick={() => { onTab('knowledge'); }} disabled={busy} icon={<Link2 size={13} aria-hidden="true" />} />
                  {!project.archived && <ActionButton label={detachId === project.project_id ? '再次点击确认解除关联' : '解除关联'} tone={detachId === project.project_id ? 'primary' : 'ghost'} onClick={() => void detach(project)} disabled={busy} icon={<Unplug size={13} aria-hidden="true" />} />}
                  {!project.archived && <ActionButton label={archiveId === project.project_id ? '再次点击确认归档' : '归档'} tone={archiveId === project.project_id ? 'primary' : 'ghost'} onClick={() => void archive(project)} disabled={busy} icon={<Archive size={13} aria-hidden="true" />} />}
                </div>
                {archiveId === project.project_id && <p className="b2-memory-warning" role="status">归档会改变项目生命周期状态。再次点击“再次点击确认归档”后才提交，未自动重放。</p>}
                {detachId === project.project_id && <p className="b2-memory-warning" role="status">解除关联只移除项目绑定，源码与历史仍保留；再次点击按钮才提交。</p>}
              </article>
            ))}
          </div>
        )}
      </section>
    </div>
  );
};

const ProjectSelect: React.FC<{
  projects: B23ManagedProject[];
  value: string;
  onChange: (value: string) => void;
  id: string;
  includeArchived?: boolean;
}> = ({ projects, value, onChange, id, includeArchived = false }) => (
  <select id={id} className="select" value={value} onChange={(event) => onChange(event.target.value)}>
    <option value="">{projects.length ? '请选择项目' : '服务端尚未返回项目'}</option>
    {projects.filter((project) => includeArchived || !project.archived).map((project) => (
      <option key={project.project_id} value={project.project_id} disabled={project.archived}>
        {project.name}{project.archived ? '（已归档）' : ''}
      </option>
    ))}
  </select>
);

const KnowledgeRecord: React.FC<{
  record: B23ManagedMemory;
  execute: ExecuteCommand;
  busy: boolean;
}> = ({ record, execute, busy }) => {
  const [proposalContent, setProposalContent] = useState('');
  const [proposalOpen, setProposalOpen] = useState(false);
  const [deactivateOpen, setDeactivateOpen] = useState(false);

  const propose = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!proposalContent.trim()) return;
    const result = await execute({
      action: 'memory_propose',
      project_id: record.project_id,
      record_id: record.record_id,
      content: proposalContent.trim(),
      expected_revision: record.revision,
    }, '提交记忆修改提议');
    if (result) {
      setProposalContent('');
      setProposalOpen(false);
    }
  };

  return (
    <article className="b2-memory-record" data-state={record.state}>
      <div className="b2-memory-record-header">
        <div><strong>记忆记录</strong> <code>{record.record_id}</code></div>
        <StatusBadge status={statusKind(record.state)} label={statusLabel(record.state)} size="sm" />
      </div>
      <p className="b2-memory-record-content">{record.content}</p>
      <div className="b2-memory-meta">
        <span>revision {record.revision}</span>
        <span>version {record.version}</span>
        <span>证据：{record.evidence}</span>
        <span>dataset <code>{record.dataset_id}</code></span>
      </div>
      <details className="b2-memory-sources">
        <summary>来源（{record.sources.length}）</summary>
        {record.sources.length === 0 ? <p>没有可显示的来源。</p> : <ul>{record.sources.map((source, index) => <li key={`${record.record_id}-source-${index}`}><code>{safeJson(source)}</code></li>)}</ul>}
      </details>
      {record.proposals.length > 0 && (
        <div className="b2-memory-proposals" aria-label={`${record.record_id} 的修改提议`}>
          <strong>修改提议</strong>
          {record.proposals.map((proposal) => (
              <div key={proposal.proposal_id} className="b2-memory-card b2-memory-proposal">
              <div className="b2-memory-meta"><code>{proposal.proposal_id}</code><StatusBadge status={statusKind(proposal.state)} label={statusLabel(proposal.state)} size="sm" /><span>期望 revision {proposal.expected_revision}</span></div>
              <p>{proposal.content}</p>
              {/* Legacy memory_confirm is intentionally not rendered; B2-5 review carries expiry/CAS guards. */}
              {proposal.state === 'pending' && <p className="b2-memory-warning" role="status">待处理候选请在下方“高级知识治理”中核对精确 Proposal、版本和 CAS 后确认。</p>}
            </div>
          ))}
        </div>
      )}
      <div className="b2-memory-actions">
        <ActionButton label={proposalOpen ? '取消提议' : '提出修改'} onClick={() => setProposalOpen((current) => !current)} disabled={busy} icon={proposalOpen ? <X size={13} aria-hidden="true" /> : <Pencil size={13} aria-hidden="true" />} />
        {!['inactive', 'revoked', 'deleted'].includes(record.state) && <ActionButton label={deactivateOpen ? '再次点击确认停用' : '停用记录'} tone={deactivateOpen ? 'primary' : 'ghost'} onClick={() => { if (!deactivateOpen) setDeactivateOpen(true); else { setDeactivateOpen(false); void execute({ action: 'memory_deactivate', project_id: record.project_id, record_id: record.record_id, expected_revision: record.revision, confirmed: true }, '停用记忆'); } }} disabled={busy} icon={<Power size={13} aria-hidden="true" />} />}
      </div>
      {proposalOpen && <form className="b2-memory-form" onSubmit={(event) => void propose(event)}><label htmlFor={`proposal-${record.record_id}`}>新的记忆内容</label><textarea id={`proposal-${record.record_id}`} className="input" rows={4} value={proposalContent} onChange={(event) => setProposalContent(event.target.value)} placeholder="写入修改提议；服务端会按 revision 检查是否过期。" required /><div className="b2-memory-actions"><ActionButton label="提交提议" tone="primary" type="submit" onClick={() => undefined} disabled={busy || !proposalContent.trim()} /></div></form>}
      {deactivateOpen && <p className="b2-memory-warning" role="status">停用会提交当前 revision（{record.revision}），结果以服务端返回为准。</p>}
    </article>
  );
};

const KnowledgePanel: React.FC<ManagementPanelProps> = ({ state, execute, busy, b25Client, b2Client, connectionStatus, refresh }) => {
  const [projectId, setProjectId] = useState('');
  const [query, setQuery] = useState('');
  const [content, setContent] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [formError, setFormError] = useState('');

  useEffect(() => {
    if (!projectId && state.projects.find((project) => !project.archived)) setProjectId(state.projects.find((project) => !project.archived)?.project_id ?? '');
  }, [projectId, state.projects]);

  const search = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!projectId) {
      setFormError('请先选择项目。');
      return;
    }
    if (!query.trim()) {
      setFormError('请输入查询内容。');
      return;
    }
    setFormError('');
    await execute({ action: 'memory_search', project_id: projectId, query: query.trim() }, '查询项目知识');
  };

  const save = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!projectId) {
      setFormError('请先选择项目。');
      return;
    }
    if (!content.trim()) {
      setFormError('记忆内容不能为空。');
      return;
    }
    if (!confirmed) {
      setFormError('请勾选“确认保存”，再提交记忆。');
      return;
    }
    setFormError('');
    const result = await execute({ action: 'memory_save', project_id: projectId, content: content.trim(), confirmed: true }, '保存记忆');
    if (result) {
      setContent('');
      setConfirmed(false);
    }
  };

  const records = state.records.filter((record) => !projectId || record.project_id === projectId);

  return (
    <div className="b2-memory" data-panel="knowledge">
      <section className="b2-memory-card">
        <div className="b2-memory-card-header"><div><h2>项目知识</h2><p>保存、搜索和审阅正式记录与修改提议。</p></div><Sparkles size={18} aria-hidden="true" /></div>
        <div className="b2-memory-form">
          <label htmlFor="knowledge-project">项目范围</label>
          <ProjectSelect projects={state.projects} value={projectId} onChange={setProjectId} id="knowledge-project" />
        </div>
        <form className="b2-memory-form" onSubmit={(event) => void search(event)}>
          {formError && <FormError message={formError} id="knowledge-form-error" />}
          <label htmlFor="knowledge-search">查询内容</label>
          <div className="b2-memory-inline-form"><input id="knowledge-search" className="input" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="例如：编码约定、项目结构" aria-describedby={formError ? 'knowledge-form-error' : undefined} /><ActionButton label="查询" tone="secondary" type="submit" onClick={() => undefined} disabled={busy || !projectId || !query.trim()} icon={<Search size={13} aria-hidden="true" />} /></div>
        </form>
        <form className="b2-memory-form" onSubmit={(event) => void save(event)}>
          <label htmlFor="knowledge-content">保存一条记忆</label>
          <textarea id="knowledge-content" className="input" rows={4} value={content} onChange={(event) => setContent(event.target.value)} placeholder="只提交你确认过的项目知识。" aria-describedby={formError ? 'knowledge-form-error' : undefined} required />
          <label className="b2-memory-check"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} /> <span>我确认保存这条项目记忆</span></label>
          <div className="b2-memory-actions"><ActionButton label="保存记忆" tone="primary" type="submit" onClick={() => undefined} disabled={busy || !projectId || !content.trim() || !confirmed} icon={<Check size={13} aria-hidden="true" />} /></div>
        </form>
      </section>
      <B25GovernancePanel
        client={b25Client}
        modelClient={b2Client}
        projects={state.projects}
        connectionStatus={connectionStatus}
        initialProjectId={projectId || undefined}
        onMutation={refresh}
      />
      <section aria-labelledby="knowledge-records-title">
        <div className="b2-memory-card-header"><div><h2 id="knowledge-records-title">正式记录与候选提议</h2><p>{records.length} 条记录；展示来源、版本与 revision，客户端不覆盖旧记录。</p></div><span className="b2-memory-status">{state.global_enabled ? '全局记忆已开启' : '全局记忆已关闭'}</span></div>
        {records.length === 0 ? <div className="b2-memory-card"><EmptyState icon={Sparkles} title="暂无项目知识" description="查询没有返回记录，或当前项目尚未保存知识。" /></div> : <div className="b2-memory-grid">{records.map((record) => <KnowledgeRecord key={record.record_id} record={record} execute={execute} busy={busy} />)}</div>}
      </section>
    </div>
  );
};

const DatasetCard: React.FC<{
  dataset: B23ManagedDataset;
  execute: ExecuteCommand;
  busy: boolean;
  catalog: B23ManagementState['catalog'];
}> = ({ dataset, execute, busy, catalog }) => {
  const [deleteOpen, setDeleteOpen] = useState(false);
  const plugin = catalog.find((entry) => entry.plugin_id === dataset.plugin_id);
  const cleanupPending = dataset.state === 'deleting';
  const cleanupBlocked = dataset.state === 'blocked';
  const canDelete = dataset.state === 'retained';
  const canExport = dataset.state !== 'deleted';
  const exportDataset = async () => {
    const result = await execute({ action: 'dataset_export', dataset_id: dataset.dataset_id, confirmed: true }, '导出保留数据集');
    if (!result?.export_data) return;
    const blob = new Blob([JSON.stringify(result.export_data, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `${dataset.dataset_id}-export.json`;
    anchor.click();
    URL.revokeObjectURL(url);
  };
  const deleteDataset = async () => {
    if (!deleteOpen) {
      setDeleteOpen(true);
      return;
    }
    setDeleteOpen(false);
    await execute({ action: 'dataset_delete', dataset_id: dataset.dataset_id, confirmed: true }, '删除数据集');
  };
  return (
      <article className="b2-memory-card" data-state={dataset.state} data-retained="true">
      <div className="b2-memory-card-header"><div><h3>{plugin?.name ?? dataset.plugin_id}</h3><p>保留数据集 <code>{dataset.dataset_id}</code></p></div><StatusBadge status={statusKind(dataset.state)} label={statusLabel(dataset.state)} size="sm" /></div>
      <div className="b2-memory-meta"><span>{dataset.record_count} 条记录</span><span>{dataset.installation_id ? `安装 ${dataset.installation_id}` : '当前没有安装'}</span></div>
      {cleanupPending && <div className="b2-memory-status" data-status="pending" role="status">清理计划仍在执行，完成后可再次查询状态。</div>}
      {cleanupBlocked && <div className="b2-memory-warning" data-severity="error" role="alert"><strong>清理被阻断：</strong>{dataset.exceptions.length > 0 ? dataset.exceptions.join('；') : '服务端需要人工处理后才能继续。'}</div>}
      <div className="b2-memory-actions"><ActionButton label="导出" onClick={() => void exportDataset()} disabled={busy || !canExport} icon={<Download size={13} aria-hidden="true" />} />{(cleanupPending || cleanupBlocked) && <ActionButton label="继续清理" onClick={() => void execute({ action: 'cleanup_resume', dataset_id: dataset.dataset_id, confirmed: true }, '继续清理数据集')} disabled={busy} icon={<RefreshCw size={13} aria-hidden="true" />} />}{canDelete && <ActionButton label={deleteOpen ? '再次点击确认删除' : '删除数据集'} tone={deleteOpen ? 'primary' : 'ghost'} onClick={() => void deleteDataset()} disabled={busy} icon={<Trash2 size={13} aria-hidden="true" />} />}</div>
      {deleteOpen && <p className="b2-memory-warning" role="status">删除会移除数据集记录；再次点击按钮才提交，完成状态以服务端返回为准。</p>}
    </article>
  );
};

const PluginPanel: React.FC<ManagementPanelProps> = ({ state, execute, busy }) => {
  const [mode, setMode] = useState<'isolated' | 'trusted_in_process'>('isolated');
  const [retainedDataset, setRetainedDataset] = useState('');
  const [selectedProject, setSelectedProject] = useState('');
  const [configInstallation, setConfigInstallation] = useState<string | null>(null);
  const [configText, setConfigText] = useState('{}');
  const [configError, setConfigError] = useState('');
  const [uninstallTarget, setUninstallTarget] = useState<string | null>(null);
  const [dataPolicy, setDataPolicy] = useState<'keep' | 'delete'>('keep');
  const [uninstallConfirmed, setUninstallConfirmed] = useState(false);

  const install = async (pluginId: string) => {
    const result = await execute({ action: 'plugin_install', plugin_id: pluginId, mode, dataset_id: retainedDataset.trim() || undefined, confirmed: true }, retainedDataset.trim() ? '重装插件并接回数据' : '安装插件');
    if (result) setRetainedDataset('');
  };

  const configure = async (installation: B23ManagedInstallation) => {
    try {
      const parsed = JSON.parse(configText) as unknown;
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('配置必须是 JSON 对象。');
      setConfigError('');
      const result = await execute({ action: 'plugin_configure', installation_id: installation.installation_id, config: parsed as Record<string, unknown>, confirmed: true }, '保存插件配置');
      if (result) setConfigInstallation(null);
    } catch (error: unknown) {
      setConfigError(error instanceof Error ? error.message : '配置 JSON 无效。');
    }
  };

  const selectBinding = async (installation: B23ManagedInstallation) => {
    if (!selectedProject) return;
    await execute({ action: 'binding_select', project_id: selectedProject, installation_id: installation.installation_id, confirmed: true }, '选择项目插件');
  };

  const uninstall = async (installation: B23ManagedInstallation) => {
    if (uninstallTarget !== installation.installation_id) {
      setUninstallTarget(installation.installation_id);
      setDataPolicy('keep');
      setUninstallConfirmed(false);
      return;
    }
    if (!uninstallConfirmed) return;
    setUninstallTarget(null);
    setUninstallConfirmed(false);
    await execute({ action: 'plugin_uninstall', installation_id: installation.installation_id, data_policy: dataPolicy, confirmed: true }, dataPolicy === 'keep' ? '卸载插件并保留数据' : '卸载插件并删除数据');
  };

  return (
    <div className="b2-memory" data-panel="plugins">
      <section className="b2-memory-card">
        <div className="b2-memory-card-header"><div><h2>插件目录</h2><p>安装目录中的正式插件包，运行模式和卸载数据策略由用户明确选择。</p></div><PackageOpen size={18} aria-hidden="true" /></div>
        <div className="b2-memory-form">
          <label htmlFor="plugin-mode">运行模式</label>
          <select id="plugin-mode" className="select" value={mode} onChange={(event) => setMode(event.target.value as typeof mode)}><option value="isolated">isolated（隔离）</option><option value="trusted_in_process">trusted_in_process（受信进程内）</option></select>
          <label htmlFor="plugin-retained-dataset">接回保留数据集（可选）</label>
          <select id="plugin-retained-dataset" className="select" value={retainedDataset} onChange={(event) => setRetainedDataset(event.target.value)}><option value="">不接回</option>{state.datasets.filter((dataset) => !dataset.installation_id).map((dataset) => <option key={dataset.dataset_id} value={dataset.dataset_id}>{dataset.plugin_id} · {dataset.dataset_id}</option>)}</select>
          <small>安装/重装按钮提交后，页面只显示服务端返回的生命周期状态。</small>
        </div>
        {state.catalog.length === 0 ? <EmptyState icon={PackageOpen} title="插件目录为空" description="Live 模式不会显示演示插件。" /> : <div className="b2-memory-grid">{state.catalog.map((plugin) => <article key={plugin.plugin_id} className="b2-memory-card"><div className="b2-memory-card-header"><div><h3>{plugin.name}</h3><p><code>{plugin.plugin_id}</code></p></div><ActionButton label="安装" tone="primary" onClick={() => void install(plugin.plugin_id)} disabled={busy} icon={<Plus size={13} aria-hidden="true" />} /></div><p>{plugin.description}</p><details><summary>配置 Schema</summary><pre className="b2-memory-record-content">{safeJson(plugin.config_schema)}</pre></details></article>)}</div>}
      </section>

      <section aria-labelledby="plugin-installations-title">
        <div className="b2-memory-card-header"><div><h2 id="plugin-installations-title">已安装插件</h2><p>{state.installations.length} 个安装实例；关闭、配置、项目选择和卸载都通过正式命令。</p></div><StatusBadge status="active" label={`${state.installations.length} 个`} size="sm" /></div>
        {state.installations.length === 0 ? <div className="b2-memory-card"><EmptyState icon={Unplug} title="暂无安装实例" description="从上方插件目录选择安装。" /></div> : <div className="b2-memory-grid">{state.installations.map((installation) => {
          const plugin = state.catalog.find((entry) => entry.plugin_id === installation.plugin_id);
          const relatedDataset = state.datasets.find((dataset) => dataset.dataset_id === installation.dataset_id);
          const lifecycleClosed = installation.state === 'uninstalled' || installation.state === 'uninstalling';
          const canConfigure = installation.state === 'disabled';
          const canToggle = !lifecycleClosed;
          const lifecycleMessage = relatedDataset?.state === 'deleted'
            ? '安装副本已卸载；对应数据集已删除，不能重新接回。'
            : relatedDataset?.state === 'retained'
              ? '安装副本已卸载；保留数据集仍可从下方重装。'
              : '安装副本已卸载；数据集状态以服务端返回为准。';
          return <article key={installation.installation_id} className="b2-memory-card" data-state={installation.state}><div className="b2-memory-card-header"><div><h3>{plugin?.name ?? installation.plugin_id}</h3><p><code>{installation.installation_id}</code></p></div><StatusBadge status={statusKind(installation.state)} label={statusLabel(installation.state)} size="sm" /></div><div className="b2-memory-meta"><span>模式：{installation.mode}</span><span>认证：{installation.certification_status}</span><span>dataset <code>{installation.dataset_id}</code></span><span>{state.projects.filter((project) => project.installation_id === installation.installation_id).map((project) => project.name).join('、') || '尚未选择项目'}</span></div>{lifecycleClosed && <p className="b2-memory-warning" role="status">{lifecycleMessage}</p>}{canToggle && <div className="b2-memory-actions"><ActionButton label={installation.state === 'enabled' || installation.state === 'active' ? '关闭插件' : '启用插件'} onClick={() => void execute({ action: installation.state === 'enabled' || installation.state === 'active' ? 'plugin_disable' : 'plugin_enable', installation_id: installation.installation_id, confirmed: true }, installation.state === 'enabled' || installation.state === 'active' ? '关闭插件' : '启用插件')} disabled={busy} icon={<Power size={13} aria-hidden="true" />} /><ActionButton label="配置" onClick={() => { setConfigInstallation(installation.installation_id); setConfigText(safeJson(installation.config)); setConfigError(''); }} disabled={busy || !canConfigure} icon={<Settings2 size={13} aria-hidden="true" />} /><ActionButton label="选择项目" onClick={() => setSelectedProject(selectedProject || state.projects.find((project) => !project.archived)?.project_id || '')} disabled={busy || state.projects.length === 0} icon={<Link2 size={13} aria-hidden="true" />} /><ActionButton label={uninstallTarget === installation.installation_id ? '收起卸载选项' : '卸载'} tone="ghost" onClick={() => { if (uninstallTarget === installation.installation_id) setUninstallTarget(null); else void uninstall(installation); }} disabled={busy} icon={<Trash2 size={13} aria-hidden="true" />} /></div>}
            {!lifecycleClosed && selectedProject && <div className="b2-memory-inline-form"><label htmlFor={`binding-project-${installation.installation_id}`}>选择项目</label><ProjectSelect projects={state.projects} value={selectedProject} onChange={setSelectedProject} id={`binding-project-${installation.installation_id}`} /><ActionButton label="确认选择" tone="primary" onClick={() => void selectBinding(installation)} disabled={busy || !selectedProject} icon={<Check size={13} aria-hidden="true" />} /></div>}
            {!lifecycleClosed && configInstallation === installation.installation_id && <div className="b2-memory-form">{configError && <FormError message={configError} id={`config-error-${installation.installation_id}`} />}<label htmlFor={`plugin-config-${installation.installation_id}`}>插件配置 JSON</label><textarea id={`plugin-config-${installation.installation_id}`} className="input" rows={5} value={configText} onChange={(event) => setConfigText(event.target.value)} aria-describedby={configError ? `config-error-${installation.installation_id}` : undefined} /><div className="b2-memory-actions"><ActionButton label="保存配置" tone="primary" onClick={() => void configure(installation)} disabled={busy} icon={<Check size={13} aria-hidden="true" />} /><ActionButton label="取消" tone="ghost" onClick={() => setConfigInstallation(null)} disabled={busy} icon={<X size={13} aria-hidden="true" />} /></div></div>}
            {!lifecycleClosed && uninstallTarget === installation.installation_id && <div className="b2-memory-form b2-memory-warning"><fieldset><legend>卸载数据策略</legend><label className="b2-memory-check b2-memory-policy-btn--keep" data-policy="keep"><input type="radio" name={`data-policy-${installation.installation_id}`} value="keep" checked={dataPolicy === 'keep'} onChange={() => setDataPolicy('keep')} />保留数据集，卸载后继续管理/导出</label><label className="b2-memory-check b2-memory-policy-btn--delete" data-policy="delete"><input type="radio" name={`data-policy-${installation.installation_id}`} value="delete" checked={dataPolicy === 'delete'} onChange={() => setDataPolicy('delete')} />删除数据集及其中记录</label></fieldset><label className="b2-memory-check"><input type="checkbox" checked={uninstallConfirmed} onChange={(event) => setUninstallConfirmed(event.target.checked)} />我确认执行“{dataPolicy === 'keep' ? '保留' : '删除'}”策略</label><ActionButton label="提交卸载" tone="primary" onClick={() => void uninstall(installation)} disabled={busy || !uninstallConfirmed} icon={<Trash2 size={13} aria-hidden="true" />} /></div>}
          </article>;
        })}</div>}
      </section>

      <section aria-labelledby="dataset-title"><div className="b2-memory-card-header"><div><h2 id="dataset-title">保留数据集</h2><p>插件卸载后仍可导出、删除或重新接回；实际状态以服务端返回为准。</p></div></div>{state.datasets.length === 0 ? <div className="b2-memory-card"><EmptyState icon={Download} title="暂无数据集" description="保留数据集会在卸载后继续出现在这里。" /></div> : <div className="b2-memory-grid">{state.datasets.map((dataset) => <DatasetCard key={dataset.dataset_id} dataset={dataset} execute={execute} busy={busy} catalog={state.catalog} />)}</div>}</section>
    </div>
  );
};

const SettingsPanel: React.FC<ManagementPanelProps> = ({ state, execute, busy, onTab }) => {
  const [projectId, setProjectId] = useState('');
  const activeProjects = state.projects.filter((project) => !project.archived);
  useEffect(() => { if (!projectId) setProjectId(activeProjects[0]?.project_id ?? ''); }, [activeProjects, projectId]);
  const switchMemory = async (next: boolean, selectedProject?: string) => {
    await execute({ action: 'memory_switch', project_id: selectedProject, enabled: next, confirmed: true }, selectedProject ? '切换项目记忆' : '切换全局记忆');
  };
  return (
    <div className="b2-memory" data-panel="settings">
      <section className="b2-memory-card"><div className="b2-memory-card-header"><div><h2>记忆开关</h2><p>全局开关优先于项目开关；实际生效范围、来源与时间由服务端返回。</p></div><Power size={18} aria-hidden="true" /></div><div className="b2-memory-actions"><span className="b2-memory-status" data-status={state.global_enabled ? 'enabled' : 'disabled'}>全局：{state.global_enabled ? '已开启' : '已关闭'}</span><ActionButton label={state.global_enabled ? '关闭全局记忆' : '开启全局记忆'} tone={state.global_enabled ? 'ghost' : 'primary'} onClick={() => void switchMemory(!state.global_enabled)} disabled={busy} icon={<Power size={13} aria-hidden="true" />} /></div><div className="b2-memory-form"><label htmlFor="settings-project">项目范围</label><ProjectSelect projects={state.projects} value={projectId} onChange={setProjectId} id="settings-project" /><div className="b2-memory-actions">{projectId && <><span className="b2-memory-status" data-status={state.projects.find((project) => project.project_id === projectId)?.memory_enabled ? 'enabled' : 'disabled'}>项目：{state.projects.find((project) => project.project_id === projectId)?.memory_enabled ? '已开启' : '已关闭'}</span><ActionButton label="切换项目记忆" onClick={() => { const project = state.projects.find((entry) => entry.project_id === projectId); if (project) void switchMemory(!project.memory_enabled, project.project_id); }} disabled={busy} icon={<Power size={13} aria-hidden="true" />} /><ActionButton label="迁移项目记忆" onClick={() => void execute({ action: 'memory_migrate', project_id: projectId, confirmed: true }, '迁移项目记忆')} disabled={busy} icon={<RefreshCw size={13} aria-hidden="true" />} /></>}</div><div className="b2-memory-actions"><ActionButton label="打开高级知识治理" onClick={() => onTab('knowledge')} disabled={busy} icon={<ExternalLink size={13} aria-hidden="true" />} /></div></div></section>
      <section aria-labelledby="settings-source-title"><div className="b2-memory-card-header"><div><h2 id="settings-source-title">配置来源与作用域</h2><p>用于核对 global/project 生效来源，客户端不自行推断覆盖关系。</p></div><ActionButton label="查看项目知识" onClick={() => onTab('knowledge')} icon={<ExternalLink size={13} aria-hidden="true" />} disabled={busy} /></div>{state.settings.length === 0 ? <div className="b2-memory-card"><EmptyState icon={Settings2} title="暂无配置" description="服务端尚未返回设置来源。" /></div> : <div className="b2-memory-grid">{state.settings.map((setting) => <article key={`${setting.scope}-${setting.key}`} className="b2-memory-card"><div className="b2-memory-card-header"><h3>{setting.key}</h3><span className="b2-memory-status">{setting.scope}</span></div><div className="b2-memory-meta"><span>来源：{setting.source}</span><span>生效：{setting.effective_at}</span></div><pre className="b2-memory-record-content">{safeJson(setting.value)}</pre></article>)}</div>}</section>
    </div>
  );
};

const RetentionPanel: React.FC<ManagementPanelProps> = ({ state, execute, busy, auditReport }) => {
  const artifacts = managementArtifacts(state);
  const runArtifactAction = (artifact: B23ManagedArtifact, actionName: B23Action, label: string) => {
    void execute({ action: actionName, artifact_id: artifact.artifact_id, enabled: actionName === 'artifact_pin' ? !artifact.pinned : undefined, confirmed: true }, label);
  };

  return (
    <div className="b2-memory" data-panel="retention">
      <section className="b2-memory-card">
        <div className="b2-memory-card-header"><div><h2>保留与审计</h2><p>查看 Artifact 的保留状态、内容哈希和审计入口；物理清理仍由服务端保留锁与宽限期管理。</p></div><Archive size={18} aria-hidden="true" /></div>
        {artifacts.length === 0 ? <EmptyState icon={Archive} title="暂无 Artifact" description="服务端尚未返回可管理的 Artifact 条目。" /> : <div className="b2-memory-grid">{artifacts.map((artifact) => <article key={artifact.artifact_id} className="b2-memory-card" data-state={artifact.lifecycle} data-blocked={artifact.blocked ? 'true' : 'false'}>
          <div className="b2-memory-card-header"><div><h3>Artifact</h3><p><code>{artifact.artifact_id}</code></p></div><span className="b2-memory-status" data-status={artifact.blocked ? 'blocked' : artifact.lifecycle}>{artifact.blocked ? '已阻断' : statusLabel(artifact.lifecycle)}</span></div>
          <div className="b2-memory-meta"><span>大小：{artifact.size_bytes} B</span><span>生命周期：{artifact.lifecycle}</span><span>哈希：<code>{artifact.content_hash}</code></span><span>固定：{artifact.pinned ? '是' : '否'}</span></div>
          {artifact.blocked && <div className="b2-memory-warning" data-severity="error" role="alert">该 Artifact 当前被阻断，服务端不会执行未获允许的生命周期操作；可先打开审计。</div>}
          <div className="b2-memory-actions">
            <ActionButton label={artifact.pinned ? '取消固定' : '固定保留'} onClick={() => runArtifactAction(artifact, 'artifact_pin', artifact.pinned ? '取消固定 Artifact' : '固定 Artifact')} disabled={busy || artifact.blocked} icon={<Check size={13} aria-hidden="true" />} />
            <ActionButton label="审计" onClick={() => runArtifactAction(artifact, 'artifact_audit', '审计 Artifact')} disabled={busy} icon={<Search size={13} aria-hidden="true" />} />
            <ActionButton label="归档" onClick={() => runArtifactAction(artifact, 'artifact_archive', '归档 Artifact')} disabled={busy || artifact.blocked} icon={<Archive size={13} aria-hidden="true" />} />
            <ActionButton label="安排清理" onClick={() => runArtifactAction(artifact, 'artifact_schedule', '安排 Artifact 清理')} disabled={busy || artifact.blocked} icon={<RefreshCw size={13} aria-hidden="true" />} />
            <ActionButton label="移入回收站" tone="ghost" onClick={() => runArtifactAction(artifact, 'artifact_trash', '移入 Artifact 回收站')} disabled={busy || artifact.blocked} icon={<Trash2 size={13} aria-hidden="true" />} />
            <ActionButton label="恢复" onClick={() => runArtifactAction(artifact, 'artifact_restore', '恢复 Artifact')} disabled={busy || artifact.blocked} icon={<RefreshCw size={13} aria-hidden="true" />} />
          </div>
        </article>)}</div>}
      </section>
      {auditReport && <section className="b2-memory-card" data-audit-result>
        <div className="b2-memory-card-header"><div><h2>最近一次审计结果</h2><p>结果来自服务端 Artifact 审计；没有审计返回时不显示此区域。</p></div><Search size={18} aria-hidden="true" /></div>
        <div className="b2-memory-meta" data-audit-summary>
          <span>扫描引用：{auditReport.scanned_database_references}</span>
          <span>扫描内容：{auditReport.scanned_blobs}</span>
          <span>发现：{auditReport.findings.length}</span>
        </div>
        {auditReport.findings.length === 0 ? <p className="b2-memory-status" role="status">未发现异常。</p> : <ul className="b2-memory-record-content" data-audit-findings>
          {auditReport.findings.map((finding, index) => <li key={`${finding.finding_hash}-${index}`}>
            <strong>{finding.finding_type}</strong>
            {finding.artifact_id && <code>Artifact {finding.artifact_id}</code>}
            {finding.content_hash && <code>内容 {finding.content_hash}</code>}
            {(finding.expected_size_bytes !== null || finding.observed_size_bytes !== null) && <span>大小：期望 {finding.expected_size_bytes ?? '未知'} B，实际 {finding.observed_size_bytes ?? '未知'} B</span>}
            {finding.repairable && <span>可修复</span>}
          </li>)}
        </ul>}
      </section>}
      <p className="section-footnote">审计请求和状态变更均由服务端返回结果；未知写结果需要刷新并人工核对。</p>
    </div>
  );
};

const SkillsPanel: React.FC<ManagementPanelProps> = ({ state, execute, busy }) => {
  const [projectId, setProjectId] = useState('');
  const [uninstallId, setUninstallId] = useState<string | null>(null);
  const activeProjects = state.projects.filter((project) => !project.archived);
  const skills = state.skills as ManagedSkillView[];

  useEffect(() => {
    if (!projectId) setProjectId(activeProjects[0]?.project_id ?? '');
  }, [activeProjects, projectId]);

  const toggleProjectSkill = async (skill: ManagedSkillView) => {
    if (!projectId) return;
    const projectIds = skill.project_ids ?? [];
    const enabled = projectIds.includes(projectId);
    const action: B23Action = enabled ? 'skill_disable' : 'skill_enable';
    await execute({ action, skill_id: skill.skill_id, project_id: projectId, confirmed: true }, enabled ? '停用项目 Skill' : '启用项目 Skill');
  };

  const uninstall = async (skill: ManagedSkillView) => {
    if (uninstallId !== skill.skill_id) {
      setUninstallId(skill.skill_id);
      return;
    }
    setUninstallId(null);
    await execute({ action: 'skill_uninstall', skill_id: skill.skill_id, confirmed: true }, '卸载 Skill');
  };

  return (
    <div className="b2-memory" data-panel="skills">
      <section className="b2-memory-card">
        <div className="b2-memory-card-header"><div><h2>Skill 基础管理</h2><p>发现服务端受信目录中的已有 Skill，安装、项目启停和卸载都由用户明确操作；执行权限仍由 Action Gateway 管理。</p></div><Sparkles size={18} aria-hidden="true" /><ActionButton label="发现已有技能" onClick={() => void execute({ action: 'skill_discover' }, '发现已有技能')} disabled={busy} icon={<Search size={13} aria-hidden="true" />} /></div>
        {state.skill_catalog.length === 0 ? <EmptyState icon={Sparkles} title="没有可安装 Skill" description="Live 模式不会显示演示 Skill；请先发现服务端已配置的受信目录。" /> : <div className="b2-memory-grid">{state.skill_catalog.map((entry) => <article className="b2-memory-card" key={entry.package_ref}><div className="b2-memory-card-header"><div><h3>{entry.name}</h3><p><code>{entry.package_ref}</code></p></div><ActionButton label="安装" tone="primary" onClick={() => void execute({ action: 'skill_install', package_ref: entry.package_ref, confirmed: true }, '安装 Skill')} disabled={busy} icon={<Plus size={13} aria-hidden="true" />} /></div></article>)}</div>}
      </section>
      <section aria-labelledby="skills-installed-title">
        <div className="b2-memory-card-header"><div><h2 id="skills-installed-title">已安装 Skill</h2><p>{skills.length} 个 Skill；可按项目启用、停用或卸载安装副本。</p></div></div>
        <div className="b2-memory-form"><label htmlFor="skills-project">项目范围</label><ProjectSelect projects={state.projects} value={projectId} onChange={setProjectId} id="skills-project" /><small>项目级启停会提交 project_id；卸载安装副本不会删除独立来源。</small></div>
        {skills.length === 0 ? <div className="b2-memory-card"><EmptyState icon={Sparkles} title="暂无已安装 Skill" description="从上方目录选择安装。" /></div> : <div className="b2-memory-grid">{skills.map((skill) => { const projectIds = skill.project_ids ?? []; const enabled = Boolean(projectId && projectIds.includes(projectId)); return <article className="b2-memory-card" key={skill.skill_id} data-state={skill.state}><div className="b2-memory-card-header"><div><h3>{skill.name}</h3><p><code>{skill.package_ref}</code></p></div><StatusBadge status={statusKind(skill.state)} label={statusLabel(skill.state)} size="sm" /></div><div className="b2-memory-meta"><span>信任：{skill.trust_status ?? '未提供'}</span><span>已启用项目：{projectIds.length ? projectIds.join('、') : '无'}</span></div><div className="b2-memory-actions"><ActionButton label={enabled ? '停用当前项目' : '启用当前项目'} onClick={() => void toggleProjectSkill(skill)} disabled={busy || !projectId || ['disabled', 'deactivated', 'uninstalled'].includes(skill.state)} icon={<Power size={13} aria-hidden="true" />} /><ActionButton label={uninstallId === skill.skill_id ? '再次点击确认卸载' : '卸载 Skill'} tone={uninstallId === skill.skill_id ? 'primary' : 'ghost'} onClick={() => void uninstall(skill)} disabled={busy} icon={<Trash2 size={13} aria-hidden="true" />} /></div>{uninstallId === skill.skill_id && <p className="b2-memory-warning" role="status">卸载只删除当前安装副本并保留来源；再次点击按钮才提交。</p>}</article>; })}</div>}
      </section>
    </div>
  );
};

export const LiveManagementView: React.FC<{ initialTab?: ManagementTab }> = ({ initialTab = 'settings' }) => {
  const { clientMode, b23Client, b2Client, connectionStatus, addNotification } = useOperant();
  const { showSidebarOpenBtn, openSidebar } = useOutletContext<RailOutletContext>();
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = queryTab(searchParams.get('tab'), initialTab);
  const adapter = useMemo(() => b23Client ? new B23ManagementAdapter(b23Client) : null, [b23Client]);
  const b25Client = useMemo(() => new B25Client(currentBrowserOrigin()), []);
  const [phase, setPhase] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle');
  const [state, setState] = useState<B23ManagementState>();
  const [error, setError] = useState<B23UiError>();
  const [actionLabel, setActionLabel] = useState<string>();
  const [lastResult, setLastResult] = useState<{ status: string; message: string }>();
  const [auditReport, setAuditReport] = useState<ArtifactAuditReportView>();
  const [unknownWrite, setUnknownWrite] = useState(false);
  const requestEpoch = useRef(0);

  const setTab = useCallback((next: ManagementTab) => {
    const nextParams = new URLSearchParams(searchParams);
    nextParams.set('tab', next);
    setSearchParams(nextParams, { replace: true });
  }, [searchParams, setSearchParams]);

  const refresh = useCallback(async () => {
    const epoch = ++requestEpoch.current;
    setPhase('loading');
    setError(undefined);
    setLastResult(undefined);
    setAuditReport(undefined);
    if (!adapter) {
      if (epoch === requestEpoch.current) {
        setError(makeAdapterUnavailableError().detail);
        setPhase('error');
      }
      return;
    }
    try {
      await adapter.connect();
      const next = await adapter.getManagement();
      if (epoch !== requestEpoch.current) return;
      setState(next);
      setPhase('ready');
      setUnknownWrite(false);
    } catch (value: unknown) {
      if (epoch !== requestEpoch.current) return;
      const detail = normalizeB23Error(value).detail;
      setError(detail);
      setPhase('error');
    }
  }, [adapter]);

  useEffect(() => {
    if (clientMode === 'live') void refresh();
    else {
      ++requestEpoch.current;
      setState(undefined);
      setPhase('idle');
      setError(undefined);
      setActionLabel(undefined);
      setLastResult(undefined);
      setAuditReport(undefined);
      setUnknownWrite(false);
    }
  }, [clientMode, refresh]);

  const execute = useCallback<ExecuteCommand>(async (command, label) => {
    if (!adapter || actionLabel) return undefined;
    const epoch = ++requestEpoch.current;
    setActionLabel(label);
    setError(undefined);
    setLastResult(undefined);
    setAuditReport(undefined);
    setUnknownWrite(false);
    try {
      const result = await adapter.executeManagementCommand(command);
      if (epoch !== requestEpoch.current) return result;
      // Only memory_search returns a filtered records side-channel. Other
      // commands may carry records=null while their authoritative state still
      // contains the complete bounded list.
      const nextState = command.action === 'memory_search' && result.records !== undefined
        ? { ...result.state, records: result.records ?? [] }
        : result.state;
      setState(nextState);
      setPhase('ready');
      setLastResult({ status: result.status, message: result.message });
      setAuditReport(command.action === 'artifact_audit' ? parseArtifactAuditExport(result.export_data) : undefined);
      addNotification(result.status === 'blocked' || result.status === 'failed' ? 'warn' : 'info', `${label}：${result.status} · ${result.message}`);
      return result;
    } catch (value: unknown) {
      const detail = normalizeB23Error(value).detail;
      if (epoch !== requestEpoch.current) return undefined;
      setError(detail);
      setPhase(blocksB23Management(detail) ? 'error' : 'ready');
      const isUnknown = detail.outcomeUnknown || detail.code === 'transport_unavailable';
      setUnknownWrite(isUnknown);
      if (isUnknown) addNotification('warn', `${label}结果未知，请刷新页面数据后人工核对；未自动重放。`);
      else addNotification('error', `${label}失败：${detail.message}`);
      return undefined;
    } finally {
      if (epoch === requestEpoch.current) setActionLabel(undefined);
    }
  }, [actionLabel, adapter, addNotification]);

  if (clientMode !== 'live') return null;

  if (!state && phase !== 'ready') {
    return (
      <div className="section-view" data-client-mode="live">
        <header className="section-header b2-memory-header"><div className="live-route-heading">{showSidebarOpenBtn && <button type="button" className="btn btn-secondary btn-icon" onClick={openSidebar} aria-label="打开服务侧栏"><PanelLeftOpen size={16} aria-hidden="true" /></button>}<div><span className="live-kicker"><span className="live-kicker-dot" aria-hidden="true" />实时服务</span><h1>管理中心</h1></div></div><ActionButton label="刷新" onClick={() => void refresh()} disabled={phase === 'loading'} icon={<RefreshCw size={14} aria-hidden="true" />} /></header>
        <div className="section-scroll"><div className="section-inner"><div className="b2-memory-card b2-memory-empty" role={error ? 'alert' : 'status'}>{phase === 'loading' && <Loader2 size={22} className="animate-spin" aria-hidden="true" />}{error ? <><AlertTriangle size={22} aria-hidden="true" /><h2>{error.code}</h2><p>{error.message}</p>{error.code === 'b2_3_client_unavailable' && <p>请在生成 `b2_3.generated.ts` 出现后把它注入 ClientContext；页面不会回退到 Demo。</p>}{error.outcomeUnknown && <p>当前写操作结果未知，请刷新后核对状态。</p>}</> : <><h2>{phase === 'loading' ? '正在读取管理 Projection…' : '管理页面未就绪'}</h2><p>Live 模式只显示服务端返回的项目、知识、插件、设置和 Skill。</p></>}{error && <ActionButton label="重新查询" tone="primary" onClick={() => void refresh()} disabled={phase === 'loading'} icon={<RefreshCw size={13} aria-hidden="true" />} />}</div></div></div>
      </div>
    );
  }

  const currentState = state as B23ManagementState;
  const writesDisabled = Boolean(actionLabel) || phase !== 'ready' || connectionStatus !== 'connected' || unknownWrite;
  const panelProps: ManagementPanelProps = {
    state: currentState,
    execute,
    busy: writesDisabled,
    onTab: setTab,
    auditReport,
    b25Client,
    b2Client,
    connectionStatus,
    refresh,
  };

  return (
    <div className="section-view" data-client-mode="live">
      <header className="section-header b2-memory-header"><div className="live-route-heading">{showSidebarOpenBtn && <button type="button" className="btn btn-secondary btn-icon" onClick={openSidebar} aria-label="打开 Core 侧栏"><PanelLeftOpen size={16} aria-hidden="true" /></button>}<div><span className="live-kicker"><span className="live-kicker-dot" aria-hidden="true" />实时服务</span><h1>管理中心</h1><p className="section-sub">项目、知识、插件、设置与 Skill 在这里统一管理。</p></div></div><div className="b2-memory-actions"><StatusBadge status={phase === 'ready' && connectionStatus === 'connected' ? 'connected' : 'pending'} label={phase === 'ready' && connectionStatus === 'connected' ? '已连接' : '未就绪'} size="sm" /><ActionButton label="刷新" onClick={() => void refresh()} disabled={Boolean(actionLabel)} icon={<RefreshCw size={14} aria-hidden="true" />} /></div></header>
      <div className="section-scroll"><div className="section-inner">
        {connectionStatus !== 'connected' && <FormError message="Core 连接已断开；管理写操作已禁用。" id="b2-connection-error" />}
        {error && <FormError message={`${error.code}：${error.message}${error.outcomeUnknown ? ' 写操作结果未知，请刷新后人工核对，客户端不会自动重放。' : ''}`} id="b2-management-error" />}
        {actionLabel && <div className="b2-memory-status" role="status" aria-live="polite"><Loader2 size={14} className="animate-spin" aria-hidden="true" />{actionLabel}处理中，请等待 Core 返回确认。</div>}
        {lastResult && <div className="b2-memory-status" data-status={lastResult.status} role={['blocked', 'failed'].includes(lastResult.status) ? 'alert' : 'status'} aria-live="polite">服务端返回：{statusLabel(lastResult.status)} · {lastResult.message}</div>}
        <nav className="b2-memory-tabs" aria-label="管理中心分区" role="tablist">{TABS.map((entry) => <button key={entry} type="button" role="tab" aria-selected={entry === tab} className={`b2-memory-tab${entry === tab ? ' active' : ''}`} onClick={() => setTab(entry)}>{TAB_LABELS[entry]}</button>)}</nav>
        {tab === 'projects' && <ProjectsPanel {...panelProps} />}
        {tab === 'knowledge' && <KnowledgePanel {...panelProps} />}
        {tab === 'plugins' && <PluginPanel {...panelProps} />}
        {tab === 'settings' && <SettingsPanel {...panelProps} />}
        {tab === 'skills' && <SkillsPanel {...panelProps} />}
        {tab === 'retention' && <RetentionPanel {...panelProps} />}
        <p className="section-footnote">管理操作由服务端确认；未知写结果只允许刷新/人工核对。<Link to="/chat">返回 B2 聊天</Link></p>
      </div></div>
    </div>
  );
};
