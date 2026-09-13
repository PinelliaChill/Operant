/**
 * B2-3 management boundary.
 *
 * The concrete client is generated from the B2-3 schema. This file keeps the
 * GUI boundary small: it validates the generated projection and translates
 * transport errors. It never supplies demo data or retries a command whose
 * transport outcome is unknown.
 */

import type * as B23 from '../../../../sdk/typescript-client/b2_3.generated';

/** Public management types are aliases to the generated B2-3 contract. */
export type B23Action = B23.ManagementCommand['action'];
export type B23ManagementCommand = B23.ManagementCommand;
export type B23ProtocolNegotiation = B23.ProtocolNegotiation;
export type B23ManagedProject = B23.ManagedProject;
export type B23PluginCatalogEntry = B23.PluginCatalogEntry;
export type B23ManagedInstallation = B23.ManagedInstallation;
export type B23ManagedDataset = B23.ManagedDataset;
export type B23ManagedArtifact = B23.ManagedArtifact;
export type B23ManagedSetting = B23.ManagedSetting;
export type B23ManagedSkill = B23.ManagedSkill;
export type B23ManagedProposal = B23.ManagedProposal;
export type B23ManagedMemory = B23.ManagedMemory;
export type B23ManagementState = B23.ManagementState;
export type B23ManagementResult = B23.ManagementResult;
export type B23ClientLike = Pick<B23.B23Client, 'protocolVersion' | 'schemaDigest' | 'negotiateProtocol' | 'getManagement' | 'executeManagementCommand'>;

export interface B23UiError {
  code: string;
  message: string;
  retryable: boolean;
  recovery: string;
  outcomeUnknown: boolean;
  detail?: unknown;
}

export class B23AdapterError extends Error {
  readonly detail: B23UiError;

  constructor(detail: B23UiError) {
    super(detail.message);
    this.name = 'B23AdapterError';
    this.detail = detail;
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function requiredRecord(value: unknown, label: string): Record<string, unknown> {
  if (!isRecord(value)) {
    throw invalidProjection(`${label} 不是对象。`, value);
  }
  return value;
}

function requiredText(value: unknown, label: string): string {
  if (typeof value !== 'string' || value.trim().length === 0) {
    throw invalidProjection(`${label} 缺失。`, value);
  }
  return value;
}

function nullableText(value: unknown, label: string): string | null {
  if (value === null) return null;
  return requiredText(value, label);
}

function requiredBoolean(value: unknown, label: string): boolean {
  if (typeof value !== 'boolean') throw invalidProjection(`${label} 无效。`, value);
  return value;
}

function requiredNumber(value: unknown, label: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw invalidProjection(`${label} 无效。`, value);
  }
  return value;
}

function stringArray(value: unknown, label: string): string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== 'string')) {
    throw invalidProjection(`${label} 无效。`, value);
  }
  return [...value];
}

function recordArray(value: unknown, label: string): Array<Record<string, unknown>> {
  if (!Array.isArray(value) || value.some((item) => !isRecord(item))) {
    throw invalidProjection(`${label} 无效。`, value);
  }
  return value as Array<Record<string, unknown>>;
}

function invalidProjection(message: string, detail: unknown): B23AdapterError {
  return new B23AdapterError({
    code: 'invalid_b2_3_projection',
    message: `Core 返回的管理 Projection ${message}`,
    retryable: false,
    recovery: 'none',
    outcomeUnknown: false,
    detail,
  });
}

function mapProject(value: unknown): B23ManagedProject {
  const source = requiredRecord(value, 'project');
  return {
    project_id: requiredText(source.project_id, 'project.project_id'),
    name: requiredText(source.name, 'project.name'),
    workspace_id: requiredText(source.workspace_id, 'project.workspace_id'),
    archived: source.archived === undefined ? false : requiredBoolean(source.archived, 'project.archived'),
    memory_enabled: source.memory_enabled === undefined ? true : requiredBoolean(source.memory_enabled, 'project.memory_enabled'),
    installation_id: source.installation_id === undefined
      ? null
      : nullableText(source.installation_id, 'project.installation_id'),
  };
}

