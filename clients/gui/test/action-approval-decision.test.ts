import assert from 'node:assert/strict';
import test from 'node:test';
import { readApprovalDecision, submitApprovalDecision, type ApprovalDecisionClient } from '../src/lib/actionApprovalDecision.ts';

test('lost approval response resolves by the same original ID without repeating the decision', async () => {
  const calls: string[] = [];
  const client = {
    async decidePhase45Approval(id: string) { calls.push(`POST:${id}`); throw new Error('lost response'); },
    async getPhase45Approval(id: string) { calls.push(`GET:${id}`); return { approval_id: id, status: 'approved' }; },
  } as ApprovalDecisionClient;
  assert.equal(await submitApprovalDecision(client, 'approval-original', true), 'approved');
  assert.deepEqual(calls, ['POST:approval-original', 'GET:approval-original']);
});

test('pending or foreign approval never clears an unknown decision', async () => {
  let posts = 0;
  let reads = 0;
  const client = {
    async decidePhase45Approval() { posts += 1; throw new Error('lost response'); },
    async getPhase45Approval(id: string) { reads += 1; return { approval_id: reads === 1 ? id : 'foreign', status: reads === 1 ? 'pending' : 'approved' }; },
  } as ApprovalDecisionClient;
  assert.equal(await submitApprovalDecision(client, 'approval-original', false), null);
  assert.equal(await readApprovalDecision(client, 'approval-original'), null);
  assert.equal(posts, 1);
  assert.equal(reads, 2);
});
