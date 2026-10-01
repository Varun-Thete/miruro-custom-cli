# ==============================================================================
# Title: Miruro API Lifecycle Manager
# Creator: Vernox
# Description: Manages the local Miruro API server lifecycle — health checks,
#              auto-spawn, and graceful shutdown.
# ==============================================================================

import sys
import time
import socket
import subprocess
from pathlib import Path

# Fix Windows console unicode issues
if sys.stdout and hasattr(sys.stdout, "reconfigure") and sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from config import cfg

# Inline color codes (matches downloader.py's C class)
_BLUE = "\033[94m"
_GREEN = "\033[92m"
_RED = "\033[91m"
_RESET = "\033[0m"

CLI_DIR = Path(__file__).resolve().parent
_api_process = None
_log_handle = None


def check_api_running() -> bool:
    """Check if the local Miruro API server is accepting TCP connections."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            return s.connect_ex(("127.0.0.1", cfg.api_port)) == 0
    except Exception:
        return False


def start_api_if_needed():
    """
    Probe the local API port and auto-spawn the uvicorn server if it's offline.
    Waits up to 15 seconds for the server to become responsive.
    """
    global _api_process, _log_handle

    if check_api_running():
        return

    print(f"{_BLUE}◆{_RESET} Local Miruro API not running. Starting on port {cfg.api_port}...")

    # Locate the Miruro-API source directory
    api_dir = CLI_DIR / "Miruro-API"
    if not api_dir.exists():
        api_dir = CLI_DIR.parent / "Miruro-API"

    if not api_dir.exists():
        raise FileNotFoundError(f"Miruro-API directory not found: {api_dir}")

    # Spawn the uvicorn process
    _log_handle = open(CLI_DIR / "api.log", "w")

    # Use CREATE_NEW_PROCESS_GROUP on Windows for clean separation
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    _api_process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api:app", "--port", str(cfg.api_port)],
        cwd=str(api_dir),
        stdout=_log_handle,
        stderr=subprocess.STDOUT,
        **kwargs,
    )

    # Wait for the server to become responsive
    for attempt in range(15):
        time.sleep(1)
        if check_api_running():
            print(f"{_GREEN}✔{_RESET} API started successfully on {cfg.api_base}")
            return

    # Clean up the failed process and file handle before raising
    shutdown_api()
    raise TimeoutError("API server failed to start within 15 seconds.")


def shutdown_api():
    """Gracefully terminate the API server if we spawned it."""
    global _api_process, _log_handle
    if _api_process is not None:
        try:
            _api_process.terminate()
            _api_process.wait(timeout=5)
        except Exception:
            try:
                _api_process.kill()
            except Exception:
                pass
        _api_process = None

    if _log_handle is not None:
        try:
            _log_handle.close()
        except Exception:
            pass
        _log_handle = None
