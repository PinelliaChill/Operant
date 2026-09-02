/**
 * 审批收件箱（v6 §审批拆分，rail 一级分区 /approvals）
 * 权限策略与分类规则配置已迁入设置中心「审批与权限」tab（PolicySettings），
 * 本页只保留审批事务：
 * - 待处理审批：富卡片（标题 + 描述 + 动作分类徽章 + 来源会话跳转 + 批准/拒绝）；
 * - 近期决定：紧凑行（标题 + 决定徽章 + 决定时间 + 来源会话跳转 + 备注）。
 * 来源跳转：按 conversationId 查会话绑定的 workflowId/tempWorkflowId——
 * 工作流实例跳 /workflow/{workflowId}/s/{conversationId}，其余跳 /chat/{conversationId}。
 * 批准/拒绝经 DemoContext approveCard/rejectCard 回写 MockClient，处理后即时移入近期决定。
 */

import React from 'react';
import { Link } from 'react-router-dom';
import { History, MessageCircle, ShieldCheck } from 'lucide-react';
import { EmptyState } from '../../components/EmptyState';
import { StatusBadge } from '../../components/StatusBadge';
import { APPROVAL_ACTION_LABELS, useDemo } from '../../demo/DemoContext';
import { formatDateTime } from '../../lib/format';
import { useOperant } from '../../context/ClientContext';
import { LiveApprovalsView } from './LiveApprovalsView';

/** 来源会话内联链接样式（12px 强调色，无下划线，全局 :focus-visible 提供焦点环） */
const sourceLinkStyle: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: 4,
  fontSize: '12px',
  color: 'var(--accent-action)',
  textDecoration: 'none',
};

/** 来源会话链接：工作流实例 → /workflow/{workflowId}/s/{id}，direct 单聊 → /chat/{id} */
const SourceConversationLink: React.FC<{ conversationId: string; title: string }> = ({
  conversationId,
  title,
}) => {
  const { conversations } = useDemo();
  const conversation = conversations.find((c) => c.id === conversationId);
  const to = conversation?.workflowId
    ? `/workflow/${conversation.workflowId}/s/${conversationId}`
    : `/chat/${conversationId}`;
  return (
    <Link to={to} style={sourceLinkStyle} aria-label={`前往会话 ${title}`}>
      <MessageCircle size={12} aria-hidden="true" />
      <span>{title}</span>
    </Link>
  );
};

export const ApprovalCenterView: React.FC = () => {
  const { clientMode } = useOperant();
  if (clientMode === 'live') return <LiveApprovalsView />;
  return <DemoApprovalCenterView />;
};

const DemoApprovalCenterView: React.FC = () => {
  const { pendingApprovals, approvalHistory, approveCard, rejectCard, getAgent } = useDemo();

  const history = approvalHistory();

  return (
    <div className="section-view">
      <header className="section-header">
        <h1 className="section-title">审批</h1>
        <p className="section-sub">待处理请求、近期决定与来源跳转。</p>
      </header>

      <div className="section-scroll">
        <div className="section-inner">
          {/* 区一：待处理审批 */}
          <section className="section-group" aria-labelledby="approval-inbox-pending-title">
            <h2 id="approval-inbox-pending-title" className="section-group-title">
              待处理审批
              <span className="section-group-count">{pendingApprovals.length} 项</span>
            </h2>

            {pendingApprovals.length === 0 ? (
              <div className="card approval-pending-empty">
                <EmptyState
                  icon={ShieldCheck}
                  title="没有待处理的审批"
                  description="所有授权请求均已处理，新的敏感操作会按「设置 → 审批与权限」中的策略执行。"
                />
              </div>
            ) : (
              <div className="approval-pending-list">
                {pendingApprovals.map(({ conversationId, conversationTitle, message }) => {
                  const p = message.payload;
                  const agent = getAgent(message.authorId);
                  return (
                    <div key={message.id} className="card approval-pending-card">
                      <div className="approval-pending-head">
                        <span className="approval-pending-title">{p.title}</span>
                        {p.actionType && (
                          <span className="badge" style={{ backgroundColor: 'var(--bg-subtle)' }}>
                            {APPROVAL_ACTION_LABELS[p.actionType]}
                          </span>
                        )}
                      </div>
                      <div className="approval-pending-detail">{p.detail}</div>
                      <div
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'space-between',
                          gap: 8,
                          flexWrap: 'wrap',
                        }}
                      >
                        <span
                          style={{
                            display: 'inline-flex',
                            alignItems: 'center',
                            gap: 6,
                            fontSize: '12px',
                            color: 'var(--text-muted)',
                            minWidth: 0,
                            flexWrap: 'wrap',
                          }}
                        >
                          <span>来源</span>
                          <SourceConversationLink
                            conversationId={conversationId}
                            title={conversationTitle}
                          />
                          {agent && <span>· {agent.name}</span>}
                        </span>
                        <div className="approval-pending-actions">
                          <button
                            className="btn btn-primary btn-sm"
                            onClick={() => approveCard(conversationId, message.id)}
                          >
                            批准
                          </button>
                          <button
                            className="btn btn-secondary btn-sm"
                            onClick={() => rejectCard(conversationId, message.id)}
                          >
                            拒绝
                          </button>
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </section>

          {/* 区二：近期决定 */}
          <section className="section-group" aria-labelledby="approval-inbox-history-title">
            <h2 id="approval-inbox-history-title" className="section-group-title">
              近期决定
              <span className="section-group-count">{history.length} 条</span>
            </h2>

            {history.length === 0 ? (
              <div className="card approval-pending-empty">
                <EmptyState
                  icon={History}
                  title="暂无审批决定记录"
                  description="批准或拒绝待处理审批后会在这里留痕。"
                />
              </div>
            ) : (
              <ul className="chat-row-list">
                {history.map((item, index) => {
                  const approved = item.decision === 'approved';
                  return (
                    <li
                      key={`${item.conversationId}-${item.decidedAt}-${index}`}
                      className="chat-row chat-row-wrap"
                    >
                      <span className="chat-row-main">
                        <span className="chat-row-title">{item.title}</span>
                        <span className="chat-row-sub">
                          <span>来源</span>
                          <SourceConversationLink
                            conversationId={item.conversationId}
                            title={item.conversationTitle}
                          />
                          <span>· {item.agentName}</span>
                        </span>
                        {item.note && <span className="chat-row-sub">{item.note}</span>}
                      </span>
                      <StatusBadge
                        status={approved ? 'safe' : 'denied'}
                        label={approved ? '已批准' : '已拒绝'}
                        size="sm"
                      />
                      <span className="chat-row-meta">{formatDateTime(item.decidedAt)}</span>
                    </li>
                  );
                })}
              </ul>
            )}
          </section>
        </div>
      </div>
    </div>
  );
};
