"""JobMonitor tests: path mapping, fetch concurrency, F-detection, path cache."""

import json
import threading
import time
from collections import OrderedDict
from getpass import getuser


def fake_pbs_output(jobs):
    """Build the JSON string que.py would print for the given job tuples."""
    return json.dumps([
        {
            "JobID": jid,
            "Job_Name": name,
            "Job_Path": f"/mnt/fhgfs/{getuser()}/proj/{name}",
            "CPUs": 16,
            "Status": status,
            "Owner": getuser(),
            "Memory": "64gb",
        }
        for jid, name, status in jobs
    ])


# ---------------------------------------------------------------------------
# Path conversion
# ---------------------------------------------------------------------------


def test_linux_to_windows_path(monitor):
    user = getuser()
    wp, host = monitor.linux_to_windows_path(f"/mnt/fhgfs/{user}/proj/jobA", "10.17.142.200")
    assert wp == r"X:\proj\jobA"
    assert host == "10.17.142.200"

    wp, host = monitor.linux_to_windows_path(f"/mnt/fhgfs/{user}/proj/jobA", "10.17.160.231")
    assert wp == r"Y:\proj\jobA"


def test_linux_to_windows_path_outside_base(monitor):
    assert monitor.linux_to_windows_path("/somewhere/else", "10.17.142.200") == (None, None)


def test_get_windows_path_uses_job_server(monitor):
    user = getuser()
    job = OrderedDict(
        Server="srvB",
        JobID="9.srvB",
        Job_Path=f"/mnt/fhgfs/{user}/proj/jobB",
    )
    assert monitor.get_windows_path(job) == r"Y:\proj\jobB"


# ---------------------------------------------------------------------------
# fetch_jobs: concurrency and finished-job detection
# ---------------------------------------------------------------------------


