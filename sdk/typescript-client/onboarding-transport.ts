/** Setup uses the established fail-closed transport. */
export {
  Phase1EError as OnboardingError,
  fetchPhase1ETransport as fetchOnboardingTransport,
  parseCursor, readJson, readText, responseHeaders, stringifyJson,
} from './phase1e-transport';
export type {
  Phase1ERequest as OnboardingRequest,
  Phase1EResponse as OnboardingResponse,
  Phase1ETransport as OnboardingTransport,
} from './phase1e-transport';
