/**
 * 审批与权限设置（v6 §审批拆分，设置中心「审批与权限」tab）
 * 自 /approvals 旧页原样迁移，交互与无障碍行为不变：
 * - 权限策略：4 张模式单选卡（grid，radio 语义 role=radio/aria-checked，当前模式 --accent 边框高亮），
 *   roving tabindex 键盘导航（方向键/Home/End 在单选组内循环移焦）；
 * - 分类规则：文件写入 / 命令执行 / 网络访问 三行 × 原生 select（跟随模式/每次询问/自动允许/拒绝）。
 * 选中态持久在 DemoContext approvalPolicy；策略切换即时生效（auto-approve/full-open 自动批准
 * 全部待处理卡，workspace-write 仅自动批准 file_write 卡，切回"询问模式"不影响已处理卡）。
 */

import React, { useRef, useState } from 'react';
import {
  CheckCheck,
  FolderPen,
  MessageCircleQuestion,
  Unlock,
  RefreshCw,
  ShieldAlert,
  History,
} from 'lucide-react';
import type { Phase45 } from '@operant/sdk';
import { useOperant } from '../../context/ClientContext';
import { usePhase45 } from '../../live45/Phase45Context';
import { StatusBadge } from '../../components/StatusBadge';
import { policyDecisionStatus } from '../../live45/phase45State';
import { formatDateTime } from '../../lib/format';
import type { LucideIcon } from 'lucide-react';
import {
  APPROVAL_ACTION_LABELS,
  APPROVAL_MODE_LABELS,
  APPROVAL_RULE_LABELS,
  useDemo,
} from '../../demo/DemoContext';
import type {
  ApprovalActionType,
  ApprovalPolicyMode,
  ApprovalRuleDecision,
} from '../../demo/types';

/** 模式单选卡定义（图标 + 名称 + 一句描述） */
const MODE_CARDS: Array<{
  mode: ApprovalPolicyMode;
  icon: LucideIcon;
  desc: string;
}> = [
  { mode: 'ask', icon: MessageCircleQuestion, desc: '每次敏感操作前都询问' },
  { mode: 'workspace-write', icon: FolderPen, desc: '自动允许工作区内文件写入，其余询问' },
  { mode: 'full-open', icon: Unlock, desc: '所有操作直接放行，不出审批卡' },
  { mode: 'auto-approve', icon: CheckCheck, desc: '待处理与新审批自动批准并留痕' },
];

/** 分类规则行定义（名称取自 APPROVAL_ACTION_LABELS） */
const RULE_ROWS: Array<{ action: ApprovalActionType; desc: string }> = [
  { action: 'file_write', desc: '写入或修改工作区内的文件' },
  { action: 'command', desc: '在 Runner 中执行命令' },
  { action: 'network', desc: '访问外部网络资源' },
];

const RULE_OPTIONS: ApprovalRuleDecision[] = ['follow', 'ask', 'auto', 'deny'];

/** 设置面板小节标题（与设置中心其余 tab 的内联排版一致） */
const subsectionTitleStyle: React.CSSProperties = {
  fontSize: '14px',
  fontWeight: 600,
  color: 'var(--text-primary)',
  margin: 0,
};

const subsectionSubStyle: React.CSSProperties = {
  fontSize: '12px',
  color: 'var(--text-muted)',
  margin: 0,
  lineHeight: 1.5,
};

export const PolicySettings: React.FC = () => {
  const { clientMode } = useOperant();
  return clientMode === 'live' ? <LivePolicySettings /> : <DemoPolicySettings />;
};

const CAPABILITIES: Phase45.Capability[] = [
  'workspace.read', 'workspace.write', 'workspace.delete', 'process.exec',
  'process.exec.no_network', 'network.egress', 'secret.use', 'git.commit',
  'git.push', 'external.message.send', 'production.mutate', 'policy.modify',
];

