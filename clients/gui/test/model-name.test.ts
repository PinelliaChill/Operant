import assert from 'node:assert/strict';
import test from 'node:test';
import { modelDisplayName } from '../src/features/settings/modelName.ts';

test('model options show server names while keeping the exact model id', () => {
  assert.equal(modelDisplayName('slug-1', { 'slug-1': '友好模型名' }), '友好模型名');
  assert.equal(modelDisplayName('slug-1', { 'slug-1': '' }), 'slug-1');
  assert.equal(modelDisplayName('slug-1', {}), 'slug-1');
});
