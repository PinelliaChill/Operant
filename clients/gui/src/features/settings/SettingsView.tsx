/**
 * 设置中心（v9：Codex 式单流纵向滚动重构 + Hermes 提供商模型扫描 + 主流厂商 OAuth 授权登录）
 * 4 大主类（左侧主导航）：
 * 1. 通用偏好 (general)：### 外观与界面 / ### 快捷键速查与自定义 / ### 对话与交互偏好
 * 2. 模型与提供商 (models)：### 已配置模型概览 / ### 主流厂商 OAuth 快速连接 / ### 模型提供商 (Hermes 模式) / ### 终端与执行环境 / ### 网络与代理
 * 3. 安全与治理 (security)：### 审批与权限策略 (PolicySettings) / ### 记忆治理 (SQLite FTS5) / ### 保留与审计策略
 * 4. 系统与设备 (system)：### 远程与设备管理 (RemoteView) / ### 关于与系统诊断
 *
 * 移除右侧顶部的横向 Sub-Tabs 切换栏，右侧为单页纵向平滑滚动流。
 * 旧深链自动兼容重定向：?tab=workflow → /collab；?tab=roles → /agents；?tab=* → ?cat=*
 */

import React, { useEffect, useMemo, useState } from 'react';
import { Navigate, useSearchParams } from 'react-router-dom';
import {
  Settings,
  Cpu,
  Sliders,
  Brain,
  ShieldCheck,
  Plus,
  RefreshCw,
  Trash2,
  CheckCircle2,
  Search,
  HardDrive,
  Radio,
  Sun,
  Moon,
  RotateCcw,
  Info,
  Keyboard,
  Palette,
  Terminal,
  Globe,
  MessageSquare,
  Download,
  Sparkles,
  Activity,
  ChevronDown,
  ChevronRight,
  Server,
  Zap,
  CheckSquare,
  Square,
  Layers,
  Power,
  CheckCheck,
  Check,
  Link as LinkIcon,
  LogOut,
  UserCheck,
  ExternalLink,
  Shield,
  KeyRound,
} from 'lucide-react';
import { useOperant } from '../../context/ClientContext';
import { useDemo } from '../../demo/DemoContext';
import { Effort, Memory, ModelProfile } from '@operant/sdk';
import { StatusBadge } from '../../components/StatusBadge';
import { Modal } from '../../components/Modal';
import { RemoteView } from '../remote/RemoteView';
import { PolicySettings } from '../approvals/PolicySettings';
import { formatNumber } from '../../lib/format';

type MainCategory = 'general' | 'models' | 'security' | 'system';

interface CategoryConfig {
  key: MainCategory;
  label: string;
  subtitle: string;
  icon: React.ComponentType<{ size?: number; color?: string; style?: React.CSSProperties }>;
}

const CATEGORIES: CategoryConfig[] = [
  {
    key: 'general',
    label: '通用偏好',
    subtitle: '配置界面外观、全局键盘快捷键以及对话生成与推理偏好',
    icon: Sliders,
  },
  {
    key: 'models',
    label: '模型与提供商',
    subtitle: '管理上游模型提供方与本地端点，支持 OAuth 账号连接、一键扫描并批量导入系统模型',
    icon: Cpu,
  },
  {
    key: 'security',
    label: '安全与治理',
    subtitle: '配置 Action Gateway 风险防护规则、记忆生命周期与审计边界',
    icon: ShieldCheck,
  },
  {
    key: 'system',
    label: '系统与设备',
    subtitle: '管理远程设备配对、检查内核诊断与生命周期状态',
    icon: Settings,
  },
];

/** 记忆 kind → 中文标签 */
const MEMORY_KIND_LABELS: Record<string, string> = {
  project: '项目',
  working: '工作',
  episodic: '情景',
};

const PRESET_ACCENT_COLORS = [
  { name: '松绿 (默认)', value: '#059669' },
  { name: '经典蓝', value: '#2563eb' },
  { name: '极光紫', value: '#7c3aed' },
  { name: '琥珀橙', value: '#ea580c' },
  { name: '玫瑰红', value: '#db2777' },
];

/** OAuth 快速连接厂商定义 */
interface OAuthProviderDef {
  id: string;
  name: string;
  desc: string;
  officialBaseUrl: string;
  defaultSecretRef: string;
  badge: string;
  scopes: string[];
  defaultUser: string;
  defaultEmail: string;
}

const OAUTH_PROVIDERS: OAuthProviderDef[] = [
  {
    id: 'github',
    name: 'GitHub Models',
    desc: 'GitHub Copilot / Azure AI 开发者模型生态',
    officialBaseUrl: 'https://models.github.ai/inference',
    defaultSecretRef: 'GITHUB_TOKEN',
    badge: 'GitHub OAuth',
    scopes: ['read:user', 'models:inference', 'repo:status'],
    defaultUser: 'operant-developer',
    defaultEmail: 'developer@github.com',
  },
  {
    id: 'anthropic',
    name: 'Anthropic',
    desc: 'Claude Code / Claude Pro 开发者账号授权',
    officialBaseUrl: 'https://api.anthropic.com/v1',
    defaultSecretRef: 'ANTHROPIC_API_KEY',
    badge: 'Claude OAuth',
    scopes: ['model:read', 'model:complete', 'user:profile'],
    defaultUser: 'Claude Pro Developer',
    defaultEmail: 'user@anthropic.com',
  },
  {
    id: 'openai',
    name: 'OpenAI',
    desc: 'ChatGPT Plus / Team / API 开发者平台授权',
    officialBaseUrl: 'https://api.openai.com/v1',
    defaultSecretRef: 'OPENAI_API_KEY',
    badge: 'OpenAI PKCE',
    scopes: ['models.read', 'completions.write', 'user.profile'],
    defaultUser: 'OpenAI Developer',
    defaultEmail: 'developer@openai.com',
  },
  {
    id: 'google',
    name: 'Google Gemini',
    desc: 'Google AI Studio / Vertex AI 账号直连',
    officialBaseUrl: 'https://generativelanguage.googleapis.com/v1beta/openai',
    defaultSecretRef: 'GEMINI_API_KEY',
    badge: 'Google OAuth',
    scopes: ['https://www.googleapis.com/auth/generative-language', 'email'],
    defaultUser: 'Gemini Advanced User',
    defaultEmail: 'developer@gmail.com',
  },
  {
    id: 'openrouter',
    name: 'OpenRouter',
    desc: '全球模型路由统一账号授权与积分共享',
    officialBaseUrl: 'https://openrouter.ai/api/v1',
    defaultSecretRef: 'OPENROUTER_API_KEY',
    badge: 'OpenRouter Key',
    scopes: ['read:models', 'write:generations'],
    defaultUser: 'OpenRouter Member',
    defaultEmail: 'openrouter-user@ai.dev',
  },
];

/** Hermes 风格云端提供商预置 */
interface HermesProviderPreset {
  id: string;
  name: string;
  description: string;
  officialBaseUrl: string;
  defaultSecretRef: string;
  tag: string;
  popularModels: string[];
  supportsOAuth?: boolean;
}

const CLOUD_PROVIDERS: HermesProviderPreset[] = [
  {
    id: 'deepseek',
    name: 'DeepSeek',
    description: '深度求索官方 API，提供强大的通用编程与 R1 深度推理能力。',
    officialBaseUrl: 'https://api.deepseek.com/v1',
    defaultSecretRef: 'DEEPSEEK_API_KEY',
    tag: '热门首选',
    popularModels: ['deepseek-chat', 'deepseek-reasoner', 'deepseek-v3', 'deepseek-r1-2025'],
  },
  {
    id: 'anthropic',
    name: 'Anthropic',
    description: '官方 Claude 系列模型，业内领先的代码理解与混合思考能力。',
    officialBaseUrl: 'https://api.anthropic.com/v1',
    defaultSecretRef: 'ANTHROPIC_API_KEY',
    tag: '代码标杆',
    popularModels: ['claude-3-7-sonnet-20250219', 'claude-3-5-sonnet-20241022', 'claude-3-5-haiku-20241022'],
    supportsOAuth: true,
  },
  {
    id: 'openai',
    name: 'OpenAI',
    description: '官方 GPT-4o 与 o1/o3-mini 推理模型系列。',
    officialBaseUrl: 'https://api.openai.com/v1',
    defaultSecretRef: 'OPENAI_API_KEY',
    tag: '旗舰通用',
    popularModels: ['gpt-4o', 'gpt-4o-mini', 'o1', 'o3-mini', 'gpt-4.5-preview'],
    supportsOAuth: true,
  },
  {
    id: 'google',
    name: 'Google Gemini',
    description: '谷歌官方 Gemini 2.0 Flash / Pro 系列超长上下文模型。',
    officialBaseUrl: 'https://generativelanguage.googleapis.com/v1beta/openai',
    defaultSecretRef: 'GEMINI_API_KEY',
    tag: '百万上下文',
    popularModels: ['gemini-2.0-flash-exp', 'gemini-2.0-flash-thinking-exp', 'gemini-1.5-pro-latest'],
    supportsOAuth: true,
  },
  {
    id: 'github',
    name: 'GitHub Models',
    description: '微软与 GitHub 托管的 Azure OpenAI 与开源大模型推断端点。',
    officialBaseUrl: 'https://models.github.ai/inference',
    defaultSecretRef: 'GITHUB_TOKEN',
    tag: '开发者免费配额',
    popularModels: ['gpt-4o', 'claude-3-5-sonnet', 'deepseek-r1', 'meta-llama-3.1-405b-instruct'],
    supportsOAuth: true,
  },
  {
    id: 'openrouter',
    name: 'OpenRouter',
    description: '全球统一多模型路由聚合网关，支持全球数百款主流模型。',
    officialBaseUrl: 'https://openrouter.ai/api/v1',
    defaultSecretRef: 'OPENROUTER_API_KEY',
    tag: '聚合网关',
    popularModels: ['anthropic/claude-3.7-sonnet', 'openai/gpt-4o', 'deepseek/deepseek-r1', 'meta-llama/llama-3.3-70b-instruct'],
    supportsOAuth: true,
  },
  {
    id: 'siliconflow',
    name: 'SiliconFlow 硅基流动',
    description: '国内高并发低延迟模型云服务，支持 DeepSeek、Qwen 等开源大模型全速托管。',
    officialBaseUrl: 'https://api.siliconflow.cn/v1',
    defaultSecretRef: 'SILICONFLOW_API_KEY',
    tag: '极速托管',
    popularModels: ['deepseek-ai/DeepSeek-V3', 'deepseek-ai/DeepSeek-R1', 'Qwen/Qwen2.5-Coder-32B-Instruct'],
  },
  {
    id: 'fireworks',
    name: 'Fireworks AI',
    description: '专为生产级应用设计的超高速生成式 AI 推理平台。',
    officialBaseUrl: 'https://api.fireworks.ai/inference/v1',
    defaultSecretRef: 'FIREWORKS_API_KEY',
    tag: '超低延迟',
    popularModels: ['accounts/fireworks/models/deepseek-v3', 'accounts/fireworks/models/deepseek-r1', 'accounts/fireworks/models/llama-v3p3-70b-instruct'],
  },
  {
    id: 'xai',
    name: 'xAI',
    description: '埃隆·马斯克旗下 xAI 官方 Grok 系列多模态推理模型。',
    officialBaseUrl: 'https://api.x.ai/v1',
    defaultSecretRef: 'XAI_API_KEY',
    tag: 'Grok 系列',
    popularModels: ['grok-2-1212', 'grok-2-vision-1212', 'grok-beta'],
  },
  {
    id: 'minimax',
    name: 'MiniMax / MiniMax (China)',
    description: '名之梦官方 API，具备卓越的中文表达与超长上下文理解能力。',
    officialBaseUrl: 'https://api.minimax.chat/v1',
    defaultSecretRef: 'MINIMAX_API_KEY',
    tag: '超长上下文',
    popularModels: ['MiniMax-Text-01', 'abab6.5s-chat', 'abab6.5t-chat'],
  },
  {
    id: 'zhipu',
    name: '智谱 GLM',
    description: '智谱 AI 开放平台，提供 GLM-4 Plus 旗舰及 GLM-Zero 深度思考模型。',
    officialBaseUrl: 'https://open.bigmodel.cn/api/paas/v4',
    defaultSecretRef: 'ZHIPU_API_KEY',
    tag: '国产旗舰',
    popularModels: ['glm-4-plus', 'glm-4-air', 'glm-4-flash', 'glm-zero-preview'],
  },
  {
    id: 'moonshot',
    name: 'Moonshot / Kimi',
    description: '月之暗面 Kimi 系列超长文本无损上下文大模型。',
    officialBaseUrl: 'https://api.moonshot.cn/v1',
    defaultSecretRef: 'MOONSHOT_API_KEY',
    tag: '长文本长记忆',
    popularModels: ['kimi-latest', 'moonshot-v1-128k', 'moonshot-v1-32k'],
  },
  {
    id: 'opencode',
    name: 'OpenCode Zen / Go',
    description: 'OpenCode 专用编程优化大模型，专为多 Agent 协同编程设计。',
    officialBaseUrl: 'https://api.opencode.ai/v1',
    defaultSecretRef: 'OPENCODE_API_KEY',
    tag: 'Agent 原生',
    popularModels: ['opencode-zen-1', 'opencode-go-preview'],
  },
];

