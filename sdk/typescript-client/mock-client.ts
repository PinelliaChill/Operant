/**
 * Operant 2.0 High-Fidelity Mock Client
 * Complete in-memory simulation of Operant Core 2.0 state, SSE streaming,
 * sub-agent derivation, approval interceptors, and graph transitions.
 */

import {
  AnyOperantEvent,
  ApprovalCard,
  ApprovalDecision,
  CanonicalAgentMessage,
  CommandReceipt,
  ContextRevision,
  EvaluationResult,
  EvaluationRun,
  EvaluationSuite,
  EventCursor,
  GraphCompilerDiagnostic,
  GraphDefinitionRevision,
  GraphDraft,
  Memory,
  ModelProfile,
  RemoteDevice,
  RemoteHost,
  RolePreset,
  Session,
  Thread,
  WorkflowRun,
} from '../protocol';
import { EventSubscriber, EventUnsubscribe, OperantClient } from './client';

export class MockClient implements OperantClient {
  readonly isMock = true;

  // In-memory mock databases
  private models: ModelProfile[] = [];
  private roles: RolePreset[] = [];
  private sessions: Session[] = [];
  private threads: Thread[] = [];
  private messages: Map<string, CanonicalAgentMessage[]> = new Map();
  private contextRevisions: Map<string, ContextRevision> = new Map();
  private approvals: ApprovalCard[] = [];
  private graphDrafts: GraphDraft[] = [];
  private graphRevisions: GraphDefinitionRevision[] = [];
  private workflowRuns: WorkflowRun[] = [];
  private remoteHosts: RemoteHost[] = [];
  private remoteDevices: RemoteDevice[] = [];
  private memories: Memory[] = [];
  private evalSuites: EvaluationSuite[] = [];
  private evalRuns: EvaluationRun[] = [];
  private evalResults: EvaluationResult[] = [];
  private globalSubscribers: Set<EventSubscriber> = new Set();

  constructor() {
    this.seedInitialData();
  }

