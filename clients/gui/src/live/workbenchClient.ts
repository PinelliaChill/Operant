import { WorkbenchClient } from '../../../../sdk/typescript-client/workbench.generated';
import type {
  ChildAgentView,
  WorkbenchAgentMessage,
  WorkbenchCommandRegistry,
  WorkbenchCommandResult,
  WorkbenchContextView,
  WorkbenchReferenceView,
  WorkbenchFileContent,
  WorkbenchFileDiff,
  TerminalView,
  WorkbenchArtifactOptions,
  ResourcePolicyPatch,
} from '../../../../sdk/typescript-client/workbench.generated';
import { currentBrowserOrigin } from '../lib/liveBaseUrl';
import { resolveTerminalStreamUrl, terminalStreamProtocols } from './terminalStreamUrl';

export type {
  ChildAgentView,
  WorkbenchCommandRegistry,
  WorkbenchCommandResult,
} from '../../../../sdk/typescript-client/workbench.generated';
export type WorkbenchMessage = WorkbenchAgentMessage;
export type WorkbenchReference = WorkbenchReferenceView;
export type WorkbenchContext = WorkbenchContextView;

let client: WorkbenchClient | null = null;

/** Every Live workbench operation uses the generated, negotiated Core client. */
function liveClient(): WorkbenchClient {
  if (!client) client = new WorkbenchClient(currentBrowserOrigin());
  return client;
}

export const workbenchClient = {
  listResources: (threadId: string, cursors: Record<string, number> = {}) =>
    liveClient().listWorkbenchResources(threadId, {
      afterArtifact: cursors.after_artifact,
      afterRevision: cursors.after_revision,
      afterCompaction: cursors.after_compaction,
    }),
  updateResourcePolicy: (threadId: string, policy: ResourcePolicyPatch, key: string) =>
    liveClient().updateWorkbenchResourcePolicy(threadId, policy, { idempotencyKey: key }),
  confirmResources: (threadId: string, completed: boolean, key: string) =>
    liveClient().confirmWorkbenchResources(threadId, { completed }, { idempotencyKey: key }),
  pinResource: (threadId: string, resourceId: string, pinned: boolean, key: string) =>
    liveClient().setWorkbenchResourcePin(threadId, resourceId, { pinned }, { idempotencyKey: key }),
  previewResourceCleanup: (threadId: string, resourceIds: string[]) =>
    liveClient().previewWorkbenchResourceCleanup(threadId, { resource_ids: resourceIds, mode: 'manual' }),
  cleanupResources: (threadId: string, resourceIds: string[], key: string) =>
    liveClient().cleanupWorkbenchResources(threadId, { resource_ids: resourceIds, mode: 'manual' }, { idempotencyKey: key }),
  listChildren: (parentId: string): Promise<ChildAgentView[]> =>
    liveClient().listWorkbenchChildAgents(parentId),
  createChild: (parentId: string, input: {
    task: string; role_id?: string; model_profile_id?: string; effort?: string;
  }, key: string): Promise<ChildAgentView> =>
    liveClient().createWorkbenchChildAgent(parentId, input, { idempotencyKey: key }),
  cancelChild: (threadId: string, key: string): Promise<ChildAgentView | null> =>
    liveClient().cancelWorkbenchThread(threadId, { idempotencyKey: key }),
  listMessages: (threadId: string): Promise<WorkbenchAgentMessage[]> =>
    liveClient().listWorkbenchAgentMessages(threadId),
  sendMessage: (threadId: string, input: {
    recipient_thread_id: string; body: string; reply_to?: string; idempotency_key: string;
  }): Promise<WorkbenchAgentMessage> =>
    liveClient().sendWorkbenchAgentMessage(threadId, input, { idempotencyKey: input.idempotency_key }),
  listCommands: (): Promise<WorkbenchCommandRegistry> =>
    liveClient().listWorkbenchCommands(),
  getContext: (threadId: string): Promise<WorkbenchContextView> =>
    liveClient().getWorkbenchContext(threadId),
  createReference: (threadId: string, input: { kind: 'file' | 'thread' | 'artifact'; target: string; max_tokens?: number }, key: string): Promise<WorkbenchReferenceView> =>
    liveClient().createWorkbenchReference(threadId, input, { idempotencyKey: key }),
  listReferenceArtifacts: (threadId: string, afterCursor?: number): Promise<WorkbenchArtifactOptions> =>
    liveClient().listWorkbenchReferenceArtifacts(threadId, { afterCursor, limit: 50 }),
  executeCommand: (threadId: string, input: { text: string; registry_version?: string; reviewer_role_id?: string }, key: string): Promise<WorkbenchCommandResult> =>
    liveClient().executeWorkbenchCommand(threadId, input, { idempotencyKey: key }),
  getFileContent: (workspaceId: string, path: string, maxBytes: number): Promise<WorkbenchFileContent> =>
    liveClient().getWorkbenchFileContent(workspaceId, { path, maxBytes }),
  getFileDiff: (workspaceId: string, path: string, maxBytes: number): Promise<WorkbenchFileDiff> =>
    liveClient().getWorkbenchFileDiff(workspaceId, { path, maxBytes }),
  createTerminal: (threadId: string, size: { cols: number; rows: number }, key: string): Promise<TerminalView> =>
    liveClient().createWorkbenchTerminal(threadId, { ...size, idempotency_key: key }, { idempotencyKey: key }),
  getTerminal: (terminalId: string): Promise<TerminalView> =>
    liveClient().getWorkbenchTerminal(terminalId),
  closeTerminal: (terminalId: string): Promise<TerminalView> =>
    liveClient().deleteWorkbenchTerminal(terminalId),
  terminalStreamUrl: (terminalId: string): string => {
    return resolveTerminalStreamUrl(currentBrowserOrigin(), terminalId);
  },
  terminalStreamProtocols,
};
