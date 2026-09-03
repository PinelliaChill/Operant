import assert from 'node:assert/strict';
import test from 'node:test';
import {
  Phase45LiveAdapter,
  mapMcpServer,
  mapMcpTools,
  mapPolicyEvaluation,
  mapSecurityAudit,
  mapSkillDiscovery,
  mapSkillList,
} from '../src/live45/phase45Adapter.ts';

const skill = {
  candidate_id: 'candidate-1',
  root_ref: 'configured-root',
  relative_directory: 'demo',
  name: 'demo',
  description: 'safe metadata',
  manifest_sha256: 'a'.repeat(64),
  trust_status: 'untrusted_candidate',
  discovered_at: '2026-09-03T00:00:00Z',
  resources: [{
    relative_path: 'scripts/main.py',
    kind: 'script',
    size_bytes: 12,
    sha256: 'b'.repeat(64),
  }],
};

test('Skill mapping preserves untrusted status and safe relative paths', () => {
  const listed = mapSkillList({ items: [skill] });
  assert.equal(listed[0].trustStatus, 'untrusted_candidate');
  assert.equal(listed[0].rootRef, 'configured-root');
  assert.equal(listed[0].resources[0].relativePath, 'scripts/main.py');
  const discovered = mapSkillDiscovery({
    candidates: [skill],
    issues: [{ root_index: 0, relative_directory: 'bad', code: 'candidate_rejected', message: 'rejected' }],
  });
  assert.equal(discovered.issues[0].code, 'candidate_rejected');
  assert.throws(
    () => mapSkillList({ items: [{ ...skill, trust_status: 'trusted' }] }),
    /信任状态/,
  );
});

test('MCP mapping retains lifecycle and tool snapshot metadata', () => {
  const server = mapMcpServer({
    server_id: 'local',
    transport: 'stdio',
    stdio_argv: ['node', 'server.js'],
    environment_refs: {},
    allow_loopback_http: false,
    lifecycle_status: 'running',
    created_at: 'x',
    updated_at: 'y',
  });
  assert.equal(server.lifecycle, 'running');
  assert.deepEqual(server.stdioArgv, ['node', 'server.js']);
  assert.equal(mapMcpTools({ items: [{ name: 'read', description: 'Read data' }] })[0].name, 'read');
  assert.throws(
    () => mapMcpServer({ ...server, server_id: 'broken', transport: 'websocket' }),
    /transport/,
  );
});

test('Policy mapping retains DENY and remediation evidence', () => {
  const result = mapPolicyEvaluation({
    evaluation: {
      action_hash: 'd'.repeat(64),
      decision: 'deny',
      reason_code: 'system.hard',
      risk_level: 'critical',
      hard_deny: true,
      matched_rule_ids: ['system-hard-deny'],
    },
    remediation: { alternatives: [] },
  });
  assert.equal(result.decision, 'deny');
  assert.equal(result.hardDeny, true);
  assert.deepEqual(result.matchedRuleIds, ['system-hard-deny']);
  assert.equal(result.actionHash, 'd'.repeat(64));
});

test('Security audit mapping keeps only the explicit safe projection fields', () => {
  const facts = mapSecurityAudit({ items: [{
    event_id: 'audit-1',
    cursor: '9007199254740993',
    action_hash: 'e'.repeat(64),
    principal: 'gui:user',
    event_type: 'policy.evaluated',
    decision: 'ask',
    rule_ids: ['secret.ask'],
    detail: { raw_argument: 'must-not-map', secret_ref: 'MUST_NOT_MAP' },
    created_at: '2026-09-03T00:00:00Z',
  }] });
  assert.equal(facts[0].cursor, 9007199254740993n);
  assert.equal(facts[0].decision, 'ask');
  assert.equal('detail' in facts[0], false);
  assert.equal('actionHash' in facts[0], false);
});

test('adapter delegates to the generated Phase45 client', async () => {
  let negotiated = false;
  let auditOptions: unknown;
  const client = {
    negotiateProtocol: async (force: boolean) => {
      negotiated = force;
      return { protocol_version: 'phase45.v1' };
    },
    listSkills: async () => ({ items: [skill] }),
    listMcpServers: async () => ({ items: [] }),
    listSecurityAudit: async (_hash: string, options: unknown) => {
      auditOptions = options;
      return { items: [] };
    },
  };
  const adapter = new Phase45LiveAdapter(client as never);
  await adapter.connect();
  assert.equal(negotiated, true);
  assert.equal((await adapter.listSkills())[0].id, 'candidate-1');
  assert.deepEqual(await adapter.listMcpServers(), []);
  assert.deepEqual(await adapter.listSecurityAudit('f'.repeat(64), 7, 25), []);
  assert.deepEqual(auditOptions, { afterCursor: 7, limit: 25 });
});