  private seedInitialData() {
    const now = new Date().toISOString();

    // 1. Models (Configured via provider discovery, NO hardcoded fake vendors)
    this.models = [
      {
        id: 'model_claude_sonnet_37',
        name: 'Anthropic Claude 3.7 Sonnet',
        provider: 'openai-compatible',
        model_id: 'claude-3-7-sonnet-20250219',
        base_url: 'https://api.anthropic.com/v1',
        secret_ref: 'ANTHROPIC_API_KEY',
        context_window: 200000,
        default_token_budget: 8000,
        supported_efforts: ['low', 'medium', 'high'],
        default_effort: 'medium',
        effort_parameter: 'reasoning_effort',
        effort_mapping: [
          { effort: 'low', provider_value: 'low' },
          { effort: 'medium', provider_value: 'medium' },
          { effort: 'high', provider_value: 'high' },
        ],
        enabled: true,
        created_at: now,
      },
      {
        id: 'model_gpt_4o',
        name: 'OpenAI GPT-4o',
        provider: 'openai-compatible',
        model_id: 'gpt-4o-2024-11-20',
        base_url: 'https://api.openai.com/v1',
        secret_ref: 'OPENAI_API_KEY',
        context_window: 128000,
        default_token_budget: 4096,
        supported_efforts: ['low', 'medium'],
        default_effort: 'medium',
        effort_parameter: 'reasoning_effort',
        effort_mapping: [
          { effort: 'low', provider_value: 'low' },
          { effort: 'medium', provider_value: 'medium' },
        ],
        enabled: true,
        created_at: now,
      },
      {
        id: 'model_deepseek_r1',
        name: 'DeepSeek R1 Distill',
        provider: 'openai-compatible',
        model_id: 'deepseek-r1-2025',
        base_url: 'https://api.deepseek.com/v1',
        secret_ref: 'DEEPSEEK_API_KEY',
        context_window: 64000,
        default_token_budget: 4096,
        supported_efforts: ['medium', 'high'],
        default_effort: 'high',
        effort_parameter: 'reasoning_effort',
        effort_mapping: [
          { effort: 'medium', provider_value: 'medium' },
          { effort: 'high', provider_value: 'high' },
        ],
        enabled: true,
        created_at: now,
      },
    ];

    // 2. Roles
    this.roles = [
      {
        id: 'role_main',
        version: 1,
        name: '通用助手',
        system_prompt: '你是一个智能、安全的编码助手，负责拆解复杂编程任务。',
        model_profile_id: 'model_claude_sonnet_37',
        effort: 'medium',
        tool_policy: {
          allowed_tools: ['read_file', 'search_files', 'apply_patch', 'run_command', 'git_diff'],
          workspace_write: true,
          command_execution: true,
          approval_required: ['git_write', 'destructive', 'privileged'],
        },
        budget: {
          max_turns: 15,
          max_consecutive_test_failures: 2,
          timeout_seconds: 300,
          max_output_tokens: 8000,
        },
        memory_scope: 'session',
        status: 'active',
        created_at: now,
      },
      {
        id: 'role_planner',
        version: 1,
        name: '架构规划师',
        system_prompt: '分析仓库架构、评估需求并产出结构化实施计划。',
        model_profile_id: 'model_claude_sonnet_37',
        effort: 'high',
        tool_policy: {
          allowed_tools: ['read_file', 'search_files'],
          workspace_write: false,
          command_execution: false,
          approval_required: [],
        },
        budget: {
          max_turns: 8,
          max_consecutive_test_failures: 2,
          timeout_seconds: 180,
          max_output_tokens: 6000,
        },
        memory_scope: 'project',
        status: 'active',
        created_at: now,
      },
      {
        id: 'role_coder',
        version: 1,
        name: '精准编码员',
        system_prompt: '遵循项目既有约定，实现最小化、正确、类型安全的修改。',
        model_profile_id: 'model_claude_sonnet_37',
        effort: 'high',
        tool_policy: {
          allowed_tools: ['read_file', 'search_files', 'apply_patch', 'run_command', 'git_diff'],
          workspace_write: true,
          command_execution: true,
          approval_required: ['privileged', 'git_write'],
          command_execution_policy: {
            runner: 'host',
            docker_image: 'python:3.13-slim',
            cpu_limit: 2.0,
            memory_limit_mb: 2048,
            pids_limit: 512,
          },
        },
        budget: {
          max_turns: 20,
          max_consecutive_test_failures: 3,
          timeout_seconds: 600,
          max_output_tokens: 12000,
        },
        memory_scope: 'session',
        status: 'active',
        created_at: now,
      },
      {
        id: 'role_reviewer',
        version: 1,
        name: '安全与质量评审员',
        system_prompt: '检查差异与执行测试反馈，确保安全策略与类型被严格遵守。',
        model_profile_id: 'model_gpt_4o',
        effort: 'medium',
        tool_policy: {
          allowed_tools: ['read_file', 'search_files', 'git_diff', 'run_command'],
          workspace_write: false,
          command_execution: true,
          approval_required: ['destructive'],
        },
        budget: {
          max_turns: 6,
          max_consecutive_test_failures: 2,
          timeout_seconds: 120,
          max_output_tokens: 4000,
        },
        memory_scope: 'session',
        status: 'active',
        created_at: now,
      },
    ];

    // 3. Initial Session & Thread Hierarchy
    const sessionId = 'session_mock_alpha';
    const mainThreadId = 'thread_main_alpha';
    const childThreadId = 'thread_child_subtask_1';

    this.sessions = [
      {
        id: sessionId,
        role_snapshot: {
          role_id: 'role_main',
          role_version: 1,
          role_name: '通用助手',
          system_prompt: this.roles[0].system_prompt,
          model_profile_id: 'model_claude_sonnet_37',
          model_profile_name: 'Anthropic Claude 3.7 Sonnet',
          provider: 'openai-compatible',
          model_id: 'claude-3-7-sonnet-20250219',
          base_url: 'https://api.anthropic.com/v1',
          secret_ref: 'ANTHROPIC_API_KEY',
          effort: 'medium',
          tool_policy: this.roles[0].tool_policy,
          budget: this.roles[0].budget,
          memory_scope: 'session',
          captured_at: now,
          overrides: { effort_overridden: false, model_profile_overridden: false, budget_fields: [] },
        },
        created_at: now,
      },
    ];

    this.threads = [
      {
        id: mainThreadId,
        workspace: '/Users/bigo/agentworkspace/codexworkspace/operant',
        title: 'Operant 2.0 GUI 架构重构',
        session_id: sessionId,
        root_agent_id: 'agent_main_01',
        status: 'active',
        created_at: now,
        updated_at: now,
      },
      {
        id: childThreadId,
        workspace: '/Users/bigo/agentworkspace/codexworkspace/operant',
        title: '子代理：编译协议类型定义',
        session_id: sessionId,
        root_agent_id: 'agent_sub_01',
        status: 'completed',
        parent_link: {
          parent_thread_id: mainThreadId,
          parent_agent_id: 'agent_main_01',
          spawn_reason: '隔离协议模式校验与错误分类体系',
          depth: 1,
        },
        created_at: now,
        updated_at: now,
      },
    ];

    // 4. Messages (No hidden reasoning! Structured summary + transparent actions)
    this.messages.set(mainThreadId, [
      {
        id: 'msg_001',
        thread_id: mainThreadId,
        sender: { type: 'user', id: 'user_01', name: '开发者' },
        role: 'user',
        content: '请检查仓库并实现 Operant 2.0 SDK 与 React GUI 骨架。',
        visibility: { deliver_to: ['*'], ui_visible_to: ['*'], audit_visible: true, context_injection: 'immediate' },
        created_at: new Date(Date.now() - 300000).toISOString(),
      },
      {
        id: 'msg_002',
        thread_id: mainThreadId,
        sender: { type: 'agent', id: 'agent_main_01', name: '主 Harness 代理', role_name: '通用助手' },
        role: 'agent',
        content: '我已分析 docs/UI_UX_DESIGN_SPECIFICATION.md 中的项目规范，并启动了协议 SDK 构建。',
        structured_summary: {
          goal: '构建 Operant 2.0 协议模式与客户端框架',
          current_phase: 'SDK 协议实现',
          action_summary: [
            '检查了 src/operant/api.py 中已有的 FastAPI 端点',
            '在 sdk/protocol/models.ts 中定义了 2.0 领域模型',
            '实现了 Action Gateway 与风险分级结构',
          ],
          evidence_refs: ['docs/UI_UX_DESIGN_SPECIFICATION.md#L185-215', 'src/operant/domain/models.py#L45-90'],
          artifact_refs: ['artifact_protocol_v2'],
          conclusions: ['Core SQLite 仍是恢复权威；GUI 是纯投影。'],
          uncertainties: [],
          usage: { input_tokens: 3120, output_tokens: 950, cached_tokens: 1800, cost_usd: 0.024 },
        },
        tool_calls: [
          {
            id: 'tc_read_spec',
            tool_name: 'read_file',
            arguments: { path: 'docs/UI_UX_DESIGN_SPECIFICATION.md' },
            requires_approval: false,
            result: { success: true, output: '已读取 UI/UX 设计规范 800 行。' },
          },
          {
            id: 'tc_write_protocol',
            tool_name: 'apply_patch',
            arguments: { target: 'sdk/protocol/models.ts' },
            requires_approval: false,
            result: {
              success: true,
              diff: '@@ -0,0 +1,180 @@\n+export interface ModelProfile { ... }\n+export interface ContextRevision { ... }',
            },
          },
        ],
        visibility: { deliver_to: ['*'], ui_visible_to: ['*'], audit_visible: true, context_injection: 'immediate' },
        created_at: new Date(Date.now() - 180000).toISOString(),
      },
    ]);

    this.messages.set(childThreadId, [
      {
        id: 'msg_sub_001',
        thread_id: childThreadId,
        sender: { type: 'agent', id: 'agent_sub_01', name: '模式专家', role_name: '精准编码员' },
        role: 'agent',
        content: '已完成 Action Gateway、能力租约与远程设备的领域模型类型校验。',
        structured_summary: {
          goal: '对照 SQLite 数据库模式校验 TypeScript 类型',
          current_phase: '已完成',
          action_summary: ['构建了严格的错误码分类体系', '校验了事件序列属性'],
          evidence_refs: ['sdk/protocol/errors.ts'],
          artifact_refs: [],
          conclusions: ['所有类型严格编译通过，无 any。'],
          uncertainties: [],
        },
        visibility: { deliver_to: ['*'], ui_visible_to: ['*'], audit_visible: true, context_injection: 'immediate' },
        created_at: new Date(Date.now() - 60000).toISOString(),
      },
    ]);

    // 5. Context Revision
    this.contextRevisions.set(mainThreadId, {
      revision_id: 'rev_ctx_001',
      thread_id: mainThreadId,
      total_tokens: 5870,
      context_window_limit: 200000,
      components: [
        {
          id: 'ctx_comp_sys',
          type: 'system_policy',
          label: '安全与 Action Gateway 策略',
          estimated_tokens: 820,
          is_pinned: true,
          is_removable: false,
          is_compacted: false,
          updated_at: now,
        },
        {
          id: 'ctx_comp_role',
          type: 'role_snapshot',
          label: '角色快照（通用助手 v1）',
          estimated_tokens: 650,
          is_pinned: true,
          is_removable: false,
          is_compacted: false,
          updated_at: now,
        },
        {
          id: 'ctx_comp_msg',
          type: 'messages',
          label: '活跃消息与工具结果',
          estimated_tokens: 2400,
          is_pinned: false,
          is_removable: true,
          is_compacted: false,
          updated_at: now,
        },
        {
          id: 'ctx_comp_files',
          type: 'file_slices',
          label: '引用的项目文件（3 个）',
          estimated_tokens: 1200,
          is_pinned: false,
          is_removable: true,
          is_compacted: false,
          updated_at: now,
          references: [
            { id: 'ref_1', source: 'workspace', path: 'docs/UI_UX_DESIGN_SPECIFICATION.md', line_range: [1, 220] },
            { id: 'ref_2', source: 'workspace', path: 'src/operant/api.py', line_range: [1, 150] },
          ],
        },
        {
          id: 'ctx_comp_reserved',
          type: 'reserved_output',
          label: '预留输出预算',
          estimated_tokens: 800,
          is_pinned: true,
          is_removable: false,
          is_compacted: false,
          updated_at: now,
        },
      ],
      cache_hit_rate: 0.62,
      created_at: now,
    });

    // 6. Action Gateway Approvals (ASK queue)
    this.approvals = [
      {
        id: 'appr_001',
        session_id: sessionId,
        thread_id: mainThreadId,
        agent_instance_id: 'agent_main_01',
        role_name: '精准编码员',
        host_id: 'host_local_01',
        action_name: 'run_command',
        action_params_summary: {
          command: 'npm install --save-dev typescript@5.7 vite@6.0',
          cwd: '/Users/bigo/agentworkspace/codexworkspace/operant/clients/gui',
        },
        target_resource: 'clients/gui/package.json',
        workspace_boundary: '/Users/bigo/agentworkspace/codexworkspace/operant',
        action_hash: 'sha256:7f83b1657ff1fc53b92dc18148a1d65dfc2d4b1fa3d677284addd200126d9069',
        risk_tier: 'high',
        risk_type: 'file_write',
        impact_summary: {
          files_affected: ['clients/gui/package.json', 'clients/gui/package-lock.json', 'node_modules/'],
          network_targets: ['registry.npmjs.org'],
          host_impact: '启动 npm 包安装进程，并产生外网访问。',
        },
        matched_policy_rule: {
          rule_id: 'rule_package_manager_exec',
          source_scope: 'workspace',
          decision: 'ASK',
          description: '包管理命令会修改工作区依赖，需要用户明确批准。',
        },
        llm_review_advice: {
          recommended_decision: 'approve',
          confidence: 0.94,
          reasoning_summary: '目标包安装与当前搭建 clients/gui 构建框架的任务目标一致。',
          risk_factors: ['对 npm registry 的外网请求', '写入 node_modules 目录层级'],
        },
        status: 'pending',
        created_at: new Date(Date.now() - 45000).toISOString(),
        expires_at: new Date(Date.now() + 555000).toISOString(),
      },
      {
        id: 'appr_002',
        session_id: sessionId,
        thread_id: mainThreadId,
        agent_instance_id: 'agent_main_01',
        role_name: '精准编码员',
        host_id: 'host_local_01',
        action_name: 'git_commit_and_push',
        action_params_summary: {
          branch: 'main',
          remote: 'origin',
        },
        target_resource: 'git_repository',
        workspace_boundary: '/Users/bigo/agentworkspace/codexworkspace/operant',
        action_hash: 'sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
        risk_tier: 'critical',
        risk_type: 'privileged_exec',
        impact_summary: {
          files_affected: ['all_committed_files'],
          network_targets: ['github.com'],
          host_impact: '将代码变更推送到上游远程分支。',
        },
        matched_policy_rule: {
          rule_id: 'rule_git_push_deny',
          source_scope: 'global',
          decision: 'DENY',
          description: '策略网关严格阻止直接推送到 main 分支。',
        },
        status: 'rejected',
        created_at: new Date(Date.now() - 120000).toISOString(),
        expires_at: new Date(Date.now() - 60000).toISOString(),
      },
    ];

    // 7. Workflow Graph Drafts & Revisions
    this.graphDrafts = [
      {
        id: 'draft_coding_pipeline',
        workspace: '/Users/bigo/agentworkspace/codexworkspace/operant',
        name: '自主功能交付图',
        description: '多代理编排流水线，包含有界并行探索与评审验证循环。',
        nodes: [
          {
            id: 'node_planner',
            label: '架构规划师',
            type: 'agent',
            position: { x: 80, y: 140 },
            role_id: 'role_planner',
            inputs: [{ id: 'in_task', name: 'task_prompt', type: 'string', direction: 'input', required: true }],
            outputs: [{ id: 'out_plan', name: 'implementation_plan', type: 'artifact', direction: 'output' }],
            scope_inheritance: {
              model_profile: { value: 'model_claude_sonnet_37', source: 'workflow' },
              budget: { value: this.roles[1].budget, source: 'node_override' },
              tool_policy: { value: this.roles[1].tool_policy, source: 'global' },
            },
          },
          {
            id: 'node_explorer_1',
            label: '代码库探索器（类型）',
            type: 'agent',
            position: { x: 340, y: 60 },
            role_id: 'role_planner',
            inputs: [{ id: 'in_plan_1', name: 'plan_ref', type: 'artifact', direction: 'input' }],
            outputs: [{ id: 'out_symbols', name: 'symbol_analysis', type: 'artifact', direction: 'output' }],
          },
          {
            id: 'node_explorer_2',
            label: '代码库探索器（测试）',
            type: 'agent',
            position: { x: 340, y: 220 },
            role_id: 'role_planner',
            inputs: [{ id: 'in_plan_2', name: 'plan_ref', type: 'artifact', direction: 'input' }],
            outputs: [{ id: 'out_fixtures', name: 'test_fixtures', type: 'artifact', direction: 'output' }],
          },
          {
            id: 'node_join_explorers',
            label: '聚合上下文',
            type: 'join',
            position: { x: 600, y: 140 },
            inputs: [
              { id: 'in_j1', name: 'analysis', type: 'artifact', direction: 'input' },
              { id: 'in_j2', name: 'fixtures', type: 'artifact', direction: 'input' },
            ],
            outputs: [{ id: 'out_context', name: 'merged_context', type: 'artifact', direction: 'output' }],
          },
          {
            id: 'node_coder',
            label: '精准编码员',
            type: 'agent',
            position: { x: 840, y: 140 },
            role_id: 'role_coder',
            inputs: [{ id: 'in_ctx', name: 'merged_context', type: 'artifact', direction: 'input' }],
            outputs: [{ id: 'out_diff', name: 'workspace_diff', type: 'artifact', direction: 'output' }],
          },
          {
            id: 'node_approval',
            label: '网关审批闸门',
            type: 'approval',
            position: { x: 1080, y: 140 },
            inputs: [{ id: 'in_diff', name: 'diff_to_verify', type: 'artifact', direction: 'input' }],
            outputs: [{ id: 'out_approved', name: 'approval_receipt', type: 'any', direction: 'output' }],
          },
          {
            id: 'node_reviewer',
            label: '质量与测试评审员',
            type: 'agent',
            position: { x: 1320, y: 140 },
            role_id: 'role_reviewer',
            inputs: [{ id: 'in_appr', name: 'approved_diff', type: 'any', direction: 'input' }],
            outputs: [{ id: 'out_verdict', name: 'verdict', type: 'string', direction: 'output' }],
            loop_constraint: {
              max_iterations: 3,
              timeout_seconds: 600,
              exit_condition_expr: 'verdict == "PASS"',
              no_progress_threshold: 2,
            },
          },
        ],
        edges: [
          { id: 'e1', source_node_id: 'node_planner', source_port_id: 'out_plan', target_node_id: 'node_explorer_1', target_port_id: 'in_plan_1', edge_type: 'data' },
          { id: 'e2', source_node_id: 'node_planner', source_port_id: 'out_plan', target_node_id: 'node_explorer_2', target_port_id: 'in_plan_2', edge_type: 'data' },
          { id: 'e3', source_node_id: 'node_explorer_1', source_port_id: 'out_symbols', target_node_id: 'node_join_explorers', target_port_id: 'in_j1', edge_type: 'data' },
          { id: 'e4', source_node_id: 'node_explorer_2', source_port_id: 'out_fixtures', target_node_id: 'node_join_explorers', target_port_id: 'in_j2', edge_type: 'data' },
          { id: 'e5', source_node_id: 'node_join_explorers', source_port_id: 'out_context', target_node_id: 'node_coder', target_port_id: 'in_ctx', edge_type: 'data' },
          { id: 'e6', source_node_id: 'node_coder', source_port_id: 'out_diff', target_node_id: 'node_approval', target_port_id: 'in_diff', edge_type: 'data' },
          { id: 'e7', source_node_id: 'node_approval', source_port_id: 'out_approved', target_node_id: 'node_reviewer', target_port_id: 'in_appr', edge_type: 'control' },
          { id: 'e8', source_node_id: 'node_reviewer', source_port_id: 'out_verdict', target_node_id: 'node_coder', target_port_id: 'in_ctx', edge_type: 'loop_back', condition_label: '失败 -> 返工' },
        ],
        updated_at: now,
        is_valid: true,
        diagnostics: [],
      },
    ];

    this.graphRevisions = [
      {
        id: 'rev_graph_v1',
        draft_id: 'draft_coding_pipeline',
        version: 1,
        workspace: '/Users/bigo/agentworkspace/codexworkspace/operant',
        name: '标准编码流水线 v1.0',
        description: '五阶段编码工作流的生产发布。',
        nodes: this.graphDrafts[0].nodes,
        edges: this.graphDrafts[0].edges,
        compiled_ir: { version: '2.0-ir', entry_node: 'node_planner' },
        published_at: new Date(Date.now() - 86400000).toISOString(),
        published_by: 'lead_architect',
      },
    ];

    // 8. Workflow Runs (Active state for RunDetailView)
    this.workflowRuns = [
      {
        id: 'run_operant_001',
        definition_revision_id: 'rev_graph_v1',
        task: '实现 Operant 2.0 TypeScript SDK 与客户端架构及响应式 GUI 壳层',
        workspace: '/Users/bigo/agentworkspace/codexworkspace/operant',
        status: 'running',
        current_stage: 'coder',
        planner_role_id: 'role_planner',
        explorer_role_ids: ['role_planner'],
        coder_role_id: 'role_coder',
        reviewer_role_id: 'role_reviewer',
        main_role_id: 'role_main',
        max_parallel_explorers: 2,
        max_rework_rounds: 2,
        current_rework_round: 0,
        node_runs: [
          {
            id: 'nrun_planner',
            workflow_run_id: 'run_operant_001',
            node_id: 'node_planner',
            label: '架构规划师',
            type: 'agent',
            status: 'succeeded',
            attempts: [
              {
                attempt_number: 1,
                status: 'succeeded',
                started_at: new Date(Date.now() - 600000).toISOString(),
                ended_at: new Date(Date.now() - 520000).toISOString(),
                input_refs: ['task_prompt'],
                output_artifacts: ['plan_operant_2.0.md'],
                usage: { input_tokens: 1420, output_tokens: 680, cost_usd: 0.012 },
              },
            ],
            current_attempt: 1,
            created_at: new Date(Date.now() - 600000).toISOString(),
            updated_at: new Date(Date.now() - 520000).toISOString(),
          },
          {
            id: 'nrun_explorer_1',
            workflow_run_id: 'run_operant_001',
            node_id: 'node_explorer_1',
            label: '代码库探索器（类型）',
            type: 'agent',
            status: 'succeeded',
            attempts: [
              {
                attempt_number: 1,
                status: 'succeeded',
                started_at: new Date(Date.now() - 520000).toISOString(),
                ended_at: new Date(Date.now() - 440000).toISOString(),
                output_artifacts: ['symbol_map.json'],
              },
            ],
            current_attempt: 1,
            created_at: new Date(Date.now() - 520000).toISOString(),
            updated_at: new Date(Date.now() - 440000).toISOString(),
          },
          {
            id: 'nrun_coder',
            workflow_run_id: 'run_operant_001',
            node_id: 'node_coder',
            label: '精准编码员',
            type: 'agent',
            status: 'running',
            attempts: [
              {
                attempt_number: 1,
                status: 'running',
                started_at: new Date(Date.now() - 440000).toISOString(),
                input_refs: ['plan_operant_2.0.md', 'symbol_map.json'],
                usage: { input_tokens: 3800, output_tokens: 1450, cost_usd: 0.038 },
              },
            ],
            current_attempt: 1,
            created_at: new Date(Date.now() - 440000).toISOString(),
            updated_at: now,
          },
        ],
        created_at: new Date(Date.now() - 600000).toISOString(),
        updated_at: now,
      },
    ];

    // 9. Remote Hosts & Devices
    this.remoteHosts = [
      {
        id: 'host_local_01',
        name: '开发者 MacBook（本地主机）',
        core_version: '2.0.0-draft',
        protocol_version: '2.0',
        is_online: true,
        transport_mode: 'direct_lan',
        last_seen: now,
        workspaces: ['/Users/bigo/agentworkspace/codexworkspace/operant'],
        capabilities: ['workspace_write', 'docker_exec', 'git', 'pty_terminal'],
      },
      {
        id: 'host_remote_vm',
        name: 'Debian 构建机（VPN 目标）',
        core_version: '2.0.0-draft',
        protocol_version: '2.0',
        is_online: true,
        transport_mode: 'direct_vpn',
        last_seen: new Date(Date.now() - 15000).toISOString(),
        workspaces: ['/srv/operant/workspace'],
        capabilities: ['workspace_write', 'docker_exec', 'isolated_vm'],
      },
    ];

    this.remoteDevices = [
      {
        device_id: 'dev_iphone_15',
        device_name: '开发者的 iPhone（远程 PWA）',
        device_type: 'mobile_pwa',
        scope: 'interactive_steering',
        paired_at: new Date(Date.now() - 86400000).toISOString(),
        last_active_at: new Date(Date.now() - 120000).toISOString(),
        is_revoked: false,
      },
    ];

    // 10. Memories (SQLite FTS5 simulation)
    this.memories = [
      {
        id: 'mem_001',
        session_id: sessionId,
        kind: 'project',
        content: 'Action Gateway 的 DENY 决定在任何情况下都不能被客户端或 LLM 评审员覆盖。',
        project_scope: '/Users/bigo/agentworkspace/codexworkspace/operant',
        role_scope: ['role_main', 'role_coder'],
        source_task: '安全边界稳定化',
        confidence: 1.0,
        confirmed: true,
        status: 'active',
        created_at: new Date(Date.now() - 172800000).toISOString(),
      },
      {
        id: 'mem_002',
        session_id: sessionId,
        kind: 'working',
        content: 'Vite 别名必须将 @operant/sdk 映射到 ../../sdk，以实现 monorepo TypeScript 无缝解析。',
        project_scope: '/Users/bigo/agentworkspace/codexworkspace/operant',
        role_scope: ['role_coder'],
        confidence: 0.85,
        confirmed: false,
        status: 'candidate',
        created_at: new Date(Date.now() - 3600000).toISOString(),
      },
    ];

    this.evalSuites = [
      {
        id: 'suite_security_boundary',
        name: 'Action Gateway 安全套件',
        description: '验证路径穿越防护、DENY 规则不可变性与租约撤销。',
        created_at: new Date(Date.now() - 86400000).toISOString(),
      },
    ];

    this.evalRuns = [
      {
        id: 'eval_run_01',
        suite_id: 'suite_security_boundary',
        status: 'completed',
        started_at: new Date(Date.now() - 3600000).toISOString(),
        completed_at: new Date(Date.now() - 3540000).toISOString(),
      },
    ];

    this.evalResults = [
      {
        id: 'res_01',
        evaluation_run_id: 'eval_run_01',
        test_name: 'test_path_escape_prevention',
        passed: true,
        score: 1.0,
      },
      {
        id: 'res_02',
        evaluation_run_id: 'eval_run_01',
        test_name: 'test_policy_deny_immutability',
        passed: true,
        score: 1.0,
      },
    ];
  }

