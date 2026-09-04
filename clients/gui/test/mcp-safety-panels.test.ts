import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const source = readFileSync(
  new URL('../src/features/extensions/McpSafetyPanels.tsx', import.meta.url),
  'utf8',
);
const styles = readFileSync(
  new URL('../src/features/extensions/live-extensions.css', import.meta.url),
  'utf8',
);

test('approval component keeps manual decisions keyboard reachable and disconnect-safe', () => {
  assert.match(source, /type="button"/);
  assert.match(source, /aria-label="允许 MCP 操作并使用同一幂等键重试"/);
  assert.match(source, /aria-label="拒绝 MCP 操作"/);
  assert.match(source, /disabled=\{busy \|\| disconnected\}/);
  assert.match(source, /审批 ID/);
  assert.match(source, /原因/);
});

test('unknown outcome component explicitly forbids automatic replay and exposes receipt lookup', () => {
  assert.match(source, /客户端不会自动重放/);
  assert.match(source, /查看 Receipt/);
  assert.match(source, /receipt\.status/);
  assert.match(source, /resultAvailable/);
});

test('MCP controls stay reachable at wide and narrow widths', () => {
  assert.match(styles, /@media \(max-width: 600px\)/);
  assert.match(styles, /min-height: 44px/);
  assert.match(styles, /flex: 1 1 132px/);
  assert.match(styles, /grid-template-columns: 1fr/);
  assert.match(styles, /font-size: 16px/);
});
