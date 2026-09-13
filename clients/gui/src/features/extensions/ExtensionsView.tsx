/**
 * 插件与MCP中心（v8：MCP Server 与插件配置）
 * - 支持 Model Context Protocol (MCP) Server (stdio / sse) 与系统插件管理；
 * - 页头「+ 添加 插件/MCP」弹窗：配置服务名称、协议模式、启动命令或 SSE 端点；
 * - 卡片列表：展示 MCP 状态、可用工具数、启动命令/URL、版本与启停开关；
 * - 提供「测试连接」交互，验证 MCP 进程/端点健康度。
 */

import React, { useEffect, useRef, useState } from 'react';
import {
  Puzzle,
  Server,
  Plus,
  Terminal,
  Globe,
  RefreshCw,
  Wrench,
  Play,
  Square,
  Trash2,
  AlertTriangle,
} from 'lucide-react';
import { EmptyState } from '../../components/EmptyState';
import { Modal } from '../../components/Modal';
import { StatusBadge } from '../../components/StatusBadge';
import { useOperant } from '../../context/ClientContext';
import { useDemo } from '../../demo/DemoContext';
import { Link, useSearchParams } from 'react-router-dom';
import type { DemoExtension } from '../../demo/types';
import { usePhase45 } from '../../live45/Phase45Context';
import {
  mcpActionState,
  phase45SideEffectsDisabled,
  phase45UnavailableMessage,
} from '../../live45/phase45State';
import { buildMcpServerRequest } from '../../live45/mcpForm';
import { McpSafetyPanels } from './McpSafetyPanels';
import './live-extensions.css';
import { LiveManagementView } from '../management/LiveManagementView';

export const ExtensionsView: React.FC = () => {
  const { clientMode } = useOperant();
  const [searchParams] = useSearchParams();
  if (clientMode === 'live' && searchParams.get('tab') === 'plugins') {
    return <LiveManagementView initialTab="plugins" />;
  }
  return clientMode === 'live' ? <LiveExtensionsView /> : <DemoExtensionsView />;
};

