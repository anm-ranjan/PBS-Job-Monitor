# PBS Job Monitor v2.0 — Quick Reference

## Architecture
FastAPI (Python) backend + React (Vite) frontend.
One uvicorn process serves all browser tabs — shared job cache, shared messag cache.

## Directory Layout
```
backend/          ← copy this to target system
  main.py         FastAPI app + static file serving
  job_monitor.py  SSH, path conversion, job ops, META integration, shared cache
  convergence_plotter.py  Parse messag → Plotly JSON
  config.yaml     Server IPs, drive letters, PBS paths, META settings
  que.py          Remote script deployed on each Linux server
  requirements.txt
  runtimePBSProDB.ses   META session file (copied to sim dir before batch run)

frontend/         ← build once, then copy dist/ (or copy whole dir and build on target)
  src/            React source
  dist/           Built output — served by FastAPI at /
  package.json
  vite.config.js
```

## First-Time Setup (on target Windows machine)
```cmd
REM 1. Install Python deps
cd U:\05_Scripts\pbs_job_monitor\v3.0\backend
pip install -r requirements.txt

REM 2. Build React frontend (Node.js required)
cd U:\05_Scripts\pbs_job_monitor\v3.0\frontend
npm install
npm run build

REM 3. Start server
cd U:\05_Scripts\pbs_job_monitor\v3.0\backend
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```
Then open http://localhost:8000

## config.yaml — key settings
| Setting | Value |
|---------|-------|
| linux_base_path | `/mnt/fhgfs` |
| remote_script_name | `que.py` |
| Drive X | 10.17.142.200 (IPA-WS-332ABR) |
| Drive Y | 10.17.160.231 (inSilico2) |
| Drive Z | 10.17.160.230 (inSilico3) |
| cache_timeout | 20s |
| messag_copy_interval | 30s (how often messag → messag_react is copied) |

Drive keys in config.yaml are **without** colons: `X`, `Y`, `Z` (not `X:`).

## API Endpoints
| Method | Path | Notes |
|--------|------|-------|
| GET | `/api/jobs` | `?force_refresh=true` bypasses cache; includes finished (F) jobs from DB |
| GET | `/api/config` | server list + drive mapping + `meta_configured` flag |
| POST | `/api/jobs/{id}/kill` | qdel + async poll for job_log |
| DELETE | `/api/jobs/{id}/directory` | rm -rf Simulation/ via SSH |
| POST | `/api/jobs/submit` | qsub via SSH; optional `meta_generate_on_finish` flag |
| GET | `/api/jobs/{id}/plots` | messag → Plotly JSON; falls back messag_react → messag |
| GET | `/api/jobs/{id}/optimal-timestep` | DTMAX change-points from converged steps |
| GET | `/api/jobs/{id}/log/stream` | SSE tail, refreshes every 3s; falls back to messag |
| GET | `/api/jobs/{id}/meta/status` | META DB generation status for a job |
| POST | `/api/jobs/{id}/meta/generate` | trigger META batch run (copies runtimePBSProDB.ses → sim dir) |
| POST | `/api/jobs/{id}/meta/launch-viewer` | launch META viewer for ready runtimePBSPro.metadb |
| POST | `/api/jobs/{id}/meta/auto-watch` | enable/disable d3plot auto-watch for a running job |
| PATCH | `/api/jobs/{id}/meta/settings` | set meta_generate_on_finish flag |
| DELETE | `/api/db/jobs/finished` | remove all finished jobs from persistent DB |
| DELETE | `/api/db/jobs/{id}` | remove one finished job from persistent DB |
| GET | `/api/report/interim-plots` | parse messag from arbitrary folder |
| GET | `/api/report/interim-optimal-timestep` | DTMAX points from arbitrary folder |
| POST | `/api/report/generate` | run_report_win.sh + poll up to 4 min |
| GET | `/api/report/status` | checks for _HTML/start_server.* |
| POST | `/api/report/launch` | launches start_server.cmd/.sh |
| GET | `/docs` | FastAPI auto-docs (Swagger UI) |

## Path Conversion
- Linux: `/mnt/fhgfs/{user}/rest/of/path`
- Windows: `Z:\rest\of\path` (Z mapped to inSilico3)
- Messag files read via Windows mapped drives (not SSH)
- Job termination detected by presence of `job_log` file in job dir

## messag_react — simulation file safety
A background thread copies `messag → messag_react` every `messag_copy_interval` seconds
(default 30 s). Plot/log reads prefer `messag_react` but fall back to `messag` automatically,
so finished jobs (Status=F) always work even if `messag_react` was never created.
When a job disappears from the PBS queue a final copy is triggered immediately.

Convergence plots auto-refresh every 60 s while a job is selected.

## Job Database (`backend/job_database.json`)
Jobs are tracked in a persistent JSON database from first appearance through completion.
- Status=F jobs remain in the dashboard after the PBS job ends
- "Clear All Finished" button removes them; individual jobs can also be cleared
- Running/queued jobs can be hidden per-tab (state saved in browser localStorage)
- `Simulation_Ret*/` directories are supported as fallback when `Simulation/` is absent

## Optimal Timestep Export
Available for Status=F jobs (JobDetail → "Optimal Timestep" tab) and Interim Reports.
Exports a 2-column ASCII DTMAX load curve (numpy fmt `["%20.6f", "%19.6f"]`) containing
only change-points where the converged timestep size differed from the previous step.

## Light / Dark Mode
Theme toggle is in the sidebar footer. Preference persisted in `localStorage`.
All colours are CSS variables — the `[data-theme="light"]` block in `App.css` overrides them globally.
Plotly charts adapt to the active theme via a `useTheme()` hook (reads `data-theme` from `<html>`, watches for changes via `MutationObserver`). Dark mode: dark blue-grey plot area, light text. Light mode: near-white plot area, dark text.

## META CAE Systems Viewer
Feature gated on `meta.executable` in `config.yaml` — remove the key or the whole `meta:` section to disable everywhere (no errors).

| config key | default | meaning |
|---|---|---|
| `meta.executable` | (none) | full path to `meta_post64.bat` |
| `meta.d3plot_poll_interval_minutes` | 10 | how often auto-watch checks for new d3plot files |
| `meta.metadb_poll_interval_seconds` | 30 | how often background thread checks metadb completion |
| `meta.metadb_poll_timeout_minutes` | 60 | give up if metadb not created within this time |

**Session file:** `backend/runtimePBSProDB.ses` is copied into the simulation directory before each batch run. META writes its output to `{sim_dir}\runtimePBSPro.metadb`.

**Job tabs:** Q jobs show [Convergence Plots, Live Log]. R/E jobs add a **META Viewer** tab. F jobs add both **Optimal Timestep** and **META Viewer** tabs.

**Viewer command (for local launch):**
```
"<meta_executable>" -p "<sim_dir>\runtimePBSPro.metadb" -viewer -nolog -noses
```
Paste this in a terminal on any machine with META installed and mapped drives.

## Known Quirks
- `que.py` outputs JSON regardless of `--json` flag
- Queued jobs (Status=Q) may lack CPUs/Memory fields
- `bash -i -c` required for server commands (loads ~/.bashrc for PATH)
- Report polling checks for `Simulation/_HTML/start_server.cmd` or `.sh`
- JS/CSS MIME types explicitly registered in `main.py` — required on Windows where the registry often maps `.js` to `text/plain`
- META: `_meta_batch_running` / `_auto_watch` are in-memory only — reset on backend restart. `meta_status` in `job_database.json` persists across restarts.