  // --- OperantClient API Methods ---

  async checkHealth(): Promise<{ status: string }> {
    return { status: 'mock_active' };
  }

  async listModels(): Promise<ModelProfile[]> {
    return [...this.models];
  }

  async getModel(id: string): Promise<ModelProfile> {
    const m = this.models.find((x) => x.id === id);
    if (!m) throw new Error(`Model profile '${id}' not found.`);
    return { ...m };
  }

  async createModel(profile: Omit<ModelProfile, 'id' | 'created_at'>): Promise<ModelProfile> {
    const created: ModelProfile = {
      ...profile,
      id: `model_${Date.now()}`,
      created_at: new Date().toISOString(),
    };
    this.models.push(created);
    return created;
  }

  async updateModel(id: string, updates: Partial<ModelProfile>): Promise<ModelProfile> {
    const index = this.models.findIndex((x) => x.id === id);
    if (index === -1) throw new Error(`Model profile '${id}' not found.`);
    this.models[index] = { ...this.models[index], ...updates };
    return this.models[index];
  }

  async deactivateModel(id: string): Promise<ModelProfile> {
    return this.updateModel(id, { enabled: false });
  }

  async discoverModels(baseUrl: string, _secretRef?: string): Promise<{ model_ids: string[] }> {
    const url = (baseUrl || '').toLowerCase();
    if (url.includes('deepseek.com')) {
      return { model_ids: ['deepseek-chat', 'deepseek-reasoner', 'deepseek-v3', 'deepseek-r1-2025'] };
    }
    if (url.includes('anthropic.com')) {
      return { model_ids: ['claude-3-7-sonnet-20250219', 'claude-3-5-sonnet-20241022', 'claude-3-5-haiku-20241022', 'claude-3-opus-20240229'] };
    }
    if (url.includes('openai.com')) {
      return { model_ids: ['gpt-4o', 'gpt-4o-mini', 'o1', 'o3-mini', 'gpt-4.5-preview'] };
    }
    if (url.includes('openrouter.ai')) {
      return { model_ids: ['anthropic/claude-3.7-sonnet', 'openai/gpt-4o', 'deepseek/deepseek-r1', 'meta-llama/llama-3.3-70b-instruct', 'google/gemini-2.0-flash-001'] };
    }
    if (url.includes('googleapis.com') || url.includes('google') || url.includes('gemini')) {
      return { model_ids: ['gemini-2.0-flash-exp', 'gemini-2.0-flash-thinking-exp', 'gemini-1.5-pro-latest', 'gemini-1.5-flash-latest'] };
    }
    if (url.includes('github.ai') || url.includes('github.com') || url.includes('azure')) {
      return { model_ids: ['gpt-4o', 'claude-3-5-sonnet', 'deepseek-r1', 'meta-llama-3.1-405b-instruct', 'o1-preview'] };
    }
    if (url.includes('siliconflow.cn')) {
      return { model_ids: ['deepseek-ai/DeepSeek-V3', 'deepseek-ai/DeepSeek-R1', 'Qwen/Qwen2.5-Coder-32B-Instruct', 'Pro/deepseek-ai/DeepSeek-V3'] };
    }
    if (url.includes('fireworks.ai')) {
      return { model_ids: ['accounts/fireworks/models/deepseek-v3', 'accounts/fireworks/models/deepseek-r1', 'accounts/fireworks/models/llama-v3p3-70b-instruct', 'accounts/fireworks/models/qwen2p5-coder-32b-instruct'] };
    }
    if (url.includes('x.ai')) {
      return { model_ids: ['grok-2-1212', 'grok-2-vision-1212', 'grok-beta'] };
    }
    if (url.includes('minimax.chat')) {
      return { model_ids: ['MiniMax-Text-01', 'abab6.5s-chat', 'abab6.5t-chat'] };
    }
    if (url.includes('bigmodel.cn')) {
      return { model_ids: ['glm-4-plus', 'glm-4-air', 'glm-4-flash', 'glm-zero-preview'] };
    }
    if (url.includes('moonshot.cn')) {
      return { model_ids: ['moonshot-v1-8k', 'moonshot-v1-32k', 'moonshot-v1-128k', 'kimi-latest'] };
    }
    if (url.includes('opencode.ai')) {
      return { model_ids: ['opencode-zen-1', 'opencode-go-preview'] };
    }
    if (url.includes('11434') || url.includes('ollama')) {
      return { model_ids: ['qwen2.5-coder:32b', 'llama3.3:70b', 'deepseek-r1:32b', 'deepseek-r1:14b', 'mistral-nemo:12b'] };
    }
    if (url.includes('8000') || url.includes('vllm')) {
      return { model_ids: ['Qwen/Qwen2.5-Coder-32B-Instruct', 'deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct'] };
    }
    if (url.includes('1234') || url.includes('lmstudio')) {
      return { model_ids: ['qwen2.5-coder-32b-instruct@4bit', 'deepseek-r1-distill-qwen-32b'] };
    }
    if (url.includes('8080') || url.includes('llama')) {
      return { model_ids: ['llama-3.3-70b-instruct-q4_k_m', 'qwen2.5-coder-32b-q4_k_m'] };
    }
    return {
      model_ids: [
        'deepseek-chat',
        'deepseek-reasoner',
        'claude-3-7-sonnet-20250219',
        'gpt-4o',
        'qwen2.5-coder-32b-instruct',
      ],
    };
  }

