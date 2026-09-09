/**
 * 任务分区（设计基线 §6，中深页）
 * 页头（标题"任务" + 总数副文案）+ 过滤 chips（全部/进行中/已完成，选中态 accent）+
 * 按会话分组的任务列表（组头=会话标题；行=复选框 toggleTask + 标题 + StatusBadge + 截止时间；
 * 完成行划线弱化）+ 空态。
 */

import React, { useMemo, useState } from 'react';
import { ListTodo } from 'lucide-react';
import { EmptyState } from '../../components/EmptyState';
import { StatusBadge } from '../../components/StatusBadge';
import { useDemo } from '../../demo/DemoContext';
import type { DemoTaskItem } from '../../demo/types';
import { formatDate } from '../../lib/format';

type TaskFilter = 'all' | 'active' | 'done';

const FILTER_OPTIONS: Array<{ key: TaskFilter; label: string }> = [
  { key: 'all', label: '全部' },
  { key: 'active', label: '进行中' },
  { key: 'done', label: '已完成' },
];

const EMPTY_COPY: Record<TaskFilter, { title: string; description: string }> = {
  all: { title: '暂无任务', description: '演示数据中还没有任何任务。' },
  active: { title: '暂无进行中的任务', description: '所有任务都已完成，可切换过滤条件查看。' },
  done: { title: '暂无已完成的任务', description: '勾选任务复选框后会出现在这里。' },
};

import { useOperant } from '../../context/ClientContext';
import { LiveUnavailableView } from '../../live/LiveUnavailableView';

interface TaskGroup {
  id: string;
  title: string;
  items: DemoTaskItem[];
}

export const TasksView: React.FC = () => {
  const { clientMode } = useOperant();
  if (clientMode === 'live') {
    return <LiveUnavailableView section="tasks" />;
  }
  return <DemoTasksView />;
};

const DemoTasksView: React.FC = () => {
  const { conversations, tasks, toggleTask } = useDemo();
  const [filter, setFilter] = useState<TaskFilter>('all');

  const filtered = useMemo(
    () => tasks.filter((t) => (filter === 'all' ? true : filter === 'done' ? t.done : !t.done)),
    [tasks, filter]
  );

  // 按会话分组（组顺序跟随会话列表），无匹配会话的任务归入"其他会话"
  const groups = useMemo<TaskGroup[]>(() => {
    const result: TaskGroup[] = [];
    const assigned = new Set<string>();
    for (const conv of conversations) {
      const items = filtered.filter((t) => t.conversationId === conv.id);
      if (items.length > 0) {
        result.push({ id: conv.id, title: conv.title, items });
        items.forEach((t) => assigned.add(t.id));
      }
    }
    const orphans = filtered.filter((t) => !assigned.has(t.id));
    if (orphans.length > 0) {
      result.push({ id: '__other__', title: '其他会话', items: orphans });
    }
    return result;
  }, [conversations, filtered]);

  const empty = EMPTY_COPY[filter];

  return (
    <div className="section-view">
      <header className="section-header">
        <h1 className="section-title">任务</h1>
        <p className="section-sub">共 {tasks.length} 项任务</p>
        <div className="section-chips" role="group" aria-label="任务过滤">
          {FILTER_OPTIONS.map((opt) => (
            <button
              key={opt.key}
              type="button"
              className={`section-chip${filter === opt.key ? ' active' : ''}`}
              aria-pressed={filter === opt.key}
              onClick={() => setFilter(opt.key)}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </header>

      <div className="section-scroll">
        <div className="section-inner">
          {groups.length === 0 ? (
            <div className="section-empty-wrap">
              <EmptyState icon={ListTodo} title={empty.title} description={empty.description} />
            </div>
          ) : (
            groups.map((group) => (
              <section key={group.id} className="section-group" aria-label={group.title}>
                <h2 className="section-group-title">
                  {group.title}
                  <span className="section-group-count">{group.items.length} 项</span>
                </h2>
                <ul className="chat-row-list">
                  {group.items.map((t) => (
                    <li key={t.id} className="chat-row chat-row-wrap">
                      <input
                        type="checkbox"
                        className="chat-task-check"
                        checked={t.done}
                        onChange={() => toggleTask(t.id)}
                        aria-label={
                          t.done ? `标记为未完成：${t.title}` : `标记为完成：${t.title}`
                        }
                      />
                      <span className="chat-row-main">
                        <span className={`chat-row-title${t.done ? ' chat-row-title-done' : ''}`}>
                          {t.title}
                        </span>
                      </span>
                      <StatusBadge status={t.done ? 'completed' : 'pending'} size="sm" />
                      {t.due && (
                        <span className="chat-row-meta">截止 {formatDate(t.due)}</span>
                      )}
                    </li>
                  ))}
                </ul>
              </section>
            ))
          )}
        </div>
      </div>
    </div>
  );
};
