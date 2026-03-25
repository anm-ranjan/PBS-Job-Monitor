import { useState } from 'react'
import KillJobDialog from './KillJobDialog.jsx'
import SubmitJobForm from './SubmitJobForm.jsx'
import ReportForm from './ReportForm.jsx'
import InterimReportForm from './InterimReportForm.jsx'
import { deleteAllFinished } from '../api.js'
import './Sidebar.css'

export default function Sidebar({
  servers, statuses, filters, onFilterChange,
  onRefresh, lastRefresh, actionInProgress,
  selectedJob, onActionInProgress, onJobsRefresh,
  onInterimReportLoaded,
  theme, onThemeToggle,
  hiddenCount, showHidden, onToggleShowHidden,
  onCollapse,
  sidebarOpen = true,
}) {
  const [panel, setPanel] = useState(null) // 'kill' | 'submit' | 'report' | 'interim' | null

  const fmt = d => d ? d.toLocaleTimeString() : '—'

  function openPanel(name) {
    onActionInProgress(true)
    setPanel(name)
  }

  function closePanel() {
    onActionInProgress(false)
    setPanel(null)
    onJobsRefresh(true)
  }

  function closeInterimPanel() {
    onActionInProgress(false)
    setPanel(null)
    // no job refresh needed for interim reports
  }

  function handleInterimLoaded(report) {
    onInterimReportLoaded(report)
  }

  async function handleClearFinished() {
    try {
      await deleteAllFinished()
    } catch (err) {
      console.error('Failed to clear finished jobs:', err)
    }
    onJobsRefresh(true)
  }

  return (
    <aside className={`sidebar${sidebarOpen ? '' : ' sidebar--collapsed'}`}>
      <div className="sidebar-logo">
        <div className="logo-text-group">
          <span className="logo-text">PBS Job Monitor</span>
          <span className="logo-version">v2.0</span>
        </div>
        <button className="sidebar-collapse-btn" onClick={onCollapse} title="Collapse sidebar">‹</button>
      </div>

      <div className="sidebar-section">
        <div className="section-title">Filters</div>

        <label>Server
          <select value={filters.server} onChange={e => onFilterChange({ ...filters, server: e.target.value })}>
            {servers.map(s => <option key={s}>{s}</option>)}
          </select>
        </label>

        <label>Status
          <select value={filters.status} onChange={e => onFilterChange({ ...filters, status: e.target.value })}>
            {statuses.map(s => <option key={s}>{s}</option>)}
          </select>
        </label>

        <label>Owner
          <input
            type="text"
            placeholder="filter by owner..."
            value={filters.owner}
            onChange={e => onFilterChange({ ...filters, owner: e.target.value })}
          />
        </label>
      </div>

      <div className="sidebar-section">
        <div className="section-title">Refresh</div>
        <button className="btn-primary w-full" onClick={() => onRefresh()} disabled={actionInProgress}>
          Refresh Now
        </button>
        <div className="refresh-time">Last: {fmt(lastRefresh)}</div>
        {actionInProgress && (
          <div className="action-guard-notice">Auto-refresh paused during action</div>
        )}
      </div>

      <div className="sidebar-section">
        <div className="section-title">Job Actions</div>

        <button
          className="btn-danger w-full"
          disabled={!selectedJob || actionInProgress}
          onClick={() => openPanel('kill')}
        >
          Kill Job
        </button>

        <button
          className="btn-primary w-full"
          disabled={actionInProgress}
          onClick={() => openPanel('submit')}
        >
          Submit Job
        </button>

        <button
          className="btn-ghost w-full"
          disabled={!selectedJob || actionInProgress}
          onClick={() => openPanel('report')}
        >
          Final Report
        </button>
      </div>

      <div className="sidebar-section">
        <div className="section-title">Reports</div>

        <button
          className="btn-ghost w-full"
          disabled={actionInProgress}
          onClick={() => openPanel('interim')}
        >
          Interim Report
        </button>
        <div className="interim-hint">Browse any finished simulation</div>
      </div>

      <div className="sidebar-section">
        <div className="section-title">Finished Jobs</div>
        <button
          className="btn-danger w-full"
          disabled={actionInProgress}
          onClick={handleClearFinished}
        >
          Clear All Finished
        </button>
        <div className="interim-hint">Removes finished jobs from the list</div>
      </div>

      <div className="sidebar-section">
        <div className="section-title">Visibility</div>
        <button className="btn-ghost w-full" onClick={onToggleShowHidden}>
          {showHidden
            ? 'Hide Hidden Jobs'
            : `Show Hidden Jobs${hiddenCount > 0 ? ` (${hiddenCount})` : ''}`}
        </button>
        <div className="interim-hint">Hidden jobs are R/Q/E jobs you muted</div>
      </div>

      <div className="sidebar-footer">
        <button className="btn-ghost w-full theme-toggle" onClick={onThemeToggle}>
          {theme === 'dark' ? '☀ Light Mode' : '☾ Dark Mode'}
        </button>
      </div>

      {/* Action panels */}
      {panel === 'kill' && selectedJob && (
        <KillJobDialog job={selectedJob} onClose={closePanel} />
      )}
      {panel === 'submit' && (
        <SubmitJobForm onClose={closePanel} />
      )}
      {panel === 'report' && selectedJob && (
        <ReportForm job={selectedJob} onClose={closePanel} />
      )}
      {panel === 'interim' && (
        <InterimReportForm
          onClose={closeInterimPanel}
          onLoaded={handleInterimLoaded}
        />
      )}
    </aside>
  )
}
