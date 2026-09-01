/**
 * 内部菜单栏（v4 设计基线 §2）：会话头部下方 36px 栏。
 * direct 会话：左=Agent 选择器（下拉列 4 位 Agent：彩点+名称+model，当前值
 *   selectedAgentBySession[会话] ?? 'agent_assistant'，切换 selectSessionAgent + toast，
 *   弹层 Esc/外点关闭、方向键导航、焦点还原）；
 *   右="为本会话搭建工作流"次按钮（GitBranch 图标，调 buildTempWorkflow；
 *   该会话已有临时工作流时禁用 + title 提示）。
 * 群聊会话（conversation.workflowId 存在）：不渲染选择器/搭建按钮，
 *   改显"所属工作流：{名称} →"链接 chip（v5 起跳协作总览 /collab）。
 */

import React, { useCallback, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { CalendarClock, Check, ChevronDown, GitBranch } from 'lucide-react';
import { useOperant } from '../../context/ClientContext';
import { useDemo } from '../../demo/DemoContext';
import type { DemoConversation } from '../../demo/types';
import { useDialogA11y } from '../../components/useDialogA11y';
import { getWorkflowDisplayName, handleMenuArrowKeys, useOutsideClick } from './chatUtils';

interface ChatToolbarProps {
  conversation: DemoConversation;
}

export const ChatToolbar: React.FC<ChatToolbarProps> = ({ conversation }) => {
  const navigate = useNavigate();
  const {
    agents,
    selectedAgentBySession,
    selectSessionAgent,
    tempWorkflows,
    buildTempWorkflow,
  } = useDemo();
  const { addNotification } = useOperant();
  const [pickerOpen, setPickerOpen] = useState(false);
  const pickerWrapRef = useRef<HTMLDivElement>(null);

  const closePicker = useCallback(() => setPickerOpen(false), []);
  const pickerPanelRef = useDialogA11y<HTMLDivElement>(pickerOpen, closePicker);
  useOutsideClick(pickerWrapRef, pickerOpen, closePicker);

  // 群聊会话：仅"所属工作流"链接 chip（v5 起跳协作总览，工作流分区已被吸收）
  if (conversation.workflowId) {
    return (
      <div className="chat-toolbar">
        <button
          className="chat-toolbar-link-chip"
          onClick={() => navigate('/collab')}
        >
          <GitBranch size={13} aria-hidden="true" />
          所属工作流：{getWorkflowDisplayName(conversation.workflowId)}
          <span aria-hidden="true">→</span>
        </button>
      </div>
    );
  }

  // direct 会话：当前选中助手（缺省 agent_assistant）
  const selectedAgentId = selectedAgentBySession[conversation.id] ?? 'agent_assistant';
  const selectedAgent =
    agents.find((a) => a.id === selectedAgentId) ??
    agents.find((a) => a.id === 'agent_assistant');
  const hasTempWorkflow = tempWorkflows.some((t) => t.sessionId === conversation.id);

  const handleSelect = (agentId: string, name: string) => {
    if (agentId !== selectedAgentId) {
      selectSessionAgent(conversation.id, agentId);
      addNotification('success', `已切换为 ${name}`);
    }
    closePicker();
  };

  return (
    <div className="chat-toolbar">
      {/* 左：Agent 选择器 */}
      <div className="chat-menu-wrap" ref={pickerWrapRef}>
        <button
          className="chat-toolbar-agent-btn"
          onClick={() => setPickerOpen((v) => !v)}
          aria-haspopup="menu"
          aria-expanded={pickerOpen}
          title="选择本会话助手"
        >
          <span
            className="rail-sidebar-dot"
            style={{ backgroundColor: selectedAgent?.color ?? 'var(--text-muted)' }}
            aria-hidden="true"
          />
          <span className="chat-toolbar-agent-name">{selectedAgent?.name ?? '选择 Agent'}</span>
          <span className="chat-toolbar-agent-model">{selectedAgent?.model ?? ''}</span>
          <ChevronDown size={13} aria-hidden="true" />
        </button>
        {pickerOpen && (
          <div
            className="chat-popover chat-agent-pop"
            role="menu"
            aria-label="选择本会话助手"
            ref={pickerPanelRef}
            onKeyDown={handleMenuArrowKeys}
          >
            {agents.map((a) => (
              <button
                key={a.id}
                className="chat-mention-item"
                role="menuitem"
                onClick={() => handleSelect(a.id, a.name)}
              >
                <span
                  className="rail-sidebar-dot"
                  style={{ backgroundColor: a.color }}
                  aria-hidden="true"
                />
                <span className="chat-mention-name">{a.name}</span>
                <span className="chat-mention-model">{a.model}</span>
                {a.id === selectedAgentId && (
                  <Check size={13} className="chat-agent-check" aria-hidden="true" />
                )}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* 右侧动作区 */}
      <div style={{ display: 'flex', gap: 6 }}>
        <button
          className="btn btn-ghost btn-sm"
          style={{ height: 28, fontSize: '12px' }}
          onClick={() =>
            navigate(
              `/schedules?new=1&targetType=prompt&targetId=${encodeURIComponent(
                conversation.id
              )}&targetName=${encodeURIComponent(conversation.title)}`
            )
          }
          title="将本会话设置为定时调度任务"
        >
          <CalendarClock size={13} aria-hidden="true" />
          <span>设为调度</span>
        </button>

        {/* 为本会话搭建工作流（已有临时工作流时禁用） */}
        <button
          className="btn btn-secondary chat-toolbar-build-btn"
          onClick={() => buildTempWorkflow(conversation.id)}
          disabled={hasTempWorkflow}
          title={
            hasTempWorkflow
              ? '本会话已搭建临时工作流，可在左侧"临时工作流"组查看'
              : '为本会话搭建临时工作流（演示）'
          }
        >
          <GitBranch size={14} aria-hidden="true" />
          为本会话搭建工作流
        </button>
      </div>
    </div>
  );
};
