import { apiJson } from './client.js';

export function fetchWebhookLog(limit = 200) {
  return apiJson(`/api/webhook-log?limit=${limit}`);
}
