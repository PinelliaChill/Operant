/**
 * 节点配置面板（340px，右侧；<960px 为底部抽屉）。
 * 表单复用原 WorkflowView 配置面板：节点标签 / 节点类型（只读）/ 角色绑定（agent）/
 * 作用域继承展示。数据流改为受控：onChange(patch) 直接回写画布本地编辑态。
 * 仅在选中节点时由父级渲染。
 * v6 §4：头部带收起钮（GraphCanvas 注入 onCollapse），收起后由画布右缘细把手展开，
 * 折叠状态独立于选中逻辑并经 localStorage 持久化。
 */

import React from 'react';
import { Layers, PanelLeftClose } from 'lucide-react';
import type { GraphNode } from '@operant/sdk';
import { NODE_TYPE_LABELS } from './graph-utils';

interface NodeConfigPanelProps {
  node: GraphNode;
  onChange: (patch: Partial<GraphNode>) => void;
  /** 收起配置面板回调（GraphCanvas 注入；收起后画布右缘留"展开配置面板"把手） */
  onCollapse?: () => void;
}

const fieldLabelStyle: React.CSSProperties = {
  fontSize: '11px',
  fontWeight: 600,
  color: 'var(--text-muted)',
  display: 'block',
  marginBottom: 4,
};

export const NodeConfigPanel: React.FC<NodeConfigPanelProps> = ({ node, onChange, onCollapse }) => {
  return (
    <div className="collab-config-panel" aria-label="节点配置面板">
      <div className="collab-config-head">
        <h3 style={{ fontSize: '14px', fontWeight: 600, color: 'var(--text-primary)' }}>
          节点配置
        </h3>
        {onCollapse && (
          <button
            type="button"
            className="collab-panel-collapse-btn"
            onClick={onCollapse}
            aria-label="收起配置面板"
            title="收起配置面板"
          >
            <PanelLeftClose size={13} />
          </button>
        )}
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        <div>
          <label style={fieldLabelStyle} htmlFor={`node-label-${node.id}`}>
            节点标签
          </label>
          <input
            id={`node-label-${node.id}`}
            type="text"
            className="input"
            value={node.label}
            onChange={(e) => onChange({ label: e.target.value })}
          />
        </div>

        <div>
          <label style={fieldLabelStyle} htmlFor={`node-type-${node.id}`}>
            节点类型
          </label>
          <input
            id={`node-type-${node.id}`}
            type="text"
            className="input"
            value={NODE_TYPE_LABELS[node.type] || node.type}
            disabled
          />
        </div>

        {node.type === 'agent' && (
          <div>
            <label style={fieldLabelStyle} htmlFor={`node-role-${node.id}`}>
              角色绑定
            </label>
            <input
              id={`node-role-${node.id}`}
              type="text"
              className="input"
              value={node.role_id || ''}
              onChange={(e) => onChange({ role_id: e.target.value })}
            />
          </div>
        )}

        {/* 作用域继承展示（全局 → 工作区 → 节点） */}
        <div
          style={{
            padding: 10,
            borderRadius: 'var(--radius-sm)',
            backgroundColor: 'var(--bg-card)',
            border: '1px solid var(--border-subtle)',
          }}
        >
          <div
            style={{
              fontSize: '11px',
              fontWeight: 600,
              color: 'var(--accent-action)',
              marginBottom: 6,
              display: 'flex',
              alignItems: 'center',
              gap: 4,
            }}
          >
            <Layers size={12} />
            <span>作用域继承（全局 → 工作区 → 节点）</span>
          </div>
          <div style={{ fontSize: '11px', color: 'var(--text-secondary)', lineHeight: 1.5 }}>
            <div>
              模型：<span style={{ fontWeight: 600 }}>Claude 3.7 Sonnet</span>（工作流来源）
            </div>
            <div>
              预算：<span style={{ fontWeight: 600 }}>15 轮 / 300 秒</span>（节点覆盖）
            </div>
            <div>
              策略：<span style={{ fontWeight: 600 }}>严格写入审批</span>（全局）
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};
