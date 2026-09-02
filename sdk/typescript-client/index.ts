/**
 * Operant 2.0 TypeScript SDK Index
 * Primary export point for clients, reducers, and protocol models
 */

export * from '../protocol';
export * from './client';
export * from './http-client';
export * from './mock-client';
export * from './event-reducer';

// The Phase 1E live surface is generated from the OpenAPI Schema. The legacy
// exports above remain available only for the existing demo/Mock client until
// the GUI line switches its provider explicitly.
export { Phase1EClient, ProtocolNegotiationError } from './phase1e.generated';
export * as Phase1E from './phase1e.generated';
export {
  MAX_SSE_DATA_BYTES,
  MAX_SSE_FRAME_BYTES,
  MAX_SSE_LINE_BYTES,
  Phase1EError,
  SseProtocolError,
  parseCursor,
  parseSse,
} from './phase1e-transport';
export type {
  Phase1EBody,
  Phase1ERequest,
  Phase1EResponse,
  Phase1ETransport,
  RawSseFrame,
} from './phase1e-transport';
