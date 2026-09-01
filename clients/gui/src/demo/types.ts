/**
 * 演示数据层类型定义（v6 §实例生命周期与模板封锁）
 * 仅用于演示模式界面填充，不修改 sdk/，不与真实 Core 契约耦合。
 * v6：会话增加实例生命周期（lifecycle/runStatus）；Agent 承载角色预设字段；审批卡补决定时间。
 */

/** 演示会话 */
export interface DemoConversation {
  id: string;
  title: string;
  pinned: boolean;
  /** ISO 8601 时间字符串 */
  updatedAt: string;
  projectId?: string;
  /** 会话 Agent id 列表（direct 单聊=[主助手]，工作流群聊=参与者列表） */
  agentIds: string[];
  /** 设置即"工作流群聊会话"（指向工作流目录条目 id）；不设为 direct 单聊 */
  workflowId?: string;
  /** direct 会话可绑定的临时工作流 id */
  tempWorkflowId?: string;
  /** 未读消息数（侧栏徽标） */
  unread?: number;
  /** 实例生命周期：活跃（缺省）/ 已归档；删除=从会话列表彻底移除，不走此字段 */
  lifecycle?: 'active' | 'archived';
  /** 运行中守卫标记：仅工作流实例使用，'running' 时归档/删除/重命名被上层拦截 */
  runStatus?: 'running' | null;
}

/** 演示项目（color 为数据级标识色，path 绑定工作区目录路径，isDefault 标记默认工作区） */
export interface DemoProject {
  id: string;
  name: string;
  color: string;
  path: string;
  isDefault?: boolean;
}

/** Agent 工具权限策略（演示级；字段口径对齐真实 Role 的 tool_policy） */
export interface DemoAgentToolPolicy {
  /** 允许调用的工具名列表 */
  allowedTools: string[];
  /** 是否允许工作区写入 */
  workspaceWrite: boolean;
  /** 是否允许命令执行 */
  commandExecution: boolean;
  /** 必须走审批的动作类别（如 destructive / git_write / privileged） */
  approvalRequired: string[];
}

/** 演示 Agent */
export interface DemoAgent {
  id: string;
  name: string;
  color: string;
  /** 当前使用的模型 ID（来自 Provider Discovery 风格的演示值） */
  model: string;
  /** 角色职责简述 */
  roleDesc: string;
  capabilities: string[];
  status: 'online' | 'busy' | 'offline';
  /** 角色系统提示词（承载原设置「角色预设」字段；v6） */
  systemPrompt?: string;
  /** 工具权限策略（编辑 Agent / 新建 Agent 使用；v6） */
  toolPolicy?: DemoAgentToolPolicy;
  /** 单次运行回合上限（v6） */
  maxTurns?: number;
}

/** 权限策略模式（v5 §3.3 审批与权限中心） */
export type ApprovalPolicyMode = 'ask' | 'workspace-write' | 'full-open' | 'auto-approve';

/** 审批动作分类（分类规则表的三个维度） */
export type ApprovalActionType = 'file_write' | 'command' | 'network';

/** 分类规则取值：跟随模式 / 每次询问 / 自动允许 / 拒绝 */
export type ApprovalRuleDecision = 'follow' | 'ask' | 'auto' | 'deny';

/** 权限策略（模式 + 分类规则；选中态持久在 DemoContext） */
export interface ApprovalPolicy {
  mode: ApprovalPolicyMode;
  rules: Record<ApprovalActionType, ApprovalRuleDecision>;
}

/** 演示消息负载（联合类型：文本 / 计划卡 / 进度卡 / 审批卡 / 文件卡） */
export type DemoMessagePayload =
  | { kind: 'text'; text: string }
  | {
      kind: 'plan';
      title: string;
      summary: string;
      items: Array<{ text: string; done: boolean }>;
    }
  | { kind: 'progress'; title: string; percent: number; note: string }
  | {
      kind: 'approval';
      title: string;
      detail: string;
      /** 关联 MockClient 审批卡 id（如 appr_001），用于审批计数联动 */
      approvalId?: string;
      /** 动作分类（分类规则自动批准匹配用；缺省不参与规则匹配） */
      actionType?: ApprovalActionType;
      status: 'pending' | 'approved' | 'rejected';
      /** 处理后备注（如 "策略自动批准"）；含"自动批准"时渲染"自动"徽章 */
      note?: string;
      /** 决定时间（ISO 8601）：approved/rejected 时由 store 盖章或种子补齐；审批历史派生用（v6） */
      decidedAt?: string;
    }
  | { kind: 'file'; name: string; path: string };

/** 审批卡负载（Extract 窄化，供待处理列表消费） */
export type DemoApprovalPayload = Extract<DemoMessagePayload, { kind: 'approval' }>;

