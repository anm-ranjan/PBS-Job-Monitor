"""
Lightweight HTML status page writer.

Optional feature gated on `status_page.output_path` in config.yaml. A daemon
thread writes a fully self-contained dark-themed HTML job-status page (no
JS/CDN) on a randomised schedule during active hours.
"""

import logging
import os
import random
import threading
import time
from collections import OrderedDict
from datetime import datetime

logger = logging.getLogger("pbs_monitor.status_page")


def parse_step_size(content: str) -> str:
    """
    Find the last 'BEGIN implicit' marker in the messag content, then
    return the 'current step size' value from the line 3 positions below it.

    Format in messag:
        BEGIN implicit statics  step  N t= X.XXE+XX   <date>
        ====================================================
                        time =  X.XXE+XX
          current step size =  X.XXE+XX          ← 3 lines below
    """
    lines = content.splitlines()
    last_begin_idx = None
    for i, line in enumerate(lines):
        if "BEGIN implicit" in line:
            last_begin_idx = i
    if last_begin_idx is None:
        return "—"
    target_idx = last_begin_idx + 3
    if target_idx >= len(lines):
        return "—"
    target_line = lines[target_idx]
    if "current step size" in target_line and "=" in target_line:
        return target_line.split("=", 1)[-1].strip()
    return "—"


def html_esc(text: str) -> str:
    """Minimal HTML escaping for safe insertion into table cells."""
    if not text:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


