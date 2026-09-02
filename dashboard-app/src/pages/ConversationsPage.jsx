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
  if (!summary) return null;
  const { subject, body } = buildOpeningMessage(summary);
  return (
    <div className="bg-[#EEF1FF] border border-gray-100 border-l-[3px] border-l-accent rounded-lg px-3.5 py-3 mb-3.5 text-[13px] leading-relaxed">
      <div className="text-[10.5px] tracking-wide text-muted mb-1.5">Outreach sent to customer</div>
      <div className="font-bold mb-2">{subject}</div>
      <div className="whitespace-pre-wrap">{body}</div>
    </div>
  );
}

function OutcomeBanner({ promises }) {
  if (!promises.length) {
    return <div className={`rounded-lg px-3 py-2 text-[12.5px] font-semibold mb-3.5 ${BANNER_CLASSES.awaiting}`}>Awaiting reply</div>;
  }
  const latest = promises[promises.length - 1];
  const outcome = ptpOutcome(latest);
  return <div className={`rounded-lg px-3 py-2 text-[12.5px] font-semibold mb-3.5 ${BANNER_CLASSES[outcome.kind]}`}>{outcome.text}</div>;
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
  const customerId = summary.customer_key || ('case:' + caseEntry.case_id);
  const [message, setMessage] = useState('');
  const [sending, setSending] = useState(false);
  const [status, setStatus] = useState('');

  async function handleSend() {
    if (!message.trim()) { setStatus('Type a message first.'); return; }
    setSending(true);
    setStatus('Sending...');
    const ok = await onSend(caseEntry.case_id, customerId, message);
    setStatus('');
    setSending(false);
    if (ok) setMessage('');
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
      <OutcomeBanner promises={promises} />
      <Thread promises={promises} />
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
