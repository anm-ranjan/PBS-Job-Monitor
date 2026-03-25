# PBS Job Monitor — Migration from Streamlit to FastAPI + React

**Session date:** 2026-02-26

---

## Why we migrated

The Streamlit dashboard had three core problems:

1. **Memory inefficiency** — Streamlit spawns a full Python execution context per browser tab. Three tabs = three independent Python environments re-running the entire script.
2. **No real-time capability** — Every interaction (selecting a job, clicking a button) reruns the whole script. Messag file parsing and Plotly figure generation on every click was the cause of slow plot loading.
3. **No shared state** — Each tab independently SSHed to servers, fetched jobs, and read files. No caching across tabs.

**Solution:** FastAPI backend (one async Python process serving all clients) + React SPA (efficient component re-rendering, all tabs share the same backend and cache).

---

## What was built

### Backend (`backend/`)

| File | Changes |
|------|---------|
| `convergence_plotter.py` | Added `to_json_dict()` to `ConvergencePlotter` and `parse_and_plot_json()` convenience function — returns Plotly figures as plain dicts suitable for JSON serialisation. Original Figure-returning methods kept intact. |
| `job_monitor.py` | Added `get_jobs_cached(ttl)` — shared module-level job cache so all browser tabs share one SSH fetch. Added `async_wait_for_job_termination()` and `async_wait_for_report_completion()` using `asyncio.sleep` so FastAPI can serve other requests while polling. |
| `main.py` | **New.** FastAPI application with all API endpoints and static file serving. |
| `requirements.txt` | **New.** `fastapi`, `uvicorn[standard]`, `paramiko`, `pyyaml`, `plotly`, `pandas`, `numpy` — no Streamlit. |
| `config.yaml` | Copied from root (unchanged). |
| `que.py` | Copied from root (unchanged). |

#### FastAPI endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/jobs` | Job list with shared TTL cache; `?force_refresh=true` bypasses it |
| GET | `/api/config` | Server list and drive mapping |
| POST | `/api/jobs/{id}/kill` | `qdel` + async poll for `job_log` file |
| DELETE | `/api/jobs/{id}/directory` | `rm -rf Simulation/` + log files via SSH |
| POST | `/api/jobs/submit` | `qsub` via SSH |
| GET | `/api/jobs/{id}/plots` | Parse messag → return all Plotly figures as JSON dicts |
| GET | `/api/jobs/{id}/log/stream` | SSE: streams last N lines of messag every 3 seconds |
| GET | `/api/report/interim-plots` | Takes `folder_path`; appends `\messag` automatically → return Plotly JSON dicts |
| POST | `/api/report/generate` | Start `run_report_win.sh` + async poll up to 4 min |
| GET | `/api/report/status` | Check if `_HTML/start_server.*` exists |
| POST | `/api/report/launch` | Launch report viewer subprocess |
| GET | `/docs` | FastAPI auto-generated Swagger UI |

### Frontend (`frontend/`)

React 18 + Vite 5 SPA. Built once to `frontend/dist/`, which FastAPI serves as static files.

| File | Purpose |
|------|---------|
| `src/api.js` | All `fetch` and `EventSource` calls to the backend |
| `src/App.jsx` + `App.css` | Root layout, job list state, 20s auto-refresh, IPA theme CSS variables |
| `components/Sidebar.jsx` | Filters, refresh, job action buttons, report buttons |
| `components/JobTable.jsx` | Sortable, filterable job table with status badges |
| `components/JobDetail.jsx` | Tabbed panel — "Convergence Plots" and "Live Log" |
| `components/ConvergencePlots.jsx` | Fetches and renders 5 Plotly charts for a running job; exports `PlotPanel` for reuse |
| `components/LogViewer.jsx` | SSE consumer — auto-scroll, configurable tail line count |
| `components/KillJobDialog.jsx` | Kill flow with optional Simulation directory delete |
| `components/SubmitJobForm.jsx` | `qsub` submission form |
| `components/ReportForm.jsx` | Final Report — `run_report_win.sh` + launch viewer |
| `components/InterimReportForm.jsx` | Interim Report — simulation folder path input (`messag` appended automatically), calls `/api/report/interim-plots` |
| `components/InterimDetail.jsx` | Main panel rendering plots for a loaded interim report |

