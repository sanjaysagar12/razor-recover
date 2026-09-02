import { useState } from 'react';
import { resetConversations } from '../api/conversationsApi.js';

// Data layer for the "Reset conversations" confirm modal (Conversations
// page). The UI layer calls this hook and never touches src/api/ directly.
export function useResetConversations({ toast, onDone }) {
  const [busy, setBusy] = useState(false);

  async function run() {
    setBusy(true);
    try {
      const { ok, data } = await resetConversations();
      if (!ok) {
        toast?.('err', 'Reset failed', data.message || 'Request failed');
        setBusy(false);
        return;
      }
      toast?.('ok', 'Conversations reset', 'Cleared: ' + (data.cleared || []).join(', '));
      setBusy(false);
      onDone?.();
    } catch (e) {
      toast?.('err', 'Reset error', String(e));
      setBusy(false);
    }
  }

  return { busy, run };
}
