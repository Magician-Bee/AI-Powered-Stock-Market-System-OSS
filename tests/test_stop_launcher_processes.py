"""Exercise shutdown against real processes, isolated from desktop services."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="macOS launcher process ownership contract"
)


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("pid_record", ["owned", "stale", "missing"])
def test_stop_releases_owned_listener_and_preserves_other_copy(tmp_path, managed, pid_record):
    project = tmp_path / "desktop project"
    runtime = tmp_path / "managed runtime"
    other = tmp_path / "other project" / "service-source"
    commands = tmp_path / "commands"
    for path in (project / "scripts", project / "logs", runtime / "service-source", other, commands):
        path.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "stop-stock-ai.sh", project / "stop-stock-ai.sh")
    helper = project / "scripts" / "runtime-root.sh"
    helper.write_text(f'#!/bin/bash\nexport STOCK_AI_RUNTIME_ROOT="{runtime}"\n')
    helper.chmod(0o755)
    # Do not touch the user's launchd jobs or enumerate unrelated processes.
    for name in ("launchctl", "diskutil"):
        script = commands / name
        script.write_text("#!/bin/bash\nexit 0\n")
        script.chmod(0o755)
    worker = (
        "import socket,time; s=socket.socket(); s.bind(('127.0.0.1',0)); "
        "s.listen(); print(s.getsockname()[1],flush=True); time.sleep(60)"
    )
    processes = []
    try:
        for cwd, identity in (
            (runtime / "service-source" if managed else project, "stock_ai.main:app"),
            (other, "stock_ai.main:app"),
            (runtime / "service-source" if managed else project, "other_app:app"),
        ):
            process = subprocess.Popen(
                [sys.executable, "-c", worker, "uvicorn", identity], cwd=cwd,
                stdout=subprocess.PIPE, text=True,
            )
            processes.append(process)
        ports = [int(process.stdout.readline()) for process in processes]
        owned, unrelated, wrong_app = processes
        if pid_record == "owned":
            (project / "logs" / "stock-ai-server.pid").write_text(str(owned.pid))
        elif pid_record == "stale":
            # A stale PID pointing at another checkout must also be harmless.
            (project / "logs" / "stock-ai-server.pid").write_text(str(unrelated.pid))
        (project / "logs" / "stock-ai-server.port").write_text(str(ports[0]))
        pgrep = commands / "pgrep"
        pgrep.write_text("#!/bin/bash\nprintf '%s\\n' " + " ".join(str(p.pid) for p in processes) + "\n")
        pgrep.chmod(0o755)
        reaper = threading.Thread(target=owned.wait, daemon=True)
        reaper.start()
        result = subprocess.run(
            ["/bin/bash", str(project / "stop-stock-ai.sh")],
            env={**os.environ, "PATH": f"{commands}:{os.environ['PATH']}"},
            capture_output=True, text=True, timeout=15,
        )
        reaper.join(timeout=2)
        assert result.returncode == 0, result.stderr
        assert "Stock AI System stopped for:" in result.stdout
        assert owned.poll() is not None
        assert unrelated.poll() is None
        assert wrong_app.poll() is None
        assert not (project / "logs" / "stock-ai-server.pid").exists()
        assert not (project / "logs" / "stock-ai-server.port").exists()
        listener = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{ports[0]}", "-sTCP:LISTEN"],
            capture_output=True, text=True,
        )
        assert listener.returncode == 1, listener.stdout
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
            process.stdout.close()
