import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';
import test from 'node:test';
import worker from './worker.mjs';

const secret = 'local-fixture-secret';
const key = 'recordings/abcd0123.mp4';
const bytes = 'abcdefghij';
function signed(overrides = {}) {
  const claims = { key, filename: 'bản ghi.mp4', mode: 'play', exp: Math.floor(Date.now() / 1000) + 60, ...overrides };
  const payload = Buffer.from(JSON.stringify(claims)).toString('base64url');
  return `${payload}.${createHmac('sha256', secret).update(payload).digest('base64url')}`;
}

function fixture() {
  const call = { head: 0, get: 0 };
  const object = { size: bytes.length, httpEtag: '"abcd"', uploaded: new Date(0) };
  const env = { RECORD_MEDIA_SIGNING_SECRET: secret, RECORDINGS: {
    async head(requestKey) {
      call.head++;
      return requestKey === key ? object : null;
    },
    async get(requestKey, { range } = {}) {
      call.get++;
      if (requestKey !== key) return null;
      const value = range ? bytes.slice(range.offset, range.offset + range.length) : bytes;
      return { ...object, body: new Blob([value]).stream(), range };
    },
  } };
  return { call, run: (token, options = {}) => worker.fetch(new Request(`https://media.example/media?token=${token}`, options), env) };
}

test('rejects tampering, expiry, unscoped keys, and invalid modes before storage', async () => {
  const { call, run } = fixture();
  for (const token of [`${signed()}junk`, signed({ exp: 0 }), signed({ key: 'private/creds' }), signed({ mode: 'write' })]) {
    assert.equal((await run(token)).status, 403);
  }
  assert.deepEqual(call, { head: 0, get: 0 });
});

test('serves range bytes and gives 416 with the object size', async () => {
  const { run } = fixture();
  for (const [range, value, contentRange] of [
    ['bytes=2-4', 'cde', 'bytes 2-4/10'],
    ['bytes=8-', 'ij', 'bytes 8-9/10'],
    ['bytes=-3', 'hij', 'bytes 7-9/10'],
  ]) {
    const response = await run(signed(), { headers: { Range: range } });
    assert.equal(response.status, 206);
    assert.equal(response.headers.get('content-range'), contentRange);
    assert.equal(response.headers.get('content-length'), String(value.length));
    assert.equal(await response.text(), value);
  }
  for (const range of ['bytes=10-', 'bytes=4-2', 'bytes=-0', 'bytes=1-2,4-6']) {
    const response = await run(signed(), { headers: { Range: range } });
    assert.equal(response.status, 416);
    assert.equal(response.headers.get('content-range'), 'bytes */10');
  }
});

test('HEAD reads only metadata and download scope sets a safe UTF-8 filename', async () => {
  const { call, run } = fixture();
  const response = await run(signed({ mode: 'download' }), { method: 'HEAD', headers: { Range: 'bytes=2-3' } });
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('content-length'), '10');
  assert.equal(response.headers.get('content-type'), 'video/mp4');
  assert.match(response.headers.get('content-disposition'), /^attachment;.*filename\*=UTF-8''b%E1%BA%A3n%20ghi\.mp4$/);
  assert.deepEqual(call, { head: 1, get: 0 });
  assert.equal(await response.text(), '');
  const stream = await run(signed());
  assert.equal(stream.headers.get('content-disposition'), null);
  assert.equal(await stream.text(), bytes);
});
