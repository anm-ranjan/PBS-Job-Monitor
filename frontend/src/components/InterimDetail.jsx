import { useState } from 'react'
import { PlotPanel } from './ConvergencePlots.jsx'
import OptimalTimestep from './OptimalTimestep.jsx'
import './JobDetail.css'
import './InterimDetail.css'

const TABS = ['Convergence Plots', 'Optimal Timestep']

/**
 * Displays convergence plots and the optimal timestep export for an Interim
 * (Dashboard) Report loaded from an arbitrary simulation folder.
 */
export default function InterimDetail({ report, onClose }) {
  const { folder_path, summary, plots } = report
  const [activeTab, setActiveTab] = useState('Convergence Plots')

  return (
    <div className="card job-detail">
      <div className="card-header job-detail-header">
        <div className="detail-title">
          <span className="interim-badge">Interim Report</span>
          <span className="detail-job-name">{folder_path}</span>
        </div>
        <button className="btn-ghost btn-sm close-btn" onClick={onClose}>✕ Close</button>
      </div>

      <div className="tab-bar">
        {TABS.map(tab => (
          <button
            key={tab}
            className={`tab-btn ${activeTab === tab ? 'active' : ''}`}
            onClick={() => setActiveTab(tab)}
          >
            {tab}
          </button>
        ))}
      </div>

      <div className="tab-content">
        {activeTab === 'Convergence Plots' && (
          <PlotPanel summary={summary} plots={plots} />
        )}
        {activeTab === 'Optimal Timestep' && (
          <OptimalTimestep folderPath={folder_path} />
        )}
      </div>
    </div>
  )
}
