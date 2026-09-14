/** B24 aliases over the frozen, bounded Phase 1E wire transport. */

export {
  Phase1EError as B24Error,
  fetchPhase1ETransport as fetchB24Transport,
  parseCursor,
  parseSse,
  readJson,
  readText,
  responseHeaders,
  stringifyJson,
} from './phase1e-transport';

export type {
  Phase1EBody as B24Body,
  Phase1ERequest as B24Request,
  Phase1EResponse as B24Response,
  Phase1ETransport as B24Transport,
} from './phase1e-transport';
