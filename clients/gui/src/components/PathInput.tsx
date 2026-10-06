import React, { useEffect, useId, useRef, useState } from 'react';
import { FolderOpen } from 'lucide-react';
import { browseLocalPath, canBrowseLocalPaths, PathSelectionError, type PathKind } from '../lib/nativePaths';
import './path-input.css';

type Props = Omit<React.InputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange'> & {
  value: string;
  onChange: (value: string) => void;
  label?: string;
  kind?: PathKind;
  relativeTo?: string;
  within?: string;
};

export const PathInput: React.FC<Props> = ({ value, onChange, label, kind = 'directory', relativeTo, within, disabled, id, className = 'input', ...inputProps }) => {
  const generatedId = useId();
  const inputId = id ?? generatedId;
  const [choosing, setChoosing] = useState(false);
  const [error, setError] = useState('');
  const active = useRef(false);
  const latest = useRef({ value, relativeTo, within, disabled });
  latest.current = { value, relativeTo, within, disabled };
  const button = useRef<HTMLButtonElement>(null);
  const wasChoosing = useRef(false);
  const pending = useRef(false);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  useEffect(() => {
    if (wasChoosing.current && !choosing) button.current?.focus();
    wasChoosing.current = choosing;
  }, [choosing]);
  const supported = canBrowseLocalPaths();
  const choose = async () => {
    if (pending.current || disabled || !supported) return;
    pending.current = true;
    const initial = { value, relativeTo, within };
    setChoosing(true); setError('');
    try {
      const selected = await browseLocalPath(kind, value, relativeTo, within);
      if (active.current && !latest.current.disabled && latest.current.value === initial.value && latest.current.relativeTo === initial.relativeTo && latest.current.within === initial.within && selected !== null) onChange(selected);
    } catch (cause) {
      if (active.current) setError(cause instanceof PathSelectionError ? cause.message : '无法选择路径，请重试或手动填写。');
    } finally {
      pending.current = false;
      if (active.current) setChoosing(false);
    }
  };
  const describedBy = [inputProps['aria-describedby'], error ? `${inputId}-error` : undefined].filter(Boolean).join(' ') || undefined;
  return <div className="path-field">
    {label && <label htmlFor={inputId}>{label}</label>}
    <div className="path-input-row" aria-busy={choosing}>
      <input {...inputProps} id={inputId} className={className} type="text" value={value} disabled={disabled || choosing} aria-describedby={describedBy} aria-invalid={error ? true : inputProps['aria-invalid']} onChange={(event) => { setError(''); onChange(event.target.value); }} />
      <button ref={button} type="button" className="btn btn-secondary path-browse" onClick={() => void choose()} disabled={disabled || choosing || !supported} aria-label={`浏览${label || inputProps['aria-label'] || (kind === 'directory' ? '文件夹' : '文件')}`} title={supported ? (kind === 'directory' ? '选择本机文件夹' : '选择本机文件') : '请在桌面版浏览本机路径，或直接填写完整路径。'}><FolderOpen size={16} aria-hidden="true" />{choosing ? '选择中…' : '浏览…'}</button>
    </div>
    {error && <p className="path-input-error" id={`${inputId}-error`} role="alert">{error}</p>}
  </div>;
};
