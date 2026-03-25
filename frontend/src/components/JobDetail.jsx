import { useState } from 'react'
import ConvergencePlots from './ConvergencePlots.jsx'
import LogViewer from './LogViewer.jsx'
import OptimalTimestep from './OptimalTimestep.jsx'
import MetaViewer from './MetaViewer.jsx'
import './JobDetail.css'

const TABS_Q   = ['Convergence Plots', 'Live Log']
const TABS_R_E = ['Convergence Plots', 'Live Log', 'META Viewer']
const TABS_F   = ['Convergence Plots', 'Optimal Timestep', 'Live Log', 'META Viewer']

export default function JobDetail({ job, onClose }) {
  const [activeTab, setActiveTab] = useState('Convergence Plots')
  const tabs = job.Status === 'F' ? TABS_F
             : job.Status === 'Q' ? TABS_Q
             : TABS_R_E

  return (
    <div className="card job-detail">
      <div className="card-header job-detail-header">
        <div className="detail-title">
          <span className="detail-job-name">{job.Job_Name}</span>
          <span className="detail-job-id">#{job.JobID}</span>
          <span className="detail-server">@ {job.Server}</span>
        </div>
        <button className="btn-ghost btn-sm close-btn" onClick={onClose}>✕ Close</button>
      </div>

      {/* Job metadata strip */}
      <div className="job-meta">
        <span><label>Status</label>{job.Status}</span>
        <span><label>Owner</label>{job.Owner}</span>
        <span><label>CPUs</label>{job.CPUs}</span>
        <span><label>Memory</label>{job.Memory}</span>
        <span className="path-meta"><label>Path</label>{job.Job_Path}</span>
      </div>

      {/* Tab bar */}
      <div className="tab-bar">
        {tabs.map(tab => (
          <button
            key={tab}
            className={`tab-btn ${activeTab === tab ? 'active' : ''}`}
            onClick={() => setActiveTab(tab)}
          >
            {tab}
          </button>
        ))}
      </div>

      {/* Tab content */}
      <div className="tab-content">
        {activeTab === 'Convergence Plots' && (
          <ConvergencePlots job={job} />
        )}
        {activeTab === 'Optimal Timestep' && (
          <OptimalTimestep job={job} />
        )}
        {activeTab === 'Live Log' && (
          <LogViewer job={job} />
        )}
        {activeTab === 'META Viewer' && (
          <MetaViewer job={job} />
        )}
      </div>
    </div>
  )
}
