// Generated from the four OpenAPI schemas by scripts/generate-native-core-routes.mjs.
// Do not hand-edit this route table.
export const nativeCoreRoutes = [
  {
    "operationId": "acquireWriterLease",
    "method": "POST",
    "pathTemplate": "/v1/writer-workspaces/{workspace_id}/lease",
    "deviceAuth": false
  },
  {
    "operationId": "actLocalControlSession",
    "method": "POST",
    "pathTemplate": "/v1/local-control/sessions/{session_id}/act",
    "deviceAuth": false
  },
  {
    "operationId": "addSkillSource",
    "method": "POST",
    "pathTemplate": "/v1/setup/skill-sources",
    "deviceAuth": false
  },
  {
    "operationId": "bootstrapSetup",
    "method": "POST",
    "pathTemplate": "/v1/setup/bootstrap",
    "deviceAuth": false
  },
  {
    "operationId": "cancelModelOAuth",
    "method": "DELETE",
    "pathTemplate": "/v1/setup/oauth/{attempt_id}",
    "deviceAuth": false
  },
  {
    "operationId": "closeLocalControlSession",
    "method": "POST",
    "pathTemplate": "/v1/local-control/sessions/{session_id}/close",
    "deviceAuth": false
  },
  {
    "operationId": "closeRemoteSession",
    "method": "POST",
    "pathTemplate": "/v1/remote-control/sessions/{session_id}/close",
    "deviceAuth": false
  },
  {
    "operationId": "createContainerWriter",
    "method": "POST",
    "pathTemplate": "/v1/writer-workspaces/{workspace_id}/container",
    "deviceAuth": false
  },
  {
    "operationId": "createMergeRun",
    "method": "POST",
    "pathTemplate": "/v1/merge-runs",
    "deviceAuth": false
  },
  {
    "operationId": "createModelConnection",
    "method": "POST",
    "pathTemplate": "/v1/setup/connections",
    "deviceAuth": false
  },
  {
    "operationId": "createPairingChallenge",
    "method": "POST",
    "pathTemplate": "/v1/remote-control/pairing-challenges",
    "deviceAuth": false
  },
  {
    "operationId": "createRemoteSession",
    "method": "POST",
    "pathTemplate": "/v1/remote-control/sessions",
    "deviceAuth": false
  },
  {
    "operationId": "createWriterWorkspace",
    "method": "POST",
    "pathTemplate": "/v1/graph/runs/{run_id}/writer-workspaces",
    "deviceAuth": false
  },
  {
    "operationId": "deleteModelConnection",
    "method": "DELETE",
    "pathTemplate": "/v1/setup/connections/{connection_id}",
    "deviceAuth": false
  },
  {
    "operationId": "detectWriterConflicts",
    "method": "POST",
    "pathTemplate": "/v1/graph/runs/{run_id}/writer-conflicts/detect",
    "deviceAuth": false
  },
  {
    "operationId": "disableExtension",
    "method": "POST",
    "pathTemplate": "/v1/extensions/{plugin_id}/disable",
    "deviceAuth": false
  },
  {
    "operationId": "discoverConnectionModels",
    "method": "POST",
    "pathTemplate": "/v1/setup/connections/{connection_id}/models",
    "deviceAuth": false
  },
  {
    "operationId": "driveLocalControlSession",
    "method": "POST",
    "pathTemplate": "/v1/local-control/sessions/{session_id}/drive",
    "deviceAuth": false
  },
  {
    "operationId": "enableExtension",
    "method": "POST",
    "pathTemplate": "/v1/extensions/{plugin_id}/enable",
    "deviceAuth": false
  },
  {
    "operationId": "enableRemoteHost",
    "method": "POST",
    "pathTemplate": "/v1/remote-control/hosts/enable",
    "deviceAuth": false
  },
  {
    "operationId": "executeExtensionCommand",
    "method": "POST",
    "pathTemplate": "/v1/workbench/threads/{thread_id}/extension-commands",
    "deviceAuth": false
  },
  {
    "operationId": "executeSkillCommand",
    "method": "POST",
    "pathTemplate": "/v1/workbench/threads/{thread_id}/skill-commands",
    "deviceAuth": false
  },
  {
    "operationId": "finalizeMergeRun",
    "method": "POST",
    "pathTemplate": "/v1/merge-runs/{merge_run_id}/finalize",
    "deviceAuth": false
  },
  {
    "operationId": "getCallerRequest",
    "method": "GET",
    "pathTemplate": "/v1/local-callers/requests/result",
    "deviceAuth": false
  },
  {
    "operationId": "getContainerWriter",
    "method": "GET",
    "pathTemplate": "/v1/writer-workspaces/{workspace_id}/container",
    "deviceAuth": false
  },
  {
    "operationId": "getConversationMetadata",
    "method": "GET",
    "pathTemplate": "/v1/setup/conversations/{thread_id}/metadata",
    "deviceAuth": false
  },
  {
    "operationId": "getLocalControlArtifact",
    "method": "GET",
    "pathTemplate": "/v1/local-control/artifacts/{job_id}",
    "deviceAuth": false
  },
  {
    "operationId": "getMergeRun",
    "method": "GET",
    "pathTemplate": "/v1/merge-runs/{merge_run_id}",
    "deviceAuth": false
  },
  {
    "operationId": "getModelConnectionRequest",
    "method": "GET",
    "pathTemplate": "/v1/setup/connections/requests/{request_id}",
    "deviceAuth": false
  },
  {
    "operationId": "getModelOAuthStatus",
    "method": "GET",
    "pathTemplate": "/v1/setup/oauth/{attempt_id}",
    "deviceAuth": false
  },
  {
    "operationId": "getRemoteCommand",
    "method": "GET",
    "pathTemplate": "/v1/remote-control/commands/{command_id}",
    "deviceAuth": false
  },
  {
    "operationId": "getRemoteHost",
    "method": "GET",
    "pathTemplate": "/v1/remote-control/hosts/{host_id}",
    "deviceAuth": false
  },
  {
    "operationId": "getSetupState",
    "method": "GET",
    "pathTemplate": "/v1/setup/state",
    "deviceAuth": false
  },
  {
    "operationId": "initializeConversation",
    "method": "POST",
    "pathTemplate": "/v1/setup/conversations",
    "deviceAuth": false
  },
  {
    "operationId": "inspectExtension",
    "method": "POST",
    "pathTemplate": "/v1/extensions/inspect",
    "deviceAuth": false
  },
  {
    "operationId": "installExtension",
    "method": "POST",
    "pathTemplate": "/v1/extensions/install",
    "deviceAuth": false
  },
  {
    "operationId": "installLocalCapabilityPlugin",
    "method": "POST",
    "pathTemplate": "/v1/local-control/plugins",
    "deviceAuth": false
  },
  {
    "operationId": "listCallerDevices",
    "method": "GET",
    "pathTemplate": "/v1/local-callers/devices",
    "deviceAuth": false
  },
  {
    "operationId": "listConversationLocalControlSessions",
    "method": "GET",
    "pathTemplate": "/v1/setup/local-control/sessions",
    "deviceAuth": false
  },
  {
    "operationId": "listConversationMetadata",
    "method": "GET",
    "pathTemplate": "/v1/setup/conversations/metadata",
    "deviceAuth": false
  },
  {
    "operationId": "listExtensionCommands",
    "method": "GET",
    "pathTemplate": "/v1/workbench/extensions/commands",
    "deviceAuth": false
  },
  {
    "operationId": "listExtensionDrivers",
    "method": "GET",
    "pathTemplate": "/v1/extensions/drivers",
    "deviceAuth": false
  },
  {
    "operationId": "listExtensions",
    "method": "GET",
    "pathTemplate": "/v1/extensions",
    "deviceAuth": false
  },
  {
    "operationId": "listLocalApplications",
    "method": "GET",
    "pathTemplate": "/v1/setup/local-apps",
    "deviceAuth": false
  },
  {
    "operationId": "listLocalCapabilityPlugins",
    "method": "GET",
    "pathTemplate": "/v1/local-control/plugins",
    "deviceAuth": false
  },
  {
    "operationId": "listLocalControlSessions",
    "method": "GET",
    "pathTemplate": "/v1/local-control/sessions",
    "deviceAuth": false
  },
  {
    "operationId": "listMergeRuns",
    "method": "GET",
    "pathTemplate": "/v1/graph/runs/{run_id}/merge-runs",
    "deviceAuth": false
  },
  {
    "operationId": "listModelConnections",
    "method": "GET",
    "pathTemplate": "/v1/setup/connections",
    "deviceAuth": false
  },
  {
    "operationId": "listRemoteControlEvents",
    "method": "GET",
    "pathTemplate": "/v1/remote-control/events",
    "deviceAuth": false
  },
  {
    "operationId": "listRemoteDevices",
    "method": "GET",
    "pathTemplate": "/v1/remote-control/devices",
    "deviceAuth": false
  },
  {
    "operationId": "listRemoteGatewayConnections",
    "method": "GET",
    "pathTemplate": "/v1/remote-control/gateway/connections",
    "deviceAuth": false
  },
  {
    "operationId": "listRemoteHosts",
    "method": "GET",
    "pathTemplate": "/v1/remote-control/hosts",
    "deviceAuth": false
  },
  {
    "operationId": "listRemoteSessions",
    "method": "GET",
    "pathTemplate": "/v1/remote-control/sessions",
    "deviceAuth": false
  },
  {
    "operationId": "listSkillCommands",
    "method": "GET",
    "pathTemplate": "/v1/workbench/threads/{thread_id}/skill-commands",
    "deviceAuth": false
  },
  {
    "operationId": "listSkillSources",
    "method": "GET",
    "pathTemplate": "/v1/setup/skill-sources",
    "deviceAuth": false
  },
  {
    "operationId": "listTeamTemplates",
    "method": "GET",
    "pathTemplate": "/v1/setup/team-templates",
    "deviceAuth": false
  },
  {
    "operationId": "listUnknownLocalControlJobs",
    "method": "GET",
    "pathTemplate": "/v1/local-control/unknown-jobs",
    "deviceAuth": false
  },
  {
    "operationId": "listWriterArtifacts",
    "method": "GET",
    "pathTemplate": "/v1/graph/runs/{run_id}/writer-artifacts",
    "deviceAuth": false
  },
  {
    "operationId": "listWriterConflicts",
    "method": "GET",
    "pathTemplate": "/v1/graph/runs/{run_id}/writer-conflicts",
    "deviceAuth": false
  },
  {
    "operationId": "listWriterWorkspaces",
    "method": "GET",
    "pathTemplate": "/v1/graph/runs/{run_id}/writer-workspaces",
    "deviceAuth": false
  },
  {
    "operationId": "observeLocalControlSession",
    "method": "POST",
    "pathTemplate": "/v1/local-control/sessions/{session_id}/observe",
    "deviceAuth": false
  },
  {
    "operationId": "openConversationLocalControl",
    "method": "POST",
    "pathTemplate": "/v1/setup/local-control/sessions",
    "deviceAuth": false
  },
  {
    "operationId": "openLocalControlSession",
    "method": "POST",
    "pathTemplate": "/v1/local-control/sessions",
    "deviceAuth": false
  },
  {
    "operationId": "pairRemoteDevice",
    "method": "POST",
    "pathTemplate": "/v1/remote-control/devices/pair",
    "deviceAuth": true
  },
  {
    "operationId": "publishWriterArtifact",
    "method": "POST",
    "pathTemplate": "/v1/writer-workspaces/{workspace_id}/artifacts",
    "deviceAuth": false
  },
  {
    "operationId": "queryRemoteSessionResult",
    "method": "POST",
    "pathTemplate": "/v1/remote-control/session-query",
    "deviceAuth": true
  },
  {
    "operationId": "reconcileContainerWriter",
    "method": "POST",
    "pathTemplate": "/v1/writer-workspaces/{workspace_id}/container/reconcile",
    "deviceAuth": false
  },
  {
    "operationId": "reconcileMergeRun",
    "method": "POST",
    "pathTemplate": "/v1/merge-runs/{merge_run_id}/reconcile",
    "deviceAuth": false
  },
  {
    "operationId": "reconcileRemoteCommand",
    "method": "POST",
    "pathTemplate": "/v1/remote-control/commands/{command_id}/reconcile",
    "deviceAuth": false
  },
  {
    "operationId": "reconcileUnknownLocalControlJob",
    "method": "POST",
    "pathTemplate": "/v1/local-control/unknown-jobs/{job_id}/reconcile",
    "deviceAuth": false
  },
  {
    "operationId": "releaseWriterLease",
    "method": "POST",
    "pathTemplate": "/v1/writer-workspaces/{workspace_id}/lease/release",
    "deviceAuth": false
  },
  {
    "operationId": "removeContainerWriter",
    "method": "POST",
    "pathTemplate": "/v1/writer-workspaces/{workspace_id}/container/remove",
    "deviceAuth": false
  },
  {
    "operationId": "removeSkillSource",
    "method": "DELETE",
    "pathTemplate": "/v1/setup/skill-sources/{root_ref}",
    "deviceAuth": false
  },
  {
    "operationId": "renameConversation",
    "method": "PATCH",
    "pathTemplate": "/v1/setup/conversations/{thread_id}/metadata",
    "deviceAuth": false
  },
  {
    "operationId": "renewWriterLease",
    "method": "POST",
    "pathTemplate": "/v1/writer-workspaces/{workspace_id}/lease/renew",
    "deviceAuth": false
  },
  {
    "operationId": "resolveMergeConflict",
    "method": "POST",
    "pathTemplate": "/v1/writer-conflicts/{conflict_id}/resolve",
    "deviceAuth": false
  },
  {
    "operationId": "resumeLocalControlSession",
    "method": "POST",
    "pathTemplate": "/v1/local-control/sessions/{session_id}/resume",
    "deviceAuth": false
  },
  {
    "operationId": "revokeCallerDevice",
    "method": "POST",
    "pathTemplate": "/v1/local-callers/devices/{device_id}/revoke",
    "deviceAuth": false
  },
  {
    "operationId": "revokeRemoteDevice",
    "method": "POST",
    "pathTemplate": "/v1/remote-control/devices/{device_id}/revoke",
    "deviceAuth": false
  },
  {
    "operationId": "selectConnectionModel",
    "method": "POST",
    "pathTemplate": "/v1/setup/connections/{connection_id}/profiles",
    "deviceAuth": false
  },
  {
    "operationId": "setLocalCapabilityPluginEnabled",
    "method": "POST",
    "pathTemplate": "/v1/local-control/plugins/{plugin_id}/enabled",
    "deviceAuth": false
  },
  {
    "operationId": "startContainerWriter",
    "method": "POST",
    "pathTemplate": "/v1/writer-workspaces/{workspace_id}/container/start",
    "deviceAuth": false
  },
  {
    "operationId": "startModelOAuth",
    "method": "POST",
    "pathTemplate": "/v1/setup/oauth/start",
    "deviceAuth": false
  },
  {
    "operationId": "startTemplateTeam",
    "method": "POST",
    "pathTemplate": "/v1/setup/teams/start",
    "deviceAuth": false
  },
  {
    "operationId": "stopContainerWriter",
    "method": "POST",
    "pathTemplate": "/v1/writer-workspaces/{workspace_id}/container/stop",
    "deviceAuth": false
  },
  {
    "operationId": "submitRemoteCommand",
    "method": "POST",
    "pathTemplate": "/v1/remote-control/commands",
    "deviceAuth": true
  },
  {
    "operationId": "takeoverLocalControlSession",
    "method": "POST",
    "pathTemplate": "/v1/local-control/sessions/{session_id}/takeover",
    "deviceAuth": false
  },
  {
    "operationId": "uninstallExtension",
    "method": "DELETE",
    "pathTemplate": "/v1/extensions/{plugin_id}",
    "deviceAuth": false
  },
  {
    "operationId": "uninstallLocalCapabilityPlugin",
    "method": "DELETE",
    "pathTemplate": "/v1/local-control/plugins/{plugin_id}",
    "deviceAuth": false
  }
] as const;
