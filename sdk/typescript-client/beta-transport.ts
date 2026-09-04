/** Beta/RC aliases over the frozen, bounded Phase 1E wire transport. */

export {
  Phase1EError as BetaError,
  fetchPhase1ETransport as fetchBetaTransport,
  parseCursor,
  parseSse,
  readJson,
  readText,
  responseHeaders,
  stringifyJson,
} from './phase1e-transport';

export type {
  Phase1EBody as BetaBody,
  Phase1ERequest as BetaRequest,
  Phase1EResponse as BetaResponse,
  Phase1ETransport as BetaTransport,
} from './phase1e-transport';
