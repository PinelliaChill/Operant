/** B23 aliases over the frozen, bounded Phase 1E wire transport. */

export {
  Phase1EError as B23Error,
  fetchPhase1ETransport as fetchB23Transport,
  parseCursor,
  parseSse,
  readJson,
  readText,
  responseHeaders,
  stringifyJson,
} from './phase1e-transport';

export type {
  Phase1EBody as B23Body,
  Phase1ERequest as B23Request,
  Phase1EResponse as B23Response,
  Phase1ETransport as B23Transport,
} from './phase1e-transport';