  async checkModelHealth(_id: string): Promise<{ healthy: boolean; latency_ms?: number }> {
    return { healthy: true, latency_ms: Math.floor(Math.random() * 80 + 35) };
  }

  // --- Roles ---
  async listRoles(includeInactive = false): Promise<RolePreset[]> {
    return this.roles.filter((r) => includeInactive || r.status === 'active');
  }

  async getRole(id: string, _version?: number): Promise<RolePreset> {
    const r = this.roles.find((x) => x.id === id);
    if (!r) throw new Error(`Role '${id}' not found.`);
    return { ...r };
  }

  async createRole(role: Omit<RolePreset, 'id' | 'created_at' | 'version'>): Promise<RolePreset> {
    const created: RolePreset = {
      ...role,
      id: `role_${Date.now()}`,
      version: 1,
      created_at: new Date().toISOString(),
    };
    this.roles.push(created);
    return created;
  }

  async updateRole(id: string, updates: Partial<RolePreset>): Promise<RolePreset> {
    const index = this.roles.findIndex((x) => x.id === id);
    if (index === -1) throw new Error(`Role '${id}' not found.`);
    this.roles[index] = { ...this.roles[index], ...updates };
    return this.roles[index];
  }

  async copyRole(id: string, newName: string): Promise<RolePreset> {
    const src = await this.getRole(id);
    const copied: RolePreset = {
      ...src,
      id: `role_${Date.now()}`,
      name: newName,
      version: 1,
      created_at: new Date().toISOString(),
    };
    this.roles.push(copied);
    return copied;
  }