function mapCatalogEntry(value: unknown): B23PluginCatalogEntry {
  const source = requiredRecord(value, 'catalog entry');
  return {
    plugin_id: requiredText(source.plugin_id, 'catalog.plugin_id'),
    name: requiredText(source.name, 'catalog.name'),
    description: requiredText(source.description, 'catalog.description'),
    config_schema: requiredRecord(source.config_schema, 'catalog.config_schema'),
  };
}

function mapInstallation(value: unknown): B23ManagedInstallation {
  const source = requiredRecord(value, 'installation');
  return {
    installation_id: requiredText(source.installation_id, 'installation.installation_id'),
    plugin_id: requiredText(source.plugin_id, 'installation.plugin_id'),
    dataset_id: requiredText(source.dataset_id, 'installation.dataset_id'),
    binding_id: source.binding_id === undefined ? null : nullableText(source.binding_id, 'installation.binding_id'),
    state: requiredText(source.state, 'installation.state'),
    mode: requiredText(source.mode, 'installation.mode'),
    certification_status: requiredText(source.certification_status, 'installation.certification_status'),
    config: requiredRecord(source.config, 'installation.config'),
  };
}

function mapDataset(value: unknown): B23ManagedDataset {
  const source = requiredRecord(value, 'dataset');
  return {
    dataset_id: requiredText(source.dataset_id, 'dataset.dataset_id'),
    plugin_id: requiredText(source.plugin_id, 'dataset.plugin_id'),
    installation_id: source.installation_id === undefined
      ? null
      : nullableText(source.installation_id, 'dataset.installation_id'),
    state: requiredText(source.state, 'dataset.state'),
    record_count: requiredNumber(source.record_count, 'dataset.record_count'),
    exceptions: source.exceptions === undefined || source.exceptions === null
      ? []
      : stringArray(source.exceptions, 'dataset.exceptions'),
  };
}

function mapSetting(value: unknown): B23ManagedSetting {
  const source = requiredRecord(value, 'setting');
  return {
    key: requiredText(source.key, 'setting.key'),
    value: source.value,
    source: requiredText(source.source, 'setting.source'),
    scope: requiredText(source.scope, 'setting.scope'),
    effective_at: requiredText(source.effective_at, 'setting.effective_at'),
  };
}

function mapSkill(value: unknown): B23ManagedSkill {
  const source = requiredRecord(value, 'skill');
  const projectIds = source.project_ids === undefined ? undefined : stringArray(source.project_ids, 'skill.project_ids');
  const trustStatus = source.trust_status === undefined ? undefined : requiredText(source.trust_status, 'skill.trust_status');
  return {
    skill_id: requiredText(source.skill_id, 'skill.skill_id'),
    name: requiredText(source.name, 'skill.name'),
    state: requiredText(source.state, 'skill.state'),
    package_ref: requiredText(source.package_ref, 'skill.package_ref'),
    ...(projectIds === undefined ? {} : { project_ids: projectIds }),
    ...(trustStatus === undefined ? {} : { trust_status: trustStatus }),
  };
}

function mapProposal(value: unknown): B23ManagedProposal {
  const source = requiredRecord(value, 'proposal');
  return {
    proposal_id: requiredText(source.proposal_id, 'proposal.proposal_id'),
    content: requiredText(source.content, 'proposal.content'),
    state: requiredText(source.state, 'proposal.state'),
    expected_revision: requiredNumber(source.expected_revision, 'proposal.expected_revision'),
  };
}

