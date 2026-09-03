/** Phase 4/5A aliases over the frozen, bounded Phase 1E wire transport. */

export {
  Phase1EError as Phase45Error,
  fetchPhase1ETransport as fetchPhase45Transport,
  parseCursor,
  parseSse,
  readJson,
  readText,
  responseHeaders,
  stringifyJson,
} from './phase1e-transport';

export type {
  Phase1EBody as Phase45Body,
  Phase1ERequest as Phase45Request,
  Phase1EResponse as Phase45Response,
  Phase1ETransport as Phase45Transport,
} from './phase1e-transport';
