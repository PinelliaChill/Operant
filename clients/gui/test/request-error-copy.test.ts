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
