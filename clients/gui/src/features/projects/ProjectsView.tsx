/**
 * 项目分区（v7 §2：项目=工作区目录绑定模型）
 * /projects：项目卡网格（包含默认工作区置顶徽标、目录路径、会话数与工作流数；头部「+ 新建项目」）；
 * /projects/:projectId：项目头（色块 + 名称 + 默认工作区标记 + 目录路径）+
 * 「会话」+「工作流」双区块（工作流行=模板名+版本+实例数，点击进入协作总览/会话）+
 * 文件/任务两栏摘要卡；项目 id 不存在时显示空态。
 */

import React, { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import {
  FileText,
  FolderKanban,
  ListTodo,
  Plus,
  Workflow,
  Check,
  ArrowUpRight,
} from 'lucide-react';
import { EmptyState } from '../../components/EmptyState';
import { Modal } from '../../components/Modal';
import { useDemo } from '../../demo/DemoContext';
import { formatRelativeDay } from '../../lib/format';
import type { DemoWorkflowDirectoryItem } from '../../demo/types';
import { useOperant } from '../../context/ClientContext';
import { LiveManagementView } from '../management/LiveManagementView';

/** 预置项目标识颜色 */
const PRESET_COLORS = ['#2563eb', '#ea580c', '#059669', '#7c3aed', '#db2777', '#0891b2'];

/** 取一组 ISO 时间中的最新值 */
function latestIso(list: string[]): string | undefined {
  return list.length === 0 ? undefined : list.slice().sort().reverse()[0];
}

interface NewProjectModalProps {
  isOpen: boolean;
  onClose: () => void;
}

const NewProjectModal: React.FC<NewProjectModalProps> = ({ isOpen, onClose }) => {
  const { createProject } = useDemo();
  const navigate = useNavigate();
  const [name, setName] = useState('');
  const [color, setColor] = useState('#2563eb');
  const [path, setPath] = useState('');
  const [pathCustomized, setPathCustomized] = useState(false);

  useEffect(() => {
    if (!pathCustomized && name.trim()) {
      const slug = name.trim().toLowerCase().replace(/\s+/g, '-');
      setPath(`/Users/operant/workspace/${slug}`);
    }
  }, [name, pathCustomized]);

  const handleCreate = () => {
    if (!name.trim()) return;
    const proj = createProject({
      name: name.trim(),
      color,
      path: path.trim() || `/Users/operant/workspace/${name.trim().toLowerCase().replace(/\s+/g, '-')}`,
    });
    onClose();
    setName('');
    setPath('');
    setPathCustomized(false);
    navigate(`/projects/${proj.id}`);
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title="新建项目"
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>
            取消
          </button>
          <button className="btn btn-primary" onClick={handleCreate} disabled={!name.trim()}>
            <Plus size={14} />
            <span>创建项目</span>
          </button>
        </>
      }
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        <div>
          <label style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: 4 }}>
            项目名称
          </label>
          <input
            type="text"
            className="input"
            placeholder="例如：数据分析工作流"
            value={name}
            onChange={(e) => setName(e.target.value)}
            autoFocus
          />
        </div>

        <div>
          <label style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: 4 }}>
            工作区绑定目录路径
          </label>
          <input
            type="text"
            className="input"
            placeholder="/Users/operant/workspace/..."
            value={path}
            onChange={(e) => {
              setPath(e.target.value);
              setPathCustomized(true);
            }}
          />
          <span style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: 4, display: 'block' }}>
            与本地文件系统 workspace 目录绑定，会话与工作流生成的文件将归属于该路径。
          </span>
        </div>

        <div>
          <label style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: 6 }}>
            标识颜色
          </label>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            {PRESET_COLORS.map((c) => (
              <button
                key={c}
                type="button"
                onClick={() => setColor(c)}
                style={{
                  width: 26,
                  height: 26,
                  borderRadius: '50%',
                  backgroundColor: c,
                  border: color === c ? '2px solid var(--text-primary)' : '2px solid transparent',
                  cursor: 'pointer',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  color: '#ffffff',
                }}
                aria-label={`选择颜色 ${c}`}
              >
                {color === c && <Check size={14} />}
              </button>
            ))}
          </div>
        </div>
      </div>
    </Modal>
  );
};

