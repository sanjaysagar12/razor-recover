const selectClass = 'bg-gray-50 text-ink border border-gray-100 rounded-md px-2 py-1 text-xs';

export default function FiltersBar({ filters, onChange, execStatusOptions }) {
  return (
    <div className="flex gap-5 flex-wrap items-center text-xs">
      <span className="flex items-center gap-1.5">
        <label className="text-muted">routed_to_llm</label>
        <select
          className={selectClass}
          value={filters.routed}
          onChange={(e) => onChange({ ...filters, routed: e.target.value })}
        >
          <option value="">all</option>
          <option value="true">true</option>
          <option value="false">false</option>
        </select>
      </span>
      <span className="flex items-center gap-1.5">
        <label className="text-muted">guardrail_overrode</label>
        <select
          className={selectClass}
          value={filters.overrode}
          onChange={(e) => onChange({ ...filters, overrode: e.target.value })}
        >
          <option value="">all</option>
          <option value="true">true</option>
          <option value="false">false</option>
        </select>
      </span>
      <span className="flex items-center gap-1.5">
        <label className="text-muted">execution_status</label>
        <select
          className={selectClass}
          value={filters.execStatus}
          onChange={(e) => onChange({ ...filters, execStatus: e.target.value })}
        >
          <option value="">all</option>
          {execStatusOptions.map((v) => (
            <option key={v} value={v}>{v}</option>
          ))}
        </select>
      </span>
    </div>
  );
}
