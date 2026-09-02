import { useState } from 'react';
import { useResetLogs } from '../data/useResetLogs.js';

export default function ResetConfirmModal({ open, onClose, onDone, toast }) {
  const [resetHistory, setResetHistory] = useState(false);
  const [deleteConversations, setDeleteConversations] = useState(false);
  const { busy, run } = useResetLogs({ toast, onDone: () => { setResetHistory(false); setDeleteConversations(false); onDone(); } });

  if (!open) return null;

  function handleConfirm() {
    run({ resetCustomerHistory: resetHistory, deleteConversations });
  }

  return (
    <div
      className="fixed inset-0 bg-[rgba(20,24,38,0.45)] flex items-center justify-center p-6 z-[110]"
      onClick={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}
    >
      <div className="bg-white border border-gray-100 rounded-2xl max-w-[420px] w-full p-6 shadow-modal">
        <h3 className="text-lg font-extrabold m-0 mb-2">Reset logs?</h3>
        <p className="text-muted text-xs leading-relaxed mb-4">
          Clears webhook_audit_log.csv, webhook_log.csv and pending_retries.csv. This cannot be undone.
        </p>

        <label className="flex items-center gap-2 text-xs text-ink mb-2.5 cursor-pointer">
          <input
            type="checkbox"
            checked={resetHistory}
            onChange={(e) => setResetHistory(e.target.checked)}
          />
          Also reset customer history
        </label>

        <label className="flex items-center gap-2 text-xs text-ink mb-5 cursor-pointer">
          <input
            type="checkbox"
            checked={deleteConversations}
            onChange={(e) => setDeleteConversations(e.target.checked)}
          />
          Also delete conversations (customer directory &amp; promise threads)
        </label>

        <div className="flex justify-end gap-2.5">
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            className="px-4 py-2 rounded-lg text-sm font-semibold text-muted hover:bg-gray-50 disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleConfirm}
            disabled={busy}
            className="px-4 py-2 rounded-lg text-sm font-bold text-white bg-red-500 hover:bg-red-600 disabled:opacity-50"
          >
            {busy ? 'Resetting...' : 'Reset logs'}
          </button>
        </div>
      </div>
    </div>
  );
}
