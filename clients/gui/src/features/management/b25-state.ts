import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type * as B2 from '../../../../../sdk/typescript-client/b2.generated';
import type * as B25 from '../../../../../sdk/typescript-client/b2_5.generated';
import type { B25PresentationProps } from './b25-presentation-types';

export type GovernanceState = B25.GovernanceState;
export type HistoryPage = B25.HistoryPage;
export type HistoryDetail = B25.HistoryDetail;
export type ExactProposal = B25.ExactProposal;
export type B25Command = B25.B25Command;
export type B25Result = B25.B25Result;
export type HistoryEntry = B25.HistoryEntry;

/**
 * The generated client is intentionally kept behind this small boundary.  It
 * makes the React state testable without constructing a fetch transport and
 * keeps the generated method names/types as the only SDK dependency.
 */
export type B25ClientLike = Pick<
  B25.B25Client,
  'protocolVersion' | 'schemaDigest' | 'negotiateProtocol' | 'getGovernance'
  | 'searchHistory' | 'getHistoryItem' | 'execute'
>;

export type B2ModelClientLike = Pick<B2.B2Client, 'listModels'>;

export interface B25UiError {
  code: string;
  message: string;
  retryable: boolean;
  recovery: string;
  outcomeUnknown: boolean;
  unsupported: boolean;
  detail?: unknown;
}

export class B25AdapterError extends Error {
  readonly detail: B25UiError;

  constructor(detail: B25UiError) {
    super(detail.message);
    this.name = 'B25AdapterError';
    this.detail = detail;
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

export const B25_HISTORY_PAGE_SIZE = 50;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function errorRecord(value: unknown): Record<string, unknown> | undefined {
  if (!isRecord(value)) return undefined;
  if (typeof value.code === 'string' || typeof value.message === 'string') return value;
  if (isRecord(value.detail)) return errorRecord(value.detail);
  return undefined;
}

function isUnsupportedCode(code: string): boolean {
  const normalized = code.toLowerCase();
  return [
    'b2_5_client_unavailable',
    'b25_client_unavailable',
    'b2_5_unsupported',
    'b25_unsupported',
    'unavailable',
    'schema_unavailable',
    'unsupported',
    'not_supported',
    'operation_not_supported',
    'not_implemented',
    'protocol_incompatible',
  ].includes(normalized)
    || normalized.includes('b2_5') && normalized.includes('unavailable');
}

function isUnknownOutcome(code: string, recovery: string): boolean {
  const normalizedCode = code.toLowerCase();
  const normalizedRecovery = recovery.toLowerCase();
  return [
    'transport_unavailable',
    'outcome_unknown',
    'manual_reconcile',
    'manual_reconcile_required',
  ].includes(normalizedCode)
    || normalizedCode.includes('outcome_unknown')
    || normalizedCode.includes('manual_reconcile')
    || normalizedRecovery === 'manual_reconcile';
}

/** Translate generated transport/protocol errors without guessing a result. */
export function normalizeB25Error(error: unknown): B25AdapterError {
  if (error instanceof B25AdapterError) return error;
  const source = errorRecord(error);
  const code = typeof source?.code === 'string' ? source.code : 'transport_unavailable';
  const message = typeof source?.message === 'string'
    ? source.message
    : error instanceof Error ? error.message : 'B2-5 Live request failed';
  const recovery = typeof source?.recovery === 'string' ? source.recovery : 'retry_later';
  return new B25AdapterError({
    code,
    message,
    retryable: source?.retryable === true,
    recovery,
    outcomeUnknown: isUnknownOutcome(code, recovery),
    unsupported: isUnsupportedCode(code),
    detail: source?.detail ?? error,
  });
}

export function makeB25UnavailableError(message = 'B2-5 高级治理接口尚未接入，基础管理仍可使用。'): B25AdapterError {
  return new B25AdapterError({
    code: 'b2_5_client_unavailable',
    message,
    retryable: false,
    recovery: 'none',
    outcomeUnknown: false,
    unsupported: true,
  });
}

export function blocksB25Writes(detail: B25UiError | null, connected: boolean): boolean {
  return !connected || Boolean(detail?.outcomeUnknown || detail?.unsupported)
    || detail?.recovery === 'manual_reconcile';
}

function finiteCursor(value: unknown, label: string): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) {
    throw new B25AdapterError({
      code: 'invalid_b2_5_projection',
      message: `Core 返回的 ${label} 无效，未更新页面。`,
      retryable: false,
      recovery: 'none',
      outcomeUnknown: false,
      unsupported: false,
      detail: value,
    });
  }
  return value;
}

