import type { Phase45Client, Phase45 } from '@operant/sdk';

export type ScheduleCreateInput = Phase45.ScheduleDefinition;
export type ScheduleStatusInput = Phase45.ScheduleStatusBody;

/** Minimal generated-client surface used by the Scheduler page and its tests. */
export type GeneratedSchedulerClient = Pick<Phase45Client,
  | 'negotiateProtocol'
  | 'listSchedules'
  | 'listSchedulerQueue'
  | 'listDeadLetter'
  | 'createSchedule'
  | 'setScheduleStatus'
  | 'triggerSchedule'
  | 'replayDeadLetter'
>;

/**
 * Page-local dependency boundary over the generated Phase45Client.
 *
 * It intentionally contains no HTTP paths, fetch calls, protocol validation,
 * or error translation; those remain owned by the generated client/transport.
 */
export class SchedulerClient {
  private readonly generated: GeneratedSchedulerClient;

  constructor(generated: GeneratedSchedulerClient) {
    this.generated = generated;
  }

  negotiateProtocol(force = false): Promise<Phase45.ProtocolNegotiation> {
    return this.generated.negotiateProtocol(force);
  }

  listSchedules(): Promise<Record<string, unknown>> {
    return this.generated.listSchedules();
  }

  listQueue(): Promise<Record<string, unknown>> {
    return this.generated.listSchedulerQueue();
  }

  listDeadLetter(): Promise<Record<string, unknown>> {
    return this.generated.listDeadLetter();
  }

  createSchedule(input: ScheduleCreateInput, idempotencyKey: string): Promise<Record<string, unknown>> {
    return this.generated.createSchedule(input, { idempotencyKey });
  }

  setScheduleStatus(scheduleId: string, input: ScheduleStatusInput, idempotencyKey: string): Promise<Record<string, unknown>> {
    return this.generated.setScheduleStatus(scheduleId, input, { idempotencyKey });
  }

  triggerSchedule(scheduleId: string, idempotencyKey: string): Promise<Record<string, unknown>> {
    return this.generated.triggerSchedule(
      scheduleId,
      { idempotency_key: idempotencyKey },
      { idempotencyKey },
    );
  }

  replayDeadLetter(requestId: string, idempotencyKey: string): Promise<Record<string, unknown>> {
    return this.generated.replayDeadLetter(
      requestId,
      { idempotency_key: idempotencyKey },
      { idempotencyKey },
    );
  }
}
