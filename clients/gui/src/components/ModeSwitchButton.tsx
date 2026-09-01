/**
 * 唯一模式切换钮（v7 §1）：对话 ↔ 协作 双模式的唯一切换入口。
 * 44×44 恒显**当前模式**图标（对话=MessageSquare / 协作=Users）。
 * 新交互（条件菜单）：
 * - 点击模式钮：
 *   - 若当前不在该模式主页（如在 /chat/:id 或 /collab?tab=canvas / /workflow/*），点击直接回到当前模式主页（对话→/chat；协作→/collab），不切换模式，不打开菜单；
 *   - 若已在该模式主页（/chat 或 /collab），点击打开切换菜单（对话/协作两项，当前项置灰不可点，另一项点击切换）；
 * - 键盘 Enter/Space 等价点击，菜单 ↑↓/Esc/Home/End 焦点管理，关闭后焦点归还按钮。
 * - 挂载位置：IconRail 顶部与窄屏抽屉顶部。
 */

import React, { useEffect, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { Check, MessageSquare, Users } from 'lucide-react';

type ModeKey = 'chat' | 'collab';

/** 模式菜单项（固定顺序：对话 → 协作） */
const MODE_ITEMS: ReadonlyArray<{ key: ModeKey; label: string; to: string }> = [
  { key: 'chat', label: '对话模式', to: '/chat' },
  { key: 'collab', label: '协作工作台', to: '/collab' },
];

interface ModeSwitchButtonProps {
  /** 菜单项激活后回调（窄屏抽屉用于关闭自身） */
  onNavigate?: () => void;
}

export const ModeSwitchButton: React.FC<ModeSwitchButtonProps> = ({ onNavigate }) => {
  const location = useLocation();
  const navigate = useNavigate();

  // /workflow 深链（群聊 / Agent 个人界面）同属协作语境
  const isCollab =
    location.pathname.startsWith('/collab') || location.pathname.startsWith('/workflow');
  const currentMode: ModeKey = isCollab ? 'collab' : 'chat';
  const currentLabel = isCollab ? '协作' : '对话';

  // 判断是否处于当前模式的主页
  const isAtHome = isCollab
    ? location.pathname === '/collab' && (!location.search || location.search === '?tab=overview')
    : location.pathname === '/chat';

  const [menuOpen, setMenuOpen] = useState(false);
  const btnRef = useRef<HTMLButtonElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([]);
  /** 打开菜单时初始聚焦的项下标 */
  const initialFocusRef = useRef(0);

  const closeMenu = (refocus: boolean) => {
    setMenuOpen(false);
    if (refocus) btnRef.current?.focus();
  };

  /** 激活菜单项：切换模式则跳转；无论是否切换都关闭菜单并归还焦点 */
  const activateItem = (item: (typeof MODE_ITEMS)[number]) => {
    if (item.key === currentMode) return;
    setMenuOpen(false);
    navigate(item.to);
    onNavigate?.();
    btnRef.current?.focus();
  };

  // 菜单打开：聚焦可选项（非禁用项）；点击外部关闭
  useEffect(() => {
    if (!menuOpen) return;
    const targetIdx = initialFocusRef.current;
    itemRefs.current[targetIdx]?.focus();

    const onOutsidePointerDown = (e: MouseEvent | TouchEvent) => {
      if (wrapRef.current && e.target instanceof Node && wrapRef.current.contains(e.target)) {
        return;
      }
      setMenuOpen(false);
    };
    document.addEventListener('mousedown', onOutsidePointerDown);
    document.addEventListener('touchstart', onOutsidePointerDown);
    return () => {
      document.removeEventListener('mousedown', onOutsidePointerDown);
      document.removeEventListener('touchstart', onOutsidePointerDown);
    };
  }, [menuOpen]);

  /** 按钮点击逻辑（条件菜单） */
  const handleButtonClick = () => {
    if (menuOpen) {
      closeMenu(true);
      return;
    }
    if (!isAtHome) {
      // 不在主页：直接导航回当前模式主页，不打开菜单
      navigate(isCollab ? '/collab' : '/chat');
      onNavigate?.();
      return;
    }
    // 已在主页：打开切换菜单（初始聚焦另一项）
    const otherIdx = MODE_ITEMS.findIndex((it) => it.key !== currentMode);
    initialFocusRef.current = otherIdx >= 0 ? otherIdx : 0;
    setMenuOpen(true);
  };

  const handleButtonKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const otherIdx = MODE_ITEMS.findIndex((it) => it.key !== currentMode);
      initialFocusRef.current = otherIdx >= 0 ? otherIdx : 0;
      setMenuOpen(true);
      return;
    }
    if (e.key === 'Escape' && menuOpen) {
      e.preventDefault();
      e.stopPropagation();
      closeMenu(true);
    }
  };

  const handleMenuKeyDown = (e: React.KeyboardEvent) => {
    const items = itemRefs.current;
    const currentIndex = items.findIndex((el) => el === document.activeElement);
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      const next = currentIndex < 0 ? 0 : (currentIndex + 1) % items.length;
      items[next]?.focus();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      const prev =
        currentIndex < 0 ? items.length - 1 : (currentIndex - 1 + items.length) % items.length;
      items[prev]?.focus();
    } else if (e.key === 'Home') {
      e.preventDefault();
      items[0]?.focus();
    } else if (e.key === 'End') {
      e.preventDefault();
      items[items.length - 1]?.focus();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      closeMenu(true);
    } else if (e.key === 'Tab') {
      setMenuOpen(false);
    }
  };

  const CurrentIcon = isCollab ? Users : MessageSquare;

  return (
    <div ref={wrapRef} className="mode-switch-wrap">
      <button
        ref={btnRef}
        type="button"
        className={`mode-switch-btn${isAtHome ? ' is-home' : ''}`}
        onClick={handleButtonClick}
        onKeyDown={handleButtonKeyDown}
        aria-haspopup="menu"
        aria-expanded={menuOpen}
        aria-label={`当前模式：${currentLabel}。${!isAtHome ? '点击返回主页' : '点击切换模式'}`}
        title={!isAtHome ? `返回${currentLabel}主页` : '切换模式'}
      >
        <CurrentIcon size={20} aria-hidden="true" />
      </button>

      {menuOpen && (
        <div
          className="mode-switch-menu"
          role="menu"
          aria-label="切换模式"
          aria-orientation="vertical"
          onKeyDown={handleMenuKeyDown}
        >
          {MODE_ITEMS.map((item, index) => {
            const isCurrent = item.key === currentMode;
            return (
              <button
                key={item.key}
                ref={(el) => {
                  itemRefs.current[index] = el;
                }}
                type="button"
                role="menuitem"
                className={`mode-switch-menu-item${isCurrent ? ' current disabled' : ''}`}
                aria-current={isCurrent ? 'true' : undefined}
                disabled={isCurrent}
                onClick={() => activateItem(item)}
              >
                <span className="mode-switch-menu-check" aria-hidden="true">
                  {isCurrent && <Check size={14} />}
                </span>
                <span>{item.label}</span>
                {isCurrent && <span className="mode-switch-current-badge">当前</span>}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
};
