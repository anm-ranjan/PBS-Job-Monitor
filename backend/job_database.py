"""
Persistent job store backed by a JSON file on disk.

Records every PBS job the monitor has ever seen, from first appearance
through completion. Survives backend restarts. Thread-safe.
"""

import json
import logging
import os
import threading
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger("pbs_monitor.job_db")


class JobDatabase:
    """
    Persistent job store backed by a JSON file on disk.

    Status values:
        R  — running (live)
        Q  — queued (live)
        E  — exiting (live)
        F  — finished (no longer in live server list)
    """

    def __init__(self, db_path: str):
        self._path = db_path
        self._lock = threading.Lock()
        self._data: Dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if os.path.exists(self._path):
            try:
                with open(self._path, "r") as f:
                    self._data = json.load(f)
            except Exception as exc:
                logger.error("Failed to load %s: %s", self._path, exc)
                self._data = {}

    def _save(self) -> None:
        try:
            with open(self._path, "w") as f:
                json.dump(self._data, f, indent=2)
        except Exception as exc:
            logger.error("Failed to save %s: %s", self._path, exc)

    def upsert(self, job: dict) -> None:
        """Add new job or refresh dynamic fields (Status/CPUs/Memory/Job_Name).
        Preserves existing meta_* fields on update."""
        job_id = job.get("JobID", "")
        if not job_id or job_id == "N/A":
            return
        now = datetime.now().isoformat()
        with self._lock:
            self._upsert_unlocked(job_id, job, now)
            self._save()

    def upsert_batch(self, jobs: List[dict]) -> None:
        """Upsert multiple jobs in one lock acquisition with a single _save()."""
        now = datetime.now().isoformat()
        with self._lock:
            for job in jobs:
                job_id = job.get("JobID", "")
                if job_id and job_id != "N/A":
                    self._upsert_unlocked(job_id, job, now)
            self._save()

    def _upsert_unlocked(self, job_id: str, job: dict, now: str) -> None:
        """Core upsert logic — caller must hold self._lock."""
        if job_id not in self._data:
            self._data[job_id] = {
                "JobID": job_id,
                "Server": job.get("Server", "N/A"),
                "Job_Name": job.get("Job_Name", "N/A"),
                "Job_Path": job.get("Job_Path", "N/A"),
                "Owner": job.get("Owner", "N/A"),
                "CPUs": job.get("CPUs", "N/A"),
                "Memory": job.get("Memory", "N/A"),
                "Status": job.get("Status", "N/A"),
                "first_seen": now,
                "last_seen": now,
                "finished_at": None,
                "meta_status": "idle",
                "meta_error": None,
                "meta_generate_on_finish": False,
            }
        else:
            for k in ("Status", "CPUs", "Memory", "Job_Name"):
                val = job.get(k)
                if val and val != "N/A":
                    self._data[job_id][k] = val
            self._data[job_id]["last_seen"] = now
            # A job seen live again was either never finished or was wrongly
            # marked F during a transient server outage — clear the marker.
            if job.get("Status") in ("R", "Q", "E"):
                self._data[job_id]["finished_at"] = None
            self._data[job_id].setdefault("meta_status", "idle")
            self._data[job_id].setdefault("meta_error", None)
            self._data[job_id].setdefault("meta_generate_on_finish", False)

    def get(self, job_id: str) -> Optional[dict]:
        """Return a single job entry or None."""
        with self._lock:
            return self._data.get(job_id)

    def set_meta_status(self, job_id: str, status: str, error: Optional[str] = None) -> None:
        """Update meta_status (and optionally meta_error) for a job."""
        with self._lock:
            if job_id in self._data:
                self._data[job_id]["meta_status"] = status
                self._data[job_id]["meta_error"] = error
                self._save()

    def set_meta_generate_on_finish(self, job_id: str, val: bool) -> None:
        """Set the meta_generate_on_finish flag for a job."""
        with self._lock:
            if job_id in self._data:
                self._data[job_id]["meta_generate_on_finish"] = val
                self._save()

    def mark_finished(self, job_id: str) -> None:
        """Set Status = 'F' and record finished_at timestamp."""
        with self._lock:
            if job_id in self._data:
                self._data[job_id]["Status"] = "F"
                self._data[job_id]["finished_at"] = datetime.now().isoformat()
                self._save()

    def get_all(self) -> List[dict]:
        with self._lock:
            return list(self._data.values())

    def get_finished(self) -> List[dict]:
        with self._lock:
            return [j for j in self._data.values() if j["Status"] == "F"]

    def get_active(self) -> List[dict]:
        """Return jobs with status R, Q, or E (not yet finished)."""
        with self._lock:
            return [j for j in self._data.values() if j["Status"] in ("R", "Q", "E")]

    def delete(self, job_id: str) -> bool:
        """Remove a finished job. Returns True if deleted, False if not found / not finished."""
        with self._lock:
            entry = self._data.get(job_id)
            if entry and entry["Status"] == "F":
                del self._data[job_id]
                self._save()
                return True
            return False

    def delete_all_finished(self) -> int:
        """Remove all finished jobs. Returns count of deleted entries."""
        with self._lock:
            ids = [jid for jid, j in self._data.items() if j["Status"] == "F"]
            for jid in ids:
                del self._data[jid]
            if ids:
                self._save()
            return len(ids)
