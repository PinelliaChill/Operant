import React, { useEffect, useState } from 'react';
import {
  Smartphone,
  Server,
  QrCode,
  Radio,
  Trash2,
} from 'lucide-react';
import { useOperant } from '../../context/ClientContext';
import { RemoteDevice, RemoteHost } from '@operant/sdk';
import { StatusBadge } from '../../components/StatusBadge';
import { Modal } from '../../components/Modal';
import { LoadingSkeleton } from '../../components/LoadingSkeleton';
import { EmptyState } from '../../components/EmptyState';
import { formatDate, formatTransportMode } from '../../lib/format';
import { LiveRemoteView } from './LiveRemoteView';

/** 设备权限范围 → 中文标签 */
const SCOPE_LABELS: Record<string, string> = {
  read_only: '只读',
  interactive_steering: '交互式引导',
  full_control: '完全控制',
};

const DemoRemoteView: React.FC = () => {
  const { client, addNotification } = useOperant();

  const [hosts, setHosts] = useState<RemoteHost[]>([]);
  const [devices, setDevices] = useState<RemoteDevice[]>([]);
  const [loading, setLoading] = useState(true);
  const [pairModalOpen, setPairModalOpen] = useState(false);
  const [deviceName] = useState('开发者的 iPhone');
  const [deviceScope, setDeviceScope] = useState<'read_only' | 'interactive_steering' | 'full_control'>('interactive_steering');
  const [pinCode, setPinCode] = useState('849201');

  const loadData = async () => {
    try {
      setLoading(true);
      const [hostList, devList] = await Promise.all([
        client.listRemoteHosts(),
        client.listRemoteDevices(),
      ]);
      setHosts(hostList);
      setDevices(devList);
    } catch (err: unknown) {
      console.error('Failed to load remote control data:', err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadData();
  }, [client]);

  const handleStartPairing = async () => {
    const randomPin = Math.floor(100000 + Math.random() * 900000).toString();
    setPinCode(randomPin);
    const host = hosts[0];
    if (host) {
      await client.requestDevicePairing(host.id, deviceName, deviceScope, randomPin);
    }
    setPairModalOpen(true);
  };

  const handleRevokeDevice = async (deviceId: string) => {
    try {
      await client.revokeRemoteDevice(deviceId);
      addNotification('warn', '远程设备已撤销，其所有活跃能力租约与会话已终止。');
      loadData();
    } catch (err: unknown) {
      addNotification('error', '撤销失败');
    }
  };

  return (
    <div style={{ padding: 24, overflowY: 'auto', height: '100%', display: 'flex', flexDirection: 'column', gap: 24 }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 16, flexWrap: 'wrap' }}>
        <div>
          <h2 style={{ fontSize: '18px', fontWeight: 700, color: 'var(--text-primary)', display: 'flex', alignItems: 'center', gap: 8 }}>
            <Radio size={20} color="var(--accent-action)" />
            <span>远程控制、设备与自托管中继</span>
          </h2>
          <p style={{ fontSize: '13px', color: 'var(--text-muted)', marginTop: 4 }}>
            通过端到端加密的中继信封，从手机 PWA 或远程终端控制本地 Operant Core。
          </p>
        </div>

        <button onClick={handleStartPairing} className="btn btn-primary">
          <QrCode size={15} />
          <span>配对新远程设备</span>
        </button>
      </div>

      {/* Host Status & Relay Topology */}
      <div style={{ minHeight: 160 }}>
        <h3 style={{ fontSize: '14px', fontWeight: 600, color: 'var(--text-primary)', marginBottom: 12 }}>
          已注册的 Operant Core 主机（{hosts.length}）
        </h3>

        {loading ? (
          <LoadingSkeleton lines={2} height={80} />
        ) : hosts.length === 0 ? (
          <EmptyState
            icon={Server}
            title="暂无已注册主机"
            description="本地或远程 Operant Core 主机注册后，会在这里显示传输方式与能力列表。"
          />
        ) : (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(360px, 420px))', gap: 16, justifyContent: 'start' }}>
            {hosts.map((host) => (
              <div key={host.id} className="card">
                <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8, gap: 8 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                    <Server size={16} color="var(--accent-action)" />
                    <span style={{ fontSize: '14px', fontWeight: 600, color: 'var(--text-primary)' }}>{host.name}</span>
                  </div>
                  <StatusBadge status={host.is_online ? 'connected' : 'disconnected'} size="sm" />
                </div>

                <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginBottom: 10 }}>
                  <div>传输方式：<span style={{ fontWeight: 600 }}>{formatTransportMode(host.transport_mode)}</span></div>
                  <div>协议版本：<span style={{ fontWeight: 600 }}>{host.protocol_version}</span></div>
                  <div>Core 版本：<span style={{ fontWeight: 600 }}>{host.core_version}</span></div>
                </div>

                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', borderTop: '1px solid var(--border-subtle)', paddingTop: 10 }}>
                  {host.capabilities.map((cap) => (
                    <span key={cap} className="badge" style={{ backgroundColor: 'var(--bg-subtle)', fontSize: '11px' }}>
                      {cap}
                    </span>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Paired Remote Devices */}
      <div style={{ flex: 1, minHeight: 200, display: 'flex', flexDirection: 'column' }}>
        <h3 style={{ fontSize: '14px', fontWeight: 600, color: 'var(--text-primary)', marginBottom: 12 }}>
          已授权的远程设备（{devices.length}）
        </h3>

        {devices.length === 0 ? (
          <div style={{ flex: 1, minHeight: 240, display: 'flex', flexDirection: 'column', justifyContent: 'center' }}>
            <EmptyState
              icon={Smartphone}
              title="尚未配对远程设备"
              description="点击右上角“配对新远程设备”，通过短时 PIN 码授权一个手机 PWA 或 TUI。"
            />
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            {devices.map((dev) => (
              <div
                key={dev.device_id}
                className="card"
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  gap: 12,
                  flexWrap: 'wrap',
                  opacity: dev.is_revoked ? 0.6 : 1,
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                  <div
                    style={{
                      width: 38,
                      height: 38,
                      borderRadius: '50%',
                      backgroundColor: 'var(--accent-subtle)',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      color: 'var(--accent-action)',
                    }}
                  >
                    <Smartphone size={18} />
                  </div>
                  <div>
                    <div style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)' }}>
                      {dev.device_name}
                    </div>
                    <div style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
                      权限范围：<span style={{ fontWeight: 600 }}>{SCOPE_LABELS[dev.scope] || dev.scope}</span> • 配对时间：{formatDate(dev.paired_at)}
                    </div>
                  </div>
                </div>

                <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                  <StatusBadge status={dev.is_revoked ? 'revoked' : 'granted'} size="sm" />
                  {!dev.is_revoked && (
                    <button
                      onClick={() => handleRevokeDevice(dev.device_id)}
                      className="btn btn-danger btn-sm"
                      title="立即撤销该设备"
                      aria-label="立即撤销该设备"
                    >
                      <Trash2 size={12} />
                      <span>撤销访问权限</span>
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Pairing Modal */}
      <Modal
        isOpen={pairModalOpen}
        onClose={() => setPairModalOpen(false)}
        title="配对远程设备（PWA / TUI）"
        footer={
          <button onClick={() => setPairModalOpen(false)} className="btn btn-primary">
            完成
          </button>
        }
      >
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 16, textAlign: 'center' }}>
          {/* Simulated QR Code SVG Frame */}
          <div
            style={{
              width: 180,
              height: 180,
              backgroundColor: '#ffffff',
              borderRadius: 'var(--radius-md)',
              border: '2px dashed var(--border-strong)',
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              justifyContent: 'center',
              padding: 12,
            }}
          >
            <QrCode size={110} color="#1c1917" />
            <div style={{ fontSize: '11px', color: '#78716c', marginTop: 6, wordBreak: 'break-all' }}>
              operant://pair?pin={pinCode}
            </div>
          </div>

          <div>
            <div style={{ fontSize: '12px', color: 'var(--text-muted)', marginBottom: 4 }}>
              或在远程设备上输入一次性 6 位 PIN 码：
            </div>
            <div
              style={{
                fontSize: '22px',
                fontWeight: 800,
                letterSpacing: 6,
                fontFamily: 'var(--font-mono)',
                color: 'var(--accent-action)',
              }}
            >
              {pinCode}
            </div>
          </div>

          <div style={{ width: '100%', textAlign: 'left' }}>
            <label style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: 4 }}>
              授予的设备权限范围
            </label>
            <select
              className="select"
              value={deviceScope}
              onChange={(e) => setDeviceScope(e.target.value as any)}
            >
              <option value="read_only">只读（状态、工件与追踪）</option>
              <option value="interactive_steering">交互式引导（排队指令与审批）</option>
              <option value="full_control">完全控制（创建会话与启动工作流）</option>
            </select>
          </div>
        </div>
      </Modal>
    </div>
  );
};

export const RemoteView: React.FC = () => {
  const { clientMode } = useOperant();
  return clientMode === 'live' ? <LiveRemoteView /> : <DemoRemoteView />;
};
