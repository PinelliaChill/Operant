import React, { useEffect, useState } from 'react';
import { QrCode, Radio, RefreshCw, Server, Smartphone, Trash2 } from 'lucide-react';
import { EmptyState } from '../../components/EmptyState';
import { LoadingSkeleton } from '../../components/LoadingSkeleton';
import { Modal } from '../../components/Modal';
import { StatusBadge } from '../../components/StatusBadge';
import { useOperant } from '../../context/ClientContext';
import { formatDate } from '../../lib/format';

type RemoteHostProjection = {
  hostId: string;
  displayName: string;
  protocolVersion: string;
  coreVersion: string;
  capabilities: string[];
  onlineState: string;
};

type RemoteDeviceProjection = {
  deviceId: string;
  displayName: string;
  scopes: string[];
  pairedAt: string;
  revokedAt?: string;
};

type RemoteTargetProjection = {
  targetId: string;
  displayName: string;
  status: string;
  capabilities: string[];
  lastSeenAt?: string;
};

type RemoteJobProjection = {
  jobId: string;
  targetId: string;
  capability: string;
  operation: string;
  status: string;
};

type PairingTicketProjection = {
  challengeId: string;
  oneTimeCode: string;
  expiresAt: string;
};

function record(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error(`${label} 投影格式无效。`);
  }
  return value as Record<string, unknown>;
}

function text(value: unknown, label: string): string {
  if (typeof value !== 'string' || value.length === 0) throw new Error(`${label} 缺失。`);
  return value;
}

function strings(value: unknown): string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== 'string')) {
    throw new Error('远程能力列表格式无效。');
  }
  return value as string[];
}

function items(value: unknown, label: string): Record<string, unknown>[] {
  const projection = record(value, label);
  if (!Array.isArray(projection.items)) throw new Error(`${label} 列表格式无效。`);
  return projection.items.map((item) => record(item, label));
}

