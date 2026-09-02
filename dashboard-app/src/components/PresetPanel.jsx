import { useState } from 'react';
import { PRESETS } from '../presets.js';

const TONE_BG = {
  override: 'bg-amber-400/10',
  review: 'bg-red-400/10',
  default: 'bg-gray-50',
};
const TONE_TEXT = {
  override: 'text-amber-700',
  review: 'text-red-600',
  default: 'text-muted',
};
const TONE_TAG = {
  override: 'bg-amber-400/20 text-amber-700',
  review: 'bg-red-400/15 text-red-600',
  default: 'bg-gray-200 text-muted',
};

export default function PresetPanel({ onTrigger }) {
  const [busyIndex, setBusyIndex] = useState(null);

  async function handleClick(preset, idx) {
    setBusyIndex(idx);
    try {
      await onTrigger(preset.payload);
    } finally {
      setBusyIndex(null);
    }
  }

  return (
    <div className="flex items-start gap-3">
      {PRESETS.map((p, idx) => {
        const openLeft = idx >= PRESETS.length - 2;
        return (
          <div key={p.name} className="group relative flex-1 min-w-0">
            <button
              disabled={busyIndex === idx}
              onClick={() => handleClick(p, idx)}
              className={`relative w-full h-16 text-left rounded-2xl border border-gray-100 p-4 flex items-center justify-between gap-2 ${TONE_BG[p.tone] || TONE_BG.default}
                disabled:opacity-50 disabled:cursor-not-allowed transition-colors`}
            >
              <div className="font-bold text-lg text-black leading-snug truncate">{p.name}</div>
              <span className="w-8 h-8 rounded-full bg-white/80 flex items-center justify-center text-black shadow-card group-hover:bg-white transition-colors shrink-0">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="w-3.5 h-3.5">
                  <line x1="7" y1="17" x2="17" y2="7" /><polyline points="7 7 17 7 17 17" />
                </svg>
              </span>
            </button>
            <div
              className={`absolute top-0 w-72 rounded-2xl border border-gray-100 p-4 shadow-card bg-white
                opacity-0 scale-95 pointer-events-none
                group-hover:opacity-100 group-hover:scale-100 group-hover:pointer-events-auto
                transition-all duration-200 ease-out z-30
                ${openLeft ? 'right-full mr-2 origin-right' : 'left-full ml-2 origin-left'}`}
            >
              <span className={`inline-block px-2 py-0.5 rounded-full text-[10px] font-mono font-semibold mb-2 ${TONE_TAG[p.tone] || TONE_TAG.default}`}>
                {p.rule}
              </span>
              <div className={`text-xs leading-relaxed ${TONE_TEXT[p.tone] || TONE_TEXT.default}`}>{p.description}</div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