class StatusPageWriter:
    """Owns the status-page-writer daemon thread."""

    def __init__(self, monitor, job_db, output_path: str):
        self._monitor = monitor
        self._job_db = job_db
        self._output_path = output_path
        t = threading.Thread(
            target=self._writer_loop,
            name="status-page-writer",
            daemon=True,
        )
        t.start()
        logger.info("Writer started → %s", output_path)

    @staticmethod
    def _in_window() -> bool:
        """
        Return True if the current local time falls inside an active writing window.

        Active windows:
          - 17:00 – 01:00 (evening / overnight)
          - 06:00 – 09:00 (morning)
        """
        h = datetime.now().hour + datetime.now().minute / 60.0
        return (h >= 17.0 or h < 1.0) or (6.0 <= h < 9.0)

    def _writer_loop(self) -> None:
        """
        Writes the status page immediately upon entering an active window, then
        sleeps a random 20–45 minutes before the next write.  When outside all
        windows the thread polls every 60 seconds so it wakes promptly at window
        open without burning CPU.
        """
        while True:
            try:
                if self._in_window():
                    self._write()
                    sleep_sec = random.uniform(20 * 60, 45 * 60)
                    logger.info("Next write in %.1f min", sleep_sec / 60)
                    time.sleep(sleep_sec)
                else:
                    time.sleep(60)
            except Exception as exc:
                logger.error("Unexpected error: %s", exc)
                time.sleep(60)

    def _write(self) -> None:
        """Render the HTML and write it to the configured output path."""
        try:
            html = self._generate_html()
            path = self._output_path
            # Write atomically via a temp file next to the target
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(html)
            os.replace(tmp, path)
            logger.info("Written at %s → %s", datetime.now().strftime("%H:%M:%S"), path)
        except Exception as exc:
            logger.error("Failed to write: %s", exc)

    def _generate_html(self) -> str:
        """
        Build a fully self-contained HTML status page.

        Reads messag_react (or messag) for R/E/F jobs to extract current sim
        time and step count via ConvergenceParser — no SSH required.
        """
        from convergence_plotter import ConvergenceParser

        monitor = self._monitor
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Merge live + finished jobs (live takes priority by JobID)
        # Use get_jobs_cached() so stale cache is refreshed and finished jobs
        # are properly detected and marked F in the DB before we render.
        live_jobs = list(monitor.get_jobs_cached())
        finished_jobs = self._job_db.get_finished()
        live_ids = {j.get("JobID") for j in live_jobs}
        all_jobs = live_jobs + [j for j in finished_jobs if j.get("JobID") not in live_ids]

        STATUS_ORDER = {"R": 0, "E": 1, "F": 2}
        all_jobs = [j for j in all_jobs if j.get("Status") in STATUS_ORDER]
        all_jobs.sort(key=lambda j: STATUS_ORDER.get(j.get("Status", "F"), 9))

        rows_html = []
        for job in all_jobs:
            job_id   = job.get("JobID", "N/A")
            job_name = job.get("Job_Name", "N/A")
            status   = job.get("Status", "N/A")
            server   = job.get("Server", "N/A")
            finished_at = job.get("finished_at", "")

            sim_time_str   = "—"
            step_size_str  = "—"
            term_str       = "—"
            notes_str      = ""

            if status in ("R", "E", "F"):
                job_od = OrderedDict(job.items())
                content = None
                react_path = monitor.get_messag_react_path(job_od)
                if react_path and os.path.isfile(react_path):
                    try:
                        with open(react_path, "r", encoding="utf-8", errors="ignore") as f:
                            content = f.read()
                    except Exception:
                        pass
                if content is None:
                    messag_path = monitor.get_messag_path(job_od)
                    if messag_path and os.path.isfile(messag_path):
                        try:
                            with open(messag_path, "r", encoding="utf-8", errors="ignore") as f:
                                content = f.read()
                        except Exception:
                            pass

                if content:
                    try:
                        parser = ConvergenceParser(content)
                        parser.parse()
                        summary = parser.get_summary()
                        sim_t = summary.get("current_sim_time")
                        sim_time_str = f"{sim_t:.4f}" if sim_t is not None else "—"
                        term_str     = summary.get("termination_status") or "—"
                    except Exception:
                        pass
                    step_size_str = parse_step_size(content)

            if status == "F" and finished_at:
                try:
                    fa = datetime.fromisoformat(finished_at)
                    notes_str = f"Finished {fa.strftime('%Y-%m-%d %H:%M')}"
                except Exception:
                    notes_str = finished_at

            badge_class = f"badge-{status}" if status in ("R", "Q", "E", "F") else "badge-unk"
            rows_html.append(f"""
        <tr>
          <td class="job-name">{html_esc(job_name)}</td>
          <td class="mono">{html_esc(job_id)}</td>
          <td><span class="badge {badge_class}">{status}</span></td>
          <td>{html_esc(server)}</td>
          <td class="mono">{sim_time_str}</td>
          <td class="mono">{step_size_str}</td>
          <td>{html_esc(term_str)}</td>
          <td class="notes">{html_esc(notes_str)}</td>
        </tr>""")

        rows = "\n".join(rows_html) if rows_html else (
            '<tr><td colspan="8" class="empty">No running or finished jobs.</td></tr>'
        )

        job_count = len(all_jobs)
        r_count   = sum(1 for j in all_jobs if j.get("Status") == "R")
        f_count   = sum(1 for j in all_jobs if j.get("Status") == "F")

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="300">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PBS Job Status — {now_str}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: #0d1117;
    color: #c9d1d9;
    font-family: 'Segoe UI', system-ui, sans-serif;
    font-size: 14px;
    padding: 24px 32px 48px;
    min-height: 100vh;
  }}
  header {{
    border-bottom: 1px solid #21262d;
    padding-bottom: 14px;
    margin-bottom: 20px;
    display: flex;
    align-items: baseline;
    gap: 20px;
    flex-wrap: wrap;
  }}
  h1 {{
    font-size: 1.35rem;
    font-weight: 600;
    color: #e6edf3;
    letter-spacing: 0.02em;
  }}
  .gen-time {{
    font-size: 0.8rem;
    color: #8b949e;
    font-family: 'Courier New', monospace;
  }}
  .summary-bar {{
    display: flex;
    gap: 18px;
    margin-bottom: 18px;
    flex-wrap: wrap;
  }}
  .stat-pill {{
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 20px;
    padding: 4px 14px;
    font-size: 0.78rem;
    color: #8b949e;
  }}
  .stat-pill span {{ color: #e6edf3; font-weight: 600; }}
  table {{
    width: 100%;
    border-collapse: collapse;
    background: #0d1117;
  }}
  thead tr {{
    border-bottom: 1px solid #30363d;
  }}
  th {{
    text-align: left;
    padding: 8px 12px;
    font-size: 0.72rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: #8b949e;
    white-space: nowrap;
  }}
  td {{
    padding: 9px 12px;
    border-bottom: 1px solid #161b22;
    vertical-align: middle;
  }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: #161b22; }}
  .job-name {{ color: #e6edf3; font-weight: 500; max-width: 260px; word-break: break-word; }}
  .mono {{ font-family: 'Courier New', monospace; font-size: 0.85rem; color: #8b949e; }}
  .notes {{ font-size: 0.8rem; color: #8b949e; }}
  .empty {{ text-align: center; padding: 32px; color: #484f58; }}
  .badge {{
    display: inline-block;
    border-radius: 4px;
    padding: 2px 8px;
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    font-family: 'Courier New', monospace;
  }}
  .badge-R {{ background: #1a3a1a; color: #3fb950; border: 1px solid #238636; }}
  .badge-Q {{ background: #2d2208; color: #d29922; border: 1px solid #9e6a03; }}
  .badge-E {{ background: #3a1a1a; color: #f85149; border: 1px solid #da3633; }}
  .badge-F {{ background: #1c1f24; color: #8b949e; border: 1px solid #30363d; }}
  .badge-unk {{ background: #1c1f24; color: #8b949e; border: 1px solid #30363d; }}
  footer {{
    margin-top: 32px;
    font-size: 0.72rem;
    color: #484f58;
    border-top: 1px solid #161b22;
    padding-top: 12px;
  }}
</style>
</head>
<body>
<header>
  <h1>PBS Job Monitor</h1>
  <span class="gen-time">Generated: {now_str} &nbsp;|&nbsp; Auto-refresh: 5 min</span>
</header>
<div class="summary-bar">
  <div class="stat-pill">Total <span>{job_count}</span></div>
  <div class="stat-pill">Running <span>{r_count}</span></div>
  <div class="stat-pill">Finished <span>{f_count}</span></div>
</div>
<table>
  <thead>
    <tr>
      <th>Job Name</th>
      <th>Job ID</th>
      <th>Status</th>
      <th>Server</th>
      <th>Sim Time</th>
      <th>Step Size (dt)</th>
      <th>Term. Status</th>
      <th>Notes</th>
    </tr>
  </thead>
  <tbody>
{rows}
  </tbody>
</table>
<footer>PBS Job Monitor v2.0 &nbsp;|&nbsp; Updates every 20–45 min during active hours (17:00–01:00, 06:00–09:00)</footer>
</body>
</html>"""
