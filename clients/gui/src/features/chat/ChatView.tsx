/**
 * 会话分区主区（设计基线 §3.2，Phase B 完整实现 + v4 §2 会话模式）
 * 结构：会话头（ChatHeader）/ 内部菜单栏（ChatToolbar：direct=Agent 选择器+搭建工作流，
 * 群聊=所属工作流链接 chip）/ Tab 行（ChatTabs）/ 对话·文件·任务·知识库四面板 /
 * Composer（底部固定；群聊平铺视图经 MessageList 寻址 chip + sendGroupMessage）/ 未选会话空态。
 * v6 修复轮：已归档实例会话只读——消息区顶部只读提示条 + Composer 禁用输入。
 * 样式全部复用 theme.css tokens，新增类见 layout.css 的 ChatView 区块（只追加）。
 */

import React, { useEffect, useState } from 'react';
import { useOutletContext, useParams } from 'react-router-dom';
import { Menu, MessageSquare, PanelLeftOpen, Plus } from 'lucide-react';
import { EmptyState } from '../../components/EmptyState';
import { useOperant } from '../../context/ClientContext';
import { useDemo } from '../../demo/DemoContext';
import type { RailOutletContext } from '../../app/RailLayout';
import { ChatHeader } from './ChatHeader';
import { ChatToolbar } from './ChatToolbar';
import { ChatTabs, type ChatTabKey } from './ChatTabs';
import { MessageList } from './MessageList';
import { FilesPanel, KnowledgePanel, TasksPanel } from './ChatPanels';
import { Composer } from './Composer';

export const ChatView: React.FC = () => {
  const { conversationId } = useParams<{ conversationId: string }>();
  const { getConversation, files, tasks } = useDemo();
  const { addNotification } = useOperant();
  const { showSidebarOpenBtn, openSidebar, isMobile } = useOutletContext<RailOutletContext>();
  const conversation = conversationId ? getConversation(conversationId) : undefined;
  const [activeTab, setActiveTab] = useState<ChatTabKey>('chat');

  // 切换会话时回到对话 Tab
  useEffect(() => {
    setActiveTab('chat');
  }, [conversationId]);

  // 侧栏开启按钮（<960 或 960–1279 收起时）：静态嵌入会话头左侧，不再浮动遮挡标题
  const sidebarOpenBtn = showSidebarOpenBtn ? (
    <button
      className="btn btn-secondary btn-icon chat-header-sidebar-btn"
      onClick={openSidebar}
      aria-label="打开侧栏"
      title="打开侧栏"
    >
      {isMobile ? <Menu size={16} /> : <PanelLeftOpen size={16} />}
    </button>
  ) : undefined;

  // 空态：未选会话或会话 id 不存在（页面级空态，标题即本页唯一 h1）
  if (!conversation) {
    return (
      <div className="chat-view chat-view-empty">
        <EmptyState
          icon={MessageSquare}
          titleAs="h1"
          title="选择一个对话开始"
          description="从会话列表选择对话，或搜索历史会话"
          action={
            <div style={{ display: 'flex', justifyContent: 'center', gap: 8, flexWrap: 'wrap' }}>
              <button
                className="btn btn-primary"
                onClick={() => addNotification('success', '已创建新会话（演示）')}
              >
                <Plus size={14} aria-hidden="true" />
                新建会话
              </button>
              {showSidebarOpenBtn && (
                <button className="btn btn-secondary" onClick={openSidebar}>
                  <Menu size={14} aria-hidden="true" />
                  打开会话列表
                </button>
              )}
            </div>
          }
        />
      </div>
    );
  }

  const fileCount = files[conversation.id]?.length ?? 0;
  const taskCount = tasks.filter((t) => t.conversationId === conversation.id).length;

  return (
    <div className="chat-view">
      <ChatHeader conversation={conversation} sidebarButton={sidebarOpenBtn} />
      <ChatToolbar conversation={conversation} />
      <ChatTabs
        active={activeTab}
        onChange={setActiveTab}
        fileCount={fileCount}
        taskCount={taskCount}
      />
      <div className="chat-body">
        {activeTab === 'chat' && (
          <>
            {conversation.lifecycle === 'archived' && (
              // 已归档实例只读提示条（v6 修复轮）：浅色横幅，不阻断历史消息查看
              <div
                role="note"
                style={{
                  flexShrink: 0,
                  padding: '8px 24px',
                  fontSize: 12,
                  color: 'var(--text-secondary)',
                  backgroundColor: 'var(--bg-subtle)',
                  borderBottom: '1px solid var(--border-subtle)',
                }}
              >
                该实例已归档，仅供查看历史消息
              </div>
            )}
            <MessageList conversation={conversation} />
          </>
        )}
        {activeTab === 'files' && <FilesPanel conversationId={conversation.id} />}
        {activeTab === 'tasks' && <TasksPanel conversationId={conversation.id} />}
        {activeTab === 'knowledge' && <KnowledgePanel conversationId={conversation.id} />}
      </div>
      <Composer conversation={conversation} />
    </div>
  );
};