---

## Interim vs Final Report

Two distinct report types were implemented, each with its own button in the sidebar:

### Final Report (static, python server)
- Requires a **running job** selected in the table
- Runs `run_report_win.sh` on the server via SSH
- Polls for `Simulation\_HTML\start_server.*` to confirm completion (up to 4 min)
- Launches the static HTML report viewer as a local subprocess
- Always reads from `{job_path}\Simulation\messag`

### Interim Report (dashboard, in-browser)
- **No job needs to be running** — always available in the sidebar
- User provides the **Windows path to the simulation folder** containing the `messag` file; the filename is appended automatically by the backend
- Not limited to `Simulation\messag` — works for any intermediate simulation output folder
- Backend reads the file via the mapped drive and returns Plotly JSON
- Plots render directly in the dashboard without launching any external process
- Selecting a running job closes the interim panel, and vice-versa

---

## Light / Dark mode toggle

A theme toggle was added to the bottom of the sidebar ("☀ Light Mode" / "☾ Dark Mode"). The preference is persisted in `localStorage` so it survives page refreshes.

**Implementation:**
- All colours in `App.css` are defined as CSS variables on `:root` (dark, default). A `[data-theme="light"]` block overrides every variable — backgrounds, text, borders, log viewer colours — so all components re-theme automatically with no per-component changes.
- `App.jsx` reads the saved preference on mount, applies `data-theme` to `<html>`, and writes back to `localStorage` on every toggle.
- The sidebar gradient is unchanged in both modes (brand colour).
- `LogViewer.css` previously used hardcoded hex values for the log background and text; these were replaced with `var(--log-bg)` / `var(--log-color)`, which are defined for both themes.
- Inputs and selects were switched from `var(--bg-dark)` to `var(--bg-card)` so they use the card background in both modes.

---

## Multi-user discussion

A question arose about whether the dashboard could be hosted by one user (User A) and accessed by others (User B) without them needing Node.js.

**Key findings:**

- The React SPA is served over HTTP — any browser on the network can access it. **User B needs no Node.js.**
- `qstat` returns all jobs from all users, so the job list is already user-agnostic.
- **Messag file reads use Windows mapped drives.** FastAPI opens files like `Z:\path\Simulation\messag` on the machine it runs on. User B's files are on their own mapped drives — unreachable from User A's machine.
- **Path conversion uses `getuser()`** (the OS user running FastAPI). User B's jobs would get the wrong Linux path.
- **Report viewer launch** spawns a subprocess on the FastAPI host machine. It must run on the user's own machine — no central hosting can solve this without a client-side component.

**Conclusion:** The mapped drive approach is a good design for per-user deployment. The cleanest solution for a small team is for each user to run their own FastAPI instance. Node.js is only needed once (by whoever builds the frontend). The pre-built `frontend/dist/` can be shared — User B copies it and never needs Node.js.

---

## Deployment package for User B

User B needs only:

```
v3.0\
  backend\          ← all backend files
  frontend\
    dist\           ← pre-built static files only (no src/, no node_modules/)
  runDashboard.cmd
  NOTES.md
```

Packaged as `pbs_job_monitor_v4.0.tar.gz` (12 files, ~1.5 MB, includes built Plotly bundle and `runtimePBSProDB.ses`).

**Steps for User B:**
1. Extract the tarball
2. `pip install -r backend\requirements.txt`
3. Edit `backend\config.yaml` if needed — set `meta.executable` to the local META installation path, or remove the `meta:` section to disable the feature
4. Run `runDashboard.cmd` → opens `http://localhost:8000`

