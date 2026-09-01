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
  json?: (() => Promise<unknown> | unknown) | unknown;
  text?: (() => Promise<string> | string) | unknown;
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
  for await (const chunk of await bodyToAsyncIterable(body)) {
    text += typeof chunk === 'string' ? chunk : new TextDecoder().decode(chunk);
  }
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

export async function readJson<T>(response: Phase1EResponse): Promise<T> {
  if (typeof response.json === 'function') {
    return (await response.json()) as T;
  }
  if (response.json !== undefined) return response.json as T;
  const text = typeof response.text === 'function'
    ? await response.text()
    : response.text !== undefined
      ? String(response.text)
      : await bodyText(response.body);
  if (!text.trim()) return undefined as T;
  return JSON.parse(text) as T;
}

export async function readText(response: Phase1EResponse): Promise<string> {
  if (typeof response.text === 'function') return await response.text();
  if (response.text !== undefined) return String(response.text);
  return bodyText(response.body);
}

function dispatchSseFrame(
  fields: { id?: string; event?: string; data: string[] },
): RawSseFrame | null {
  if (fields.data.length === 0) return null;
  const rawData = fields.data.join('\n');
  let data: unknown = rawData;
  try {
    data = JSON.parse(rawData) as unknown;
  } catch {
    // A malformed data field remains visible to the caller as text. The
    // typed client never interprets it as a successful protocol event.
  }
  return { id: fields.id, event: fields.event, data };
}

/** Parse standard SSE id/event/data fields, including multiline data. */
export async function* parseSse(body: Phase1EBody): AsyncGenerator<RawSseFrame> {
  const source = bodyToSseIterable(body);
  let buffer = '';
  let fields: { id?: string; event?: string; data: string[] } = { data: [] };
  for await (const chunk of source) {
    buffer += chunk;
    let boundary = buffer.indexOf('\n');
    while (boundary >= 0) {
      let line = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 1);
      if (line.endsWith('\r')) line = line.slice(0, -1);
      if (line === '') {
        const frame = dispatchSseFrame(fields);
        if (frame) yield frame;
        fields = { data: [] };
      } else if (!line.startsWith(':')) {
        const separator = line.indexOf(':');
        const name = separator < 0 ? line : line.slice(0, separator);
        let value = separator < 0 ? '' : line.slice(separator + 1);
        if (value.startsWith(' ')) value = value.slice(1);
        if (name === 'id') fields.id = value;
        else if (name === 'event') fields.event = value;
        else if (name === 'data') fields.data.push(value);
      }
      boundary = buffer.indexOf('\n');
    }
  }
  if (buffer.length > 0) {
    let line = buffer;
    if (line.endsWith('\r')) line = line.slice(0, -1);
    if (line.startsWith('data:')) fields.data.push(line.slice(5).replace(/^ /, ''));
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
  for await (const chunk of await bodyToAsyncIterable(body)) {
    yield typeof chunk === 'string' ? chunk : new TextDecoder().decode(chunk);
  }
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
    let payload: unknown;
    try {
      payload = await readJson<unknown>(response);
    } catch {
      payload = undefined;
    }
    if (isRecord(payload)) {
      const error = payload.error;
      if (isRecord(error)) {
        const code = typeof error.code === 'string' ? error.code : `http_${response.status}`;
        const message = typeof error.message === 'string' ? error.message : 'request failed';
        const retryable = error.retryable === true;
        const recovery = typeof error.recovery === 'string' ? error.recovery : 'none';
        return new Phase1EError(code, message, retryable, recovery, payload.detail);
      }
      const detail = payload.detail;
      if (typeof detail === 'string') return new Phase1EError(`http_${response.status}`, detail, response.status >= 500, 'none', detail);
    }
    return new Phase1EError(`http_${response.status}`, `HTTP ${response.status} request failed`, response.status >= 500, 'none', payload);
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
    json: () => response.json(),
    text: () => response.text(),
  };
};