const ProjectGrid: React.FC = () => {
  const { projects, conversations, getWorkflowDirectory } = useDemo();
  const [modalOpen, setModalOpen] = useState(false);
  const [workflows, setWorkflows] = useState<DemoWorkflowDirectoryItem[]>([]);

  useEffect(() => {
    getWorkflowDirectory().then(setWorkflows);
  }, [getWorkflowDirectory]);

  // 默认工作区项目排首位
  const sortedProjects = useMemo(() => {
    return [...projects].sort((a, b) => {
      if (a.isDefault) return -1;
      if (b.isDefault) return 1;
      return 0;
    });
  }, [projects]);

  return (
    <div className="section-view">
      <header className="section-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <h1 className="section-title">项目</h1>
          <p className="section-sub">共 {projects.length} 个项目（绑定工作区目录）</p>
        </div>
        <button className="btn btn-primary btn-sm" onClick={() => setModalOpen(true)}>
          <Plus size={13} />
          <span>新建项目</span>
        </button>
      </header>

      <div className="section-scroll">
        <div className="section-inner">
          {sortedProjects.length === 0 ? (
            <div className="section-empty-wrap">
              <EmptyState
                icon={FolderKanban}
                title="暂无项目"
                description="演示数据中还没有任何项目。"
              />
            </div>
          ) : (
            <div className="section-card-grid">
              {sortedProjects.map((p) => {
                const convs = conversations.filter((c) => c.projectId === p.id);
                const wfs = workflows.filter((w) => w.projectId === p.id);
                const lastActive = latestIso(convs.map((c) => c.updatedAt));
                return (
                  <Link
                    key={p.id}
                    to={`/projects/${p.id}`}
                    className="section-card"
                    aria-label={`进入项目 ${p.name}`}
                    style={{ position: 'relative' }}
                  >
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', width: '100%' }}>
                      <span
                        className="section-card-icon"
                        style={{ backgroundColor: p.color }}
                        aria-hidden="true"
                      >
                        <FolderKanban size={18} />
                      </span>
                      {p.isDefault && (
                        <span className="badge" style={{ backgroundColor: 'var(--accent-subtle)', color: 'var(--accent-action)', fontSize: '10px', fontWeight: 600 }}>
                          默认工作区
                        </span>
                      )}
                    </div>
                    <span className="section-card-title" style={{ marginTop: 8 }}>{p.name}</span>
                    <span style={{ fontSize: '11px', fontFamily: 'var(--font-mono)', color: 'var(--text-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {p.path}
                    </span>
                    <span className="section-card-meta">
                      {convs.length} 个会话 · {wfs.length} 个工作流
                      {lastActive ? ` · 最近 ${formatRelativeDay(lastActive)}` : ''}
                    </span>
                  </Link>
                );
              })}
            </div>
          )}
        </div>
      </div>

      <NewProjectModal isOpen={modalOpen} onClose={() => setModalOpen(false)} />
    </div>
  );
};

const ProjectDetail: React.FC<{ projectId: string }> = ({ projectId }) => {
  const { getProject, conversations, files, tasks, getWorkflowDirectory, instancesOfWorkflow } = useDemo();
  const project = getProject(projectId);
  const [modalOpen, setModalOpen] = useState(false);
  const [workflows, setWorkflows] = useState<DemoWorkflowDirectoryItem[]>([]);

  useEffect(() => {
    getWorkflowDirectory().then(setWorkflows);
  }, [getWorkflowDirectory]);

  // 项目内会话按更新时间倒序
  const projectConvs = useMemo(
    () =>
      conversations
        .filter((c) => c.projectId === projectId)
        .sort((a, b) => (a.updatedAt < b.updatedAt ? 1 : -1)),
    [conversations, projectId]
  );

  // 归属于该项目的工作流模板
  const projectWorkflows = useMemo(
    () => workflows.filter((w) => w.projectId === projectId),
    [workflows, projectId]
  );

  if (!project) {
    return (
      <div className="section-view">
        <div className="section-scroll">
          <div className="section-inner">
            <div className="section-empty-wrap">
              <EmptyState
                icon={FolderKanban}
                title="未找到该项目"
                description="项目不存在或已被移除。"
                action={
                  <Link to="/projects" className="btn btn-secondary">
                    返回项目列表
                  </Link>
                }
              />
            </div>
          </div>
        </div>
      </div>
    );
  }

  const convIds = new Set(projectConvs.map((c) => c.id));
  const fileList = projectConvs.flatMap((c) => files[c.id] ?? []);
  const taskList = tasks.filter((t) => convIds.has(t.conversationId));
  const lastActive = latestIso(projectConvs.map((c) => c.updatedAt));

  return (
    <div className="section-view">
      <header className="section-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div className="section-detail-head">
          <span
            className="section-detail-icon"
            style={{ backgroundColor: project.color }}
            aria-hidden="true"
          >
            <FolderKanban size={20} />
          </span>
          <div className="section-detail-main">
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <h1 className="section-title">{project.name}</h1>
              {project.isDefault && (
                <span className="badge" style={{ backgroundColor: 'var(--accent-subtle)', color: 'var(--accent-action)', fontSize: '11px', fontWeight: 600 }}>
                  默认工作区
                </span>
              )}
            </div>
            <p className="section-detail-sub" style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
              <span style={{ fontFamily: 'var(--font-mono)', fontSize: '11px', color: 'var(--text-muted)' }}>
                📁 {project.path}
              </span>
              <span>•</span>
              <span>
                {projectConvs.length} 个会话 · {projectWorkflows.length} 个工作流
                {lastActive ? ` · 最近活跃 ${formatRelativeDay(lastActive)}` : ''}
              </span>
            </p>
          </div>
        </div>

        <button className="btn btn-secondary btn-sm" onClick={() => setModalOpen(true)}>
          <Plus size={13} />
          <span>新建项目</span>
        </button>
      </header>

      <div className="section-scroll">
        <div className="section-inner">
          {/* 双区块之 1：会话 */}
          <section className="section-group" aria-label="项目会话">
            <h2 className="section-group-title">
              会话
              <span className="section-group-count">{projectConvs.length} 个</span>
            </h2>
            {projectConvs.length === 0 ? (
              <p className="section-summary-empty">该项目下暂无会话。</p>
            ) : (
              <ul className="chat-row-list">
                {projectConvs.map((c) => (
                  <li key={c.id}>
                    <Link
                      to={`/chat/${c.id}`}
                      className="chat-row chat-row-link"
                      aria-label={`打开会话 ${c.title}`}
                    >
                      <span className="chat-row-main">
                        <span className="chat-row-title">{c.title}</span>
                      </span>
                      <span className="chat-row-meta">{formatRelativeDay(c.updatedAt)}</span>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </section>

          {/* 双区块之 2：工作流模板 */}
          <section className="section-group" aria-label="项目工作流模板">
            <h2 className="section-group-title">
              工作流
              <span className="section-group-count">{projectWorkflows.length} 个模板</span>
            </h2>
            {projectWorkflows.length === 0 ? (
              <p className="section-summary-empty">该项目下暂无关联工作流模板。</p>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                {projectWorkflows.map((wf) => {
                  const groups = instancesOfWorkflow(wf.id);
                  const instCount = groups.active.length + groups.archived.length;
                  return (
                    <div
                      key={wf.id}
                      className="card"
                      style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'center',
                        padding: '10px 14px',
                        gap: 12,
                      }}
                    >
                      <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 }}>
                        <span
                          style={{
                            width: 28,
                            height: 28,
                            borderRadius: 'var(--radius-sm)',
                            backgroundColor: 'var(--accent-subtle)',
                            color: 'var(--accent-action)',
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'center',
                            flexShrink: 0,
                          }}
                          aria-hidden="true"
                        >
                          <Workflow size={15} />
                        </span>
                        <div style={{ minWidth: 0 }}>
                          <div style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)' }}>
                            {wf.name}
                          </div>
                          <div style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
                            {wf.version ? `v${wf.version} · ` : ''}
                            {wf.nodeCount} 节点 · {instCount} 个运行实例
                          </div>
                        </div>
                      </div>

                      <Link
                        to="/collab"
                        className="btn btn-secondary btn-sm"
                        style={{ flexShrink: 0 }}
                      >
                        <span>打开协作总览</span>
                        <ArrowUpRight size={13} />
                      </Link>
                    </div>
                  );
                })}
              </div>
            )}
          </section>

          {/* 摘要卡片两栏 */}
          <div className="section-duo">
            <section className="section-summary-card" aria-label="项目文件摘要">
              <div className="section-summary-head">
                <span className="chat-icon-box" aria-hidden="true">
                  <FileText size={16} />
                </span>
                文件
                <span className="chat-source-tag">{fileList.length}</span>
              </div>
              {fileList.length === 0 ? (
                <p className="section-summary-empty">暂无文件</p>
              ) : (
                <div className="section-summary-list">
                  {fileList.slice(0, 3).map((f) => (
                    <div key={f.id} className="section-summary-item">
                      <span className="section-summary-name">{f.name}</span>
                      <span className="section-summary-sub">{f.path}</span>
                    </div>
                  ))}
                </div>
              )}
            </section>

            <section className="section-summary-card" aria-label="项目任务摘要">
              <div className="section-summary-head">
                <span className="chat-icon-box" aria-hidden="true">
                  <ListTodo size={16} />
                </span>
                任务
                <span className="chat-source-tag">{taskList.length}</span>
              </div>
              {taskList.length === 0 ? (
                <p className="section-summary-empty">暂无任务</p>
              ) : (
                <div className="section-summary-list">
                  {taskList.slice(0, 3).map((t) => (
                    <div key={t.id} className="section-summary-item">
                      <span
                        className={`section-summary-name${t.done ? ' chat-row-title-done' : ''}`}
                      >
                        {t.title}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </section>
          </div>
        </div>
      </div>

      <NewProjectModal isOpen={modalOpen} onClose={() => setModalOpen(false)} />
    </div>
  );
};

export const ProjectsView: React.FC = () => {
  const { clientMode } = useOperant();
  if (clientMode === 'live') return <LiveManagementView initialTab="projects" />;
  return <DemoProjectsView />;
};

const DemoProjectsView: React.FC = () => {
  const { projectId } = useParams<{ projectId: string }>();
  return projectId ? <ProjectDetail projectId={projectId} /> : <ProjectGrid />;
};
