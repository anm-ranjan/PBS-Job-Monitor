import { useEffect, useMemo, useRef, useState } from 'react'
import Plot from 'react-plotly.js'
import { fetchPlots } from '../api.js'
import './ConvergencePlots.css'

const PLOT_LABELS = {
  iterations_per_timestep: 'Iterations / Timestep',
  time_duration: 'Time Duration',
  displacement_norm: 'Displacement Norm Ratio',
  energy_norm: 'Energy Norm Ratio',
  convergence_status: 'Status Summary',
  displacement_evolution: 'Displacement Evolution',
}

const DARK_LAYOUT = {
  paper_bgcolor: 'rgba(0,0,0,0)',
  plot_bgcolor: 'rgba(10,18,30,0.7)',
  font: { color: '#dce8f5', family: 'DM Mono, Consolas, monospace', size: 11 },
  xaxis: { gridcolor: '#101e30', zerolinecolor: '#101e30' },
  yaxis: { gridcolor: '#101e30', zerolinecolor: '#101e30' },
  margin: { l: 50, r: 50, t: 60, b: 50 },
}

const LIGHT_LAYOUT = {
  paper_bgcolor: 'rgba(0,0,0,0)',
  plot_bgcolor: 'rgba(248,250,252,0.9)',
  font: { color: '#0e1e2e', family: 'DM Mono, Consolas, monospace', size: 11 },
  xaxis: { gridcolor: '#d0dce8', zerolinecolor: '#b8cad8', tickfont: { color: '#0e1e2e' } },
  yaxis: { gridcolor: '#d0dce8', zerolinecolor: '#b8cad8', tickfont: { color: '#0e1e2e' } },
  margin: { l: 50, r: 50, t: 60, b: 50 },
}

/** Read current theme from the <html> data-theme attribute. */
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

function mergeLayout(fig, theme) {
  const base = theme === 'light' ? LIGHT_LAYOUT : DARK_LAYOUT
  return {
    ...fig,
    layout: {
      ...fig.layout,
      ...base,
      font: { ...base.font, ...fig.layout?.font },
      // Axis overrides: merge per-axis so log/title settings from backend are preserved
      xaxis: { ...fig.layout?.xaxis, ...base.xaxis },
      yaxis: { ...fig.layout?.yaxis, ...base.yaxis },
    },
  }
}

// Keys that get step-based opacity + blinking current-position marker.
const NORM_PLOT_KEYS = new Set(['displacement_norm', 'energy_norm'])

// Keys that get per-bar opacity fading when step fading is enabled.
const HISTOGRAM_PLOT_KEYS = new Set(['iterations_per_timestep', 'time_duration'])

/**
 * Override trace opacity based on distance from the last (current) step.
 *   rank 0  → 1.00  (current step, full opacity)
 *   rank 1  → 0.75  (n-1 step, 25% transparent)
 *   rank 2  → 0.50  (n-2 step, 50% transparent)
 *   rank 3+ → 0.10  (all older steps, 90% transparent)
 */
function applyNormStepOpacity(traces) {
  const n = traces.length
  return traces.map((trace, i) => {
    const rank = n - 1 - i
    const opacity = rank === 0 ? 1.0 : rank === 1 ? 0.75 : rank === 2 ? 0.50 : 0.10
    return { ...trace, opacity }
  })
}

function clearNormStepOpacity(traces) {
  return traces.map(trace => ({ ...trace, opacity: 1.0 }))
}

/**
 * Apply per-bar opacity to histogram traces based on distance from the last bar.
 *   rank 0  → 1.00  (current/latest step, fully opaque)
 *   rank 1  → 0.50  (one step back)
 *   rank 2+ → 0.05  (all older steps, nearly invisible)
 */
function applyHistogramStepOpacity(traces) {
  return traces.map(trace => {
    if (!trace.x) return trace
    const n = trace.x.length
    const opacities = trace.x.map((_, i) => {
      const rank = n - 1 - i
      if (rank === 0) return 1.0
      if (rank === 1) return 0.50
      return 0.05
    })
    return { ...trace, marker: { ...trace.marker, opacity: opacities } }
  })
}

