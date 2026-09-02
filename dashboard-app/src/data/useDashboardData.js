import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { fetchSummary, fetchAuditLog, triggerCase as apiTriggerCase } from '../api/dashboardApi.js';

const REFRESH_MS = 5000;

export function execTileTone(key) {
  const k = key.toUpperCase();
  if (k.includes('FAIL')) return 'bad';
  if (k.includes('SCHEDULE') || k.includes('SUCCESS') || k.includes('LOGGED') || k.includes('RECOVER')) return 'good';
  return 'neutral';
}

// Data layer for the Dashboard page -- owns fetching/polling/filtering of
// summary + audit-log state and the trigger-case action. The UI layer calls
// this hook and never touches src/api/ directly; `toast` is injected so this
// hook can report outcomes without owning its own notification list (the
// page's single Toasts instance stays the source of truth for what's shown).
export function useDashboardData({ paused, toast }) {
  const [summary, setSummary] = useState({});
  const [auditRows, setAuditRows] = useState([]);
  const [filters, setFilters] = useState({ routed: '', overrode: '', execStatus: '' });
  const [lastUpdated, setLastUpdated] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  const pausedRef = useRef(paused);
  pausedRef.current = paused;

  const refreshAll = useCallback(async () => {
    try {
      const [summaryRes, auditRes] = await Promise.all([fetchSummary(), fetchAuditLog(200)]);
      if (summaryRes.ok) setSummary(summaryRes.data);
      if (auditRes.ok) setAuditRows(auditRes.data.rows || []);
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

  const execEntries = useMemo(
    () => Object.entries(summary.execution_status_breakdown || {}),
    [summary],
  );

  const filteredRows = useMemo(() => {
    let rows = auditRows;
    if (filters.routed) rows = rows.filter((r) => String(r.routed_to_llm) === filters.routed);
    if (filters.overrode) rows = rows.filter((r) => String(r.guardrail_overrode) === filters.overrode);
    if (filters.execStatus) rows = rows.filter((r) => r.execution_status === filters.execStatus);
    return rows;
  }, [auditRows, filters]);

  async function triggerCase(payload, endpoint) {
    try {
      const { ok, data } = await apiTriggerCase(payload, endpoint);
      if (!ok) {
        toast?.('err', 'Trigger failed', data.message || 'Request failed');
      } else {
        const row = data.row || {};
        toast?.(
          row.guardrail_overrode ? 'warn' : 'ok',
          `${row.case_id || '(case)'} -> ${row.final_action}`,
          `guardrail_overrode=${row.guardrail_overrode} override_rule=${row.override_rule ?? 'none'} requires_human_review=${row.requires_human_review}`,
        );
      }
      await refreshAll();
      return data;
    } catch (e) {
      toast?.('err', 'Trigger error', String(e));
      return null;
    }
  }

  return {
    summary,
    filteredRows,
    execEntries,
    execStatusOptions,
    filters,
    setFilters,
    lastUpdated,
    refreshing,
    handleManualRefresh,
    triggerCase,
    refreshAll,
  };
}