function mapMemory(value: unknown): B23ManagedMemory {
  const source = requiredRecord(value, 'memory record');
  const proposals = source.proposals === undefined || source.proposals === null ? [] : source.proposals;
  if (!Array.isArray(proposals)) throw invalidProjection('memory.proposals 缺失。', proposals);
  return {
    record_id: requiredText(source.record_id, 'memory.record_id'),
    dataset_id: requiredText(source.dataset_id, 'memory.dataset_id'),
    project_id: requiredText(source.project_id, 'memory.project_id'),
    content: requiredText(source.content, 'memory.content'),
    state: requiredText(source.state, 'memory.state'),
    revision: requiredNumber(source.revision, 'memory.revision'),
    evidence: requiredText(source.evidence, 'memory.evidence'),
    version: typeof source.version === 'number'
      ? source.version
      : (() => { throw invalidProjection('memory.version 无效。', source.version); })(),
    sources: recordArray(source.sources, 'memory.sources'),
    proposals: proposals.map(mapProposal),
  };
}

export function mapManagementState(value: unknown): B23ManagementState {
  const source = requiredRecord(value, 'ManagementState');
  const skillCatalog = source.skill_catalog;
  if (!Array.isArray(skillCatalog)) throw invalidProjection('skill_catalog 缺失。', skillCatalog);
  const artifacts = source.artifacts === undefined || source.artifacts === null
    ? undefined
    : recordArray(source.artifacts, 'artifacts').map((entry) => ({
      artifact_id: requiredText(entry.artifact_id, 'artifact.artifact_id'),
      content_hash: requiredText(entry.content_hash, 'artifact.content_hash'),
      size_bytes: requiredNumber(entry.size_bytes, 'artifact.size_bytes'),
      lifecycle: requiredText(entry.lifecycle, 'artifact.lifecycle'),
      pinned: requiredBoolean(entry.pinned, 'artifact.pinned'),
      blocked: requiredBoolean(entry.blocked, 'artifact.blocked'),
    }));
  return {
    projects: recordArray(source.projects, 'projects').map(mapProject),
    global_enabled: requiredBoolean(source.global_enabled, 'global_enabled'),
    catalog: recordArray(source.catalog, 'catalog').map(mapCatalogEntry),
    installations: recordArray(source.installations, 'installations').map(mapInstallation),
    datasets: recordArray(source.datasets, 'datasets').map(mapDataset),
    settings: recordArray(source.settings, 'settings').map(mapSetting),
    skills: recordArray(source.skills, 'skills').map(mapSkill),
    skill_catalog: skillCatalog.map((value) => {
      const entry = requiredRecord(value, 'skill_catalog entry');
      return {
        package_ref: requiredText(entry.package_ref, 'skill_catalog.package_ref'),
        name: requiredText(entry.name, 'skill_catalog.name'),
      };
    }),
    records: recordArray(source.records, 'records').map(mapMemory),
    ...(artifacts === undefined ? {} : { artifacts }),
  };
}

export function mapManagementResult(value: unknown): B23ManagementResult {
  const source = requiredRecord(value, 'ManagementResult');
  let records: B23ManagedMemory[] | null | undefined;
  if (source.records === null) records = null;
  else if (source.records !== undefined) records = recordArray(source.records, 'result.records').map(mapMemory);
  let exportData: Record<string, unknown> | null | undefined;
  if (source.export_data === null) exportData = null;
  else if (source.export_data !== undefined) exportData = requiredRecord(source.export_data, 'result.export_data');
  return {
    status: requiredText(source.status, 'result.status'),
    message: requiredText(source.message, 'result.message'),
    state: mapManagementState(source.state),
    export_data: exportData,
    records,
  };
}

function mapProtocol(value: unknown): B23ProtocolNegotiation {
  const source = requiredRecord(value, 'B2-3 protocol negotiation');
  return {
    protocol_version: requiredText(source.protocol_version, 'protocol_version'),
    schema_digest: requiredText(source.schema_digest, 'schema_digest'),
    min_client_version: requiredText(source.min_client_version, 'min_client_version'),
    capabilities: stringArray(source.capabilities, 'capabilities'),
  } as B23ProtocolNegotiation;
}

