/**
 * Operant 2.0 Protocol Query Definitions
 * Typed Query Parameters and Projection Return Types
 */

// ----------------------------------------------------------------------------
// Model & Role Queries
// ----------------------------------------------------------------------------

export interface ListModelProfilesQuery {
  enabled_only?: boolean;
}

export interface ListRolesQuery {
  include_inactive?: boolean;
}

// ----------------------------------------------------------------------------
// Session & Thread Queries
// ----------------------------------------------------------------------------

export interface ListSessionsQuery {
  limit?: number;
  workspace?: string;
}

export interface ListThreadsQuery {
  session_id?: string;
  workspace?: string;
  parent_thread_id?: string;
}

export interface GetThreadMessagesQuery {
  thread_id: string;
  cursor?: number;
  limit?: number;
}

export interface GetContextRevisionQuery {
  thread_id: string;
  revision_id?: string;
}

// ----------------------------------------------------------------------------
// Workflow & Graph Queries
// ----------------------------------------------------------------------------

export interface ListGraphDraftsQuery {
  workspace?: string;
}

export interface ListGraphRevisionsQuery {
  workspace?: string;
  draft_id?: string;
}

export interface ListWorkflowRunsQuery {
  workspace?: string;
  status?: string;
  limit?: number;
}

export interface GetWorkflowTraceQuery {
  workflow_run_id: string;
}

// ----------------------------------------------------------------------------
// Approval Queries
// ----------------------------------------------------------------------------

export interface ListApprovalsQuery {
  session_id?: string;
  status?: 'pending' | 'decided' | 'all';
}

// ----------------------------------------------------------------------------
// Remote Control Queries
// ----------------------------------------------------------------------------

export interface ListRemoteHostsQuery {
  online_only?: boolean;
}

export interface ListRemoteDevicesQuery {
  host_id?: string;
  include_revoked?: boolean;
}

// ----------------------------------------------------------------------------
// Memory Queries
// ----------------------------------------------------------------------------

export interface SearchMemoriesQuery {
  session_id: string;
  query: string;
  project_scope?: string;
  include_candidates?: boolean;
}

// ----------------------------------------------------------------------------
// Evaluation Queries
// ----------------------------------------------------------------------------

export interface ListEvaluationSuitesQuery {
  status?: string;
  limit?: number;
}

export interface ListEvaluationRunsQuery {
  suite_id?: string;
  status?: string;
  limit?: number;
}

export interface ListEvaluationResultsQuery {
  evaluation_run_id: string;
}
