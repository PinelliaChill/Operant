/**
 * 画布节点卡片：绝对定位于 position.x/y，宽 220px。
 * 结构：头部（图标+标签+类型徽章）→ 端口区（左列输入 / 右列输出，行高固定 16px，
 * 与 graph-utils 的端口坐标常量对应）→ 元信息区（角色 / 循环约束）。
 * 卡片可聚焦（tabIndex=0），聚焦即选中；端口为独立 button，支持点连与键盘操作。
 */

import React from 'react';
import type { GraphNode } from '@operant/sdk';
import { NODE_TYPE_ICONS, NODE_TYPE_LABELS, NODE_WIDTH } from './graph-utils';

interface GraphNodeCardProps {
  node: GraphNode;
  selected: boolean;
  dragging: boolean;
  /** 本节点上处于连线待命态的源输出端口 id（无则 null） */
  armedOutputPortId: string | null;
  onPointerDown: (e: React.PointerEvent) => void;
  onKeyDown: (e: React.KeyboardEvent) => void;
  onFocusSelect: () => void;
  onOutputPortPointerDown: (portId: string, e: React.PointerEvent) => void;
  /** 仅响应键盘激活（click detail === 0），指针点连由 pointer 流程处理 */
  onOutputPortClick: (portId: string, e: React.MouseEvent) => void;
  onInputPortClick: (portId: string) => void;
}

export const GraphNodeCard: React.FC<GraphNodeCardProps> = ({
  node,
  selected,
  dragging,
  armedOutputPortId,
  onPointerDown,
  onKeyDown,
  onFocusSelect,
  onOutputPortPointerDown,
  onOutputPortClick,
  onInputPortClick,
}) => {
  const TypeIcon = NODE_TYPE_ICONS[node.type] ?? NODE_TYPE_ICONS.subworkflow;

  return (
    <div
      className={`graph-node${selected ? ' selected' : ''}${dragging ? ' dragging' : ''}`}
      style={{ left: node.position.x, top: node.position.y, width: NODE_WIDTH }}
      tabIndex={0}
      role="button"
      aria-label={`节点：${node.label}`}
      onPointerDown={onPointerDown}
      onKeyDown={onKeyDown}
      onFocus={(e) => {
        // 仅卡片自身聚焦时选中；端口按钮聚焦不改动选中态
        if (e.target === e.currentTarget) onFocusSelect();
      }}
    >
      <div className="graph-node-header">
        <TypeIcon size={14} color="var(--accent-action)" aria-hidden="true" />
        <span className="graph-node-label">{node.label}</span>
        <span className="badge graph-node-badge">{NODE_TYPE_LABELS[node.type] ?? node.type}</span>
      </div>

      <div className="graph-node-ports">
        <div className="graph-port-col">
          {node.inputs.map((port) => (
            <div key={port.id} className="graph-port-row graph-port-row-in">
              <button
                type="button"
                className="graph-port"
                data-node-id={node.id}
                data-port-id={port.id}
                data-direction="input"
                aria-label={`${node.label} 的输入端口 ${port.name}`}
                onPointerDown={(e) => e.stopPropagation()}
                onClick={() => onInputPortClick(port.id)}
              />
              <span className="graph-port-name">{port.name}</span>
            </div>
          ))}
        </div>
        <div className="graph-port-col">
          {node.outputs.map((port) => {
            const armed = armedOutputPortId === port.id;
            return (
              <div key={port.id} className="graph-port-row graph-port-row-out">
                <span className="graph-port-name">{port.name}</span>
                <button
                  type="button"
                  className={`graph-port${armed ? ' armed' : ''}`}
                  data-node-id={node.id}
                  data-port-id={port.id}
                  data-direction="output"
                  aria-label={armed ? '选择目标输入端口' : `${node.label} 的输出端口 ${port.name}`}
                  onPointerDown={(e) => onOutputPortPointerDown(port.id, e)}
                  onClick={(e) => onOutputPortClick(port.id, e)}
                />
              </div>
            );
          })}
        </div>
      </div>

      {(node.role_id || node.loop_constraint) && (
        <div className="graph-node-meta">
          {node.role_id && (
            <div>
              角色：<span style={{ fontWeight: 600 }}>{node.role_id}</span>
            </div>
          )}
          {node.loop_constraint && (
            <div
              style={{
                marginTop: 2,
                fontSize: 11,
                color: 'var(--status-warn-text)',
                backgroundColor: 'var(--status-warn-bg)',
                padding: '2px 6px',
                borderRadius: 4,
              }}
            >
              循环上限：{node.loop_constraint.max_iterations} 次（
              {node.loop_constraint.exit_condition_expr}）
            </div>
          )}
        </div>
      )}
    </div>
  );
};
