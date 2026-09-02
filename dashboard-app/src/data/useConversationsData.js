import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  fetchCustomers,
  fetchCustomerConversations,
  sendPromiseReply as apiSendPromiseReply,
} from '../api/conversationsApi.js';

function matchingCaseId(customer, filter) {
  return (customer.case_ids || []).find((id) => id.toLowerCase().includes(filter));
}

// Data layer for the Customer Conversations page -- owns the customer list,
// search filtering, the selected customer's case threads, and the
// send-reply action. The UI layer calls this hook and never touches
// src/api/ directly.
export function useConversationsData({ toast }) {
  const [customers, setCustomers] = useState([]);
  const [search, setSearch] = useState('');
  const [selectedEmail, setSelectedEmail] = useState(null);
  const [cases, setCases] = useState(null);
  const [loadingCases, setLoadingCases] = useState(false);
  const [casesError, setCasesError] = useState(null);
  const autoSelectedRef = useRef(false);

  const refreshCustomers = useCallback(async () => {
    const { ok, data } = await fetchCustomers();
    if (ok) setCustomers(data.customers || []);
  }, []);

  useEffect(() => {
    refreshCustomers();
  }, [refreshCustomers]);

  // Open on the customer with the most recent message by default (the
  // backend already sorts /api/customers by last_activity desc) instead of
  // leaving the page on an empty "select a customer" placeholder. Only runs
  // once per page visit -- a later refreshCustomers() (e.g. after sending a
  // reply) must not yank the operator back to whoever's now on top.
  useEffect(() => {
    if (!autoSelectedRef.current && customers.length > 0) {
      autoSelectedRef.current = true;
      selectCustomer(customers[0].email);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [customers]);

  const filteredCustomers = useMemo(() => {
    const filter = search.trim().toLowerCase();
    if (!filter) return customers;
    return customers.filter((c) => c.email.toLowerCase().includes(filter) || matchingCaseId(c, filter));
  }, [customers, search]);

  const loadConversations = useCallback(async (email) => {
    setLoadingCases(true);
    setCasesError(null);
    try {
      const { ok, data } = await fetchCustomerConversations(email);
      if (!ok) {
        setCasesError(data.message || 'Not found');
        setCases(null);
        return;
      }
      setCases(data.cases || []);
    } catch (e) {
      setCasesError(String(e));
      setCases(null);
    } finally {
      setLoadingCases(false);
    }
  }, []);

  async function selectCustomer(email) {
    setSelectedEmail(email);
    await loadConversations(email);
  }

  async function sendReply(caseId, customerId, message) {
    try {
      const { ok, data } = await apiSendPromiseReply(caseId, customerId, message);
      if (!ok) {
        toast?.('err', 'Reply failed', data.message || 'unknown error');
        return false;
      }
      toast?.('ok', 'Reply sent', `case ${caseId}`);
      await Promise.all([loadConversations(selectedEmail), refreshCustomers()]);
      return true;
    } catch (e) {
      toast?.('err', 'Reply error', String(e));
      return false;
    }
  }

  function clearSelection() {
    setSelectedEmail(null);
    setCases(null);
  }

  return {
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
  };
}
