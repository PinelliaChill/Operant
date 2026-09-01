import React, { createContext, useContext, useEffect, useMemo, useState } from 'react';
import { HttpClient, MockClient, OperantClient } from '@operant/sdk';
import { formatTime } from '../lib/format';

export type ClientMode = 'mock' | 'live';
export type ConnectionStatus = 'connected' | 'reconnecting' | 'disconnected' | 'mock_active';
export type InspectorTab = 'context' | 'diff' | 'terminal' | 'audit';

export interface AppNotification {
  id: string;
  type: 'info' | 'success' | 'warn' | 'error';
  message: string;
  timestamp: string;
}

interface ClientContextValue {
  client: OperantClient;
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

  const [activeWorkspace, setActiveWorkspace] = useState<string>(
    '/Users/bigo/agentworkspace/codexworkspace/operant'
  );

  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>('mock_active');
  const [pendingApprovalCount, setPendingApprovalCount] = useState<number>(0);
  const [selectedThreadId, setSelectedThreadId] = useState<string | null>('thread_main_alpha');
  const [selectedWorkflowRunId, setSelectedWorkflowRunId] = useState<string | null>('run_operant_001');
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>('context');
  const [inspectorOpen, setInspectorOpen] = useState<boolean>(false);
  const [sidebarOpen, setSidebarOpen] = useState<boolean>(false);
  const [notifications, setNotifications] = useState<AppNotification[]>([]);

  // Instantiate client singletons
  const mockClient = useMemo(() => new MockClient(), []);
  const httpClient = useMemo(() => new HttpClient('http://127.0.0.1:8000'), []);

  const client: OperantClient = clientMode === 'mock' ? mockClient : httpClient;

  const setClientMode = (mode: ClientMode) => {
    localStorage.setItem('operant_client_mode', mode);
    setClientModeState(mode);
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
          await client.checkHealth();
          if (isMounted) setConnectionStatus('connected');
        } catch {
          if (isMounted) setConnectionStatus('disconnected');
        }
      }
      refreshPendingApprovals();
    };

    checkConn();
    const interval = setInterval(checkConn, 10000);
    return () => {
      isMounted = false;
      clearInterval(interval);
    };
  }, [client, clientMode, theme]);

  return (
    <ClientContext.Provider
      value={{
        client,
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
