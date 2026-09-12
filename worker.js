const MODEL_REPO = 'Xenova/htdemucs';

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': 'no-store',
      'access-control-allow-origin': '*',
      'access-control-allow-methods': 'POST,OPTIONS',
      'access-control-allow-headers': 'content-type',
    },
  });
}

export default {
  async fetch(request) {
    if (request.method === 'OPTIONS') return json({ ok: true });
    if (request.method !== 'POST') return json({ error: 'POST an audio file to /separate' }, 405);

    const url = new URL(request.url);
    if (url.pathname !== '/separate') return json({ error: 'Not found' }, 404);

    // Cloudflare Workers cannot run Demucs/ONNX CPU inference reliably in the
    // standard isolate. Return an explicit capability response instead of
    // pretending that a browser-side source-separation pipeline exists.
    return json({
      error: 'STEM_BACKEND_REQUIRED',
      message: 'Real Demucs separation requires a Python/ONNX backend. Configure SEPARATION_API_URL in the frontend deployment.',
      model: MODEL_REPO,
    }, 501);
  },
};
