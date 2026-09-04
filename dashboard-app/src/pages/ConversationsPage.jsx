import { useState } from 'react';
import Toasts from '../components/Toasts.jsx';
import ResetConversationsModal from '../components/ResetConversationsModal.jsx';
import { useToasts } from '../data/useToasts.js';
import { useConversationsData } from '../data/useConversationsData.js';

const BANNER_CLASSES = {
  scheduled: 'bg-emerald-500/10 text-emerald-600 border border-emerald-500/25',
  fallback: 'bg-amber-400/10 text-amber-700 border border-amber-400/30',
  awaiting: 'bg-gray-50 text-muted border border-gray-100',
  review: 'bg-red-400/10 text-red-500 border border-red-400/25',
  recovered: 'bg-emerald-500/10 text-emerald-600 border border-emerald-500/25',
};

function ptpOutcome(promise) {
  const o = promise.scheduled_outcome || {};
  if (o.kind === 'scheduled') return { kind: 'scheduled', text: `Scheduled for: ${o.scheduled_for}` };
  if (o.kind === 'fallback') return { kind: 'fallback', text: `Fallback: automatic scheduling, scheduled ${o.scheduled_for || '--'}` };
  if (o.kind === 'requires_human_review') return { kind: 'review', text: `Flagged for human review (guardrail_status=${promise.guardrail_status || 'n/a'})` };
  if (o.kind === 'reschedule_failed') return { kind: 'review', text: `Guardrail approved (date ${o.extracted_date || '--'}) but scheduling failed -- Razorpay API error` };
  return { kind: 'awaiting', text: 'Awaiting reply' };
}

function systemBubbleForPromise(promise) {
  if (promise.status === 'clarifying') return `Could you clarify -- I couldn't quite pin down a date from that message. (clarification round ${promise.clarification_round})`;
  if (promise.status === 'fallback') return `No clear reply after multiple attempts -- falling back to an automatic schedule${promise.extracted_date ? ' for ' + promise.extracted_date : ''}.`;
  if (promise.status === 'scheduled') return `Got it -- payment reminder scheduled for ${promise.extracted_date}.`;
  if (promise.status === 'requires_human_review') return 'This reply needs a human to review it before scheduling anything.';
  if (promise.outcome === 'reschedule_failed') return 'Guardrail approved this reply, but creating the payment link failed (Razorpay API error) -- not scheduled.';
  if (promise.guardrail_status && promise.guardrail_status !== 'approved') return `Guardrail verdict: ${promise.guardrail_status} -- not auto-scheduled.`;
  return 'Reply received, still processing.';
}

function formatAmount(amount) {
  const n = Number(amount);
  return Number.isFinite(n) ? n.toFixed(2) : (amount ?? 'the amount');
}

function buildOpeningMessage(summary) {
  const amount = formatAmount(summary.amount);
  const declineCode = summary.decline_code ? summary.decline_code.replace(/_/g, ' ') : 'a technical issue';
  if (summary.ptp_trigger_category === 'hard_decline') {
    return {
      subject: 'Action needed: update your payment method',
      body: `Hi,\n\nWe tried to process your payment of ₹${amount} but it didn't go through -- your payment method was declined (${declineCode}). This isn't something we can fix by retrying.\n\nCould you update your payment method so we can complete this?\n\nThanks,\nRecovery Team`,
    };
  }
  return {
    subject: 'Action needed on your recent payment',
    body: `Hi,\n\nYour payment of ₹${amount} didn't go through (${declineCode}). When would you like us to retry?\n\nThanks,\nRecovery Team`,
  };
}

