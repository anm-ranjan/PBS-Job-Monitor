# PBS Job Monitor

A **FastAPI + React** dashboard for submitting, killing, and monitoring implicit LS-DYNA simulations running on Linux HPC servers via **PBS Pro** job scheduler. Runs on a Windows client and connects to compute servers over SSH.

---

## Features

- **Job table** — live view of all R/Q/E jobs across multiple servers, with sorting and filtering
- **Convergence plots** — parsed from `messag` files; displacement norm, energy norm, and displacement evolution charts
- **Optimal timestep export** — computes converged timestep change-points and exports a `.dat` file (for finished jobs)
- **Live log viewer** — SSE-based streaming tail of the `messag` file, no polling overhead
- **Job submission** — `qsub` form with resource fields (nodes, CPUs, memory, walltime)
- **Job termination** — `qdel` with optional simulation directory deletion
- **Job database** — finished jobs (Status=F) are persisted across server restarts in `job_database.json`
- **META CAE viewer integration** — generate MetaDB files in batch and launch the META post-processor (optional, gated on `config.yaml`)
- **Collapsible sidebar** — themed UI with smooth collapse/expand transition
- **Auto-refresh** — job list refreshes every 20 s; convergence plots refresh every 60 s while a job is selected

## Architecture

```
Windows Client
│
├── backend/          FastAPI app (Python)
│   ├── main.py       All API endpoints + static file serving
│   ├── job_monitor.py  SSH job polling, job database, META integration
│   ├── convergence_plotter.py  messag parser + Plotly figure builder
│   └── que.py        Remote script deployed on each Linux server
│
└── frontend/         React/Vite SPA
    └── src/
        ├── App.jsx
        ├── api.js
        └── components/   JobTable, JobDetail, Sidebar, LogViewer, …
```

One FastAPI process serves all browser tabs. The frontend is built once (`npm run build`) and served as static files by FastAPI — no separate Node server needed in production.

## Requirements

- Python 3.10+ (Windows)
- Node.js 18+ (for frontend build only)
- PBS Pro on the Linux compute servers
- SSH key-based authentication from the Windows client to each server

## Setup

### 1. Configure

```bash
cp backend/config.yaml.example backend/config.yaml
# Edit backend/config.yaml — set server IPs, drive mapping, PBS paths
```

### 2. Build the frontend (once, or after source changes)

```bash
cd frontend
npm install
npm run build
```

### 3. Start the backend

```bash
cd backend
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

Or double-click `runDashboard.cmd` (starts the server and opens the browser automatically).

Open **http://localhost:8000** in a browser.

## Configuration

All site-specific settings live in `backend/config.yaml` (not tracked in git — copy from `config.yaml.example`):

| Key | Description |
|-----|-------------|
| `servers[].hostname` | IP or hostname of each PBS server |
| `drive_mapping` | Windows drive letter → server IP |
| `paths.linux_base_path` | Base path for user home directories on servers |
| `meta.executable` | Path to `meta_post64.bat` — omit section to disable META integration |

## Branding / Logo

Place your organisation logo at `Logo.png` in the project root. This file is intentionally not tracked in git — the repository ships a plain white placeholder. The logo is used by the legacy Streamlit dashboard (`streamlit_dashboard.py`); the React frontend uses a CSS text logo.

## Notes

- `streamlit_dashboard.py` is kept for reference only (the original v1 implementation). It is not used by the FastAPI stack.
- `messag` files are never read directly by the backend. A background thread copies `messag` → `messag_react` every 30 s; all reads target the copy to avoid file-lock conflicts with LS-DYNA.
- Queued jobs (Status=Q) may not report CPU/memory fields — this is normal PBS Pro behaviour.
