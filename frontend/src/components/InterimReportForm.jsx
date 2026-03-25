import { useState } from 'react'
import { fetchInterimPlots } from '../api.js'
import './Dialog.css'

/**
 * Interim (Dashboard) Report form.
 *
 * User provides the Windows path to the simulation folder containing the
 * messag file. The 'messag' filename is appended automatically by the backend.
 * Works for any intermediate or final simulation output folder.
 */
export default function InterimReportForm({ onClose, onLoaded }) {
  const [folder_path, setFolderPath] = useState('')
  const [phase, setPhase] = useState('form')   // 'form' | 'loading' | 'error'
  const [error, setError] = useState(null)

  async function handleLoad(e) {
    e.preventDefault()
    if (!folder_path.trim()) return
    setPhase('loading')
    setError(null)
    try {
      const data = await fetchInterimPlots(folder_path.trim())
      onLoaded({ folder_path: folder_path.trim(), ...data })
      onClose()
    } catch (err) {
      setError(err.message)
      setPhase('error')
    }
  }

  return (
    <div className="dialog-overlay">
      <div className="dialog">
        <div className="dialog-header">
          Interim Report
          <button className="btn-ghost btn-sm" onClick={onClose}>✕</button>
        </div>

        {phase === 'form' && (
          <form className="dialog-body" onSubmit={handleLoad}>
            <label>Simulation folder path
              <input
                type="text"
                value={folder_path}
                onChange={e => setFolderPath(e.target.value)}
                placeholder="Z:\path\to\simulation\folder"
                required
                autoFocus
              />
            </label>
            <p className="hint">
              Provide the path to the folder containing the <code>messag</code> file.
              This can be any intermediate or final simulation output — the filename
              is added automatically.
            </p>
            <div className="dialog-actions">
              <button type="button" className="btn-ghost" onClick={onClose}>Cancel</button>
              <button type="submit" className="btn-primary">Load Plots</button>
            </div>
          </form>
        )}

        {phase === 'loading' && (
          <div className="dialog-body">
            <div className="spinner-row">
              <div className="spinner" />
              Parsing messag file and generating plots...
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
