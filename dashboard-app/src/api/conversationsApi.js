import { apiJson, postJson } from './client.js';

export function fetchCustomers() {
  return apiJson('/api/customers');
}

export function fetchCustomerConversations(email) {
  return apiJson('/api/customer/' + encodeURIComponent(email) + '/conversations');
}

export function sendPromiseReply(caseId, customerId, message) {
  return postJson('/api/promise-reply', { case_id: caseId, customer_id: customerId, message });
}

export function resetConversations() {
  return apiJson('/api/reset-conversations', { method: 'POST' });
}
