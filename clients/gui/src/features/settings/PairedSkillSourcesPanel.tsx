import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { SkillSourceView } from '../../../../../sdk/typescript-client/onboarding.generated.ts';
import { PathInput } from '../../components/PathInput';
import { pairedErrorCopy } from '../../lib/pairedErrorCopy';
import {
  type CallerPairingClientLike, type PairedReceipt, type PairingAttempt, PairedSkillSourceSession,
  prepareSkillSourcePairing, parsePairingTicket,
} from '../../lib/pairedSkillSourceTransport';
import { createPairedSkillSourceClient } from '../../lib/pairedSkillSourceOnboarding';
import {
  listPairingIdentities, readPendingPairedWrite,
  type PairingIdentity, type PendingPairedWrite,
} from '../../lib/pairingIdentityStore';
import { skillIssueCopy } from './skillIssueCopy';

interface Props { createCallerClient: (baseUrl: string) => CallerPairingClientLike }

export const PairedSkillSourcesPanel: React.FC<Props> = ({ createCallerClient }) => {
  const [identities, setIdentities] = useState<PairingIdentity[]>([]);
  const [identity, setIdentity] = useState<PairingIdentity | null>(null);
  const [items, setItems] = useState<SkillSourceView[]>([]);
  const [ticketInput, setTicketInput] = useState('');
  const [displayName, setDisplayName] = useState('此浏览器');
  const [path, setPath] = useState('');
  const [pending, setPending] = useState<PendingPairedWrite | null>(null);
  const [pairAttempt, setPairAttempt] = useState<PairingAttempt | null>(null);
  const [receipt, setReceipt] = useState<PairedReceipt | null>(null);
  const [continueNeedsReadback, setContinueNeedsReadback] = useState(false);
  const [loading, setLoading] = useState(true);
  const [verified, setVerified] = useState(false);
  const [storageReady, setStorageReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const dispatching = useRef(false);
  const session = useMemo(() => identity?.paired && identity.expiresAt > Date.now() / 1000
    ? new PairedSkillSourceSession(createCallerClient(identity.baseUrl), identity) : null,
  [createCallerClient, identity]);
  const sourceClient = useMemo(() => session ? createPairedSkillSourceClient(session) : null, [session]);

  const syncPending = useCallback(async (deviceId: string) => {
    try {
      const next = await readPendingPairedWrite(deviceId);
      setPending(next);
      setStorageReady(true);
      if (!next) { setReceipt(null); setContinueNeedsReadback(false); }
      return next;
    } catch (cause: unknown) {
      setStorageReady(false);
      throw cause;
    }
  }, []);

  useEffect(() => {
    let active = true;
    void listPairingIdentities().then(async (saved) => {
      if (!active) return;
      setIdentities(saved);
      const selected = saved.find((entry) => entry.paired && entry.expiresAt > Date.now() / 1000) ?? saved[0] ?? null;
      setIdentity(selected);
      if (selected) await syncPending(selected.deviceId);
      else setStorageReady(true);
    }).catch(() => { if (active) { setStorageReady(false); setError('无法读取已保存的配对状态，请检查浏览器存储权限。'); } })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [syncPending]);

  const refresh = useCallback(async () => {
    if (!sourceClient) return false;
    setLoading(true);
    try {
      const page = await sourceClient.listSkillSources();
      setItems(page.items);
      setVerified(true);
      setError('');
      return true;
    } catch (cause: unknown) {
      setVerified(false);
      setError(pairedErrorCopy(cause));
      return false;
    } finally { setLoading(false); }
  }, [sourceClient]);

  useEffect(() => { if (sourceClient) void refresh(); }, [sourceClient, refresh]);

  useEffect(() => {
    if (!identity?.paired || identity.expiresAt * 1000 <= Date.now()) return;
    const timer = window.setTimeout(() => {
      setVerified(false);
      setIdentity((current) => current?.deviceId === identity.deviceId ? { ...current } : current);
    }, identity.expiresAt * 1000 - Date.now());
    return () => window.clearTimeout(timer);
  }, [identity]);

  const pair = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (dispatching.current || !storageReady) return;
    const pasted = ticketInput;
    setTicketInput(''); // The one-time code is never kept after this click.
    dispatching.current = true; setBusy(true); setError('');
    try {
      const ticket = parsePairingTicket(pasted);
      const matching = identities.filter((entry) => entry.baseUrl === ticket.base_url);
      const existing = matching.find((entry) => entry.paired) ?? matching[0] ?? null;
      const attempt = await prepareSkillSourcePairing(createCallerClient(ticket.base_url), ticket, displayName, existing);
      setPairAttempt(attempt);
      const paired = await attempt.confirm();
      setPairAttempt(null);
      setIdentities((current) => [...current.filter((entry) => entry.deviceId !== paired.deviceId), paired]);
      setIdentity(paired);
      setVerified(false);
      await syncPending(paired.deviceId);
    } catch (cause: unknown) {
      setError(pairedErrorCopy(cause));
    } finally { dispatching.current = false; setBusy(false); }
  };

  const checkPairing = async () => {
    if (!pairAttempt || pairAttempt.expiresAt <= Date.now() / 1000 || dispatching.current) return;
    dispatching.current = true; setBusy(true); setError('');
    try {
      const paired = await pairAttempt.confirm();
      setPairAttempt(null);
      setIdentities((current) => [...current.filter((entry) => entry.deviceId !== paired.deviceId), paired]);
      setIdentity(paired);
      setVerified(false);
      await syncPending(paired.deviceId);
    } catch (cause: unknown) { setError(pairedErrorCopy(cause)); }
    finally { dispatching.current = false; setBusy(false); }
  };

  const abandonPairing = async () => {
    if (busy || !window.confirm('放弃本页核对，并改用桌面版新生成的配对码？')) return;
    try {
      const saved = await listPairingIdentities();
      setIdentities(saved);
      setPairAttempt(null);
      setError('请在桌面版生成新配对码，沿用此浏览器身份重新配对。旧配对结果仍未核实。');
    } catch { setError('无法读取本机配对身份，请检查浏览器存储后重试。'); }
  };

  const write = async (operation: 'add' | 'remove', value: string) => {
    if (!sourceClient || !identity || !verified || !storageReady || dispatching.current || pending || pairAttempt) return;
    dispatching.current = true; setBusy(true); setError('');
    try {
      const key = crypto.randomUUID();
      if (operation === 'add') await sourceClient.addSkillSource({ path: value }, { idempotencyKey: key });
      else await sourceClient.removeSkillSource(value, { idempotencyKey: key });
      await syncPending(identity.deviceId);
      if (operation === 'add') setPath('');
      await refresh();
    } catch (cause: unknown) {
      try { await syncPending(identity.deviceId); setError(pairedErrorCopy(cause)); }
      catch { setError('无法读取原请求状态。请保留当前页面，检查浏览器存储后再核对。'); }
    } finally { dispatching.current = false; setBusy(false); }
  };

  const reconcile = async () => {
    if (!session || !pending || dispatching.current) return;
    dispatching.current = true; setBusy(true); setError('');
    try {
      const result = await session.readback(pending);
      setReceipt(result);
      setContinueNeedsReadback(false);
      await syncPending(pending.deviceId);
      if (result.state === 'completed') await refresh();
      else if (result.state === 'failed') setError('这次修改未完成，请检查目录和权限后再试。');
    } catch (cause: unknown) { setError(pairedErrorCopy(cause)); }
    finally { dispatching.current = false; setBusy(false); }
  };

  const continueApproved = async () => {
    if (!session || !pending || !receipt || dispatching.current) return;
    dispatching.current = true; setBusy(true); setError(''); setContinueNeedsReadback(true);
    try {
      const result = await session.continueApproved(receipt, pending);
      setReceipt(result);
      await syncPending(pending.deviceId);
      if (result.state === 'completed') await refresh();
      else if (result.state === 'failed') setError('桌面审批后的修改未完成，请核对目录和权限。');
    } catch (cause: unknown) { setError(pairedErrorCopy(cause)); }
    finally { dispatching.current = false; setBusy(false); }
  };

  return <div className="skill-sources">
    <header><h1>技能来源</h1><p>先连接这台电脑的桌面 Core，再查看或修改技能目录。</p></header>
    {error && <div className="live-alert live-alert-error" role="alert">{error}</div>}
    <section className="skill-sources-card">
      <h2>连接桌面 Core</h2>
      {identity?.paired && <p>{verified ? '已连接' : '已保存配对'} {identity.baseUrl}，权限仅限技能来源。{identity.expiresAt <= Date.now() / 1000 ? '连接已过期，请重新配对。' : ''}</p>}
      {!identity?.paired && !loading && <p>{identity
        ? '上次配对结果尚未确认。请在桌面版重新生成配对码，并沿用此浏览器身份连接。'
        : '在这台电脑的桌面版“技能来源”生成短时配对码，再粘贴到这里。配对需要桌面确认。'}</p>}
      {pairAttempt && <div className="live-alert live-alert-warn" role="alert">
        配对结果尚未确认。当前页面可以按原请求核对；刷新页面后需在桌面版重新生成配对码。
        <button type="button" className="btn btn-secondary btn-sm" disabled={busy || pairAttempt.expiresAt <= Date.now() / 1000}
          onClick={() => { void checkPairing(); }}>核对配对结果</button>
        <button type="button" className="btn btn-ghost btn-sm" disabled={busy}
          onClick={() => { void abandonPairing(); }}>改用新配对码</button>
      </div>}
      <form onSubmit={(event) => { void pair(event); }}>
        <label htmlFor="caller-pairing-name">设备名称</label>
        <input id="caller-pairing-name" className="input" value={displayName} onChange={(event) => setDisplayName(event.target.value)} maxLength={100} disabled={busy} />
        <label htmlFor="caller-pairing-ticket">桌面配对码</label>
        <textarea id="caller-pairing-ticket" className="textarea" value={ticketInput} onChange={(event) => setTicketInput(event.target.value)}
          placeholder="粘贴桌面版生成的短时配对码" autoComplete="off" spellCheck={false} disabled={busy} />
        <button type="submit" className="btn btn-primary" disabled={busy || !storageReady || Boolean(pairAttempt) || !ticketInput.trim()}>{busy ? '正在连接…' : '连接'}</button>
      </form>
    </section>
    {pending && <div className="live-alert live-alert-warn" role="alert">
      修改结果尚未确认。请核对原请求，暂时不要再次修改。
      <button type="button" className="btn btn-secondary btn-sm" disabled={busy || !session} onClick={() => { void reconcile(); }}>刷新并核对</button>
      {receipt?.state === 'awaiting_approval' && <>
        <span>请先到桌面版完成审批，再继续原请求。</span>
        {pending.coreEpochId !== identity?.coreEpochId && <span>桌面 Core 已重新启动，旧请求只能核对。</span>}
        <button type="button" className="btn btn-secondary btn-sm"
          disabled={busy || continueNeedsReadback || pending.coreEpochId !== identity?.coreEpochId}
          onClick={() => { void continueApproved(); }}>继续原请求</button>
      </>}
    </div>}
    {session && <>
      <section className="skill-sources-card"><h2>已登记目录</h2>
        {loading ? <p role="status">正在读取…</p> : items.length ? <ul>{items.map((item) => <li key={item.root_ref}>
          <div><strong>{item.label}</strong><span>{!item.exists ? '目录未找到' : item.enabled ? '可用' : '未启用'}</span>
            <small>{item.path}</small>
            {item.issue && <small>{skillIssueCopy(item.issue).guidance}</small>}
          </div>
          {item.root_ref.startsWith('user-') && <button type="button" className="btn btn-ghost btn-sm" disabled={busy || !verified || !storageReady || Boolean(pending) || Boolean(pairAttempt)}
            onClick={() => { if (window.confirm(`移除技能来源“${item.label}”？`)) void write('remove', item.root_ref); }}>移除</button>}
        </li>)}</ul> : <p>尚未找到技能目录。</p>}
        <button type="button" className="btn btn-ghost btn-sm" disabled={busy} onClick={() => { void refresh(); }}>刷新目录</button>
      </section>
      <form className="skill-sources-card" onSubmit={(event) => { event.preventDefault(); if (path.trim()) void write('add', path.trim()); }}>
        <h2>添加目录</h2>
        <PathInput label="技能文件夹" value={path} onChange={setPath} placeholder="填写桌面 Core 上的完整路径" disabled={busy || !verified || !storageReady || Boolean(pending) || Boolean(pairAttempt)} />
        <button type="submit" className="btn btn-primary" disabled={busy || !verified || !storageReady || Boolean(pending) || Boolean(pairAttempt) || !path.trim()}>添加来源</button>
      </form>
    </>}
  </div>;
};
