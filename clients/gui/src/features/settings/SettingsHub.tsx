import React from 'react';
import { Navigate, useSearchParams } from 'react-router-dom';
import { Bot, Brain, Cpu, Settings2, ShieldCheck, Sliders, Wrench } from 'lucide-react';
import { useOperant } from '../../context/ClientContext';
import { AgentsView } from '../agents/AgentsView';
import { LiveManagementView } from '../management/LiveManagementView';
import { PolicySettings } from '../approvals/PolicySettings';
import { RemoteView } from '../remote/RemoteView';
import { ExtensionsView } from '../extensions/ExtensionsView';
import { LiveConfigSettings } from './LiveConfigSettings';
import { LiveModelConnections } from './LiveModelConnections';
import { SkillSourcesView } from './SkillSourcesView';
import { SearchSelect } from '../../components/SearchSelect';
import './settings-hub.css';

type Section = 'general' | 'models' | 'roles' | 'tools' | 'memory' | 'advanced';
const SECTIONS = [
  { key: 'general', label: '常规', icon: Sliders },
  { key: 'models', label: '模型连接', icon: Cpu },
  { key: 'roles', label: '助手与角色', icon: Bot },
  { key: 'tools', label: '工具与扩展', icon: Wrench },
  { key: 'memory', label: '记忆', icon: Brain },
  { key: 'advanced', label: '权限与高级', icon: ShieldCheck },
] as const;

const LEGACY: Record<string, Section> = {
  general: 'general', models: 'models', roles: 'roles', agents: 'roles',
  plugins: 'tools', plugin: 'tools', skills: 'tools', skill: 'tools', tools: 'tools',
  retention: 'memory',
  security: 'advanced', approvals: 'advanced', remote: 'advanced',
  system: 'advanced', config: 'advanced', settings: 'general',
};

function SettingPanel({ title, children }: { title: string; children: React.ReactNode }) {
  return <div className="settings-hub-panel"><h1>{title}</h1>{children}</div>;
}

export const SettingsHub: React.FC = () => {
  const { clientMode, setClientMode, theme, setTheme } = useOperant();
  const [params, setParams] = useSearchParams();
  const old = params.get('tab') || params.get('cat');
  const section = params.get('section');
  if (!section && (old === 'knowledge' || old === 'memory')) return <Navigate to="/projects?tab=knowledge" replace />;
  if (!section && old === 'projects') return <Navigate to="/projects" replace />;
  if (!section && old === 'approvals') return <Navigate to="/tasks?tab=approvals" replace />;
  if (!section && old === 'schedules') return <Navigate to="/tasks?tab=schedules" replace />;
  if (!section && old === 'workflow') return <Navigate to="/collab" replace />;
  if (!section && (old === 'plugins' || old === 'plugin')) return <Navigate to="/settings?section=tools&page=plugins" replace />;
  if (!section && (old === 'skills' || old === 'skill')) return <Navigate to="/settings?section=tools&page=skills" replace />;
  if (!section && (old === 'extensions' || old === 'tools')) return <Navigate to="/settings?section=tools&page=local" replace />;
  if (!section && old && LEGACY[old]) return <Navigate to={`/settings?section=${LEGACY[old]}`} replace />;
  const current: Section = SECTIONS.some((entry) => entry.key === section) ? section as Section : 'general';
  const page = params.get('page') || '';
  const choose = (key: Section) => setParams({ section: key });
  const subnav = (items: readonly (readonly [string, string])[]) => <nav className="settings-hub-subnav" aria-label="设置内容">{items.map(([key, label]) => <button key={key} type="button" className={page === key || (!page && key === items[0]?.[0]) ? 'active' : ''} aria-current={page === key || (!page && key === items[0]?.[0]) ? 'page' : undefined} onClick={() => setParams({ section: current, page: key })}>{label}</button>)}</nav>;
  return <div className="settings-hub" data-client-mode={clientMode}>
    <nav className="settings-hub-nav" aria-label="设置分类">
      <h1>设置</h1>
      {SECTIONS.map(({ key, label, icon: Icon }) => <button key={key} type="button" className={`settings-hub-nav-item${key === current ? ' active' : ''}`} aria-current={key === current ? 'page' : undefined} onClick={() => choose(key)}><Icon size={17} aria-hidden="true" />{label}</button>)}
    </nav>
    <div className="settings-hub-content">
      {current === 'general' && <SettingPanel title="常规">
        <section className="settings-hub-card"><h2>外观</h2><p>选择适合你的界面外观。</p><SearchSelect label="主题" value={theme} onChange={(value) => setTheme(value as 'light' | 'dark')} options={[{ value: 'light', label: '浅色' }, { value: 'dark', label: '深色' }]} /></section>
        <details className="settings-hub-card"><summary>开发与演示</summary><p>演示模式使用本地示例数据，适合体验界面。</p><SearchSelect label="数据来源" value={clientMode} onChange={(value) => setClientMode(value as 'live' | 'mock')} options={[{ value: 'live', label: '真实连接' }, { value: 'mock', label: '演示数据' }]} /></details>
      </SettingPanel>}
      {current === 'models' && (clientMode === 'live' ? <LiveModelConnections /> : <SettingPanel title="模型连接"><p>演示模式使用示例数据。切换到真实连接后可添加模型。</p></SettingPanel>)}
      {current === 'roles' && <AgentsView roleOnly />}
      {current === 'tools' && <>{subnav([['skills', '技能'], ['sources', '技能来源'], ['plugins', '扩展'], ['local', '本机能力']])}{clientMode === 'live' ? page === 'sources' ? <SkillSourcesView /> : page === 'plugins' ? <LiveManagementView initialTab="plugins" focused /> : page === 'local' ? <ExtensionsView /> : <LiveManagementView initialTab="skills" focused /> : <SettingPanel title="工具与扩展"><p>演示模式不管理本机工具。</p></SettingPanel>}</>}
      {current === 'memory' && <>{subnav([['settings', '记忆设置'], ['retention', '保留与审计']])}{clientMode === 'live' ? <LiveManagementView initialTab={page === 'retention' ? 'retention' : 'settings'} focused /> : <SettingPanel title="记忆"><p>演示模式不保存真实记忆。</p></SettingPanel>}</>}
      {current === 'advanced' && <SettingPanel title="权限与高级">
        <section className="settings-hub-card"><h2><ShieldCheck size={18} aria-hidden="true" />操作权限</h2><PolicySettings /></section>
        {clientMode === 'live' && <><details className="settings-hub-card"><summary><Settings2 size={16} aria-hidden="true" />运行配置</summary><LiveConfigSettings /></details><details className="settings-hub-card"><summary>远程与设备</summary><RemoteView /></details></>}
      </SettingPanel>}
    </div>
  </div>;
};
