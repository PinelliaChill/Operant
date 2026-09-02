/**
 * 实例行「···」轻量 popover 菜单（v6 包C）。
 * 交互惯例对齐 components/ModeSwitchButton（WAI-ARIA menu button）：
 * Esc / 点击外部 / Tab 关闭，ArrowUp/ArrowDown 在菜单项间循环移焦，
 * Home/End 跳首末项，激活后关闭菜单；Esc 关闭时阻止冒泡，避免外层连锁关闭。
 * 触发钮默认透明、随宿主行 hover/聚焦显现（见 collab-inst.css）。
 */

import React, { useEffect, useRef, useState } from 'react';
import { MoreHorizontal } from 'lucide-react';
import './collab-inst.css';

/** 菜单项（danger 项以语义红渲染，如「删除」） */
export interface InstanceMenuItem {
  key: string;
  label: string;
  danger?: boolean;
  onSelect: () => void;
}

interface InstanceMenuProps {
  items: InstanceMenuItem[];
  /** 触发钮与菜单的无障碍标签（如「实例「xx」操作菜单」） */
  ariaLabel: string;
  /** 弹出方向：down（默认）/ up（归档区等靠近容器底部时向上） */
  direction?: 'down' | 'up';
}

export const InstanceMenu: React.FC<InstanceMenuProps> = ({
  items,
  ariaLabel,
  direction = 'down',
}) => {
  const [open, setOpen] = useState(false);
  const btnRef = useRef<HTMLButtonElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([]);
  /** 打开菜单时初始聚焦的项下标（ArrowUp 打开聚焦末项，其余聚焦首项） */
  const initialFocusRef = useRef(0);

  const closeMenu = (refocus: boolean) => {
    setOpen(false);
    if (refocus) btnRef.current?.focus();
  };

  // 菜单打开：聚焦初始项；点击外部（mousedown/touchstart）关闭（不抢焦点）
  useEffect(() => {
    if (!open) return;
    itemRefs.current[initialFocusRef.current]?.focus();

    const onOutsidePointerDown = (e: MouseEvent | TouchEvent) => {
      if (wrapRef.current && e.target instanceof Node && wrapRef.current.contains(e.target)) {
        return;
      }
      setOpen(false);
    };
    document.addEventListener('mousedown', onOutsidePointerDown);
    document.addEventListener('touchstart', onOutsidePointerDown);
    return () => {
      document.removeEventListener('mousedown', onOutsidePointerDown);
      document.removeEventListener('touchstart', onOutsidePointerDown);
    };
  }, [open]);

  const handleButtonKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      initialFocusRef.current = e.key === 'ArrowDown' ? 0 : items.length - 1;
      setOpen(true);
      return;
    }
    if (e.key === 'Escape' && open) {
      // 仅关闭菜单：阻止冒泡，避免移动端 Drawer 等外层 Esc 监听连带关闭（v6 修复轮）
      e.preventDefault();
      e.stopPropagation();
      closeMenu(true);
    }
  };

  const handleMenuKeyDown = (e: React.KeyboardEvent) => {
    const els = itemRefs.current;
    const currentIndex = els.findIndex((el) => el === document.activeElement);
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      const next = currentIndex < 0 ? 0 : (currentIndex + 1) % els.length;
      els[next]?.focus();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      const prev = currentIndex < 0 ? els.length - 1 : (currentIndex - 1 + els.length) % els.length;
      els[prev]?.focus();
    } else if (e.key === 'Home') {
      e.preventDefault();
      els[0]?.focus();
    } else if (e.key === 'End') {
      e.preventDefault();
      els[els.length - 1]?.focus();
    } else if (e.key === 'Escape') {
      // 仅关闭菜单：阻止冒泡，避免抽屉 / 对话框等外层 Esc 监听连锁关闭
      e.preventDefault();
      e.stopPropagation();
      closeMenu(true);
    } else if (e.key === 'Tab') {
      // 按惯例 Tab 离开菜单即关闭（焦点自然移动，不拦截）
      setOpen(false);
    }
  };

  return (
    <div ref={wrapRef} className="inst-menu-wrap">
      <button
        ref={btnRef}
        type="button"
        className="inst-menu-btn"
        onClick={() => {
          if (open) {
            closeMenu(true);
          } else {
            initialFocusRef.current = 0;
            setOpen(true);
          }
        }}
        onKeyDown={handleButtonKeyDown}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={ariaLabel}
        title="更多操作"
      >
        <MoreHorizontal size={14} aria-hidden="true" />
      </button>

      {open && (
        <div
          className={`inst-menu${direction === 'up' ? ' inst-menu--up' : ''}`}
          role="menu"
          aria-label={ariaLabel}
          aria-orientation="vertical"
          onKeyDown={handleMenuKeyDown}
        >
          {items.map((item, index) => (
            <button
              key={item.key}
              ref={(el) => {
                itemRefs.current[index] = el;
              }}
              type="button"
              role="menuitem"
              className={`inst-menu-item${item.danger ? ' inst-menu-item--danger' : ''}`}
              onClick={() => {
                // 菜单项可能打开对话框（重命名/删除确认）：先关菜单，焦点交由对话框接管
                setOpen(false);
                item.onSelect();
              }}
            >
              <span>{item.label}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
};
