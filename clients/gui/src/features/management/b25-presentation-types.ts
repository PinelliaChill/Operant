import type {
  GovernanceState, HistoryPage, HistoryDetail, ExactProposal, B25Command,
} from '../../../../../sdk/typescript-client/b2_5.generated';

export interface B25PresentationProps {
  state: GovernanceState | null;
  history: HistoryPage | null;
  detail: HistoryDetail | null;
  selected: ExactProposal[];
  query: string;
  loading: boolean;
  busy: boolean;
  readOnly: boolean;
  error: string | null;
  notice: string | null;
  modelProfiles: { id: string; name: string; model_id: string }[];
  onQueryChange: (query: string) => void;
  onSearch: (more?: boolean) => void;
  onExpand: (itemId: string) => void;
  onRefresh: () => void;
  onSelectionChange: (selection: ExactProposal[]) => void;
  onCommand: (command: B25Command) => void;
}
