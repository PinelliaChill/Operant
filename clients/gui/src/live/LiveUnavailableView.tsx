import React from 'react';
import { LockKeyhole, Radio } from 'lucide-react';
import { useOperant } from '../context/ClientContext';

const SECTION_LABELS: Record<string, string> = {
  collab: '协作工作台',
  tasks: '任务',
  schedules: '调度',
  agents: 'Agent',
  extensions: '插件与 MCP',
  skills: '技能',
  settings: '设置',
  runs: '运行详情',
  workflow: '工作流深链',
};

/** Explicit live boundary for demo-only routes.  It never renders Demo data. */
export const LiveUnavailableView: React.FC<{ section: string }> = ({ section }) => {
  const { setClientMode } = useOperant();
  const label = SECTION_LABELS[section] ?? section;
  return (
    <div className="live-route-state live-unavailable-state" role="note">
      <div className="live-route-state-icon"><LockKeyhole size={22} aria-hidden="true" /></div>
      <h1>{label}：Live 本阶段未接入</h1>
      <p>此功能当前仅有演示界面。Live 模式不会静默显示或混入演示数据。</p>
      <div className="live-unavailable-boundary"><Radio size={14} aria-hidden="true" /><span>已接入：Core、Workspace/Project、Thread、Session/Run、SSE、Approval、类型化错误</span></div>
      <button type="button" className="btn btn-secondary" onClick={() => setClientMode('mock')}>切换到演示模式查看</button>
    </div>
  );
};
