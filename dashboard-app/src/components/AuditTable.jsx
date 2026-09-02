import Badge, { boolTone } from './Badge.jsx';

export const AUDIT_COLS = [
  'case_id', 'timestamp', 'source', 'decline_code', 'decline_code_bucket', 'amount', 'tree_model_score',
  'routed_to_llm', 'proposed_action', 'final_action', 'guardrail_overrode', 'override_rule',
  'requires_human_review', 'guardrail_flags', 'execution_status', 'execution_mechanism', 'customer_history_source',
  'ptp_offer', 'ptp_status',
];

const BOOL_COLS = new Set(['guardrail_overrode', 'requires_human_review', 'routed_to_llm', 'signature_valid']);

// Short human labels for each trigger_category -- the raw category string
// is still available as the badge's title tooltip alongside the full
// reason sentence, so nothing is lost, just kept off the main row.
const PTP_TRIGGER_LABELS = {
  retry_failed_once: 'retry failed once',
  hard_decline: 'hard decline',
  open_promise_exists: 'open promise exists',
  restricted_tier: 'restricted tier',
  insufficient_funds_code: 'insufficient funds code',
  approaching_retry_cap: 'approaching retry cap',
  high_ltv_first_failure: 'high LTV, first failure',
  first_failure_awaiting_auto_retry: 'first failure, awaiting auto-retry',
};

function isTrueVal(v) {
  return v === true || v === 'True' || v === 'true';
}

function PtpOfferBadge({ row }) {
  const category = row.ptp_trigger_category;
  if (!category) return <span className="text-muted text-xs">--</span>;
  const label = PTP_TRIGGER_LABELS[category] || category;
  const offered = isTrueVal(row.ptp_offer_decision);
  const text = (offered ? 'PTP offered -- ' : 'PTP not offered -- ') + label;
  const tooltip = `${category}: ${row.ptp_offer_reason || ''}`;
  return <Badge tone={offered ? 'good' : 'neutral'} title={tooltip}>{text}</Badge>;
}

function Cell({ col, row }) {
  const v = row[col];
  if (BOOL_COLS.has(col)) {
    const truthy = isTrueVal(v);
    const falsy = v === false || v === 'False' || v === 'false';
    if (!truthy && !falsy) return <td className="px-2.5 py-1.5 whitespace-nowrap">{String(v ?? '')}</td>;
    return (
      <td className="px-2.5 py-1.5 whitespace-nowrap">
        <Badge tone={boolTone(col, truthy)}>{truthy ? 'true' : 'false'}</Badge>
      </td>
    );
  }
  if (col === 'source' && v === 'manual_test') {
    return <td className="px-2.5 py-1.5 whitespace-nowrap"><Badge tone="manual">manual_test</Badge></td>;
  }
  if (col === 'ptp_offer') {
    return <td className="px-2.5 py-1.5 whitespace-nowrap"><PtpOfferBadge row={row} /></td>;
  }
  if (col === 'ptp_status') {
    const isScheduled = typeof v === 'string' && v.startsWith('Scheduled for:');
    return (
      <td className={`px-2.5 py-1.5 whitespace-nowrap max-w-[260px] overflow-hidden text-ellipsis ${isScheduled ? 'text-emerald-600' : 'text-muted'}`} title={String(v ?? '')}>
        {v}
      </td>
    );
  }
  return (
    <td className="px-2.5 py-1.5 whitespace-nowrap max-w-[260px] overflow-hidden text-ellipsis" title={String(v ?? '')}>
      {v}
    </td>
  );
}

export default function AuditTable({ rows, onSelectCase }) {
  return (
    <div className="overflow-auto max-h-[560px] border border-gray-100 rounded-[10px]">
      <table className="border-collapse w-full text-xs">
        <thead>
          <tr>
            {AUDIT_COLS.map((c) => (
              <th
                key={c}
                className="sticky top-0 bg-gray-50 text-left px-2.5 py-2.5 border-b border-gray-100 whitespace-nowrap font-extrabold text-sm text-black z-10"
              >
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr>
              <td colSpan={AUDIT_COLS.length} className="text-muted text-center py-5">No rows yet.</td>
            </tr>
          )}
          {rows.map((row) => {
            const overrode = isTrueVal(row.guardrail_overrode);
            const review = isTrueVal(row.requires_human_review);
            const rowBg = overrode && review ? 'bg-amber-400/10' : overrode ? 'bg-amber-400/5' : review ? 'bg-red-400/5' : '';
            const borderColor = overrode ? 'border-l-amber-400' : review ? 'border-l-red-400' : 'border-l-transparent';
            return (
              <tr
                key={row.case_id}
                onClick={() => onSelectCase(row.case_id)}
                className={`cursor-pointer border-l-[3px] ${borderColor} ${rowBg} border-b border-gray-100 hover:bg-[#EEF1FF]`}
              >
                {AUDIT_COLS.map((c) => <Cell key={c} col={c} row={row} />)}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
