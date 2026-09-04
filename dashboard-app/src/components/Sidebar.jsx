const navItems = [
  {
    key: 'dashboard',
    label: 'Dashboard',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <rect x="3" y="3" width="7" height="7" rx="1.5" />
        <rect x="14" y="3" width="7" height="7" rx="1.5" />
        <rect x="3" y="14" width="7" height="7" rx="1.5" />
        <rect x="14" y="14" width="7" height="7" rx="1.5" />
      </svg>
    ),
  },
  {
    key: 'conversations',
    label: 'Conversations',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" />
      </svg>
    ),
  },
  {
    key: 'logs',
    label: 'Log',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
        <path d="M14 2v6h6" />
        <line x1="8" y1="13" x2="16" y2="13" />
        <line x1="8" y1="17" x2="16" y2="17" />
        <line x1="8" y1="9" x2="10" y2="9" />
      </svg>
    ),
  },
];

// Second group -- more operational/config-leaning pages than the daily-use
// group above, visually separated the same way the reference sidebar splits
// its primary nav from Settings/Sign out.
const secondaryNavItems = [
  {
    key: 'run-batch',
    label: 'Run Batch',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <polygon points="5 3 19 12 5 21 5 3" />
      </svg>
    ),
  },
  {
    key: 'model',
    label: 'Model',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <rect x="7" y="7" width="10" height="10" rx="1.5" />
        <line x1="7" y1="2" x2="7" y2="5" />
        <line x1="12" y1="2" x2="12" y2="5" />
        <line x1="17" y1="2" x2="17" y2="5" />
        <line x1="7" y1="19" x2="7" y2="22" />
        <line x1="12" y1="19" x2="12" y2="22" />
        <line x1="17" y1="19" x2="17" y2="22" />
        <line x1="2" y1="7" x2="5" y2="7" />
        <line x1="2" y1="12" x2="5" y2="12" />
        <line x1="2" y1="17" x2="5" y2="17" />
        <line x1="19" y1="7" x2="22" y2="7" />
        <line x1="19" y1="12" x2="22" y2="12" />
        <line x1="19" y1="17" x2="22" y2="17" />
      </svg>
    ),
  },
];

function NavButton({ item, active, onClick }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex items-center gap-3 px-3.5 py-3 rounded-xl text-[14px] font-semibold text-left transition-colors ${
        active ? 'bg-accent/10 text-accent' : 'text-muted hover:bg-gray-50 hover:text-ink'
      }`}
    >
      <span className="w-5 h-5 shrink-0">{item.icon}</span>
      <span>{item.label}</span>
    </button>
  );
}

export default function Sidebar({ page = 'dashboard', onNavigate, live = true }) {
  return (
    <aside className="w-[248px] shrink-0 bg-white border-r border-gray-100 px-4 py-6 flex flex-col gap-8 h-screen sticky top-0">
      <div className="flex items-center gap-2.5 px-2">
        
        <span className="font-extrabold text-[19px] leading-tight text-ink">
          Razor Recover
          <small className="block font-medium text-muted text-[11px] mt-0.5">AI pipeline</small>
        </span>
      </div>

      <div className="flex flex-col gap-5 mt-2">
        <nav className="flex flex-col gap-1">
          {navItems.map((item) => (
            <NavButton key={item.key} item={item} active={page === item.key} onClick={() => onNavigate?.(item.key)} />
          ))}
        </nav>

        <div className="border-t border-gray-100" />

        <nav className="flex flex-col gap-1">
          {secondaryNavItems.map((item) => (
            <NavButton key={item.key} item={item} active={page === item.key} onClick={() => onNavigate?.(item.key)} />
          ))}
        </nav>
      </div>

      <div className="mt-auto px-2">
        {live ? (
          <div className="inline-flex items-center gap-1.5 rounded-full bg-emerald-500/10 border border-emerald-500/30 text-emerald-600 px-2.5 py-1 text-[11px] font-bold tracking-wide">
            <span className="relative w-1.5 h-1.5 rounded-full bg-emerald-500 pulse-dot" />
            Live
          </div>
        ) : (
          <div className="inline-flex items-center gap-1.5 rounded-full bg-slate-400/10 border border-slate-400/30 text-muted px-2.5 py-1 text-[11px] font-bold tracking-wide">
            <span className="w-1.5 h-1.5 rounded-full bg-slate-400" />
            Paused
          </div>
        )}
      </div>
    </aside>
  );
}
