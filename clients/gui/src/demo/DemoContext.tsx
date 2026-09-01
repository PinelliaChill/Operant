/**
 * 演示数据 React 绑定层。
 * 必须挂在 ClientProvider 之内：审批卡操作会回写 MockClient.submitApproval，
 * 并刷新 ClientContext 的待审批计数，保持审批中心徽标联动。
 * v4：会话/群聊统一走 messages 索引；工作流目录、运行实例与临时工作流由此派生。
 * v6：实例生命周期（归档/恢复/删除，运行中守卫）、新建实例（模板名 · 实例 N）、
 *     模板封锁查询选择器、审批历史与 Agent upsert。
 * v6 修复轮：工作流目录收敛为模板级视图（published 按来源草稿去重，每草稿保留最新版本），
 *     实例分组与封锁判定按模板键跨发布版本聚合；归档实例会话只读；取消运行解除运行中标记。
 */

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { useOperant } from '../context/ClientContext';
import { DEMO_WORKFLOW_NAMES, DEMO_WORKFLOW_PROJECTS } from './fixtures';
import {
  appendDemoMessage,
  appendRunExtra,
  appendUserMessage,
  buildTempWorkflowForSession,
  clearRunningRunStatus as clearRunningRunStatusState,
  collectPendingApprovalCards,
  createDirectConversation as createDirectConversationInStore,
  createInitialDemoState,
  createProjectInStore,
  createScheduleInStore,
  deleteScheduleInStore,
  triggerScheduleInStore,
  createSkillInStore,
  createExtensionInStore,
  createWorkflowInstance as createWorkflowInstanceInStore,
  demoId,
  deleteInstance as deleteInstanceState,
  archiveInstance as archiveInstanceState,
  restoreInstance as restoreInstanceState,
  renameConversation as renameConversationState,
  upsertAgent as upsertAgentState,
  DemoState,
  markTempWorkflowSaved,
  toggleConversationPin,
  toggleExtensionEnabled,
  toggleScheduleEnabled,
  toggleSkillEnabled,
  toggleProjectSkill as toggleProjectSkillState,
  toggleTaskDone,
  updateApprovalCardStatus,
} from './store';
import type {
  ApprovalActionType,
  ApprovalPolicy,
  ApprovalPolicyMode,
  ApprovalRuleDecision,
  DemoAgent,
  DemoAgentLogEntry,
  DemoApprovalHistoryItem,
  DemoApprovalPayload,
  DemoConversation,
  DemoExtension,
  DemoFileItem,
  DemoGroupViewFilter,
  DemoInstanceGroups,
  DemoKnowledgeItem,
  DemoMessage,
  DemoProject,
  DemoRunItem,
  DemoSchedule,
  DemoSkill,
  DemoTaskItem,
  DemoTempWorkflow,
  DemoWorkflowDirectoryItem,
} from './types';

/** MockClient 内置演示会话 id（与 sdk 演示数据一致） */
const MOCK_SESSION_ID = 'session_mock_alpha';

/** 运行中实例守卫提示（归档 / 删除共用口径） */
const RUNNING_GUARD_MESSAGE = '运行中的实例不能归档或删除，请先在运行进度中取消运行';

/** 运行中实例恢复守卫提示（恢复动作单独口径） */
const RESTORE_RUNNING_GUARD_MESSAGE = '运行中的实例不能恢复，请先在运行进度中取消运行';

/** 运行中实例重命名守卫提示 */
const RENAME_RUNNING_GUARD_MESSAGE = '运行中的实例不能重命名，请先在运行进度中取消运行';

/** 群聊寻址参数（'all'=全员，'user'=发给用户，string[]=指定 Agent） */
export type DemoGroupAudience = NonNullable<DemoMessage['audience']>;

/** 会话演示统计（状态栏使用） */
export interface DemoConversationStats {
  /** 演示用 token 计数（由消息量确定性推导） */
  tokens: number;
  /** 工具数（演示固定 12） */
  toolCount: number;
  /** 会话主助手（agentIds 首位）的模型 */
  model: string;
}

/** 待处理审批卡（审批中心列表 + 状态栏计数共用；message 已窄化为审批负载） */
export interface DemoPendingApproval {
  conversationId: string;
  conversationTitle: string;
  message: DemoMessage & { payload: DemoApprovalPayload };
}

/** 权限策略模式 → 中文名（审批中心模式卡 / StatusBar 中栏文案） */
export const APPROVAL_MODE_LABELS: Record<ApprovalPolicyMode, string> = {
  ask: '询问模式',
  'workspace-write': '工作区写入',
  'full-open': '完全开放',
  'auto-approve': '自动审批',
};

/** 审批动作分类 → 中文名（分类规则表行名） */
export const APPROVAL_ACTION_LABELS: Record<ApprovalActionType, string> = {
  file_write: '文件写入',
  command: '命令执行',
  network: '网络访问',
};

/** 分类规则取值 → 中文名（select 选项与切换反馈文案） */
export const APPROVAL_RULE_LABELS: Record<ApprovalRuleDecision, string> = {
  follow: '跟随模式',
  ask: '每次询问',
  auto: '自动允许',
  deny: '拒绝',
};

