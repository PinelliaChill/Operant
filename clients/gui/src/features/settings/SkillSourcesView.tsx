import React, { useCallback, useEffect, useRef, useState } from 'react';
import { OnboardingError, type SkillSourceView } from '../../../../../sdk/typescript-client/onboarding';
import { PathInput } from '../../components/PathInput';
import { useOnboarding } from '../../live/OnboardingContext';
import { skillIssueCopy } from './skillIssueCopy';
import './skill-sources.css';

export const SkillSourcesView: React.FC = () => {
  const { client } = useOnboarding();
  const [items, setItems] = useState<SkillSourceView[]>([]);
  const [path, setPath] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [unknown, setUnknown] = useState(false);
  const dispatching = useRef(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try { const page = await client.listSkillSources(); setItems(page.items); setError(''); return true; }
    catch (cause: unknown) { setError(cause instanceof Error ? cause.message : '技能来源读取失败。'); return false; }
    finally { setLoading(false); }
  }, [client]);
  useEffect(() => { void refresh(); }, [refresh]);

  const write = async (run: () => Promise<unknown>) => {
    if (dispatching.current || unknown) return false;
    dispatching.current = true; setBusy(true); setError('');
    try { await run(); await refresh(); return true; }
    catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : '修改技能来源失败。');
      if (!(cause instanceof OnboardingError) || cause.code === 'transport_unavailable' || cause.recovery === 'manual_reconcile') setUnknown(true);
      return false;
    } finally { dispatching.current = false; setBusy(false); }
  };

  return <div className="skill-sources"><header><h1>技能来源</h1><p>本机已有的技能目录会自动列出。也可以添加其他目录。</p></header>
    {error && <div className="live-alert live-alert-error" role="alert">{error}</div>}
    {unknown && <div className="live-alert live-alert-warn" role="alert">修改结果尚未确认。请刷新列表，核对后再继续。<button type="button" className="btn btn-secondary btn-sm" onClick={() => { void refresh().then((ok) => { if (ok) setUnknown(false); }); }}>刷新并核对</button></div>}
    <section className="skill-sources-card"><h2>已登记目录</h2>{loading ? <p role="status">正在读取…</p> : items.length ? <ul>{items.map((item) => <li key={item.root_ref}><div><strong>{item.label}</strong><span>{!item.exists ? '目录未找到' : item.enabled ? '可用' : '未启用'}</span><small>{item.path}</small>{!item.exists && <small>{skillIssueCopy(item.issue || '目录不存在或不可读取').guidance}</small>}{item.exists && item.issue && <><small role="alert">{skillIssueCopy(item.issue).skillName ? `${skillIssueCopy(item.issue).skillName}：` : ''}{skillIssueCopy(item.issue).guidance}</small><details><summary>原始问题</summary><code>{item.issue}</code></details></>}</div><button type="button" className="btn btn-ghost btn-sm" disabled={busy || unknown} onClick={() => { if (window.confirm(`移除技能来源“${item.label}”？`)) void write(() => client.removeSkillSource(item.root_ref, { idempotencyKey: crypto.randomUUID() })); }}>移除</button></li>)}</ul> : <p>尚未找到技能目录。添加目录后可发现其中的技能。</p>}</section>
    <form className="skill-sources-card" onSubmit={(event) => { event.preventDefault(); if (path.trim()) void write(() => client.addSkillSource({ path: path.trim() }, { idempotencyKey: crypto.randomUUID() })).then((ok) => { if (ok) setPath(''); }); }}><h2>添加目录</h2><PathInput label="技能文件夹" value={path} onChange={setPath} placeholder="选择文件夹或填写完整路径" disabled={busy || unknown} /><button type="submit" className="btn btn-primary" disabled={busy || unknown || !path.trim()}>添加来源</button></form>
  </div>;
};