export const LiveRemoteView: React.FC = () => {
  const { phase56Client, connectionStatus, addNotification } = useOperant();
  const [hosts, setHosts] = useState<RemoteHostProjection[]>([]);
  const [devices, setDevices] = useState<RemoteDeviceProjection[]>([]);
  const [targets, setTargets] = useState<RemoteTargetProjection[]>([]);
  const [jobs, setJobs] = useState<RemoteJobProjection[]>([]);
  const [ticket, setTicket] = useState<PairingTicketProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadData = async () => {
    setLoading(true);
    try {
      const [rawHosts, rawTargets, rawJobs] = await Promise.all([
        phase56Client.listRemoteHosts(),
        phase56Client.listRemoteTargets(),
        phase56Client.listRemoteTargetJobs(),
      ]);
      const nextHosts = items(rawHosts, 'Remote Host').map((value) => ({
        hostId: text(value.host_id, 'host_id'),
        displayName: text(value.display_name, 'display_name'),
        protocolVersion: text(value.protocol_version, 'protocol_version'),
        coreVersion: text(value.core_version, 'core_version'),
        capabilities: strings(value.capabilities),
        onlineState: text(value.online_state, 'online_state'),
      }));
      const devicePages = await Promise.all(
        nextHosts.map((host) => phase56Client.listRemoteDevices({ hostId: host.hostId })),
      );
      setHosts(nextHosts);
      setDevices(devicePages.flatMap((page) => items(page, 'Remote Device')).map((value) => ({
        deviceId: text(value.device_id, 'device_id'),
        displayName: text(value.display_name, 'display_name'),
        scopes: strings(value.scopes),
        pairedAt: text(value.paired_at, 'paired_at'),
        revokedAt: typeof value.revoked_at === 'string' ? value.revoked_at : undefined,
      })));
      setTargets(items(rawTargets, 'Remote Target').map((value) => {
        const manifest = record(value.capability_manifest, 'Capability Manifest');
        return {
          targetId: text(value.target_id, 'target_id'),
          displayName: text(value.display_name, 'display_name'),
          status: text(value.status, 'target status'),
          capabilities: strings(manifest.capabilities),
          lastSeenAt: typeof value.last_seen_at === 'string' ? value.last_seen_at : undefined,
        };
      }));
      setJobs(items(rawJobs, 'Remote Job').map((value) => ({
        jobId: text(value.job_id, 'job_id'),
        targetId: text(value.target_id, 'target_id'),
        capability: text(value.capability, 'capability'),
        operation: text(value.operation, 'operation'),
        status: text(value.status, 'job status'),
      })));
      setError(null);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : '远程实时投影加载失败。');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadData();
  }, [phase56Client]);

  const enableHost = async () => {
    try {
      await phase56Client.enableRemoteHost({
        display_name: 'Local Core',
        core_version: '0.1.0',
        protocol_version: 'phase56.v1',
        capabilities: ['remote.control', 'remote.target', 'multi_writer'],
      });
      addNotification('success', '本地 Remote Control Host 已启用。');
      await loadData();
    } catch (reason: unknown) {
      addNotification('error', reason instanceof Error ? reason.message : '启用失败。');
    }
  };

  const createPairingTicket = async () => {
    const host = hosts[0];
    if (!host) return;
    try {
      const value = record(await phase56Client.createPairingChallenge({
        host_id: host.hostId,
        ttl_seconds: 120,
      }), 'Pairing Ticket');
      setTicket({
        challengeId: text(value.challenge_id, 'challenge_id'),
        oneTimeCode: text(value.one_time_code, 'one_time_code'),
        expiresAt: text(value.expires_at, 'expires_at'),
      });
    } catch (reason: unknown) {
      addNotification('error', reason instanceof Error ? reason.message : '创建配对票据失败。');
    }
  };

  const revokeDevice = async (deviceId: string) => {
    try {
      await phase56Client.revokeRemoteDevice(deviceId);
      addNotification('warn', '远程设备已撤销，关联会话已关闭。');
      await loadData();
    } catch (reason: unknown) {
      addNotification('error', reason instanceof Error ? reason.message : '撤销失败。');
    }
  };

  const actionsDisabled = connectionStatus !== 'connected' || loading;
  return (
    <div style={{ padding: 24, overflowY: 'auto', height: '100%', display: 'flex', flexDirection: 'column', gap: 24 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 16, flexWrap: 'wrap' }}>
        <div>
          <h2 style={{ fontSize: 18, fontWeight: 700, color: 'var(--text-primary)', display: 'flex', alignItems: 'center', gap: 8 }}>
            <Radio size={20} color="var(--accent-action)" />
            <span>远程控制、执行目标与自托管中继</span>
          </h2>
          <p style={{ fontSize: 13, color: 'var(--text-muted)', marginTop: 4 }}>
            实时数据来自本地 Core；Remote Control 与 Remote Execution Target 分开显示。
          </p>
        </div>
        <button className="btn btn-secondary" onClick={() => void loadData()} disabled={loading}>
          <RefreshCw size={15} /><span>刷新</span>
        </button>
      </div>

      {error && <div className="live-error" role="alert">{error} 页面保留最近一次投影，不会回退演示数据。</div>}
      {loading && hosts.length === 0 ? <LoadingSkeleton lines={3} height={72} /> : (
        <>
          <section>
            <h3 style={{ fontSize: 14, fontWeight: 600, marginBottom: 12 }}>Remote Control Host（{hosts.length}）</h3>
            {hosts.length === 0 ? (
              <EmptyState icon={Server} title="尚未启用本地 Host" description="Remote Control 默认关闭，需要从本机实时页面显式启用。" action={
                <button className="btn btn-primary" onClick={() => void enableHost()} disabled={actionsDisabled}>启用本地 Host</button>
              } />
            ) : <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 420px))', gap: 16 }}>
              {hosts.map((host) => <div className="card" key={host.hostId}>
                <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
                  <strong>{host.displayName}</strong><StatusBadge status={host.onlineState === 'online' ? 'connected' : 'disconnected'} size="sm" />
                </div>
                <p style={{ fontSize: 12, color: 'var(--text-secondary)' }}>{host.protocolVersion} · Core {host.coreVersion}</p>
                <p style={{ fontSize: 11, color: 'var(--text-muted)', overflowWrap: 'anywhere' }}>{host.hostId}</p>
              </div>)}
            </div>}
            {hosts.length > 0 && <button className="btn btn-primary" style={{ marginTop: 12 }} onClick={() => void createPairingTicket()} disabled={actionsDisabled}>
              <QrCode size={15} /><span>创建短时配对票据</span>
            </button>}
          </section>

          <section>
            <h3 style={{ fontSize: 14, fontWeight: 600, marginBottom: 12 }}>已配对设备（{devices.length}）</h3>
            {devices.length === 0 ? <EmptyState icon={Smartphone} title="尚未配对远程设备" description="配对票据只在本机显示，设备提交公钥后才会出现在这里。" /> :
              <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>{devices.map((device) => <div className="card" key={device.deviceId} style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
                <div><strong>{device.displayName}</strong><div style={{ fontSize: 11, color: 'var(--text-muted)' }}>{device.scopes.join(' · ')} · {formatDate(device.pairedAt)}</div></div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}><StatusBadge status={device.revokedAt ? 'revoked' : 'granted'} size="sm" />
                  {!device.revokedAt && <button className="btn btn-danger btn-sm" aria-label={`撤销 ${device.displayName}`} onClick={() => void revokeDevice(device.deviceId)} disabled={actionsDisabled}><Trash2 size={12} /><span>撤销</span></button>}
                </div>
              </div>)}</div>}
          </section>

          <section>
            <h3 style={{ fontSize: 14, fontWeight: 600, marginBottom: 12 }}>Remote Execution Target（{targets.length}）</h3>
            {targets.length === 0 ? <EmptyState icon={Server} title="尚无执行目标" description="Target 需经 Action Gateway 审批注册；凭据只保存环境变量引用。" /> :
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 420px))', gap: 16 }}>{targets.map((target) => <div className="card" key={target.targetId}>
                <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}><strong>{target.displayName}</strong><StatusBadge status={target.status === 'online' ? 'connected' : target.status} size="sm" /></div>
                <p style={{ fontSize: 12, color: 'var(--text-secondary)', overflowWrap: 'anywhere' }}>{target.capabilities.join(' · ')}</p>
                <p style={{ fontSize: 11, color: 'var(--text-muted)' }}>最近心跳：{target.lastSeenAt ? formatDate(target.lastSeenAt) : '—'}</p>
              </div>)}</div>}
          </section>

          <section>
            <h3 style={{ fontSize: 14, fontWeight: 600, marginBottom: 12 }}>Browser / Computer Job（{jobs.length}）</h3>
            {jobs.length === 0 ? <EmptyState title="暂无远程任务" description="观察和动作任务会显示绑定的 Capability 与明确状态。" /> :
              <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>{jobs.slice(0, 50).map((job) => <div className="card" key={job.jobId} style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
                <span>{job.capability} · {job.operation}<small style={{ display: 'block', color: 'var(--text-muted)' }}>{job.targetId}</small></span><StatusBadge status={job.status} size="sm" />
              </div>)}</div>}
          </section>
        </>
      )}

      <Modal isOpen={ticket !== null} onClose={() => setTicket(null)} title="短时配对票据" footer={<button className="btn btn-primary" onClick={() => setTicket(null)}>关闭</button>}>
        {ticket && <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <p>请让远程设备在票据过期前提交自己的签名公钥与交换公钥。</p>
          <code style={{ overflowWrap: 'anywhere' }}>operant://pair?challenge={encodeURIComponent(ticket.challengeId)}&amp;code={encodeURIComponent(ticket.oneTimeCode)}</code>
          <p style={{ fontSize: 12, color: 'var(--text-muted)' }}>过期时间：{formatDate(ticket.expiresAt)}。票据只能使用一次。</p>
        </div>}
      </Modal>
    </div>
  );
};
