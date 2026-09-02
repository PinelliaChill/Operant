import { Component, ErrorInfo, ReactNode } from 'react';
import { AlertOctagon, RotateCcw } from 'lucide-react';

interface Props {
  children: ReactNode;
  fallbackTitle?: string;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  public state: State = {
    hasError: false,
    error: null,
  };

  public static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  public componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error('Uncaught error in Operant UI boundary:', error, errorInfo);
  }

  private handleReset = () => {
    this.setState({ hasError: false, error: null });
  };

  public render() {
    if (this.state.hasError) {
      return (
        <div
          style={{
            padding: 32,
            margin: 16,
            borderRadius: 'var(--radius-md)',
            backgroundColor: 'var(--status-error-bg)',
            border: '1px solid var(--status-error-border)',
            color: 'var(--status-error-text)',
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'flex-start',
            gap: 12,
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontWeight: 600, fontSize: '15px' }}>
            <AlertOctagon size={20} />
            <span>{this.props.fallbackTitle || '此视图发生渲染错误'}</span>
          </div>
          <p style={{ fontSize: '13px', lineHeight: 1.5, fontFamily: 'var(--font-mono)' }}>
            {this.state.error?.message || '未知运行时异常'}
          </p>
          <button onClick={this.handleReset} className="btn btn-secondary btn-sm" style={{ marginTop: 4 }}>
            <RotateCcw size={14} />
            <span>重试</span>
          </button>
        </div>
      );
    }

    return this.props.children;
  }
}
