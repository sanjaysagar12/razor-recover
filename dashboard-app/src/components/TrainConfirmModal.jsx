export default function TrainConfirmModal({ open, onClose, onConfirm, busy }) {
  if (!open) return null;

  return (
    <div
      className="fixed inset-0 bg-[rgba(20,24,38,0.45)] flex items-center justify-center p-6 z-[110]"
      onClick={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}
    >
      <div className="bg-white border border-gray-100 rounded-2xl max-w-[460px] w-full p-6 shadow-modal">
        <h3 className="text-lg font-extrabold m-0 mb-2">Retrain the official model?</h3>
        <p className="text-muted text-xs leading-relaxed mb-5">
          Runs models/train_tree_models.py against data/train.csv + data/holdout.csv, overwriting
          models/artifacts/*.joblib and models/model_report.md. The live pipeline will pick up the
          newly trained model on its next restart -- it does not affect cases already scored this
          session, and it never touches any uploaded custom dataset.
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
            onClick={onConfirm}
            disabled={busy}
            className="px-4 py-2 rounded-lg text-sm font-bold text-white bg-accent hover:brightness-110 disabled:opacity-50"
          >
            {busy ? 'Training...' : 'Retrain model'}
          </button>
        </div>
      </div>
    </div>
  );
}
