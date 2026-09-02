/**
 * 节点面板：7 种节点类型按钮（图标 + 中文名）。
 * 点击 = 追加到画布级联空位；按住拖动 = 在指针落点生成节点（次要交互，由 GraphCanvas 编排）。
 * 桌面为左侧 160px 竖列；<960px 由 CSS 改为顶部横排 chips。
 * v6 §4：头部带收起钮（GraphCanvas 注入 onCollapse），收起后由画布左缘细把手展开。
 */

import React from 'react';
import { PanelLeftClose } from 'lucide-react';
import type { GraphNodeType } from '@operant/sdk';
import { NODE_TYPE_ICONS, NODE_TYPE_LABELS, PALETTE_TYPES } from './graph-utils';

interface NodePaletteProps {
  onAddNode: (type: GraphNodeType) => void;
  onDragStart: (type: GraphNodeType, e: React.PointerEvent) => void;
  /** 收起节点面板回调（GraphCanvas 注入；收起后画布左缘留"展开节点面板"把手） */
  onCollapse?: () => void;
}

export const NodePalette: React.FC<NodePaletteProps> = ({
  onAddNode,
  onDragStart,
  onCollapse,
}) => {
  return (
    <div className="collab-palette" aria-label="节点面板">
      <div className="collab-palette-title">
        <span>节点</span>
        {onCollapse && (
          <button
            type="button"
            className="collab-panel-collapse-btn"
            onClick={onCollapse}
            aria-label="收起节点面板"
            title="收起节点面板"
          >
            <PanelLeftClose size={13} />
          </button>
        )}
      </div>
      {PALETTE_TYPES.map((type) => {
        const Icon = NODE_TYPE_ICONS[type];
        const label = NODE_TYPE_LABELS[type] ?? type;
        return (
          <button
            key={type}
            type="button"
            className="collab-palette-item"
            aria-label={`添加${label}节点`}
            title={`点击添加，或拖入画布：${label}`}
            onClick={() => onAddNode(type)}
            onPointerDown={(e) => onDragStart(type, e)}
          >
            <Icon size={14} aria-hidden="true" />
            <span>{label}</span>
          </button>
        );
      })}
    </div>
  );
};