`runDashboard.cmd` uses `%~dp0` (self-relative path) and plain `python` on PATH — no hardcoded user-specific paths. It also checks that Python is on PATH and prints a helpful error if not.

---

## Key efficiency gains over Streamlit

| | Streamlit | FastAPI + React |
|---|---|---|
| Python processes | 1 per browser tab | 1 total |
| Job fetch SSH calls | 1 per tab per refresh | 1 shared, TTL cached |
| Messag file reads | 1 per tab per interaction | 1 shared, TTL cached |
| Plot generation | Every script rerun | Once per request, returned as JSON |
| Log streaming | Polling (full rerun) | SSE push, 3s interval |
| Re-render on interaction | Full page rerun | Only changed React components |
| Finished job plots | Not possible | Interim Report — any messag file |

---

## Post-migration fixes (2026-02-26, session 2)

### Problem 1 — messag file locking corrupting simulations

**Root cause:** On CIFS/Samba mapped drives, Python's `open(messag, "r")` acquires a Windows read-lock that propagates back through the network share to the Linux side. LS-DYNA writes to `messag` sequentially; holding the file open during full content reads (plot requests) or repeated SSE polling (every 3 s) can stall or corrupt those writes.

**Solution: background copier + `messag_react`**

A daemon thread (`threading.Thread`, started in `JobMonitor.__init__`) runs every `messag_copy_interval` seconds (default 30, configurable in `config.yaml`). Each cycle it calls `shutil.copy2(messag → messag_react)` for every job in `_jobs_cache`. `shutil.copy2` opens the source file only for the duration of the OS binary copy (milliseconds), then closes it immediately.

All Python reads — `fetch_messag_content()`, `get_log_content()`, and the interim-plots endpoint — now target `messag_react` exclusively. The live `messag` file is never held open for analysis.

**Timing chain (worst-case data age):**
- LS-DYNA writes to `messag`
- Background copier runs → `messag_react` updated (up to 30 s later)
- Backend messag content cache expires (20 s TTL)
- Frontend plot poll fires (every 60 s)
- Total maximum lag: ~90 s (acceptable; live log still streams from fresh `messag_react`)

**Edge cases handled:**
- `messag_react` not yet created (server just started, first 30 s): `fetch_messag_content()` returns `None`; plots endpoint returns HTTP 404 with message "Retry in a few seconds"
- `interim-plots` endpoint: prefers `messag_react`; falls back to `messag` for finished simulations where there is no active writer
- Thread-safety: `_jobs_cache` snapshotted with `list()` at start of each copy cycle to avoid races with TTL refresh

**Files changed:** `backend/job_monitor.py` (added `import shutil`, `import threading`; new methods `get_messag_react_path`, `_copy_all_messag_files`, `_background_copy_loop`, `_start_background_copier`; updated `fetch_messag_content` and `get_log_content`), `backend/main.py` (updated `get_interim_plots`), `backend/config.yaml` (added `messag_copy_interval: 30` under `dashboard:`).

---

### Problem 2 — MIME types for JS/CSS assets not served correctly on Windows

**Root cause:** Starlette's `StaticFiles` resolves MIME types via Python's `mimetypes` module, which on Windows reads from the registry. The registry frequently maps `.js` to `text/plain` (or has no entry), causing browsers to refuse to execute the React bundle.

**Solution:** Three `mimetypes.add_type()` calls at module level in `main.py`, before any route or `StaticFiles` registration:

```python
import mimetypes
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("text/javascript", ".mjs")
```

**Files changed:** `backend/main.py`.

---

### Problem 3 — Convergence plots not auto-updating while a job is selected

**Root cause:** `ConvergencePlots.jsx` used a single `useEffect` with `[job.JobID, job.Job_Path]` dependencies — it fired once on mount (when a job was selected) and never again. Plots only "updated" because deselect/reselect unmounted and remounted the component, triggering a fresh fetch.