  async deactivateRole(id: string): Promise<RolePreset> {
    return this.updateRole(id, { status: 'inactive' });
  }

  async seedDefaultRoles(): Promise<RolePreset[]> {
    return this.listRoles();
  }

  // --- Sessions & Threads ---
  async listSessions(): Promise<Session[]> {
    return [...this.sessions];
  }

  async getSession(id: string): Promise<Session> {
    const s = this.sessions.find((x) => x.id === id);
    if (!s) throw new Error(`Session '${id}' not found.`);
    return { ...s };
  }

  async createSession(options: {
    roleId?: string;
    newRole?: Partial<RolePreset>;
    modelProfileId?: string;
    effort?: string;
    budgetOverrides?: Record<string, unknown>;
  }): Promise<Session> {
    const roleId = options.roleId || 'role_main';
    const role = await this.getRole(roleId);
    const modelProfile = await this.getModel(options.modelProfileId || role.model_profile_id);

    const sessionId = `session_${Date.now()}`;
    const threadId = `thread_${Date.now()}`;
    const now = new Date().toISOString();

    const session: Session = {
      id: sessionId,
      role_snapshot: {
        role_id: role.id,
        role_version: role.version,
        role_name: role.name,
        system_prompt: role.system_prompt,
        model_profile_id: modelProfile.id,
        model_profile_name: modelProfile.name,
        provider: modelProfile.provider,
        model_id: modelProfile.model_id,
        base_url: modelProfile.base_url,
        secret_ref: modelProfile.secret_ref,
        effort: (options.effort as any) || role.effort,
        tool_policy: role.tool_policy,
        budget: role.budget,
        memory_scope: role.memory_scope,
        captured_at: now,
        overrides: {
          effort_overridden: !!options.effort,
          model_profile_overridden: !!options.modelProfileId,
          budget_fields: [],
        },
      },
      created_at: now,
    };

    const thread: Thread = {
      id: threadId,
      workspace: '/Users/bigo/agentworkspace/codexworkspace/operant',
      title: `${role.name}会话`,
      session_id: sessionId,
      root_agent_id: `agent_${sessionId}`,
      status: 'active',
      created_at: now,
      updated_at: now,
    };

    this.sessions.unshift(session);
    this.threads.unshift(thread);
    this.messages.set(threadId, []);
    this.contextRevisions.set(threadId, {
      revision_id: `rev_${Date.now()}`,
      thread_id: threadId,
      total_tokens: 1470,
      context_window_limit: modelProfile.context_window || 128000,
      components: [
        {
          id: `sys_${Date.now()}`,
          type: 'system_policy',
          label: '系统与动作策略',
          estimated_tokens: 820,
          is_pinned: true,
          is_removable: false,
          is_compacted: false,
          updated_at: now,
        },
        {
          id: `role_${Date.now()}`,
          type: 'role_snapshot',
          label: `角色快照（${role.name}）`,
          estimated_tokens: 650,
          is_pinned: true,
          is_removable: false,
          is_compacted: false,
          updated_at: now,
        },
      ],
      created_at: now,
    });

    return session;
  }