function validateGovernanceState(value: GovernanceState, projectId: string): GovernanceState {
  if (!isRecord(value) || value.project_id !== projectId) {
    throw new B25AdapterError({
      code: 'invalid_b2_5_projection',
      message: 'Core 返回了不属于当前项目的治理 Projection，未更新页面。',
      retryable: false,
      recovery: 'none',
      outcomeUnknown: false,
      unsupported: false,
      detail: value,
    });
  }
  if (!Array.isArray(value.records) || !Array.isArray(value.proposals) || !Array.isArray(value.jobs)) {
    throw new B25AdapterError({
      code: 'invalid_b2_5_projection',
      message: 'Core 返回的治理 Projection 缺少 records、proposals 或 jobs，未更新页面。',
      retryable: false,
      recovery: 'none',
      outcomeUnknown: false,
      unsupported: false,
      detail: value,
    });
  }
  return value;
}

function validateHistoryPage(value: HistoryPage, projectId: string): HistoryPage {
  if (!isRecord(value) || value.project_id !== projectId || value.perspective !== 'historical_fact') {
    throw new B25AdapterError({
      code: 'invalid_b2_5_projection',
      message: 'Core 返回的历史 Projection 无效或项目范围不匹配，未更新页面。',
      retryable: false,
      recovery: 'none',
      outcomeUnknown: false,
      unsupported: false,
      detail: value,
    });
  }
  finiteCursor(value.cutoff_cursor, 'history.cutoff_cursor');
  if (value.next_cursor !== null && value.next_cursor !== undefined) finiteCursor(value.next_cursor, 'history.next_cursor');
  if (!Array.isArray(value.items)) {
    throw new B25AdapterError({
      code: 'invalid_b2_5_projection',
      message: 'Core 返回的历史 Projection 缺少 items，未更新页面。',
      retryable: false,
      recovery: 'none',
      outcomeUnknown: false,
      unsupported: false,
      detail: value,
    });
  }
  return value;
}

export function exactProposalKey(proposal: ExactProposal): string {
  const version = proposal.proposed_version;
  return [
    proposal.proposal_id,
    String(proposal.proposal_revision),
    version.dataset_id,
    version.record_id,
    String(version.version),
    version.content_digest,
    String(proposal.base_head_revision),
  ].join('\u0000');
}

/** Copy the complete CAS/version identity before it enters a batch selection. */
export function snapshotExactProposal(proposal: ExactProposal): ExactProposal {
  return {
    proposal_id: proposal.proposal_id,
    proposal_revision: proposal.proposal_revision,
    proposed_version: { ...proposal.proposed_version },
    base_head_revision: proposal.base_head_revision,
  };
}

export function exactProposalFromEntry(entry: B25.GovernanceEntry): ExactProposal {
  return snapshotExactProposal({
    proposal_id: entry.proposal.proposal_id,
    proposal_revision: entry.proposal.proposal_revision,
    proposed_version: { ...entry.proposal.proposed_version },
    base_head_revision: entry.proposal.base_head.revision,
  });
}