const DemoExtensionsView: React.FC = () => {
  const { extensions, toggleExtension, createExtension } = useDemo();
  const { addNotification } = useOperant();

  const [modalOpen, setModalOpen] = useState(false);
  const [name, setName] = useState('');
  const [desc, setDesc] = useState('');
  const [type, setType] = useState<'mcp' | 'plugin'>('mcp');
  const [protocol, setProtocol] = useState<'stdio' | 'sse'>('stdio');
  const [command, setCommand] = useState('');
  const [endpoint, setEndpoint] = useState('');
  const [version, setVersion] = useState('1.0.0');

  const [testingId, setTestingId] = useState<string | null>(null);

  const handleToggle = (id: string, extName: string, enabled: boolean) => {
    toggleExtension(id);
    addNotification(
      'success',
      enabled ? `已停用「${extName}」（演示）` : `已启用「${extName}」（演示）`
    );
  };

  const handleTestConnection = (ext: DemoExtension) => {
    setTestingId(ext.id);
    setTimeout(() => {
      setTestingId(null);
      addNotification('success', `MCP 服务「${ext.name}」连接成功，检测到 ${ext.toolsCount ?? 5} 个可用工具。`);
    }, 600);
  };

  const handleCreate = () => {
    if (!name.trim() || !desc.trim()) return;

    createExtension({
      name: name.trim(),
      desc: desc.trim(),
      type,
      protocol: type === 'mcp' ? protocol : undefined,
      command: type === 'mcp' && protocol === 'stdio' ? command.trim() : undefined,
      endpoint: type === 'mcp' && protocol === 'sse' ? endpoint.trim() : undefined,
      toolsCount: type === 'mcp' ? 6 : undefined,
      version: version.trim() || '1.0.0',
      enabled: true,
      status: 'connected',
    });

    setModalOpen(false);
    setName('');
    setDesc('');
    setCommand('');
    setEndpoint('');
  };

  return (
    <div className="section-view">
      <header className="section-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 12 }}>
        <div>
          <h1 className="section-title">插件与MCP</h1>
          <p className="section-sub">
            管理 Model Context Protocol (MCP) 服务与系统插件（共 {extensions.length} 个）
          </p>
        </div>
        <button className="btn btn-primary btn-sm" onClick={() => setModalOpen(true)}>
          <Plus size={13} />
          <span>添加 插件/MCP</span>
        </button>
      </header>

      <div className="section-scroll">
        <div className="section-inner">
          {extensions.length === 0 ? (
            <div className="section-empty-wrap">
              <EmptyState
                icon={Server}
                title="暂无插件或 MCP 服务"
                description="点击上方按钮添加标准 MCP Server (stdio/sse) 或系统插件。"
                action={
                  <button className="btn btn-primary" onClick={() => setModalOpen(true)}>
                    <Plus size={14} />
                    <span>添加 插件/MCP</span>
                  </button>
                }
              />
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              {extensions.map((e) => {
                const isMcp = e.type === 'mcp';
                const isTesting = testingId === e.id;

                return (
                  <div
                    key={e.id}
                    className="card"
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                      padding: '14px 18px',
                      gap: 16,
                      flexWrap: 'wrap',
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12, minWidth: 280, flex: 1 }}>
                      <span
                        style={{
                          width: 34,
                          height: 34,
                          borderRadius: 'var(--radius-sm)',
                          backgroundColor: isMcp ? 'var(--accent-subtle)' : 'var(--bg-subtle)',
                          color: isMcp ? 'var(--accent-action)' : 'var(--text-secondary)',
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'center',
                          flexShrink: 0,
                          marginTop: 2,
                        }}
                      >
                        {isMcp ? <Server size={16} /> : <Puzzle size={16} />}
                      </span>

                      <div style={{ display: 'flex', flexDirection: 'column', gap: 4, minWidth: 0 }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                          <span style={{ fontSize: '14px', fontWeight: 600, color: 'var(--text-primary)' }}>
                            {e.name}
                          </span>
                          <span className="badge" style={{ backgroundColor: 'var(--bg-surface)', fontSize: '10px' }}>
                            {isMcp ? `MCP Server (${e.protocol ?? 'stdio'})` : '系统插件'}
                          </span>
                          <span className="badge" style={{ backgroundColor: 'var(--bg-subtle)', fontSize: '10px', fontFamily: 'var(--font-mono)' }}>
                            v{e.version}
                          </span>
                          {e.status && (
                            <StatusBadge
                              status={e.status === 'connected' ? 'connected' : 'disconnected'}
                              label={e.status === 'connected' ? '已连接' : '未连接'}
                              size="sm"
                            />
                          )}
                        </div>

                        <div style={{ fontSize: '12px', color: 'var(--text-secondary)', lineHeight: 1.4 }}>
                          {e.desc}
                        </div>

                        {isMcp && (
                          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 2, flexWrap: 'wrap' }}>
                            {e.command && (
                              <span style={{ fontFamily: 'var(--font-mono)', fontSize: '11px', color: 'var(--text-muted)' }}>
                                终端启动：<code>{e.command}</code>
                              </span>
                            )}
                            {e.endpoint && (
                              <span style={{ fontFamily: 'var(--font-mono)', fontSize: '11px', color: 'var(--text-muted)' }}>
                                SSE 端点：<code>{e.endpoint}</code>
                              </span>
                            )}
                            {e.toolsCount && (
                              <span className="badge" style={{ backgroundColor: 'var(--bg-surface)', fontSize: '10px' }}>
                                <Wrench size={10} style={{ marginRight: 4 }} />
                                {e.toolsCount} 个工具注册
                              </span>
                            )}
                          </div>
                        )}
                      </div>
                    </div>

                    <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                      {isMcp && (
                        <button
                          type="button"
                          className="btn btn-secondary btn-sm"
                          onClick={() => handleTestConnection(e)}
                          disabled={isTesting || !e.enabled}
                          title="测试 MCP 连接"
                        >
                          <RefreshCw size={12} className={isTesting ? 'animate-spin' : undefined} />
                          <span>测试连接</span>
                        </button>
                      )}

                      <button
                        type="button"
                        role="switch"
                        aria-checked={e.enabled}
                        className="switch"
                        onClick={() => handleToggle(e.id, e.name, e.enabled)}
                        aria-label={e.enabled ? `停用 ${e.name}` : `启用 ${e.name}`}
                      />
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          <p className="section-footnote" style={{ marginTop: 24 }}>
            MCP 服务遵循 Model Context Protocol 标准协议，支持 stdio 子进程管道与 SSE 远程端点接入。
          </p>
        </div>
      </div>

      {/* 新建 插件 / MCP Modal */}
      <Modal
        isOpen={modalOpen}
        onClose={() => setModalOpen(false)}
        title="添加插件或 MCP 服务"
        footer={
          <>
            <button className="btn btn-ghost" onClick={() => setModalOpen(false)}>
              取消
            </button>
            <button className="btn btn-primary" onClick={handleCreate} disabled={!name.trim() || !desc.trim()}>
              <Plus size={14} />
              <span>确认添加</span>
            </button>
          </>
        }
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div>
            <label style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: 6 }}>
              插件 / MCP 类型
            </label>
            <div style={{ display: 'flex', gap: 10 }}>
              <button
                type="button"
                className={`btn btn-sm ${type === 'mcp' ? 'btn-primary' : 'btn-secondary'}`}
                onClick={() => setType('mcp')}
              >
                <Server size={13} />
                <span>Model Context Protocol (MCP)</span>
              </button>
              <button
                type="button"
                className={`btn btn-sm ${type === 'plugin' ? 'btn-primary' : 'btn-secondary'}`}
                onClick={() => setType('plugin')}
              >
                <Puzzle size={13} />
                <span>本地系统插件</span>
              </button>
            </div>
          </div>

          <div>
            <label style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: 4 }}>
              服务 / 插件名称
            </label>
            <input
              type="text"
              className="input"
              placeholder="例如：SQLite Database MCP"
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoFocus
            />
          </div>

          <div>
            <label style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: 4 }}>
              功能描述
            </label>
            <textarea
              className="input"
              rows={2}
              placeholder="描述该 MCP 服务提供的工具与上下文资源…"
              value={desc}
              onChange={(e) => setDesc(e.target.value)}
            />
          </div>

          {type === 'mcp' && (
            <>
              <div>
                <label style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: 6 }}>
                  通信协议
                </label>
                <div style={{ display: 'flex', gap: 10 }}>
                  <button
                    type="button"
                    className={`btn btn-sm ${protocol === 'stdio' ? 'btn-primary' : 'btn-secondary'}`}
                    onClick={() => setProtocol('stdio')}
                  >
                    <Terminal size={13} />
                    <span>stdio 子进程</span>
                  </button>
                  <button
                    type="button"
                    className={`btn btn-sm ${protocol === 'sse' ? 'btn-primary' : 'btn-secondary'}`}
                    onClick={() => setProtocol('sse')}
                  >
                    <Globe size={13} />
                    <span>SSE 远程端点</span>
                  </button>
                </div>
              </div>

              {protocol === 'stdio' ? (
                <div>
                  <label style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: 4 }}>
                    启动命令 (Command)
                  </label>
                  <input
                    type="text"
                    className="input"
                    placeholder="npx -y @modelcontextprotocol/server-..."
                    value={command}
                    onChange={(e) => setCommand(e.target.value)}
                  />
                </div>
              ) : (
                <div>
                  <label style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: 4 }}>
                    SSE 接口 URL (Endpoint)
                  </label>
                  <input
                    type="text"
                    className="input"
                    placeholder="http://localhost:8080/sse"
                    value={endpoint}
                    onChange={(e) => setEndpoint(e.target.value)}
                  />
                </div>
              )}
            </>
          )}

          <div>
            <label style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: 4 }}>
              版本号
            </label>
            <input
              type="text"
              className="input"
              value={version}
              onChange={(e) => setVersion(e.target.value)}
              placeholder="1.0.0"
              style={{ maxWidth: 140 }}
            />
          </div>
        </div>
      </Modal>
    </div>
  );
};