**Solution:** Combined the initial fetch and a `setInterval` (60 000 ms) inside one `useEffect`. A `cancelled` ref prevents stale-state updates after the job changes or the component unmounts. Background refreshes do not reset `loading` or clear existing plots — the charts stay visible while the new request is in flight, then swap silently. A small "Plots updated: HH:MM:SS · auto-refresh every 60 s" line appears above the plots to confirm live data.

```js
// inside useEffect([job.JobID, job.Job_Path])
doFetch(true)                                     // initial — shows spinner
const id = setInterval(() => doFetch(false), 60_000)  // silent background refresh
return () => { cancelled = true; clearInterval(id) }
```

The 60 s interval is intentional: `messag_copy_interval` (30 s) + `cache_timeout` (20 s) = 50 s minimum before genuinely new data is available.

**Files changed:** `frontend/src/components/ConvergencePlots.jsx` (added `useRef`, background poll logic, `lastUpdated` state), `frontend/src/components/ConvergencePlots.css` (added `.plots-updated` style).

---

### Vite build — fixed asset filenames

`vite.config.js` updated with `rollupOptions.output` to produce `assets/index.js` and `assets/index.css` (no content-hash suffix). This keeps tarball asset paths stable across rebuilds so `pbs_job_monitor_v3.0.tar.gz` can be updated in-place without filename changes.

**Files changed:** `frontend/vite.config.js`, `pbs_job_monitor_v3.0.tar.gz` (repacked with rebuilt dist).

---

## Convergence plot improvements (2026-02-27)

Both changes are backend-only — no frontend rebuild required. `ConvergencePlots.jsx` is a dumb renderer that passes whatever Plotly JSON dict the backend returns straight to `react-plotly.js`.

### Change 1 — Convergence Metrics: split into two standalone figures (revised)

**Problem (original):** Three stacked subplots (Displacement / Energy / Residual) were overlapping.
**Attempted fix:** Reduced to 2 subplots with `vertical_spacing=0.22` — still overlapping when 10 steps × legend labels pushed the horizontal legend into the first panel's data area.
**Final fix:** Removed `plot_convergence_metrics` entirely. Replaced with two standalone `go.Figure` instances via a shared `_build_norm_figure()` helper:

- `plot_displacement_norm_metrics()` → backend key `displacement_norm`
- `plot_energy_norm_metrics()` → backend key `energy_norm`

Each chart is `height=420` with a **vertical legend on the right** (`x=1.02`, `orientation="v"`) so legend entries never overlap the plot area regardless of how many steps are shown. The `make_subplots` import was removed as it is no longer used.

The residual data is still **parsed** by `ConvergenceParser` — it is simply not plotted.

`PLOT_LABELS` in `ConvergencePlots.jsx` updated: `convergence_metrics` removed, `displacement_norm` and `energy_norm` added.

**Files changed:** `backend/convergence_plotter.py`, `frontend/src/components/ConvergencePlots.jsx`.
**Frontend rebuild required.**

### Change 2 — Displacement Evolution: rainbow coloring by iteration number within step

**Problem (original):** All markers were the same teal colour.
**Attempted fix:** Coloured by step number — but the user's intent was different. Each timestep runs multiple Newton iterations (1, 2, 3, …, N); at the next timestep the counter resets to 1. The interesting question is: *which iteration within a step* shows a spike, not which step.
**Final fix:** Colour encodes `iteration["iterationNumber"]` — the within-step iteration counter that already resets to 1 for each new timestep. Colorscale is Jet (`cmin=1`, `cmax=max_iter_across_all_steps`):

- Iteration 1 → **blue** (first attempt, usually high displacement norm)
- Middle iterations → cyan → green → yellow → orange (converging)
- Last iteration → **red** (either converged or hit iteration limit)

Two layered traces:
1. Thin neutral line (`rgba(100,130,160,0.25)`, width=1) — continuity guide, no hover.
2. Coloured markers with `customdata=[step_num, iter_num]` — hover shows `Time / Disp Norm / Step N, Iter M`.

