import React, { useState, useId, useMemo, useEffect, useRef } from 'react';
import {
  Sparkles,
  GitBranch,
  Share2,
  Radio,
  CheckCircle,
  AlertCircle,
  AlertTriangle,
  RefreshCw,
  X,
  Check,
  Lock,
  Layers,
  Search,
  Copy,
  SlidersHorizontal,
  FileQuestion,
  ShieldAlert,
} from 'lucide-react';
import type {
  B26PresentationProps,
  B26Section,
  B26Action,
} from './b26-presentation-types';
import './b26-experience.css';

/**
 * 映射状态徽章色调；文本标签永远完整保留并显示，杜绝仅靠颜色传意
 */
function getBadgeTone(status: string): 'safe' | 'warn' | 'error' | 'info' | 'neutral' {
  const s = status.toLowerCase();
  // Current failure/revocation wins over historical "published" text.
  if (['revoked', 'blocked', 'disabled', 'inactive', 'expired', '已撤销', '已停用', '不可用'].some(value => s.includes(value))) return 'error';
  if (
    s.includes('已发布') ||
    s.includes('active') ||
    s.includes('published') ||
    s.includes('已验证') ||
    s.includes('已晋级') ||
    s.includes('成功')
  ) {
    return 'safe';
  }
  if (
    s.includes('待验证') ||
    s.includes('草稿') ||
    s.includes('draft') ||
    s.includes('pending') ||
    s.includes('review') ||
    s.includes('未知结果') ||
    s.includes('待晋级')
  ) {
    return 'warn';
  }
  if (
    s.includes('已撤销') ||
    s.includes('来源撤销') ||
    s.includes('无权限') ||
    s.includes('已拒绝') ||
    s.includes('已停用') ||
    s.includes('已回退') ||
    s.includes('error') ||
    s.includes('failed') ||
    s.includes('forbidden')
  ) {
    return 'error';
  }
  if (s.includes('共享') || s.includes('remote') || s.includes('晋级')) {
    return 'info';
  }
  return 'neutral';
}

/**
 * 板块图标匹配
 */
function getSectionIcon(id: B26Section['id']) {
  switch (id) {
    case 'skills':
      return <Sparkles size={16} aria-hidden="true" />;
    case 'writers':
      return <GitBranch size={16} aria-hidden="true" />;
    case 'sharing':
      return <Share2 size={16} aria-hidden="true" />;
    case 'remote':
      return <Radio size={16} aria-hidden="true" />;
    default:
      return <Layers size={16} aria-hidden="true" />;
  }
}

