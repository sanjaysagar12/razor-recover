import CustomSelect from './CustomSelect.jsx';

const BOOL_OPTIONS = [
  { value: '', label: 'all' },
  { value: 'true', label: 'true' },
  { value: 'false', label: 'false' },
];

export default function FiltersBar({ filters, onChange, execStatusOptions }) {
  const execStatusOpts = [
    { value: '', label: 'all' },
    ...execStatusOptions.map((v) => ({ value: v, label: v })),
  ];

  return (
    <div className="flex gap-5 flex-wrap items-center text-xs">
      <span className="flex items-center gap-1.5">
        <label className="text-muted">routed_to_llm</label>
        <CustomSelect
          value={filters.routed}
          options={BOOL_OPTIONS}
          onChange={(v) => onChange({ ...filters, routed: v })}
        />
      </span>
      <span className="flex items-center gap-1.5">
        <label className="text-muted">guardrail_overrode</label>
        <CustomSelect
          value={filters.overrode}
          options={BOOL_OPTIONS}
          onChange={(v) => onChange({ ...filters, overrode: v })}
        />
      </span>
      <span className="flex items-center gap-1.5">
        <label className="text-muted">execution_status</label>
        <CustomSelect
          value={filters.execStatus}
          options={execStatusOpts}
          onChange={(v) => onChange({ ...filters, execStatus: v })}
        />
      </span>
    </div>
  );
}
