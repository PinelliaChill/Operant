import assert from 'node:assert/strict';
import test from 'node:test';
import { pairedErrorCopy } from '../src/lib/pairedErrorCopy.ts';

test('paired recovery text keeps the distinct action without exposing raw server detail', () => {
  const error = {
    code: 'invalid_error_envelope',
    detail: { detail: { code: 'epoch_changed', message: 'internal epoch 123' } },
  };
  assert.match(pairedErrorCopy(error), /重新启动.*重新配对.*核对原请求/);
  assert.doesNotMatch(pairedErrorCopy(error), /internal|123|epoch_changed/);
  assert.match(pairedErrorCopy({ code: 'invalid_error_envelope', detail: { detail: { code: 'approval_not_granted' } } }), /桌面审批尚未通过/);
  assert.match(pairedErrorCopy({ code: 'transport_unavailable' }), /连接已中断.*核对/);
});
