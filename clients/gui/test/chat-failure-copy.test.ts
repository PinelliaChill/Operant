import assert from 'node:assert/strict';
import test from 'node:test';
import { chatFailureCode, chatFailureCopy } from '../src/lib/chatFailureCopy.ts';

const secret = 'SENTINEL_SECRET_IN_EXCEPTION';

test('history read failures show a recovery step while raw code and message stay out of ordinary copy', () => {
  const error = { code: 'http_404', message: `Graph Run not found ${secret}`, recovery: 'none' };
  assert.match(chatFailureCopy(error, 'history'), /目标记录未找到.*刷新列表/);
  assert.equal(chatFailureCode(error), 'http_404');
  assert.doesNotMatch(chatFailureCopy(error, 'history'), /http_404|Graph Run|SENTINEL/);
});

test('unknown errors and malicious code never appear in chat, command, or reference copy', () => {
  for (const context of ['status', 'history', 'command_list', 'artifact_list', 'arguments', 'command', 'reference', 'approval'] as const) {
    const copy = chatFailureCopy(new Error(`处理失败 ${secret}`), context);
    assert.match(copy, /请|暂时不要/);
    assert.doesNotMatch(copy, /SENTINEL/);
    assert.doesNotMatch(chatFailureCopy({ code: `http_500_${secret}`, message: `内部错误 ${secret}` }, context), /SENTINEL/);
  }
  assert.equal(chatFailureCode({ code: `http_500_${secret}` }), undefined);
});

test('unknown write outcomes retain original-request reconciliation and never suggest automatic resubmission', () => {
  const error = { code: 'http_503', recovery: 'manual_reconcile', message: secret };
  assert.match(chatFailureCopy(error, 'command'), /原请求.*核对.*勿再次提交/);
  assert.match(chatFailureCopy({ code: 'command_in_progress', message: secret }, 'approval'), /原请求.*核对.*勿再次提交/);
  assert.match(chatFailureCopy({ code: 'http_400', recovery: 'retry_same_idempotency_key', message: secret }, 'reference'), /原请求.*核对.*勿再次提交/);
  assert.doesNotMatch(chatFailureCopy(error, 'command'), /SENTINEL|重新提交|重试/);
});

test('known authorization and validation failures keep fixed next steps without raw message', () => {
  assert.match(chatFailureCopy({ code: 'permission_denied', message: secret }, 'command'), /检查授权目标/);
  assert.match(chatFailureCopy({ code: 'request_validation_failed', message: secret }, 'arguments'), /检查所选目标和输入/);
  assert.doesNotMatch(chatFailureCopy({ code: 'permission_denied', message: secret }, 'command'), /SENTINEL/);
});

test('status banner keeps whitelisted model diagnosis without exposing provider exception text', () => {
  const error = {
    code: 'agent_failed',
    message: secret,
    detail: { provider_failure: { stage: 'inference_transport', category: 'proxy_error' } },
  };
  assert.match(chatFailureCopy(error, 'status'), /代理连接失败.*检查代理设置/);
  assert.doesNotMatch(chatFailureCopy(error, 'status'), /SENTINEL/);
});
