import type { RuntimeCursor, RuntimeEvent } from './runtimeState.ts';

export interface Phase23Stream extends AsyncIterable<unknown> {
  close(): void;
}

export interface Phase23ClientLike {
  negotiateProtocol(strict?: boolean): Promise<unknown>;
  createWorkflowDraft(input: unknown, options?: unknown): Promise<unknown>;
  compileWorkflowDraft(draftId: string): Promise<unknown>;
  publishWorkflow(draftId: string, input?: unknown, options?: unknown): Promise<unknown>;
  getWorkflowDefinition(definitionId: string): Promise<unknown>;
  startGraphRun(definitionId: string, input?: unknown, options?: unknown): Promise<unknown>;
  getGraphRun(runId: string): Promise<unknown>;
  getGraphRunByLegacyWorkflow(legacyWorkflowRunId: string): Promise<unknown>;
  listNodeRuns(runId: string): Promise<unknown>;
  streamGraphRunEvents(runId: string, options?: unknown): Promise<Phase23Stream>;
  resumeGraphRun(runId: string, input?: unknown, options?: unknown): Promise<unknown>;
  cancelGraphRun(runId: string, options?: unknown): Promise<unknown>;
  provideNodeInput(runId: string, nodeRunId: string, input: unknown, options?: unknown): Promise<unknown>;
  createTeamDefinition(input: unknown, options?: unknown): Promise<unknown>;
  startTeamRun(definitionId: string, input?: unknown, options?: unknown): Promise<unknown>;
  getTeamRun(runId: string): Promise<unknown>;
  sendTeamMessage(runId: string, input: unknown, options?: unknown): Promise<unknown>;
  listTeamMessages(runId: string, options?: unknown): Promise<unknown>;
  getMailbox(runId: string, recipientId: string, options?: unknown): Promise<unknown>;
  acknowledgeMailboxDelivery(runId: string, deliveryId: string, options?: unknown): Promise<unknown>;
  getTaskBoard(runId: string): Promise<unknown>;
  updateTeamTask(runId: string, taskId: string, input: unknown, options?: unknown): Promise<unknown>;
  getArtifactBoard(runId: string): Promise<unknown>;
  publishArtifactBoardItem(runId: string, input: unknown, options?: unknown): Promise<unknown>;
  streamTeamRunEvents(runId: string, options?: unknown): Promise<Phase23Stream>;
}

export interface GraphDraftView {
  id: string;
  name: string;
  description?: string;
  workspaceRef?: string;
  nodes: Record<string, unknown>[];
  edges: Record<string, unknown>[];
}

export interface GraphRunView {
  id: string;
  definitionId: string;
  status: string;
  createdAt?: string;
  updatedAt?: string;
  manualReconcileRequired: boolean;
}

export interface NodeRunView {
  id: string;
  nodeId: string;
  status: string;
  activeAttemptId?: string;
  error?: unknown;
}

export interface TeamRunView {
  id: string;
  definitionId: string;
  status: string;
  roster: Record<string, unknown>[];
}

export interface TeamMessageView {
  id: string;
  senderId: string;
  recipients: string[];
  body: unknown;
  createdAt?: string;
}

export interface MailboxDeliveryView {
  id: string;
  messageId: string;
  recipientId: string;
  status: string;
  acknowledgedAt?: string;
}

export interface BoardItemView {
  id: string;
  title: string;
  status: string;
  detail?: unknown;
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error(`${label} projection is not an object`);
  }
  return value as Record<string, unknown>;
}

function requiredString(value: Record<string, unknown>, keys: string[], label: string): string {
  for (const key of keys) {
    if (typeof value[key] === 'string' && value[key]) return value[key] as string;
  }
  throw new Error(`${label} projection is missing ${keys.join('/')}`);
}

function optionalString(value: Record<string, unknown>, keys: string[]): string | undefined {
  for (const key of keys) {
    if (typeof value[key] === 'string') return value[key] as string;
  }
  return undefined;
}

function records(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.filter((entry): entry is Record<string, unknown> => (
      typeof entry === 'object' && entry !== null && !Array.isArray(entry)
    ))
    : [];
}

function pageItems(value: unknown, keys: string[]): Record<string, unknown>[] {
  if (Array.isArray(value)) return records(value);
  const projection = record(value, 'list');
  for (const key of keys) {
    if (Array.isArray(projection[key])) return records(projection[key]);
  }
  return [];
}

