/**
 * 调度分区（v8：多入口联动调度与即时触发）
 * - 页头「+ 新建调度」弹窗：支持 Cron 周期表达式与一次性 Timer 倒计时；
 *   调度目标支持工作流模板、指定 Prompt 指令任务与系统巡检脚本；
 * - 列表卡片：展示触发规则、目标类型、下次运行时间、最近运行状态（成功/运行中/失败）；
 * - 动作：立即运行 (Run Now)、启停开关、删除；
 * - 多入口联动：支持通过 URL 参数 ?new=1&targetType=...&targetId=... 快速预填弹窗创建。
 */

import React, { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  CalendarClock,
  Clock,
  Play,
  Plus,
  Trash2,
  Workflow,
  MessageSquare,
  Activity,
} from 'lucide-react';
import { EmptyState } from '../../components/EmptyState';
import { Modal } from '../../components/Modal';
import { useOperant } from '../../context/ClientContext';
import { useDemo } from '../../demo/DemoContext';
import { formatDateTime } from '../../lib/format';

const CRON_PRESETS = [
  { label: '每小时整点', value: '0 * * * *' },
  { label: '每日凌晨 3 点', value: '0 3 * * *' },
  { label: '工作日早 9:30', value: '30 9 * * 1-5' },
  { label: '每 15 分钟', value: '*/15 * * * *' },
];

