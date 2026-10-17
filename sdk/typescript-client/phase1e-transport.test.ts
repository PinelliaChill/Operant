import assert from 'node:assert/strict';
import test from 'node:test';
import {
  Phase1EError,
  MAX_SSE_DATA_BYTES,
  MAX_SSE_FRAME_BYTES,
  MAX_SSE_LINE_BYTES,
  SseProtocolError,
  parseSse,
} from './phase1e-transport.ts';

async function collect(body: Parameters<typeof parseSse>[0]): Promise<unknown[]> {
  const frames: unknown[] = [];
  for await (const frame of parseSse(body)) frames.push(frame);
  return frames;
}

test('Phase 1E SSE parser handles UTF-8 and field boundaries split across chunks', async () => {
  const raw = new TextEncoder().encode('id: 1\ndata: {"text": "é"}\n\n');
  const split = raw.indexOf(0xc3) + 1;
  const frames = await collect((async function* () {
    yield raw.slice(0, split);
    yield raw.slice(split);
  })());
  assert.deepEqual(frames, [{ id: '1', event: undefined, data: { text: 'é' } }]);
});

test('Phase 1E SSE parser rejects line, frame, and data overflows', async () => {
  await assert.rejects(
    collect(`${'x'.repeat(MAX_SSE_LINE_BYTES)}\n`),
    (error: unknown) => error instanceof SseProtocolError && error.code === 'sse_line_too_large',
  );

  const frameLine = `${'x'.repeat(MAX_SSE_LINE_BYTES - 2)}\n`;
  const frameSource = frameLine.repeat(Math.floor(MAX_SSE_FRAME_BYTES / frameLine.length) + 1);
  await assert.rejects(
    collect((async function* () {
      yield frameSource.slice(0, 100);
      yield frameSource.slice(100);
    })()),
    (error: unknown) => error instanceof SseProtocolError && error.code === 'sse_frame_too_large',
  );

  const dataValue = 'x'.repeat(MAX_SSE_LINE_BYTES - 'data: '.length - 1);
  const dataSource = Array.from(
    { length: Math.floor(MAX_SSE_DATA_BYTES / (dataValue.length + 1)) + 1 },
    () => `data: ${dataValue}\n`,
  ).join('');
  await assert.rejects(
    collect((async function* () {
      yield dataSource.slice(0, 77);
      yield dataSource.slice(77);
    })()),
    (error: unknown) => error instanceof SseProtocolError && error.code === 'sse_data_too_large',
  );
});


test('only the exact legacy local-caller 403 is recognized outside full envelopes', async () => {
  const denied = { error: { code: 'local_caller_required', message: '本机调用者验证失败' } };
  const result = await Phase1EError.fromResponse({ status: 403, json: JSON.stringify(denied) });
  assert.equal(result.code, 'local_caller_required');
  assert.equal(result.retryable, false);
  assert.equal(result.recovery, 'none');
  for (const [status, payload] of [
    [500, denied], [403, { error: { code: 'permission_denied', message: 'no' } }],
    [403, { ...denied, detail: null }],
    [403, { error: { ...denied.error, recovery: 'manual_reconcile' } }],
  ] as const) {
    assert.equal((await Phase1EError.fromResponse({ status, json: JSON.stringify(payload) })).code, 'invalid_error_envelope');
  }
  const full = await Phase1EError.fromResponse({ status: 403, json: JSON.stringify({ error: { ...denied.error, retryable: false, recovery: 'none' }, detail: null }) });
  assert.equal(full.code, 'local_caller_required');
});
