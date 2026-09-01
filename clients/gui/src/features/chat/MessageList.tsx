/**
 * 对话 Tab：消息流（aria-live=polite）+ 富消息卡（设计基线 §4）。
 * 用户消息右侧 accent-subtle 气泡；Agent 消息左侧彩色方块头像（首字 + Agent color）+
 * 名称 + 时间。卡片统一左侧 3px 类型色条：计划 accent / 进度 info / 审批 warning / 文件中性。
 * 新消息到达或切换会话时自动滚动到底部。
 *
 * MessageItem 为纯渲染子组件（消息气泡 + 四种富消息卡 + 可操作审批卡），
 * direct 会话与工作流群聊（channel='group'）复用同一路渲染，审批动作经 conversationId 路由。
 * v4 §2：群聊消息右上角渲染 GroupAudienceChip 寻址 chip（'all'→发给全员；含 'user'→发给我；
 * 数组→@名称 / @名称 等 n 人）。该 chip 组件导出供 §3 群聊界面复用。
 */

import React, { useEffect, useRef } from 'react';
import { FileText, MessageSquare } from 'lucide-react';
import { EmptyState } from '../../components/EmptyState';
import { useDemo } from '../../demo/DemoContext';
import type { DemoConversation, DemoMessage } from '../../demo/types';
import { formatChatTimestamp } from './chatUtils';

/** 群聊寻址 chip（v4 §2 会话模式与 §3.4 群聊界面共用渲染规则） */
export const GroupAudienceChip: React.FC<{
  audience: NonNullable<DemoMessage['audience']>;
}> = ({ audience }) => {
  const { getAgent } = useDemo();
  let label: string;
  if (audience === 'all') {
    label = '发给全员';
  } else if (audience === 'user' || (Array.isArray(audience) && audience.includes('user'))) {
    label = '发给我';
  } else {
    const ids = Array.isArray(audience) ? audience : [audience];
    const firstName = getAgent(ids[0])?.name ?? ids[0] ?? 'Agent';
    label = ids.length === 1 ? `@ ${firstName}` : `@ ${firstName} 等 ${ids.length} 人`;
  }
  return <span className="chat-audience-chip">{label}</span>;
};

/** 仅群聊消息（channel='group'）渲染寻址 chip；user 消息缺省 audience 视为 ['agent_planner'] */
const audienceChipOf = (m: DemoMessage): React.ReactNode => {
  if (m.channel !== 'group') return null;
  const audience = m.audience ?? (m.authorId === 'user' ? ['agent_planner'] : undefined);
  return audience ? <GroupAudienceChip audience={audience} /> : null;
};

interface MessageItemProps {
  message: DemoMessage;
  /** 审批卡动作路由 id（会话 id；群聊会话同样走 messages 索引） */
  conversationId: string;
}

