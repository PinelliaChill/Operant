import { PathInput } from '../../components/PathInput';
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { RefreshCw, RotateCcw, Save } from 'lucide-react';
import { useOperant } from '../../context/ClientContext';
import { sessionControlClient, type ConfigField, type ConfigOverride, type ConfigScope, type ConfigValues, type EffectiveConfig } from '../../live/sessionControlClient';
import './live-config.css';

const FIELDS: Array<{ key: ConfigField; label: string; format: 'text' | 'number' | 'json' }> = [
  { key: 'system_prompt', label: '系统提示词', format: 'text' },
  { key: 'model_profile_id', label: '模型配置', format: 'text' },
  { key: 'effort', label: '推理强度', format: 'text' },
  { key: 'tool_policy', label: '工具权限', format: 'json' },
  { key: 'budget', label: '运行预算', format: 'json' },
  { key: 'temperature', label: '温度', format: 'number' },
  { key: 'skill_ids', label: '使用技能', format: 'json' },
  { key: 'mcp_server_ids', label: '外部工具服务', format: 'json' },
];

function display(value: unknown): string {
  if (value === undefined || value === null) return '';
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2);
}

function parseField(key: ConfigField, raw: string): unknown {
  const format = FIELDS.find((field) => field.key === key)?.format;
  if (format === 'json') return JSON.parse(raw);
  if (format === 'number') {
    const number = Number(raw);
    if (!Number.isFinite(number)) throw new Error('温度必须为有效数字');
    return number;
  }
  return raw;
}

