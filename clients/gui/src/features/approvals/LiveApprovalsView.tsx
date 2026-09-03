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
  const phaseLabel = phase === 'ready' ? 'Core 已连接' : phase === 'error' ? 'Core 连接失败' : '正在连接 Core';

  if (phase !== 'ready' && approvals.length === 0) {
    return (
      <div className="live-route-state" role={lastError ? 'alert' : 'status'}>
        <div className="live-route-state-icon"><ShieldCheck size={22} aria-hidden="true" /></div>
        <h1>{lastError ? 'Core Approval Projection 不可用' : '正在读取 Approval Projection…'}</h1>
        <p>{lastError?.message ?? 'Live 模式不会用演示审批卡填充此页面。'}</p>
        <div className="live-empty-actions"><button type="button" className="btn btn-primary" onClick={() => void reconnect()}><RefreshCw size={14} aria-hidden="true" />重连 Core</button><button type="button" className="btn btn-secondary" onClick={() => void refresh()}>重新查询</button></div>
      </div>
    );
  }

  return (
    <div className="live-route-view">
      <header className="live-route-header">
        <div className="live-route-heading">
          {showSidebarOpenBtn && <button type="button" className="btn btn-secondary btn-icon" onClick={openSidebar} aria-label="打开 Core 侧栏" title="打开 Core 侧栏"><PanelLeftOpen size={16} aria-hidden="true" /></button>}
          <div><span className="live-kicker"><span className="live-kicker-dot" aria-hidden="true" />实时 Core · Approval phase1e.v1 · Policy phase45.v1</span><h1>Approval Center</h1></div>
        </div>
        <div className="live-route-header-actions"><StatusBadge status={phase === 'ready' ? 'connected' : phase === 'error' ? 'disconnected' : 'pending'} label={phaseLabel} size="sm" /><button type="button" className="btn btn-ghost btn-icon" onClick={() => void refresh()} aria-label="刷新 Approval Projection" title="刷新 Approval Projection"><RefreshCw size={15} aria-hidden="true" /></button></div>
      </header>
      {lastError && <div className="live-alert live-alert-error" role="alert"><AlertTriangle size={16} aria-hidden="true" /><span>{lastError.code}：{lastError.message}</span></div>}
      <div className="live-approval-route-summary"><span><ShieldCheck size={16} aria-hidden="true" />待处理 Approval</span><strong>{pending.length}</strong><small>服务端 Projection 权威 · Policy {policyPhase === 'ready' ? '已连接' : '未就绪'} · <Link to="/settings?cat=security">检查 Policy</Link></small></div>
      {pending.length === 0 ? (
        <div className="live-route-empty"><ShieldCheck size={24} aria-hidden="true" /><h2>没有待处理 Approval</h2><p>当前 Core 没有返回待决定的审批事实。</p><button type="button" className="btn btn-secondary" onClick={() => navigate('/chat')}>返回 Live Thread</button></div>
      ) : (
        <div className="live-approval-route-list">
          {pending.map((approval) => (
            <LiveApprovalCard
              key={approval.id}
              approval={approval}
              busy={phase !== 'ready' || !canDecideApproval(
                approval,
                approvalAction,
                connectionStatus,
                stream.status,
                manualReconcileRequired,
              )}
              onDecide={(decision) => decide(approval, decision)}
            />
          ))}
        </div>
      )}
      {selectedSessionId && <p className="live-route-footnote">当前 Session：{selectedSessionId} · 决定提交后仍等待 Core Approval Projection 校正。</p>}
    </div>
  );
};
