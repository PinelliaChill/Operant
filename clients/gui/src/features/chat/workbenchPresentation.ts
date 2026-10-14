import type { WorkbenchMessage } from '../../live/workbenchClient';

/** Describe only the server's delivery and wake projection. */
export function messageDeliveryLabel(item: Pick<WorkbenchMessage, 'delivery_status' | 'wake_status'>): string {
  if (item.delivery_status === 'consumed') return '已读取';
  if (item.wake_status === 'limit_reached') return '已投递 · 未读取 · 自动唤醒已达上限，需人工继续';
  if (item.wake_status === 'scheduled') return '已投递 · 未读取 · 已安排唤醒';
  if (item.wake_status === 'active_run') return '已投递 · 未读取 · 对方运行中';
  if (item.wake_status === 'pending') return '已投递 · 未读取 · 待唤醒';
  return '已投递 · 未读取';
}
