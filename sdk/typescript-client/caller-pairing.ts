/** Browser/PWA facade for the independent caller-pairing.v1 permission domain. */
import {
  CallerPairingClient,
  type CallerCommand,
  type CallerEncryptedReply,
  type CallerPairRequest,
} from './caller_pairing.generated';
import {
  fetchCallerPairingTransport,
  type CallerPairingTransport,
} from './caller-pairing-transport';

const allowed = new Set([
  'GET /v1/protocol/caller-pairing',
  'POST /v1/local-callers/pair',
  'POST /v1/local-callers/commands',
  'POST /v1/local-callers/requests/readback',
]);

function trustedLoopbackOrigin(value: string): string {
  const url = new URL(value);
  if (url.protocol !== 'http:' || !['127.0.0.1', '[::1]'].includes(url.hostname)
    || !/^\d+$/.test(url.port) || url.username || url.password
    || url.pathname !== '/' || url.search || url.hash) {
    throw new Error('桌面 Core 地址无效，请重新复制桌面配对信息。');
  }
  return url.origin;
}

export class PairedCallerPairingClient {
  private readonly raw: CallerPairingClient;

  constructor(baseUrl: string, transport: CallerPairingTransport = fetchCallerPairingTransport) {
    const origin = trustedLoopbackOrigin(baseUrl);
    const scoped: CallerPairingTransport = async (request) => {
      const url = new URL(request.url);
      if (url.origin !== origin || request.path !== url.pathname || url.search || url.hash
        || !allowed.has(`${request.method.toUpperCase()} ${url.pathname}`)) {
        throw new Error('当前连接只允许配对和技能来源操作。');
      }
      return transport(request);
    };
    this.raw = new CallerPairingClient(origin, scoped);
  }

  pairLocalCaller(request: CallerPairRequest): Promise<CallerEncryptedReply> {
    return this.raw.pairLocalCaller(request, { idempotencyKey: request.pair_request_id });
  }

  executeCallerCommand(request: CallerCommand): Promise<CallerEncryptedReply> {
    return this.raw.executeCallerCommand(request, { idempotencyKey: request.request_id });
  }

  readCallerRequest(request: CallerCommand): Promise<CallerEncryptedReply> {
    if (request.operation !== 'readback') throw new Error('只允许核对原请求。');
    return this.raw.readCallerRequest(request, { idempotencyKey: request.request_id });
  }
}

export type { CallerCommand, CallerEncryptedReply, CallerPairRequest };
