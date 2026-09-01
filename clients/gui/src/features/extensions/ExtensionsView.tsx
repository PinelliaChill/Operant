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
} from 'lucide-react';
import { EmptyState } from '../../components/EmptyState';
import { Modal } from '../../components/Modal';
import { StatusBadge } from '../../components/StatusBadge';
import { useOperant } from '../../context/ClientContext';
import { useDemo } from '../../demo/DemoContext';
import type { DemoExtension } from '../../demo/types';

export const ExtensionsView: React.FC = () => {
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
