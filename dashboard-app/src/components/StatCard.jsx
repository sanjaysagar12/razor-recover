const ICONS = {
  neutral: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="4" width="18" height="16" rx="2" /><line x1="7" y1="9" x2="17" y2="9" /><line x1="7" y1="13" x2="17" y2="13" />
    </svg>
  ),
  llm: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="4" y="4" width="16" height="16" rx="3" /><path d="M9 9h6v6H9z" /><path d="M4 9H2M4 15H2M22 9h-2M22 15h-2M9 4V2M15 4V2M9 22v-2M15 22v-2" />
    </svg>
  ),
  override: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" /><line x1="12" y1="8" x2="12" y2="12" /><line x1="12" y1="16" x2="12.01" y2="16" />
    </svg>
  ),
  review: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 9v4" /><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" /><line x1="12" y1="17" x2="12.01" y2="17" />
    </svg>
  ),
  good: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="10" /><path d="m9 12 2 2 4-4" />
    </svg>
  ),
  bad: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="10" /><line x1="15" y1="9" x2="9" y2="15" /><line x1="9" y1="9" x2="15" y2="15" />
    </svg>
  ),
};

// Single-digit counts are padded to two digits (4 -> "04") so every tile's
// number occupies the same visual width.
function formatValue(value) {
  if (typeof value === 'number' && value >= 0 && value < 10) {
    return String(value).padStart(2, '0');
  }
  return value;
}

export default function StatCard({ label, value, tone = 'neutral', compact = false, fill = false, description }) {
  const icon = ICONS[tone] || ICONS.neutral;
  const displayValue = formatValue(value);

  if (compact) {
    return (
      <div className="bg-white border border-gray-100 rounded-xl shadow-card px-3 py-2 min-w-[110px] flex items-center gap-2.5">
        <span className="w-6 h-6 flex items-center justify-center shrink-0 text-black">
          <span className="w-4 h-4 block">{icon}</span>
        </span>
        <div>
          <div className="text-lg font-extrabold tabular leading-none text-black">{displayValue}</div>
          <div className="text-[10px] text-muted font-medium mt-0.5">{label}</div>
        </div>
      </div>
    );
  }

  // fill -- for a card that stands alone in a column next to taller stacked
  // siblings (see DashboardPage's "Failed / pending" tile): grows to the
  // column's full height and centers its content instead of sitting short
  // with the number pinned to the top.
  if (fill) {
    return (
      <div className="bg-white border border-gray-100 rounded-2xl shadow-card p-5 min-w-[150px] h-full flex flex-col justify-center">
        <div className="flex items-center gap-3">
          <span className="w-10 h-10 flex items-center justify-center shrink-0 text-black">
            <span className="w-6 h-6 block">{icon}</span>
          </span>
          <div className="text-base text-black font-bold">{label}</div>
        </div>
        <div className="text-6xl font-extrabold tabular mt-3 text-black">{displayValue}</div>
        {description && <div className="text-[12.5px] text-muted mt-1.5">{description}</div>}
      </div>
    );
  }

  return (
    <div className="bg-white border border-gray-100 rounded-2xl shadow-card p-4 min-w-[150px] flex-1">
      <div className="flex items-start justify-between gap-3">
        <div className="text-[13px] text-black font-bold">{label}</div>
        <span className="w-8 h-8 flex items-center justify-center shrink-0 text-black">
          <span className="w-5 h-5 block">{icon}</span>
        </span>
      </div>
      <div className="text-4xl font-extrabold tabular mt-2 text-black">{displayValue}</div>
      {description && <div className="text-[11px] text-muted mt-1 leading-snug">{description}</div>}
    </div>
  );
}
