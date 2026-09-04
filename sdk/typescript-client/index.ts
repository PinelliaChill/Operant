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

// Phase 2 Graph Runtime and Phase 3 local Team Runtime are additive. Phase 1E
// remains available unchanged for existing live clients.
export {
  Phase23Client,
  ProtocolNegotiationError as Phase23ProtocolNegotiationError,
} from './phase23.generated';
export * as Phase23 from './phase23.generated';
export type {
  Phase23Body,
  Phase23Request,
  Phase23Response,
  Phase23Transport,
} from './phase23-transport';

// Phase 4 security is additive and keeps the prior protocol clients frozen.
export {
  Phase45Client,
  ProtocolNegotiationError as Phase45ProtocolNegotiationError,
} from './phase45.generated';
export * as Phase45 from './phase45.generated';
export type {
  Phase45Body,
  Phase45Request,
  Phase45Response,
  Phase45Transport,
} from './phase45-transport';
