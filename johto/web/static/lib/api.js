// Thin JSON client for the johto web API. Errors carry the server's plain-words message.
export class ApiError extends Error {
  constructor(status, body) {
    const e = (body && body.error) || {};
    super(e.message || (typeof body === "string" && body.trim() && body.length < 200 ? body.trim() : `HTTP ${status}`));
    this.status = status; this.code = e.code; this.details = e.details || {};
  }
}

async function call(method, path, body) {
  const r = await fetch(path, {
    method, credentials: "same-origin",
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!r.ok) throw new ApiError(r.status, data);
  return data;
}

// Two-press writes (contract 5.1). The first call comes back 409 needs_confirm with a nonce that is
// valid for a few seconds; the next call to the same route inside that window sends it back.
const armed = new Map(); // "METHOD path" -> {nonce, until, timer, onDisarm}

function disarm(key) {
  const a = armed.get(key);
  if (!a) return;
  clearTimeout(a.timer);
  armed.delete(key);
  a.onDisarm?.();
}

export const api = {
  get: (p) => call("GET", p),
  send: (m, p, b) => call(m, p, b ?? {}),
  post: (p, b) => call("POST", p, b ?? {}),

  /**
   * Run a write that may need a second press. Resolves to the server's answer, or to
   * `{armed: true, details}` after the first press. `onArm(summary, expiresIn, details)` fires when
   * the route arms; `onDisarm()` when the window closes or the second press goes out.
   * A server that never asks for confirmation just answers the first call.
   */
  async confirmable(method, path, body = {}, { onArm, onDisarm } = {}) {
    const key = `${method} ${path}`;
    const a = armed.get(key);
    if (a && Date.now() < a.until) {
      disarm(key);
      return call(method, path, { ...body, confirm: a.nonce });
    }
    disarm(key);
    try {
      return await call(method, path, body);
    } catch (e) {
      if (e.code !== "needs_confirm" || !e.details?.nonce) throw e;
      const secs = e.details.expires_in || 4;
      const timer = setTimeout(() => disarm(key), secs * 1000);
      armed.set(key, { nonce: e.details.nonce, until: Date.now() + secs * 1000, timer, onDisarm });
      // The message is the short "press again" line; details.summary may be one of its rows.
      onArm?.(e.message || e.details.summary || "", secs, e.details);
      return { armed: true, details: e.details };
    }
  },
  isArmed: (method, path) => { const a = armed.get(`${method} ${path}`); return !!a && Date.now() < a.until; },
  disarm: (method, path) => disarm(`${method} ${path}`),
};
