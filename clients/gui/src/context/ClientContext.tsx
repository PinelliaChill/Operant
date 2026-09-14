import React, { createContext, useContext, useEffect, useMemo, useState } from 'react';
import { MockClient, OperantClient, Phase1EClient, Phase23Client, Phase45Client, Phase56Client } from '@operant/sdk';
import { B2Client } from '../../../../sdk/typescript-client/b2.generated';
import { B23Client } from '../../../../sdk/typescript-client/b2_3.generated';
import { B24Client } from '../../../../sdk/typescript-client/b2_4.generated';
import { formatTime } from '../lib/format';
import { currentBrowserOrigin } from '../lib/liveBaseUrl';

export type ClientMode = 'mock' | 'live';
export type ConnectionStatus = 'connected' | 'reconnecting' | 'disconnected' | 'mock_active';
export type InspectorTab = 'context' | 'diff' | 'terminal' | 'audit';

export interface AppNotification {
  id: string;
  type: 'info' | 'success' | 'warn' | 'error';
  message: string;
  timestamp: string;
}

const DEFAULT_WORKSPACE = '/Users/bigo/agentworkspace/codexworkspace/operant';

interface ClientContextValue {
  /** Legacy client retained for explicitly rendered Demo surfaces only. */
  client: OperantClient;
  /** The generated Phase 1E client used by every live surface. */
  phase1eClient: Phase1EClient;
  /** Additive Phase 2/3 Graph and local Team client; Phase 1E stays frozen. */
  phase23Client: Phase23Client;
  /** Additive Phase 4/5 security, Skill and MCP client. */
  phase45Client: Phase45Client;
  /** Additive Phase 5B remote and Phase 6 multi-writer client. */
  phase56Client: Phase56Client;
  /** Additive B2 configuration/task/history client; live surfaces only. */
  b2Client: B2Client;
  /** Additive B2-3 management client generated from the management schema. */
  b23Client: B23Client;
  b24Client: B24Client;
  clientMode: ClientMode;
  setClientMode: (mode: ClientMode) => void;
  connectionStatus: ConnectionStatus;
  pendingApprovalCount: number;
  refreshPendingApprovals: () => Promise<void>;
  activeWorkspace: string;
  setActiveWorkspace: (ws: string) => void;
  theme: 'light' | 'dark';
  setTheme: (theme: 'light' | 'dark') => void;
  toggleTheme: () => void;
  selectedThreadId: string | null;
  setSelectedThreadId: (id: string | null) => void;
  selectedWorkflowRunId: string | null;
  setSelectedWorkflowRunId: (id: string | null) => void;
  inspectorTab: InspectorTab;
  setInspectorTab: (tab: InspectorTab) => void;
  inspectorOpen: boolean;
  setInspectorOpen: (open: boolean) => void;
  toggleInspector: () => void;
  sidebarOpen: boolean;
  setSidebarOpen: (open: boolean) => void;
  toggleSidebar: () => void;
  notifications: AppNotification[];
  addNotification: (type: AppNotification['type'], message: string) => void;
  removeNotification: (id: string) => void;
}

const ClientContext = createContext<ClientContextValue | null>(null);

