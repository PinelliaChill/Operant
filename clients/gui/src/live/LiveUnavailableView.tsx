import React from 'react';
import { LockKeyhole } from 'lucide-react';
import { useOperant } from '../context/ClientContext';
import type { LiveUnavailableSection } from './liveRouteSupport';

export type { LiveUnavailableSection };

const SECTION_LABELS: Record<string, string> = {
  collab: '协作工作台',
  collab_canvas: '工作流画布预览',
  tasks: '任务',
  schedules: '调度',
  agents: '模型与角色',
  extensions: '插件与 MCP',
  skills: '技能',
  settings: '设置',
  runs: '运行详情',
  workflow: '工作流',
  session: '会话',
};

/** Explicit live boundary for demo-only routes.  It never renders Demo data. */
export const LiveUnavailableView: React.FC<{ section: string }> = ({ section }) => {
  const { setClientMode } = useOperant();
  const label = SECTION_LABELS[section] ?? section;
  return (
    <div className="live-route-state live-unavailable-state" role="note">
      <div className="live-route-state-icon"><LockKeyhole size={22} aria-hidden="true" /></div>
      <h1>{label}：暂不可用</h1>
      <p>此功能尚未接入当前连接。</p>
      <button type="button" className="btn btn-secondary" onClick={() => setClientMode('mock')}>切换到演示模式查看</button>
    </div>
  );
};
