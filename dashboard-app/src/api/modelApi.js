import { apiFetch, apiJson, postJson } from './client.js';

export function fetchDataset() {
  return apiJson('/api/model/dataset');
}

export function fetchOfficialReport() {
  return apiJson('/api/model/report');
}

export function triggerOfficialTrain() {
  return apiJson('/api/model/train', { method: 'POST' });
}

export function fetchTrainStatus() {
  return apiJson('/api/model/train/status');
}

export async function uploadDataset(file) {
  const formData = new FormData();
  formData.append('file', file);
  const resp = await apiFetch('/api/model/upload', { method: 'POST', body: formData });
  const data = await resp.json();
  return { ok: resp.ok, status: resp.status, data };
}

export function fetchUploads() {
  return apiJson('/api/model/uploads');
}

export function triggerCustomTrain(uploadId) {
  return postJson('/api/model/train-custom', { upload_id: uploadId });
}

export function fetchCustomReport(uploadId) {
  return apiJson('/api/model/custom-report/' + encodeURIComponent(uploadId));
}
