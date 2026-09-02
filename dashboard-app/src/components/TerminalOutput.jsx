// Shared terminal-chrome block for any "console output" display in the app
// (Run Batch, Model training) -- same dark window / dot-header / monospace
// look as the custom-trigger terminal on the Dashboard, so captured stdout
// always reads as the same product.
export default function TerminalOutput({ title, promptLine, output, error, emptyMessage, maxHeight = '400px' }) {
  const hasOutput = Boolean(output || error);

  return (
    <div className="rounded-xl overflow-hidden border border-black/40 bg-[#0b0f19] shadow-card">
      <div className="flex items-center gap-3 px-3 py-2 bg-[#161b22] border-b border-white/10">
        <div className="flex gap-1.5 shrink-0">
          <span className="w-2.5 h-2.5 rounded-full bg-red-500" />
          <span className="w-2.5 h-2.5 rounded-full bg-yellow-400" />
          <span className="w-2.5 h-2.5 rounded-full bg-emerald-500" />
        </div>
        <div className="text-gray-400 text-xs font-mono truncate">{title}</div>
      </div>
      <div className="p-4 font-mono text-[12px] leading-relaxed">
        {promptLine && (
          <div className="text-gray-300 mb-2">
            <span className="text-emerald-400">$</span> {promptLine}
          </div>
        )}
        {hasOutput ? (
          <pre
            className="terminal-scroll text-gray-300 m-0 overflow-auto whitespace-pre-wrap"
            style={{ maxHeight }}
          >
            {output || ''}
            {error && <span className="text-red-400">{'\n'}Error: {error}</span>}
          </pre>
        ) : (
          <div className="text-gray-500 text-xs">{emptyMessage}</div>
        )}
      </div>
    </div>
  );
}
