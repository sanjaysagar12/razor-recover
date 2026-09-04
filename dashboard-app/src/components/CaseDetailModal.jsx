import { useCaseDetail } from '../data/useCaseDetail.js';
import LogTrace from './LogTrace.jsx';

function fmtBool(v) {
  if (v === true || v === 'True') return <Badge tone="good">true</Badge>;
  if (v === false || v === 'False') return <Badge tone="neutral">false</Badge>;
  return <Badge tone="neutral">--</Badge>;
}

// Local tiny badge (avoids a circular import with AuditTable's richer one).
function Badge({ tone, children }) {
  const cls = {
    good: 'bg-emerald-500/15 text-emerald-600',
    neutral: 'bg-slate-400/15 text-slate-500',
  }[tone] || 'bg-slate-400/15 text-slate-500';
  return <span className={`inline-block px-2 py-0.5 rounded-full text-[10px] font-bold ${cls}`}>{children}</span>;
}

function Pill({ tone, children }) {
  const cls = {
    llm: 'bg-accent/15 text-accent',
    template: 'bg-slate-400/15 text-muted',
    amber: 'bg-amber-400/20 text-amber-700',
    red: 'bg-red-400/15 text-red-500',
    green: 'bg-emerald-500/15 text-emerald-600',
  }[tone] || 'bg-slate-400/15 text-muted';
  return <span className={`inline-block px-2.5 py-0.5 rounded-full text-[11px] font-bold ${cls}`}>{children}</span>;
}

