import React, { useCallback, useId, useMemo, useState } from 'react';
import {
  AlertCircle,
  AlertTriangle,
  Ban,
  Bot,
  Check,
  CheckSquare,
  Clock,
  Cpu,
  Database,
  Eye,
  FileText,
  History,
  Info,
  Loader2,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  ShieldAlert,
  Sparkles,
  X,
} from 'lucide-react';
import type { B25PresentationProps } from './b25-presentation-types';
import { exactRelationshipTarget } from './b25-state';
import './b25-governance.css';

type GovernanceState = NonNullable<B25PresentationProps['state']>;
type GovernanceRecord = GovernanceState['records'][number];
type GovernanceEntry = GovernanceState['proposals'][number];
type ExactProposal = B25PresentationProps['selected'][number];
type B25Command = Parameters<B25PresentationProps['onCommand']>[0];
type MemoryRelationship = NonNullable<GovernanceRecord['relationships']>[number];

export type B25TabKey = 'inbox' | 'records' | 'history' | 'maintenance';

function formatDate(isoString?: string | null): string {
  if (!isoString) return '未设置';
  try {
    const date = new Date(isoString);
    if (Number.isNaN(date.getTime())) return isoString;
    return date.toLocaleString('zh-CN', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    });
  } catch {
    return isoString;
  }
}

function formatScope(scope: unknown): string {
  if (!scope) return '项目范围';
  if (typeof scope === 'string') return scope;
  if (typeof scope === 'object' && scope !== null) {
    const s = scope as Record<string, unknown>;
    if (s.kind === 'workspace') return `工作区${s.workspace_id ? ` (${s.workspace_id})` : ''}`;
    if (s.kind === 'session') return `会话${s.session_id ? ` (${s.session_id})` : ''}`;
    if (s.kind === 'run') return `运行${s.run_id ? ` (${s.run_id})` : ''}`;
    if (s.kind === 'personal') return `个人${s.user_id ? ` (${s.user_id})` : ''}`;
    if (typeof s.kind === 'string') return s.kind;
  }
  return '项目范围';
}

