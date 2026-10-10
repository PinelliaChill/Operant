import { readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';

const root = resolve(import.meta.dirname, '../../..');
const schemas = [
  'operant-onboarding.openapi.json',
  'operant-phase56.openapi.json',
  'operant-beta.openapi.json',
];
const protectedPrefixes = [
  '/v1/setup', '/v1/local-control', '/v1/remote-control', '/v1/extensions',
  '/v1/writer-workspaces', '/v1/writer-conflicts', '/v1/merge-runs',
];
const graphManagementPath = /^\/v1\/graph\/runs\/\{run_id\}\/(?:writer-workspaces|writer-artifacts|writer-conflicts|merge-runs)(?:\/|$)/;
const workbenchManagementPath = /^\/v1\/workbench\/(?:extensions\/commands|threads\/\{thread_id\}\/(?:extension-commands|skill-commands))$/;
const deviceAuth = new Set(['submitRemoteCommand', 'pairRemoteDevice', 'queryRemoteSessionResult']);
const operations = [];

for (const filename of schemas) {
  const schema = JSON.parse(readFileSync(resolve(root, 'sdk/protocol/schema', filename), 'utf8'));
  for (const [pathTemplate, methods] of Object.entries(schema.paths)) {
    if (!protectedPrefixes.some((prefix) => pathTemplate === prefix || pathTemplate.startsWith(`${prefix}/`))
      && !graphManagementPath.test(pathTemplate) && !workbenchManagementPath.test(pathTemplate)) continue;
    for (const [method, spec] of Object.entries(methods)) {
      if (!spec || typeof spec !== 'object' || typeof spec.operationId !== 'string') continue;
      operations.push({ operationId: spec.operationId, method: method.toUpperCase(), pathTemplate, deviceAuth: deviceAuth.has(spec.operationId) });
    }
  }
}

operations.sort((a, b) => a.operationId.localeCompare(b.operationId));
const duplicate = operations.find((item, index) => operations.findIndex((other) => other.operationId === item.operationId) !== index);
if (duplicate) throw new Error(`duplicate operation ID: ${duplicate.operationId}`);
for (const id of deviceAuth) if (!operations.some((item) => item.operationId === id)) throw new Error(`missing device-auth operation: ${id}`);
const output = `// Generated from the three frozen OpenAPI schemas by scripts/generate-native-core-routes.mjs.\n// Do not hand-edit this route table.\nexport const nativeCoreRoutes = ${JSON.stringify(operations, null, 2)} as const;\n`;
const target = resolve(root, 'clients/gui/src/lib/nativeCoreRoutes.generated.ts');
if (process.argv.includes('--check')) {
  if (readFileSync(target, 'utf8') !== output) throw new Error('native Core route table is out of date');
} else {
  writeFileSync(target, output);
}