export interface DemoContextValue {
  conversations: DemoConversation[];
  projects: DemoProject[];
  agents: DemoAgent[];
  /** 按会话 id 索引的消息流（direct 单聊与工作流群聊统一存放） */
  messages: Record<string, DemoMessage[]>;
  /** 按会话 id 索引的文件列表 */
  files: Record<string, DemoFileItem[]>;
  tasks: DemoTaskItem[];
  /** 按会话 id 索引的知识条目 */
  knowledge: Record<string, DemoKnowledgeItem[]>;
  schedules: DemoSchedule[];
  extensions: DemoExtension[];
  skills: DemoSkill[];
  /** 各项目工作区装载的技能 ID 列表（projectId → skillId[]） */
  projectSkills: Record<string, string[]>;
  /** 临时工作流（运行时创建；脱钩另存仅改 status） */
  tempWorkflows: DemoTempWorkflow[];
  /** 演示运行实例（与 MockClient 真实 run 区分） */
  runsExtra: DemoRunItem[];
  /** 每个 direct 会话当前选中的助手（缺省 'agent_assistant'） */
  selectedAgentBySession: Record<string, string>;
  /** 群聊消息流过滤：全部 / 只看发给我的 */
  groupViewFilter: DemoGroupViewFilter;
  /** 权限策略（模式 + 分类规则；审批与权限中心读写） */
  approvalPolicy: ApprovalPolicy;
  /** 全部会话消息流中的待处理审批卡（按会话索引顺序） */
  pendingApprovals: DemoPendingApproval[];
  /** 切换权限策略模式；auto-approve/full-open 自动批准全部待处理卡，
      workspace-write 仅自动批准 file_write 卡（延迟 ~600ms 逐一执行并留痕/toast） */
  setApprovalMode: (mode: ApprovalPolicyMode) => void;
  /** 切换某动作分类的规则；切到"自动允许"时即时自动批准匹配的待处理卡 */
  setApprovalRule: (action: ApprovalActionType, rule: ApprovalRuleDecision) => void;
  getConversation: (id: string) => DemoConversation | undefined;
  getAgent: (id: string) => DemoAgent | undefined;
  getProject: (id: string) => DemoProject | undefined;
  getConversationStats: (id: string) => DemoConversationStats;
  togglePin: (conversationId: string) => void;
  approveCard: (conversationId: string, messageId: string) => void;
  rejectCard: (conversationId: string, messageId: string) => void;
  toggleTask: (taskId: string) => void;
  toggleSchedule: (scheduleId: string) => void;
  toggleExtension: (extensionId: string) => void;
  toggleSkill: (skillId: string) => void;
  /** 切换某项目工作区下指定技能的装载/启用状态 */
  toggleProjectSkill: (projectId: string, skillId: string) => void;
  /** 获取指定项目工作区已装载的技能 ID 列表 */
  getProjectSkills: (projectId: string) => string[];
  /** direct 会话发送：追加用户消息后自动追加选中助手的演示回复；toast 由调用方触发 */
  sendMessage: (conversationId: string, text: string) => void;
  /** 切换 direct 会话当前选中的助手 */
  selectSessionAgent: (sessionId: string, agentId: string) => void;
  /** 为 direct 会话搭建临时工作流（5 节点 / 4 连线演示值 + 计划卡 + toast） */
  buildTempWorkflow: (sessionId: string) => void;
  /** 临时工作流脱钩另存：status → saved_as（会话绑定不变）+ toast */
  persistTempWorkflow: (tempId: string) => void;
  /** 新增 running 演示实例 + toast */
  createWorkflowTask: (workflowId: string) => void;
  /** 群聊发言（authorId='user'，channel='group'）；随后同步追加首个接收者的演示回复（audience=['user']） */
  sendGroupMessage: (sessionId: string, text: string, audience: DemoGroupAudience) => void;
  /** 单独发给某 Agent（audience=[agentId]；"单独发给"标注由 UI 层渲染） */
  sendAgentDM: (sessionId: string, agentId: string, text: string) => void;
  /** 某群聊会话中某 Agent 的过程日志条目 */
  agentLogsOf: (sessionId: string, agentId: string) => DemoAgentLogEntry[];
  setGroupViewFilter: (filter: DemoGroupViewFilter) => void;
  /** 工作流目录（模板级视图）：发布版本按来源草稿去重、每草稿保留最新版本一条
      + 已另存的临时工作流（persisted） */
  getWorkflowDirectory: () => Promise<DemoWorkflowDirectoryItem[]>;
  /* ---- v6：实例生命周期 / 新建实例 / 查询选择器 / 审批历史 / Agent CRUD ---- */
  /** 归档实例（lifecycle → 'archived'）；目标运行中时提示并拒绝（返回 false） */
  archiveInstance: (conversationId: string) => boolean;
  /** 恢复实例为活跃（lifecycle → 'active'）；目标运行中时提示并拒绝（返回 false） */
  restoreInstance: (conversationId: string) => boolean;
  /** 彻底删除实例（连同其消息/过程日志/文件/任务/知识条目）；目标运行中时提示并拒绝（返回 false） */
  deleteInstance: (conversationId: string) => boolean;
  /** 重命名会话（单聊/群聊/实例通用）；目标实例运行中时提示并拒绝（返回 false） */
  renameConversation: (conversationId: string, title: string) => boolean;
  /** 按模板新建工作流实例会话（自动命名或自定义名称，挂载到指定项目工作区），返回新会话 id */
  createWorkflowInstance: (workflowDirectoryItemId: string, customTitle?: string, projectId?: string) => string;
  /** 模板下实例分组（lifecycle 缺省计为 active） */
  instancesOfWorkflow: (workflowId: string) => DemoInstanceGroups;
  /** 模板下活跃实例数 */
  activeInstanceCountOf: (workflowId: string) => number;
  /** 模板封锁判定：存在活跃实例时禁止发布新版本（草稿编辑不受限）；
      活跃实例按模板键跨发布版本聚合（旧版本上的活跃实例同样封锁） */
  canPublishTemplate: (workflowId: string) => { ok: boolean; activeCount: number };
  /** 取消运行成功后同步解除运行中标记：绑定该工作流且 runStatus='running' 的会话置 null
      （运行进度页取消运行时调用，避免守卫死路） */
  clearRunningRunStatus: (workflowId: string) => void;
  /** 实例运行守卫判定（归档/恢复/删除/重命名前可先检查） */
  instanceRunGuard: (conversationId: string) => { blocked: boolean; reason?: string };
  /** 已决定（approved/rejected）审批历史，按决定时间倒序 */
  approvalHistory: () => DemoApprovalHistoryItem[];
  /** 新建普通会话（默认归属工作区项目），返回新会话 id */
  createConversation: (title?: string, projectId?: string, agentId?: string) => string;
  /** 新建项目（演示层） */
  createProject: (project: { name: string; color: string; path?: string }) => DemoProject;
  /** 切换会话所属项目 */
  setConversationProject: (conversationId: string, projectId: string) => void;
  /** 清空会话上下文消息（/清空上下文） */
  clearConversationMessages: (conversationId: string) => void;
  /** 压缩会话上下文（/压缩上下文） */
  compactConversationContext: (conversationId: string) => void;
  /** 重置演示数据为初始状态 */
  resetDemoState: () => void;
  /** 新建或更新 Agent（按 id 匹配：存在则整体替换，不存在则追加） */
  upsertAgent: (agent: DemoAgent) => void;
  /** 新建调度任务 */
  createSchedule: (schedule: Omit<DemoSchedule, 'id'>) => DemoSchedule;
  /** 删除调度任务 */
  deleteSchedule: (id: string) => void;
  /** 立即触发运行调度任务 */
  triggerSchedule: (id: string) => void;
  /** 新建技能 */
  createSkill: (skill: Omit<DemoSkill, 'id'>) => DemoSkill;
  /** 扫描本地技能并导入 */
  scanLocalSkills: () => void;
  /** 新建扩展/MCP Server */
  createExtension: (ext: Omit<DemoExtension, 'id'>) => DemoExtension;
}

