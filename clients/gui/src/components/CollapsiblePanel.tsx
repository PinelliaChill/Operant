/**
 * 可折叠面板共享机制（v6 §3/§4）：localStorage 持久化的展开状态 + 收起态细竖把手。
 * 使用方：RailLayout 情境侧栏（operant.panel.chatSidebar / operant.panel.collabSidebar）
 * 与 GraphCanvas 节点面板 / 节点配置面板（operant.panel.nodePalette / operant.panel.nodeConfig）。
 * 约定：值 "1"=展开、"0"=收起；缺失或非法值一律按展开处理；各面板初始默认展开。
 */

import React, { useCallback, useState } from 'react';
import { ChevronLeft, ChevronRight } from 'lucide-react';

/** 容错读取面板展开状态（异常/缺失/非法值按展开处理） */
export const readPanelExpanded = (storageKey: string): boolean => {
  try {
    const raw = window.localStorage.getItem(storageKey);
    if (raw === '0') return false;
    return true;
  } catch {
    return true;
  }
};

/** 容错写入面板展开状态（隐私模式等异常静默忽略） */
export const writePanelExpanded = (storageKey: string, expanded: boolean): void => {
  try {
    window.localStorage.setItem(storageKey, expanded ? '1' : '0');
  } catch {
    // 忽略写入异常：仅影响下次记忆，不影响本次交互
  }
};

/** 面板展开状态 hook：读写 localStorage（默认展开），同 key 各自独立、互不影响 */
export function usePanelExpanded(storageKey: string): [boolean, (expanded: boolean) => void] {
  const [expanded, setExpandedState] = useState<boolean>(() => readPanelExpanded(storageKey));

  const setExpanded = useCallback(
    (next: boolean) => {
      setExpandedState(next);
      writePanelExpanded(storageKey, next);
    },
    [storageKey]
  );

  return [expanded, setExpanded];
}

interface PanelCollapseHandleProps {
  /** 把手所在侧：left=面板停靠左缘（chevron 朝右示意展开），right=面板停靠右缘（chevron 朝左） */
  side: 'left' | 'right';
  /** 点击 / Enter 后展开面板 */
  onExpand: () => void;
  /** 无障碍名称（如「展开侧栏」「展开节点面板」） */
  label: string;
}

/**
 * 收起态细竖把手（约 22px 宽、全高）：居中一个 chevron 图标按钮，
 * 点击或 Enter 展开对应面板；把手即按钮本身，无障碍名称经 aria-label 提供。
 */
export const PanelCollapseHandle: React.FC<PanelCollapseHandleProps> = ({
  side,
  onExpand,
  label,
}) => {
  const Chevron = side === 'left' ? ChevronRight : ChevronLeft;
  return (
    <button
      type="button"
      className={`panel-handle panel-handle-${side}`}
      onClick={onExpand}
      aria-label={label}
      title={label}
    >
      <Chevron size={14} aria-hidden="true" />
    </button>
  );
};
