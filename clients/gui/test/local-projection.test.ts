import assert from 'node:assert/strict';
import test from 'node:test';
import { approvalId, outcomeNeedsReconciliation, projectionItems, requestCode, requiredText } from '../src/features/extensions/localProjection.ts';

test('local extension projections fail visibly on malformed Core data', () => {
  assert.throws(() => projectionItems({ items: null }, '扩展'), /列表格式无效/);
  assert.throws(() => projectionItems({ items: [null] }, '扩展'), /格式无效/);
  assert.throws(() => requiredText('', 'plugin_id'), /缺失/);
});

test('approval errors retain the Core approval id without manufacturing one', () => {
  const ask = Object.assign(new Error('approval required'), {
    code: 'approval_required',
    detail: { approval_id: 'approval-42', action_hash: 'hash-42' },
  });
  assert.equal(requestCode(ask), 'approval_required');
  assert.equal(approvalId(ask), 'approval-42');
  const fastApiEnvelope = Object.assign(new Error('invalid error envelope'), {
    code: 'invalid_error_envelope',
    detail: { detail: { code: 'approval_required', approval_id: 'approval-43' } },
  });
  assert.equal(requestCode(fastApiEnvelope), 'approval_required');
  assert.equal(approvalId(fastApiEnvelope), 'approval-43');
  assert.equal(approvalId(new Error('network unavailable')), undefined);
});

test('unknown local outcomes lock later actions until an explicit projection refresh', () => {
  assert.equal(outcomeNeedsReconciliation({ code: 'outcome_unknown' }), true);
  assert.equal(outcomeNeedsReconciliation({ detail: { code: 'command_outcome_unknown' } }), true);
  assert.equal(outcomeNeedsReconciliation({ detail: { code: 'command_in_progress' } }), true);
  assert.equal(outcomeNeedsReconciliation({ code: 'transport_unavailable' }), true);
  assert.equal(outcomeNeedsReconciliation({ outcomeUnknown: true }), true);
  assert.equal(outcomeNeedsReconciliation({ code: 'approval_required' }), false);
});
