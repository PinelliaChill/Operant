import React, { useEffect, useRef, useState } from 'react';
import type { Phase23Client } from '@operant/sdk';
import type * as Phase23 from '../../../../../sdk/typescript-client/phase23.generated';
import type { CollaborationTeam } from './b24-client';

interface Props {
  client: Phase23Client;
  team: CollaborationTeam | undefined;
  baseWorkflow: { workflow_id: string; version: number } | undefined;
  disabled?: boolean;
  onDraftReady: (definition: Phase23.WorkflowDefinition) => void;
}

export const WorkflowAssistant: React.FC<Props> = ({ client, team, baseWorkflow, disabled, onDraftReady }) => {
  const [instruction, setInstruction] = useState('');
  const [suggestionState, setSuggestionState] = useState<{ value: Phase23.WorkflowSuggestion; scope: string } | null>(null);
  const [pending, setPending] = useState(false);
  const [history, setHistory] = useState<Phase23.WorkflowSuggestionConversationSummary[]>([]);
  const [conversation, setConversation] = useState<Phase23.WorkflowSuggestionConversation | null>(null);
  const [activeConversationId, setActiveConversationId] = useState('');
  const [historyLoading, setHistoryLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const scope = JSON.stringify([team?.team_id, team?.version, baseWorkflow?.workflow_id, baseWorkflow?.version]);
  const currentScope = useRef(scope);
  const requestVersion = useRef(0);
  const mutationKeys = useRef(new Map<string, string>());
  currentScope.current = scope;
  const suggestion = suggestionState?.scope === scope ? suggestionState.value : null;

  useEffect(() => {
    requestVersion.current += 1;
    setSuggestionState(null);
    setConversation(null);
    setActiveConversationId('');
    setPending(false);
    setError(null);
  }, [scope]);

  useEffect(() => {
    const selectedTeam = team?.team_id;
    const selectedVersion = team?.version;
    if (!selectedTeam || !selectedVersion) { setHistory([]); setConversation(null); return; }
    let current = true;
    setHistoryLoading(true);
    void client.listWorkflowSuggestionConversations({ limit: 50 }).then((page) => {
      if (current) setHistory(page.items.filter((item) => item.team_id === selectedTeam && item.team_version === selectedVersion));
    }).catch((caught) => { if (current) setError(caught instanceof Error ? caught.message : '对话历史读取失败。'); })
      .finally(() => { if (current) setHistoryLoading(false); });
    return () => { current = false; };
  }, [client, team?.team_id, team?.version]);

  const openConversation = async (id: string) => {
    const requestId = ++requestVersion.current;
    const requestScope = scope;
    const preserveKnown = id !== '' && id === activeConversationId;
    if (!preserveKnown) { setSuggestionState(null); setConversation(null); }
    setActiveConversationId(id);
    if (!id) return;
    setHistoryLoading(true); setError(null);
    try {
      const result = await client.getWorkflowSuggestionConversation(id);
      if (requestId !== requestVersion.current || currentScope.current !== requestScope) return;
      if (result.team_id !== team?.team_id || result.team_version !== team.version) throw new Error('历史对话不属于当前 Team。');
      setConversation(result);
      const latest = result.turns.at(-1);
      if (latest) setSuggestionState({ scope, value: { ...latest, conversation_id: id, turn_id: latest.turn_id } });
    } catch (caught) { if (requestId === requestVersion.current && currentScope.current === requestScope) setError(caught instanceof Error ? caught.message : '对话历史读取失败。'); }
    finally { if (requestId === requestVersion.current && currentScope.current === requestScope) setHistoryLoading(false); }
  };

  const refreshHistory = async () => {
    if (!team?.team_id || !team.version) return;
    const requestScope = scope;
    const requestId = requestVersion.current;
    setHistoryLoading(true); setError(null);
    try {
      const [page, detail] = await Promise.all([
        client.listWorkflowSuggestionConversations({ limit: 50 }),
        activeConversationId ? client.getWorkflowSuggestionConversation(activeConversationId) : Promise.resolve(null),
      ]);
      if (requestId !== requestVersion.current || currentScope.current !== requestScope) return;
      setHistory(page.items.filter((item) => item.team_id === team.team_id && item.team_version === team.version));
      if (detail) {
        if (detail.team_id !== team.team_id || detail.team_version !== team.version) throw new Error('历史对话不属于当前 Team。');
        setConversation(detail);
        const latest = detail.turns.at(-1);
        if (latest) setSuggestionState({ scope, value: { ...latest, conversation_id: activeConversationId, turn_id: latest.turn_id } });
      }
    } catch (caught) { if (requestId === requestVersion.current && currentScope.current === requestScope) setError(caught instanceof Error ? caught.message : '历史刷新失败。'); }
    finally { if (requestId === requestVersion.current && currentScope.current === requestScope) setHistoryLoading(false); }
  };

  const suggest = async () => {
    if (!team?.team_id || !team.version || !instruction.trim()) return;
    const requestScope = scope;
    const requestId = ++requestVersion.current;
    setPending(true);
    setError(null);
    setSuggestionState(null);
    const request = {
      instruction: instruction.trim(),
      team_id: team.team_id,
      team_version: team.version,
      ...(activeConversationId ? { conversation_id: activeConversationId } : baseWorkflow ? {
        base_workflow_id: baseWorkflow.workflow_id,
        base_version: baseWorkflow.version,
      } : {}),
    };
    const action = JSON.stringify([requestScope, activeConversationId, request.instruction]);
    const idempotencyKey = mutationKeys.current.get(action) ?? crypto.randomUUID();
    mutationKeys.current.set(action, idempotencyKey);
    try {
      const result = await client.suggestWorkflowDraft(request, { idempotencyKey });
      mutationKeys.current.delete(action);
      if (requestVersion.current === requestId && currentScope.current === requestScope) {
        setSuggestionState({ value: result, scope: requestScope });
        if (result.conversation_id) {
          setActiveConversationId(result.conversation_id);
          try {
            const [detail, page] = await Promise.all([
              client.getWorkflowSuggestionConversation(result.conversation_id),
              client.listWorkflowSuggestionConversations({ limit: 50 }),
            ]);
            if (requestVersion.current === requestId && currentScope.current === requestScope) {
              setConversation(detail);
              setHistory(page.items.filter((item) => item.team_id === team.team_id && item.team_version === team.version));
            }
          } catch (caught) {
            if (requestVersion.current === requestId && currentScope.current === requestScope) {
              setError(`草稿建议已保存为对话 ${result.conversation_id}，但历史读取失败：${caught instanceof Error ? caught.message : '请刷新历史。'}`);
            }
          }
        }
      }
    } catch (caught) {
      if (requestVersion.current === requestId && currentScope.current === requestScope) {
        setError(caught instanceof Error ? caught.message : '草稿建议失败，请检查模型连接。');
      }
    } finally {
      if (requestVersion.current === requestId && currentScope.current === requestScope) setPending(false);
    }
  };

  return <section className="b24-card b24-workflow-assistant" aria-label="对话创建工作流">
    <h3>对话创建工作流</h3>
    <p className="b24-helper-text">基于当前 Team 生成可校验草稿。建议只进入画布，保存、发布和运行由你分别确认。</p>
    <label className="b24-field"><span className="b24-field-label">历史对话</span><select value={activeConversationId} onChange={(event) => void openConversation(event.target.value)} disabled={disabled || pending || historyLoading}><option value="">新建对话</option>{activeConversationId && !history.some((item) => item.conversation_id === activeConversationId) && <option value={activeConversationId}>{activeConversationId} · 已保存，历史待刷新</option>}{history.map((item) => <option key={item.conversation_id} value={item.conversation_id}>{item.last_instruction || item.conversation_id} · {item.turn_count} 轮</option>)}</select></label>
    <button className="btn btn-secondary btn-sm" type="button" onClick={() => void refreshHistory()} disabled={disabled || historyLoading || !team}>刷新对话历史</button>
    {conversation && <div className="b24-card b24-card-subtle"><strong>已保存的对话 · {conversation.turn_count} 轮</strong><p className="b24-helper-text">起点：{conversation.base_workflow_id ? `${conversation.base_workflow_id} v${conversation.base_version}` : '新建工作流'}。继续修改以最后一轮候选为基础。</p><ol>{conversation.turns.map((turn) => <li key={turn.turn_id}><strong>第 {turn.ordinal} 轮</strong>：{turn.instruction}<small className="b24-field-help">{turn.changes.join('；') || '无结构变化'}</small></li>)}</ol></div>}
    <label className="b24-field">
      <span className="b24-field-label">想怎样编排？</span>
      <textarea rows={3} value={instruction} onChange={(event) => setInstruction(event.target.value)} maxLength={8000} placeholder="例如：两名 Agent 并行分析，汇合后由主 Agent 总结" />
    </label>
    <div className="b24-actions">
      <button className="btn btn-secondary" type="button" onClick={() => void suggest()} disabled={disabled || pending || !team || !instruction.trim()}>
        {pending ? '生成并校验中…' : activeConversationId ? '继续修改草稿' : '生成草稿建议'}
      </button>
      {(!team?.team_id || !team.version) && <span className="b24-helper-text">先选择一个 Team。</span>}
    </div>
    {error && <p className="b24-error" role="alert">{error}</p>}
    {suggestion && <div className="b24-card b24-card-subtle" aria-live="polite">
      <h4>{suggestion.definition.name} · 待确认</h4>
      <p>{suggestion.definition.description}</p>
      <ul>{suggestion.changes.map((change) => <li key={change}>{change}</li>)}</ul>
      <p className="b24-helper-text">权限：{suggestion.definition.nodes.filter((node) => node.writes_workspace).length} 个写入节点、{suggestion.definition.nodes.filter((node) => node.node_kind === 'tool' || node.node_kind === 'script').length} 个独立工具/脚本节点；执行仍受各 RolePreset 的工具权限和 Core 审批约束。</p>
      <p className="b24-helper-text">默认预算：{suggestion.definition.default_budget?.max_turns ?? 12} 轮、{suggestion.definition.default_budget?.timeout_seconds ?? 300} 秒；输出 Token {suggestion.definition.default_budget?.max_output_tokens ?? '未设置'}，费用上限 {suggestion.definition.default_budget?.max_cost_usd ?? '未设置'} 美元，工具调用 {suggestion.definition.default_budget?.max_tool_calls ?? '未设置'}。</p>
      {suggestion.input_redacted && <p className="b24-helper-text">输入中的凭据样式内容已脱敏后交给模型，请核对建议是否仍符合原意。</p>}
      <p className="b24-helper-text">{suggestion.definition.nodes.length} 个节点 · {suggestion.definition.edges?.length ?? 0} 条连线 · 模型 {suggestion.model_id}</p>
      <button className="btn btn-primary" type="button" onClick={() => { if (suggestionState?.scope !== currentScope.current) return; onDraftReady(suggestion.definition); setSuggestionState(null); }} disabled={disabled}>应用到画布审阅</button>
    </div>}
  </section>;
};