export const SchedulesView: React.FC = () => {
  const { schedules, toggleSchedule, createSchedule, deleteSchedule, triggerSchedule, getWorkflowDirectory } = useDemo();
  const { addNotification } = useOperant();
  const [searchParams, setSearchParams] = useSearchParams();

  const [modalOpen, setModalOpen] = useState(false);
  const [name, setName] = useState('');
  const [type, setType] = useState<'cron' | 'timer'>('cron');
  const [cron, setCron] = useState('0 3 * * *');
  const [timerMinutes, setTimerMinutes] = useState('60');
  const [targetType, setTargetType] = useState<'workflow' | 'prompt' | 'health_check'>('workflow');
  const [targetId, setTargetId] = useState('');
  const [targetPayload, setTargetPayload] = useState('');

  const [workflows, setWorkflows] = useState<Array<{ id: string; name: string }>>([]);

  useEffect(() => {
    getWorkflowDirectory().then((list) => {
      setWorkflows(list.map((w) => ({ id: w.id, name: w.name })));
      if (list.length > 0 && !targetId) {
        setTargetId(list[0].id);
      }
    });
  }, [getWorkflowDirectory]);

  // 处理多入口联动预填参数
  useEffect(() => {
    if (searchParams.get('new') === '1') {
      setModalOpen(true);
      const urlTargetType = searchParams.get('targetType') as 'workflow' | 'prompt' | 'health_check' | null;
      const urlTargetId = searchParams.get('targetId');
      const urlTargetName = searchParams.get('targetName');

      if (urlTargetType) setTargetType(urlTargetType);
      if (urlTargetId) setTargetId(urlTargetId);
      if (urlTargetName) {
        setName(`定时触发 · ${urlTargetName}`);
        setTargetPayload(urlTargetName);
      }
    }
  }, [searchParams]);

  const handleToggle = (id: string, schedName: string, enabled: boolean) => {
    toggleSchedule(id);
    addNotification(
      'success',
      enabled ? `已停用「${schedName}」（演示）` : `已启用「${schedName}」（演示）`
    );
  };

  const handleCreate = () => {
    if (!name.trim()) return;

    let nextRunIso = new Date(Date.now() + 3600 * 1000).toISOString();
    let finalPayload = targetPayload;

    if (targetType === 'workflow') {
      const foundWf = workflows.find((w) => w.id === targetId);
      if (foundWf) finalPayload = foundWf.name;
    }

    if (type === 'timer') {
      const mins = parseInt(timerMinutes, 10) || 60;
      nextRunIso = new Date(Date.now() + mins * 60 * 1000).toISOString();
    }

    createSchedule({
      name: name.trim(),
      type,
      cron: type === 'cron' ? cron : undefined,
      timerSeconds: type === 'timer' ? (parseInt(timerMinutes, 10) || 60) * 60 : undefined,
      targetType,
      targetId: targetId || undefined,
      targetPayload: finalPayload || name.trim(),
      nextRun: nextRunIso,
      enabled: true,
      lastRunStatus: undefined,
    });

    setModalOpen(false);
    setName('');
    // 清除 URL 参数
    setSearchParams({}, { replace: true });
  };

  return (
    <div className="section-view">
      <header className="section-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <h1 className="section-title">调度中心</h1>
          <p className="section-sub">按计划或倒计时自动触发工作流与 Prompt 任务（共 {schedules.length} 个）</p>
        </div>
        <button className="btn btn-primary btn-sm" onClick={() => setModalOpen(true)}>
          <Plus size={13} />
          <span>新建调度</span>
        </button>
      </header>

      <div className="section-scroll">
        <div className="section-inner">
          {schedules.length === 0 ? (
            <div className="section-empty-wrap">
              <EmptyState
                icon={CalendarClock}
                title="暂无调度任务"
                description="点击上方「新建调度」创建周期性 Cron 或一次性倒计时任务。"
                action={
                  <button className="btn btn-primary" onClick={() => setModalOpen(true)}>
                    <Plus size={14} />
                    <span>新建调度</span>
                  </button>
                }
              />
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              {schedules.map((s) => {
                const TargetIcon =
                  s.targetType === 'workflow'
                    ? Workflow
                    : s.targetType === 'prompt'
                      ? MessageSquare
                      : Activity;

                const targetLabel =
                  s.targetType === 'workflow'
                    ? '工作流'
                    : s.targetType === 'prompt'
                      ? 'Prompt'
                      : '系统巡检';

                return (
                  <div
                    key={s.id}
                    className="card"
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                      padding: '12px 16px',
                      gap: 14,
                      flexWrap: 'wrap',
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: 12, minWidth: 260, flex: 1 }}>
                      <span
                        style={{
                          width: 32,
                          height: 32,
                          borderRadius: 'var(--radius-sm)',
                          backgroundColor: s.type === 'cron' ? 'var(--accent-subtle)' : 'var(--bg-subtle)',
                          color: s.type === 'cron' ? 'var(--accent-action)' : 'var(--text-secondary)',
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'center',
                          flexShrink: 0,
                        }}
                      >
                        {s.type === 'cron' ? <CalendarClock size={16} /> : <Clock size={16} />}
                      </span>

                      <div style={{ display: 'flex', flexDirection: 'column', gap: 3, minWidth: 0 }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                          <span style={{ fontSize: '14px', fontWeight: 600, color: 'var(--text-primary)' }}>
                            {s.name}
                          </span>
                          <span className="badge" style={{ backgroundColor: 'var(--bg-surface)', fontSize: '10px' }}>
                            <TargetIcon size={10} style={{ marginRight: 4 }} />
                            {targetLabel}
                          </span>
                          {s.lastRunStatus && (
                            <span
                              className={`badge ${
                                s.lastRunStatus === 'success'
                                  ? 'badge-safe'
                                  : s.lastRunStatus === 'running'
                                    ? 'badge-action'
                                    : 'badge-warn'
                              }`}
                              style={{ fontSize: '10px' }}
                            >
                              {s.lastRunStatus === 'success'
                                ? '最近成功'
                                : s.lastRunStatus === 'running'
                                  ? '运行中'
                                  : '最近失败'}
                            </span>
                          )}
                        </div>

                        <div style={{ fontSize: '12px', color: 'var(--text-secondary)', display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                          <span style={{ fontFamily: 'var(--font-mono)', fontSize: '11px', color: 'var(--accent-action)' }}>
                            {s.type === 'cron' ? s.cron : `倒计时 ${(s.timerSeconds ?? 3600) / 60} 分钟`}
                          </span>
                          <span>•</span>
                          <span>下次触发：{formatDateTime(s.nextRun)}</span>
                          {s.targetPayload && (
                            <>
                              <span>•</span>
                              <span style={{ color: 'var(--text-muted)' }}>目标：{s.targetPayload}</span>
                            </>
                          )}
                        </div>
                      </div>
                    </div>

                    <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                      <button
                        type="button"
                        className="btn btn-secondary btn-sm"
                        onClick={() => triggerSchedule(s.id)}
                        disabled={!s.enabled}
                        title="立即触发运行"
                      >
                        <Play size={12} />
                        <span>立即运行</span>
                      </button>

                      <button
                        type="button"
                        role="switch"
                        aria-checked={s.enabled}
                        className="switch"
                        onClick={() => handleToggle(s.id, s.name, s.enabled)}
                        aria-label={s.enabled ? `停用 ${s.name}` : `启用 ${s.name}`}
                      />

                      <button
                        type="button"
                        className="btn btn-ghost btn-icon"
                        style={{ padding: 4, color: 'var(--text-muted)' }}
                        onClick={() => deleteSchedule(s.id)}
                        title="删除调度"
                        aria-label="删除调度"
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          <p className="section-footnote" style={{ marginTop: 24 }}>
            调度任务由本地 Scheduler 定时驱动，触发时自动派发 Workflow Run 或生成会话消息。
          </p>
        </div>
      </div>

      {/* 新建调度 Modal */}
      <Modal
        isOpen={modalOpen}
        onClose={() => {
          setModalOpen(false);
          setSearchParams({}, { replace: true });
        }}
        title="新建调度任务"
        footer={
          <>
            <button
              className="btn btn-ghost"
              onClick={() => {
                setModalOpen(false);
                setSearchParams({}, { replace: true });
              }}
            >
              取消
            </button>
            <button className="btn btn-primary" onClick={handleCreate} disabled={!name.trim()}>
              <Plus size={14} />
              <span>创建调度</span>
            </button>
          </>
        }
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div>
            <label style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: 4 }}>
              调度名称
            </label>
            <input
              type="text"
              className="input"
              placeholder="例如：每日全量测试回归"
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoFocus
            />
          </div>

          <div>
            <label style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: 6 }}>
              调度类型
            </label>
            <div style={{ display: 'flex', gap: 10 }}>
              <button
                type="button"
                className={`btn btn-sm ${type === 'cron' ? 'btn-primary' : 'btn-secondary'}`}
                onClick={() => setType('cron')}
              >
                <CalendarClock size={13} />
                <span>Cron 周期循环</span>
              </button>
              <button
                type="button"
                className={`btn btn-sm ${type === 'timer' ? 'btn-primary' : 'btn-secondary'}`}
                onClick={() => setType('timer')}
              >
                <Clock size={13} />
                <span>一次性倒计时 (Timer)</span>
              </button>
            </div>
          </div>

          {type === 'cron' ? (
            <div>
              <label style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: 4 }}>
                Cron 表达式
              </label>
              <input
                type="text"
                className="input"
                value={cron}
                onChange={(e) => setCron(e.target.value)}
                placeholder="* * * * *"
              />
              <div style={{ display: 'flex', gap: 6, marginTop: 6, flexWrap: 'wrap' }}>
                {CRON_PRESETS.map((p) => (
                  <button
                    key={p.value}
                    type="button"
                    className="btn btn-ghost btn-sm"
                    style={{ fontSize: '11px', padding: '2px 6px' }}
                    onClick={() => setCron(p.value)}
                  >
                    {p.label}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div>
              <label style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: 4 }}>
                倒计时时长 (分钟)
              </label>
              <input
                type="number"
                className="input"
                value={timerMinutes}
                onChange={(e) => setTimerMinutes(e.target.value)}
                min="1"
              />
            </div>
          )}

          <div>
            <label style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: 6 }}>
              调度触发目标
            </label>
            <div style={{ display: 'flex', gap: 8, marginBottom: 8 }}>
              <button
                type="button"
                className={`btn btn-sm ${targetType === 'workflow' ? 'btn-primary' : 'btn-secondary'}`}
                onClick={() => setTargetType('workflow')}
              >
                <Workflow size={13} />
                <span>工作流模板</span>
              </button>
              <button
                type="button"
                className={`btn btn-sm ${targetType === 'prompt' ? 'btn-primary' : 'btn-secondary'}`}
                onClick={() => setTargetType('prompt')}
              >
                <MessageSquare size={13} />
                <span>Prompt 指令</span>
              </button>
              <button
                type="button"
                className={`btn btn-sm ${targetType === 'health_check' ? 'btn-primary' : 'btn-secondary'}`}
                onClick={() => setTargetType('health_check')}
              >
                <Activity size={13} />
                <span>系统巡检</span>
              </button>
            </div>

            {targetType === 'workflow' && (
              <select className="select" value={targetId} onChange={(e) => setTargetId(e.target.value)}>
                {workflows.map((w) => (
                  <option key={w.id} value={w.id}>
                    {w.name}
                  </option>
                ))}
              </select>
            )}

            {targetType === 'prompt' && (
              <textarea
                className="input"
                rows={2}
                placeholder="请输入定时触发的 Prompt 指令内容…"
                value={targetPayload}
                onChange={(e) => setTargetPayload(e.target.value)}
              />
            )}
          </div>
        </div>
      </Modal>
    </div>
  );
};