export const LiveConfigSettings: React.FC = () => {
  const { b2Client, connectionStatus } = useOperant();
  const [scope, setScope] = useState<ConfigScope>('global');
  const [projectId, setProjectId] = useState('');
  const [workspaceRef, setWorkspaceRef] = useState('');
  const [roleId, setRoleId] = useState('');
  const [models, setModels] = useState<Array<{ id: string; name: string }>>([]);
  const [roles, setRoles] = useState<Array<{ id: string; name: string }>>([]);
  const [effective, setEffective] = useState<EffectiveConfig | null>(null);
  const [override, setOverride] = useState<ConfigOverride | null>(null);
  const [editing, setEditing] = useState<Record<string, string>>({});
  const [selected, setSelected] = useState<Set<ConfigField>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const currentId = effective?.scopes[scope]?.scope_id || '';

  const read = useCallback(async () => {
    if (connectionStatus !== 'connected') return;
    setBusy(true); setError('');
    try {
      await b2Client.negotiateProtocol();
      const [nextModels, nextRoles] = await Promise.all([b2Client.listModels(), b2Client.listRoles()]);
      const nextEffective = roleId.trim() ? await sessionControlClient.effective({ project_id: projectId.trim() || undefined, workspace_ref: workspaceRef.trim() || undefined, role_id: roleId.trim() }) : null;
      const nextOverride = nextEffective?.scopes[scope] ?? null;
      setEffective(nextEffective);
      setOverride(nextOverride);
      setEditing(Object.fromEntries(FIELDS.map(({ key }) => [key, display(nextOverride?.patch[key])])));
      setSelected(new Set(FIELDS.filter(({ key }) => Object.hasOwn(nextOverride?.patch ?? {}, key)).map(({ key }) => key)));
      setModels(nextModels.filter((model) => model.id).map((model) => ({ id: model.id!, name: model.name })));
      setRoles(nextRoles.filter((role) => role.id).map((role) => ({ id: role.id!, name: role.name })));
    } catch (cause) { setError(cause instanceof Error ? cause.message : '配置查询失败'); }
    finally { setBusy(false); }
  }, [b2Client, connectionStatus, projectId, roleId, scope, workspaceRef]);

  useEffect(() => { void read(); }, [read]);
  const sourceText = (field: ConfigField) => {
    const source = effective?.sources[field];
    return source ? `${source.scope_type} · ${source.scope_id}` : '来源未知';
  };
  const parsedPatch = useMemo(() => {
    try {
      const patch: ConfigValues = {};
      for (const key of selected) patch[key] = parseField(key, editing[key] ?? '');
      return { patch, error: '' };
    } catch (cause) { return { patch: null, error: cause instanceof Error ? cause.message : '覆盖值格式无效' }; }
  }, [editing, selected]);
  const save = async () => {
    if (!override || !currentId || !parsedPatch.patch || busy) return;
    setBusy(true); setError(''); setNotice('');
    try {
      await sessionControlClient.saveOverride(scope, currentId, { ...parsedPatch.patch, ...(override.patch.approval_reviewer === undefined ? {} : { approval_reviewer: override.patch.approval_reviewer }) }, override.revision);
      await read();
      setNotice('设置已保存，对新会话生效。');
    } catch (cause) { setError(cause instanceof Error ? cause.message : '保存失败，请刷新后重试'); }
    finally { setBusy(false); }
  };
  const reset = async () => {
    if (!override || !currentId || busy || !window.confirm('清除当前层的全部覆盖并恢复继承？')) return;
    setBusy(true); setError(''); setNotice('');
    try {
      await sessionControlClient.resetOverride(scope, currentId, override.revision);
      await read();
      setNotice('已恢复默认设置，对新会话生效。');
    } catch (cause) { setError(cause instanceof Error ? cause.message : '重置失败，请刷新后重试'); }
    finally { setBusy(false); }
  };

  return <div className="section-view config-page" data-client-mode="live">
    <header className="section-header"><div><h1 className="section-title">运行设置</h1><p className="section-sub">修改仅对新会话生效。</p></div><button type="button" className="btn btn-ghost btn-sm" onClick={() => { setNotice(''); void read(); }} disabled={busy}><RefreshCw size={14} />刷新</button></header>
    <div className="section-scroll"><div className="section-inner config-inner">
      <section className="config-card" aria-label="配置层级"><div className="config-grid">
        <label>编辑层级<select className="select" value={scope} onChange={(event) => setScope(event.target.value as ConfigScope)}><option value="global">全局</option><option value="project">项目</option><option value="workspace">工作区</option><option value="role">角色</option></select></label>
        <label>项目（可选）<input className="input" value={projectId} onChange={(event) => setProjectId(event.target.value)} placeholder="项目编号" /></label>
        <PathInput label="项目文件夹（可选）" value={workspaceRef} onChange={setWorkspaceRef} placeholder="选择文件夹或填写完整路径" disabled={busy} />
        <label>角色<select className="select" value={roleId} onChange={(event) => setRoleId(event.target.value)}><option value="">选择角色</option>{roles.map((role) => <option key={role.id} value={role.id}>{role.name} · {role.id}</option>)}</select></label>
      </div></section>
      {error && <div className="live-alert live-alert-error" role="alert">{error}</div>}
      {notice && <div className="live-alert" role="status">{notice}</div>}
      {!roleId ? <p role="status">请选择角色以查询有效配置。</p> : busy && !effective ? <p role="status">正在读取配置…</p> : effective && !override ? <p role="status">此上下文没有 {scope} 层；请先填写对应项目或工作区。</p> : effective && override ? <>
        <div className="config-fields">{FIELDS.filter((field) => field.key !== 'temperature' || Object.hasOwn(effective.values, 'temperature')).map(({ key, label, format }) => <section className="config-card" key={key}>
          <div className="config-field-head"><h2>{label}</h2><span>有效来源：{sourceText(key)}</span></div>
          {key === 'system_prompt' && effective.prompt_sources.length > 1 && <p className="config-hint">提示词合成顺序：{effective.prompt_sources.map((source) => `${source.scope_type} · ${source.scope_id}`).join(' → ')}</p>}
          <p className="config-effective">当前有效值：<code>{display(effective.values[key]) || '未设置'}</code></p>
          <label className="config-checkbox"><input type="checkbox" checked={selected.has(key)} onChange={(event) => setSelected((current) => { const next = new Set(current); if (event.target.checked) next.add(key); else next.delete(key); return next; })} />在当前层覆盖</label>
          {selected.has(key) && (key === 'model_profile_id' ? <select className="select" aria-label={`${label}覆盖值`} value={editing[key] ?? ''} onChange={(event) => setEditing((current) => ({ ...current, [key]: event.target.value }))}><option value="">选择模型配置</option>{models.map((model) => <option key={model.id} value={model.id}>{model.name} · {model.id}</option>)}</select> : key === 'effort' ? <select className="select" aria-label={`${label}覆盖值`} value={editing[key] ?? ''} onChange={(event) => setEditing((current) => ({ ...current, [key]: event.target.value }))}><option value="">选择推理强度</option><option value="low">low</option><option value="medium">medium</option><option value="high">high</option></select> : format === 'text' || format === 'json' ? <textarea className="input config-textarea" aria-label={`${label}覆盖值`} value={editing[key] ?? ''} onChange={(event) => setEditing((current) => ({ ...current, [key]: event.target.value }))} spellCheck={false} /> : <input className="input" type="number" step="any" aria-label={`${label}覆盖值`} value={editing[key] ?? ''} onChange={(event) => setEditing((current) => ({ ...current, [key]: event.target.value }))} />)}
        </section>)}</div>
        {parsedPatch.error && <div className="live-alert live-alert-error" role="alert">覆盖值格式错误：{parsedPatch.error}</div>}
        <div className="config-actions"><button type="button" className="btn btn-primary" onClick={() => void save()} disabled={busy || !currentId || !parsedPatch.patch || Boolean(parsedPatch.error)}><Save size={14} />保存当前层</button><button type="button" className="btn btn-secondary" onClick={() => void reset()} disabled={busy || Object.keys(override.patch).length === 0}><RotateCcw size={14} />清除当前层覆盖</button></div>
      </> : null}
    </div></div>
  </div>;
};
