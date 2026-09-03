import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import type { Phase45 } from '@operant/sdk';
import { useOperant } from '../context/ClientContext';
import {
  Phase45LiveAdapter,
  normalizePhase45Error,
  type LiveMcpServer,
  type LiveMcpTool,
  type LivePolicyEvaluation,
  type LiveSecurityAuditFact,
  type LiveSkillCandidate,
  type LiveSkillIssue,
  type Phase45UiError,
} from './phase45Adapter';
import { boundedAuditFacts } from './phase45State';

type Phase45LoadPhase = 'idle' | 'loading' | 'ready' | 'error';
export const SECURITY_AUDIT_LIMIT = 100;

interface Phase45ContextValue {
  phase: Phase45LoadPhase;
  skills: LiveSkillCandidate[];
  skillIssues: LiveSkillIssue[];
  servers: LiveMcpServer[];
  toolsByServer: Record<string, LiveMcpTool[]>;
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
  explainPolicy: (request: Phase45.NormalizeActionBody) => Promise<LivePolicyEvaluation | undefined>;
  loadSecurityAudit: (actionHash: string) => Promise<void>;
}

const Phase45Context = createContext<Phase45ContextValue | null>(null);

export const Phase45Provider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { clientMode, phase45Client, addNotification } = useOperant();
  const adapter = useMemo(() => new Phase45LiveAdapter(phase45Client), [phase45Client]);
  const [phase, setPhase] = useState<Phase45LoadPhase>('idle');
  const [skills, setSkills] = useState<LiveSkillCandidate[]>([]);
  const [skillIssues, setSkillIssues] = useState<LiveSkillIssue[]>([]);
  const [servers, setServers] = useState<LiveMcpServer[]>([]);
  const [toolsByServer, setToolsByServer] = useState<Record<string, LiveMcpTool[]>>({});
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
      const [nextSkills, nextServers] = await Promise.all([adapter.listSkills(), adapter.listMcpServers()]);
      setSkills(nextSkills);
      setServers(nextServers);
      setPhase('ready');
    } catch (value: unknown) {
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
      setToolsByServer({});
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

  const startMcpServer = useCallback((id: string) => runAction('启动 MCP 服务', async () => {
    await adapter.startMcpServer(id);
    setServers(await adapter.listMcpServers());
    setToolsByServer((current) => ({ ...current, [id]: [] }));
  }), [adapter, runAction]);

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
    phase, skills, skillIssues, servers, toolsByServer, auditsByAction,
    auditLoadingActionHash, auditError, error, actionLabel, refresh,
    discoverSkills, createMcpServer, startMcpServer, stopMcpServer, deleteMcpServer,
    loadMcpTools, explainPolicy, loadSecurityAudit,
  }}>{children}</Phase45Context.Provider>;
};

export function usePhase45(): Phase45ContextValue {
  const value = useContext(Phase45Context);
  if (!value) throw new Error('usePhase45 must be used within Phase45Provider');
  return value;
}
