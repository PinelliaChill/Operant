/**
 * 演示数据 Fixtures（开发者风中文内容）
 * 时间均相对当前时刻生成，保证侧栏"今天/昨天/周一/上周"标签稳定自洽。
 * 项目 / Agent 的 color 为数据级标识色（蓝 / 橙 / 绿 / 规划师 violet #7c3aed），不属于主题色相。
 * v6：Agent 补齐角色预设字段（systemPrompt/toolPolicy/maxTurns）；
 *     工作流实例补生命周期种子（1 个运行中活跃实例 + 1 个已归档实例）与已决定审批卡。
 * v6 修复轮：已归档实例的已批准审批卡 decidedAt 调整为晚于消息 createdAt。
 */

import type {
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

const MIN = 60_000;
const HOUR = 3_600_000;
const DAY = 86_400_000;

/** 以当前时刻为基准向前偏移，输出 ISO 字符串 */
function ago(offsetMs: number): string {
  return new Date(Date.now() - offsetMs).toISOString();
}

/** 以当前时刻为基准向后偏移，输出 ISO 字符串 */
function fromNow(offsetMs: number): string {
  return new Date(Date.now() + offsetMs).toISOString();
}

/* ========== 项目 ========== */

export const DEMO_PROJECTS: DemoProject[] = [
  {
    id: 'proj_workspace',
    name: '工作区',
    color: '#059669',
    path: '/Users/operant/workspace',
    isDefault: true,
  },
  {
    id: 'proj_kernel',
    name: 'Operant 内核',
    color: '#2563eb',
    path: '/Users/operant/workspace/operant-core',
  },
  {
    id: 'proj_clients',
    name: '客户端工作台',
    color: '#ea580c',
    path: '/Users/operant/workspace/operant-clients',
  },
  {
    id: 'proj_eval',
    name: '评估实验',
    color: '#7c3aed',
    path: '/Users/operant/workspace/operant-eval',
  },
];

/* ========== Agent ========== */

/* ========== Agent（systemPrompt/toolPolicy/maxTurns 口径参考 sdk mock-client 的 role 种子） ========== */

export const DEMO_AGENTS: DemoAgent[] = [
  {
    id: 'agent_assistant',
    name: '通用助手',
    color: '#2563eb',
    model: 'gpt-4o-2024-11-20',
    roleDesc: '任务拆解与方案讨论',
    capabilities: ['任务拆解', '代码解释', '文档检索'],
    status: 'online',
    systemPrompt: '你是一个智能、安全的编码助手，负责拆解复杂编程任务。',
    toolPolicy: {
      allowedTools: ['read_file', 'search_files', 'apply_patch', 'run_command', 'git_diff'],
      workspaceWrite: true,
      commandExecution: true,
      approvalRequired: ['git_write', 'destructive', 'privileged'],
    },
    maxTurns: 15,
  },
  {
    id: 'agent_planner',
    name: '规划师',
    color: '#7c3aed',
    model: 'o3-2025-04-16',
    roleDesc: '任务拆解与计划编排',
    capabilities: ['规划', '任务分发'],
    status: 'online',
    systemPrompt: '分析仓库架构、评估需求并产出结构化实施计划。',
    toolPolicy: {
      allowedTools: ['read_file', 'search_files'],
      workspaceWrite: false,
      commandExecution: false,
      approvalRequired: [],
    },
    maxTurns: 8,
  },
  {
    id: 'agent_coder',
    name: '精准编码员',
    color: '#ea580c',
    model: 'claude-3-7-sonnet-20250219',
    roleDesc: '独占写入的编码执行者',
    capabilities: ['代码生成', 'apply_patch', 'run_command'],
    status: 'busy',
    systemPrompt: '遵循项目既有约定，实现最小化、正确、类型安全的修改。',
    toolPolicy: {
      allowedTools: ['read_file', 'search_files', 'apply_patch', 'run_command', 'git_diff'],
      workspaceWrite: true,
      commandExecution: true,
      approvalRequired: ['git_write', 'privileged'],
    },
    maxTurns: 20,
  },
  {
    id: 'agent_reviewer',
    name: '评审员',
    color: '#059669',
    model: 'deepseek-r1-2025',
    roleDesc: '只读评审与返工裁决',
    capabilities: ['代码评审', '测试分析'],
    status: 'online',
    systemPrompt: '检查差异与执行测试反馈，确保安全策略与类型被严格遵守。',
    toolPolicy: {
      allowedTools: ['read_file', 'search_files', 'git_diff', 'run_command'],
      workspaceWrite: false,
      commandExecution: true,
      approvalRequired: ['destructive'],
    },
    maxTurns: 6,
  },
];

/* ========== 工作流模板名（发布版本 id → 模板名；单一来源：实例命名、目录兜底与
    chatUtils.getWorkflowDisplayName 展示共用口径） ========== */

export const DEMO_WORKFLOW_NAMES: Record<string, string> = {
  rev_graph_v1: '自主功能交付图',
  rev_coding_pipeline: '标准编码流水线',
  rev_multi_review: '多 Agent 审查流水线',
};

/** 工作流模板归属项目 ID 映射 */
export const DEMO_WORKFLOW_PROJECTS: Record<string, string> = {
  rev_graph_v1: 'proj_workspace',
  rev_coding_pipeline: 'proj_kernel',
  rev_multi_review: 'proj_workspace',
  draft_coding: 'proj_kernel',
  draft_review: 'proj_workspace',
};

/* ========== 会话 ========== */

export const DEMO_CONVERSATIONS: DemoConversation[] = [
  {
    id: 'conv_recovery',
    title: '修复 workflow 恢复缺陷',
    pinned: true,
    updatedAt: ago(26 * MIN),
    projectId: 'proj_kernel',
    agentIds: ['agent_assistant'],
    unread: 2,
  },
  {
    id: 'conv_pr4',
    title: 'PR #4 评审：审批持久化',
    pinned: true,
    updatedAt: ago(3 * HOUR),
    projectId: 'proj_kernel',
    agentIds: ['agent_assistant'],
  },
  {
    id: 'conv_eval',
    title: 'Evaluation Runner 实验 19—24 设计',
    pinned: false,
    updatedAt: ago(DAY + 2 * HOUR),
    projectId: 'proj_eval',
    agentIds: ['agent_assistant'],
    unread: 1,
  },
  {
    id: 'conv_graph',
    title: 'Graph 编译器诊断文案',
    pinned: false,
    updatedAt: ago(3 * DAY),
    projectId: 'proj_kernel',
    agentIds: ['agent_assistant'],
  },
  {
    id: 'conv_remote',
    title: 'Remote Gateway 配对流程',
    pinned: false,
    updatedAt: ago(6 * DAY),
    projectId: 'proj_clients',
    agentIds: ['agent_assistant'],
    unread: 3,
  },
  {
    id: 'conv_wf_delivery',
    title: '自主功能交付 · 执行群聊',
    pinned: false,
    updatedAt: ago(12 * MIN),
    projectId: 'proj_workspace',
    workflowId: 'rev_graph_v1',
    agentIds: ['agent_planner', 'agent_coder', 'agent_reviewer'],
    unread: 1,
    runStatus: 'running',
  },
  {
    /* v6 已归档实例种子：绑定同一发布版本，命名遵循「模板名 · 实例 N」规则 */
    id: 'conv_wf_inst3',
    title: '自主功能交付图 · 实例 3',
    pinned: false,
    updatedAt: ago(2 * DAY),
    projectId: 'proj_workspace',
    workflowId: 'rev_graph_v1',
    agentIds: ['agent_planner', 'agent_coder', 'agent_reviewer'],
    unread: 0,
    lifecycle: 'archived',
  },
  {
    id: 'conv_rail',
    title: 'GUI 图标导航栏重构',
    pinned: false,
    updatedAt: ago(9 * DAY),
    projectId: 'proj_clients',
    agentIds: ['agent_assistant'],
  },
];

/* ========== 消息流 ========== */

export const DEMO_MESSAGES: Record<string, DemoMessage[]> = {
  conv_recovery: [
    {
      id: 'msg_rec_01',
      conversationId: 'conv_recovery',
      authorId: 'user',
      createdAt: ago(2 * HOUR),
      payload: {
        kind: 'text',
        text: 'workflow 在 Coder 阶段写入结果未知时没有进入 manual_reconcile_required，直接重放导致补丁被应用了两次。请定位原因并给出修复方案。',
      },
    },
    {
      id: 'msg_rec_02',
      conversationId: 'conv_recovery',
      authorId: 'agent_assistant',
      createdAt: ago(2 * HOUR - 4 * MIN),
      payload: {
        kind: 'plan',
        title: '修复计划：恢复检查点幂等',
        summary: '在阶段检查点恢复路径上补充写入结果确认，未知结果一律转人工核对。',
        items: [
          { text: '在 checkpoint 恢复处校验 Coder 写入回执', done: true },
          { text: '新增 manual_reconcile_required 状态分支', done: true },
          { text: '补充跨进程恢复回归测试', done: false },
        ],
      },
    },
    {
      id: 'msg_rec_03',
      conversationId: 'conv_recovery',
      authorId: 'agent_assistant',
      createdAt: ago(2 * HOUR - 8 * MIN),
      payload: {
        kind: 'progress',
        title: '回归测试执行中',
        percent: 68,
        note: '已跑完 34 / 50 个恢复场景用例，暂无非预期失败。',
      },
    },
    {
      id: 'msg_rec_04',
      conversationId: 'conv_recovery',
      authorId: 'agent_assistant',
      createdAt: ago(2 * HOUR - 12 * MIN),
      payload: {
        kind: 'file',
        name: 'recovery.py',
        path: 'src/operant/workflow/recovery.py',
      },
    },
    {
      id: 'msg_rec_05',
      conversationId: 'conv_recovery',
      authorId: 'agent_assistant',
      createdAt: ago(2 * HOUR - 15 * MIN),
      payload: {
        kind: 'approval',
        title: '申请写入工作区文件',
        detail: '把补丁写入 src/operant/workflow/recovery.py，并新增 tests/workflow/test_recovery_reconcile.py。',
        approvalId: 'appr_001',
        actionType: 'file_write',
        status: 'pending',
      },
    },
    {
      id: 'msg_rec_06',
      conversationId: 'conv_recovery',
      authorId: 'agent_assistant',
      createdAt: ago(40 * MIN),
      payload: {
        kind: 'text',
        text: '评审通过前的两点提醒：重放路径必须核对阶段检查点的写入回执；测试反馈超过 12,000 字符时先做结构化截断。',
      },
    },
    {
      id: 'msg_rec_07',
      conversationId: 'conv_recovery',
      authorId: 'user',
      createdAt: ago(26 * MIN),
      payload: {
        kind: 'text',
        text: '收到，审批我稍后处理，先把恢复矩阵文档补齐。',
      },
    },
  ],
  conv_pr4: [
    {
      id: 'msg_pr4_01',
      conversationId: 'conv_pr4',
      authorId: 'user',
      createdAt: ago(4 * HOUR),
      payload: {
        kind: 'text',
        text: 'PR #4 把审批决定持久化到 SQLite，请评审 lease 过期与续期的边界条件。',
      },
    },
    {
      id: 'msg_pr4_02',
      conversationId: 'conv_pr4',
      authorId: 'agent_assistant',
      createdAt: ago(3 * HOUR - 30 * MIN),
      payload: {
        kind: 'text',
        text: '已通读 diff。lease 过期路径有审计记录，但续期缺少并发保护，建议在 approve_for_run 上补一条集成测试。',
      },
    },
    {
      id: 'msg_pr4_03',
      conversationId: 'conv_pr4',
      authorId: 'agent_assistant',
      createdAt: ago(3 * HOUR),
      payload: {
        kind: 'progress',
        title: '评审进度',
        percent: 82,
        note: '剩余项：续期并发保护与拒绝原因文案核对。',
      },
    },
  ],
  conv_eval: [
    {
      id: 'msg_eval_01',
      conversationId: 'conv_eval',
      authorId: 'user',
      createdAt: ago(DAY + 3 * HOUR),
      payload: {
        kind: 'text',
        text: '实验 19—24 想对比三种 Explorer 并行度对返工率的影响，帮我整理实验矩阵。',
      },
    },
    {
      id: 'msg_eval_02',
      conversationId: 'conv_eval',
      authorId: 'agent_assistant',
      createdAt: ago(DAY + 2 * HOUR),
      payload: {
        kind: 'plan',
        title: '实验矩阵草案',
        summary: '并行度取 1 / 2 / 4，固定 max_rework_rounds=1，观察返工率与平均耗时。',
        items: [
          { text: '实验 19—20：并行度 1 基线', done: true },
          { text: '实验 21—22：并行度 2', done: false },
          { text: '实验 23—24：并行度 4 与失败注入', done: false },
        ],
      },
    },
  ],
  conv_graph: [
    {
      id: 'msg_graph_01',
      conversationId: 'conv_graph',
      authorId: 'user',
      createdAt: ago(3 * DAY + HOUR),
      payload: {
        kind: 'text',
        text: 'Graph 编译器的诊断信息太技术化，帮我润色成用户能看懂的中文，保留 error code。',
      },
    },
    {
      id: 'msg_graph_02',
      conversationId: 'conv_graph',
      authorId: 'agent_assistant',
      createdAt: ago(3 * DAY + 30 * MIN),
      payload: {
        kind: 'text',
        text: '建议三段式：发生了什么、为什么、怎么修。诊断码保留原值放进折叠详情，例如 GRAPH_CYCLE_DETECTED。',
      },
    },
    {
      id: 'msg_graph_03',
      conversationId: 'conv_graph',
      authorId: 'agent_assistant',
      createdAt: ago(3 * DAY),
      payload: {
        kind: 'file',
        name: 'diagnostics.ts',
        path: 'src/operant/graph/diagnostics.ts',
      },
    },
  ],
  conv_remote: [
    {
      id: 'msg_remote_01',
      conversationId: 'conv_remote',
      authorId: 'user',
      createdAt: ago(6 * DAY + 2 * HOUR),
      payload: {
        kind: 'text',
        text: '手机端配对流程想控制在 60 秒内完成，超时与撤销的提示文案一起设计一下。',
      },
    },
    {
      id: 'msg_remote_02',
      conversationId: 'conv_remote',
      authorId: 'agent_assistant',
      createdAt: ago(6 * DAY),
      payload: {
        kind: 'text',
        text: '配对码 6 位、有效期 120 秒；撤销后立即失效并广播 device.revoked。提示文案我按直连 / Relay 两种链路各写一版。',
      },
    },
  ],
  conv_rail: [
    {
      id: 'msg_rail_01',
      conversationId: 'conv_rail',
      authorId: 'user',
      createdAt: ago(9 * DAY + 5 * HOUR),
      payload: {
        kind: 'text',
        text: '把双模式壳层重构成图标导航栏 + 情境侧栏 + 状态栏，设计基线锁定后开工。',
      },
    },
    {
      id: 'msg_rail_02',
      conversationId: 'conv_rail',
      authorId: 'agent_assistant',
      createdAt: ago(9 * DAY),
      payload: {
        kind: 'progress',
        title: '壳层重构进度',
        percent: 35,
        note: 'IconRail 与状态栏已就位，会话主区等待 Phase B。',
      },
    },
  ],
  conv_wf_delivery: [
    {
      /* v6 已决定审批卡种子：上一轮返工中被拒绝的发布申请，供审批历史派生 */
      id: 'msg_wf_00',
      conversationId: 'conv_wf_delivery',
      authorId: 'agent_reviewer',
      createdAt: ago(5 * HOUR),
      channel: 'group',
      audience: 'all',
      payload: {
        kind: 'approval',
        title: '批准发布 v0.2.9 候选版本',
        detail: '上一轮返工后的候选发布申请：评审发现恢复矩阵未覆盖跨进程场景，本轮发布已拒绝并转回返工。',
        actionType: 'file_write',
        status: 'rejected',
        decidedAt: ago(5 * HOUR - 10 * MIN),
        note: '评审驳回后转入返工',
      },
    },
    {
      id: 'msg_wf_01',
      conversationId: 'conv_wf_delivery',
      authorId: 'agent_planner',
      createdAt: ago(92 * MIN),
      channel: 'group',
      audience: 'all',
      payload: {
        kind: 'plan',
        title: '运行计划：自主功能交付图',
        summary: '拆解为四个阶段，Explorer 并行度 2，Reviewer 只读裁决，max_rework_rounds=1。',
        items: [
          { text: 'Explorer 收集上下文（并行度 2）', done: true },
          { text: 'Coder 独占写入实现与自测', done: true },
          { text: 'Reviewer 评审并输出 verdict', done: false },
          { text: '发布 v0.3.0 候选版本并回归验证', done: false },
        ],
      },
    },
    {
      id: 'msg_wf_02',
      conversationId: 'conv_wf_delivery',
      authorId: 'agent_coder',
      createdAt: ago(88 * MIN),
      channel: 'group',
      audience: ['agent_planner'],
      payload: {
        kind: 'text',
        text: '自主功能交付图 v0.3.0 候选版本的运行已启动。我负责 Coder 阶段独占写入，先同步上下文收集结果与补丁路径。',
      },
    },
    {
      id: 'msg_wf_03',
      conversationId: 'conv_wf_delivery',
      authorId: 'agent_reviewer',
      createdAt: ago(84 * MIN),
      channel: 'group',
      audience: 'all',
      payload: {
        kind: 'text',
        text: '收到。评审侧提醒两点：阶段检查点必须先核对写入回执再重放；失败测试反馈按 12,000 字符上限做结构化截断。',
      },
    },
    {
      id: 'msg_wf_04',
      conversationId: 'conv_wf_delivery',
      authorId: 'user',
      createdAt: ago(70 * MIN),
      channel: 'group',
      payload: {
        kind: 'text',
        text: '按当前节奏推进即可，发布候选版本之前叫我确认。',
      },
    },
    {
      id: 'msg_wf_05',
      conversationId: 'conv_wf_delivery',
      authorId: 'agent_coder',
      createdAt: ago(56 * MIN),
      channel: 'group',
      audience: ['user'],
      payload: {
        kind: 'progress',
        title: '自主功能交付运行进度',
        percent: 62,
        note: '编码阶段 5 / 8 个子任务完成，测试反馈均在结构化上限内，暂无阻塞。',
      },
    },
    {
      id: 'msg_wf_06',
      conversationId: 'conv_wf_delivery',
      authorId: 'agent_reviewer',
      createdAt: ago(34 * MIN),
      channel: 'group',
      audience: ['agent_coder'],
      payload: {
        kind: 'text',
        text: '进度正常。裁决口径：只有明确输出 VERDICT: REWORK 才触发返工；缺失或含糊的 verdict 会标记 workflow.review_verdict_missing，不做静默放行。',
      },
    },
    {
      id: 'msg_wf_07',
      conversationId: 'conv_wf_delivery',
      authorId: 'agent_planner',
      createdAt: ago(12 * MIN),
      channel: 'group',
      audience: ['user'],
      payload: {
        kind: 'approval',
        title: '批准发布 v0.3.0 候选版本',
        detail: '把当前图草稿发布为不可变版本 v0.3.0，活跃触发器与后续执行将绑定到该发布版本。',
        actionType: 'file_write',
        status: 'pending',
      },
    },
  ],
  /* v6 已归档实例的消息种子：一轮已完成的运行 + 一张已批准的审批卡 */
  conv_wf_inst3: [
    {
      id: 'msg_inst3_01',
      conversationId: 'conv_wf_inst3',
      authorId: 'agent_planner',
      createdAt: ago(3 * DAY),
      channel: 'group',
      audience: 'all',
      payload: {
        kind: 'plan',
        title: '运行计划：自主功能交付图（实例 3）',
        summary: '补齐跨进程恢复场景回归，Explorer 并行度 2，max_rework_rounds=1。',
        items: [
          { text: 'Explorer 收集上下文（并行度 2）', done: true },
          { text: 'Coder 补齐恢复矩阵用例', done: true },
          { text: 'Reviewer 评审并输出 verdict', done: true },
          { text: '应用恢复补丁并回归验证', done: true },
        ],
      },
    },
    {
      id: 'msg_inst3_02',
      conversationId: 'conv_wf_inst3',
      authorId: 'agent_coder',
      createdAt: ago(3 * DAY - 30 * MIN),
      channel: 'group',
      audience: ['agent_planner'],
      payload: {
        kind: 'text',
        text: '恢复矩阵已补齐 6 个跨进程场景，等待评审裁决后应用补丁。',
      },
    },
    {
      id: 'msg_inst3_03',
      conversationId: 'conv_wf_inst3',
      authorId: 'agent_reviewer',
      createdAt: ago(3 * DAY - 55 * MIN),
      channel: 'group',
      audience: 'all',
      payload: {
        kind: 'approval',
        title: '批准应用恢复补丁',
        detail: '把 6 个跨进程恢复场景补丁写入 src/operant/workflow/recovery.py 并合入回归基线。',
        actionType: 'file_write',
        status: 'approved',
        /* 决定时间晚于卡片 createdAt（v6 修复轮）：批准发生在评审提出申请约 35 分钟后 */
        decidedAt: ago(3 * DAY - 90 * MIN),
        note: '用户批准',
      },
    },
    {
      id: 'msg_inst3_04',
      conversationId: 'conv_wf_inst3',
      authorId: 'agent_planner',
      createdAt: ago(2 * DAY),
      channel: 'group',
      audience: ['user'],
      payload: {
        kind: 'text',
        text: '实例 3 运行完成：补丁已合入，回归全绿。实例已归档，可在归档列表中回看。',
      },
    },
  ],
};

/* ========== Agent 过程日志（群聊会话 × Agent，个人界面条目流；永不进入群聊消息流） ========== */

export const DEMO_AGENT_LOGS: DemoAgentLogEntry[] = [
  /* ---- 规划师（agent_planner）---- */
  {
    id: 'log_wf_planner_01',
    sessionId: 'conv_wf_delivery',
    agentId: 'agent_planner',
    kind: 'thought',
    title: '拆解验收口径为四个阶段',
    detail: '把交付目标拆成上下文收集、独占写入、只读评审、候选发布四段；Explorer 并行度取 2，返工上限 1 轮。',
    createdAt: ago(95 * MIN),
  },
  {
    id: 'log_wf_planner_02',
    sessionId: 'conv_wf_delivery',
    agentId: 'agent_planner',
    kind: 'tool_call',
    title: 'list_graph_revisions: 校验发布基线',
    detail: '确认当前草稿与 rev_graph_v1 的节点差异，避免在过期版本上规划。',
    createdAt: ago(93 * MIN),
  },
  {
    id: 'log_wf_planner_03',
    sessionId: 'conv_wf_delivery',
    agentId: 'agent_planner',
    kind: 'intermediate',
    title: '已下发执行计划给编码员与评审员',
    detail: '计划卡同步到群聊，寻址全员；编码员确认接手 Coder 阶段。',
    createdAt: ago(91 * MIN),
  },
  {
    id: 'log_wf_planner_04',
    sessionId: 'conv_wf_delivery',
    agentId: 'agent_planner',
    kind: 'thought',
    title: '发布前需用户确认候选版本',
    detail: 'v0.3.0 发布会绑定活跃触发器，属不可逆动作，须走审批卡等用户裁决。',
    createdAt: ago(14 * MIN),
  },
  /* ---- 精准编码员（agent_coder）---- */
  {
    id: 'log_wf_coder_01',
    sessionId: 'conv_wf_delivery',
    agentId: 'agent_coder',
    kind: 'thought',
    title: '确认写入回执后再进入下一阶段',
    detail: 'Coder 阶段写入结果未知时禁止重放，须转人工核对（manual_reconcile_required）。',
    createdAt: ago(87 * MIN),
  },
  {
    id: 'log_wf_coder_02',
    sessionId: 'conv_wf_delivery',
    agentId: 'agent_coder',
    kind: 'tool_call',
    title: 'run_command: pytest tests/workflow -q',
    detail: '34 / 50 用例通过，失败签名已按 12,000 字符上限结构化截断。',
    createdAt: ago(74 * MIN),
  },
  {
    id: 'log_wf_coder_03',
    sessionId: 'conv_wf_delivery',
    agentId: 'agent_coder',
    kind: 'intermediate',
    title: '已生成补丁草稿 recovery.patch',
    detail: '覆盖 5 / 8 个子任务；等待评审员裁决后再写入工作区。',
    createdAt: ago(58 * MIN),
  },
  {
    id: 'log_wf_coder_04',
    sessionId: 'conv_wf_delivery',
    agentId: 'agent_coder',
    kind: 'tool_call',
    title: 'apply_patch: src/operant/workflow/recovery.py',
    detail: '补充检查点恢复路径的写入回执校验，差异 42 行。',
    createdAt: ago(55 * MIN),
  },
  /* ---- 评审员（agent_reviewer）---- */
  {
    id: 'log_wf_reviewer_01',
    sessionId: 'conv_wf_delivery',
    agentId: 'agent_reviewer',
    kind: 'thought',
    title: '核对 verdict 输出口径',
    detail: '只有明确输出 VERDICT: REWORK 才触发返工；缺失或含糊一律标记 review_verdict_missing。',
    createdAt: ago(83 * MIN),
  },
  {
    id: 'log_wf_reviewer_02',
    sessionId: 'conv_wf_delivery',
    agentId: 'agent_reviewer',
    kind: 'tool_call',
    title: 'run_command: pytest tests/workflow/test_recovery_reconcile.py -q',
    detail: '恢复回归用例全绿，续期并发保护测试仍缺。',
    createdAt: ago(40 * MIN),
  },
  {
    id: 'log_wf_reviewer_03',
    sessionId: 'conv_wf_delivery',
    agentId: 'agent_reviewer',
    kind: 'intermediate',
    title: '评审意见草稿：续期并发保护需补测试',
    detail: '若最终 verdict 为 REWORK，仅此一项；不构成当前阻塞。',
    createdAt: ago(33 * MIN),
  },
];

/* ========== 临时工作流（运行时经 buildTempWorkflow 创建，初始为空） ========== */

export const DEMO_TEMP_WORKFLOWS: DemoTempWorkflow[] = [];

/* ========== 演示运行实例（任务=运行实例；与 MockClient run_operant_001 区分） ========== */

export const DEMO_RUNS_EXTRA: DemoRunItem[] = [
  {
    id: 'demo_run_seed_001',
    workflowId: 'rev_graph_v1',
    name: '自主功能交付 · 演示实例 A',
    status: 'running',
    createdAt: ago(50 * MIN),
  },
];

/* ========== 文件（与会话联动，数量即 Tab 徽标） ========== */

export const DEMO_FILES: Record<string, DemoFileItem[]> = {
  conv_recovery: [
    {
      id: 'file_rec_01',
      conversationId: 'conv_recovery',
      name: 'recovery.py',
      path: 'src/operant/workflow/recovery.py',
      size: '12.4 KB',
      modifiedAt: ago(2 * HOUR - 12 * MIN),
    },
    {
      id: 'file_rec_02',
      conversationId: 'conv_recovery',
      name: 'checkpoint.py',
      path: 'src/operant/workflow/checkpoint.py',
      size: '8.1 KB',
      modifiedAt: ago(3 * HOUR),
    },
    {
      id: 'file_rec_03',
      conversationId: 'conv_recovery',
      name: 'test_recovery_reconcile.py',
      path: 'tests/workflow/test_recovery_reconcile.py',
      size: '5.6 KB',
      modifiedAt: ago(50 * MIN),
    },
  ],
  conv_pr4: [
    {
      id: 'file_pr4_01',
      conversationId: 'conv_pr4',
      name: 'approvals.py',
      path: 'src/operant/gateway/approvals.py',
      size: '9.8 KB',
      modifiedAt: ago(4 * HOUR),
    },
    {
      id: 'file_pr4_02',
      conversationId: 'conv_pr4',
      name: 'schema.sql',
      path: 'src/operant/storage/schema.sql',
      size: '3.2 KB',
      modifiedAt: ago(5 * HOUR),
    },
  ],
  conv_graph: [
    {
      id: 'file_graph_01',
      conversationId: 'conv_graph',
      name: 'diagnostics.ts',
      path: 'src/operant/graph/diagnostics.ts',
      size: '6.7 KB',
      modifiedAt: ago(3 * DAY),
    },
  ],
  conv_eval: [],
  conv_remote: [],
  conv_rail: [],
  conv_wf_delivery: [],
  conv_wf_inst3: [],
};

/* ========== 任务（与会话联动，数量即 Tab 徽标） ========== */

export const DEMO_TASKS: DemoTaskItem[] = [
  {
    id: 'task_rec_01',
    title: '校验 Coder 写入回执后再重放',
    done: true,
    conversationId: 'conv_recovery',
  },
  {
    id: 'task_rec_02',
    title: '补充 manual_reconcile_required 状态分支',
    done: true,
    conversationId: 'conv_recovery',
  },
  {
    id: 'task_rec_03',
    title: '跨进程恢复回归测试补齐到 50 例',
    done: false,
    conversationId: 'conv_recovery',
    due: fromNow(DAY),
  },
  {
    id: 'task_pr4_01',
    title: 'approve_for_run 续期并发保护测试',
    done: false,
    conversationId: 'conv_pr4',
  },
  {
    id: 'task_pr4_02',
    title: '拒绝原因文案与审计字段对齐',
    done: false,
    conversationId: 'conv_pr4',
  },
  {
    id: 'task_eval_01',
    title: '实验 21—22 并行度 2 数据采集',
    done: false,
    conversationId: 'conv_eval',
    due: fromNow(3 * DAY),
  },
];

/* ========== 知识库 ========== */

export const DEMO_KNOWLEDGE: Record<string, DemoKnowledgeItem[]> = {
  conv_recovery: [
    {
      id: 'kn_rec_01',
      conversationId: 'conv_recovery',
      title: '恢复权威原则',
      summary: 'Workflow Run 以 SQLite 阶段检查点为唯一恢复权威；写入结果未知时必须转人工核对，禁止盲目重放。',
      source: '项目记忆',
    },
    {
      id: 'kn_rec_02',
      conversationId: 'conv_recovery',
      title: '失败测试反馈截断',
      summary: '结构化失败反馈上限 12,000 字符；连续相同失败签名达到阈值即停止并报告 agent.no_progress。',
      source: '会话摘要',
    },
  ],
  conv_pr4: [
    {
      id: 'kn_pr4_01',
      conversationId: 'conv_pr4',
      title: '审批租约模型',
      summary: 'approve_once 仅单次动作有效；approve_for_run 绑定 Run 生命周期，过期需显式续期。',
      source: '工作区文件',
    },
  ],
  conv_eval: [],
  conv_graph: [],
  conv_remote: [],
  conv_rail: [],
  conv_wf_delivery: [],
  conv_wf_inst3: [],
};

/* ========== 调度 ========== */

export const DEMO_SCHEDULES: DemoSchedule[] = [
  {
    id: 'sch_nightly_eval',
    name: '每日评估回归',
    type: 'cron',
    cron: '0 3 * * *',
    targetType: 'workflow',
    targetId: 'wf_delivery_01',
    targetPayload: '自主功能交付图 (v0.3.0)',
    nextRun: fromNow(7 * HOUR),
    enabled: true,
    lastRunStatus: 'success',
    lastRunAt: ago(17 * HOUR),
  },
  {
    id: 'sch_memory_sweep',
    name: '候选记忆周清理',
    type: 'cron',
    cron: '30 2 * * 1',
    targetType: 'health_check',
    targetPayload: '候选记忆 SQLite FTS5 索引与孤儿项巡检',
    nextRun: fromNow(2 * DAY),
    enabled: true,
    lastRunStatus: 'success',
    lastRunAt: ago(3 * DAY),
  },
  {
    id: 'sch_timer_build',
    name: '诊断与测试巡检',
    type: 'timer',
    timerSeconds: 3600,
    targetType: 'prompt',
    targetId: 'conv_alpha',
    targetPayload: '运行全量测试并导出诊断日志',
    nextRun: fromNow(45 * MIN),
    enabled: true,
    lastRunStatus: 'running',
    lastRunAt: ago(15 * MIN),
  },
  {
    id: 'sch_cache_prune',
    name: '执行缓存宽限清理',
    type: 'cron',
    cron: '15 4 * * 6',
    targetType: 'health_check',
    targetPayload: '执行缓存 7 天宽限期清理',
    nextRun: fromNow(4 * DAY),
    enabled: false,
  },
];

/* ========== 扩展（MCP Server 与插件） ========== */

export const DEMO_EXTENSIONS: DemoExtension[] = [
  {
    id: 'ext_mcp_github',
    name: 'GitHub MCP Server',
    desc: 'GitHub 仓库读写、PR 评审、Issue 跟踪与 Actions 日志检索。',
    type: 'mcp',
    protocol: 'stdio',
    command: 'npx -y @modelcontextprotocol/server-github',
    toolsCount: 18,
    version: '1.2.0',
    enabled: true,
    status: 'connected',
  },
  {
    id: 'ext_mcp_postgres',
    name: 'PostgreSQL MCP Server',
    desc: '安全只读结构化查询、Schema 导出与性能分析。',
    type: 'mcp',
    protocol: 'stdio',
    command: 'uvx mcp-server-postgres --conn postgresql://...',
    toolsCount: 8,
    version: '0.4.1',
    enabled: true,
    status: 'connected',
  },
  {
    id: 'ext_mcp_brave',
    name: 'Brave Search MCP',
    desc: 'Brave Search 实时公网检索与权威资料引用。',
    type: 'mcp',
    protocol: 'sse',
    endpoint: 'http://localhost:8080/sse',
    toolsCount: 4,
    version: '0.9.0',
    enabled: false,
    status: 'disconnected',
  },
  {
    id: 'ext_audit_export',
    name: '审计导出插件',
    desc: '把 Action Gateway 审计轨迹导出为 JSONL 归档。',
    type: 'plugin',
    version: '0.3.1',
    enabled: true,
    status: 'connected',
  },
  {
    id: 'ext_lark_notify',
    name: '飞书通知插件',
    desc: '审批等待与 Run 完成时推送飞书即时消息。',
    type: 'plugin',
    version: '0.1.4',
    enabled: true,
    status: 'connected',
  },
  {
    id: 'ext_trace_viewer',
    name: 'Trace 查看器插件',
    desc: '为每次 Run 生成可视化调用链火焰图。',
    type: 'plugin',
    version: '0.0.9',
    enabled: false,
    status: 'disconnected',
  },
];

/* ========== 技能（遵循 standard Agent Skill 规范） ========== */

export const DEMO_SKILLS: DemoSkill[] = [
  {
    id: 'skill_agy_customizations',
    name: 'agy-customizations',
    desc: 'Antigravity 自定义系统综合参考与规范加载器，管理技能、规则与 MCP。',
    spec: 'standard-v1',
    path: '~/.gemini/antigravity/builtin/skills/agy-customizations',
    author: 'Antigravity Team',
    category: '系统定制',
    tools: ['view_file', 'grep_search', 'manage_task'],
    scripts: ['load_customizations.py'],
    markdownContent: `# agy-customizations\n\n解析并管理技能、规则、插件与 MCP 服务。支持动态扩展 Agent 工具箱与上下文。`,
    enabled: true,
  },
  {
    id: 'skill_generative_ui',
    name: 'generative_ui',
    desc: '在聊天界面内联渲染富交互 HTML 组件与独立 Artifact 图表与数据表格。',
    spec: 'standard-v1',
    path: '~/.gemini/antigravity/builtin/skills/generative_ui',
    author: 'Google DeepMind',
    category: '界面交互',
    tools: ['write_to_file', 'render_ui'],
    scripts: ['render_widget.js'],
    markdownContent: `# generative_ui\n\n生成富客户端图表、表单与可视化组件，无缝集成于聊天信息流中。`,
    enabled: true,
  },
  {
    id: 'skill_python_deps',
    name: 'managing-python-dependencies',
    desc: '规范化 Python 依赖管理，遵循 uv 与项目特定虚拟环境，避免全局污染。',
    spec: 'standard-v1',
    path: '~/.gemini/config/skills/managing-python-dependencies',
    author: 'Operant Core',
    category: '工程环境',
    tools: ['run_command'],
    scripts: ['uv_sync.sh'],
    markdownContent: `# managing-python-dependencies\n\n禁止全局 pip install，严格使用 uv 与虚拟环境，保持依赖锁定一致性。`,
    enabled: true,
  },
  {
    id: 'skill_ml_practices',
    name: 'ml-best-practices',
    desc: '机器学习与数据分析工程最佳实践与规范指导（回归/分类/聚类/时序）。',
    spec: 'standard-v1',
    path: '~/.gemini/config/skills/ml-best-practices',
    author: 'Data Platform',
    category: '算法建模',
    tools: ['view_file', 'run_command'],
    scripts: ['eval_metrics.py'],
    markdownContent: `# ml-best-practices\n\n指导回归、分类、聚类与时间序列分析，提供标准分析流水线模板。`,
    enabled: true,
  },
  {
    id: 'skill_apply_patch',
    name: 'apply_patch 安全写入',
    desc: '约束 Coder 仅通过 apply_patch 修改源码，容器测试写入不回传宿主。',
    spec: 'standard-v1',
    path: '.operant/skills/apply_patch',
    author: 'Codex',
    category: '代码修改',
    tools: ['apply_patch'],
    scripts: ['patch_guard.py'],
    markdownContent: `# apply_patch 安全写入\n\n必须使用标准 patch hunk 进行原子写入，测试环境隔离执行。`,
    enabled: true,
  },
  {
    id: 'skill_test_feedback',
    name: '结构化测试反馈',
    desc: '把失败测试摘要为签名与关键帧，限制在 12,000 字符内，防止模型上下文膨胀。',
    spec: 'standard-v1',
    path: '.operant/skills/test_feedback',
    author: 'Codex',
    category: '测试验证',
    tools: ['run_command'],
    scripts: ['parse_failures.py'],
    markdownContent: `# 结构化测试反馈\n\n提取连续失败签名并做结构化截断，防止无效循环。`,
    enabled: true,
  },
];

/* ========== 项目已装载技能映射（projectId -> skillId[]） ========== */
export const DEMO_PROJECT_SKILLS: Record<string, string[]> = {
  proj_workspace: [
    'skill_agy_customizations',
    'skill_generative_ui',
    'skill_python_deps',
    'skill_apply_patch',
    'skill_test_feedback',
  ],
  proj_kernel: ['skill_python_deps', 'skill_apply_patch', 'skill_test_feedback'],
  proj_clients: ['skill_agy_customizations', 'skill_generative_ui'],
  proj_eval: ['skill_ml_practices', 'skill_test_feedback'],
};
