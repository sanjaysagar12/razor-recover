import { useState } from 'react';
import Sidebar from './components/Sidebar.jsx';
import DashboardPage from './pages/DashboardPage.jsx';
import ConversationsPage from './pages/ConversationsPage.jsx';
import LogsPage from './pages/LogsPage.jsx';
import RunBatchPage from './pages/RunBatchPage.jsx';
import ModelPage from './pages/ModelPage.jsx';

// Which page is showing lives in localStorage, not the URL -- navigating the
// sidebar never touches window.location/history, so the address bar stays
// put and a refresh just re-reads the last-active page from storage instead
// of resetting to Dashboard.
const STORAGE_KEY = 'rr_dashboard_active_page';
const VALID_PAGES = ['dashboard', 'conversations', 'logs', 'run-batch', 'model'];

function readStoredPage() {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    return VALID_PAGES.includes(stored) ? stored : 'dashboard';
  } catch {
    return 'dashboard';
  }
}

function writeStoredPage(page) {
  try {
    window.localStorage.setItem(STORAGE_KEY, page);
  } catch {
    // Private browsing / storage disabled -- navigation still works for
    // this session, it just won't survive a refresh.
  }
}

export default function AppRouter() {
  const [page, setPage] = useState(readStoredPage);
  const [paused, setPaused] = useState(false);

  function navigate(nextPage) {
    if (nextPage === page) return;
    setPage(nextPage);
    writeStoredPage(nextPage);
  }

  return (
    <div className="flex min-h-screen">
      <Sidebar page={page} onNavigate={navigate} live={!paused} />
      {page === 'conversations' && <ConversationsPage />}
      {page === 'logs' && <LogsPage paused={paused} setPaused={setPaused} />}
      {page === 'run-batch' && <RunBatchPage />}
      {page === 'model' && <ModelPage />}
      {page === 'dashboard' && <DashboardPage paused={paused} setPaused={setPaused} />}
    </div>
  );
}