/** 单条消息渲染（用户气泡 / Agent 头像 + 富消息卡）；审批卡按钮经 DemoContext 动作路由 */
export const MessageItem: React.FC<MessageItemProps> = ({ message: m, conversationId }) => {
  const { getAgent, approveCard, rejectCard } = useDemo();

  const renderPayload = () => {
    const p = m.payload;
    switch (p.kind) {
      case 'text':
        return <div className="chat-bubble chat-bubble-agent">{p.text}</div>;
      case 'plan':
        return (
          <div className="chat-card chat-card-plan">
            <div className="chat-card-title">{p.title}</div>
            <div className="chat-card-summary">{p.summary}</div>
            <ul className="chat-card-list">
              {p.items.map((item, i) => (
                <li key={i}>
                  <span className="chat-card-list-icon" aria-hidden="true">
                    {item.done ? '✅' : '▸'}
                  </span>
                  <span>{item.text}</span>
                </li>
              ))}
            </ul>
          </div>
        );
      case 'progress':
        return (
          <div className="chat-card chat-card-progress">
            <div className="chat-card-title">{p.title}</div>
            <div className="chat-progress-row">
              <div
                className="chat-progress-track"
                role="progressbar"
                aria-valuenow={p.percent}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-label={p.title}
              >
                <div className="chat-progress-fill" style={{ width: `${p.percent}%` }} />
              </div>
              <span className="chat-progress-percent">{p.percent}%</span>
            </div>
            <div className="chat-card-note">{p.note}</div>
          </div>
        );
      case 'approval':
        return (
          <div className="chat-card chat-card-approval">
            <div className="chat-card-title">{p.title}</div>
            <div className="chat-card-detail">{p.detail}</div>
            {p.status === 'pending' ? (
              <div className="chat-card-actions">
                <button
                  className="btn btn-primary"
                  onClick={() => approveCard(conversationId, m.id)}
                >
                  批准
                </button>
                <button
                  className="btn btn-secondary"
                  onClick={() => rejectCard(conversationId, m.id)}
                >
                  拒绝
                </button>
              </div>
            ) : (
              <>
                <div className={`chat-card-status ${p.status}`} aria-live="polite">
                  <span>{p.status === 'approved' ? '已批准 ✓' : '已拒绝'}</span>
                  {p.note?.includes('自动批准') && (
                    <span className="badge" style={{ backgroundColor: 'var(--bg-subtle)' }}>
                      自动
                    </span>
                  )}
                </div>
                {p.note && <div className="chat-card-note">{p.note}</div>}
              </>
            )}
          </div>
        );
      case 'file':
        return (
          <div className="chat-card chat-card-file">
            <span className="chat-icon-box" aria-hidden="true">
              <FileText size={16} />
            </span>
            <span className="chat-file-main">
              <span className="chat-file-name">{p.name}</span>
              <span className="chat-file-path">{p.path}</span>
            </span>
          </div>
        );
    }
  };

  // 用户消息：右侧气泡（演示数据用户消息均为文本负载）；群聊时气泡右上角寻址 chip
  if (m.authorId === 'user') {
    return (
      <div className="chat-msg chat-msg-user">
        <div className="chat-msg-user-stack">
          {audienceChipOf(m)}
          <div className="chat-bubble chat-bubble-user">
            {m.payload.kind === 'text' ? m.payload.text : ''}
          </div>
        </div>
      </div>
    );
  }

  const agent = getAgent(m.authorId);
  return (
    <div className="chat-msg">
      <span
        className="chat-avatar"
        style={{ backgroundColor: agent?.color ?? 'var(--text-muted)' }}
        aria-hidden="true"
      >
        {agent?.name.charAt(0) ?? 'A'}
      </span>
      <div className="chat-msg-main">
        <div className="chat-msg-meta">
          <span className="chat-msg-name">{agent?.name ?? 'Agent'}</span>
          <span className="chat-msg-time">{formatChatTimestamp(m.createdAt)}</span>
          {audienceChipOf(m)}
        </div>
        {renderPayload()}
      </div>
    </div>
  );
};

interface MessageListProps {
  conversation: DemoConversation;
}

export const MessageList: React.FC<MessageListProps> = ({ conversation }) => {
  const { messages } = useDemo();
  const list = messages[conversation.id] ?? [];
  const scrollRef = useRef<HTMLDivElement>(null);

  // 新消息追加或切换会话时滚动到流尾
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [conversation.id, list.length]);

  return (
    <div
      className="chat-scroll"
      role="tabpanel"
      id="chat-panel-chat"
      aria-labelledby="chat-tab-chat"
      tabIndex={0}
      ref={scrollRef}
    >
      <div className="chat-messages" aria-live="polite" aria-label="消息流">
        {list.length > 0 ? (
          list.map((m) => <MessageItem key={m.id} message={m} conversationId={conversation.id} />)
        ) : (
          <EmptyState
            icon={MessageSquare}
            title="暂无消息"
            description="发送第一条消息，开始与 Agent 协作。"
          />
        )}
      </div>
    </div>
  );
};
