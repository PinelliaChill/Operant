import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import {
  exactProposalKey,
  exactRelationshipTarget,
  mergeHistoryPages,
  normalizeB25Error,
  selectionSnapshot,
  snapshotExactProposal,
} from '../src/features/management/b25-state.ts';

const proposal = {
  proposal_id: 'proposal-1',
  proposal_revision: 7,
  proposed_version: {
    dataset_id: 'dataset-1',
    record_id: 'record-1',
    version: 3,
    content_digest: 'digest-3',
  },
  base_head_revision: 12,
};

const state = {
  project_id: 'project-1',
  enabled: true,
  records: [],
  proposals: [{
    proposal: {
      ...proposal,
      owner: {},
      operation: 'modify',
      base_head: { revision: 12 },
      source_refs: [],
      extractor_version: 'manual',
      reason: '用户纠正',
      state: 'pending',
    },
    version: {},
    current_head: {},
    independent_evidence_count: 1,
    relationships: [],
  }],
  jobs: [],
};

test('B2-5 selection snapshot carries the complete proposal/version/CAS identity', () => {
  const copy = snapshotExactProposal(proposal);
  assert.deepEqual(copy, proposal);
  assert.notEqual(copy, proposal);
  assert.notEqual(copy.proposed_version, proposal.proposed_version);
  assert.equal(exactProposalKey(copy), 'proposal-1\u00007\u0000dataset-1\u0000record-1\u00003\u0000digest-3\u000012');
});

test('B2-5 selection rejects a stale revision or digest instead of replacing it with current state', () => {
  const stale = {
    ...proposal,
    proposal_revision: 6,
    proposed_version: { ...proposal.proposed_version, content_digest: 'old-digest' },
  };
  assert.deepEqual(selectionSnapshot([stale], state), []);
  assert.deepEqual(selectionSnapshot([proposal], state), [proposal]);
});

test('B2-5 history pagination keeps the original cutoff and deduplicates item ids', () => {
  const first = {
    project_id: 'project-1', perspective: 'historical_fact', cutoff_cursor: 42,
    next_cursor: 2, items: [{ item_id: 'item-1', cursor: 1 }],
  };
  const second = {
    project_id: 'project-1', perspective: 'historical_fact', cutoff_cursor: 42,
    next_cursor: null, items: [{ item_id: 'item-1', cursor: 1 }, { item_id: 'item-2', cursor: 2 }],
  };
  const merged = mergeHistoryPages(first, second);
  assert.equal(merged.cutoff_cursor, 42);
  assert.deepEqual(merged.items.map((item) => item.item_id), ['item-1', 'item-2']);
  assert.throws(
    () => mergeHistoryPages(first, { ...second, cutoff_cursor: 43 }),
    /cutoff/,
  );
});

test('B2-5 errors classify unknown writes and unsupported interfaces explicitly', () => {
  const unknown = normalizeB25Error({ code: 'transport_unavailable', message: 'offline' }).detail;
  assert.equal(unknown.outcomeUnknown, true);
  assert.equal(unknown.unsupported, false);
  const unsupported = normalizeB25Error({ code: 'operation_not_supported', message: 'missing' }).detail;
  assert.equal(unsupported.unsupported, true);
  assert.equal(unsupported.outcomeUnknown, false);
});

test('knowledge management routes B2-5 through the advanced panel and removes legacy confirmation bypass', async () => {
  const source = await readFile(new URL('../src/features/management/LiveManagementView.tsx', import.meta.url), 'utf8');
  assert.match(source, /<B25GovernancePanel/);
  assert.match(source, /高级知识治理/);
  assert.doesNotMatch(source, /execute\(\{\s*action:\s*'memory_confirm'/);
  assert.match(source, /b25Client/);
});

test('relationship targets preserve the selected published digest and reject stale selection', () => {
  const ref = { dataset_id: 'dataset', record_id: 'record', version: 1, content_digest: 'a'.repeat(64) };
  const records = [{ version: { ref }, head: { state: 'published' } }] as Parameters<typeof exactRelationshipTarget>[1];
  assert.deepEqual(exactRelationshipTarget(ref, records), ref);
  assert.notEqual(exactRelationshipTarget(ref, records), ref);
  assert.equal(exactRelationshipTarget({ ...ref, content_digest: '' }, records), null);
  assert.equal(exactRelationshipTarget({ ...ref, version: 2 }, records), null);
  assert.equal(exactRelationshipTarget(null, records), null);
  records[0].head.state = 'inactive';
  assert.equal(exactRelationshipTarget(ref, records), null);
});
