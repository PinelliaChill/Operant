import {
  PairedCallerPairingClient,
  type CallerCommand,
  type CallerEncryptedReply,
  type CallerPairRequest,
} from '../../../../sdk/typescript-client/caller-pairing';
import type { CallerPairingClientLike, CallerEncryptedReply as LocalReply } from './pairedSkillSourceTransport';

/** Only envelopes assembled by pairingCrypto reach the generated SDK. */
export function createPairedBrowserClient(baseUrl: string): CallerPairingClientLike {
  const client = new PairedCallerPairingClient(baseUrl);
  return {
    pairLocalCaller: (envelope) => client.pairLocalCaller(envelope as unknown as CallerPairRequest) as Promise<CallerEncryptedReply> as Promise<LocalReply>,
    executeCallerCommand: (envelope) => client.executeCallerCommand(envelope as unknown as CallerCommand) as Promise<CallerEncryptedReply> as Promise<LocalReply>,
    readCallerRequest: (envelope) => client.readCallerRequest(envelope as unknown as CallerCommand) as Promise<CallerEncryptedReply> as Promise<LocalReply>,
  };
}
