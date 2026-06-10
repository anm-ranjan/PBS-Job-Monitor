"""
META CAE Systems viewer integration.

Optional feature gated on `meta.executable` in config.yaml. Handles batch
metadb generation, viewer launch, d3plot auto-watch polling, and the
generate-on-finish hook fired by JobMonitor when a job leaves the live list.
"""

import logging
import os
import shutil
import subprocess
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger("pbs_monitor.meta")


class MetaManager:
    """Owns all META state and the meta-watcher daemon thread."""

    def __init__(self, monitor, job_db, meta_cfg: dict):
        self._monitor = monitor
        self._job_db = job_db
        self.executable: str = meta_cfg["executable"]
        self._d3plot_poll_interval: int = int(meta_cfg.get("d3plot_poll_interval_minutes", 10)) * 60
        self._metadb_poll_interval: int = int(meta_cfg.get("metadb_poll_interval_seconds", 30))
        self._metadb_poll_timeout: int = int(meta_cfg.get("metadb_poll_timeout_minutes", 60)) * 60

        # In-memory state (reset on restart, not persisted)
        self._auto_watch: Set[str] = set()
        self._batch_running: Set[str] = set()
        self._batch_start: Dict[str, float] = {}
        self._d3plot_counts: Dict[str, int] = {}
        self._d3plot_last_checked: Dict[str, float] = {}
        self._lock = threading.Lock()

        # Submit-time pending flags (windows_job_path → True), cleared on F transition
        self._pending_on_finish: Dict[str, bool] = {}

        self._start_watcher()

    # ------------------------------------------------------------------
    # Hooks called from JobMonitor.fetch_jobs()
    # ------------------------------------------------------------------

    def set_pending_for_path(self, windows_path: str) -> None:
        """Remember a submit-time generate-on-finish request by job directory."""
        self._pending_on_finish[windows_path] = True

    def sync_live_jobs(self, live_jobs: List[OrderedDict]) -> None:
        """Transfer submit-time pending flags to the job DB once the job appears live."""
        for job in live_jobs:
            win_path = self._monitor.get_windows_path(job)
            if win_path and win_path in self._pending_on_finish:
                self._job_db.set_meta_generate_on_finish(job["JobID"], True)

    def on_job_finished(self, db_job: dict) -> None:
        """R→F hook: auto-launch batch generation if requested for this job."""
        job_od = OrderedDict(db_job.items())
        win_path = self._monitor.get_windows_path(job_od)
        pending = bool(win_path and self._pending_on_finish.pop(win_path, False))
        db_entry = self._job_db.get(db_job["JobID"])
        if db_entry is None:
            return
        if (pending or db_entry.get("meta_generate_on_finish")) \
                and db_entry.get("meta_status", "idle") not in ("generating", "ready"):
            sim_dir = self._monitor.get_sim_dir(job_od)
            if sim_dir:
                self.launch_batch(db_job["JobID"], sim_dir)

    # ------------------------------------------------------------------
    # Batch generation / viewer launch
    # ------------------------------------------------------------------

    def launch_batch(self, job_id: str, sim_dir: str) -> None:
        """
        Copy runtimePBSProDB.ses into sim_dir and launch META in batch mode to
        generate runtimePBSPro.metadb. Marks meta_status='generating' in the DB.
        """
        src_ses = Path(__file__).parent / "runtimePBSProDB.ses"
        dst_ses = Path(sim_dir) / "runtimePBSProDB.ses"
        try:
            shutil.copy2(str(src_ses), str(dst_ses))
        except Exception as exc:
            logger.error("Failed to copy runtimePBSProDB.ses to %s: %s", sim_dir, exc)
            self._job_db.set_meta_status(job_id, "error", f"Failed to copy runtimePBSProDB.ses: {exc}")
            return

        cmd = f'"{self.executable}" -b -s "{dst_ses}" "{sim_dir}" -nolog -noses'
        try:
            subprocess.Popen(cmd, shell=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            logger.error("Failed to launch META batch for job %s: %s", job_id, exc)
            self._job_db.set_meta_status(job_id, "error", f"Failed to launch META: {exc}")
            return

        with self._lock:
            self._batch_running.add(job_id)
            self._batch_start[job_id] = time.time()
        self._job_db.set_meta_status(job_id, "generating")
        logger.info("Launched batch for job %s in %s", job_id, sim_dir)

    def launch_viewer(self, sim_dir: str) -> Tuple[bool, str]:
        """
        Launch META viewer for the runtimePBSPro.metadb in sim_dir.
        Returns (True, cmd_string) so the frontend can show the copy-able command.
        """
        metadb = os.path.join(sim_dir, "runtimePBSPro.metadb")
        cmd = f'"{self.executable}" -p "{metadb}" -viewer -nolog -noses'
        try:
            subprocess.Popen(cmd, shell=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True, cmd
        except Exception as exc:
            return False, str(exc)

    # ------------------------------------------------------------------
    # Status / settings
    # ------------------------------------------------------------------

    def get_status(self, job_id: str) -> dict:
        """Return current META status dict for the given job."""
        db_entry = self._job_db.get(job_id)
        meta_status = db_entry.get("meta_status", "idle") if db_entry else "idle"

        # If DB says "ready", verify the metadb file actually exists on disk.
        # It may have been deleted externally; if so, reset to idle.
        if meta_status == "ready" and db_entry:
            sim_dir = self._monitor.get_sim_dir(OrderedDict(db_entry.items()))
            if not sim_dir or not os.path.isfile(os.path.join(sim_dir, "runtimePBSPro.metadb")):
                self._job_db.set_meta_status(job_id, "idle")
                meta_status = "idle"

        with self._lock:
            return {
                "configured": True,
                "meta_status": meta_status,
                "meta_error": db_entry.get("meta_error") if db_entry else None,
                "auto_watch": job_id in self._auto_watch,
                "meta_generate_on_finish": db_entry.get("meta_generate_on_finish", False) if db_entry else False,
                "batch_running": job_id in self._batch_running,
            }

    def set_auto_watch(self, job_id: str, enabled: bool) -> None:
        """Enable or disable d3plot auto-watch polling for a job."""
        with self._lock:
            if enabled:
                self._auto_watch.add(job_id)
            else:
                self._auto_watch.discard(job_id)

    def clear_state(self, job_ids: List[str]) -> None:
        """Remove in-memory META state for the given job IDs (call on delete)."""
        with self._lock:
            for jid in job_ids:
                self._auto_watch.discard(jid)
                self._batch_running.discard(jid)
                self._batch_start.pop(jid, None)
                self._d3plot_counts.pop(jid, None)
                self._d3plot_last_checked.pop(jid, None)

    # ------------------------------------------------------------------
    # Watcher daemon
    # ------------------------------------------------------------------

    def _watcher_loop(self) -> None:
        """Daemon thread: poll for metadb completion and d3plot changes."""
        while True:
            try:
                now = time.time()

                # 1. Check metadb completion for batch-running jobs
                with self._lock:
                    batch_jobs = set(self._batch_running)

                for job_id in batch_jobs:
                    db_entry = self._job_db.get(job_id)
                    if not db_entry:
                        with self._lock:
                            self._batch_running.discard(job_id)
                            self._batch_start.pop(job_id, None)
                        continue

                    job_od = OrderedDict(db_entry.items())
                    sim_dir = self._monitor.get_sim_dir(job_od)
                    if sim_dir and os.path.isfile(os.path.join(sim_dir, "runtimePBSPro.metadb")):
                        with self._lock:
                            self._batch_running.discard(job_id)
                            self._batch_start.pop(job_id, None)
                        self._job_db.set_meta_status(job_id, "ready")
                        logger.info("runtimePBSPro.metadb ready for job %s", job_id)
                    else:
                        start = self._batch_start.get(job_id, now)
                        if now - start > self._metadb_poll_timeout:
                            with self._lock:
                                self._batch_running.discard(job_id)
                                self._batch_start.pop(job_id, None)
                            self._job_db.set_meta_status(
                                job_id, "error", "Timed out waiting for metadb"
                            )
                            logger.warning("Timeout waiting for metadb for job %s", job_id)

                # 2. D3plot count check for auto-watched jobs
                with self._lock:
                    watched = set(self._auto_watch)

                for job_id in watched:
                    last = self._d3plot_last_checked.get(job_id, 0)
                    if now - last < self._d3plot_poll_interval:
                        continue
                    db_entry = self._job_db.get(job_id)
                    if not db_entry:
                        continue
                    job_od = OrderedDict(db_entry.items())
                    sim_dir = self._monitor.get_sim_dir(job_od)
                    if not sim_dir:
                        self._d3plot_last_checked[job_id] = now
                        continue
                    count = self._monitor.get_d3plot_count(sim_dir)
                    self._d3plot_last_checked[job_id] = now
                    prev = self._d3plot_counts.get(job_id, 0)
                    self._d3plot_counts[job_id] = count
                    if count > prev:
                        with self._lock:
                            already = job_id in self._batch_running
                        if not already:
                            logger.info("New d3plots for job %s (%d→%d), launching batch in 10s",
                                        job_id, prev, count)
                            time.sleep(10)
                            self.launch_batch(job_id, sim_dir)

            except Exception as exc:
                logger.error("Unexpected error: %s", exc)

            time.sleep(self._metadb_poll_interval)

    def _start_watcher(self) -> None:
        t = threading.Thread(
            target=self._watcher_loop,
            name="meta-watcher",
            daemon=True,
        )
        t.start()
