import React, { Suspense, lazy } from 'react';
import { Navigate, createHashRouter } from 'react-router-dom';
import { RailLayout } from './RailLayout';

const ChatView = lazy(() => import('../features/chat/ChatView').then((module) => ({ default: module.ChatView })));
const TasksView = lazy(() => import('../features/tasks/TasksView').then((module) => ({ default: module.TasksView })));
const CollabView = lazy(() => import('../features/collab/CollabView').then((module) => ({ default: module.CollabView })));
const CollabWorkflowCanvas = lazy(() => import('../features/collab/CollabWorkflowCanvas').then((module) => ({ default: module.CollabWorkflowCanvas })));
const RunDetailView = lazy(() => import('../features/runs/RunDetailView').then((module) => ({ default: module.RunDetailView })));
const SchedulesView = lazy(() => import('../features/schedules/SchedulesView').then((module) => ({ default: module.SchedulesView })));
const ProjectsView = lazy(() => import('../features/projects/ProjectsView').then((module) => ({ default: module.ProjectsView })));
const AgentsView = lazy(() => import('../features/agents/AgentsView').then((module) => ({ default: module.AgentsView })));
const ExtensionsView = lazy(() => import('../features/extensions/ExtensionsView').then((module) => ({ default: module.ExtensionsView })));
const SkillsView = lazy(() => import('../features/skills/SkillsView').then((module) => ({ default: module.SkillsView })));
const SettingsView = lazy(() => import('../features/settings/SettingsView').then((module) => ({ default: module.SettingsView })));
const ApprovalCenterView = lazy(() => import('../features/approvals/ApprovalCenterView').then((module) => ({ default: module.ApprovalCenterView })));
const WorkflowSessionView = lazy(() => import('../features/workflowdir/WorkflowSessionView').then((module) => ({ default: module.WorkflowSessionView })));
const AgentFocusView = lazy(() => import('../features/workflowdir/AgentFocusView').then((module) => ({ default: module.AgentFocusView })));

const route = (element: React.ReactNode) => (
  <Suspense fallback={<div className="route-loading" role="status">正在加载界面…</div>}>
    {element}
  </Suspense>
);

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
        element: route(<ChatView />),
      },
      {
        path: 'chat/:conversationId',
        element: route(<ChatView />),
      },
      {
        path: 'tasks',
        element: route(<TasksView />),
      },
      {
        // 协作模式：总览 / 编排画布 / 运行进度（?view=home|canvas|runs）
        path: 'collab',
        element: route(<CollabView />),
      },
      {
        // 协作总览卡片 → 工作流只读画布（v5 §2.2）
        path: 'collab/:wfId/canvas',
        element: route(<CollabWorkflowCanvas />),
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
        element: route(<WorkflowSessionView />),
      },
      {
        // 保留深链：Agent 个人界面（过程明细条目流 + 单独 DM）
        path: 'workflow/:id/s/:sid/agent/:aid',
        element: route(<AgentFocusView />),
      },
      {
        path: 'runs/:id',
        element: route(<RunDetailView />),
      },
      {
        path: 'schedules',
        element: route(<SchedulesView />),
      },
      {
        path: 'projects',
        element: route(<ProjectsView />),
      },
      {
        path: 'projects/:projectId',
        element: route(<ProjectsView />),
      },
      {
        path: 'agents',
        element: route(<AgentsView />),
      },
      {
        path: 'extensions',
        element: route(<ExtensionsView />),
      },
      {
        path: 'skills',
        element: route(<SkillsView />),
      },
      {
        path: 'settings',
        element: route(<SettingsView />),
      },
      {
        // 旧路由重定向：协作叙事并入会话分区
        path: 'session',
        element: <Navigate to="/chat" replace />,
      },
      {
        // 审批与权限中心（v5 §3：rail 一级分区，策略管理 + 待处理列表）
        path: 'approvals',
        element: route(<ApprovalCenterView />),
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