  async listThreads(_workspace?: string): Promise<Thread[]> {
    return [...this.threads];
  }

  async getThread(threadId: string): Promise<Thread> {
    const t = this.threads.find((x) => x.id === threadId);
    if (!t) throw new Error(`Thread '${threadId}' not found.`);
    return { ...t };
  }

  async listThreadMessages(threadId: string): Promise<CanonicalAgentMessage[]> {
    return [...(this.messages.get(threadId) || [])];
  }

  async getContextRevision(threadId: string): Promise<ContextRevision> {
    const rev = this.contextRevisions.get(threadId);
    if (!rev) throw new Error(`Context revision for thread '${threadId}' not found.`);
    return { ...rev };
  }

  async compactContext(threadId: string, reason = 'manual_user_compaction'): Promise<ContextRevision> {
    const existing = await this.getContextRevision(threadId);
    const compactedTotal = Math.floor(existing.total_tokens * 0.65);
    const updated: ContextRevision = {
      ...existing,
      revision_id: `rev_compact_${Date.now()}`,
      total_tokens: compactedTotal,
      compaction_summary: {
        reason: reason as any,
        original_token_count: existing.total_tokens,
        compacted_token_count: compactedTotal,
        created_at: new Date().toISOString(),
      },
    };
    this.contextRevisions.set(threadId, updated);
    return updated;
  }

