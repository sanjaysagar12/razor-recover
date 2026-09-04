import { useEffect, useRef, useState } from 'react';

// A styled dropdown replacing the native <select> -- same value/onChange
// contract (value is the selected option's `value`, onChange receives the
// new value), so callers swap it in without touching their own state.
// options: [{ value, label }]
export default function CustomSelect({ value, options, onChange, className = '' }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef(null);

  useEffect(() => {
    if (!open) return;
    function onDocPointerDown(e) {
      if (rootRef.current && !rootRef.current.contains(e.target)) setOpen(false);
    }
    function onKeyDown(e) {
      if (e.key === 'Escape') setOpen(false);
    }
    document.addEventListener('mousedown', onDocPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onDocPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  const current = options.find((o) => o.value === value) ?? options[0];

  return (
    <div ref={rootRef} className={`relative inline-block ${className}`}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="listbox"
        aria-expanded={open}
        className={`flex items-center gap-1.5 bg-gray-50 text-ink border rounded-md px-2 py-1 text-xs font-medium transition-colors ${
          open ? 'border-accent bg-white' : 'border-gray-100 hover:bg-gray-100'
        }`}
      >
        <span>{current?.label}</span>
        <svg
          viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
          className={`w-3 h-3 text-muted shrink-0 transition-transform ${open ? 'rotate-180' : ''}`}
        >
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </button>

      {open && (
        <div
          role="listbox"
          className="absolute left-0 z-30 mt-1 min-w-full w-max max-h-56 overflow-y-auto rounded-lg border border-gray-100 bg-white shadow-modal py-1"
        >
          {options.map((opt) => {
            const selected = opt.value === value;
            return (
              <button
                key={opt.value}
                type="button"
                role="option"
                aria-selected={selected}
                onClick={() => {
                  onChange(opt.value);
                  setOpen(false);
                }}
                className={`w-full text-left px-3 py-1.5 text-xs whitespace-nowrap transition-colors ${
                  selected ? 'bg-accent/10 text-accent font-semibold' : 'text-ink hover:bg-gray-50'
                }`}
              >
                {opt.label}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
