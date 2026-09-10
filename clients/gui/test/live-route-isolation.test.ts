import assert from 'node:assert/strict';
import test from 'node:test';
import { resolveLiveRouteSupport } from '../src/live/liveRouteSupport.ts';

test('resolveLiveRouteSupport permits all standard Live routes', () => {
  const supportedRoutes = [
    '/',
    '/chat',
    '/chat/thread_live_001',
    '/tasks',
    '/agents',
    '/runs/run_live_001',
    '/projects',
    '/projects/proj_alpha',
    '/approvals',
    '/schedules',
    '/collab',
    '/collab/wf_live_overview',
    '/skills',
    '/extensions',
    '/settings',
    '/remote',
    '/remote/',
    '/session',
    '/session/',
  ];

  for (const route of supportedRoutes) {
    const result = resolveLiveRouteSupport(route);
    assert.equal(result.isSupported, true, `Route ${route} should be supported in Live mode`);
  }
});

test('resolveLiveRouteSupport strictly intercepts nested /collab/:wfId/canvas demo routes', () => {
  const nestedCanvasRoutes = [
    '/collab/wf-1/canvas',
    '/collab/wf-alpha/canvas',
    '/collab/12345/canvas',
    '/collab/any/nested/canvas',
  ];

  for (const route of nestedCanvasRoutes) {
    const result = resolveLiveRouteSupport(route);
    assert.equal(
      result.isSupported,
      false,
      `Nested canvas route ${route} must NOT be supported in Live mode`,
    );
    assert.equal(
      result.unavailableSection,
      'collab_canvas',
      `Nested canvas route ${route} must map to collab_canvas section`,
    );
  }
});

test('resolveLiveRouteSupport strictly intercepts legacy demo-only routes in Live mode', () => {
  const demoOnlyRoutes: Record<string, string> = {
    '/tasks/task-001': 'tasks',
    '/agents/coder_agent': 'agents',
    '/runs': 'runs',
    '/runs/run_operant_002/details': 'runs',
    '/workflow': 'workflow',
    '/workflow/wf-1/session/s-1': 'workflow',
    '/workflow/session/s-2': 'workflow',
    '/non-existent-subpath': 'non-existent-subpath',
    '/remote/unknown': 'remote',
    '/session/unknown': 'session',
  };

  for (const [route, expectedSection] of Object.entries(demoOnlyRoutes)) {
    const result = resolveLiveRouteSupport(route);
    assert.equal(
      result.isSupported,
      false,
      `Legacy demo route ${route} must NOT be supported in Live mode`,
    );
    assert.equal(
      result.unavailableSection,
      expectedSection,
      `Legacy demo route ${route} must map to ${expectedSection}`,
    );
  }
});
