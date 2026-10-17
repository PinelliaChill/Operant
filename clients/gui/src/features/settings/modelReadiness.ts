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
  const unconfirmed = unavailable || !setup;
  const serviceConnected = !unconfirmed && (connections.some(healthy) || Boolean(profile && !owner));
  const modelSelected = !unconfirmed && Boolean(profile);
  const ready = !unconfirmed && Boolean(setup?.ready && profile && (!owner || healthy(owner)));
  return {
    unconfirmed,
    serviceLabel: unconfirmed ? '待确认' : serviceConnected ? '已连接' : '待连接',
    selectionLabel: unconfirmed ? '待确认' : modelSelected ? '已选择' : '待选择',
    conversationLabel: unconfirmed ? '待确认' : ready ? '可开始对话' : '尚未就绪',
    profile, connectionId: owner?.connection_id,
    serviceConnected,
    modelSelected,
    ready,
  };
}
