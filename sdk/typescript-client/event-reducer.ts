/**
 * Operant 2.0 Event Reducer & Stream Accumulator
 * Deduplicates events by sequence, manages replay from EventCursor,
 * and maintains consistent projection state across reconnection boundaries.
 */

import { AnyOperantEvent, EventCursor } from '../protocol';

export interface AccumulatedStreamState {
  lastSequence: number;
  lastEventId?: string;
  lastTimestamp?: string;
  isStreaming: boolean;
  activeTurnIndex: number;
  accumulatedText: string;
  events: AnyOperantEvent[];
  toolCallsInProgress: Map<string, { toolName: string; args: Record<string, unknown>; startedAt: string }>;
}

export class EventReducer {
  private state: AccumulatedStreamState;
  private seenEventIds: Set<string> = new Set();

  constructor(initialCursor?: EventCursor) {
    this.state = {
      lastSequence: initialCursor?.sequence ?? 0,
      lastEventId: initialCursor?.event_id,
      lastTimestamp: initialCursor?.timestamp,
      isStreaming: false,
      activeTurnIndex: 0,
      accumulatedText: '',
      events: [],
      toolCallsInProgress: new Map(),
    };
  }

  get cursor(): EventCursor {
    return {
      sequence: this.state.lastSequence,
      event_id: this.state.lastEventId,
      timestamp: this.state.lastTimestamp,
    };
  }

  getState(): Readonly<AccumulatedStreamState> {
    return this.state;
  }

  reset(cursor?: EventCursor): void {
    this.seenEventIds.clear();
    this.state = {
      lastSequence: cursor?.sequence ?? 0,
      lastEventId: cursor?.event_id,
      lastTimestamp: cursor?.timestamp,
      isStreaming: false,
      activeTurnIndex: 0,
      accumulatedText: '',
      events: [],
      toolCallsInProgress: new Map(),
    };
  }

  reduce(event: AnyOperantEvent): AccumulatedStreamState {
    // 1. Deduplication by ID
    if (event.id && this.seenEventIds.has(event.id)) {
      return this.state;
    }
    if (event.id) {
      this.seenEventIds.add(event.id);
    }

    // 2. Sequence ordering
    if (event.sequence && event.sequence > this.state.lastSequence) {
      this.state.lastSequence = event.sequence;
    }
    this.state.lastEventId = event.id;
    this.state.lastTimestamp = event.occurred_at;
    this.state.events.push(event);

    // 3. State transitions based on standard 2.0 events
    switch (event.event_type) {
      case 'agent.started':
        this.state.isStreaming = true;
        this.state.accumulatedText = '';
        break;

      case 'model.delta': {
        const payload = event.payload as { delta_text?: string; turn_index?: number };
        if (payload.delta_text) {
          this.state.accumulatedText += payload.delta_text;
        }
        if (payload.turn_index !== undefined) {
          this.state.activeTurnIndex = payload.turn_index;
        }
        break;
      }

      case 'tool.started': {
        const payload = event.payload as { tool_call_id: string; tool_name: string; arguments: Record<string, unknown> };
        this.state.toolCallsInProgress.set(payload.tool_call_id, {
          toolName: payload.tool_name,
          args: payload.arguments,
          startedAt: event.occurred_at,
        });
        break;
      }

      case 'tool.completed':
      case 'tool.failed': {
        const payload = event.payload as { tool_call_id: string };
        this.state.toolCallsInProgress.delete(payload.tool_call_id);
        break;
      }

      case 'agent.completed':
      case 'agent.failed':
      case 'agent.cancelled':
      case 'agent.timed_out':
        this.state.isStreaming = false;
        break;
    }

    return this.state;
  }
}
