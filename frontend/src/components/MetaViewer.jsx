import { useState, useEffect, useCallback } from 'react'
import {
  fetchMetaStatus,
  triggerMetaGenerate,
  launchMetaViewer,
  setMetaAutoWatch,
  setMetaGenerateOnFinish,
} from '../api.js'

const POLL_INTERVAL_MS = 10_000

export default function MetaViewer({ job }) {
  const [status, setStatus] = useState(null)
  const [launching, setLaunching] = useState(false)
  const [launchCmd, setLaunchCmd] = useState(null)
  const [copyDone, setCopyDone] = useState(false)
  const [error, setError] = useState(null)

  const poll = useCallback(async () => {
    try {
      const s = await fetchMetaStatus(job.JobID, job)
      setStatus(s)
    } catch (err) {
      // Silently ignore poll errors — backend may be briefly unreachable
    }
  }, [job.JobID])

  useEffect(() => {
    poll()
    const id = setInterval(poll, POLL_INTERVAL_MS)
    return () => clearInterval(id)
  }, [poll])

  // Show launch command as soon as metadb becomes ready
  useEffect(() => {
    if (status?.meta_status === 'ready' && !launchCmd) {
      // pre-build a display command so users can copy without clicking "Launch"
      // Actual command comes from the server on launch-viewer call
    }
  }, [status?.meta_status, launchCmd])

  if (!status) {
    return <div className="meta-viewer"><div className="spinner-row"><div className="spinner" />Loading META status...</div></div>
  }

  if (!status.configured) {
    return (
      <div className="meta-viewer">
        <div className="meta-not-configured">
          META not configured. Add <code>meta.executable</code> to <code>config.yaml</code> to enable this feature.
        </div>
      </div>
    )
  }

  async function handleGenerate() {
    setError(null)
    try {
      await triggerMetaGenerate(job.JobID, job)
      poll()
    } catch (err) {
      setError(err.message)
    }
  }

  async function handleLaunch() {
    setLaunching(true)
    setError(null)
    try {
      const res = await launchMetaViewer(job.JobID, job)
      setLaunchCmd(res.cmd)
    } catch (err) {
      setError(err.message)
    } finally {
      setLaunching(false)
    }
  }

  async function handleAutoWatch(e) {
    const enabled = e.target.checked
    try {
      await setMetaAutoWatch(job.JobID, job, enabled)
      setStatus(s => ({ ...s, auto_watch: enabled }))
    } catch (err) {
      setError(err.message)
    }
  }

  async function handleGenerateOnFinish(e) {
    const enabled = e.target.checked
    try {
      await setMetaGenerateOnFinish(job.JobID, job, enabled)
      setStatus(s => ({ ...s, meta_generate_on_finish: enabled }))
    } catch (err) {
      setError(err.message)
    }
  }

  async function handleCopy() {
    if (!launchCmd) return
    try {
      await navigator.clipboard.writeText(launchCmd)
      setCopyDone(true)
      setTimeout(() => setCopyDone(false), 2000)
    } catch {
      // Clipboard API unavailable — user can select+copy manually
    }
  }

  const metaStatus = status.meta_status
  const isFinished = job.Status === 'F'
  const isRunning  = job.Status === 'R'

  const canGenerate = (metaStatus === 'idle' || metaStatus === 'error') && !status.batch_running
  const canRegenerate = !isFinished && metaStatus === 'ready'

  return (
    <div className="meta-viewer">
      <div className="meta-status-row">
        <span className="meta-label">META DB Status:</span>
        {metaStatus === 'idle' && <span className="badge badge-Q">idle</span>}
        {metaStatus === 'generating' && (
          <span className="badge badge-R">
            <span className="spinner spinner-inline" /> generating…
          </span>
        )}
        {metaStatus === 'ready' && <span className="badge badge-done">ready ✓</span>}
        {metaStatus === 'error' && (
          <span className="badge badge-error" title={status.meta_error || ''}>error ✗</span>
        )}
        {status.batch_running && metaStatus !== 'generating' && (
          <span className="badge badge-R"><span className="spinner spinner-inline" /> running</span>
        )}
      </div>

      {metaStatus === 'error' && status.meta_error && (
        <div className="meta-error-detail">{status.meta_error}</div>
      )}

      <div className="meta-controls">
        {canGenerate && (
          <button className="btn-primary" onClick={handleGenerate}>
            Generate META DB
          </button>
        )}
        {canRegenerate && (
          <button className="btn-ghost" onClick={handleGenerate}>
            Re-generate
          </button>
        )}

        {isRunning && (
          <label className="meta-toggle">
            <input
              type="checkbox"
              checked={status.auto_watch}
              onChange={handleAutoWatch}
            />
            Auto-watch (regenerate on new d3plot)
          </label>
        )}

        {(isRunning || job.Status === 'E') && (
          <label className="meta-toggle">
            <input
              type="checkbox"
              checked={status.meta_generate_on_finish}
              onChange={handleGenerateOnFinish}
            />
            Generate on finish
          </label>
        )}
      </div>

      {metaStatus === 'ready' && (
        <div className="meta-launch-section">
          <button className="btn-primary" onClick={handleLaunch} disabled={launching}>
            {launching ? <><span className="spinner spinner-inline" /> Launching…</> : 'Launch on server'}
          </button>

          {launchCmd && (
            <div className="meta-cmd-row">
              <input
                type="text"
                readOnly
                value={launchCmd}
                className="meta-cmd-input"
              />
              <button className="btn-ghost btn-sm" onClick={handleCopy}>
                {copyDone ? 'Copied!' : 'Copy'}
              </button>
            </div>
          )}

          <p className="meta-hint">
            To open locally, paste this command in a terminal (requires META and mapped drives on your machine).
          </p>
        </div>
      )}

      {error && <div className="meta-error-detail">{error}</div>}
    </div>
  )
}
