/** B25 aliases over the frozen, bounded Phase 1E wire transport. */

export {
  Phase1EError as B25Error,
  fetchPhase1ETransport as fetchB25Transport,
  parseCursor,
  parseSse,
  readJson,
  readText,
  responseHeaders,
  stringifyJson,
} from './phase1e-transport';

export type {
  Phase1EBody as B25Body,
  Phase1ERequest as B25Request,
  Phase1EResponse as B25Response,
  Phase1ETransport as B25Transport,
} from './phase1e-transport';
