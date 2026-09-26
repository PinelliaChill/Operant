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
  const [error, setError] = useState<string | null>(null);
  const scope = JSON.stringify([team?.team_id, team?.version, baseWorkflow?.workflow_id, baseWorkflow?.version]);
  const currentScope = useRef(scope);
  const requestVersion = useRef(0);
  currentScope.current = scope;
  const suggestion = suggestionState?.scope === scope ? suggestionState.value : null;

  useEffect(() => {
    requestVersion.current += 1;
    setSuggestionState(null);
    setPending(false);
    setError(null);
  }, [scope]);

  const suggest = async () => {
    if (!team?.team_id || !team.version || !instruction.trim()) return;
    const requestScope = scope;
    const requestId = ++requestVersion.current;
    setPending(true);
    setError(null);
    setSuggestionState(null);
    try {
      const result = await client.suggestWorkflowDraft({
        instruction: instruction.trim(),
        team_id: team.team_id,
        team_version: team.version,
        ...(baseWorkflow ? {
          base_workflow_id: baseWorkflow.workflow_id,
          base_version: baseWorkflow.version,
        } : {}),
      });
      if (requestVersion.current === requestId && currentScope.current === requestScope) {
        setSuggestionState({ value: result, scope: requestScope });
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
    <label className="b24-field">
      <span className="b24-field-label">想怎样编排？</span>
      <textarea rows={3} value={instruction} onChange={(event) => setInstruction(event.target.value)} maxLength={8000} placeholder="例如：两名 Agent 并行分析，汇合后由主 Agent 总结" />
    </label>
    <div className="b24-actions">
      <button className="btn btn-secondary" type="button" onClick={() => void suggest()} disabled={disabled || pending || !team || !instruction.trim()}>
        {pending ? '生成并校验中…' : '生成草稿建议'}
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