export function mapGraphDraft(value: unknown): GraphDraftView {
  const projection = record(value, 'workflow draft');
  return {
    id: requiredString(projection, ['draft_id', 'id'], 'workflow draft'),
    name: requiredString(projection, ['name'], 'workflow draft'),
    description: optionalString(projection, ['description']),
    workspaceRef: optionalString(projection, ['workspace_ref', 'workspace']),
    nodes: records(projection.nodes),
    edges: records(projection.edges),
  };
}

export function mapGraphRun(value: unknown): GraphRunView {
  const projection = record(value, 'graph run');
  const status = requiredString(projection, ['status'], 'graph run');
  return {
    id: requiredString(projection, ['run_id', 'id'], 'graph run'),
    definitionId: requiredString(projection, ['definition_id', 'workflow_definition_id'], 'graph run'),
    status,
    createdAt: optionalString(projection, ['created_at']),
    updatedAt: optionalString(projection, ['updated_at']),
    manualReconcileRequired: status === 'manual_reconcile_required' || status === 'outcome_unknown',
  };
}

export function mapNodeRuns(value: unknown): NodeRunView[] {
  return pageItems(value, ['node_runs', 'items']).map((projection) => ({
    id: requiredString(projection, ['node_run_id', 'id'], 'node run'),
    nodeId: requiredString(projection, ['node_id'], 'node run'),
    status: requiredString(projection, ['status'], 'node run'),
    activeAttemptId: optionalString(projection, ['active_attempt_id']),
    error: projection.error,
  }));
}

export function mapTeamRun(value: unknown): TeamRunView {
  const projection = record(value, 'team run');
  return {
    id: requiredString(projection, ['team_run_id', 'run_id', 'id'], 'team run'),
    definitionId: requiredString(projection, ['team_definition_id', 'definition_id'], 'team run'),
    status: requiredString(projection, ['status'], 'team run'),
    roster: records(projection.roster),
  };
}

export function mapTeamMessages(value: unknown): TeamMessageView[] {
  return pageItems(value, ['messages', 'items']).map((projection) => ({
    id: requiredString(projection, ['message_id', 'id'], 'team message'),
    senderId: requiredString(projection, ['sender_id'], 'team message'),
    recipients: Array.isArray(projection.recipient_ids)
      ? projection.recipient_ids.filter((entry): entry is string => typeof entry === 'string')
      : [],
    body: projection.body ?? projection.payload,
    createdAt: optionalString(projection, ['created_at']),
  }));
}

export function mapMailbox(value: unknown): MailboxDeliveryView[] {
  return pageItems(value, ['deliveries', 'items']).map((projection) => ({
    id: requiredString(projection, ['delivery_id', 'id'], 'mailbox delivery'),
    messageId: requiredString(projection, ['message_id'], 'mailbox delivery'),
    recipientId: requiredString(projection, ['recipient_id'], 'mailbox delivery'),
    status: requiredString(projection, ['status'], 'mailbox delivery'),
    acknowledgedAt: optionalString(projection, ['acknowledged_at', 'acked_at']),
  }));
}

export function mapBoard(value: unknown, keys: string[]): BoardItemView[] {
  return pageItems(value, keys).map((projection) => ({
    id: requiredString(projection, ['task_id', 'artifact_id', 'item_id', 'id'], 'board item'),
    title: optionalString(projection, ['title', 'name']) ?? '未命名条目',
    status: optionalString(projection, ['status']) ?? 'unknown',
    detail: projection,
  }));
}

export function mapRuntimeFrame(value: unknown, fallbackScope: string): RuntimeEvent {
  const frame = record(value, 'runtime event');
  const data = typeof frame.data === 'object' && frame.data !== null && !Array.isArray(frame.data)
    ? frame.data as Record<string, unknown>
    : frame;
  const rawCursor = frame.id ?? data.cursor;
  const cursor: RuntimeCursor | null = typeof rawCursor === 'bigint' || typeof rawCursor === 'number'
    ? rawCursor
    : typeof rawCursor === 'string' && /^\d+$/.test(rawCursor)
      ? BigInt(rawCursor)
      : null;
  return {
    id: optionalString(data, ['id', 'event_id']) ?? `${fallbackScope}:${cursor ?? 'none'}`,
    cursor,
    eventType: optionalString(data, ['event_type']) ?? optionalString(frame, ['event']) ?? 'runtime.event',
    resourceScope: optionalString(frame, ['resource_scope']) ?? fallbackScope,
    payload: typeof data.payload === 'object' && data.payload !== null && !Array.isArray(data.payload)
      ? data.payload as Record<string, unknown>
      : {},
  };
}
