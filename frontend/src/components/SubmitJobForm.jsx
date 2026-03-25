import { useState, useEffect } from 'react'
import { submitJob, fetchConfig } from '../api.js'
import './Dialog.css'

const PBS_REGISTER_WAIT_S = 5  // seconds to wait for PBS to register the new job

export default function SubmitJobForm({ onClose }) {
  const [windowsPath, setWindowsPath] = useState('')
  const [scriptName, setScriptName] = useState('qsubrunfhgfs.sh')
  const [metaOnFinish, setMetaOnFinish] = useState(false)
  const [metaConfigured, setMetaConfigured] = useState(false)
  const [phase, setPhase] = useState('form')   // 'form' | 'submitting' | 'waiting' | 'error'
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [countdown, setCountdown] = useState(PBS_REGISTER_WAIT_S)

  useEffect(() => {
    fetchConfig().then(cfg => setMetaConfigured(!!cfg.meta_configured)).catch(() => {})
  }, [])

  // Tick the countdown down every second while in 'waiting' phase.
  // When it hits 0 auto-close (which triggers the force-refresh in Sidebar).
  useEffect(() => {
    if (phase !== 'waiting') return
    if (countdown <= 0) {
      onClose()
      return
    }
    const id = setTimeout(() => setCountdown(c => c - 1), 1000)
    return () => clearTimeout(id)
  }, [phase, countdown, onClose])

  async function handleSubmit(e) {
    e.preventDefault()
    if (!windowsPath.trim()) return
    setPhase('submitting')
    setError(null)
    try {
      const res = await submitJob(windowsPath.trim(), scriptName.trim(), metaOnFinish)
      setResult(res)
      setCountdown(PBS_REGISTER_WAIT_S)
      setPhase('waiting')
    } catch (err) {
      setError(err.message)
      setPhase('error')
    }
  }

  return (
    <div className="dialog-overlay">
      <div className="dialog">
        <div className="dialog-header">
          Submit PBS Job
          <button className="btn-ghost btn-sm" onClick={onClose}>✕</button>
        </div>

        {phase === 'form' && (
          <form className="dialog-body" onSubmit={handleSubmit}>
            <label>Job Directory (Windows path)
              <input
                type="text"
                value={windowsPath}
                onChange={e => setWindowsPath(e.target.value)}
                placeholder="Z:\path\to\job"
                required
              />
            </label>
            <label>Submit Script
              <input
                type="text"
                value={scriptName}
                onChange={e => setScriptName(e.target.value)}
              />
            </label>
            {metaConfigured && (
              <label style={{ flexDirection: 'row', alignItems: 'center', gap: '8px', fontWeight: 'normal' }}>
                <input
                  type="checkbox"
                  checked={metaOnFinish}
                  onChange={e => setMetaOnFinish(e.target.checked)}
                />
                Generate META DB when simulation completes
              </label>
            )}
            <div className="dialog-actions">
              <button type="button" className="btn-ghost" onClick={onClose}>Cancel</button>
              <button type="submit" className="btn-primary">Submit</button>
            </div>
          </form>
        )}

        {phase === 'submitting' && (
          <div className="dialog-body">
            <div className="spinner-row"><div className="spinner" />Submitting job...</div>
          </div>
        )}

        {phase === 'waiting' && (
          <div className="dialog-body">
            <div className="result-msg success">✓ {result?.message}</div>
            <div className="result-msg">Waiting for PBS to register job... ({countdown}s)</div>
            <div className="dialog-actions">
              <button className="btn-primary" onClick={onClose}>Refresh Now</button>
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