def test_concurrent_fetch_no_duplicates(monitor):
    """
    Regression: concurrent fetch_jobs calls used to rebind self.all_jobs
    mid-build, appending one server's jobs twice and showing duplicate rows
    in the frontend.
    """
    def slow_execute(server, command):
        time.sleep(0.05)  # widen the race window
        if server["name"] == "srvA":
            return fake_pbs_output([("1.srvA", "caseA", "R"), ("2.srvA", "caseB", "Q")])
        return fake_pbs_output([("3.srvB", "caseC", "R")])

    monitor.connect_and_execute = slow_execute

    results = []
    threads = [threading.Thread(target=lambda: results.append(monitor.fetch_jobs()))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for jobs in results:
        ids = [j["JobID"] for j in jobs]
        assert len(ids) == 3
        assert len(set(ids)) == 3


def test_server_outage_does_not_mark_finished(monitor):
    """
    Regression: a transient SSH failure made all of that server's jobs vanish
    from the live list, wrongly marking them finished.
    """
    responses = {
        "srvA": fake_pbs_output([("1.srvA", "caseA", "R")]),
        "srvB": fake_pbs_output([("2.srvB", "caseB", "R")]),
    }

    monitor.connect_and_execute = lambda server, cmd: responses[server["name"]]
    monitor.fetch_jobs()
    assert monitor.job_db.get("2.srvB")["Status"] == "R"

    # srvB SSH fails — its job must stay active, not become F
    responses["srvB"] = None
    monitor.fetch_jobs()
    assert monitor.job_db.get("2.srvB")["Status"] == "R"
    assert monitor.job_db.get("2.srvB")["finished_at"] is None

    # srvB responds with corrupt JSON — same protection applies
    responses["srvB"] = "qstat: error\n"
    monitor.fetch_jobs()
    assert monitor.job_db.get("2.srvB")["Status"] == "R"

    # srvB responds with an empty queue — NOW the job is finished
    responses["srvB"] = "[]"
    monitor.fetch_jobs()
    assert monitor.job_db.get("2.srvB")["Status"] == "F"
    assert monitor.job_db.get("2.srvB")["finished_at"] is not None

    # The job reappears (requeued) — finished marker must clear
    responses["srvB"] = fake_pbs_output([("2.srvB", "caseB", "R")])
    monitor.fetch_jobs()
    assert monitor.job_db.get("2.srvB")["Status"] == "R"
    assert monitor.job_db.get("2.srvB")["finished_at"] is None


def test_get_jobs_cached_ttl_and_force(monitor):
    calls = {"n": 0}

    def counting_execute(server, command):
        if server["name"] == "srvA":
            calls["n"] += 1
            return fake_pbs_output([("1.srvA", "caseA", "R")])
        return "[]"

    monitor.connect_and_execute = counting_execute

    monitor.get_jobs_cached()
    monitor.get_jobs_cached()  # within TTL — served from cache
    assert calls["n"] == 1

    monitor.get_jobs_cached(force=True)
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# Simulation dir resolution (Simulation vs Simulation_Ret*)
# ---------------------------------------------------------------------------


def test_sim_dir_simulation_preferred_over_ret(monitor, tmp_path):
    """A new run's Simulation/ must win over a leftover Simulation_Ret*/."""
    job_dir = tmp_path / "jobA"
    ret_dir = job_dir / "Simulation_Ret1"
    ret_dir.mkdir(parents=True)

    monitor.get_windows_path = lambda job: str(job_dir)
    job = OrderedDict(JobID="1.srvA", Job_Path="/mnt/fhgfs/u/jobA", Server="srvA")

    # Only the Ret dir exists → resolves there
    assert monitor.get_sim_dir(job) == str(ret_dir)
    assert monitor.get_messag_react_path(job) == str(ret_dir / "messag_react")

    # A new run creates a fresh Simulation/ — resolution must switch over
    sim_dir = job_dir / "Simulation"
    sim_dir.mkdir()
    assert monitor.get_sim_dir(job) == str(sim_dir)
    assert monitor.get_messag_react_path(job) == str(sim_dir / "messag_react")
    assert monitor._linux_sim_dir(job) == "/mnt/fhgfs/u/jobA/Simulation"


def test_sim_dir_multiple_ret_dirs_unresolvable(monitor, tmp_path):
    job_dir = tmp_path / "jobC"
    (job_dir / "Simulation_Ret1").mkdir(parents=True)
    (job_dir / "Simulation_Ret2").mkdir()

    monitor.get_windows_path = lambda job: str(job_dir)
    job = OrderedDict(JobID="3.srvA", Job_Path="/mnt/fhgfs/u/jobC", Server="srvA")

    assert monitor.get_sim_dir(job) is None
    assert monitor._linux_sim_dir(job) is None


# ---------------------------------------------------------------------------
# Server-side messag copying — the client must never open the live messag
# ---------------------------------------------------------------------------


def test_copier_runs_server_side_batched(monitor):
    """
    The periodic copier must issue one SSH cp command per server covering all
    R jobs, and must not open any file on the client.
    """
    sent = []
    monitor.connect_and_execute = lambda server, cmd: sent.append((server["name"], cmd)) or ""
    monitor._jobs_cache = [
        OrderedDict(Server="srvA", JobID="1.srvA", Status="R", Job_Path="/mnt/fhgfs/u/a"),
        OrderedDict(Server="srvA", JobID="2.srvA", Status="R", Job_Path="/mnt/fhgfs/u/b"),
        OrderedDict(Server="srvB", JobID="3.srvB", Status="Q", Job_Path="/mnt/fhgfs/u/c"),
        OrderedDict(Server="srvB", JobID="4.srvB", Status="R", Job_Path="/mnt/fhgfs/u/d"),
    ]

    monitor._copy_all_messag_files()

    assert sorted(name for name, _ in sent) == ["srvA", "srvB"]
    cmd_a = next(cmd for name, cmd in sent if name == "srvA")
    assert '"/mnt/fhgfs/u/a/Simulation"' in cmd_a
    assert '"/mnt/fhgfs/u/b/Simulation"' in cmd_a
    assert "cp -p" in cmd_a and "messag_react" in cmd_a
    cmd_b = next(cmd for name, cmd in sent if name == "srvB")
    assert '"/mnt/fhgfs/u/d/Simulation"' in cmd_b
    assert "/mnt/fhgfs/u/c" not in cmd_b  # Q job skipped


def test_ensure_messag_react_requests_server_copy(monitor, tmp_path):
    job_dir = tmp_path / "jobX"
    sim_dir = job_dir / "Simulation"
    sim_dir.mkdir(parents=True)

    monitor.get_windows_path = lambda job: str(job_dir)
    calls = []

    def fake_exec(server, cmd):
        calls.append((server["name"], cmd))
        # Simulate the server-side cp landing on the shared filesystem
        (sim_dir / "messag_react").write_text("data")
        return ""

    monitor.connect_and_execute = fake_exec
    job = OrderedDict(JobID="9.srvA", Server="srvA", Job_Path="/mnt/fhgfs/u/jobX")

    path = monitor.ensure_messag_react(job)
    assert path == str(sim_dir / "messag_react")
    assert len(calls) == 1
    assert calls[0][0] == "srvA"
    assert 'cp -p "/mnt/fhgfs/u/jobX/Simulation/messag"' in calls[0][1]

    # File now exists → no further SSH on subsequent calls
    assert monitor.ensure_messag_react(job) == path
    assert len(calls) == 1


def test_ensure_messag_react_rate_limited(monitor, tmp_path):
    """When the server has no messag either, retries are rate-limited."""
    job_dir = tmp_path / "jobY"
    (job_dir / "Simulation").mkdir(parents=True)

    monitor.get_windows_path = lambda job: str(job_dir)
    calls = []
    monitor.connect_and_execute = lambda server, cmd: calls.append(cmd) or ""
    job = OrderedDict(JobID="10.srvA", Server="srvA", Job_Path="/mnt/fhgfs/u/jobY")

    assert monitor.ensure_messag_react(job) is None
    assert monitor.ensure_messag_react(job) is None  # within retry interval
    assert len(calls) == 1


def test_get_log_content_never_opens_live_messag(monitor, tmp_path):
    """With messag_react absent and the server copy failing, the log read
    must return empty — it must not fall back to opening messag."""
    job_dir = tmp_path / "jobZ"
    sim_dir = job_dir / "Simulation"
    sim_dir.mkdir(parents=True)
    (sim_dir / "messag").write_text("live solver output — must not be read")

    monitor.get_windows_path = lambda job: str(job_dir)
    monitor.connect_and_execute = lambda server, cmd: ""  # copy "fails" silently
    job = OrderedDict(JobID="11.srvA", Server="srvA", Job_Path="/mnt/fhgfs/u/jobZ")

    content, size = monitor.get_log_content(job)
    assert content is None
    assert size == 0

    # Once messag_react exists, the tail works again
    (sim_dir / "messag_react").write_text("line1\nline2\n")
    monitor._ensure_attempt_ts.clear()
    content, size = monitor.get_log_content(job)
    assert content == "line1\nline2\n"
