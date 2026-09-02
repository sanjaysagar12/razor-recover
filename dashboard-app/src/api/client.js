// API layer core -- the only place that touches fetch() directly. Every
// other API-layer module builds on these two helpers; nothing outside
// src/api/ should call fetch() or know an endpoint's URL/method/body shape.
export async function apiFetch(path, options) {
  return fetch(path, options || {});
}

export async function apiJson(path, options) {
  const resp = await apiFetch(path, options || {});
  const data = await resp.json();
  return { ok: resp.ok, status: resp.status, data };
}

export function postJson(path, body) {
  return apiJson(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}
