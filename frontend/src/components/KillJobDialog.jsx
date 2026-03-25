import { useState } from 'react'
import { killJob, deleteDirectory } from '../api.js'
import './Dialog.css'

export default function KillJobDialog({ job, onClose }) {
  const [phase, setPhase] = useState('confirm')   // 'confirm' | 'killing' | 'done' | 'error'
  const [result, setResult] = useState(null)
  const [deleteDir, setDeleteDir] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [delResult, setDelResult] = useState(null)
  const [error, setError] = useState(null)

  async function handleKill() {
    setPhase('killing')
    setError(null)
    try {
      const res = await killJob(job.JobID, job)
      setResult(res)
      setPhase('done')
    } catch (e) {
      setError(e.message)
      setPhase('error')
    }
  }

  async function handleDelete() {
    setDeleting(true)
    try {
      const res = await deleteDirectory(job.JobID, job)
      setDelResult(res)
    } catch (e) {
      setDelResult({ message: `Error: ${e.message}` })
    } finally {
      setDeleting(false)
    }
  }

  return (
    <div className="dialog-overlay">
      <div className="dialog">
        <div className="dialog-header">
          Kill Job: {job.Job_Name}
          <button className="btn-ghost btn-sm" onClick={onClose}>✕</button>
        </div>

        {phase === 'confirm' && (
          <div className="dialog-body">
            <p>Send <code>qdel {job.JobID}</code> on <strong>{job.Server}</strong>?</p>
            <p className="hint">The dashboard will poll for up to 120s for job termination.</p>
            <div className="dialog-actions">
              <button className="btn-ghost" onClick={onClose}>Cancel</button>
              <button className="btn-danger" onClick={handleKill}>Kill Job</button>
            </div>
          </div>
        )}

        {phase === 'killing' && (
          <div className="dialog-body">
            <div className="spinner-row">
              <div className="spinner" />
              Sending qdel and waiting for termination...
            </div>
          </div>
        )}

        {phase === 'done' && (
          <div className="dialog-body">
            <div className={`result-msg ${result?.terminated ? 'success' : 'warning'}`}>
              {result?.terminated ? '✓ Job terminated' : '⚠ Kill sent but termination unconfirmed'}
            </div>
            <p>{result?.message}</p>

            {result?.terminated && (
              <div className="delete-section">
                <label style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
                  <input
                    type="checkbox"
                    checked={deleteDir}
                    onChange={e => setDeleteDir(e.target.checked)}
                    style={{ width: 'auto' }}
                  />
                  Also delete Simulation directory + log files
                </label>
                {deleteDir && (
                  <button
                    className="btn-danger"
                    disabled={deleting || delResult}
                    onClick={handleDelete}
                  >
                    {deleting ? 'Deleting...' : 'Delete Directory'}
                  </button>
                )}
                {delResult && <p className="hint">{delResult.message}</p>}
              </div>
            )}

            <div className="dialog-actions">
              <button className="btn-primary" onClick={onClose}>Done</button>
            </div>
          </div>
        )}

        {phase === 'error' && (
          <div className="dialog-body">
            <div className="result-msg error">✗ Error: {error}</div>
            <div className="dialog-actions">
              <button className="btn-ghost" onClick={onClose}>Close</button>
              <button className="btn-danger" onClick={handleKill}>Retry</button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
