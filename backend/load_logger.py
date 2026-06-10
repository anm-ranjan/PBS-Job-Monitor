"""
Server load logger.

Optional feature gated on `load_logger.output_dir` in config.yaml. A daemon
thread appends one NDJSON snapshot of PBS-only job data to a daily file
(`server_load_YYYY-MM-DD.jsonl`) on a fixed interval. An optional secondary
directory mirrors every line; failures there are non-fatal.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Dict, Optional, Set

logger = logging.getLogger("pbs_monitor.load_logger")


def parse_memory_gb(mem_str: str) -> Optional[float]:
    """
    Convert a PBS memory string to gigabytes.

    PBS reports memory as e.g. '192gb', '4096mb', '1024kb', '512000kb'.
    Returns None if the string cannot be parsed.
    """
    if not mem_str or mem_str == "N/A":
        return None
    s = mem_str.strip().lower()
    try:
        if s.endswith("gb"):
            return float(s[:-2])
        if s.endswith("mb"):
            return float(s[:-2]) / 1024.0
        if s.endswith("kb"):
            return float(s[:-2]) / (1024.0 ** 2)
        if s.endswith("b"):
            return float(s[:-1]) / (1024.0 ** 3)
        # Bare number — assume bytes
        return float(s) / (1024.0 ** 3)
    except ValueError:
        return None


class LoadLogger:
    """Owns the load-logger daemon thread."""

    def __init__(self, monitor, job_db, output_dir: str,
                 secondary_dir: Optional[str], interval_seconds: int):
        self._monitor = monitor
        self._job_db = job_db
        self._output_dir = output_dir
        self._secondary_dir = secondary_dir
        self._interval = interval_seconds
        # In-memory set of finished job IDs whose finish-event has already been
        # logged. Reset on restart (acceptable: a duplicate event will be
        # written, not a missing one).
        self._finished_logged: Set[str] = set()

        t = threading.Thread(
            target=self._logger_loop,
            name="load-logger",
            daemon=True,
        )
        t.start()
        logger.info("Started → %s (every %d min)", output_dir, interval_seconds // 60)

    def _collect_snapshot(self) -> dict:
        """
        Build one complete load snapshot dict from PBS data only.

        Data source is exclusively the live PBS job list (via get_jobs_cached).
        No messag/messag_react files are read, so all jobs from all users are
        treated consistently regardless of drive mapping or file accessibility.
        """
        monitor = self._monitor
        now = datetime.now()
        ts = now.isoformat()

        # ── live jobs (PBS data only) ─────────────────────────────────────
        live_jobs = list(monitor.get_jobs_cached())

        # ── server-level aggregates ───────────────────────────────────────
        server_map: Dict[str, dict] = {}
        for srv in monitor.servers:
            server_map[srv["name"]] = {
                "name": srv["name"],
                "hostname": srv["hostname"],
                "jobs_R": 0, "jobs_Q": 0, "jobs_E": 0,
                "cpus_used": 0,
                "memory_used_gb": 0.0,
            }

        job_records = []
        for job in live_jobs:
            status = job.get("Status", "N/A")
            srv_name = job.get("Server", "")
            cpus_raw = job.get("CPUs", "N/A")
            mem_raw = job.get("Memory", "N/A")
            job_id = job.get("JobID", "N/A")

            try:
                cpus = int(cpus_raw) if cpus_raw not in (None, "N/A") else 0
            except (ValueError, TypeError):
                cpus = 0
            mem_gb = parse_memory_gb(mem_raw)

            # Per-server counters (R and E consume CPUs/memory; Q is just queued)
            if srv_name in server_map:
                agg = server_map[srv_name]
                if status == "R":
                    agg["jobs_R"] += 1
                    agg["cpus_used"] += cpus
                    if mem_gb is not None:
                        agg["memory_used_gb"] += mem_gb
                elif status == "Q":
                    agg["jobs_Q"] += 1
                elif status == "E":
                    agg["jobs_E"] += 1
                    agg["cpus_used"] += cpus
                    if mem_gb is not None:
                        agg["memory_used_gb"] += mem_gb

            # Timing fields from job DB (monitor-side timestamps, not PBS)
            db_entry = self._job_db.get(job_id) or {}
            first_seen = db_entry.get("first_seen")
            last_seen = db_entry.get("last_seen")

            elapsed_s: Optional[float] = None
            if first_seen:
                try:
                    elapsed_s = (now - datetime.fromisoformat(first_seen)).total_seconds()
                except Exception:
                    pass

            # Determine server hostname for this job
            srv_hostname: Optional[str] = None
            for s in monitor.servers:
                if s["name"] == srv_name:
                    srv_hostname = s["hostname"]
                    break

            rec = {
                "job_id": job_id,
                "job_name": job.get("Job_Name", "N/A"),
                "owner": job.get("Owner", "N/A"),
                "server": srv_name,
                "server_hostname": srv_hostname,
                "status": status,
                "cpus": cpus,
                "memory_raw": mem_raw,
                "memory_gb": round(mem_gb, 3) if mem_gb is not None else None,
                "first_seen": first_seen,
                "last_seen": last_seen,
                "walltime_elapsed_s": round(elapsed_s, 1) if elapsed_s is not None else None,
            }
            job_records.append(rec)

        # ── global aggregates ─────────────────────────────────────────────
        total_cpus = sum(s["cpus_used"] for s in server_map.values())
        total_mem = sum(s["memory_used_gb"] for s in server_map.values())
        total_R = sum(s["jobs_R"] for s in server_map.values())
        total_Q = sum(s["jobs_Q"] for s in server_map.values())
        total_E = sum(s["jobs_E"] for s in server_map.values())

        snapshot = {
            "total_jobs_R": total_R,
            "total_jobs_Q": total_Q,
            "total_jobs_E": total_E,
            "total_cpus_used": total_cpus,
            "total_memory_used_gb": round(total_mem, 3),
            "servers": [
                {**s, "memory_used_gb": round(s["memory_used_gb"], 3)}
                for s in server_map.values()
            ],
        }

        # ── finish events (R→F transitions since last poll) ───────────────
        events = []
        for finished_job in self._job_db.get_finished():
            fid = finished_job.get("JobID", "")
            if not fid or fid in self._finished_logged:
                continue
            first_seen = finished_job.get("first_seen")
            finished_at_str = finished_job.get("finished_at")
            duration_s: Optional[float] = None
            if first_seen and finished_at_str:
                try:
                    duration_s = (
                        datetime.fromisoformat(finished_at_str)
                        - datetime.fromisoformat(first_seen)
                    ).total_seconds()
                except Exception:
                    pass

            mem_raw_f = finished_job.get("Memory", "N/A")
            mem_gb_f = parse_memory_gb(mem_raw_f)
            cpus_raw_f = finished_job.get("CPUs", "N/A")
            try:
                cpus_f = int(cpus_raw_f) if cpus_raw_f not in (None, "N/A") else None
            except (ValueError, TypeError):
                cpus_f = None

            events.append({
                "type": "job_finished",
                "job_id": fid,
                "job_name": finished_job.get("Job_Name", "N/A"),
                "owner": finished_job.get("Owner", "N/A"),
                "server": finished_job.get("Server", "N/A"),
                "cpus": cpus_f,
                "memory_raw": mem_raw_f,
                "memory_gb": round(mem_gb_f, 3) if mem_gb_f is not None else None,
                "first_seen": first_seen,
                "finished_at": finished_at_str,
                "sim_duration_s": round(duration_s, 1) if duration_s is not None else None,
            })
            self._finished_logged.add(fid)

        return {
            "timestamp": ts,
            "snapshot": snapshot,
            "jobs": job_records,
            "events": events,
        }

    def _write_snapshot(self) -> None:
        """Collect one snapshot and append it as a single line to today's NDJSON file.
        If a secondary_dir is configured the same line is also appended there."""
        try:
            record = self._collect_snapshot()
        except Exception as exc:
            logger.error("Failed to collect snapshot: %s", exc)
            return

        date_str = datetime.now().strftime("%Y-%m-%d")
        filename = f"server_load_{date_str}.jsonl"
        line = json.dumps(record, default=str) + "\n"

        # Primary path
        filepath = os.path.join(self._output_dir, filename)
        try:
            os.makedirs(self._output_dir, exist_ok=True)
            with open(filepath, "a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception as exc:
            logger.error("Failed to write snapshot to %s: %s", filepath, exc)
            return

        logger.info(
            "Snapshot written → %s (%d jobs, %d finish events)",
            filename, len(record["jobs"]), len(record["events"]),
        )

        # Secondary (backup) path — failure here is non-fatal
        if self._secondary_dir:
            sec_path = os.path.join(self._secondary_dir, filename)
            try:
                os.makedirs(self._secondary_dir, exist_ok=True)
                with open(sec_path, "a", encoding="utf-8") as fh:
                    fh.write(line)
            except Exception as exc:
                logger.warning("Failed to write secondary snapshot to %s: %s", sec_path, exc)

    def _logger_loop(self) -> None:
        """Daemon thread body: write a load snapshot every interval."""
        while True:
            try:
                self._write_snapshot()
            except Exception as exc:
                logger.error("Unexpected error: %s", exc)
            time.sleep(self._interval)
