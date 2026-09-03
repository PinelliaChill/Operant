import { Phase45Client } from '@operant/sdk';
import { currentBrowserOrigin } from '../lib/liveBaseUrl';
import { SchedulerClient } from './schedulerClient';

/** Create the Scheduler adapter with the one generated Phase 4/5 client. */
export function createSchedulerClient(): SchedulerClient {
  return new SchedulerClient(new Phase45Client(currentBrowserOrigin()));
}
