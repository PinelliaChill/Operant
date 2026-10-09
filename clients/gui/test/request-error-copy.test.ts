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
