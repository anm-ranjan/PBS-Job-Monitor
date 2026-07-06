"""
Job Monitor - Fetches PBS jobs from Linux servers and handles path conversion

Provides functionality to:
- Fetch running jobs from multiple PBS servers
- Convert between Windows and Linux paths using drive mappings
- Fetch messag files for convergence analysis
- Stream live log output
- Shared job cache for FastAPI (serves all browser tabs)

Optional features live in their own modules:
- job_database.py  — persistent job store (JobDatabase)
- meta_manager.py  — META CAE Systems viewer integration
- status_page.py   — lightweight HTML status page writer
- load_logger.py   — periodic NDJSON server load snapshots
"""

import glob as _glob
import json
import logging
import os
import platform
import subprocess
import threading
import time
from collections import OrderedDict
from getpass import getuser
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import paramiko
import yaml

from job_database import JobDatabase
from load_logger import LoadLogger
from meta_manager import MetaManager
from status_page import StatusPageWriter

logger = logging.getLogger("pbs_monitor.monitor")


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
        self.all_jobs: List[OrderedDict] = []
        self._messag_cache: Dict[str, str] = {}
        self._messag_cache_time: Dict[str, float] = {}
        # Rate-limiter for on-demand server-side messag copies, keyed by
        # Job_Path / folder: avoids an SSH call per SSE tick when a job has
        # no messag yet.
        self._ensure_attempt_ts: Dict[str, float] = {}

        # Shared job list cache (serves all FastAPI clients)
        self._jobs_cache: List[OrderedDict] = []
        self._jobs_cache_time: float = 0.0
        # Serialises concurrent fetches from the API threadpool and the
        # background daemons (status page writer, load logger).
        self._fetch_lock = threading.Lock()

        self._setup_from_config()

        # Persistent job database (survives backend restarts)
        db_path = Path(config_path).parent / "job_database.json"
        self.job_db = JobDatabase(str(db_path))

        # Background thread: copies messag → messag_react so Python never
        # holds the live LS-DYNA output file open for extended periods.
        self._copy_interval: int = self.dashboard_config.get("messag_copy_interval", 30)
        self._start_background_copier()

        # META CAE Systems viewer integration (optional — disabled if not configured)
        meta_cfg = self.config.get("meta", {})
        self.meta: Optional[MetaManager] = (
            MetaManager(self, self.job_db, meta_cfg) if meta_cfg.get("executable") else None
        )

        # Lightweight HTML status page writer (optional — disabled if not configured)
        sp_cfg = self.config.get("status_page", {})
        self.status_page: Optional[StatusPageWriter] = (
            StatusPageWriter(self, self.job_db, sp_cfg["output_path"])
            if sp_cfg.get("output_path") else None
        )

        # Server load logger (optional — disabled if not configured)
        ll_cfg = self.config.get("load_logger", {})
        self.load_logger: Optional[LoadLogger] = (
            LoadLogger(
                self,
                self.job_db,
                ll_cfg["output_dir"],
                ll_cfg.get("secondary_dir") or None,
                int(ll_cfg.get("interval_minutes", 10)) * 60,
            )
            if ll_cfg.get("output_dir") else None
        )

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
                logger.warning("Error from %s: %s", server["hostname"], error)
                return None

            return output

        except Exception as e:
            logger.warning("Connection error to %s: %s", server["hostname"], e)
            return None

    def parse_output(self, output: str, server_name: str) -> Optional[List[OrderedDict]]:
        """
        Parse the JSON output from the remote script.

        Returns None (not an empty list) on a JSON decode error so callers can
        distinguish "server responded with no jobs" from "response was corrupt".
        """
        try:
            jobs_data = json.loads(output)
        except json.JSONDecodeError as e:
            logger.warning("Error parsing JSON from %s: %s", server_name, e)
            return None

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

    def fetch_jobs(self) -> List[OrderedDict]:
        """Fetch jobs from all servers (bypasses cache) and sync with job database."""
        with self._fetch_lock:
            return self._fetch_jobs_locked()

    def _fetch_jobs_locked(self) -> List[OrderedDict]:
        """
        Core fetch logic — caller must hold self._fetch_lock.

        The job list is built in a local variable and assigned to
        self.all_jobs once at the end. (Concurrent fetches used to rebind
        self.all_jobs mid-build, making two threads append into the same
        list and producing duplicate job rows in the frontend.)
        """
        all_jobs: List[OrderedDict] = []
        responded: Set[str] = set()

        for server in self.servers:
            command = f"python3 {self.script_path} --json"
            output = self.connect_and_execute(server, command)
            if output is None:
                continue
            jobs = self.parse_output(output, server["name"])
            if jobs is None:
                continue
            responded.add(server["name"])
            all_jobs.extend(jobs)

        # Upsert every live job into the persistent database in one batch (single save)
        live_ids = {j["JobID"] for j in all_jobs}
        self.job_db.upsert_batch([dict(j) for j in all_jobs])
        # Transfer submit-time pending META flags to DB (kept until F transition)
        if self.meta:
            self.meta.sync_live_jobs(all_jobs)

        # Any DB-active job that vanished from the live list has finished:
        # do a final messag copy, then mark it F in the DB. Only do this for
        # jobs whose server actually responded this cycle — a transient SSH
        # failure must not mark a whole server's jobs as finished.
        for db_job in self.job_db.get_active():
            if db_job["JobID"] in live_ids:
                continue
            if db_job.get("Server") not in responded:
                continue
            self._do_final_messag_copy(db_job)
            self.job_db.mark_finished(db_job["JobID"])
            logger.info("Marked finished: %s (%s)", db_job["JobID"], db_job.get("Job_Name", ""))

            if self.meta:
                self.meta.on_job_finished(db_job)

        self.all_jobs = all_jobs
        return all_jobs

    def _do_final_messag_copy(self, job_dict: dict) -> None:
        """
        Copy messag → messag_react one final time when a job is detected as
        finished. The copy runs server-side via SSH — the client never opens
        the messag file (see the messag copier section below).
        """
        job_od = OrderedDict(job_dict.items())
        server = self._get_server_by_name(job_od.get("Server", ""))
        linux_sim_dir = self._linux_sim_dir(job_od)
        if server and linux_sim_dir:
            self._ssh_copy_messag(server, linux_sim_dir)
            logger.info("Final messag copy requested for job %s", job_dict.get("JobID"))

    def _jobs_cache_fresh(self, ttl: int) -> bool:
        return bool(self._jobs_cache) and (time.time() - self._jobs_cache_time) < ttl

    def get_jobs_cached(self, ttl: Optional[int] = None, force: bool = False) -> List[OrderedDict]:
        """
        Fetch jobs with a shared TTL cache (used by FastAPI to serve all clients).

        All browser tabs share this single cache — no redundant SSH calls.

        Args:
            ttl: Cache time-to-live in seconds. Defaults to config cache_timeout.
            force: Bypass the TTL check and fetch fresh data now.

        Returns:
            List of job OrderedDicts
        """
        if ttl is None:
            ttl = self.cache_timeout

        if not force and self._jobs_cache_fresh(ttl):
            return self._jobs_cache

        with self._fetch_lock:
            # Double-check: another thread may have refreshed while we waited.
            if not force and self._jobs_cache_fresh(ttl):
                return self._jobs_cache
            jobs = self._fetch_jobs_locked()
            self._jobs_cache = jobs
            self._jobs_cache_time = time.time()
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

    # ------------------------------------------------------------------
    # Simulation directory / messag path resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_sim_dir(windows_path: str) -> Optional[str]:
        """
        Return the Simulation/ directory under windows_path, falling back to a
        single Simulation_Ret*/ directory (finished RetForce runs). None if
        neither exists.
        """
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

    def get_sim_dir(self, job: OrderedDict) -> Optional[str]:
        """
        Return the Simulation/ (or Simulation_Ret*/) directory as a Windows path.
        Returns None if not resolvable.
        """
        windows_path = self.get_windows_path(job)
        if not windows_path:
            return None
        return self._resolve_sim_dir(windows_path)

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
        sim_dir = self.get_sim_dir(job)
        if sim_dir:
            return os.path.join(sim_dir, "messag_react")
        return None

    # ------------------------------------------------------------------
    # Background messag copier (server-side)
    # ------------------------------------------------------------------
    #
    # The Windows client must NEVER open the live LS-DYNA messag file —
    # an open() over the SMB share can block the solver's I/O and has been
    # observed to force restarts. All messag → messag_react copies therefore
    # run ON the Linux server via SSH (cp on the local parallel filesystem,
    # which LS-DYNA shares natively). `cp -p` preserves the source mtime so
    # the mtime-keyed plot parse cache stays effective.

    def _linux_sim_dir(self, job: OrderedDict) -> Optional[str]:
        """
        Linux path of the job's Simulation (or Simulation_Ret*) directory.
        Resolution uses a directory listing on the mapped drive — no file
        inside the directory is ever opened.
        """
        sim_dir_win = self.get_sim_dir(job)
        job_path = job.get("Job_Path", "")
        if not sim_dir_win or not job_path or job_path == "N/A":
            return None
        return f"{job_path}/{os.path.basename(sim_dir_win)}"

    def _ssh_copy_messag(self, server: dict, linux_sim_dir: str) -> None:
        """
        Run <source> → messag_react copy on the server for one directory.

        The source is the LS-DYNA message file: 'messag' for SMP runs,
        'mes0000' (master rank) for MPP runs. Prefer 'messag'; fall back to
        'mes0000' when it is absent. The destination name is unchanged so all
        downstream readers stay messag_react-only.
        """
        cmd = (
            f'src="{linux_sim_dir}/messag"; '
            f'[ -f "$src" ] || src="{linux_sim_dir}/mes0000"; '
            f'[ -f "$src" ] && cp -p "$src" "{linux_sim_dir}/messag_react"'
        )
        self.connect_and_execute(server, cmd)

    def ensure_messag_react(self, job: OrderedDict,
                            min_retry_interval: float = 30.0) -> Optional[str]:
        """
        Return the path to an existing messag_react, requesting a server-side
        copy when it is missing (e.g. job finished while the backend was down).

        Never opens the live messag file from the client. Attempts are
        rate-limited per Job_Path so SSE polling cannot hammer SSH.
        """
        react_path = self.get_messag_react_path(job)
        if not react_path:
            return None
        if os.path.isfile(react_path):
            return react_path

        key = job.get("Job_Path") or react_path
        now = time.time()
        if now - self._ensure_attempt_ts.get(key, 0) < min_retry_interval:
            return None
        self._ensure_attempt_ts[key] = now

        server = self._get_server_by_name(job.get("Server", ""))
        linux_sim_dir = self._linux_sim_dir(job)
        if not server or not linux_sim_dir:
            return None
        self._ssh_copy_messag(server, linux_sim_dir)
        return react_path if os.path.isfile(react_path) else None

    def ensure_messag_react_in_folder(self, windows_folder: str) -> Optional[str]:
        """
        Folder-based variant for the interim-report endpoints: windows_folder
        is the simulation directory itself (messag directly inside it).
        Returns the messag_react path or None.
        """
        react_path = os.path.join(windows_folder, "messag_react")
        if os.path.isfile(react_path):
            return react_path

        now = time.time()
        if now - self._ensure_attempt_ts.get(windows_folder, 0) < 30.0:
            return None
        self._ensure_attempt_ts[windows_folder] = now

        hostname, linux_folder = self.windows_to_linux_path(windows_folder)
        if hostname and linux_folder:
            server = self._get_server_by_hostname(hostname)
            if server:
                self._ssh_copy_messag(server, linux_folder)
        return react_path if os.path.isfile(react_path) else None

    def _copy_all_messag_files(self) -> None:
        """
        Request messag → messag_react copies for every running job, batched
        into a single SSH command per server.

        Q and E jobs are skipped: Q jobs have no messag file yet, and E jobs
        are exiting (PBS will remove them shortly). Running jobs always write
        into Simulation/, so no Simulation_Ret* resolution is needed here.
        """
        # Snapshot the list to avoid races with cache updates
        jobs = list(self._jobs_cache)
        by_server: Dict[str, List[str]] = {}
        for job in jobs:
            if job.get("Status") != "R":
                continue
            job_path = job.get("Job_Path", "")
            if job_path and job_path != "N/A":
                by_server.setdefault(job.get("Server", ""), []).append(job_path)

        for server_name, paths in by_server.items():
            server = self._get_server_by_name(server_name)
            if not server:
                continue
            dir_list = " ".join(f'"{p}/Simulation"' for p in paths)
            cmd = (
                f"for d in {dir_list}; do "
                'src="$d/messag"; [ -f "$src" ] || src="$d/mes0000"; '
                '[ -f "$src" ] && cp -p "$src" "$d/messag_react"; '
                "done"
            )
            self.connect_and_execute(server, cmd)

    def _background_copy_loop(self) -> None:
        """Daemon thread body: copy messag files periodically."""
        while True:
            try:
                self._copy_all_messag_files()
            except Exception as exc:
                logger.error("messag copier unexpected error: %s", exc)
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
    # META delegation (safe when META is not configured)
    # ------------------------------------------------------------------

    def get_d3plot_count(self, sim_dir: str) -> int:
        """Return the number of d3plot* files in sim_dir (local mapped drive, no SSH)."""
        return len(_glob.glob(os.path.join(sim_dir, "d3plot*")))

    def get_meta_status(self, job_id: str) -> dict:
        """Return current META status dict for the given job."""
        if self.meta is None:
            return {
                "configured": False,
                "meta_status": "idle",
                "meta_error": None,
                "auto_watch": False,
                "meta_generate_on_finish": False,
                "batch_running": False,
            }
        return self.meta.get_status(job_id)

    def clear_meta_state(self, job_ids: List[str]) -> None:
        """Remove in-memory META state for the given job IDs (no-op if META disabled)."""
        if self.meta is not None:
            self.meta.clear_state(job_ids)

    # ------------------------------------------------------------------
    # Messag / log access
    # ------------------------------------------------------------------

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

        cache_key = job.get("JobID", react_path)
        current_time = time.time()

        if not force_refresh:
            if cache_key in self._messag_cache:
                cached_time = self._messag_cache_time.get(cache_key, 0)
                if current_time - cached_time < self.cache_timeout:
                    return self._messag_cache[cache_key]

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
            logger.warning("Error reading messag_react: %s", e)
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
        # Never falls back to the live messag file: if messag_react is
        # missing (e.g. job ran while the backend was down), request a
        # server-side copy instead of opening messag from the client.
        read_path = self.ensure_messag_react(job)
        if not read_path:
            return None, 0

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
            logger.warning("Error reading messag_react log: %s", e)
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

    def _get_server_by_name(self, server_name: str) -> Optional[dict]:
        """Get server config by display name."""
        for server in self.servers:
            if server["name"] == server_name:
                return server
        return None

    def _get_server_by_hostname(self, hostname: str) -> Optional[dict]:
        """Get server config by hostname"""
        for server in self.servers:
            if server["hostname"] == hostname:
                return server
        return None

    # ------------------------------------------------------------------
    # Job operations (kill / delete / submit)
    # ------------------------------------------------------------------

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

        server = self._get_server_by_name(server_name)
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
    ) -> Tuple[bool, str]:
        """
        Wait for job to terminate by polling for job_log file.

        Runs in a FastAPI threadpool worker, so blocking here does not stall
        the event loop.

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

            time.sleep(poll_interval)

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

        server = self._get_server_by_name(server_name)
        if not server:
            return False, f"Server {server_name} not found"

        sim_path = f"{job_path}/Simulation"
        del_command = f"rm -rf {sim_path}"
        output = self.connect_and_execute(server, del_command)

        if output is None:
            return False, f"Failed to connect to {server_name}"

        job_log_file = f"{job_path}/job_log"
        self.connect_and_execute(server, f"rm {job_log_file}")
        job_err_file = f"{job_path}/job_error"
        self.connect_and_execute(server, f"rm {job_err_file}")

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

        server = self._get_server_by_hostname(server_hostname)
        if not server:
            return False, f"Server for {server_hostname} not found"

        # Submit the job
        command = f"cd {linux_path} && {self.qsub_path} {script_name}"
        output = self.connect_and_execute(server, command)

        if output is None:
            return False, "Failed to connect to server"

        if output.strip():
            return True, f"Job submitted: {output.strip()}"
        return True, "Job submitted successfully"

    # ------------------------------------------------------------------
    # Report generation
    # ------------------------------------------------------------------

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

        server = self._get_server_by_hostname(server_hostname)
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
    ) -> Tuple[bool, str]:
        """
        Wait for report generation to complete.

        Runs in a FastAPI threadpool worker, so blocking here does not stall
        the event loop.

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

            time.sleep(poll_interval)

    def launch_report_viewer(self, windows_path: str) -> Tuple[bool, str]:
        """
        Launch the HTML report viewer for a job

        Args:
            windows_path: Windows path to the job directory (or Linux path)

        Returns:
            Tuple of (success, message)
        """
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
                return False, "Only .cmd found but running on Linux. Please use start_server.sh"

            else:
                return False, f"No viewer script found in: {html_dir}"

        except Exception as e:
            return False, f"Failed to launch report viewer: {str(e)}"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    monitor = JobMonitor()
    jobs = monitor.fetch_jobs()

    print(f"Found {len(jobs)} jobs:")
    for job in jobs:
        print(f"  {job['JobID']} - {job['Job_Name']} - {job['Status']}")
        windows_path = monitor.get_windows_path(job)
        if windows_path:
            print(f"    Windows Path: {windows_path}")
