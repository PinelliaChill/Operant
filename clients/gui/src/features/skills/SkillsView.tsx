/**
 * 技能分区（v8：本地 Skill 规范扫描与标准技能包管理）
 * - 遵循 standard Agent Skill 规范（包含 SKILL.md 元数据、scripts/、references/ 结构）；
 * - 页头：
 *   - 「扫描本地技能」按钮：模拟扫描本地 ~/.gemini/antigravity/builtin/skills 与 .operant/skills 目录；
 *   - 「+ 新建技能」按钮：打开新建标准技能 Modal；
 * - 列表/网格：按分类过滤与搜索，展示技能名称、路径、工具依赖与启停开关；
 * - 点击技能卡片打开右侧「技能详情抽屉」：查看完整的 SKILL.md 规范说明、脚本清单与调用指令。
 */

import React, { useMemo, useState } from 'react';
import {
  Sparkles,
  Search,
  Plus,
  RefreshCw,
  FileCode,
  Wrench,
  FolderKanban,
  AlertTriangle,
  ShieldAlert,
} from 'lucide-react';
import { EmptyState } from '../../components/EmptyState';
import { Modal } from '../../components/Modal';
import { Drawer } from '../../components/Drawer';
import { useOperant } from '../../context/ClientContext';
import { useDemo } from '../../demo/DemoContext';
import type { DemoSkill } from '../../demo/types';
import { usePhase45 } from '../../live45/Phase45Context';
import type { LiveSkillCandidate } from '../../live45/phase45Adapter';

export const SkillsView: React.FC = () => {
  const { clientMode } = useOperant();
  return clientMode === 'live' ? <LiveSkillsView /> : <DemoSkillsView />;
};

