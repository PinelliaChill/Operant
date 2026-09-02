/**
 * 工作流群聊界面（/workflow/:id/s/:sid 深链，v4 §3.4；v5 起返回目标为协作总览）。
 * 头部：← 返回协作总览、会话名 h1、参与者头像彩点连排（点击头像 → Agent 个人界面）、
 * 右侧"只看发给我的" switch（role=switch，绑 groupViewFilter/setGroupViewFilter）。
 * 消息流：仅 channel='group' 消息（MessageItem + GroupAudienceChip 寻址 chip）；
 * filter='to_me' 时仅显 audience 含 'user' 或 'all'；AgentLog 永不出现。
 * Composer：@ 选择器多选接收者（chips 在输入框上方可移除，默认 ['agent_planner']，
 * 至少保留 1 个），发送 sendGroupMessage（DemoContext 同步追加首接收者演示回复
 * "收到，已纳入计划跟踪（演示回复）。"，进共享 messages）；空内容禁用。
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { ArrowLeft, AtSign, Check, MessageSquare, SendHorizontal, X } from 'lucide-react';
import { EmptyState } from '../../components/EmptyState';
import { useOperant } from '../../context/ClientContext';
import { useDemo } from '../../demo/DemoContext';
import type { DemoAgent, DemoMessage } from '../../demo/types';
import { MessageItem } from '../chat/MessageList';
import { useOutsideClick } from '../chat/chatUtils';

/** 消息是否"发给我"（只看发给我的过滤口径：audience 含 'user' 或 'all'） */
const isToMe = (m: DemoMessage): boolean =>
  m.audience === 'all' || m.audience === 'user' || (Array.isArray(m.audience) && m.audience.includes('user'));

