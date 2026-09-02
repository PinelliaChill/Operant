/**
 * 演示数据 Store：状态模型与纯不可变更新函数。
 * React 绑定与副作用（审批回写、通知）见 DemoContext.tsx。
 * v6：实例生命周期（归档/恢复/删除）、会话重命名、Agent upsert、
 *     工作流实例创建（「模板名 · 实例 N」序号计数）与审批决定时间盖章。
 * v6 修复轮：演示 id 统一时间戳 + 短随机后缀（防同毫秒撞 id）；
 *     新增取消运行后解除实例运行中标记的内部 action。
 */

import {
  DEMO_AGENT_LOGS,
  DEMO_AGENTS,
  DEMO_CONVERSATIONS,
  DEMO_EXTENSIONS,
  DEMO_FILES,
  DEMO_KNOWLEDGE,
  DEMO_MESSAGES,
  DEMO_PROJECTS,
  DEMO_RUNS_EXTRA,
  DEMO_SCHEDULES,
  DEMO_SKILLS,
  DEMO_PROJECT_SKILLS,
  DEMO_TASKS,
  DEMO_TEMP_WORKFLOWS,
} from './fixtures';
import type {
  ApprovalActionType,
  DemoAgent,
  DemoAgentLogEntry,
  DemoConversation,
  DemoExtension,
  DemoFileItem,
  DemoKnowledgeItem,
  DemoMessage,
  DemoProject,
  DemoRunItem,
  DemoSchedule,
  DemoSkill,
  DemoTaskItem,
  DemoTempWorkflow,
} from './types';