function ShapTable({ jsonStr }) {
  let features;
  try { features = JSON.parse(jsonStr); } catch { return <div className="text-muted text-sm">(none)</div>; }
  if (!Array.isArray(features) || !features.length) return <div className="text-muted text-sm">(none)</div>;
  return (
    <table className="w-full text-xs border-collapse">
      <tbody>
        <tr><td className="p-1.5 font-bold">feature</td><td className="p-1.5 font-bold">value</td><td className="p-1.5 font-bold">shap_value</td></tr>
        {features.map((f, i) => {
          const shap = Number(f.shap_value);
          const cls = shap >= 0 ? 'text-emerald-600' : 'text-red-500';
          const sign = shap >= 0 ? '+' : '';
          return (
            <tr key={i} className="border-b border-gray-100">
              <td className="p-1.5">{f.feature}</td>
              <td className="p-1.5">{String(f.value)}</td>
              <td className={`p-1.5 ${cls}`}>{sign}{shap.toFixed(4)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function StepShell({ num, active, children }) {
  return (
    <div className="grid grid-cols-[34px_1fr] gap-3.5">
      <div className="relative flex justify-center">
        <div className={`w-[30px] h-[30px] rounded-full flex items-center justify-center font-bold text-xs border-2 z-10 shrink-0 ${active}`}>
          {num}
        </div>
      </div>
      <div className="pb-5 min-w-0">{children}</div>
    </div>
  );
}

function Stepper({ row }) {
  const routedToLlm = row.routed_to_llm === true || row.routed_to_llm === 'True';
  const overrode = row.guardrail_overrode === true || row.guardrail_overrode === 'True';
  const needsReview = row.requires_human_review === true || row.requires_human_review === 'True';
  const score = Number(row.tree_model_score);
  const scorePct = Number.isFinite(score) ? Math.max(0, Math.min(100, score * 100)) : 0;

  let guardrailTone = { active: 'bg-emerald-500 text-white border-emerald-500', card: 'bg-gray-50 border-gray-100' };
  let guardrailTitle = 'Guardrail check -- passed';
  let guardrailBody = (
    <>
      <div className="mt-2"><Pill tone="green">No intervention needed</Pill></div>
      <div className="text-muted text-xs mt-2">final_action matches proposed_action ({row.final_action}).</div>
    </>
  );
  if (overrode) {
    guardrailTone = { active: 'bg-amber-400 text-[#3A2600] border-amber-400', card: 'bg-amber-50 border-amber-300/50' };
    guardrailTitle = 'Guardrail check -- overrode the proposed action';
    guardrailBody = (
      <>
        <div className="mt-2"><Pill tone="amber">Guardrail overrode this case</Pill></div>
        <div className="flex flex-wrap gap-x-5 gap-y-3 mt-2.5">
          <div><div className="text-[10px] text-muted uppercase tracking-wide">override_rule</div><div className="text-[13px] font-bold mt-0.5">{row.override_rule || '--'}</div></div>
          <div><div className="text-[10px] text-muted uppercase tracking-wide">proposed_action</div><div className="text-[13px] font-bold mt-0.5">{row.proposed_action}</div></div>
          <div><div className="text-[10px] text-muted uppercase tracking-wide">final_action</div><div className="text-[13px] font-bold mt-0.5">{row.final_action}</div></div>
        </div>
        <div className="text-muted text-xs mt-2">guardrail_flags: {row.guardrail_flags || '--'}</div>
      </>
    );
  } else if (needsReview) {
    guardrailTone = { active: 'bg-red-400 text-white border-red-400', card: 'bg-red-50 border-red-300/40' };
    guardrailTitle = 'Guardrail check -- flagged for human review';
    guardrailBody = (
      <>
        <div className="mt-2"><Pill tone="red">Requires human review</Pill></div>
        <div className="text-muted text-xs mt-2">guardrail_flags: {row.guardrail_flags || '--'}</div>
      </>
    );
  }

  const execTone = /fail/i.test(row.execution_status || '') ? 'red'
    : /schedul|success|logged/i.test(row.execution_status || '') ? 'green' : 'template';

  const activeNum = 'bg-[#E4E9FB] text-ink border-[#C7CDE0]';
  const activeCard = 'bg-gray-50 border-gray-100';

  return (
    <div className="mb-2 flex flex-col gap-0">
      <StepShell num="1" active={activeNum}>
        <div className={`rounded-[10px] border p-3.5 ${activeCard}`}>
          <div className="font-bold text-[13.5px]">Tree model score</div>
          <div className="text-muted text-xs mt-0.5">decline_code={row.decline_code} ({row.decline_code_bucket}, ambiguous={String(row.decline_code_is_ambiguous)}) &middot; amount={row.amount}</div>
          <div className="flex items-center gap-2.5 mt-2">
            <div className="flex-1 h-1.5 rounded-full bg-gray-100 overflow-hidden">
              <div className="h-full bg-accent rounded-full" style={{ width: `${scorePct}%` }} />
            </div>
            <div className="text-[15px] font-bold tabular min-w-[52px] text-right">{Number.isFinite(score) ? score.toFixed(4) : '--'}</div>
          </div>
          <details className="mt-2.5">
            <summary className="cursor-pointer text-muted text-xs font-semibold select-none">SHAP top features</summary>
            <div className="mt-2"><ShapTable jsonStr={row.tree_model_top_features} /></div>
          </details>
        </div>
      </StepShell>

      <StepShell num="2" active={activeNum}>
        <div className={`rounded-[10px] border p-3.5 ${activeCard}`}>
          <div className="font-bold text-[13.5px]">Confidence gate</div>
          <div className="text-muted text-xs mt-0.5">{row.routing_rationale || 'Routing rationale not recorded.'}</div>
          <div className="mt-2">{routedToLlm ? <Pill tone="llm">Routed to LLM</Pill> : <Pill tone="template">Resolved by template</Pill>}</div>
        </div>
      </StepShell>

      <StepShell num="3" active={routedToLlm ? 'bg-accent text-white border-accent' : activeNum}>
        <div className={`rounded-[10px] border p-3.5 ${routedToLlm ? 'bg-[#EEF1FF] border-accent/40' : activeCard}`}>
          {routedToLlm ? (
            <>
              <div className="font-bold text-[13.5px]">AI reasoning (LLM)</div>
              <div className="text-muted text-xs mt-0.5">
                recommended_action={row.llm_recommended_action || '--'} &middot; confidence={row.llm_confidence ?? '--'} &middot; schema_valid={fmtBool(row.llm_schema_valid)}
              </div>
              <div className="bg-[#EEF1FF] border-l-[3px] border-accent rounded-md px-3.5 py-3 text-[12.5px] leading-relaxed whitespace-pre-wrap mt-2.5">
                {row.llm_reasoning_summary || '(LLM routed but no reasoning_summary captured -- likely a schema-validation fallback; check llm_schema_valid.)'}
              </div>
            </>
          ) : (
            <>
              <div className="font-bold text-[13.5px]">Template action</div>
              <div className="text-muted text-xs mt-0.5">Resolved directly from the tree model's score against threshold -- no AI reasoning involved.</div>
              <div className="mt-2"><Pill tone="template">proposed_action = {row.proposed_action}</Pill></div>
            </>
          )}
          <div className="opacity-70 text-[11.5px] text-muted mt-2.5 pt-2.5 border-t border-dashed border-gray-200">
            {routedToLlm
              ? <><b>Template action</b> -- skipped (case was routed to the LLM instead).</>
              : <><b>AI reasoning (LLM)</b> -- skipped (tree model confidence was sufficient; no LLM call made).</>}
          </div>
        </div>
      </StepShell>

      <StepShell num="4" active={guardrailTone.active}>
        <div className={`rounded-[10px] border p-3.5 ${guardrailTone.card}`}>
          <div className="font-bold text-[13.5px]">{guardrailTitle}</div>
          {guardrailBody}
        </div>
      </StepShell>

      <StepShell num="5" active={activeNum}>
        <div className={`rounded-[10px] border p-3.5 ${activeCard}`}>
          <div className="font-bold text-[13.5px]">Execution</div>
          <div className="mt-2 flex items-center gap-2 flex-wrap text-xs">
            <Pill tone={execTone}>{row.execution_status}</Pill>
            <span className="text-muted">mechanism={row.execution_mechanism}</span>
          </div>
          <div className="text-muted text-xs mt-2">{row.execution_detail}</div>
          <div className="text-muted text-xs mt-2">customer_key={row.customer_key || '--'} &middot; history_source={row.customer_history_source || '--'}</div>
        </div>
      </StepShell>
    </div>
  );
}

export default function CaseDetailModal({ caseId, onClose }) {
  const state = useCaseDetail(caseId);

  if (!caseId) return null;

  return (
    <div
      className="fixed inset-0 bg-[rgba(20,24,38,0.45)] flex items-start justify-center p-10 overflow-y-auto z-[100]"
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div className="bg-white border border-gray-100 rounded-2xl max-w-[820px] w-full p-6 shadow-modal">
        {state.loading && <div className="text-muted text-center py-5">Loading...</div>}
        {state.error && (
          <>
            <div className="flex justify-between items-start mb-5">
              <h3 className="text-lg font-extrabold m-0">{caseId}</h3>
              <button onClick={onClose} className="text-muted hover:text-ink text-xl leading-none">&times;</button>
            </div>
            <div className="text-muted text-center py-5">{state.error}</div>
          </>
        )}
        {state.data && (() => {
          const { audit_row: row, log_lines = [] } = state.data;
          if (!row) {
            return (
              <>
                <div className="flex justify-between items-start mb-5">
                  <h3 className="text-lg font-extrabold m-0">{caseId}</h3>
                  <button onClick={onClose} className="text-muted hover:text-ink text-xl leading-none">&times;</button>
                </div>
                <div className="text-muted text-center py-5">No audit row found for this case_id (may only exist in the webhook transport log).</div>
                {log_lines.length > 0 && (
                  <div className="mb-4">
                    <h4 className="text-[12.5px] font-bold mb-2">Log trace</h4>
                    <LogTrace lines={log_lines} maxHeight="260px" />
                  </div>
                )}
              </>
            );
          }

          return (
            <>
              <div className="flex justify-between items-start mb-5">
                <div>
                  <h3 className="text-lg font-extrabold m-0">{row.case_id}</h3>
                  <div className="text-muted text-xs mt-0.5">{row.timestamp} &middot; source={row.source} &middot; pipeline={row.pipeline_version}</div>
                </div>
                <button onClick={onClose} className="text-muted hover:text-ink text-xl leading-none">&times;</button>
              </div>

              <Stepper row={row} />

              <details className="mt-2.5 mb-4" open>
                <summary className="cursor-pointer text-muted text-xs font-semibold select-none">Log trace (logs/webhook_receiver.log, step-by-step)</summary>
                <div className="mt-2">
                  <LogTrace lines={log_lines} title={`logs/webhook_receiver.log -- case_id=${row.case_id}`} maxHeight="260px" />
                </div>
              </details>
            </>
          );
        })()}
      </div>
    </div>
  );
}
