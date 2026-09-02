/**
 * 运行进度面板（v6 包C：由「运行监控」更名）：自原 WorkflowView 的 monitor tab 迁入
 * （协作模式 chips 之一）。展示工作流图执行记录与当前阶段，可跳转 /runs/:id 流水线详情。
 */

import React from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowRight, Workflow } from 'lucide-react';
import type { WorkflowRun } from '@operant/sdk';
import { StatusBadge } from '../../components/StatusBadge';
import { EmptyState } from '../../components/EmptyState';
import { formatStage } from '../../lib/format';

interface MonitorPanelProps {
  runs: WorkflowRun[];
}

export const MonitorPanel: React.FC<MonitorPanelProps> = ({ runs }) => {
  const navigate = useNavigate();

  return (
    <div style={{ padding: 24, overflowY: 'auto', flex: 1 }}>
      <h3 style={{ fontSize: '15px', fontWeight: 600, color: 'var(--text-primary)', marginBottom: 12 }}>
        运行进度（{runs.length}）
      </h3>
      {runs.length === 0 ? (
        <div style={{ minHeight: 280, display: 'flex', flexDirection: 'column', justifyContent: 'center' }}>
          <EmptyState
            icon={Workflow}
            title="暂无运行进度"
            description="启动一个工作流运行后，这里会显示执行记录与当前阶段。"
          />
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {runs.map((run) => (
            <div
              key={run.id}
              className="card"
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                gap: 12,
                flexWrap: 'wrap',
              }}
            >
              <div style={{ minWidth: 0 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                  <span
                    style={{ fontSize: '11px', fontFamily: 'var(--font-mono)', color: 'var(--text-muted)' }}
                  >
                    {run.id}
                  </span>
                  <StatusBadge status={run.status} size="sm" />
                </div>
                <div style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)' }}>
                  {run.task}
                </div>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                <span style={{ fontSize: '12px', color: 'var(--text-muted)' }}>
                  阶段：{formatStage(run.current_stage)}
                </span>
                <button onClick={() => navigate(`/runs/${run.id}`)} className="btn btn-secondary btn-sm">
                  <span>查看流水线详情</span>
                  <ArrowRight size={12} />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};