export const B26Presentation: React.FC<B26PresentationProps> = ({
  sections,
  loading,
  busy,
  readOnly,
  error,
  notice,
  onRefresh,
  onAction,
}) => {
  // 活跃选项卡：默认第一个板块或 'all'
  const [activeTab, setActiveTab] = useState<string>(() => {
    return sections.length > 0 ? sections[0].id : 'skills';
  });

  // 搜索关键字
  const [searchTerms, setSearchTerms] = useState<Record<string, string>>({});

  // 复制反馈状态
  const [copiedKey, setCopiedKey] = useState<string | null>(null);

  // 活跃操作模态框状态
  const [activeAction, setActiveAction] = useState<B26Action | null>(null);
  const [formValues, setFormValues] = useState<Record<string, string>>({});
  const [confirmed, setConfirmed] = useState<boolean>(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!activeAction) return;
    const previous = document.activeElement;
    dialogRef.current?.querySelector<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled)')?.focus();
    return () => { if (previous instanceof HTMLElement && previous.isConnected) previous.focus(); };
  }, [activeAction]);

  const confirmCheckboxId = useId();

  // 复制事实或 ID 到剪贴板
  const handleCopy = (text: string, key: string) => {
    if (typeof navigator !== 'undefined' && navigator.clipboard) {
      navigator.clipboard.writeText(text).catch(() => {});
      setCopiedKey(key);
      setTimeout(() => setCopiedKey(null), 1600);
    }
  };

  // 打开操作交互框
  const handleOpenAction = (action: B26Action) => {
    const initialValues: Record<string, string> = {};
    action.fields.forEach((field) => {
      initialValues[field.key] = field.value ?? (field.kind === 'checkbox' ? 'false' : '');
    });
    setFormValues(initialValues);
    setConfirmed(false);
    setActiveAction(action);
  };

  // 关闭模态框
  const handleCloseAction = () => {
    if (busy) return;
    setActiveAction(null);
    setFormValues({});
    setConfirmed(false);
  };

  // 校验当前操作表单是否有效
  const isActionValid = useMemo(() => {
    if (!activeAction) return false;
    if (activeAction.disabled) return false;
    if (readOnly || busy) return false;
    if (!confirmed) return false;

    for (const field of activeAction.fields) {
      if (field.required) {
        const val = formValues[field.key];
        if (field.kind === 'checkbox') {
          if (val !== 'true') return false;
        } else {
          if (!val || val.trim() === '') return false;
        }
      }
    }
    return true;
  }, [activeAction, confirmed, formValues, readOnly, busy]);

  // 提交操作
  const handleSubmitAction = () => {
    if (!activeAction || !isActionValid) return;
    onAction(activeAction.id, formValues);
    setActiveAction(null);
    setFormValues({});
    setConfirmed(false);
  };

  // 过滤当前展示的板块
  const displayedSections = useMemo(() => {
    if (activeTab === 'all') {
      return sections;
    }
    return sections.filter((s) => s.id === activeTab);
  }, [sections, activeTab]);

  return (
    <main className="b26-presentation" role="region" aria-label="经验技能与授权协作工作台">
      {/* 头部栏 */}
      <header className="b26-header">
        <div className="b26-title-group">
          <h1 className="b26-title">
            <span className="b26-title-icon" aria-hidden="true">
              <Layers size={18} />
            </span>
            经验技能与授权协作
          </h1>
          <p className="b26-subtitle">
            整理和发布经验，核对分支知识，管理共享授权与远程记忆包。
          </p>
        </div>

        <div className="b26-header-controls">
          {readOnly && (
            <span className="b26-status-pill readonly" role="status">
              <Lock size={12} aria-hidden="true" /> 只读模式 (readOnly)
            </span>
          )}
          {busy && (
            <span className="b26-status-pill busy" role="status">
              <RefreshCw size={12} className="animate-spin" aria-hidden="true" /> 处理中 (busy)
            </span>
          )}
          <button
            type="button"
            className="b26-btn b26-btn-secondary"
            onClick={onRefresh}
            disabled={loading || busy}
            aria-label="刷新经验技能与协作状态"
          >
            <RefreshCw size={14} className={loading ? 'animate-spin' : ''} aria-hidden="true" />
            <span>刷新</span>
          </button>
        </div>
      </header>

      {/* 错误通知横幅 */}
      {error && (
        <div className="b26-banner error" role="alert">
          <AlertCircle className="b26-banner-icon" size={18} aria-hidden="true" />
          <div className="b26-banner-content">
            <strong>操作异常：</strong>
            {error}
          </div>
        </div>
      )}

      {/* 状态通知横幅 */}
      {notice && (
        <div className="b26-banner notice" role="status">
          <CheckCircle className="b26-banner-icon" size={18} aria-hidden="true" />
          <div className="b26-banner-content">
            <strong>系统提示：</strong>
            {notice}
          </div>
        </div>
      )}

      {/* 只读警示横幅 */}
      {readOnly && (
        <div className="b26-banner readonly-warning" role="status">
          <Lock className="b26-banner-icon" size={18} aria-hidden="true" />
          <div className="b26-banner-content">
            <strong>只读模式：</strong> 当前处于只读状态（未连接或无写入权限），所有新建操作、参数提交与晋级共享已禁用。
          </div>
        </div>
      )}

      {/* 标签页导航 */}
      <nav className="b26-tabs" aria-label="经验与协作板块切换">
        {sections.map((section) => {
          const isActive = activeTab === section.id;
          return (
            <button
              key={section.id}
              type="button"
              className={`b26-tab-btn ${isActive ? 'active' : ''}`}
              onClick={() => setActiveTab(section.id)}
              aria-selected={isActive}
              role="tab"
            >
              {getSectionIcon(section.id)}
              <span>{section.title}</span>
              <span className="b26-tab-badge" aria-label={`共 ${section.cards.length} 项`}>
                {section.cards.length}
              </span>
            </button>
          );
        })}
        <button
          type="button"
          className={`b26-tab-btn ${activeTab === 'all' ? 'active' : ''}`}
          onClick={() => setActiveTab('all')}
          aria-selected={activeTab === 'all'}
          role="tab"
        >
          <SlidersHorizontal size={16} aria-hidden="true" />
          <span>全部总览</span>
          <span className="b26-tab-badge">
            {sections.reduce((acc, s) => acc + s.cards.length, 0)}
          </span>
        </button>
      </nav>

      {/* 板块主体 */}
      {displayedSections.map((section) => {
        const query = (searchTerms[section.id] || '').toLowerCase().trim();
        const filteredCards = section.cards.filter((card) => {
          if (!query) return true;
          const matchTitle = card.title.toLowerCase().includes(query);
          const matchId = card.id.toLowerCase().includes(query);
          const matchStatus = card.status.toLowerCase().includes(query);
          const matchDesc = (card.description || '').toLowerCase().includes(query);
          const matchFacts = card.facts.some(
            (f) =>
              f.label.toLowerCase().includes(query) ||
              f.value.toLowerCase().includes(query)
          );
          return matchTitle || matchId || matchStatus || matchDesc || matchFacts;
        });

        return (
          <section
            key={section.id}
            className="b26-section"
            aria-labelledby={`b26-sec-title-${section.id}`}
          >
            {/* 板块标题与操作工具栏 */}
            <div className="b26-section-header">
              <div className="b26-section-info">
                <h2 id={`b26-sec-title-${section.id}`} className="b26-section-title">
                  {getSectionIcon(section.id)}
                  {section.title}
                </h2>
                <p className="b26-section-desc">{section.description}</p>
              </div>

              <div className="b26-section-actions">
                {section.actions.map((action) => (
                  <button
                    key={action.id}
                    type="button"
                    className={`b26-btn ${
                      action.dangerous ? 'b26-btn-danger' : 'b26-btn-primary'
                    }`}
                    onClick={() => handleOpenAction(action)}
                    disabled={readOnly || busy || action.disabled}
                    title={action.disabled ? action.disabledReason : action.description}
                  >
                    <span>{action.title}</span>
                  </button>
                ))}
              </div>
            </div>

            {/* 搜索/过滤工具条 */}
            {section.cards.length > 0 && (
              <div className="b26-search-bar" role="search">
                <Search size={16} className="text-muted" aria-hidden="true" />
                <input
                  type="text"
                  className="b26-search-input"
                  placeholder={`搜索 ${section.title} 下的标题、ID、状态或属性...`}
                  value={searchTerms[section.id] || ''}
                  onChange={(e) =>
                    setSearchTerms((prev) => ({ ...prev, [section.id]: e.target.value }))
                  }
                  aria-label={`搜索 ${section.title}`}
                />
                {searchTerms[section.id] && (
                  <button
                    type="button"
                    className="b26-modal-close-btn"
                    onClick={() =>
                      setSearchTerms((prev) => ({ ...prev, [section.id]: '' }))
                    }
                    aria-label="清空搜索"
                  >
                    <X size={14} aria-hidden="true" />
                  </button>
                )}
              </div>
            )}

            {/* 卡片列表 / 空状态 */}
            {filteredCards.length === 0 ? (
              <div className="b26-empty" role="region" aria-label="空数据提示">
                <FileQuestion className="b26-empty-icon" aria-hidden="true" />
                <h3 className="b26-empty-title">
                  {query ? '未找到符合条件的卡片' : '暂无数据'}
                </h3>
                <p className="b26-empty-desc">
                  {query ? `未匹配到与 "${query}" 相关的记录。` : section.emptyMessage}
                </p>
              </div>
            ) : (
              <div className="b26-card-grid">
                {filteredCards.map((card) => {
                  const badgeTone = getBadgeTone(card.status);
                  return (
                    <article key={card.id} className="b26-card" aria-labelledby={`card-t-${card.id}`}>
                      {/* 卡片头部 */}
                      <div className="b26-card-header">
                        <div className="b26-card-title-wrap">
                          <h3 id={`card-t-${card.id}`} className="b26-card-title">
                            {card.title}
                          </h3>
                          <div className="b26-card-id">
                            <span>ID: {card.id}</span>
                            <button
                              type="button"
                              className="b26-btn b26-btn-secondary"
                              style={{
                                padding: '1px 6px',
                                fontSize: 10,
                                marginLeft: 6,
                                display: 'inline-flex',
                              }}
                              onClick={() => handleCopy(card.id, `id-${card.id}`)}
                              title="复制完整 ID"
                              aria-label={`复制 ${card.id}`}
                            >
                              {copiedKey === `id-${card.id}` ? (
                                <Check size={10} aria-hidden="true" />
                              ) : (
                                <Copy size={10} aria-hidden="true" />
                              )}
                              <span>{copiedKey === `id-${card.id}` ? '已复制' : '复制'}</span>
                            </button>
                          </div>
                        </div>

                        {/* 状态徽章：文字明确可读 */}
                        <span className={`b26-badge ${badgeTone}`} role="status">
                          {card.status}
                        </span>
                      </div>

                      {/* 卡片说明 */}
                      {card.description && (
                        <p className="b26-card-desc">{card.description}</p>
                      )}

                      {/* 事实元数据 (Facts) */}
                      {card.facts.length > 0 && (
                        <div className="b26-card-facts" aria-label="属性与凭据事实">
                          {card.facts.map((fact, fIdx) => {
                            const factKey = `fact-${card.id}-${fIdx}`;
                            return (
                              <div key={factKey} className="b26-fact-item">
                                <span className="b26-fact-label">{fact.label}:</span>
                                <span className="b26-fact-value" title={fact.value}>
                                  {fact.value}
                                </span>
                              </div>
                            );
                          })}
                        </div>
                      )}

                      {/* 卡片警告提示 */}
                      {card.warning && (
                        <div className="b26-card-warning" role="alert">
                          <AlertTriangle
                            className="b26-card-warning-icon"
                            size={16}
                            aria-hidden="true"
                          />
                          <span>{card.warning}</span>
                        </div>
                      )}
                    </article>
                  );
                })}
              </div>
            )}
          </section>
        );
      })}

      {/* 操作与提交交互模态框 */}
      {activeAction && (
        <div
          className="b26-modal-backdrop"
          role="dialog"
          aria-modal="true"
          aria-labelledby="b26-modal-title"
          ref={dialogRef}
          onKeyDown={event => {
            if (event.key === 'Escape') { event.preventDefault(); handleCloseAction(); }
            if (event.key !== 'Tab') return;
            const elements = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled)') ?? []);
            const first = elements[0], last = elements[elements.length - 1];
            if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
            if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
          }}
        >
          <div className="b26-modal-panel">
            {/* 模态框头部 */}
            <header className="b26-modal-header">
              <div className="b26-modal-title-wrap">
                <h3 id="b26-modal-title" className="b26-modal-title">
                  {activeAction.title}
                </h3>
                <p className="b26-modal-desc">{activeAction.description}</p>
              </div>
              <button
                type="button"
                className="b26-modal-close-btn"
                onClick={handleCloseAction}
                disabled={busy}
                aria-label="关闭操作窗口"
              >
                <X size={18} aria-hidden="true" />
              </button>
            </header>

            {/* 模态框表单主体 */}
            <div className="b26-modal-body">
              {/* 禁用原因展示 */}
              {activeAction.disabled && (
                <div className="b26-disabled-notice" role="alert">
                  <ShieldAlert size={18} aria-hidden="true" />
                  <div>
                    <strong>当前操作已被禁用：</strong>
                    {activeAction.disabledReason || '未满足前置条件或权限不足'}
                  </div>
                </div>
              )}

              {/* 字段渲染（均来自 Core 父组件 props 契约） */}
              {activeAction.fields.map((field) => {
                const fieldId = `field-${activeAction.id}-${field.key}`;
                const fieldValue = formValues[field.key] ?? '';

                return (
                  <div key={field.key} className="b26-field-group">
                    {field.kind !== 'checkbox' && (
                      <label htmlFor={fieldId} className="b26-field-label">
                        <span>{field.label}</span>
                        {field.required && <span className="b26-required-mark">*</span>}
                      </label>
                    )}

                    {/* text */}
                    {field.kind === 'text' && (
                      <input
                        id={fieldId}
                        type="text"
                        className="b26-input"
                        value={fieldValue}
                        onChange={(e) =>
                          setFormValues((prev) => ({
                            ...prev,
                            [field.key]: e.target.value,
                          }))
                        }
                        disabled={readOnly || busy || activeAction.disabled}
                        required={field.required}
                      />
                    )}

                    {/* textarea */}
                    {field.kind === 'textarea' && (
                      <textarea
                        id={fieldId}
                        className="b26-textarea"
                        value={fieldValue}
                        onChange={(e) =>
                          setFormValues((prev) => ({
                            ...prev,
                            [field.key]: e.target.value,
                          }))
                        }
                        disabled={readOnly || busy || activeAction.disabled}
                        required={field.required}
                      />
                    )}

                    {/* number */}
                    {field.kind === 'number' && (
                      <input
                        id={fieldId}
                        type="number"
                        className="b26-input"
                        min={field.min}
                        max={field.max}
                        value={fieldValue}
                        onChange={(e) =>
                          setFormValues((prev) => ({
                            ...prev,
                            [field.key]: e.target.value,
                          }))
                        }
                        disabled={readOnly || busy || activeAction.disabled}
                        required={field.required}
                      />
                    )}

                    {/* select */}
                    {field.kind === 'select' && (
                      <select
                        id={fieldId}
                        className="b26-select"
                        value={fieldValue}
                        onChange={(e) =>
                          setFormValues((prev) => ({
                            ...prev,
                            [field.key]: e.target.value,
                          }))
                        }
                        disabled={readOnly || busy || activeAction.disabled}
                        required={field.required}
                      >
                        <option value="">-- 请选择 --</option>
                        {field.options?.map((opt) => (
                          <option key={opt.value} value={opt.value}>
                            {opt.label}
                          </option>
                        ))}
                      </select>
                    )}

                    {/* checkbox */}
                    {field.kind === 'checkbox' && (
                      <label className="b26-checkbox-row">
                        <input
                          id={fieldId}
                          type="checkbox"
                          className="b26-checkbox"
                          checked={fieldValue === 'true'}
                          onChange={(e) =>
                            setFormValues((prev) => ({
                              ...prev,
                              [field.key]: e.target.checked ? 'true' : 'false',
                            }))
                          }
                          disabled={readOnly || busy || activeAction.disabled}
                          required={field.required}
                        />
                        <span>
                          {field.label}
                          {field.required && <span className="b26-required-mark"> *</span>}
                        </span>
                      </label>
                    )}

                    {/* 字段提示 */}
                    {field.hint && <p className="b26-field-hint">{field.hint}</p>}
                  </div>
                );
              })}

              {/* 明确确认勾选 */}
              <div
                className={`b26-confirm-box ${
                  activeAction.dangerous ? 'dangerous' : ''
                }`}
              >
                <input
                  type="checkbox"
                  id={confirmCheckboxId}
                  className="b26-checkbox"
                  checked={confirmed}
                  onChange={(e) => setConfirmed(e.target.checked)}
                  disabled={readOnly || busy || activeAction.disabled}
                />
                <label htmlFor={confirmCheckboxId}>
                  {activeAction.dangerous ? (
                    <span>
                      <strong>操作确认：</strong> 我已核对上述参数及受影响对象，确认执行。
                    </span>
                  ) : (
                    <span>我已核对上述操作参数与目标对象，确认提交执行此操作。</span>
                  )}
                </label>
              </div>
            </div>

            {/* 模态框底部按钮 */}
            <footer className="b26-modal-footer">
              <button
                type="button"
                className="b26-btn b26-btn-secondary"
                onClick={handleCloseAction}
                disabled={busy}
              >
                取消
              </button>
              <button
                type="button"
                className={`b26-btn ${
                  activeAction.dangerous ? 'b26-btn-danger' : 'b26-btn-primary'
                }`}
                onClick={handleSubmitAction}
                disabled={!isActionValid || busy || readOnly || activeAction.disabled}
              >
                {busy ? (
                  <RefreshCw className="animate-spin" size={14} aria-hidden="true" />
                ) : (
                  <Check size={14} aria-hidden="true" />
                )}
                <span>{activeAction.confirmLabel || '确认提交'}</span>
              </button>
            </footer>
          </div>
        </div>
      )}
    </main>
  );
};