const DemoSkillsView: React.FC = () => {
  const {
    skills,
    projects,
    toggleProjectSkill,
    getProjectSkills,
    createSkill,
    scanLocalSkills,
  } = useDemo();
  const { addNotification } = useOperant();

  const defaultProjectId =
    projects.find((p) => p.isDefault)?.id || projects[0]?.id || 'proj_workspace';
  const [selectedProjectId, setSelectedProjectId] = useState<string>(defaultProjectId);
  const currentProject = projects.find((p) => p.id === selectedProjectId) || projects[0];

  const enabledSkillIds = useMemo(() => {
    return currentProject ? getProjectSkills(currentProject.id) : [];
  }, [currentProject, getProjectSkills]);

  const [query, setQuery] = useState('');
  const [selectedCategory, setSelectedCategory] = useState<string>('全部');
  const [selectedSkill, setSelectedSkill] = useState<DemoSkill | null>(null);

  // 新建技能弹窗状态
  const [newModalOpen, setNewModalOpen] = useState(false);
  const [newName, setNewName] = useState('');
  const [newDesc, setNewDesc] = useState('');
  const [newCategory, setNewCategory] = useState('自定义技能');
  const [newTools, setNewTools] = useState('view_file, run_command');
  const [newPrompt, setNewPrompt] = useState('');

  const categories = useMemo(() => {
    const set = new Set<string>();
    skills.forEach((s) => {
      if (s.category) set.add(s.category);
    });
    return ['全部', ...Array.from(set)];
  }, [skills]);

  const filteredSkills = useMemo(() => {
    return skills.filter((s) => {
      const matchCat = selectedCategory === '全部' || s.category === selectedCategory;
      const q = query.trim().toLowerCase();
      const matchQ =
        !q ||
        s.name.toLowerCase().includes(q) ||
        s.desc.toLowerCase().includes(q) ||
        (s.path && s.path.toLowerCase().includes(q));
      return matchCat && matchQ;
    });
  }, [skills, selectedCategory, query]);

  const handleToggle = (
    e: React.MouseEvent,
    projectId: string,
    projectName: string,
    skillId: string,
    skillName: string,
    isEnabled: boolean
  ) => {
    e.stopPropagation();
    toggleProjectSkill(projectId, skillId);
    addNotification(
      'success',
      isEnabled
        ? `已从「${projectName}」卸载「${skillName}」软链接（演示）`
        : `已将「${skillName}」软链接装载至「${projectName}」（演示）`
    );
  };

  const handleCreate = () => {
    if (!newName.trim() || !newDesc.trim()) return;

    const created = createSkill({
      name: newName.trim(),
      desc: newDesc.trim(),
      spec: 'standard-v1',
      category: newCategory.trim() || '自定义技能',
      path: `.operant/skills/${newName.trim().toLowerCase().replace(/\s+/g, '_')}`,
      author: 'User',
      tools: newTools.split(',').map((t) => t.trim()).filter(Boolean),
      scripts: ['main.py'],
      markdownContent: `# ${newName.trim()}\n\n${newDesc.trim()}\n\n## 系统提示词与指令\n\n${newPrompt || '暂无专属提示词。'}`,
      enabled: true,
    });

    // 默认装载到当前选中项目
    if (currentProject) {
      toggleProjectSkill(currentProject.id, created.id);
    }

    setNewModalOpen(false);
    setNewName('');
    setNewDesc('');
    setNewPrompt('');
  };

  return (
    <div className="section-view">
      <header
        className="section-header"
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          flexWrap: 'wrap',
          gap: 12,
        }}
      >
        <div>
          <h1 className="section-title">技能中心</h1>
          <p className="section-sub">
            遵循 standard Agent Skill 规范管理本地内置与自定义技能包（共 {skills.length} 个）
          </p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button className="btn btn-secondary btn-sm" onClick={scanLocalSkills}>
            <RefreshCw size={13} />
            <span>扫描本地技能</span>
          </button>
          <button className="btn btn-primary btn-sm" onClick={() => setNewModalOpen(true)}>
            <Plus size={13} />
            <span>新建技能</span>
          </button>
        </div>
      </header>

      {/* 当前项目工作区选择器 */}
      <div
        className="card"
        style={{
          margin: '0 24px 16px',
          padding: '10px 16px',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 12,
          flexWrap: 'wrap',
          backgroundColor: 'var(--bg-surface)',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0, flex: 1 }}>
          <span
            style={{
              width: 10,
              height: 10,
              borderRadius: '50%',
              backgroundColor: currentProject?.color || '#2563eb',
              flexShrink: 0,
            }}
          />
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', minWidth: 0 }}>
            <span style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-muted)' }}>
              当前项目工作区：
            </span>
            <select
              className="select"
              value={selectedProjectId}
              onChange={(e) => setSelectedProjectId(e.target.value)}
              style={{
                width: 'auto',
                minWidth: 180,
                height: 30,
                padding: '2px 8px',
                fontSize: '13px',
                fontWeight: 600,
              }}
            >
              {projects.map((p) => {
                const count = (getProjectSkills(p.id) ?? []).length;
                return (
                  <option key={p.id} value={p.id}>
                    {p.name} {p.isDefault ? '（默认工作区）' : ''} ({count} 个技能装载)
                  </option>
                );
              })}
            </select>
            <span
              style={{
                fontSize: '12px',
                fontFamily: 'var(--font-mono)',
                color: 'var(--text-muted)',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              📁 {currentProject?.path}
            </span>
          </div>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span
            className="badge"
            style={{
              backgroundColor: 'var(--accent-subtle)',
              color: 'var(--accent-action)',
              fontSize: '11px',
              fontWeight: 600,
            }}
          >
            已装载 {enabledSkillIds.length} / {skills.length} 个技能
          </span>
        </div>
      </div>

      {/* 搜索与分类过滤条 */}
      <div
        style={{
          padding: '0 24px',
          marginBottom: 16,
          display: 'flex',
          gap: 12,
          alignItems: 'center',
          flexWrap: 'wrap',
        }}
      >
        <div style={{ position: 'relative', width: 280 }}>
          <Search
            size={14}
            style={{
              position: 'absolute',
              left: 10,
              top: '50%',
              transform: 'translateY(-50%)',
              color: 'var(--text-muted)',
            }}
          />
          <input
            type="text"
            className="input"
            placeholder="搜索技能名称、描述或路径…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            style={{ paddingLeft: 30 }}
          />
        </div>

        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {categories.map((cat) => (
            <button
              key={cat}
              type="button"
              className={`btn btn-sm ${selectedCategory === cat ? 'btn-primary' : 'btn-ghost'}`}
              style={{ borderRadius: 'var(--radius-sm)', fontSize: '11px', height: 28 }}
              onClick={() => setSelectedCategory(cat)}
            >
              {cat}
            </button>
          ))}
        </div>
      </div>

      <div className="section-scroll">
        <div className="section-inner">
          {filteredSkills.length === 0 ? (
            <div className="section-empty-wrap">
              <EmptyState
                icon={Sparkles}
                title="未找到匹配技能"
                description="未检索到符合条件的技能规范包，点击「扫描本地技能」重新发现。"
                action={
                  <button className="btn btn-secondary" onClick={scanLocalSkills}>
                    <RefreshCw size={14} />
                    <span>扫描本地技能</span>
                  </button>
                }
              />
            </div>
          ) : (
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fill, minmax(340px, 1fr))',
                gap: 14,
              }}
            >
              {filteredSkills.map((s) => {
                const isLoaded = enabledSkillIds.includes(s.id);
                return (
                  <div
                    key={s.id}
                    className="card"
                    style={{
                      display: 'flex',
                      flexDirection: 'column',
                      justifyContent: 'space-between',
                      cursor: 'pointer',
                      transition:
                        'border-color var(--dur-1) var(--ease-standard), box-shadow var(--dur-1) var(--ease-standard)',
                      borderColor: isLoaded ? 'var(--border-strong)' : undefined,
                    }}
                    onClick={() => setSelectedSkill(s)}
                  >
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                      <div
                        style={{
                          display: 'flex',
                          justifyContent: 'space-between',
                          alignItems: 'flex-start',
                          gap: 8,
                        }}
                      >
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                          <span
                            style={{
                              width: 28,
                              height: 28,
                              borderRadius: 'var(--radius-sm)',
                              backgroundColor: isLoaded
                                ? 'var(--accent-subtle)'
                                : 'var(--bg-subtle)',
                              color: isLoaded
                                ? 'var(--accent-action)'
                                : 'var(--text-secondary)',
                              display: 'flex',
                              alignItems: 'center',
                              justifyContent: 'center',
                              flexShrink: 0,
                            }}
                          >
                            <Sparkles size={14} />
                          </span>
                          <div>
                            <div
                              style={{
                                fontSize: '13px',
                                fontWeight: 600,
                                color: 'var(--text-primary)',
                              }}
                            >
                              {s.name}
                            </div>
                            {s.category && (
                              <span
                                className="badge"
                                style={{
                                  backgroundColor: 'var(--bg-surface)',
                                  fontSize: '10px',
                                }}
                              >
                                {s.category}
                              </span>
                            )}
                          </div>
                        </div>

                        <button
                          type="button"
                          role="switch"
                          aria-checked={isLoaded}
                          className="switch"
                          onClick={(e) =>
                            handleToggle(
                              e,
                              currentProject?.id ?? '',
                              currentProject?.name ?? '',
                              s.id,
                              s.name,
                              isLoaded
                            )
                          }
                          aria-label={
                            isLoaded
                              ? `从 ${currentProject?.name} 卸载 ${s.name}`
                              : `装载 ${s.name} 到 ${currentProject?.name}`
                          }
                          title={
                            isLoaded
                              ? `已装载至 ${currentProject?.name} (点击卸载)`
                              : `未装载至 ${currentProject?.name} (点击装载)`
                          }
                        />
                      </div>

                      <div
                        style={{
                          fontSize: '12px',
                          color: 'var(--text-secondary)',
                          lineHeight: 1.5,
                          minHeight: 36,
                        }}
                      >
                        {s.desc}
                      </div>

                      {/* 软链接状态与物理路径映射 */}
                      {isLoaded ? (
                        <div
                          style={{
                            fontFamily: 'var(--font-mono)',
                            fontSize: '11px',
                            color: 'var(--text-secondary)',
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            whiteSpace: 'nowrap',
                            backgroundColor: 'var(--bg-subtle)',
                            padding: '4px 8px',
                            borderRadius: 'var(--radius-sm)',
                            border: '1px solid var(--border-subtle)',
                          }}
                          title={`软链接已映射：${currentProject?.path}/.operant/skills/${s.name} ➔ ${s.path}`}
                        >
                          📁 <span style={{ color: 'var(--accent-action)', fontWeight: 500 }}>{currentProject?.path}/.operant/skills/{s.name}</span> <span style={{ color: 'var(--text-muted)' }}>➔ {s.path}</span>
                        </div>
                      ) : (
                        <div
                          style={{
                            fontSize: '11px',
                            color: 'var(--text-muted)',
                            backgroundColor: 'var(--bg-surface)',
                            padding: '4px 8px',
                            borderRadius: 'var(--radius-sm)',
                            display: 'flex',
                            alignItems: 'center',
                            gap: 6,
                          }}
                        >
                          <span>⚪</span>
                          <span>未装载到此工作区</span>
                        </div>
                      )}
                    </div>

                    <div
                      style={{
                        marginTop: 12,
                        paddingTop: 8,
                        borderTop: '1px solid var(--border-subtle)',
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'center',
                      }}
                    >
                      <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                        {s.tools?.slice(0, 2).map((t) => (
                          <span
                            key={t}
                            className="badge"
                            style={{
                              backgroundColor: 'var(--bg-subtle)',
                              fontSize: '10px',
                              fontFamily: 'var(--font-mono)',
                            }}
                          >
                            {t}
                          </span>
                        ))}
                        {(s.tools?.length ?? 0) > 2 && (
                          <span
                            className="badge"
                            style={{
                              backgroundColor: 'var(--bg-subtle)',
                              fontSize: '10px',
                            }}
                          >
                            +{(s.tools?.length ?? 0) - 2}
                          </span>
                        )}
                      </div>
                      <span
                        style={{
                          fontSize: '11px',
                          color: 'var(--accent-action)',
                          fontWeight: 500,
                        }}
                      >
                        查看详情 →
                      </span>
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          <p className="section-footnote" style={{ marginTop: 24 }}>
            遵循 standard Agent Skill 规范（包含 SKILL.md 与 scripts/ 目录），通过项目工作区软链接 <code>.operant/skills/</code> 动态映射与加载。
          </p>
        </div>
      </div>

      {/* 技能详情抽屉 */}
      <Drawer
        isOpen={Boolean(selectedSkill)}
        onClose={() => setSelectedSkill(null)}
        title={selectedSkill ? `技能规范 · ${selectedSkill.name}` : '技能详情'}
        width={540}
      >
        {selectedSkill && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
            <div
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
              }}
            >
              <div>
                <span
                  className="badge"
                  style={{
                    backgroundColor: 'var(--accent-subtle)',
                    color: 'var(--accent-action)',
                  }}
                >
                  规范版本：{selectedSkill.spec}
                </span>
                {selectedSkill.author && (
                  <span
                    style={{
                      fontSize: '12px',
                      color: 'var(--text-muted)',
                      marginLeft: 8,
                    }}
                  >
                    作者：{selectedSkill.author}
                  </span>
                )}
              </div>
            </div>

            {/* 所有项目工作区软链接装载清单 */}
            <div className="card" style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <div
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                }}
              >
                <div style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)', display: 'flex', alignItems: 'center', gap: 6 }}>
                  <FolderKanban size={15} color="var(--accent-action)" />
                  <span>各项目工作区软链接装载清单</span>
                </div>
                <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
                  共 {projects.length} 个工作区
                </span>
              </div>

              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                {projects.map((p) => {
                  const pEnabled = (getProjectSkills(p.id) ?? []).includes(selectedSkill.id);
                  return (
                    <div
                      key={p.id}
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'space-between',
                        padding: '10px 12px',
                        borderRadius: 'var(--radius-sm)',
                        backgroundColor: 'var(--bg-surface)',
                        border: '1px solid var(--border-subtle)',
                        gap: 10,
                      }}
                    >
                      <div style={{ minWidth: 0, flex: 1, display: 'flex', flexDirection: 'column', gap: 3 }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                          <span
                            style={{
                              width: 8,
                              height: 8,
                              borderRadius: '50%',
                              backgroundColor: p.color,
                              flexShrink: 0,
                            }}
                          />
                          <span style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-primary)' }}>
                            {p.name}
                          </span>
                          {p.isDefault && (
                            <span
                              className="badge"
                              style={{
                                fontSize: '9px',
                                padding: '1px 5px',
                                backgroundColor: 'var(--bg-subtle)',
                                color: 'var(--text-muted)',
                              }}
                            >
                              默认
                            </span>
                          )}
                          <span
                            className="badge"
                            style={{
                              fontSize: '10px',
                              backgroundColor: pEnabled ? 'var(--accent-subtle)' : 'var(--bg-subtle)',
                              color: pEnabled ? 'var(--accent-action)' : 'var(--text-muted)',
                            }}
                          >
                            {pEnabled ? '已装载' : '未装载'}
                          </span>
                        </div>
                        <div
                          style={{
                            fontFamily: 'var(--font-mono)',
                            fontSize: '11px',
                            color: pEnabled ? 'var(--text-secondary)' : 'var(--text-muted)',
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            whiteSpace: 'nowrap',
                          }}
                        >
                          {pEnabled ? (
                            <span>📁 {p.path}/.operant/skills/{selectedSkill.name} ➔ {selectedSkill.path}</span>
                          ) : (
                            <span>未装载到该工作区目录</span>
                          )}
                        </div>
                      </div>

                      <button
                        type="button"
                        role="switch"
                        aria-checked={pEnabled}
                        className="switch"
                        onClick={(e) =>
                          handleToggle(
                            e,
                            p.id,
                            p.name,
                            selectedSkill.id,
                            selectedSkill.name,
                            pEnabled
                          )
                        }
                        aria-label={pEnabled ? `从 ${p.name} 卸载` : `装载至 ${p.name}`}
                        title={pEnabled ? `从 ${p.name} 卸载` : `装载至 ${p.name}`}
                      />
                    </div>
                  );
                })}
              </div>
            </div>

            <div className="card" style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-muted)' }}>
                规范源存储路径
              </div>
              <div
                style={{
                  fontFamily: 'var(--font-mono)',
                  fontSize: '12px',
                  color: 'var(--text-primary)',
                  wordBreak: 'break-all',
                }}
              >
                {selectedSkill.path}
              </div>
            </div>

            {selectedSkill.tools && selectedSkill.tools.length > 0 && (
              <div className="card" style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-muted)' }}>
                  依赖工具权限
                </div>
                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                  {selectedSkill.tools.map((t) => (
                    <span
                      key={t}
                      className="badge"
                      style={{ backgroundColor: 'var(--bg-surface)', fontFamily: 'var(--font-mono)' }}
                    >
                      <Wrench size={11} style={{ marginRight: 4 }} />
                      {t}
                    </span>
                  ))}
                </div>
              </div>
            )}

            {selectedSkill.scripts && selectedSkill.scripts.length > 0 && (
              <div className="card" style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-muted)' }}>
                  配套脚本清单 (scripts/)
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                  {selectedSkill.scripts.map((sc) => (
                    <div
                      key={sc}
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: 6,
                        fontSize: '12px',
                        fontFamily: 'var(--font-mono)',
                        color: 'var(--text-secondary)',
                      }}
                    >
                      <FileCode size={13} color="var(--accent-action)" />
                      <span>{sc}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            <div className="card" style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-muted)' }}>
                SKILL.md 规范说明文档
              </div>
              <pre
                style={{
                  margin: 0,
                  padding: 10,
                  background: 'var(--bg-surface)',
                  borderRadius: 'var(--radius-sm)',
                  fontSize: '12px',
                  lineHeight: 1.5,
                  whiteSpace: 'pre-wrap',
                  fontFamily: 'var(--font-mono)',
                  color: 'var(--text-primary)',
                }}
              >
                {selectedSkill.markdownContent || `# ${selectedSkill.name}\n\n${selectedSkill.desc}`}
              </pre>
            </div>
          </div>
        )}
      </Drawer>

      {/* 新建技能 Modal */}
      <Modal
        isOpen={newModalOpen}
        onClose={() => setNewModalOpen(false)}
        title="新建 Agent 技能规范包"
        footer={
          <>
            <button className="btn btn-ghost" onClick={() => setNewModalOpen(false)}>
              取消
            </button>
            <button
              className="btn btn-primary"
              onClick={handleCreate}
              disabled={!newName.trim() || !newDesc.trim()}
            >
              <Plus size={14} />
              <span>创建并装载</span>
            </button>
          </>
        }
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div>
            <label
              style={{
                fontSize: '11px',
                fontWeight: 600,
                color: 'var(--text-muted)',
                display: 'block',
                marginBottom: 4,
              }}
            >
              技能标识名称
            </label>
            <input
              type="text"
              className="input"
              placeholder="例如：code-quality-auditor"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              autoFocus
            />
          </div>

          <div>
            <label
              style={{
                fontSize: '11px',
                fontWeight: 600,
                color: 'var(--text-muted)',
                display: 'block',
                marginBottom: 4,
              }}
            >
              技能分类
            </label>
            <input
              type="text"
              className="input"
              placeholder="例如：代码修改 / 测试验证 / 数据分析"
              value={newCategory}
              onChange={(e) => setNewCategory(e.target.value)}
            />
          </div>

          <div>
            <label
              style={{
                fontSize: '11px',
                fontWeight: 600,
                color: 'var(--text-muted)',
                display: 'block',
                marginBottom: 4,
              }}
            >
              功能描述
            </label>
            <textarea
              className="input"
              rows={2}
              placeholder="描述该技能的作用与触发时机…"
              value={newDesc}
              onChange={(e) => setNewDesc(e.target.value)}
            />
          </div>

          <div>
            <label
              style={{
                fontSize: '11px',
                fontWeight: 600,
                color: 'var(--text-muted)',
                display: 'block',
                marginBottom: 4,
              }}
            >
              所需工具（逗号分隔）
            </label>
            <input
              type="text"
              className="input"
              value={newTools}
              onChange={(e) => setNewTools(e.target.value)}
              placeholder="view_file, run_command, apply_patch"
            />
          </div>

          <div>
            <label
              style={{
                fontSize: '11px',
                fontWeight: 600,
                color: 'var(--text-muted)',
                display: 'block',
                marginBottom: 4,
              }}
            >
              系统 Prompt 指令与规范
            </label>
            <textarea
              className="input"
              rows={4}
              placeholder="请输入技能被加载时的系统注入提示词与执行准则…"
              value={newPrompt}
              onChange={(e) => setNewPrompt(e.target.value)}
            />
          </div>
        </div>
      </Modal>
    </div>
  );
};