Colorbar labelled "Iteration #".

**Files changed:** `backend/convergence_plotter.py` (`plot_displacement_evolution`).

### Build and deployment

Frontend rebuilt with `npm run build` (Vite 5, ~10 s). Output: `frontend/dist/assets/index.js` (4.9 MB, 1.5 MB gzipped) and `index.css`. Tarball `pbs_job_monitor_v3.0.tar.gz` repacked in-place with the same 11 files at 1.5 MB — no path changes, User B deployment procedure unchanged.

---

## Enhancements (2026-03-02)

### Persistent job database

**Problem:** Finished jobs disappeared from the dashboard the moment PBS dequeued them, taking all path/server metadata with them. The background messag copier also stopped copying at that point, so the final simulation state (normal/error termination) was often not captured in `messag_react`.

**Solution:** `JobDatabase` class in `job_monitor.py`, backed by `backend/job_database.json`.

- Every live R/Q/E job is upserted into the DB on each `fetch_jobs()` call.
- When a job vanishes from the live list, `_do_final_messag_copy()` is called immediately (capturing the last messag state), then the job is marked `Status=F` in the DB.
- `GET /api/jobs` now returns live jobs merged with all `Status=F` jobs from the DB.
- Two new endpoints: `DELETE /api/db/jobs/finished` (clear all) and `DELETE /api/db/jobs/{id}` (clear one). Only `F`-status jobs can be deleted.
- Frontend: "Clear All Finished" button in sidebar; "Hide"/"Show" button per active job row (hidden state persisted in `localStorage`); "Show Hidden Jobs (N)" toggle.
- `badge-F` given a distinct neutral grey colour (previously shared the error-red of `badge-E`).

### Simulation_Ret* directory fallback

`get_messag_path()` and `get_messag_react_path()` now check whether `Simulation/` exists; if not, they fall back to a single `Simulation_Ret*/` directory in the job's Windows path.

### messag fallback chain (all read endpoints)

All plot, log, and optimal-timestep endpoints now try three sources in order: (1) `messag_react` via TTL cache, (2) direct `messag_react` file read, (3) live `messag`. This ensures finished jobs work even when `messag_react` was never created (e.g. backend was not running when the job ran).

A `_read_messag_content()` helper in `main.py` centralises this logic.

### Job submission delay

`SubmitJobForm.jsx` now shows a 5-second countdown after a successful `qsub` response ("Waiting for PBS to register job... (5s)"). The dialog auto-closes after the countdown, triggering a force-refresh. A "Refresh Now" button skips the wait. This prevents the job list from refreshing before PBS has registered the new job.

### Optimal timestep export

At the end of a simulation all convergence history is available. A DTMAX load curve derived from this history lets a subsequent run skip the timestep reductions that are already known to fail.

**Algorithm** (`compute_optimal_timestep()` in `convergence_plotter.py`):
1. Filter to converged steps only.
2. For each: `start_time = targetTime − stepSize`.
3. Emit `(start_time, dt)` only at *change-points* — where `dt` differs from the previous converged step. Always emit the first entry.

**Endpoints:**
- `GET /api/jobs/{id}/optimal-timestep` — for PBS-tracked finished jobs.
- `GET /api/report/interim-optimal-timestep?folder_path=` — for arbitrary folders (mirrors `interim-plots`, same `messag_react → messag` fallback).

**Frontend:**
- `JobDetail` gains an "Optimal Timestep" tab for `Status=F` jobs only (between "Convergence Plots" and "Live Log").
- `InterimDetail` gains the same tab via tabs (was previously a flat panel).
- `OptimalTimestep.jsx`: Plotly step-function chart (hv line shape, teal), info bar (change-point count, min/max dt), and "Export .dat" button.
- Export is browser-side only (no server round-trip). Format: 2-column ASCII, `numpy fmt=["%20.6f", "%19.6f"]`, with a 2-line comment header.

