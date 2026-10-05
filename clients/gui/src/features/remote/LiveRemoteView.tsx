import React, { useEffect, useMemo, useState } from 'react';
import { ClipboardCopy, Radio, RefreshCw, Server, Smartphone, Trash2 } from 'lucide-react';
import { BetaClient } from '../../../../../sdk/typescript-client/beta.generated';
import { PHASE56_PROTOCOL_VERSION, type RemoteCapability, type RemoteScope } from '../../../../../sdk/typescript-client/phase56.generated';
import { EmptyState } from '../../components/EmptyState';
import { LoadingSkeleton } from '../../components/LoadingSkeleton';
import { Modal } from '../../components/Modal';
import { StatusBadge } from '../../components/StatusBadge';
import { useOperant } from '../../context/ClientContext';
import { formatDate } from '../../lib/format';
import { currentBrowserOrigin } from '../../lib/liveBaseUrl';

const scopeOptions: { value: RemoteScope; label: string }[] = [
  { value: 'remote.control.observe', label: '查看状态与结果' },
  { value: 'remote.control.command', label: '发送任务命令' },
  { value: 'remote.control.approve', label: '处理审批' },
  { value: 'remote.control.browser', label: '浏览器操作' },
  { value: 'remote.control.computer', label: '电脑操作' },
  { value: 'remote.control.settings', label: '修改设置' },
];

type RemoteHostProjection = {
  hostId: string;
  displayName: string;
  protocolVersion: string;
  coreVersion: string;
  capabilities: string[];
  onlineState: string;
};

type RemoteDeviceProjection = {
  hostId: string;
  deviceId: string;
  displayName: string;
  scopes: string[];
  pairedAt: string;
  lastSeenAt?: string;
  revokedAt?: string;
};

type RemoteTargetProjection = {
  targetId: string;
  displayName: string;
  status: string;
  capabilities: string[];
  supportedOperations: string[];
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
  hostId: string;
  oneTimeCode: string;
  expiresAt: string;
  allowedScopes: string[];
  relayUrl: string | null;
  hostSigningPublicKey: string;
  hostExchangePublicKey: string;
};

type RemoteSessionProjection = {
  sessionId: string;
  deviceId: string;
  transportMode: string;
  connectionState: string;
  eventCursor: string;
  expiresAt: string;
};

type RemoteEventProjection = {
  cursor: string;
  commandId: string;
  status: string;
  hostAcknowledgedAt?: string;
  errorCode?: string;
};

type GatewayConnectionProjection = {
  connectionId: string;
  sessionId: string;
  status: string;
  eventCursor: number;
  lastSeenAt: string;
  errorCode?: string;
};

