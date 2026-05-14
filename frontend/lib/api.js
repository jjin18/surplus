// Thin fetch wrappers around the surplus backend.
//
// In production the React app is served by FastAPI at the same origin, so
// relative URLs ("/events", "/webhooks/...") just work. In dev, Vite proxies
// these same paths to localhost:8000 — same code, no env switching.
//
// Every call throws on non-2xx so the caller can use try/catch + render
// errors normally.

async function request(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "content-type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText} — ${text.slice(0, 240)}`);
  }
  // some endpoints return empty body
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : null;
}

export const api = {
  // 01 intake
  createEvent: (body) =>
    request("/events", { method: "POST", body: JSON.stringify(body) }),
  getEvent: (id) => request(`/events/${id}`),

  // 02 prospecting
  runProspect: (id) => request(`/events/${id}/prospect`, { method: "POST" }),
  getProspects: (id) => request(`/events/${id}/prospects`),

  // 03 outreach (provider-backed; DRY_RUN by default)
  previewOutreach: (id) => request(`/events/${id}/outreach/preview`),
  runOutreach: (id) => request(`/events/${id}/outreach`, { method: "POST" }),
  getOutreachLog: (id) => request(`/events/${id}/outreach/log`),

  // per-prospect, one-at-a-time. Safer than the batch /outreach for live.
  sendInvite: (eid, pid) =>
    request(`/events/${eid}/prospects/${pid}/invite`, { method: "POST" }),
  sendDirectMessage: (eid, pid) =>
    request(`/events/${eid}/prospects/${pid}/dm`, { method: "POST" }),

  // convenience — full pipeline in one call (BLOCKED in live without confirm)
  runPipeline: (id) => request(`/events/${id}/run`, { method: "POST" }),

  // 04 matching
  runMatch: (id) => request(`/events/${id}/match`, { method: "POST" }),
  getMatches: (id) => request(`/events/${id}/matches`),

  // 05 ROI
  getRoi: (id) => request(`/events/${id}/roi`),

  // meta
  health: () => request("/api/health"),
};
