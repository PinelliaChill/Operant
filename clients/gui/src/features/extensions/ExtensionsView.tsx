/**
 * 插件与MCP中心（v8：MCP Server 与插件配置）
 * - 支持 Model Context Protocol (MCP) Server (stdio / sse) 与系统插件管理；
 * - 页头「+ 添加 插件/MCP」弹窗：配置服务名称、协议模式、启动命令或 SSE 端点；
 * - 卡片列表：展示 MCP 状态、可用工具数、启动命令/URL、版本与启停开关；
 * - 提供「测试连接」交互，验证 MCP 进程/端点健康度。
 */

import React, { useState } from 'react';
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
import type { DemoExtension } from '../../demo/types';
import { usePhase45 } from '../../live45/Phase45Context';
import { mcpActionState } from '../../live45/phase45State';

export const ExtensionsView: React.FC = () => {
  const { clientMode } = useOperant();
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
  const { servers, toolsByServer, phase, error, actionLabel, refresh, createMcpServer, startMcpServer, stopMcpServer, deleteMcpServer, loadMcpTools } = usePhase45();
  const [modalOpen, setModalOpen] = useState(false);
  const [serverId, setServerId] = useState('');
  const [transport, setTransport] = useState<'stdio' | 'legacy_sse'>('stdio');
  const [stdioArgv, setStdioArgv] = useState('["npx", "-y", "@modelcontextprotocol/server-filesystem"]');
  const [endpointRef, setEndpointRef] = useState('MCP_ENDPOINT');
  const [formError, setFormError] = useState('');

  const submit = async () => {
    setFormError('');
    try {
      const request = transport === 'stdio'
        ? { server_id: serverId.trim(), transport, stdio_argv: JSON.parse(stdioArgv) as string[] }
        : { server_id: serverId.trim(), transport, endpoint_ref: endpointRef.trim() };
      if (!request.server_id || (transport === 'stdio' && (!Array.isArray(request.stdio_argv) || request.stdio_argv.length === 0 || request.stdio_argv.some((item) => typeof item !== 'string' || !item)))) throw new Error('请提供服务 ID 和非空 JSON argv 字符串数组。');
      if (transport === 'legacy_sse' && !/^[A-Z][A-Z0-9_]{1,127}$/.test(endpointRef.trim())) throw new Error('Endpoint Ref 必须是环境变量名，不能直接填写 URL。');
      if (await createMcpServer(request)) { setModalOpen(false); setServerId(''); }
    } catch (value: unknown) { setFormError(value instanceof Error ? value.message : 'MCP 配置无效。'); }
  };

  if (phase === 'loading' && servers.length === 0) return <div className="live-route-state" role="status"><RefreshCw size={22} aria-hidden="true" /><h1>正在读取 MCP Projection…</h1><p>Live 模式不会用演示插件填充页面。</p></div>;

  return <div className="section-view" data-client-mode="live">
    <header className="section-header" style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}><div><h1 className="section-title">MCP 服务</h1><p className="section-sub">Phase 5A MCP 配置与生命周期；所有副作用仍由 Action Gateway 裁决。</p></div><div style={{ display: 'flex', gap: 8 }}><button type="button" className="btn btn-secondary btn-sm" onClick={() => void refresh()} disabled={Boolean(actionLabel)}><RefreshCw size={13} aria-hidden="true" />刷新</button><button type="button" className="btn btn-primary btn-sm" onClick={() => setModalOpen(true)}><Plus size={13} aria-hidden="true" />添加 MCP</button></div></header>
    <div className="section-scroll"><div className="section-inner">
      <div aria-live="polite">{connectionStatus !== 'connected' && <div className="live-alert live-alert-error" role="alert"><AlertTriangle size={16} aria-hidden="true" />Core 连接已断开；现有 MCP Projection 可能过期，生命周期操作已禁用。</div>}{error && <div className="live-alert live-alert-error" role="alert"><AlertTriangle size={16} aria-hidden="true" />{error.code}：{error.message}</div>}{actionLabel && <div className="live-alert" role="status">{actionLabel}处理中，请等待 Core 确认。</div>}</div>
      {servers.length === 0 ? <div className="section-empty-wrap"><EmptyState icon={Server} title="没有 MCP 服务" description="只可创建 Core 支持的 stdio 或 legacy SSE 配置；Live 模式不支持演示插件。" /></div> : <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        {servers.map((server) => { const tools = toolsByServer[server.id] ?? []; const actions = mcpActionState(server.lifecycle, Boolean(actionLabel) || connectionStatus !== 'connected'); return <section key={server.id} className="card" style={{ padding: 16 }} aria-labelledby={`mcp-${server.id}`}>
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}><div><h2 id={`mcp-${server.id}`} style={{ margin: 0, fontSize: 15 }}>{server.id}</h2><p><code>{server.transport}</code> · {server.transport === 'stdio' ? `${server.stdioArgv.length} 个 argv 参数` : `Endpoint Ref: ${server.endpointRef}`}</p><StatusBadge status={server.lifecycle === 'running' ? 'connected' : server.lifecycle === 'failed' ? 'denied' : 'pending'} label={server.lifecycle} size="sm" /></div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            {server.lifecycle === 'running' ? <button type="button" className="btn btn-secondary btn-sm" disabled={!actions.canStop} onClick={() => { if (window.confirm(`停止 MCP 服务“${server.id}”？进行中的调用可能失败。`)) void stopMcpServer(server.id); }}><Square size={13} aria-hidden="true" />停止</button> : <button type="button" className="btn btn-primary btn-sm" disabled={!actions.canStart} onClick={() => { if (window.confirm(`启动 MCP 服务“${server.id}”？Core 将先经过 Policy/Approval 裁决。`)) void startMcpServer(server.id); }}><Play size={13} aria-hidden="true" />启动</button>}
            <button type="button" className="btn btn-secondary btn-sm" disabled={!actions.canRefreshTools} onClick={() => void loadMcpTools(server.id)}><Wrench size={13} aria-hidden="true" />工具</button>
            <button type="button" className="btn btn-ghost btn-sm" disabled={!actions.canDelete} onClick={() => { if (window.confirm(`永久删除已停止的 MCP 配置“${server.id}”？此操作不可撤销。`)) void deleteMcpServer(server.id); }}><Trash2 size={13} aria-hidden="true" />删除</button>
          </div></div>
          {tools.length > 0 && <ul aria-label={`${server.id} 工具列表`}>{tools.map((tool) => <li key={tool.name}><code>{tool.name}</code>{tool.description ? ` — ${tool.description}` : ''}</li>)}</ul>}
        </section>; })}
      </div>}
      <p className="section-footnote">客户端不直接执行命令、不解析 Secret，也不提供绕过 Policy 的 MCP tool call 入口。</p>
    </div></div>
    <Modal isOpen={modalOpen} onClose={() => setModalOpen(false)} title="添加 MCP 服务" footer={<><button type="button" className="btn btn-ghost" onClick={() => setModalOpen(false)}>取消</button><button type="button" className="btn btn-primary" onClick={() => void submit()} disabled={Boolean(actionLabel) || connectionStatus !== 'connected' || !serverId.trim()}>保存配置</button></>}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        {formError && <div className="live-alert live-alert-error" role="alert">{formError}</div>}
        <label>服务 ID<input className="input" value={serverId} onChange={(event) => setServerId(event.target.value)} autoFocus required /></label>
        <label>Transport<select className="select" value={transport} onChange={(event) => setTransport(event.target.value as typeof transport)}><option value="stdio">stdio</option><option value="legacy_sse">legacy SSE</option></select></label>
        {transport === 'stdio' ? <label>argv（JSON 字符串数组）<textarea className="input" rows={4} value={stdioArgv} onChange={(event) => setStdioArgv(event.target.value)} aria-describedby="mcp-argv-help" /><small id="mcp-argv-help">参数数组直接交给 Core，不经过 Shell。</small></label> : <label>Endpoint Ref（环境变量名）<input className="input" value={endpointRef} onChange={(event) => setEndpointRef(event.target.value)} aria-describedby="mcp-endpoint-help" /><small id="mcp-endpoint-help">为避免暴露端点，Live API 只接受引用名，不接受 URL。</small></label>}
      </div>
    </Modal>
  </div>;
};
