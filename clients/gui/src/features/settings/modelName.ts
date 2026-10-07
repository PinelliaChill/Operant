export function modelDisplayName(modelId: string, names?: Record<string, unknown>): string {
  const value = names?.[modelId];
  return typeof value === 'string' && value.trim() ? value.trim() : modelId;
}
