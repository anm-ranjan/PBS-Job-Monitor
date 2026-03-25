/**
 * API client — all fetch/SSE calls to the FastAPI backend.
 *
 * The base URL is empty so calls go to the same origin (FastAPI serves
 * the built React dist, or Vite's proxy forwards /api/* in dev mode).
 */

const BASE = ''

async function apiCall(method, path, body) {
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json' },
  }
  if (body !== undefined) opts.body = JSON.stringify(body)

  const res = await fetch(`${BASE}${path}`, opts)
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || `HTTP ${res.status}`)
  }
  return res.json()
}

// ---------------------------------------------------------------------------
// Jobs
// ---------------------------------------------------------------------------

/** Fetch all jobs. Pass forceRefresh=true to bypass backend cache. */
export function fetchJobs(forceRefresh = false) {
  const qs = forceRefresh ? '?force_refresh=true' : ''
  return apiCall('GET', `/api/jobs${qs}`)
}

/** Fetch backend config (servers, drive_mapping). */
export function fetchConfig() {
  return apiCall('GET', '/api/config')
}

/**
 * Kill a running job. Blocks until job_log appears or timeout (on the server side).
 * @param {string} jobId
 * @param {object} jobData  { server, job_path }
 */
export function killJob(jobId, jobData) {
  return apiCall('POST', `/api/jobs/${encodeURIComponent(jobId)}/kill`, {
    server: jobData.Server,
    job_id: jobId,
    job_path: jobData.Job_Path,
  })
}

/**
 * Delete a job's Simulation directory.
 * @param {string} jobId
 * @param {object} jobData  { server, job_path }
 */
export function deleteDirectory(jobId, jobData) {
  return apiCall('DELETE', `/api/jobs/${encodeURIComponent(jobId)}/directory`, {
    server: jobData.Server,
    job_id: jobId,
    job_path: jobData.Job_Path,
  })
}

/**
 * Submit a new PBS job via qsub.
 * @param {string} windowsPath  - Windows path to job directory
 * @param {string} scriptName   - submission script filename
 * @param {boolean} metaGenerateOnFinish - generate META DB when job finishes
 */
export function submitJob(windowsPath, scriptName = 'qsubrunfhgfs.sh', metaGenerateOnFinish = false) {
  return apiCall('POST', '/api/jobs/submit', {
    windows_path: windowsPath,
    script_name: scriptName,
    meta_generate_on_finish: metaGenerateOnFinish,
  })
}

/**
 * Fetch all Plotly figures for a job's messag file.
 * @param {string} jobId
 * @param {object} jobData  { Server, Job_Path }
 * @returns {Promise<{summary: object, plots: object}>}
 */
export function fetchPlots(jobId, jobData) {
  const params = new URLSearchParams({
    server: jobData.Server,
    job_path: jobData.Job_Path,
  })
  return apiCall('GET', `/api/jobs/${encodeURIComponent(jobId)}/plots?${params}`)
}

/**
 * Fetch the optimal DTMAX load-curve points for a finished job.
 * @param {string} jobId
 * @param {object} jobData  { Server, Job_Path }
 * @returns {Promise<{points: Array<{time: number, dt: number}>}>}
 */
export function fetchOptimalTimestep(jobId, jobData) {
  const params = new URLSearchParams({
    server: jobData.Server,
    job_path: jobData.Job_Path,
  })
  return apiCall('GET', `/api/jobs/${encodeURIComponent(jobId)}/optimal-timestep?${params}`)
}

/**
 * Create an EventSource for live log streaming (SSE).
 * Call .close() on the returned EventSource when done.
 *
 * @param {string} jobId
 * @param {object} jobData  { Server, Job_Path }
 * @param {number} lines    tail line count (default 100)
 * @returns {EventSource}
 */
export function createLogEventSource(jobId, jobData, lines = 100) {
  const params = new URLSearchParams({
    server: jobData.Server,
    job_path: jobData.Job_Path,
    lines: String(lines),
  })
  const url = `${BASE}/api/jobs/${encodeURIComponent(jobId)}/log/stream?${params}`
  return new EventSource(url)
}

// ---------------------------------------------------------------------------
// Job database management
// ---------------------------------------------------------------------------

