export default function DatasetTable({ columns, rows, maxHeight = '420px' }) {
  return (
    <div className="light-scroll overflow-auto border border-gray-100 rounded-[10px]" style={{ maxHeight }}>
      <table className="border-collapse w-full text-xs">
        <thead>
          <tr>
            {columns.map((c) => (
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
              <td colSpan={columns.length} className="text-muted text-center py-5">No rows.</td>
            </tr>
          )}
          {rows.map((row, i) => (
            <tr key={i} className="border-b border-gray-100 hover:bg-gray-50">
              {columns.map((c) => (
                <td
                  key={c}
                  className="px-2.5 py-1.5 whitespace-nowrap max-w-[200px] overflow-hidden text-ellipsis"
                  title={String(row[c] ?? '')}
                >
                  {String(row[c] ?? '')}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
