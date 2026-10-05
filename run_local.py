"""One-command local start: vendor-risk mock API + Procurement Copilot UI.

    python run_local.py            # API on :8001 + UI on :8501
    python run_local.py --no-ui    # API only
"""
from __future__ import annotations

import argparse
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=False)


def _handle_termination(signum: int, frame: object) -> None:
    """Route SIGTERM through normal cleanup (useful for IDE/terminal stop actions)."""
    raise KeyboardInterrupt


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-ui", action="store_true")
    ap.add_argument("--ui-port", type=int, default=8501)
    args = ap.parse_args()

    from src.config import get_settings
    from src.mock_service import start_mock_api, stop

    settings = get_settings()
    signal.signal(signal.SIGTERM, _handle_termination)
    procs: list[subprocess.Popen] = []
    try:
        print(f"Vendor-risk API: {settings.vendor_risk_base_url} ...")
        api = start_mock_api(settings.vendor_risk_base_url)
        print("Vendor-risk API is ready." + ("" if api else " (already running - reusing it)"))
        if api:
            procs.append(api)

        mode = (f"LLM provider: {settings.llm_provider} / {settings.model_name}" if settings.llm_enabled
                else "No LLM key found - running in deterministic fallback mode (add ANTHROPIC_API_KEY to .env)")
        print(mode)

        if not args.no_ui:
            if not _port_free(args.ui_port):
                raise RuntimeError(f"Port {args.ui_port} is already in use. Stop the other process or pass --ui-port.")
            print(f"Starting Procurement Copilot UI on http://127.0.0.1:{args.ui_port} ...")
            # Starter-pack fix: without --server.headless Streamlit blocks on a first-run e-mail prompt.
            procs.append(subprocess.Popen(
                [sys.executable, "-m", "streamlit", "run", "app.py", "--server.port", str(args.ui_port),
                 "--server.headless", "true", "--browser.gatherUsageStats", "false"], cwd=ROOT))
            print(f"\n  Open http://127.0.0.1:{args.ui_port}   (Ctrl+C to stop)\n")

        while True:
            time.sleep(1)
            for proc in procs:
                if proc.poll() is not None:
                    raise RuntimeError(f"A local process exited with code {proc.returncode}")
    except KeyboardInterrupt:
        print("\nStopping local services ...")
    except RuntimeError as exc:
        print(f"\nERROR: {exc}")
        sys.exit(1)
    finally:
        for proc in procs:
            stop(proc)


if __name__ == "__main__":
    main()
