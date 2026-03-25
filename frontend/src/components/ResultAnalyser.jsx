import { useState, useEffect } from 'react'
import Plot from 'react-plotly.js'
import { fetchBinoutEntries, fetchBinoutData } from '../api.js'

export default function ResultAnalyser({ job }) {
  const [index, setIndex]           = useState(null)
  const [loadingIdx, setLoadingIdx] = useState(true)
  const [idxError, setIdxError]     = useState(null)

  const [selEntry, setSelEntry]       = useState(null)
  const [selVariable, setSelVariable] = useState(null)

  const [idFilter, setIdFilter] = useState('')
  const [selIds, setSelIds]     = useState(new Set())

  const [plotData, setPlotData]     = useState(null)
  const [loadingPlot, setLoadingPlot] = useState(false)
  const [plotError, setPlotError]   = useState(null)

  useEffect(() => {
    setLoadingIdx(true)
    fetchBinoutEntries(job.JobID, job)
      .then(data => { setIndex(data); setLoadingIdx(false) })
      .catch(err  => { setIdxError(err.message); setLoadingIdx(false) })
  }, [job.JobID])

  function handleEntryClick(entryName) {
    if (selEntry === entryName) {
      setSelEntry(null)
    } else {
      setSelEntry(entryName)
      setSelVariable(null)
      setSelIds(new Set())
      setIdFilter('')
      setPlotData(null)
      setPlotError(null)
    }
  }

  function handleVariableClick(varInfo) {
    setSelVariable(varInfo.name)
    setSelIds(new Set())
    setPlotData(null)
    setPlotError(null)
    // Scalar variables: auto-fetch immediately.
    // Per-entity variables: show entity selector first; user picks parts then clicks Plot.
    if (!varInfo.per_entity) {
      doFetch(selEntry, varInfo.name, null)
    }
  }

  function doFetch(entry, variable, ids) {
    setLoadingPlot(true)
    setPlotError(null)
    fetchBinoutData(job.JobID, job, entry, variable, ids)
      .then(data => { setPlotData(data); setLoadingPlot(false) })
      .catch(err  => { setPlotError(err.message); setLoadingPlot(false) })
  }

  function toggleId(id) {
    setSelIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) {
        next.delete(id)
      } else if (next.size < 10) {
        next.add(id)
      }
      return next
    })
  }

  function selectAll(allIds) {
    const visible = getFilteredIds(allIds)
    setSelIds(prev => {
      const next = new Set(prev)
      for (const id of visible) {
        if (next.size >= 10) break
        next.add(id)
      }
      return next
    })
  }

  function clearIds() {
    setSelIds(new Set())
  }

  function getFilteredIds(ids) {
    if (!idFilter) return ids
    const q = idFilter.toLowerCase()
    return ids.filter(id => id.toLowerCase().includes(q))
  }

  const currentEntry = index?.entries?.find(e => e.name === selEntry)
  const currentVar   = currentEntry?.variables?.find(v => v.name === selVariable)

  const plotTraces = plotData
    ? plotData.series.map(s => ({
        x: plotData.time,
        y: s.values,
        name: s.id,
        type: 'scatter',
        mode: 'lines',
      }))
    : []

  return (
    <div className="result-analyser">

      {/* ── Left sidebar ── */}
      <div className="ra-sidebar">
        {loadingIdx && (
          <div className="ra-loading">
            <span className="spinner-inline" /> Reading binout…
          </div>
        )}
        {idxError && (
          <div className="ra-error">{idxError}</div>
        )}
        {index && !index.found && (
          <div className="ra-empty">No binout files found in Simulation directory.</div>
        )}

        {index?.entries?.map(entry => {
          const isOpen      = selEntry === entry.name
          const filteredIds = entry.ids ? getFilteredIds(entry.ids) : []

          return (
            <div key={entry.name} className="ra-entry">
              <div className="ra-entry-row" onClick={() => handleEntryClick(entry.name)}>
                <span className="ra-chevron">{isOpen ? '▾' : '▸'}</span>
                <span className="ra-entry-name">{entry.name}</span>
              </div>

              {isOpen && (
                <div className="ra-entry-body">
                  {[...entry.variables].sort((a, b) => a.name.localeCompare(b.name)).map(v => (
                    <div
                      key={v.name}
                      className={`ra-var-row ${selVariable === v.name ? 'active' : ''}`}
                      onClick={() => handleVariableClick(v)}
                    >
                      <span className="ra-var-dot">{selVariable === v.name ? '●' : '○'}</span>
                      {v.name}
                    </div>
                  ))}

                  {/* Entity selector — shown only for the open entry's per-entity variable */}
                  {isOpen && selVariable && currentVar?.per_entity && entry.ids && (
                    <div className="ra-entity-section">
                      <input
                        className="ra-entity-filter"
                        placeholder="Filter IDs…"
                        value={idFilter}
                        onChange={e => setIdFilter(e.target.value)}
                      />
                      <div className="ra-entity-actions">
                        <button className="ra-link-btn" onClick={() => selectAll(entry.ids)}>
                          Select All
                        </button>
                        <button className="ra-link-btn" onClick={clearIds}>Clear</button>
                        <span className="ra-id-count">{selIds.size}/10</span>
                      </div>
                      <div className="ra-entity-list">
                        {filteredIds.map(id => (
                          <label key={id} className="ra-entity-item">
                            <input
                              type="checkbox"
                              checked={selIds.has(id)}
                              disabled={!selIds.has(id) && selIds.size >= 10}
                              onChange={() => toggleId(id)}
                            />
                            <span>{id}</span>
                          </label>
                        ))}
                      </div>
                      <button
                        className="ra-plot-btn"
                        onClick={() => doFetch(
                          selEntry,
                          selVariable,
                          selIds.size > 0 ? Array.from(selIds).join(',') : null
                        )}
                      >
                        {selIds.size > 0 ? 'Plot Selected' : 'Plot All'}
                      </button>
                    </div>
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>

      {/* ── Right chart panel ── */}
      <div className="ra-chart-panel">
        {loadingPlot && (
          <div className="ra-chart-overlay">
            <span className="spinner-inline" /> Loading data…
          </div>
        )}
        {plotError && !loadingPlot && (
          <div className="ra-chart-overlay ra-error">{plotError}</div>
        )}
        {!plotData && !loadingPlot && !plotError && (
          <div className="ra-placeholder">
            {selVariable && currentVar?.per_entity
              ? 'Select parts in the sidebar, then click Plot'
              : 'Select an entry and variable to plot'}
          </div>
        )}
        {plotData && !loadingPlot && (
          <Plot
            data={plotTraces}
            layout={{
              paper_bgcolor: 'rgba(0,0,0,0)',
              plot_bgcolor:  'rgba(0,0,0,0)',
              font: { family: 'DM Mono, monospace', color: '#a0aec0', size: 11 },
              margin: { t: 30, r: 20, b: 50, l: 60 },
              xaxis: {
                title:     { text: 'Time', font: { size: 11 } },
                gridcolor: 'rgba(255,255,255,0.06)',
                zeroline:  false,
              },
              yaxis: {
                title:     { text: selVariable || '', font: { size: 11 } },
                gridcolor: 'rgba(255,255,255,0.06)',
                zeroline:  false,
              },
              legend: {
                bgcolor:     'rgba(0,0,0,0)',
                bordercolor: 'rgba(255,255,255,0.1)',
                borderwidth: 1,
              },
              autosize: true,
            }}
            config={{ displayModeBar: false, responsive: true }}
            style={{ width: '100%', height: '100%' }}
            useResizeHandler
          />
        )}
      </div>
    </div>
  )
}
