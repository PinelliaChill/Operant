import React, { useEffect, useLayoutEffect, useRef, useState } from 'react';
import {
  CircleStop,
  Play,
  RefreshCw,
  RotateCcw,
  Send,
  ShieldAlert,
} from 'lucide-react';
import type { Phase23 } from '@operant/sdk';
import { useOperant } from '../../context/ClientContext';
import {
  consumeIteratorForEpoch,
  cursorForScope,
  eventsForScope,
  graphRunIsTerminal,
  IdempotencyKeyRegistry,
  emptyRuntimeEventState,
  reduceRuntimeEvent,
} from '../../live23/runtimeState';
import { mapRuntimeFrame } from '../../live23/phase23Adapter';
import {
  loadMultiWriterProjection,
  type MergeRunView,
  type WriterArtifactView,
  type WriterConflictView,
  type WriterWorkspaceView,
} from '../../live23/multiwriterAdapter';
import './live-graph-team.css';

type LiveTab = 'home' | 'canvas' | 'runs';

function message(error: unknown): string {
  return error instanceof Error ? error.message : 'Core 请求失败，请检查本地服务后重试。';
}

export const LiveGraphTeamView: React.FC<{ activeTab: LiveTab }> = ({ activeTab }) => {
  const { phase23Client, connectionStatus, activeWorkspace, addNotification } = useOperant();
  const keys = useRef(new IdempotencyKeyRegistry());
  const streamEpoch = useRef(0);
  const graphScopeEpoch = useRef(0);
  const graphQueryEpoch = useRef(0);
  const teamScopeEpoch = useRef(0);
  const teamQueryEpoch = useRef(0);
  const workspaceEpoch = useRef(0);
  const previousWorkspace = useRef(activeWorkspace);
  const busyEpoch = useRef(0);
  const streamIterator = useRef<AsyncIterator<Phase23.SseFrame> | null>(null);
  const [workflowId, setWorkflowId] = useState('builtin.coding-review');
  const [workflowVersion, setWorkflowVersion] = useState(1);
  const [definition, setDefinition] = useState<Phase23.WorkflowDefinition | null>(null);
  const [legacyWorkflowRunId, setLegacyWorkflowRunId] = useState('');
  const [graphRunId, setGraphRunId] = useState('');
  const [graphRun, setGraphRun] = useState<Phase23.GraphWorkflowRun | null>(null);
  const [nodeRuns, setNodeRuns] = useState<Phase23.NodeRun[]>([]);
  const [writerWorkspaces, setWriterWorkspaces] = useState<WriterWorkspaceView[]>([]);
  const [writerArtifacts, setWriterArtifacts] = useState<WriterArtifactView[]>([]);
  const [writerConflicts, setWriterConflicts] = useState<WriterConflictView[]>([]);
  const [mergeRuns, setMergeRuns] = useState<MergeRunView[]>([]);
  const [events, setEvents] = useState(emptyRuntimeEventState);
  const [streamStatus, setStreamStatus] = useState<'idle' | 'connecting' | 'live' | 'disconnected'>('idle');
  const [teamRunId, setTeamRunId] = useState('');
  const [agentId, setAgentId] = useState('');
  const [senderId, setSenderId] = useState('');
  const [teamRun, setTeamRun] = useState<Phase23.TeamRunProjection | null>(null);
  const [messages, setMessages] = useState<Phase23.TeamMessage[]>([]);
  const [mailbox, setMailbox] = useState<Phase23.MailboxDelivery[]>([]);
  const [tasks, setTasks] = useState<Phase23.TeamTask[]>([]);
  const [artifacts, setArtifacts] = useState<Phase23.ArtifactBoardItem[]>([]);
  const [outgoing, setOutgoing] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async (operation: () => Promise<unknown>, isCurrent = () => true) => {
    const epoch = ++busyEpoch.current;
    setBusy(true);
    setError(null);
    try {
      await operation();
    } catch (caught) {
      if (epoch === busyEpoch.current && isCurrent()) setError(message(caught));
    } finally {
      if (epoch === busyEpoch.current && isCurrent()) setBusy(false);
    }
  };

  const refreshGraph = async (
    runId = graphRunId,
    expectedScopeEpoch = graphScopeEpoch.current,
    isCurrent = () => true,
  ) => {
    const normalizedRunId = runId.trim();
    if (!normalizedRunId) throw new Error('请先输入 Graph Run ID。');
    const epoch = ++graphQueryEpoch.current;
    const [nextRun, nextNodes, multiWriter] = await Promise.all([
      phase23Client.getGraphRun(normalizedRunId),
      phase23Client.listNodeRuns(normalizedRunId),
      loadMultiWriterProjection(normalizedRunId),
    ]);
    if (
      epoch !== graphQueryEpoch.current
      || expectedScopeEpoch !== graphScopeEpoch.current
      || !isCurrent()
    ) return null;
    setGraphRun(nextRun);
    setNodeRuns(nextNodes);
    setWriterWorkspaces(multiWriter.workspaces);
    setWriterArtifacts(multiWriter.artifacts);
    setWriterConflicts(multiWriter.conflicts);
    setMergeRuns(multiWriter.merges);
    return nextRun;
  };

  const loadDefinition = () => {
    const expectedWorkspaceEpoch = workspaceEpoch.current;
    return run(async () => {
      const nextDefinition = await phase23Client.getWorkflowDefinition(workflowId.trim(), workflowVersion);
      if (expectedWorkspaceEpoch !== workspaceEpoch.current) return;
      setDefinition(nextDefinition);
    }, () => expectedWorkspaceEpoch === workspaceEpoch.current);
  };

  const safelyReturnIterator = (iterator: AsyncIterator<Phase23.SseFrame> | null) => {
    if (!iterator?.return) return;
    void Promise.resolve(iterator.return()).catch(() => undefined);
  };

  const stopStream = () => {
    streamEpoch.current += 1;
    safelyReturnIterator(streamIterator.current);
    streamIterator.current = null;
    setStreamStatus('idle');
  };

  const invalidateGraphProjection = ({
    clearRunId = false,
    clearEvents = false,
    cancelPending = false,
  } = {}) => {
    stopStream();
    graphScopeEpoch.current += 1;
    graphQueryEpoch.current += 1;
    if (cancelPending) {
      busyEpoch.current += 1;
      setBusy(false);
      setError(null);
    }
    if (clearRunId) setGraphRunId('');
    if (clearEvents) setEvents(emptyRuntimeEventState());
    setGraphRun(null);
    setNodeRuns([]);
    setWriterWorkspaces([]);
    setWriterArtifacts([]);
    setWriterConflicts([]);
    setMergeRuns([]);
    return graphScopeEpoch.current;
  };

  const resolveLegacyWorkflow = () => {
    const expectedScopeEpoch = invalidateGraphProjection({ clearRunId: true });
    return run(async () => {
      if (!legacyWorkflowRunId.trim()) throw new Error('请先输入旧 Coding Workflow Run ID。');
      const linked = await phase23Client.getGraphRunByLegacyWorkflow(legacyWorkflowRunId.trim());
      const linkedNodes = await phase23Client.listNodeRuns(linked.id);
      if (expectedScopeEpoch !== graphScopeEpoch.current) return;
      setGraphRunId(linked.id);
      setGraphRun(linked);
      setNodeRuns(linkedNodes);
    }, () => expectedScopeEpoch === graphScopeEpoch.current);
  };

  const startGraph = () => {
    const expectedScopeEpoch = invalidateGraphProjection({ clearRunId: true });
    const expectedWorkspaceEpoch = workspaceEpoch.current;
    const logical = `start-graph:${workflowId}:${workflowVersion}:${activeWorkspace}`;
    const isCurrent = () => (
      expectedScopeEpoch === graphScopeEpoch.current
      && expectedWorkspaceEpoch === workspaceEpoch.current
    );
    return run(async () => {
      const receipt = await phase23Client.startGraphRun(
        {
          workflow_id: workflowId.trim(),
          definition_version: workflowVersion,
          workspace_or_target: activeWorkspace,
        },
        { idempotencyKey: keys.current.get(logical) },
      );
      if (!receipt.resource_id) throw new Error('Core 未返回 Graph Run ID。');
      keys.current.release(logical);
      if (!isCurrent()) return;
      setGraphRunId(receipt.resource_id);
      await refreshGraph(receipt.resource_id, expectedScopeEpoch, isCurrent);
      if (!isCurrent()) return;
      addNotification('success', `Graph Run 已创建：${receipt.resource_id}`);
    }, isCurrent);
  };

  const changeGraphRunId = (value: string) => {
    if (value !== graphRunId) {
      invalidateGraphProjection({ cancelPending: true });
    }
    setGraphRunId(value);
  };

  const requestGraphRefresh = () => {
    const expectedScopeEpoch = graphScopeEpoch.current;
    return run(
      () => refreshGraph(graphRunId, expectedScopeEpoch),
      () => expectedScopeEpoch === graphScopeEpoch.current,
    );
  };

  const clearTeamProjection = (cancelPending = false, clearRunId = false) => {
    teamScopeEpoch.current += 1;
    teamQueryEpoch.current += 1;
    if (cancelPending) {
      busyEpoch.current += 1;
      setBusy(false);
      setError(null);
    }
    if (clearRunId) setTeamRunId('');
    setTeamRun(null);
    setMessages([]);
    setMailbox([]);
    setTasks([]);
    setArtifacts([]);
    return teamScopeEpoch.current;
  };

  useLayoutEffect(() => {
    if (previousWorkspace.current === activeWorkspace) return;
    previousWorkspace.current = activeWorkspace;
    workspaceEpoch.current += 1;
    invalidateGraphProjection({ clearRunId: true, clearEvents: true, cancelPending: true });
    clearTeamProjection(true, true);
    setDefinition(null);
  }, [activeWorkspace]);

  useEffect(() => () => {
    streamEpoch.current += 1;
    graphScopeEpoch.current += 1;
    graphQueryEpoch.current += 1;
    teamScopeEpoch.current += 1;
    teamQueryEpoch.current += 1;
    busyEpoch.current += 1;
    safelyReturnIterator(streamIterator.current);
    streamIterator.current = null;
  }, []);

  const monitorGraph = async () => {
    setError(null);
    const monitoredRunId = graphRunId.trim();
    const expectedScopeEpoch = graphScopeEpoch.current;
    let epoch = streamEpoch.current;
    let iterator: AsyncIterator<Phase23.SseFrame> | null = null;
    try {
      if (!monitoredRunId) throw new Error('请先输入 Graph Run ID。');
      stopStream();
      epoch = ++streamEpoch.current;
      const isCurrent = () => (
        epoch === streamEpoch.current
        && expectedScopeEpoch === graphScopeEpoch.current
      );
      setStreamStatus('connecting');
      const resourceScope = `graph_run:${monitoredRunId}`;
      const stream = await phase23Client.streamGraphRunEvents(monitoredRunId, {
        lastEventId: cursorForScope(events, resourceScope),
      });
      iterator = stream.events[Symbol.asyncIterator]();
      if (!isCurrent()) {
        safelyReturnIterator(iterator);
        return;
      }
      streamIterator.current = iterator;
      setStreamStatus('live');
      const completion = await consumeIteratorForEpoch(iterator, isCurrent, (frame) => {
        setEvents((current) => reduceRuntimeEvent(
          current,
          mapRuntimeFrame(frame, resourceScope),
        ));
      });
      if (streamIterator.current === iterator) streamIterator.current = null;
      if (completion === 'stale' || !isCurrent()) return;
      const correctedRun = await refreshGraph(
        monitoredRunId,
        expectedScopeEpoch,
        isCurrent,
      );
      if (correctedRun && graphRunIsTerminal(correctedRun.status)) {
        setStreamStatus('idle');
      } else {
        setStreamStatus('disconnected');
        setError('事件流已结束，但 Graph Run 尚未终止。已保留 Cursor，可点击“从 Cursor 重连”。');
      }
    } catch (caught) {
      if (epoch !== streamEpoch.current || expectedScopeEpoch !== graphScopeEpoch.current) return;
      if (streamIterator.current === iterator) streamIterator.current = null;
      setStreamStatus('disconnected');
      setError(`${message(caught)} 已保留 Cursor，点击“从 Cursor 重连”继续。`);
    }
  };

  const refreshTeamProjection = async (expectedScopeEpoch = teamScopeEpoch.current) => {
    const normalizedRunId = teamRunId.trim();
    const viewerId = agentId.trim();
    if (!normalizedRunId || !viewerId) {
      throw new Error('请填写 Team Run ID 和当前 Agent ID。');
    }
    const epoch = ++teamQueryEpoch.current;
    const [nextRun, nextMessages, nextMailbox, nextTasks, nextArtifacts] = await Promise.all([
      phase23Client.getTeamRun(normalizedRunId),
      phase23Client.listTeamMessages(normalizedRunId, { viewerId }),
      phase23Client.getMailbox(normalizedRunId, viewerId),
      phase23Client.getTaskBoard(normalizedRunId),
      phase23Client.getArtifactBoard(normalizedRunId, { viewerId }),
    ]);
    if (epoch !== teamQueryEpoch.current || expectedScopeEpoch !== teamScopeEpoch.current) return;
    setTeamRun(nextRun);
    setMessages(nextMessages);
    setMailbox(nextMailbox.deliveries);
    setTasks(nextTasks.tasks);
    setArtifacts(nextArtifacts.artifacts);
  };

  const refreshTeam = () => {
    const expectedScopeEpoch = teamScopeEpoch.current;
    return run(
      () => refreshTeamProjection(expectedScopeEpoch),
      () => expectedScopeEpoch === teamScopeEpoch.current,
    );
  };

  const acknowledge = (deliveryId: string) => {
    const expectedScopeEpoch = teamScopeEpoch.current;
    const logical = `ack:${teamRunId}:${agentId}:${deliveryId}`;
    return run(async () => {
      await phase23Client.acknowledgeMailboxDelivery(
        teamRunId.trim(),
        agentId.trim(),
        deliveryId,
        { idempotencyKey: keys.current.get(logical) },
      );
      keys.current.release(logical);
      if (expectedScopeEpoch !== teamScopeEpoch.current) return;
      await refreshTeamProjection(expectedScopeEpoch);
    }, () => expectedScopeEpoch === teamScopeEpoch.current);
  };

  const sendMessage = () => {
    const expectedScopeEpoch = teamScopeEpoch.current;
    const sentText = outgoing.trim();
    const logical = `message:${teamRunId}:${senderId}:${agentId}:${outgoing}`;
    return run(async () => {
      if (!sentText || !senderId.trim() || !agentId.trim()) {
        throw new Error('发送方、接收方和消息内容不能为空。');
      }
      await phase23Client.sendTeamMessage(
        teamRunId.trim(),
        {
          sender_id: senderId.trim(),
          recipient_ids: [agentId.trim()],
          message_kind: 'StatusUpdate',
          payload: { text: sentText },
        },
        { idempotencyKey: keys.current.get(logical) },
      );
      keys.current.release(logical);
      if (expectedScopeEpoch !== teamScopeEpoch.current) return;
      setOutgoing((current) => current.trim() === sentText ? '' : current);
      await refreshTeamProjection(expectedScopeEpoch);
    }, () => expectedScopeEpoch === teamScopeEpoch.current);
  };

  const status = connectionStatus === 'connected' ? '已连接本地 Core' : '本地 Core 未连接';
  const graphScope = `graph_run:${graphRunId.trim()}`;
  const scopedGraphEvents = eventsForScope(events, graphScope);
  return (
    <div className="section-scroll">
      <div className="section-inner live23-shell">
        <div className="live23-status" role="status" aria-live="polite">
          <span className={`status-dot ${connectionStatus === 'connected' ? 'active' : ''}`} />
          <span>{status}</span>
          <span className="live23-muted">Phase 2/3 使用真实 Client；服务器投影是状态权威。</span>
        </div>
        {error && (
          <div className="live23-error" role="alert">
            <ShieldAlert size={16} aria-hidden="true" />
            <span>{error}</span>
          </div>
        )}

        {activeTab === 'canvas' && (
          <section className="live23-card" aria-labelledby="live-graph-definition-title">
            <h2 id="live-graph-definition-title">Graph 定义</h2>
            <div className="live23-form-grid">
              <label>Workflow ID<input value={workflowId} onChange={(event) => setWorkflowId(event.target.value)} /></label>
              <label>发布版本<input type="number" min={1} value={workflowVersion} onChange={(event) => setWorkflowVersion(Number(event.target.value))} /></label>
            </div>
            <div className="live23-actions">
              <button className="btn btn-secondary" disabled={busy} onClick={loadDefinition}><RefreshCw size={14} aria-hidden="true" />读取定义</button>
              <button className="btn btn-primary" disabled={busy} onClick={startGraph}><Play size={14} aria-hidden="true" />启动 Graph Run</button>
            </div>
            {definition && (
              <div className="live23-definition">
                <strong>{definition.name} · v{definition.version}</strong>
                <span>{definition.nodes.length} 节点 · {definition.edges?.length ?? 0} 连线</span>
                <ol>{definition.nodes.map((node) => <li key={node.node_id}>{node.node_id} · {node.node_kind}</li>)}</ol>
              </div>
            )}
          </section>
        )}

        {activeTab === 'runs' && (
          <section className="live23-card" aria-labelledby="live-run-title">
            <h2 id="live-run-title">Graph 运行监控</h2>
            <div className="live23-form-grid">
              <label>Graph Run ID<input value={graphRunId} onChange={(event) => changeGraphRunId(event.target.value)} /></label>
              <label>旧 Coding Workflow Run ID<input value={legacyWorkflowRunId} onChange={(event) => setLegacyWorkflowRunId(event.target.value)} /></label>
            </div>
            <div className="live23-actions">
              <button className="btn btn-secondary" disabled={busy} onClick={resolveLegacyWorkflow}><RefreshCw size={14} aria-hidden="true" />解析旧 Workflow</button>
              <button className="btn btn-secondary" disabled={busy} onClick={requestGraphRefresh}><RefreshCw size={14} aria-hidden="true" />刷新投影</button>
              <button className="btn btn-primary" disabled={busy || streamStatus === 'live'} onClick={monitorGraph}><RotateCcw size={14} aria-hidden="true" />{streamStatus === 'disconnected' ? '从 Cursor 重连' : '监控事件'}</button>
              <button className="btn btn-secondary" disabled={streamStatus === 'idle'} onClick={stopStream}><CircleStop size={14} aria-hidden="true" />停止监控</button>
            </div>
            <p className="live23-muted">流状态：{streamStatus} · Cursor {cursorForScope(events, graphScope).toString()}</p>
            {graphRun && <p><strong>{graphRun.status}</strong> · revision {graphRun.revision} · 当前节点 {graphRun.current_node_ids.join(', ') || '无'}</p>}
            <div className="live23-grid">
              <div><h3>节点</h3><ul>{nodeRuns.map((node) => <li key={node.id}><span>{node.node_id}</span><strong>{node.status}</strong></li>)}</ul></div>
              <div><h3>最近事件</h3><ul>{scopedGraphEvents.slice().reverse().map((event) => <li key={`${event.resourceScope}:${event.id}`}><span>{event.eventType}</span><strong>{event.cursor?.toString() ?? '—'}</strong></li>)}</ul></div>
            </div>
            <div className="live23-grid live23-writer-grid" aria-label="多 Writer 隔离与合并状态">
              <div>
                <h3>Writer Workspace / Lease</h3>
                <ul>{writerWorkspaces.map((workspace) => (
                  <li key={workspace.writerWorkspaceId}>
                    <span>{workspace.writerKey} · {workspace.isolationKind}<small>{workspace.isolationRef}<br />{workspace.ownershipPaths.join(', ')}</small></span>
                    <strong>{workspace.lease === null ? '无 Lease' : workspace.lease.releasedAt ? '已释放' : `fence ${workspace.lease.fencing}`}</strong>
                  </li>
                ))}</ul>
              </div>
              <div>
                <h3>Patch / Commit Artifact</h3>
                <ul>{writerArtifacts.map((artifact) => (
                  <li key={artifact.writerArtifactId}>
                    <span>{artifact.artifactKind}<small>{artifact.changedPaths.join(', ')}</small></span>
                    <strong>{artifact.testEvidenceRefs.length} 份测试证据</strong>
                  </li>
                ))}</ul>
              </div>
              <div>
                <h3>Conflict</h3>
                <ul>{writerConflicts.map((conflict) => (
                  <li key={conflict.conflictId}>
                    <span>{conflict.paths.join(', ')}</span><strong>{conflict.status}</strong>
                  </li>
                ))}</ul>
              </div>
              <div>
                <h3>Merge Node</h3>
                <ul>{mergeRuns.map((merge) => (
                  <li key={merge.mergeRunId}>
                    <span>{merge.mergeNodeId}<small>{merge.resultArtifactRef ?? merge.errorCode ?? '等待结果'}</small></span>
                    <strong>{merge.status}</strong>
                  </li>
                ))}</ul>
              </div>
            </div>
          </section>
        )}

        {activeTab === 'home' && (
          <section className="live23-card" aria-labelledby="live-team-title">
            <h2 id="live-team-title">本地 Team</h2>
            <div className="live23-form-grid">
              <label>Team Run ID<input value={teamRunId} onChange={(event) => { clearTeamProjection(true); setTeamRunId(event.target.value); }} /></label>
              <label>当前 Agent ID<input value={agentId} onChange={(event) => { clearTeamProjection(true); setAgentId(event.target.value); }} /></label>
              <label>发送方 Agent ID<input value={senderId} onChange={(event) => setSenderId(event.target.value)} /></label>
            </div>
            <div className="live23-actions"><button className="btn btn-primary" disabled={busy} onClick={refreshTeam}><RefreshCw size={14} aria-hidden="true" />刷新 Team 投影</button></div>
            {teamRun && <p><strong>{teamRun.status}</strong> · {teamRun.roster.length} 名成员 · Workflow {teamRun.workflow_run_id}</p>}
            <div className="live23-grid">
              <div><h3>Mailbox</h3><ul>{mailbox.map((delivery) => <li key={delivery.delivery_id}><span>{delivery.message.message_kind} · {delivery.status}</span>{delivery.status !== 'acked' && <button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => acknowledge(delivery.delivery_id)}>Ack</button>}</li>)}</ul></div>
              <div><h3>Task / Artifact Board</h3><ul>{tasks.map((task) => <li key={task.task_id}><span>{task.title}</span><strong>{task.status}</strong></li>)}{artifacts.map((artifact) => <li key={artifact.artifact_id}><span>{artifact.title}</span><strong>{artifact.media_type}</strong></li>)}</ul></div>
            </div>
            <label>发给当前 Agent 的状态消息<textarea rows={3} value={outgoing} onChange={(event) => setOutgoing(event.target.value)} /></label>
            <div className="live23-actions"><button className="btn btn-primary" disabled={busy} onClick={sendMessage}><Send size={14} aria-hidden="true" />发送持久消息</button></div>
            <p className="live23-muted">时间线消息：{messages.length}。Ack 使用同一逻辑动作的幂等键，成功后才释放。</p>
          </section>
        )}
      </div>
    </div>
  );
};
