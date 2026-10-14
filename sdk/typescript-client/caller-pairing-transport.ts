/** The additive caller-pairing client uses the established lossless transport. */
export {
  Phase1EError as CallerPairingError,
  parseCursor, readJson, readText, responseHeaders, stringifyJson,
} from './phase1e-transport.ts';
export type {
  Phase1ERequest as CallerPairingRequest,
  Phase1EResponse as CallerPairingResponse,
  Phase1ETransport as CallerPairingTransport,
} from './phase1e-transport.ts';

import type { Phase1ETransport } from './phase1e-transport.ts';

/** Only use behind an exact origin/method/path allowlist. */
export const fetchCallerPairingTransport: Phase1ETransport = async (request) => {
  const response = await fetch(request.url, {
    method: request.method,
    headers: request.headers,
    body: request.body,
    redirect: 'error',
    credentials: 'omit',
    cache: 'no-store',
    referrerPolicy: 'no-referrer',
  });
  return {
    status: response.status,
    headers: response.headers,
    body: response.body,
    text: () => response.text(),
  };
};
