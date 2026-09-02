import { useCallback, useState } from 'react';

let toastSeq = 0;

export function useToasts() {
  const [toasts, setToasts] = useState([]);

  const toast = useCallback((kind, title, message) => {
    const id = ++toastSeq;
    setToasts((prev) => [...prev, { id, kind, title, message }]);
    setTimeout(() => setToasts((prev) => prev.filter((t) => t.id !== id)), 7000);
  }, []);

  return { toasts, toast };
}