/** 审批历史条目（approvalHistory 派生：已决定审批卡 + 来源会话与 Agent 信息；v6） */
export interface DemoApprovalHistoryItem {
  /** 来源会话（实例）id 与标题 */
  conversationId: string;
  conversationTitle: string;
  /** 审批卡发送者 Agent id 与名称 */
  agentId: string;
  agentName: string;
  title: string;
  detail: string;
  /** 决定结果 */
  decision: 'approved' | 'rejected';
  /** 决定时间（ISO 8601；种子数据补齐，运行期决定由 store 盖章） */
  decidedAt: string;
  /** 处理备注（如 "策略自动批准"） */
  note?: string;
}

/** 工作流实例分组查询结果（lifecycle 缺省视为 active；v6） */
export interface DemoInstanceGroups {
  active: DemoConversation[];
  archived: DemoConversation[];
}

/** 演示消息 */
export interface DemoMessage {
  id: string;
  conversationId: string;
  /** 发送者：Agent id 或 'user' */
  authorId: string;
  /** ISO 8601 时间字符串 */
  createdAt: string;
  /** 消息通道：缺省 direct；工作流群聊内为 group */
  channel?: 'direct' | 'group';
  /** 群聊寻址：'all'=全员，'user'=发给用户，string[]=指定 Agent；user 消息缺省 ['agent_planner'] */
  audience?: 'all' | 'user' | string[];
  payload: DemoMessagePayload;
}

/** 演示 Agent 过程日志条目（群聊会话 × Agent 的 thought / tool_call / intermediate 流） */
export interface DemoAgentLogEntry {
  id: string;
  sessionId: string;
  agentId: string;
  kind: 'thought' | 'tool_call' | 'intermediate';
  title: string;
  /** 可折叠详情 */
  detail?: string;
  /** ISO 8601 时间字符串 */
  createdAt: string;
}

/** 演示临时工作流（会话模式渐进承诺路径；脱钩另存仅改 status，会话绑定不变） */
export interface DemoTempWorkflow {
  id: string;
  sessionId: string;
  name: string;
  nodeCount: number;
  edgeCount: number;
  status: 'temp' | 'saved_as';
  /** ISO 8601 时间字符串 */
  createdAt: string;
  /** 归属项目 ID */
  projectId?: string;
}

/** 演示运行实例（任务=运行实例；runsExtra 种子与 MockClient 的 run_operant_001 区分） */
export interface DemoRunItem {
  id: string;
  workflowId: string;
  name: string;
  status: 'running' | 'success' | 'failed';
  /** ISO 8601 时间字符串 */
  createdAt: string;
}

/** 工作流目录项：published=MockClient 发布版本；persisted=已另存的临时工作流 */
export interface DemoWorkflowDirectoryItem {
  id: string;
  name: string;
  /** 仅 published 有版本号 */
  version?: number;
  nodeCount: number;
  edgeCount: number;
  kind: 'published' | 'persisted';
  /** published 条目来源草稿 id */
  draftId?: string;
  /** 归属项目 ID */
  projectId?: string;
}

/** 群聊消息流过滤：全部 / 只看发给我的 */
export type DemoGroupViewFilter = 'all' | 'to_me';

/** 演示文件条目 */
export interface DemoFileItem {
  id: string;
  conversationId: string;
  name: string;
  path: string;
  /** 预格式化大小，如 "12.4 KB" */
  size: string;
  /** ISO 8601 时间字符串 */
  modifiedAt: string;
}

/** 演示任务条目 */
export interface DemoTaskItem {
  id: string;
  title: string;
  done: boolean;
  conversationId: string;
  /** ISO 8601 日期字符串（可选截止时间） */
  due?: string;
}

/** 演示知识库条目 */
export interface DemoKnowledgeItem {
  id: string;
  conversationId: string;
  title: string;
  summary: string;
  /** 来源标签，如 "会话摘要" / "工作区文件" */
  source: string;
}

/** 演示调度任务（v8：支持 Cron 周期与 Timer 倒计时，多目标类型） */
export interface DemoSchedule {
  id: string;
  name: string;
  type: 'cron' | 'timer';
  cron?: string;
  timerSeconds?: number;
  targetType: 'workflow' | 'prompt' | 'health_check';
  targetId?: string;
  targetPayload?: string;
  /** ISO 8601 时间字符串 */
  nextRun: string;
  enabled: boolean;
  lastRunStatus?: 'success' | 'failed' | 'running';
  lastRunAt?: string;
}

/** 演示扩展（v8：支持 MCP Server 与插件管理） */
export interface DemoExtension {
  id: string;
  name: string;
  desc: string;
  type: 'mcp' | 'plugin';
  protocol?: 'stdio' | 'sse';
  command?: string;
  endpoint?: string;
  toolsCount?: number;
  version: string;
  enabled: boolean;
  status?: 'connected' | 'disconnected' | 'error';
}

/** 演示技能（v8：遵循 standard Agent Skill 规范） */
export interface DemoSkill {
  id: string;
  name: string;
  desc: string;
  spec: 'standard-v1';
  path: string;
  author?: string;
  category?: string;
  tools?: string[];
  scripts?: string[];
  markdownContent?: string;
  enabled: boolean;
}
