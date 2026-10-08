import React, { useMemo } from 'react';
import { AlertTriangle, PanelLeftOpen, RefreshCw, ShieldCheck } from 'lucide-react';
import { Link, useNavigate, useOutletContext } from 'react-router-dom';
import { StatusBadge } from '../../components/StatusBadge';
import { useOperant } from '../../context/ClientContext';
import { useLive, type LiveApprovalDecision } from '../../live/LiveContext';
import { approvalsForSession, canDecideApproval } from '../../live/liveState';
import { LiveApprovalCard } from '../chat/LiveChatView';
import type { RailOutletContext } from '../../app/RailLayout';
import { usePhase45 } from '../../live45/Phase45Context';
import { LiveReviewerSettings } from './LiveReviewerSettings';
import { LiveAskAudit } from './LiveAskAudit';
import '../settings/live-config.css';

export const LiveApprovalsView: React.FC = () => {
  const { phase: policyPhase } = usePhase45();
  const navigate = useNavigate();
  const { showSidebarOpenBtn, openSidebar } = useOutletContext<RailOutletContext>();
  const { connectionStatus } = useOperant();
  const {
    phase,
    approvals,
    selectedThread,
    selectedSessionId,
    approvalAction,
    terminalSessionId,
    manualReconcileRequired,
    stream,
    lastError,
    refresh,
    reconnect,
    decideApproval,
  } = useLive();

  const pending = useMemo(
    () => approvalsForSession(approvals, selectedThread?.sessionId ?? null)
      .filter((approval) => approval.status === 'pending'),
    [approvals, selectedThread?.sessionId],
  );
  const decide = (approval: (typeof approvals)[number], decision: LiveApprovalDecision) => {
    void decideApproval(approval, decision);
  };
  const phaseLabel = phase === 'ready' ? '已连接' : phase === 'error' ? '连接失败' : '连接中';

  if (phase !== 'ready' && approvals.length === 0) {
    return (
      <div className="live-route-state" role={lastError ? 'alert' : 'status'}>
        <div className="live-route-state-icon"><ShieldCheck size={22} aria-hidden="true" /></div>
        <h1>{lastError ? '审批信息暂不可用' : '正在读取审批…'}</h1>
        <p>{lastError?.message ?? '连接后会显示待处理的审批。'}</p>
        <div className="live-empty-actions"><button type="button" className="btn btn-primary" onClick={() => void reconnect()}><RefreshCw size={14} aria-hidden="true" />重新连接</button><button type="button" className="btn btn-secondary" onClick={() => void refresh()}>刷新</button></div>
      </div>
    );
  }

  return (
    <div className="live-route-view">
      <header className="live-route-header">
        <div className="live-route-heading">
          {showSidebarOpenBtn && <button type="button" className="btn btn-secondary btn-icon" onClick={openSidebar} aria-label="打开侧栏" title="打开侧栏"><PanelLeftOpen size={16} aria-hidden="true" /></button>}
          <div><h1>审批中心</h1></div>
        </div>
        <div className="live-route-header-actions"><StatusBadge status={phase === 'ready' ? 'connected' : phase === 'error' ? 'disconnected' : 'pending'} label={phaseLabel} size="sm" /><button type="button" className="btn btn-ghost btn-icon" onClick={() => void refresh()} aria-label="刷新审批" title="刷新审批"><RefreshCw size={15} aria-hidden="true" /></button></div>
      </header>
      {lastError && <div className="live-alert live-alert-error" role="alert"><AlertTriangle size={16} aria-hidden="true" /><span>{lastError.code}：{lastError.message}</span></div>}
      <LiveReviewerSettings />
      <LiveAskAudit />
      <div className="live-approval-route-summary"><span><ShieldCheck size={16} aria-hidden="true" />待处理审批</span><strong>{pending.length}</strong><small>安全设置{policyPhase === 'ready' ? '已就绪' : '暂不可用'} · <Link to="/settings?cat=security">查看安全设置</Link></small></div>
      {pending.length === 0 ? (
        <div className="live-route-empty"><ShieldCheck size={24} aria-hidden="true" /><h2>没有待处理审批</h2><button type="button" className="btn btn-secondary" onClick={() => navigate('/chat')}>返回会话</button></div>
      ) : (
        <div className="live-approval-route-list">
          {pending.map((approval) => (
            <LiveApprovalCard
              key={approval.id}
              approval={approval}
              runEnded={terminalSessionId === approval.sessionId}
              busy={phase !== 'ready' || !canDecideApproval(
                approval,
                approvalAction,
                connectionStatus,
                stream.status,
                manualReconcileRequired,
                terminalSessionId,
              )}
              onDecide={(decision) => decide(approval, decision)}
            />
          ))}
        </div>
      )}
      {selectedSessionId && <p className="live-route-footnote">当前会话：{selectedSessionId} · 提交决定后，审批状态可能需要稍后刷新。</p>}
    </div>
  );
};