function OpeningMessage({ summary }) {
  // Only ever a real outreach if should_offer_ptp actually offered PTP for
  // this case (ptp_offer_decision===true) -- a recovered-on-arrival case
  // (no payment.failed ever ran through the pipeline for it) has this null,
  // and rendering an "Outreach sent to customer" bubble for it would be
  // fabricating an event that never happened.
  if (!summary || summary.ptp_offer_decision !== true) return null;
  const { subject, body } = buildOpeningMessage(summary);
  return (
    <div className="bg-[#EEF1FF] border border-gray-100 border-l-[3px] border-l-accent rounded-lg px-3.5 py-3 mb-3.5 text-[13px] leading-relaxed">
      <div className="text-[10.5px] tracking-wide text-muted mb-1.5">Outreach sent to customer</div>
      <div className="font-bold mb-2">{subject}</div>
      <div className="whitespace-pre-wrap">{body}</div>
    </div>
  );
}

function ResolutionNotice({ recovery }) {
  if (!recovery?.recovered) return null;
  const when = recovery.recovered_at ? new Date(recovery.recovered_at).toLocaleString() : null;
  return (
    <div className="bg-emerald-500/10 border border-emerald-500/25 border-l-[3px] border-l-emerald-500 rounded-lg px-3.5 py-3 mb-3.5 text-[13px] leading-relaxed text-emerald-700">
      <div className="font-bold">Payment recovered -- no reply needed</div>
      {when && <div className="text-[11px] opacity-80 mt-0.5">Resolved {when}</div>}
    </div>
  );
}

function OutcomeBanner({ promises, recovery }) {
  if (recovery?.recovered) {
    return <div className={`rounded-lg px-3 py-2 text-[12.5px] font-semibold mb-3.5 ${BANNER_CLASSES.recovered}`}>Recovered -- resolved</div>;
  }
  if (!promises.length) {
    return <div className={`rounded-lg px-3 py-2 text-[12.5px] font-semibold mb-3.5 ${BANNER_CLASSES.awaiting}`}>Awaiting reply</div>;
  }
  const latest = promises[promises.length - 1];
  const outcome = ptpOutcome(latest);
  return <div className={`rounded-lg px-3 py-2 text-[12.5px] font-semibold mb-3.5 ${BANNER_CLASSES[outcome.kind]}`}>{outcome.text}</div>;
}

function copyToClipboard(text) {
  try {
    navigator.clipboard?.writeText(text);
  } catch {
    // Clipboard API unavailable (non-HTTPS/older browser) -- the link is
    // still visible and selectable by hand, so this is a silent no-op, not
    // a broken row.
  }
}

// Payment-link display for one promise row -- Task 1: a customer-facing
// link.id alone isn't clickable, so this renders the real short_url when
// the row is scheduled, and an explicit pending/failed state otherwise --
// never a blank line where a link might have been.
function PaymentLinkChip({ promise }) {
  const kind = promise.scheduled_outcome?.kind;
  if (kind === 'scheduled') {
    const url = promise.scheduled_outcome?.payment_link_url || promise.payment_link_url;
    if (!url) {
      // Guardrail-approved and STATUS_SCHEDULED, but no URL on the row --
      // only expected for a promise scheduled before payment_link_url
      // started being persisted. Shown as pending, never left blank.
      return <div className="mt-1.5 text-[11px] text-muted italic">Payment link pending...</div>;
    }
    return (
      <div className="mt-1.5 flex items-center gap-1.5 flex-wrap">
        <a
          href={url}
          target="_blank"
          rel="noreferrer"
          className="text-accent font-semibold underline text-[11.5px] break-all"
        >
          {url}
        </a>
        <button
          type="button"
          onClick={() => copyToClipboard(url)}
          className="text-muted hover:text-ink border border-gray-100 rounded px-1.5 py-0.5 text-[10px] font-semibold"
        >
          Copy
        </button>
      </div>
    );
  }
  if (kind === 'reschedule_failed') {
    return <div className="mt-1.5 text-[11px] text-red-500 font-semibold">Payment link failed to generate</div>;
  }
  return null;
}

