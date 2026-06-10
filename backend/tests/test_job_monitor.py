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
# messag path cache invalidation (Simulation vs Simulation_Ret*)
# ---------------------------------------------------------------------------


def test_messag_path_cache_invalidated_when_new_simulation_appears(monitor, tmp_path):
    """
    Regression: the Job_Path-keyed cache kept pointing at Simulation_Ret*/messag
    after a new run created a fresh Simulation/, so the copier fed the old
    run's messag into the new run's messag_react.
    """
    job_dir = tmp_path / "jobA"
    ret_dir = job_dir / "Simulation_Ret1"
    ret_dir.mkdir(parents=True)
    (ret_dir / "messag").write_text("old run")

    monitor.get_windows_path = lambda job: str(job_dir)
    job = OrderedDict(JobID="1.srvA", Job_Path="/mnt/fhgfs/u/jobA", Server="srvA")

    # First resolution: only the Ret dir exists → cached
    first = monitor.get_messag_path(job)
    assert first == str(ret_dir / "messag")
    assert monitor.get_messag_path(job) == first  # cache hit

    # A new run creates a fresh Simulation/ — the stale entry must be dropped
    sim_dir = job_dir / "Simulation"
    sim_dir.mkdir()
    (sim_dir / "messag").write_text("new run")

    assert monitor.get_messag_path(job) == str(sim_dir / "messag")
    assert monitor.get_messag_react_path(job) == str(sim_dir / "messag_react")
    assert monitor.get_sim_dir(job) == str(sim_dir)


def test_messag_path_simulation_preferred_from_start(monitor, tmp_path):
    job_dir = tmp_path / "jobB"
    (job_dir / "Simulation").mkdir(parents=True)
    (job_dir / "Simulation_Ret1").mkdir()

    monitor.get_windows_path = lambda job: str(job_dir)
    job = OrderedDict(JobID="2.srvA", Job_Path="/mnt/fhgfs/u/jobB", Server="srvA")

    assert monitor.get_messag_path(job) == str(job_dir / "Simulation" / "messag")


def test_messag_path_multiple_ret_dirs_unresolvable(monitor, tmp_path):
    job_dir = tmp_path / "jobC"
    (job_dir / "Simulation_Ret1").mkdir(parents=True)
    (job_dir / "Simulation_Ret2").mkdir()

    monitor.get_windows_path = lambda job: str(job_dir)
    job = OrderedDict(JobID="3.srvA", Job_Path="/mnt/fhgfs/u/jobC", Server="srvA")

    assert monitor.get_messag_path(job) is None
    assert monitor.get_sim_dir(job) is None
