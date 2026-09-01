/**
 * Composer（v7 §3）：底部输入与多维控制区。
 * - 斜杠命令面板（`/` 唤起）：输入即过滤，↑↓ 选择、Enter/Tab 确认、Esc 关闭；
 *   三组命令：导航（/帮助 /模型 /角色 /审批 /新建会话 /导出追踪）、
 *   会话动作（/重命名 /清空上下文 /压缩上下文 /切换演示模式）、
 *   CLI 风格占位（/init /review——执行后提示“需后端实现”）；
 * - `/帮助` 打开命令速查面板（Modal）；
 * - `@` 统一上下文选择器：四类对象分组（Agent / 文件 / 会话 / 工作流），
 *   支持多选与全键盘导航，选中项以 chips 浮现于输入框上方、可单个移除；
 * - 原「添加上下文」按钮保留（终端输出 / 代码片段等演示项）。
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  AtSign,
  Bot,
  Check,
  Code,
  FileText,
  MessageSquare,
  Paperclip,
  SendHorizontal,
  Slash,
  Terminal,
  Workflow,
  X,
} from 'lucide-react';
import { useOperant } from '../../context/ClientContext';
import { useDemo } from '../../demo/DemoContext';
import type { DemoAgent, DemoConversation } from '../../demo/types';
import { Modal } from '../../components/Modal';
import { useDialogA11y } from '../../components/useDialogA11y';
import { handleMenuArrowKeys, useOutsideClick } from './chatUtils';

const TEXTAREA_MAX_HEIGHT = 118;

/** 上下文对象定义 */
export interface ContextItem {
  id: string;
  type: 'agent' | 'file' | 'conversation' | 'workflow_template' | 'workflow_instance';
  name: string;
  meta?: string;
  color?: string;
}

/** 斜杠命令定义 */
interface SlashCommand {
  id: string;
  label: string;
  aliases: string[];
  desc: string;
  category: 'nav' | 'action' | 'cli';
  backendNote?: string;
}

const SLASH_COMMANDS: SlashCommand[] = [
  // 导航组
  { id: 'help', label: '/帮助', aliases: ['/help', '/?'], desc: '查看全部命令速查与说明', category: 'nav' },
  { id: 'models', label: '/模型', aliases: ['/models'], desc: '跳转至设置「模型配置」', category: 'nav' },
  { id: 'agents', label: '/角色', aliases: ['/agents'], desc: '跳转至 Agent 角色管理', category: 'nav' },
  { id: 'approvals', label: '/审批', aliases: ['/approvals'], desc: '跳转至审批与权限中心', category: 'nav' },
  { id: 'new', label: '/新建会话', aliases: ['/new'], desc: '创建新的对话会话', category: 'nav' },
  { id: 'export-trace', label: '/导出追踪', aliases: ['/export', '/trace'], desc: '导出当前会话的执行 Trace 记录', category: 'nav' },
  // 会话动作组
  { id: 'rename', label: '/重命名', aliases: ['/rename'], desc: '重命名当前会话', category: 'action' },
  { id: 'clear', label: '/清空上下文', aliases: ['/clear'], desc: '清空当前会话历史消息', category: 'action' },
  { id: 'compact', label: '/压缩上下文', aliases: ['/compact'], desc: '阶段性压缩上下文历史并归纳摘要', category: 'action' },
  { id: 'demo', label: '/切换演示模式', aliases: ['/demo'], desc: '切换演示数据模拟响应模式', category: 'action' },
  // CLI 风格占位（后端实现要求）
  {
    id: 'init',
    label: '/init',
    aliases: ['/init'],
    desc: '【需后端实现】CLI 风格工作区初始化命令',
    category: 'cli',
    backendNote: '需后端提供 CLI/API 工作区初始化语义与模版配置',
  },
  {
    id: 'review',
    label: '/review',
    aliases: ['/review'],
    desc: '【需后端实现】CLI 风格代码审查流水线命令',
    category: 'cli',
    backendNote: '需后端提供审查只读 Workflow 启动与裁决接口',
  },
];