/** Keep only proposals currently displayed by the authoritative projection. */
export function selectionSnapshot(
  requested: ExactProposal[],
  state: GovernanceState | null,
): ExactProposal[] {
  if (!state) return [];
  const allowed = new Map(state.proposals.map((entry) => {
    const proposal = exactProposalFromEntry(entry);
    return [exactProposalKey(proposal), proposal] as const;
  }));
  const selected: ExactProposal[] = [];
  const seen = new Set<string>();
  for (const proposal of requested) {
    const key = exactProposalKey(proposal);
    const exact = allowed.get(key);
    if (!exact || seen.has(key)) continue;
    seen.add(key);
    selected.push(snapshotExactProposal(exact));
  }
  return selected;
}

export function mergeHistoryPages(current: HistoryPage | null, next: HistoryPage): HistoryPage {
  if (!current) return next;
  if (current.project_id !== next.project_id || current.cutoff_cursor !== next.cutoff_cursor) {
    throw new B25AdapterError({
      code: 'history_cutoff_changed',
      message: '历史查询的固定 cutoff 已变化，请重新查询；旧页面未拼接。',
      retryable: false,
      recovery: 'refresh_and_retry',
      outcomeUnknown: false,
      unsupported: false,
    });
  }
  const entries = new Map(current.items.map((entry) => [entry.item_id, entry]));
  for (const entry of next.items) entries.set(entry.item_id, entry);
  return {
    ...next,
    items: [...entries.values()],
    cutoff_cursor: current.cutoff_cursor,
  };
}

export function modelProfileOptions(models: Array<B2.ModelProfile>): Array<{ id: string; name: string; model_id: string }> {
  return models
    .filter((model): model is B2.ModelProfile & { id: string } => typeof model.id === 'string' && model.id.length > 0)
    .filter((model) => model.enabled !== false)
    .map((model) => ({ id: model.id, name: model.name, model_id: model.model_id }));
}

export interface UseB25GovernanceOptions {
  client: B25ClientLike | null;
  modelClient: B2ModelClientLike | null;
  projectId: string | null;
  connected: boolean;
  onMutation?: () => void;
}

export interface B25GovernanceController extends B25PresentationProps {
  supported: boolean;
  projectId: string | null;
  refreshProjection: () => void;
}

type ProjectionLoadOptions = {
  keepHistory?: boolean;
  keepNotice?: boolean;
};

