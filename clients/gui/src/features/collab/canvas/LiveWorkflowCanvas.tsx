import React, { useEffect, useRef, useState } from 'react';
import { Bot, GitBranch, GitFork, Merge, Plus, Timer, Trash2, Wrench, FileCode, Hand, ShieldCheck, Hourglass, Workflow, Package } from 'lucide-react';
import type { Phase23 } from '@operant/sdk';
import { hasOutputPort, newNode, nodePosition } from './live-graph-model';
export { graphDifference } from './live-graph-model';

type Node = Phase23.NodeSpec;
type Edge = Phase23.EdgeSpec;
type Kind = Node['node_kind'];

const kinds: { kind: Kind; label: string; icon: typeof Bot }[] = [
  { kind: 'agent', label: 'Agent', icon: Bot },
  { kind: 'tool', label: '工具', icon: Wrench },
  { kind: 'script', label: '脚本', icon: FileCode },
  { kind: 'condition', label: '条件', icon: GitFork },
  { kind: 'fan_out', label: '分支', icon: GitBranch },
  { kind: 'join', label: '汇合', icon: Merge },
  { kind: 'loop', label: '循环', icon: GitBranch },
  { kind: 'timer', label: '定时', icon: Timer },
  { kind: 'human_input', label: '人工输入', icon: Hand },
  { kind: 'approval', label: '审批', icon: ShieldCheck },
  { kind: 'wait', label: '等待', icon: Hourglass },
  { kind: 'subworkflow', label: '子工作流', icon: Workflow },
  { kind: 'artifact', label: '工件', icon: Package },
  { kind: 'merge', label: '合并', icon: Merge },
];
const width = 190;
const height = 106;
const size = { width: 1200, height: 650 };


function parsePorts(raw: string, previous: Phase23.PortSpec[]): Phase23.PortSpec[] {
  return [...new Set(raw.split(',').map((name) => name.trim()).filter(Boolean))].map((name) => previous.find((port) => port.name === name) || { name, value_type: 'any', required: false });
}