export const WorkflowSessionView: React.FC = () => {
  const { id = '', sid = '' } = useParams<{ id: string; sid: string }>();
  const navigate = useNavigate();
  const { addNotification } = useOperant();
  const {
    getConversation,
    getAgent,
    messages,
    sendGroupMessage,
    groupViewFilter,
    setGroupViewFilter,
  } = useDemo();

  const conversation = getConversation(sid);
  const valid = Boolean(conversation && conversation.workflowId === id);

  const [value, setValue] = useState('');
  /** 多选接收者（默认规划师；至少保留 1 个） */
  const [recipients, setRecipients] = useState<string[]>(['agent_planner']);
  const [mentionOpen, setMentionOpen] = useState(false);

  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const mentionWrapRef = useRef<HTMLDivElement>(null);

  const closeMention = useCallback(() => setMentionOpen(false), []);
  useOutsideClick(mentionWrapRef, mentionOpen, closeMention);

  // 切换会话时重置草稿与接收者
  useEffect(() => {
    setValue('');
    setRecipients(['agent_planner']);
    setMentionOpen(false);
  }, [sid]);

  // textarea 自动增高（上限 5 行，与会话 Composer 一致）
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 118)}px`;
  }, [value, sid]);

  const allMessages = messages[sid] ?? [];
  const groupMessages = allMessages.filter((m) => m.channel === 'group');
  const list = groupViewFilter === 'to_me' ? groupMessages.filter(isToMe) : groupMessages;

  // 新消息或过滤切换时滚动到流尾
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [sid, list.length, groupViewFilter]);

  const participants: DemoAgent[] = (conversation?.agentIds ?? [])
    .map((aid) => getAgent(aid))
    .filter((a): a is DemoAgent => Boolean(a));

  const toggleRecipient = (agentId: string) => {
    setRecipients((prev) => {
      if (prev.includes(agentId)) {
        // 至少保留 1 个接收者
        return prev.length <= 1 ? prev : prev.filter((x) => x !== agentId);
      }
      return [...prev, agentId];
    });
  };

  const doSend = () => {
    const text = value.trim();
    if (!text || recipients.length === 0 || !valid) return;
    // 演示回复由 DemoContext.sendGroupMessage 同步追加（共享 messages，跨视图/路由可见）
    sendGroupMessage(sid, text, [...recipients]);
    addNotification('success', '已发送（演示）');
    setValue('');
    setMentionOpen(false);
    textareaRef.current?.focus();
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (mentionOpen && e.key === 'Escape') {
      e.preventDefault();
      setMentionOpen(false);
      return;
    }
    // Enter 发送；IME 中文输入组合中的 Enter 不触发；Shift+Enter 换行
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      doSend();
    }
  };

  // 会话不存在或不属于该工作流：空态（页面级，标题即唯一 h1）
  if (!conversation || !valid) {
    return (
      <div className="chat-view chat-view-empty">
        <EmptyState
          icon={MessageSquare}
          titleAs="h1"
          title="未找到该群聊会话"
          description="它可能已被移除，请返回协作总览选择会话。"
          action={
            <button className="btn btn-secondary" onClick={() => navigate('/collab')}>
              <ArrowLeft size={14} aria-hidden="true" />
              返回协作总览
            </button>
          }
        />
      </div>
    );
  }

  const isDefaultRecipients = recipients.length === 1 && recipients[0] === 'agent_planner';
  const placeholder = isDefaultRecipients
    ? '发送给 规划师（默认）'
    : `发送给 ${recipients.map((rid) => getAgent(rid)?.name ?? rid).join('、')}`;
  const canSend = value.trim().length > 0 && recipients.length > 0;

  return (
    <div className="chat-view">
      <header className="chat-header">
        <button
          className="btn btn-secondary btn-icon"
          onClick={() => navigate('/collab')}
          aria-label="返回协作总览"
          title="返回协作总览"
        >
          <ArrowLeft size={16} aria-hidden="true" />
        </button>
        <div className="chat-header-main">
          <div className="chat-header-title-row">
            <h1 className="chat-header-title">{conversation.title}</h1>
            {/* 参与者头像彩点连排：点击头像进 Agent 个人界面 */}
            <div className="wf-agent-avatars">
              {participants.map((a) => (
                <button
                  key={a.id}
                  type="button"
                  className="wf-agent-avatar"
                  style={{ backgroundColor: a.color }}
                  onClick={() => navigate(`/workflow/${id}/s/${sid}/agent/${a.id}`)}
                  aria-label={`查看 ${a.name} 的个人界面`}
                  title={a.name}
                >
                  {a.name.charAt(0)}
                </button>
              ))}
            </div>
          </div>
          <div className="chat-header-sub">
            <span className="chat-header-sub-text">
              {participants.length} 位参与者的群聊会话
            </span>
          </div>
        </div>

        <div className="chat-header-actions">
          <span className="wf-filter-label">只看发给我的</span>
          <button
            type="button"
            role="switch"
            aria-checked={groupViewFilter === 'to_me'}
            className="switch"
            onClick={() => setGroupViewFilter(groupViewFilter === 'to_me' ? 'all' : 'to_me')}
            aria-label="只看发给我的"
          />
        </div>
      </header>

      <div className="chat-body">
        <div className="chat-scroll" ref={scrollRef} tabIndex={0} aria-label="群聊消息流">
          <div className="chat-messages" aria-live="polite">
            {list.length > 0 ? (
              list.map((m) => <MessageItem key={m.id} message={m} conversationId={sid} />)
            ) : (
              <EmptyState
                icon={MessageSquare}
                title={groupViewFilter === 'to_me' ? '暂无发给我的消息' : '暂无消息'}
                description="在下方输入框发言，默认发送给规划师。"
              />
            )}
          </div>
        </div>
      </div>

      <div className="chat-composer">
        {/* 多选接收者 chips：输入框上方，可移除（至少保留 1 个） */}
        <div className="wf-recipient-row" aria-label="接收者">
          {recipients.map((rid) => {
            const a = getAgent(rid);
            return (
              <span key={rid} className="wf-recipient-chip">
                <span
                  className="rail-sidebar-dot"
                  style={{ backgroundColor: a?.color }}
                  aria-hidden="true"
                />
                {a?.name ?? rid}
                <button
                  type="button"
                  onClick={() => toggleRecipient(rid)}
                  disabled={recipients.length <= 1}
                  aria-label={`移除接收者 ${a?.name ?? rid}`}
                  title={recipients.length <= 1 ? '至少保留 1 个接收者' : `移除 ${a?.name ?? rid}`}
                >
                  <X size={11} aria-hidden="true" />
                </button>
              </span>
            );
          })}
        </div>

        <div className="chat-composer-inner">
          <div className="chat-composer-tools">
            {/* @ 选择器：多选接收者（aria-pressed 标记选中），外点 / Esc 关闭 */}
            <div className="chat-menu-wrap" ref={mentionWrapRef}>
              <button
                className="chat-composer-btn"
                onClick={() => {
                  setMentionOpen((v) => !v);
                  textareaRef.current?.focus();
                }}
                aria-label="选择接收者"
                title="选择接收者（可多选）"
                aria-haspopup="listbox"
                aria-expanded={mentionOpen}
                aria-controls="wf-recipient-listbox"
              >
                <AtSign size={15} aria-hidden="true" />
              </button>
              {mentionOpen && (
                <div
                  className="chat-popover chat-mention-pop"
                  role="listbox"
                  id="wf-recipient-listbox"
                  aria-label="选择接收者（可多选）"
                  aria-multiselectable="true"
                >
                  {participants.map((a) => {
                    const selected = recipients.includes(a.id);
                    return (
                      <button
                        key={a.id}
                        role="option"
                        aria-selected={selected}
                        aria-pressed={selected}
                        className={`chat-mention-item${selected ? ' active' : ''}`}
                        onMouseDown={(e) => e.preventDefault()}
                        onClick={() => toggleRecipient(a.id)}
                      >
                        <span
                          className="rail-sidebar-dot"
                          style={{ backgroundColor: a.color }}
                          aria-hidden="true"
                        />
                        <span className="chat-mention-name">{a.name}</span>
                        <span className="chat-mention-model">{a.model}</span>
                        {selected && (
                          <Check size={13} className="chat-agent-check" aria-hidden="true" />
                        )}
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          </div>

          <textarea
            ref={textareaRef}
            className="chat-composer-input"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={handleKeyDown}
            rows={1}
            placeholder={placeholder}
            aria-label="输入群聊消息"
          />

          <button
            className="btn btn-primary chat-send-btn"
            onClick={doSend}
            disabled={!canSend}
            aria-label="发送消息"
          >
            <SendHorizontal size={14} aria-hidden="true" />
            发送
          </button>
        </div>
      </div>
    </div>
  );
};
