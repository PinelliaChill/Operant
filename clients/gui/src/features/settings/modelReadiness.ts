interface ReadinessSetup {
  ready: boolean;
  default_model_profile_id?: string | null;
  models?: { id: string; name: string; model_id: string }[];
}
interface ReadinessConnection { connection_id: string; status: string; error?: string | null; profile_ids?: string[] }
export function modelReadiness(setup: ReadinessSetup | null | undefined, connections: ReadinessConnection[], unavailable: boolean) {
  const profile = setup?.models?.find((model) => model.id === setup.default_model_profile_id);
  const owner = connections.find((connection) => connection.profile_ids?.includes(setup?.default_model_profile_id || ''));
  const healthy = (connection: ReadinessConnection) => ['connected', 'ready'].includes(connection.status) && !connection.error;
  return {
    profile, connectionId: owner?.connection_id,
    serviceConnected: !unavailable && (connections.some(healthy) || Boolean(profile && !owner)),
    modelSelected: !unavailable && Boolean(profile),
    ready: !unavailable && Boolean(setup?.ready && profile && (!owner || healthy(owner))),
  };
}
