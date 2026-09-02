import MarkdownView from '../components/MarkdownView.jsx';
import TerminalOutput from '../components/TerminalOutput.jsx';
import { useRunBatchData } from '../data/useRunBatchData.js';

const STATUS_LABEL = { idle: 'Idle', running: 'Running', done: 'Done', error: 'Error' };
const DOT_CLASSES = { idle: 'bg-slate-400', running: 'bg-accent', done: 'bg-emerald-500', error: 'bg-red-400' };

function fmtDuration(seconds) {
  if (seconds == null) return null;
  return seconds < 60 ? `${seconds.toFixed(1)}s` : `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
}

export default function RunBatchPage() {
  const { status, busy, run: handleRun } = useRunBatchData();
  const effectiveStatus = status?.status || 'idle';

  return (
    <div className="flex-1 min-w-0">
      <div className="flex items-end justify-between flex-wrap gap-5 px-7 pt-6 pb-2">
        <div className="flex flex-col gap-2.5">
          <h1 className="text-[34px] font-extrabold m-0 tracking-tight">Run Batch</h1>
          <div className="text-muted text-[13px]">
            pipeline/run_batch.py &middot; scores + SHAP + routes + guardrails the 280-row synthetic batch, then writes logs/audit_log.csv and demo/pitch_numbers.md
          </div>
        </div>
        <div className="flex items-center gap-3">
          <span className="inline-flex items-center gap-1.5 text-[11px] font-bold text-muted">
            <span className={`w-1.5 h-1.5 rounded-full ${DOT_CLASSES[effectiveStatus]} ${effectiveStatus === 'running' ? 'pulse-dot relative' : ''}`} />
            {STATUS_LABEL[effectiveStatus]}
          </span>
          <button
            type="button"
            onClick={handleRun}
            disabled={busy}
            title="Runs pipeline/run_batch.py against the 280-row synthetic batch"
            className="px-4 py-2 rounded-lg bg-accent text-white text-sm font-bold hover:brightness-110 disabled:opacity-50"
          >
            {busy ? 'Running...' : status?.finished_at || status?.generated_at ? 'Run again' : 'Run batch'}
          </button>
        </div>
      </div>

      <section className="px-7 pb-7 pt-2">
        <div className="bg-white border border-gray-100 rounded-2xl shadow-card p-[18px] mb-4">
          <div className="text-[11px] font-semibold text-muted uppercase tracking-wider mb-1">Last run</div>
          {status?.finished_at || status?.generated_at ? (
            <div className="flex flex-wrap gap-x-8 gap-y-2 mt-2">
              <div>
                <div className="text-[10px] text-muted uppercase tracking-wide">Finished</div>
                <div className="text-sm font-bold mt-0.5">
                  {new Date(status.finished_at || status.generated_at).toLocaleString()}
                </div>
              </div>
              {status.duration_seconds != null && (
                <div>
                  <div className="text-[10px] text-muted uppercase tracking-wide">Duration</div>
                  <div className="text-sm font-bold mt-0.5">{fmtDuration(status.duration_seconds)}</div>
                </div>
              )}
              {status.n_cases != null && (
                <div>
                  <div className="text-[10px] text-muted uppercase tracking-wide">Cases</div>
                  <div className="text-sm font-bold mt-0.5">{status.n_cases}</div>
                </div>
              )}
            </div>
          ) : (
            <div className="text-muted text-sm mt-2">Never run yet -- click "Run batch" to kick off a run.</div>
          )}
          {effectiveStatus === 'error' && status?.error && (
            <div className="text-red-500 text-xs mt-3 bg-red-400/5 border border-red-400/20 rounded-lg px-3 py-2">{status.error}</div>
          )}
        </div>

        <div className="bg-white border border-gray-100 rounded-2xl shadow-card p-[18px] mb-4">
          <div className="flex items-center gap-2 mb-4">
            <h2 className="text-2xl font-extrabold text-black leading-tight m-0">Console output</h2>
            <span className="group/info relative inline-flex">
              <button
                type="button"
                className="w-5 h-5 rounded-full border border-gray-300 text-muted text-[11px] font-bold flex items-center justify-center hover:bg-gray-50 hover:text-black"
                aria-label="Console output info"
              >
                i
              </button>
              <span className="pointer-events-none absolute left-0 top-full mt-2 w-72 rounded-lg bg-black text-white text-[11px] leading-relaxed px-3 py-2 opacity-0 group-hover/info:opacity-100 transition-opacity z-30 shadow-lg">
                Captured stdout from the most recent run -- stored in logs/run_batch_state.json, so it survives a page refresh and a server restart.
              </span>
            </span>
          </div>
          <TerminalOutput
            title="pipeline/run_batch.py"
            promptLine="python pipeline/run_batch.py"
            output={status?.output}
            error={status?.error}
            emptyMessage='No output captured yet -- click "Run batch" above to trigger a run and see live output here.'
          />
        </div>

        <div className="bg-white border border-gray-100 rounded-2xl shadow-card p-[18px] mb-4">
          <div className="flex items-center justify-between gap-2 mb-4 flex-wrap">
            <div className="flex items-center gap-2">
              <h2 className="text-2xl font-extrabold text-black leading-tight m-0">Pitch numbers</h2>
              <span className="group/info relative inline-flex">
                <button
                  type="button"
                  className="w-5 h-5 rounded-full border border-gray-300 text-muted text-[11px] font-bold flex items-center justify-center hover:bg-gray-50 hover:text-black"
                  aria-label="Pitch numbers info"
                >
                  i
                </button>
                <span className="pointer-events-none absolute left-0 top-full mt-2 w-72 rounded-lg bg-black text-white text-[11px] leading-relaxed px-3 py-2 opacity-0 group-hover/info:opacity-100 transition-opacity z-30 shadow-lg">
                  demo/pitch_numbers.md &middot; batch simulation report, read straight off disk (survives a server restart).
                </span>
              </span>
            </div>
            {status?.generated_at && <div className="text-[11px] text-muted">generated {status.generated_at}</div>}
          </div>
          {status?.pitch_numbers ? (
            <div className="light-scroll bg-gray-50 border border-gray-100 rounded-xl px-4 py-1 max-h-[560px] overflow-auto">
              <MarkdownView markdown={status.pitch_numbers} />
            </div>
          ) : (
            <div className="text-muted text-xs bg-gray-50 border border-gray-100 rounded-xl p-4">
              demo/pitch_numbers.md hasn't been written yet -- run the batch to generate it.
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
