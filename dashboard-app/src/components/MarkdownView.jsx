// Minimal, self-contained Markdown renderer for the report-style .md files
// this app displays (demo/pitch_numbers.md) -- headings, paragraphs, bullet
// lists, tables and **bold** inline text. Not a general CommonMark
// implementation; deliberately scoped to what write_pitch_numbers() in
// pipeline/run_batch.py actually emits, styled with the app's own design
// tokens instead of a library's default stylesheet.
const HEADING_CLASSES = {
  1: 'text-xl font-extrabold text-black mt-5 mb-2 first:mt-0',
  2: 'text-base font-extrabold text-black mt-5 mb-2 first:mt-0',
  3: 'text-sm font-bold text-black mt-4 mb-1.5 first:mt-0',
};

function parseInline(text, keyPrefix = 'i') {
  const parts = [];
  const regex = /\*\*(.+?)\*\*/g;
  let lastIndex = 0;
  let match;
  let key = 0;
  while ((match = regex.exec(text))) {
    if (match.index > lastIndex) parts.push(text.slice(lastIndex, match.index));
    parts.push(<strong key={`${keyPrefix}-${key++}`}>{match[1]}</strong>);
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < text.length) parts.push(text.slice(lastIndex));
  return parts;
}

function parseRow(line) {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());
}

function Table({ lines, itemKey }) {
  const header = parseRow(lines[0]);
  const body = lines.slice(2).map(parseRow);
  return (
    <div key={itemKey} className="overflow-x-auto my-3 border border-gray-100 rounded-lg">
      <table className="w-full text-xs border-collapse">
        <thead>
          <tr>
            {header.map((h, i) => (
              <th key={i} className="text-left px-2.5 py-2 border-b border-gray-200 font-bold text-ink bg-gray-50 whitespace-nowrap">
                {parseInline(h, `th-${i}`)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {body.map((row, ri) => (
            <tr key={ri} className="border-b border-gray-100 last:border-b-0">
              {row.map((cell, ci) => (
                <td key={ci} className="px-2.5 py-1.5 text-ink whitespace-nowrap">{parseInline(cell, `td-${ri}-${ci}`)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function MarkdownView({ markdown, className = '' }) {
  if (!markdown) return null;
  const lines = markdown.replace(/\r\n/g, '\n').split('\n');
  const blocks = [];
  let i = 0;
  let key = 0;

  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i++;
      continue;
    }

    const headingMatch = line.match(/^(#{1,6})\s+(.*)/);
    if (headingMatch) {
      const level = Math.min(headingMatch[1].length, 3);
      blocks.push(
        <div key={key++} className={HEADING_CLASSES[level] || HEADING_CLASSES[3]}>
          {parseInline(headingMatch[2], `h-${key}`)}
        </div>,
      );
      i++;
      continue;
    }

    if (line.trim().startsWith('|')) {
      const tableLines = [];
      while (i < lines.length && lines[i].trim().startsWith('|')) {
        tableLines.push(lines[i]);
        i++;
      }
      if (tableLines.length >= 2) blocks.push(<Table key={key++} lines={tableLines} itemKey={key} />);
      continue;
    }

    if (/^-\s+/.test(line.trim())) {
      const items = [];
      while (i < lines.length && /^-\s+/.test(lines[i].trim())) {
        items.push(lines[i].trim().replace(/^-\s+/, ''));
        i++;
      }
      blocks.push(
        <ul key={key++} className="list-disc pl-5 my-2 space-y-1 text-[13px] text-ink leading-relaxed">
          {items.map((item, idx) => <li key={idx}>{parseInline(item, `li-${key}-${idx}`)}</li>)}
        </ul>,
      );
      continue;
    }

    const paraLines = [];
    while (
      i < lines.length
      && lines[i].trim()
      && !/^#{1,6}\s+/.test(lines[i])
      && !lines[i].trim().startsWith('|')
      && !/^-\s+/.test(lines[i].trim())
    ) {
      paraLines.push(lines[i]);
      i++;
    }
    blocks.push(
      <p key={key++} className="text-[13px] text-ink leading-relaxed my-2">
        {parseInline(paraLines.join(' '), `p-${key}`)}
      </p>,
    );
  }

  return <div className={className}>{blocks}</div>;
}
