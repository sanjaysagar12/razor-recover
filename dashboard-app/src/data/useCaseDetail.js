import { useEffect, useState } from 'react';
import { fetchCaseDetail } from '../api/dashboardApi.js';

// Data layer for the case-detail popup (used by both the Dashboard and Logs
// pages). The UI layer calls this hook and never touches src/api/ directly.
export function useCaseDetail(caseId) {
  const [state, setState] = useState({ loading: true, error: null, data: null });

  useEffect(() => {
    if (!caseId) return;
    let cancelled = false;
    setState({ loading: true, error: null, data: null });
    (async () => {
      const { ok, data } = await fetchCaseDetail(caseId);
      if (cancelled) return;
      if (!ok) setState({ loading: false, error: data.message || 'Not found', data: null });
      else setState({ loading: false, error: null, data });
    })();
    return () => {
      cancelled = true;
    };
  }, [caseId]);

  return state;
}
