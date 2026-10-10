import React, { useCallback, useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { useOperant } from '../../context/ClientContext';
import { useLive } from '../../live/LiveContext';
import { useOnboarding } from '../../live/OnboardingContext';
import { SearchSelect } from '../../components/SearchSelect';
import { sessionControlClient, type ChecklistRecord, type GoalRecord, type PlanRecord } from '../../live/sessionControlClient';
import './live-goal-plan.css';

const lines = (value: string) => value.split('\n').map((item) => item.trim()).filter(Boolean);
const PLAN_LIST_FIELDS = ['assumptions', 'constraints', 'open_questions', 'proposed_changes', 'risk_items', 'approval_requirements', 'verification_plan'] as const;
const PLAN_LABELS: Record<(typeof PLAN_LIST_FIELDS)[number], string> = {
  assumptions: '前提', constraints: '约束', open_questions: '待确认问题', proposed_changes: '拟改动', risk_items: '风险', approval_requirements: '审批要求', verification_plan: '验证计划',
};
const goalStatusLabel = (status: GoalRecord['status']) => ({ active: '进行中', blocked: '受阻', completed: '已完成', cancelled: '已取消' })[status];
const planStatusLabel = (status: PlanRecord['status']) => ({ draft: '草稿', in_review: '待审阅', approved: '已批准', in_progress: '执行中', completed: '已完成', superseded: '已替代' })[status];
const checklistStatusLabel = (status: ChecklistRecord['status']) => ({ todo: '待办', in_progress: '执行中', blocked: '受阻', done: '已完成', skipped: '已跳过' })[status];

export const LiveGoalPlanPanel: React.FC = () => {
  const { connectionStatus } = useOperant();
  const { threads, selectedThread } = useLive();
  const { metadata } = useOnboarding();
  const [threadId, setThreadId] = useState(selectedThread?.id || '');
  const [goals, setGoals] = useState<GoalRecord[]>([]);
  const [goalId, setGoalId] = useState('');
  const [plans, setPlans] = useState<PlanRecord[]>([]);
  const [planId, setPlanId] = useState('');
  const [checklist, setChecklist] = useState<ChecklistRecord[]>([]);
  const [objective, setObjective] = useState('');
  const [criteria, setCriteria] = useState('');
  const [goalStatus, setGoalStatus] = useState<GoalRecord['status']>('active');
  const [goalReason, setGoalReason] = useState('');
  const [goalBudget, setGoalBudget] = useState('');
  const [planScope, setPlanScope] = useState('');
  const [planStatus, setPlanStatus] = useState<PlanRecord['status']>('draft');
  const [planLists, setPlanLists] = useState<Record<string, string>>({});
  const [newItem, setNewItem] = useState('');
  const [itemEvidence, setItemEvidence] = useState('');
  const [itemBlocker, setItemBlocker] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const goal = goals.find((item) => item.id === goalId);
  const plan = plans.find((item) => item.id === planId);
  const sourceSessionId = threads.find((item) => item.id === threadId)?.sessionId;

  const loadGoals = useCallback(async () => {
    if (!threadId || connectionStatus !== 'connected') { setGoals([]); return; }
    setLoading(true); setError('');
    try { const next = await sessionControlClient.listGoals(threadId); setGoals(next); setGoalId((current) => next.some((item) => item.id === current) ? current : next[0]?.id || ''); }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'Goal 查询失败'); }
    finally { setLoading(false); }
  }, [connectionStatus, threadId]);
  useEffect(() => { void loadGoals(); }, [loadGoals]);
  useEffect(() => { if (!goalId) { setPlans([]); setPlanId(''); return; } void sessionControlClient.listPlans(goalId).then((next) => { setPlans(next); setPlanId((current) => next.some((item) => item.id === current) ? current : next[0]?.id || ''); }).catch((cause) => setError(cause instanceof Error ? cause.message : 'Plan 查询失败')); }, [goalId]);
  useEffect(() => { if (!planId) { setChecklist([]); return; } void sessionControlClient.listChecklist(planId).then(setChecklist).catch((cause) => setError(cause instanceof Error ? cause.message : 'Checklist 查询失败')); }, [planId]);
  useEffect(() => { if (!goal) return; setObjective(goal.objective); setCriteria(goal.completion_criteria.join('\n')); setGoalStatus(goal.status); setGoalReason(goal.blocked_reason || goal.result_ref || ''); setGoalBudget(goal.token_budget == null ? '' : String(goal.token_budget)); }, [goal]);
  useEffect(() => { if (!plan) return; setPlanScope(plan.scope); setPlanStatus(plan.status); setPlanLists(Object.fromEntries(PLAN_LIST_FIELDS.map((key) => [key, plan[key].join('\n')]))); }, [plan]);

  const act = async (action: () => Promise<void>, success: string) => {
    if (busy || connectionStatus !== 'connected') return;
    setBusy(true); setError(''); setNotice('');
    try { await action(); await loadGoals(); if (goalId) { const next = await sessionControlClient.listPlans(goalId); setPlans(next); } if (planId) setChecklist(await sessionControlClient.listChecklist(planId)); setNotice(success); }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'Core 命令失败，请刷新核对'); }
    finally { setBusy(false); }
  };
  const createGoal = () => void act(async () => { const created = await sessionControlClient.createGoal({ owner_thread_id: threadId, objective: objective.trim(), completion_criteria: lines(criteria), ...(goalBudget ? { token_budget: Number(goalBudget) } : {}) }); setGoalId(created.id); }, 'Goal 已保存到 Core。');
  const saveGoal = () => {
    if (!goal) return;
    if (goalStatus === 'blocked' && !goalReason.trim()) { setError('阻塞状态需要填写原因。'); return; }
    if (goalStatus === 'completed' && !goalReason.trim()) { setError('完成状态需要填写结果引用。'); return; }
    void act(async () => { await sessionControlClient.updateGoal(goal.id, goal.revision, { objective: objective.trim(), completion_criteria: lines(criteria), token_budget: goalBudget ? Number(goalBudget) : null, status: goalStatus, blocked_reason: goalStatus === 'blocked' ? goalReason.trim() : null, result_ref: goalStatus === 'completed' ? goalReason.trim() : null }); }, 'Goal 已按修订号保存。');
  };
  const createPlan = () => void act(async () => { const created = await sessionControlClient.createPlan(goalId, { scope: planScope.trim() }); setPlanId(created.id); }, 'Plan 草稿已保存。');
  const generatePlan = () => {
    if (!sourceSessionId) return;
    void act(async () => { const created = await sessionControlClient.generatePlan(goalId, sourceSessionId); setPlanId(created.id); }, '只读 Plan 草稿已保存，请先审阅。');
  };
  const savePlan = () => {
    if (!plan) return;
    void act(async () => { await sessionControlClient.updatePlan(plan.id, plan.revision, { scope: planScope.trim(), status: planStatus, ...Object.fromEntries(PLAN_LIST_FIELDS.map((key) => [key, lines(planLists[key] || '')])) }); }, 'Plan 已按修订号保存。');
  };
  const createItem = () => void act(async () => { await sessionControlClient.createChecklist(planId, newItem.trim()); setNewItem(''); }, '清单项已保存。');
  const updateItem = (item: ChecklistRecord, status: ChecklistRecord['status']) => {
    if (status === 'done' && !itemEvidence.trim()) { setError('完成清单项需要填写证据引用。'); return; }
    if (status === 'blocked' && !itemBlocker.trim()) { setError('阻塞清单项需要填写原因。'); return; }
    void act(async () => { await sessionControlClient.updateChecklistStatus(item.id, item.revision, status, { ...(status === 'done' ? { evidence_refs: lines(itemEvidence) } : {}), ...(status === 'blocked' ? { blocker: itemBlocker.trim() } : {}) }); setItemEvidence(''); setItemBlocker(''); }, '清单状态已由 Core 更新。');
  };

  return <section className="goal-plan-panel" aria-labelledby="goal-plan-title" data-client-mode="live">
    <div className="goal-plan-head"><div><h2 id="goal-plan-title">目标与计划</h2><p>管理当前对话的目标、计划和执行清单。</p></div><button type="button" className="btn btn-ghost btn-sm" onClick={() => void loadGoals()} disabled={loading || !threadId}><RefreshCw size={13} />刷新</button></div>
    <SearchSelect label="所属对话" value={threadId} onChange={(value) => { setThreadId(value); setGoalId(''); setPlanId(''); }} placeholder="选择对话" options={threads.map((thread) => ({ value: thread.id, label: metadata[thread.id]?.title || (thread.title && thread.title !== thread.id ? thread.title : '新对话') }))} />
    {threadId && <details><summary>对话详情</summary><code>{threadId}</code></details>}
    {connectionStatus !== 'connected' && <p role="alert">连接已断开，目标与计划暂不可操作。</p>}
    {error && <div className="live-alert live-alert-error" role="alert">{error}</div>}{notice && <div className="live-alert" role="status">{notice}</div>}
    {threadId && <div className="goal-plan-columns"><section className="config-card"><h3>目标</h3><label>选择目标<select className="select" value={goalId} onChange={(event) => setGoalId(event.target.value)}><option value="">新建目标</option>{goals.map((item) => <option key={item.id} value={item.id}>{item.objective.slice(0, 60)} · {goalStatusLabel(item.status)}</option>)}</select></label><label>目标<textarea className="input" value={objective} onChange={(event) => setObjective(event.target.value)} /></label><label>完成条件（每行一条）<textarea className="input" value={criteria} onChange={(event) => setCriteria(event.target.value)} /></label><details><summary>高级选项</summary><label>Token 预算（可选）<input className="input" type="number" min="1" value={goalBudget} onChange={(event) => setGoalBudget(event.target.value)} /></label></details>{goal && <><label>状态<select className="select" value={goalStatus} onChange={(event) => setGoalStatus(event.target.value as GoalRecord['status'])}><option value="active">进行中</option><option value="blocked">阻塞</option><option value="completed">完成</option><option value="cancelled">取消</option></select></label>{(goalStatus === 'blocked' || goalStatus === 'completed') && <label>{goalStatus === 'blocked' ? '阻塞原因' : '结果引用'}<input className="input" value={goalReason} onChange={(event) => setGoalReason(event.target.value)} /></label>}<small>修订号 {goal.revision}</small></>}<button type="button" className="btn btn-primary btn-sm" disabled={busy || !objective.trim() || !connectedStatus(connectionStatus)} onClick={goal ? saveGoal : createGoal}>{goal ? '保存目标' : '创建目标'}</button></section>
      {goal && <section className="config-card"><h3>计划</h3><label>选择计划<select className="select" value={planId} onChange={(event) => setPlanId(event.target.value)}><option value="">新建计划</option>{plans.map((item) => <option key={item.id} value={item.id}>{item.scope.slice(0, 60) || item.id} · {planStatusLabel(item.status)}</option>)}</select></label><label>计划范围<textarea className="input" value={planScope} onChange={(event) => setPlanScope(event.target.value)} /></label>{plan && <><label>状态<select className="select" value={planStatus} onChange={(event) => setPlanStatus(event.target.value as PlanRecord['status'])}><option value="draft">草稿</option><option value="in_review">待审</option><option value="approved">已批准</option><option value="in_progress">执行中</option><option value="completed">完成</option><option value="superseded">已替代</option></select></label>{PLAN_LIST_FIELDS.map((key) => <label key={key}>{PLAN_LABELS[key]}（每行一条）<textarea className="input" value={planLists[key] || ''} onChange={(event) => setPlanLists((current) => ({ ...current, [key]: event.target.value }))} /></label>)}<small>只读来源 · 修订号 {plan.revision}</small></>}<button type="button" className="btn btn-primary btn-sm" disabled={busy || !connectedStatus(connectionStatus)} onClick={plan ? savePlan : createPlan}>{plan ? '保存计划' : '创建计划'}</button>{sourceSessionId && <button type="button" className="btn btn-secondary btn-sm" disabled={busy || !connectedStatus(connectionStatus)} onClick={generatePlan}>生成只读草稿</button>}</section>}
      {plan && <section className="config-card"><h3>执行清单</h3><ol className="goal-checklist">{checklist.map((item) => <li key={item.id}><strong>{item.description}</strong><small>{checklistStatusLabel(item.status)} · 修订号 {item.revision}</small><div className="goal-checklist-actions"><select className="select" aria-label={`${item.description} 新状态`} defaultValue={item.status} id={`check-status-${item.id}`}><option value="todo">待办</option><option value="in_progress">执行中</option><option value="blocked">阻塞</option><option value="done">完成</option><option value="skipped">跳过</option></select><button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={() => { const element = document.getElementById(`check-status-${item.id}`) as HTMLSelectElement | null; if (element) updateItem(item, element.value as ChecklistRecord['status']); }}>更新</button></div></li>)}</ol><label>完成证据引用（更新为完成时必填）<input className="input" value={itemEvidence} onChange={(event) => setItemEvidence(event.target.value)} /></label><label>阻塞原因（更新为阻塞时必填）<input className="input" value={itemBlocker} onChange={(event) => setItemBlocker(event.target.value)} /></label><label>新增清单项<input className="input" value={newItem} onChange={(event) => setNewItem(event.target.value)} /></label><button type="button" className="btn btn-secondary btn-sm" disabled={busy || !newItem.trim()} onClick={createItem}>添加清单项</button></section>}
    </div>}
  </section>;
};

function connectedStatus(status: string): boolean { return status === 'connected'; }
