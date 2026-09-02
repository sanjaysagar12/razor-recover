import { postJson } from './client.js';

export function resetLogs(resetCustomerHistory) {
  return postJson('/api/reset-logs', { reset_customer_history: resetCustomerHistory });
}
