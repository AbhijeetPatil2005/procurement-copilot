"""Start (or reuse) the local mock vendor-risk API.

Used by run_local.py and the public eval runner so results never silently
degrade to "vendor-risk unavailable" just because the service wasn't started.
"""
from __future__ import annotations

import atexit
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

import requests

from src.config import get_settings

ROOT = Path(__file__).resolve().parents[1]


def is_healthy(base_url: str, timeout: float = 0.5) -> bool:
    try:
        r = requests.get(f"{base_url}/health", timeout=timeout)
        return r.ok and r.json().get("status") == "ok"
    except (requests.RequestException, ValueError):
        return False


def start_mock_api(base_url: str | None = None, timeout_s: float = 15.0) -> subprocess.Popen | None:
    """Return a Popen if we started the service, None if it was already running."""
    base_url = base_url or get_settings().vendor_risk_base_url
    if is_healthy(base_url):
        return None
    parsed = urlparse(base_url)
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise RuntimeError(f"Vendor-risk API at {base_url} is not reachable and is not local, so it cannot be auto-started.")
    port = str(parsed.port or 8001)
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "mock_api.app:app", "--host", "127.0.0.1", "--port", port,
                             "--log-level", "warning"], cwd=ROOT)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Vendor-risk API exited during startup (code {proc.returncode}); is port {port} in use?")
        if is_healthy(base_url):
            return proc
        time.sleep(0.25)
    proc.terminate()
    raise RuntimeError(f"Vendor-risk API did not become healthy within {timeout_s:.0f}s at {base_url}")


def stop(proc: subprocess.Popen | None) -> None:
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@contextmanager
def mock_api_running():
    proc = start_mock_api()
    atexit.register(stop, proc)
    try:
        yield
    finally:
        stop(proc)