/** 本地端点预设 */
const LOCAL_ENDPOINTS = [
  { id: 'ollama', name: 'Ollama', baseUrl: 'http://localhost:11434/v1', secretRef: 'OLLAMA_API_KEY', desc: '本地 Ollama 引擎' },
  { id: 'vllm', name: 'vLLM', baseUrl: 'http://localhost:8000/v1', secretRef: 'VLLM_API_KEY', desc: '高性能本地 vLLM 实例' },
  { id: 'lmstudio', name: 'LM Studio', baseUrl: 'http://localhost:1234/v1', secretRef: 'LM_STUDIO_API_KEY', desc: 'LM Studio 桌面服务' },
  { id: 'llamacpp', name: 'Llama.cpp', baseUrl: 'http://localhost:8080/v1', secretRef: 'LLAMA_CPP_API_KEY', desc: 'Llama.cpp Server 端点' },
  { id: 'custom', name: '自定义 Base URL', baseUrl: 'http://localhost:8080/v1', secretRef: 'CUSTOM_API_KEY', desc: '自定义本地/内网端点' },
];

/** OAuth 登录账号信息 */
interface OAuthAccountInfo {
  providerId: string;
  providerName: string;
  accountName: string;
  email: string;
  tokenRef: string;
  authorizedAt: string;
}

/** 已接入的 API 提供商信息 */
interface ConnectedApiInfo {
  id: string;
  name: string;
  baseUrl: string;
  secretRef: string;
  tag?: string;
  addedAt?: string;
}

const DEFAULT_CONNECTED_APIS: ConnectedApiInfo[] = [
  {
    id: 'deepseek',
    name: 'DeepSeek',
    baseUrl: 'https://api.deepseek.com/v1',
    secretRef: 'DEEPSEEK_API_KEY',
    tag: '热门首选',
    addedAt: '2026-08-30',
  },
  {
    id: 'siliconflow',
    name: 'SiliconFlow 硅基流动',
    baseUrl: 'https://api.siliconflow.cn/v1',
    secretRef: 'SILICONFLOW_API_KEY',
    tag: '极速托管',
    addedAt: '2026-08-30',
  },
];

/** 智能推断上游模型配置元数据 */
function inferModelMetadata(modelId: string): {
  name: string;
  contextWindow: number;
  defaultTokenBudget: number;
  supportedEfforts: Effort[];
  defaultEffort: Effort;
} {
  const lower = modelId.toLowerCase();

  // Anthropic Claude
  if (lower.includes('claude-3-7-sonnet')) {
    return { name: 'Claude 3.7 Sonnet', contextWindow: 200000, defaultTokenBudget: 8000, supportedEfforts: ['low', 'medium', 'high'], defaultEffort: 'high' };
  }
  if (lower.includes('claude-3-5-sonnet')) {
    return { name: 'Claude 3.5 Sonnet', contextWindow: 200000, defaultTokenBudget: 8000, supportedEfforts: ['low', 'medium'], defaultEffort: 'medium' };
  }
  if (lower.includes('claude-3-5-haiku')) {
    return { name: 'Claude 3.5 Haiku', contextWindow: 200000, defaultTokenBudget: 4096, supportedEfforts: ['low', 'medium'], defaultEffort: 'low' };
  }
  if (lower.includes('claude-3-opus')) {
    return { name: 'Claude 3 Opus', contextWindow: 200000, defaultTokenBudget: 4096, supportedEfforts: ['medium', 'high'], defaultEffort: 'high' };
  }

  // DeepSeek
  if (lower.includes('deepseek-reasoner') || lower.includes('deepseek-r1')) {
    return { name: 'DeepSeek R1 (深度推理)', contextWindow: 64000, defaultTokenBudget: 8000, supportedEfforts: ['medium', 'high'], defaultEffort: 'high' };
  }
  if (lower.includes('deepseek-chat') || lower.includes('deepseek-v3')) {
    return { name: 'DeepSeek V3 (通用对话)', contextWindow: 64000, defaultTokenBudget: 4096, supportedEfforts: ['low', 'medium'], defaultEffort: 'medium' };
  }

  // OpenAI
  if (lower === 'gpt-4o' || lower.includes('gpt-4o-2024')) {
    return { name: 'OpenAI GPT-4o', contextWindow: 128000, defaultTokenBudget: 4096, supportedEfforts: ['low', 'medium'], defaultEffort: 'medium' };
  }
  if (lower.includes('gpt-4o-mini')) {
    return { name: 'OpenAI GPT-4o mini', contextWindow: 128000, defaultTokenBudget: 4096, supportedEfforts: ['low', 'medium'], defaultEffort: 'low' };
  }
  if (lower.includes('o1') || lower.includes('o3-mini')) {
    return { name: `OpenAI ${modelId}`, contextWindow: 200000, defaultTokenBudget: 8000, supportedEfforts: ['medium', 'high'], defaultEffort: 'high' };
  }
  if (lower.includes('gpt-4.5')) {
    return { name: 'OpenAI GPT-4.5 Preview', contextWindow: 128000, defaultTokenBudget: 4096, supportedEfforts: ['low', 'medium', 'high'], defaultEffort: 'medium' };
  }

  // Google Gemini
  if (lower.includes('gemini-2.0-flash-thinking')) {
    return { name: 'Gemini 2.0 Flash Thinking (思考推理)', contextWindow: 1000000, defaultTokenBudget: 8192, supportedEfforts: ['medium', 'high'], defaultEffort: 'high' };
  }
  if (lower.includes('gemini-2.0-flash')) {
    return { name: 'Gemini 2.0 Flash (极速百万上下文)', contextWindow: 1000000, defaultTokenBudget: 8192, supportedEfforts: ['low', 'medium'], defaultEffort: 'medium' };
  }
  if (lower.includes('gemini-1.5-pro')) {
    return { name: 'Gemini 1.5 Pro (200万上下文旗舰)', contextWindow: 2000000, defaultTokenBudget: 8192, supportedEfforts: ['low', 'medium', 'high'], defaultEffort: 'medium' };
  }
  if (lower.includes('gemini-1.5-flash')) {
    return { name: 'Gemini 1.5 Flash (轻量高效)', contextWindow: 1000000, defaultTokenBudget: 4096, supportedEfforts: ['low', 'medium'], defaultEffort: 'low' };
  }

  // Qwen
  if (lower.includes('qwen2.5-coder') || lower.includes('qwen2p5-coder')) {
    return { name: `Qwen 2.5 Coder (${modelId})`, contextWindow: 32000, defaultTokenBudget: 4096, supportedEfforts: ['low', 'medium', 'high'], defaultEffort: 'medium' };
  }

  // Llama
  if (lower.includes('llama-3.3') || lower.includes('llama3.3') || lower.includes('llama')) {
    return { name: `Llama (${modelId})`, contextWindow: 128000, defaultTokenBudget: 4096, supportedEfforts: ['low', 'medium'], defaultEffort: 'medium' };
  }

  // Grok
  if (lower.includes('grok')) {
    return { name: `xAI Grok (${modelId})`, contextWindow: 128000, defaultTokenBudget: 4096, supportedEfforts: ['low', 'medium'], defaultEffort: 'medium' };
  }

  // GLM
  if (lower.includes('glm')) {
    return { name: `智谱 GLM (${modelId})`, contextWindow: 128000, defaultTokenBudget: 4096, supportedEfforts: ['low', 'medium'], defaultEffort: 'medium' };
  }

  // MiniMax
  if (lower.includes('minimax') || lower.includes('abab')) {
    return { name: `MiniMax (${modelId})`, contextWindow: 245000, defaultTokenBudget: 4096, supportedEfforts: ['low', 'medium'], defaultEffort: 'medium' };
  }

  // Moonshot
  if (lower.includes('moonshot') || lower.includes('kimi')) {
    return { name: `Kimi / Moonshot (${modelId})`, contextWindow: 128000, defaultTokenBudget: 4096, supportedEfforts: ['low', 'medium'], defaultEffort: 'medium' };
  }

  // OpenCode
  if (lower.includes('opencode')) {
    return { name: `OpenCode (${modelId})`, contextWindow: 128000, defaultTokenBudget: 4096, supportedEfforts: ['low', 'medium', 'high'], defaultEffort: 'high' };
  }

  // Default fallback
  const cleanName = modelId.split('/').pop() || modelId;
  return {
    name: cleanName,
    contextWindow: 128000,
    defaultTokenBudget: 4096,
    supportedEfforts: ['low', 'medium', 'high'],
    defaultEffort: 'medium',
  };
}

/** 区块三级标题组件 (Codex 样式) */
const SectionHeader: React.FC<{
  title: string;
  subtitle?: string;
  icon?: React.ComponentType<{ size?: number; color?: string; style?: React.CSSProperties }>;
  action?: React.ReactNode;
}> = ({ title, subtitle, icon: Icon, action }) => (
  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 12, gap: 12, flexWrap: 'wrap' }}>
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        {Icon && <Icon size={16} color="var(--accent-action)" />}
        <h3 style={{ fontSize: '15px', fontWeight: 600, color: 'var(--text-primary)', margin: 0 }}>
          {title}
        </h3>
      </div>
      {subtitle && (
        <p style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: 4, margin: 0 }}>
          {subtitle}
        </p>
      )}
    </div>
    {action && <div>{action}</div>}
  </div>
);

