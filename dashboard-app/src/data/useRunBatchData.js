import { useCallback, useEffect, useState } from 'react';
import { triggerRunBatch, fetchRunBatchStatus } from '../api/runBatchApi.js';

const REFRESH_MS = 5000;

// Data layer for the Run Batch page -- owns polling pipeline/run_batch.py's
// status and the trigger action. The UI layer calls this hook and never
// touches src/api/ directly.
export function useRunBatchData() {
  const [status, setStatus] = useState(null);
  const [triggering, setTriggering] = useState(false);

  const refreshStatus = useCallback(async () => {
    const { ok, data } = await fetchRunBatchStatus();
    if (ok) setStatus(data);
  }, []);

  useEffect(() => {
    refreshStatus();
    const handle = setInterval(refreshStatus, REFRESH_MS);
    return () => clearInterval(handle);
  }, [refreshStatus]);

  async function run() {
    setTriggering(true);
    try {
      await triggerRunBatch();
      await refreshStatus();
    } finally {
      setTriggering(false);
    }
  }

  return { status, busy: triggering || status?.status === 'running', run };
}
