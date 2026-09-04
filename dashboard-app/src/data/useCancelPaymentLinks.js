import { useState } from 'react';
import { cancelAllPaymentLinks } from '../api/paymentLinksApi.js';

// Data layer for the "Cancel all payment links" confirm modal (Logs page).
// The UI layer calls this hook and never touches src/api/ directly.
export function useCancelPaymentLinks({ toast, onDone }) {
  const [busy, setBusy] = useState(false);

  async function run() {
    setBusy(true);
    try {
      const { ok, data } = await cancelAllPaymentLinks();
      if (!ok) {
        toast?.('err', 'Cancel failed', data.message || 'Request failed');
        setBusy(false);
        return;
      }
      const failedCount = (data.failed || []).length;
      if (failedCount > 0) {
        toast?.(
          'err',
          'Partially cancelled',
          `Cancelled ${data.cancelled.length}/${data.total_active}, ${failedCount} failed`,
        );
      } else {
        toast?.('ok', 'Payment links cancelled', `Cancelled ${data.cancelled.length} active link(s)`);
      }
      setBusy(false);
      onDone?.();
    } catch (e) {
      toast?.('err', 'Cancel error', String(e));
      setBusy(false);
    }
  }

  return { busy, run };
}
