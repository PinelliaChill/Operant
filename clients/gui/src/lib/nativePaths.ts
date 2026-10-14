export type PathKind = 'directory' | 'file';
export class PathSelectionError extends Error {}

export function canBrowseLocalPaths(): boolean {
  return typeof window !== 'undefined'
    && Boolean((window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__);
}

export function isAbsoluteLocalPath(value: string): boolean {
  return value.startsWith('/') || /^[A-Za-z]:[\\/]/.test(value) || /^\\\\[^\\]+\\[^\\]+/.test(value);
}

/** Keep workspace-relative inputs relative; the Core still validates access. */
export function selectedPathValue(selected: string | string[] | null, relativeTo?: string): string | null {
  if (selected === null) return null;
  if (typeof selected !== 'string' || !isAbsoluteLocalPath(selected)) {
    throw new PathSelectionError('未能读取所选路径，请重新选择。');
  }
  if (!relativeTo) return selected;
  const normalize = (value: string) => value.replace(/\\/g, '/').replace(/\/+$/, '') || '/';
  const root = normalize(relativeTo);
  const path = normalize(selected);
  if (!isAbsoluteLocalPath(relativeTo) || path.split('/').includes('..') || root.split('/').includes('..')) {
    throw new PathSelectionError('请先选择项目文件夹。');
  }
  const comparable = (value: string) => /^[A-Za-z]:/.test(root) || root.startsWith('//') ? value.toLowerCase() : value;
  if (comparable(path) === comparable(root)) return '.';
  const prefix = root === '/' ? '/' : `${root}/`;
  if (!comparable(path).startsWith(comparable(prefix))) throw new PathSelectionError('请选择项目文件夹内的文件或文件夹。');
  return path.slice(prefix.length);
}

export async function browseLocalPath(kind: PathKind, current: string, relativeTo?: string, within?: string): Promise<string | null> {
  if (!canBrowseLocalPaths()) throw new Error('请在桌面版浏览本机路径，或直接填写完整路径。');
  const { open } = await import('@tauri-apps/plugin-dialog');
  const defaultPath = isAbsoluteLocalPath(current) ? current : relativeTo || within;
  const selected = await open({
    directory: kind === 'directory',
    multiple: false,
    title: kind === 'directory' ? '选择文件夹' : '选择文件',
    ...(defaultPath && isAbsoluteLocalPath(defaultPath) ? { defaultPath } : {}),
  });
  if (within) selectedPathValue(selected, within);
  return selectedPathValue(selected, relativeTo);
}
