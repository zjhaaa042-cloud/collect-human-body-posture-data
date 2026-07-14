"""Run backend and Vite as one application lifecycle."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"


def stop_process_tree(process: subprocess.Popen):
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def main() -> int:
    env = os.environ.copy()
    env["BODY_COLLECTOR_SHUTDOWN_WHEN_IDLE"] = "1"
    env["PYTHONUTF8"] = "1"
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    start_new_session = os.name != "nt"

    backend = subprocess.Popen(
        [sys.executable, str(ROOT / "run_backend.py")],
        cwd=ROOT,
        env=env,
        creationflags=creationflags,
        start_new_session=start_new_session,
    )
    frontend = None
    try:
        # Give the backend a moment to bind its port before opening the browser.
        time.sleep(1.0)
        if backend.poll() is not None:
            return backend.returncode or 1

        npm = "npm.cmd" if os.name == "nt" else "npm"
        frontend = subprocess.Popen(
            [npm, "start"],
            cwd=FRONTEND,
            env=env,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )

        while True:
            backend_code = backend.poll()
            frontend_code = frontend.poll()
            if backend_code is not None:
                return backend_code
            if frontend_code is not None:
                return frontend_code
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        stop_process_tree(frontend)
        stop_process_tree(backend)


if __name__ == "__main__":
    raise SystemExit(main())