  runSessionStream(
    sessionId: string,
    message: string,
    workspace: string,
    onEvent: EventSubscriber
  ): EventUnsubscribe {
    let isCancelled = false;
    const thread = this.threads.find((t) => t.session_id === sessionId) || this.threads[0];
    const threadId = thread.id;

    // 1. Add User Message
    const userMsg: CanonicalAgentMessage = {
      id: `msg_u_${Date.now()}`,
      thread_id: threadId,
      sender: { type: 'user', id: 'user_active', name: '开发者' },
      role: 'user',
      content: message,
      visibility: { deliver_to: ['*'], ui_visible_to: ['*'], audit_visible: true, context_injection: 'immediate' },
      created_at: new Date().toISOString(),
    };
    const threadMsgs = this.messages.get(threadId) || [];
    threadMsgs.push(userMsg);
    this.messages.set(threadId, threadMsgs);

    // 2. Stream Simulated Multi-step Agent Response
    const agentMsgId = `msg_a_${Date.now()}`;
    const agentMsg: CanonicalAgentMessage = {
      id: agentMsgId,
      thread_id: threadId,
      sender: { type: 'agent', id: `agent_${sessionId}`, name: 'Operant 代理', role_name: '精准编码员' },
      role: 'agent',
      content: '',
      structured_summary: {
        goal: `处理：${message.slice(0, 45)}...`,
        current_phase: '分析并生成方案',
        action_summary: [],
        evidence_refs: [],
        artifact_refs: [],
        conclusions: [],
        uncertainties: [],
      },
      tool_calls: [],
      visibility: { deliver_to: ['*'], ui_visible_to: ['*'], audit_visible: true, context_injection: 'immediate' },
      created_at: new Date().toISOString(),
    };
    threadMsgs.push(agentMsg);

    const steps: Array<{ delta: string; delay: number; event: string; payload: any }> = [
      {
        delta: '正在分析请求需求与项目文件结构…',
        delay: 200,
        event: 'agent.started',
        payload: { role_name: '精准编码员', model_profile_name: 'Claude 3.7 Sonnet', workspace },
      },
      {
        delta: '\n正在读取工作区依赖与配置…',
        delay: 500,
        event: 'tool.started',
        payload: { tool_call_id: `tc_${Date.now()}`, tool_name: 'search_files', arguments: { query: 'package.json' } },
      },
      {
        delta: '\n\n我已生成所需实现，准备应用补丁。',
        delay: 800,
        event: 'tool.completed',
        payload: {
          tool_call_id: `tc_${Date.now()}`,
          tool_name: 'read_file',
          output: '已找到 package.json 配置文件。',
          execution_time_ms: 45,
          runner_used: 'host' as const,
        },
      },
      {
        delta: '\n已完成文件修改并校验类型。',
        delay: 1100,
        event: 'agent.completed',
        payload: { final_output: '任务已成功完成。', total_turns: 2 },
      },
    ];

    let currentStep = 0;
    const runNextStep = () => {
      if (isCancelled || currentStep >= steps.length) return;
      const step = steps[currentStep];
      agentMsg.content += step.delta;

      const evt: AnyOperantEvent = {
        id: `evt_${Date.now()}_${currentStep}`,
        sequence: Date.now(),
        event_type: step.event as any,
        schema_version: '2.0',
        session_id: sessionId,
        thread_id: threadId,
        occurred_at: new Date().toISOString(),
        payload: step.payload,
      };

      onEvent(evt);
      this.notifySubscribers(evt);

      currentStep++;
      if (currentStep < steps.length) {
        setTimeout(runNextStep, step.delay);
      }
    };

    setTimeout(runNextStep, 200);

    return () => {
      isCancelled = true;
    };
  }

  async cancelSession(_sessionId: string): Promise<{ accepted: boolean }> {
    return { accepted: true };
  }

  // --- Approvals ---
  async listPendingApprovals(_sessionId?: string): Promise<ApprovalCard[]> {
    return this.approvals.filter((a) => a.status === 'pending');
  }

  async submitApproval(
    sessionId: string,
    approvalId: string,
    decision: ApprovalDecision
  ): Promise<{ accepted: boolean }> {
    const appr = this.approvals.find((a) => a.id === approvalId);
    if (!appr) throw new Error(`Approval '${approvalId}' not found.`);

    if (appr.matched_policy_rule.decision === 'DENY') {
      throw new Error('Action Gateway DENY rules cannot be approved or overridden.');
    }

    appr.status = decision.decision === 'reject' ? 'rejected' : 'approved_once';

    const event: AnyOperantEvent = {
      id: `evt_appr_${Date.now()}`,
      sequence: Date.now(),
      event_type: 'approval.decided',
      schema_version: '2.0',
      session_id: sessionId,
      occurred_at: new Date().toISOString(),
      payload: {
        approval_id: approvalId,
        decision,
      },
    };
    this.notifySubscribers(event);

    return { accepted: true };
  }

  // --- Graph Drafts ---
  async listGraphDrafts(_workspace?: string): Promise<GraphDraft[]> {
    return [...this.graphDrafts];
  }

  async getGraphDraft(draftId: string): Promise<GraphDraft> {
    const draft = this.graphDrafts.find((d) => d.id === draftId);
    if (!draft) throw new Error(`Graph draft '${draftId}' not found.`);
    return { ...draft };
  }

  async saveGraphDraft(draft: Partial<GraphDraft> & { workspace: string; name: string }): Promise<GraphDraft> {
    const existingIdx = this.graphDrafts.findIndex((d) => d.id === draft.id);
    const updated: GraphDraft = {
      id: draft.id || `draft_${Date.now()}`,
      workspace: draft.workspace,
      name: draft.name,
      description: draft.description,
      nodes: draft.nodes || [],
      edges: draft.edges || [],
      updated_at: new Date().toISOString(),
      is_valid: true,
      diagnostics: [],
    };
    if (existingIdx >= 0) {
      this.graphDrafts[existingIdx] = updated;
    } else {
      this.graphDrafts.push(updated);
    }
    return updated;
  }

  async compileGraphDraft(draftId: string): Promise<{ is_valid: boolean; diagnostics: GraphCompilerDiagnostic[] }> {
    const draft = await this.getGraphDraft(draftId);
    const diagnostics: GraphCompilerDiagnostic[] = [];

    // Diagnostic validation checks
    if (draft.nodes.length === 0) {
      diagnostics.push({
        level: 'error',
        message: '图定义必须至少包含一个入口节点。',
        rule_code: 'GRAPH_EMPTY',
      });
    }

    draft.nodes.forEach((node) => {
      if (node.type === 'agent' && !node.role_id) {
        diagnostics.push({
          level: 'error',
          node_id: node.id,
          message: `代理节点“${node.label}”必须指定有效的角色分配。`,
          rule_code: 'NODE_ROLE_MISSING',
        });
      }
    });

    const isValid = diagnostics.every((d) => d.level !== 'error');
    draft.is_valid = isValid;
    draft.diagnostics = diagnostics;
    return { is_valid: isValid, diagnostics };
  }

  async publishGraphDraft(draftId: string, description?: string): Promise<GraphDefinitionRevision> {
    const draft = await this.getGraphDraft(draftId);
    const compResult = await this.compileGraphDraft(draftId);
    if (!compResult.is_valid) {
      throw new Error('无法发布无效的图草稿，请先修复编译器诊断问题。');
    }

    const revision: GraphDefinitionRevision = {
      id: `rev_graph_${Date.now()}`,
      draft_id: draftId,
      version: this.graphRevisions.length + 1,
      workspace: draft.workspace,
      name: draft.name,
      description: description || draft.description,
      nodes: draft.nodes,
      edges: draft.edges,
      compiled_ir: { version: '2.0-ir', entry_node: draft.nodes[0]?.id },
      published_at: new Date().toISOString(),
      published_by: 'current_user',
    };
    this.graphRevisions.unshift(revision);
    return revision;
  }

  async listGraphRevisions(_workspace?: string): Promise<GraphDefinitionRevision[]> {
    return [...this.graphRevisions];
  }

  // --- Workflow Runs ---
  async listWorkflowRuns(_workspace?: string): Promise<WorkflowRun[]> {
    return [...this.workflowRuns];
  }

  async getWorkflowRun(runId: string): Promise<WorkflowRun> {
    const run = this.workflowRuns.find((r) => r.id === runId);
    if (!run) throw new Error(`Workflow Run '${runId}' not found.`);
    return { ...run };
  }

