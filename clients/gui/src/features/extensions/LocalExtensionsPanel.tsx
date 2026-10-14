import React, { useCallback, useEffect, useRef, useState } from 'react';
import { AlertTriangle, RefreshCw } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import { useOperant } from '../../context/ClientContext';
import { Modal } from '../../components/Modal';
import { SearchSelect } from '../../components/SearchSelect';
import { useOnboarding } from '../../live/OnboardingContext';
import { useLive } from '../../live/LiveContext';
import { currentBrowserOrigin } from '../../lib/liveBaseUrl';
import type { LocalApplication } from '../../../../../sdk/typescript-client/onboarding';
import { isWriteOutcomeUnknown } from '../../lib/writeOutcome';
import { localStateLabel } from '../../lib/statusCopy';
import { approvalId, idempotencyKey, LOCAL_EXTENSION_CHANGE_EVENT, optionalText, projection, projectionItems, requestCode, requestError, requiredText, stringList } from './localProjection';
import { approvalStillAllowsContinuation, chooseConversationControlSession, confirmedControlOpen, controlApprovalAction, controlOpenStorageKey, controlRouteStillCurrent, openedControlCanStartConversation, readPendingControlOpen, sameControlTargets, type ControlSessionChoice, type PendingControlOpen } from './conversationLocalControl';
import { visibleOnboardingError } from '../../live/createRequestRecovery';

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

