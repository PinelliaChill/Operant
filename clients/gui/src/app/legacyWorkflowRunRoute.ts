import { redirect, type LoaderFunctionArgs } from 'react-router-dom';

export const legacyWorkflowRunRoute = {
  path: 'workflow/:id',
  loader: ({ params }: LoaderFunctionArgs) => {
    if (!params.id) throw new Response('Workflow ID missing', { status: 404 });
    const query = new URLSearchParams({ view: 'runs', legacyWorkflowRunId: params.id });
    return redirect(`/collab?${query}`);
  },
};
