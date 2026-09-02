import { apiJson, postJson } from './client.js';

export function fetchSummary() {
  return apiJson('/api/summary');
}

export function fetchAuditLog(limit = 200) {
  return apiJson(`/api/audit-log?limit=${limit}`);
}

export function fetchCaseDetail(caseId) {
  return apiJson('/api/case-detail/' + encodeURIComponent(caseId));
}

export function triggerCase(payload, endpoint = '/api/trigger-test-case') {
  return postJson(endpoint, payload);
}
