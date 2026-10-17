import assert from 'node:assert/strict';
import test from 'node:test';
import { buildVersionLabel, type BuildInfo } from '../src/lib/buildInfo.ts';

const base: BuildInfo = { buildKind: 'packaged', productVersion: '0.1.0', releaseLabel: '2.0.0-beta.5', sourceCommit: 'a'.repeat(40), sourceDirty: false, candidateId: 'candidate', bundleIdentifier: 'test' };
test('release display uses package provenance and never turns development into a release', () => {
  assert.equal(buildVersionLabel(base), '2.0.0-beta.5');
  assert.match(buildVersionLabel({ ...base, releaseLabel: null }), /未标记发布版本.*0.1.0/);
  assert.match(buildVersionLabel({ ...base, buildKind: 'development' }), /开发构建/);
  assert.doesNotMatch(buildVersionLabel({ ...base, buildKind: 'development' }), /beta/);
});