const JsonField: React.FC<{ label: string; value: unknown; onChange: (value: Record<string, unknown> | string[]) => void; disabled: boolean; array?: boolean }> = ({ label, value, onChange, disabled, array = false }) => {
  const serialized = JSON.stringify(value);
  const [text, setText] = useState(() => JSON.stringify(value, null, 2));
  const [error, setError] = useState('');
  useEffect(() => { setText(JSON.stringify(value, null, 2)); setError(''); }, [serialized]);
  return <label>{label}<textarea rows={3} value={text} onChange={(event) => {
    const next = event.target.value; setText(next);
    try {
      const parsed: unknown = JSON.parse(next);
      if (array ? !Array.isArray(parsed) || !parsed.every((item) => typeof item === 'string') : !parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error(array ? '需填写字符串数组' : '需填写 JSON 对象');
      onChange(parsed as Record<string, unknown> | string[]); setError('');
    } catch (caught) { setError(caught instanceof Error ? caught.message : 'JSON 格式无效'); }
  }} disabled={disabled} aria-invalid={Boolean(error)} />{error && <small role="alert">{error}</small>}</label>;
};

const NodeFields: React.FC<{ node: Node; nodes: Node[]; roles: { id: string; label: string }[]; disabled: boolean; update: (patch: Partial<Node>) => void }> = ({ node, nodes, roles, disabled, update }) => {
  const metadata = node.metadata || {};
  const setMetadata = (patch: Record<string, unknown>) => update({ metadata: { ...metadata, ...patch } });
  const policy = node.loop_policy;
  return <div className="livegraph-fields">
    <label>显示名称<input value={typeof metadata.label === 'string' ? metadata.label : ''} onChange={(event) => setMetadata({ label: event.target.value })} disabled={disabled} /></label>
    <label>节点 ID<input value={node.node_id} readOnly /></label>
    {(['agent', 'tool', 'script'].includes(node.node_kind)) && <label>绑定 RolePreset<select value={typeof metadata.role_id === 'string' ? metadata.role_id : ''} onChange={(event) => setMetadata({ role_id: event.target.value, role_version: undefined })} disabled={disabled}><option value="">选择角色</option>{roles.map((role) => <option key={role.id} value={role.id}>{role.label}</option>)}</select></label>}
    {node.node_kind === 'agent' && <label>节点任务<input value={typeof metadata.task === 'string' ? metadata.task : ''} onChange={(event) => setMetadata({ task: event.target.value })} disabled={disabled} /></label>}
    {node.node_kind === 'tool' && <><label>工具<select value={typeof metadata.tool_name === 'string' ? metadata.tool_name : ''} onChange={(event) => setMetadata({ tool_name: event.target.value })} disabled={disabled}><option value="">选择工具</option>{['read_file', 'search_files', 'git_diff', 'apply_patch', 'run_command'].map((name) => <option key={name} value={name}>{name}</option>)}</select></label><JsonField key={`${node.node_id}:arguments`} label={'参数绑定 JSON（可用 {"$input":"端口名"}）'} value={metadata.arguments || {}} onChange={(value) => setMetadata({ arguments: value })} disabled={disabled} /></>}
    {node.node_kind === 'script' && <><JsonField key={`${node.node_id}:argv`} label="命令 argv（字符串数组）" value={metadata.argv || []} onChange={(value) => setMetadata({ argv: value })} disabled={disabled} array /><label>工作目录<input value={typeof metadata.cwd === 'string' ? metadata.cwd : ''} onChange={(event) => setMetadata({ cwd: event.target.value })} disabled={disabled} /></label><label>超时秒数<input type="number" min="1" value={typeof metadata.timeout_seconds === 'number' ? metadata.timeout_seconds : 300} onChange={(event) => setMetadata({ timeout_seconds: Number(event.target.value) })} disabled={disabled} /></label></>}
    {node.node_kind === 'condition' && <label>条件表达式<input value={typeof metadata.expression === 'string' ? metadata.expression : ''} onChange={(event) => setMetadata({ expression: event.target.value })} disabled={disabled} /></label>}
    {node.node_kind === 'timer' && <label>延迟秒数<input type="number" min="0" max="86400" value={typeof metadata.delay_seconds === 'number' ? metadata.delay_seconds : 0} onChange={(event) => setMetadata({ delay_seconds: Number(event.target.value) })} disabled={disabled} /></label>}
    {node.node_kind === 'wait' && <label>等待秒数<input type="number" min="0" max="86400" value={typeof metadata.delay_seconds === 'number' ? metadata.delay_seconds : 0} onChange={(event) => setMetadata({ delay_seconds: Number(event.target.value) })} disabled={disabled} /></label>}
    {node.node_kind === 'human_input' && <label>输入提示<textarea rows={2} value={typeof metadata.prompt === 'string' ? metadata.prompt : ''} onChange={(event) => setMetadata({ prompt: event.target.value })} disabled={disabled} /></label>}
    {node.node_kind === 'approval' && <><label>审批说明<textarea rows={2} value={typeof metadata.detail === 'string' ? metadata.detail : ''} onChange={(event) => setMetadata({ detail: event.target.value })} disabled={disabled} /></label><label>审批类别<input value={typeof metadata.category === 'string' ? metadata.category : ''} onChange={(event) => setMetadata({ category: event.target.value })} disabled={disabled} /></label></>}
    {(['human_input', 'approval'].includes(node.node_kind)) && <><label>超时秒数<input type="number" min="1" max="86400" value={node.timeout_policy?.timeout_seconds ?? 300} onChange={(event) => update({ timeout_policy: { timeout_seconds: Number(event.target.value), on_timeout_node_id: node.timeout_policy?.on_timeout_node_id ?? null } })} disabled={disabled} /></label><label>超时分支节点<select value={node.timeout_policy?.on_timeout_node_id ?? ''} onChange={(event) => update({ timeout_policy: { timeout_seconds: node.timeout_policy?.timeout_seconds ?? 300, on_timeout_node_id: event.target.value || null } })} disabled={disabled}><option value="">选择节点</option>{nodes.filter((item) => item.node_id !== node.node_id).map((item) => <option key={item.node_id} value={item.node_id}>{item.node_id}</option>)}</select></label><p className="b24-field-help">超时分支需从当前节点连线至所选节点，保存前由 Core 编译校验。</p></>}
    {node.node_kind === 'subworkflow' && <><label>已发布子工作流 ID<input value={node.subworkflow_id ?? ''} onChange={(event) => update({ subworkflow_id: event.target.value })} disabled={disabled} /></label><label>固定版本<input type="number" min="1" step="1" value={node.subworkflow_version ?? 1} onChange={(event) => update({ subworkflow_version: Number(event.target.value) })} disabled={disabled} /></label><JsonField key={`${node.node_id}:capabilities`} label="授权能力 JSON（字符串数组）" value={node.capability_requirements ?? []} onChange={(value) => update({ capability_requirements: value as string[] })} disabled={disabled} array /></>}
    {node.node_kind === 'artifact' && <><label>工件标题<input value={typeof metadata.title === 'string' ? metadata.title : ''} onChange={(event) => setMetadata({ title: event.target.value })} disabled={disabled} /></label><label>媒体类型<input value={typeof metadata.media_type === 'string' ? metadata.media_type : 'text/plain'} onChange={(event) => setMetadata({ media_type: event.target.value })} disabled={disabled} /></label><label>敏感级别<select value={typeof metadata.sensitivity === 'string' ? metadata.sensitivity : 'normal'} onChange={(event) => setMetadata({ sensitivity: event.target.value })} disabled={disabled}><option value="normal">普通</option><option value="sensitive">敏感</option><option value="restricted">受限</option></select></label><label><input type="checkbox" checked={Boolean(metadata.content && typeof metadata.content === 'object' && !Array.isArray(metadata.content) && '$input' in metadata.content)} onChange={(event) => setMetadata({ content: event.target.checked ? { $input: 'content' } : '', artifact_id: '' })} disabled={disabled} />正文来自 content 输入端口</label>{!(metadata.content && typeof metadata.content === 'object' && !Array.isArray(metadata.content) && '$input' in metadata.content) && <label>工件正文<textarea rows={4} value={typeof metadata.content === 'string' ? metadata.content : ''} onChange={(event) => setMetadata({ content: event.target.value, artifact_id: '' })} disabled={disabled} /></label>}<label>已有工件 ID（复用时填写）<input value={typeof metadata.artifact_id === 'string' ? metadata.artifact_id : ''} onChange={(event) => setMetadata({ artifact_id: event.target.value, content: event.target.value ? undefined : metadata.content })} disabled={disabled} /><small className="b24-field-help">正文与已有 ID 二选一。也可从上游 artifact_id 输入端口获取；Core 验证工件归属和正文哈希，并将新正文发布到当前 Team。</small></label></>}
    {node.node_kind === 'merge' && <><label>合并策略<select value={node.merge_policy?.strategy ?? 'three_way'} onChange={(event) => update({ merge_policy: { ...node.merge_policy, strategy: event.target.value as Phase23.MergeStrategy, source_writer_keys: node.merge_policy?.source_writer_keys ?? [] } })} disabled={disabled}><option value="three_way">三方合并</option><option value="cherry_pick">Cherry-pick</option><option value="apply_patch">应用补丁</option></select></label><JsonField key={`${node.node_id}:writers`} label="来源 Writer Key JSON（至少两个）" value={node.merge_policy?.source_writer_keys ?? []} onChange={(value) => update({ merge_policy: { ...node.merge_policy, source_writer_keys: value as string[] } })} disabled={disabled} array /><label><input type="checkbox" checked={node.merge_policy?.require_review ?? true} onChange={(event) => update({ merge_policy: { ...node.merge_policy, source_writer_keys: node.merge_policy?.source_writer_keys ?? [], require_review: event.target.checked } })} disabled={disabled} />合并前人工审阅</label><label><input type="checkbox" checked={node.merge_policy?.rollback_on_failure ?? true} onChange={(event) => update({ merge_policy: { ...node.merge_policy, source_writer_keys: node.merge_policy?.source_writer_keys ?? [], rollback_on_failure: event.target.checked } })} disabled={disabled} />失败时回滚</label></>}
    {(['fan_out', 'join'].includes(node.node_kind)) && <JsonField key={`${node.node_id}:outputs`} label="静态输出 JSON" value={metadata.outputs || {}} onChange={(value) => setMetadata({ outputs: value })} disabled={disabled} />}
    {node.node_kind === 'loop' && policy && <><label>退出条件<input value={policy.exit_expression} onChange={(event) => update({ loop_policy: { ...policy, exit_expression: event.target.value } })} disabled={disabled} /></label><label>最多循环次数<input type="number" min="1" max="1000" value={policy.max_iterations} onChange={(event) => update({ loop_policy: { ...policy, max_iterations: Number(event.target.value) } })} disabled={disabled} /></label><label>最长秒数<input type="number" min="1" max="86400" value={policy.max_wall_seconds} onChange={(event) => update({ loop_policy: { ...policy, max_wall_seconds: Number(event.target.value) } })} disabled={disabled} /></label><label>最多输出 Token<input type="number" min="1" value={policy.max_output_tokens} onChange={(event) => update({ loop_policy: { ...policy, max_output_tokens: Number(event.target.value) } })} disabled={disabled} /></label><label>最高成本 USD<input type="number" min="0.01" step="0.01" value={policy.max_cost_usd} onChange={(event) => update({ loop_policy: { ...policy, max_cost_usd: Number(event.target.value) } })} disabled={disabled} /></label><label>最多子 Agent<input type="number" min="0" max="100" value={policy.max_subagents} onChange={(event) => update({ loop_policy: { ...policy, max_subagents: Number(event.target.value) } })} disabled={disabled} /></label><label>最大递归层数<input type="number" min="1" max="32" value={policy.max_recursion_depth} onChange={(event) => update({ loop_policy: { ...policy, max_recursion_depth: Number(event.target.value) } })} disabled={disabled} /></label><label>达到上限时流向<select value={policy.on_limit_node_id} onChange={(event) => update({ loop_policy: { ...policy, on_limit_node_id: event.target.value } })} disabled={disabled}><option value="">选择节点</option>{nodes.filter((item) => item.node_id !== node.node_id).map((item) => <option key={item.node_id} value={item.node_id}>{item.node_id}</option>)}</select></label></>}
    {(['tool', 'script', 'agent'].includes(node.node_kind)) && <><label><input type="checkbox" checked={Boolean(node.writes_workspace)} onChange={(event) => update({ writes_workspace: event.target.checked, idempotency_class: event.target.checked ? 'non_idempotent' : 'pure', writer_policy: event.target.checked ? node.writer_policy : null })} disabled={disabled} />写入工作区</label><label>幂等类型<select value={node.idempotency_class || 'pure'} onChange={(event) => update({ idempotency_class: event.target.value as Node['idempotency_class'] })} disabled={disabled || Boolean(node.writer_policy)}><option value="pure">纯读取</option><option value="idempotent">幂等写入</option><option value="non_idempotent">非幂等写入</option></select></label></>}
    {Boolean(node.writes_workspace) && ['tool', 'script', 'agent'].includes(node.node_kind) && <fieldset><legend>隔离 Writer</legend><label><input type="checkbox" checked={Boolean(node.writer_policy)} onChange={(event) => update({ writer_policy: event.target.checked ? { writer_key: node.node_id, isolation_kind: 'worktree', isolation_ref: '', ownership_paths: [] } : null, idempotency_class: 'non_idempotent' })} disabled={disabled} />启用可信 Worktree Writer</label>{node.writer_policy && <><label>Writer Key<input value={node.writer_policy.writer_key} onChange={(event) => update({ writer_policy: { ...node.writer_policy!, writer_key: event.target.value } })} disabled={disabled} /></label><label>隔离类型<select value={node.writer_policy.isolation_kind} onChange={(event) => update({ writer_policy: { ...node.writer_policy!, isolation_kind: event.target.value as Phase23.WriterIsolationKind } })} disabled={disabled}><option value="worktree">Git Worktree</option><option value="container" disabled>容器（当前执行不支持）</option></select></label><label>可信隔离引用<input value={node.writer_policy.isolation_ref} onChange={(event) => update({ writer_policy: { ...node.writer_policy!, isolation_ref: event.target.value } })} disabled={disabled} /><small className="b24-field-help">由管理员映射到独立且干净的 Worktree。Core 会验证引用及实际工作区。</small></label><JsonField key={`${node.node_id}:ownership`} label="拥有路径 JSON（仓库相对路径）" value={node.writer_policy.ownership_paths} onChange={(value) => update({ writer_policy: { ...node.writer_policy!, ownership_paths: value as string[] } })} disabled={disabled} array /></>}</fieldset>}
    <label>输入端口（逗号分隔）<input value={(node.input_ports || []).map((port) => port.name).join(', ')} onChange={(event) => update({ input_ports: parsePorts(event.target.value, node.input_ports || []) })} disabled={disabled} /></label>
    <label>输出端口（逗号分隔）<input value={(node.output_ports || []).map((port) => port.name).join(', ')} onChange={(event) => update({ output_ports: parsePorts(event.target.value, node.output_ports || []) })} disabled={disabled} /></label>
  </div>;
};

interface Props {
  nodes: Node[];
  edges: Edge[];
  diagnostics: Phase23.CompileDiagnostic[];
  nodeRuns?: Phase23.NodeRun[];
  onChangeNodes: (nodes: Node[]) => void;
  onChangeEdges: (edges: Edge[]) => void;
  roles: { id: string; label: string }[];
  disabled?: boolean;
}

export const LiveWorkflowCanvas: React.FC<Props> = ({ nodes, edges, diagnostics, nodeRuns = [], onChangeNodes, onChangeEdges, roles, disabled = false }) => {
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const [selectedEdge, setSelectedEdge] = useState<string | null>(null);
  const [source, setSource] = useState<{ node: string; port: string } | null>(null);
  const [drag, setDrag] = useState<{ id: string; startX: number; startY: number; x: number; y: number } | null>(null);
  const surface = useRef<HTMLDivElement>(null);
  const node = nodes.find((item) => item.node_id === selectedNode);
  const edge = edges.find((item) => item.edge_id === selectedEdge);
  const nodeIssues = (id: string) => diagnostics.filter((item) => item.node_id === id && item.severity === 'error');
  const runFor = (id: string) => nodeRuns.find((item) => item.node_id === id);
  useEffect(() => { if (source && !hasOutputPort(nodes, source)) setSource(null); }, [nodes, source]);
  const updateNode = (id: string, patch: Partial<Node>) => onChangeNodes(nodes.map((item) => item.node_id === id ? { ...item, ...patch } : item));
  const updateEdge = (id: string, patch: Partial<Edge>) => onChangeEdges(edges.map((item) => item.edge_id === id ? { ...item, ...patch } : item));
  const add = (kind: Kind) => { const created = newNode(kind, nodes.length); onChangeNodes([...nodes, created]); setSelectedNode(created.node_id); setSelectedEdge(null); };
  const connect = (target: Node, port: string) => {
    if (disabled || !hasOutputPort(nodes, source) || !source || source.node === target.node_id) return;
    if (edges.some((item) => item.source_node === source.node && item.source_port === source.port && item.target_node === target.node_id && item.target_port === port)) return;
    const created: Edge = { edge_id: `edge_${crypto.randomUUID().slice(0, 8)}`, source_node: source.node, source_port: source.port, target_node: target.node_id, target_port: port, delivery_mode: 'value' };
    onChangeEdges([...edges, created]); setSource(null); setSelectedEdge(created.edge_id); setSelectedNode(null);
  };
  const onMove = (event: React.PointerEvent) => {
    if (!drag || disabled) return;
    const x = Math.max(0, Math.min(size.width - width, drag.x + event.clientX - drag.startX));
    const y = Math.max(0, Math.min(size.height - height, drag.y + event.clientY - drag.startY));
    updateNode(drag.id, { metadata: { ...nodes.find((item) => item.node_id === drag.id)?.metadata, canvas_position: { x, y } } });
  };
  const remove = () => {
    if (selectedNode) { onChangeNodes(nodes.filter((item) => item.node_id !== selectedNode)); onChangeEdges(edges.filter((item) => item.source_node !== selectedNode && item.target_node !== selectedNode)); if (source?.node === selectedNode) setSource(null); setSelectedNode(null); }
    if (selectedEdge) { onChangeEdges(edges.filter((item) => item.edge_id !== selectedEdge)); setSelectedEdge(null); }
  };
  return <div className="livegraph-editor">
    <div className="livegraph-palette" aria-label="添加节点">{kinds.map(({ kind, label, icon: Icon }) => <button type="button" key={kind} className="btn btn-secondary btn-sm" onClick={() => add(kind)} disabled={disabled}><Icon size={14} aria-hidden="true" />{label}<Plus size={12} aria-hidden="true" /></button>)}</div>
    <p className="b24-field-help">拖动卡片调整位置；先点输出端口，再点目标输入端口连线。端口和参数在右侧编辑。</p>
    <div className="livegraph-scroll"><div className="livegraph-surface" ref={surface} style={{ width: size.width, height: size.height }} onPointerMove={onMove} onPointerUp={() => setDrag(null)} onPointerCancel={() => setDrag(null)} onKeyDown={(event) => { if ((event.key === 'Delete' || event.key === 'Backspace') && event.target === event.currentTarget && !disabled) remove(); }} tabIndex={0} aria-label="工作流画布">
      <svg className="livegraph-edges" viewBox={`0 0 ${size.width} ${size.height}`} aria-hidden="true">{edges.map((item) => { const a = nodes.findIndex((n) => n.node_id === item.source_node); const b = nodes.findIndex((n) => n.node_id === item.target_node); if (a < 0 || b < 0) return null; const start = nodePosition(nodes[a], a); const end = nodePosition(nodes[b], b); return <path key={item.edge_id} d={`M ${start.x + width} ${start.y + 50} C ${start.x + width + 90} ${start.y + 50}, ${end.x - 90} ${end.y + 50}, ${end.x} ${end.y + 50}`} className={selectedEdge === item.edge_id ? 'selected' : ''} />; })}</svg>
      {edges.map((item) => { const a = nodes.findIndex((n) => n.node_id === item.source_node); const b = nodes.findIndex((n) => n.node_id === item.target_node); if (a < 0 || b < 0) return null; const start = nodePosition(nodes[a], a); const end = nodePosition(nodes[b], b); return <button key={item.edge_id} type="button" className={`livegraph-edge-hit${selectedEdge === item.edge_id ? ' selected' : ''}`} style={{ left: (start.x + width + end.x) / 2 - 12, top: (start.y + end.y) / 2 + 38 }} onClick={() => { setSelectedEdge(item.edge_id); setSelectedNode(null); }} aria-label={`编辑连线 ${item.source_node}.${item.source_port} 到 ${item.target_node}.${item.target_port}`} title={`${item.source_port} → ${item.target_port}`} />; })}
      {nodes.map((item, index) => { const pos = nodePosition(item, index); const run = runFor(item.node_id); return <article key={item.node_id} className={`livegraph-node${selectedNode === item.node_id ? ' selected' : ''}${nodeIssues(item.node_id).length ? ' invalid' : ''}`} style={{ left: pos.x, top: pos.y, width }}><div className="livegraph-node-head" onPointerDown={(event) => { if (disabled) return; event.currentTarget.setPointerCapture(event.pointerId); setDrag({ id: item.node_id, startX: event.clientX, startY: event.clientY, x: pos.x, y: pos.y }); setSelectedNode(item.node_id); setSelectedEdge(null); }} onPointerMove={onMove} onPointerUp={() => setDrag(null)}><button type="button" onClick={() => { setSelectedNode(item.node_id); setSelectedEdge(null); }} aria-label={`配置节点 ${item.node_id}`}>{item.metadata?.label && typeof item.metadata.label === 'string' ? item.metadata.label : item.node_id}</button><small>{item.node_kind}</small></div><div className="livegraph-node-ports"><div>{(item.input_ports || []).map((port) => <button type="button" key={port.name} className="livegraph-port" onClick={() => connect(item, port.name)} aria-label={`${item.node_id} 输入 ${port.name}`}>◉ {port.name}</button>)}</div><div>{(item.output_ports || []).map((port) => <button type="button" key={port.name} className={`livegraph-port${source?.node === item.node_id && source.port === port.name ? ' active' : ''}`} onClick={() => { if (!disabled) setSource({ node: item.node_id, port: port.name }); }} aria-label={`${item.node_id} 输出 ${port.name}`}>{port.name} ◉</button>)}</div></div>{run && <span className="livegraph-run-status">{run.status} · rev {run.revision}</span>}{nodeIssues(item.node_id).length > 0 && <span className="livegraph-run-status" role="alert">{nodeIssues(item.node_id).length} 个校验错误</span>}</article>; })}
    </div></div>
    {source && <div className="b24-actions"><span className="b24-field-help">正在从 {source.node}.{source.port} 连线</span><button type="button" className="btn btn-secondary btn-sm" onClick={() => setSource(null)}>取消连线</button></div>}
    {(node || edge) && <div className="livegraph-inspector"><div className="b24-card-heading"><strong>{node ? '节点参数' : '连线与参数绑定'}</strong><button type="button" className="btn btn-danger btn-sm" onClick={remove} disabled={disabled}><Trash2 size={13} aria-hidden="true" />删除</button></div>{node && <NodeFields node={node} nodes={nodes} roles={roles} disabled={disabled} update={(patch) => updateNode(node.node_id, patch)} />}{edge && <div className="livegraph-fields"><label>来源端口<select value={edge.source_port} onChange={(event) => updateEdge(edge.edge_id, { source_port: event.target.value })} disabled={disabled}>{nodes.find((n) => n.node_id === edge.source_node)?.output_ports?.map((port) => <option key={port.name} value={port.name}>{port.name}</option>)}</select></label><label>目标端口<select value={edge.target_port} onChange={(event) => updateEdge(edge.edge_id, { target_port: event.target.value })} disabled={disabled}>{nodes.find((n) => n.node_id === edge.target_node)?.input_ports?.map((port) => <option key={port.name} value={port.name}>{port.name}</option>)}</select></label><label>交付方式<select value={edge.delivery_mode || 'value'} onChange={(event) => updateEdge(edge.edge_id, { delivery_mode: event.target.value as Edge['delivery_mode'] })} disabled={disabled}><option value="value">值</option><option value="reference">引用</option></select></label><label>条件表达式<input value={edge.condition || ''} onChange={(event) => updateEdge(edge.edge_id, { condition: event.target.value || null })} disabled={disabled} /></label><label>汇合模式<select value={edge.join_mode || 'all'} onChange={(event) => updateEdge(edge.edge_id, { join_mode: event.target.value as Edge['join_mode'] })} disabled={disabled}><option value="all">全部</option><option value="any">任一</option></select></label><label><input type="checkbox" checked={Boolean(edge.loop_back)} onChange={(event) => updateEdge(edge.edge_id, { loop_back: event.target.checked })} disabled={disabled} />循环回边</label></div>}</div>}
  </div>;
};
