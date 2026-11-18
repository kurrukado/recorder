const encoder = new TextEncoder();
const decoder = new TextDecoder('utf-8', { fatal: true });

function decodeBase64url(value) {
  return Uint8Array.from(atob(value.replace(/-/g, '+').replace(/_/g, '/')), char => char.charCodeAt(0));
}

async function verifyToken(token, secret) {
  if (!secret || !token || token.length > 2048) return null;
  const match = /^([\w-]+)\.([\w-]{42}[AEIMQUYcgkosw048])$/.exec(token);
  if (!match) return null;
  const [, payload, signature] = match;
  try {
    const hmac = await crypto.subtle.importKey('raw', encoder.encode(secret),
      { name: 'HMAC', hash: 'SHA-256' }, false, ['verify']);
    if (!await crypto.subtle.verify('HMAC', hmac, decodeBase64url(signature), encoder.encode(payload))) return null;
    const { key, filename, mode, exp } = JSON.parse(decoder.decode(decodeBase64url(payload)));
    if (typeof key !== 'string' || !/^recordings\/[\w.-]{1,200}$/.test(key) || /\/\.{1,2}$/.test(key)
      || typeof filename !== 'string' || !filename || filename.length > 300 || /[\\/\u0000-\u001F\u007F]/.test(filename)
      || !['play', 'download'].includes(mode) || !Number.isSafeInteger(exp) || exp <= Date.now() / 1000) return null;
    return { key, filename, mode };
  } catch {
    return null;
  }
}

function parseRange(value, size) {
  const match = /^bytes=(\d*)-(\d*)$/i.exec(value);
  if (!match || !(match[1] || match[2]) || size === 0) return null;
  const start = Number(match[1]);
  const end = Number(match[2]);
  if (!match[1]) {
    if (end === 0) return null;
    const length = Math.min(end, size);
    return { offset: size - length, length };
  }
  if (start >= size || (match[2] && end < start)) return null;
  return { offset: start, length: (match[2] ? Math.min(end, size - 1) : size - 1) - start + 1 };
}

function ifRangeMatches(value, object) {
  if (!value || value === object.httpEtag) return true;
  return Number.isFinite(Date.parse(value)) && Math.floor(object.uploaded.getTime() / 1000) * 1000 === Date.parse(value);
}

function responseHeaders(object, claims) {
  const headers = new Headers({
    'Accept-Ranges': 'bytes',
    'Cache-Control': 'private, no-store',
    'Content-Type': 'video/mp4',
    'ETag': object.httpEtag,
    'Last-Modified': object.uploaded.toUTCString(),
    'Referrer-Policy': 'no-referrer',
    'X-Content-Type-Options': 'nosniff',
  });
  if (claims.mode === 'download') {
    const fallback = claims.filename.replace(/[^\w. -]/g, '_');
    const filename = encodeURIComponent(claims.filename.toWellFormed()).replace(/['()*]/g,
      char => `%${char.charCodeAt(0).toString(16).toUpperCase()}`);
    headers.set('Content-Disposition', `attachment; filename="${fallback}"; filename*=UTF-8''${filename}`);
  }
  return headers;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const errorHeaders = { 'Cache-Control': 'private, no-store' };
    if (url.pathname !== '/media') return new Response(null, { status: 404, headers: errorHeaders });
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      return new Response(null, { status: 405, headers: { ...errorHeaders, Allow: 'GET, HEAD' } });
    }
    const claims = await verifyToken(url.searchParams.get('token'), env.RECORD_MEDIA_SIGNING_SECRET);
    if (!claims) return new Response(null, { status: 403, headers: errorHeaders });
    const { RECORDINGS: bucket } = env;
    const headOnly = request.method === 'HEAD';
    const rangeHeader = !headOnly && request.headers.get('range');
    let object;
    let range;
    if (headOnly || rangeHeader) {
      object = await bucket.head(claims.key);
      if (!object) return new Response(null, { status: 404, headers: errorHeaders });
      if (rangeHeader && ifRangeMatches(request.headers.get('if-range'), object)) {
        range = parseRange(rangeHeader, object.size);
        if (!range) return new Response(null, {
          status: 416,
          headers: { ...errorHeaders, 'Accept-Ranges': 'bytes', 'Content-Range': `bytes */${object.size}` },
        });
        // Keep the range tied to the metadata even if another upload replaces the object.
        object = await bucket.get(claims.key, { range, onlyIf: { etagMatches: object.etag } });
      } else if (!headOnly) {
        object = await bucket.get(claims.key);
      }
    } else {
      object = await bucket.get(claims.key);
    }
    if (!object) return new Response(null, { status: 404, headers: errorHeaders });
    if (!headOnly && !object.body) return new Response(null, { status: 503, headers: errorHeaders });
    const headers = responseHeaders(object, claims);
    headers.set('Content-Length', String(range?.length ?? object.size));
    if (range) headers.set('Content-Range', `bytes ${range.offset}-${range.offset + range.length - 1}/${object.size}`);
    return new Response(headOnly ? null : object.body, { status: range ? 206 : 200, headers });
  },
};
