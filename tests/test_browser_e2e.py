"""Browser end-to-end test (headless Edge/Chrome via CDP) against a throw-away server.

Opt-in because it launches a browser and a uvicorn process:
    set IDAA_E2E=1 and run pytest.
The server uses its own temporary SQLite database and working directory.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("IDAA_E2E") != "1", reason="set IDAA_E2E=1 to run the browser test")

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_browser_end_to_end():
    sys.path.insert(0, str(Path(__file__).resolve().parent / "e2e"))
    import browser_e2e

    workdir = Path(tempfile.mkdtemp(prefix="idaa_e2e_srv_"))
    db_url = f"sqlite:///{(workdir / 'e2e.db').as_posix()}"
    env = {**os.environ, "DATABASE_URL": db_url, "GEMINI_API_KEY": "", "PYTHONPATH": str(PROJECT_ROOT),
           "PYTHONDONTWRITEBYTECODE": "1"}
    subprocess.run([sys.executable, "-c", "from app.db.session import init_db; init_db()"],
                   cwd=workdir, env=env, check=True)
    port = browser_e2e.free_port()
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)],
                              cwd=workdir, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(base + "/health", timeout=1)
                break
            except OSError:
                time.sleep(0.25)
        summary = browser_e2e.run(base, workdir / "artifacts", "e2e_")
        assert summary["checks"] >= 36          # every check in browser_e2e.run executed
        assert len(summary["dataset_ids"]) == 3
    finally:
        server.terminate()
        server.wait(15)
