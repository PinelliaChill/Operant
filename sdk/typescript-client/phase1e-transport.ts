/**
 * Hand-written Phase 1E transport and SSE plumbing.
 *
 * Request/response models and the public client are generated from the
 * OpenAPI Schema. This file deliberately contains only runtime adapters.
 */

export type HeaderMap = Record<string, string | undefined>;

export type Phase1EBody =
  | string
  | Uint8Array
  | AsyncIterable<string | Uint8Array>
  | ReadableStream<Uint8Array>
  | null
  | undefined;

export type Phase1ERawText = string | (() => Promise<string> | string);

export interface Phase1ERequest {
  method: string;
  url: string;
  headers?: Record<string, string>;
  body?: string;
  /** The generated client keeps these wire fields for transport test doubles. */
  path?: string;
  query?: Record<string, string | undefined>;
}

export interface Phase1EResponse {
  status: number;
  headers?: Headers | Record<string, string | string[] | undefined>;
  body?: Phase1EBody;
  /** JSON must remain raw text until parseJsonLossless runs. */
  json?: Phase1ERawText;
  text?: Phase1ERawText;
}

export type Phase1ETransport = (
  request: Phase1ERequest,
) => Promise<Phase1EResponse>;

export interface RawSseFrame {
  id?: string;
  event?: string;
  data: unknown;
}

const MAX_CURSOR = 9223372036854775807n;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

export function parseCursor(value: unknown): number | bigint {
  if (typeof value === 'bigint') {
    if (value < 0n || value > MAX_CURSOR) throw new RangeError('cursor is outside the Phase 1E range');
    return value;
  }
  if (typeof value === 'number') {
    if (!Number.isSafeInteger(value) || value < 0 || BigInt(value) > MAX_CURSOR) {
      throw new RangeError('cursor must be an integer between 0 and 2^63-1');
    }
    return value;
  }
  if (typeof value !== 'string' || !/^\d+$/.test(value)) {
    throw new TypeError('cursor must be a decimal integer');
  }
  const parsed = BigInt(value);
  if (parsed < 0n || parsed > MAX_CURSOR) throw new RangeError('cursor is outside the Phase 1E range');
  return parsed <= BigInt(Number.MAX_SAFE_INTEGER) ? Number(parsed) : parsed;
}

export function responseHeaders(
  headers: Headers | Record<string, string | string[] | undefined> | undefined,
): HeaderMap {
  const result: HeaderMap = {};
  if (!headers) return result;
  if (typeof Headers !== 'undefined' && headers instanceof Headers) {
    headers.forEach((value, key) => {
      result[key.toLowerCase()] = value;
    });
    return result;
  }
  for (const [key, value] of Object.entries(headers)) {
    if (value === undefined) continue;
    result[key.toLowerCase()] = Array.isArray(value) ? value.join(', ') : value;
  }
  return result;
}

async function bodyText(body: Phase1EBody): Promise<string> {
  if (body === undefined || body === null) return '';
  if (typeof body === 'string') return body;
  if (body instanceof Uint8Array) return new TextDecoder().decode(body);
  let text = '';
  const decoder = new TextDecoder();
  for await (const chunk of await bodyToAsyncIterable(body)) {
    if (typeof chunk === 'string') {
      text += decoder.decode();
      text += chunk;
    } else {
      text += decoder.decode(chunk, { stream: true });
    }
  }
  text += decoder.decode();
  return text;
}

async function bodyToAsyncIterable(
  body: Exclude<Phase1EBody, string | Uint8Array | null | undefined>,
): Promise<AsyncIterable<string | Uint8Array>> {
  if (typeof ReadableStream !== 'undefined' && body instanceof ReadableStream) {
    const reader = body.getReader();
    return (async function* (): AsyncGenerator<Uint8Array> {
      try {
        while (true) {
          const next = await reader.read();
          if (next.done) return;
          yield next.value;
        }
      } finally {
        reader.releaseLock();
      }
    })();
  }
  return body as AsyncIterable<string | Uint8Array>;
}

const MAX_SAFE_BIGINT = BigInt(Number.MAX_SAFE_INTEGER);

class LosslessJsonParser {
  private index = 0;

  constructor(private readonly source: string) {}

  parse(): unknown {
    const value = this.value();
    this.whitespace();
    if (this.index !== this.source.length) throw new SyntaxError('unexpected JSON input');
    return value;
  }

  private whitespace(): void {
    while (this.index < this.source.length && /\s/.test(this.source[this.index] ?? '')) this.index += 1;
  }

  private value(): unknown {
    this.whitespace();
    const character = this.source[this.index];
    if (character === '"') return this.string();
    if (character === '{') return this.object();
    if (character === '[') return this.array();
    if (this.source.startsWith('true', this.index)) { this.index += 4; return true; }
    if (this.source.startsWith('false', this.index)) { this.index += 5; return false; }
    if (this.source.startsWith('null', this.index)) { this.index += 4; return null; }
    if (character === '-' || (character !== undefined && /\d/.test(character))) return this.number();
    throw new SyntaxError(`invalid JSON at offset ${this.index}`);
  }