const LiveExtensionsView: React.FC = () => {
  const { connectionStatus } = useOperant();
  const {
    servers, workspaceRoots, toolsByServer, toolResults, mcpIntervention, mcpReceipt,
    phase, error, actionLabel, refresh, createMcpServer, startMcpServer, stopMcpServer,
    deleteMcpServer, loadMcpTools, callMcpTool, decideMcpApproval, loadMcpReceipt,
  } = usePhase45();
  const [modalOpen, setModalOpen] = useState(false);
  const [serverId, setServerId] = useState('');
  const [transport, setTransport] = useState<'stdio' | 'legacy_sse'>('stdio');
  const [stdioArgv, setStdioArgv] = useState('["npx", "-y", "@modelcontextprotocol/server-filesystem"]');
  const [cwdRef, setCwdRef] = useState('.');
  const [workspaceRootRef, setWorkspaceRootRef] = useState('');
  const [dockerImage, setDockerImage] = useState('');
  const [endpointRef, setEndpointRef] = useState('MCP_ENDPOINT');
  const [secretRef, setSecretRef] = useState('MCP_SECRET');
  const [formError, setFormError] = useState('');
  const [toolArguments, setToolArguments] = useState<Record<string, string>>({});
  const [toolError, setToolError] = useState('');
  const formErrorRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!workspaceRootRef && workspaceRoots[0]) setWorkspaceRootRef(workspaceRoots[0].rootRef);
  }, [workspaceRootRef, workspaceRoots]);

  useEffect(() => {
    if (formError) formErrorRef.current?.focus();
  }, [formError]);

  const submit = async () => {
    setFormError('');
    try {
      const request = buildMcpServerRequest({
        serverId, transport, stdioArgv, cwdRef, workspaceRootRef,
        dockerImage, endpointRef, secretRef,
      }, workspaceRoots.map((root) => root.rootRef));
      if (await createMcpServer(request)) { setModalOpen(false); setServerId(''); }
    } catch (value: unknown) { setFormError(value instanceof Error ? value.message : 'MCP 配置无效。'); }
  };

  const invokeTool = async (server: string, tool: string) => {
    setToolError('');
    const key = `${server}\0${tool}`;
    try {
      const parsed = JSON.parse(toolArguments[key] ?? '{}') as unknown;
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('工具参数必须是 JSON 对象。');
      await callMcpTool(server, tool, parsed as Record<string, unknown>);
    } catch (value: unknown) {
      setToolError(value instanceof Error ? value.message : '工具参数无效。');
    }
  };

  const busy = Boolean(actionLabel);
  const sideEffectsDisabled = phase45SideEffectsDisabled(phase, connectionStatus, busy);
  const unavailableMessage = phase45UnavailableMessage(phase, connectionStatus, error?.code);

  if (phase === 'loading' && servers.length === 0) return <div className="live-route-state" role="status"><RefreshCw size={22} aria-hidden="true" /><h1>正在读取 MCP Projection…</h1><p>Live 模式不会用演示插件填充页面。</p></div>;

  return <div className="section-view" data-client-mode="live">
    <header className="section-header mcp-live-header"><div><h1 className="section-title">MCP 服务</h1><p className="section-sub">Phase 5A MCP 配置、审批、调用 Receipt 与生命周期；所有副作用仍由 Action Gateway 裁决。</p></div><div className="mcp-live-actions"><Link className="btn btn-secondary" to="/projects?tab=plugins"><Puzzle size={14} aria-hidden="true" />记忆插件</Link><button type="button" className="btn btn-secondary" onClick={() => void refresh()} disabled={busy}><RefreshCw size={14} aria-hidden="true" />刷新</button><button type="button" className="btn btn-primary" onClick={() => setModalOpen(true)} disabled={sideEffectsDisabled}><Plus size={14} aria-hidden="true" />添加 MCP</button></div></header>
    <div className="section-scroll"><div className="section-inner">
      <div className="mcp-route-alerts" aria-live="polite">{unavailableMessage && <div className="live-alert live-alert-error" role="alert"><AlertTriangle size={16} aria-hidden="true" />{unavailableMessage}</div>}{error && !unavailableMessage && !mcpIntervention && <div className="live-alert live-alert-error" role="alert"><AlertTriangle size={16} aria-hidden="true" />{error.code}：{error.message}</div>}{actionLabel && <div className="live-alert" role="status">{actionLabel}处理中，请等待 Core 确认。</div>}{toolError && <div className="live-alert live-alert-error" role="alert"><AlertTriangle size={16} aria-hidden="true" />{toolError}</div>}</div>
      <McpSafetyPanels
        intervention={mcpIntervention}
        receipt={mcpReceipt}
        busy={busy}
        disconnected={sideEffectsDisabled}
        onDecision={(approved) => void decideMcpApproval(approved)}
        onLoadReceipt={(hash) => void loadMcpReceipt(hash)}
      />
      {servers.length === 0 ? <div className="section-empty-wrap"><EmptyState icon={Server} title="没有 MCP 服务" description="只可创建 Core 支持的 stdio 或 legacy SSE 配置；Live 模式不支持演示插件。" /></div> : <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        {servers.map((server) => { const tools = toolsByServer[server.id] ?? []; const actions = mcpActionState(server.lifecycle, sideEffectsDisabled); return <section key={server.id} className="card mcp-server-card" aria-labelledby={`mcp-${server.id}`}>
          <div className="mcp-server-heading"><div><h2 id={`mcp-${server.id}`}>{server.id}</h2><p><code>{server.transport}</code> · {server.transport === 'stdio' ? `${server.workspaceRootRef ?? 'unknown root'} / ${server.cwdRef ?? '.'} / ${server.stdioArgv.length} 个 argv` : `Endpoint Ref: ${server.endpointRef ?? 'unknown'}`}</p><StatusBadge status={server.lifecycle === 'running' ? 'connected' : server.lifecycle === 'failed' ? 'denied' : 'pending'} label={server.lifecycle} size="sm" /></div>
          <div className="mcp-server-actions">
            {server.lifecycle === 'running' ? <button type="button" className="btn btn-secondary btn-sm" disabled={!actions.canStop} onClick={() => { if (window.confirm(`停止 MCP 服务“${server.id}”？进行中的调用可能失败。`)) void stopMcpServer(server.id); }}><Square size={13} aria-hidden="true" />停止</button> : <button type="button" className="btn btn-primary btn-sm" disabled={!actions.canStart} onClick={() => { if (window.confirm(`启动 MCP 服务“${server.id}”？Core 将先经过 Policy/Approval 裁决。`)) void startMcpServer(server.id); }}><Play size={13} aria-hidden="true" />启动</button>}
            <button type="button" className="btn btn-secondary btn-sm" disabled={!actions.canRefreshTools} onClick={() => void loadMcpTools(server.id)}><Wrench size={13} aria-hidden="true" />工具</button>
            <button type="button" className="btn btn-ghost btn-sm" disabled={!actions.canDelete} onClick={() => { if (window.confirm(`永久删除已停止的 MCP 配置“${server.id}”？此操作不可撤销。`)) void deleteMcpServer(server.id); }}><Trash2 size={13} aria-hidden="true" />删除</button>
          </div></div>
          {tools.length > 0 && <div className="mcp-tool-list" aria-label={`${server.id} 工具列表`}>{tools.map((tool) => {
            const key = `${server.id}\0${tool.name}`;
            const result = toolResults[key];
            return <section className="mcp-tool" key={tool.name} aria-labelledby={`mcp-tool-${server.id}-${tool.name}`}>
              <div><h3 id={`mcp-tool-${server.id}-${tool.name}`}><code>{tool.name}</code></h3>{tool.description && <p>{tool.description}</p>}</div>
              <label htmlFor={`mcp-args-${server.id}-${tool.name}`}>参数（JSON 对象）</label>
              <textarea id={`mcp-args-${server.id}-${tool.name}`} className="input" rows={3} value={toolArguments[key] ?? '{}'} onChange={(event) => setToolArguments((current) => ({ ...current, [key]: event.target.value }))} disabled={!actions.canRefreshTools} />
              <button type="button" className="btn btn-secondary" disabled={!actions.canRefreshTools} onClick={() => void invokeTool(server.id, tool.name)} aria-label={`调用 MCP 工具 ${tool.name}`}>调用工具</button>
              {result !== undefined && <pre className="mcp-tool-result" aria-label={`${tool.name} 调用结果`}>{JSON.stringify(result, null, 2).slice(0, 4000)}</pre>}
            </section>;
          })}</div>}
        </section>; })}
      </div>}
      <p className="section-footnote">客户端不直接执行命令、不解析 Secret；MCP tool call 必须经过 Action Gateway，outcome_unknown 永不自动重放。</p>
    </div></div>
    <Modal isOpen={modalOpen} onClose={() => setModalOpen(false)} title="添加 MCP 服务" footer={<><button type="button" className="btn btn-ghost" onClick={() => setModalOpen(false)}>取消</button><button type="button" className="btn btn-primary" onClick={() => void submit()} disabled={sideEffectsDisabled || !serverId.trim() || (transport === 'stdio' && workspaceRoots.length === 0)}>保存配置</button></>}>
      <div className="mcp-form">
        {formError && <div ref={formErrorRef} className="live-alert live-alert-error mcp-form-error" role="alert" tabIndex={-1}>{formError}</div>}
        <label htmlFor="mcp-server-id">服务 ID</label><input id="mcp-server-id" className="input" value={serverId} onChange={(event) => setServerId(event.target.value)} autoFocus required />
        <label htmlFor="mcp-transport">Transport</label><select id="mcp-transport" className="select" value={transport} onChange={(event) => setTransport(event.target.value as typeof transport)}><option value="stdio">stdio（Docker，无网络）</option><option value="legacy_sse">legacy SSE（引用凭据）</option></select>
        {transport === 'stdio' ? <>
          <label htmlFor="mcp-root-ref">工作区 Root Ref</label>
          <select id="mcp-root-ref" className="select" value={workspaceRootRef} onChange={(event) => setWorkspaceRootRef(event.target.value)} aria-describedby="mcp-root-help" required>
            <option value="" disabled>{workspaceRoots.length ? '请选择 Core Root Ref' : 'Core 未返回可用 Root Ref'}</option>
            {workspaceRoots.map((root) => <option key={root.rootRef} value={root.rootRef}>{root.rootRef}</option>)}
          </select>
          <small id="mcp-root-help">只能选择 Core 返回的引用；客户端不会显示或提交宿主绝对路径。</small>
          <label htmlFor="mcp-cwd-ref">相对工作目录</label><input id="mcp-cwd-ref" className="input" value={cwdRef} onChange={(event) => setCwdRef(event.target.value)} aria-describedby="mcp-cwd-help" required /><small id="mcp-cwd-help">相对于所选 Root Ref，例如 <code>.</code> 或 <code>tools/server</code>；禁止绝对路径和 <code>..</code>。</small>
          <label htmlFor="mcp-docker-image">Docker 镜像 digest</label><input id="mcp-docker-image" className="input" value={dockerImage} onChange={(event) => setDockerImage(event.target.value)} placeholder="registry/image@sha256:…" aria-describedby="mcp-image-help" required /><small id="mcp-image-help">必须固定到 64 位小写 sha256 digest，不接受可变 tag。</small>
          <label htmlFor="mcp-argv">argv（JSON 字符串数组）</label><textarea id="mcp-argv" className="input" rows={4} value={stdioArgv} onChange={(event) => setStdioArgv(event.target.value)} aria-describedby="mcp-argv-help" required /><small id="mcp-argv-help">命令与参数分项传给 Core，不经过 Shell；文件参数应相对于容器工作目录。</small>
        </> : <>
          <label htmlFor="mcp-endpoint-ref">Endpoint Ref</label><input id="mcp-endpoint-ref" className="input" value={endpointRef} onChange={(event) => setEndpointRef(event.target.value)} aria-describedby="mcp-endpoint-help" required /><small id="mcp-endpoint-help">只填环境变量引用名，不填写 URL。</small>
          <label htmlFor="mcp-secret-ref">Secret Ref（可选）</label><input id="mcp-secret-ref" className="input" value={secretRef} onChange={(event) => setSecretRef(event.target.value)} aria-describedby="mcp-secret-help" /><small id="mcp-secret-help">只填环境变量引用名，不填写 Token 或密钥。</small>
        </>}
      </div>
    </Modal>
  </div>;
};
