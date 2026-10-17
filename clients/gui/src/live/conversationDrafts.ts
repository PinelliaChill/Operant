/** Local draft text only. Request recovery records and credentials never enter this store. */
export interface DraftSnapshot { scope: string; text: string; revision: number }
interface DraftRecord { text: string; revision: number }
interface DraftStorage { getItem(key: string): string | null; setItem(key: string, value: string): void }
const MAX_TEXT = 32_000;
const MAX_TOTAL = 256_000;
const MAX_SCOPES = 50;
export function draftScope(origin: string, workspace: string, threadId: string | null): string {
  // Thread IDs are Core-global and retain identity if a project is detached.
  return JSON.stringify([new URL(origin).origin, threadId ? 'thread' : 'new', threadId || workspace || 'unselected']);
}
export class ConversationDrafts {
  readonly storageKey = 'operant.conversation-drafts.v1';
  private records: Record<string, DraftRecord> = {};
  warning = '';
  private storage: () => DraftStorage;
  constructor(storage: () => DraftStorage) {
    this.storage = storage;
    try {
      const raw = storage().getItem(this.storageKey);
      if (!raw) return;
      if (raw.length > MAX_TOTAL) throw new Error('capacity');
      const data: unknown = JSON.parse(raw);
      if (!data || typeof data !== 'object' || (data as { version?: unknown }).version !== 1) throw new Error('version');
      const records = (data as { records?: unknown }).records;
      if (!records || typeof records !== 'object' || Array.isArray(records) || Object.keys(records).length > MAX_SCOPES) throw new Error('records');
      for (const [key, value] of Object.entries(records)) {
        if (!value || typeof value.text !== 'string' || value.text.length > MAX_TEXT || !Number.isSafeInteger(value.revision) || value.revision < 0) throw new Error('record');
        const scope: unknown = JSON.parse(key);
        if (!Array.isArray(scope) || scope.length !== 3 || scope.some((part) => typeof part !== 'string')) throw new Error('scope');
      }
      this.records = Object.fromEntries(Object.entries(records).map(([key, value]) => [key, { text: value.text, revision: value.revision }]));
    } catch { this.warning = '本地草稿无法读取或已损坏。当前编辑仍保留在内存中，刷新或退出可能丢失。'; }
  }
  read(scope: string): DraftSnapshot {
    const current = this.records[scope];
    return { scope, text: current?.text || '', revision: current?.revision || 0 };
  }
  write(scope: string, text: string): DraftSnapshot {
    this.records[scope] = { text, revision: this.read(scope).revision + 1 };
    this.persist();
    return this.read(scope);
  }
  accept(submitted: DraftSnapshot): void {
    const current = this.read(submitted.scope);
    if (current.revision === submitted.revision && current.text.trim() === submitted.text.trim()) this.write(submitted.scope, '');
  }
  move(source: DraftSnapshot, target: string): DraftSnapshot {
    if (source.scope === target) return source;
    const current = this.read(source.scope);
    // Never overwrite another thread's saved work.
    if (this.read(target).text) return source;
    this.records[target] = { text: current.text, revision: current.revision };
    delete this.records[source.scope];
    this.persist();
    return { ...source, scope: target };
  }
  private persist(): void {
    try {
      const records = Object.fromEntries(Object.entries(this.records).filter(([, value]) => value.text));
      const raw = JSON.stringify({ version: 1, records });
      if (Object.keys(records).length > MAX_SCOPES || raw.length > MAX_TOTAL || Object.values(records).some((value) => value.text.length > MAX_TEXT)) {
        this.warning = '草稿超过本地保存容量（单条 32000 字、最多 50 条、合计 256000 字符）。当前编辑仍在内存中，请缩短或另行保存；刷新或退出可能丢失。';
        return;
      }
      this.storage().setItem(this.storageKey, raw);
      this.warning = '';
    } catch { this.warning = '本地草稿保存失败，可能是存储权限或空间不足。当前编辑仍在内存中，刷新或退出可能丢失。'; }
  }
}

export function draftThreadFromPath(path: string): string | null {
  const encoded = /^\/chat\/([^/]+)$/.exec(path)?.[1];
  if (!encoded) return null;
  try { return decodeURIComponent(encoded); } catch { return encoded; }
}

export function draftThreadForSelection(path: string, selectedThreadId: string | null): string | null {
  return draftThreadFromPath(path) || (path === '/chat' ? selectedThreadId : null);
}
export function isNewConversationDraft(snapshot: DraftSnapshot): boolean {
  try { return JSON.parse(snapshot.scope)[1] === 'new'; } catch { return false; }
}
