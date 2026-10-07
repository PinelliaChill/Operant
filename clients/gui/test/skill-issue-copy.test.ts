import assert from 'node:assert/strict';
import test from 'node:test';
import { skillIssueCopy } from '../src/features/settings/skillIssueCopy.ts';

test('skill source issues give concrete Chinese repair steps', () => {
  assert.deepEqual(skillIssueCopy('lark-approval: frontmatter structure exceeds limit'), { skillName: 'lark-approval', guidance: '元数据过深或过于复杂，请检查 SKILL.md 字段。' });
  assert.match(skillIssueCopy('demo: Skill link target is outside registered roots').guidance, /添加实际父目录/);
  assert.match(skillIssueCopy('demo: malformed or unsafe YAML').guidance, /修复 SKILL.md 开头的 YAML/);
  assert.match(skillIssueCopy('unknown format').guidance, /部分技能无法读取/);
  assert.match(skillIssueCopy('目录不存在或不可读取').guidance, /选择实际存在的目录/);
});
