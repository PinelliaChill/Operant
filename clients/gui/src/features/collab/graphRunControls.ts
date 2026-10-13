import type { GraphRunStatus } from '../../../../../sdk/typescript-client/phase23.generated';

const CANCELLABLE: ReadonlySet<GraphRunStatus> = new Set([
  'created', 'queued', 'running', 'waiting_input', 'waiting_approval', 'interrupted',
]);

export function graphRunControlAvailability(
  status: GraphRunStatus | null,
  connected: boolean,
  busy: boolean,
): { canResume: boolean; canCancel: boolean } {
  if (!status || !connected || busy) return { canResume: false, canCancel: false };
  return { canResume: status === 'interrupted', canCancel: CANCELLABLE.has(status) };
}
