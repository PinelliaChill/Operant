/** Phase 2/3 aliases over the frozen, bounded Phase 1E wire transport. */

export {
  MAX_SSE_DATA_BYTES,
  MAX_SSE_FRAME_BYTES,
  MAX_SSE_LINE_BYTES,
  Phase1EError as Phase23Error,
  SseProtocolError,
  fetchPhase1ETransport as fetchPhase23Transport,
  parseCursor,
  parseJsonLossless,
  parseSse,
  readJson,
  readText,
  responseHeaders,
  stringifyJson,
} from './phase1e-transport';

export type {
  Phase1EBody as Phase23Body,
  Phase1ERequest as Phase23Request,
  Phase1EResponse as Phase23Response,
  Phase1ETransport as Phase23Transport,
  RawSseFrame,
} from './phase1e-transport';
