/** Presentation-only contract. Core state and command construction belong to Codex. */
export interface B26Field {
  key: string;
  label: string;
  kind: 'text' | 'textarea' | 'select' | 'number' | 'checkbox';
  required?: boolean;
  hint?: string;
  value?: string;
  options?: { value: string; label: string }[];
  min?: number;
  max?: number;
}
export interface B26Action {
  id: string;
  title: string;
  description: string;
  confirmLabel: string;
  fields: B26Field[];
  disabled?: boolean;
  disabledReason?: string;
  dangerous?: boolean;
}
export interface B26Card {
  id: string;
  title: string;
  status: string;
  description?: string;
  facts: { label: string; value: string }[];
  warning?: string;
}
export interface B26Section {
  id: 'skills' | 'writers' | 'sharing' | 'remote';
  title: string;
  description: string;
  emptyMessage: string;
  cards: B26Card[];
  actions: B26Action[];
}
export interface B26PresentationProps {
  sections: B26Section[];
  loading: boolean;
  busy: boolean;
  readOnly: boolean;
  error: string | null;
  notice: string | null;
  onRefresh: () => void;
  onAction: (actionId: string, values: Record<string, string>) => void;
}
