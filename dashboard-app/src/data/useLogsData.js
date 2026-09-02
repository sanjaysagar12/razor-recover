import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { fetchAuditLog } from '../api/dashboardApi.js';
import { fetchWebhookLog } from '../api/logsApi.js';

const REFRESH_MS = 5000;

// Data layer for the Logs page -- owns fetching/polling/filtering of the
// audit log + webhook transport log. The UI layer calls this hook and never
// touches src/api/ directly.
export function useLogsData({ paused }) {
  const [auditRows, setAuditRows] = useState([]);
  const [webhookRows, setWebhookRows] = useState([]);
  const [filters, setFilters] = useState({ routed: '', overrode: '', execStatus: '' });
  const [lastUpdated, setLastUpdated] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  const pausedRef = useRef(paused);
  pausedRef.current = paused;

  const refreshAll = useCallback(async () => {
    try {
      const [auditRes, webhookRes] = await Promise.all([fetchAuditLog(200), fetchWebhookLog(200)]);
      if (auditRes.ok) setAuditRows(auditRes.data.rows || []);
      if (webhookRes.ok) setWebhookRows(webhookRes.data.rows || []);
      setLastUpdated(new Date());
    } catch (e) {
      console.error(e);
    }
  }, []);

  useEffect(() => {
    refreshAll();
    const handle = setInterval(() => {
      if (!pausedRef.current) refreshAll();
    }, REFRESH_MS);
    return () => clearInterval(handle);
  }, [refreshAll]);

  async function handleManualRefresh() {
    setRefreshing(true);
    await refreshAll();
    setRefreshing(false);
  }

  const execStatusOptions = useMemo(
    () => [...new Set(auditRows.map((r) => r.execution_status).filter(Boolean))].sort(),
    [auditRows],
  );

  const filteredRows = useMemo(() => {
    let rows = auditRows;
    if (filters.routed) rows = rows.filter((r) => String(r.routed_to_llm) === filters.routed);
    if (filters.overrode) rows = rows.filter((r) => String(r.guardrail_overrode) === filters.overrode);
    if (filters.execStatus) rows = rows.filter((r) => r.execution_status === filters.execStatus);
    return rows;
  }, [auditRows, filters]);

  return {
    filteredRows,
    webhookRows,
    execStatusOptions,
    filters,
    setFilters,
    lastUpdated,
    refreshing,
    handleManualRefresh,
    refreshAll,
  };
}
