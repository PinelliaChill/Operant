import assert from 'node:assert/strict';
import test from 'node:test';
import {
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