function mapControlSession(value: Record<string, unknown>): ControlSessionChoice {
  return {
    sessionId: requiredText(value.session_id, 'session_id'), pluginId: requiredText(value.plugin_id, 'plugin_id'),
    state: requiredText(value.state, 'state'), computerBundleId: optionalText(value.computer_bundle_id),
    allowedTargets: Array.isArray(value.allowed_targets) ? stringList(value.allowed_targets, 'allowed_targets') : undefined,
  };
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
  const navigate = useNavigate();
  const { phase56Client, phase45Client, connectionStatus } = useOperant();
  const { client: onboardingClient, setup, initializeConversation, getRouteRevision, createBusy, createOutcomeUnknown, createError, metadataWarning, recoveredConversationId, clearCreateUncertainty } = useOnboarding();
  const onboardingError = visibleOnboardingError(createError, metadataWarning);
  const { selectedProjectId } = useLive();
  const selectedProjectIdRef = useRef(selectedProjectId);
  selectedProjectIdRef.current = selectedProjectId;
  const [localApps, setLocalApps] = useState<LocalApplication[]>([]);
  const [localAppsError, setLocalAppsError] = useState('');
  const [plugins, setPlugins] = useState<Plugin[]>([]);
  const [conversationSessions, setConversationSessions] = useState<ControlSessionChoice[]>([]);
  const [manualSessions, setManualSessions] = useState<ControlSessionChoice[]>([]);
  const [sessionsLoaded, setSessionsLoaded] = useState(false);
  const [computerTargets, setComputerTargets] = useState<Record<string, string>>({});
  const [conversationBusy, setConversationBusy] = useState(false);
  const conversationInFlight = useRef(false);
  const [conversationError, setConversationError] = useState('');
  const [conversationNotice, setConversationNotice] = useState('');
  const controlStorageKey = controlOpenStorageKey(currentBrowserOrigin());
  const [pendingControlOpen, setPendingControlOpen] = useState<PendingControlOpen | null>(() => {
    try {
      const raw = localStorage.getItem(controlOpenStorageKey(currentBrowserOrigin()));
      return readPendingControlOpen(raw) ?? (raw ? { key: '', pluginId: '', expectedTargets: [] } : null);
    } catch { return { key: '', pluginId: '', expectedTargets: [] }; }
  });
  const [controlApprovalStatus, setControlApprovalStatus] = useState<'checking' | 'pending' | 'approved' | 'denied' | 'expired' | 'consumed' | 'error' | null>(null);
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
    setSessionsLoaded(false);
    const [localPage, extensionPage, conversationPage, localSessionPage] = await Promise.all([
      phase56Client.listLocalCapabilityPlugins(), phase56Client.listExtensions(),
      onboardingClient.listConversationLocalControlSessions(), phase56Client.listLocalControlSessions(),
    ]);
    setPlugins(projectionItems(localPage, '本机能力插件').map(mapPlugin));
    setExternal(projectionItems(extensionPage, '外部扩展').map(mapExternal));
    const conversation = conversationPage.items.map((item) => mapControlSession(projection(item, '对话控制会话')));
    const conversationIds = new Set(conversation.map((item) => item.sessionId));
    setConversationSessions(conversation);
    setManualSessions(projectionItems(localSessionPage, '手动控制会话').map(mapControlSession).filter((item) => !conversationIds.has(item.sessionId)));
    setSessionsLoaded(true);
    setGrants({});
  }, [onboardingClient, phase56Client]);
  useEffect(() => { void refresh().catch((reason: unknown) => setError(requestError(reason))); }, [refresh]);
  useEffect(() => {
    const changed = () => { void refresh().catch((reason: unknown) => setConversationError(`控制会话读取失败：${requestError(reason)}。请刷新后重试。`)); };
    window.addEventListener(LOCAL_EXTENSION_CHANGE_EVENT, changed);
    return () => window.removeEventListener(LOCAL_EXTENSION_CHANGE_EVENT, changed);
  }, [refresh]);

  const rememberControlOpen = (request: PendingControlOpen | null) => {
    if (request) localStorage.setItem(controlStorageKey, JSON.stringify(request));
    else localStorage.removeItem(controlStorageKey);
    setPendingControlOpen(request);
    if (!request?.approvalId) setControlApprovalStatus(null);
  };

  const checkControlApproval = async (request: PendingControlOpen) => {
    if (!request.approvalId) return;
    setControlApprovalStatus('checking');
    try {
      const result = await phase45Client.getPhase45Approval(request.approvalId);
      if (result.approval_id !== request.approvalId) throw new Error('审批记录不匹配，请刷新核对。');
      const status = requiredText(result.status, '审批状态');
      if (!['pending', 'approved', 'denied', 'expired', 'consumed'].includes(status)) throw new Error('审批状态暂不可识别，请稍后刷新核对。');
      if (status === 'denied' || status === 'expired') {
        rememberControlOpen(null);
        setConversationNotice(status === 'denied' ? '开启本机控制的申请已被拒绝。' : '开启本机控制的申请已过期。');
      } else setControlApprovalStatus(status as 'pending' | 'approved' | 'consumed');
    } catch (reason: unknown) {
      setControlApprovalStatus('error');
      setConversationError(`审批状态未确认：${requestError(reason)}。请稍后刷新，不要重复决定。`);
    }
  };

  useEffect(() => {
    if (connectionStatus !== 'connected' || !pendingControlOpen?.approvalId) return;
    void checkControlApproval(pendingControlOpen);
  }, [connectionStatus, pendingControlOpen?.approvalId]);

  const createConversationWithControl = async (sessionId: string, stillHere: () => boolean) => {
    if (!stillHere()) {
      setConversationNotice('控制会话已准备好。返回本页后可继续创建对话。');
      return;
    }
    const result = await initializeConversation({
      ...(selectedProjectId ? { workspace_id: selectedProjectId } : {}),
      local_control_session_ids: [sessionId],
    });
    if (!result) {
      setConversationError('对话创建未完成。请按页面提示核对原请求；控制会话不会自动再次开启。');
      return;
    }
    if (stillHere()) navigate(`/chat/${encodeURIComponent(result.thread_id)}`);
    else setConversationNotice('对话已创建。请刷新对话列表后打开，当前页面不会被切换。');
  };

  const openConversationControl = async (request: PendingControlOpen): Promise<string | null> => {
    try {
      rememberControlOpen(request);
    } catch {
      setConversationError('无法保存本次开启请求的核对标识。请检查本地存储权限后重试。');
      return null;
    }
    try {
      const result = await onboardingClient.openConversationLocalControl({
        plugin_id: request.pluginId as 'operant.chrome.browser' | 'operant.macos.computer',
        ...(request.computerBundleId ? { computer_bundle_id: request.computerBundleId } : {}),
      }, { idempotencyKey: request.key });
      rememberControlOpen(null);
      if (!openedControlCanStartConversation(mapControlSession(projection(result, '控制会话')), request)) {
        setConversationError('控制会话已开启，但目标范围或状态与本次选择不一致。请刷新核对，暂不创建对话。');
        try { await refresh(); } catch { /* The known result still cannot be attached. */ }
        return null;
      }
      try { await refresh(); }
      catch (reason: unknown) { setConversationNotice(`控制会话已开启，但列表暂时无法更新：${requestError(reason)}。新对话仍会核对该会话。`); }
      return result.session_id;
    } catch (reason: unknown) {
      const approval = approvalId(reason);
      if (requestCode(reason) === 'approval_required' && approval) {
        rememberControlOpen({ ...request, approvalId: approval });
        setControlApprovalStatus('pending');
        setConversationError('开启本机控制需要你确认。批准后会继续原请求。');
      } else if (isWriteOutcomeUnknown(reason)) {
        setConversationError('没有收到控制会话的开启结果。请刷新并核对原请求，暂时不要重试。');
      } else {
        try { rememberControlOpen(null); } catch { /* Keep the safe recovery barrier. */ }
        setConversationError(`无法开启本机控制。${requestError(reason)}`);
      }
      return null;
    }
  };

  const startConversation = async (plugin: Plugin) => {
    if (conversationInFlight.current || disconnected || createBusy || createOutcomeUnknown || pendingControlOpen) return;
    conversationInFlight.current = true;
    setConversationBusy(true); setConversationError(''); setConversationNotice('');
    const routeRevision = getRouteRevision();
    const locationHref = window.location.href;
    const workspaceId = selectedProjectId;
    const clickedRoute = { revision: routeRevision, href: locationHref, workspaceId };
    const stillHere = () => controlRouteStillCurrent(clickedRoute, { revision: getRouteRevision(), href: window.location.href, workspaceId: selectedProjectIdRef.current });
    try {
      if (!setup?.ready) throw new Error('请先完成模型连接和首次设置，再创建对话。');
      const [pluginPage, conversationPage, localSessionPage] = await Promise.all([
        phase56Client.listLocalCapabilityPlugins(), onboardingClient.listConversationLocalControlSessions(), phase56Client.listLocalControlSessions(),
      ]);
      const freshPlugin = projectionItems(pluginPage, '本机能力插件').map(mapPlugin).find((item) => item.id === plugin.id);
      setPlugins(projectionItems(pluginPage, '本机能力插件').map(mapPlugin));
      if (!freshPlugin || freshPlugin.state !== 'enabled') throw new Error('此能力尚未启用，请先安装并启用。');
      if (!sameControlTargets(freshPlugin.targets, plugin.targets)) throw new Error('授权目标已变化。请核对列表后重试。');
      const target = plugin.id === 'operant.macos.computer' ? (computerTargets[plugin.id] || (plugin.targets.length === 1 ? plugin.targets[0] : '')) : undefined;
      if (plugin.id === 'operant.macos.computer' && (!target || !freshPlugin.targets.includes(target))) throw new Error('请先选择已授权的应用。');
      const expectedTargets = target ? [target] : [...plugin.targets];
      const conversation = conversationPage.items.map((item) => mapControlSession(projection(item, '对话控制会话')));
      const conversationIds = new Set(conversation.map((item) => item.sessionId));
      const manual = projectionItems(localSessionPage, '手动控制会话').map(mapControlSession).filter((item) => !conversationIds.has(item.sessionId));
      setConversationSessions(conversation); setManualSessions(manual);
      const choice = chooseConversationControlSession(plugin.id, target, expectedTargets, conversation, manual);
      if (choice.kind === 'blocked') throw new Error(choice.reason);
      if (!stillHere()) return;
      const sessionId = choice.kind === 'reuse' ? choice.sessionId : await openConversationControl({ key: idempotencyKey(), pluginId: plugin.id, expectedTargets, ...(target ? { computerBundleId: target } : {}) });
      if (sessionId) await createConversationWithControl(sessionId, stillHere);
    } catch (reason: unknown) {
      setConversationError(requestError(reason));
    } finally {
      conversationInFlight.current = false;
      setConversationBusy(false);
    }
  };

  const reconcileControlOpen = async () => {
    if (!pendingControlOpen || conversationInFlight.current || disconnected) return;
    conversationInFlight.current = true; setConversationBusy(true); setConversationError('');
    try {
      if (!pendingControlOpen.key) throw new Error('本地核对标识无法读取。请到本机操控台人工核对，勿再次开启。');
      const page = await onboardingClient.listConversationLocalControlSessions({ requestId: pendingControlOpen.key });
      const confirmed = confirmedControlOpen(page.items.map((item) => mapControlSession(projection(item, '原控制会话'))), pendingControlOpen);
      if (!confirmed) {
        setConversationError('原请求尚未确认，或控制会话的目标与状态已变化。请稍后刷新或人工核对，勿再次开启。');
        return;
      }
      rememberControlOpen(null);
      await refresh();
      if (confirmed.state === 'active') setConversationNotice('已找到原请求开启的控制会话。现在可以使用对应能力创建新对话。');
      else setConversationError('已确认原请求曾开启控制会话，但会话现在不可用。请先核对并结束旧会话，再主动开启新的控制会话。');
    } catch (reason: unknown) {
      setConversationError(`原请求核对失败：${requestError(reason)}。请恢复连接后再刷新，不要重复开启。`);
    } finally { conversationInFlight.current = false; setConversationBusy(false); }
  };

  const decideControlApproval = async (approved: boolean) => {
    if (!pendingControlOpen?.approvalId || controlApprovalStatus !== 'pending' || pendingControlOpen.decisionSubmitted || conversationInFlight.current || disconnected) return;
    conversationInFlight.current = true; setConversationBusy(true); setConversationError('');
    const request = { ...pendingControlOpen, decisionSubmitted: true };
    try {
      rememberControlOpen(request);
      await phase45Client.decidePhase45Approval(request.approvalId!, { approved }, { idempotencyKey: idempotencyKey() });
      await checkControlApproval(request);
    } catch (reason: unknown) {
      setControlApprovalStatus('error');
      setConversationError(`审批决定结果尚未确认：${requestError(reason)}。请刷新审批状态，不要重复决定。`);
    } finally { conversationInFlight.current = false; setConversationBusy(false); }
  };

  const continueApprovedControlOpen = async () => {
    const request = pendingControlOpen;
    if (!request?.approvalId || controlApprovalStatus !== 'approved' || conversationInFlight.current || createBusy || createOutcomeUnknown || disconnected) return;
    conversationInFlight.current = true; setConversationBusy(true); setConversationError('');
    const clickedRoute = { revision: getRouteRevision(), href: window.location.href, workspaceId: selectedProjectId };
    const stillHere = () => controlRouteStillCurrent(clickedRoute, { revision: getRouteRevision(), href: window.location.href, workspaceId: selectedProjectIdRef.current });
    try {
      const approval = await phase45Client.getPhase45Approval(request.approvalId);
      if (!approvalStillAllowsContinuation(approval, request.approvalId)) {
        setControlApprovalStatus(typeof approval.status === 'string' && ['pending', 'consumed', 'denied', 'expired'].includes(approval.status) ? approval.status as 'pending' | 'consumed' | 'denied' | 'expired' : 'error');
        throw new Error('审批状态已变化。请刷新并核对原请求。');
      }
      const pluginPage = await phase56Client.listLocalCapabilityPlugins();
      const plugin = projectionItems(pluginPage, '本机能力插件').map(mapPlugin).find((item) => item.id === request.pluginId);
      const expected = request.computerBundleId ? [request.computerBundleId] : plugin?.targets ?? [];
      if (!plugin || plugin.state !== 'enabled' || !sameControlTargets(expected, request.expectedTargets) || (request.computerBundleId && !plugin.targets.includes(request.computerBundleId))) {
        throw new Error('授权目标已变化。请刷新并核对原审批，暂不继续开启。');
      }
      if (!stillHere()) return;
      const sessionId = await openConversationControl({ key: request.key, pluginId: request.pluginId, expectedTargets: request.expectedTargets, ...(request.computerBundleId ? { computerBundleId: request.computerBundleId } : {}) });
      if (sessionId) await createConversationWithControl(sessionId, stillHere);
    } catch (reason: unknown) {
      setConversationError(requestError(reason));
    } finally { conversationInFlight.current = false; setConversationBusy(false); }
  };

  const act = async (label: string, run: () => Promise<unknown>, after?: (result: Record<string, unknown>) => void) => {
    if (busy || disconnected) return;
    setBusy(true); setError(''); setNotice(''); setPending(undefined);
    try {
      const result = projection(await run(), label);
      after?.(result);
      setNotice(`${label}已完成。`);
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
      if (approved) { await resume.run(); setNotice(`${resume.label}已确认。`); await refresh(); window.dispatchEvent(new Event(LOCAL_EXTENSION_CHANGE_EVENT)); }
      else setNotice('已拒绝操作。');
    } catch (reason: unknown) { setError(`审批或原操作失败：${requestError(reason)} 请人工核对。`); }
    finally { setBusy(false); }
  };

  return <section className="local-panel" id="local-driver-setup" aria-labelledby="local-extensions-title"><div className="local-panel-heading"><div><h2 id="local-extensions-title">安装与授权</h2><p>选择允许操作的应用或网站，再安装对应能力。</p></div><button type="button" className="btn btn-secondary" disabled={busy} onClick={() => void refresh().catch((reason: unknown) => setError(requestError(reason)))}><RefreshCw size={14} aria-hidden="true" />刷新</button></div>
    {disconnected && <p className="live-alert live-alert-error" role="alert">连接已断开，扩展管理已暂停。恢复连接后请刷新。</p>}
    {error && <p className="live-alert live-alert-error" role="alert"><AlertTriangle size={14} aria-hidden="true" />{error}</p>}
    {notice && <p className="live-alert" role="status">{notice}</p>}
    {conversationError && <p className="live-alert live-alert-error" role="alert"><AlertTriangle size={14} aria-hidden="true" />{conversationError}</p>}
    {conversationNotice && <p className="live-alert" role="status">{conversationNotice}</p>}
    {createOutcomeUnknown && <div className="local-unknown-stack" role="group" aria-label="核对新对话"><strong>新对话结果待核对</strong><p>{createError || '请按原请求刷新核对，不要重复创建。'}</p><button type="button" className="btn btn-secondary" disabled={conversationBusy || disconnected} onClick={() => void clearCreateUncertainty()}>刷新并核对原请求</button></div>}
    {!createOutcomeUnknown && onboardingError && <p className="live-alert live-alert-error" role="alert">{onboardingError}</p>}
    {recoveredConversationId && <button type="button" className="btn btn-secondary" onClick={() => navigate(`/chat/${encodeURIComponent(recoveredConversationId)}`)}>打开已找到的对话</button>}
    {pendingControlOpen && <div className="local-unknown-stack" role="group" aria-label="核对本机控制会话"><strong>{pendingControlOpen.approvalId ? '本机控制等待审批或核对' : '本机控制结果待核对'}</strong><p>原请求已保存。确认结果前不会开启另一个控制会话。</p><div className="local-actions">{pendingControlOpen.approvalId && controlApprovalAction(controlApprovalStatus, Boolean(pendingControlOpen.decisionSubmitted)) === 'decide' && <><button type="button" className="btn btn-primary" disabled={conversationBusy || disconnected} onClick={() => void decideControlApproval(true)}>批准</button><button type="button" className="btn btn-secondary" disabled={conversationBusy || disconnected} onClick={() => void decideControlApproval(false)}>拒绝</button></>}{pendingControlOpen.approvalId && controlApprovalAction(controlApprovalStatus, Boolean(pendingControlOpen.decisionSubmitted)) === 'continue' && <button type="button" className="btn btn-primary" disabled={conversationBusy || disconnected} onClick={() => void continueApprovedControlOpen()}>继续原请求</button>}{pendingControlOpen.approvalId && <button type="button" className="btn btn-secondary" disabled={conversationBusy || disconnected} onClick={() => void checkControlApproval(pendingControlOpen)}>刷新审批状态</button>}<button type="button" className="btn btn-secondary" disabled={conversationBusy || disconnected || !pendingControlOpen.key} onClick={() => void reconcileControlOpen()}>核对控制会话</button></div><details className="local-advanced"><summary>请求详情</summary><code>{pendingControlOpen.key || '标识不可读取'}</code>{pendingControlOpen.approvalId && <p>审批编号：<code>{pendingControlOpen.approvalId}</code></p>}</details></div>}
    {pending && <div className="local-approval" role="group" aria-label="扩展操作审批"><strong>{pending.label}需要你确认</strong><details><summary>审批详情</summary><span>审批编号：<code>{pending.approvalId}</code></span></details><div className="local-actions"><button type="button" className="btn btn-primary" disabled={busy || disconnected} onClick={() => void decide(true)}>允许并提交</button><button type="button" className="btn btn-danger" disabled={busy || disconnected} onClick={() => void decide(false)}>拒绝</button></div></div>}
    <div className="local-extension-grid"><div className="local-extension-group"><h3>本机能力</h3>{bundledDrivers.map((driver) => { const plugin = plugins.find((item) => item.id === driver.id); const selectedTarget = driver.id === 'operant.macos.computer' ? (computerTargets[driver.id] || (plugin?.targets.length === 1 ? plugin.targets[0] : '')) : undefined; const expectedTargets = selectedTarget ? [selectedTarget] : plugin?.targets ?? []; const choice = chooseConversationControlSession(driver.id, selectedTarget, expectedTargets, conversationSessions, manualSessions); return <article className="local-extension-card" key={driver.id} id={`local-driver-${driver.id}`} tabIndex={-1}><strong>{driver.label}</strong><div className="local-meta"><span>状态：{plugin ? localStateLabel(plugin.state) : '未安装'}</span><span>已授权目标：{plugin?.targets.length || 0} 个</span></div>{driver.id === 'operant.macos.computer' ? <><SearchSelect label="要授权的应用" value={targetInput[driver.id] ?? ''} onChange={(value) => setTargetInput((current) => ({ ...current, [driver.id]: value }))} placeholder="选择本机应用" disabled={busy || disconnected || Boolean(plugin)} options={localApps.map((app) => ({ value: app.bundle_id, label: app.name, detail: app.bundle_id }))} />{localAppsError && <small role="alert">{localAppsError}</small>}</> : <label>要授权的网站地址<input className="input" type="url" value={targetInput[driver.id] ?? ''} onChange={(event) => setTargetInput((current) => ({ ...current, [driver.id]: event.target.value }))} placeholder="https://example.com" disabled={busy || disconnected || Boolean(plugin)} /></label>}
      {plugin?.state === 'enabled' && <div id={`local-conversation-${driver.id}`} tabIndex={-1} className="local-conversation-start">{driver.id === 'operant.chrome.browser' ? <><p>这次对话可访问以下全部网站：</p><ul>{plugin.targets.map((target) => <li key={target}>{target}</li>)}</ul>{plugin.targets.length > 1 && <details><summary>关于网站范围</summary><small>新控制会话沿用以上整组授权网站。</small></details>}</> : <SearchSelect label="这次要操作的应用" value={selectedTarget || ''} onChange={(value) => setComputerTargets((current) => ({ ...current, [driver.id]: value }))} placeholder="选择已授权应用" disabled={conversationBusy || disconnected} options={plugin.targets.map((target) => ({ value: target, label: localApps.find((app) => app.bundle_id === target)?.name || target.split('.').pop() || target, detail: target }))} />}{sessionsLoaded && choice.kind === 'blocked' && <small role="status">{choice.reason}</small>}<button type="button" className="btn btn-primary" disabled={busy || conversationBusy || createBusy || createOutcomeUnknown || disconnected || !sessionsLoaded || Boolean(pendingControlOpen) || !plugin.targets.length || (driver.id === 'operant.macos.computer' && !selectedTarget) || choice.kind === 'blocked'} onClick={() => void startConversation(plugin)}>创建并打开对话</button></div>}
      <details className="local-advanced"><summary>高级：目标与来源详情</summary><label htmlFor={`local-target-${driver.id}`}>{driver.targetHint}</label><input id={`local-target-${driver.id}`} className="input" value={targetInput[driver.id] ?? plugin?.targets.join(', ') ?? ''} onChange={(event) => setTargetInput((current) => ({ ...current, [driver.id]: event.target.value }))} disabled={busy || disconnected || Boolean(plugin)} /><p>驱动编号：<code>{driver.id}</code></p>{plugin && <p>来源摘要：<code>{plugin.digest}</code></p>}</details><div className="local-actions">
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
