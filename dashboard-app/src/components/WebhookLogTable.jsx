import Badge, { boolTone } from './Badge.jsx';

export const WEBHOOK_COLS = ['timestamp', 'event_type', 'signature_valid', 'case_id', 'outcome', 'error_detail'];

function isTrueVal(v) {
  return v === true || v === 'True' || v === 'true';
}

function Cell({ col, row }) {
  const v = row[col];
  if (col === 'signature_valid') {
    const truthy = isTrueVal(v);
    const falsy = v === false || v === 'False' || v === 'false';
    if (!truthy && !falsy) return <td className="px-2.5 py-1.5 whitespace-nowrap">{String(v ?? '')}</td>;
    return (
      <td className="px-2.5 py-1.5 whitespace-nowrap">
        <Badge tone={boolTone(col, truthy)}>{truthy ? 'true' : 'false'}</Badge>
      </td>
    );
  }
  return (
    <td className="px-2.5 py-1.5 whitespace-nowrap max-w-[260px] overflow-hidden text-ellipsis" title={String(v ?? '')}>
      {v}
    </td>
  );
}

export default function WebhookLogTable({ rows }) {
  return (
    <div className="overflow-auto max-h-[460px] border border-gray-100 rounded-[10px]">
      <table className="border-collapse w-full text-xs">
        <thead>
          <tr>
            {WEBHOOK_COLS.map((c) => (
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
              <td colSpan={WEBHOOK_COLS.length} className="text-muted text-center py-5">No rows yet.</td>
            </tr>
          )}
          {rows.map((row, i) => (
            <tr key={i} className="border-b border-gray-100 hover:bg-gray-50">
              {WEBHOOK_COLS.map((c) => <Cell key={c} col={c} row={row} />)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
