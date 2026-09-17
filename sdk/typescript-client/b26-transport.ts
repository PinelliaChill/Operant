/** B2-6 shares the established transport and explicit failure semantics. */
export {
  Phase1EError as B26Error,
  fetchPhase1ETransport as fetchB26Transport,
  parseCursor, readJson, readText, responseHeaders, stringifyJson,
} from './phase1e-transport';
export type {
  Phase1ERequest as B26Request,
  Phase1EResponse as B26Response,
  Phase1ETransport as B26Transport,
} from './phase1e-transport';
