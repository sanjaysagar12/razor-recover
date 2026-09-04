import { useState } from 'react';

// Status colors (fixed, reserved -- see the dataviz skill's palette.md):
// this series means pass/fail (a case either can or can't be retried), so it
// wears status tokens rather than a categorical hue.
const CAN_RETRY_COLOR = '#0ca30c';
const CANNOT_RETRY_COLOR = '#d03b3b';
const TRACK_COLOR = '#eef0f5';

// Wider than a plain surface-gap and paired with round caps below -- reads
// as two separated rounded arcs ("exploded" donut) rather than one ring
// split by a hairline.
const ROW_GEOMETRY = { size: 86, stroke: 13, gap: 16 };
const STACKED_GEOMETRY = { size: 132, stroke: 18, gap: 22 };

function LegendLine({ color, pct, label, active, onHover, onLeave }) {
  return (
    <button
      type="button"
      onMouseEnter={onHover}
      onMouseLeave={onLeave}
      onFocus={onHover}
      onBlur={onLeave}
      className={`flex items-center gap-2 text-left rounded px-1 py-0.5 -mx-1 transition-colors ${active ? 'bg-gray-50' : ''}`}
    >
      <span className="w-2.5 h-2.5 rounded-full shrink-0" style={{ backgroundColor: color }} />
      <span className="text-[13px] leading-none whitespace-nowrap">
        <span className="font-bold text-black">{pct}%</span>{' '}
        <span className="text-muted">{label}</span>
      </span>
    </button>
  );
}

// Retry-eligibility breakdown -- headline number (total cases) + subtitle,
// a legend, and an exploded-donut (no center label). Two layouts:
//   row     -- (default) text block left, donut right, side by side --
//              used when the card shares a row with a neighbor (narrow).
//   stacked -- title/number/subtitle on top, a bigger centered donut below,
//              legend at the bottom -- used when this card gets a full
//              column's height to itself.
export default function RetryDonutChart({ canRetry, cannotRetry, totalCases, stacked = false }) {
  const [hovered, setHovered] = useState(null); // 'can' | 'cannot' | null
  const total = canRetry + cannotRetry;
  const pctCan = total > 0 ? Math.round((canRetry / total) * 100) : 0;
  const pctCannot = total > 0 ? 100 - pctCan : 0;

  const { size, stroke, gap } = stacked ? STACKED_GEOMETRY : ROW_GEOMETRY;
  const radius = (size - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  const center = size / 2;

  const canLen = total > 0 ? circumference * (canRetry / total) : 0;
  const cannotLen = circumference - canLen;
  // Each arc is inset by `gap` only on the edge where it meets the OTHER
  // arc -- both arcs inset the same way, so the two insets land on the two
  // seams where the ring wraps around, never as a border around either mark.
  const bothPresent = cannotRetry > 0 && canRetry > 0;
  const canDash = Math.max(0, canLen - (bothPresent ? gap : 0));
  const cannotDash = Math.max(0, cannotLen - (bothPresent ? gap : 0));

  const chart =
    total === 0 ? (
      <div className="text-muted text-xs text-center shrink-0" style={{ width: size }}>
        No decided cases yet.
      </div>
    ) : (
      <svg
        width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="shrink-0"
        role="img" aria-label={`${pctCan}% of cases can be retried, ${pctCannot}% can't`}
      >
        <circle cx={center} cy={center} r={radius} fill="none" stroke={TRACK_COLOR} strokeWidth={stroke} />
        <g transform={`rotate(-90 ${center} ${center})`}>
          {cannotRetry > 0 && (
            <circle
              cx={center} cy={center} r={radius} fill="none"
              stroke={CANNOT_RETRY_COLOR} strokeWidth={stroke} strokeLinecap="round"
              strokeDasharray={`${cannotDash} ${circumference - cannotDash}`}
              strokeDashoffset={-canLen}
              style={{ opacity: hovered === 'can' ? 0.35 : 1, transition: 'opacity 120ms' }}
            >
              <title>{`Can't retry: ${cannotRetry} (${pctCannot}%)`}</title>
            </circle>
          )}
          {canRetry > 0 && (
            <circle
              cx={center} cy={center} r={radius} fill="none"
              stroke={CAN_RETRY_COLOR} strokeWidth={stroke} strokeLinecap="round"
              strokeDasharray={`${canDash} ${circumference - canDash}`}
              style={{ opacity: hovered === 'cannot' ? 0.35 : 1, transition: 'opacity 120ms' }}
            >
              <title>{`Can retry: ${canRetry} (${pctCan}%)`}</title>
            </circle>
          )}
        </g>
      </svg>
    );

  const legendItems = total > 0 && (
    <>
      <LegendLine
        color={CAN_RETRY_COLOR} pct={pctCan} label="Can retry"
        active={hovered === 'can'} onHover={() => setHovered('can')} onLeave={() => setHovered(null)}
      />
      <LegendLine
        color={CANNOT_RETRY_COLOR} pct={pctCannot} label="Can't retry"
        active={hovered === 'cannot'} onHover={() => setHovered('cannot')} onLeave={() => setHovered(null)}
      />
    </>
  );

  const header = (
    <div>
      <div className="text-[15px] font-bold text-black leading-tight">Retry eligibility</div>
      <div className="text-[28px] font-extrabold tabular text-black leading-tight mt-1">{totalCases ?? 0}</div>
      <div className="text-[12px] font-bold text-black mt-0.5 leading-tight">cases decided</div>
    </div>
  );

  if (stacked) {
    return (
      <div className="bg-white border border-gray-100 rounded-2xl shadow-card p-[18px] h-full flex flex-col">
        {header}
        <div className="flex-1 flex items-center justify-center py-3">{chart}</div>
        {legendItems && <div className="flex items-center justify-center gap-4">{legendItems}</div>}
      </div>
    );
  }

  return (
    <div className="bg-white border border-gray-100 rounded-2xl shadow-card p-[18px] flex-1 min-w-0">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          {header}
          {legendItems && <div className="flex flex-col gap-1 mt-3">{legendItems}</div>}
        </div>
        {chart}
      </div>
    </div>
  );
}