function errorRecord(value: unknown): Record<string, unknown> | undefined {
  if (!isRecord(value)) return undefined;
  if (typeof value.code === 'string' && typeof value.message === 'string') return value;
  if (isRecord(value.detail) && typeof value.detail.code === 'string' && typeof value.detail.message === 'string') {
    return value.detail;
  }
  return undefined;
}

export function normalizeB23Error(error: unknown): B23AdapterError {
  if (error instanceof B23AdapterError) return error;
  const source = errorRecord(error);
  const code = typeof source?.code === 'string' ? source.code : 'transport_unavailable';
  const message = typeof source?.message === 'string'
    ? source.message
    : error instanceof Error ? error.message : 'B2-3 Live request failed';
  const recovery = typeof source?.recovery === 'string' ? source.recovery : 'retry_later';
  const outcomeUnknown = code === 'outcome_unknown'
    || code === 'transport_unavailable'
    || code === 'manual_reconcile_required'
    || code === 'manual_reconcile'
    || recovery === 'manual_reconcile'
    || code.includes('outcome_unknown')
    || code.includes('manual_reconcile');
  return new B23AdapterError({
    code,
    message,
    retryable: source?.retryable === true,
    recovery,
    outcomeUnknown,
    detail: source?.detail ?? error,
  });
}

/** Business rejections keep the last authoritative projection usable. */
export function blocksB23Management(detail: B23UiError): boolean {
  return detail.outcomeUnknown
    || detail.recovery === 'manual_reconcile'
    || [
      'transport_unavailable',
      'protocol_incompatible',
      'invalid_b2_3_projection',
      'b2_3_client_unavailable',
    ].includes(detail.code);
}

export function idempotencyKey(): string {
  const cryptoApi = globalThis.crypto;
  if (typeof cryptoApi?.randomUUID === 'function') return cryptoApi.randomUUID();
  if (typeof cryptoApi?.getRandomValues === 'function') {
    const bytes = cryptoApi.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
  throw new B23AdapterError({
    code: 'crypto_unavailable',
    message: '安全的管理命令幂等键不可用，未提交命令。',
    retryable: false,
    recovery: 'none',
    outcomeUnknown: false,
  });
}

export class B23ManagementAdapter {
  private readonly client: B23ClientLike;

  constructor(client: B23ClientLike) {
    this.client = client;
  }

  async connect(): Promise<B23ProtocolNegotiation> {
    try {
      const metadata = mapProtocol(await this.client.negotiateProtocol(true));
      if (metadata.protocol_version !== this.client.protocolVersion
        || metadata.min_client_version !== this.client.protocolVersion
        || metadata.schema_digest !== this.client.schemaDigest) {
        throw new B23AdapterError({
          code: 'protocol_incompatible',
          message: 'Core B2-3 协议协商结果与生成 Client 不匹配，未建立管理连接。',
          retryable: false,
          recovery: 'none',
          outcomeUnknown: false,
          detail: metadata,
        });
      }
      return metadata;
    } catch (error: unknown) {
      throw normalizeB23Error(error);
    }
  }

  async getManagement(): Promise<B23ManagementState> {
    try {
      return mapManagementState(await this.client.getManagement());
    } catch (error: unknown) {
      throw normalizeB23Error(error);
    }
  }

  async executeManagementCommand(
    command: B23ManagementCommand,
    commandKey?: string,
  ): Promise<B23ManagementResult> {
    if (!command.action) {
      throw new B23AdapterError({
        code: 'action_required',
        message: '管理命令缺少 action，未提交。',
        retryable: false,
        recovery: 'none',
        outcomeUnknown: false,
      });
    }
    try {
      // A command has exactly one attempt.  If this request fails after the
      // transport boundary, callers must query/reconcile instead of replaying.
      return mapManagementResult(await this.client.executeManagementCommand(command, { idempotencyKey: commandKey ?? idempotencyKey() }));
    } catch (error: unknown) {
      throw normalizeB23Error(error);
    }
  }
}
