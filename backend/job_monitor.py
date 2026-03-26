"""
Job Monitor - Fetches PBS jobs from Linux servers and handles path conversion

Provides functionality to:
- Fetch running jobs from multiple PBS servers
- Convert between Windows and Linux paths using drive mappings
- Fetch messag files for convergence analysis
- Stream live log output
- Shared job cache for FastAPI (replaces per-session Streamlit state)
"""

import glob as _glob
import paramiko
import subprocess
import yaml
import os
import sys
import time
import random
import asyncio
import json
import shutil
import threading
from pathlib import Path
from typing import Optional, Dict, List, Any, Set, Tuple
from datetime import datetime
from collections import OrderedDict
from getpass import getuser


class JobDatabase:
    """
    Persistent job store backed by a JSON file on disk.

    Records every PBS job the monitor has ever seen, from first appearance
    through completion. Survives backend restarts. Thread-safe.

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
                print(f"[job-db] Failed to load {self._path}: {exc}")
                self._data = {}

    def _save(self) -> None:
        try:
            with open(self._path, "w") as f:
                json.dump(self._data, f, indent=2)
        except Exception as exc:
            print(f"[job-db] Failed to save {self._path}: {exc}")

    def upsert(self, job: dict) -> None:
        """Add new job or refresh dynamic fields (Status/CPUs/Memory/Job_Name).
        Preserves existing meta_* fields on update."""
        job_id = job.get("JobID", "")
        if not job_id or job_id == "N/A":
            return
        now = datetime.now().isoformat()
        with self._lock:
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
                # Refresh fields that can change while running
                for k in ("Status", "CPUs", "Memory", "Job_Name"):
                    val = job.get(k)
                    if val and val != "N/A":
                        self._data[job_id][k] = val
                self._data[job_id]["last_seen"] = now
                # Ensure meta fields exist for jobs created before this feature
                self._data[job_id].setdefault("meta_status", "idle")
                self._data[job_id].setdefault("meta_error", None)
                self._data[job_id].setdefault("meta_generate_on_finish", False)
            self._save()

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


class JobMonitor:
    """
    Manages PBS job monitoring across multiple Linux servers

    Handles SSH connections, job retrieval, and path conversion
    between Windows and Linux file systems.
    """

    def __init__(self, config_path: str = "./config.yaml"):
        """
        Initialize JobMonitor with configuration

        Args:
            config_path: Path to config.yaml file
        """
        self.config = self._load_config(config_path)
        self.user = getuser()
        self.all_jobs = []
        self._messag_cache = {}
        self._messag_cache_time = {}

        # Shared job list cache (serves all FastAPI clients)
        self._jobs_cache: List[OrderedDict] = []
        self._jobs_cache_time: float = 0.0

        self._setup_from_config()

        # Persistent job database (survives backend restarts)
        db_path = Path(config_path).parent / "job_database.json"
        self._job_db = JobDatabase(str(db_path))

        # Background thread: copies messag → messag_react so Python never
        # holds the live LS-DYNA output file open for extended periods.
        self._copy_interval: int = self.dashboard_config.get("messag_copy_interval", 30)
        self._start_background_copier()

        # META CAE Systems viewer integration (optional — disabled if not configured)
        if self._meta_exe:
            self._start_meta_watcher()

        # Lightweight HTML status page writer (optional — disabled if not configured)
        if self._status_page_path:
            self._start_status_page_writer()

    def _load_config(self, config_path: str) -> dict:
        """Load configuration from YAML file"""
        config_file = Path(config_path)
        if not config_file.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        with open(config_file, "r") as f:
            config = yaml.safe_load(f)

        return config

    def _setup_from_config(self):
        """Extract configuration values from loaded config"""
        self.qdel_path = self.config["pbs"]["qdel_path"]
        self.qsub_path = self.config["pbs"]["qsub_path"]
        self.linux_base_path = self.config["paths"]["linux_base_path"]
        self.remote_script_name = self.config["paths"]["remote_script_name"]
        self.drive_mapping = self.config["drive_mapping"]
        self.ssh_timeout = self.config.get("ssh", {}).get("connection_timeout", 10)
        self.dashboard_config = self.config.get("dashboard", {})
        self.cache_timeout = self.dashboard_config.get("cache_timeout", 20)

        self.server_to_drive = {
            hostname: drive for drive, hostname in self.drive_mapping.items()
        }
        self.servers = self._setup_servers(self.config["servers"])

        self.script_dir = f"{self.linux_base_path}/{self.user}"
        self.script_path = f"{self.script_dir}/{self.remote_script_name}"

        # META CAE Systems integration (optional)
        meta_cfg = self.config.get("meta", {})
        self._meta_exe: Optional[str] = meta_cfg.get("executable") or None
        self._d3plot_poll_interval: int = int(meta_cfg.get("d3plot_poll_interval_minutes", 10)) * 60
        self._metadb_poll_interval: int = int(meta_cfg.get("metadb_poll_interval_seconds", 30))
        self._metadb_poll_timeout: int = int(meta_cfg.get("metadb_poll_timeout_minutes", 60)) * 60

        # In-memory META state (reset on restart, not persisted)
        self._auto_watch: Set[str] = set()
        self._meta_batch_running: Set[str] = set()
        self._meta_batch_start: Dict[str, float] = {}
        self._d3plot_counts: Dict[str, int] = {}
        self._d3plot_last_checked: Dict[str, float] = {}
        self._meta_lock = threading.Lock()

        # Submit-time pending flags (windows_job_path → True), cleared on F transition
        self._pending_meta_on_finish: Dict[str, bool] = {}

        # Lightweight HTML status page (optional)
        sp_cfg = self.config.get("status_page", {})
        self._status_page_path: Optional[str] = sp_cfg.get("output_path") or None

    def _setup_servers(self, servers_config: List[dict]) -> List[dict]:
        """Add username and key_file to server configurations"""
        user_home = os.path.expanduser("~")
        default_key_path = os.path.join(user_home, ".ssh", "id_rsa")

        config_key = self.config.get("ssh", {}).get("key_file", "")

        if config_key and os.path.exists(os.path.expanduser(config_key)):
            use_key = os.path.expanduser(config_key)
        elif os.path.exists(default_key_path):
            use_key = default_key_path
        else:
            use_key = None

        servers = []
        for srv in servers_config:
            server = {
                "hostname": srv["hostname"],
                "name": srv["name"],
                "username": self.user,
                "key_file": use_key,
            }
            servers.append(server)

        return servers

    def connect_and_execute(self, server: dict, command: str) -> Optional[str]:
        """Execute command on remote server via SSH"""
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            if server.get("key_file"):
                ssh.connect(
                    server["hostname"],
                    username=server["username"],
                    key_filename=server["key_file"],
                    timeout=self.ssh_timeout,
                )
            else:
                ssh.connect(
                    server["hostname"],
                    username=server["username"],
                    timeout=self.ssh_timeout,
                )

            stdin, stdout, stderr = ssh.exec_command(command)
            output = stdout.read().decode("utf-8")
            error = stderr.read().decode("utf-8")

            ssh.close()

            if error and not output:
                print(f"Error from {server['hostname']}: {error}")
                return None

            return output

        except Exception as e:
            print(f"Connection error to {server['hostname']}: {str(e)}")
            return None

    def parse_output(self, output: str, server_name: str) -> List[OrderedDict]:
        """Parse the JSON output from the Python script"""
        try:
            jobs_data = json.loads(output)
            jobs = []

            for job_data in jobs_data:
                job = OrderedDict(
                    [
                        ("Server", server_name),
                        ("JobID", job_data.get("JobID", "N/A")),
                        ("Job_Name", job_data.get("Job_Name", "N/A")),
                        ("Job_Path", job_data.get("Job_Path", "N/A")),
                        ("CPUs", str(job_data.get("CPUs", "N/A"))),
                        ("Status", job_data.get("Status", "N/A")),
                        ("Owner", job_data.get("Owner", "N/A")),
                        ("Memory", job_data.get("Memory", "N/A")),
                    ]
                )
                jobs.append(job)

            return jobs

        except json.JSONDecodeError as e:
            print(f"Error parsing JSON from {server_name}: {str(e)}")
            return []

    def fetch_jobs(self) -> List[OrderedDict]:
        """Fetch jobs from all servers (bypasses cache) and sync with job database."""
        self.all_jobs = []

        for server in self.servers:
            command = f"python3 {self.script_path} --json"
            output = self.connect_and_execute(server, command)

            if output:
                jobs = self.parse_output(output, server["name"])
                self.all_jobs.extend(jobs)

        # Upsert every live job into the persistent database
        live_ids = {j["JobID"] for j in self.all_jobs}
        for job in self.all_jobs:
            self._job_db.upsert(dict(job))
            # Transfer submit-time pending META flag to DB (keep until F transition)
            if self._meta_exe:
                win_path = self.get_windows_path(job)
                if win_path and win_path in self._pending_meta_on_finish:
                    self._job_db.set_meta_generate_on_finish(job["JobID"], True)

        # Any DB-active job that vanished from live list has finished:
        # do a final messag copy, then mark it F in the DB.
        for db_job in self._job_db.get_active():
            if db_job["JobID"] not in live_ids:
                self._do_final_messag_copy(db_job)
                self._job_db.mark_finished(db_job["JobID"])
                print(f"[job-db] Marked finished: {db_job['JobID']} ({db_job.get('Job_Name', '')})")

                # META on-finish hook
                if self._meta_exe:
                    job_path = db_job.get("Job_Path", "")
                    win_path = self.get_windows_path(OrderedDict(db_job.items()))
                    pending = bool(win_path and self._pending_meta_on_finish.pop(win_path, False))
                    db_entry = self._job_db.get(db_job["JobID"])
                    if (pending or (db_entry and db_entry.get("meta_generate_on_finish"))) \
                            and db_entry.get("meta_status", "idle") not in ("generating", "ready"):
                        sim_dir = self.get_sim_dir(OrderedDict(db_job.items()))
                        if sim_dir:
                            self.launch_meta_batch(db_job["JobID"], sim_dir)

        return self.all_jobs

    def _do_final_messag_copy(self, job_dict: dict) -> None:
        """
        Copy messag → messag_react one final time when a job is detected as finished.

        Called from fetch_jobs() the moment a previously-active job disappears
        from the live server list, ensuring the last simulation output is captured
        before the entry is archived in the database.
        """
        job_od = OrderedDict(job_dict.items())
        src = self.get_messag_path(job_od)
        dst = self.get_messag_react_path(job_od)
        if src and dst:
            try:
                if os.path.isfile(src):
                    shutil.copy2(src, dst)
                    print(f"[messag-copier] Final copy done for job {job_dict.get('JobID')}")
            except Exception as exc:
                print(f"[messag-copier] Final copy failed for job {job_dict.get('JobID')}: {exc}")

    def get_jobs_cached(self, ttl: Optional[int] = None) -> List[OrderedDict]:
        """
        Fetch jobs with a shared TTL cache (used by FastAPI to serve all clients).

        All browser tabs share this single cache — no redundant SSH calls.

        Args:
            ttl: Cache time-to-live in seconds. Defaults to config cache_timeout.

        Returns:
            List of job OrderedDicts
        """
        if ttl is None:
            ttl = self.cache_timeout

        now = time.time()
        if self._jobs_cache and (now - self._jobs_cache_time) < ttl:
            return self._jobs_cache

        jobs = self.fetch_jobs()
        self._jobs_cache = jobs
        self._jobs_cache_time = now
        return jobs

    def windows_to_linux_path(
        self, windows_path: str
    ) -> Tuple[Optional[str], Optional[str]]:
        """Convert Windows path to Linux path and determine server"""
        windows_path = os.path.abspath(windows_path)
        drive = windows_path[0].upper()

        if drive not in self.drive_mapping:
            return None, None

        server_hostname = self.drive_mapping[drive]
        path_after_drive = windows_path[2:].replace("\\", "/")
        linux_path = f"{self.linux_base_path}/{self.user}{path_after_drive}"

        return server_hostname, linux_path

    def linux_to_windows_path(
        self, linux_path: str, server_hostname: Optional[str] = None
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Convert Linux path to Windows path

        Args:
            linux_path: Full Linux path (e.g., /mnt/fhgfs/user/path/to/job)
            server_hostname: Optional hostname to select the correct drive mapping

        Returns:
            Tuple of (windows_path, server_hostname) or (None, None) if no match
        """
        linux_path = os.path.normpath(linux_path).replace(os.sep, "/")

        expected_prefix = f"{self.linux_base_path}/{self.user}"
        if not linux_path.startswith(expected_prefix):
            return None, None

        relative_path = linux_path[len(expected_prefix):]

        # If a specific hostname is provided, use its drive mapping
        if server_hostname:
            drive = self.server_to_drive.get(server_hostname)
            if drive:
                windows_path = drive + ":" + relative_path.replace("/", "\\")
                return windows_path, server_hostname
            return None, None

        # Fallback: return first drive mapping (legacy behavior)
        for drive, hostname in self.drive_mapping.items():
            windows_path = drive + ":" + relative_path.replace("/", "\\")
            return windows_path, hostname

        return None, None

    def get_windows_path(self, job: OrderedDict) -> Optional[str]:
        """
        Get Windows path for a job

        Args:
            job: Job OrderedDict from fetch_jobs()

        Returns:
            Windows path string or None
        """
        linux_path = job.get("Job_Path", "")
        if not linux_path or linux_path == "N/A":
            return None

        # Look up the hostname from the job's server name
        server_name = job.get("Server", "")
        hostname = None
        for server in self.servers:
            if server["name"] == server_name:
                hostname = server["hostname"]
                break

        windows_path, _ = self.linux_to_windows_path(linux_path, hostname)
        return windows_path

    def get_messag_path(self, job: OrderedDict) -> Optional[str]:
        """
        Get full path to the live messag file for a job.

        Used internally by the background copier as the *source*.
        Do NOT open this file directly for analysis — use get_messag_react_path()
        instead so that Python never holds the LS-DYNA output file open.

        Args:
            job: Job OrderedDict from fetch_jobs()

        Returns:
            Windows path to messag file or None
        """
        windows_path = self.get_windows_path(job)
        if windows_path:
            if os.path.exists(os.path.join(windows_path, "Simulation")):
                return os.path.join(windows_path, "Simulation", "messag")
            else:
                dirSimRet = [f.name for f in Path(windows_path).iterdir() if f.is_dir() and f.name.startswith("Simulation_Ret")]
                if len(dirSimRet) == 1:
                    return os.path.join(windows_path, dirSimRet[0], "messag")
        return None

    def get_messag_react_path(self, job: OrderedDict) -> Optional[str]:
        """
        Get full path to the safe-to-read copy of the messag file.

        The background copier maintains this file as a periodic snapshot of
        the live messag so Python analysis never blocks LS-DYNA writes.

        Args:
            job: Job OrderedDict from fetch_jobs()

        Returns:
            Windows path to messag_react file or None
        """
        windows_path = self.get_windows_path(job)
        if windows_path:
            if os.path.exists(os.path.join(windows_path, "Simulation")):
                return os.path.join(windows_path, "Simulation", "messag_react")
            else:
                dirSimRet = [f.name for f in Path(windows_path).iterdir() if f.is_dir() and f.name.startswith("Simulation_Ret")]
                if len(dirSimRet) == 1:
                    return os.path.join(windows_path, dirSimRet[0], "messag_react")
        return None

    # ------------------------------------------------------------------
    # Background messag copier
    # ------------------------------------------------------------------

    def _copy_all_messag_files(self) -> None:
        """
        Copy messag → messag_react for every job currently in the cache.

        shutil.copy2 opens the source file only briefly (binary stream copy)
        and closes it immediately, so the live LS-DYNA output file is locked
        for the minimum possible time.
        """
        # Snapshot the list to avoid races with cache updates
        jobs = list(self._jobs_cache)
        for job in jobs:
            src = self.get_messag_path(job)
            dst = self.get_messag_react_path(job)
            if src and dst:
                try:
                    if os.path.isfile(src):
                        shutil.copy2(src, dst)
                except Exception as exc:
                    print(f"[messag-copier] Failed to copy for job {job.get('JobID')}: {exc}")

    def _background_copy_loop(self) -> None:
        """Daemon thread body: copy messag files periodically."""
        while True:
            try:
                self._copy_all_messag_files()
            except Exception as exc:
                print(f"[messag-copier] Unexpected error: {exc}")
            time.sleep(self._copy_interval)

    def _start_background_copier(self) -> None:
        """Start the daemon thread that keeps messag_react files up-to-date."""
        t = threading.Thread(
            target=self._background_copy_loop,
            name="messag-copier",
            daemon=True,
        )
        t.start()

    # ------------------------------------------------------------------
    # META CAE Systems integration
    # ------------------------------------------------------------------

    def get_sim_dir(self, job: OrderedDict) -> Optional[str]:
        """
        Return the Simulation/ (or Simulation_Ret*/) directory as a Windows path.
        Reuses the same fallback logic as get_messag_path.
        Returns None if not resolvable.
        """
        windows_path = self.get_windows_path(job)
        if not windows_path:
            return None
        sim_dir = os.path.join(windows_path, "Simulation")
        if os.path.exists(sim_dir):
            return sim_dir
        try:
            dirs = [f.name for f in Path(windows_path).iterdir()
                    if f.is_dir() and f.name.startswith("Simulation_Ret")]
            if len(dirs) == 1:
                return os.path.join(windows_path, dirs[0])
        except Exception:
            pass
        return None

    def get_d3plot_count(self, sim_dir: str) -> int:
        """Return the number of d3plot* files in sim_dir (local mapped drive, no SSH)."""
        return len(_glob.glob(os.path.join(sim_dir, "d3plot*")))

    def launch_meta_batch(self, job_id: str, sim_dir: str) -> None:
        """
        Copy runtimePBSProDB.ses into sim_dir and launch META in batch mode to generate runtimePBSPro.metadb.
        Marks meta_status='generating' in the job database.
        """
        src_ses = Path(__file__).parent / "runtimePBSProDB.ses"
        dst_ses = Path(sim_dir) / "runtimePBSProDB.ses"
        try:
            shutil.copy2(str(src_ses), str(dst_ses))
        except Exception as exc:
            print(f"[meta] Failed to copy runtimePBSProDB.ses to {sim_dir}: {exc}")
            self._job_db.set_meta_status(job_id, "error", f"Failed to copy runtimePBSProDB.ses: {exc}")
            return

        cmd = f'"{self._meta_exe}" -b -s "{dst_ses}" "{sim_dir}" -nolog -noses'
        try:
            subprocess.Popen(cmd, shell=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            print(f"[meta] Failed to launch META batch for job {job_id}: {exc}")
            self._job_db.set_meta_status(job_id, "error", f"Failed to launch META: {exc}")
            return

        with self._meta_lock:
            self._meta_batch_running.add(job_id)
            self._meta_batch_start[job_id] = time.time()
        self._job_db.set_meta_status(job_id, "generating")
        print(f"[meta] Launched batch for job {job_id} in {sim_dir}")

    def launch_meta_viewer(self, sim_dir: str) -> Tuple[bool, str]:
        """
        Launch META viewer for the runtimePBSPro.metadb in sim_dir.
        Returns (True, cmd_string) so the frontend can show the copy-able command.
        """
        metadb = os.path.join(sim_dir, "runtimePBSPro.metadb")
        cmd = f'"{self._meta_exe}" -p "{metadb}" -viewer -nolog -noses'
        try:
            subprocess.Popen(cmd, shell=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True, cmd
        except Exception as exc:
            return False, str(exc)

    def _meta_watcher_loop(self) -> None:
        """Daemon thread: poll for metadb completion and d3plot changes."""
        while True:
            try:
                now = time.time()

                # 1. Check metadb completion for batch-running jobs
                with self._meta_lock:
                    batch_jobs = set(self._meta_batch_running)

                for job_id in batch_jobs:
                    db_entry = self._job_db.get(job_id)
                    if not db_entry:
                        with self._meta_lock:
                            self._meta_batch_running.discard(job_id)
                            self._meta_batch_start.pop(job_id, None)
                        continue

                    # Reconstruct job OrderedDict to get sim_dir
                    job_od = OrderedDict(db_entry.items())
                    sim_dir = self.get_sim_dir(job_od)
                    if sim_dir and os.path.isfile(os.path.join(sim_dir, "runtimePBSPro.metadb")):
                        with self._meta_lock:
                            self._meta_batch_running.discard(job_id)
                            self._meta_batch_start.pop(job_id, None)
                        self._job_db.set_meta_status(job_id, "ready")
                        print(f"[meta] runtimePBSPro.metadb ready for job {job_id}")
                    else:
                        start = self._meta_batch_start.get(job_id, now)
                        if now - start > self._metadb_poll_timeout:
                            with self._meta_lock:
                                self._meta_batch_running.discard(job_id)
                                self._meta_batch_start.pop(job_id, None)
                            self._job_db.set_meta_status(
                                job_id, "error", "Timed out waiting for metadb"
                            )
                            print(f"[meta] Timeout waiting for metadb for job {job_id}")

                # 2. D3plot count check for auto-watched jobs
                with self._meta_lock:
                    watched = set(self._auto_watch)

                for job_id in watched:
                    last = self._d3plot_last_checked.get(job_id, 0)
                    if now - last < self._d3plot_poll_interval:
                        continue
                    db_entry = self._job_db.get(job_id)
                    if not db_entry:
                        continue
                    job_od = OrderedDict(db_entry.items())
                    sim_dir = self.get_sim_dir(job_od)
                    if not sim_dir:
                        self._d3plot_last_checked[job_id] = now
                        continue
                    count = self.get_d3plot_count(sim_dir)
                    self._d3plot_last_checked[job_id] = now
                    prev = self._d3plot_counts.get(job_id, 0)
                    self._d3plot_counts[job_id] = count
                    if count > prev:
                        with self._meta_lock:
                            already = job_id in self._meta_batch_running
                        if not already:
                            print(f"[meta] New d3plots for job {job_id} ({prev}→{count}), launching batch in 10s")
                            time.sleep(10)
                            self.launch_meta_batch(job_id, sim_dir)

            except Exception as exc:
                print(f"[meta-watcher] Unexpected error: {exc}")

            time.sleep(self._metadb_poll_interval)

    def _start_meta_watcher(self) -> None:
        """Start the daemon thread that watches META batch jobs and d3plot counts."""
        t = threading.Thread(
            target=self._meta_watcher_loop,
            name="meta-watcher",
            daemon=True,
        )
        t.start()

    # ------------------------------------------------------------------
    # Lightweight HTML status page writer
    # ------------------------------------------------------------------

    def _in_status_page_window(self) -> bool:
        """
        Return True if the current local time falls inside an active writing window.

        Active windows:
          - 17:00 – 01:00 (evening / overnight)
          - 06:00 – 09:00 (morning)
        """
        h = datetime.now().hour + datetime.now().minute / 60.0
        return (h >= 17.0 or h < 1.0) or (6.0 <= h < 9.0)

    def _start_status_page_writer(self) -> None:
        """Start the daemon thread that writes the HTML status page on a random schedule."""
        t = threading.Thread(
            target=self._status_page_writer_loop,
            name="status-page-writer",
            daemon=True,
        )
        t.start()
        print(f"[status-page] Writer started → {self._status_page_path}")

    def _status_page_writer_loop(self) -> None:
        """
        Daemon thread body.

        Writes the status page immediately upon entering an active window, then
        sleeps a random 20–45 minutes before the next write.  When outside all
        windows the thread polls every 60 seconds so it wakes promptly at window
        open without burning CPU.
        """
        while True:
            try:
                if self._in_status_page_window():
                    self._write_status_page()
                    sleep_sec = random.uniform(20 * 60, 45 * 60)
                    print(
                        f"[status-page] Next write in {sleep_sec / 60:.1f} min"
                    )
                    time.sleep(sleep_sec)
                else:
                    time.sleep(60)
            except Exception as exc:
                print(f"[status-page] Unexpected error: {exc}")
                time.sleep(60)

    def _write_status_page(self) -> None:
        """Render the HTML and write it to the configured output path."""
        try:
            html = self._generate_status_html()
            path = self._status_page_path
            # Write atomically via a temp file next to the target
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(html)
            os.replace(tmp, path)
            print(
                f"[status-page] Written at {datetime.now().strftime('%H:%M:%S')} → {path}"
            )
        except Exception as exc:
            print(f"[status-page] Failed to write: {exc}")

    def _generate_status_html(self) -> str:
        """
        Build a fully self-contained HTML status page.

        Reads messag_react (or messag) for R/E/F jobs to extract current sim
        time and step count via ConvergenceParser — no SSH required.
        """
        from convergence_plotter import ConvergenceParser

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Merge live + finished jobs (live takes priority by JobID)
        # Use get_jobs_cached() so stale cache is refreshed and finished jobs
        # are properly detected and marked F in the DB before we render.
        live_jobs = list(self.get_jobs_cached())
        finished_jobs = self._job_db.get_finished()
        live_ids = {j.get("JobID") for j in live_jobs}
        all_jobs = live_jobs + [j for j in finished_jobs if j.get("JobID") not in live_ids]

        STATUS_ORDER = {"R": 0, "E": 1, "Q": 2, "F": 3}
        all_jobs.sort(key=lambda j: STATUS_ORDER.get(j.get("Status", "F"), 9))

        rows_html = []
        for job in all_jobs:
            job_id   = job.get("JobID", "N/A")
            job_name = job.get("Job_Name", "N/A")
            status   = job.get("Status", "N/A")
            server   = job.get("Server", "N/A")
            owner    = job.get("Owner", "N/A")
            finished_at = job.get("finished_at", "")

            sim_time_str   = "—"
            step_size_str  = "—"
            term_str       = "—"
            notes_str      = ""

            if status in ("R", "E", "F"):
                job_od = OrderedDict(job.items())
                content = None
                react_path = self.get_messag_react_path(job_od)
                if react_path and os.path.isfile(react_path):
                    try:
                        with open(react_path, "r", encoding="utf-8", errors="ignore") as f:
                            content = f.read()
                    except Exception:
                        pass
                if content is None:
                    messag_path = self.get_messag_path(job_od)
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
                    step_size_str = self._parse_step_size(content)

            if status == "F" and finished_at:
                try:
                    fa = datetime.fromisoformat(finished_at)
                    notes_str = f"Finished {fa.strftime('%Y-%m-%d %H:%M')}"
                except Exception:
                    notes_str = finished_at

            badge_class = f"badge-{status}" if status in ("R", "Q", "E", "F") else "badge-unk"
            rows_html.append(f"""
        <tr>
          <td class="job-name">{self._html_esc(job_name)}</td>
          <td class="mono">{self._html_esc(job_id)}</td>
          <td><span class="badge {badge_class}">{status}</span></td>
          <td>{self._html_esc(server)}</td>
          <td class="mono">{sim_time_str}</td>
          <td class="mono">{step_size_str}</td>
          <td>{self._html_esc(term_str)}</td>
          <td class="notes">{self._html_esc(notes_str)}</td>
        </tr>""")

        rows = "\n".join(rows_html) if rows_html else (
            '<tr><td colspan="8" class="empty">No jobs found.</td></tr>'
        )

        job_count = len(all_jobs)
        r_count   = sum(1 for j in all_jobs if j.get("Status") == "R")
        q_count   = sum(1 for j in all_jobs if j.get("Status") == "Q")
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
  <div class="stat-pill">Queued <span>{q_count}</span></div>
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

    @staticmethod
    def _parse_step_size(content: str) -> str:
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

    @staticmethod
    def _html_esc(text: str) -> str:
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

    def get_meta_status(self, job_id: str) -> dict:
        """Return current META status dict for the given job."""
        db_entry = self._job_db.get(job_id)
        meta_status = db_entry.get("meta_status", "idle") if db_entry else "idle"

        # If DB says "ready", verify the metadb file actually exists on disk.
        # It may have been deleted externally; if so, reset to idle.
        if meta_status == "ready" and db_entry:
            sim_dir = self.get_sim_dir(OrderedDict(db_entry.items()))
            if not sim_dir or not os.path.isfile(os.path.join(sim_dir, "runtimePBSPro.metadb")):
                self._job_db.set_meta_status(job_id, "idle")
                meta_status = "idle"

        with self._meta_lock:
            return {
                "configured": self._meta_exe is not None,
                "meta_status": meta_status,
                "meta_error": db_entry.get("meta_error") if db_entry else None,
                "auto_watch": job_id in self._auto_watch,
                "meta_generate_on_finish": db_entry.get("meta_generate_on_finish", False) if db_entry else False,
                "batch_running": job_id in self._meta_batch_running,
            }

    def _clear_meta_state(self, job_ids: List[str]) -> None:
        """Remove in-memory META state for the given job IDs (call on delete)."""
        with self._meta_lock:
            for jid in job_ids:
                self._auto_watch.discard(jid)
                self._meta_batch_running.discard(jid)
                self._meta_batch_start.pop(jid, None)
                self._d3plot_counts.pop(jid, None)
                self._d3plot_last_checked.pop(jid, None)

    def fetch_messag_content(
        self, job: OrderedDict, force_refresh: bool = False
    ) -> Optional[str]:
        """
        Fetch messag file content for a job (with caching).

        Reads from messag_react (the background-maintained safe copy) so that
        Python never holds the live LS-DYNA output file open for analysis.
        Returns None if messag_react has not been created yet (copy not run
        at least once since the job started).

        Args:
            job: Job OrderedDict from fetch_jobs()
            force_refresh: Force refetch even if cached recently

        Returns:
            Messag file content as string or None
        """
        react_path = self.get_messag_react_path(job)
        if not react_path:
            return None

        # Derive server hostname from the original messag path for validation
        messag_path = self.get_messag_path(job)
        if not messag_path:
            return None

        linux_path = self._get_linux_messag_path(job)
        if not linux_path:
            return None

        cache_key = job.get("JobID", react_path)
        current_time = time.time()

        if not force_refresh:
            if cache_key in self._messag_cache:
                cached_time = self._messag_cache_time.get(cache_key, 0)
                if current_time - cached_time < self.cache_timeout:
                    return self._messag_cache[cache_key]

        server_hostname, _ = self.windows_to_linux_path(messag_path)
        if not server_hostname:
            return None

        server = self._get_server_by_hostname(server_hostname)
        if not server:
            return None

        if not os.path.exists(react_path):
            # messag_react not yet created — background copier hasn't run yet
            return None

        try:
            with open(react_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()

            self._messag_cache[cache_key] = content
            self._messag_cache_time[cache_key] = current_time

            return content

        except Exception as e:
            print(f"Error reading messag_react: {e}")
            return None

    def _get_linux_messag_path(self, job: OrderedDict) -> Optional[str]:
        """Get Linux path to messag file"""
        linux_path = job.get("Job_Path", "")
        if not linux_path or linux_path == "N/A":
            return None
        return f"{linux_path}/Simulation/messag"

    def _get_server_by_hostname(self, hostname: str) -> Optional[dict]:
        """Get server config by hostname"""
        for server in self.servers:
            if server["hostname"] == hostname:
                return server
        return None

    def get_log_content(
        self, job: OrderedDict, max_lines: int = 100
    ) -> Tuple[Optional[str], int]:
        """
        Get the last N lines of the log file (tail behaviour).

        Reads from messag_react (safe copy) so Python never holds the live
        LS-DYNA output file open during the SSE polling loop.

        Args:
            job: Job OrderedDict
            max_lines: Maximum lines to return (tail)

        Returns:
            Tuple of (content, current_file_size)
        """
        react_path = self.get_messag_react_path(job)
        if react_path and os.path.exists(react_path):
            read_path = react_path
        else:
            # Fall back to the live messag file — safe for finished jobs
            # where messag_react was never created.
            messag_path = self.get_messag_path(job)
            if not messag_path or not os.path.exists(messag_path):
                return None, 0
            read_path = messag_path

        try:
            with open(read_path, "r", encoding="utf-8", errors="ignore") as f:
                # Get file size
                f.seek(0, 2)
                file_size = f.tell()

                # Read all lines and take the last max_lines
                f.seek(0)
                lines = f.readlines()

                if len(lines) > max_lines:
                    lines = lines[-max_lines:]

                content = "".join(lines)
                return content, file_size

        except Exception as e:
            print(f"Error reading messag_react log: {e}")
            return None, 0

    def clear_cache(self):
        """Clear the messag file cache"""
        self._messag_cache.clear()
        self._messag_cache_time.clear()

    def get_servers(self) -> List[dict]:
        """Get list of configured servers"""
        return self.servers

    def get_drive_mapping(self) -> dict:
        """Get drive to hostname mapping"""
        return self.drive_mapping.copy()

    def kill_job(self, job: OrderedDict) -> Tuple[bool, str]:
        """
        Send qdel command to kill a PBS job (does not wait for termination)

        Args:
            job: Job OrderedDict from fetch_jobs()

        Returns:
            Tuple of (success, message)
        """
        job_id = job.get("JobID", "")
        server_name = job.get("Server", "")

        if not job_id or not server_name:
            return False, "Invalid job data"

        # Find the server config
        server = None
        for s in self.servers:
            if s["name"] == server_name:
                server = s
                break

        if not server:
            return False, f"Server {server_name} not found"

        # Kill the job
        command = f"{self.qdel_path} {job_id}"
        output = self.connect_and_execute(server, command)

        if output is None:
            return False, f"Failed to connect to {server_name}"

        return True, f"Kill signal sent for job {job_id}"

    def is_job_terminated(self, job: OrderedDict) -> bool:
        """
        Check if job has been terminated by looking for job_log file

        Args:
            job: Job OrderedDict from fetch_jobs()

        Returns:
            True if job_log file exists (job terminated)
        """
        windows_path = self.get_windows_path(job)
        if not windows_path:
            return False

        job_log_path = os.path.join(windows_path, "job_log")
        return os.path.isfile(job_log_path)

    def wait_for_job_termination(
        self,
        job: OrderedDict,
        timeout: int = 120,
        poll_interval: float = 2.0,
        progress_callback=None,
    ) -> Tuple[bool, str]:
        """
        Wait for job to terminate by polling for job_log file (synchronous).

        Args:
            job: Job OrderedDict from fetch_jobs()
            timeout: Maximum time to wait in seconds (default 2 minutes)
            poll_interval: Time between checks in seconds
            progress_callback: Optional callback(elapsed_seconds) for progress updates

        Returns:
            Tuple of (success, message)
        """
        job_id = job.get("JobID", "")
        start_time = time.time()

        while True:
            elapsed = time.time() - start_time

            if progress_callback:
                progress_callback(elapsed)

            if self.is_job_terminated(job):
                return True, f"Job {job_id} terminated ({int(elapsed)}s)"

            if elapsed >= timeout:
                return False, f"Timeout waiting for job {job_id} to terminate after {timeout}s"

            time.sleep(poll_interval)

    async def async_wait_for_job_termination(
        self,
        job: OrderedDict,
        timeout: int = 120,
        poll_interval: float = 2.0,
    ) -> Tuple[bool, str]:
        """
        Async version: wait for job termination without blocking the event loop.

        Uses asyncio.sleep so FastAPI can serve other requests while polling.

        Args:
            job: Job OrderedDict from fetch_jobs()
            timeout: Maximum time to wait in seconds (default 2 minutes)
            poll_interval: Time between checks in seconds

        Returns:
            Tuple of (success, message)
        """
        job_id = job.get("JobID", "")
        start_time = time.time()

        while True:
            elapsed = time.time() - start_time

            if self.is_job_terminated(job):
                return True, f"Job {job_id} terminated ({int(elapsed)}s)"

            if elapsed >= timeout:
                return False, f"Timeout waiting for job {job_id} to terminate after {timeout}s"

            await asyncio.sleep(poll_interval)

    def delete_simulation_directory(self, job: OrderedDict) -> Tuple[bool, str]:
        """
        Delete the Simulation directory for a job

        Args:
            job: Job OrderedDict from fetch_jobs()

        Returns:
            Tuple of (success, message)
        """
        job_path = job.get("Job_Path", "")
        server_name = job.get("Server", "")

        if not job_path or not server_name:
            return False, "Invalid job data"

        # Find the server config
        server = None
        for s in self.servers:
            if s["name"] == server_name:
                server = s
                break

        if not server:
            return False, f"Server {server_name} not found"

        sim_path = f"{job_path}/Simulation"
        del_command = f"rm -rf {sim_path}"
        output = self.connect_and_execute(server, del_command)

        if output is None:
            return False, f"Failed to connect to {server_name}"

        job_log_file = f"{job_path}/job_log"
        del_command = f"rm {job_log_file}"
        output = self.connect_and_execute(server, del_command)
        job_err_file = f"{job_path}/job_error"
        del_command = f"rm {job_err_file}"
        output = self.connect_and_execute(server, del_command)

        return True, f"Simulation directory and job log files deleted: {sim_path}"

    def submit_job(
        self, windows_path: str, script_name: str = "qsubrunfhgfs.sh"
    ) -> Tuple[bool, str]:
        """
        Submit a new PBS job

        Args:
            windows_path: Windows path to the job directory
            script_name: Name of the submission script

        Returns:
            Tuple of (success, message)
        """
        # Convert path and get server
        server_hostname, linux_path = self.windows_to_linux_path(windows_path)

        if not server_hostname or not linux_path:
            return False, "Could not convert path or determine server"

        # Find server config by hostname
        server = None
        for s in self.servers:
            if s["hostname"] == server_hostname:
                server = s
                break

        if not server:
            return False, f"Server for {server_hostname} not found"

        # Submit the job
        command = f"cd {linux_path} && {self.qsub_path} {script_name}"
        output = self.connect_and_execute(server, command)

        if output is None:
            return False, f"Failed to connect to server"

        if output.strip():
            return True, f"Job submitted: {output.strip()}"
        return True, "Job submitted successfully"

    def generate_report(self, windows_path: str) -> Tuple[bool, str]:
        """
        Start report generation by running run_report_win.sh (non-blocking)

        Args:
            windows_path: Windows path to the job directory

        Returns:
            Tuple of (success, message)
        """
        # Convert path and get server
        server_hostname, linux_path = self.windows_to_linux_path(windows_path)

        if not server_hostname or not linux_path:
            return False, "Could not convert path or determine server"

        # Find server config by hostname
        server = None
        for s in self.servers:
            if s["hostname"] == server_hostname:
                server = s
                break

        if not server:
            return False, f"Server for {server_hostname} not found"

        # Check if run_report_win.sh exists (use interactive shell to load user's PATH)
        check_cmd = 'bash -i -c "which run_report_win.sh 2>/dev/null || command -v run_report_win.sh 2>/dev/null"'
        result = self.connect_and_execute(server, check_cmd)

        if not result or not result.strip():
            return False, "run_report_win.sh not found on server PATH"

        # Execute report generation in background with nohup
        report_cmd = f'bash -i -c \'nohup run_report_win.sh "{linux_path}" > "{linux_path}/report_gen.log" 2>&1 & echo STARTED\''
        output = self.connect_and_execute(server, report_cmd)

        if output is None:
            return False, "Failed to connect to server"

        return True, "Report generation started"

    def is_report_complete(self, windows_path: str) -> bool:
        """
        Check if report generation is complete by looking for start_server.cmd or .sh

        Args:
            windows_path: Windows path to the job directory

        Returns:
            True if report generation is complete
        """
        html_dir = os.path.join(windows_path, "Simulation", "_HTML")
        server_cmd = os.path.join(html_dir, "start_server.cmd")
        server_sh = os.path.join(html_dir, "start_server.sh")

        return os.path.isfile(server_cmd) or os.path.isfile(server_sh)

    def wait_for_report_completion(
        self,
        windows_path: str,
        timeout: int = 240,
        poll_interval: float = 3.0,
        progress_callback=None,
    ) -> Tuple[bool, str]:
        """
        Wait for report generation to complete (synchronous).

        Args:
            windows_path: Windows path to the job directory
            timeout: Maximum time to wait in seconds (default 4 minutes)
            poll_interval: Time between checks in seconds
            progress_callback: Optional callback(elapsed_seconds) for progress updates

        Returns:
            Tuple of (success, message)
        """
        start_time = time.time()

        while True:
            elapsed = time.time() - start_time

            if progress_callback:
                progress_callback(elapsed)

            if self.is_report_complete(windows_path):
                return True, f"Report generation complete ({int(elapsed)}s)"

            if elapsed >= timeout:
                return False, f"Report generation timed out after {timeout}s"

            time.sleep(poll_interval)

    async def async_wait_for_report_completion(
        self,
        windows_path: str,
        timeout: int = 240,
        poll_interval: float = 3.0,
    ) -> Tuple[bool, str]:
        """
        Async version: wait for report completion without blocking the event loop.

        Uses asyncio.sleep so FastAPI can serve other requests while polling.

        Args:
            windows_path: Windows path to the job directory
            timeout: Maximum time to wait in seconds (default 4 minutes)
            poll_interval: Time between checks in seconds

        Returns:
            Tuple of (success, message)
        """
        start_time = time.time()

        while True:
            elapsed = time.time() - start_time

            if self.is_report_complete(windows_path):
                return True, f"Report generation complete ({int(elapsed)}s)"

            if elapsed >= timeout:
                return False, f"Report generation timed out after {timeout}s"

            await asyncio.sleep(poll_interval)

    def launch_report_viewer(self, windows_path: str) -> Tuple[bool, str]:
        """
        Launch the HTML report viewer for a job

        Args:
            windows_path: Windows path to the job directory (or Linux path)

        Returns:
            Tuple of (success, message)
        """
        import subprocess
        import platform

        # Define paths for the HTML viewer
        html_dir = os.path.join(windows_path, "Simulation", "_HTML")
        server_cmd_path = os.path.join(html_dir, "start_server.cmd")
        server_sh_path = os.path.join(html_dir, "start_server.sh")

        # Check if HTML directory exists
        if not os.path.isdir(html_dir):
            return False, f"HTML directory not found: {html_dir}"

        # Determine which script exists and platform
        is_windows = platform.system() == "Windows"

        try:
            if is_windows and os.path.isfile(server_cmd_path):
                # Windows: Launch in new window
                launch_cmd = f'start "Report Server" /D "{html_dir}" cmd /c "call start_server.cmd"'
                subprocess.Popen(launch_cmd, shell=True, cwd=html_dir)
                return True, f"Report viewer launched from: {html_dir}"

            elif os.path.isfile(server_sh_path):
                # Linux: Launch the shell script in background
                os.chmod(server_sh_path, 0o755)
                subprocess.Popen(
                    ["bash", server_sh_path],
                    cwd=html_dir,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                return True, f"Report viewer launched from: {html_dir}"

            elif os.path.isfile(server_cmd_path):
                return False, f"Only .cmd found but running on Linux. Please use start_server.sh"

            else:
                return False, f"No viewer script found in: {html_dir}"

        except Exception as e:
            return False, f"Failed to launch report viewer: {str(e)}"


if __name__ == "__main__":
    monitor = JobMonitor()
    jobs = monitor.fetch_jobs()

    print(f"Found {len(jobs)} jobs:")
    for job in jobs:
        print(f"  {job['JobID']} - {job['Job_Name']} - {job['Status']}")
        windows_path = monitor.get_windows_path(job)
        if windows_path:
            print(f"    Windows Path: {windows_path}")
