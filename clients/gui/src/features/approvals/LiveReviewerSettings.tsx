import React, { useCallback, useEffect, useState } from 'react';
import { RefreshCw, Save } from 'lucide-react';
import { useOperant } from '../../context/ClientContext';
import { sessionControlClient, type ConfigOverride } from '../../live/sessionControlClient';

interface ReviewerConfig {
  mode: 'off' | 'human' | 'auto';
  profile_id: string | null;
  strictness: 'cautious' | 'balanced' | 'permissive';
  custom_rules: string[];
}
const DEFAULT: ReviewerConfig = { mode: 'human', profile_id: null, strictness: 'balanced', custom_rules: [] };

function parseConfig(value: unknown): ReviewerConfig {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return DEFAULT;
  const source = value as Record<string, unknown>;
  if (!['off', 'human', 'auto'].includes(String(source.mode)) || !['cautious', 'balanced', 'permissive'].includes(String(source.strictness))) throw new Error('Core Reviewer 配置格式无效');
  if (!Array.isArray(source.custom_rules) || source.custom_rules.some((rule) => typeof rule !== 'string')) throw new Error('Core Reviewer 自定义规则格式无效');
  return { mode: source.mode as ReviewerConfig['mode'], strictness: source.strictness as ReviewerConfig['strictness'], profile_id: typeof source.profile_id === 'string' ? source.profile_id : null, custom_rules: source.custom_rules as string[] };
}

export const LiveReviewerSettings: React.FC = () => {
  const { b2Client, connectionStatus } = useOperant();
  const [override, setOverride] = useState<ConfigOverride | null>(null);
  const [config, setConfig] = useState<ReviewerConfig>(DEFAULT);
  const [rulesText, setRulesText] = useState('');
  const [models, setModels] = useState<Array<{ id: string; name: string }>>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const load = useCallback(async () => {
    if (connectionStatus !== 'connected') return;
    setBusy(true); setError('');
    try {
      const [next, profiles] = await Promise.all([sessionControlClient.getOverride('global', 'default'), b2Client.listModels()]);
      const value = parseConfig(next.patch.approval_reviewer);
      setOverride(next); setConfig(value); setRulesText(value.custom_rules.join('\n'));
      setModels(profiles.filter((model) => model.id && model.enabled !== false).map((model) => ({ id: model.id!, name: model.name })));
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Reviewer 配置读取失败'); }
    finally { setBusy(false); }
  }, [b2Client, connectionStatus]);
  useEffect(() => { void load(); }, [load]);

  const save = async () => {
    if (!override || busy) return;
    if (config.mode === 'auto' && !config.profile_id) { setError('自动审批必须选择独立模型 Profile。'); return; }
    setBusy(true); setError(''); setNotice('');
    try {
      const custom_rules = rulesText.split('\n').map((rule) => rule.trim()).filter(Boolean);
      await sessionControlClient.saveOverride('global', 'default', { ...override.patch, approval_reviewer: { ...config, custom_rules } }, override.revision);
      await load();
      setNotice('Reviewer 配置已保存。硬 DENY 始终优先；模型不可用时交由人工处理。');
    } catch (cause) { setError(cause instanceof Error ? cause.message : '保存失败，请刷新配置后重试'); }
    finally { setBusy(false); }
  };

  return <section className="config-card" aria-labelledby="reviewer-config-heading" data-client-mode="live">
    <div className="config-field-head"><h2 id="reviewer-config-heading">审批 Reviewer</h2><button type="button" className="btn btn-ghost btn-sm" onClick={() => void load()} disabled={busy}><RefreshCw size={13} />刷新</button></div>
    <p className="config-hint">仅处理 Policy 判为 ASK 的申请。Reviewer 无权覆盖 DENY；模型失败或无结论时由人工决定。</p>
    {error && <div className="live-alert live-alert-error" role="alert">{error}</div>}
    {notice && <div className="live-alert" role="status">{notice}</div>}
    {override && <><div className="config-grid">
      <label>处理方式<select className="select" value={config.mode} onChange={(event) => setConfig((current) => ({ ...current, mode: event.target.value as ReviewerConfig['mode'] }))}><option value="off">关闭自动审核</option><option value="human">始终人工处理</option><option value="auto">ASK 自动触发 Reviewer</option></select></label>
      <label>独立模型 Profile<select className="select" value={config.profile_id || ''} onChange={(event) => setConfig((current) => ({ ...current, profile_id: event.target.value || null }))} disabled={config.mode !== 'auto'}><option value="">选择模型</option>{models.map((model) => <option key={model.id} value={model.id}>{model.name} · {model.id}</option>)}</select></label>
      <label>严格度<select className="select" value={config.strictness} onChange={(event) => setConfig((current) => ({ ...current, strictness: event.target.value as ReviewerConfig['strictness'] }))}><option value="cautious">谨慎</option><option value="balanced">平衡</option><option value="permissive">宽松</option></select></label>
    </div><label className="config-rules-label">自定义规则（每行一条）<textarea className="input config-textarea" value={rulesText} onChange={(event) => setRulesText(event.target.value)} placeholder="写明审核要求，不输入密钥或敏感内容" /></label><div className="config-actions"><button type="button" className="btn btn-primary" onClick={() => void save()} disabled={busy || connectionStatus !== 'connected'}><Save size={14} />保存 Reviewer 配置</button><span className="config-hint">全局修订号 {override.revision}</span></div></>}
  </section>;
};