function Thread({ promises }) {
  if (!promises.length) return <div className="text-muted text-xs py-2">No replies yet for this case.</div>;
  return (
    <div className="flex flex-col gap-2.5 mb-3.5">
      {promises.map((p, i) => (
        <div key={i}>
          <div className="flex justify-end">
            <div className="max-w-[70%] rounded-xl rounded-br-[3px] bg-gray-50 border border-gray-100 px-3.5 py-2.5 text-[13px] leading-relaxed">
              {p.message}
              <div className="text-[10.5px] opacity-70 mt-1">
                {new Date(p.created_at).toLocaleString()}{p.extraction_confidence != null ? ` · confidence ${p.extraction_confidence}` : ''}
              </div>
            </div>
          </div>
          <div className="flex justify-start mt-2.5">
            <div className="max-w-[70%] rounded-xl rounded-bl-[3px] bg-[#EEF1FF] border border-accent/30 border-l-[3px] border-l-accent px-3.5 py-2.5 text-[13px] leading-relaxed">
              {systemBubbleForPromise(p)}
              <PaymentLinkChip promise={p} />
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

function CaseCard({ caseEntry, onSend }) {
  const summary = caseEntry.case_summary || {};
  const promises = caseEntry.promises || [];
  const recovery = caseEntry.recovery || null;
  const customerId = summary.customer_key || ('case:' + caseEntry.case_id);
  const [message, setMessage] = useState('');
  const [sending, setSending] = useState(false);
  const [status, setStatus] = useState('');
  // Dashboard-preview only, from api_promise_reply's customer_message field
  // (see webhook_receiver.py's own comment on that field) -- shown here so
  // an operator can see what a customer-facing confirmation would say.
  // Never an outbound send; cleared on the next send attempt so it can't be
  // mistaken for a persisted part of the thread.
  const [replyPreview, setReplyPreview] = useState(null);

  async function handleSend() {
    if (!message.trim()) { setStatus('Type a message first.'); return; }
    setSending(true);
    setStatus('Sending...');
    setReplyPreview(null);
    const result = await onSend(caseEntry.case_id, customerId, message);
    setStatus('');
    setSending(false);
    if (result.ok) {
      setMessage('');
      setReplyPreview(result.customerMessage || null);
    }
  }

  return (
    <div className="bg-white border border-gray-100 rounded-2xl shadow-card p-[18px] mb-4">
      <div className="flex justify-between items-start flex-wrap gap-2 mb-3">
        <div>
          <h3 className="text-sm font-bold m-0">{caseEntry.case_id}</h3>
          <div className="text-muted text-xs mt-0.5">
            {summary.timestamp ? new Date(summary.timestamp).toLocaleString() : ''}
            {summary.decline_code ? ` · ${summary.decline_code}` : ''}
            {summary.amount != null ? ` · amount ${summary.amount}` : ''}
          </div>
        </div>
      </div>
      <OpeningMessage summary={summary} />
      <OutcomeBanner promises={promises} recovery={recovery} />
      <Thread promises={promises} />
      <ResolutionNotice recovery={recovery} />
      {replyPreview && (
        <div className="bg-emerald-500/10 border border-emerald-500/25 border-l-[3px] border-l-emerald-500 rounded-lg px-3.5 py-3 mb-3.5 text-[13px] leading-relaxed">
          <div className="text-[10.5px] tracking-wide text-muted mb-1.5">
            Preview -- what we'd tell the customer (not sent automatically)
          </div>
          <div className="whitespace-pre-wrap">{replyPreview}</div>
        </div>
      )}
      {recovery?.recovered ? (
        <div className="text-muted text-xs mt-1.5">This payment has recovered -- no reply needed, replying is disabled.</div>
      ) : (
        <div className="flex gap-2 items-center flex-wrap mt-1.5">
          <input
            type="text"
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') handleSend(); }}
            placeholder="Reply as this customer..."
            disabled={sending}
            className="flex-1 min-w-[220px] bg-gray-50 text-ink border border-gray-100 rounded-lg px-2.5 py-2 text-sm disabled:opacity-50"
          />
          <button
            disabled={sending}
            onClick={handleSend}
            className="bg-accent text-white rounded-lg px-4 py-2 font-bold text-sm disabled:opacity-50"
          >
            Send
          </button>
        </div>
      )}
      <div className="text-muted text-xs mt-2">{status}</div>
    </div>
  );
}

export default function ConversationsPage() {
  const [showResetModal, setShowResetModal] = useState(false);
  const { toasts, toast } = useToasts();
  const {
    customers: filteredCustomers,
    search,
    setSearch,
    matchingCaseId,
    selectedEmail,
    selectCustomer,
    cases,
    loadingCases,
    casesError,
    sendReply,
    refreshCustomers,
    clearSelection,
  } = useConversationsData({ toast });

  return (
    <div className="flex-1 min-w-0 flex flex-col min-h-screen">
      <div className="flex items-end justify-between flex-wrap gap-5 px-7 pt-6 pb-2">
        <div className="flex flex-col gap-2.5">
          <h1 className="text-[34px] font-extrabold m-0 tracking-tight">Customer Conversations</h1>
          <div className="text-muted text-[13px]">Full promise-to-pay reply threads, grouped by customer email</div>
        </div>
        <button
          onClick={() => setShowResetModal(true)}
          title="Clears the customer directory, promise threads, and promise_log.csv"
          className="px-4 py-2 rounded-lg border border-red-400 text-red-500 text-xs font-bold hover:bg-red-50"
        >
          Reset conversations
        </button>
      </div>

      <div className="flex-1 flex min-h-0 px-7 pb-7 pt-4 gap-4">
        <div className="w-[300px] shrink-0 bg-white border border-gray-100 rounded-2xl shadow-card flex flex-col overflow-hidden">
          <div className="p-4 border-b border-gray-100">
            <div className="text-[11px] font-semibold text-muted uppercase tracking-wider mb-2">Customers</div>
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search by email or case_id..."
              className="w-full bg-gray-50 text-ink border border-gray-100 rounded-lg px-2.5 py-2 text-sm"
            />
          </div>
          <div className="flex-1 overflow-y-auto">
            {filteredCustomers.length === 0 && <div className="text-muted text-sm text-center py-8">No customers found.</div>}
            {filteredCustomers.map((c) => {
              const active = c.email === selectedEmail;
              const matchedCaseId = search.trim() ? matchingCaseId(c, search.trim().toLowerCase()) : null;
              return (
                <div
                  key={c.email}
                  onClick={() => selectCustomer(c.email)}
                  className={`px-4 py-3 border-b border-gray-100 border-l-[3px] cursor-pointer ${
                    active ? 'bg-[#EEF1FF] border-l-accent' : 'border-l-transparent hover:bg-gray-50'
                  }`}
                >
                  <div className="font-semibold text-sm break-all">{c.email}</div>
                  <div className="text-muted text-[11px] mt-1">
                    {c.case_count} case{c.case_count === 1 ? '' : 's'} &middot; last active {c.last_activity ? new Date(c.last_activity).toLocaleString() : '--'}
                  </div>
                  {matchedCaseId && <div className="text-accent text-[11px] mt-1">matched case_id: {matchedCaseId}</div>}
                </div>
              );
            })}
          </div>
        </div>

        <div className="flex-1 min-w-0 overflow-y-auto">
          {!selectedEmail && (
            <div className="text-muted text-center py-16">Select a customer on the left to view their conversation history.</div>
          )}
          {selectedEmail && loadingCases && <div className="text-muted text-center py-16">Loading...</div>}
          {selectedEmail && !loadingCases && casesError && <div className="text-muted text-center py-16">{casesError}</div>}
          {selectedEmail && !loadingCases && !casesError && cases && cases.length === 0 && (
            <div className="text-muted text-center py-16">No cases found for {selectedEmail}.</div>
          )}
          {selectedEmail && !loadingCases && !casesError && cases && cases.map((c) => (
            <CaseCard key={c.case_id} caseEntry={c} onSend={sendReply} />
          ))}
        </div>
      </div>

      <Toasts toasts={toasts} />
      <ResetConversationsModal
        open={showResetModal}
        onClose={() => setShowResetModal(false)}
        onDone={() => {
          setShowResetModal(false);
          clearSelection();
          refreshCustomers();
        }}
        toast={toast}
      />
    </div>
  );
}
