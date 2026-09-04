import React from 'react';
import { AlertTriangle, Check, FileSearch, ShieldX } from 'lucide-react';
import type { LiveMcpIntervention } from '../../live45/Phase45Context';
import type { LiveMcpReceipt } from '../../live45/phase45Adapter';

interface McpSafetyPanelsProps {
  intervention?: LiveMcpIntervention;
  receipt?: LiveMcpReceipt;
  busy: boolean;
  disconnected: boolean;
  onDecision: (approved: boolean) => void;
  onLoadReceipt: (actionHash: string) => void;
}

export const McpSafetyPanels: React.FC<McpSafetyPanelsProps> = ({
  intervention,
  receipt,
  busy,
  disconnected,
  onDecision,
  onLoadReceipt,
}) => {
  if (!intervention && !receipt) return null;
  const approvalPending = intervention?.status === 'approval_required';
  const outcomeUnknown = intervention?.status === 'outcome_unknown';
  return <div className="mcp-safety-stack" aria-live="polite">
    {intervention && <section
      className={`mcp-intervention ${outcomeUnknown ? 'mcp-intervention-danger' : 'mcp-intervention-warn'}`}
      role={outcomeUnknown ? 'alert' : 'status'}
      aria-labelledby="mcp-intervention-title"
    >
      <AlertTriangle size={18} aria-hidden="true" />
      <div className="mcp-intervention-content">
        <h2 id="mcp-intervention-title">
          {approvalPending ? '需要人工审批' : outcomeUnknown ? '调用结果未知' : '操作已拒绝'}
        </h2>
        <p>{outcomeUnknown ? '服务端可能已经执行该调用。请人工核对 Receipt 或外部系统，客户端不会自动重放。' : intervention.message}</p>
        <dl className="mcp-facts">
          {intervention.approvalId && <><dt>审批 ID</dt><dd><code>{intervention.approvalId}</code></dd></>}
          {intervention.reasonCode && <><dt>原因</dt><dd><code>{intervention.reasonCode}</code></dd></>}
          {intervention.actionHash && <><dt>Action Hash</dt><dd><code>{intervention.actionHash}</code></dd></>}
        </dl>
        <div className="mcp-intervention-actions">
          {approvalPending && <>
            <button type="button" className="btn btn-primary" disabled={busy || disconnected} onClick={() => onDecision(true)} aria-label="允许 MCP 操作并使用同一幂等键重试">
              <Check size={14} aria-hidden="true" />允许并重试
            </button>
            <button type="button" className="btn btn-danger" disabled={busy || disconnected} onClick={() => onDecision(false)} aria-label="拒绝 MCP 操作">
              <ShieldX size={14} aria-hidden="true" />拒绝
            </button>
          </>}
          {intervention.actionHash && intervention.operation.kind === 'call' && <button type="button" className="btn btn-secondary" disabled={busy || disconnected} onClick={() => onLoadReceipt(intervention.actionHash!)}>
            <FileSearch size={14} aria-hidden="true" />查看 Receipt
          </button>}
        </div>
      </div>
    </section>}
    {receipt && <section className="mcp-receipt" aria-labelledby="mcp-receipt-title">
      <h2 id="mcp-receipt-title">MCP Receipt</h2>
      <dl className="mcp-facts">
        <dt>状态</dt><dd><code>{receipt.status}</code></dd>
        <dt>服务 / 工具</dt><dd>{receipt.serverId} / {receipt.toolName}</dd>
        <dt>结果可用</dt><dd>{receipt.resultAvailable ? '是' : '否'}</dd>
        {receipt.errorCode && <><dt>错误</dt><dd><code>{receipt.errorCode}</code></dd></>}
        <dt>更新时间</dt><dd>{receipt.updatedAt}</dd>
      </dl>
    </section>}
  </div>;
};