  private string(): string {
    const start = this.index;
    this.index += 1;
    let escaped = false;
    while (this.index < this.source.length) {
      const character = this.source[this.index++];
      if (escaped) { escaped = false; continue; }
      if (character === '\\') { escaped = true; continue; }
      if (character === '"') return JSON.parse(this.source.slice(start, this.index)) as string;
    }
    throw new SyntaxError('unterminated JSON string');
  }

  private number(): number | bigint {
    const match = this.source.slice(this.index).match(/^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/);
    if (!match) throw new SyntaxError(`invalid JSON number at offset ${this.index}`);
    const token = match[0];
    this.index += token.length;
    if (!/[.eE]/.test(token)) {
      const integer = BigInt(token);
      if (integer >= -MAX_SAFE_BIGINT && integer <= MAX_SAFE_BIGINT) return Number(token);
      return integer;
    }
    const parsed = Number(token);
    if (!Number.isFinite(parsed)) throw new SyntaxError('JSON number is not finite');
    return parsed;
  }

  private object(): Record<string, unknown> {
    this.index += 1;
    const result: Record<string, unknown> = {};
    this.whitespace();
    if (this.source[this.index] === '}') { this.index += 1; return result; }
    while (true) {
      this.whitespace();
      if (this.source[this.index] !== '"') throw new SyntaxError('JSON object key must be a string');
      const key = this.string();
      this.whitespace(); this.expect(':');
      result[key] = this.value();
      this.whitespace();
      if (this.source[this.index] === '}') { this.index += 1; return result; }
      this.expect(',');
    }
  }

  private array(): unknown[] {
    this.index += 1;
    const result: unknown[] = [];
    this.whitespace();
    if (this.source[this.index] === ']') { this.index += 1; return result; }
    while (true) {
      result.push(this.value());
      this.whitespace();
      if (this.source[this.index] === ']') { this.index += 1; return result; }
      this.expect(',');
    }
  }

  private expect(character: string): void {
    if (this.source[this.index] !== character) throw new SyntaxError(`expected ${character}`);
    this.index += 1;
  }
}

/** Parse JSON while promoting integer tokens outside the safe number range to bigint. */
export function parseJsonLossless(text: string): unknown {
  return new LosslessJsonParser(text).parse();
}

/** Serialize request payloads without allowing bigint values to throw or round. */
export function stringifyJson(value: unknown): string {
  const active = new WeakSet<object>();
  const encode = (current: unknown, inArray = false): string => {
    if (current === null) return 'null';
    if (typeof current === 'bigint') return current.toString();
    if (typeof current === 'string') return JSON.stringify(current);
    if (typeof current === 'boolean') return current ? 'true' : 'false';
    if (typeof current === 'number') {
      if (!Number.isFinite(current)) throw new TypeError('JSON number must be finite');
      if (Number.isInteger(current) && !Number.isSafeInteger(current)) throw new TypeError('unsafe integer cannot be serialized losslessly');
      return JSON.stringify(current);
    }
    if (current === undefined || typeof current === 'function' || typeof current === 'symbol') {
      return inArray ? 'null' : '';
    }
    if (typeof current !== 'object') throw new TypeError('unsupported JSON value');
    if (active.has(current)) throw new TypeError('cannot serialize a cyclic JSON value');
    active.add(current);
    let result: string;
    if (Array.isArray(current)) {
      result = `[${current.map(item => encode(item, true)).join(',')}]`;
    } else {
      const entries = Object.entries(current)
        .map(([key, item]) => [key, encode(item)] as const)
        .filter(([, encoded]) => encoded !== '')
        .map(([key, encoded]) => `${JSON.stringify(key)}:${encoded}`);
      result = `{${entries.join(',')}}`;
    }
    active.delete(current);
    return result;
  };
  return encode(value);
}

export async function readJson<T>(response: Phase1EResponse): Promise<T> {
  if (typeof response.json === 'function') {
    const value = await response.json();
    if (typeof value !== 'string') throw new Phase1EError('preparsed_json_unsupported', 'Transport must return raw JSON text', false, 'none');
    return parseJsonLossless(value) as T;
  }
  if (response.json !== undefined) {
    return parseJsonLossless(response.json) as T;
  }
  const text = typeof response.text === 'function' ? await response.text() : response.text ?? await bodyText(response.body);
  if (typeof text !== 'string') throw new Phase1EError('raw_text_required', 'Transport must return raw text', false, 'none');
  if (!text.trim()) return undefined as T;
  return parseJsonLossless(text) as T;
}

export async function readText(response: Phase1EResponse): Promise<string> {
  if (typeof response.text === 'function') {
    const value = await response.text();
    if (typeof value !== 'string') throw new Phase1EError('raw_text_required', 'Transport must return raw text', false, 'none');
    return value;
  }
  if (response.text !== undefined) return response.text;
  return bodyText(response.body);
}