const DemoContext = createContext<DemoContextValue | null>(null);

export const DemoProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { client, addNotification, refreshPendingApprovals } = useOperant();
  const [state, setState] = useState<DemoState>(() => createInitialDemoState());
  const [selectedAgentBySession, setSelectedAgentBySession] = useState<Record<string, string>>({});
  const [groupViewFilter, setGroupViewFilter] = useState<DemoGroupViewFilter>('all');
  /** 权限策略（默认询问模式 + 分类规则全部跟随模式） */
  const [approvalPolicy, setApprovalPolicy] = useState<ApprovalPolicy>({
    mode: 'ask',
    rules: { file_write: 'follow', command: 'follow', network: 'follow' },
  });

  /** 最新 state 引用（供延时回调读取，避免自动批准定时器拿到陈旧闭包） */
  const stateRef = useRef(state);
  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  /** 策略自动批准的待触发定时器（卸载时清理） */
  const autoApproveTimers = useRef<number[]>([]);
  useEffect(
    () => () => {
      autoApproveTimers.current.forEach((t) => window.clearTimeout(t));
    },
    []
  );

  /** 工作流目录条目名缓存（getWorkflowDirectory 成功后更新；实例命名同步取模板名用） */
  const workflowNameCacheRef = useRef<Record<string, string>>({});

  /** 发布版本 id → 来源草稿 id 缓存（getWorkflowDirectory 成功后更新）：
      实例分组 / 封锁判定按模板键（草稿）跨发布版本聚合（v6 修复轮） */
  const revisionDraftRef = useRef<Record<string, string>>({});

  const getConversation = useCallback(
    (id: string) => state.conversations.find((c) => c.id === id),
    [state.conversations]
  );

  const getAgent = useCallback((id: string) => state.agents.find((a) => a.id === id), [state.agents]);

  const getProject = useCallback(
    (id: string) => state.projects.find((p) => p.id === id),
    [state.projects]
  );

  const getConversationStats = useCallback(
    (id: string): DemoConversationStats => {
      const conversation = state.conversations.find((c) => c.id === id);
      const messageCount = state.messages[id]?.length ?? 0;
      const leadAgent = conversation
        ? state.agents.find((a) => a.id === conversation.agentIds[0])
        : undefined;
      return {
        tokens: 5800 + messageCount * 3200 + id.length * 137,
        toolCount: 12,
        model: leadAgent?.model ?? '—',
      };
    },
    [state.conversations, state.messages, state.agents]
  );

  const togglePin = useCallback((conversationId: string) => {
    setState((prev) => toggleConversationPin(prev, conversationId));
  }, []);

  /** 审批卡操作：更新演示状态 → 回写 MockClient（保持审批计数联动）→ 通知。
      群聊审批卡不绑定 approvalId：仅本地状态 + 通知，不调用 submitApproval。
      opts.note 在处理后写卡留痕（如 "策略自动批准"）；opts.notify 覆盖默认通知文案。 */
  const decideCard = useCallback(
    (
      conversationId: string,
      messageId: string,
      decision: 'approved' | 'rejected',
      opts?: { note?: string; notify?: string }
    ) => {
      const message = stateRef.current.messages[conversationId]?.find((m) => m.id === messageId);
      const approvalId =
        message && message.payload.kind === 'approval' ? message.payload.approvalId : undefined;

      setState((prev) => updateApprovalCardStatus(prev, conversationId, messageId, decision, opts?.note));

      if (approvalId) {
        client
          .submitApproval(MOCK_SESSION_ID, approvalId, {
            approval_id: approvalId,
            decision: decision === 'approved' ? 'approve_once' : 'reject',
            decided_by: 'demo_user',
            decided_at: new Date().toISOString(),
          })
          .catch(() => {
            /* 演示数据与 mock 审批状态不一致时忽略，界面状态已更新 */
          })
          .finally(() => {
            void refreshPendingApprovals();
          });
      }

      addNotification(
        decision === 'approved' ? 'success' : 'warn',
        opts?.notify ?? (decision === 'approved' ? '已批准该动作（演示）' : '已拒绝该动作（演示）')
      );
    },
    [client, addNotification, refreshPendingApprovals]
  );

  const approveCard = useCallback(
    (conversationId: string, messageId: string) => decideCard(conversationId, messageId, 'approved'),
    [decideCard]
  );

  const rejectCard = useCallback(
    (conversationId: string, messageId: string) => decideCard(conversationId, messageId, 'rejected'),
    [decideCard]
  );

  /**
   * 策略自动批准（v5 §3.3 演示核心）：延迟 ~600ms 起逐一批准目标待处理卡，
   * 卡 note 置 "策略自动批准"，每张弹 toast "已按策略自动批准：{标题}"。
   * 定时器触发时复检当前状态，用户已手动处理的卡不重复批准。
   */
  const scheduleAutoApprovals = useCallback(
    (targets: Array<{ conversationId: string; messageId: string; title: string }>) => {
      targets.forEach((target, index) => {
        const timer = window.setTimeout(() => {
          const current = stateRef.current.messages[target.conversationId]?.find(
            (m) => m.id === target.messageId
          );
          if (!current || current.payload.kind !== 'approval' || current.payload.status !== 'pending') {
            return;
          }
          decideCard(target.conversationId, target.messageId, 'approved', {
            note: '策略自动批准',
            notify: `已按策略自动批准：${target.title}`,
          });
        }, 600 + index * 400);
        autoApproveTimers.current.push(timer);
      });
    },
    [decideCard]
  );

  const setApprovalMode = useCallback(
    (mode: ApprovalPolicyMode) => {
      setApprovalPolicy((prev) => ({ ...prev, mode }));
      const pending = collectPendingApprovalCards(stateRef.current);
      const targets =
        mode === 'auto-approve' || mode === 'full-open'
          ? pending
          : mode === 'workspace-write'
            ? pending.filter((c) => c.actionType === 'file_write')
            : [];
      if (targets.length > 0) scheduleAutoApprovals(targets);
    },
    [scheduleAutoApprovals]
  );

  const setApprovalRule = useCallback(
    (action: ApprovalActionType, rule: ApprovalRuleDecision) => {
      setApprovalPolicy((prev) => ({ ...prev, rules: { ...prev.rules, [action]: rule } }));
      addNotification(
        'info',
        `${APPROVAL_ACTION_LABELS[action]}规则已切换为：${APPROVAL_RULE_LABELS[rule]}`
      );
      if (rule === 'auto') {
        const targets = collectPendingApprovalCards(stateRef.current).filter(
          (c) => c.actionType === action
        );
        if (targets.length > 0) scheduleAutoApprovals(targets);
      }
    },
    [scheduleAutoApprovals, addNotification]
  );

  /** 待处理审批卡（含会话标题，供审批中心列表与状态栏计数） */
  const pendingApprovals = useMemo<DemoPendingApproval[]>(() => {
    const titles = new Map(state.conversations.map((c) => [c.id, c.title]));
    const result: DemoPendingApproval[] = [];
    for (const ref of collectPendingApprovalCards(state)) {
      const message = state.messages[ref.conversationId]?.find((m) => m.id === ref.messageId);
      if (message && message.payload.kind === 'approval') {
        result.push({
          conversationId: ref.conversationId,
          conversationTitle: titles.get(ref.conversationId) ?? ref.conversationId,
          message: message as DemoPendingApproval['message'],
        });
      }
    }
    return result;
  }, [state]);

  const toggleTask = useCallback((taskId: string) => {
    setState((prev) => toggleTaskDone(prev, taskId));
  }, []);

  const toggleSchedule = useCallback((scheduleId: string) => {
    setState((prev) => toggleScheduleEnabled(prev, scheduleId));
  }, []);

  const toggleExtension = useCallback((extensionId: string) => {
    setState((prev) => toggleExtensionEnabled(prev, extensionId));
  }, []);

  const toggleSkill = useCallback((skillId: string) => {
    setState((prev) => toggleSkillEnabled(prev, skillId));
  }, []);

  const toggleProjectSkill = useCallback((projectId: string, skillId: string) => {
    setState((prev) => toggleProjectSkillState(prev, projectId, skillId));
  }, []);

  const getProjectSkills = useCallback(
    (projectId: string): string[] => {
      return state.projectSkills[projectId] ?? [];
    },
    [state.projectSkills]
  );

  const sendMessage = useCallback(
    (conversationId: string, text: string) => {
      const trimmed = text.trim();
      if (!trimmed) return;
      setState((prev) => {
        const next = appendUserMessage(prev, conversationId, trimmed);
        const authorId = selectedAgentBySession[conversationId] ?? 'agent_assistant';
        return appendDemoMessage(next, {
          id: `${demoId('demo_msg')}_reply`,
          conversationId,
          authorId,
          createdAt: new Date().toISOString(),
          payload: { kind: 'text', text: '收到，我会继续处理（演示回复）。' },
        });
      });
    },
    [selectedAgentBySession]
  );

  const selectSessionAgent = useCallback((sessionId: string, agentId: string) => {
    setSelectedAgentBySession((prev) => ({ ...prev, [sessionId]: agentId }));
  }, []);

  const buildTempWorkflow = useCallback(
    (sessionId: string) => {
      setState((prev) =>
        buildTempWorkflowForSession(
          prev,
          sessionId,
          selectedAgentBySession[sessionId] ?? 'agent_assistant'
        )
      );
      addNotification('success', '已为本会话搭建临时工作流（演示）');
    },
    [selectedAgentBySession, addNotification]
  );

  const persistTempWorkflow = useCallback(
    (tempId: string) => {
      setState((prev) => markTempWorkflowSaved(prev, tempId));
      addNotification('success', '已另存为正式工作流（演示）');
    },
    [addNotification]
  );

  const createWorkflowTask = useCallback(
    (workflowId: string) => {
      setState((prev) =>
        appendRunExtra(prev, {
          id: demoId('demo_run'),
          workflowId,
          name: `演示运行实例 · ${new Date().toLocaleTimeString('zh-CN', {
            hour: '2-digit',
            minute: '2-digit',
          })}`,
          status: 'running',
          createdAt: new Date().toISOString(),
        })
      );
      addNotification('success', '已创建运行实例（演示）');
    },
    [addNotification]
  );

  /**
   * 群聊发言（authorId='user'，channel='group'）。
   * 追加用户消息后同步追加一条演示回复（channel='group'，authorId=audience 数组首个接收者，
   * audience=['user']）——回复进共享 messages，协作模式群聊、会话模式平铺视图与
   * 路由往返均可见（Reviewer B1 修复）。
   */
  const sendGroupMessage = useCallback(
    (sessionId: string, text: string, audience: DemoGroupAudience) => {
      const trimmed = text.trim();
      if (!trimmed) return;
      setState((prev) => {
        const next = appendDemoMessage(prev, {
          id: demoId('demo_msg'),
          conversationId: sessionId,
          authorId: 'user',
          createdAt: new Date().toISOString(),
          channel: 'group',
          audience,
          payload: { kind: 'text', text: trimmed },
        });
        const replyAuthor = Array.isArray(audience) ? audience[0] : 'agent_planner';
        return appendDemoMessage(next, {
          id: `${demoId('demo_msg')}_reply`,
          conversationId: sessionId,
          authorId: replyAuthor,
          createdAt: new Date().toISOString(),
          channel: 'group',
          audience: ['user'],
          payload: { kind: 'text', text: '收到，已纳入计划跟踪（演示回复）。' },
        });
      });
    },
    []
  );

  const sendAgentDM = useCallback(
    (sessionId: string, agentId: string, text: string) => {
      const trimmed = text.trim();
      if (!trimmed) return;
      setState((prev) =>
        appendDemoMessage(prev, {
          id: demoId('demo_msg'),
          conversationId: sessionId,
          authorId: 'user',
          createdAt: new Date().toISOString(),
          channel: 'group',
          audience: [agentId],
          payload: { kind: 'text', text: trimmed },
        })
      );
      const agentName = state.agents.find((a) => a.id === agentId)?.name ?? 'Agent';
      addNotification('success', `已单独发送给 ${agentName}（演示）`);
    },
    [state.agents, addNotification]
  );

  const agentLogsOf = useCallback(
    (sessionId: string, agentId: string) =>
      state.agentLogs.filter((l) => l.sessionId === sessionId && l.agentId === agentId),
    [state.agentLogs]
  );

  /** 工作流目录（v6 修复轮改为模板级视图）：published 条目按来源草稿（draftId）去重，
      每草稿仅保留最新版本一条（版本号取最大），消除同一模板的多条重复行；
      persisted（已另存临时工作流）逻辑不变。 */
  const getWorkflowDirectory = useCallback(async (): Promise<DemoWorkflowDirectoryItem[]> => {
    /** 模板级视图：draftId → 该草稿最新版本的目录条目 */
    const latestPublishedByDraft = new Map<string, DemoWorkflowDirectoryItem>();
    try {
      const [revisions, drafts] = await Promise.all([
        client.listGraphRevisions(),
        client.listGraphDrafts(),
      ]);
      revisionDraftRef.current = {
        ...revisionDraftRef.current,
        ...Object.fromEntries(revisions.map((rev) => [rev.id, rev.draft_id])),
      };
      for (const rev of revisions) {
        const templateProjectId =
          DEMO_WORKFLOW_PROJECTS[rev.draft_id ?? ''] ??
          DEMO_WORKFLOW_PROJECTS[rev.id] ??
          'proj_workspace';
        const item: DemoWorkflowDirectoryItem = {
          id: rev.id,
          // 目录展示名优先取来源草稿名（发布版本自带名多为"标准编码流水线 vN"式内部名）
          name: drafts.find((d) => d.id === rev.draft_id)?.name ?? rev.name,
          version: rev.version,
          nodeCount: rev.nodes.length,
          edgeCount: rev.edges.length,
          kind: 'published',
          draftId: rev.draft_id,
          projectId: templateProjectId,
        };
        const existing = latestPublishedByDraft.get(rev.draft_id);
        if (!existing || (item.version ?? 0) > (existing.version ?? 0)) {
          latestPublishedByDraft.set(rev.draft_id, item);
        }
      }
    } catch {
      /* live 后端不可用时仅返回已另存的临时工作流 */
    }
    const published = [...latestPublishedByDraft.values()];
    const persisted: DemoWorkflowDirectoryItem[] = state.tempWorkflows
      .filter((t) => t.status === 'saved_as')
      .map((t) => ({
        id: t.id,
        name: t.name,
        nodeCount: t.nodeCount,
        edgeCount: t.edgeCount,
        kind: 'persisted',
        projectId: t.projectId ?? 'proj_workspace',
      }));
    const list = [...published, ...persisted];
    // 缓存条目名：createWorkflowInstance 命名「{模板名} · 实例 N」时同步取用
    workflowNameCacheRef.current = {
      ...workflowNameCacheRef.current,
      ...Object.fromEntries(list.map((i) => [i.id, i.name])),
    };
    return list;
  }, [client, state.tempWorkflows]);

  /** 解析工作流目录条目的模板名（同步）：已另存临时工作流名 → 目录缓存 → 演示种子表 → id 兜底 */
  const resolveWorkflowTemplateName = useCallback(
    (workflowId: string): string =>
      state.tempWorkflows.find((t) => t.id === workflowId)?.name ??
      workflowNameCacheRef.current[workflowId] ??
      DEMO_WORKFLOW_NAMES[workflowId] ??
      workflowId,
    [state.tempWorkflows]
  );

  /** 新建工作流实例会话（v6）：复用群聊会话骨架与空消息容器，
      自动命名或自定义命名，绑定发布版本且挂载到指定项目工作区 */
  const createWorkflowInstance = useCallback(
    (workflowDirectoryItemId: string, customTitle?: string, projectId?: string): string => {
      const templateProjectId =
        projectId ??
        DEMO_WORKFLOW_PROJECTS[workflowDirectoryItemId] ??
        'proj_workspace';
      const result = createWorkflowInstanceInStore(
        state,
        workflowDirectoryItemId,
        resolveWorkflowTemplateName(workflowDirectoryItemId),
        templateProjectId,
        customTitle
      );
      setState(result.state);
      addNotification('success', `已创建实例「${result.conversation.title}」（演示）`);
      return result.conversation.id;
    },
    [state, resolveWorkflowTemplateName, addNotification]
  );

  /** 归档实例（运行中守卫：提示并拒绝） */
  const archiveInstance = useCallback(
    (conversationId: string): boolean => {
      const target = stateRef.current.conversations.find((c) => c.id === conversationId);
      if (!target) return false;
      if (target.runStatus === 'running') {
        addNotification('warn', RUNNING_GUARD_MESSAGE);
        return false;
      }
      setState((prev) => archiveInstanceState(prev, conversationId));
      addNotification('success', '已归档实例（演示）');
      return true;
    },
    [addNotification]
  );

  /** 恢复实例为活跃（运行中守卫：提示并拒绝） */
  const restoreInstance = useCallback(
    (conversationId: string): boolean => {
      const target = stateRef.current.conversations.find((c) => c.id === conversationId);
      if (!target) return false;
      if (target.runStatus === 'running') {
        addNotification('warn', RESTORE_RUNNING_GUARD_MESSAGE);
        return false;
      }
      setState((prev) => restoreInstanceState(prev, conversationId));
      addNotification('success', '已恢复为活跃实例（演示）');
      return true;
    },
    [addNotification]
  );

  /** 彻底删除实例及其消息/日志/文件/任务/知识（运行中守卫：提示并拒绝） */
  const deleteInstance = useCallback(
    (conversationId: string): boolean => {
      const target = stateRef.current.conversations.find((c) => c.id === conversationId);
      if (!target) return false;
      if (target.runStatus === 'running') {
        addNotification('warn', RUNNING_GUARD_MESSAGE);
        return false;
      }
      setState((prev) => deleteInstanceState(prev, conversationId));
      addNotification('success', '已删除实例及其消息记录（演示）');
      return true;
    },
    [addNotification]
  );

  /** 重命名会话（单聊/群聊/实例通用；空标题视为无操作，运行中实例被守卫拦截） */
  const renameConversation = useCallback(
    (conversationId: string, title: string): boolean => {
      if (!title.trim()) return false;
      const target = stateRef.current.conversations.find((c) => c.id === conversationId);
      if (!target) return false;
      if (target.runStatus === 'running') {
        addNotification('warn', RENAME_RUNNING_GUARD_MESSAGE);
        return false;
      }
      setState((prev) => renameConversationState(prev, conversationId, title));
      return true;
    },
    [addNotification]
  );

  /** 解析目录条目 id 所属模板键：已知发布版本映射到来源草稿 id（同模板跨版本聚合），
      未知 id（另存临时工作流 / 目录未加载）回退 id 本身 */
  const templateKeyOf = useCallback(
    (workflowId: string): string => revisionDraftRef.current[workflowId] ?? workflowId,
    []
  );

  /** 模板下实例分组（lifecycle 缺省计为 active；保持 conversations 数组顺序）。
      v6 修复轮：按模板键聚合——同一草稿历史发布版本上的实例一并归组，
      目录收敛为最新版本后旧版本实例不丢失，封锁口径随之跨版本求和 */
  const instancesOfWorkflow = useCallback(
    (workflowId: string): DemoInstanceGroups => {
      const templateKey = templateKeyOf(workflowId);
      const active: DemoConversation[] = [];
      const archived: DemoConversation[] = [];
      for (const c of state.conversations) {
        if (!c.workflowId || templateKeyOf(c.workflowId) !== templateKey) continue;
        (c.lifecycle === 'archived' ? archived : active).push(c);
      }
      return { active, archived };
    },
    [state.conversations, templateKeyOf]
  );

  /** 模板下活跃实例数 */
  const activeInstanceCountOf = useCallback(
    (workflowId: string): number => instancesOfWorkflow(workflowId).active.length,
    [instancesOfWorkflow]
  );

  /** 模板封锁判定：存在活跃实例时禁止发布新版本（草稿编辑不受限）；
      活跃实例经 instancesOfWorkflow 按模板键跨发布版本聚合 */
  const canPublishTemplate = useCallback(
    (workflowId: string): { ok: boolean; activeCount: number } => {
      const activeCount = instancesOfWorkflow(workflowId).active.length;
      return { ok: activeCount === 0, activeCount };
    },
    [instancesOfWorkflow]
  );

  /** 取消运行成功后解除运行中标记（RunDetailView 取消运行调用）：绑定该工作流
      且 runStatus='running' 的会话置 runStatus=null，避免守卫提示"先取消运行"的死路 */
  const clearRunningRunStatus = useCallback((workflowId: string): void => {
    setState((prev) => clearRunningRunStatusState(prev, workflowId));
  }, []);

  /** 实例运行守卫判定（上层归档/恢复/删除/重命名前可先检查） */
  const instanceRunGuard = useCallback(
    (conversationId: string): { blocked: boolean; reason?: string } => {
      const conversation = state.conversations.find((c) => c.id === conversationId);
      return conversation?.runStatus === 'running'
        ? { blocked: true, reason: RUNNING_GUARD_MESSAGE }
        : { blocked: false };
    },
    [state.conversations]
  );

  /** 已决定审批历史（approved/rejected），按决定时间倒序；
      decidedAt 取卡上盖章或种子值，缺省回退到消息创建时间 */
  const approvalHistory = useCallback((): DemoApprovalHistoryItem[] => {
    const titles = new Map(state.conversations.map((c) => [c.id, c.title]));
    const agentNames = new Map(state.agents.map((a) => [a.id, a.name]));
    const items: DemoApprovalHistoryItem[] = [];
    for (const [conversationId, list] of Object.entries(state.messages)) {
      for (const m of list) {
        if (m.payload.kind !== 'approval' || m.payload.status === 'pending') continue;
        items.push({
          conversationId,
          conversationTitle: titles.get(conversationId) ?? conversationId,
          agentId: m.authorId,
          agentName: agentNames.get(m.authorId) ?? m.authorId,
          title: m.payload.title,
          detail: m.payload.detail,
          decision: m.payload.status,
          decidedAt: m.payload.decidedAt ?? m.createdAt,
          note: m.payload.note,
        });
      }
    }
    return items.sort((a, b) => b.decidedAt.localeCompare(a.decidedAt));
  }, [state]);

  /** 新建普通会话（默认归属工作区项目） */
  const createConversation = useCallback(
    (title = '新会话', projectId = 'proj_workspace', agentId = 'agent_assistant'): string => {
      const result = createDirectConversationInStore(state, title, projectId, agentId);
      setState(result.state);
      addNotification('success', `已创建会话「${result.conversation.title}」（演示）`);
      return result.conversation.id;
    },
    [state, addNotification]
  );

  /** 新建项目（演示层） */
  const createProject = useCallback(
    (project: { name: string; color: string; path?: string }): DemoProject => {
      const result = createProjectInStore(state, project);
      setState(result.state);
      addNotification('success', `已创建项目「${result.project.name}」（演示）`);
      return result.project;
    },
    [state, addNotification]
  );

  /** 切换会话所属项目 */
  const setConversationProject = useCallback(
    (conversationId: string, projectId: string) => {
      setState((prev) => ({
        ...prev,
        conversations: prev.conversations.map((c) =>
          c.id === conversationId ? { ...c, projectId } : c
        ),
      }));
      const projectName = state.projects.find((p) => p.id === projectId)?.name ?? '项目';
      addNotification('success', `已将会话移至「${projectName}」（演示）`);
    },
    [state.projects, addNotification]
  );

  /** 清空会话上下文消息（/清空上下文） */
  const clearConversationMessages = useCallback(
    (conversationId: string) => {
      setState((prev) => ({
        ...prev,
        messages: {
          ...prev.messages,
          [conversationId]: [],
        },
      }));
      addNotification('success', '已清空会话上下文（演示）');
    },
    [addNotification]
  );

  /** 压缩会话上下文（/压缩上下文） */
  const compactConversationContext = useCallback(
    (conversationId: string) => {
      const conv = state.conversations.find((c) => c.id === conversationId);
      const leadAgentId = conv?.agentIds[0] ?? 'agent_assistant';
      const now = new Date().toISOString();
      const compactMsg: DemoMessage = {
        id: demoId('demo_msg_compact'),
        conversationId,
        authorId: leadAgentId,
        createdAt: now,
        payload: {
          kind: 'text',
          text: '【上下文压缩完成】已将前序多轮历史归纳为阶段摘要，保留关键决策事实与当前变量定义，Token 占用已优化 65%（演示）。',
        },
      };
      setState((prev) => appendDemoMessage(prev, compactMsg));
      addNotification('success', '已生成上下文压缩摘要（演示）');
    },
    [state.conversations, addNotification]
  );

  /** 重置演示数据为初始状态 */
  const resetDemoState = useCallback(() => {
    setState(createInitialDemoState());
    addNotification('success', '已重置演示数据为初始状态（演示）');
  }, [addNotification]);

  /** 新建或更新 Agent（按 id 匹配；「编辑 Agent / 新建 Agent」入口使用） */
  const upsertAgent = useCallback(
    (agent: DemoAgent) => {
      setState((prev) => upsertAgentState(prev, agent));
      addNotification('success', `已保存 Agent「${agent.name}」（演示）`);
    },
    [addNotification]
  );

  /** 新建调度任务 */
  const createSchedule = useCallback(
    (scheduleData: Omit<DemoSchedule, 'id'>) => {
      let created: DemoSchedule;
      setState((prev) => {
        const res = createScheduleInStore(prev, scheduleData);
        created = res.schedule;
        return res.state;
      });
      addNotification('success', `已创建调度「${scheduleData.name}」（演示）`);
      return created!;
    },
    [addNotification]
  );

  /** 删除调度任务 */
  const deleteSchedule = useCallback(
    (id: string) => {
      setState((prev) => deleteScheduleInStore(prev, id));
      addNotification('warn', '已删除调度任务（演示）');
    },
    [addNotification]
  );

  /** 立即触发运行调度任务 */
  const triggerSchedule = useCallback(
    (id: string) => {
      setState((prev) => triggerScheduleInStore(prev, id));
      addNotification('success', '已触发调度任务执行（演示）');
    },
    [addNotification]
  );

  /** 新建技能包 */
  const createSkill = useCallback(
    (skillData: Omit<DemoSkill, 'id'>) => {
      let created: DemoSkill;
      setState((prev) => {
        const res = createSkillInStore(prev, skillData);
        created = res.skill;
        return res.state;
      });
      addNotification('success', `已创建技能「${skillData.name}」（演示）`);
      return created!;
    },
    [addNotification]
  );

  /** 扫描本地标准技能目录并导入 */
  const scanLocalSkills = useCallback(() => {
    addNotification('info', '正在扫描本地 ~/.gemini/antigravity/builtin/skills 与 .operant/skills/ …');
    setTimeout(() => {
      addNotification('success', '本地技能扫描完成：已同步 6 个标准 Agent 技能规范。');
    }, 400);
  }, [addNotification]);

  /** 新建扩展/MCP Server */
  const createExtension = useCallback(
    (extData: Omit<DemoExtension, 'id'>) => {
      let created: DemoExtension;
      setState((prev) => {
        const res = createExtensionInStore(prev, extData);
        created = res.extension;
        return res.state;
      });
      addNotification('success', `已添加扩展「${extData.name}」（演示）`);
      return created!;
    },
    [addNotification]
  );

  const value = useMemo<DemoContextValue>(
    () => ({
      conversations: state.conversations,
      projects: state.projects,
      agents: state.agents,
      messages: state.messages,
      files: state.files,
      tasks: state.tasks,
      knowledge: state.knowledge,
      schedules: state.schedules,
      extensions: state.extensions,
      skills: state.skills,
      projectSkills: state.projectSkills,
      tempWorkflows: state.tempWorkflows,
      runsExtra: state.runsExtra,
      selectedAgentBySession,
      groupViewFilter,
      approvalPolicy,
      pendingApprovals,
      setApprovalMode,
      setApprovalRule,
      getConversation,
      getAgent,
      getProject,
      getConversationStats,
      togglePin,
      approveCard,
      rejectCard,
      toggleTask,
      toggleSchedule,
      toggleExtension,
      toggleSkill,
      toggleProjectSkill,
      getProjectSkills,
      sendMessage,
      selectSessionAgent,
      buildTempWorkflow,
      persistTempWorkflow,
      createWorkflowTask,
      sendGroupMessage,
      sendAgentDM,
      agentLogsOf,
      setGroupViewFilter,
      getWorkflowDirectory,
      archiveInstance,
      restoreInstance,
      deleteInstance,
      renameConversation,
      createWorkflowInstance,
      instancesOfWorkflow,
      activeInstanceCountOf,
      canPublishTemplate,
      clearRunningRunStatus,
      instanceRunGuard,
      approvalHistory,
      createConversation,
      createProject,
      setConversationProject,
      clearConversationMessages,
      compactConversationContext,
      resetDemoState,
      upsertAgent,
      createSchedule,
      deleteSchedule,
      triggerSchedule,
      createSkill,
      scanLocalSkills,
      createExtension,
    }),
    [
      state,
      selectedAgentBySession,
      groupViewFilter,
      approvalPolicy,
      pendingApprovals,
      setApprovalMode,
      setApprovalRule,
      getConversation,
      getAgent,
      getProject,
      getConversationStats,
      togglePin,
      approveCard,
      rejectCard,
      toggleTask,
      toggleSchedule,
      toggleExtension,
      toggleSkill,
      toggleProjectSkill,
      getProjectSkills,
      sendMessage,
      selectSessionAgent,
      buildTempWorkflow,
      persistTempWorkflow,
      createWorkflowTask,
      sendGroupMessage,
      sendAgentDM,
      agentLogsOf,
      getWorkflowDirectory,
      archiveInstance,
      restoreInstance,
      deleteInstance,
      renameConversation,
      createWorkflowInstance,
      instancesOfWorkflow,
      activeInstanceCountOf,
      canPublishTemplate,
      clearRunningRunStatus,
      instanceRunGuard,
      approvalHistory,
      createConversation,
      createProject,
      setConversationProject,
      clearConversationMessages,
      compactConversationContext,
      resetDemoState,
      upsertAgent,
      createSchedule,
      deleteSchedule,
      triggerSchedule,
      createSkill,
      scanLocalSkills,
      createExtension,
    ]
  );

  return <DemoContext.Provider value={value}>{children}</DemoContext.Provider>;
};

export const useDemo = (): DemoContextValue => {
  const ctx = useContext(DemoContext);
  if (!ctx) {
    throw new Error('useDemo must be used within a DemoProvider');
  }
  return ctx;
};
