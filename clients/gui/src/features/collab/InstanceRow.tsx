/**
 * 工作流实例行（v6 包C）：协作总览活跃实例列表与侧栏「实例」组共用。
 * 行=实例名 + 状态徽章（运行中/空闲，侧栏仅运行中显示）+（可选时间）+ hover「···」菜单；
 * 活跃行点击进群聊会话 /workflow/:wfId/s/:conversationId（wfId=会话 workflowId），
 * 归档行为只读行（菜单仅 恢复/删除）。
 * 重命名走 Modal 输入框、删除走确认 Modal；归档/恢复直接调用数据层——
 * 运行中实例的守卫拦截在数据层完成（返回 false 时已弹 warn 通知，UI 不重复弹提示）。
 */

import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { StatusBadge } from '../../components/StatusBadge';
import { Modal } from '../../components/Modal';
import { useDemo } from '../../demo/DemoContext';
import type { DemoConversation } from '../../demo/types';
import { formatChatTimestamp } from '../chat/chatUtils';
import { InstanceMenu } from './InstanceMenu';

interface InstanceRowProps {
  conversation: DemoConversation;
  /** 归档只读行：主区不可点，菜单为 恢复/删除 */
  archived?: boolean;
  /** 空闲实例也显示状态徽章（总览右侧列表用；侧栏默认只标运行中） */
  showIdleBadge?: boolean;
  /** 行尾显示相对更新时间（总览列表用） */
  showTime?: boolean;
  /** 「···」菜单弹出方向：归档区靠近容器底部时传 up */
  direction?: 'down' | 'up';
  /** 行点击跳转后回调（移动端抽屉用于关闭自身） */
  onNavigate?: () => void;
}

export const InstanceRow: React.FC<InstanceRowProps> = ({
  conversation,
  archived = false,
  showIdleBadge = false,
  showTime = false,
  direction = 'down',
  onNavigate,
}) => {
  const navigate = useNavigate();
  const { renameConversation, archiveInstance, restoreInstance, deleteInstance } = useDemo();

  const [renameOpen, setRenameOpen] = useState(false);
  const [renameDraft, setRenameDraft] = useState('');
  const [deleteOpen, setDeleteOpen] = useState(false);

  const isRunning = conversation.runStatus === 'running';

  const goSession = () => {
    navigate(`/workflow/${conversation.workflowId}/s/${conversation.id}`);
    onNavigate?.();
  };

  const openRename = () => {
    setRenameDraft(conversation.title);
    setRenameOpen(true);
  };

  const confirmRename = () => {
    const trimmed = renameDraft.trim();
    if (!trimmed) return;
    // 运行中实例被数据层拦截：返回 false 时已弹 warn 通知，这里不重复提示
    renameConversation(conversation.id, trimmed);
    setRenameOpen(false);
  };

  const confirmDelete = () => {
    deleteInstance(conversation.id);
    setDeleteOpen(false);
  };

  const menuItems = archived
    ? [
        {
          key: 'restore',
          label: '恢复',
          onSelect: () => {
            restoreInstance(conversation.id);
          },
        },
        { key: 'delete', label: '删除', danger: true, onSelect: () => setDeleteOpen(true) },
      ]
    : [
        { key: 'rename', label: '重命名', onSelect: openRename },
        {
          key: 'archive',
          label: '归档',
          onSelect: () => {
            archiveInstance(conversation.id);
          },
        },
        { key: 'delete', label: '删除', danger: true, onSelect: () => setDeleteOpen(true) },
      ];

  return (
    <div className="inst-row">
      {archived ? (
        <span className="inst-row-static">
          <span className="inst-row-name">{conversation.title}</span>
          {showTime && (
            <span className="inst-row-time">{formatChatTimestamp(conversation.updatedAt)}</span>
          )}
        </span>
      ) : (
        <button
          type="button"
          className="inst-row-main"
          onClick={goSession}
          title={`进入会话：${conversation.title}`}
        >
          <span className="inst-row-name">{conversation.title}</span>
          {isRunning ? (
            <StatusBadge status="running" size="sm" />
          ) : showIdleBadge ? (
            <StatusBadge status="idle" label="空闲" size="sm" />
          ) : null}
          {showTime && (
            <span className="inst-row-time">{formatChatTimestamp(conversation.updatedAt)}</span>
          )}
        </button>
      )}

      <InstanceMenu
        items={menuItems}
        ariaLabel={`实例「${conversation.title}」操作菜单`}
        direction={direction}
      />

      {/* 重命名 Modal */}
      <Modal
        isOpen={renameOpen}
        onClose={() => setRenameOpen(false)}
        title="重命名实例"
        maxWidth={420}
        footer={
          <>
            <button type="button" className="btn btn-ghost" onClick={() => setRenameOpen(false)}>
              取消
            </button>
            <button
              type="button"
              className="btn btn-primary"
              disabled={!renameDraft.trim()}
              onClick={confirmRename}
            >
              确认重命名
            </button>
          </>
        }
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <p style={{ fontSize: '13px', color: 'var(--text-secondary)', margin: 0 }}>
            重命名只影响本实例会话的展示名，不影响工作流模板与运行记录。
          </p>
          <input
            type="text"
            className="input"
            value={renameDraft}
            onChange={(e) => setRenameDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') confirmRename();
            }}
            aria-label="实例名称"
            placeholder="输入新的实例名称"
          />
        </div>
      </Modal>

      {/* 删除确认 Modal */}
      <Modal
        isOpen={deleteOpen}
        onClose={() => setDeleteOpen(false)}
        title="删除实例"
        maxWidth={420}
        footer={
          <>
            <button type="button" className="btn btn-ghost" onClick={() => setDeleteOpen(false)}>
              取消
            </button>
            <button type="button" className="btn btn-danger" onClick={confirmDelete}>
              确认删除
            </button>
          </>
        }
      >
        <p style={{ fontSize: '13px', color: 'var(--text-secondary)', margin: 0 }}>
          将彻底删除实例「{conversation.title}」及其消息、过程日志、文件、任务与知识条目，该操作不可撤销。
        </p>
      </Modal>
    </div>
  );
};