/** 演示 id 生成：时间戳 + 短随机后缀，避免同一毫秒内创建多条记录时撞 id（v6 修复轮） */
export function demoId(prefix: string): string {
  return `${prefix}_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
}
/** 演示数据整体状态（messages/files/knowledge 按会话 id 索引） */
export interface DemoState {
  conversations: DemoConversation[];
  projects: DemoProject[];
  agents: DemoAgent[];
  messages: Record<string, DemoMessage[]>;
  /** Agent 过程日志（群聊会话 × Agent，个人界面条目流） */
  agentLogs: DemoAgentLogEntry[];
  /** 临时工作流（运行时创建；脱钩另存仅改 status） */
  tempWorkflows: DemoTempWorkflow[];
  /** 演示运行实例（与 MockClient 真实 run 区分） */
  runsExtra: DemoRunItem[];
  files: Record<string, DemoFileItem[]>;
  tasks: DemoTaskItem[];
  knowledge: Record<string, DemoKnowledgeItem[]>;
  schedules: DemoSchedule[];
  extensions: DemoExtension[];
  skills: DemoSkill[];
  /** 各项目工作区装载的技能 ID 列表（projectId → skillId[]） */
  projectSkills: Record<string, string[]>;
  /** 工作流实例序号计数器（workflowId → 已用最大序号 N；删除实例不回收序号，v6） */
  instanceCounters: Record<string, number>;
}

/** 深拷贝 fixtures 作为初始状态，避免演示操作污染模块级常量 */
export function createInitialDemoState(): DemoState {
  return {
    conversations: structuredClone(DEMO_CONVERSATIONS),
    projects: structuredClone(DEMO_PROJECTS),
    agents: structuredClone(DEMO_AGENTS),
    messages: structuredClone(DEMO_MESSAGES),
    agentLogs: structuredClone(DEMO_AGENT_LOGS),
    tempWorkflows: structuredClone(DEMO_TEMP_WORKFLOWS),
    runsExtra: structuredClone(DEMO_RUNS_EXTRA),
    files: structuredClone(DEMO_FILES),
    tasks: structuredClone(DEMO_TASKS),
    knowledge: structuredClone(DEMO_KNOWLEDGE),
    schedules: structuredClone(DEMO_SCHEDULES),
    extensions: structuredClone(DEMO_EXTENSIONS),
    skills: structuredClone(DEMO_SKILLS),
    projectSkills: structuredClone(DEMO_PROJECT_SKILLS),
    instanceCounters: {},
  };
}

/** 切换会话置顶状态 */
export function toggleConversationPin(state: DemoState, conversationId: string): DemoState {
  return {
    ...state,
    conversations: state.conversations.map((c) =>
      c.id === conversationId ? { ...c, pinned: !c.pinned } : c
    ),
  };
}

/** 更新审批卡消息状态（approved / rejected）；会话与群聊消息统一走 messages 索引。
    note 用于策略自动批准等场景在卡上留痕（如 "策略自动批准"）；
    决定时同步盖章 decidedAt（审批历史派生用，v6）。 */
export function updateApprovalCardStatus(
  state: DemoState,
  conversationId: string,
  messageId: string,
  status: 'approved' | 'rejected',
  note?: string
): DemoState {
  const list = state.messages[conversationId];
  if (!list) return state;
  return {
    ...state,
    messages: {
      ...state.messages,
      [conversationId]: list.map((m) =>
        m.id === messageId && m.payload.kind === 'approval'
          ? {
              ...m,
              payload: {
                ...m.payload,
                status,
                decidedAt: new Date().toISOString(),
                ...(note !== undefined ? { note } : {}),
              },
            }
          : m
      ),
    },
  };
}

/** 待处理审批卡定位（策略自动批准 / 状态栏计数共用） */
export interface PendingApprovalRef {
  conversationId: string;
  messageId: string;
  title: string;
  actionType?: ApprovalActionType;
}

/** 收集全部会话消息流中的待处理审批卡（按会话索引顺序） */
export function collectPendingApprovalCards(state: DemoState): PendingApprovalRef[] {
  const result: PendingApprovalRef[] = [];
  for (const [conversationId, list] of Object.entries(state.messages)) {
    for (const m of list) {
      if (m.payload.kind === 'approval' && m.payload.status === 'pending') {
        result.push({
          conversationId,
          messageId: m.id,
          title: m.payload.title,
          actionType: m.payload.actionType,
        });
      }
    }
  }
  return result;
}

/** 切换任务完成状态 */
export function toggleTaskDone(state: DemoState, taskId: string): DemoState {
  return {
    ...state,
    tasks: state.tasks.map((t) => (t.id === taskId ? { ...t, done: !t.done } : t)),
  };
}

/** 切换调度启用状态 */
export function toggleScheduleEnabled(state: DemoState, scheduleId: string): DemoState {
  return {
    ...state,
    schedules: state.schedules.map((s) =>
      s.id === scheduleId ? { ...s, enabled: !s.enabled } : s
    ),
  };
}

/** 切换扩展启用状态 */
export function toggleExtensionEnabled(state: DemoState, extensionId: string): DemoState {
  return {
    ...state,
    extensions: state.extensions.map((e) =>
      e.id === extensionId ? { ...e, enabled: !e.enabled } : e
    ),
  };
}

/** 切换技能启用状态 */
export function toggleSkillEnabled(state: DemoState, skillId: string): DemoState {
  return {
    ...state,
    skills: state.skills.map((s) => (s.id === skillId ? { ...s, enabled: !s.enabled } : s)),
  };
}

/** 切换某项目工作区下指定技能的装载/启用状态（软链接映射） */
export function toggleProjectSkill(
  state: DemoState,
  projectId: string,
  skillId: string
): DemoState {
  const current = state.projectSkills[projectId] ?? [];
  const exists = current.includes(skillId);
  const next = exists ? current.filter((id) => id !== skillId) : [...current, skillId];
  return {
    ...state,
    projectSkills: {
      ...state.projectSkills,
      [projectId]: next,
    },
  };
}

/** 追加一条用户消息，并把会话置顶到"最近更新" */
export function appendUserMessage(
  state: DemoState,
  conversationId: string,
  text: string
): DemoState {
  const now = new Date().toISOString();
  const message: DemoMessage = {
    id: demoId('demo_msg'),
    conversationId,
    authorId: 'user',
    createdAt: now,
    payload: { kind: 'text', text },
  };
  return appendDemoMessage(state, message);
}

/** 追加任意演示消息（群聊寻址 / Agent 演示回复等），并把会话置顶到"最近更新" */
export function appendDemoMessage(state: DemoState, message: DemoMessage): DemoState {
  return {
    ...state,
    messages: {
      ...state.messages,
      [message.conversationId]: [...(state.messages[message.conversationId] ?? []), message],
    },
    conversations: state.conversations.map((c) =>
      c.id === message.conversationId ? { ...c, updatedAt: message.createdAt, unread: 0 } : c
    ),
  };
}

/** 为 direct 会话创建临时工作流（演示值 5 节点 / 4 连线），并追加当前选中助手的计划卡消息 */
export function buildTempWorkflowForSession(
  state: DemoState,
  sessionId: string,
  authorId: string
): DemoState {
  const now = new Date().toISOString();
  const conversation = state.conversations.find((c) => c.id === sessionId);
  const tempWorkflow: DemoTempWorkflow = {
    id: demoId('temp_wf'),
    sessionId,
    name: `临时工作流 · ${conversation?.title ?? sessionId}`,
    nodeCount: 5,
    edgeCount: 4,
    status: 'temp',
    createdAt: now,
  };
  const planMessage: DemoMessage = {
    id: `${demoId('demo_msg')}_plan`,
    conversationId: sessionId,
    authorId,
    createdAt: now,
    payload: {
      kind: 'plan',
      title: '临时工作流计划',
      summary: '已按本会话目标搭建 5 节点 / 4 连线的临时编排，可在侧栏查看节点并另存为正式工作流。',
      items: [
        { text: '规划节点：拆解会话目标', done: true },
        { text: '探索节点：收集上下文（并行度 2）', done: true },
        { text: '编码节点：独占写入实现', done: false },
        { text: '评审节点：只读裁决', done: false },
      ],
    },
  };
  const withTemp: DemoState = {
    ...state,
    tempWorkflows: [...state.tempWorkflows, tempWorkflow],
    conversations: state.conversations.map((c) =>
      c.id === sessionId ? { ...c, tempWorkflowId: tempWorkflow.id } : c
    ),
  };
  return appendDemoMessage(withTemp, planMessage);
}

/** 临时工作流脱钩另存：仅标注 saved_as，会话绑定（tempWorkflowId）不变 */
export function markTempWorkflowSaved(state: DemoState, tempId: string): DemoState {
  return {
    ...state,
    tempWorkflows: state.tempWorkflows.map((t) =>
      t.id === tempId ? { ...t, status: 'saved_as' as const } : t
    ),
  };
}

/** 新建工作流群聊会话（参与者固定为规划师 / 编码员 / 评审员），返回新会话供跳转。
    同时初始化 messages 容器，保证群聊界面/会话统计按 id 索引时拿到空流而非 undefined */
export function createWorkflowGroupSession(
  state: DemoState,
  workflowId: string,
  name: string,
  projectId = 'proj_workspace'
): { state: DemoState; conversation: DemoConversation } {
  const now = new Date().toISOString();
  const conversation: DemoConversation = {
    id: demoId('conv_wf'),
    title: name,
    pinned: false,
    updatedAt: now,
    projectId,
    workflowId,
    agentIds: ['agent_planner', 'agent_coder', 'agent_reviewer'],
    unread: 0,
  };
  return {
    state: {
      ...state,
      conversations: [conversation, ...state.conversations],
      messages: { ...state.messages, [conversation.id]: [] },
    },
    conversation,
  };
}

/** 追加一条演示运行实例（runsExtra） */
export function appendRunExtra(state: DemoState, item: DemoRunItem): DemoState {
  return { ...state, runsExtra: [...state.runsExtra, item] };
}

/** 取消运行后解除实例运行中标记（v6 修复轮）：绑定该工作流且 runStatus='running'
    的会话置 runStatus=null——运行进度页取消运行后守卫不再误拦归档/恢复/删除 */
export function clearRunningRunStatus(state: DemoState, workflowId: string): DemoState {
  return {
    ...state,
    conversations: state.conversations.map((c) =>
      c.workflowId === workflowId && c.runStatus === 'running' ? { ...c, runStatus: null } : c
    ),
  };
}

/* ========== v6：实例生命周期 / 会话重命名 / Agent CRUD / 工作流实例创建 ========== */

/** 去掉记录中的某个键（其余键不动） */
function omitKey<T>(record: Record<string, T>, key: string): Record<string, T> {
  const next: Record<string, T> = {};
  for (const [k, v] of Object.entries(record)) {
    if (k !== key) next[k] = v;
  }
  return next;
}

/** 归档实例（lifecycle → 'archived'）；运行中拦截由上层守卫负责，store 层不做断言 */
export function archiveInstance(state: DemoState, conversationId: string): DemoState {
  return {
    ...state,
    conversations: state.conversations.map((c) =>
      c.id === conversationId ? { ...c, lifecycle: 'archived' as const } : c
    ),
  };
}

/** 恢复实例（lifecycle → 'active'） */
export function restoreInstance(state: DemoState, conversationId: string): DemoState {
  return {
    ...state,
    conversations: state.conversations.map((c) =>
      c.id === conversationId ? { ...c, lifecycle: 'active' as const } : c
    ),
  };
}

/** 彻底删除会话：从 conversations 移除，并清理其消息、过程日志、文件、任务与知识条目 */
export function deleteInstance(state: DemoState, conversationId: string): DemoState {
  return {
    ...state,
    conversations: state.conversations.filter((c) => c.id !== conversationId),
    messages: omitKey(state.messages, conversationId),
    agentLogs: state.agentLogs.filter((l) => l.sessionId !== conversationId),
    files: omitKey(state.files, conversationId),
    knowledge: omitKey(state.knowledge, conversationId),
    tasks: state.tasks.filter((t) => t.conversationId !== conversationId),
  };
}

/** 重命名会话（单聊 / 群聊 / 实例通用）；空标题视为无操作 */
export function renameConversation(
  state: DemoState,
  conversationId: string,
  title: string
): DemoState {
  const trimmed = title.trim();
  if (!trimmed) return state;
  return {
    ...state,
    conversations: state.conversations.map((c) =>
      c.id === conversationId ? { ...c, title: trimmed } : c
    ),
  };
}

/** 新建或更新 Agent（按 id 匹配：存在则整体替换，不存在则追加） */
export function upsertAgent(state: DemoState, agent: DemoAgent): DemoState {
  const exists = state.agents.some((a) => a.id === agent.id);
  return {
    ...state,
    agents: exists
      ? state.agents.map((a) => (a.id === agent.id ? agent : a))
      : [...state.agents, agent],
  };
}

/** 从会话标题解析「{模板名} · 实例 N」中的序号 N，非该命名格式的会话不参与计数 */
function parseInstanceSeq(title: string, prefix: string): number {
  if (!title.startsWith(prefix)) return 0;
  const n = Number.parseInt(title.slice(prefix.length), 10);
  return Number.isFinite(n) ? n : 0;
}

/** 新建普通会话（默认归属默认工作区项目） */
export function createDirectConversation(
  state: DemoState,
  title = '新会话',
  projectId = 'proj_workspace',
  agentId = 'agent_assistant'
): { state: DemoState; conversation: DemoConversation } {
  const now = new Date().toISOString();
  const conversation: DemoConversation = {
    id: demoId('conv'),
    title,
    pinned: false,
    updatedAt: now,
    projectId,
    agentIds: [agentId],
    unread: 0,
    lifecycle: 'active',
  };
  return {
    state: {
      ...state,
      conversations: [conversation, ...state.conversations],
      messages: { ...state.messages, [conversation.id]: [] },
    },
    conversation,
  };
}

/** 新建项目（演示层，默认 path 若未提供则生成 /Users/operant/workspace/{name}） */
export function createProjectInStore(
  state: DemoState,
  project: { name: string; color: string; path?: string }
): { state: DemoState; project: DemoProject } {
  const trimmed = project.name.trim();
  const newProject: DemoProject = {
    id: demoId('proj'),
    name: trimmed,
    color: project.color || '#2563eb',
    path:
      project.path?.trim() ||
      `/Users/operant/workspace/${trimmed.toLowerCase().replace(/\s+/g, '-')}`,
  };
  return {
    state: {
      ...state,
      projects: [...state.projects, newProject],
    },
    project: newProject,
  };
}

/** 新建工作流实例会话：复用 createWorkflowGroupSession 的会话骨架与消息容器，
    自动命名「{模板名} · 实例 N」——N 取「标题解析出的历史最大序号」与
    「序号计数器」二者的最大值 +1（跨活跃+已归档；删除不回收序号）；
    绑定模板发布版本（workflowId=目录条目 id）且 lifecycle='active'。 */
export function createWorkflowInstance(
  state: DemoState,
  workflowId: string,
  templateName: string,
  projectId = 'proj_workspace',
  customTitle?: string
): { state: DemoState; conversation: DemoConversation } {
  const prefix = `${templateName} · 实例 `;
  let maxSeq = state.instanceCounters[workflowId] ?? 0;
  for (const c of state.conversations) {
    if (c.workflowId !== workflowId) continue;
    const seq = parseInstanceSeq(c.title, prefix);
    if (seq > maxSeq) maxSeq = seq;
  }
  const seq = maxSeq + 1;
  const title = customTitle?.trim() || `${templateName} · 实例 ${seq}`;
  const created = createWorkflowGroupSession(
    state,
    workflowId,
    title,
    projectId
  );
  const conversation: DemoConversation = { ...created.conversation, lifecycle: 'active' };
  return {
    state: {
      ...created.state,
      instanceCounters: { ...created.state.instanceCounters, [workflowId]: seq },
      conversations: created.state.conversations.map((c) =>
        c.id === conversation.id ? conversation : c
      ),
    },
    conversation,
  };
}

/** 新建调度任务 */
export function createScheduleInStore(
  state: DemoState,
  schedule: Omit<DemoSchedule, 'id'>
): { state: DemoState; schedule: DemoSchedule } {
  const newSchedule: DemoSchedule = {
    ...schedule,
    id: demoId('sch'),
  };
  return {
    state: {
      ...state,
      schedules: [newSchedule, ...state.schedules],
    },
    schedule: newSchedule,
  };
}

/** 删除调度任务 */
export function deleteScheduleInStore(state: DemoState, id: string): DemoState {
  return {
    ...state,
    schedules: state.schedules.filter((s) => s.id !== id),
  };
}

/** 立即运行调度任务（模拟状态置为 running 与 success） */
export function triggerScheduleInStore(state: DemoState, id: string): DemoState {
  const now = new Date().toISOString();
  return {
    ...state,
    schedules: state.schedules.map((s) =>
      s.id === id
        ? {
            ...s,
            lastRunStatus: 'success',
            lastRunAt: now,
          }
        : s
    ),
  };
}

/** 新建技能包 */
export function createSkillInStore(
  state: DemoState,
  skill: Omit<DemoSkill, 'id'>
): { state: DemoState; skill: DemoSkill } {
  const newSkill: DemoSkill = {
    ...skill,
    id: demoId('skill'),
  };
  return {
    state: {
      ...state,
      skills: [newSkill, ...state.skills],
    },
    skill: newSkill,
  };
}

/** 新建扩展/MCP Server */
export function createExtensionInStore(
  state: DemoState,
  ext: Omit<DemoExtension, 'id'>
): { state: DemoState; extension: DemoExtension } {
  const newExt: DemoExtension = {
    ...ext,
    id: demoId('ext'),
    status: 'connected',
  };
  return {
    state: {
      ...state,
      extensions: [newExt, ...state.extensions],
    },
    extension: newExt,
  };
}
