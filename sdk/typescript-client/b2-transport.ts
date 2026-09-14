/** B2 aliases over the frozen, bounded Phase 1E wire transport. */

export {
  Phase1EError as B2Error,
  fetchPhase1ETransport as fetchB2Transport,
  parseCursor,
  parseSse,
  readJson,
  readText,
  responseHeaders,
  stringifyJson,
} from './phase1e-transport';

export type {
  Phase1EBody as B2Body,
  Phase1ERequest as B2Request,
  Phase1EResponse as B2Response,
  Phase1ETransport as B2Transport,
} from './phase1e-transport';
