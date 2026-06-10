import { useState, useEffect } from 'react'
import Plot from 'react-plotly.js'
import { fetchOptimalTimestep, fetchInterimOptimalTimestep } from '../api.js'
import './OptimalTimestep.css'

function useTheme() {
  const [theme, setTheme] = useState(
    () => document.documentElement.getAttribute('data-theme') || 'dark'
  )
  useEffect(() => {
    const observer = new MutationObserver(() => {
      setTheme(document.documentElement.getAttribute('data-theme') || 'dark')
    })
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    return () => observer.disconnect()
  }, [])
  return theme
}

/**
 * Pad a number to a fixed-width column using LS-DYNA ASCII convention.
 * Mirrors numpy fmt="%Nf" format strings.
 */
function fmtF(value, totalWidth, decimals) {
  return value.toFixed(decimals).padStart(totalWidth)
}

/** Build the ASCII export content (two-column, numpy-style fmt). */
function buildAsciiContent(points) {
  const header = '# Optimal DTMAX load curve — exported by PBS Job Monitor\n' +
                 '# Col 1: simulation time   Col 2: timestep size (dt)\n'
  const rows = points.map(p => fmtF(p.time, 20, 6) + fmtF(p.dt, 19, 6))
  return header + rows.join('\n') + '\n'
}

/** Trigger a browser file download without a server round-trip. */
function downloadText(filename, content) {
  const blob = new Blob([content], { type: 'text/plain' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

/**
 * Expand change-points into a step-function dataset for Plotly.
 *
 * Given change-point pairs [(t0, dt0), (t1, dt1), ...] the dt value at t0
 * stays constant until t1, then switches.  We need an extra point just
 * before each transition so Plotly draws a clean horizontal step.
 *
 * The final step extends to the last time value (estimated from the last
 * change-point plus one dt into the future) so the line doesn't end abruptly.
 */
function expandToStepFunction(points) {
  if (points.length === 0) return { x: [], y: [] }

  const x = []
  const y = []

  for (let i = 0; i < points.length; i++) {
    const { time, dt } = points[i]
    x.push(time)
    y.push(dt)

    if (i + 1 < points.length) {
      // Add a point just before the next transition so the segment is horizontal
      x.push(points[i + 1].time)
      y.push(dt)
    }
  }

  // Extend the last step one dt past the last change-point
  const last = points[points.length - 1]
  x.push(last.time + last.dt)
  y.push(last.dt)

  return { x, y }
}

/**
 * Render the optimal timestep chart and export button.
 *
 * Props (mutually exclusive — supply one):
 *   job        — job object from the PBS database (used for JobDetail)
 *   folderPath — Windows path to the simulation folder (used for InterimDetail)
 */
export default function OptimalTimestep({ job, folderPath }) {
  const [phase, setPhase] = useState('loading')  // 'loading' | 'ready' | 'empty' | 'error'
  const [points, setPoints] = useState([])
  const [error, setError] = useState(null)
  const theme = useTheme()

  useEffect(() => {
    let cancelled = false
    setPhase('loading')
    setPoints([])

    const fetchFn = folderPath
      ? fetchInterimOptimalTimestep(folderPath)
      : fetchOptimalTimestep(job.JobID, job)

    fetchFn
      .then(data => {
        if (cancelled) return
        if (!data.points || data.points.length === 0) {
          setPhase('empty')
        } else {
          setPoints(data.points)
          setPhase('ready')
        }
      })
      .catch(err => {
        if (!cancelled) {
          setError(err.message)
          setPhase('error')
        }
      })

    return () => { cancelled = true }
  }, [folderPath, job?.JobID, job?.Job_Path])

  function handleExport() {
    const content = buildAsciiContent(points)
    const label = job ? job.JobID : folderPath.replace(/[\\/:*?"<>|]/g, '_').slice(-30)
    const filename = `optimal_timestep_${label}.dat`
    downloadText(filename, content)
  }

  if (phase === 'loading') {
    return <div className="ots-status">Computing optimal timestep schedule...</div>
  }

  if (phase === 'error') {
    return <div className="ots-status ots-error">Error: {error}</div>
  }

  if (phase === 'empty') {
    return (
      <div className="ots-status">
        No converged steps found — cannot compute optimal timestep schedule.
      </div>
    )
  }

  const { x, y } = expandToStepFunction(points)

  const plotData = [
    {
      x,
      y,
      type: 'scatter',
      mode: 'lines+markers',
      name: 'Optimal dt',
      line: { color: '#2d9ea8', width: 2, shape: 'hv' },
      marker: {
        symbol: 'circle',
        size: 7,
        color: x.map((_, i) => i % 2 === 0 ? '#2d9ea8' : null).filter(Boolean),
      },
      hovertemplate: 'Time: %{x:.6g}<br>dt: %{y:.6g}<extra></extra>',
    },
  ]

  const isDark = theme === 'dark'
  const plotLayout = {
    xaxis: {
      title: 'Simulation Time',
      color: isDark ? '#8fadc5' : '#1a2733',
      gridcolor: isDark ? '#1e3a52' : '#d0d8e0',
      tickfont: { color: isDark ? '#e0e8ef' : '#1a2733' },
    },
    yaxis: {
      title: 'Timestep Size (dt)',
      color: isDark ? '#8fadc5' : '#1a2733',
      gridcolor: isDark ? '#1e3a52' : '#d0d8e0',
      tickfont: { color: isDark ? '#e0e8ef' : '#1a2733' },
    },
    paper_bgcolor: 'transparent',
    plot_bgcolor: isDark ? 'rgba(15,25,35,0.6)' : 'rgba(248,250,252,0.85)',
    font: { color: isDark ? '#e0e8ef' : '#1a2733', size: 12 },
    height: 380,
    margin: { l: 70, r: 40, t: 40, b: 60 },
    showlegend: false,
    uirevision: 'keep',
  }

  const plotConfig = { displayModeBar: true, responsive: true }

  return (
    <div className="ots-container">
      <div className="ots-info-bar">
        <span className="ots-stat">
          <label>Change points</label>{points.length}
        </span>
        <span className="ots-stat">
          <label>Min dt</label>{Math.min(...points.map(p => p.dt)).toExponential(3)}
        </span>
        <span className="ots-stat">
          <label>Max dt</label>{Math.max(...points.map(p => p.dt)).toExponential(3)}
        </span>
        <button className="btn-primary ots-export-btn" onClick={handleExport}>
          Export .dat
        </button>
      </div>

      <Plot data={plotData} layout={plotLayout} config={plotConfig} style={{ width: '100%' }} />

      <div className="ots-hint">
        Each point marks where the timestep size changes. Export as a DTMAX load curve
        to skip failed attempts in the next run.
      </div>
    </div>
  )
}
