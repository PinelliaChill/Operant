/**
 * Agent 个人界面（/workflow/:id/s/:sid/agent/:aid，v4 §3.5）。
 * 头部：← 返回群聊、彩色方块头像 + 名称 h1、model + 状态徽章；
 * 副文案"该 Agent 在此会话中的过程明细"。
 * 条目流：agentLogsOf 渲染（thought=灰卡 Brain 图标 / tool_call=等宽命令卡 Terminal 图标 /
 * intermediate=气泡 MessageSquare 图标；detail 用 chevron 折叠展开 aria-expanded），
 * 按时间穿插该 Agent 收到的 DM（accent 边框高亮）。
 * Composer：placeholder"单独发给 {名称}"，发送 sendAgentDM（DemoContext 负责 toast）；
 * 该 DM 同步出现在群聊流（audience=[aid]，GroupAudienceChip 单人渲染为"@ {名称}"）。
 */

import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  ArrowLeft,
  Bot,
  Brain,
  ChevronDown,
  ChevronRight,
  MessageSquare,
  SendHorizontal,
  Terminal,
} from 'lucide-react';
import { EmptyState } from '../../components/EmptyState';
import { useDemo } from '../../demo/DemoContext';
import type { DemoAgentLogEntry, DemoMessage } from '../../demo/types';
import { useOperant } from '../../context/ClientContext';
import { LiveUnavailableView } from '../../live/LiveUnavailableView';
import { formatChatTimestamp } from '../chat/chatUtils';

const AGENT_STATUS_LABELS: Record<string, string> = {
  online: '在线',
  busy: '忙碌',
  offline: '离线',
};

type StreamEntry =
  | { kind: 'log'; at: string; log: DemoAgentLogEntry }
  | { kind: 'dm'; at: string; msg: DemoMessage };

/** 单条过程日志条目（kind 决定卡片形态；detail 折叠展开） */
const LogEntryItem: React.FC<{ log: DemoAgentLogEntry }> = ({ log }) => {
  const [expanded, setExpanded] = useState(false);
  const Icon = log.kind === 'thought' ? Brain : log.kind === 'tool_call' ? Terminal : MessageSquare;
  const kindLabel =
    log.kind === 'thought' ? '思考' : log.kind === 'tool_call' ? '工具调用' : '中间产出';
  return (
    <div className={`wf-log-item wf-log-${log.kind}`}>
      <div className="wf-log-head">
        <span className="chat-icon-box" aria-hidden="true">
          <Icon size={14} />
        </span>
        <span className={`wf-log-title${log.kind === 'tool_call' ? ' mono' : ''}`}>
          {log.title}
        </span>
        <span className="wf-log-kind">{kindLabel}</span>
        <span className="wf-log-time">{formatChatTimestamp(log.createdAt)}</span>
        {log.detail && (
          <button
            type="button"
            className="wf-log-toggle"
            aria-expanded={expanded}
            aria-label={expanded ? `收起详情：${log.title}` : `展开详情：${log.title}`}
            onClick={() => setExpanded((v) => !v)}
          >
            {expanded ? (
              <ChevronDown size={14} aria-hidden="true" />
            ) : (
              <ChevronRight size={14} aria-hidden="true" />
            )}
          </button>
        )}
      </div>
      {log.detail && expanded && <div className="wf-log-detail">{log.detail}</div>}
    </div>
  );
};

export const AgentFocusView: React.FC = () => {
  const { clientMode } = useOperant();
  if (clientMode === 'live') {
    return <LiveUnavailableView section="workflow" />;
  }
  return <DemoAgentFocusView />;
};

