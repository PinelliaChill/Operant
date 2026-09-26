import assert from 'node:assert/strict';
import test from 'node:test';
import { messageDeliveryLabel } from '../src/features/chat/workbenchPresentation.ts';

test('a persisted message is not presented as read before Core reports consumption', () => {
  assert.equal(messageDeliveryLabel({ delivery_status: 'delivered', wake_status: 'pending' }), '已投递 · 未读取 · 待唤醒');
  assert.equal(messageDeliveryLabel({ delivery_status: 'delivered', wake_status: 'active_run' }), '已投递 · 未读取 · 对方运行中');
  assert.equal(messageDeliveryLabel({ delivery_status: 'delivered', wake_status: 'scheduled' }), '已投递 · 未读取 · 已安排唤醒');
  assert.equal(messageDeliveryLabel({ delivery_status: 'delivered', wake_status: 'limit_reached' }), '已投递 · 未读取 · 自动唤醒已达上限，需人工继续');
  assert.equal(messageDeliveryLabel({ delivery_status: 'consumed', wake_status: 'consumed' }), '已读取');
});
