export function graphRunHref(graphRunId: string): string {
  return `/collab?view=runs&graphRunId=${encodeURIComponent(graphRunId)}`;
}

export function graphRunIdFromSearch(params: URLSearchParams): string {
  return params.get('view') === 'runs' ? params.get('graphRunId')?.trim() || '' : '';
}
