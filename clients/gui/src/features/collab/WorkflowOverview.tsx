/**
 * 协作总览（/collab 默认 Tab，v6 包C + v10 深度重构）：模板为主的视图。
 * 优化：
 * 1. 默认以右侧选中模板详情全宽展示，消除与全局侧栏的重复感；
 * 2. 顶部提供「展开/收起模板列表」切换按钮；展开时呈现左侧模板列表 + 右侧详情双栏布局，收起时详情全宽铺满；
 * 3. 点击「+ 新建实例」时弹出 Modal，提供「实例名称」输入与「所属项目工作区」下拉选择器；
 * 4. 支持 URL ?tpl= 深链联动高亮选中模板。
 */

import React, { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { CalendarClock, ChevronRight, GitBranch, LayoutList, Plus, Workflow } from 'lucide-react';
import { EmptyState } from '../../components/EmptyState';
import { Modal } from '../../components/Modal';
import { useDemo } from '../../demo/DemoContext';
import type { DemoWorkflowDirectoryItem } from '../../demo/types';
import { InstanceRow } from './InstanceRow';

/** 版本徽章：published=已发布 v{version}；persisted=另存的临时工作流（无版本号） */
const VersionBadge: React.FC<{ item: DemoWorkflowDirectoryItem }> = ({ item }) =>
  item.kind === 'published' ? (
    <span className="wf-badge published">已发布 v{item.version}</span>
  ) : (
    <span className="wf-badge persisted">另存</span>
  );

export const WorkflowOverview: React.FC = () => {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const { getWorkflowDirectory, instancesOfWorkflow, createWorkflowInstance, projects, conversations } =
    useDemo();

  /** undefined=加载中；加载完成后为目录条目数组（可能为空） */
  const [items, setItems] = useState<DemoWorkflowDirectoryItem[] | undefined>(undefined);
  /** 手动选中的模板 id；为空/失效时回退第一项 */
  const [manualSelectedId, setManualSelectedId] = useState<string | null>(null);
  /** 右栏底部「已归档实例」折叠区（默认收起） */
  const [archivedOpen, setArchivedOpen] = useState(false);

  // 默认收起模板列表以全宽展示详情，提供展开/收起切换（持久化到 localStorage）
  const [showTemplateList, setShowTemplateList] = useState<boolean>(() => {
    return localStorage.getItem('operant.collab.overview.showTemplates') === 'true';
  });

  const toggleTemplateList = () => {
    setShowTemplateList((prev) => {
      const next = !prev;
      localStorage.setItem('operant.collab.overview.showTemplates', String(next));
      return next;
    });
  };

  // 新建实例 Modal 状态
  const [newInstanceModalOpen, setNewInstanceModalOpen] = useState(false);
  const [newInstanceTitle, setNewInstanceTitle] = useState('');
  const defaultProjectId =
    projects.find((p) => p.isDefault)?.id || projects[0]?.id || 'proj_workspace';
  const [newInstanceProjectId, setNewInstanceProjectId] = useState(defaultProjectId);

  useEffect(() => {
    let cancelled = false;
    getWorkflowDirectory().then((list) => {
      if (!cancelled) setItems(list);
    });
    return () => {
      cancelled = true;
    };
  }, [getWorkflowDirectory]);

  // 当外部通过侧栏深链 ?tpl= 改变时，同步重置手动选中状态
  const tplParam = searchParams.get('tpl');
  useEffect(() => {
    if (tplParam) {
      setManualSelectedId(tplParam);
    }
  }, [tplParam]);

  /** 空态引导：切到编排画布 Tab（保留既有查询参数） */
  const goCanvas = () => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.set('view', 'canvas');
      return next;
    });
  };

  if (items === undefined) {
    return (
      <div className="section-scroll">
        <div className="section-inner">
          <p className="section-footnote">正在加载工作流总览…</p>
        </div>
      </div>
    );
  }

  if (items.length === 0) {
    return (
      <div className="section-scroll">
        <div className="section-inner">
          <div className="section-empty-wrap">
            <EmptyState
              icon={GitBranch}
              title="暂无工作流模板"
              description="在编排画布校验并发布图版本，或在会话中把临时工作流另存后，会作为模板出现在这里。"
              action={
                <button type="button" className="btn btn-primary" onClick={goCanvas}>
                  <Plus size={14} aria-hidden="true" />
                  <span>去画布搭建</span>
                </button>
              }
            />
          </div>
          <p className="section-footnote">演示数据，未连接真实 Core</p>
        </div>
      </div>
    );
  }

  // 目录非空时必有选中项（URL query ?tpl= 或 manualSelectedId，失效自动回退第一项）
  const selectedId = manualSelectedId || tplParam || items[0].id;
  const selected = items.find((i) => i.id === selectedId) ?? items[0];
  const { active, archived } = instancesOfWorkflow(selected.id);

  const openNewInstanceModal = () => {
    const prefix = `${selected.name} · 实例 `;
    const allConvs = conversations.filter((c) => c.workflowId === selected.id);
    const maxSeq = allConvs.reduce((max, c) => {
      if (c.title.startsWith(prefix)) {
        const n = parseInt(c.title.slice(prefix.length), 10);
        return Number.isFinite(n) && n > max ? n : max;
      }
      return max;
    }, 0);
    setNewInstanceTitle(`${selected.name} · 实例 ${maxSeq + 1}`);
    setNewInstanceProjectId(defaultProjectId);
    setNewInstanceModalOpen(true);
  };

  const handleConfirmCreateInstance = (e?: React.FormEvent) => {
    e?.preventDefault();
    const title = newInstanceTitle.trim() || undefined;
    const conversationId = createWorkflowInstance(selected.id, title, newInstanceProjectId);
    setNewInstanceModalOpen(false);
    navigate(`/workflow/${selected.id}/s/${conversationId}`);
  };

  return (
    <div className="section-scroll">
      <div className="section-inner collab-overview-inner">
        {/* 顶部工具栏：展开/收起模板列表 + 快速切换模板 */}
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            marginBottom: 12,
            flexWrap: 'wrap',
            gap: 8,
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
            <button
              type="button"
              className={`btn btn-sm ${showTemplateList ? 'btn-primary' : 'btn-secondary'}`}
              onClick={toggleTemplateList}
              title={showTemplateList ? '收起模板列表（全宽展示详情）' : '展开模板列表'}
            >
              <LayoutList size={13} aria-hidden="true" />
              <span>{showTemplateList ? '收起模板列表' : `展开模板列表 (${items.length})`}</span>
            </button>

            {!showTemplateList && items.length > 1 && (
              <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <span style={{ fontSize: '12px', color: 'var(--text-muted)' }}>当前模板:</span>
                <select
                  className="select"
                  style={{ maxWidth: 240, fontSize: '12px', padding: '3px 8px', height: 28 }}
                  value={selected.id}
                  onChange={(e) => {
                    setManualSelectedId(e.target.value);
                  }}
                >
                  {items.map((i) => (
                    <option key={i.id} value={i.id}>
                      {i.name} ({i.kind === 'published' ? `v${i.version}` : '另存'})
                    </option>
                  ))}
                </select>
              </div>
            )}
          </div>

          <button type="button" className="btn btn-ghost btn-sm" onClick={goCanvas}>
            <Plus size={13} aria-hidden="true" />
            <span>新建模板草稿</span>
          </button>
        </div>

        <div
          className={`collab-overview-grid ${showTemplateList ? 'templates-expanded' : 'templates-collapsed'}`}
        >
          {/* 左栏：工作流模板列表（可展开/收起） */}
          {showTemplateList && (
            <aside className="collab-ov-panel" aria-label="工作流模板列表">
              <div className="collab-ov-panel-title">工作流模板（{items.length}）</div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                {items.map((item) => {
                  const isSelected = item.id === selected.id;
                  const activeCount = instancesOfWorkflow(item.id).active.length;
                  return (
                    <button
                      key={item.id}
                      type="button"
                      className={`rail-sidebar-row${isSelected ? ' active' : ''}`}
                      onClick={() => setManualSelectedId(item.id)}
                      aria-current={isSelected ? 'true' : undefined}
                    >
                      <span className="rail-sidebar-row-main">
                        <span className="collab-ov-tpl-name">
                          <span className="rail-sidebar-row-title">{item.name}</span>
                          <VersionBadge item={item} />
                        </span>
                        <span className="rail-sidebar-row-sub">{activeCount} 个活跃实例</span>
                      </span>
                    </button>
                  );
                })}
              </div>
              <button type="button" className="btn btn-secondary btn-sm" onClick={goCanvas}>
                <Plus size={13} aria-hidden="true" />
                <span>新建模板</span>
              </button>
            </aside>
          )}

          {/* 右栏：选中模板详情（收起左栏时自适应全宽） */}
          <section className="collab-ov-panel" aria-label={`模板详情：${selected.name}`}>
            <div className="collab-ov-detail-head">
              <div className="collab-ov-detail-titlerow">
                <h3 className="collab-ov-detail-title">{selected.name}</h3>
                <VersionBadge item={selected} />
              </div>
              <p className="collab-ov-detail-meta">
                {selected.nodeCount} 节点 · {selected.edgeCount} 连线 · {active.length} 个活跃实例
              </p>
              <div className="collab-ov-detail-actions">
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  onClick={() => navigate(`/collab/${selected.id}/canvas`)}
                >
                  <Workflow size={13} aria-hidden="true" />
                  <span>打开画布</span>
                </button>
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  onClick={() =>
                    navigate(
                      `/schedules?new=1&targetType=workflow&targetId=${encodeURIComponent(
                        selected.id
                      )}&targetName=${encodeURIComponent(selected.name)}`
                    )
                  }
                  title="添加到定时调度"
                >
                  <CalendarClock size={13} aria-hidden="true" />
                  <span>添加到调度</span>
                </button>
                <button
                  type="button"
                  className="btn btn-primary btn-sm"
                  onClick={openNewInstanceModal}
                >
                  <Plus size={13} aria-hidden="true" />
                  <span>新建实例</span>
                </button>
              </div>
            </div>

            {/* 活跃实例列表 */}
            <div className="collab-wf-sessions">
              <div className="collab-wf-sessions-title">活跃实例（{active.length}）</div>
              {active.length === 0 ? (
                <div className="collab-wf-sessions-empty">
                  暂无活跃实例，点击「新建实例」为该模板创建一个会话
                </div>
              ) : (
                active.map((c) => (
                  <InstanceRow key={c.id} conversation={c} showIdleBadge showTime />
                ))
              )}
            </div>

            {/* 已归档实例折叠区（默认收起） */}
            {archived.length > 0 && (
              <div className="collab-wf-sessions">
                <button
                  type="button"
                  className="collab-wf-sessions-title inst-arch-toggle"
                  onClick={() => setArchivedOpen((v) => !v)}
                  aria-expanded={archivedOpen}
                >
                  <ChevronRight size={12} className="inst-arch-toggle-chevron" aria-hidden="true" />
                  <span>已归档实例（{archived.length}）</span>
                </button>
                {archivedOpen &&
                  archived.map((c) => (
                    <InstanceRow key={c.id} conversation={c} archived showTime direction="up" />
                  ))}
              </div>
            )}
          </section>
        </div>
        <p className="section-footnote">演示数据，未连接真实 Core</p>
      </div>

      {/* 新建工作流实例 Modal */}
      <Modal
        isOpen={newInstanceModalOpen}
        onClose={() => setNewInstanceModalOpen(false)}
        title={`新建工作流实例 · ${selected.name}`}
        footer={
          <>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => setNewInstanceModalOpen(false)}
            >
              取消
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={handleConfirmCreateInstance}
            >
              创建实例
            </button>
          </>
        }
      >
        <form
          onSubmit={handleConfirmCreateInstance}
          style={{ display: 'flex', flexDirection: 'column', gap: 14 }}
        >
          <div>
            <label
              style={{
                fontSize: '12px',
                fontWeight: 600,
                color: 'var(--text-primary)',
                display: 'block',
                marginBottom: 6,
              }}
            >
              实例名称
            </label>
            <input
              type="text"
              className="input"
              value={newInstanceTitle}
              onChange={(e) => setNewInstanceTitle(e.target.value)}
              placeholder="例如：自主功能交付图 · 实例 1"
              autoFocus
            />
          </div>

          <div>
            <label
              style={{
                fontSize: '12px',
                fontWeight: 600,
                color: 'var(--text-primary)',
                display: 'block',
                marginBottom: 6,
              }}
            >
              所属项目工作区
            </label>
            <select
              className="select"
              value={newInstanceProjectId}
              onChange={(e) => setNewInstanceProjectId(e.target.value)}
            >
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} {p.isDefault ? '（默认工作区）' : ''}
                </option>
              ))}
            </select>
            <span
              style={{
                fontSize: '11px',
                color: 'var(--text-muted)',
                marginTop: 4,
                display: 'block',
              }}
            >
              实例群聊将挂载到所选项目，并共享该工作区上下文文件与知识库。
            </span>
          </div>
        </form>
      </Modal>
    </div>
  );
};
