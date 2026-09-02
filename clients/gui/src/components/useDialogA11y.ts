import { useEffect, useRef } from 'react';

const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * 已打开对话框栈（模块级，v6 修复轮）：Esc 仅由最上层（最后打开且未关闭）的对话框响应，
 * 下层对话框忽略——Drawer 内叠开重命名/删除确认 Modal 时，第一次 Esc 只关 Modal、
 * 第二次才关 Drawer；单个对话框（含桌面侧栏场景）行为不变。
 */
const openDialogStack: symbol[] = [];

/**
 * 对话框无障碍通用逻辑：
 * - 打开时焦点移入面板内第一个可聚焦元素（无则聚焦面板本身）；
 * - 关闭时焦点还原到触发前的元素；
 * - Tab / Shift+Tab 在面板内循环（简版 focus trap）；
 * - Esc 关闭（多对话框叠开时仅最上层响应）。
 */
export function useDialogA11y<T extends HTMLElement>(isOpen: boolean, onClose: () => void) {
  const panelRef = useRef<T | null>(null);
  // onClose 经 ref 间接调用：effect 仅随 isOpen 变化重跑，避免父组件重渲染
  // （如内联箭头 onClose 变引用）导致栈内 token 被弹出重压而打乱层级顺序
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  });

  useEffect(() => {
    if (!isOpen) return;
    const token = Symbol('dialog');
    openDialogStack.push(token);
    const removeFromStack = () => {
      const index = openDialogStack.indexOf(token);
      if (index >= 0) openDialogStack.splice(index, 1);
    };

    const panel = panelRef.current;
    if (!panel) {
      // 面板未挂载（理论不可达）：仍保证栈对称清理
      return removeFromStack;
    }

    const previouslyFocused = document.activeElement as HTMLElement | null;

    const getFocusable = () =>
      Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
        (el) => el.getClientRects().length > 0
      );

    // 打开时焦点移入面板
    const first = getFocusable()[0];
    (first ?? panel).focus();

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        // 非最上层对话框忽略 Esc，防止一次按键连关多层
        if (openDialogStack[openDialogStack.length - 1] !== token) return;
        e.preventDefault();
        e.stopPropagation();
        onCloseRef.current();
        return;
      }
      if (e.key !== 'Tab') return;
      const items = getFocusable();
      if (items.length === 0) {
        e.preventDefault();
        panel.focus();
        return;
      }
      const firstItem = items[0];
      const lastItem = items[items.length - 1];
      const active = document.activeElement;
      if (e.shiftKey && (active === firstItem || active === panel)) {
        e.preventDefault();
        lastItem.focus();
      } else if (!e.shiftKey && (active === lastItem || active === panel)) {
        e.preventDefault();
        firstItem.focus();
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => {
      removeFromStack();
      document.removeEventListener('keydown', handleKeyDown);
      // 关闭时焦点还原到触发元素
      previouslyFocused?.focus?.();
    };
  }, [isOpen]);

  return panelRef;
}