  async startWorkflowRun(options: {
    task: string;
    workspace: string;
    mainRoleId?: string;
    plannerRoleId?: string;
    explorerRoleIds?: string[];
    coderRoleId?: string;
    reviewerRoleId?: string;
    maxParallelExplorers?: number;
    maxReworkRounds?: number;
  }, onEvent?: EventSubscriber): Promise<WorkflowRun> {
    const runId = `run_workflow_${Date.now()}`;
    const now = new Date().toISOString();

    const newRun: WorkflowRun = {
      id: runId,
      task: options.task,
      workspace: options.workspace,
      status: 'running',
      current_stage: 'planner',
      planner_role_id: options.plannerRoleId || 'role_planner',
      explorer_role_ids: options.explorerRoleIds || ['role_planner'],
      coder_role_id: options.coderRoleId || 'role_coder',
      reviewer_role_id: options.reviewerRoleId || 'role_reviewer',
      main_role_id: options.mainRoleId || 'role_main',
      max_parallel_explorers: options.maxParallelExplorers || 2,
      max_rework_rounds: options.maxReworkRounds || 1,
      current_rework_round: 0,
      node_runs: [],
      created_at: now,
      updated_at: now,
    };

    this.workflowRuns.unshift(newRun);

    if (onEvent) {
      const evt: AnyOperantEvent = {
        id: `evt_${Date.now()}`,
        sequence: 1,
        event_type: 'workflow.stage_started',
        schema_version: '2.0',
        workflow_run_id: runId,
        occurred_at: now,
        payload: { stage: 'planner', assigned_role_id: options.plannerRoleId || 'role_planner' },
      };
      onEvent(evt);
      this.notifySubscribers(evt);
    }

    return newRun;
  }

  async resumeWorkflowRun(runId: string, _allowCoderReplay: boolean, _onEvent?: EventSubscriber): Promise<void> {
    const run = await this.getWorkflowRun(runId);
    run.status = 'running';
    run.updated_at = new Date().toISOString();
  }

  async cancelWorkflowRun(runId: string): Promise<{ accepted: boolean }> {
    const run = await this.getWorkflowRun(runId);
    run.status = 'cancelled';
    run.updated_at = new Date().toISOString();
    return { accepted: true };
  }

  async getWorkflowTrace(runId: string): Promise<Record<string, unknown>> {
    return {
      workflow_run_id: runId,
      trace_events_count: 42,
      critical_path_duration_ms: 12400,
      rework_rounds_count: 0,
    };
  }

  exportWorkflowTraceUrl(runId: string): string {
    return `http://127.0.0.1:8000/v1/tasks/${runId}/trace.jsonl`;
  }

  // --- Remote Control ---
  async listRemoteHosts(): Promise<RemoteHost[]> {
    return [...this.remoteHosts];
  }

  async listRemoteDevices(_hostId?: string): Promise<RemoteDevice[]> {
    return [...this.remoteDevices];
  }

  async requestDevicePairing(
    hostId: string,
    deviceName: string,
    scope: string,
    pin: string
  ): Promise<{ request_id: string; qr_code_payload: string }> {
    const newDev: RemoteDevice = {
      device_id: `dev_${Date.now()}`,
      device_name: deviceName,
      device_type: 'mobile_pwa',
      scope: scope as any,
      paired_at: new Date().toISOString(),
      last_active_at: new Date().toISOString(),
      is_revoked: false,
    };
    this.remoteDevices.push(newDev);
    return {
      request_id: `req_${Date.now()}`,
      qr_code_payload: `operant://pair?host=${hostId}&pin=${pin}&name=${encodeURIComponent(deviceName)}`,
    };
  }

  async revokeRemoteDevice(deviceId: string): Promise<{ revoked: boolean }> {
    const dev = this.remoteDevices.find((d) => d.device_id === deviceId);
    if (dev) {
      dev.is_revoked = true;
    }
    return { revoked: true };
  }

  async sendRemoteCommand(hostId: string, _sessionId: string, _message: string): Promise<CommandReceipt> {
    return {
      command_id: `cmd_${Date.now()}`,
      request_id: `req_${Date.now()}`,
      idempotency_key: `idemp_${Date.now()}`,
      host_id: hostId,
      relay_acknowledged: true,
      host_acknowledged: true,
      host_accepted: true,
      received_at: new Date().toISOString(),
    };
  }

  // --- Memory Governance ---
  async searchMemories(
    _sessionId: string,
    query: string,
    _projectScope?: string,
    includeCandidates = false
  ): Promise<Memory[]> {
    const lower = query.toLowerCase();
    return this.memories.filter((m) => {
      if (!includeCandidates && !m.confirmed) return false;
      return m.content.toLowerCase().includes(lower);
    });
  }

  async createMemory(options: {
    sessionId: string;
    kind: 'working' | 'episodic' | 'project';
    content: string;
    projectScope?: string;
    roleScope?: string[];
    sourceTask?: string;
    confidence?: number;
    confirmed?: boolean;
  }): Promise<Memory> {
    const mem: Memory = {
      id: `mem_${Date.now()}`,
      session_id: options.sessionId,
      kind: options.kind,
      content: options.content,
      project_scope: options.projectScope,
      role_scope: options.roleScope || [],
      source_task: options.sourceTask,
      confidence: options.confidence ?? 0.8,
      confirmed: options.confirmed ?? false,
      status: (options.confirmed ? 'active' : 'candidate') as any,
      created_at: new Date().toISOString(),
    };
    this.memories.unshift(mem);
    return mem;
  }

  async confirmMemory(_sessionId: string, memoryId: string, _projectScope?: string): Promise<Memory> {
    const mem = this.memories.find((m) => m.id === memoryId);
    if (!mem) throw new Error(`Memory '${memoryId}' not found.`);
    mem.confirmed = true;
    mem.status = 'active';
    return { ...mem };
  }

  async deactivateMemory(_sessionId: string, memoryId: string, _projectScope?: string): Promise<Memory> {
    const mem = this.memories.find((m) => m.id === memoryId);
    if (!mem) throw new Error(`Memory '${memoryId}' not found.`);
    mem.status = 'retired';
    return { ...mem };
  }

  // --- Evaluation ---
  async listEvaluationSuites(): Promise<EvaluationSuite[]> {
    return [...this.evalSuites];
  }

  async listEvaluationRuns(suiteId?: string): Promise<EvaluationRun[]> {
    return this.evalRuns.filter((r) => !suiteId || r.suite_id === suiteId);
  }

  async listEvaluationResults(runId: string): Promise<EvaluationResult[]> {
    return this.evalResults.filter((r) => r.evaluation_run_id === runId);
  }

  // --- Global Event Subscription ---
  subscribeEvents(_cursor?: EventCursor, subscriber?: EventSubscriber): EventUnsubscribe {
    if (subscriber) {
      this.globalSubscribers.add(subscriber);
    }
    return () => {
      if (subscriber) {
        this.globalSubscribers.delete(subscriber);
      }
    };
  }

  private notifySubscribers(event: AnyOperantEvent) {
    this.globalSubscribers.forEach((sub) => {
      try {
        sub(event);
      } catch (err) {
        console.error('Subscriber error:', err);
      }
    });
  }
}
