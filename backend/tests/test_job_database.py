"""JobDatabase persistence and state-transition tests."""

from job_database import JobDatabase


def make_job(job_id="1.srvA", status="R", **over):
    job = {
        "JobID": job_id,
        "Server": "srvA",
        "Job_Name": "case01",
        "Job_Path": "/mnt/fhgfs/user/proj/case01",
        "Owner": "user",
        "CPUs": "16",
        "Status": status,
        "Memory": "64gb",
    }
    job.update(over)
    return job


def test_upsert_and_get(tmp_path):
    db = JobDatabase(str(tmp_path / "db.json"))
    db.upsert(make_job())
    entry = db.get("1.srvA")
    assert entry["Status"] == "R"
    assert entry["finished_at"] is None
    assert entry["meta_status"] == "idle"


def test_persistence_across_instances(tmp_path):
    path = str(tmp_path / "db.json")
    JobDatabase(path).upsert(make_job())
    db2 = JobDatabase(path)
    assert db2.get("1.srvA")["Job_Name"] == "case01"


def test_mark_finished_and_reappear_clears_finished_at(tmp_path):
    db = JobDatabase(str(tmp_path / "db.json"))
    db.upsert(make_job())
    db.mark_finished("1.srvA")
    assert db.get("1.srvA")["Status"] == "F"
    assert db.get("1.srvA")["finished_at"] is not None

    # The job shows up live again (e.g. it was wrongly marked finished
    # during a transient SSH failure) — finished_at must be cleared.
    db.upsert(make_job(status="R"))
    entry = db.get("1.srvA")
    assert entry["Status"] == "R"
    assert entry["finished_at"] is None


def test_upsert_preserves_meta_fields(tmp_path):
    db = JobDatabase(str(tmp_path / "db.json"))
    db.upsert(make_job())
    db.set_meta_generate_on_finish("1.srvA", True)
    db.set_meta_status("1.srvA", "generating")

    db.upsert(make_job(status="R", CPUs="32"))
    entry = db.get("1.srvA")
    assert entry["meta_generate_on_finish"] is True
    assert entry["meta_status"] == "generating"
    assert entry["CPUs"] == "32"


def test_get_active_and_finished(tmp_path):
    db = JobDatabase(str(tmp_path / "db.json"))
    db.upsert_batch([make_job("1.srvA", "R"), make_job("2.srvA", "Q"), make_job("3.srvA", "R")])
    db.mark_finished("3.srvA")
    assert {j["JobID"] for j in db.get_active()} == {"1.srvA", "2.srvA"}
    assert {j["JobID"] for j in db.get_finished()} == {"3.srvA"}


def test_delete_only_removes_finished(tmp_path):
    db = JobDatabase(str(tmp_path / "db.json"))
    db.upsert(make_job("1.srvA", "R"))
    db.upsert(make_job("2.srvA", "R"))
    db.mark_finished("2.srvA")

    assert db.delete("1.srvA") is False  # still active
    assert db.delete("2.srvA") is True
    assert db.delete("missing") is False
    assert db.get("2.srvA") is None


def test_delete_all_finished(tmp_path):
    db = JobDatabase(str(tmp_path / "db.json"))
    db.upsert_batch([make_job("1.srvA"), make_job("2.srvA")])
    db.mark_finished("1.srvA")
    db.mark_finished("2.srvA")
    assert db.delete_all_finished() == 2
    assert db.get_finished() == []
