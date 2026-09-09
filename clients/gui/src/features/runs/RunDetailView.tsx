import React, { useEffect, useState } from 'react';
import {
  RotateCcw,
  Square,
  CheckCircle2,
  Download,
  RefreshCw,
} from 'lucide-react';
import { useOperant } from '../../context/ClientContext';
import { useDemo } from '../../demo/DemoContext';
import { WorkflowRun, WorkflowStage } from '@operant/sdk';
import { StatusBadge, STATUS_LABELS } from '../../components/StatusBadge';
import { Modal } from '../../components/Modal';
import { LoadingSkeleton } from '../../components/LoadingSkeleton';
import { formatNumber, formatStage } from '../../lib/format';
import { LiveUnavailableView } from '../../live/LiveUnavailableView';

export const RunDetailView: React.FC = () => {
  const { clientMode } = useOperant();
  if (clientMode === 'live') {
    return <LiveUnavailableView section="runs" />;
  }
  return <DemoRunDetailView />;
};

const DemoRunDetailView: React.FC = () => {
  const { client, selectedWorkflowRunId, addNotification } = useOperant();
  // v6 修复轮：取消运行成功后同步解除绑定工作流的演示实例运行中标记（DemoContext 守卫口径）
  const { clearRunningRunStatus } = useDemo();

  const [run, setRun] = useState<WorkflowRun | null>(null);
  const [loading, setLoading] = useState(true);
  const [resumeModalOpen, setResumeModalOpen] = useState(false);
  const [allowCoderReplay, setAllowCoderReplay] = useState(false);

  const runId = selectedWorkflowRunId || 'run_operant_001';

  const loadRun = async () => {
    try {
      setLoading(true);
      const data = await client.getWorkflowRun(runId);
      setRun(data);
    } catch (err: unknown) {
      console.error('Failed to load run:', err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadRun();
  }, [client, runId]);

  const stages: WorkflowStage[] = ['planner', 'explorers', 'coder', 'reviewer', 'main'];

  const getStageIndex = (stage: WorkflowStage) => stages.indexOf(stage);

  const handleResume = async () => {
    if (!run) return;
    try {
      await client.resumeWorkflowRun(run.id, allowCoderReplay);
      setResumeModalOpen(false);
      addNotification('success', `工作流运行 ${run.id} 已恢复。`);
      loadRun();
    } catch (err: unknown) {
      addNotification('error', `恢复运行失败：${err instanceof Error ? err.message : '未知错误'}`);
    }
  };

  const handleCancel = async () => {
    if (!run) return;
    try {
      await client.cancelWorkflowRun(run.id);
      // 同步解除绑定该工作流的演示会话运行中标记（runStatus → null），
      // 避免实例守卫提示"先在运行进度中取消运行"后照做仍被拦截的死路
      if (run.definition_revision_id) clearRunningRunStatus(run.definition_revision_id);
      addNotification('warn', `工作流运行 ${run.id} 已取消。`);
      loadRun();
    } catch (err: unknown) {
      addNotification('error', '取消失败');
    }
  };

  return (
    <div style={{ padding: 24, overflowY: 'auto', height: '100%', display: 'flex', flexDirection: 'column', gap: 20 }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 16, flexWrap: 'wrap' }}>
        <div style={{ minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4, flexWrap: 'wrap' }}>
            <h1 style={{ fontSize: '18px', fontWeight: 700, color: 'var(--text-primary)' }}>
              执行运行：{run?.id}
            </h1>
            {run && <StatusBadge status={run.status} />}
          </div>
          <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>{run?.task}</div>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <button onClick={loadRun} className="btn btn-ghost btn-sm" title="刷新运行状态" aria-label="刷新运行状态">
            <RefreshCw size={14} />
          </button>
          <a
            href={client.exportWorkflowTraceUrl(runId)}
            download
            className="btn btn-secondary btn-sm"
            target="_blank"
            rel="noreferrer"
          >
            <Download size={13} />
            <span>导出追踪 JSONL</span>
          </a>
          {run?.status !== 'completed' && run?.status !== 'cancelled' && (
            <>
              <button onClick={() => setResumeModalOpen(true)} className="btn btn-secondary btn-sm">
                <RotateCcw size={13} />
                <span>恢复 / 人工核对</span>
              </button>
              <button onClick={handleCancel} className="btn btn-danger btn-sm">
                <Square size={13} />
                <span>取消运行</span>
              </button>
            </>
          )}
        </div>
      </div>

      {loading ? (
        <LoadingSkeleton lines={4} height={100} />
      ) : run ? (
        <>
          {/* Stage Progression Pipeline */}
          <div className="card" style={{ padding: 20 }}>
            <h3 style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-muted)', marginBottom: 16 }}>
              五阶段顺序流水线进度
            </h3>

            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', position: 'relative', overflowX: 'auto' }}>
              {stages.map((stage, idx) => {
                const currentIdx = getStageIndex(run.current_stage);
                const isPast = idx < currentIdx || run.status === 'completed';
                const isCurrent = idx === currentIdx && run.status !== 'completed';

                return (
                  <React.Fragment key={stage}>
                    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6, zIndex: 2, flexShrink: 0 }}>
                      <div
                        style={{
                          width: 38,
                          height: 38,
                          borderRadius: '50%',
                          backgroundColor: isPast
                            ? 'var(--status-safe-bg)'
                            : isCurrent
                            ? 'var(--accent-action)'
                            : 'var(--bg-subtle)',
                          color: isPast
                            ? 'var(--status-safe-text)'
                            : isCurrent
                            ? 'var(--text-inverse)'
                            : 'var(--text-muted)',
                          border: isPast
                            ? '1px solid var(--status-safe-border)'
                            : isCurrent
                            ? '2px solid var(--border-focus)'
                            : '1px solid var(--border-subtle)',
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'center',
                          fontWeight: 700,
                          fontSize: '13px',
                          boxShadow: isCurrent ? 'var(--shadow-md)' : 'none',
                        }}
                      >
                        {isPast ? <CheckCircle2 size={18} /> : idx + 1}
                      </div>
                      <span
                        style={{
                          fontSize: '12px',
                          fontWeight: isCurrent ? 700 : 500,
                          color: isCurrent ? 'var(--accent-action)' : 'var(--text-secondary)',
                        }}
                      >
                        {formatStage(stage)}
                      </span>
                    </div>

                    {idx < stages.length - 1 && (
                      <div
                        style={{
                          flex: 1,
                          minWidth: 16,
                          height: 2,
                          backgroundColor: idx < currentIdx ? 'var(--status-safe-text)' : 'var(--border-subtle)',
                          margin: '0 8px',
                          marginBottom: 20,
                        }}
                      />
                    )}
                  </React.Fragment>
                );
              })}
            </div>
          </div>

          {/* Execution Metrics & Rework Counter */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: 14 }}>
            <div className="card" style={{ padding: 14 }}>
              <div style={{ fontSize: '11px', color: 'var(--text-muted)' }}>当前返工轮次</div>
              <div style={{ fontSize: '22px', fontWeight: 700, color: 'var(--text-primary)', marginTop: 4 }}>
                {run.current_rework_round} / {run.max_rework_rounds}
              </div>
            </div>
            <div className="card" style={{ padding: 14 }}>
              <div style={{ fontSize: '11px', color: 'var(--text-muted)' }}>并行探索器</div>
              <div style={{ fontSize: '22px', fontWeight: 700, color: 'var(--text-primary)', marginTop: 4 }}>
                {run.max_parallel_explorers} 个活跃
              </div>
            </div>
            <div className="card" style={{ padding: 14 }}>
              <div style={{ fontSize: '11px', color: 'var(--text-muted)' }}>已分配的编码角色</div>
              <div style={{ fontSize: '14px', fontWeight: 600, color: 'var(--accent-action)', marginTop: 8 }}>
                {run.coder_role_id}
              </div>
            </div>
            <div className="card" style={{ padding: 14 }}>
              <div style={{ fontSize: '11px', color: 'var(--text-muted)' }}>目标工作区</div>
              <div style={{ fontSize: '12px', fontFamily: 'var(--font-mono)', color: 'var(--text-secondary)', marginTop: 8, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {run.workspace}
              </div>
            </div>
          </div>

          {/* Node Runs & Attempts Breakdown */}
          <div>
            <h3 style={{ fontSize: '14px', fontWeight: 600, color: 'var(--text-primary)', marginBottom: 12 }}>
              阶段检查点与节点运行（{run.node_runs.length}）
            </h3>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              {run.node_runs.map((nr) => (
                <div key={nr.id} className="card" style={{ padding: 14 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8, gap: 8 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                      <span style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)' }}>{nr.label}</span>
                      <span className="badge" style={{ backgroundColor: 'var(--bg-subtle)', fontSize: '11px' }}>
                        {nr.type}
                      </span>
                    </div>
                    <StatusBadge status={nr.status} size="sm" />
                  </div>

                  {nr.attempts.map((att, i) => (
                    <div
                      key={i}
                      style={{
                        padding: 8,
                        borderRadius: 'var(--radius-sm)',
                        backgroundColor: 'var(--bg-surface)',
                        fontSize: '11px',
                        color: 'var(--text-secondary)',
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'center',
                        gap: 8,
                        flexWrap: 'wrap',
                      }}
                    >
                      <span>第 {att.attempt_number} 次尝试（{STATUS_LABELS[att.status] || att.status}）</span>
                      {att.usage && (
                        <span>
                          输入 {formatNumber(att.usage.input_tokens)} tokens • ${att.usage.cost_usd?.toFixed(4) || '0.00'}
                        </span>
                      )}
                    </div>
                  ))}
                </div>
              ))}
            </div>
          </div>
        </>
      ) : null}

      {/* Resume Modal */}
      <Modal
        isOpen={resumeModalOpen}
        onClose={() => setResumeModalOpen(false)}
        title="恢复与人工核对工作流运行"
        footer={
          <>
            <button onClick={() => setResumeModalOpen(false)} className="btn btn-ghost">
              取消
            </button>
            <button onClick={handleResume} className="btn btn-primary">
              <RotateCcw size={14} />
              <span>确认并恢复</span>
            </button>
          </>
        }
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <p style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
            恢复将从 SQLite 中最新的持久检查点继续执行。继续前请核对安全权限。
          </p>

          <div style={{ padding: 12, borderRadius: 'var(--radius-sm)', backgroundColor: 'var(--bg-surface)', border: '1px solid var(--border-subtle)' }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: '12px', fontWeight: 600, color: 'var(--text-primary)', cursor: 'pointer' }}>
              <input
                type="checkbox"
                checked={allowCoderReplay}
                onChange={(e) => setAllowCoderReplay(e.target.checked)}
              />
              <span>允许 Coder 重放（allow_coder_replay）</span>
            </label>
            <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: 4, marginLeft: 20 }}>
              启用 allow_coder_replay 后，若工作区差异未损坏，将从最近验证的检查点重新执行 Coder 写入。
            </div>
          </div>
        </div>
      </Modal>
    </div>
  );
};
