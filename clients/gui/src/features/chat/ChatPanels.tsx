/**
 * 文件 / 任务 / 知识库 Tab 面板（设计基线 §3.2），均含空态。
 * 文件行：图标 + 名称 + 路径 + 大小 + 修改时间；
 * 任务行：复选框（toggleTask）+ 标题 + 状态徽章（StatusBadge）；
 * 知识卡：标题 + 摘要 + 来源标签。
 */

import React from 'react';
import { BookOpen, FileText, FolderOpen, ListTodo } from 'lucide-react';
import { EmptyState } from '../../components/EmptyState';
import { StatusBadge } from '../../components/StatusBadge';
import { useDemo } from '../../demo/DemoContext';
import { formatChatTimestamp } from './chatUtils';

interface PanelProps {
  conversationId: string;
}

export const FilesPanel: React.FC<PanelProps> = ({ conversationId }) => {
  const { files } = useDemo();
  const list = files[conversationId] ?? [];

  return (
    <div
      className="chat-scroll"
      role="tabpanel"
      id="chat-panel-files"
      aria-labelledby="chat-tab-files"
      tabIndex={0}
    >
      <div className="chat-panel-inner">
        {list.length === 0 ? (
          <EmptyState icon={FolderOpen} title="暂无文件" description="该会话还没有关联文件。" />
        ) : (
          <ul className="chat-row-list">
            {list.map((f) => (
              <li key={f.id} className="chat-row">
                <span className="chat-icon-box" aria-hidden="true">
                  <FileText size={16} />
                </span>
                <span className="chat-row-main">
                  <span className="chat-row-title">{f.name}</span>
                  <span className="chat-row-sub">{f.path}</span>
                </span>
                <span className="chat-row-meta">{f.size}</span>
                <span className="chat-row-meta">{formatChatTimestamp(f.modifiedAt)}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
};

export const TasksPanel: React.FC<PanelProps> = ({ conversationId }) => {
  const { tasks, toggleTask } = useDemo();
  const list = tasks.filter((t) => t.conversationId === conversationId);

  return (
    <div
      className="chat-scroll"
      role="tabpanel"
      id="chat-panel-tasks"
      aria-labelledby="chat-tab-tasks"
      tabIndex={0}
    >
      <div className="chat-panel-inner">
        {list.length === 0 ? (
          <EmptyState icon={ListTodo} title="暂无任务" description="该会话还没有关联任务。" />
        ) : (
          <ul className="chat-row-list">
            {list.map((t) => (
              <li key={t.id} className="chat-row">
                <input
                  type="checkbox"
                  className="chat-task-check"
                  checked={t.done}
                  onChange={() => toggleTask(t.id)}
                  aria-label={t.done ? `标记为未完成：${t.title}` : `标记为完成：${t.title}`}
                />
                <span className="chat-row-main">
                  <span className={`chat-row-title${t.done ? ' chat-row-title-done' : ''}`}>
                    {t.title}
                  </span>
                </span>
                <StatusBadge status={t.done ? 'completed' : 'pending'} size="sm" />
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
};

export const KnowledgePanel: React.FC<PanelProps> = ({ conversationId }) => {
  const { knowledge } = useDemo();
  const list = knowledge[conversationId] ?? [];

  return (
    <div
      className="chat-scroll"
      role="tabpanel"
      id="chat-panel-knowledge"
      aria-labelledby="chat-tab-knowledge"
      tabIndex={0}
    >
      <div className="chat-panel-inner">
        {list.length === 0 ? (
          <EmptyState icon={BookOpen} title="暂无知识条目" description="该会话还没有沉淀知识。" />
        ) : (
          <div className="chat-knowledge-list">
            {list.map((k) => (
              <div key={k.id} className="chat-knowledge-card">
                <div className="chat-knowledge-title">{k.title}</div>
                <p className="chat-knowledge-summary">{k.summary}</p>
                <span className="chat-source-tag">{k.source}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
};
