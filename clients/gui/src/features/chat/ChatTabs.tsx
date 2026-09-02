/**
 * 会话页 Tab 行（设计基线 §3.2）：对话 / 文件(n) / 任务(n) / 知识库。
 * role=tablist，左右方向键 + Home/End 切换并移动焦点（roving tabindex）；
 * 选中 Tab 下划线 accent。
 */

import React, { useRef } from 'react';

export type ChatTabKey = 'chat' | 'files' | 'tasks' | 'knowledge';

const TABS: Array<{ key: ChatTabKey; label: string }> = [
  { key: 'chat', label: '对话' },
  { key: 'files', label: '文件' },
  { key: 'tasks', label: '任务' },
  { key: 'knowledge', label: '知识库' },
];

interface ChatTabsProps {
  active: ChatTabKey;
  onChange: (tab: ChatTabKey) => void;
  fileCount: number;
  taskCount: number;
}

export const ChatTabs: React.FC<ChatTabsProps> = ({ active, onChange, fileCount, taskCount }) => {
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const current = TABS.findIndex((t) => t.key === active);
    let next = -1;
    if (e.key === 'ArrowRight') next = (current + 1) % TABS.length;
    else if (e.key === 'ArrowLeft') next = (current - 1 + TABS.length) % TABS.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = TABS.length - 1;
    if (next < 0) return;
    e.preventDefault();
    onChange(TABS[next].key);
    tabRefs.current[next]?.focus();
  };

  const countOf = (key: ChatTabKey): number | null =>
    key === 'files' ? fileCount : key === 'tasks' ? taskCount : null;

  return (
    <div className="chat-tabs" role="tablist" aria-label="会话内容" onKeyDown={handleKeyDown}>
      {TABS.map((tab, index) => {
        const count = countOf(tab.key);
        const isActive = tab.key === active;
        return (
          <button
            key={tab.key}
            ref={(el) => {
              tabRefs.current[index] = el;
            }}
            className={`chat-tab${isActive ? ' active' : ''}`}
            role="tab"
            id={`chat-tab-${tab.key}`}
            aria-selected={isActive}
            aria-controls={`chat-panel-${tab.key}`}
            tabIndex={isActive ? 0 : -1}
            onClick={() => onChange(tab.key)}
          >
            {tab.label}
            {count !== null && <span className="chat-tab-count">（{count}）</span>}
          </button>
        );
      })}
    </div>
  );
};
