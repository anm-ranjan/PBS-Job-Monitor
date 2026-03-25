import { useState, useEffect, useCallback } from 'react'
import Sidebar from './components/Sidebar.jsx'
import JobTable from './components/JobTable.jsx'
import JobDetail from './components/JobDetail.jsx'
import InterimDetail from './components/InterimDetail.jsx'
import { fetchJobs } from './api.js'
import './App.css'

const REFRESH_INTERVAL_MS = 20_000

export default function App() {
  const [jobs, setJobs] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [selectedJob, setSelectedJob] = useState(null)
  const [interimReport, setInterimReport] = useState(null)
  const [filters, setFilters] = useState({ server: 'All', status: 'All', owner: '' })
  const [actionInProgress, setActionInProgress] = useState(false)
  const [lastRefresh, setLastRefresh] = useState(null)
  const [theme, setTheme] = useState(() => localStorage.getItem('pbs-theme') || 'dark')
  const [sidebarOpen, setSidebarOpen] = useState(true)
  // Hidden jobs — persisted in localStorage so they survive browser reloads
  const [hiddenJobIds, setHiddenJobIds] = useState(() => {
    try {
      return new Set(JSON.parse(localStorage.getItem('pbs-hidden-jobs') || '[]'))
    } catch { return new Set() }
  })
  const [showHidden, setShowHidden] = useState(false)

  // Keep localStorage in sync whenever the hidden set changes
  useEffect(() => {
    localStorage.setItem('pbs-hidden-jobs', JSON.stringify([...hiddenJobIds]))
  }, [hiddenJobIds])

  // Apply theme to <html> so CSS variables cascade everywhere
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    localStorage.setItem('pbs-theme', theme)
  }, [theme])

  function toggleTheme() {
    setTheme(t => t === 'dark' ? 'light' : 'dark')
  }

  const loadJobs = useCallback(async (force = false) => {
    try {
      const data = await fetchJobs(force)
      setJobs(data)
      setLastRefresh(new Date())
      setError(null)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  // Initial load
  useEffect(() => { loadJobs() }, [loadJobs])

  // Auto-refresh — paused while an action is running
  useEffect(() => {
    if (actionInProgress) return
    const id = setInterval(() => loadJobs(), REFRESH_INTERVAL_MS)
    return () => clearInterval(id)
  }, [actionInProgress, loadJobs])

  // Selecting a job clears any open interim report, and vice-versa
  function handleSelectJob(job) {
    setSelectedJob(job)
    if (job) setInterimReport(null)
  }

  function handleInterimReportLoaded(report) {
    setInterimReport(report)
    setSelectedJob(null)
  }

  function toggleHideJob(jobId) {
    setHiddenJobIds(prev => {
      const next = new Set(prev)
      if (next.has(jobId)) next.delete(jobId)
      else next.add(jobId)
      return next
    })
  }

  // Derived: apply filters + hidden suppression
  const filteredJobs = jobs.filter(j => {
    if (!showHidden && hiddenJobIds.has(j.JobID)) return false
    if (filters.server !== 'All' && j.Server !== filters.server) return false
    if (filters.status !== 'All' && j.Status !== filters.status) return false
    if (filters.owner && !j.Owner?.toLowerCase().includes(filters.owner.toLowerCase())) return false
    return true
  })

  const servers = ['All', ...new Set(jobs.map(j => j.Server))]
  const statuses = ['All', ...new Set(jobs.map(j => j.Status))]

  return (
    <div className="app-layout">
      {!sidebarOpen && (
        <button
          className="sidebar-reopen-btn"
          onClick={() => setSidebarOpen(true)}
          title="Open sidebar"
        >☰</button>
      )}
      <Sidebar
        servers={servers}
        statuses={statuses}
        filters={filters}
        onFilterChange={setFilters}
        onRefresh={() => loadJobs(true)}
        lastRefresh={lastRefresh}
        actionInProgress={actionInProgress}
        selectedJob={selectedJob}
        onActionInProgress={setActionInProgress}
        onJobsRefresh={loadJobs}
        onInterimReportLoaded={handleInterimReportLoaded}
        theme={theme}
        onThemeToggle={toggleTheme}
        hiddenCount={hiddenJobIds.size}
        showHidden={showHidden}
        onToggleShowHidden={() => setShowHidden(v => !v)}
        onCollapse={() => setSidebarOpen(false)}
        sidebarOpen={sidebarOpen}
      />

      <main className="main-content">
        {loading && <div className="status-msg">Connecting to servers...</div>}
        {error && <div className="status-msg error">Connection error: {error}</div>}

        <JobTable
          jobs={filteredJobs}
          selectedJob={selectedJob}
          onSelect={handleSelectJob}
          hiddenJobIds={hiddenJobIds}
          showHidden={showHidden}
          onToggleHide={toggleHideJob}
        />

        {selectedJob && (
          <JobDetail
            job={selectedJob}
            onClose={() => setSelectedJob(null)}
          />
        )}

        {interimReport && (
          <InterimDetail
            report={interimReport}
            onClose={() => setInterimReport(null)}
          />
        )}
      </main>
    </div>
  )
}