export const B25Presentation: React.FC<B25PresentationProps> = ({
  state,
  history,
  detail,
  selected,
  query,
  loading,
  busy,
  readOnly,
  error,
  notice,
  modelProfiles,
  onQueryChange,
  onSearch,
  onExpand,
  onRefresh,
  onSelectionChange,
  onCommand,
}) => {
  const [activeTab, setActiveTab] = useState<B25TabKey>('inbox');
  const [batchConfirmed, setBatchConfirmed] = useState(false);

  // Propose form state
  const [showProposeForm, setShowProposeForm] = useState(false);
  const [proposeRecordId, setProposeRecordId] = useState('');
  const [proposeExpectedHeadRevision, setProposeExpectedHeadRevision] = useState<number | ''>('');
  const [proposeContent, setProposeContent] = useState('');
  const [proposeValidFrom, setProposeValidFrom] = useState('');
  const [proposeValidUntil, setProposeValidUntil] = useState('');
  const [proposeReviewDueAt, setProposeReviewDueAt] = useState('');
  const [hasRelationship, setHasRelationship] = useState(false);
  const [relType, setRelType] = useState<'conflicts_with' | 'supersedes'>('conflicts_with');
  const [relTarget, setRelTarget] = useState<MemoryRelationship['target'] | null>(null);
  const [formValidationError, setFormValidationError] = useState<string | null>(null);

  // Maintenance job form state
  const [selectedModelProfileId, setSelectedModelProfileId] = useState('');
  const [maxSources, setMaxSources] = useState(20);
  const [maxOutputTokens, setMaxOutputTokens] = useState(1024);

  const queryInputId = useId();
  const proposeContentId = useId();
  const proposeRecordIdInput = useId();
  const modelSelectId = useId();

  // Initialize selected model profile if available
  const activeModelProfileId = useMemo(() => {
    if (selectedModelProfileId) return selectedModelProfileId;
    return modelProfiles[0]?.id ?? '';
  }, [selectedModelProfileId, modelProfiles]);

  // Conflict count in proposals
  const conflictCount = useMemo(() => {
    if (!state?.proposals) return 0;
    return state.proposals.filter(
      (entry) =>
        entry.proposal.state === 'conflict' ||
        (entry.relationships ?? []).some((r) => r.relation === 'conflicts_with'),
    ).length;
  }, [state?.proposals]);

  // Check if a proposal is selected in ExactProposal[]
  const isProposalSelected = useCallback(
    (entry: GovernanceEntry) => {
      return selected.some(
        (sel) =>
          sel.proposal_id === entry.proposal.proposal_id &&
          sel.proposal_revision === entry.proposal.proposal_revision,
      );
    },
    [selected],
  );

  // Toggle proposal selection
  const toggleSelectProposal = useCallback(
    (entry: GovernanceEntry) => {
      if (readOnly) return;
      const exists = isProposalSelected(entry);
      if (exists) {
        onSelectionChange(
          selected.filter(
            (sel) =>
              !(
                sel.proposal_id === entry.proposal.proposal_id &&
                sel.proposal_revision === entry.proposal.proposal_revision
              ),
          ),
        );
      } else {
        const item: ExactProposal = {
          proposal_id: entry.proposal.proposal_id,
          proposal_revision: entry.proposal.proposal_revision,
          proposed_version: entry.proposal.proposed_version,
          base_head_revision: entry.proposal.base_head.revision,
        };
        onSelectionChange([...selected, item]);
      }
    },
    [selected, isProposalSelected, onSelectionChange, readOnly],
  );

  // Select all visible proposals
  const handleSelectAllVisible = useCallback(() => {
    if (readOnly || !state?.proposals) return;
    const allVisible: ExactProposal[] = state.proposals.map((entry) => ({
      proposal_id: entry.proposal.proposal_id,
      proposal_revision: entry.proposal.proposal_revision,
      proposed_version: entry.proposal.proposed_version,
      base_head_revision: entry.proposal.base_head.revision,
    }));
    onSelectionChange(allVisible);
  }, [readOnly, state?.proposals, onSelectionChange]);

  // Deselect all
  const handleDeselectAll = useCallback(() => {
    onSelectionChange([]);
    setBatchConfirmed(false);
  }, [onSelectionChange]);

  // Single review
  const handleSingleReview = useCallback(
    (entry: GovernanceEntry, decision: 'accept' | 'reject') => {
      if (!state?.project_id || readOnly) return;
      const exactItem: ExactProposal = {
        proposal_id: entry.proposal.proposal_id,
        proposal_revision: entry.proposal.proposal_revision,
        proposed_version: entry.proposal.proposed_version,
        base_head_revision: entry.proposal.base_head.revision,
      };
      onCommand({
        action: 'review',
        project_id: state.project_id,
        decision,
        selections: [exactItem],
      });
    },
    [state?.project_id, readOnly, onCommand],
  );

  // Batch review execution
  const handleBatchReview = useCallback(
    (decision: 'accept' | 'reject') => {
      if (!state?.project_id || selected.length === 0 || !batchConfirmed || readOnly) return;
      onCommand({
        action: 'review',
        project_id: state.project_id,
        decision,
        selections: selected,
      });
      onSelectionChange([]);
      setBatchConfirmed(false);
    },
    [state?.project_id, selected, batchConfirmed, readOnly, onCommand, onSelectionChange],
  );

  // Submit propose command
  const handleSubmitPropose = useCallback(
    (event: React.FormEvent) => {
      event.preventDefault();
      if (!state?.project_id || readOnly) return;
      setFormValidationError(null);

      const trimmedContent = proposeContent.trim();
      if (!trimmedContent) {
        setFormValidationError('提议正文不能为空。');
        return;
      }

      // Validate dates
      if (proposeValidFrom && proposeValidUntil) {
        const fromTime = new Date(proposeValidFrom).getTime();
        const untilTime = new Date(proposeValidUntil).getTime();
        if (untilTime <= fromTime) {
          setFormValidationError('失效时间必须晚于生效时间。');
          return;
        }
      }

      const relationships: MemoryRelationship[] = [];
      if (hasRelationship) {
        const target = exactRelationshipTarget(relTarget, state.records);
        if (!target) {
          setFormValidationError('请选择当前已发布记录的精确版本；目标变化后需重新选择。');
          return;
        }
        relationships.push({ relation: relType, target });
      }

      const cmd: B25Command = {
        action: 'propose',
        project_id: state.project_id,
        record_id: proposeRecordId.trim() || null,
        expected_head_revision:
          typeof proposeExpectedHeadRevision === 'number' ? proposeExpectedHeadRevision : null,
        content: trimmedContent,
        relationships,
        valid_from: proposeValidFrom ? new Date(proposeValidFrom).toISOString() : null,
        valid_until: proposeValidUntil ? new Date(proposeValidUntil).toISOString() : null,
        review_due_at: proposeReviewDueAt ? new Date(proposeReviewDueAt).toISOString() : null,
      };

      onCommand(cmd);

      // Reset form
      setProposeContent('');
      setProposeRecordId('');
      setProposeExpectedHeadRevision('');
      setProposeValidFrom('');
      setProposeValidUntil('');
      setProposeReviewDueAt('');
      setHasRelationship(false);
      setRelTarget(null);
      setShowProposeForm(false);
    },
    [
      state?.project_id,
      readOnly,
      proposeContent,
      proposeValidFrom,
      proposeValidUntil,
      proposeReviewDueAt,
      hasRelationship,
      relType,
      relTarget,
      state?.records,
      proposeRecordId,
      proposeExpectedHeadRevision,
      onCommand,
    ],
  );

  // Pre-fill propose form from existing record
  const handlePrefillPropose = useCallback((record: GovernanceRecord) => {
    setProposeRecordId(record.version.ref.record_id);
    setProposeExpectedHeadRevision(record.head.revision);
    setProposeContent(record.version.content);
    setShowProposeForm(true);
    setActiveTab('records');
  }, []);

  // Submit maintenance create
  const handleCreateMaintenance = useCallback(
    (event: React.FormEvent) => {
      event.preventDefault();
      if (!state?.project_id || !state.maintenance_enabled || !activeModelProfileId || readOnly) return;
      onCommand({
        action: 'maintenance_create',
        project_id: state.project_id,
        model_profile_id: activeModelProfileId,
        max_sources: maxSources,
        max_output_tokens: maxOutputTokens,
      });
    },
    [state?.project_id, state?.maintenance_enabled, activeModelProfileId, maxSources, maxOutputTokens, readOnly, onCommand],
  );

  return (
    <div className="b25-governance" data-testid="b25-governance-view">
      {/* 1. Header with perspective and metadata */}
      <header className="b25-header">
        <div className="b25-title-group">
          <h1 className="b25-title">
            <Sparkles size={20} className="b25-icon" aria-hidden="true" />
            <span>知识整理与治理</span>
            {loading && <Loader2 size={16} className="animate-spin" aria-label="加载中" />}
          </h1>
          <p className="b25-subtitle">
            项目范围：<code>{state?.project_id ?? '未指定项目'}</code> · 遵循 MP-4 水位幂等、有权限历史与有限后台整理规范
          </p>
        </div>

        <div className="b25-header-tags">
          <span
            className="b25-perspective-tag"
            data-perspective={activeTab === 'history' ? 'historical_fact' : 'current_agreement'}
          >
            {activeTab === 'history' ? (
              <>
                <History size={13} aria-hidden="true" />
                <span>当时事实视角 (historical_fact)</span>
              </>
            ) : (
              <>
                <FileText size={13} aria-hidden="true" />
                <span>当前约定视角 (current_agreement)</span>
              </>
            )}
          </span>

          <span className={`b25-badge ${state?.enabled ? 'b25-badge-safe' : 'b25-badge-neutral'}`}>
            记忆功能：{state?.enabled ? '已开启' : '已关闭'}
          </span>

          {readOnly && (
            <span className="b25-badge b25-badge-warn">
              <Ban size={12} aria-hidden="true" />
              <span>只读模式</span>
            </span>
          )}

          <button
            type="button"
            className="b25-btn b25-btn-secondary b25-btn-sm"
            onClick={onRefresh}
            disabled={busy}
            title="刷新当前治理状态"
          >
            <RefreshCw size={13} className={busy ? 'animate-spin' : ''} aria-hidden="true" />
            <span>刷新</span>
          </button>
        </div>
      </header>

      {/* 2. Global Error / Notice / ReadOnly Banners */}
      {error && (
        <div className="b25-alert b25-alert-error" role="alert">
          <AlertCircle size={17} aria-hidden="true" />
          <span>{error}</span>
        </div>
      )}

      {(state?.unresolved_command_ids ?? []).length > 0 && (
        <div className="b25-alert b25-alert-warning" role="alert">
          <AlertTriangle size={17} aria-hidden="true" />
          <span>有 {(state?.unresolved_command_ids ?? []).length} 项操作结果待核对。请对照当前记录、候选与后台状态核实；原请求不会自动重放。</span>
        </div>
      )}

      {notice && (
        <div className="b25-alert b25-alert-info" role="status">
          <Info size={17} aria-hidden="true" />
          <span>{notice}</span>
        </div>
      )}

      {readOnly && (
        <div className="b25-alert b25-alert-warning" role="status">
          <AlertTriangle size={17} aria-hidden="true" />
          <div className="b25-alert-content">
            <strong>当前处于只读模式：</strong>
            <span>服务断线或无修改权限，界面仅展示本地已有只读投影。所有提议、审阅及后台调度操作均已禁用。</span>
          </div>
          <div className="b25-alert-actions">
            <button
              type="button"
              className="b25-btn b25-btn-secondary b25-btn-sm"
              onClick={onRefresh}
              disabled={busy}
            >
              <RotateCcw size={13} aria-hidden="true" />
              <span>重试连接</span>
            </button>
          </div>
        </div>
      )}

      {/* 3. Navigation Tabs */}
      <nav className="b25-tabs" aria-label="知识治理模块导航">
        <button
          type="button"
          className={`b25-tab ${activeTab === 'inbox' ? 'b25-tab-active' : ''}`}
          onClick={() => setActiveTab('inbox')}
        >
          <span>候选与冲突收件箱</span>
          <span
            className={`b25-tab-count ${conflictCount > 0 ? 'b25-tab-count-alert' : ''}`}
            title={conflictCount > 0 ? `存在 ${conflictCount} 项冲突` : undefined}
          >
            {state?.proposals?.length ?? 0}
          </span>
        </button>

        <button
          type="button"
          className={`b25-tab ${activeTab === 'records' ? 'b25-tab-active' : ''}`}
          onClick={() => setActiveTab('records')}
        >
          <span>正式知识约定</span>
          <span className="b25-tab-count">{state?.records?.length ?? 0}</span>
        </button>

        <button
          type="button"
          className={`b25-tab ${activeTab === 'history' ? 'b25-tab-active' : ''}`}
          onClick={() => setActiveTab('history')}
        >
          <span>原始历史搜索</span>
          <span className="b25-tab-count">{history?.items?.length ?? 0}</span>
        </button>

        <button
          type="button"
          className={`b25-tab ${activeTab === 'maintenance' ? 'b25-tab-active' : ''}`}
          onClick={() => setActiveTab('maintenance')}
        >
          <span>后台整理与状态</span>
          <span className="b25-tab-count">{state?.jobs?.length ?? 0}</span>
        </button>
      </nav>

      {/* 4. Batch Operations Floating / Top Bar (Displayed whenever ExactProposals are selected) */}
      {selected.length > 0 && (
        <section className="b25-batch-bar" aria-labelledby="b25-batch-title">
          <div className="b25-batch-header">
            <h2 id="b25-batch-title" className="b25-batch-title">
              <CheckSquare size={16} aria-hidden="true" />
              <span>批量精确审阅（已选中 {selected.length} 项 ExactProposal）</span>
            </h2>
            <button
              type="button"
              className="b25-btn b25-btn-ghost b25-btn-sm"
              onClick={handleDeselectAll}
              disabled={busy}
            >
              <X size={13} aria-hidden="true" />
              <span>清空选择</span>
            </button>
          </div>

          <div className="b25-batch-items" aria-label="所选精确候选清单">
            {selected.map((item) => (
              <span
                key={`${item.proposal_id}-${item.proposal_revision}`}
                className="b25-batch-item-chip"
              >
                <span>ID: {item.proposal_id}</span>
                <span>rev {item.proposal_revision}</span>
                <span>目标: v{item.proposed_version.version}</span>
                <span>base rev: {item.base_head_revision}</span>
              </span>
            ))}
          </div>

          <div className="b25-batch-footer">
            <label className="b25-batch-confirm">
              <input
                type="checkbox"
                checked={batchConfirmed}
                onChange={(e) => setBatchConfirmed(e.target.checked)}
                disabled={busy || readOnly}
              />
              <span>我已仔细核对所选 {selected.length} 项候选的内容及精确版本</span>
            </label>

            <div className="b25-btn-group">
              <button
                type="button"
                className="b25-btn b25-btn-primary"
                onClick={() => handleBatchReview('accept')}
                disabled={busy || !batchConfirmed || readOnly}
              >
                <Check size={14} aria-hidden="true" />
                <span>批量接受并合并</span>
              </button>
              <button
                type="button"
                className="b25-btn b25-btn-danger"
                onClick={() => handleBatchReview('reject')}
                disabled={busy || !batchConfirmed || readOnly}
              >
                <X size={14} aria-hidden="true" />
                <span>批量拒绝</span>
              </button>
            </div>
          </div>
        </section>
      )}

      {/* 5. TAB 1: 候选与冲突收件箱 (Inbox) */}
      {activeTab === 'inbox' && (
        <section aria-labelledby="b25-inbox-title" className="b25-section">
          <div className="b25-card-header">
            <div>
              <h2 id="b25-inbox-title" className="b25-card-title">
                待审阅候选与冲突收件箱
              </h2>
              <p className="b25-subtitle">
                共 {state?.proposals?.length ?? 0} 条待确认项；
                模型推断必须经人工核验，冲突需解决后方可合并入正式知识。
              </p>
            </div>

            <div className="b25-btn-group">
              <button
                type="button"
                className="b25-btn b25-btn-secondary b25-btn-sm"
                onClick={handleSelectAllVisible}
                disabled={busy || readOnly || !state?.proposals?.length}
              >
                <CheckSquare size={13} aria-hidden="true" />
                <span>全选已展示候选</span>
              </button>
            </div>
          </div>

          {(!state?.proposals || state.proposals.length === 0) ? (
            <div className="b25-empty">
              <Sparkles size={32} className="b25-empty-icon" aria-hidden="true" />
              <h3 className="b25-empty-title">收件箱为空</h3>
              <p className="b25-empty-desc">
                当前项目没有待审阅或发生冲突的记忆提议。后台整理或任务产出新候选后会显示在此处。
              </p>
            </div>
          ) : (
            <div className="b25-grid">
              {state.proposals.map((entry) => {
                const isSelected = isProposalSelected(entry);
                const hasConflict =
                  entry.proposal.state === 'conflict' ||
                  (entry.relationships ?? []).some((r) => r.relation === 'conflicts_with');
                const isBlocked = Boolean(entry.blocked_reason);

                return (
                  <article
                    key={`${entry.proposal.proposal_id}-${entry.proposal.proposal_revision}`}
                    className={`b25-card ${isSelected ? 'b25-card-selected' : ''} ${
                      hasConflict ? 'b25-card-conflict' : ''
                    } ${isBlocked ? 'b25-card-blocked' : ''}`}
                    data-proposal-id={entry.proposal.proposal_id}
                  >
                    <div className="b25-card-header">
                      <div className="b25-card-header-left">
                        <input
                          type="checkbox"
                          checked={isSelected}
                          onChange={() => toggleSelectProposal(entry)}
                          disabled={busy || readOnly}
                          aria-label={`选择提议 ${entry.proposal.proposal_id}`}
                        />
                        <div>
                          <h3 className="b25-card-title">
                            提议 <code>{entry.proposal.proposal_id}</code>
                          </h3>
                          <div className="b25-card-meta">
                            <span>rev {entry.proposal.proposal_revision}</span>
                            <span>操作: {entry.proposal.operation}</span>
                            <span>基准 rev: {entry.proposal.base_head.revision}</span>
                          </div>
                        </div>
                      </div>

                      <div className="b25-card-meta">
                        {hasConflict && (
                          <span className="b25-badge b25-badge-warn">
                            <AlertTriangle size={11} aria-hidden="true" />
                            <span>冲突发生</span>
                          </span>
                        )}
                        <span
                          className={`b25-badge ${
                            entry.proposal.state === 'accepted'
                              ? 'b25-badge-safe'
                              : entry.proposal.state === 'rejected'
                              ? 'b25-badge-error'
                              : 'b25-badge-neutral'
                          }`}
                        >
                          {entry.proposal.state}
                        </span>
                      </div>
                    </div>

                    {/* Evidence & Status Tags */}
                    <div className="b25-card-meta">
                      {entry.version.evidence === 'inferred' ? (
                        <span className="b25-badge b25-badge-inferred" title="模型自动推断提取，必须人工验证">
                          <AlertCircle size={11} aria-hidden="true" />
                          <span>{entry.proposal.extractor_version === 'user' ? '人工提议（待确认）' : '模型推断（未验证）'}</span>
                        </span>
                      ) : entry.version.evidence === 'tested' ? (
                        <span className="b25-badge b25-badge-safe">
                          <Check size={11} aria-hidden="true" />
                          <span>测试验证</span>
                        </span>
                      ) : entry.version.evidence === 'user_asserted' ? (
                        <span className="b25-badge b25-badge-safe">
                          <Check size={11} aria-hidden="true" />
                          <span>用户确认</span>
                        </span>
                      ) : (
                        <span className="b25-badge b25-badge-neutral">
                          <span>证据: {entry.version.evidence}</span>
                        </span>
                      )}

                      <span className="b25-badge b25-badge-neutral">
                        独立来源数: {entry.independent_evidence_count}
                      </span>

                      {entry.review_expired ? (
                        <span className="b25-badge b25-badge-error">
                          <Clock size={11} aria-hidden="true" />
                          <span>复核已过期</span>
                        </span>
                      ) : entry.review_due_at ? (
                        <span className="b25-badge b25-badge-neutral">
                          <Clock size={11} aria-hidden="true" />
                          <span>到期: {formatDate(entry.review_due_at)}</span>
                        </span>
                      ) : null}
                    </div>

                    {/* Blocked reason banner */}
                    {entry.blocked_reason && (
                      <div className="b25-alert b25-alert-error" role="alert">
                        <ShieldAlert size={14} aria-hidden="true" />
                        <span>阻断原因：{entry.blocked_reason}</span>
                      </div>
                    )}

                    {/* Relationships */}
                    {(entry.relationships ?? []).length > 0 && (
                      <div className="b25-card-meta">
                        {(entry.relationships ?? []).map((rel, idx) => (
                          <span
                            key={`${entry.proposal.proposal_id}-rel-${idx}`}
                            className={`b25-badge ${
                              rel.relation === 'conflicts_with' ? 'b25-badge-warn' : 'b25-badge-info'
                            }`}
                          >
                            <span>{rel.relation === 'conflicts_with' ? '冲突' : '替代'}: </span>
                            <code>
                              {rel.target.record_id}:v{rel.target.version}
                            </code>
                          </span>
                        ))}
                      </div>
                    )}

                    {/* Proposed Content */}
                    <div className="b25-card-content">{entry.version.content}</div>

                    {/* Proposal rationale */}
                    {entry.proposal.reason && (
                      <div className="b25-help-box">
                        <strong>提取理由：</strong>
                        <span>{entry.proposal.reason}</span>
                        <span className="b25-label-help">（extractor: {entry.proposal.extractor_version}）</span>
                      </div>
                    )}

                    {/* Footer Actions */}
                    <div className="b25-card-footer">
                      <div className="b25-card-meta">
                        <span>
                          目标版本: <code>v{entry.proposal.proposed_version.version}</code>
                        </span>
                      </div>

                      <div className="b25-btn-group">
                        <button
                          type="button"
                          className="b25-btn b25-btn-primary b25-btn-sm"
                          onClick={() => handleSingleReview(entry, 'accept')}
                          disabled={busy || readOnly}
                        >
                          <Check size={13} aria-hidden="true" />
                          <span>接受</span>
                        </button>
                        <button
                          type="button"
                          className="b25-btn b25-btn-danger b25-btn-sm"
                          onClick={() => handleSingleReview(entry, 'reject')}
                          disabled={busy || readOnly}
                        >
                          <X size={13} aria-hidden="true" />
                          <span>拒绝</span>
                        </button>
                      </div>
                    </div>
                  </article>
                );
              })}
            </div>
          )}
        </section>
      )}

      {/* 6. TAB 2: 正式知识约定 (Records) & 纠正表单 */}
      {activeTab === 'records' && (
        <section aria-labelledby="b25-records-title" className="b25-section">
          <div className="b25-card-header">
            <div>
              <h2 id="b25-records-title" className="b25-card-title">
                当前正式知识约定
              </h2>
              <p className="b25-subtitle">
                当前项目共 {state?.records?.length ?? 0} 条知识记录（含已阻止项）；修改需以 Proposal / CAS 提议提交，不会自动静默覆盖。
              </p>
            </div>

            <div className="b25-btn-group">
              <button
                type="button"
                className="b25-btn b25-btn-primary b25-btn-sm"
                onClick={() => {
                  setShowProposeForm((prev) => !prev);
                  setFormValidationError(null);
                }}
                disabled={busy || readOnly}
              >
                {showProposeForm ? <X size={13} aria-hidden="true" /> : <Plus size={13} aria-hidden="true" />}
                <span>{showProposeForm ? '收起提议表单' : '提出知识修改提议'}</span>
              </button>
            </div>
          </div>

          {/* Propose / Correct Form */}
          {showProposeForm && (
            <form className="b25-card b25-form" onSubmit={handleSubmitPropose}>
              <div className="b25-card-header">
                <h3 className="b25-card-title">
                  <Plus size={15} aria-hidden="true" />
                  <span>提交知识提议（Propose）</span>
                </h3>
                <span className="b25-badge b25-badge-info">进入候选审阅流</span>
              </div>

              {formValidationError && (
                <div className="b25-alert b25-alert-error" role="alert">
                  <AlertTriangle size={15} aria-hidden="true" />
                  <span>{formValidationError}</span>
                </div>
              )}

              <div className="b25-form-row">
                <div className="b25-form-col">
                  <label htmlFor={proposeRecordIdInput} className="b25-label">
                    <span>目标记录 ID（修改现有记录时填写，留空为新记录）</span>
                  </label>
                  <input
                    id={proposeRecordIdInput}
                    className="b25-input"
                    value={proposeRecordId}
                    onChange={(e) => setProposeRecordId(e.target.value)}
                    placeholder="留空则创建全新知识条目"
                    disabled={busy || readOnly}
                  />
                </div>

                <div className="b25-form-col">
                  <label className="b25-label">
                    <span>期望基准 head revision</span>
                    <span className="b25-label-help">（CAS 乐观并发检查）</span>
                  </label>
                  <input
                    type="number"
                    min={0}
                    className="b25-input"
                    value={proposeExpectedHeadRevision}
                    onChange={(e) =>
                      setProposeExpectedHeadRevision(
                        e.target.value === '' ? '' : parseInt(e.target.value, 10),
                      )
                    }
                    placeholder="例如 0 或目标记录 head revision"
                    disabled={busy || readOnly}
                  />
                </div>
              </div>

              <div className="b25-form-col">
                <label htmlFor={proposeContentId} className="b25-label">
                  <span>提议知识内容 *</span>
                </label>
                <textarea
                  id={proposeContentId}
                  className="b25-textarea"
                  rows={4}
                  value={proposeContent}
                  onChange={(e) => setProposeContent(e.target.value)}
                  placeholder="清晰描述经过核实的约定、偏好或事实..."
                  required
                  disabled={busy || readOnly}
                />
              </div>

              {/* Time bounding */}
              <div className="b25-form-row">
                <div className="b25-form-col">
                  <label className="b25-label">
                    <span>生效时间（valid_from）</span>
                  </label>
                  <input
                    type="datetime-local"
                    className="b25-input"
                    value={proposeValidFrom}
                    onChange={(e) => setProposeValidFrom(e.target.value)}
                    disabled={busy || readOnly}
                  />
                </div>

                <div className="b25-form-col">
                  <label className="b25-label">
                    <span>失效时间（valid_until）</span>
                  </label>
                  <input
                    type="datetime-local"
                    className="b25-input"
                    value={proposeValidUntil}
                    onChange={(e) => setProposeValidUntil(e.target.value)}
                    disabled={busy || readOnly}
                  />
                </div>

                <div className="b25-form-col">
                  <label className="b25-label">
                    <span>复核到期时间（review_due_at）</span>
                  </label>
                  <input
                    type="datetime-local"
                    className="b25-input"
                    value={proposeReviewDueAt}
                    onChange={(e) => setProposeReviewDueAt(e.target.value)}
                    disabled={busy || readOnly}
                  />
                </div>
              </div>

              {/* Relationships builder */}
              <div className="b25-form-col">
                <label className="b25-batch-confirm">
                  <input
                    type="checkbox"
                    checked={hasRelationship}
                    onChange={(e) => setHasRelationship(e.target.checked)}
                    disabled={busy || readOnly}
                  />
                  <span>声明与既有记录的关系（冲突 / 替代）</span>
                </label>

                {hasRelationship && (
                  <div className="b25-form-row" style={{ marginTop: '8px' }}>
                    <div className="b25-form-col">
                      <label className="b25-label">关系类型</label>
                      <select
                        className="b25-select"
                        value={relType}
                        onChange={(e) => setRelType(e.target.value as typeof relType)}
                        disabled={busy || readOnly}
                      >
                        <option value="conflicts_with">发生冲突 (conflicts_with)</option>
                        <option value="supersedes">替代旧版 (supersedes)</option>
                      </select>
                    </div>

                    <div className="b25-form-col">
                      <label className="b25-label" htmlFor="b25-relation-target">目标已发布记录</label>
                      <select id="b25-relation-target" className="b25-select"
                        value={relTarget ? JSON.stringify(relTarget) : ''}
                        onChange={(event) => {
                          const record = state?.records.find((item) => JSON.stringify(item.version.ref) === event.target.value);
                          setRelTarget(record ? { ...record.version.ref } : null);
                        }}
                        disabled={busy || readOnly}>
                        <option value="">选择目标记录与版本</option>
                        {state?.records.filter((record) => record.head.state === 'published').map((record) => (
                          <option key={JSON.stringify(record.version.ref)} value={JSON.stringify(record.version.ref)}>
                            {record.version.content.slice(0, 70)} · v{record.version.ref.version} · {record.version.ref.record_id}
                          </option>
                        ))}
                      </select>
                      <p className="b25-help-text">提交所选版本的完整来源身份；目标已变化时请刷新后重新选择。</p>
                    </div>
                  </div>
                )}
              </div>

              <div className="b25-help-box">
                <span>提示：新提议将进入待确认候选收件箱，需经人工确认后发布。不会直接改写前台运行中的正式知识。</span>
              </div>

              <div className="b25-card-footer">
                <button
                  type="button"
                  className="b25-btn b25-btn-ghost"
                  onClick={() => setShowProposeForm(false)}
                  disabled={busy}
                >
                  取消
                </button>
                <button
                  type="submit"
                  className="b25-btn b25-btn-primary"
                  disabled={busy || readOnly || !proposeContent.trim()}
                >
                  <Check size={14} aria-hidden="true" />
                  <span>提交提议</span>
                </button>
              </div>
            </form>
          )}

          {/* Records Grid */}
          {(!state?.records || state.records.length === 0) ? (
            <div className="b25-empty">
              <Database size={32} className="b25-empty-icon" aria-hidden="true" />
              <h3 className="b25-empty-title">暂无正式知识记录</h3>
              <p className="b25-empty-desc">
                当前项目尚未合并任何正式知识。点击上方“提出知识修改提议”提交第一条约定。
              </p>
            </div>
          ) : (
            <div className="b25-grid">
              {state.records.map((record) => (
                <article
                  key={`${record.version.ref.record_id}-${record.version.ref.version}`}
                  className={`b25-card ${!record.currently_usable ? 'b25-card-blocked' : ''}`}
                >
                  <div className="b25-card-header">
                    <div>
                      <h3 className="b25-card-title">
                        记录 <code>{record.version.ref.record_id}</code>
                      </h3>
                      <div className="b25-card-meta">
                        <span>版本: v{record.version.ref.version}</span>
                        <span>head rev: {record.head.revision}</span>
                        <span>类型: {record.version.content_type}</span>
                      </div>
                    </div>

                    <span
                      className={`b25-badge ${
                        record.currently_usable ? 'b25-badge-safe' : 'b25-badge-error'
                      }`}
                    >
                      {record.currently_usable ? '当前可用' : '已被阻止'}
                    </span>
                  </div>

                  {/* Badges & Metadata */}
                  <div className="b25-card-meta">
                    <span className="b25-badge b25-badge-neutral">
                      证据: {record.version.evidence}
                    </span>
                    <span className="b25-badge b25-badge-neutral">
                      独立证据数: {record.independent_evidence_count}
                    </span>
                    <span className="b25-badge b25-badge-neutral">
                      范围: {formatScope(record.version.scope)}
                    </span>
                  </div>

                  {/* Blocked reason */}
                  {record.blocked_reason && (
                    <div className="b25-alert b25-alert-error" role="alert">
                      <ShieldAlert size={14} aria-hidden="true" />
                      <span>阻断原因：{record.blocked_reason}</span>
                    </div>
                  )}

                  {/* Relationships */}
                  {(record.relationships ?? []).length > 0 && (
                    <div className="b25-card-meta">
                      {(record.relationships ?? []).map((rel, idx) => (
                        <span
                          key={`rec-rel-${idx}`}
                          className={`b25-badge ${
                            rel.relation === 'conflicts_with' ? 'b25-badge-warn' : 'b25-badge-info'
                          }`}
                        >
                          <span>{rel.relation === 'conflicts_with' ? '冲突' : '替代'}: </span>
                          <code>
                            {rel.target.record_id}:v{rel.target.version}
                          </code>
                        </span>
                      ))}
                    </div>
                  )}

                  {/* Content */}
                  <div className="b25-card-content">{record.version.content}</div>

                  {/* Footer Action */}
                  <div className="b25-card-footer">
                    <span className="b25-card-meta">
                      录入时间: {formatDate(record.version.recorded_at)}
                    </span>

                    <button
                      type="button"
                      className="b25-btn b25-btn-secondary b25-btn-sm"
                      onClick={() => handlePrefillPropose(record)}
                      disabled={busy || readOnly}
                    >
                      <Plus size={12} aria-hidden="true" />
                      <span>对此记录提出修改</span>
                    </button>
                  </div>
                </article>
              ))}
            </div>
          )}
        </section>
      )}

      {/* 7. TAB 3: 原始历史搜索 (History) & 来源详情 */}
      {activeTab === 'history' && (
        <section aria-labelledby="b25-history-title" className="b25-section">
          <div className="b25-card-header">
            <div>
              <h2 id="b25-history-title" className="b25-card-title">
                原始执行历史查询
              </h2>
              <p className="b25-subtitle">
                查阅具有权限边界的不可变原始历史事实（historical_fact 视角）；展开来源只读取真实记录，不拼接虚构数据。
              </p>
            </div>
          </div>

          {/* Search bar */}
          <form
            className="b25-search-bar"
            onSubmit={(e) => {
              e.preventDefault();
              onSearch(false);
            }}
          >
            <div className="b25-search-input-wrapper">
              <Search size={16} className="b25-search-icon" aria-hidden="true" />
              <input
                id={queryInputId}
                className="b25-input b25-search-input"
                value={query}
                onChange={(e) => onQueryChange(e.target.value)}
                placeholder="搜索原始历史事实、Thread 内容、错误与产物摘要..."
              />
            </div>

            <button
              type="submit"
              className="b25-btn b25-btn-primary"
              disabled={busy || !query.trim()}
            >
              <Search size={14} aria-hidden="true" />
              <span>查询历史</span>
            </button>

            {history?.next_cursor && (
              <button
                type="button"
                className="b25-btn b25-btn-secondary"
                onClick={() => onSearch(true)}
                disabled={busy}
              >
                <span>加载更多</span>
              </button>
            )}
          </form>

          {/* Expanded Detail Box */}
          {detail && (
            <div className="b25-detail-box" role="region" aria-label="历史详情快照">
              <div className="b25-detail-header">
                <div>
                  <h3 className="b25-card-title">
                    历史记录详情：<code>{detail.entry.item_id}</code>
                  </h3>
                  <div className="b25-card-meta">
                    <span>视角: {detail.perspective}</span>
                    <span>Thread: <code>{detail.entry.thread_id}</code></span>
                    <span>Cursor: #{detail.entry.cursor}</span>
                    <span>时间: {formatDate(detail.entry.occurred_at)}</span>
                  </div>
                </div>

                <div className="b25-btn-group">
                  {detail.entry.source && (
                    <button
                      type="button"
                      className="b25-btn b25-btn-danger b25-btn-sm"
                      onClick={() => {
                        if (!state?.project_id || !detail.entry.source || readOnly) return;
                        onCommand({
                          action: 'source_revoke',
                          project_id: state.project_id,
                          source: detail.entry.source,
                        });
                      }}
                      disabled={busy || readOnly}
                      title="撤销该来源事实的信任凭据"
                    >
                      <Ban size={12} aria-hidden="true" />
                      <span>撤销此来源</span>
                    </button>
                  )}
                </div>
              </div>

              <pre className="b25-raw-text">{detail.text}</pre>

              {detail.entry.source && (
                <div className="b25-help-box">
                  <strong>关联来源凭据：</strong>
                  <code>{JSON.stringify(detail.entry.source)}</code>
                </div>
              )}
            </div>
          )}

          {/* History List */}
          {(!history?.items || history.items.length === 0) ? (
            <div className="b25-empty">
              <History size={32} className="b25-empty-icon" aria-hidden="true" />
              <h3 className="b25-empty-title">暂无历史查询结果</h3>
              <p className="b25-empty-desc">
                在上方输入关键词检索项目原始执行流与不可变事实记录。
              </p>
            </div>
          ) : (
            <div className="b25-grid b25-grid-single">
              {history.items.map((item) => (
                <article key={item.item_id} className="b25-card">
                  <div className="b25-card-header">
                    <div>
                      <h3 className="b25-card-title">
                        <span>条目: </span>
                        <code>{item.item_id}</code>
                      </h3>
                      <div className="b25-card-meta">
                        <span>Thread: <code>{item.thread_id}</code></span>
                        <span>Cursor: #{item.cursor}</span>
                        <span>类型: {item.kind}</span>
                        <span>时间: {formatDate(item.occurred_at)}</span>
                      </div>
                    </div>

                    <button
                      type="button"
                      className="b25-btn b25-btn-secondary b25-btn-sm"
                      onClick={() => onExpand(item.item_id)}
                      disabled={busy}
                    >
                      <Eye size={12} aria-hidden="true" />
                      <span>展开来源与详情</span>
                    </button>
                  </div>

                  <p className="b25-card-content">{item.excerpt}</p>
                </article>
              ))}
            </div>
          )}
        </section>
      )}

      {/* 8. TAB 4: 后台整理与调度 (Maintenance) */}
      {activeTab === 'maintenance' && (
        <section aria-labelledby="b25-maint-title" className="b25-section">
          <div className="b25-card-header">
            <div>
              <h2 id="b25-maint-title" className="b25-card-title">
                后台整理任务与水线状态
              </h2>
              <p className="b25-subtitle">
                使用受限维护 Executor 运行；整理任务在事务外执行模型调用，严格隔离预算与 DLQ 死信重试，提取结果仅生成候选提议。
              </p>
            </div>
          </div>

          <div className="b25-card b25-form">
            <p>后台整理：{state?.maintenance_enabled ? '已开启' : '已关闭'}。关闭后拒绝新整理并取消未完成作业。</p>
            <button type="button" className="b25-btn b25-btn-secondary" disabled={busy || readOnly || !state?.project_id}
              onClick={() => state && onCommand({ action: 'maintenance_configure', project_id: state.project_id, enabled: !state.maintenance_enabled })}>
              {state?.maintenance_enabled ? '关闭后台整理' : '开启后台整理'}
            </button>
          </div>
          {/* Trigger Maintenance Job Form */}
          <form className="b25-card b25-form" onSubmit={handleCreateMaintenance}>
            <div className="b25-card-header">
              <h3 className="b25-card-title">
                <Bot size={16} aria-hidden="true" />
                <span>发起受限后台整理任务</span>
              </h3>
              <span className="b25-badge b25-badge-info">受控 Executor 调度</span>
            </div>

            <div className="b25-form-row">
              <div className="b25-form-col">
                <label htmlFor={modelSelectId} className="b25-label">
                  <Cpu size={14} aria-hidden="true" />
                  <span>选择整理模型（ModelProfile）</span>
                </label>
                <select
                  id={modelSelectId}
                  className="b25-select"
                  value={activeModelProfileId}
                  onChange={(e) => setSelectedModelProfileId(e.target.value)}
                  disabled={busy || readOnly || modelProfiles.length === 0}
                >
                  {modelProfiles.length === 0 && <option value="">无可用模型 Profile</option>}
                  {modelProfiles.map((prof) => (
                    <option key={prof.id} value={prof.id}>
                      {prof.name} ({prof.model_id})
                    </option>
                  ))}
                </select>
              </div>

              <div className="b25-form-col">
                <label className="b25-label">最大处理来源数 (1~50)</label>
                <input
                  type="number"
                  min={1}
                  max={50}
                  className="b25-input"
                  value={maxSources}
                  onChange={(e) => setMaxSources(parseInt(e.target.value, 10) || 20)}
                  disabled={busy || readOnly}
                />
              </div>

              <div className="b25-form-col">
                <label className="b25-label">最大输出 Token 预算 (64~4096)</label>
                <input
                  type="number"
                  min={64}
                  max={4096}
                  className="b25-input"
                  value={maxOutputTokens}
                  onChange={(e) => setMaxOutputTokens(parseInt(e.target.value, 10) || 1024)}
                  disabled={busy || readOnly}
                />
              </div>
            </div>

            <div className="b25-help-box">
              <span>整理任务由现有调度器在后台有限执行，优先保证前台对话预算。提取失败不改写原任务结果。</span>
            </div>

            <div className="b25-card-footer">
              <button
                type="submit"
                className="b25-btn b25-btn-primary"
                disabled={busy || readOnly || !state?.maintenance_enabled || !activeModelProfileId}
              >
                <Plus size={14} aria-hidden="true" />
                <span>创建后台整理任务</span>
              </button>
            </div>
          </form>

          {/* Job View List */}
          {(!state?.jobs || state.jobs.length === 0) ? (
            <div className="b25-empty">
              <Bot size={32} className="b25-empty-icon" aria-hidden="true" />
              <h3 className="b25-empty-title">暂无后台任务</h3>
              <p className="b25-empty-desc">
                当前项目暂无活跃或历史整理任务。可在上方选择模型并发起整理。
              </p>
            </div>
          ) : (
            <div className="b25-grid">
              {state.jobs.map((job) => {
                const isDlq = job.state === 'dlq';
                const isFailed = job.state === 'failed';
                const isRunning = job.state === 'running';
                const isPending = job.state === 'pending';

                const progressPercent =
                  job.source_cursor > 0
                    ? Math.min(100, Math.round((job.processed_cursor / job.source_cursor) * 100))
                    : 0;

                const jobProposals = job.proposal_ids ?? [];

                return (
                  <article
                    key={job.job_id}
                    className={`b25-card ${isDlq || isFailed ? 'b25-card-conflict' : ''}`}
                  >
                    <div className="b25-card-header">
                      <div>
                        <h3 className="b25-card-title">
                          任务 <code>{job.job_id}</code>
                        </h3>
                        <div className="b25-card-meta">
                          <span>Workflow: <code>{job.workflow_id}:v{job.workflow_version}</code></span>
                          <span>模型: <code>{job.model_id}</code></span>
                        </div>
                      </div>

                      <span
                        className={`b25-badge ${
                          isRunning
                            ? 'b25-badge-safe'
                            : isDlq || isFailed
                            ? 'b25-badge-error'
                            : isPending
                            ? 'b25-badge-warn'
                            : 'b25-badge-neutral'
                        }`}
                      >
                        {isDlq ? '死信队列 (DLQ)' : job.state}
                      </span>
                    </div>

                    {/* Progress & Watermark */}
                    <div className="b25-job-progress">
                      <div className="b25-card-meta">
                        <span>
                          处理水线: {job.processed_cursor} / {job.source_cursor} ({progressPercent}%)
                        </span>
                        <span>重试尝试: {job.attempts} / {job.max_attempts}</span>
                      </div>
                      <div className="b25-progress-bar">
                        <div
                          className="b25-progress-fill"
                          style={{ width: `${progressPercent}%` }}
                        />
                      </div>
                    </div>

                    {/* Token metrics */}
                    <div className="b25-token-metrics">
                      <span>输入: {job.input_tokens} tokens</span>
                      <span>输出: {job.output_tokens} tokens</span>
                    </div>

                    {/* Error code display */}
                    {job.error_code && (
                      <div className="b25-alert b25-alert-error" role="alert">
                        <AlertTriangle size={13} aria-hidden="true" />
                        <span>错误代号：{job.error_code}</span>
                      </div>
                    )}

                    {/* Associated proposals */}
                    {jobProposals.length > 0 && (
                      <div className="b25-card-meta">
                        <span>产出候选 ({jobProposals.length})：</span>
                        {jobProposals.map((pid) => (
                          <span key={pid} className="b25-code-chip">
                            {pid}
                          </span>
                        ))}
                      </div>
                    )}

                    {/* Footer Actions */}
                    <div className="b25-card-footer">
                      <span className="b25-card-meta">更新: {formatDate(job.updated_at)}</span>

                      <div className="b25-btn-group">
                        {(isRunning || isPending) && (
                          <button
                            type="button"
                            className="b25-btn b25-btn-danger b25-btn-sm"
                            onClick={() => {
                              if (!state?.project_id || readOnly) return;
                              onCommand({
                                action: 'maintenance_cancel',
                                project_id: state.project_id,
                                job_id: job.job_id,
                              });
                            }}
                            disabled={busy || readOnly}
                          >
                            <Ban size={12} aria-hidden="true" />
                            <span>取消任务</span>
                          </button>
                        )}

                        {(isFailed || isDlq || job.state === 'cancelled') && (
                          <button
                            type="button"
                            className="b25-btn b25-btn-secondary b25-btn-sm"
                            onClick={() => {
                              if (!state?.project_id || readOnly) return;
                              onCommand({
                                action: 'maintenance_retry',
                                project_id: state.project_id,
                                job_id: job.job_id,
                              });
                            }}
                            disabled={busy || readOnly}
                          >
                            <RotateCcw size={12} aria-hidden="true" />
                            <span>重试任务</span>
                          </button>
                        )}
                      </div>
                    </div>
                  </article>
                );
              })}
            </div>
          )}
        </section>
      )}
    </div>
  );
};