/** Remove a single finished job from the backend database. */
export function deleteJobRecord(jobId) {
  return apiCall('DELETE', `/api/db/jobs/${encodeURIComponent(jobId)}`)
}

/** Remove all finished jobs from the backend database. */
export function deleteAllFinished() {
  return apiCall('DELETE', '/api/db/jobs/finished')
}

/**
 * Compute optimal DTMAX load-curve points from an arbitrary simulation folder.
 * Mirrors fetchInterimPlots — works without messag_react.
 * @param {string} folderPath  Windows path to the folder containing messag
 */
export function fetchInterimOptimalTimestep(folderPath) {
  const params = new URLSearchParams({ folder_path: folderPath })
  return apiCall('GET', `/api/report/interim-optimal-timestep?${params}`)
}

// ---------------------------------------------------------------------------
// META CAE Systems viewer
// ---------------------------------------------------------------------------

/**
 * Fetch META viewer status for a job.
 * @param {string} jobId
 * @param {object} jobData  { Server, Job_Path }
 */
export function fetchMetaStatus(jobId, jobData) {
  return apiCall('GET', `/api/jobs/${encodeURIComponent(jobId)}/meta/status`)
}

/**
 * Trigger META batch DB generation for a job.
 * @param {string} jobId
 * @param {object} jobData  { Server, Job_Path }
 */
export function triggerMetaGenerate(jobId, jobData) {
  return apiCall('POST', `/api/jobs/${encodeURIComponent(jobId)}/meta/generate`, {
    server: jobData.Server,
    job_path: jobData.Job_Path,
  })
}

/**
 * Launch META viewer on the server for a ready metadb.
 * @param {string} jobId
 * @param {object} jobData  { Server, Job_Path }
 * @returns {Promise<{cmd: string}>}
 */
export function launchMetaViewer(jobId, jobData) {
  return apiCall('POST', `/api/jobs/${encodeURIComponent(jobId)}/meta/launch-viewer`, {
    server: jobData.Server,
    job_path: jobData.Job_Path,
  })
}

/**
 * Enable or disable auto-watch (d3plot polling) for a job.
 * @param {string} jobId
 * @param {object} jobData  { Server, Job_Path }
 * @param {boolean} enabled
 */
export function setMetaAutoWatch(jobId, jobData, enabled) {
  return apiCall('POST', `/api/jobs/${encodeURIComponent(jobId)}/meta/auto-watch`, {
    enabled,
    server: jobData.Server,
    job_path: jobData.Job_Path,
  })
}

/**
 * Update meta_generate_on_finish flag for a job.
 * @param {string} jobId
 * @param {object} jobData  { Server, Job_Path }
 * @param {boolean} enabled
 */
export function setMetaGenerateOnFinish(jobId, jobData, enabled) {
  return apiCall('PATCH', `/api/jobs/${encodeURIComponent(jobId)}/meta/settings`, {
    meta_generate_on_finish: enabled,
    server: jobData.Server,
    job_path: jobData.Job_Path,
  })
}

// ---------------------------------------------------------------------------
// Interim (Dashboard) Report
// ---------------------------------------------------------------------------

/**
 * Parse the messag file inside a simulation folder and return Plotly figures.
 * The backend appends '\messag' automatically.
 * @param {string} folder_path  Windows path to the simulation folder
 */
export function fetchInterimPlots(folder_path) {
  const params = new URLSearchParams({ folder_path })
  return apiCall('GET', `/api/report/interim-plots?${params}`)
}

// ---------------------------------------------------------------------------
// Final (Static) Report — python server
// ---------------------------------------------------------------------------

/**
 * Trigger report generation and wait for completion (blocking on server side).
 * @param {string} windowsPath
 */
export function generateReport(windowsPath) {
  return apiCall('POST', '/api/report/generate', { windows_path: windowsPath })
}

/**
 * Check if a report's start_server.* file exists.
 * @param {string} windowsPath
 */
export function checkReportStatus(windowsPath) {
  const params = new URLSearchParams({ windows_path: windowsPath })
  return apiCall('GET', `/api/report/status?${params}`)
}

/**
 * Launch the HTML report viewer subprocess on the server.
 * @param {string} windowsPath
 */
export function launchReportViewer(windowsPath) {
  return apiCall('POST', '/api/report/launch', { windows_path: windowsPath })
}