const LivePolicySettings: React.FC = () => {
  const { connectionStatus } = useOperant();
  const {
    explainPolicy,
    loadSecurityAudit,
    auditsByAction,
    auditLoadingActionHash,
    auditError,
    actionLabel,
    error,
  } = usePhase45();
  const [principal, setPrincipal] = useState('gui:user');
  const [tool, setTool] = useState('apply_patch');
  const [operation, setOperation] = useState('execute');
  const [workspace, setWorkspace] = useState('');
  const [capability, setCapability] = useState<Phase45.Capability>('workspace.write');
  const [result, setResult] = useState<Awaited<ReturnType<typeof explainPolicy>>>();

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const next = await explainPolicy({
      principal: principal.trim(), tool: tool.trim(), operation: operation.trim(),
      requested_capabilities: [capability], idempotency_key: crypto.randomUUID(),
      arguments: {}, workspace: workspace.trim() || undefined, dry_run: true,
    });
    setResult(next);
    if (next) await loadSecurityAudit(next.actionHash);
  };

  const audit = result ? auditsByAction[result.actionHash] ?? [] : [];
  const auditLoading = result?.actionHash === auditLoadingActionHash;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div>
        <h2 style={subsectionTitleStyle}>Policy 检查与解释</h2>
        <p style={subsectionSubStyle}>通过 Phase 4 Action Gateway 对一个明确动作做 dry-run。此处不修改 Policy，也不能覆盖 DENY。</p>
      </div>
      <form className="card approval-rules" onSubmit={submit} aria-describedby="live-policy-help">
        <p id="live-policy-help" className="section-footnote">每次检查使用新的幂等键，并由 Core 返回权威 Policy 结果。</p>
        <label>主体<input className="input" value={principal} onChange={(event) => setPrincipal(event.target.value)} required /></label>
        <label>工具<input className="input" value={tool} onChange={(event) => setTool(event.target.value)} required /></label>
        <label>操作<input className="input" value={operation} onChange={(event) => setOperation(event.target.value)} required /></label>
        <label>能力<select className="select" value={capability} onChange={(event) => setCapability(event.target.value as Phase45.Capability)}>{CAPABILITIES.map((item) => <option key={item}>{item}</option>)}</select></label>
        <label>Workspace（可选）<input className="input" value={workspace} onChange={(event) => setWorkspace(event.target.value)} placeholder="绝对路径" /></label>
        <button type="submit" className="btn btn-primary" disabled={Boolean(actionLabel) || connectionStatus !== 'connected'}><RefreshCw size={14} aria-hidden="true" />{actionLabel === 'Policy 检查' ? '检查中…' : '检查并解释'}</button>
      </form>
      <div aria-live="polite" aria-atomic="true">
        {connectionStatus !== 'connected' && <div className="live-alert live-alert-error" role="alert"><ShieldAlert size={16} aria-hidden="true" />Core 连接已断开；Policy 检查已禁用。</div>}
        {error && <div className="live-alert live-alert-error" role="alert"><ShieldAlert size={16} aria-hidden="true" />{error.code}：{error.message}</div>}
        {result && <div className="card" style={{ padding: 16 }}>
          <StatusBadge status={policyDecisionStatus(result.decision)} label={result.decision.toUpperCase()} />
          <p>原因：<code>{result.reasonCode}</code> · 风险：{result.riskLevel}{result.hardDeny ? ' · 硬拒绝' : ''}</p>
          {result.matchedRuleIds.length > 0 && <p>命中规则：{result.matchedRuleIds.join('、')}</p>}
          {result.remediation !== undefined && <details><summary>合规替代建议</summary><pre>{JSON.stringify(result.remediation, null, 2)}</pre></details>}
        </div>}
      </div>
      {result && <section aria-labelledby="security-audit-title" style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
          <div><h2 id="security-audit-title" style={subsectionTitleStyle}>安全审计事实</h2><p style={subsectionSubStyle}>仅展示最新加载的 100 条安全字段；原始参数、detail 与 Secret 不进入客户端状态。</p></div>
          <button type="button" className="btn btn-secondary btn-sm" disabled={auditLoading || connectionStatus !== 'connected'} onClick={() => void loadSecurityAudit(result.actionHash)}><RefreshCw size={13} aria-hidden="true" />{auditLoading ? '刷新中…' : '刷新审计'}</button>
        </div>
        <div aria-live="polite" aria-busy={auditLoading}>
          {auditError && <div className="live-alert live-alert-error" role="alert"><ShieldAlert size={16} aria-hidden="true" />{auditError.code}：{auditError.message}</div>}
          {!auditLoading && !auditError && audit.length === 0 && <div className="card" style={{ padding: 16 }}><History size={18} aria-hidden="true" /><p>此 Action Hash 暂无安全审计事实。</p></div>}
          {audit.length > 0 && <ol className="chat-row-list" aria-label="安全审计事实列表">
            {audit.map((fact) => <li key={fact.eventId} className="chat-row chat-row-wrap">
              <span className="chat-row-main"><span className="chat-row-title">#{fact.cursor.toString()} · {fact.eventType}</span><span className="chat-row-sub">{formatDateTime(fact.createdAt)} · 主体 {fact.principal}</span>{fact.ruleIds.length > 0 && <span className="chat-row-sub">规则：{fact.ruleIds.join('、')}</span>}</span>
              <StatusBadge status={fact.decision ? policyDecisionStatus(fact.decision) : 'pending'} label={fact.decision?.toUpperCase() ?? 'FACT'} size="sm" />
            </li>)}
          </ol>}
        </div>
      </section>}
    </div>
  );
};

