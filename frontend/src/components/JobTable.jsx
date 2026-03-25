import { useState } from 'react'
import './JobTable.css'

const CHEVRON_DOWN = '▾'
const CHEVRON_RIGHT = '▸'

const COLUMNS = [
  { key: 'Server', label: 'Server' },
  { key: 'JobID', label: 'Job ID' },
  { key: 'Job_Name', label: 'Name' },
  { key: 'Owner', label: 'Owner' },
  { key: 'Status', label: 'Status' },
  { key: 'CPUs', label: 'CPUs' },
  { key: 'Memory', label: 'Memory' },
]

function StatusBadge({ status }) {
  const cls = ['R', 'Q', 'E', 'F'].includes(status)
    ? `badge badge-${status}`
    : 'badge badge-default'
  return <span className={cls}>{status}</span>
}

export default function JobTable({ jobs, selectedJob, onSelect, hiddenJobIds = new Set(), showHidden = false, onToggleHide }) {
  const [sortKey, setSortKey] = useState('Server')
  const [sortDir, setSortDir] = useState(1)
  const [collapsed, setCollapsed] = useState(false)

  function handleSort(key) {
    if (key === sortKey) setSortDir(d => -d)
    else { setSortKey(key); setSortDir(1) }
  }

  const sorted = [...jobs].sort((a, b) => {
    const av = a[sortKey] ?? ''
    const bv = b[sortKey] ?? ''
    return av < bv ? -sortDir : av > bv ? sortDir : 0
  })

  if (jobs.length === 0) {
    return (
      <div className="card job-table-empty">
        <div className="card-header">
          <span>Jobs (0)</span>
        </div>
        <div className="empty-msg">No jobs match the current filters.</div>
      </div>
    )
  }

  return (
    <div className="card job-table-card">
      <div className="card-header">
        <div className="table-header-left">
          <button
            className="collapse-toggle"
            onClick={() => setCollapsed(v => !v)}
            title={collapsed ? 'Expand job table' : 'Collapse job table'}
          >
            {collapsed ? CHEVRON_RIGHT : CHEVRON_DOWN}
          </button>
          <span>Jobs ({jobs.length})</span>
          {selectedJob && (
            <span className="selected-hint">Selected: {selectedJob.JobID}</span>
          )}
        </div>
      </div>
      <div className={`table-wrap${collapsed ? ' table-wrap--collapsed' : ''}`}>
        <table className="job-table">
          <thead>
            <tr>
              {COLUMNS.map(col => (
                <th key={col.key} onClick={() => handleSort(col.key)} className="sortable">
                  {col.label}
                  {sortKey === col.key && (
                    <span className="sort-arrow">{sortDir === 1 ? ' ▲' : ' ▼'}</span>
                  )}
                </th>
              ))}
              <th className="col-actions"></th>
            </tr>
          </thead>
          <tbody>
            {sorted.map(job => {
              const isSelected = selectedJob?.JobID === job.JobID
              const isHidden = hiddenJobIds.has(job.JobID)
              const isFinished = job.Status === 'F'
              const rowClass = [
                isSelected ? 'selected' : '',
                isHidden && showHidden ? 'job-hidden-visible' : '',
              ].filter(Boolean).join(' ')
              return (
                <tr
                  key={`${job.Server}-${job.JobID}`}
                  className={rowClass}
                  onClick={() => onSelect(isSelected ? null : job)}
                >
                  <td>{job.Server}</td>
                  <td className="mono">{job.JobID}</td>
                  <td>{job.Job_Name}</td>
                  <td>{job.Owner}</td>
                  <td><StatusBadge status={job.Status} /></td>
                  <td>{job.CPUs}</td>
                  <td>{job.Memory}</td>
                  <td className="col-actions" onClick={e => e.stopPropagation()}>
                    {!isFinished && onToggleHide && (
                      <button
                        className="btn-ghost btn-xs"
                        onClick={() => onToggleHide(job.JobID)}
                        title={isHidden ? 'Show this job' : 'Hide this job'}
                      >
                        {isHidden ? 'Show' : 'Hide'}
                      </button>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
