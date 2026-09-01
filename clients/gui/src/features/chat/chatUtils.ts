/**
 * 会话页共享工具：时间格式化、弹层外点关闭、菜单方向键导航、工作流目录展示名。
 * 被 src/features/chat/ 下组件与壳层（RailLayout 状态栏）使用。
 * v6 修复轮：工作流展示名单一来源收敛到 demo/fixtures 的 DEMO_WORKFLOW_NAMES
 * （实例命名与展示共用口径，消除与 fixtures 的重复映射）。
 */

import { useEffect } from 'react';
import type { KeyboardEvent, RefObject } from 'react';
import { DEMO_WORKFLOW_NAMES } from '../../demo/fixtures';
import { formatRelativeDay, formatTime } from '../../lib/format';

/** 工作流目录条目 id → 展示名（单一来源：demo/fixtures；未知 id 回退 id 本身） */
export function getWorkflowDisplayName(workflowId: string): string {
  return DEMO_WORKFLOW_NAMES[workflowId] ?? workflowId;
}

/** 会话内时间戳：当天 → 仅时分；更早 → 相对日期 + 时分（统一走 lib/format） */
export function formatChatTimestamp(iso: string): string {
  const day = formatRelativeDay(iso);
  return day === '今天' ? formatTime(iso) : `${day} ${formatTime(iso)}`;
}

/** 弹层外点关闭：active 时监听 document mousedown，点击 ref 容器外触发 onClose */
export function useOutsideClick(
  ref: RefObject<HTMLElement | null>,
  active: boolean,
  onClose: () => void
): void {
  useEffect(() => {
    if (!active) return;
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [ref, active, onClose]);
}

/** 菜单弹层方向键导航：ArrowUp / ArrowDown 在 [role="menuitem"] 间循环移动焦点 */
export function handleMenuArrowKeys(e: KeyboardEvent<HTMLElement>): void {
  if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
  const items = Array.from(e.currentTarget.querySelectorAll<HTMLElement>('[role="menuitem"]'));
  if (items.length === 0) return;
  e.preventDefault();
  const index = items.indexOf(document.activeElement as HTMLElement);
  const next =
    e.key === 'ArrowDown' ? (index + 1) % items.length : (index - 1 + items.length) % items.length;
  items[next].focus();
}
