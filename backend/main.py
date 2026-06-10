"""
FastAPI backend for PBS Job Monitor

Serves:
- REST API endpoints for job management
- SSE endpoint for live log streaming
- Static React SPA (production build)

One uvicorn process serves all browser tabs — shared messag cache,
shared job list cache — no per-session overhead.

Endpoints that touch SSH or the mapped network drives are plain `def`
functions so FastAPI runs them in its threadpool: a slow network-drive stat
or SSH call must never stall the event loop (and with it every other browser
tab, including the SSE log streams).
"""

import asyncio
import glob as _glob
import json
import logging
import mimetypes
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ensure correct MIME types for the React SPA assets.
# On Windows the registry can return wrong or missing values for these.
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("text/javascript", ".mjs")

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from binout_utils import build_binout_index, read_binout_series
from convergence_plotter import parse_and_plot_json, compute_optimal_timestep
from job_monitor import JobMonitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("pbs_monitor.api")

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="PBS Job Monitor API",
    description="FastAPI backend for LS-DYNA simulation monitoring via PBS Pro",
    version="2.0.0",
)

# Allow Vite dev server (port 5173) during development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Shared monitor instance (one per process — shared across all clients)
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).parent / "config.yaml"

_monitor: Optional[JobMonitor] = None


def get_monitor() -> JobMonitor:
    global _monitor
    if _monitor is None:
        _monitor = JobMonitor(config_path=str(CONFIG_PATH))
    return _monitor


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class KillRequest(BaseModel):
    server: str
    job_id: str
    job_path: str


class DeleteDirectoryRequest(BaseModel):
    server: str
    job_id: str
    job_path: str


class SubmitRequest(BaseModel):
    windows_path: str
    script_name: str = "qsubrunfhgfs.sh"
    meta_generate_on_finish: bool = False


class ReportRequest(BaseModel):
    windows_path: str


class LaunchViewerRequest(BaseModel):
    windows_path: str


class MetaGenerateRequest(BaseModel):
    server: str
    job_path: str


class MetaLaunchViewerRequest(BaseModel):
    server: str
    job_path: str


class MetaAutoWatchRequest(BaseModel):
    enabled: bool
    server: str
    job_path: str


class MetaSettingsRequest(BaseModel):
    meta_generate_on_finish: bool
    server: str
    job_path: str


# ---------------------------------------------------------------------------
# Helper: build a job OrderedDict from routing info so monitor methods work
# ---------------------------------------------------------------------------


def _make_job_od(server: str, job_id: str, job_path: str) -> OrderedDict:
    return OrderedDict(
        [
            ("Server", server),
            ("JobID", job_id),
            ("Job_Name", ""),
            ("Job_Path", job_path),
            ("CPUs", "N/A"),
            ("Status", "N/A"),
            ("Owner", "N/A"),
            ("Memory", "N/A"),
        ]
    )


