import { postJson } from './client.js';

export function cancelAllPaymentLinks() {
  return postJson('/api/payment-links/cancel-all', {});
}
