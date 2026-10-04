import assert from 'node:assert/strict';
import test from 'node:test';
import { parseExtensionArguments } from '../src/features/chat/extensionCommandArguments.ts';

const schema = {
  type: 'object',
  properties: { prompt: { type: 'string' }, count: { type: 'integer' } },
  required: ['prompt'],
  additionalProperties: false,
};

test('extension command input follows the discovered parameter contract', () => {
  assert.deepEqual(parseExtensionArguments('{"prompt":"hello","count":2}', schema), { prompt: 'hello', count: 2 });
  assert.throws(() => parseExtensionArguments('{}', schema), /缺少必填参数/);
  assert.throws(() => parseExtensionArguments('{"prompt":3}', schema), /必须是文本/);
  assert.throws(() => parseExtensionArguments('{"prompt":"ok","extra":true}', schema), /不支持参数/);
});

test('Skill prompt honors the discovered length and closed object contract', () => {
  const skillSchema = {
    type: 'object',
    properties: { prompt: { type: 'string', minLength: 1, maxLength: 4000 } },
    required: ['prompt'],
    additionalProperties: false,
  };
  assert.deepEqual(parseExtensionArguments('{"prompt":"请总结这一段"}', skillSchema), { prompt: '请总结这一段' });
  assert.throws(() => parseExtensionArguments('{"prompt":""}', skillSchema), /至少需要/);
  assert.throws(() => parseExtensionArguments(JSON.stringify({ prompt: 'a'.repeat(4001) }), skillSchema), /最多允许/);
  assert.throws(() => parseExtensionArguments('{"prompt":"ok","extra":true}', skillSchema), /不支持参数/);
});
