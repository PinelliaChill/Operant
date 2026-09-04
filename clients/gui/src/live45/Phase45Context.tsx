import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import type { Phase45 } from '@operant/sdk';
import { useOperant } from '../context/ClientContext';
import {
  Phase45LiveAdapter,
  normalizePhase45Error,
  type LiveMcpServer,
  type LiveMcpTool,
  type LiveMcpWorkspaceRoot,
  type LiveMcpReceipt,
  type LivePolicyEvaluation,
  type LiveSecurityAuditFact,
  type LiveSkillCandidate,
  type LiveSkillIssue,
  type Phase45UiError,
} from './phase45Adapter';
import { boundedAuditFacts, preservePhase45Projection } from './phase45State';

type Phase45LoadPhase = 'idle' | 'loading' | 'ready' | 'error';
export const SECURITY_AUDIT_LIMIT = 100;

interface Phase45ContextValue {
  phase: Phase45LoadPhase;
  skills: LiveSkillCandidate[];
  skillIssues: LiveSkillIssue[];
  servers: LiveMcpServer[];
  workspaceRoots: LiveMcpWorkspaceRoot[];
  toolsByServer: Record<string, LiveMcpTool[]>;
  toolResults: Record<string, unknown>;
  mcpIntervention?: LiveMcpIntervention;
  mcpReceipt?: LiveMcpReceipt;
  auditsByAction: Record<string, LiveSecurityAuditFact[]>;
  auditLoadingActionHash?: string;
  auditError?: Phase45UiError;
  error?: Phase45UiError;
  actionLabel?: string;
  refresh: () => Promise<void>;
  discoverSkills: () => Promise<void>;
  createMcpServer: (request: Phase45.McpServerBody) => Promise<boolean>;
  startMcpServer: (id: string) => Promise<boolean>;
  stopMcpServer: (id: string) => Promise<boolean>;
  deleteMcpServer: (id: string) => Promise<boolean>;
  loadMcpTools: (id: string) => Promise<void>;
  callMcpTool: (id: string, toolName: string, argumentsValue: Record<string, unknown>) => Promise<boolean>;
  decideMcpApproval: (approved: boolean) => Promise<void>;
  loadMcpReceipt: (actionHash: string) => Promise<void>;
  explainPolicy: (request: Phase45.NormalizeActionBody) => Promise<LivePolicyEvaluation | undefined>;
  loadSecurityAudit: (actionHash: string) => Promise<void>;
}

type PendingMcpOperation =
  | { kind: 'start'; serverId: string }
  | { kind: 'call'; serverId: string; toolName: string; argumentsValue: Record<string, unknown> };

export interface LiveMcpIntervention {
  status: 'approval_required' | 'denied' | 'outcome_unknown';
  operation: PendingMcpOperation;
  approvalId?: string;
  actionHash?: string;
  reasonCode?: string;
  message: string;
}

const Phase45Context = createContext<Phase45ContextValue | null>(null);

