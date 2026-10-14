import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { CallerPairingClient, type CallerDeviceView } from '../../../../../sdk/typescript-client/caller_pairing.generated';
import { currentBrowserOrigin } from '../../lib/liveBaseUrl';
import { nativeCoreTransport } from '../../lib/nativeCoreTransport';
import { requestErrorCopy } from '../../lib/requestErrorCopy';
import { isWriteOutcomeUnknown } from '../../lib/writeOutcome';

const stateLabel: Record<CallerDeviceView['state'], string> = {
  active: '已连接', expired: '已过期', revoked: '已断开', needs_pairing: '需重新配对',
};

export const CallerPairingDevicesPanel: React.FC = () => {
  const client = useMemo(() => new CallerPairingClient(currentBrowserOrigin(), nativeCoreTransport), []);
  const [devices, setDevices] = useState<CallerDeviceView[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [unknownDeviceId, setUnknownDeviceId] = useState<string | null>(null);
  const unknownRef = useRef<string | null>(null);
  const dispatching = useRef(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const page = await client.listCallerDevices();
      setDevices(page.items);
      if (unknownRef.current && page.items.some((item) => item.device_id === unknownRef.current && item.state === 'revoked')) {
        unknownRef.current = null;
        setUnknownDeviceId(null);
      }
      setError('');
    } catch (cause: unknown) { setError(requestErrorCopy(cause)); }
    finally { setLoading(false); }
  }, [client]);

  useEffect(() => { void refresh(); }, [refresh]);

  const revoke = async (device: CallerDeviceView) => {
    if (dispatching.current || unknownDeviceId || !window.confirm(`断开“${device.display_name}”的技能来源连接？`)) return;
    dispatching.current = true; setBusy(true); setError('');
    try {
      const result = await client.revokeCallerDevice(device.device_id, { idempotencyKey: crypto.randomUUID() });
      setDevices(result.items);
      if (!result.items.some((item) => item.device_id === device.device_id && item.state === 'revoked')) {
        unknownRef.current = device.device_id;
        setUnknownDeviceId(device.device_id);
        setError('断开结果尚未确认，请刷新列表核对该客户端。');
      }
    } catch (cause: unknown) {
      setError(requestErrorCopy(cause));
      if (isWriteOutcomeUnknown(cause)) { unknownRef.current = device.device_id; setUnknownDeviceId(device.device_id); }
    } finally { dispatching.current = false; setBusy(false); }
  };

  return <section className="skill-sources-card">
    <h2>已连接的客户端</h2>
    {error && <p role="alert">{error}</p>}
    {unknownDeviceId && <div className="live-alert live-alert-warn" role="alert">
      断开结果尚未确认。请刷新并核对状态，暂时不要再次断开。
      <button type="button" className="btn btn-secondary btn-sm" disabled={loading} onClick={() => { void refresh(); }}>刷新并核对</button>
    </div>}
    {loading ? <p role="status">正在读取…</p> : devices.length ? <ul>{devices.map((device) => <li key={device.device_id}>
      <div><strong>{device.display_name}</strong><span>{stateLabel[device.state]}</span><small>仅可管理技能来源</small></div>
      {device.state !== 'revoked' && <button type="button" className="btn btn-ghost btn-sm"
        disabled={busy || Boolean(unknownDeviceId)} onClick={() => { void revoke(device); }}>断开</button>}
    </li>)}</ul> : <p>还没有配对的客户端。</p>}
    <button type="button" className="btn btn-ghost btn-sm" disabled={busy || loading} onClick={() => { void refresh(); }}>刷新列表</button>
  </section>;
};