const LiveSkillsView: React.FC = () => {
  const { connectionStatus } = useOperant();
  const { phase, skills, skillIssues, error, actionLabel, refresh, discoverSkills } = usePhase45();
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState<LiveSkillCandidate | null>(null);
  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return needle ? skills.filter((item) => `${item.name} ${item.description} ${item.relativeDirectory}`.toLowerCase().includes(needle)) : skills;
  }, [query, skills]);

  if (phase === 'loading' && skills.length === 0) return <div className="live-route-state" role="status"><RefreshCw size={22} aria-hidden="true" /><h1>正在读取 Skill Projection…</h1><p>Live 模式不会使用演示技能填充页面。</p></div>;

  return <div className="section-view" data-client-mode="live">
    <header className="section-header" style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
      <div><h1 className="section-title">技能中心</h1><p className="section-sub">Core 发现的候选技能；发现不代表信任、安装或授权。</p></div>
      <div style={{ display: 'flex', gap: 8 }}><button type="button" className="btn btn-secondary btn-sm" onClick={() => void refresh()} disabled={Boolean(actionLabel)}><RefreshCw size={13} aria-hidden="true" />刷新</button><button type="button" className="btn btn-primary btn-sm" onClick={() => void discoverSkills()} disabled={Boolean(actionLabel) || connectionStatus !== 'connected'}><Search size={13} aria-hidden="true" />{actionLabel === '扫描技能' ? '扫描中…' : '扫描受信根目录'}</button></div>
    </header>
    <div className="section-scroll"><div className="section-inner">
      <div aria-live="polite">{connectionStatus !== 'connected' && <div className="live-alert live-alert-error" role="alert"><AlertTriangle size={16} aria-hidden="true" />Core 连接已断开；现有 Skill Projection 可能过期，写操作已禁用。</div>}{error && <div className="live-alert live-alert-error" role="alert"><AlertTriangle size={16} aria-hidden="true" />{error.code}：{error.message}</div>}{skillIssues.length > 0 && <div className="live-alert" role="status"><ShieldAlert size={16} aria-hidden="true" />扫描跳过 {skillIssues.length} 个不安全或无效候选。</div>}</div>
      <label style={{ display: 'block', maxWidth: 360, marginBottom: 16 }}>搜索候选技能<input className="input" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="名称、描述或相对目录" /></label>
      {visible.length === 0 ? <div className="section-empty-wrap"><EmptyState icon={Sparkles} title="没有 Skill candidate" description="可扫描 Core 已配置的受信根目录；客户端不能提交任意本机路径。" /></div> : <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(min(100%, 320px), 1fr))', gap: 14 }}>
        {visible.map((skill) => <button key={skill.id} type="button" className="card" style={{ textAlign: 'left', padding: 16, color: 'inherit' }} onClick={() => setSelected(skill)} aria-label={`查看候选技能 ${skill.name}`}>
          <strong>{skill.name}</strong><span className="badge" style={{ marginLeft: 8 }}>未信任候选</span>
          <p>{skill.description}</p><code>{skill.relativeDirectory}</code><p className="section-footnote">{skill.resources.length} 个已哈希资源</p>
        </button>)}
      </div>}
    </div></div>
    <Drawer isOpen={Boolean(selected)} onClose={() => setSelected(null)} title={selected ? `候选技能 · ${selected.name}` : '候选技能'} width={540}>
      {selected && <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}><p>{selected.description}</p><p><strong>信任状态：</strong>untrusted_candidate</p><p><strong>根引用：</strong><code>{selected.rootRef}</code></p><p><strong>相对目录：</strong><code>{selected.relativeDirectory}</code></p><p><strong>Manifest SHA-256：</strong><code style={{ overflowWrap: 'anywhere' }}>{selected.manifestSha256}</code></p><h3>资源</h3>{selected.resources.length === 0 ? <p>无脚本或参考资源。</p> : <ul>{selected.resources.map((resource) => <li key={resource.sha256}><code>{resource.relativePath}</code> · {resource.kind} · {resource.sizeBytes} B</li>)}</ul>}<p className="section-footnote">当前 Phase 4/5 API 只发现候选，不提供信任、安装或项目装载 Command。</p></div>}
    </Drawer>
  </div>;
};
