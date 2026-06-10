"""Shared fixtures for the backend test suite."""

import gzip
import sys
from pathlib import Path

import pytest

# Make the backend package importable when pytest runs from the repo root
BACKEND_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BACKEND_DIR))

DATA_DIR = Path(__file__).parent / "data"


@pytest.fixture(scope="session")
def messag_content() -> str:
    """Real LS-DYNA implicit messag file (28 steps), stored gzipped."""
    with gzip.open(DATA_DIR / "messag_sample.gz", "rt", encoding="utf-8", errors="ignore") as f:
        return f.read()


@pytest.fixture
def monitor_config(tmp_path):
    """Minimal config.yaml for an offline JobMonitor instance."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
pbs:
  qdel_path: /opt/pbs/bin/qdel
  qsub_path: /opt/pbs/bin/qsub
paths:
  linux_base_path: /mnt/fhgfs
  remote_script_name: que.py
drive_mapping:
  X: 10.17.142.200
  Y: 10.17.160.231
servers:
  - hostname: 10.17.142.200
    name: srvA
  - hostname: 10.17.160.231
    name: srvB
dashboard:
  cache_timeout: 20
  messag_copy_interval: 3600
"""
    )
    return str(cfg)


@pytest.fixture
def monitor(monitor_config):
    from job_monitor import JobMonitor

    return JobMonitor(config_path=monitor_config)
