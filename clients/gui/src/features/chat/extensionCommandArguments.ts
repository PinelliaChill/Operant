export function parseExtensionArguments(text: string, schema?: Record<string, unknown>): Record<string, unknown> {
  const value: unknown = JSON.parse(text);
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('扩展命令参数必须是 JSON 对象。');
  const args = value as Record<string, unknown>;
  if (!schema) return args;
  const properties = schema.properties && typeof schema.properties === 'object' && !Array.isArray(schema.properties)
    ? schema.properties as Record<string, unknown> : {};
  const required = Array.isArray(schema.required) ? schema.required.filter((item): item is string => typeof item === 'string') : [];
  for (const key of required) if (!Object.hasOwn(args, key)) throw new Error(`缺少必填参数：${key}。`);
  for (const [key, item] of Object.entries(args)) {
    const property = properties[key];
    if (!property) {
      if (schema.additionalProperties === false) throw new Error(`不支持参数：${key}。`);
      continue;
    }
    if (property && typeof property === 'object' && !Array.isArray(property)) {
      const rule = property as Record<string, unknown>;
      const expected = rule.type;
      if (expected === 'string' && typeof item !== 'string') throw new Error(`${key} 必须是文本。`);
      if (typeof item === 'string') {
        if (typeof rule.minLength === 'number' && item.length < rule.minLength) throw new Error(`${key} 至少需要 ${rule.minLength} 个字符。`);
        if (typeof rule.maxLength === 'number' && item.length > rule.maxLength) throw new Error(`${key} 最多允许 ${rule.maxLength} 个字符。`);
      }
      if (expected === 'number' && (typeof item !== 'number' || !Number.isFinite(item))) throw new Error(`${key} 必须是数字。`);
      if (expected === 'integer' && !Number.isInteger(item)) throw new Error(`${key} 必须是整数。`);
      if (expected === 'boolean' && typeof item !== 'boolean') throw new Error(`${key} 必须是布尔值。`);
    }
  }
  return args;
}
