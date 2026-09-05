import { readdir, stat } from 'node:fs/promises';
import { join } from 'node:path';

const assetDirectory = new URL('../dist/assets/', import.meta.url);
const limitBytes = 500 * 1024;
const oversized = [];

for (const name of await readdir(assetDirectory)) {
  if (!name.endsWith('.js')) continue;
  const size = (await stat(join(assetDirectory.pathname, name))).size;
  if (size > limitBytes) oversized.push(`${name}: ${size} bytes`);
}

if (oversized.length > 0) {
  throw new Error(`JavaScript bundle budget exceeded (${limitBytes} bytes):\n${oversized.join('\n')}`);
}