function clearHistogramStepOpacity(traces) {
  return traces.map(trace => {
    if (!trace.marker) return trace
    const { opacity: _removed, ...markerRest } = trace.marker
    return { ...trace, marker: markerRest }
  })
}

function HistogramPlot({ fig, theme, fadingEnabled }) {
  const processedFig = useMemo(() => {
    const data = fadingEnabled
      ? applyHistogramStepOpacity(fig.data)
      : clearHistogramStepOpacity(fig.data)
    return { ...fig, data }
  }, [fig, fadingEnabled])

  const merged = mergeLayout(processedFig, theme)
  return (
    <Plot
      data={merged.data}
      layout={merged.layout}
      config={{ responsive: true, displayModeBar: true, displaylogo: false }}
      style={{ width: '100%' }}
      useResizeHandler
    />
  )
}

/**
 * Return the last data point from the last trace (= current step, last iteration).
 * Returns null if the figure has no usable traces.
 */
function getLastPoint(fig) {
  const traces = fig.data
  if (!traces || traces.length === 0) return null
  const last = traces[traces.length - 1]
  if (!last.x || !last.y || last.x.length === 0) return null
  const n = last.x.length
  return {
    x: last.x[n - 1],
    y: last.y[n - 1],
    stepName: last.name || '',
    iterNum: last.x[n - 1],
  }
}

/**
 * Norm plot with step-based opacity and a blinking current-position marker.
 * Isolated in its own component so only these two charts re-render on each
 * blink tick — the other four plots are completely unaffected.
 */
function NormPlot({ fig, theme, fadingEnabled }) {
  const [blinkVisible, setBlinkVisible] = useState(true)
  useEffect(() => {
    const id = setInterval(() => setBlinkVisible(v => !v), 1400)
    return () => clearInterval(id)
  }, [])

  const processedFig = useMemo(() => {
    const data = fadingEnabled ? applyNormStepOpacity(fig.data) : clearNormStepOpacity(fig.data)
    const pt = getLastPoint(fig)
    if (pt) {
      const blinkTrace = {
        type: 'scatter',
        x: [pt.x],
        y: [pt.y],
        mode: 'markers',
        marker: {
          symbol: 'circle-open',
          size: 12,
          color: '#f39c12',
          line: { color: '#f39c12', width: 2.5 },
        },
        opacity: blinkVisible ? 1.0 : 0.0,
        showlegend: false,
        name: '_blink',
        hovertemplate: `${pt.stepName} — Iter ${pt.iterNum} (current)<extra></extra>`,
      }
      return { ...fig, data: [...data, blinkTrace] }
    }
    return { ...fig, data }
  }, [fig, blinkVisible, fadingEnabled])

  const merged = mergeLayout(processedFig, theme)
  return (
    <Plot
      data={merged.data}
      layout={merged.layout}
      config={{ responsive: true, displayModeBar: true, displaylogo: false }}
      style={{ width: '100%' }}
      useResizeHandler
    />
  )
}

