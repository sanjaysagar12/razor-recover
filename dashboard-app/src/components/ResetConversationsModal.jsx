import { useResetConversations } from '../data/useResetConversations.js';

export default function ResetConversationsModal({ open, onClose, onDone, toast }) {
  const { busy, run } = useResetConversations({ toast, onDone });

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 bg-[rgba(20,24,38,0.45)] flex items-center justify-center p-6 z-[110]"
      onClick={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}
    >
      <div className="bg-white border border-gray-100 rounded-2xl max-w-[420px] w-full p-6 shadow-modal">
        <h3 className="text-lg font-extrabold m-0 mb-2">Reset conversations?</h3>
        <p className="text-muted text-xs leading-relaxed mb-5">
          Clears the customer directory, all promise-reply threads, and promise_log.csv. This does not touch
          customer_history scoring data or the audit log. This cannot be undone.
        </p>

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
            onClick={run}
            disabled={busy}
            className="px-4 py-2 rounded-lg text-sm font-bold text-white bg-red-500 hover:bg-red-600 disabled:opacity-50"
          >
            {busy ? 'Resetting...' : 'Reset conversations'}
          </button>
        </div>
      </div>
    </div>
  );
}