const DemoSettingsView: React.FC = () => {
  const { client, theme, toggleTheme, addNotification, connectionStatus } = useOperant();
  const { resetDemoState } = useDemo();
  const [searchParams, setSearchParams] = useSearchParams();

  // 读取与兼容老参数 tab (models, approvals, memory, retention, remote, general)
  const categoryParam = (searchParams.get('cat') as MainCategory) || 'general';

  const currentCategory: MainCategory = CATEGORIES.some((c) => c.key === categoryParam)
    ? categoryParam
    : 'general';
  const activeCategoryConfig = CATEGORIES.find((c) => c.key === currentCategory)!;

  const setCategory = (cat: MainCategory) => {
    setSearchParams({ cat }, { replace: true });
  };

  const [models, setModels] = useState<ModelProfile[]>([]);
  const [memories, setMemories] = useState<Memory[]>([]);
  const [memoryQuery, setMemoryQuery] = useState('');
  const [modelLatencies, setModelLatencies] = useState<Record<string, number | undefined>>({});

  // 偏好状态
  const [density, setDensity] = useState<'comfortable' | 'compact'>(() => {
    return (localStorage.getItem('operant.ui.density') as 'comfortable' | 'compact') || 'comfortable';
  });
  const [accentColor, setAccentColor] = useState('#059669');
  const [showLineNumbers, setShowLineNumbers] = useState(true);
  const [reduceMotion, setReduceMotion] = useState(false);
  const [streamingOutput, setStreamingOutput] = useState(true);
  const [reasoningEffort, setReasoningEffort] = useState<'low' | 'medium' | 'high'>('medium');
  const [compactionThreshold, setCompactionThreshold] = useState('80%');
  const [defaultShell, setDefaultShell] = useState('/bin/zsh');
  const [sandboxMode, setSandboxMode] = useState<'docker' | 'host' | 'remote'>('docker');
  const [proxyUrl, setProxyUrl] = useState('');
  const [timeoutSec, setTimeoutSec] = useState('30');

  // Hermes 提供商管理状态
  const [providerSearch, setProviderSearch] = useState('');
  const [expandedProviders, setExpandedProviders] = useState<Record<string, boolean>>({
    deepseek: true,
    siliconflow: true,
    fireworks: false,
    xai: false,
  });
  const [providerConfigs, setProviderConfigs] = useState<Record<string, { baseUrl: string; secretRef: string }>>(() => {
    const init: Record<string, { baseUrl: string; secretRef: string }> = {};
    CLOUD_PROVIDERS.forEach((p) => {
      init[p.id] = { baseUrl: p.officialBaseUrl, secretRef: p.defaultSecretRef };
    });
    return init;
  });

  // OAuth 账号状态
  const [oauthAccounts, setOauthAccounts] = useState<Record<string, OAuthAccountInfo>>(() => {
    const saved = localStorage.getItem('operant.oauth.accounts');
    if (saved) {
      try {
        return JSON.parse(saved);
      } catch {
        // fallback
      }
    }
    return {
      anthropic: {
        providerId: 'anthropic',
        providerName: 'Anthropic',
        accountName: 'Claude Pro (Developer)',
        email: 'developer@anthropic.com',
        tokenRef: 'OAUTH_TOKEN_ANTHROPIC',
        authorizedAt: '2026-08-30 15:20',
      },
    };
  });

  // 保存 OAuth 账号
  const saveOauthAccounts = (accounts: Record<string, OAuthAccountInfo>) => {
    setOauthAccounts(accounts);
    localStorage.setItem('operant.oauth.accounts', JSON.stringify(accounts));
  };

  // 已接入的 API 提供商状态
  const [connectedApis, setConnectedApis] = useState<ConnectedApiInfo[]>(() => {
    const saved = localStorage.getItem('operant.connected.apis');
    if (saved) {
      try {
        return JSON.parse(saved);
      } catch {
        // fallback
      }
    }
    return DEFAULT_CONNECTED_APIS;
  });

  // 保存已接入的 API
  const saveConnectedApis = (apis: ConnectedApiInfo[]) => {
    setConnectedApis(apis);
    localStorage.setItem('operant.connected.apis', JSON.stringify(apis));
  };

  // 移除 API 提供商
  const handleRemoveApiProvider = async (apiId: string, apiName: string, baseUrl: string) => {
    const nextApis = connectedApis.filter((a) => a.id !== apiId);
    saveConnectedApis(nextApis);
    const cleanUrl = baseUrl.replace(/\/v1\/?$/, '').toLowerCase();
    const matchedModels = models.filter((m) => m.base_url.toLowerCase().includes(cleanUrl));
    for (const m of matchedModels) {
      try {
        await client.deactivateModel(m.id);
      } catch {
        // ignore
      }
    }
    addNotification('warn', `已移除 API 提供商「${apiName}」及关联的 ${matchedModels.length} 个模型配置`);
    loadData();
  };

  // 查找某个 OAuth 账号关联的模型
  const getModelsForOAuthAccount = (account: OAuthAccountInfo) => {
    const oauthDef = OAUTH_PROVIDERS.find((p) => p.id === account.providerId);
    const targetBaseUrl = oauthDef?.officialBaseUrl.replace(/\/v1\/?$/, '').toLowerCase() || '';
    return models.filter(
      (m) =>
        m.secret_ref === account.tokenRef ||
        (targetBaseUrl && m.base_url.toLowerCase().includes(targetBaseUrl)) ||
        m.name.toLowerCase().includes(account.providerName.toLowerCase())
    );
  };

  // 查找某个已接入 API 关联的模型
  const getModelsForConnectedApi = (api: ConnectedApiInfo) => {
    const targetBaseUrl = api.baseUrl.replace(/\/v1\/?$/, '').toLowerCase();
    return models.filter(
      (m) =>
        m.secret_ref === api.secretRef ||
        (targetBaseUrl && m.base_url.toLowerCase().includes(targetBaseUrl)) ||
        m.name.toLowerCase().includes(api.name.toLowerCase())
    );
  };

  // OAuth 授权弹窗状态
  const [oauthModalOpen, setOauthModalOpen] = useState(false);
  const [currentOAuthTarget, setCurrentOAuthTarget] = useState<OAuthProviderDef | null>(null);
  const [oauthStep, setOauthStep] = useState<'prompt' | 'authorizing' | 'success'>('prompt');

  // 本地端点配置状态
  const [selectedLocalPreset, setSelectedLocalPreset] = useState('ollama');
  const [localBaseUrl, setLocalBaseUrl] = useState('http://localhost:11434/v1');
  const [localSecretRef, setLocalSecretRef] = useState('OLLAMA_API_KEY');

  // 扫描与导入 Modal 状态
  const [scanModalOpen, setScanModalOpen] = useState(false);
  const [scanningProviderName, setScanningProviderName] = useState('');
  const [scanningBaseUrl, setScanningBaseUrl] = useState('');
  const [scanningSecretRef, setScanningSecretRef] = useState('');
  const [isScanning, setIsScanning] = useState(false);
  const [discoveredModels, setDiscoveredModels] = useState<string[]>([]);
  const [selectedDiscoveredIds, setSelectedDiscoveredIds] = useState<Set<string>>(new Set());

  // 重置确认弹窗
  const [resetModalOpen, setResetModalOpen] = useState(false);

  // 手动添加自定义模型配置弹窗
  const [manualModelModalOpen, setManualModelModalOpen] = useState(false);
  const [manualModelName, setManualModelName] = useState('');
  const [manualModelId, setManualModelId] = useState('');
  const [manualModelBaseUrl, setManualModelBaseUrl] = useState('https://api.openai.com/v1');
  const [manualModelSecretRef, setManualModelSecretRef] = useState('OPENAI_API_KEY');
  const [manualModelContextWindow, setManualModelContextWindow] = useState('128000');

  const loadData = async () => {
    try {
      const [modelList, memList] = await Promise.all([
        client.listModels(),
        client.searchMemories('session_mock_alpha', memoryQuery || ' ', undefined, true).catch(() => []),
      ]);
      setModels(modelList);
      setMemories(memList);
    } catch (err: unknown) {
      console.error('Failed to load settings data:', err);
    }
  };

  useEffect(() => {
    loadData();
  }, [client]);

  // 触发 Provider 扫描
  const handleStartScan = async (providerName: string, baseUrl: string, secretRef: string) => {
    setScanningProviderName(providerName);
    setScanningBaseUrl(baseUrl);
    setScanningSecretRef(secretRef);
    setDiscoveredModels([]);
    setSelectedDiscoveredIds(new Set());
    setScanModalOpen(true);
    setIsScanning(true);

    try {
      const res = await client.discoverModels(baseUrl, secretRef);
      setDiscoveredModels(res.model_ids);
      // 默认勾选所有尚未添加的模型
      const existingIds = new Set(models.map((m) => m.model_id));
      const newSelected = new Set<string>();
      res.model_ids.forEach((id) => {
        if (!existingIds.has(id)) {
          newSelected.add(id);
        }
      });
      setSelectedDiscoveredIds(newSelected.size > 0 ? newSelected : new Set(res.model_ids));
      addNotification('info', `从 ${providerName} 成功扫描到 ${res.model_ids.length} 个上游模型`);
    } catch (err: unknown) {
      addNotification('error', `扫描上游模型失败：${err instanceof Error ? err.message : '未知错误'}`);
    } finally {
      setIsScanning(false);
    }
  };

  // 发起 OAuth 授权流程
  const handleOpenOAuthFlow = (providerDef: OAuthProviderDef) => {
    setCurrentOAuthTarget(providerDef);
    setOauthStep('prompt');
    setOauthModalOpen(true);
  };

  // 执行模拟 OAuth 授权
  const handleConfirmOAuth = async () => {
    if (!currentOAuthTarget) return;
    setOauthStep('authorizing');

    setTimeout(async () => {
      const newAccount: OAuthAccountInfo = {
        providerId: currentOAuthTarget.id,
        providerName: currentOAuthTarget.name,
        accountName: currentOAuthTarget.defaultUser,
        email: currentOAuthTarget.defaultEmail,
        tokenRef: `OAUTH_TOKEN_${currentOAuthTarget.id.toUpperCase()}`,
        authorizedAt: new Date().toLocaleDateString() + ' ' + new Date().toLocaleTimeString().slice(0, 5),
      };

      const updated = { ...oauthAccounts, [currentOAuthTarget.id]: newAccount };
      saveOauthAccounts(updated);
      setOauthStep('success');

      addNotification('success', `🎉 ${currentOAuthTarget.name} 账号授权成功！已保存 Access Token。`);

      setTimeout(() => {
        setOauthModalOpen(false);
        // 自动触发上游模型扫描
        handleStartScan(
          currentOAuthTarget.name,
          currentOAuthTarget.officialBaseUrl,
          newAccount.tokenRef
        );
      }, 700);
    }, 1200);
  };

  // 退出 OAuth 登录
  const handleOAuthLogout = (providerId: string, providerName: string) => {
    const updated = { ...oauthAccounts };
    delete updated[providerId];
    saveOauthAccounts(updated);
    addNotification('info', `已退出 ${providerName} 账号登录`);
  };

  // 批量导入选中的模型
  const handleBatchImport = async (targetIds: string[]) => {
    if (targetIds.length === 0) return;
    let importedCount = 0;

    for (const modelId of targetIds) {
      const meta = inferModelMetadata(modelId);
      try {
        await client.createModel({
          name: `${scanningProviderName} ${meta.name}`,
          provider: 'openai-compatible',
          model_id: modelId,
          base_url: scanningBaseUrl,
          secret_ref: scanningSecretRef,
          context_window: meta.contextWindow,
          default_token_budget: meta.defaultTokenBudget,
          supported_efforts: meta.supportedEfforts,
          default_effort: meta.defaultEffort,
          effort_parameter: 'reasoning_effort',
          effort_mapping: [
            { effort: 'low', provider_value: 'low' },
            { effort: 'medium', provider_value: 'medium' },
            { effort: 'high', provider_value: 'high' },
          ],
          enabled: true,
        });
        importedCount++;
      } catch (err: unknown) {
        console.error(`Failed to import model ${modelId}:`, err);
      }
    }

    // 若非 OAuth 凭据，确保记入层级 ② connectedApis
    if (!scanningSecretRef.startsWith('OAUTH_TOKEN')) {
      const existing = connectedApis.find(
        (a) =>
          a.baseUrl.replace(/\/v1\/?$/, '').toLowerCase() ===
            scanningBaseUrl.replace(/\/v1\/?$/, '').toLowerCase() ||
          a.name.toLowerCase() === scanningProviderName.toLowerCase()
      );
      if (!existing) {
        const newApi: ConnectedApiInfo = {
          id: scanningProviderName.toLowerCase().replace(/[^a-z0-9]/g, '_'),
          name: scanningProviderName,
          baseUrl: scanningBaseUrl,
          secretRef: scanningSecretRef,
          tag: '已接入',
          addedAt: new Date().toISOString().slice(0, 10),
        };
        saveConnectedApis([...connectedApis, newApi]);
      }
    }

    await loadData();
    setScanModalOpen(false);
    addNotification('success', `🎉 成功将 ${importedCount} 个模型配置导入为系统 ModelProfile！`);
  };

  // 手动创建自定义模型
  const handleManualCreateModel = async () => {
    if (!manualModelName.trim() || !manualModelId.trim() || !manualModelBaseUrl.trim()) return;
    try {
      const meta = inferModelMetadata(manualModelId);
      await client.createModel({
        name: manualModelName.trim(),
        provider: 'openai-compatible',
        model_id: manualModelId.trim(),
        base_url: manualModelBaseUrl.trim(),
        secret_ref: manualModelSecretRef.trim() || 'OPENAI_API_KEY',
        context_window: parseInt(manualModelContextWindow, 10) || meta.contextWindow,
        default_token_budget: meta.defaultTokenBudget,
        supported_efforts: meta.supportedEfforts,
        default_effort: meta.defaultEffort,
        effort_parameter: 'reasoning_effort',
        effort_mapping: [
          { effort: 'low', provider_value: 'low' },
          { effort: 'medium', provider_value: 'medium' },
          { effort: 'high', provider_value: 'high' },
        ],
        enabled: true,
      });
      setManualModelModalOpen(false);
      setManualModelName('');
      setManualModelId('');
      addNotification('success', '已保存新的模型配置。');
      loadData();
    } catch (err: unknown) {
      addNotification('error', `保存失败：${err instanceof Error ? err.message : '未知错误'}`);
    }
  };

  // 切换模型启停
  const handleToggleModelEnabled = async (model: ModelProfile) => {
    try {
      await client.updateModel(model.id, { enabled: !model.enabled });
      addNotification('info', `模型「${model.name}」已${!model.enabled ? '启用' : '停用'}`);
      loadData();
    } catch (err: unknown) {
      addNotification('error', '切换状态失败');
    }
  };

  // 检查模型健康 / 测速
  const handleCheckModelHealth = async (modelId: string) => {
    try {
      const res = await client.checkModelHealth(modelId);
      setModelLatencies((prev) => ({ ...prev, [modelId]: res.latency_ms || 42 }));
      addNotification('success', `模型连接健康，响应延迟：${res.latency_ms || 42}ms`);
    } catch (err: unknown) {
      addNotification('error', '健康检查失败');
    }
  };

  // 停用 / 移除模型
  const handleDeactivateModel = async (modelId: string) => {
    try {
      await client.deactivateModel(modelId);
      addNotification('warn', '已停用该模型配置');
      loadData();
    } catch (err: unknown) {
      addNotification('error', '操作失败');
    }
  };

  // 记忆治理操作
  const handleConfirmMemory = async (memId: string) => {
    try {
      await client.confirmMemory('session_mock_alpha', memId);
      addNotification('success', '候选记忆已确认为活跃长期知识。');
      loadData();
    } catch (err: unknown) {
      addNotification('error', '确认失败');
    }
  };

  const handleDeactivateMemory = async (memId: string) => {
    try {
      await client.deactivateMemory('session_mock_alpha', memId);
      addNotification('warn', '该记忆项已停用。');
      loadData();
    } catch (err: unknown) {
      addNotification('error', '停用失败');
    }
  };

  // 过滤提供商列表
  const filteredCloudProviders = useMemo(() => {
    if (!providerSearch.trim()) return CLOUD_PROVIDERS;
    const query = providerSearch.toLowerCase().trim();
    return CLOUD_PROVIDERS.filter((p) => {
      return (
        p.name.toLowerCase().includes(query) ||
        p.officialBaseUrl.toLowerCase().includes(query) ||
        p.description.toLowerCase().includes(query) ||
        p.tag.toLowerCase().includes(query) ||
        p.popularModels.some((m) => m.toLowerCase().includes(query))
      );
    });
  }, [providerSearch]);

  // 已配置模型按 BaseURL 映射已匹配数量
  const getProviderConfiguredCount = (baseUrl: string) => {
    const cleanUrl = baseUrl.replace(/\/v1\/?$/, '').toLowerCase();
    return models.filter((m) => m.base_url.toLowerCase().includes(cleanUrl)).length;
  };

  // 旧深链重定向
  const tabParamRaw = searchParams.get('tab');
  if (tabParamRaw === 'workflow') return <Navigate to="/collab" replace />;
  if (tabParamRaw === 'roles') return <Navigate to="/agents" replace />;
  if (tabParamRaw === 'remote') return <Navigate to="/settings?cat=system" replace />;
  if (tabParamRaw === 'models') return <Navigate to="/settings?cat=models" replace />;
  if (tabParamRaw === 'approvals') return <Navigate to="/settings?cat=security" replace />;
  if (tabParamRaw === 'memory') return <Navigate to="/settings?cat=security" replace />;
  if (tabParamRaw === 'retention') return <Navigate to="/settings?cat=security" replace />;
  if (tabParamRaw === 'general') return <Navigate to="/settings?cat=general" replace />;

  return (
    <div className="settings-layout">
      {/* 左侧大类导航 */}
      <nav className="settings-subnav" aria-label="设置中心主分类导航">
        <div className="settings-subnav-title">
          <Settings size={16} color="var(--accent-action)" />
          <h1>设置中心</h1>
        </div>
        {CATEGORIES.map((cat) => {
          const Icon = cat.icon;
          const isActive = currentCategory === cat.key;
          return (
            <button
              key={cat.key}
              onClick={() => setCategory(cat.key)}
              className={`settings-subnav-item${isActive ? ' active' : ''}`}
              aria-current={isActive ? 'page' : undefined}
            >
              <Icon size={15} />
              <span>{cat.label}</span>
            </button>
          );
        })}
      </nav>

      {/* 右侧内容区：Codex 式单流纵向平滑滚动 */}
      <div className="settings-content">
        <div className="settings-panel" style={{ padding: '24px 28px', maxWidth: 960 }}>
          {/* 页头大类标题 */}
          <div style={{ marginBottom: 12, paddingBottom: 16, borderBottom: '1px solid var(--border-subtle)' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              {React.createElement(activeCategoryConfig.icon, { size: 20, color: 'var(--accent-action)' })}
              <h2 style={{ fontSize: '18px', fontWeight: 700, color: 'var(--text-primary)', margin: 0 }}>
                {activeCategoryConfig.label}
              </h2>
            </div>
            <p style={{ fontSize: '13px', color: 'var(--text-secondary)', marginTop: 4, margin: 0 }}>
              {activeCategoryConfig.subtitle}
            </p>
          </div>

          {/* ========================================================================= */}
          {/* ============================== 1. 通用偏好 =============================== */}
          {/* ========================================================================= */}
          {currentCategory === 'general' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 32 }}>
              {/* 区块 1.1：### 外观与界面 */}
              <section aria-labelledby="sec-appearance">
                <SectionHeader
                  title="外观与界面"
                  subtitle="个性化定制主题色彩、明暗模式与界面信息展示密度"
                  icon={Palette}
                />
                <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                  {/* 外观主题 */}
                  <div className="card" style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                    <div style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)' }}>外观主题模式</div>
                    <div style={{ display: 'flex', gap: 10 }}>
                      <button
                        type="button"
                        className={`btn btn-sm ${theme === 'light' ? 'btn-primary' : 'btn-secondary'}`}
                        onClick={() => { if (theme !== 'light') toggleTheme(); }}
                      >
                        <Sun size={13} />
                        <span>浅色模式</span>
                      </button>
                      <button
                        type="button"
                        className={`btn btn-sm ${theme === 'dark' ? 'btn-primary' : 'btn-secondary'}`}
                        onClick={() => { if (theme !== 'dark') toggleTheme(); }}
                      >
                        <Moon size={13} />
                        <span>深色模式</span>
                      </button>
                    </div>
                  </div>

                  {/* 界面密度与强调色 */}
                  <div className="card" style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                    <div style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)' }}>界面信息密度与强调色</div>
                    <div>
                      <label style={{ fontSize: '12px', color: 'var(--text-secondary)', display: 'block', marginBottom: 6 }}>
                        字号与行距密度
                      </label>
                      <div style={{ display: 'flex', gap: 10 }}>
                        <button
                          type="button"
                          className={`btn btn-sm ${density === 'comfortable' ? 'btn-primary' : 'btn-secondary'}`}
                          onClick={() => {
                            setDensity('comfortable');
                            localStorage.setItem('operant.ui.density', 'comfortable');
                            addNotification('info', '已切换为舒适密度');
                          }}
                        >
                          舒适（标准）
                        </button>
                        <button
                          type="button"
                          className={`btn btn-sm ${density === 'compact' ? 'btn-primary' : 'btn-secondary'}`}
                          onClick={() => {
                            setDensity('compact');
                            localStorage.setItem('operant.ui.density', 'compact');
                            addNotification('info', '已切换为紧凑密度');
                          }}
                        >
                          紧凑（密集）
                        </button>
                      </div>
                    </div>

                    <div style={{ marginTop: 6 }}>
                      <label style={{ fontSize: '12px', color: 'var(--text-secondary)', display: 'block', marginBottom: 6 }}>
                        强调色彩方案
                      </label>
                      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                        {PRESET_ACCENT_COLORS.map((c) => (
                          <button
                            key={c.value}
                            type="button"
                            className="btn btn-sm btn-ghost"
                            style={{
                              display: 'flex',
                              alignItems: 'center',
                              gap: 6,
                              border: accentColor === c.value ? '2px solid var(--accent-action)' : '1px solid var(--border-subtle)',
                            }}
                            onClick={() => {
                              setAccentColor(c.value);
                              addNotification('success', `已应用强调色「${c.name}」`);
                            }}
                          >
                            <span style={{ width: 12, height: 12, borderRadius: '50%', backgroundColor: c.value }} />
                            <span>{c.name}</span>
                          </button>
                        ))}
                      </div>
                    </div>
                  </div>

                  {/* 代码与动效体验 */}
                  <div className="card" style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                    <div style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)' }}>代码与动效体验</div>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <div>
                        <div style={{ fontSize: '13px', fontWeight: 500, color: 'var(--text-primary)' }}>代码块显示行号</div>
                        <div style={{ fontSize: '12px', color: 'var(--text-muted)' }}>在 DiffViewer 与消息代码块中默认渲染左侧行号</div>
                      </div>
                      <button
                        type="button"
                        role="switch"
                        aria-checked={showLineNumbers}
                        className="switch"
                        onClick={() => setShowLineNumbers((v) => !v)}
                      />
                    </div>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', paddingTop: 10, borderTop: '1px solid var(--border-subtle)' }}>
                      <div>
                        <div style={{ fontSize: '13px', fontWeight: 500, color: 'var(--text-primary)' }}>减弱动画效果</div>
                        <div style={{ fontSize: '12px', color: 'var(--text-muted)' }}>遵循 prefers-reduced-motion，减少页面过渡与波浪动效</div>
                      </div>
                      <button
                        type="button"
                        role="switch"
                        aria-checked={reduceMotion}
                        className="switch"
                        onClick={() => setReduceMotion((v) => !v)}
                      />
                    </div>
                  </div>
                </div>
              </section>

              {/* 区块 1.2：### 快捷键速查与自定义 */}
              <section aria-labelledby="sec-shortcuts">
                <SectionHeader
                  title="快捷键速查与自定义"
                  subtitle="全键盘流操作指南与自定义绑定"
                  icon={Keyboard}
                  action={
                    <button className="btn btn-ghost btn-sm" onClick={() => addNotification('info', '快捷键已恢复默认绑定')}>
                      <RotateCcw size={13} />
                      <span>恢复默认</span>
                    </button>
                  }
                />
                <div className="card" style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 12 }}>
                    <div style={{ padding: 12, background: 'var(--bg-surface)', borderRadius: 'var(--radius-sm)', border: '1px solid var(--border-subtle)' }}>
                      <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-primary)', marginBottom: 8, display: 'flex', alignItems: 'center', gap: 6 }}>
                        <Globe size={13} color="var(--accent-action)" />
                        <span>全局与导航</span>
                      </div>
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, fontSize: '12px', color: 'var(--text-secondary)' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                          <span>搜索与命令跳转</span>
                          <kbd style={{ background: 'var(--bg-card)', padding: '2px 6px', borderRadius: 3, border: '1px solid var(--border-subtle)' }}>⌘/Ctrl + K</kbd>
                        </div>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                          <span>关闭弹层 / 抽屉</span>
                          <kbd style={{ background: 'var(--bg-card)', padding: '2px 6px', borderRadius: 3, border: '1px solid var(--border-subtle)' }}>Esc</kbd>
                        </div>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                          <span>焦点顺次移动</span>
                          <kbd style={{ background: 'var(--bg-card)', padding: '2px 6px', borderRadius: 3, border: '1px solid var(--border-subtle)' }}>Tab / Shift+Tab</kbd>
                        </div>
                      </div>
                    </div>

                    <div style={{ padding: 12, background: 'var(--bg-surface)', borderRadius: 'var(--radius-sm)', border: '1px solid var(--border-subtle)' }}>
                      <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-primary)', marginBottom: 8, display: 'flex', alignItems: 'center', gap: 6 }}>
                        <MessageSquare size={13} color="var(--accent-action)" />
                        <span>输入框 (Composer)</span>
                      </div>
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, fontSize: '12px', color: 'var(--text-secondary)' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                          <span>唤起斜杠命令面板</span>
                          <kbd style={{ background: 'var(--bg-card)', padding: '2px 6px', borderRadius: 3, border: '1px solid var(--border-subtle)' }}>/</kbd>
                        </div>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                          <span>关联多维上下文 (@)</span>
                          <kbd style={{ background: 'var(--bg-card)', padding: '2px 6px', borderRadius: 3, border: '1px solid var(--border-subtle)' }}>@</kbd>
                        </div>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                          <span>发送消息 / 换行</span>
                          <kbd style={{ background: 'var(--bg-card)', padding: '2px 6px', borderRadius: 3, border: '1px solid var(--border-subtle)' }}>Enter / Shift+Enter</kbd>
                        </div>
                      </div>
                    </div>

                    <div style={{ padding: 12, background: 'var(--bg-surface)', borderRadius: 'var(--radius-sm)', border: '1px solid var(--border-subtle)' }}>
                      <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-primary)', marginBottom: 8, display: 'flex', alignItems: 'center', gap: 6 }}>
                        <Layers size={13} color="var(--accent-action)" />
                        <span>协作画布 (Graph)</span>
                      </div>
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, fontSize: '12px', color: 'var(--text-secondary)' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                          <span>级联删除选中节点或边</span>
                          <kbd style={{ background: 'var(--bg-card)', padding: '2px 6px', borderRadius: 3, border: '1px solid var(--border-subtle)' }}>Delete / Backspace</kbd>
                        </div>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                          <span>画布拖动平移</span>
                          <kbd style={{ background: 'var(--bg-card)', padding: '2px 6px', borderRadius: 3, border: '1px solid var(--border-subtle)' }}>Space + 拖动</kbd>
                        </div>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                          <span>节点间键盘连线</span>
                          <kbd style={{ background: 'var(--bg-card)', padding: '2px 6px', borderRadius: 3, border: '1px solid var(--border-subtle)' }}>Enter (选中端口)</kbd>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              </section>

              {/* 区块 1.3：### 对话与交互偏好 */}
              <section aria-labelledby="sec-chat-pref">
                <SectionHeader
                  title="对话与交互偏好"
                  subtitle="控制模型响应的流式渲染、思考深度与上下文压缩行为"
                  icon={MessageSquare}
                />
                <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                  <div className="card" style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                    <div style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)' }}>对话生成与推理偏好</div>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <div>
                        <div style={{ fontSize: '13px', fontWeight: 500, color: 'var(--text-primary)' }}>流式输出响应 (Streaming)</div>
                        <div style={{ fontSize: '12px', color: 'var(--text-muted)' }}>实时通过 SSE 流渲染模型生成的文字与 Token 块</div>
                      </div>
                      <button
                        type="button"
                        role="switch"
                        aria-checked={streamingOutput}
                        className="switch"
                        onClick={() => setStreamingOutput((v) => !v)}
                      />
                    </div>

                    <div style={{ paddingTop: 10, borderTop: '1px solid var(--border-subtle)' }}>
                      <label style={{ fontSize: '12px', color: 'var(--text-secondary)', display: 'block', marginBottom: 6 }}>
                        默认推理努力等级 (Reasoning Effort)
                      </label>
                      <div style={{ display: 'flex', gap: 10 }}>
                        {(['low', 'medium', 'high'] as const).map((eff) => (
                          <button
                            key={eff}
                            type="button"
                            className={`btn btn-sm ${reasoningEffort === eff ? 'btn-primary' : 'btn-secondary'}`}
                            onClick={() => setReasoningEffort(eff)}
                          >
                            <span>{eff.toUpperCase()}</span>
                          </button>
                        ))}
                      </div>
                    </div>
                  </div>

                  <div className="card" style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                    <div style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)' }}>上下文压缩与预算阈值</div>
                    <div>
                      <label style={{ fontSize: '12px', color: 'var(--text-secondary)', display: 'block', marginBottom: 6 }}>
                        自动 Compaction 触发阈值（占模型上下文窗口比例）
                      </label>
                      <select
                        className="select"
                        value={compactionThreshold}
                        onChange={(e) => setCompactionThreshold(e.target.value)}
                        style={{ maxWidth: 260 }}
                      >
                        <option value="70%">70% (保守，频繁压缩)</option>
                        <option value="80%">80% (标准推荐)</option>
                        <option value="90%">90% (激进，最大限度保留原文)</option>
                      </select>
                    </div>
                  </div>
                </div>
              </section>
            </div>
          )}

          {/* ========================================================================= */}
          {/* ======================= 2. 模型提供商 5 层瀑布流架构 ====================== */}
          {/* ========================================================================= */}
          {currentCategory === 'models' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 32 }}>
              {/* ------------------------------------------------------------------------- */}
              {/* 层级 ①：已连接的账号 (Connected Accounts - OAuth) */}
              {/* ------------------------------------------------------------------------- */}
              <section aria-labelledby="sec-connected-accounts">
                <SectionHeader
                  title="① 已连接的账号 (Connected Accounts)"
                  subtitle="展示已通过 OAuth 2.0 PKCE 授权登录的厂商开发者账号与旗下已激活的模型配置"
                  icon={UserCheck}
                />

                {Object.keys(oauthAccounts).length === 0 ? (
                  <div className="card" style={{ padding: 24, textAlign: 'center', color: 'var(--text-secondary)' }}>
                    <UserCheck size={28} style={{ margin: '0 auto 8px', opacity: 0.5 }} />
                    <div style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)' }}>
                      暂无已连接的 OAuth 账号
                    </div>
                    <div style={{ fontSize: '12px', color: 'var(--text-muted)', marginTop: 4 }}>
                      可在下方【③ 连接账号 (Connect Account - OAuth)】中快速授权登录各大厂商开发者账号。
                    </div>
                  </div>
                ) : (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                    {Object.values(oauthAccounts).map((account) => {
                      const accountModels = getModelsForOAuthAccount(account);
                      const oauthDef = OAUTH_PROVIDERS.find((p) => p.id === account.providerId);
                      const baseUrl = oauthDef?.officialBaseUrl || 'https://api.openai.com/v1';

                      return (
                        <div
                          key={account.providerId}
                          className="card"
                          style={{
                            background: 'linear-gradient(135deg, var(--bg-card) 0%, var(--accent-subtle) 100%)',
                            border: '1.5px solid var(--accent-action)',
                            display: 'flex',
                            flexDirection: 'column',
                            gap: 12,
                          }}
                        >
                          {/* 账号头 */}
                          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: 10 }}>
                            <div>
                              <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                                <span style={{ fontSize: '15px', fontWeight: 700, color: 'var(--text-primary)' }}>
                                  {account.providerName}
                                </span>
                                <span className="badge" style={{ backgroundColor: 'var(--accent-subtle)', color: 'var(--accent-on-subtle)', fontSize: '11px', display: 'flex', alignItems: 'center', gap: 4 }}>
                                  <UserCheck size={12} />
                                  <span>已授权</span>
                                </span>
                                <span className="badge" style={{ backgroundColor: 'var(--bg-surface)', fontSize: '11px', fontFamily: 'var(--font-mono)' }}>
                                  {account.tokenRef} (有效)
                                </span>
                              </div>
                              <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: 4 }}>
                                登录账号: <strong style={{ color: 'var(--text-primary)' }}>{account.email || account.accountName}</strong> · 授权于 {account.authorizedAt}
                              </div>
                            </div>

                            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                              <button
                                type="button"
                                className="btn btn-primary btn-sm"
                                onClick={() => handleStartScan(account.providerName, baseUrl, account.tokenRef)}
                              >
                                <Zap size={12} />
                                <span>🚀 扫描并拉取模型</span>
                              </button>
                              <button
                                type="button"
                                className="btn btn-secondary btn-sm"
                                onClick={() => {
                                  const matched = OAUTH_PROVIDERS.find((p) => p.id === account.providerId) || {
                                    id: account.providerId,
                                    name: account.providerName,
                                    desc: '',
                                    officialBaseUrl: baseUrl,
                                    defaultSecretRef: account.tokenRef,
                                    badge: 'OAuth',
                                    scopes: ['read:user'],
                                    defaultUser: account.accountName,
                                    defaultEmail: account.email,
                                  };
                                  handleOpenOAuthFlow(matched);
                                }}
                              >
                                <RotateCcw size={12} />
                                <span>重新授权</span>
                              </button>
                              <button
                                type="button"
                                className="btn btn-ghost btn-sm"
                                onClick={() => handleOAuthLogout(account.providerId, account.providerName)}
                                title="断开连接并退出登录"
                              >
                                <LogOut size={12} color="var(--text-muted)" />
                                <span>断开连接 / 退出登录</span>
                              </button>
                            </div>
                          </div>

                          {/* 该账号下的已激活模型列表 */}
                          <div style={{ borderTop: '1px solid var(--border-subtle)', paddingTop: 10 }}>
                            <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-primary)', marginBottom: 8, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                              <span>已激活模型 ({accountModels.length})</span>
                              {accountModels.length > 0 && (
                                <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
                                  包含已启用与已停用模型，可单独测速或删除
                                </span>
                              )}
                            </div>

                            {accountModels.length === 0 ? (
                              <div style={{ padding: '12px 14px', background: 'var(--bg-surface)', borderRadius: 'var(--radius-sm)', fontSize: '12px', color: 'var(--text-muted)', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                                <span>暂无已激活的模型</span>
                                <button
                                  type="button"
                                  className="btn btn-secondary btn-sm"
                                  style={{ fontSize: '11px' }}
                                  onClick={() => handleStartScan(account.providerName, baseUrl, account.tokenRef)}
                                >
                                  <Zap size={11} />
                                  <span>立即拉取可用模型</span>
                                </button>
                              </div>
                            ) : (
                              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                                {accountModels.map((m) => (
                                  <div
                                    key={m.id}
                                    style={{
                                      display: 'flex',
                                      justifyContent: 'space-between',
                                      alignItems: 'center',
                                      padding: '8px 12px',
                                      background: 'var(--bg-surface)',
                                      border: '1px solid var(--border-subtle)',
                                      borderRadius: 'var(--radius-sm)',
                                      gap: 10,
                                      flexWrap: 'wrap',
                                    }}
                                  >
                                    <div style={{ minWidth: 0, flex: 1 }}>
                                      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                                        <span style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)' }}>
                                          {m.name}
                                        </span>
                                        <span style={{ fontFamily: 'var(--font-mono)', fontSize: '11px', color: 'var(--text-muted)' }}>
                                          {m.model_id}
                                        </span>
                                        <StatusBadge status={m.enabled ? 'active' : 'inactive'} size="sm" />
                                      </div>
                                      <div style={{ fontSize: '11px', color: 'var(--text-secondary)', marginTop: 2, display: 'flex', gap: 12 }}>
                                        <span>上下文: {m.context_window ? `${formatNumber(m.context_window)} tokens` : '128k'}</span>
                                        {modelLatencies[m.id] !== undefined && (
                                          <span style={{ color: 'var(--accent-action)', fontWeight: 600 }}>
                                            ⚡ {modelLatencies[m.id]}ms
                                          </span>
                                        )}
                                      </div>
                                    </div>

                                    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                                      <button
                                        type="button"
                                        className="btn btn-ghost btn-sm"
                                        style={{ fontSize: '11px', padding: '2px 6px' }}
                                        onClick={() => handleCheckModelHealth(m.id)}
                                        title="测试延迟"
                                      >
                                        <Activity size={11} />
                                        <span>测速</span>
                                      </button>
                                      <button
                                        type="button"
                                        className={`btn btn-sm ${m.enabled ? 'btn-ghost' : 'btn-primary'}`}
                                        style={{ fontSize: '11px', padding: '2px 8px' }}
                                        onClick={() => handleToggleModelEnabled(m)}
                                      >
                                        <Power size={11} />
                                        <span>{m.enabled ? '停用' : '启用'}</span>
                                      </button>
                                      <button
                                        type="button"
                                        className="btn btn-ghost btn-icon"
                                        style={{ padding: '2px 6px' }}
                                        title="删除配置"
                                        onClick={() => handleDeactivateModel(m.id)}
                                      >
                                        <Trash2 size={12} color="var(--text-muted)" />
                                      </button>
                                    </div>
                                  </div>
                                ))}
                              </div>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
              </section>

              {/* ------------------------------------------------------------------------- */}
              {/* 层级 ②：已接入的 API (Connected APIs) */}
              {/* ------------------------------------------------------------------------- */}
              <section aria-labelledby="sec-connected-apis">
                <SectionHeader
                  title="② 已接入的 API (Connected APIs)"
                  subtitle="展示所有已配置 API Key 的提供商卡片及旗下已激活模型"
                  icon={Server}
                />

                {connectedApis.length === 0 ? (
                  <div className="card" style={{ padding: 24, textAlign: 'center', color: 'var(--text-secondary)' }}>
                    <Server size={28} style={{ margin: '0 auto 8px', opacity: 0.5 }} />
                    <div style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)' }}>
                      暂无已接入的 API 提供商
                    </div>
                    <div style={{ fontSize: '12px', color: 'var(--text-muted)', marginTop: 4 }}>
                      可在下方【④ 以 API 接入】或【⑤ 自定义提供商 / 本地端点】中配置接入。
                    </div>
                  </div>
                ) : (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                    {connectedApis.map((api) => {
                      const apiModels = getModelsForConnectedApi(api);
                      return (
                        <div
                          key={api.id}
                          className="card"
                          style={{
                            display: 'flex',
                            flexDirection: 'column',
                            gap: 12,
                            border: '1px solid var(--border-strong)',
                          }}
                        >
                          {/* API 头 */}
                          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: 10 }}>
                            <div>
                              <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                                <span style={{ fontSize: '15px', fontWeight: 700, color: 'var(--text-primary)' }}>
                                  {api.name}
                                </span>
                                {api.tag && (
                                  <span className="badge" style={{ backgroundColor: 'var(--bg-subtle)', fontSize: '11px' }}>
                                    {api.tag}
                                  </span>
                                )}
                                <span className="badge" style={{ backgroundColor: 'var(--accent-subtle)', color: 'var(--accent-on-subtle)', fontSize: '11px' }}>
                                  ✓ API 已接入
                                </span>
                              </div>
                              <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: 4 }}>
                                Base URL: <code style={{ fontFamily: 'var(--font-mono)' }}>{api.baseUrl}</code> · 环境变量: <code style={{ fontFamily: 'var(--font-mono)' }}>{api.secretRef}</code>
                              </div>
                            </div>

                            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                              <button
                                type="button"
                                className="btn btn-primary btn-sm"
                                onClick={() => handleStartScan(api.name, api.baseUrl, api.secretRef)}
                              >
                                <Zap size={12} />
                                <span>🚀 重新扫描上游模型</span>
                              </button>
                              <button
                                type="button"
                                className="btn btn-ghost btn-sm"
                                onClick={() => handleRemoveApiProvider(api.id, api.name, api.baseUrl)}
                                title="移除该 API 提供商配置"
                              >
                                <Trash2 size={12} color="var(--text-muted)" />
                                <span>移除 API 提供商</span>
                              </button>
                            </div>
                          </div>

                          {/* 旗下已激活模型 */}
                          <div style={{ borderTop: '1px solid var(--border-subtle)', paddingTop: 10 }}>
                            <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-primary)', marginBottom: 8, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                              <span>已激活模型 ({apiModels.length})</span>
                            </div>

                            {apiModels.length === 0 ? (
                              <div style={{ padding: '12px 14px', background: 'var(--bg-surface)', borderRadius: 'var(--radius-sm)', fontSize: '12px', color: 'var(--text-muted)', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                                <span>暂无已导入的模型配置</span>
                                <button
                                  type="button"
                                  className="btn btn-secondary btn-sm"
                                  style={{ fontSize: '11px' }}
                                  onClick={() => handleStartScan(api.name, api.baseUrl, api.secretRef)}
                                >
                                  <Zap size={11} />
                                  <span>立即扫描导入</span>
                                </button>
                              </div>
                            ) : (
                              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                                {apiModels.map((m) => (
                                  <div
                                    key={m.id}
                                    style={{
                                      display: 'flex',
                                      justifyContent: 'space-between',
                                      alignItems: 'center',
                                      padding: '8px 12px',
                                      background: 'var(--bg-surface)',
                                      border: '1px solid var(--border-subtle)',
                                      borderRadius: 'var(--radius-sm)',
                                      gap: 10,
                                      flexWrap: 'wrap',
                                    }}
                                  >
                                    <div style={{ minWidth: 0, flex: 1 }}>
                                      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                                        <span style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)' }}>
                                          {m.name}
                                        </span>
                                        <span style={{ fontFamily: 'var(--font-mono)', fontSize: '11px', color: 'var(--text-muted)' }}>
                                          {m.model_id}
                                        </span>
                                        <StatusBadge status={m.enabled ? 'active' : 'inactive'} size="sm" />
                                      </div>
                                      <div style={{ fontSize: '11px', color: 'var(--text-secondary)', marginTop: 2, display: 'flex', gap: 12 }}>
                                        <span>上下文: {m.context_window ? `${formatNumber(m.context_window)} tokens` : '128k'}</span>
                                        {modelLatencies[m.id] !== undefined && (
                                          <span style={{ color: 'var(--accent-action)', fontWeight: 600 }}>
                                            ⚡ {modelLatencies[m.id]}ms
                                          </span>
                                        )}
                                      </div>
                                    </div>

                                    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                                      <button
                                        type="button"
                                        className="btn btn-ghost btn-sm"
                                        style={{ fontSize: '11px', padding: '2px 6px' }}
                                        onClick={() => handleCheckModelHealth(m.id)}
                                        title="测试延迟"
                                      >
                                        <Activity size={11} />
                                        <span>测速</span>
                                      </button>
                                      <button
                                        type="button"
                                        className={`btn btn-sm ${m.enabled ? 'btn-ghost' : 'btn-primary'}`}
                                        style={{ fontSize: '11px', padding: '2px 8px' }}
                                        onClick={() => handleToggleModelEnabled(m)}
                                      >
                                        <Power size={11} />
                                        <span>{m.enabled ? '停用' : '启用'}</span>
                                      </button>
                                      <button
                                        type="button"
                                        className="btn btn-ghost btn-icon"
                                        style={{ padding: '2px 6px' }}
                                        title="删除配置"
                                        onClick={() => handleDeactivateModel(m.id)}
                                      >
                                        <Trash2 size={12} color="var(--text-muted)" />
                                      </button>
                                    </div>
                                  </div>
                                ))}
                              </div>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
              </section>

              {/* ------------------------------------------------------------------------- */}
              {/* 层级 ③：连接账号 (Connect Account - OAuth) */}
              {/* ------------------------------------------------------------------------- */}
              <section aria-labelledby="sec-oauth-connect">
                <SectionHeader
                  title="③ 连接账号 (Connect Account - OAuth)"
                  subtitle="主流 AI 厂商账号浏览器 PKCE 快速授权，成功后自动挂载到层级 ① 并一键拉取可用模型"
                  icon={KeyRound}
                />
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 12 }}>
                  {OAUTH_PROVIDERS.map((op) => {
                    const account = oauthAccounts[op.id];
                    const isAuthed = !!account;

                    return (
                      <div
                        key={op.id}
                        className="card"
                        style={{
                          background: isAuthed
                            ? 'linear-gradient(135deg, var(--bg-card) 0%, var(--accent-subtle) 100%)'
                            : 'var(--bg-card)',
                          borderColor: isAuthed ? 'var(--accent-action)' : 'var(--border-subtle)',
                          display: 'flex',
                          flexDirection: 'column',
                          justifyContent: 'space-between',
                          gap: 12,
                        }}
                      >
                        <div>
                          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
                            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                              <span style={{ fontSize: '14px', fontWeight: 600, color: 'var(--text-primary)' }}>
                                {op.name}
                              </span>
                              <span className="badge" style={{ backgroundColor: 'var(--bg-subtle)', fontSize: '10px' }}>
                                {op.badge}
                              </span>
                            </div>
                            {isAuthed ? (
                              <span className="badge" style={{ backgroundColor: 'var(--accent-subtle)', color: 'var(--accent-on-subtle)', fontSize: '11px', display: 'flex', alignItems: 'center', gap: 4 }}>
                                <UserCheck size={12} />
                                <span>已授权</span>
                              </span>
                            ) : (
                              <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>支持 OAuth 直连</span>
                            )}
                          </div>
                          <p style={{ fontSize: '12px', color: 'var(--text-secondary)', margin: 0, lineHeight: 1.4 }}>
                            {op.desc}
                          </p>

                          {isAuthed && (
                            <div style={{ marginTop: 8, padding: '6px 8px', background: 'var(--bg-surface)', borderRadius: 'var(--radius-sm)', fontSize: '11px', color: 'var(--text-secondary)' }}>
                              <div>账号：<span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{account.email}</span></div>
                            </div>
                          )}
                        </div>

                        <div style={{ display: 'flex', gap: 8, alignItems: 'center', paddingTop: 8, borderTop: '1px solid var(--border-subtle)' }}>
                          <button
                            type="button"
                            className={`btn btn-sm ${isAuthed ? 'btn-secondary' : 'btn-primary'}`}
                            style={{ width: '100%' }}
                            onClick={() => handleOpenOAuthFlow(op)}
                          >
                            <LinkIcon size={12} />
                            <span>{isAuthed ? '✓ 已连接 (点击重新授权)' : '🔗 账号授权登录'}</span>
                          </button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </section>

              {/* ------------------------------------------------------------------------- */}
              {/* 层级 ④：以 API 接入 (Connect with API) */}
              {/* ------------------------------------------------------------------------- */}
              <section aria-labelledby="sec-cloud-api-connect">
                <SectionHeader
                  title="④ 以 API 接入 (Connect with API)"
                  subtitle="热门主流云端大模型服务商：展开配置 Base URL 与 API Key (Secret Ref)，扫描后录入层级 ②"
                  icon={Sparkles}
                />

                {/* 搜索框与提供商计数 */}
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, marginBottom: 14, flexWrap: 'wrap' }}>
                  <div style={{ position: 'relative', flex: 1, minWidth: 260 }}>
                    <Search size={14} style={{ position: 'absolute', left: 10, top: '50%', transform: 'translateY(-50%)', color: 'var(--text-muted)' }} />
                    <input
                      type="text"
                      className="input"
                      style={{ paddingLeft: 32 }}
                      placeholder="搜索提供方（DeepSeek、SiliconFlow、Fireworks AI、xAI...）"
                      value={providerSearch}
                      onChange={(e) => setProviderSearch(e.target.value)}
                    />
                  </div>
                  <span style={{ fontSize: '12px', color: 'var(--text-muted)' }}>
                    共 {CLOUD_PROVIDERS.length} 个服务商（匹配 {filteredCloudProviders.length} 个）
                  </span>
                </div>

                {/* 云端 Provider 列表 */}
                <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                  {filteredCloudProviders.map((provider) => {
                    const isExpanded = !!expandedProviders[provider.id];
                    const config = providerConfigs[provider.id] || { baseUrl: provider.officialBaseUrl, secretRef: provider.defaultSecretRef };
                    const configuredCount = getProviderConfiguredCount(config.baseUrl);

                    return (
                      <div
                        key={provider.id}
                        className="card"
                        style={{
                          display: 'flex',
                          flexDirection: 'column',
                          gap: 12,
                          borderColor: configuredCount > 0 ? 'var(--border-strong)' : 'var(--border-subtle)',
                        }}
                      >
                        {/* 头部摘要行 */}
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 10 }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 }}>
                            <button
                              type="button"
                              className="btn btn-ghost btn-sm"
                              style={{ padding: 4 }}
                              onClick={() => setExpandedProviders((prev) => ({ ...prev, [provider.id]: !prev[provider.id] }))}
                              aria-label={isExpanded ? '收起配置' : '展开配置'}
                            >
                              {isExpanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
                            </button>

                            <div>
                              <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                                <span style={{ fontSize: '14px', fontWeight: 600, color: 'var(--text-primary)' }}>
                                  {provider.name}
                                </span>
                                <span className="badge" style={{ backgroundColor: 'var(--bg-subtle)', fontSize: '11px' }}>
                                  {provider.tag}
                                </span>
                                {configuredCount > 0 && (
                                  <span className="badge" style={{ backgroundColor: 'var(--accent-subtle)', color: 'var(--accent-on-subtle)', fontSize: '11px' }}>
                                    ✓ 已接入 {configuredCount} 个模型
                                  </span>
                                )}
                              </div>
                              <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: 2 }}>
                                {provider.description}
                              </div>
                            </div>
                          </div>

                          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                            <button
                              type="button"
                              className="btn btn-primary btn-sm"
                              onClick={() => handleStartScan(
                                provider.name,
                                config.baseUrl,
                                config.secretRef
                              )}
                            >
                              <Zap size={13} />
                              <span>🚀 扫描并接入</span>
                            </button>
                          </div>
                        </div>

                        {/* 展开的配置输入 */}
                        {isExpanded && (
                          <div style={{ paddingTop: 12, borderTop: '1px solid var(--border-subtle)', display: 'flex', flexDirection: 'column', gap: 12 }}>
                            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 10 }}>
                              <div>
                                <label style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: 4 }}>
                                  Base URL
                                </label>
                                <input
                                  type="text"
                                  className="input"
                                  value={config.baseUrl}
                                  onChange={(e) => {
                                    const val = e.target.value;
                                    setProviderConfigs((prev) => ({
                                      ...prev,
                                      [provider.id]: { ...prev[provider.id], baseUrl: val },
                                    }));
                                  }}
                                />
                              </div>
                              <div>
                                <label style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: 4 }}>
                                  API Key 凭据环境变量名 (Secret Ref)
                                </label>
                                <input
                                  type="text"
                                  className="input"
                                  value={config.secretRef}
                                  onChange={(e) => {
                                    const val = e.target.value;
                                    setProviderConfigs((prev) => ({
                                      ...prev,
                                      [provider.id]: { ...prev[provider.id], secretRef: val },
                                    }));
                                  }}
                                />
                              </div>
                            </div>

                            <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap', marginTop: 2 }}>
                              <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>常见模型：</span>
                              {provider.popularModels.map((pm) => (
                                <span
                                  key={pm}
                                  style={{
                                    fontSize: '11px',
                                    fontFamily: 'var(--font-mono)',
                                    padding: '1px 6px',
                                    background: 'var(--bg-surface)',
                                    borderRadius: 'var(--radius-sm)',
                                    border: '1px solid var(--border-subtle)',
                                    color: 'var(--text-secondary)',
                                  }}
                                >
                                  {pm}
                                </span>
                              ))}
                            </div>
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </section>

              {/* ------------------------------------------------------------------------- */}
              {/* 层级 ⑤：自定义提供商 / 本地端点 (Custom API Provider & Local) */}
              {/* ------------------------------------------------------------------------- */}
              <section aria-labelledby="sec-local-custom-connect">
                <SectionHeader
                  title="⑤ 自定义提供商 / 本地端点 (Custom API Provider & Local)"
                  subtitle="连接本地运行的 Ollama、vLLM、LM Studio、Llama.cpp 或自定义 Base URL 端点，扫描后接入层级 ②"
                  icon={HardDrive}
                  action={
                    <button
                      onClick={() => setManualModelModalOpen(true)}
                      className="btn btn-secondary btn-sm"
                    >
                      <Plus size={13} />
                      <span>手动添加配置</span>
                    </button>
                  }
                />

                <div
                  className="card"
                  style={{
                    background: 'linear-gradient(135deg, var(--bg-card) 0%, var(--bg-surface) 100%)',
                    border: '1.5px solid var(--accent-subtle)',
                  }}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: 8, marginBottom: 12 }}>
                    <div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        <Server size={16} color="var(--accent-action)" />
                        <h4 style={{ fontSize: '14px', fontWeight: 600, color: 'var(--text-primary)', margin: 0 }}>
                          本地引擎与自定义端点
                        </h4>
                        <span className="badge" style={{ backgroundColor: 'var(--accent-subtle)', color: 'var(--accent-on-subtle)', fontSize: '11px' }}>
                          本地托管 / 内网
                        </span>
                      </div>
                      <p style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: 4, margin: 0 }}>
                        选择预设本地端点或输入自定义 Base URL，一键扫描上游模型并录入层级 ②。
                      </p>
                    </div>

                    <button
                      type="button"
                      className="btn btn-primary btn-sm"
                      style={{ flexShrink: 0 }}
                      onClick={() => handleStartScan(
                        LOCAL_ENDPOINTS.find((p) => p.id === selectedLocalPreset)?.name || '本地端点',
                        localBaseUrl,
                        localSecretRef
                      )}
                    >
                      <Zap size={13} />
                      <span>🚀 扫描本地端点模型并接入</span>
                    </button>
                  </div>

                  {/* 快捷预设按钮组 */}
                  <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 14 }}>
                    {LOCAL_ENDPOINTS.map((lp) => (
                      <button
                        key={lp.id}
                        type="button"
                        className={`btn btn-sm ${selectedLocalPreset === lp.id ? 'btn-primary' : 'btn-ghost'}`}
                        style={{
                          borderRadius: 'var(--radius-sm)',
                          fontSize: '12px',
                          border: selectedLocalPreset === lp.id ? 'none' : '1px solid var(--border-subtle)',
                        }}
                        onClick={() => {
                          setSelectedLocalPreset(lp.id);
                          setLocalBaseUrl(lp.baseUrl);
                          setLocalSecretRef(lp.secretRef);
                        }}
                      >
                        <span>{lp.name}</span>
                      </button>
                    ))}
                  </div>

                  {/* 端点配置输入 */}
                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 12 }}>
                    <div>
                      <label style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: 4 }}>
                        Base URL (OpenAI 兼容)
                      </label>
                      <input
                        type="text"
                        className="input"
                        value={localBaseUrl}
                        onChange={(e) => setLocalBaseUrl(e.target.value)}
                        placeholder="http://localhost:11434/v1"
                      />
                    </div>
                    <div>
                      <label style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: 4 }}>
                        凭据引用 / API Key (本地服务可选)
                      </label>
                      <input
                        type="text"
                        className="input"
                        value={localSecretRef}
                        onChange={(e) => setLocalSecretRef(e.target.value)}
                        placeholder="OLLAMA_API_KEY (或填任意)"
                      />
                    </div>
                  </div>
                </div>
              </section>

              {/* 区块 2.4：### 终端与执行环境 */}
              <section aria-labelledby="sec-env">
                <SectionHeader
                  title="终端与执行环境"
                  subtitle="配置代码执行 Shell 环境与 Docker 安全沙箱运行模式"
                  icon={Terminal}
                />
                <div className="card" style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                  <div>
                    <label style={{ fontSize: '12px', color: 'var(--text-secondary)', display: 'block', marginBottom: 6 }}>
                      默认 Shell 终端程序
                    </label>
                    <select
                      className="select"
                      value={defaultShell}
                      onChange={(e) => setDefaultShell(e.target.value)}
                      style={{ maxWidth: 260 }}
                    >
                      <option value="/bin/zsh">/bin/zsh (macOS 默认)</option>
                      <option value="/bin/bash">/bin/bash</option>
                      <option value="/bin/sh">/bin/sh</option>
                    </select>
                  </div>

                  <div style={{ marginTop: 6, paddingTop: 12, borderTop: '1px solid var(--border-subtle)' }}>
                    <label style={{ fontSize: '12px', color: 'var(--text-secondary)', display: 'block', marginBottom: 6 }}>
                      代码与测试执行沙箱模式
                    </label>
                    <div style={{ display: 'flex', gap: 10 }}>
                      <button
                        type="button"
                        className={`btn btn-sm ${sandboxMode === 'docker' ? 'btn-primary' : 'btn-secondary'}`}
                        onClick={() => setSandboxMode('docker')}
                      >
                        <span>Docker 隔离容器 (推荐)</span>
                      </button>
                      <button
                        type="button"
                        className={`btn btn-sm ${sandboxMode === 'host' ? 'btn-primary' : 'btn-secondary'}`}
                        onClick={() => setSandboxMode('host')}
                      >
                        <span>Host 本机用户权限</span>
                      </button>
                    </div>
                    <span style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: 6, display: 'block' }}>
                      Docker 模式下源码测试修改不回传宿主，安全性最高。
                    </span>
                  </div>
                </div>
              </section>

              {/* 区块 2.5：### 网络与代理 */}
              <section aria-labelledby="sec-network">
                <SectionHeader
                  title="网络与代理"
                  subtitle="配置 Core 连接外部模型提供商的代理服务器与请求超时"
                  icon={Globe}
                />
                <div className="card" style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                  <div>
                    <label style={{ fontSize: '12px', color: 'var(--text-secondary)', display: 'block', marginBottom: 6 }}>
                      网络代理服务器地址 (Proxy URL)
                    </label>
                    <input
                      type="text"
                      className="input"
                      placeholder="http://127.0.0.1:7890"
                      value={proxyUrl}
                      onChange={(e) => setProxyUrl(e.target.value)}
                    />
                  </div>
                  <div>
                    <label style={{ fontSize: '12px', color: 'var(--text-secondary)', display: 'block', marginBottom: 6 }}>
                      请求连接超时 (秒)
                    </label>
                    <input
                      type="number"
                      className="input"
                      value={timeoutSec}
                      onChange={(e) => setTimeoutSec(e.target.value)}
                      style={{ maxWidth: 160 }}
                    />
                  </div>
                </div>
              </section>
            </div>
          )}

          {/* ========================================================================= */}
          {/* ============================= 3. 安全与治理 ============================= */}
          {/* ========================================================================= */}
          {currentCategory === 'security' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 36 }}>
              {/* 区块 3.1：### 审批与权限策略 (PolicySettings) */}
              <section aria-labelledby="sec-policy-settings">
                <SectionHeader
                  title="审批与权限策略 (PolicySettings)"
                  subtitle="配置 Action Gateway 敏感操作拦截规则与权限降级保护"
                  icon={ShieldCheck}
                />
                <PolicySettings />
              </section>

              {/* 区块 3.2：### 记忆治理 (SQLite FTS5) */}
              <section aria-labelledby="sec-memory-gov">
                <SectionHeader
                  title="记忆治理 (SQLite FTS5)"
                  subtitle="检索与管理持久化分层记忆，确认候选记忆或停用陈旧知识"
                  icon={Brain}
                />
                <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                  <div style={{ display: 'flex', gap: 10 }}>
                    <input
                      type="text"
                      placeholder="搜索 SQLite FTS5 记忆索引…"
                      value={memoryQuery}
                      onChange={(e) => setMemoryQuery(e.target.value)}
                      className="input"
                      style={{ maxWidth: 400 }}
                    />
                    <button onClick={loadData} className="btn btn-secondary">
                      <Search size={14} />
                      <span>搜索</span>
                    </button>
                  </div>

                  <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                    {memories.map((mem) => (
                      <div key={mem.id} className="card" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
                        <div style={{ minWidth: 0 }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                            <span className="badge" style={{ backgroundColor: 'var(--bg-subtle)' }}>
                              {MEMORY_KIND_LABELS[mem.kind] || mem.kind}记忆
                            </span>
                            <StatusBadge
                              status={mem.confirmed ? 'safe' : 'warn'}
                              label={mem.confirmed ? '活跃知识' : '候选记忆'}
                              size="sm"
                            />
                          </div>
                          <div style={{ fontSize: '13px', color: 'var(--text-primary)', lineHeight: 1.4 }}>{mem.content}</div>
                        </div>

                        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                          {!mem.confirmed && (
                            <button onClick={() => handleConfirmMemory(mem.id)} className="btn btn-primary btn-sm">
                              <CheckCircle2 size={13} />
                              <span>确认为活跃</span>
                            </button>
                          )}
                          <button
                            onClick={() => handleDeactivateMemory(mem.id)}
                            className="btn btn-ghost btn-sm"
                            title="停用该记忆"
                            aria-label="停用该记忆"
                          >
                            <Trash2 size={13} color="var(--text-muted)" />
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              </section>

              {/* 区块 3.3：### 保留与审计策略 */}
              <section aria-labelledby="sec-retention">
                <SectionHeader
                  title="保留与审计策略"
                  subtitle="数据生命周期、宽限期清理与 SQLite 不可变历史凭据"
                  icon={HardDrive}
                />
                <div className="card" style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                  <p style={{ fontSize: '13px', color: 'var(--text-secondary)', lineHeight: 1.5, margin: 0 }}>
                    按照 Operant 2.0 规范，自然语言的“完成”表述不会触发静默删除。归档会在 SQLite 中创建可验证的生命周期记录。
                  </p>
                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 12 }}>
                    <div style={{ padding: 12, borderRadius: 'var(--radius-sm)', backgroundColor: 'var(--bg-surface)', border: '1px solid var(--border-subtle)' }}>
                      <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-primary)' }}>权威历史记录</div>
                      <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: 2 }}>
                        持久化 SQLite 审计轨迹，缓存清理永不删除。
                      </div>
                    </div>
                    <div style={{ padding: 12, borderRadius: 'var(--radius-sm)', backgroundColor: 'var(--bg-surface)', border: '1px solid var(--border-subtle)' }}>
                      <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-primary)' }}>临时执行缓存</div>
                      <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: 2 }}>
                        快速索引与差异构建产物，清理前保留 7 天宽限期。
                      </div>
                    </div>
                  </div>
                </div>
              </section>
            </div>
          )}

          {/* ========================================================================= */}
          {/* ============================= 4. 系统与设备 ============================= */}
          {/* ========================================================================= */}
          {currentCategory === 'system' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 36 }}>
              {/* 区块 4.1：### 远程与设备管理 (RemoteView) */}
              <section aria-labelledby="sec-remote-view">
                <SectionHeader
                  title="远程与设备管理 (RemoteView)"
                  subtitle="通过端到端加密通道与短时 PIN 码安全配对手机 PWA 或远程 TUI"
                  icon={Radio}
                />
                <RemoteView />
              </section>

              {/* 区块 4.2：### 关于与系统诊断 */}
              <section aria-labelledby="sec-about">
                <SectionHeader
                  title="关于与系统诊断"
                  subtitle="Operant 系统版本、运行环境状态与诊断数据导出"
                  icon={Info}
                />
                <div className="card" style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 8, fontSize: '13px', color: 'var(--text-secondary)' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <span>Operant 版本</span>
                      <span style={{ fontFamily: 'var(--font-mono)', fontWeight: 600, color: 'var(--text-primary)' }}>v2.0-preview (UI Demo v9)</span>
                    </div>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <span>内核连接状态</span>
                      <StatusBadge status={connectionStatus} size="sm" pulse={connectionStatus === 'connected'} />
                    </div>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <span>运行环境模式</span>
                      <span>本地交互式 Demo (内存状态驱动)</span>
                    </div>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <span>前端框架与内核</span>
                      <span style={{ fontFamily: 'var(--font-mono)', fontSize: '12px' }}>React 19 + TypeScript + Vite</span>
                    </div>
                  </div>

                  <div style={{ marginTop: 8, paddingTop: 12, borderTop: '1px solid var(--border-subtle)', display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}>
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      onClick={() => addNotification('success', '已导出系统诊断日志 bundle.json')}
                    >
                      <Download size={13} />
                      <span>导出诊断日志</span>
                    </button>
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      onClick={() => setResetModalOpen(true)}
                    >
                      <RotateCcw size={13} />
                      <span>重置演示数据</span>
                    </button>
                  </div>
                </div>
              </section>
            </div>
          )}
        </div>
      </div>

      {/* ========================================================================= */}
      {/* =================== 弹窗：OAuth 2.0 PKCE 授权体验 ======================== */}
      {/* ========================================================================= */}
      <Modal
        isOpen={oauthModalOpen}
        onClose={() => setOauthModalOpen(false)}
        title={
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <LinkIcon size={16} color="var(--accent-action)" />
            <span>{currentOAuthTarget?.name} 账号授权登录 (OAuth 2.0 PKCE)</span>
          </div>
        }
        maxWidth={520}
        footer={
          <>
            <button
              onClick={() => setOauthModalOpen(false)}
              disabled={oauthStep === 'authorizing'}
              className="btn btn-ghost"
            >
              取消
            </button>
            <button
              onClick={handleConfirmOAuth}
              disabled={oauthStep === 'authorizing' || oauthStep === 'success'}
              className="btn btn-primary"
            >
              {oauthStep === 'authorizing' ? (
                <>
                  <RefreshCw size={13} className="animate-spin" />
                  <span>正在握手授权...</span>
                </>
              ) : oauthStep === 'success' ? (
                <>
                  <CheckCircle2 size={13} />
                  <span>已完成授权</span>
                </>
              ) : (
                <>
                  <ExternalLink size={13} />
                  <span>打开浏览器并确认授权</span>
                </>
              )}
            </button>
          </>
        }
      >
        {currentOAuthTarget && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: 12, background: 'var(--bg-surface)', borderRadius: 'var(--radius-sm)', border: '1px solid var(--border-subtle)' }}>
              <div style={{ width: 40, height: 40, borderRadius: 'var(--radius-sm)', background: 'var(--accent-subtle)', color: 'var(--accent-on-subtle)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontWeight: 700, fontSize: '18px' }}>
                {currentOAuthTarget.name.slice(0, 1)}
              </div>
              <div style={{ minWidth: 0 }}>
                <div style={{ fontSize: '14px', fontWeight: 600, color: 'var(--text-primary)' }}>
                  {currentOAuthTarget.name}
                </div>
                <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
                  {currentOAuthTarget.desc}
                </div>
              </div>
            </div>

            {/* 授权范围说明 */}
            <div>
              <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-primary)', marginBottom: 6, display: 'flex', alignItems: 'center', gap: 6 }}>
                <Shield size={13} color="var(--accent-action)" />
                <span>请求的权限范围 (Scopes)：</span>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4, background: 'var(--bg-surface)', padding: 10, borderRadius: 'var(--radius-sm)', fontSize: '12px', color: 'var(--text-secondary)' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <Check size={13} color="var(--accent-action)" />
                  <span>读取上游可用模型目录与配额权限</span>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <Check size={13} color="var(--accent-action)" />
                  <span>发送代码补全、规划拆解与深度推理请求</span>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <Check size={13} color="var(--accent-action)" />
                  <span>获取账号基本开发者信息与订阅层级</span>
                </div>
              </div>
            </div>

            {/* 流程状态说明 */}
            <div style={{ padding: '10px 12px', background: 'var(--bg-card)', border: '1px dashed var(--border-subtle)', borderRadius: 'var(--radius-sm)', fontSize: '11px', color: 'var(--text-muted)', lineHeight: 1.5 }}>
              <div>• 采用标准 OAuth 2.0 PKCE 授权码流程，私钥仅存于本机沙箱；</div>
              <div>• 授权成功后将自动拉取您拥有的模型权限并导入为系统 ModelProfile。</div>
            </div>
          </div>
        )}
      </Modal>

      {/* ========================================================================= */}
      {/* =================== 弹窗：上游模型扫描与批量导入 =========================== */}
      {/* ========================================================================= */}
      <Modal
        isOpen={scanModalOpen}
        onClose={() => setScanModalOpen(false)}
        title={
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <Zap size={16} color="var(--accent-action)" />
            <span>发现并导入上游模型 — {scanningProviderName}</span>
          </div>
        }
        maxWidth={620}
        footer={
          <>
            <button onClick={() => setScanModalOpen(false)} className="btn btn-ghost">
              取消
            </button>
            <button
              onClick={() => handleBatchImport(discoveredModels)}
              disabled={discoveredModels.length === 0 || isScanning}
              className="btn btn-secondary"
            >
              <CheckCheck size={14} />
              <span>⚡ 一键全量导入 ({discoveredModels.length})</span>
            </button>
            <button
              onClick={() => handleBatchImport(Array.from(selectedDiscoveredIds))}
              disabled={selectedDiscoveredIds.size === 0 || isScanning}
              className="btn btn-primary"
            >
              <Plus size={14} />
              <span>📥 导入选中项 ({selectedDiscoveredIds.size})</span>
            </button>
          </>
        }
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div style={{ fontSize: '12px', color: 'var(--text-secondary)', background: 'var(--bg-surface)', padding: '8px 12px', borderRadius: 'var(--radius-sm)' }}>
            <div>端点：<span style={{ fontFamily: 'var(--font-mono)' }}>{scanningBaseUrl}</span></div>
            <div>凭据引用：<span style={{ fontFamily: 'var(--font-mono)' }}>{scanningSecretRef}</span></div>
          </div>

          {isScanning ? (
            <div style={{ padding: '36px 0', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 12, color: 'var(--text-secondary)' }}>
              <RefreshCw size={24} className="animate-spin" color="var(--accent-action)" />
              <div style={{ fontSize: '13px' }}>正在连接上游提供商并拉取可用模型目录…</div>
            </div>
          ) : discoveredModels.length === 0 ? (
            <div style={{ padding: '24px 0', textAlign: 'center', color: 'var(--text-secondary)', fontSize: '13px' }}>
              未扫描到可用模型，请检查 Base URL 与凭据配置。
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-primary)' }}>
                  扫描到 {discoveredModels.length} 个可用模型：
                </span>
                <div style={{ display: 'flex', gap: 8 }}>
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    style={{ fontSize: '11px', padding: '2px 6px' }}
                    onClick={() => setSelectedDiscoveredIds(new Set(discoveredModels))}
                  >
                    全选
                  </button>
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    style={{ fontSize: '11px', padding: '2px 6px' }}
                    onClick={() => setSelectedDiscoveredIds(new Set())}
                  >
                    全不选
                  </button>
                </div>
              </div>

              <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 320, overflowY: 'auto' }}>
                {discoveredModels.map((mid) => {
                  const meta = inferModelMetadata(mid);
                  const isSelected = selectedDiscoveredIds.has(mid);
                  const isAlreadyConfigured = models.some((m) => m.model_id === mid);

                  return (
                    <div
                      key={mid}
                      onClick={() => {
                        setSelectedDiscoveredIds((prev) => {
                          const next = new Set(prev);
                          if (next.has(mid)) next.delete(mid);
                          else next.add(mid);
                          return next;
                        });
                      }}
                      style={{
                        padding: '8px 12px',
                        borderRadius: 'var(--radius-sm)',
                        background: isSelected ? 'var(--accent-subtle)' : 'var(--bg-surface)',
                        border: isSelected ? '1px solid var(--accent-action)' : '1px solid var(--border-subtle)',
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'space-between',
                        cursor: 'pointer',
                        gap: 10,
                      }}
                    >
                      <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 }}>
                        {isSelected ? (
                          <CheckSquare size={16} color="var(--accent-action)" style={{ flexShrink: 0 }} />
                        ) : (
                          <Square size={16} color="var(--text-muted)" style={{ flexShrink: 0 }} />
                        )}
                        <div style={{ minWidth: 0 }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                            <span style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)' }}>
                              {meta.name}
                            </span>
                            {isAlreadyConfigured && (
                              <span className="badge" style={{ backgroundColor: 'var(--bg-subtle)', fontSize: '10px' }}>
                                已导入
                              </span>
                            )}
                          </div>
                          <div style={{ fontSize: '11px', fontFamily: 'var(--font-mono)', color: 'var(--text-muted)' }}>
                            {mid}
                          </div>
                        </div>
                      </div>

                      <div style={{ display: 'flex', gap: 6, flexShrink: 0, fontSize: '11px', color: 'var(--text-secondary)' }}>
                        <span className="badge" style={{ backgroundColor: 'var(--bg-card)' }}>
                          {formatNumber(meta.contextWindow)} ctx
                        </span>
                        <span className="badge" style={{ backgroundColor: 'var(--bg-card)' }}>
                          {meta.defaultEffort.toUpperCase()}
                        </span>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      </Modal>

      {/* ========================================================================= */}
      {/* =================== 弹窗：手动添加模型配置 =============================== */}
      {/* ========================================================================= */}
      <Modal
        isOpen={manualModelModalOpen}
        onClose={() => setManualModelModalOpen(false)}
        title="添加自定义模型配置"
        footer={
          <>
            <button onClick={() => setManualModelModalOpen(false)} className="btn btn-ghost">
              取消
            </button>
            <button
              onClick={handleManualCreateModel}
              disabled={!manualModelName.trim() || !manualModelId.trim()}
              className="btn btn-primary"
            >
              <Plus size={14} />
              <span>保存配置</span>
            </button>
          </>
        }
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div>
            <label style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: 4 }}>
              配置名称
            </label>
            <input
              type="text"
              className="input"
              placeholder="例如：本地 Qwen 2.5 Coder 32B"
              value={manualModelName}
              onChange={(e) => setManualModelName(e.target.value)}
            />
          </div>

          <div>
            <label style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: 4 }}>
              Base URL (OpenAI 兼容)
            </label>
            <input
              type="text"
              className="input"
              value={manualModelBaseUrl}
              onChange={(e) => setManualModelBaseUrl(e.target.value)}
            />
          </div>

          <div>
            <label style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: 4 }}>
              模型 ID
            </label>
            <input
              type="text"
              className="input"
              placeholder="例如：qwen2.5-coder-32b-instruct"
              value={manualModelId}
              onChange={(e) => {
                const val = e.target.value;
                setManualModelId(val);
                if (!manualModelName && val) {
                  setManualModelName(inferModelMetadata(val).name);
                }
              }}
            />
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
            <div>
              <label style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: 4 }}>
                Secret Ref（环境变量名）
              </label>
              <input
                type="text"
                className="input"
                value={manualModelSecretRef}
                onChange={(e) => setManualModelSecretRef(e.target.value)}
              />
            </div>
            <div>
              <label style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: 4 }}>
                上下文窗口 (Tokens)
              </label>
              <input
                type="number"
                className="input"
                value={manualModelContextWindow}
                onChange={(e) => setManualModelContextWindow(e.target.value)}
              />
            </div>
          </div>
        </div>
      </Modal>

      {/* ========================================================================= */}
      {/* =================== 弹窗：重置演示数据确认 =============================== */}
      {/* ========================================================================= */}
      <Modal
        isOpen={resetModalOpen}
        onClose={() => setResetModalOpen(false)}
        title="确认重置演示数据"
        footer={
          <>
            <button className="btn btn-ghost" onClick={() => setResetModalOpen(false)}>
              取消
            </button>
            <button
              className="btn btn-primary"
              onClick={() => {
                resetDemoState();
                setResetModalOpen(false);
                addNotification('success', '已重置演示数据为初始状态。');
              }}
            >
              确认重置
            </button>
          </>
        }
      >
        <p style={{ fontSize: '13px', color: 'var(--text-secondary)', lineHeight: 1.5, margin: 0 }}>
          这将把会话列表、项目结构、临时工作流、调度任务与技能状态全部复位为默认初始状态。确定继续吗？
        </p>
      </Modal>
    </div>
  );
};

const LiveSecuritySettingsView: React.FC = () => (
  <div className="section-view" data-client-mode="live">
    <header className="section-header">
      <h1 className="section-title">安全与治理</h1>
      <p className="section-sub">Phase 4 Policy 检查与解释。Live 模式不会显示或修改演示设置。</p>
    </header>
    <div className="section-scroll">
      <div className="section-inner"><PolicySettings /></div>
    </div>
  </div>
);

export const SettingsView: React.FC = () => {
  const { clientMode } = useOperant();
  return clientMode === 'live' ? <LiveSecuritySettingsView /> : <DemoSettingsView />;
};
