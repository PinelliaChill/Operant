import { Navigate, createHashRouter } from 'react-router-dom';
import { RailLayout } from './RailLayout';
import { ChatView } from '../features/chat/ChatView';
import { TasksView } from '../features/tasks/TasksView';
import { CollabView } from '../features/collab/CollabView';
import { CollabWorkflowCanvas } from '../features/collab/CollabWorkflowCanvas';
import { RunDetailView } from '../features/runs/RunDetailView';
import { SchedulesView } from '../features/schedules/SchedulesView';
import { ProjectsView } from '../features/projects/ProjectsView';
import { AgentsView } from '../features/agents/AgentsView';
import { ExtensionsView } from '../features/extensions/ExtensionsView';
import { SkillsView } from '../features/skills/SkillsView';
import { SettingsView } from '../features/settings/SettingsView';
import { ApprovalCenterView } from '../features/approvals/ApprovalCenterView';
import { WorkflowSessionView } from '../features/workflowdir/WorkflowSessionView';
import { AgentFocusView } from '../features/workflowdir/AgentFocusView';

export const router = createHashRouter([
  {
    path: '/',
    element: <RailLayout />,
    children: [
      {
        index: true,
        element: <Navigate to="/chat" replace />,
      },
      {
        path: 'chat',
        element: <ChatView />,
      },
      {
        path: 'chat/:conversationId',
        element: <ChatView />,
      },
      {
        path: 'tasks',
        element: <TasksView />,
      },
      {
        // 协作模式：总览 / 编排画布 / 运行进度（?view=home|canvas|runs）
        path: 'collab',
        element: <CollabView />,
      },
      {
        // 协作总览卡片 → 工作流只读画布（v5 §2.2）
        path: 'collab/:wfId/canvas',
        element: <CollabWorkflowCanvas />,
      },
      {
        // v5 §2.1：工作流分区被协作总览吸收，旧入口重定向
        path: 'workflow',
        element: <Navigate to="/collab" replace />,
      },
      {
        // 旧工作流界面深链 → 协作总览
        path: 'workflow/:id',
        element: <Navigate to="/collab" replace />,
      },
      {
        // 保留深链：工作流群聊界面（寻址 chip + "只看发给我的"过滤）
        path: 'workflow/:id/s/:sid',
        element: <WorkflowSessionView />,
      },
      {
        // 保留深链：Agent 个人界面（过程明细条目流 + 单独 DM）
        path: 'workflow/:id/s/:sid/agent/:aid',
        element: <AgentFocusView />,
      },
      {
        path: 'runs/:id',
        element: <RunDetailView />,
      },
      {
        path: 'schedules',
        element: <SchedulesView />,
      },
      {
        path: 'projects',
        element: <ProjectsView />,
      },
      {
        path: 'projects/:projectId',
        element: <ProjectsView />,
      },
      {
        path: 'agents',
        element: <AgentsView />,
      },
      {
        path: 'extensions',
        element: <ExtensionsView />,
      },
      {
        path: 'skills',
        element: <SkillsView />,
      },
      {
        path: 'settings',
        element: <SettingsView />,
      },
      {
        // 旧路由重定向：协作叙事并入会话分区
        path: 'session',
        element: <Navigate to="/chat" replace />,
      },
      {
        // 审批与权限中心（v5 §3：rail 一级分区，策略管理 + 待处理列表）
        path: 'approvals',
        element: <ApprovalCenterView />,
      },
      {
        path: 'remote',
        element: <Navigate to="/settings?tab=remote" replace />,
      },
      {
        path: '*',
        element: <Navigate to="/chat" replace />,
      },
    ],
  },
]);
