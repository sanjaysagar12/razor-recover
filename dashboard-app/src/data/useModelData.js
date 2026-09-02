import { useCallback, useEffect, useState } from 'react';
import {
  fetchDataset,
  fetchTrainStatus,
  triggerOfficialTrain,
  uploadDataset,
  fetchUploads,
  triggerCustomTrain,
  fetchCustomReport,
} from '../api/modelApi.js';

const REFRESH_MS = 5000;

// Data layer for the Model page -- owns the dataset preview, the shared
// training status (official retrain and custom-dataset training use the
// same backend lock/state, only one runs at a time), the uploads list, and
// whichever upload's report is currently selected. The UI layer calls this
// hook and never touches src/api/ directly.
export function useModelData({ toast }) {
  const [dataset, setDataset] = useState(null);
  const [datasetLoading, setDatasetLoading] = useState(true);
  const [trainStatus, setTrainStatus] = useState(null);
  const [triggeringOfficial, setTriggeringOfficial] = useState(false);
  const [uploads, setUploads] = useState([]);
  const [uploading, setUploading] = useState(false);
  const [selectedUploadId, setSelectedUploadId] = useState(null);
  const [selectedReport, setSelectedReport] = useState(null);
  const [triggeringCustom, setTriggeringCustom] = useState(false);

  const refreshDataset = useCallback(async () => {
    setDatasetLoading(true);
    const { ok, data } = await fetchDataset();
    if (ok) setDataset(data);
    setDatasetLoading(false);
  }, []);

  const refreshTrainStatus = useCallback(async () => {
    const { ok, data } = await fetchTrainStatus();
    if (ok) setTrainStatus(data);
  }, []);

  const refreshUploads = useCallback(async () => {
    const { ok, data } = await fetchUploads();
    if (ok) setUploads(data.uploads || []);
  }, []);

  useEffect(() => {
    refreshDataset();
    refreshUploads();
  }, [refreshDataset, refreshUploads]);

  useEffect(() => {
    refreshTrainStatus();
    const handle = setInterval(refreshTrainStatus, REFRESH_MS);
    return () => clearInterval(handle);
  }, [refreshTrainStatus]);

  // Once a custom run for the currently-selected upload finishes, pull its
  // report automatically instead of making the operator re-click it.
  useEffect(() => {
    if (
      trainStatus?.kind === 'custom'
      && trainStatus.status === 'done'
      && trainStatus.upload_id
      && trainStatus.upload_id === selectedUploadId
    ) {
      fetchCustomReport(trainStatus.upload_id).then(({ ok, data }) => {
        if (ok) setSelectedReport(data);
      });
      refreshUploads();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [trainStatus?.status, trainStatus?.upload_id]);

  async function trainOfficial() {
    setTriggeringOfficial(true);
    try {
      const { ok, data } = await triggerOfficialTrain();
      if (!ok) toast?.('err', 'Train failed', data.message || 'Request failed');
      await refreshTrainStatus();
    } finally {
      setTriggeringOfficial(false);
    }
  }

  async function uploadFile(file) {
    setUploading(true);
    try {
      const { ok, data } = await uploadDataset(file);
      if (!ok) {
        toast?.('err', 'Upload failed', data.error || 'Request failed');
        return null;
      }
      if (!data.valid) {
        toast?.('warn', 'Uploaded, but invalid', data.validation?.error || 'Schema check failed');
      } else {
        toast?.('ok', 'Dataset uploaded', `${data.filename} · ${data.row_count} rows`);
      }
      await refreshUploads();
      setSelectedUploadId(data.upload_id);
      setSelectedReport(null);
      return data;
    } catch (e) {
      toast?.('err', 'Upload error', String(e));
      return null;
    } finally {
      setUploading(false);
    }
  }

  async function selectUpload(uploadId) {
    setSelectedUploadId(uploadId);
    setSelectedReport(null);
    const { ok, data } = await fetchCustomReport(uploadId);
    if (ok) setSelectedReport(data);
  }

  async function trainCustom(uploadId) {
    setTriggeringCustom(true);
    try {
      const { ok, data } = await triggerCustomTrain(uploadId);
      if (!ok) {
        toast?.('err', 'Train failed', data.message || 'Request failed');
        return;
      }
      setSelectedUploadId(uploadId);
      setSelectedReport(null);
      await refreshTrainStatus();
    } finally {
      setTriggeringCustom(false);
    }
  }

  const trainingBusy = trainStatus?.status === 'running';

  return {
    dataset,
    datasetLoading,
    trainStatus,
    trainOfficialBusy: triggeringOfficial || (trainingBusy && trainStatus?.kind === 'official'),
    trainOfficial,
    uploads,
    uploading,
    uploadFile,
    selectedUploadId,
    selectedReport,
    selectUpload,
    trainCustomBusy: triggeringCustom || (trainingBusy && trainStatus?.kind === 'custom'),
    trainCustom,
    anyTrainingBusy: trainingBusy,
  };
}
