import { useState } from 'react'
import { fetchParts } from '../api.js'

export default function PartsList({ job }) {
  const [parts, setParts]     = useState(null)   // null = not loaded yet
  const [cached, setCached]   = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError]     = useState(null)

  async function handleLoad() {
    setLoading(true)
    setError(null)
    try {
      const data = await fetchParts(job.JobID, job)
      setParts(data.parts)
      setCached(data.cached)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="parts-list">
      {parts === null && !loading && (
        <button className="btn btn-sm" onClick={handleLoad}>
          Show Parts
        </button>
      )}

      {loading && (
        <div className="parts-loading">
          <span className="spinner-inline" />
          {cached === false ? 'Reading d3plot…' : 'Loading…'}
        </div>
      )}

      {error && (
        <div className="parts-error">{error}</div>
      )}

      {parts !== null && !loading && (
        <>
          <div className="parts-count">
            {parts.length} parts
            {cached && <span className="parts-cached-badge">cached</span>}
          </div>
          <table className="parts-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>Title</th>
                <th>Material Type</th>
              </tr>
            </thead>
            <tbody>
              {parts.map(p => (
                <tr key={p.id}>
                  <td className="parts-id">{p.id}</td>
                  <td>{p.title}</td>
                  <td className="parts-mattype">{p.material_type ?? '-'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  )
}
