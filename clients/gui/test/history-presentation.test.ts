import assert from 'node:assert/strict';
import test from 'node:test';
import { systemEventLabel, toolActionLabel, toolResultLabel } from '../src/features/chat/historyPresentation.ts';

test('common tool activity is summarized without technical names', () => {
  assert.equal(toolActionLabel('read_file'), '读取文件');
  assert.equal(toolActionLabel('apply_patch'), '修改文件');
  assert.equal(toolActionLabel('run_command'), '运行命令');
  assert.equal(toolResultLabel('read_file completed', 'completed'), '读取文件已完成');
  assert.equal(toolResultLabel('apply_patch failed', 'failed'), '修改文件失败');
  assert.equal(toolResultLabel('run_command result unknown', 'outcome_unknown'), '运行命令结果待核对');
});

test('system events use short Chinese labels, leaving raw summaries for details', () => {
  assert.equal(systemEventLabel('agent.started', 'agent.started'), '开始处理');
  assert.equal(systemEventLabel('tool.completed', 'tool.completed'), '工具已完成');
  assert.equal(systemEventLabel('agent.timed_out', 'agent.timed_out'), '任务超时');
  assert.equal(systemEventLabel('tool.started', '/skill:skill_UUID'), '技能开始');
  assert.equal(systemEventLabel('tool.failed', '/skill:skill_UUID failed'), '技能执行失败');
  assert.equal(systemEventLabel('unknown.event', 'opaque code'), '运行记录');
});
