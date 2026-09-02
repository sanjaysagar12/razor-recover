import { apiJson } from './client.js';

export function triggerRunBatch() {
  return apiJson('/api/run-batch', { method: 'POST' });
}

export function fetchRunBatchStatus() {
  return apiJson('/api/run-batch/status');
}
