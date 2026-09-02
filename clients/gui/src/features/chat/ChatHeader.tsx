/**
 * 会话头（设计基线 §1.2 对话模式纯净化 + v4 §2 副标题联动）：标题 18px/600 + ⭐ 置顶切换
 * （置顶态 accent 填充）+ 分享（演示 toast）+ 更多下拉菜单（置顶/重命名/归档/导出，
 * Esc/外点关闭、焦点还原）。
 * 副标题：direct 会话="{选中助手} · {model}"（随 selectedAgentBySession 联动）；
 * 群聊会话（workflowId）="{N} 位参与者 · 所属工作流 {名称}"。
 */

import React, { useCallback, useRef, useState } from 'react';
import { Archive, Download, MoreHorizontal, Pencil, Pin, Share2, Star } from 'lucide-react';
import { useOperant } from '../../context/ClientContext';
import { useDemo } from '../../demo/DemoContext';
import type { DemoConversation } from '../../demo/types';
import { useDialogA11y } from '../../components/useDialogA11y';
import { getWorkflowDisplayName, handleMenuArrowKeys, useOutsideClick } from './chatUtils';

interface ChatHeaderProps {
  conversation: DemoConversation;
  /** <960 或 960–1279 收起侧栏时传入：静态嵌入标题左侧的"打开侧栏"按钮 */
  sidebarButton?: React.ReactNode;
}

export const ChatHeader: React.FC<ChatHeaderProps> = ({ conversation, sidebarButton }) => {
  const { getAgent, togglePin, selectedAgentBySession } = useDemo();
  const { addNotification } = useOperant();
  const [menuOpen, setMenuOpen] = useState(false);
  const menuWrapRef = useRef<HTMLDivElement>(null);

  const closeMenu = useCallback(() => setMenuOpen(false), []);
  const menuPanelRef = useDialogA11y<HTMLDivElement>(menuOpen, closeMenu);
  useOutsideClick(menuWrapRef, menuOpen, closeMenu);

  // direct：副标题随选中助手联动（缺省 agent_assistant）；群聊：参与者数 + 所属工作流名
  const isGroup = Boolean(conversation.workflowId);
  const selectedAgent = isGroup
    ? undefined
    : getAgent(selectedAgentBySession[conversation.id] ?? 'agent_assistant');
  const subtitle = isGroup
    ? `${conversation.agentIds.length} 位参与者 · 所属工作流 ${getWorkflowDisplayName(conversation.workflowId ?? '')}`
    : selectedAgent
      ? `${selectedAgent.name} · ${selectedAgent.model}`
      : '暂无主助手';

  const handleTogglePin = useCallback(() => {
    togglePin(conversation.id);
    addNotification('success', conversation.pinned ? '已取消置顶（演示）' : '已置顶（演示）');
  }, [togglePin, conversation.id, conversation.pinned, addNotification]);

  const handleShare = () => {
    addNotification('success', '分享链接已复制（演示）');
  };

  const demoAction = (message: string) => {
    addNotification('info', message);
    closeMenu();
  };

  return (
    <header className="chat-header">
      {sidebarButton}
      <div className="chat-header-main">
        <div className="chat-header-title-row">
          <h1 className="chat-header-title">{conversation.title}</h1>
          <button
            className={`chat-pin-btn${conversation.pinned ? ' pinned' : ''}`}
            onClick={handleTogglePin}
            aria-pressed={conversation.pinned}
            aria-label={conversation.pinned ? '取消置顶' : '置顶会话'}
            title={conversation.pinned ? '取消置顶' : '置顶会话'}
          >
            <Star
              size={16}
              fill={conversation.pinned ? 'currentColor' : 'none'}
              aria-hidden="true"
            />
          </button>
        </div>
        <div className="chat-header-sub">
          <span className="chat-header-sub-text">{subtitle}</span>
        </div>
      </div>

      <div className="chat-header-actions">
        <button className="btn btn-secondary" onClick={handleShare}>
          <Share2 size={14} aria-hidden="true" />
          分享
        </button>
        <div className="chat-menu-wrap" ref={menuWrapRef}>
          <button
            className="btn btn-secondary"
            onClick={() => setMenuOpen((v) => !v)}
            aria-haspopup="menu"
            aria-expanded={menuOpen}
          >
            <MoreHorizontal size={14} aria-hidden="true" />
            更多
          </button>
          {menuOpen && (
            <div
              className="chat-popover chat-menu"
              role="menu"
              aria-label="会话操作"
              ref={menuPanelRef}
              onKeyDown={handleMenuArrowKeys}
            >
              <button
                className="chat-menu-item"
                role="menuitem"
                onClick={() => {
                  handleTogglePin();
                  closeMenu();
                }}
              >
                <Pin size={14} aria-hidden="true" />
                {conversation.pinned ? '取消置顶' : '置顶会话'}
              </button>
              <button
                className="chat-menu-item"
                role="menuitem"
                onClick={() => demoAction('重命名会话（演示）')}
              >
                <Pencil size={14} aria-hidden="true" />
                重命名
              </button>
              <button
                className="chat-menu-item"
                role="menuitem"
                onClick={() => demoAction('归档会话（演示）')}
              >
                <Archive size={14} aria-hidden="true" />
                归档
              </button>
              <button
                className="chat-menu-item"
                role="menuitem"
                onClick={() => demoAction('导出会话（演示）')}
              >
                <Download size={14} aria-hidden="true" />
                导出
              </button>
            </div>
          )}
        </div>
      </div>
    </header>
  );
};
