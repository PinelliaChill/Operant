/** Phase 5B/6 aliases over the frozen, bounded Phase 1E wire transport. */

export {
  Phase1EError as Phase56Error,
  fetchPhase1ETransport as fetchPhase56Transport,
  parseCursor,
  parseSse,
  readJson,
  readText,
  responseHeaders,
  stringifyJson,
} from './phase1e-transport';

export type {
  Phase1EBody as Phase56Body,
  Phase1ERequest as Phase56Request,
  Phase1EResponse as Phase56Response,
  Phase1ETransport as Phase56Transport,
} from './phase1e-transport';