const CONTEXT_ADD_ITEMS = [
  { key: 'terminal', icon: Terminal, label: '终端输出' },
  { key: 'snippet', icon: Code, label: '代码片段' },
] as const;

interface ComposerProps {
  conversation: DemoConversation;
}

export const Composer: React.FC<ComposerProps> = ({ conversation }) => {
  const {
    agents,
    getAgent,
    conversations,
    files,
    sendMessage,
    sendGroupMessage,
    createConversation,
    renameConversation,
    clearConversationMessages,
    compactConversationContext,
    getWorkflowDirectory,
  } = useDemo();
  const { addNotification } = useOperant();
  const navigate = useNavigate();

  const conversationId = conversation.id;
  const isGroup = Boolean(conversation.workflowId);
  const readOnly = conversation.lifecycle === 'archived';

  const [value, setValue] = useState('');
  const [selectedContexts, setSelectedContexts] = useState<ContextItem[]>([]);

  // 弹层状态
  const [slashOpen, setSlashOpen] = useState(false);
  const [slashIndex, setSlashIndex] = useState(0);
  const [contextPickerOpen, setContextPickerOpen] = useState(false);
  const [addContextOpen, setAddContextOpen] = useState(false);
  const [helpModalOpen, setHelpModalOpen] = useState(false);
  const [renameModalOpen, setRenameModalOpen] = useState(false);
  const [renameInput, setRenameInput] = useState('');

  // 群聊默认接收者
  const [audienceAgentId, setAudienceAgentId] = useState('agent_planner');

  // 工作流目录缓存
  const [wfTemplates, setWfTemplates] = useState<Array<{ id: string; name: string; version?: number; nodeCount: number }>>([]);

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const slashWrapRef = useRef<HTMLDivElement>(null);
  const contextPickerWrapRef = useRef<HTMLDivElement>(null);
  const addContextWrapRef = useRef<HTMLDivElement>(null);

  const closeAddContext = useCallback(() => setAddContextOpen(false), []);
  const closeContextPicker = useCallback(() => setContextPickerOpen(false), []);
  const closeSlash = useCallback(() => setSlashOpen(false), []);

  const addContextPanelRef = useDialogA11y<HTMLDivElement>(addContextOpen, closeAddContext);
  useOutsideClick(addContextWrapRef, addContextOpen, closeAddContext);
  useOutsideClick(contextPickerWrapRef, contextPickerOpen, closeContextPicker);
  useOutsideClick(slashWrapRef, slashOpen, closeSlash);

  useEffect(() => {
    getWorkflowDirectory().then((list) => {
      setWfTemplates(list.map((w) => ({ id: w.id, name: w.name, version: w.version, nodeCount: w.nodeCount })));
    });
  }, [getWorkflowDirectory]);

  // 切换会话时重置
  useEffect(() => {
    setValue('');
    setSelectedContexts([]);
    setSlashOpen(false);
    setContextPickerOpen(false);
    setAddContextOpen(false);
    setAudienceAgentId('agent_planner');
  }, [conversationId]);

  // 自动增高
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, TEXTAREA_MAX_HEIGHT)}px`;
  }, [value, conversationId]);

  // 过滤斜杠命令
  const slashFilterQuery = useMemo(() => {
    if (!value.startsWith('/')) return '';
    return value.slice(1).trim().toLowerCase();
  }, [value]);

  const filteredSlashCommands = useMemo(() => {
    if (!value.startsWith('/')) return SLASH_COMMANDS;
    if (!slashFilterQuery) return SLASH_COMMANDS;
    return SLASH_COMMANDS.filter((cmd) =>
      cmd.label.toLowerCase().includes(slashFilterQuery) ||
      cmd.desc.toLowerCase().includes(slashFilterQuery) ||
      cmd.aliases.some((a) => a.toLowerCase().includes(slashFilterQuery))
    );
  }, [value, slashFilterQuery]);

  // @ 上下文候选对象构建（四类分组）
  const allContextCandidates = useMemo(() => {
    // 1. Agent
    const agentItems: ContextItem[] = (isGroup
      ? conversation.agentIds.map((id) => getAgent(id)).filter((a): a is DemoAgent => Boolean(a))
      : agents
    ).map((a) => ({
      id: a.id,
      type: 'agent',
      name: a.name,
      meta: a.model,
      color: a.color,
    }));

    // 2. 文件
    const convFiles = files[conversationId] ?? [];
    const fileItems: ContextItem[] = convFiles.map((f) => ({
      id: f.id,
      type: 'file',
      name: f.name,
      meta: f.path,
    }));

    // 3. 会话
    const otherConvs = conversations.filter((c) => c.id !== conversationId && c.lifecycle !== 'archived');
    const convItems: ContextItem[] = otherConvs.map((c) => ({
      id: c.id,
      type: 'conversation',
      name: c.title,
      meta: c.workflowId ? '工作流实例' : '对话',
    }));

    // 4. 工作流
    const wfItems: ContextItem[] = wfTemplates.map((t) => ({
      id: t.id,
      type: 'workflow_template',
      name: t.name,
      meta: `${t.version ? `v${t.version} · ` : ''}${t.nodeCount} 节点`,
    }));

    return {
      agents: agentItems,
      files: fileItems,
      conversations: convItems,
      workflows: wfItems,
    };
  }, [isGroup, conversation.agentIds, getAgent, agents, files, conversationId, conversations, wfTemplates]);

  // 执行斜杠命令
  const executeSlashCommand = useCallback(
    (cmd: SlashCommand) => {
      setValue('');
      setSlashOpen(false);

      switch (cmd.id) {
        case 'help':
          setHelpModalOpen(true);
          break;
        case 'models':
          navigate('/settings?tab=models');
          break;
        case 'agents':
          navigate('/agents');
          break;
        case 'approvals':
          navigate('/approvals');
          break;
        case 'new': {
          const newId = createConversation('新会话');
          navigate(`/chat/${newId}`);
          break;
        }
        case 'export-trace':
          addNotification('success', `已导出会话「${conversation.title}」的 Trace 记录（演示）`);
          break;
        case 'rename':
          setRenameInput(conversation.title);
          setRenameModalOpen(true);
          break;
        case 'clear':
          clearConversationMessages(conversationId);
          break;
        case 'compact':
          compactConversationContext(conversationId);
          break;
        case 'demo':
          addNotification('info', '当前处于本地 Demo 模拟运行模式');
          break;
        case 'init':
          addNotification('warn', '需后端实现：CLI 风格 /init 初始化命令（已立项契约提议）');
          break;
        case 'review':
          addNotification('warn', '需后端实现：CLI 风格 /review 审查命令（已立项契约提议）');
          break;
        default:
          break;
      }
    },
    [navigate, createConversation, conversation.title, addNotification, conversationId, clearConversationMessages, compactConversationContext]
  );

  // 切换选中上下文项
  const toggleContextItem = (item: ContextItem) => {
    setSelectedContexts((prev) => {
      const exists = prev.some((c) => c.id === item.id && c.type === item.type);
      if (exists) {
        return prev.filter((c) => !(c.id === item.id && c.type === item.type));
      }
      return [...prev, item];
    });
  };

  const removeContextChip = (item: ContextItem) => {
    setSelectedContexts((prev) => prev.filter((c) => !(c.id === item.id && c.type === item.type)));
  };

  const doSend = useCallback(() => {
    const text = value.trim();
    if ((!text && selectedContexts.length === 0) || readOnly) return;

    // 若以 / 开头且匹配到命令，则执行第一条匹配命令
    if (value.startsWith('/') && filteredSlashCommands.length > 0) {
      executeSlashCommand(filteredSlashCommands[0]);
      return;
    }

    // 格式化携带上下文的消息内容
    let finalPayloadText = text;
    if (selectedContexts.length > 0) {
      const ctxPrefix = selectedContexts
        .map((c) => `[上下文: ${c.name}]`)
        .join(' ');
      finalPayloadText = text ? `${ctxPrefix}\n${text}` : ctxPrefix;
    }

    if (isGroup) {
      sendGroupMessage(conversationId, finalPayloadText, [audienceAgentId]);
    } else {
      sendMessage(conversationId, finalPayloadText);
    }

    addNotification('success', '已发送（演示）');
    setValue('');
    setSelectedContexts([]);
    setSlashOpen(false);
    setContextPickerOpen(false);
    textareaRef.current?.focus();
  }, [value, selectedContexts, readOnly, filteredSlashCommands, executeSlashCommand, isGroup, sendGroupMessage, conversationId, audienceAgentId, sendMessage, addNotification]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // 斜杠命令键盘导航
    if (slashOpen && filteredSlashCommands.length > 0) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setSlashIndex((i) => (i + 1) % filteredSlashCommands.length);
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setSlashIndex((i) => (i - 1 + filteredSlashCommands.length) % filteredSlashCommands.length);
        return;
      }
      if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault();
        executeSlashCommand(filteredSlashCommands[slashIndex]);
        return;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        setSlashOpen(false);
        return;
      }
    }

    // 普通键盘发送
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      doSend();
    }
  };

  const handleInputChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const val = e.target.value;
    setValue(val);
    if (val.startsWith('/')) {
      setSlashOpen(true);
      setSlashIndex(0);
    } else {
      setSlashOpen(false);
    }
  };

  const handleRenameSubmit = () => {
    if (renameInput.trim()) {
      renameConversation(conversationId, renameInput.trim());
      addNotification('success', `会话已重命名为「${renameInput.trim()}」（演示）`);
    }
    setRenameModalOpen(false);
  };

  const canSend = (value.trim().length > 0 || selectedContexts.length > 0) && !readOnly;
  const leadAgent = agents.find((a) => a.id === audienceAgentId);

  const placeholder = readOnly
    ? '该实例已归档，仅供查看历史消息'
    : isGroup
      ? audienceAgentId === 'agent_planner'
        ? '发言默认发给规划师，输入 / 唤起命令，输入 @ 关联上下文'
        : `发送给 ${leadAgent?.name ?? '规划师'}，输入 / 唤起命令，输入 @ 关联上下文`
      : '输入消息，输入 / 唤起命令，@ 关联上下文，Enter 发送';

  return (
    <div className="chat-composer">
      {/* Context Chips 浮现于输入框上方 */}
      {selectedContexts.length > 0 && (
        <div className="chat-context-chips-bar" aria-label="已关联的上下文对象">
          <div className="chat-context-chips-list">
            {selectedContexts.map((ctx) => {
              const Icon =
                ctx.type === 'agent'
                  ? Bot
                  : ctx.type === 'file'
                    ? FileText
                    : ctx.type === 'conversation'
                      ? MessageSquare
                      : Workflow;
              return (
                <span key={`${ctx.type}_${ctx.id}`} className="chat-context-chip">
                  <Icon size={12} className="chat-context-chip-icon" style={{ color: ctx.color }} aria-hidden="true" />
                  <span className="chat-context-chip-name">{ctx.name}</span>
                  <button
                    type="button"
                    className="chat-context-chip-del"
                    onClick={() => removeContextChip(ctx)}
                    aria-label={`移除 ${ctx.name}`}
                  >
                    <X size={10} />
                  </button>
                </span>
              );
            })}
          </div>
          <button
            type="button"
            className="chat-context-chips-clear"
            onClick={() => setSelectedContexts([])}
            title="清空全部上下文"
          >
            清空
          </button>
        </div>
      )}

      <div className="chat-composer-inner">
        <div className="chat-composer-tools">
          {/* 斜杠命令按钮 */}
          <div className="chat-menu-wrap" ref={slashWrapRef}>
            <button
              className={`chat-composer-btn${slashOpen ? ' active' : ''}`}
              onClick={() => {
                setSlashOpen((v) => !v);
                setContextPickerOpen(false);
                setAddContextOpen(false);
              }}
              disabled={readOnly}
              aria-label="斜杠命令"
              title="输入 / 唤起命令面板"
            >
              <Slash size={15} aria-hidden="true" />
            </button>
            {slashOpen && (
              <div className="chat-popover chat-slash-palette" role="menu" aria-label="斜杠命令列表">
                <div className="chat-slash-head">
                  <span className="chat-slash-title">斜杠命令</span>
                  <span className="chat-slash-hint">↑↓ 选择，Enter 确认，Esc 关闭</span>
                </div>
                <div className="chat-slash-list">
                  {filteredSlashCommands.length === 0 ? (
                    <div className="chat-popover-empty">无匹配命令</div>
                  ) : (
                    filteredSlashCommands.map((cmd, i) => (
                      <button
                        key={cmd.id}
                        role="menuitem"
                        className={`chat-slash-item${i === slashIndex ? ' active' : ''}`}
                        onMouseEnter={() => setSlashIndex(i)}
                        onClick={() => executeSlashCommand(cmd)}
                      >
                        <div className="chat-slash-item-main">
                          <span className="chat-slash-item-name">{cmd.label}</span>
                          <span className="chat-slash-item-desc">{cmd.desc}</span>
                        </div>
                        <span className={`chat-slash-badge badge-${cmd.category}`}>
                          {cmd.category === 'nav' ? '导航' : cmd.category === 'action' ? '动作' : 'CLI 契约'}
                        </span>
                      </button>
                    ))
                  )}
                </div>
              </div>
            )}
          </div>

          {/* @ 统一上下文选择器按钮 */}
          <div className="chat-menu-wrap" ref={contextPickerWrapRef}>
            <button
              className={`chat-composer-btn${contextPickerOpen ? ' active' : ''}`}
              onClick={() => {
                setContextPickerOpen((v) => !v);
                setSlashOpen(false);
                setAddContextOpen(false);
              }}
              disabled={readOnly}
              aria-label="关联上下文对象"
              title="输入 @ 关联 Agent / 文件 / 会话 / 工作流"
            >
              <AtSign size={15} aria-hidden="true" />
            </button>

            {contextPickerOpen && (
              <div className="chat-popover chat-context-picker" role="dialog" aria-label="统一上下文选择器">
                <div className="chat-context-picker-head">
                  <span className="chat-context-picker-title">关联上下文</span>
                  <span className="chat-context-picker-count">已选 {selectedContexts.length} 项</span>
                </div>

                <div className="chat-context-picker-scroll">
                  {/* Agent 分组 */}
                  <div className="chat-context-group">
                    <div className="chat-context-group-title">
                      <Bot size={12} /> Agent 角色
                    </div>
                    {allContextCandidates.agents.map((item) => {
                      const isSelected = selectedContexts.some((c) => c.id === item.id && c.type === item.type);
                      return (
                        <button
                          key={item.id}
                          type="button"
                          className={`chat-context-item${isSelected ? ' selected' : ''}`}
                          onClick={() => toggleContextItem(item)}
                        >
                          <span className="chat-context-check">{isSelected ? <Check size={12} /> : null}</span>
                          <span className="rail-sidebar-dot" style={{ backgroundColor: item.color }} />
                          <span className="chat-context-item-name">{item.name}</span>
                          <span className="chat-context-item-meta">{item.meta}</span>
                        </button>
                      );
                    })}
                  </div>

                  {/* 文件分组 */}
                  {allContextCandidates.files.length > 0 && (
                    <div className="chat-context-group">
                      <div className="chat-context-group-title">
                        <FileText size={12} /> 会话文件
                      </div>
                      {allContextCandidates.files.map((item) => {
                        const isSelected = selectedContexts.some((c) => c.id === item.id && c.type === item.type);
                        return (
                          <button
                            key={item.id}
                            type="button"
                            className={`chat-context-item${isSelected ? ' selected' : ''}`}
                            onClick={() => toggleContextItem(item)}
                          >
                            <span className="chat-context-check">{isSelected ? <Check size={12} /> : null}</span>
                            <span className="chat-context-item-name">{item.name}</span>
                            <span className="chat-context-item-meta">{item.meta}</span>
                          </button>
                        );
                      })}
                    </div>
                  )}

                  {/* 工作流模板/实例分组 */}
                  {allContextCandidates.workflows.length > 0 && (
                    <div className="chat-context-group">
                      <div className="chat-context-group-title">
                        <Workflow size={12} /> 工作流模板
                      </div>
                      {allContextCandidates.workflows.map((item) => {
                        const isSelected = selectedContexts.some((c) => c.id === item.id && c.type === item.type);
                        return (
                          <button
                            key={item.id}
                            type="button"
                            className={`chat-context-item${isSelected ? ' selected' : ''}`}
                            onClick={() => toggleContextItem(item)}
                          >
                            <span className="chat-context-check">{isSelected ? <Check size={12} /> : null}</span>
                            <span className="chat-context-item-name">{item.name}</span>
                            <span className="chat-context-item-meta">{item.meta}</span>
                          </button>
                        );
                      })}
                    </div>
                  )}

                  {/* 会话分组 */}
                  {allContextCandidates.conversations.length > 0 && (
                    <div className="chat-context-group">
                      <div className="chat-context-group-title">
                        <MessageSquare size={12} /> 其他会话
                      </div>
                      {allContextCandidates.conversations.map((item) => {
                        const isSelected = selectedContexts.some((c) => c.id === item.id && c.type === item.type);
                        return (
                          <button
                            key={item.id}
                            type="button"
                            className={`chat-context-item${isSelected ? ' selected' : ''}`}
                            onClick={() => toggleContextItem(item)}
                          >
                            <span className="chat-context-check">{isSelected ? <Check size={12} /> : null}</span>
                            <span className="chat-context-item-name">{item.name}</span>
                            <span className="chat-context-item-meta">{item.meta}</span>
                          </button>
                        );
                      })}
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>

          {/* 原「添加上下文」按钮（终端输出 / 代码片段） */}
          <div className="chat-menu-wrap" ref={addContextWrapRef}>
            <button
              className="chat-composer-btn"
              onClick={() => {
                setAddContextOpen((v) => !v);
                setSlashOpen(false);
                setContextPickerOpen(false);
              }}
              disabled={readOnly}
              aria-haspopup="menu"
              aria-expanded={addContextOpen}
              title="添加片段/终端输出"
            >
              <Paperclip size={15} aria-hidden="true" />
              <span className="chat-composer-btn-label">添加上下文</span>
            </button>
            {addContextOpen && (
              <div
                className="chat-popover chat-context-pop"
                role="menu"
                aria-label="添加上下文"
                ref={addContextPanelRef}
                onKeyDown={handleMenuArrowKeys}
              >
                {CONTEXT_ADD_ITEMS.map((item) => {
                  const Icon = item.icon;
                  return (
                    <button
                      key={item.key}
                      className="chat-menu-item"
                      role="menuitem"
                      onClick={() => {
                        addNotification('info', `添加${item.label}上下文（演示）`);
                        closeAddContext();
                      }}
                    >
                      <Icon size={14} aria-hidden="true" />
                      {item.label}
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        </div>

        <textarea
          ref={textareaRef}
          className="chat-composer-input"
          value={value}
          onChange={handleInputChange}
          onKeyDown={handleKeyDown}
          rows={1}
          placeholder={placeholder}
          aria-label="输入消息"
          disabled={readOnly}
        />

        <button
          className="btn btn-primary chat-send-btn"
          onClick={doSend}
          disabled={!canSend}
          aria-label="发送消息"
        >
          <SendHorizontal size={14} aria-hidden="true" />
          发送
        </button>
      </div>

      {/* 命令速查 Modal (/帮助) */}
      <Modal
        isOpen={helpModalOpen}
        onClose={() => setHelpModalOpen(false)}
        title="斜杠命令速查"
        maxWidth={560}
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <p style={{ fontSize: '13px', color: 'var(--text-secondary)', lineHeight: 1.5 }}>
            在输入框中输入 <code style={{ color: 'var(--accent-action)' }}>/</code> 即可随时唤起命令面板并实时过滤。
          </p>

          <div>
            <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-muted)', marginBottom: 8 }}>
              导航命令
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {SLASH_COMMANDS.filter((c) => c.category === 'nav').map((cmd) => (
                <div key={cmd.id} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '6px 10px', background: 'var(--bg-surface)', borderRadius: 'var(--radius-sm)' }}>
                  <div>
                    <span style={{ fontFamily: 'var(--font-mono)', fontWeight: 600, fontSize: '13px', color: 'var(--text-primary)' }}>{cmd.label}</span>
                    <span style={{ fontSize: '12px', color: 'var(--text-secondary)', marginLeft: 8 }}>{cmd.desc}</span>
                  </div>
                  <span className="badge" style={{ backgroundColor: 'var(--bg-subtle)' }}>{cmd.aliases.join(', ')}</span>
                </div>
              ))}
            </div>
          </div>

          <div>
            <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-muted)', marginBottom: 8 }}>
              会话动作命令
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {SLASH_COMMANDS.filter((c) => c.category === 'action').map((cmd) => (
                <div key={cmd.id} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '6px 10px', background: 'var(--bg-surface)', borderRadius: 'var(--radius-sm)' }}>
                  <div>
                    <span style={{ fontFamily: 'var(--font-mono)', fontWeight: 600, fontSize: '13px', color: 'var(--text-primary)' }}>{cmd.label}</span>
                    <span style={{ fontSize: '12px', color: 'var(--text-secondary)', marginLeft: 8 }}>{cmd.desc}</span>
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div>
            <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--status-warn-text)', marginBottom: 8 }}>
              CLI 风格命令（需后端实现要求）
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {SLASH_COMMANDS.filter((c) => c.category === 'cli').map((cmd) => (
                <div key={cmd.id} style={{ display: 'flex', flexDirection: 'column', gap: 4, padding: '8px 10px', background: 'var(--bg-surface)', borderLeft: '3px solid var(--status-warn-border)', borderRadius: 'var(--radius-sm)' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <span style={{ fontFamily: 'var(--font-mono)', fontWeight: 600, fontSize: '13px', color: 'var(--text-primary)' }}>{cmd.label}</span>
                    <span className="badge badge-warn" style={{ fontSize: '10px' }}>需后端实现</span>
                  </div>
                  <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>{cmd.desc}</span>
                  {cmd.backendNote && <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>规范要求：{cmd.backendNote}</span>}
                </div>
              ))}
            </div>
          </div>
        </div>
      </Modal>

      {/* 会话重命名 Modal */}
      <Modal
        isOpen={renameModalOpen}
        onClose={() => setRenameModalOpen(false)}
        title="重命名会话"
        footer={
          <>
            <button className="btn btn-ghost" onClick={() => setRenameModalOpen(false)}>
              取消
            </button>
            <button className="btn btn-primary" onClick={handleRenameSubmit} disabled={!renameInput.trim()}>
              确认重命名
            </button>
          </>
        }
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          <label style={{ fontSize: '12px', color: 'var(--text-muted)' }}>请输入新的会话标题：</label>
          <input
            type="text"
            className="input"
            value={renameInput}
            onChange={(e) => setRenameInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') handleRenameSubmit();
            }}
            autoFocus
          />
        </div>
      </Modal>
    </div>
  );
};