type ActiveTargetLease = {
  targetId: string;
  leaseId: string;
  token: string;
  fencing: number;
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
  const { phase56Client, connectionStatus, addNotification, activeWorkspace } = useOperant();
  const betaClient = useMemo(() => new BetaClient(currentBrowserOrigin()), []);
  const [hosts, setHosts] = useState<RemoteHostProjection[]>([]);
  const [selectedHostId, setSelectedHostId] = useState('');
  const [confirmDisableHost, setConfirmDisableHost] = useState<RemoteHostProjection | null>(null);
  const [devices, setDevices] = useState<RemoteDeviceProjection[]>([]);
  const [targets, setTargets] = useState<RemoteTargetProjection[]>([]);
  const [jobs, setJobs] = useState<RemoteJobProjection[]>([]);
  const [sessions, setSessions] = useState<RemoteSessionProjection[]>([]);
  const [events, setEvents] = useState<RemoteEventProjection[]>([]);
  const [gatewayConnections, setGatewayConnections] = useState<GatewayConnectionProjection[]>([]);
  const [selectedScopes, setSelectedScopes] = useState<RemoteScope[]>(['remote.control.observe']);
  const [transportMode, setTransportMode] = useState<'direct' | 'relay'>('direct');
  const [commandId, setCommandId] = useState('');
  const [commandDetail, setCommandDetail] = useState<Record<string, unknown> | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmRevoke, setConfirmRevoke] = useState<RemoteDeviceProjection | null>(null);
  const [confirmClose, setConfirmClose] = useState<RemoteSessionProjection | null>(null);
  const [targetForm, setTargetForm] = useState({ targetId: '', name: '', endpoint: '', publicKey: '', credentialRef: '', policyRef: '', artifactNamespace: '', platform: 'linux', operations: 'read_text,run_allowlisted' });
  const [targetId, setTargetId] = useState('');
  const [workspaceRef, setWorkspaceRef] = useState(activeWorkspace);
  const [lease, setLease] = useState<ActiveTargetLease | null>(null);
  const [showLeaseToken, setShowLeaseToken] = useState(false);
  const [jobCapability, setJobCapability] = useState<RemoteCapability>('remote.target.read');
  const [jobOperation, setJobOperation] = useState('read_text');
  const [jobArguments, setJobArguments] = useState('{"path":"README.md"}');
  const [jobIdempotency, setJobIdempotency] = useState<'idempotent' | 'non_idempotent'>('idempotent');
  const [confirmCancelJob, setConfirmCancelJob] = useState<RemoteJobProjection | null>(null);
  const [ticket, setTicket] = useState<PairingTicketProjection | null>(null);
  const [selectedJob, setSelectedJob] = useState<RemoteJobProjection | null>(null);
  const [jobDetail, setJobDetail] = useState<Record<string, unknown> | null>(null);
  const [jobDetailError, setJobDetailError] = useState<string | null>(null);
  const [jobDetailLoading, setJobDetailLoading] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadData = async () => {
    setLoading(true);
    try {
      const [rawHosts, rawTargets, rawJobs, rawConnections] = await Promise.all([
        phase56Client.listRemoteHosts(),
        phase56Client.listRemoteTargets(),
        phase56Client.listRemoteTargetJobs(),
        betaClient.listRemoteGatewayConnections(),
      ]);
      const nextHosts = items(rawHosts, 'Remote Host').map((value) => ({
        hostId: text(value.host_id, 'host_id'),
        displayName: text(value.display_name, 'display_name'),
        protocolVersion: text(value.protocol_version, 'protocol_version'),
        coreVersion: text(value.core_version, 'core_version'),
        capabilities: strings(value.capabilities),
        onlineState: text(value.online_state, 'online_state'),
      }));
      const [devicePages, sessionPages, eventPages] = await Promise.all([
        Promise.all(
        nextHosts.map((host) => phase56Client.listRemoteDevices({ hostId: host.hostId })),
        ),
        Promise.all(nextHosts.map((host) => phase56Client.listRemoteSessions({ hostId: host.hostId }))),
        Promise.all(nextHosts.map((host) => phase56Client.listRemoteControlEvents({ hostId: host.hostId, limit: 50 }))),
      ]);
      setHosts(nextHosts);
      setSelectedHostId((current) => nextHosts.some((host) => host.hostId === current) ? current : (nextHosts.find((host) => host.onlineState === 'online')?.hostId ?? nextHosts[0]?.hostId ?? ''));
      setGatewayConnections(rawConnections.items.map((value) => ({
        connectionId: value.connection_id,
        sessionId: value.remote_session_id,
        status: value.status,
        eventCursor: value.event_cursor,
        lastSeenAt: value.last_seen_at,
        errorCode: value.error_code ?? undefined,
      })));
      setDevices(devicePages.flatMap((page) => items(page, 'Remote Device')).map((value) => ({
        hostId: text(value.host_id, 'host_id'),
        deviceId: text(value.device_id, 'device_id'),
        displayName: text(value.display_name, 'display_name'),
        scopes: strings(value.scopes),
        pairedAt: text(value.paired_at, 'paired_at'),
        lastSeenAt: typeof value.last_seen_at === 'string' ? value.last_seen_at : undefined,
        revokedAt: typeof value.revoked_at === 'string' ? value.revoked_at : undefined,
      })));
      setSessions(sessionPages.flatMap((page) => items(page, 'Remote Session')).map((value) => ({
        sessionId: text(value.remote_session_id, 'remote_session_id'),
        deviceId: text(value.device_id, 'device_id'),
        transportMode: text(value.transport_mode, 'transport_mode'),
        connectionState: text(value.connection_state, 'connection_state'),
        eventCursor: String(value.event_cursor ?? 0),
        expiresAt: text(value.expires_at, 'expires_at'),
      })));
      setEvents(eventPages.flatMap((page) => items(page, 'Remote Event')).map((value) => ({
        cursor: String(value.cursor ?? ''),
        commandId: text(value.command_id, 'command_id'),
        status: text(value.status, 'command status'),
        hostAcknowledgedAt: typeof value.host_acknowledged_at === 'string' ? value.host_acknowledged_at : undefined,
        errorCode: typeof value.error_code === 'string' ? value.error_code : undefined,
      })).slice(-50));
      setTargets(items(rawTargets, 'Remote Target').map((value) => {
        const manifest = record(value.capability_manifest, 'Capability Manifest');
        return {
          targetId: text(value.target_id, 'target_id'),
          displayName: text(value.display_name, 'display_name'),
          status: text(value.status, 'target status'),
          capabilities: strings(manifest.capabilities),
          supportedOperations: strings(manifest.supported_operations),
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
  }, [phase56Client, betaClient]);

  const enableHost = async (host?: RemoteHostProjection, enabled = true) => {
    setBusy(true);
    try {
      await phase56Client.enableRemoteHost({
        display_name: host?.displayName ?? 'Local Core',
        ...(host ? { host_id: host.hostId } : {}),
        capabilities: ['remote.control'],
        enabled,
      });
      setConfirmDisableHost(null);
      addNotification(enabled ? 'success' : 'warn', enabled ? '本地 Remote Control Host 已启用。' : 'Host 已停用，关联会话已关闭。');
      await loadData();
    } catch (reason: unknown) {
      addNotification('error', reason instanceof Error ? reason.message : '启用失败。');
    } finally {
      setBusy(false);
    }
  };

  const createPairingTicket = async () => {
    const host = hosts.find((item) => item.hostId === selectedHostId && item.onlineState === 'online');
    if (!host) {
      addNotification('error', '请选择已启用的 Host。');
      return;
    }
    setBusy(true);
    try {
      const value = record(await phase56Client.createPairingChallenge({
        host_id: host.hostId,
        ttl_seconds: 300,
        allowed_scopes: selectedScopes,
      }), 'Pairing Ticket');
      setTicket({
        challengeId: text(value.challenge_id, 'challenge_id'),
        hostId: text(value.host_id, 'host_id'),
        oneTimeCode: text(value.one_time_code, 'one_time_code'),
        expiresAt: text(value.expires_at, 'expires_at'),
        allowedScopes: strings(value.allowed_scopes),
        relayUrl: typeof value.relay_url === 'string' ? value.relay_url : null,
        hostSigningPublicKey: text(value.host_signing_public_key, 'host_signing_public_key'),
        hostExchangePublicKey: text(value.host_exchange_public_key, 'host_exchange_public_key'),
      });
    } catch (reason: unknown) {
      addNotification('error', reason instanceof Error ? reason.message : '创建配对票据失败。');
    } finally {
      setBusy(false);
    }
  };

  const revokeDevice = async (deviceId: string) => {
    setBusy(true);
    try {
      await phase56Client.revokeRemoteDevice(deviceId);
      addNotification('warn', '远程设备已撤销，关联会话已关闭。');
      setConfirmRevoke(null);
      await loadData();
    } catch (reason: unknown) {
      addNotification('error', reason instanceof Error ? reason.message : '撤销失败。');
    } finally {
      setBusy(false);
    }
  };

  const createSession = async (device: RemoteDeviceProjection) => {
    setBusy(true);
    try {
      const result = record(await phase56Client.createRemoteSession({
        host_id: device.hostId,
        device_id: device.deviceId,
        transport_mode: transportMode,
        protocol_version: PHASE56_PROTOCOL_VERSION,
      }), 'Remote Session');
      addNotification('success', `会话已创建：${text(result.remote_session_id, 'remote_session_id')}。请在远端用此 ID 连接 Gateway。`);
      await loadData();
    } catch (reason: unknown) {
      addNotification('error', reason instanceof Error ? reason.message : '创建会话失败。');
    } finally {
      setBusy(false);
    }
  };

  const closeSession = async (sessionId: string) => {
    setBusy(true);
    try {
      await phase56Client.closeRemoteSession(sessionId);
      setConfirmClose(null);
      addNotification('success', '远程会话已关闭。');
      await loadData();
    } catch (reason: unknown) {
      addNotification('error', reason instanceof Error ? reason.message : '关闭会话失败。');
    } finally {
      setBusy(false);
    }
  };

  const readCommand = async (id: string) => {
    if (!id.trim()) return;
    try {
      setCommandDetail(record(await phase56Client.getRemoteCommand(id.trim()), 'Remote Command'));
    } catch (reason: unknown) {
      setCommandDetail(null);
      addNotification('error', reason instanceof Error ? reason.message : '命令回执读取失败。');
    }
  };

  const registerTarget = async () => {
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(targetForm.credentialRef)) {
      addNotification('error', '凭据引用只能填写环境变量名，不能填写 Token 值。');
      return;
    }
    setBusy(true);
    try {
      await phase56Client.registerRemoteTarget({
        target_id: targetForm.targetId.trim(),
        display_name: targetForm.name.trim(),
        endpoint_ref: targetForm.endpoint.trim(),
        identity_public_key: targetForm.publicKey.trim(),
        credential_ref: targetForm.credentialRef.trim(),
        policy_ref: targetForm.policyRef.trim(),
        artifact_namespace: targetForm.artifactNamespace.trim(),
        capability_manifest: {
          version: '1', platform: targetForm.platform.trim(),
          capabilities: ['remote.target.read', 'remote.target.exec'],
          supported_operations: targetForm.operations.split(',').map((item) => item.trim()).filter(Boolean),
        },
      });
      setTargetId(targetForm.targetId.trim());
      addNotification('success', '远程执行目标已登记；等目标心跳后再申请租约。');
      await loadData();
    } catch (reason: unknown) {
      addNotification('error', reason instanceof Error ? reason.message : '目标登记失败。');
    } finally { setBusy(false); }
  };

  const acquireLease = async () => {
    if (!targetId || !workspaceRef.trim()) return;
    setBusy(true);
    try {
      const key = crypto.randomUUID();
      const value = record(await phase56Client.acquireRemoteTargetLease(targetId, {
        owner: 'gui-local-user', workspace_ref: workspaceRef.trim(), ttl_seconds: 300,
        idempotency_key: key,
      }, { idempotencyKey: key }), 'Target Lease');
      setLease({ targetId, leaseId: text(value.lease_id, 'lease_id'), token: text(value.token, 'token'), fencing: Number(value.fencing), expiresAt: text(value.expires_at, 'expires_at') });
      setShowLeaseToken(true);
      addNotification('success', '已取得短时 Target 租约；Token 仅保留在本页面内存中。');
    } catch (reason: unknown) {
      addNotification('error', reason instanceof Error ? reason.message : '租约申请失败。');
    } finally { setBusy(false); }
  };

  const releaseLease = async () => {
    if (!lease) return;
    setBusy(true);
    try {
      const key = crypto.randomUUID();
      await phase56Client.releaseRemoteTargetLease(lease.targetId, {
        lease_id: lease.leaseId, token: lease.token, fencing: lease.fencing, idempotency_key: key,
      }, { idempotencyKey: key });
      setLease(null);
      setShowLeaseToken(false);
      addNotification('success', 'Target 租约已释放。');
    } catch (reason: unknown) {
      addNotification('error', reason instanceof Error ? reason.message : '租约释放失败；请核对 Core 状态。');
    } finally { setBusy(false); }
  };

  const createJob = async () => {
    if (!lease || lease.targetId !== targetId || new Date(lease.expiresAt).getTime() <= Date.now()) return;
    let args: Record<string, unknown>;
    try {
      args = record(JSON.parse(jobArguments), 'Job 参数');
    } catch (reason: unknown) {
      addNotification('error', reason instanceof Error ? reason.message : 'Job 参数需要 JSON 对象。');
      return;
    }
    setBusy(true);
    try {
      const key = crypto.randomUUID();
      const result = record(await phase56Client.createRemoteTargetJob(targetId, {
        lease_id: lease.leaseId, token: lease.token, fencing: lease.fencing,
        capability: jobCapability, operation: jobOperation.trim(), arguments: args,
        idempotency_key: key, idempotency: jobIdempotency,
      }, { idempotencyKey: key }), 'Target Job');
      addNotification('success', `作业已排队：${text(result.job_id, 'job_id')}。须由远端 Target 正式派发后回读结果。`);
      await loadData();
    } catch (reason: unknown) {
      addNotification('error', reason instanceof Error ? reason.message : '作业创建失败；结果未知时按 Job ID 核对。');
    } finally { setBusy(false); }
  };

  const cancelJob = async (job: RemoteJobProjection) => {
    setBusy(true);
    try {
      const key = crypto.randomUUID();
      await phase56Client.cancelRemoteTargetJob(job.jobId, { idempotency_key: key }, { idempotencyKey: key });
      setConfirmCancelJob(null);
      addNotification('warn', '取消请求已提交；请刷新结果核对终态。');
      await loadData();
    } catch (reason: unknown) {
      addNotification('error', reason instanceof Error ? reason.message : '取消结果未知，请按 Job ID 核对。');
    } finally { setBusy(false); }
  };

  const loadJobDetail = async (job: RemoteJobProjection) => {
    setSelectedJob(job);
    setJobDetail(null);
    setJobDetailLoading(true);
    setJobDetailError(null);
    try {
      setJobDetail(record(await phase56Client.getRemoteTargetJobResult(job.jobId), 'Job result'));
    } catch (reason: unknown) {
      setJobDetail(null);
      setJobDetailError(reason instanceof Error ? reason.message : '任务结果读取失败。');
    } finally {
      setJobDetailLoading(false);
    }
  };

  const actionsDisabled = connectionStatus !== 'connected' || loading || busy;
  const ticketJson = ticket ? JSON.stringify({
    challenge_id: ticket.challengeId,
    host_id: ticket.hostId,
    one_time_code: ticket.oneTimeCode,
    expires_at: ticket.expiresAt,
    allowed_scopes: ticket.allowedScopes,
    relay_url: ticket.relayUrl,
    host_signing_public_key: ticket.hostSigningPublicKey,
    host_exchange_public_key: ticket.hostExchangePublicKey,
  }, null, 2) : '';
  const detailResult = jobDetail?.result && typeof jobDetail.result === 'object' && !Array.isArray(jobDetail.result) ? jobDetail.result as Record<string, unknown> : null;
  const detailObservation = jobDetail?.observation && typeof jobDetail.observation === 'object' && !Array.isArray(jobDetail.observation) ? jobDetail.observation as Record<string, unknown> : null;
  const detailPostcondition = detailResult?.postcondition && typeof detailResult.postcondition === 'object' && !Array.isArray(detailResult.postcondition) ? detailResult.postcondition as Record<string, unknown> : null;
  const detailStatus = typeof jobDetail?.status === 'string' ? jobDetail.status : selectedJob?.status;
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
                {host.onlineState === 'online' ? <button className="btn btn-danger btn-sm" onClick={() => setConfirmDisableHost(host)} disabled={actionsDisabled}>紧急停用 Host</button> : <button className="btn btn-secondary btn-sm" onClick={() => void enableHost(host)} disabled={actionsDisabled}>重新启用</button>}
              </div>)}
            </div>}
            {hosts.length > 0 && <div style={{ marginTop: 16 }}>
              <label htmlFor="remote-host-choice">配对使用的 Host</label>{' '}
              <select id="remote-host-choice" className="select" value={selectedHostId} onChange={(event) => setSelectedHostId(event.target.value)} disabled={actionsDisabled}>
                {hosts.map((host) => <option key={host.hostId} value={host.hostId}>{host.displayName} · {host.onlineState}</option>)}
              </select>
              <strong>配对授权范围</strong>
              <p style={{ fontSize: 12, color: 'var(--text-secondary)' }}>先在本机选择要授予的权限。创建票据即确认此范围；票据过期或使用后失效。</p>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12, margin: '10px 0' }}>
                {scopeOptions.map((scope) => <label key={scope.value} style={{ display: 'inline-flex', alignItems: 'center', gap: 6, minHeight: 36 }}>
                  <input type="checkbox" checked={selectedScopes.includes(scope.value)} disabled={actionsDisabled || scope.value === 'remote.control.observe'} onChange={(event) => setSelectedScopes((current) => event.target.checked ? [...current, scope.value] : current.filter((value) => value !== scope.value))} />
                  {scope.label}
                </label>)}
              </div>
              <button className="btn btn-primary" onClick={() => void createPairingTicket()} disabled={actionsDisabled || !hosts.some((host) => host.hostId === selectedHostId && host.onlineState === 'online')}>
                <ClipboardCopy size={15} aria-hidden="true" /><span>确认权限并创建短时票据</span>
              </button>
            </div>}
          </section>

          <section>
            <h3 style={{ fontSize: 14, fontWeight: 600, marginBottom: 12 }}>已配对设备（{devices.length}）</h3>
            {devices.length === 0 ? <EmptyState icon={Smartphone} title="尚未配对远程设备" description="配对票据只在本机显示，设备提交公钥后才会出现在这里。" /> :
              <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>{devices.map((device) => <div className="card" key={device.deviceId} style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
                <div><strong>{device.displayName}</strong><div style={{ fontSize: 11, color: 'var(--text-muted)' }}>{device.scopes.join(' · ')} · 配对于 {formatDate(device.pairedAt)} · 最近活动 {device.lastSeenAt ? formatDate(device.lastSeenAt) : '尚无'}</div></div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}><StatusBadge status={device.revokedAt ? 'revoked' : 'granted'} size="sm" />
                  {!device.revokedAt && <><button className="btn btn-secondary btn-sm" onClick={() => void createSession(device)} disabled={actionsDisabled}>创建会话</button><button className="btn btn-danger btn-sm" aria-label={`撤销 ${device.displayName}`} onClick={() => setConfirmRevoke(device)} disabled={actionsDisabled}><Trash2 size={12} aria-hidden="true" /><span>撤销</span></button></>}
                </div>
              </div>)}</div>}
          </section>

          <section>
            <h3 style={{ fontSize: 14, fontWeight: 600, marginBottom: 12 }}>连接与断线诊断</h3>
            <label htmlFor="remote-transport">新会话传输方式</label>{' '}
            <select id="remote-transport" className="select" value={transportMode} onChange={(event) => setTransportMode(event.target.value as 'direct' | 'relay')} disabled={actionsDisabled}>
              <option value="direct">直连 Gateway</option><option value="relay">自托管 Relay</option>
            </select>
            <p style={{ fontSize: 12, color: 'var(--text-secondary)', margin: '8px 0' }}>断线后先用原会话和 Cursor 重连并刷新投影；会话过期后再创建新会话。Relay 接收不代表 Host 已确认。</p>
            {sessions.length === 0 ? <EmptyState title="尚无远程会话" description="设备配对后可创建会话；连接是否成功由 Gateway 状态判定。" /> :
              <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>{sessions.map((session) => {
                const connection = gatewayConnections.find((item) => item.sessionId === session.sessionId && item.status !== 'closed');
                return <div className="card" key={session.sessionId} style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
                  <div style={{ minWidth: 0, overflowWrap: 'anywhere' }}><strong>{session.deviceId}</strong><div><code>{session.sessionId}</code> · {session.transportMode} · 会话 {session.connectionState} · Gateway {connection?.status ?? '未连接'}</div>
                    <small>可信 Cursor：{connection?.eventCursor ?? session.eventCursor} · 最近活动：{connection ? formatDate(connection.lastSeenAt) : '尚无'} · 过期：{formatDate(session.expiresAt)}{connection?.errorCode ? ` · 错误：${connection.errorCode}` : ''}</small></div>
                  {session.connectionState !== 'closed' && <button className="btn btn-secondary btn-sm" onClick={() => setConfirmClose(session)} disabled={actionsDisabled}>关闭会话</button>}
                </div>;
              })}</div>}
          </section>

          <section>
            <h3 style={{ fontSize: 14, fontWeight: 600, marginBottom: 12 }}>Host 命令回执</h3>
            <p style={{ fontSize: 12, color: 'var(--text-secondary)' }}>按 Command ID 读取 Core 状态；未确认或结果不明时先核对，不能自动重发。</p>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}><input className="input" aria-label="Command ID" placeholder="Command ID" value={commandId} onChange={(event) => setCommandId(event.target.value)} /><button className="btn btn-secondary" onClick={() => void readCommand(commandId)} disabled={actionsDisabled || !commandId.trim()}>读取回执</button></div>
            {commandDetail && <div className="card" role="status">状态：{String(commandDetail.status ?? '未知')} · Host 确认：{typeof commandDetail.host_acknowledged_at === 'string' ? formatDate(commandDetail.host_acknowledged_at) : '未确认'} · 结果引用：{String(commandDetail.result_ref ?? '无')}{typeof commandDetail.error_code === 'string' ? ` · 错误：${commandDetail.error_code}` : ''}</div>}
            {events.length > 0 && <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginTop: 12 }}>{events.slice(-10).reverse().map((event) => <div className="card" key={`${event.cursor}-${event.commandId}`}><button className="btn btn-ghost btn-sm" style={{ maxWidth: '100%', whiteSpace: 'normal', overflowWrap: 'anywhere', textAlign: 'left' }} onClick={() => { setCommandId(event.commandId); void readCommand(event.commandId); }} disabled={actionsDisabled}>{event.commandId}</button> · Cursor {event.cursor} · {event.status} · Host {event.hostAcknowledgedAt ? '已确认' : '未确认'}{event.errorCode ? ` · ${event.errorCode}` : ''}</div>)}</div>}
          </section>

          <section>
            <h3 style={{ fontSize: 14, fontWeight: 600, marginBottom: 12 }}>Remote Execution Target（{targets.length}）</h3>
              {targets.length === 0 ? <EmptyState icon={Server} title="尚无执行目标" description="先在远端初始化 Target 身份并准备 TLS 服务，再在本机登记公钥和环境变量引用。" /> :
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 420px))', gap: 16 }}>{targets.map((target) => <div className="card" key={target.targetId}>
                <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}><strong>{target.displayName}</strong><StatusBadge status={target.status === 'online' ? 'connected' : target.status} size="sm" /></div>
                <p style={{ fontSize: 11, color: 'var(--text-muted)', overflowWrap: 'anywhere' }}>{target.targetId}</p>
                <p style={{ fontSize: 12, color: 'var(--text-secondary)', overflowWrap: 'anywhere' }}>{target.capabilities.join(' · ')}</p>
                <p style={{ fontSize: 12, color: 'var(--text-secondary)' }}>允许操作：{target.supportedOperations.join(' · ')}</p>
                <p style={{ fontSize: 11, color: 'var(--text-muted)' }}>最近心跳：{target.lastSeenAt ? formatDate(target.lastSeenAt) : '—'}</p>
              </div>)}</div>}
            <details style={{ marginTop: 16 }}>
              <summary>登记受控执行目标</summary>
              <p style={{ fontSize: 12, color: 'var(--text-secondary)' }}>仅填写远端公开身份和本机环境变量名。Target 服务与派发由远端正式 CLI 运行，Token 不写入配置。</p>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 10, marginTop: 12 }}>
                {([
                  ['targetId', 'Target ID'], ['name', '显示名称'], ['endpoint', 'HTTPS Endpoint'],
                  ['publicKey', '签名公钥'], ['credentialRef', 'Bearer 环境变量名'],
                  ['policyRef', 'Policy Ref'], ['artifactNamespace', 'Artifact Namespace'],
                  ['platform', '平台'], ['operations', '允许操作，逗号分隔'],
                ] as const).map(([key, label]) => <label key={key}>{label}<input className="input" value={targetForm[key]} onChange={(event) => setTargetForm((current) => ({ ...current, [key]: event.target.value }))} autoComplete="off" /></label>)}
              </div>
              <button className="btn btn-primary" style={{ marginTop: 12 }} onClick={() => void registerTarget()} disabled={actionsDisabled || !targetForm.targetId.trim() || !targetForm.endpoint.trim() || !targetForm.publicKey.trim()}>登记 Target</button>
            </details>
            <div className="card" style={{ marginTop: 16 }}>
              <h4 style={{ marginBottom: 8 }}>短时租约与作业</h4>
              <label htmlFor="remote-target-choice">执行目标</label>{' '}
              <select id="remote-target-choice" className="select" value={targetId} onChange={(event) => { setTargetId(event.target.value); setLease(null); }} disabled={actionsDisabled || lease !== null}>
                <option value="">选择 Target</option>{targets.map((target) => <option key={target.targetId} value={target.targetId}>{target.displayName} · {target.targetId} · {target.status}</option>)}
              </select>
              <label htmlFor="remote-workspace-ref" style={{ display: 'block', marginTop: 8 }}>目标 Workspace Ref</label>
              <input id="remote-workspace-ref" className="input" value={workspaceRef} onChange={(event) => setWorkspaceRef(event.target.value)} disabled={actionsDisabled || lease !== null} placeholder="远端已授权 Workspace 引用" />
              {!lease ? <button className="btn btn-secondary" style={{ marginTop: 8 }} onClick={() => void acquireLease()} disabled={actionsDisabled || !targetId || !workspaceRef.trim()}>申请 5 分钟租约</button> : <div style={{ marginTop: 8 }} role="status">租约 {lease.leaseId} · fencing {lease.fencing} · 过期 {formatDate(lease.expiresAt)} <button className="btn btn-danger btn-sm" onClick={() => void releaseLease()} disabled={actionsDisabled}>释放租约</button></div>}
              <p style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 8 }}>租约 Token 仅在当前页面内存中；远端 serve 与本机 dispatch 均需同一 Token。刷新或离开后 Token 无法恢复，等待最多 5 分钟租约过期后再申请。操作结果未知时按 Job ID 核对，勿自动重发。</p>
              {lease && <button className="btn btn-ghost btn-sm" onClick={() => setShowLeaseToken(true)} disabled={actionsDisabled}>查看一次性租约交接信息</button>}
              {lease && <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginTop: 12 }}>
                <label htmlFor="remote-job-capability">Capability</label>
                <select id="remote-job-capability" className="select" value={jobCapability} onChange={(event) => { const next = event.target.value as RemoteCapability; setJobCapability(next); setJobOperation(next === 'remote.target.read' ? 'read_text' : 'run_allowlisted'); }} disabled={actionsDisabled}>
                  <option value="remote.target.read">只读远端文本</option><option value="remote.target.exec">运行远端允许命令</option>
                </select>
                <label htmlFor="remote-job-operation">Operation</label><input id="remote-job-operation" className="input" value={jobOperation} onChange={(event) => setJobOperation(event.target.value)} disabled={actionsDisabled} />
                <label htmlFor="remote-job-arguments">参数 JSON</label><textarea id="remote-job-arguments" className="input" value={jobArguments} onChange={(event) => setJobArguments(event.target.value)} disabled={actionsDisabled} rows={3} />
                <label htmlFor="remote-job-idempotency">幂等性</label><select id="remote-job-idempotency" className="select" value={jobIdempotency} onChange={(event) => setJobIdempotency(event.target.value as 'idempotent' | 'non_idempotent')} disabled={actionsDisabled}><option value="idempotent">可安全去重</option><option value="non_idempotent">可能产生一次性副作用</option></select>
                <button className="btn btn-primary" onClick={() => void createJob()} disabled={actionsDisabled || !jobOperation.trim() || !targets.find((target) => target.targetId === targetId)?.supportedOperations.includes(jobOperation.trim()) || new Date(lease.expiresAt).getTime() <= Date.now()}>提交受控作业</button>
              </div>}
            </div>
          </section>

          <section>
            <h3 style={{ fontSize: 14, fontWeight: 600, marginBottom: 12 }}>Remote Execution Target Job（{jobs.length}）</h3>
            {jobs.length === 0 ? <EmptyState title="暂无远程执行任务" description="远程执行任务与控制命令分开显示，结果按 Job ID 从 Core 回读。" /> :
              <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>{jobs.slice(0, 50).map((job) => <div className="card" key={job.jobId} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
                <span style={{ minWidth: 0, overflowWrap: 'anywhere' }}>{job.capability} · {job.operation}<small style={{ display: 'block', color: 'var(--text-muted)' }}>{job.targetId} · {job.jobId}</small></span>
                <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}><StatusBadge status={job.status} size="sm" /><button className="btn btn-secondary btn-sm" onClick={() => void loadJobDetail(job)} disabled={actionsDisabled} aria-label={`查看任务 ${job.jobId} 的结果`}>查看结果</button>{['queued', 'leased', 'running'].includes(job.status) && <button className="btn btn-danger btn-sm" onClick={() => setConfirmCancelJob(job)} disabled={actionsDisabled}>取消…</button>}</span>
              </div>)}</div>}
          </section>
        </>
      )}

      <Modal isOpen={ticket !== null} onClose={() => setTicket(null)} title="短时配对票据" footer={<button className="btn btn-primary" onClick={() => setTicket(null)}>关闭</button>}>
        {ticket && <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <p>复制完整 JSON，在远端保存为仅当前用户可读的票据文件，再运行正式 remote-device pair。票据只在此处显示。</p>
          <p>已批准：{ticket.allowedScopes.join(' · ')}</p>
          <pre style={{ maxHeight: '35vh', overflow: 'auto', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', userSelect: 'all' }}>{ticketJson}</pre>
          <button className="btn btn-secondary" onClick={() => void navigator.clipboard.writeText(ticketJson).then(() => addNotification('success', '票据 JSON 已复制。'), () => addNotification('error', '浏览器拒绝复制；可选中文本手动复制。'))}>复制完整票据 JSON</button>
          <p style={{ fontSize: 12, color: 'var(--text-muted)' }}>过期时间：{formatDate(ticket.expiresAt)}。票据只能使用一次。</p>
        </div>}
      </Modal>

      <Modal isOpen={confirmRevoke !== null} onClose={() => setConfirmRevoke(null)} title="确认撤销远程设备" footer={<><button className="btn btn-secondary" onClick={() => setConfirmRevoke(null)} disabled={busy}>取消</button><button className="btn btn-danger" onClick={() => confirmRevoke && void revokeDevice(confirmRevoke.deviceId)} disabled={actionsDisabled}>确认撤销</button></>}>
        <p>撤销 {confirmRevoke?.displayName} 后，关联会话会关闭；该设备不能继续发送控制命令。</p>
      </Modal>
      <Modal isOpen={confirmClose !== null} onClose={() => setConfirmClose(null)} title="确认关闭远程会话" footer={<><button className="btn btn-secondary" onClick={() => setConfirmClose(null)} disabled={busy}>取消</button><button className="btn btn-danger" onClick={() => confirmClose && void closeSession(confirmClose.sessionId)} disabled={actionsDisabled}>确认关闭</button></>}>
        <p>关闭会话 {confirmClose?.sessionId} 后，远端需要重新建立会话。</p>
      </Modal>
      <Modal isOpen={confirmDisableHost !== null} onClose={() => setConfirmDisableHost(null)} title="确认紧急停用 Host" footer={<><button className="btn btn-secondary" onClick={() => setConfirmDisableHost(null)} disabled={busy}>取消</button><button className="btn btn-danger" onClick={() => confirmDisableHost && void enableHost(confirmDisableHost, false)} disabled={actionsDisabled}>停用并关闭会话</button></>}><p>停用 {confirmDisableHost?.displayName} 会关闭它的全部远程会话。重新启用后需要由远端重新建立会话。</p></Modal>
      <Modal isOpen={confirmCancelJob !== null} onClose={() => setConfirmCancelJob(null)} title="确认取消远程作业" footer={<><button className="btn btn-secondary" onClick={() => setConfirmCancelJob(null)} disabled={busy}>取消</button><button className="btn btn-danger" onClick={() => confirmCancelJob && void cancelJob(confirmCancelJob)} disabled={actionsDisabled}>确认发送取消请求</button></>}><p>作业 {confirmCancelJob?.jobId} 可能正在远端运行；发送取消后仍须刷新结果确认终态。</p></Modal>

      <Modal isOpen={showLeaseToken && lease !== null} onClose={() => setShowLeaseToken(false)} title="短时 Target 租约交接" footer={<button className="btn btn-primary" onClick={() => setShowLeaseToken(false)}>已记录并关闭</button>}>
        {lease && <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          <p>把以下值交给远端正式 Target serve，并在本机 Target dispatch 使用相同租约。Token 只在当前页面内存中，不会写入配置或日志。</p>
          <div>Target ID：<code>{lease.targetId}</code></div><div>Lease ID：<code>{lease.leaseId}</code></div><div>Fencing：<code>{lease.fencing}</code></div><div>过期：<code>{lease.expiresAt}</code></div>
          <div>OPERANT_REMOTE_TARGET_LEASE_TOKEN：<code style={{ overflowWrap: 'anywhere', userSelect: 'all' }}>{lease.token}</code></div>
          <p style={{ fontSize: 12, color: 'var(--text-secondary)' }}>远端启动需私有身份文件、CA/TLS、工作区和允许命令清单；本机派发还需凭据环境变量与 CA 文件。Token 丢失时等待租约过期后重新申请。</p>
        </div>}
      </Modal>

      <Modal isOpen={selectedJob !== null} onClose={() => setSelectedJob(null)} title="能力任务结果" footer={<><button className="btn btn-secondary" onClick={() => selectedJob && void loadJobDetail(selectedJob)} disabled={jobDetailLoading || connectionStatus !== 'connected'}>刷新结果</button><button className="btn btn-primary" onClick={() => setSelectedJob(null)}>关闭</button></>}>
        {selectedJob && <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }} aria-busy={jobDetailLoading}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}><StatusBadge status={detailStatus || 'unknown'} size="sm" /><code style={{ overflowWrap: 'anywhere' }}>{selectedJob.jobId}</code></div>
          <p style={{ color: 'var(--text-secondary)' }}>{selectedJob.capability} · {selectedJob.operation} · {selectedJob.targetId}</p>
          {jobDetailLoading && <p role="status">正在读取持久结果…</p>}
          {jobDetailError && <div className="live-error" role="alert">{jobDetailError} 请刷新结果，保留原 Job ID 核对。</div>}
          {detailStatus === 'manual_reconcile_required' && <div className="live-error" role="status">结果不明：先检查目标当前状态，再决定后续动作。不要自动重试这项操作。</div>}
          {!jobDetailLoading && !jobDetailError && !detailResult && !detailObservation && <p role="status" style={{ color: 'var(--text-muted)' }}>暂无终态回执，可稍后按原 Job ID 刷新。</p>}
          {detailObservation && <><strong>观察</strong><pre style={{ maxHeight: '35vh', overflow: 'auto', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontSize: 12, padding: 12, background: 'var(--bg-surface)', borderRadius: 8 }}>{JSON.stringify(detailObservation.body ?? detailObservation, null, 2)}</pre></>}
          {detailResult && <><strong>回执</strong>{typeof detailResult.error_code === 'string' && <p role="alert">错误代码：{detailResult.error_code}</p>}{detailPostcondition && Object.keys(detailPostcondition).length > 0 && <pre style={{ maxHeight: '35vh', overflow: 'auto', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontSize: 12, padding: 12, background: 'var(--bg-surface)', borderRadius: 8 }}>{JSON.stringify(detailPostcondition, null, 2)}</pre>}</>}
        </div>}
      </Modal>
    </div>
  );
};
