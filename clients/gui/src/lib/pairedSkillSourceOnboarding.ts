import { OnboardingClient } from '../../../../sdk/typescript-client/onboarding.generated.ts';
import { PairedSkillSourceSession } from './pairedSkillSourceTransport.ts';
import { createPairedSkillSourceTransport } from './pairedSkillSourceOnboardingTransport.ts';

/** The formal Onboarding SDK is reused for exactly three skill-source operations. */
export function createPairedSkillSourceClient(session: PairedSkillSourceSession): OnboardingClient {
  return new OnboardingClient(session.identity.baseUrl, createPairedSkillSourceTransport(session));
}