export const Phase45Provider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { clientMode, phase45Client, addNotification } = useOperant();
  const adapter = useMemo(() => new Phase45LiveAdapter(phase45Client), [phase45Client]);
  const [phase, setPhase] = useState<Phase45LoadPhase>('idle');
  const [skills, setSkills] = useState<LiveSkillCandidate[]>([]);
  const [skillIssues, setSkillIssues] = useState<LiveSkillIssue[]>([]);
  const [servers, setServers] = useState<LiveMcpServer[]>([]);
  const [workspaceRoots, setWorkspaceRoots] = useState<LiveMcpWorkspaceRoot[]>([]);
  const [toolsByServer, setToolsByServer] = useState<Record<string, LiveMcpTool[]>>({});
  const [toolResults, setToolResults] = useState<Record<string, unknown>>({});
  const [mcpIntervention, setMcpIntervention] = useState<LiveMcpIntervention>();
  const [mcpReceipt, setMcpReceipt] = useState<LiveMcpReceipt>();
  const [auditsByAction, setAuditsByAction] = useState<Record<string, LiveSecurityAuditFact[]>>({});
  const [auditLoadingActionHash, setAuditLoadingActionHash] = useState<string>();
  const [auditError, setAuditError] = useState<Phase45UiError>();
  const [error, setError] = useState<Phase45UiError>();
  const [actionLabel, setActionLabel] = useState<string>();

  const fail = useCallback((value: unknown) => {
    const detail = normalizePhase45Error(value);
    setError(detail);
    setPhase('error');
    return detail;
  }, []);

  const refresh = useCallback(async () => {
    if (clientMode !== 'live') return;
    setPhase('loading');
    setError(undefined);
    try {
      await adapter.connect();
      const [nextSkills, nextServers, nextWorkspaceRoots] = await Promise.all([
        adapter.listSkills(), adapter.listMcpServers(), adapter.listMcpWorkspaceRoots(),
      ]);
      setSkills(nextSkills);
      setServers(nextServers);
      setWorkspaceRoots(nextWorkspaceRoots);
      setPhase('ready');
    } catch (value: unknown) {
      setSkills(preservePhase45Projection);
      setServers(preservePhase45Projection);
      setWorkspaceRoots(preservePhase45Projection);
      setToolsByServer(preservePhase45Projection);
      fail(value);
    }
  }, [adapter, clientMode, fail]);

  useEffect(() => {
    if (clientMode === 'live') void refresh();
    else {
      setPhase('idle');
      setSkills([]);
      setSkillIssues([]);
      setServers([]);
      setWorkspaceRoots([]);
      setToolsByServer({});
      setToolResults({});
      setMcpIntervention(undefined);
      setMcpReceipt(undefined);
      setAuditsByAction({});
      setAuditLoadingActionHash(undefined);
      setAuditError(undefined);
      setError(undefined);
      setActionLabel(undefined);
    }
  }, [clientMode, refresh]);

  const runAction = useCallback(async (label: string, action: () => Promise<void>): Promise<boolean> => {
    if (clientMode !== 'live' || actionLabel) return false;
    setActionLabel(label);
    setError(undefined);
    try {
      await action();
      setPhase('ready');
      addNotification('success', `${label}已由 Core 确认。`);
      return true;
    } catch (value: unknown) {
      const detail = fail(value);
      addNotification('error', `${label}失败：${detail.message}`);
      return false;
    } finally {
      setActionLabel(undefined);
    }
  }, [actionLabel, addNotification, clientMode, fail]);

  const discoverSkills = useCallback(async () => {
    await runAction('扫描技能', async () => {
      const result = await adapter.discoverSkills();
      setSkills(result.candidates);
      setSkillIssues(result.issues);
    });
  }, [adapter, runAction]);

  const createMcpServer = useCallback((request: Phase45.McpServerBody) => runAction('保存 MCP 配置', async () => {
    await adapter.createMcpServer(request);
    setServers(await adapter.listMcpServers());
  }), [adapter, runAction]);

  const executeMcpOperation = useCallback(async (operation: PendingMcpOperation) => {
    if (operation.kind === 'start') {
      await adapter.startMcpServer(operation.serverId);
      setServers(await adapter.listMcpServers());
      setToolsByServer((current) => ({ ...current, [operation.serverId]: [] }));
      return;
    }
    const result = await adapter.callMcpTool(
      operation.serverId,
      operation.toolName,
      { arguments: operation.argumentsValue },
    );
    setToolResults((current) => ({
      ...current,
      [`${operation.serverId}\0${operation.toolName}`]: result.result,
    }));
  }, [adapter]);

  const handleMcpFailure = useCallback((operation: PendingMcpOperation, value: unknown) => {
    const detail = fail(value);
    const approvalRequired = detail.code === 'approval_required' || detail.code === 'mcp.approval_required';
    if (approvalRequired && detail.approvalId) {
      setMcpIntervention({
        status: 'approval_required', operation, approvalId: detail.approvalId,
        actionHash: detail.actionHash, reasonCode: detail.reasonCode, message: detail.message,
      });
    } else if (detail.outcomeUnknown) {
      setMcpIntervention((current) => ({
        status: 'outcome_unknown', operation,
        actionHash: detail.actionHash ?? current?.actionHash,
        reasonCode: detail.reasonCode, message: detail.message,
      }));
    }
    return detail;
  }, [fail]);

  const startMcpServer = useCallback(async (id: string) => {
    if (clientMode !== 'live' || actionLabel) return false;
    const operation: PendingMcpOperation = { kind: 'start', serverId: id };
    setActionLabel('启动 MCP 服务');
    setError(undefined);
    setMcpIntervention(undefined);
    setMcpReceipt(undefined);
    try {
      await executeMcpOperation(operation);
      setMcpIntervention(undefined);
      setPhase('ready');
      addNotification('success', '启动 MCP 服务已由 Core 确认。');
      return true;
    } catch (value: unknown) {
      const detail = handleMcpFailure(operation, value);
      addNotification('error', `启动 MCP 服务失败：${detail.message}`);
      return false;
    } finally {
      setActionLabel(undefined);
    }
  }, [actionLabel, addNotification, clientMode, executeMcpOperation, handleMcpFailure]);

  const stopMcpServer = useCallback((id: string) => runAction('停止 MCP 服务', async () => {
    await adapter.stopMcpServer(id);
    setServers(await adapter.listMcpServers());
  }), [adapter, runAction]);

  const deleteMcpServer = useCallback((id: string) => runAction('删除 MCP 配置', async () => {
    await adapter.deleteMcpServer(id);
    setServers(await adapter.listMcpServers());
    setToolsByServer((current) => {
      const next = { ...current };
      delete next[id];
      return next;
    });
  }), [adapter, runAction]);

  const loadMcpTools = useCallback(async (id: string) => {
    await runAction('刷新 MCP 工具', async () => {
      const tools = await adapter.listMcpTools(id);
      setToolsByServer((current) => ({ ...current, [id]: tools }));
    });
  }, [adapter, runAction]);

  const callMcpTool = useCallback(async (id: string, toolName: string, argumentsValue: Record<string, unknown>) => {
    if (clientMode !== 'live' || actionLabel) return false;
    const operation: PendingMcpOperation = { kind: 'call', serverId: id, toolName, argumentsValue };
    setActionLabel(`调用 ${toolName}`);
    setError(undefined);
    setMcpIntervention(undefined);
    setMcpReceipt(undefined);
    try {
      await executeMcpOperation(operation);
      setMcpIntervention(undefined);
      setPhase('ready');
      addNotification('success', `MCP 工具 ${toolName} 调用完成。`);
      return true;
    } catch (value: unknown) {
      const detail = handleMcpFailure(operation, value);
      addNotification('error', detail.outcomeUnknown ? '调用结果未知，必须人工核对。' : `MCP 工具调用失败：${detail.message}`);
      return false;
    } finally {
      setActionLabel(undefined);
    }
  }, [actionLabel, addNotification, clientMode, executeMcpOperation, handleMcpFailure]);

  const decideMcpApproval = useCallback(async (approved: boolean) => {
    const pending = mcpIntervention;
    if (clientMode !== 'live' || actionLabel || pending?.status !== 'approval_required' || !pending.approvalId) return;
    setActionLabel(approved ? '允许并重试 MCP 操作' : '拒绝 MCP 操作');
    setError(undefined);
    try {
      await adapter.decideMcpApproval(pending.approvalId, approved);
      if (!approved) {
        setMcpIntervention({ ...pending, status: 'denied', message: '用户已拒绝；原操作未执行。' });
        addNotification('info', '已拒绝 MCP 操作。');
        return;
      }
      // The adapter deliberately retains the original mutation key after an
      // approval_required response. This retry therefore uses the exact same key.
      await executeMcpOperation(pending.operation);
      if (pending.operation.kind === 'call' && pending.actionHash) {
        setMcpReceipt(await adapter.getMcpActionReceipt(pending.actionHash));
      }
      setMcpIntervention(undefined);
      setPhase('ready');
      addNotification('success', '审批已允许，原 MCP 操作已使用同一 Idempotency-Key 重试。');
    } catch (value: unknown) {
      const detail = handleMcpFailure(pending.operation, value);
      addNotification('error', detail.outcomeUnknown ? '重试结果未知，必须人工核对。' : `审批或重试失败：${detail.message}`);
    } finally {
      setActionLabel(undefined);
    }
  }, [actionLabel, adapter, addNotification, clientMode, executeMcpOperation, handleMcpFailure, mcpIntervention]);

  const loadMcpReceipt = useCallback(async (actionHash: string) => {
    if (clientMode !== 'live' || !/^[0-9a-f]{64}$/.test(actionHash)) return;
    setActionLabel('读取 MCP Receipt');
    setError(undefined);
    try {
      setMcpReceipt(await adapter.getMcpActionReceipt(actionHash));
    } catch (value: unknown) {
      fail(value);
    } finally {
      setActionLabel(undefined);
    }
  }, [adapter, clientMode, fail]);

  const explainPolicy = useCallback(async (request: Phase45.NormalizeActionBody) => {
    let result: LivePolicyEvaluation | undefined;
    await runAction('Policy 检查', async () => { result = await adapter.explainPolicy(request); });
    return result;
  }, [adapter, runAction]);

  const loadSecurityAudit = useCallback(async (actionHash: string) => {
    if (clientMode !== 'live' || !/^[0-9a-f]{64}$/.test(actionHash)) return;
    setAuditLoadingActionHash(actionHash);
    setAuditError(undefined);
    try {
      const facts = await adapter.listSecurityAudit(actionHash, 0, SECURITY_AUDIT_LIMIT);
      setAuditsByAction((current) => ({
        ...current,
        [actionHash]: boundedAuditFacts(facts, SECURITY_AUDIT_LIMIT),
      }));
    } catch (value: unknown) {
      setAuditError(normalizePhase45Error(value));
    } finally {
      setAuditLoadingActionHash(undefined);
    }
  }, [adapter, clientMode]);

  return <Phase45Context.Provider value={{
    phase, skills, skillIssues, servers, workspaceRoots, toolsByServer, toolResults,
    mcpIntervention, mcpReceipt, auditsByAction,
    auditLoadingActionHash, auditError, error, actionLabel, refresh,
    discoverSkills, createMcpServer, startMcpServer, stopMcpServer, deleteMcpServer,
    loadMcpTools, callMcpTool, decideMcpApproval, loadMcpReceipt,
    explainPolicy, loadSecurityAudit,
  }}>{children}</Phase45Context.Provider>;
};

export function usePhase45(): Phase45ContextValue {
  const value = useContext(Phase45Context);
  if (!value) throw new Error('usePhase45 must be used within Phase45Provider');
  return value;
}
