import { redirect } from 'react-router-dom';

export const legacyRemoteRoute = {
  path: 'remote',
  loader: () => redirect('/settings?section=advanced&page=remote'),
};
