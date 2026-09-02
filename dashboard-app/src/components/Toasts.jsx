const BORDER_CLASSES = {
  ok: 'border-l-4 border-l-emerald-500',
  warn: 'border-l-4 border-l-amber-400',
  err: 'border-l-4 border-l-red-400',
};

export default function Toasts({ toasts }) {
  return (
    <div className="fixed bottom-5 right-5 flex flex-col gap-2.5 z-50">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={`bg-white border border-gray-100 rounded-[10px] px-4 py-3 max-w-[340px] shadow-modal animate-slide-in ${BORDER_CLASSES[t.kind] || ''}`}
        >
          <div className="font-bold text-xs mb-1 text-ink">{t.title}</div>
          <div className="text-xs text-muted">{t.message}</div>
        </div>
      ))}
    </div>
  );
}
