const TONE_CLASSES = {
  neutral: 'bg-slate-400/15 text-slate-500',
  llm: 'bg-accent/15 text-accent',
  override: 'bg-amber-400/25 text-amber-700',
  review: 'bg-red-400/15 text-red-500',
  good: 'bg-emerald-500/15 text-emerald-600',
  bad: 'bg-red-400/15 text-red-500',
  manual: 'bg-violet-500/15 text-violet-600',
};

export default function Badge({ tone = 'neutral', children, title }) {
  return (
    <span
      title={title}
      className={`inline-block px-2 py-0.5 rounded-full text-[10px] font-bold whitespace-nowrap ${TONE_CLASSES[tone] || TONE_CLASSES.neutral}`}
    >
      {children}
    </span>
  );
}

// Semantic badge coloring -- the same true/false value means something
// different depending on which column it's in: routed_to_llm=blue (AI
// path), guardrail_overrode=amber (guardrail caught something -- the
// single most important signal in this table), requires_human_review=red
// (needs a human), signature_valid=green/red (transport-layer
// correctness), everything else neutral.
export function boolTone(col, isTrue) {
  if (col === 'routed_to_llm') return isTrue ? 'llm' : 'neutral';
  if (col === 'guardrail_overrode') return isTrue ? 'override' : 'neutral';
  if (col === 'requires_human_review') return isTrue ? 'review' : 'neutral';
  if (col === 'signature_valid') return isTrue ? 'good' : 'bad';
  return 'neutral';
}