export function useB25Governance({
  client,
  modelClient,
  projectId,
  connected,
  onMutation,
}: UseB25GovernanceOptions): B25GovernanceController {
  const [state, setState] = useState<GovernanceState | null>(null);
  const [history, setHistory] = useState<HistoryPage | null>(null);
  const [detail, setDetail] = useState<HistoryDetail | null>(null);
  const [selected, setSelected] = useState<ExactProposal[]>([]);
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<B25UiError | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [modelProfiles, setModelProfiles] = useState<Array<{ id: string; name: string; model_id: string }>>([]);
  const [unsupported, setUnsupported] = useState(client === null);
  const [unknownWrite, setUnknownWrite] = useState(false);

  const requestEpoch = useRef(0);
  const selectedRef = useRef<ExactProposal[]>([]);
  const previousConnected = useRef<boolean | null>(null);
  const connectedRef = useRef(connected);
  connectedRef.current = connected;
  const cutoffCursor = useRef<number | null>(null);
  const nextCursor = useRef<number | null>(null);
  const searchedQuery = useRef<string | null>(null);

  const setSelection = useCallback((next: ExactProposal[]) => {
    const snapshot = selectionSnapshot(next, state);
    selectedRef.current = snapshot;
    setSelected(snapshot);
  }, [state]);

  const loadProjection = useCallback(async ({
    keepHistory = true,
    keepNotice = false,
  }: ProjectionLoadOptions = {}): Promise<boolean> => {
    const epoch = ++requestEpoch.current;
    if (!client || !projectId) {
      setUnsupported(client === null);
      setState(null);
      selectedRef.current = [];
      setSelected([]);
      return false;
    }
    if (!connectedRef.current) return false;
    setLoading(true);
    setError(null);
    if (!keepNotice) setNotice(null);
    try {
      try {
        await client.negotiateProtocol(true);
      } catch (value: unknown) {
        const negotiationError = normalizeB25Error(value).detail;
        // A Core from before B2-5 commonly answers the version probe with a
        // plain 404/invalid envelope. Treat that probe failure as an explicit
        // unsupported capability while preserving other errors verbatim.
        if (['not_found', 'invalid_error_envelope'].includes(negotiationError.code)) {
          throw new B25AdapterError({
            ...negotiationError,
            code: 'b2_5_client_unavailable',
            message: '当前 Core 不支持 B2-5 高级治理接口，基础管理仍可使用。',
            unsupported: true,
          });
        }
        throw value;
      }
      const [governanceResult, modelsResult] = await Promise.allSettled([
        client.getGovernance(projectId),
        modelClient?.listModels() ?? Promise.resolve([] as B2.ModelProfile[]),
      ]);
      if (epoch !== requestEpoch.current) return false;
      if (governanceResult.status === 'rejected') throw governanceResult.reason;
      const nextState = validateGovernanceState(governanceResult.value, projectId);
      setState(nextState);
      selectedRef.current = [];
      setSelected([]);
      setUnsupported(false);
      setUnknownWrite(false);
      if (modelsResult.status === 'fulfilled') {
        setModelProfiles(modelProfileOptions(modelsResult.value));
      } else {
        // Governance remains usable, but maintenance creation must show an
        // empty formal ModelProfile list instead of inventing a model.
        setModelProfiles([]);
        setNotice('治理状态已读取；正式 ModelProfile 暂时不可用，后台整理创建已禁用。');
      }
      if (!keepHistory) {
        setHistory(null);
        setDetail(null);
        cutoffCursor.current = null;
        nextCursor.current = null;
        searchedQuery.current = null;
      }
      return true;
    } catch (value: unknown) {
      if (epoch !== requestEpoch.current) return false;
      const normalized = normalizeB25Error(value).detail;
      setError(normalized);
      setUnsupported(normalized.unsupported);
      if (normalized.outcomeUnknown) setUnknownWrite(true);
      if (!keepHistory && normalized.unsupported) {
        setState(null);
        setHistory(null);
        setDetail(null);
      }
      return false;
    } finally {
      if (epoch === requestEpoch.current) setLoading(false);
    }
  }, [client, modelClient, projectId]);

  useEffect(() => {
    requestEpoch.current += 1;
    selectedRef.current = [];
    setSelected([]);
    setState(null);
    setHistory(null);
    setDetail(null);
    setQuery('');
    setError(null);
    setNotice(null);
    setUnknownWrite(false);
    cutoffCursor.current = null;
    nextCursor.current = null;
    searchedQuery.current = null;
    setUnsupported(client === null);
    if (client && projectId && connected) void loadProjection({ keepHistory: false });
  }, [client, loadProjection, projectId]);

  useEffect(() => {
    const wasConnected = previousConnected.current;
    previousConnected.current = connected;
    if (wasConnected === null || wasConnected === connected) return;
    requestEpoch.current += 1;
    if (!connected) {
      // Preserve the last server projection for read-only inspection while
      // invalidating requests that could otherwise commit after disconnect.
      setError({
        code: 'transport_unavailable',
        message: 'Core 连接已断开，当前治理内容保留为只读投影。恢复连接后请刷新。',
        retryable: true,
        recovery: 'retry_later',
        outcomeUnknown: false,
        unsupported: false,
      });
      setBusy(false);
      return;
    }
    if (client && projectId) void loadProjection({ keepHistory: true });
  }, [client, connected, loadProjection, projectId]);

  const onQueryChange = useCallback((value: string) => {
    setQuery(value);
    setHistory(null);
    setDetail(null);
    cutoffCursor.current = null;
    nextCursor.current = null;
    searchedQuery.current = null;
  }, []);

  const onSearch = useCallback(async (more = false) => {
    if (!client || !projectId || !connected) return;
    const normalizedQuery = query.trim();
    const isMore = more
      && searchedQuery.current === normalizedQuery
      && cutoffCursor.current !== null
      && nextCursor.current !== null;
    if (more && !isMore) return;
    const epoch = ++requestEpoch.current;
    setLoading(true);
    setError(null);
    setNotice(null);
    try {
      const page = validateHistoryPage(await client.searchHistory(projectId, {
        query: normalizedQuery || undefined,
        afterCursor: isMore ? nextCursor.current : undefined,
        cutoffCursor: isMore ? cutoffCursor.current : undefined,
        limit: B25_HISTORY_PAGE_SIZE,
      }), projectId);
      if (epoch !== requestEpoch.current) return;
      if (!isMore) {
        cutoffCursor.current = page.cutoff_cursor;
        searchedQuery.current = normalizedQuery;
        setHistory(page);
      } else {
        setHistory((current) => mergeHistoryPages(current, page));
      }
      nextCursor.current = page.next_cursor ?? null;
      setDetail(null);
    } catch (value: unknown) {
      if (epoch !== requestEpoch.current) return;
      const normalized = normalizeB25Error(value).detail;
      setError(normalized);
    } finally {
      if (epoch === requestEpoch.current) setLoading(false);
    }
  }, [client, connected, projectId, query]);

  const onExpand = useCallback(async (itemId: string) => {
    if (!client || !projectId || !connected || !itemId.trim()) return;
    const epoch = ++requestEpoch.current;
    setLoading(true);
    setError(null);
    try {
      const nextDetail = await client.getHistoryItem(projectId, itemId);
      if (epoch !== requestEpoch.current) return;
      if (nextDetail.project_id !== projectId || nextDetail.perspective !== 'historical_fact') {
        throw new B25AdapterError({
          code: 'invalid_b2_5_projection',
          message: 'Core 返回的历史详情不属于当前项目，未展开。',
          retryable: false,
          recovery: 'none',
          outcomeUnknown: false,
          unsupported: false,
          detail: nextDetail,
        });
      }
      setDetail(nextDetail);
    } catch (value: unknown) {
      if (epoch !== requestEpoch.current) return;
      setError(normalizeB25Error(value).detail);
    } finally {
      if (epoch === requestEpoch.current) setLoading(false);
    }
  }, [client, connected, projectId]);

  const executeCommand = useCallback(async (command: B25Command) => {
    if (!client || !projectId) {
      setError(makeB25UnavailableError().detail);
      return;
    }
    if (!connected || unsupported || unknownWrite) {
      setError({
        code: connected ? 'write_blocked' : 'transport_unavailable',
        message: connected ? '当前写操作已阻断，请先刷新并核对服务端状态。' : 'Core 连接已断开，治理写操作已禁用。',
        retryable: false,
        recovery: connected ? 'refresh_and_retry' : 'retry_later',
        outcomeUnknown: unknownWrite || !connected,
        unsupported,
      });
      return;
    }
    if (busy) return;
    if (command.project_id !== projectId) {
      setError({
        code: 'project_scope_mismatch',
        message: '命令项目与当前治理项目不一致，未提交。',
        retryable: false,
        recovery: 'refresh_and_retry',
        outcomeUnknown: false,
        unsupported: false,
      });
      return;
    }
    const commandSelection = command.selections ?? [];
    if (command.action === 'review') {
      const exactCurrentSelection = selectionSnapshot(commandSelection, state);
      const currentKeys = new Set(exactCurrentSelection.map(exactProposalKey));
      const selectedKeys = new Set(selectedRef.current.map(exactProposalKey));
      const selectionIsExact = commandSelection.length > 0
        && exactCurrentSelection.length === commandSelection.length
        && commandSelection.every((entry) => currentKeys.has(exactProposalKey(entry)))
        // A multi-item review must come from the explicit batch snapshot. A
        // single-item review is a direct card action and has its own exact
        // selection in the command payload.
        && (commandSelection.length === 1
          || commandSelection.every((entry) => selectedKeys.has(exactProposalKey(entry))));
      if (!selectionIsExact) {
        setError({
          code: 'selection_changed',
          message: '批量确认必须使用当前页面选中的完整 Proposal/version/CAS 快照，未提交。',
          retryable: false,
          recovery: 'refresh_and_retry',
          outcomeUnknown: false,
          unsupported: false,
        });
        return;
      }
    }
    const exactCommand: B25Command = {
      ...command,
      selections: commandSelection.map(snapshotExactProposal),
    };
    const epoch = ++requestEpoch.current;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      // One request, one generated idempotency key.  A transport failure is
      // never replayed here; the user must refresh and reconcile explicitly.
      const result = await client.execute(exactCommand, {});
      if (epoch !== requestEpoch.current) return;
      const refreshed = await loadProjection({ keepHistory: true, keepNotice: true });
      if (!refreshed && requestEpoch.current !== epoch + 1) return;
      if (refreshed) setNotice(`${result.status}：${result.message}`);
      else setNotice('命令已收到，但最新治理状态读取失败；请刷新后人工核对。');
      setUnknownWrite(false);
      onMutation?.();
    } catch (value: unknown) {
      if (epoch !== requestEpoch.current) return;
      const normalized = normalizeB25Error(value).detail;
      setError(normalized);
      if (normalized.unsupported) setUnsupported(true);
      if (normalized.outcomeUnknown) setUnknownWrite(true);
    } finally {
      if (requestEpoch.current >= epoch) setBusy(false);
    }
  }, [busy, client, connected, loadProjection, onMutation, projectId, state, unknownWrite, unsupported]);

  const refreshProjection = useCallback(() => {
    if (!connected) {
      setError({
        code: 'transport_unavailable',
        message: 'Core 连接已断开，恢复连接后才能刷新治理状态。',
        retryable: true,
        recovery: 'retry_later',
        outcomeUnknown: false,
        unsupported: false,
      });
      return;
    }
    void loadProjection({ keepHistory: true });
  }, [connected, loadProjection]);

  const controller = useMemo<B25GovernanceController>(() => ({
    state,
    history,
    detail,
    selected,
    query,
    loading,
    busy,
    readOnly: !client || !projectId || blocksB25Writes(error, connected) || unknownWrite,
    error: error?.message ?? null,
    notice,
    modelProfiles,
    onQueryChange,
    onSearch: (more = false) => { void onSearch(more); },
    onExpand: (itemId: string) => { void onExpand(itemId); },
    onRefresh: refreshProjection,
    onSelectionChange: setSelection,
    onCommand: (command: B25Command) => { void executeCommand(command); },
    supported: !unsupported,
    projectId,
    refreshProjection,
  }), [
    client,
    connected,
    detail,
    error,
    executeCommand,
    history,
    loading,
    modelProfiles,
    notice,
    onExpand,
    onQueryChange,
    onSearch,
    projectId,
    refreshProjection,
    selected,
    setSelection,
    state,
    unknownWrite,
    unsupported,
  ]);

  return controller;
}

/** Preserve the displayed relationship identity; never silently upgrade its target. */
export function exactRelationshipTarget(
  selected: B25.MemoryVersionRef | null,
  records: B25.GovernanceRecord[],
): B25.MemoryVersionRef | null {
  if (!selected) return null;
  const record = records.find(({ version, head }) => head.state === 'published'
    && version.ref.dataset_id === selected.dataset_id
    && version.ref.record_id === selected.record_id
    && version.ref.version === selected.version
    && version.ref.content_digest === selected.content_digest);
  return record ? { ...record.version.ref } : null;
}
