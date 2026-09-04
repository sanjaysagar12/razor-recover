// Terminal-styled viewer for a case's slice of logs/webhook_receiver.log
// (see api_case_detail's `log_lines`). Same dark window chrome as
// TerminalOutput (Run Batch / Model training consoles) so it reads as the
// same product, but parses each line's `%(asctime)s %(levelname)s
// [%(name)s] %(message)s` shape (webhook_receiver.py's logging.basicConfig
// format) to color-code by level and pull "step N/6" run_case.py markers
// out as their own highlighted lane, closer to how a real terminal log
// tails would read.
const LINE_RE = /^(\S+ \S+)\s+(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+\[([^\]]+)\]\s+(.*)$/;
const STEP_RE = /^(case_id=\S+: )?(step \d+\/\d+ -- .+?)(?=$| -- )/i;

const LEVEL_STYLE = {
  DEBUG: 'text-gray-500',
  INFO: 'text-gray-300',
  WARNING: 'text-amber-400',
  ERROR: 'text-red-400',
  CRITICAL: 'text-red-400',
};

function LogLine({ raw, num }) {
  const match = LINE_RE.exec(raw);
  if (!match) {
    // Traceback continuation lines, etc. -- no timestamp/level to parse,
    // render as raw dimmed text under whichever line preceded them.
    return (
      <div className="flex gap-3">
        <span className="shrink-0 w-8 text-right text-gray-700 select-none tabular">{num}</span>
        <span className="text-gray-500 whitespace-pre-wrap break-all">{raw}</span>
      </div>
    );
  }

  const [, ts, level, loggerName, message] = match;
  const levelCls = LEVEL_STYLE[level] || 'text-gray-300';
  const isRequest = /^REQUEST /.test(message);
  const isResponse = /^RESPONSE /.test(message);
  const stepMatch = STEP_RE.exec(message);

  return (
    <div className="flex gap-3">
      <span className="shrink-0 w-8 text-right text-gray-700 select-none tabular">{num}</span>
      <span className="flex-1 min-w-0 whitespace-pre-wrap break-all">
        <span className="text-gray-600">{ts}</span>{' '}
        <span className={`font-bold ${levelCls}`}>{level.padEnd(5, ' ')}</span>{' '}
        <span className="text-gray-600">[{loggerName}]</span>{' '}
        {isRequest && <span className="text-sky-400">&#8594; </span>}
        {isResponse && <span className="text-emerald-400">&#8592; </span>}
        {stepMatch ? (
          <>
            <span className="text-accent font-bold">{stepMatch[2]}</span>
            <span className={levelCls}>{message.slice(stepMatch[0].length)}</span>
          </>
        ) : (
          <span className={isRequest ? 'text-sky-300' : isResponse ? 'text-emerald-300' : levelCls}>{message}</span>
        )}
      </span>
    </div>
  );
}

export default function LogTrace({ lines = [], title = 'logs/webhook_receiver.log', maxHeight = '320px' }) {
  return (
    <div className="rounded-xl overflow-hidden border border-black/40 bg-[#0b0f19] shadow-card">
      <div className="flex items-center gap-3 px-3 py-2 bg-[#161b22] border-b border-white/10">
        <div className="flex gap-1.5 shrink-0">
          <span className="w-2.5 h-2.5 rounded-full bg-red-500" />
          <span className="w-2.5 h-2.5 rounded-full bg-yellow-400" />
          <span className="w-2.5 h-2.5 rounded-full bg-emerald-500" />
        </div>
        <div className="text-gray-400 text-xs font-mono truncate">{title}</div>
        <div className="ml-auto text-gray-600 text-[10px] font-mono shrink-0">{lines.length} line{lines.length === 1 ? '' : 's'}</div>
      </div>
      <div className="p-3 font-mono text-[11.5px] leading-relaxed">
        {lines.length ? (
          <div className="terminal-scroll overflow-auto" style={{ maxHeight }}>
            {lines.map((line, i) => <LogLine key={i} raw={line} num={i + 1} />)}
          </div>
        ) : (
          <div className="text-gray-500 text-xs">(no matching log lines -- may have been cleared by a reset)</div>
        )}
      </div>
    </div>
  );
}
