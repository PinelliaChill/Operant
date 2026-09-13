import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

async function source(path: string): Promise<string> {
  return readFile(new URL(path, import.meta.url), 'utf8');
}

test('B2-3 Live entrypoints use the management view while Demo stays separate', async () => {
  const [management, projects, settings, skills, context, extensions] = await Promise.all([
    source('../src/features/management/LiveManagementView.tsx'),
    source('../src/features/projects/ProjectsView.tsx'),
    source('../src/features/settings/SettingsView.tsx'),
    source('../src/features/skills/SkillsView.tsx'),
    source('../src/context/ClientContext.tsx'),
    source('../src/features/extensions/ExtensionsView.tsx'),
  ]);
  assert.match(management, /useOperant\(\)/);
  assert.match(management, /b23Client/);
  assert.doesNotMatch(management, /useDemo|MockClient|DemoProvider/);
  assert.match(context, /new B23Client\(currentBrowserOrigin\(\)\)/);
  assert.match(projects, /<LiveManagementView initialTab="projects" \/>/);
  assert.match(settings, /<LiveManagementView initialTab=\{managementTab\} \/>/);
  assert.match(skills, /<LiveManagementView initialTab="skills" \/>/);
  assert.match(extensions, /searchParams\.get\('tab'\) === 'plugins'/);
});

test('B2-3 management UI exposes the required command families and unknown-result guard', async () => {
  const management = await source('../src/features/management/LiveManagementView.tsx');
  for (const action of [
    'project_create', 'project_update', 'project_archive', 'project_detach',
    'memory_switch', 'memory_save', 'memory_search', 'memory_propose', 'memory_confirm', 'memory_deactivate', 'memory_migrate',
    'plugin_install', 'plugin_enable', 'plugin_disable', 'plugin_uninstall', 'plugin_configure', 'binding_select',
    'dataset_export', 'dataset_delete', 'cleanup_resume', 'skill_discover', 'skill_install', 'skill_disable', 'skill_enable', 'skill_uninstall',
    'artifact_pin', 'artifact_archive', 'artifact_schedule', 'artifact_trash', 'artifact_restore', 'artifact_audit',
  ]) assert.match(management, new RegExp(action));
  assert.match(management, /未知写结果/);
  assert.match(management, /role="alert"/);
  assert.match(management, /aria-live="polite"/);
  assert.match(management, /requestEpoch/);
  assert.match(management, /command\.action === 'memory_search'/);
  assert.doesNotMatch(management, /action: 'memory_propose'[^}]*confirmed/);
  assert.match(management, /保留与审计/);
  assert.match(management, /artifact\.blocked/);
});

test('B2-3 lifecycle UI only exposes cleanup controls for active cleanup states', async () => {
  const management = await source('../src/features/management/LiveManagementView.tsx');
  assert.match(management, /cleanupPending = dataset\.state === 'deleting'/);
  assert.match(management, /cleanupBlocked = dataset\.state === 'blocked'/);
  assert.doesNotMatch(management, /dataset\.exceptions\.length > 0 && <div className="b2-memory-warning"/);
  assert.match(management, /!lifecycleClosed && configInstallation === installation\.installation_id/);
  assert.match(management, /!lifecycleClosed && uninstallTarget === installation\.installation_id/);
  assert.doesNotMatch(management, /已选择项目.*installation\.binding_id/);
  assert.match(management, /blocksB23Management\(detail\) \? 'error' : 'ready'/);
});
