import React, { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, RefreshCw } from 'lucide-react';
import { useOperant } from '../../context/ClientContext';
import { Modal } from '../../components/Modal';
import { SearchSelect } from '../../components/SearchSelect';
import { useOnboarding } from '../../live/OnboardingContext';
import type { LocalApplication } from '../../../../../sdk/typescript-client/onboarding';
import { localStateLabel } from '../../lib/statusCopy';
import { approvalId, idempotencyKey, LOCAL_EXTENSION_CHANGE_EVENT, optionalText, projection, projectionItems, requestCode, requestError, requiredText, stringList } from './localProjection';

type Plugin = { id: string; digest: string; state: string; targets: string[] };
type Category = 'tool' | 'command' | 'event' | 'provider' | 'runtime' | 'capability_driver';
type ExternalPlugin = { id: string; version: string; digest: string; state: string; installationId?: string; sandboxRef?: string; granted: Category[]; categories: Record<string, string[]> };
type Pending = { run: () => Promise<unknown>; approvalId: string; label: string };

const categories = [
  ['tool', '工具'], ['command', '命令'], ['event', '事件'], ['provider', '模型提供方'], ['runtime', '运行时'], ['capability_driver', '能力驱动'],
] as const;
const isCategory = (value: string): value is Category => categories.some(([key]) => key === value);
const bundledDrivers = [
  { id: 'operant.chrome.browser', label: '独立 Chrome 浏览器', targetHint: '允许的完整 Origin（如 https://example.com），逗号分隔' },
  { id: 'operant.macos.computer', label: 'macOS 窗口操控', targetHint: '允许的应用 Bundle ID（如 com.apple.TextEdit），逗号分隔' },
] as const;

function mapPlugin(value: Record<string, unknown>): Plugin {
  return { id: requiredText(value.plugin_id, 'plugin_id'), digest: requiredText(value.source_digest, 'source_digest'), state: requiredText(value.state, 'state'), targets: stringList(value.allowed_targets, 'allowed_targets') };
}

function mapExternal(value: Record<string, unknown>): ExternalPlugin {
  const categoryMap: Record<string, string[]> = {};
  for (const [key] of categories) {
    const raw = value[key];
    if (!Array.isArray(raw)) throw new Error(`${key} 扩展声明格式无效。`);
    categoryMap[key] = raw.map((item) => requiredText(projection(item, `${key} 声明`).name, `${key} 名称`));
  }
  return {
    id: requiredText(value.plugin_id, 'plugin_id'), version: requiredText(value.version, 'version'),
    digest: requiredText(value.package_digest, 'package_digest'), state: requiredText(value.state, 'state'),
    installationId: optionalText(value.installation_id), sandboxRef: optionalText(value.sandbox_evidence_ref),
    granted: Array.isArray(value.granted_categories) ? stringList(value.granted_categories, 'granted_categories').filter(isCategory) : [], categories: categoryMap,
  };
}

