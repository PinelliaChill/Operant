import React, { useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Check, ChevronDown, Search } from 'lucide-react';
import './search-select.css';

export interface SearchOption { value: string; label: string; detail?: string; disabled?: boolean }
export interface SearchSelectProps {
  label: string;
  value: string;
  options: readonly SearchOption[];
  onChange: (value: string) => void;
  placeholder?: string;
  disabled?: boolean;
  searchable?: boolean;
  buttonRef?: React.Ref<HTMLButtonElement>;
}

/** Shared searchable chooser for short, server supplied model, role and project lists. */
export const SearchSelect: React.FC<SearchSelectProps> = ({ label, value, options, onChange, placeholder = '请选择', disabled = false, searchable = true, buttonRef }) => {
  const id = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const popupRef = useRef<HTMLDivElement>(null);
  const optionsRef = useRef<Array<HTMLButtonElement | null>>([]);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [focusIndex, setFocusIndex] = useState(0);
  const [rect, setRect] = useState<DOMRect | null>(null);
  const selected = options.find((option) => option.value === value);
  const visible = options.filter((option) => !query || `${option.label} ${option.detail || ''}`.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  const firstAvailable = visible.findIndex((option) => !option.disabled);
  const showAbove = Boolean(rect && window.innerHeight - rect.bottom < 220 && rect.top > window.innerHeight - rect.bottom);
  const popupHeight = rect ? Math.min(300, Math.max(120, showAbove ? rect.top - 16 : window.innerHeight - rect.bottom - 16)) : 300;

  const close = (restoreFocus = true) => {
    setOpen(false);
    setQuery('');
    if (restoreFocus) requestAnimationFrame(() => triggerRef.current?.focus());
  };
  const choose = (option: SearchOption) => {
    if (option.disabled) return;
    onChange(option.value);
    close();
  };
  const move = (direction: 1 | -1) => {
    if (!visible.length) return;
    let next = focusIndex;
    for (let tries = 0; tries < visible.length; tries += 1) {
      next = (next + direction + visible.length) % visible.length;
      if (!visible[next]?.disabled) { setFocusIndex(next); optionsRef.current[next]?.focus(); return; }
    }
  };

  useEffect(() => {
    if (!open) return;
    const updateRect = () => setRect(triggerRef.current?.getBoundingClientRect() || null);
    updateRect();
    inputRef.current?.focus();
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !popupRef.current?.contains(event.target) && !triggerRef.current?.contains(event.target)) close(false);
    };
    window.addEventListener('resize', updateRect);
    window.addEventListener('scroll', updateRect, true);
    document.addEventListener('pointerdown', outside);
    return () => { window.removeEventListener('resize', updateRect); window.removeEventListener('scroll', updateRect, true); document.removeEventListener('pointerdown', outside); };
  }, [open]);

  const onKeys = (event: React.KeyboardEvent) => {
    if (event.key === 'Escape') { event.preventDefault(); close(); }
    else if (event.key === 'ArrowDown') { event.preventDefault(); move(1); }
    else if (event.key === 'ArrowUp') { event.preventDefault(); move(-1); }
    else if (event.key === 'Home') { event.preventDefault(); setFocusIndex(firstAvailable); optionsRef.current[firstAvailable]?.focus(); }
    else if (event.key === 'End') { event.preventDefault(); const last = visible.map((option, index) => option.disabled ? -1 : index).filter((index) => index >= 0).pop() ?? -1; setFocusIndex(last); optionsRef.current[last]?.focus(); }
    else if (event.key === 'Enter' && event.target === inputRef.current) { const option = visible[focusIndex] || visible[firstAvailable]; if (option && !option.disabled) { event.preventDefault(); choose(option); } }
    else if (event.key === 'Tab') close(false);
  };

  return <div className="search-select">
    <span id={`${id}-label`} className="search-select-label">{label}</span>
    <button ref={(element) => { triggerRef.current = element; if (typeof buttonRef === 'function') buttonRef(element); else if (buttonRef) buttonRef.current = element; }} type="button" className="search-select-trigger" aria-labelledby={`${id}-label ${id}-value`} aria-haspopup="listbox" aria-expanded={open} aria-controls={open ? `${id}-list` : undefined} disabled={disabled} onClick={() => { setRect(triggerRef.current?.getBoundingClientRect() || null); setFocusIndex(Math.max(0, visible.findIndex((option) => option.value === value))); setOpen((current) => !current); }}><span id={`${id}-value`}>{selected?.label || placeholder}</span><ChevronDown size={16} aria-hidden="true" /></button>
    {open && rect && createPortal(<div ref={popupRef} className="search-select-popup" style={{ left: Math.max(8, rect.left), width: Math.min(Math.max(rect.width, 220), window.innerWidth - 16), top: showAbove ? Math.max(8, rect.top - popupHeight - 4) : rect.bottom + 4, maxHeight: popupHeight }} onKeyDown={onKeys}>
      {searchable && <label className="search-select-search"><Search size={15} aria-hidden="true" /><input ref={inputRef} type="search" value={query} onChange={(event) => { setQuery(event.target.value); setFocusIndex(0); }} placeholder="搜索选项" aria-label={`搜索${label}`} /></label>}
      <div id={`${id}-list`} role="listbox" aria-labelledby={`${id}-label`} className="search-select-options">
        {visible.length ? visible.map((option, index) => <button key={option.value} ref={(element) => { optionsRef.current[index] = element; }} type="button" role="option" aria-selected={option.value === value} disabled={option.disabled} className="search-select-option" onFocus={() => setFocusIndex(index)} onClick={() => choose(option)}><span><strong>{option.label}</strong>{option.detail && <small>{option.detail}</small>}</span>{option.value === value && <Check size={15} aria-hidden="true" />}</button>) : <div className="search-select-empty">没有匹配的选项</div>}
      </div>
    </div>, document.body)}
  </div>;
};