function dispatchSseFrame(
  fields: { id?: string; event?: string; data: string[] },
): RawSseFrame | null {
  if (fields.data.length === 0) return null;
  const rawData = fields.data.join('\n');
  let data: unknown = rawData;
  try {
    data = parseJsonLossless(rawData);
  } catch {
    // A malformed data field remains visible to the caller as text. The
    // typed client never interprets it as a successful protocol event.
  }
  return { id: fields.id, event: fields.event, data };
}

/** Parse standard SSE id/event/data fields, including multiline data. */
export async function* parseSse(body: Phase1EBody): AsyncGenerator<RawSseFrame> {
  const source = bodyToSseIterable(body);
  let fields: { id?: string; event?: string; data: string[] } = { data: [] };
  let line = '';
  let pendingCarriageReturn = false;
  const consumeLine = (value: string): RawSseFrame | null => {
    if (value === '') {
      const frame = dispatchSseFrame(fields);
      fields = { data: [] };
      return frame;
    }
    if (value.startsWith(':')) return null;
    const separator = value.indexOf(':');
    const name = separator < 0 ? value : value.slice(0, separator);
    let fieldValue = separator < 0 ? '' : value.slice(separator + 1);
    if (fieldValue.startsWith(' ')) fieldValue = fieldValue.slice(1);
    if (name === 'id') fields.id = fieldValue;
    else if (name === 'event') fields.event = fieldValue;
    else if (name === 'data') fields.data.push(fieldValue);
    return null;
  };
  for await (const chunk of source) {
    for (const character of chunk) {
      if (pendingCarriageReturn) {
        if (character === '\n') {
          const frame = consumeLine(line);
          if (frame) yield frame;
          line = '';
          pendingCarriageReturn = false;
          continue;
        }
        const frame = consumeLine(line);
        if (frame) yield frame;
        line = '';
        pendingCarriageReturn = false;
      }
      if (character === '\r') pendingCarriageReturn = true;
      else if (character === '\n') {
        const frame = consumeLine(line);
        if (frame) yield frame;
        line = '';
      } else line += character;
    }
  }
  if (pendingCarriageReturn) {
    const frame = consumeLine(line);
    if (frame) yield frame;
  } else if (line.length > 0) {
    const frame = consumeLine(line);
    if (frame) yield frame;
  }
  const frame = dispatchSseFrame(fields);
  if (frame) yield frame;
}

async function* bodyToSseIterable(body: Phase1EBody): AsyncGenerator<string> {
  if (body === undefined || body === null) return;
  if (typeof body === 'string') {
    yield body;
    return;
  }
  if (body instanceof Uint8Array) {
    yield new TextDecoder().decode(body);
    return;
  }
  const decoder = new TextDecoder();
  for await (const chunk of await bodyToAsyncIterable(body)) {
    if (typeof chunk === 'string') {
      yield decoder.decode();
      yield chunk;
    } else {
      yield decoder.decode(chunk, { stream: true });
    }
  }
  const tail = decoder.decode();
  if (tail) yield tail;
}

export class Phase1EError extends Error {
  readonly code: string;
  readonly retryable: boolean;
  readonly recovery: string;
  readonly detail: unknown;

  constructor(
    code: string,
    message: string,
    retryable = false,
    recovery = 'none',
    detail?: unknown,
  ) {
    super(message);
    this.name = 'Phase1EError';
    this.code = code;
    this.retryable = retryable;
    this.recovery = recovery;
    this.detail = detail;
  }

  static async fromResponse(response: Phase1EResponse): Promise<Phase1EError> {
    const validRecoveries = new Set([
      'none',
      'retry',
      'retry_later',
      'retry_same_idempotency_key',
      'use_new_idempotency_key',
      'refresh_and_retry',
      'manual_reconcile',
    ]);
    let payload: unknown;
    try {
      payload = await readJson<unknown>(response);
    } catch {
      payload = undefined;
    }
    if (isRecord(payload) && isRecord(payload.error)) {
      const error = payload.error;
      if (
        Object.prototype.hasOwnProperty.call(payload, 'detail') &&
        typeof error.code === 'string' && error.code.length > 0 &&
        typeof error.message === 'string' && error.message.length > 0 &&
        typeof error.retryable === 'boolean' &&
        typeof error.recovery === 'string' && validRecoveries.has(error.recovery)
      ) {
        return new Phase1EError(error.code, error.message, error.retryable, error.recovery, payload.detail);
      }
    }
    return new Phase1EError('invalid_error_envelope', 'Core returned an invalid error envelope', false, 'none', payload);
  }
}

export const fetchPhase1ETransport: Phase1ETransport = async (
  request: Phase1ERequest,
): Promise<Phase1EResponse> => {
  const response = await fetch(request.url, {
    method: request.method,
    headers: request.headers,
    body: request.body,
  });
  return {
    status: response.status,
    headers: response.headers,
    body: response.body,
    text: () => response.text(),
  };
};
