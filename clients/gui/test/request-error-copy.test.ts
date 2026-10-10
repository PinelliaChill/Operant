import assert from 'node:assert/strict';
import test from 'node:test';
import { requestErrorCopy } from '../src/lib/requestErrorCopy.ts';

test('request errors give recovery action without raw HTTP or English codes', () => {
  assert.match(requestErrorCopy({ code: 'transport_unavailable', message: 'fetch failed' }), /恢复连接.*核对/);
  assert.match(requestErrorCopy({ code: 'approval_required', message: 'http_409: approval_required' }), /人工确认/);
  assert.match(requestErrorCopy({ code: 'http_422', message: 'http_422: validation error' }), /检查/);
  assert.match(requestErrorCopy({ code: 'request_validation_failed', message: 'request validation failed' }), /检查/);
  assert.match(requestErrorCopy({ code: 'http_404', message: 'Graph Run not found' }), /目标记录未找到/);
  assert.match(requestErrorCopy({ code: 'observation_expired', message: 'observation expired' }), /重新查看/);
  assert.match(requestErrorCopy({ code: 'command_outcome_unknown', message: 'unknown' }), /勿再次提交/);
  assert.doesNotMatch(requestErrorCopy({ code: 'http_502', message: 'model discovery failed' }), /http_|model discovery/);
  assert.match(requestErrorCopy({ code: 'unmapped_error', message: 'internal failure' }), /刷新并核对结果/);
});

test('specific Chinese service guidance remains visible', () => {
  assert.equal(requestErrorCopy(new Error('目录不存在或不可读取')), '目录不存在或不可读取');
  assert.equal(requestErrorCopy(new Error('http_409: 目标已变化，请刷新')), '目标已变化，请刷新');
});

test('confirmed Gemini setup failures name the missing configuration and its entry', () => {
  const error = (message: string) => ({ code: 'http_400', recovery: 'none', message });
  assert.match(requestErrorCopy(error('Google desktop client ID and Cloud project ID are required')), /项目 ID.*高级选项.*客户端/);
  assert.match(requestErrorCopy(error('Google desktop client secret is required')), /缺少.*密钥.*高级选项/);
  assert.match(requestErrorCopy(error('Google OAuth configuration is invalid')), /配置无效.*检查/);
  assert.match(requestErrorCopy(error('Google Cloud project does not match this connection')), /项目.*不一致.*原项目/);
  assert.match(requestErrorCopy(error('Google OAuth client does not match this connection')), /客户端.*不一致.*原客户端/);
});

test('unknown model setup writes retain reconciliation guidance instead of a configuration retry hint', () => {
  const message = 'Google desktop client secret is required';
  assert.match(requestErrorCopy({ code: 'command_outcome_unknown', recovery: 'manual_reconcile', message }), /核对.*勿再次提交/);
  assert.match(requestErrorCopy({ code: 'http_400', recovery: 'retry_same_idempotency_key', message }), /核对.*勿再次提交/);
  assert.doesNotMatch(requestErrorCopy({ code: 'http_502', recovery: 'none', message }), /高级选项|客户端密钥/);
});

test('terminal model diagnostics show a concrete next step while unsafe or old payloads stay generic', () => {
  const diagnostic = {
    code: 'agent_failed',
    message: '模型连接中断，请检查网络连接。',
    detail: { provider_failure: { stage: 'inference_transport', category: 'network_error' } },
  };
  assert.equal(requestErrorCopy(diagnostic), '模型连接中断，请检查网络连接。');
  assert.equal(requestErrorCopy({ ...diagnostic, code: 'session_run_failed', detail: { provider_failure: { stage: 'inference_response', category: 'invalid_response' } } }), '模型响应格式异常，请稍后再试。');
  assert.match(requestErrorCopy({ code: 'agent_failed', message: 'ProviderError: raw token abc', detail: { provider_failure: { stage: 'inference_transport', category: 'unknown' } } }), /任务运行失败.*检查模型连接/);
  assert.doesNotMatch(requestErrorCopy({ code: 'agent_failed', message: 'ProviderError: raw token abc' }), /ProviderError|abc|Core|Agent|Session/);
  assert.match(requestErrorCopy({ ...diagnostic, recovery: 'manual_reconcile' }), /核对.*勿再次提交/);
});

test('safe model HTTP failures give distinct actions without revealing status or suggesting payment', () => {
  const cases = [
    ['bad_request', /检查所选模型和高级参数/],
    ['authentication_required', /检查模型连接设置/],
    ['permission_denied', /检查账号、项目或模型的访问权限/],
    ['rate_limited', /稍后再试.*用量限制/],
    ['provider_unavailable', /模型服务暂时不可用/],
    ['http_error', /查看服务状态/],
  ] as const;
  for (const [category, guidance] of cases) {
    const visible = requestErrorCopy({
      code: 'agent_failed',
      message: 'raw HTTP 429 billing token',
      detail: { error_type: 'ProviderError', provider_failure: { stage: 'inference_response', category }, failure_http_status: 429 },
    });
    assert.match(visible, guidance);
    assert.doesNotMatch(visible, /HTTP|429|ProviderError|token|充值|付费|重新登录/);
  }
  assert.match(requestErrorCopy({ code: 'session_run_failed', detail: { provider_failure: { stage: 'inference_transport', category: 'bad_request' } } }), /任务启动失败/);
  assert.match(requestErrorCopy({ code: 'agent_failed', recovery: 'manual_reconcile', detail: { provider_failure: { stage: 'inference_response', category: 'rate_limited' } } }), /核对.*勿再次提交/);
});

test('unsupported Gemini API snapshot asks for a newly selected model without pretending an HTTP response', () => {
  const detail = { error_type: 'ProviderError', provider_failure: { stage: 'inference_response', category: 'unsupported_api_version' } };
  const visible = requestErrorCopy({ code: 'agent_failed', message: 'untrusted detail', detail });
  assert.equal(visible, '当前模型配置不支持这些工具。请重新选择模型后新建对话。');
  assert.doesNotMatch(visible, /HTTP|400|ProviderError|重新登录|充值|付费/);
  for (const invalid of [
    { stage: 'inference_transport', category: 'unsupported_api_version' },
    { stage: 'inference_response', category: 'unsupported_api_version', http_status: 400 },
  ]) {
    assert.match(requestErrorCopy({ code: 'agent_failed', detail: { provider_failure: invalid } }), /任务运行失败/);
  }
  assert.match(requestErrorCopy({ code: 'agent_failed', recovery: 'manual_reconcile', detail }), /核对.*勿再次提交/);
});

test('terminal status copy cannot leak raw cancellation, timeout, or budget reasons', () => {
  for (const code of ['agent_cancelled', 'agent_timed_out', 'budget_exhausted', 'agent_no_progress', 'agent_max_turns']) {
    const copy = requestErrorCopy({ code, message: 'Core Agent raw reason secret_ref', detail: { reason: 'private' } });
    assert.match(copy, /^任务/);
    assert.doesNotMatch(copy, /Core|Agent|Session|raw|private|secret_ref/);
  }
});
