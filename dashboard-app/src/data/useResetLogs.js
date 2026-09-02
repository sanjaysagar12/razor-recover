import { useState } from 'react';
import { resetLogs } from '../api/resetApi.js';
import { resetConversations } from '../api/conversationsApi.js';

// Data layer for the "Reset logs" confirm modal (Dashboard + Logs pages).
// The UI layer calls this hook and never touches src/api/ directly.
export function useResetLogs({ toast, onDone }) {
  const [busy, setBusy] = useState(false);

  async function run({ resetCustomerHistory, deleteConversations }) {
    setBusy(true);
    try {
      const { ok, data } = await resetLogs(resetCustomerHistory);
      if (!ok) {
        toast?.('err', 'Reset failed', data.message || 'Request failed');
        setBusy(false);
        return;
      }
      const cleared = [...(data.cleared || [])];

      if (deleteConversations) {
        const convRes = await resetConversations();
        if (!convRes.ok) {
          toast?.('err', 'Reset conversations failed', convRes.data.message || 'Request failed');
          setBusy(false);
          return;
        }
        cleared.push(...(convRes.data.cleared || []));
      }

      toast?.('ok', 'Reset complete', 'Cleared: ' + cleared.join(', '));
      setBusy(false);
      onDone?.();
    } catch (e) {
      toast?.('err', 'Reset error', String(e));
      setBusy(false);
    }
  }

  return { busy, run };
}