export const LocalExtensionsPanel: React.FC = () => {
  const { phase56Client, phase45Client, connectionStatus } = useOperant();
  const { client: onboardingClient } = useOnboarding();
  const [localApps, setLocalApps] = useState<LocalApplication[]>([]);
  const [localAppsError, setLocalAppsError] = useState('');
  const [plugins, setPlugins] = useState<Plugin[]>([]);
  const [external, setExternal] = useState<ExternalPlugin[]>([]);
  const [source, setSource] = useState('');
  const [digest, setDigest] = useState('');
  const [inspection, setInspection] = useState<Record<string, unknown>>();
  const [targetInput, setTargetInput] = useState<Record<string, string>>({});
  const [grants, setGrants] = useState<Record<string, Category[]>>({});
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<Pending>();
  const [confirmUninstall, setConfirmUninstall] = useState<{ kind: 'driver' | 'extension'; id: string }>();
  useEffect(() => {
    if (connectionStatus !== 'connected') return;
    let active = true;
    void onboardingClient.listLocalApplications().then((page) => { if (active) { setLocalApps(page.items); setLocalAppsError(''); } })
      .catch((reason: unknown) => { if (active) setLocalAppsError(`应用列表读取失败：${requestError(reason)}`); });
    return () => { active = false; };
  }, [connectionStatus, onboardingClient]);
  const disconnected = connectionStatus !== 'connected';
  useEffect(() => { if (disconnected) setConfirmUninstall(undefined); }, [disconnected]);

  const uninstallConfirmed = () => {
    if (!confirmUninstall || busy || disconnected) return;
    const { kind, id } = confirmUninstall;
    if (kind === 'driver' && !plugins.some((item) => item.id === id && item.state === 'disabled')) return;
    if (kind === 'extension' && !external.some((item) => item.id === id && item.state !== 'enabled')) return;
    setConfirmUninstall(undefined);
    const key = idempotencyKey();
    if (kind === 'driver') void act('卸载驱动', () => phase56Client.uninstallLocalCapabilityPlugin(id, { idempotency_key: key }, { idempotencyKey: key }));
    else void act('卸载扩展', () => phase56Client.uninstallExtension(id, { idempotency_key: key }, { idempotencyKey: key }));
  };

  const refresh = useCallback(async () => {
    const [localPage, extensionPage] = await Promise.all([phase56Client.listLocalCapabilityPlugins(), phase56Client.listExtensions()]);
    setPlugins(projectionItems(localPage, '本机能力插件').map(mapPlugin));
    setExternal(projectionItems(extensionPage, '外部扩展').map(mapExternal));
    setGrants({});
  }, [phase56Client]);
  useEffect(() => { void refresh().catch((reason: unknown) => setError(requestError(reason))); }, [refresh]);

  const act = async (label: string, run: () => Promise<unknown>, after?: (result: Record<string, unknown>) => void) => {
    if (busy || disconnected) return;
    setBusy(true); setError(''); setNotice(''); setPending(undefined);
    try {
      const result = projection(await run(), label);
      after?.(result);
      setNotice(`${label}已由 Core 确认。`);
      await refresh();
      window.dispatchEvent(new Event(LOCAL_EXTENSION_CHANGE_EVENT));
    } catch (reason: unknown) {
      const approval = approvalId(reason);
      if (requestCode(reason) === 'approval_required' && approval) { setPending({ run, approvalId: approval, label }); setError(`${label}需要人工审批。`); }
      else setError(`${label}失败：${requestError(reason)} 如果结果未知，请先人工核对，勿自动重试。`);
    } finally { setBusy(false); }
  };

  const decide = async (approved: boolean) => {
    if (!pending || busy || disconnected) return;
    setBusy(true); setError('');
    try {
      await phase45Client.decidePhase45Approval(pending.approvalId, { approved });
      const resume = pending; setPending(undefined);
      if (approved) { await resume.run(); setNotice(`${resume.label}已获批准并由 Core 确认。`); await refresh(); window.dispatchEvent(new Event(LOCAL_EXTENSION_CHANGE_EVENT)); }
      else setNotice('已拒绝操作。');
    } catch (reason: unknown) { setError(`审批或原操作失败：${requestError(reason)} 请人工核对。`); }
    finally { setBusy(false); }
  };

  return <section className="local-panel" id="local-driver-setup" aria-labelledby="local-extensions-title"><div className="local-panel-heading"><div><h2 id="local-extensions-title">安装与授权</h2><p>选择允许操作的应用或网站，再安装对应能力。</p></div><button type="button" className="btn btn-secondary" disabled={busy} onClick={() => void refresh().catch((reason: unknown) => setError(requestError(reason)))}><RefreshCw size={14} aria-hidden="true" />刷新</button></div>
    {disconnected && <p className="live-alert live-alert-error" role="alert">连接已断开，扩展管理已暂停。恢复连接后请刷新。</p>}
    {error && <p className="live-alert live-alert-error" role="alert"><AlertTriangle size={14} aria-hidden="true" />{error}</p>}
    {notice && <p className="live-alert" role="status">{notice}</p>}
    {pending && <div className="local-approval" role="group" aria-label="扩展操作审批"><span>审批 ID：<code>{pending.approvalId}</code></span><div className="local-actions"><button type="button" className="btn btn-primary" disabled={busy || disconnected} onClick={() => void decide(true)}>允许并提交</button><button type="button" className="btn btn-danger" disabled={busy || disconnected} onClick={() => void decide(false)}>拒绝</button></div></div>}
    <div className="local-extension-grid"><div className="local-extension-group"><h3>本机能力</h3>{bundledDrivers.map((driver) => { const plugin = plugins.find((item) => item.id === driver.id); return <article className="local-extension-card" key={driver.id}><strong>{driver.label}</strong><div className="local-meta"><span>状态：{plugin ? localStateLabel(plugin.state) : '未安装'}</span><span>已授权目标：{plugin?.targets.length || 0} 个</span></div>{driver.id === 'operant.macos.computer' ? <><SearchSelect label="要授权的应用" value={targetInput[driver.id] ?? ''} onChange={(value) => setTargetInput((current) => ({ ...current, [driver.id]: value }))} placeholder="选择本机应用" disabled={busy || disconnected || Boolean(plugin)} options={localApps.map((app) => ({ value: app.bundle_id, label: app.name, detail: app.bundle_id }))} />{localAppsError && <small role="alert">{localAppsError}</small>}</> : <label>要授权的网站地址<input className="input" type="url" value={targetInput[driver.id] ?? ''} onChange={(event) => setTargetInput((current) => ({ ...current, [driver.id]: event.target.value }))} placeholder="https://example.com" disabled={busy || disconnected || Boolean(plugin)} /></label>}<details className="local-advanced"><summary>高级：目标与来源详情</summary><label htmlFor={`local-target-${driver.id}`}>{driver.targetHint}</label><input id={`local-target-${driver.id}`} className="input" value={targetInput[driver.id] ?? plugin?.targets.join(', ') ?? ''} onChange={(event) => setTargetInput((current) => ({ ...current, [driver.id]: event.target.value }))} disabled={busy || disconnected || Boolean(plugin)} /><p>驱动编号：<code>{driver.id}</code></p>{plugin && <p>来源摘要：<code>{plugin.digest}</code></p>}</details><div className="local-actions">
      {!plugin && <button type="button" className="btn btn-primary" disabled={busy || disconnected || !(targetInput[driver.id] ?? '').trim()} onClick={() => { const key = idempotencyKey(); const targets = (targetInput[driver.id] ?? '').split(',').map((item) => item.trim()).filter(Boolean); void act('安装驱动', () => phase56Client.installLocalCapabilityPlugin({ plugin_id: driver.id, allowed_targets: targets, idempotency_key: key }, { idempotencyKey: key })); }}>安装并授权目标</button>}
      {plugin?.state === 'disabled' && <button type="button" className="btn btn-primary" disabled={busy || disconnected} onClick={() => { const key = idempotencyKey(); void act('启用驱动', () => phase56Client.setLocalCapabilityPluginEnabled(driver.id, { enabled: true, idempotency_key: key }, { idempotencyKey: key })); }}>启用</button>}
      {plugin?.state === 'enabled' && <button type="button" className="btn btn-secondary" disabled={busy || disconnected} onClick={() => { const key = idempotencyKey(); void act('停用驱动', () => phase56Client.setLocalCapabilityPluginEnabled(driver.id, { enabled: false, idempotency_key: key }, { idempotencyKey: key })); }}>停用</button>}
      {plugin?.state === 'disabled' && <button type="button" className="btn btn-ghost" disabled={busy || disconnected} onClick={() => setConfirmUninstall({ kind: 'driver', id: driver.id })}>卸载</button>}
    </div></article>; })}</div>
    <details className="local-extension-group local-advanced"><summary>外部扩展包（高级）</summary><div className="local-install-form"><label htmlFor="extension-source">可信来源引用</label><input id="extension-source" className="input" value={source} onChange={(event) => setSource(event.target.value)} placeholder="Core 可访问的包来源" /><button type="button" className="btn btn-secondary" disabled={busy || disconnected || !source.trim()} onClick={() => void act('检查扩展包', () => phase56Client.inspectExtension({ source: source.trim() }), (result) => { setInspection(result); setDigest(optionalText(result.package_digest) ?? optionalText(result.sha256) ?? ''); })}>检查包</button>{inspection && <pre className="local-evidence" aria-label="扩展包检查结果">{JSON.stringify(inspection, null, 2)}</pre>}<label htmlFor="extension-digest">期望 SHA-256</label><input id="extension-digest" className="input" value={digest} onChange={(event) => setDigest(event.target.value)} /><button type="button" className="btn btn-primary" disabled={busy || disconnected || !source.trim() || !/^[0-9a-f]{64}$/.test(digest)} onClick={() => { const key = idempotencyKey(); void act('安装扩展包', () => phase56Client.installExtension({ source: source.trim(), expected_sha256: digest, idempotency_key: key }, { idempotencyKey: key })); }}>安装校验过的包</button></div>
      {external.length === 0 && <p className="local-help">还没有安装外部扩展。</p>}{external.map((item) => <article className="local-extension-card" key={item.id}><strong>{item.id} <small>v{item.version}</small></strong><div className="local-meta"><span>状态：{localStateLabel(item.state)}</span><span>包摘要：<code>{item.digest.slice(0, 18)}…</code></span>{item.installationId && <span>安装 ID：<code>{item.installationId}</code></span>}{item.sandboxRef && <span>隔离证据：<code>{item.sandboxRef}</code></span>}</div><div className="local-category-list">{categories.map(([key, label]) => <label key={key} className="local-category"><input type="checkbox" checked={(grants[item.id] ?? item.granted).includes(key)} disabled={busy || disconnected || item.state === 'enabled' || item.categories[key].length === 0} onChange={(event) => setGrants((current) => { const old = current[item.id] ?? item.granted; return { ...current, [item.id]: event.target.checked ? [...old, key] : old.filter((value) => value !== key) }; })} /><span><strong>{label}</strong>（{item.categories[key].length}）<small>{item.categories[key].slice(0, 3).join('、') || '无声明'}</small></span></label>)}</div><div className="local-actions">{item.state !== 'enabled' && <button type="button" className="btn btn-primary" disabled={busy || disconnected || (grants[item.id] ?? item.granted).length === 0} onClick={() => { const key = idempotencyKey(); void act('启用扩展', () => phase56Client.enableExtension(item.id, { granted_categories: grants[item.id] ?? item.granted, idempotency_key: key }, { idempotencyKey: key })); }}>按所选类别授权并启用</button>}{item.state === 'enabled' && <button type="button" className="btn btn-secondary" disabled={busy || disconnected} onClick={() => { const key = idempotencyKey(); void act('停用扩展', () => phase56Client.disableExtension(item.id, { idempotency_key: key }, { idempotencyKey: key })); }}>停用</button>}{item.state !== 'enabled' && <button type="button" className="btn btn-ghost" disabled={busy || disconnected} onClick={() => setConfirmUninstall({ kind: 'extension', id: item.id })}>卸载</button>}</div></article>)}</details></div>
    <Modal isOpen={Boolean(confirmUninstall)} portal title="确认卸载" onClose={() => { if (!busy) setConfirmUninstall(undefined); }} footer={<><button type="button" className="btn btn-secondary" disabled={busy} onClick={() => setConfirmUninstall(undefined)}>取消</button><button type="button" className="btn btn-danger" disabled={busy || disconnected} onClick={uninstallConfirmed}>确认卸载</button></>}><p>卸载{confirmUninstall?.kind === 'driver' ? '驱动' : '扩展'}“{confirmUninstall?.id}”？已停用的安装记录将从 Core 移除。</p></Modal>
  </section>;
};
