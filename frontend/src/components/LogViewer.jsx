import { useEffect, useRef, useState } from 'react'
import { createLogEventSource } from '../api.js'
import './LogViewer.css'

export default function LogViewer({ job }) {
  const [content, setContent] = useState('')
  const [fileSize, setFileSize] = useState(0)
  const [connected, setConnected] = useState(false)
  const [error, setError] = useState(null)
  const [lines, setLines] = useState(100)
  const [autoScroll, setAutoScroll] = useState(true)
  const esRef = useRef(null)
  const preRef = useRef(null)

  function connect(lineCount) {
    // Close existing connection
    if (esRef.current) esRef.current.close()

    setConnected(false)
    setError(null)

    const es = createLogEventSource(job.JobID, job, lineCount)
    esRef.current = es

    es.onopen = () => setConnected(true)

    es.onmessage = e => {
      const msg = JSON.parse(e.data)
      setContent(msg.content)
      setFileSize(msg.size)
      // Auto-scroll to bottom
      if (autoScroll && preRef.current) {
        preRef.current.scrollTop = preRef.current.scrollHeight
      }
    }

    es.onerror = () => {
      setConnected(false)
      setError('SSE connection lost — retrying...')
    }
  }

  // Connect on mount / job change
  useEffect(() => {
    connect(lines)
    return () => { if (esRef.current) esRef.current.close() }
  }, [job.JobID, job.Job_Path])

  // Scroll to bottom when content updates and autoScroll is on
  useEffect(() => {
    if (autoScroll && preRef.current) {
      preRef.current.scrollTop = preRef.current.scrollHeight
    }
  }, [content, autoScroll])

  function handleLinesChange(e) {
    const n = Number(e.target.value)
    setLines(n)
    connect(n)
  }

  const fmt = bytes => bytes > 1024 * 1024
    ? `${(bytes / 1024 / 1024).toFixed(1)} MB`
    : `${(bytes / 1024).toFixed(1)} KB`

  return (
    <div className="log-viewer">
      <div className="log-toolbar">
        <div className="log-status">
          <span className={`dot ${connected ? 'green' : error ? 'red' : 'grey'}`} />
          {connected ? 'Live' : error || 'Connecting...'}
        </div>

        <div className="log-controls">
          <label>Tail lines
            <select value={lines} onChange={handleLinesChange} style={{ width: 80 }}>
              {[50, 100, 200, 500, 1000].map(n => (
                <option key={n} value={n}>{n}</option>
              ))}
            </select>
          </label>

          <label style={{ flexDirection: 'row', alignItems: 'center', gap: 6 }}>
            <input
              type="checkbox"
              checked={autoScroll}
              onChange={e => setAutoScroll(e.target.checked)}
              style={{ width: 'auto' }}
            />
            Auto-scroll
          </label>
        </div>

        <div className="log-size">{fileSize > 0 ? fmt(fileSize) : '—'}</div>
      </div>

      <pre ref={preRef} className="log-pre">
        {content || '(no content — messag file not found or empty)'}
      </pre>
    </div>
  )
}
