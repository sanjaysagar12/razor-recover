import { useState } from 'react';
import FiltersBar from '../components/FiltersBar.jsx';
import AuditTable from '../components/AuditTable.jsx';
import WebhookLogTable from '../components/WebhookLogTable.jsx';
import CaseDetailModal from '../components/CaseDetailModal.jsx';
import ResetConfirmModal from '../components/ResetConfirmModal.jsx';
import Toasts from '../components/Toasts.jsx';
import { useToasts } from '../data/useToasts.js';
import { useLogsData } from '../data/useLogsData.js';

export default function LogsPage({ paused, setPaused }) {
  const [selectedCaseId, setSelectedCaseId] = useState(null);
  const [showResetModal, setShowResetModal] = useState(false);
  const { toasts, toast } = useToasts();
  const {
    filteredRows,
    webhookRows,
    execStatusOptions,
    filters,
    setFilters,
    lastUpdated,
    refreshing,
    handleManualRefresh,
    refreshAll,
  } = useLogsData({ paused });

  return (
    <div className="flex-1 min-w-0">
      <div className="flex items-end justify-between flex-wrap gap-5 px-7 pt-6 pb-2">
        <div className="flex flex-col gap-2.5">
          <h1 className="text-[34px] font-extrabold m-0 tracking-tight">Logs</h1>
          <div className="text-muted text-[13px]">Audit log (pipeline decisions) and webhook log (transport layer), newest first</div>
        </div>
        <div className="flex items-center gap-3">
          <span className="tabular text-[11px] text-muted">{lastUpdated ? `last updated ${lastUpdated.toLocaleTimeString()}` : 'not yet loaded'}</span>
          <button
            onClick={() => setPaused((p) => !p)}
            title={paused ? 'Resume auto-refresh' : 'Pause auto-refresh'}
            className="w-9 h-9 rounded-lg border border-gray-200 flex items-center justify-center text-black hover:bg-gray-50 shrink-0"
          >
            {paused ? (
              <svg viewBox="0 0 24 24" fill="currentColor" className="w-4 h-4">
                <polygon points="6 4 20 12 6 20 6 4" />
              </svg>
            ) : (
              <svg viewBox="0 0 24 24" fill="currentColor" className="w-4 h-4">
                <rect x="5" y="4" width="5" height="16" rx="1" />
                <rect x="14" y="4" width="5" height="16" rx="1" />
              </svg>
            )}
          </button>
          <button
            onClick={handleManualRefresh}
            title="Refresh now"
            className="w-9 h-9 rounded-lg border border-gray-200 flex items-center justify-center text-black hover:bg-gray-50 shrink-0"
          >
            <svg
              viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
              className={`w-4 h-4 ${refreshing ? 'animate-spin' : ''}`}
            >
              <path d="M23 4v6h-6" /><path d="M1 20v-6h6" />
              <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
            </svg>
          </button>
          <button
            onClick={() => setShowResetModal(true)}
            title="Reset logs"
            className="w-9 h-9 rounded-lg border border-gray-200 flex items-center justify-center text-red-500 hover:bg-red-50 shrink-0"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4">
              <polyline points="3 6 5 6 21 6" />
              <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
              <line x1="10" y1="11" x2="10" y2="17" />
              <line x1="14" y1="11" x2="14" y2="17" />
            </svg>
          </button>
        </div>
      </div>

      <section className="px-7 pb-7 pt-4">
        <div className="bg-white border border-gray-100 rounded-2xl shadow-card p-[18px] mb-4">
          <div className="flex items-center gap-2 mb-4">
            <h2 className="text-2xl font-extrabold text-black leading-tight m-0">Audit log</h2>
            <span className="group/info relative inline-flex">
              <button
                type="button"
                className="w-5 h-5 rounded-full border border-gray-300 text-muted text-[11px] font-bold flex items-center justify-center hover:bg-gray-50 hover:text-black"
                aria-label="Audit log info"
              >
                i
              </button>
              <span className="pointer-events-none absolute left-0 top-full mt-2 w-72 rounded-lg bg-black text-white text-[11px] leading-relaxed px-3 py-2 opacity-0 group-hover/info:opacity-100 transition-opacity z-30 shadow-lg">
                logs/webhook_audit_log.csv &middot; pipeline decisions, newest first &middot; audit log, client-side
              </span>
            </span>
          </div>
          <div className="mb-4">
            <div className="text-[11px] font-semibold text-muted uppercase tracking-wider mb-2">Filters</div>
            <FiltersBar filters={filters} onChange={setFilters} execStatusOptions={execStatusOptions} />
          </div>
          <AuditTable rows={filteredRows} onSelectCase={setSelectedCaseId} />
        </div>

        <div className="bg-white border border-gray-100 rounded-2xl shadow-card p-[18px] mb-4">
          <div className="flex items-center gap-2 mb-4">
            <h2 className="text-2xl font-extrabold text-black leading-tight m-0">Webhook log</h2>
            <span className="group/info relative inline-flex">
              <button
                type="button"
                className="w-5 h-5 rounded-full border border-gray-300 text-muted text-[11px] font-bold flex items-center justify-center hover:bg-gray-50 hover:text-black"
                aria-label="Webhook log info"
              >
                i
              </button>
              <span className="pointer-events-none absolute left-0 top-full mt-2 w-72 rounded-lg bg-black text-white text-[11px] leading-relaxed px-3 py-2 opacity-0 group-hover/info:opacity-100 transition-opacity z-30 shadow-lg">
                logs/webhook_log.csv &middot; transport layer, every POST received, newest first
              </span>
            </span>
          </div>
          <WebhookLogTable rows={webhookRows} />
        </div>
      </section>

      <Toasts toasts={toasts} />
      <CaseDetailModal caseId={selectedCaseId} onClose={() => setSelectedCaseId(null)} />
      <ResetConfirmModal
        open={showResetModal}
        onClose={() => setShowResetModal(false)}
        onDone={() => { setShowResetModal(false); refreshAll(); }}
        toast={toast}
      />
    </div>
  );
}