const DemoPolicySettings: React.FC = () => {
  const { approvalPolicy, setApprovalMode, setApprovalRule } = useDemo();

  /** 模式卡 roving tabindex 焦点引用（方向键在单选组内循环移动） */
  const modeCardRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const activeModeIndex = MODE_CARDS.findIndex((c) => c.mode === approvalPolicy.mode);

  const handleModeKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const keys = ['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp', 'Home', 'End'];
    if (!keys.includes(e.key)) return;
    e.preventDefault();
    const count = MODE_CARDS.length;
    let next = activeModeIndex;
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') next = (activeModeIndex + 1) % count;
    if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') next = (activeModeIndex - 1 + count) % count;
    if (e.key === 'Home') next = 0;
    if (e.key === 'End') next = count - 1;
    setApprovalMode(MODE_CARDS[next].mode);
    modeCardRefs.current[next]?.focus();
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
      {/* 小节一：权限策略 */}
      <section
        aria-labelledby="policy-mode-title"
        style={{ display: 'flex', flexDirection: 'column', gap: 12 }}
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          <h2 id="policy-mode-title" style={subsectionTitleStyle}>
            权限策略
          </h2>
          <p style={subsectionSubStyle}>选择敏感操作的默认审批模式，切换即时生效。</p>
        </div>

        <div
          className="approval-mode-grid"
          role="radiogroup"
          aria-label="权限策略模式"
          onKeyDown={handleModeKeyDown}
        >
          {MODE_CARDS.map((card, index) => {
            const Icon = card.icon;
            const checked = approvalPolicy.mode === card.mode;
            return (
              <button
                key={card.mode}
                ref={(el) => {
                  modeCardRefs.current[index] = el;
                }}
                type="button"
                role="radio"
                aria-checked={checked}
                tabIndex={checked ? 0 : -1}
                className={`approval-mode-card${checked ? ' active' : ''}`}
                onClick={() => setApprovalMode(card.mode)}
              >
                <span className="approval-mode-card-head">
                  <Icon size={16} aria-hidden="true" />
                  <span>{APPROVAL_MODE_LABELS[card.mode]}</span>
                </span>
                <span className="approval-mode-card-desc">{card.desc}</span>
              </button>
            );
          })}
        </div>
      </section>

      {/* 小节二：分类规则 */}
      <section
        aria-labelledby="policy-rules-title"
        style={{ display: 'flex', flexDirection: 'column', gap: 12 }}
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          <h2 id="policy-rules-title" style={subsectionTitleStyle}>
            分类规则
          </h2>
          <p style={subsectionSubStyle}>各操作分类可覆盖模式默认行为。</p>
        </div>

        <div className="card approval-rules">
          {RULE_ROWS.map((row) => (
            <div key={row.action} className="approval-rule-row">
              <div className="approval-rule-info">
                <span className="approval-rule-name">{APPROVAL_ACTION_LABELS[row.action]}</span>
                <span className="approval-rule-desc">{row.desc}</span>
              </div>
              <select
                className="select approval-rule-select"
                value={approvalPolicy.rules[row.action]}
                aria-label={`${APPROVAL_ACTION_LABELS[row.action]}规则`}
                onChange={(e) =>
                  setApprovalRule(row.action, e.target.value as ApprovalRuleDecision)
                }
              >
                {RULE_OPTIONS.map((opt) => (
                  <option key={opt} value={opt}>
                    {APPROVAL_RULE_LABELS[opt]}
                  </option>
                ))}
              </select>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
};