### Collapsible sidebar

A "‹" button in the sidebar header collapses the sidebar to `width: 0` with a 0.25 s CSS transition. A fixed "☰" button (top-left, z-index 50) re-opens it. `sidebarOpen` state lives in `App.jsx`.

### Build and deployment

Frontend rebuilt (`npm run build`, Vite 5, 9.97 s). Tarball `pbs_job_monitor_v3.0.tar.gz` repacked in-place, same 11 files, 1.5 MB.

---

## Light-mode plot theme fix (2026-03-02)

### Problem

All Plotly charts rendered with a dark grey plot background and near-white text regardless of the active theme. In light mode this produced:
- An opaque dark-blue-grey data area (`rgba(15,25,35,0.6)`) inside an otherwise white card.
- Invisible axis tick labels and titles (light `#e0e8ef` text against the dark background).
- Invisible chart titles (same light font colour).

**Root cause:** `ConvergencePlots.jsx` unconditionally applied a hardcoded `DARK_LAYOUT` object (via `mergeDarkLayout()`) to every figure returned by the backend, with no awareness of the current theme. `OptimalTimestep.jsx` had the same issue with its own inline `plotLayout`.

### Solution

**`ConvergencePlots.jsx`:**

- Added `LIGHT_LAYOUT` constant alongside `DARK_LAYOUT`:
  - `plot_bgcolor: rgba(248,250,252,0.85)` — near-white data area
  - `font.color: #1a2733` — dark text for all labels and titles
  - `xaxis/yaxis: gridcolor: #d0d8e0, zerolinecolor: #aab4bc, tickfont.color: #1a2733`
- Added `useTheme()` hook — reads `data-theme` from `<html>` on mount; a `MutationObserver` watches for attribute changes and updates the state immediately when the user toggles the theme.
- Renamed `mergeDarkLayout` → `mergeLayout(fig, theme)` — selects `LIGHT_LAYOUT` or `DARK_LAYOUT` based on current theme. Axis merge order: backend values (`fig.layout.xaxis`) first, then theme overrides — so `type: "log"`, axis titles, and other backend-set properties are preserved while grid/tick colours are replaced by the theme.
- `PlotPanel` calls `useTheme()` internally, so it works correctly whether rendered from `JobDetail` or `InterimDetail`.

**`OptimalTimestep.jsx`:**

- Same `useTheme()` hook added (self-contained copy — no cross-component import needed).
- `plotLayout` computed from `isDark` flag — all colour values (`plot_bgcolor`, `font.color`, `xaxis.color/gridcolor/tickfont`, `yaxis.color/gridcolor/tickfont`) switch between dark and light equivalents.

**Files changed:** `frontend/src/components/ConvergencePlots.jsx`, `frontend/src/components/OptimalTimestep.jsx`.

### Build and deployment

Frontend rebuilt (`npm run build`, Vite 5, 9.94 s). Tarball `pbs_job_monitor_v3.0.tar.gz` repacked in-place, same 11 files.

---

## META CAE Systems viewer integration (2026-03-04)

### Overview

META results viewer support added as an optional, gated feature. The feature is entirely disabled (no UI, no background threads) when `meta.executable` is absent from `config.yaml`.

### Backend (`backend/`)

**`config.yaml`** — new `meta:` section:
```yaml
meta:
  executable: 'C:\...\meta_post64.bat'
  d3plot_poll_interval_minutes: 10
  metadb_poll_interval_seconds: 30
  metadb_poll_timeout_minutes: 60
```

**`job_monitor.py`** — additions:

