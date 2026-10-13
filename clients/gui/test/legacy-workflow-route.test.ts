import assert from 'node:assert/strict';
import test from 'node:test';
import { createMemoryRouter } from 'react-router-dom';
import { legacyWorkflowRunRoute } from '../src/app/legacyWorkflowRunRoute.ts';
import { resolveLiveRouteSupport } from '../src/live/liveRouteSupport.ts';

test('legacy Workflow links retain the exact run ID for formal Graph lookup', async () => {
  const router = createMemoryRouter([{ path: '/', children: [
    { path: 'chat' },
    legacyWorkflowRunRoute,
    { path: 'collab' },
  ] }], { initialEntries: ['/chat'] });
  try {
    for (const [path, id] of [['/workflow/wf-1', 'wf-1'], ['/workflow/wf%2Fpart', 'wf/part']]) {
      assert.equal(resolveLiveRouteSupport(path).isSupported, true);
      await router.navigate(path);
      assert.equal(router.state.location.pathname, '/collab');
      const query = new URLSearchParams(router.state.location.search);
      assert.equal(query.get('view'), 'runs');
      assert.equal(query.get('legacyWorkflowRunId'), id);
    }
    assert.equal(resolveLiveRouteSupport('/workflow/wf-1/session/s-1').isSupported, false);
  } finally {
    router.dispose();
  }
});
