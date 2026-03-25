import { useState } from 'react'
import { generateReport, checkReportStatus, launchReportViewer } from '../api.js'
import './Dialog.css'

export default function ReportForm({ job, onClose }) {
  // Derive default Windows path from job if possible
  const defaultPath = job.Job_Path || ''

  const [windowsPath, setWindowsPath] = useState(defaultPath)
  const [phase, setPhase] = useState('form')   // 'form' | 'generating' | 'done' | 'error'
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [launching, setLaunching] = useState(false)
  const [launchMsg, setLaunchMsg] = useState(null)

  async function handleGenerate(e) {
    e.preventDefault()
    if (!windowsPath.trim()) return
    setPhase('generating')
    setError(null)
    try {
      const res = await generateReport(windowsPath.trim())
      setResult(res)
      setPhase('done')
    } catch (err) {
      setError(err.message)
      setPhase('error')
    }
  }

  async function handleLaunch() {
    setLaunching(true)
    try {
      const res = await launchReportViewer(windowsPath.trim())
      setLaunchMsg(res.message)
    } catch (e) {
      setLaunchMsg(`Error: ${e.message}`)
    } finally {
      setLaunching(false)
    }
  }

  return (
    <div className="dialog-overlay">
      <div className="dialog">
        <div className="dialog-header">
          Generate Report: {job.Job_Name}
          <button className="btn-ghost btn-sm" onClick={onClose}>✕</button>
        </div>

        {phase === 'form' && (
          <form className="dialog-body" onSubmit={handleGenerate}>
            <label>Job Directory (Windows path)
              <input
                type="text"
                value={windowsPath}
                onChange={e => setWindowsPath(e.target.value)}
                placeholder="Z:\path\to\job"
                required
              />
            </label>
            <p className="hint">
              Runs <code>run_report_win.sh</code> on the server. Waits up to 4 minutes for completion.
            </p>
            <div className="dialog-actions">
              <button type="button" className="btn-ghost" onClick={onClose}>Cancel</button>
              <button type="submit" className="btn-primary">Generate</button>
            </div>
          </form>
        )}

        {phase === 'generating' && (
          <div className="dialog-body">
            <div className="spinner-row">
              <div className="spinner" />
              Running report generation on server...
            </div>
            <p className="hint">This may take several minutes. Do not close this dialog.</p>
          </div>
        )}

        {phase === 'done' && (
          <div className="dialog-body">
            <div className={`result-msg ${result?.complete ? 'success' : 'warning'}`}>
              {result?.complete ? '✓ Report generated' : '⚠ Report generation timed out'}
            </div>
            <p>{result?.message}</p>

            {result?.complete && (
              <div className="launch-section">
                <button
                  className="btn-primary"
                  disabled={launching}
                  onClick={handleLaunch}
                >
                  {launching ? 'Launching...' : 'Launch Report Viewer'}
                </button>
                {launchMsg && <p className="hint">{launchMsg}</p>}
              </div>
            )}

            <div className="dialog-actions">
              <button className="btn-ghost" onClick={onClose}>Close</button>
            </div>
          </div>
        )}

        {phase === 'error' && (
          <div className="dialog-body">
            <div className="result-msg error">✗ {error}</div>
            <div className="dialog-actions">
              <button className="btn-ghost" onClick={onClose}>Close</button>
              <button className="btn-primary" onClick={() => setPhase('form')}>Back</button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
