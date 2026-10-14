import assert from 'node:assert/strict';
import test from 'node:test';
import { createMemoryRouter } from 'react-router-dom';
import { legacyRemoteRoute } from '../src/app/legacyRemoteRoute.ts';

test('old remote link opens the advanced settings section and keeps back navigation intact', async () => {
  const router = createMemoryRouter([{ path: '/', children: [
    { path: 'chat' },
    legacyRemoteRoute,
    { path: 'settings' },
  ] }], { initialEntries: ['/chat'] });
  try {
    await router.navigate('/remote');
    assert.equal(router.state.location.pathname, '/settings');
    assert.equal(new URLSearchParams(router.state.location.search).get('section'), 'advanced');
    assert.equal(new URLSearchParams(router.state.location.search).get('page'), 'remote');
    await router.navigate(-1);
    assert.equal(router.state.location.pathname, '/chat');
  } finally {
    router.dispose();
  }
});
