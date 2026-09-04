export interface WriterWorkspaceView {
  writerWorkspaceId: string;
  writerKey: string;
  isolationKind: string;
  isolationRef: string;
  ownershipPaths: string[];
  lease: null | {
    owner: string;
    fencing: number;
    expiresAt?: string;
    releasedAt?: string;
  };
}

export interface WriterArtifactView {
  writerArtifactId: string;
  writerWorkspaceId: string;
  artifactKind: string;
  changedPaths: string[];
  testEvidenceRefs: string[];
}

export interface WriterConflictView {
  conflictId: string;
  status: string;
  paths: string[];
}

export interface MergeRunView {
  mergeRunId: string;
  mergeNodeId: string;
  status: string;
  resultArtifactRef?: string;
  errorCode?: string;
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error(`${label} projection is not an object`);
  }
  return value as Record<string, unknown>;
}

function text(value: unknown, label: string): string {
  if (typeof value !== 'string' || !value) throw new Error(`${label} is missing`);
  return value;
}

function strings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string')
    : [];
}

async function get(path: string): Promise<unknown[]> {
  const response = await fetch(path, { headers: { Accept: 'application/json' } });
  if (!response.ok) throw new Error(`Core multi-writer query failed (${response.status})`);
  const value: unknown = await response.json();
  if (!Array.isArray(value)) throw new Error('Core multi-writer list is invalid');
  return value;
}

export async function loadMultiWriterProjection(runId: string): Promise<{
  workspaces: WriterWorkspaceView[];
  artifacts: WriterArtifactView[];
  conflicts: WriterConflictView[];
  merges: MergeRunView[];
}> {
  const encoded = encodeURIComponent(runId);
  const [rawWorkspaces, rawArtifacts, rawConflicts, rawMerges] = await Promise.all([
    get(`/v1/graph/runs/${encoded}/writer-workspaces`),
    get(`/v1/graph/runs/${encoded}/writer-artifacts`),
    get(`/v1/graph/runs/${encoded}/writer-conflicts`),
    get(`/v1/graph/runs/${encoded}/merge-runs`),
  ]);
  return {
    workspaces: rawWorkspaces.map((item) => {
      const value = record(item, 'writer workspace');
      const rawLease = value.lease === null || value.lease === undefined
        ? null
        : record(value.lease, 'writer lease');
      return {
        writerWorkspaceId: text(value.writer_workspace_id, 'writer_workspace_id'),
        writerKey: text(value.writer_key, 'writer_key'),
        isolationKind: text(value.isolation_kind, 'isolation_kind'),
        isolationRef: text(value.isolation_ref, 'isolation_ref'),
        ownershipPaths: strings(value.ownership_paths),
        lease: rawLease === null ? null : {
          owner: text(rawLease.owner, 'lease owner'),
          fencing: Number(rawLease.fencing),
          expiresAt: typeof rawLease.expires_at === 'string' ? rawLease.expires_at : undefined,
          releasedAt: typeof rawLease.released_at === 'string' ? rawLease.released_at : undefined,
        },
      };
    }),
    artifacts: rawArtifacts.map((item) => {
      const value = record(item, 'writer artifact');
      return {
        writerArtifactId: text(value.writer_artifact_id, 'writer_artifact_id'),
        writerWorkspaceId: text(value.writer_workspace_id, 'writer_workspace_id'),
        artifactKind: text(value.artifact_kind, 'artifact_kind'),
        changedPaths: strings(value.changed_paths),
        testEvidenceRefs: strings(value.test_evidence_refs),
      };
    }),
    conflicts: rawConflicts.map((item) => {
      const value = record(item, 'writer conflict');
      return {
        conflictId: text(value.conflict_id, 'conflict_id'),
        status: text(value.status, 'conflict status'),
        paths: strings(value.paths),
      };
    }),
    merges: rawMerges.map((item) => {
      const value = record(item, 'merge run');
      return {
        mergeRunId: text(value.merge_run_id, 'merge_run_id'),
        mergeNodeId: text(value.merge_node_id, 'merge_node_id'),
        status: text(value.status, 'merge status'),
        resultArtifactRef: typeof value.result_artifact_ref === 'string'
          ? value.result_artifact_ref : undefined,
        errorCode: typeof value.error_code === 'string' ? value.error_code : undefined,
      };
    }),
  };
}