const DemoAgentFocusView: React.FC = () => {
  const { id = '', sid = '', aid = '' } = useParams<{ id: string; sid: string; aid: string }>();
  const navigate = useNavigate();
  const { getConversation, getAgent, messages, agentLogsOf, sendAgentDM } = useDemo();

  const conversation = getConversation(sid);
  const agent = getAgent(aid);
  const valid = Boolean(
    conversation && conversation.workflowId === id && agent && conversation.agentIds.includes(aid)
  );

  const [value, setValue] = useState('');
  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // 条目流：过程日志 + 单独发给该 Agent 的 DM，按时间排序
  const entries = useMemo<StreamEntry[]>(() => {
    if (!valid) return [];
    const logs = agentLogsOf(sid, aid).map(
      (log): StreamEntry => ({ kind: 'log', at: log.createdAt, log })
    );
    const dms = (messages[sid] ?? [])
      .filter(
        (m) =>
          m.channel === 'group' &&
          m.authorId === 'user' &&
          Array.isArray(m.audience) &&
          m.audience.length === 1 &&
          m.audience[0] === aid
      )
      .map((msg): StreamEntry => ({ kind: 'dm', at: msg.createdAt, msg }));
    return [...logs, ...dms].sort((a, b) => a.at.localeCompare(b.at));
  }, [valid, agentLogsOf, messages, sid, aid]);

  // 切换 Agent / 新条目时：清空草稿、滚动到流尾
  useEffect(() => {
    setValue('');
  }, [sid, aid]);
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [sid, aid, entries.length]);

  // textarea 自动增高（上限 5 行）
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 118)}px`;
  }, [value, sid, aid]);

  const doSend = () => {
    const text = value.trim();
    if (!text || !valid) return;
    sendAgentDM(sid, aid, text);
    setValue('');
    textareaRef.current?.focus();
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      doSend();
    }
  };

  if (!valid || !conversation || !agent) {
    return (
      <div className="chat-view chat-view-empty">
        <EmptyState
          icon={Bot}
          titleAs="h1"
          title="未找到该 Agent 或会话"
          description="请返回群聊界面，从参与者头像进入 Agent 个人界面。"
          action={
            <button className="btn btn-secondary" onClick={() => navigate(`/workflow/${id}/s/${sid}`)}>
              <ArrowLeft size={14} aria-hidden="true" />
              返回群聊
            </button>
          }
        />
      </div>
    );
  }

  const canSend = value.trim().length > 0;

  return (
    <div className="chat-view">
      <header className="chat-header">
        <button
          className="btn btn-secondary btn-icon"
          onClick={() => navigate(`/workflow/${id}/s/${sid}`)}
          aria-label="返回群聊"
          title="返回群聊"
        >
          <ArrowLeft size={16} aria-hidden="true" />
        </button>
        <span
          className="chat-avatar"
          style={{ backgroundColor: agent.color }}
          aria-hidden="true"
        >
          {agent.name.charAt(0)}
        </span>
        <div className="chat-header-main">
          <div className="chat-header-title-row">
            <h1 className="chat-header-title">{agent.name}</h1>
            <span className={`agent-status ${agent.status}`}>
              <span className="agent-status-dot" aria-hidden="true" />
              {AGENT_STATUS_LABELS[agent.status] ?? agent.status}
            </span>
          </div>
          <div className="chat-header-sub">
            <span className="chat-header-sub-text">
              <span className="section-mono">{agent.model}</span> · 该 Agent 在此会话中的过程明细
            </span>
          </div>
        </div>
      </header>

      <div className="chat-body">
        <div className="chat-scroll" ref={scrollRef} tabIndex={0} aria-label={`${agent.name} 的过程明细`}>
          <div className="chat-messages">
            {entries.length > 0 ? (
              entries.map((entry) =>
                entry.kind === 'log' ? (
                  <LogEntryItem key={entry.log.id} log={entry.log} />
                ) : (
                  <div key={entry.msg.id} className="wf-dm-item">
                    <div className="wf-dm-label">
                      我单独发给 {agent.name} · {formatChatTimestamp(entry.msg.createdAt)}
                    </div>
                    <div className="wf-dm-text">
                      {entry.msg.payload.kind === 'text' ? entry.msg.payload.text : ''}
                    </div>
                  </div>
                )
              )
            ) : (
              <EmptyState
                icon={Bot}
                title="暂无过程明细"
                description="该 Agent 在此会话中还没有过程日志或单独消息。"
              />
            )}
          </div>
        </div>
      </div>

      <div className="chat-composer">
        <div className="chat-composer-inner">
          <textarea
            ref={textareaRef}
            className="chat-composer-input"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={handleKeyDown}
            rows={1}
            placeholder={`单独发给 ${agent.name}`}
            aria-label={`单独发给 ${agent.name}`}
          />
          <button
            className="btn btn-primary chat-send-btn"
            onClick={doSend}
            disabled={!canSend}
            aria-label="发送单独消息"
          >
            <SendHorizontal size={14} aria-hidden="true" />
            发送
          </button>
        </div>
      </div>
    </div>
  );
};