export const ClientProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [clientMode, setClientModeState] = useState<ClientMode>(() => {
    return (localStorage.getItem('operant_client_mode') as ClientMode) || 'mock';
  });

  const [theme, setThemeState] = useState<'light' | 'dark'>(() => {
    return (localStorage.getItem('operant_theme') as 'light' | 'dark') || 'light';
  });

  const [activeWorkspace, setActiveWorkspace] = useState<string>(() => {
    const mode = (localStorage.getItem('operant_client_mode') as ClientMode) || 'mock';
    return mode === 'live' ? '' : DEFAULT_WORKSPACE;
  });

  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>('mock_active');
  const [pendingApprovalCount, setPendingApprovalCount] = useState<number>(0);
  const [selectedThreadId, setSelectedThreadId] = useState<string | null>(() => {
    const mode = (localStorage.getItem('operant_client_mode') as ClientMode) || 'mock';
    return mode === 'live' ? null : 'thread_main_alpha';
  });
  const [selectedWorkflowRunId, setSelectedWorkflowRunId] = useState<string | null>(() => {
    const mode = (localStorage.getItem('operant_client_mode') as ClientMode) || 'mock';
    return mode === 'live' ? null : 'run_operant_001';
  });
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>('context');
  const [inspectorOpen, setInspectorOpen] = useState<boolean>(false);
  const [sidebarOpen, setSidebarOpen] = useState<boolean>(false);
  const [notifications, setNotifications] = useState<AppNotification[]>([]);

  // Keep the legacy client isolated to the explicitly selected Demo surface.
  // LiveProvider receives phase1eClient below and never calls this object.
  const mockClient = useMemo(() => new MockClient(), []);
  // Live requests stay same-origin: Vite proxies /v1 in development and the
  // production reverse proxy owns the same path. Core intentionally has no
  // browser CORS dependency, and Live never falls back to this MockClient.
  const phase1eClient = useMemo(() => new Phase1EClient(currentBrowserOrigin()), []);
  const phase23Client = useMemo(() => new Phase23Client(currentBrowserOrigin()), []);
  const phase45Client = useMemo(() => new Phase45Client(currentBrowserOrigin()), []);
  const phase56Client = useMemo(() => new Phase56Client(currentBrowserOrigin()), []);
  const b2Client = useMemo(() => new B2Client(currentBrowserOrigin()), []);
  const b23Client = useMemo(() => new B23Client(currentBrowserOrigin()), []);
  const b24Client = useMemo(() => new B24Client(currentBrowserOrigin()), []);
  const client: OperantClient = mockClient;

  const setClientMode = (mode: ClientMode) => {
    localStorage.setItem('operant_client_mode', mode);
    setClientModeState(mode);
    if (mode === 'live') {
      setSelectedThreadId(null);
      setSelectedWorkflowRunId(null);
      setActiveWorkspace('');
    } else {
      setSelectedThreadId('thread_main_alpha');
      setSelectedWorkflowRunId('run_operant_001');
      setActiveWorkspace(DEFAULT_WORKSPACE);
    }
    addNotification('info', mode === 'mock' ? '已切换到演示模式（内置演示数据）' : '已切换到实时连接（Core 后端 /v1/*）');
  };

  const setTheme = (t: 'light' | 'dark') => {
    localStorage.setItem('operant_theme', t);
    setThemeState(t);
    document.documentElement.setAttribute('data-theme', t);
  };

  const toggleTheme = () => {
    setTheme(theme === 'light' ? 'dark' : 'light');
  };

  const toggleInspector = () => {
    setInspectorOpen((prev) => !prev);
  };

  const toggleSidebar = () => {
    setSidebarOpen((prev) => !prev);
  };

  const addNotification = (type: AppNotification['type'], message: string) => {
    const id = `notif_${Date.now()}_${Math.random().toString(36).substr(2, 4)}`;
    setNotifications((prev) => [{ id, type, message, timestamp: formatTime(new Date()) }, ...prev.slice(0, 4)]);
    setTimeout(() => {
      removeNotification(id);
    }, 5000);
  };

  const removeNotification = (id: string) => {
    setNotifications((prev) => prev.filter((n) => n.id !== id));
  };

  const refreshPendingApprovals = async () => {
    // Phase 1E has no global approvals query. LiveProvider queries each
    // exact session scope; this legacy counter is Demo-only.
    if (clientMode !== 'mock') {
      setPendingApprovalCount(0);
      return;
    }
    try {
      const approvals = await client.listPendingApprovals();
      setPendingApprovalCount(approvals.length);
    } catch {
      setPendingApprovalCount(0);
    }
  };

  // Sync health and approval count
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);

    let isMounted = true;
    const checkConn = async () => {
      if (clientMode === 'mock') {
        if (isMounted) setConnectionStatus('mock_active');
      } else {
        try {
          // Protocol negotiation is the live connection check and validates
          // both the supported version and generated Schema digest.
          await Promise.all([
            phase1eClient.negotiateProtocol(true),
            phase23Client.negotiateProtocol(true),
            phase45Client.negotiateProtocol(true),
            phase56Client.negotiateProtocol(true),
            b23Client.negotiateProtocol(true),
            b24Client.negotiateProtocol(true),
          ]);
          if (isMounted) setConnectionStatus('connected');
        } catch {
          if (isMounted) setConnectionStatus('disconnected');
        }
      }
      void refreshPendingApprovals();
    };

    checkConn();
    const interval = setInterval(checkConn, 10000);
    return () => {
      isMounted = false;
      clearInterval(interval);
    };
  }, [b24Client, b23Client, client, clientMode, phase1eClient, phase23Client, phase45Client, phase56Client, theme]);

  return (
    <ClientContext.Provider
      value={{
        client,
        phase1eClient,
        phase23Client,
        phase45Client,
        phase56Client,
        b2Client,
        b23Client,
        b24Client,
        clientMode,
        setClientMode,
        connectionStatus,
        pendingApprovalCount,
        refreshPendingApprovals,
        activeWorkspace,
        setActiveWorkspace,
        theme,
        setTheme,
        toggleTheme,
        selectedThreadId,
        setSelectedThreadId,
        selectedWorkflowRunId,
        setSelectedWorkflowRunId,
        inspectorTab,
        setInspectorTab,
        inspectorOpen,
        setInspectorOpen,
        toggleInspector,
        sidebarOpen,
        setSidebarOpen,
        toggleSidebar,
        notifications,
        addNotification,
        removeNotification,
      }}
    >
      {children}
    </ClientContext.Provider>
  );
};

export const useOperant = (): ClientContextValue => {
  const ctx = useContext(ClientContext);
  if (!ctx) {
    throw new Error('useOperant must be used within a ClientProvider');
  }
  return ctx;
};