def _read_messag_content(monitor: JobMonitor, job: OrderedDict) -> Optional[str]:
    """
    Read messag content for a job, trying three paths in order:

    1. fetch_messag_content()   — reads messag_react via TTL cache
    2. Direct messag_react read — bypasses cache (cold start or cache miss)
    3. Live messag file         — safe for finished jobs; messag_react may
                                  never have been created if the backend
                                  was not running when the job ran.
    """
    content = monitor.fetch_messag_content(job)
    if content is not None:
        return content

    react_path = monitor.get_messag_react_path(job)
    if react_path and os.path.isfile(react_path):
        try:
            with open(react_path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
        except Exception:
            pass

    messag_path = monitor.get_messag_path(job)
    if messag_path and os.path.isfile(messag_path):
        try:
            with open(messag_path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
        except Exception:
            pass

    return None


# ---------------------------------------------------------------------------
# Parse-result cache — mtime-keyed to avoid re-parsing unchanged messag files
# ---------------------------------------------------------------------------

# key  : absolute path to messag_react (one entry per monitored file)
# value: (mtime_float, (summary_dict, plots_json_dict))
# Capped at _PLOT_CACHE_MAX entries; oldest entry evicted on overflow.
_PLOT_CACHE_MAX = 10
_plot_cache: Dict[str, Tuple[float, Any]] = {}
_plot_cache_order: List[str] = []  # insertion-order tracker for FIFO eviction


def _best_messag_react_path(monitor: JobMonitor, job: OrderedDict) -> Optional[str]:
    """
    Return the absolute path to messag_react for a job, or None if it does not
    exist yet.  Never falls back to the live messag file — callers that need
    that fallback (e.g. interim-plots) handle it themselves.
    """
    react_path = monitor.get_messag_react_path(job)
    if react_path and os.path.isfile(react_path):
        return react_path
    return None


def _parse_plots_cached(file_path: str) -> Tuple[dict, dict]:
    """
    Run parse_and_plot_json on file_path and cache the result keyed by mtime.

    Cache hit  : returned immediately when mtime is unchanged (no file read).
    Cache miss : file is read fresh, parsed, result stored under the new mtime.

    Always operates on the path supplied by the caller — it is the caller's
    responsibility to pass messag_react (not the live messag file).

    Returns (summary_dict, plots_json_dict).  Raises on read / parse error.
    """
    mtime = os.path.getmtime(file_path)
    cached = _plot_cache.get(file_path)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    with open(file_path, "r", encoding="utf-8", errors="ignore") as fh:
        content = fh.read()

    result = parse_and_plot_json(content)

    if file_path not in _plot_cache:
        # Evict oldest entry if at capacity
        if len(_plot_cache) >= _PLOT_CACHE_MAX:
            oldest = _plot_cache_order.pop(0)
            _plot_cache.pop(oldest, None)
        _plot_cache_order.append(file_path)

    _plot_cache[file_path] = (mtime, result)
    return result


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------


@app.get("/api/jobs", response_model=List[Dict[str, Any]])
def get_jobs(force_refresh: bool = Query(False)):
    """
    Return the current job list.

    Uses shared TTL cache so multiple browser tabs don't trigger duplicate
    SSH fetches. Pass ?force_refresh=true to bypass the cache.
    """
    monitor = get_monitor()
    try:
        jobs = monitor.get_jobs_cached(force=force_refresh)

        # Merge live jobs with finished jobs from the persistent database.
        # Finished jobs retain all info (Server, Job_Path, etc.) even after
        # they disappear from the PBS queue.
        live_ids = {j["JobID"] for j in jobs}
        finished = [j for j in monitor.job_db.get_finished() if j["JobID"] not in live_ids]

        return [dict(j) for j in jobs] + finished
    except Exception as exc:
        # Return empty list rather than crashing — servers may be unreachable
        logger.error("Error fetching jobs: %s", exc)
        return []


@app.get("/api/config")
def get_config():
    """Return server list and drive mapping from config."""
    monitor = get_monitor()
    servers = [{"hostname": s["hostname"], "name": s["name"]} for s in monitor.servers]
    return {
        "servers": servers,
        "drive_mapping": monitor.get_drive_mapping(),
        "cache_timeout": monitor.cache_timeout,
        "meta_configured": monitor.meta is not None,
    }


@app.post("/api/jobs/{job_id}/kill")
def kill_job(job_id: str, body: KillRequest):
    """
    Send qdel to kill a job, then poll until job_log appears or timeout.

    Runs in the threadpool, so the long poll does not block other requests.
    """
    monitor = get_monitor()
    job = _make_job_od(body.server, body.job_id, body.job_path)

    success, msg = monitor.kill_job(job)
    if not success:
        raise HTTPException(status_code=400, detail=msg)

    terminated, term_msg = monitor.wait_for_job_termination(job)
    return {"terminated": terminated, "message": term_msg, "kill_message": msg}


@app.delete("/api/jobs/{job_id}/directory")
def delete_directory(job_id: str, body: DeleteDirectoryRequest):
    """Delete Simulation directory and job log files via SSH."""
    monitor = get_monitor()
    job = _make_job_od(body.server, body.job_id, body.job_path)

    success, msg = monitor.delete_simulation_directory(job)
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return {"success": True, "message": msg}


@app.post("/api/jobs/submit")
def submit_job(body: SubmitRequest):
    """Submit a PBS job via qsub."""
    monitor = get_monitor()
    success, msg = monitor.submit_job(body.windows_path, body.script_name)
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    # If caller requested META DB generation on finish, store the pending flag
    if body.meta_generate_on_finish and monitor.meta:
        monitor.meta.set_pending_for_path(body.windows_path)
    return {"success": True, "message": msg}


@app.get("/api/jobs/{job_id}/plots")
def get_plots(
    job_id: str,
    server: str = Query(...),
    job_path: str = Query(...),
):
    """
    Parse the job's messag file and return all Plotly figures as JSON dicts.

    The frontend renders these with react-plotly.js without any server-side
    figure objects kept in memory between requests.
    """
    monitor = get_monitor()
    job = _make_job_od(server, job_id, job_path)

    file_path = _best_messag_react_path(monitor, job)
    if file_path is None:
        raise HTTPException(
            status_code=404,
            detail="messag_react not found. If the job just started, retry in a few seconds.",
        )

    try:
        summary, plots_json = _parse_plots_cached(file_path)
        return {"summary": summary, "plots": plots_json}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Plot generation failed: {exc}")


@app.get("/api/report/interim-plots")
def get_interim_plots(folder_path: str = Query(...)):
    """
    Parse a messag file inside the given folder and return all Plotly figures as JSON dicts.

    Used for Interim (Dashboard) Reports on finished/intermediate simulations.
    The caller provides the Windows path to the simulation folder; 'messag' /
    'messag_react' is appended automatically.

    Prefers messag_react (the background-maintained safe copy) to avoid
    holding the live LS-DYNA output file open.  Falls back to messag for
    finished simulations where messag_react has not been created yet.
    """
    react_path = os.path.join(folder_path, "messag_react")
    messag_path = os.path.join(folder_path, "messag")

    if os.path.isfile(react_path):
        read_path = react_path
    elif os.path.isfile(messag_path):
        read_path = messag_path
    else:
        raise HTTPException(
            status_code=404,
            detail=f"messag file not found in: {folder_path}"
        )
    try:
        summary, plots_json = _parse_plots_cached(read_path)
        return {"summary": summary, "plots": plots_json}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Plot generation failed: {exc}")


@app.get("/api/report/interim-optimal-timestep")
def get_interim_optimal_timestep(folder_path: str = Query(...)):
    """
    Compute optimal DTMAX load-curve points from an arbitrary simulation folder.

    Mirrors /api/report/interim-plots: tries messag_react first, falls back
    to the live messag file so this works for any folder regardless of whether
    the background copier has ever touched it.
    """
    react_path = os.path.join(folder_path, "messag_react")
    messag_path = os.path.join(folder_path, "messag")

    if os.path.isfile(react_path):
        read_path = react_path
    elif os.path.isfile(messag_path):
        read_path = messag_path
    else:
        raise HTTPException(
            status_code=404,
            detail=f"messag file not found in: {folder_path}",
        )

    try:
        with open(read_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        points = compute_optimal_timestep(content)
        return {"points": [{"time": t, "dt": dt} for t, dt in points]}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Timestep computation failed: {exc}")


@app.get("/api/jobs/{job_id}/optimal-timestep")
def get_optimal_timestep(
    job_id: str,
    server: str = Query(...),
    job_path: str = Query(...),
):
    """
    Compute the optimal DTMAX load-curve points from the job's messag file.

    Returns only change-points (entries where dt differs from the previous
    converged step).  The frontend uses these to draw a step-function chart
    and to generate the downloadable ASCII table.
    """
    monitor = get_monitor()
    job = _make_job_od(server, job_id, job_path)

    content = _read_messag_content(monitor, job)
    if content is None:
        raise HTTPException(
            status_code=404,
            detail="messag / messag_react not found for this job.",
        )

    try:
        points = compute_optimal_timestep(content)
        return {"points": [{"time": t, "dt": dt} for t, dt in points]}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Timestep computation failed: {exc}")


@app.get("/api/jobs/{job_id}/parts")
def get_parts_list(
    job_id: str,
    server: str = Query(...),
    job_path: str = Query(...),
):
    """
    Return part IDs and titles for a job.

    First call: reads d3plot/d3plot01 with lasso-python, writes parts.json
    into the Simulation directory, then returns the data.
    Subsequent calls: reads parts.json directly — lasso is never invoked again.

    Returns: { parts: [ { id: int, title: str } ], cached: bool }
    """
    monitor = get_monitor()
    job = _make_job_od(server, job_id, job_path)

    sim_dir = monitor.get_sim_dir(job)
    if not sim_dir:
        raise HTTPException(status_code=404, detail="Simulation directory not found.")

    parts_json_path = os.path.join(sim_dir, "parts.json")

    # Fast path — return cached JSON if it already exists
    if os.path.isfile(parts_json_path):
        try:
            with open(parts_json_path, "r", encoding="utf-8") as f:
                parts = json.load(f)
            return {"parts": parts, "cached": True}
        except Exception:
            pass  # Corrupted cache — fall through to regenerate

    # Slow path — run lasso-python and write parts.json
    d3plot_path = os.path.join(sim_dir, "d3plot")
    d3plot01_path = os.path.join(sim_dir, "d3plot01")
    if not os.path.isfile(d3plot_path) or not os.path.isfile(d3plot01_path):
        raise HTTPException(
            status_code=404,
            detail="d3plot / d3plot01 not found in Simulation directory.",
        )

    try:
        from lasso.dyna import D3plot, ArrayType  # lazy import — optional dependency

        d3 = D3plot(d3plot_path, state_filter={0})
        part_ids      = d3.arrays.get(ArrayType.part_ids)
        part_titles   = d3.arrays.get(ArrayType.part_titles)
        part_mat_type = d3.arrays.get(ArrayType.part_material_type)  # may be None

        mat_list = part_mat_type.tolist() if part_mat_type is not None else None

        parts = []
        if part_ids is not None and part_titles is not None:
            for i, (pid, raw_title) in enumerate(zip(part_ids.tolist(), part_titles)):
                if isinstance(raw_title, (bytes, bytearray)):
                    title = raw_title.decode("utf-8", errors="replace").strip()
                else:
                    title = str(raw_title).strip()
                mat = mat_list[i] if mat_list is not None else "-"
                parts.append({"id": pid, "title": title, "material_type": mat})

        with open(parts_json_path, "w", encoding="utf-8") as f:
            json.dump(parts, f)

        return {"parts": parts, "cached": False}
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="lasso-python is not installed in the backend environment.",
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read d3plot: {exc}")


@app.get("/api/jobs/{job_id}/binout/entries")
def get_binout_entries(
    job_id: str,
    server: str = Query(...),
    job_path: str = Query(...),
):
    """
    Discover plottable variables in the job's binout file(s).

    First call: opens binout*, probes each entry/variable, writes _binout_index.json.
    Subsequent calls: reads _binout_index.json directly (cached: true).
    Note: underscore prefix keeps it out of the binout* glob so lasso never tries to open it.

    Returns: { entries: [{name, variables: [{name, per_entity}], ids: [...] | null}],
               found: bool, cached: bool }
    """
    monitor = get_monitor()
    job = _make_job_od(server, job_id, job_path)

    sim_dir = monitor.get_sim_dir(job)
    if not sim_dir:
        raise HTTPException(status_code=404, detail="Simulation directory not found.")

    index_path = os.path.join(sim_dir, "_binout_index.json")

    # Fast path — return cached index
    if os.path.isfile(index_path):
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["cached"] = True
            return data
        except Exception:
            pass  # Corrupted cache — fall through to regenerate

    glob_pattern = os.path.join(sim_dir, "binout*")
    if not _glob.glob(glob_pattern):
        return {"entries": [], "found": False, "cached": False}

    try:
        entries = build_binout_index(glob_pattern)
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="lasso-python is not installed in the backend environment.",
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read binout: {exc}")

    result = {"entries": entries, "found": True, "cached": False}

    try:
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(result, f)
    except Exception:
        pass

    return result


@app.get("/api/jobs/{job_id}/binout/data")
def get_binout_data(
    job_id: str,
    server: str = Query(...),
    job_path: str = Query(...),
    entry: str = Query(...),
    variable: str = Query(...),
    ids: Optional[str] = Query(None),  # comma-separated ID strings; None = all
):
    """
    Return time-series data for one variable within a binout entry.

    For per-entity variables the optional `ids` param filters which entity
    columns are returned (max 10). For scalar variables `ids` is ignored.

    Returns: { time: [...], series: [{id: str, values: [...]}] }
    """
    monitor = get_monitor()
    job = _make_job_od(server, job_id, job_path)

    sim_dir = monitor.get_sim_dir(job)
    if not sim_dir:
        raise HTTPException(status_code=404, detail="Simulation directory not found.")

    glob_pattern = os.path.join(sim_dir, "binout*")
    requested_ids = [x.strip() for x in ids.split(",")] if ids else None

    try:
        return read_binout_series(glob_pattern, entry, variable, requested_ids)
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="lasso-python is not installed in the backend environment.",
        )
    except OSError:
        raise HTTPException(status_code=404, detail="No binout files found in Simulation directory.")
    except Exception as exc:
        import traceback as _tb
        raise HTTPException(status_code=500, detail=f"Failed to read binout data: {exc}\n{_tb.format_exc()}")


@app.get("/api/jobs/{job_id}/log/stream")
async def stream_log(
    job_id: str,
    server: str = Query(...),
    job_path: str = Query(...),
    lines: int = Query(100, ge=10, le=2000),
):
    """
    SSE endpoint: streams the last N lines of the messag file every 3 seconds.

    The browser's EventSource keeps the connection open; each message carries
    the full tail so the frontend just replaces the displayed content.
    """
    monitor = get_monitor()
    job = _make_job_od(server, job_id, job_path)

    async def generate():
        while True:
            # The tail read hits the mapped network drive — run it in a worker
            # thread so a slow share never stalls the event loop.
            content, size = await asyncio.to_thread(monitor.get_log_content, job, lines)
            payload = json.dumps(
                {
                    "content": content or "",
                    "size": size,
                    "timestamp": time.time(),
                }
            )
            yield f"data: {payload}\n\n"
            await asyncio.sleep(3)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Job database management
# ---------------------------------------------------------------------------
# NOTE: the literal route /api/db/jobs/finished MUST be defined before the
# parameterised /api/db/jobs/{job_id} so FastAPI matches it correctly.


@app.delete("/api/db/jobs/finished")
def delete_all_finished_jobs():
    """Remove all finished (Status=F) jobs from the persistent database."""
    monitor = get_monitor()
    finished_ids = [j["JobID"] for j in monitor.job_db.get_finished()]
    count = monitor.job_db.delete_all_finished()
    monitor.clear_meta_state(finished_ids)
    return {"success": True, "message": f"Removed {count} finished job(s) from database"}


@app.delete("/api/db/jobs/{job_id}")
def delete_job_record(job_id: str):
    """Remove a single finished job from the persistent database."""
    monitor = get_monitor()
    deleted = monitor.job_db.delete(job_id)
    if not deleted:
        raise HTTPException(
            status_code=400,
            detail=f"Job {job_id} not found or not in finished state",
        )
    monitor.clear_meta_state([job_id])
    return {"success": True, "message": f"Job {job_id} removed from database"}


# ---------------------------------------------------------------------------
# META CAE Systems viewer endpoints
# ---------------------------------------------------------------------------


@app.get("/api/jobs/{job_id}/meta/status")
def get_meta_status(job_id: str):
    """Return META viewer status for a job."""
    monitor = get_monitor()
    return monitor.get_meta_status(job_id)


@app.post("/api/jobs/{job_id}/meta/generate")
def meta_generate(job_id: str, body: MetaGenerateRequest):
    """Trigger META batch generation for a job."""
    monitor = get_monitor()
    if not monitor.meta:
        raise HTTPException(status_code=400, detail="META not configured")

    status = monitor.get_meta_status(job_id)
    if status["batch_running"]:
        raise HTTPException(status_code=409, detail="META batch already running for this job")

    job = _make_job_od(body.server, job_id, body.job_path)
    sim_dir = monitor.get_sim_dir(job)
    if not sim_dir:
        raise HTTPException(status_code=404, detail="Simulation directory not found")

    monitor.meta.launch_batch(job_id, sim_dir)
    return {"success": True, "message": "META batch generation started"}


@app.post("/api/jobs/{job_id}/meta/launch-viewer")
def meta_launch_viewer(job_id: str, body: MetaLaunchViewerRequest):
    """Launch META viewer on the server for a ready metadb."""
    monitor = get_monitor()
    if not monitor.meta:
        raise HTTPException(status_code=400, detail="META not configured")

    status = monitor.get_meta_status(job_id)
    if status["meta_status"] != "ready":
        raise HTTPException(status_code=400, detail="META DB not ready yet")

    job = _make_job_od(body.server, job_id, body.job_path)
    sim_dir = monitor.get_sim_dir(job)
    if not sim_dir:
        raise HTTPException(status_code=404, detail="Simulation directory not found")

    ok, cmd = monitor.meta.launch_viewer(sim_dir)
    if not ok:
        raise HTTPException(status_code=500, detail=cmd)
    return {"success": True, "cmd": cmd}


@app.post("/api/jobs/{job_id}/meta/auto-watch")
def meta_auto_watch(job_id: str, body: MetaAutoWatchRequest):
    """Enable or disable auto-watch (d3plot polling) for a job."""
    monitor = get_monitor()
    if not monitor.meta:
        raise HTTPException(status_code=400, detail="META not configured")

    monitor.meta.set_auto_watch(job_id, body.enabled)
    return {"success": True, "auto_watch": body.enabled}


@app.patch("/api/jobs/{job_id}/meta/settings")
def meta_settings(job_id: str, body: MetaSettingsRequest):
    """Update meta_generate_on_finish flag for a job."""
    monitor = get_monitor()
    monitor.job_db.set_meta_generate_on_finish(job_id, body.meta_generate_on_finish)
    return {"success": True, "meta_generate_on_finish": body.meta_generate_on_finish}


@app.post("/api/report/generate")
def generate_report(body: ReportRequest):
    """
    Start report generation on the server, then poll until complete or timeout.

    Runs in the threadpool, so the long poll does not block other requests.
    """
    monitor = get_monitor()

    success, msg = monitor.generate_report(body.windows_path)
    if not success:
        raise HTTPException(status_code=400, detail=msg)

    complete, comp_msg = monitor.wait_for_report_completion(body.windows_path)
    return {"complete": complete, "message": comp_msg, "start_message": msg}


@app.get("/api/report/status")
def report_status(windows_path: str = Query(...)):
    """Check if start_server.* exists (report ready)."""
    monitor = get_monitor()
    ready = monitor.is_report_complete(windows_path)
    return {"ready": ready, "windows_path": windows_path}


@app.post("/api/report/launch")
def launch_report(body: LaunchViewerRequest):
    """Launch the HTML report viewer subprocess."""
    monitor = get_monitor()
    success, msg = monitor.launch_report_viewer(body.windows_path)
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return {"success": True, "message": msg}


# ---------------------------------------------------------------------------
# Serve built React SPA (must be registered LAST so API routes take priority)
# ---------------------------------------------------------------------------

_FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"

if _FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="static")
else:
    @app.get("/")
    async def root():
        return {
            "message": "PBS Job Monitor API",
            "docs": "/docs",
            "note": "Frontend not built yet. Run: cd frontend && npm install && npm run build",
        }
