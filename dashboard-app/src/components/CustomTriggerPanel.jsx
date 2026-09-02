import { useState } from 'react';
import { PRESETS, buildRazorpayShapeExample } from '../presets.js';

// Two independent terminal "sessions" -- one per trigger endpoint. Each
// keeps its own draft payload, run state and last response, so switching
// tabs never loses what the user was editing in the other one.
const SESSIONS = [
  {
    id: 'internal',
    label: 'trigger-test-case',
    endpoint: '/api/trigger-test-case',
    defaultPayload: PRESETS[0].payload,
  },
  {
    id: 'razorpay',
    label: 'trigger-webhook-shaped',
    endpoint: '/api/trigger-webhook-shaped',
    defaultPayload: buildRazorpayShapeExample(),
  },
];

function initialSessionState() {
  const state = {};
  SESSIONS.forEach((s) => {
    state[s.id] = { draft: JSON.stringify(s.defaultPayload, null, 2), response: null, busy: false, validation: '' };
  });
  return state;
}

export default function CustomTriggerPanel({ onTrigger }) {
  const [activeId, setActiveId] = useState(SESSIONS[0].id);
  const [sessions, setSessions] = useState(initialSessionState);

  const active = SESSIONS.find((s) => s.id === activeId);
  const state = sessions[activeId];
  const origin = typeof window !== 'undefined' ? window.location.origin : '';

  function patchSession(id, patch) {
    setSessions((prev) => ({ ...prev, [id]: { ...prev[id], ...patch } }));
  }

  async function handleRun() {
    let payload;
    try {
      payload = JSON.parse(state.draft);
    } catch (e) {
      patchSession(activeId, { validation: 'Invalid JSON: ' + e.message });
      return;
    }
    patchSession(activeId, { validation: '', busy: true });
    const data = await onTrigger(payload, active.endpoint);
    patchSession(activeId, { busy: false, response: data });
  }

  function handleKeyDown(e) {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault();
      handleRun();
    }
  }

  return (
    <details open className="mt-5 group/details">
      <summary className="cursor-pointer text-muted text-xs select-none font-mono">$ custom trigger (terminal)</summary>

      <div className="mt-2.5 rounded-xl overflow-hidden border border-black/40 bg-[#0b0f19] shadow-card">
        <div className="flex items-center gap-3 px-3 py-2 bg-[#161b22] border-b border-white/10">
          <div className="flex gap-1.5 shrink-0">
            <span className="w-2.5 h-2.5 rounded-full bg-red-500" />
            <span className="w-2.5 h-2.5 rounded-full bg-yellow-400" />
            <span className="w-2.5 h-2.5 rounded-full bg-emerald-500" />
          </div>
          <div className="flex gap-1">
            {SESSIONS.map((s) => (
              <button
                key={s.id}
                type="button"
                onClick={() => setActiveId(s.id)}
                className={`px-3 py-1 rounded-md text-xs font-mono transition-colors ${
                  s.id === activeId ? 'bg-white/10 text-emerald-400' : 'text-gray-500 hover:text-gray-300'
                }`}
              >
                {s.label}
              </button>
            ))}
          </div>
        </div>

        <div className="p-4 font-mono text-[12.5px] leading-relaxed">
          <div className="text-gray-300">
            <span className="text-emerald-400">$</span> curl -X POST {origin}{active.endpoint} \
          </div>
          <div className="text-gray-300 pl-4">-H "Content-Type: application/json" \</div>
          <div className="text-gray-300 pl-4">-d '</div>
          <textarea
            spellCheck={false}
            value={state.draft}
            onChange={(e) => patchSession(activeId, { draft: e.target.value })}
            onKeyDown={handleKeyDown}
            className="terminal-scroll w-full min-h-[180px] max-h-[360px] bg-transparent focus:bg-white/[0.04] text-gray-100 pl-6 py-1 resize-y outline-none border-none font-mono text-[12.5px] leading-relaxed"
          />
          <div className="text-gray-300">'</div>

          <div className="flex items-center gap-3 mt-3">
            <button
              type="button"
              onClick={handleRun}
              disabled={state.busy}
              className="text-emerald-400 hover:text-emerald-300 disabled:opacity-50 font-bold"
            >
              {state.busy ? '$ running...' : '$ press to run ▶'}
            </button>
            <span className="text-gray-600 text-[11px]">(or Ctrl/Cmd + Enter)</span>
            {state.validation && <span className="text-red-400 text-xs">{state.validation}</span>}
          </div>

          {state.response && (
            <pre className="terminal-scroll mt-3 pt-3 border-t border-white/10 text-gray-400 text-[11px] max-h-[220px] overflow-auto whitespace-pre-wrap">
              {JSON.stringify(state.response, null, 2)}
            </pre>
          )}
        </div>
      </div>
    </details>
  );
}
