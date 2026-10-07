import assert from 'node:assert/strict';
import test from 'node:test';
import { graphRunHref, graphRunIdFromSearch } from '../src/features/collab/graphRunLink.ts';
import { canDiscoverConnectionModels, canReauthorizeModelConnection, collaborationStatusLabel, localStateLabel, modelConnectionErrorLabel, modelConnectionStatusLabel, oauthStatusLabel, offersApiKeyAlternative } from '../src/lib/statusCopy.ts';

test('team run link addresses the Graph view and preserves the exact run id', () => {
  const id = 'graph_run/with space';
  const href = graphRunHref(id);
  assert.equal(href, '/collab?view=runs&graphRunId=graph_run%2Fwith%20space');
  assert.equal(graphRunIdFromSearch(new URL(href, 'http://localhost').searchParams), id);
  assert.equal(graphRunIdFromSearch(new URLSearchParams('view=home&graphRunId=graph_run')), '');
});

test('common OAuth, collaboration and local states are understandable', () => {
  assert.equal(oauthStatusLabel('cancelled'), '登录已取消');
  assert.equal(oauthStatusLabel('expired'), '登录已过期，请重新连接');
  assert.equal(collaborationStatusLabel('running'), '协作运行中');
  assert.equal(collaborationStatusLabel('completed'), '协作已完成');
  assert.equal(localStateLabel('enabled'), '已启用');
  assert.equal(localStateLabel('failed'), '失败，请查看详情');
});

test('OAuth connection failures use distinct recovery actions', () => {
  assert.match(modelConnectionErrorLabel('chatgpt_plan_usage_disabled'), /已登录.*套餐调用.*重新授权.*API/);
  assert.match(modelConnectionErrorLabel('user_not_eligible'), /没有套餐调用资格/);
  assert.match(modelConnectionErrorLabel('permission_denied'), /未允许此账号或密钥访问.*项目设置/);
  assert.match(modelConnectionErrorLabel('usage_unavailable'), /登录状态已保留.*稍后/);
  assert.equal(canReauthorizeModelConnection('error', 'chatgpt_plan_usage_disabled'), true);
  assert.equal(canDiscoverConnectionModels('error', 'chatgpt_plan_usage_disabled'), false);
  assert.equal(modelConnectionStatusLabel('error', 'chatgpt_plan_usage_disabled'), '已登录，需授权套餐');
  for (const code of ['user_not_eligible', 'permission_denied', 'usage_unavailable']) {
    assert.equal(canReauthorizeModelConnection('error', code), false);
    assert.equal(canDiscoverConnectionModels('error', code), true);
  }
  assert.equal(canReauthorizeModelConnection('needs_auth', 'authentication_required'), true);
  assert.equal(canDiscoverConnectionModels('needs_auth', 'authentication_required'), false);
  assert.equal(offersApiKeyAlternative('permission_denied'), false);
  assert.equal(offersApiKeyAlternative('usage_unavailable'), false);
  assert.equal(modelConnectionStatusLabel('error', 'usage_unavailable'), '用量暂不可查');
  assert.match(modelConnectionErrorLabel('unsupported_capability'), /本模型不支持当前请求参数.*高级参数/);
  assert.match(modelConnectionErrorLabel('revocation_unconfirmed'), /重试断开.*不要重新登录/);
  assert.equal(canReauthorizeModelConnection('error', 'revocation_unconfirmed'), false);
  assert.equal(canReauthorizeModelConnection('needs_auth', 'revocation_unconfirmed'), false);
  assert.equal(canDiscoverConnectionModels('error', 'revocation_unconfirmed'), false);
  assert.doesNotMatch(modelConnectionErrorLabel('unknown_internal_code'), /unknown_internal_code/);
});