- `JobDatabase` extended with three new fields per job entry: `meta_status` (`idle | generating | ready | error`), `meta_error`, `meta_generate_on_finish`. New methods: `get()`, `set_meta_status()`, `set_meta_generate_on_finish()`. `upsert()` preserves existing `meta_*` fields on refresh.
- `JobMonitor._setup_from_config()` loads META config; initialises in-memory state: `_meta_exe`, `_auto_watch`, `_meta_batch_running`, `_meta_batch_start`, `_d3plot_counts`, `_d3plot_last_checked`, `_meta_lock`, `_pending_meta_on_finish`.
- New methods: `get_sim_dir()`, `get_d3plot_count()`, `launch_meta_batch()`, `launch_meta_viewer()`, `get_meta_status()`, `_clear_meta_state()`, `_meta_watcher_loop()`, `_start_meta_watcher()`.
- `fetch_jobs()` — transfers submit-time pending flags to DB, and on R→F transition auto-launches batch if `meta_generate_on_finish` is set.
- Background `meta-watcher` daemon thread (started alongside messag copier when META configured): every 30 s checks metadb completion for batch-running jobs and d3plot count for auto-watched jobs.

**`runtimePBSProDB.ses`** — META session file (new, copied from `Feature_META/temp.ses`). Copied into the simulation directory before each batch run; output written to `runtimePBSPro.metadb` in the same directory.

**`main.py`** — additions:
- 4 new Pydantic models for META request bodies.
- `GET /api/config` now includes `meta_configured: bool`.
- `POST /api/jobs/submit` accepts optional `meta_generate_on_finish: bool`; stores path in `_pending_meta_on_finish` if set.
- Both delete endpoints (`/api/db/jobs/finished` and `/api/db/jobs/{id}`) call `_clear_meta_state()` after DB deletion.
- 5 new endpoints: `GET .../meta/status`, `POST .../meta/generate`, `POST .../meta/launch-viewer`, `POST .../meta/auto-watch`, `PATCH .../meta/settings`.

### Frontend (`frontend/`)

**`api.js`** — 5 new functions: `fetchMetaStatus`, `triggerMetaGenerate`, `launchMetaViewer`, `setMetaAutoWatch`, `setMetaGenerateOnFinish`. `submitJob` signature updated to accept `metaGenerateOnFinish` parameter.

**`JobDetail.jsx`** — tab sets updated:
- Q: `[Convergence Plots, Live Log]`
- R / E: `[Convergence Plots, Live Log, META Viewer]`
- F: `[Convergence Plots, Optimal Timestep, Live Log, META Viewer]`

**`MetaViewer.jsx`** (new) — polls `fetchMetaStatus` every 10 s. Shows:
- Status badge (idle / generating + spinner / ready ✓ / error ✗)
- Generate / Re-generate button (guarded by state)
- Auto-watch toggle (R jobs only)
- Generate-on-finish toggle (R / E jobs)
- When ready: "Launch on server" button + copy-able viewer command text input + clipboard copy button
- Static hint for local launch (requires META + mapped drives on client machine)

**`SubmitJobForm.jsx`** — fetches `meta_configured` on mount; renders "Generate META DB when simulation completes" checkbox when configured.

**`JobDetail.css`** — META viewer CSS added: `.meta-viewer`, `.meta-status-row`, `.meta-controls`, `.meta-toggle`, `.meta-launch-section`, `.meta-cmd-row`, `.meta-cmd-input`, `.meta-hint`, `.meta-not-configured`, `.meta-error-detail`, `.badge-done`, `.badge-error`, `.spinner-inline`.

### File naming

The session file and generated database use project-specific names to avoid confusion with any other temp files in the simulation directory:

| File | Location |
|------|----------|
| `runtimePBSProDB.ses` | `backend/` (source); copied to `{sim_dir}\` before each run |
| `runtimePBSPro.metadb` | `{sim_dir}\` (generated by META batch) |

### Build and deployment

Frontend rebuilt (`npm run build`, Vite 5, 9.88 s). New tarball `pbs_job_monitor_v4.0.tar.gz` — same 11 files as v3.0 plus `./backend/runtimePBSProDB.ses` (12 files total, ~1.5 MB).