/** Shared rendering — accepts already-fetched {summary, plots} */
export function PlotPanel({ summary, plots }) {
  const theme = useTheme()
  const [fadingEnabled, setFadingEnabled] = useState(true)

  return (
    <div className="convergence-plots">
      <div className="summary-strip">
        <div className="summary-item">
          <span className="s-label">Hostname</span>
          <span className="s-value">{summary.hostname || '—'}</span>
        </div>
        <div className="summary-item">
          <span className="s-label">Started</span>
          <span className="s-value">
            {summary.start_date || '—'}<br />{summary.start_time || ''}
          </span>
        </div>
        <div className="summary-item">
          <span className="s-label">Timesteps</span>
          <span className="s-value">{summary.total_steps}</span>
        </div>
        <div className="summary-item">
          <span className="s-label">Converged</span>
          <span className="s-value success">{summary.converged_steps}</span>
        </div>
        <div className="summary-item">
          <span className="s-label">Failed</span>
          <span className="s-value danger">{summary.failed_steps}</span>
        </div>
        <div className="summary-item">
          <span className="s-label">Iterations</span>
          <span className="s-value">{summary.total_iterations}</span>
        </div>
        {summary.current_sim_time != null && (
          <div className="summary-item">
            <span className="s-label">Sim Time</span>
            <span className="s-value">{Number(summary.current_sim_time).toExponential(3)}</span>
          </div>
        )}
        {summary.current_step_size != null && (
          <div className="summary-item">
            <span className="s-label">Step Time</span>
            <span className="s-value">{Number(summary.current_step_size).toExponential(3)}</span>
          </div>
        )}
        <div className="summary-item">
          <span className="s-label">Status</span>
          <span className={`s-value status-${summary.termination_status}`}>
            {summary.termination_status}
          </span>
        </div>
      </div>

      <div className="plots-toolbar">
        <button
          className={`btn-ghost btn-sm fade-toggle${fadingEnabled ? ' fade-toggle--on' : ''}`}
          onClick={() => setFadingEnabled(v => !v)}
          title="Toggle step fading on norm ratio plots"
        >
          {fadingEnabled ? 'Step Fading: On' : 'Step Fading: Off'}
        </button>
      </div>

      <div className="plot-grid">
        {Object.entries(plots).map(([key, fig]) => (
          <div key={key} className="plot-wrap">
            <div className="plot-label">{PLOT_LABELS[key] || key}</div>
            {NORM_PLOT_KEYS.has(key) ? (
              <NormPlot fig={fig} theme={theme} fadingEnabled={fadingEnabled} />
            ) : HISTOGRAM_PLOT_KEYS.has(key) ? (
              <HistogramPlot fig={fig} theme={theme} fadingEnabled={fadingEnabled} />
            ) : (() => {
              const m = mergeLayout(fig, theme)
              return (
                <Plot
                  data={m.data}
                  layout={m.layout}
                  config={{ responsive: true, displayModeBar: true, displaylogo: false }}
                  style={{ width: '100%' }}
                  useResizeHandler
                />
              )
            })()}
          </div>
        ))}
      </div>
    </div>
  )
}

// Plots re-fetch interval while a job is selected (ms).
// Must be >= messag_copy_interval (30 s) + backend cache_timeout (20 s)
// so each refresh returns genuinely updated data.
const PLOT_REFRESH_MS = 60_000

/** Used by JobDetail — fetches its own data from a running/queued job */
export default function ConvergencePlots({ job }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [lastUpdated, setLastUpdated] = useState(null)
  // Keep a ref so the interval callback always sees the latest setter fns
  const jobRef = useRef(job)
  useEffect(() => { jobRef.current = job }, [job])

  useEffect(() => {
    let cancelled = false

    function doFetch(isInitial) {
      const j = jobRef.current
      fetchPlots(j.JobID, j)
        .then(d => {
          if (cancelled) return
          setData(d)
          setLastUpdated(new Date())
          setError(null)
          if (isInitial) setLoading(false)
        })
        .catch(e => {
          if (cancelled) return
          // On refresh failure keep existing data visible; only show error on
          // initial load when there is nothing else to display.
          if (isInitial) { setError(e.message); setLoading(false) }
        })
    }

    // Initial load — clear stale data and show spinner
    setLoading(true)
    setError(null)
    setData(null)
    setLastUpdated(null)
    doFetch(true)

    // Silent background refresh while this job stays selected
    const id = setInterval(() => doFetch(false), PLOT_REFRESH_MS)
    return () => { cancelled = true; clearInterval(id) }
  }, [job.JobID, job.Job_Path])

  if (loading) return <div className="plots-msg">Parsing messag file and generating plots...</div>
  if (error) return <div className="plots-msg error">Error: {error}</div>
  if (!data) return null

  return (
    <>
      {lastUpdated && (
        <div className="plots-updated">
          Plots updated: {lastUpdated.toLocaleTimeString()} · auto-refresh every {PLOT_REFRESH_MS / 1000} s
        </div>
      )}
      <PlotPanel summary={data.summary} plots={data.plots} />
    </>
  )
}
