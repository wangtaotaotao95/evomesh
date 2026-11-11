import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import tracemalloc

import pytest

from evomesh.capabilities import execute_code_tool, terminal_tool


@pytest.mark.parametrize("kind", ["terminal", "execute_code"])
def test_large_output_uses_bounded_memory_without_disk_spool(tmp_path, monkeypatch, kind):
    def no_spool(*args, **kwargs):
        pytest.fail("Command output must not be spooled to disk")

    monkeypatch.setattr("evomesh.capabilities.tempfile.TemporaryFile", no_spool)
    code = (
        "import os\n"
        "for _ in range(100):\n"
        "    os.write(1, b'x' * 65536)\n"
        "os.write(2, b'FINAL_STDERR\\n')\n"
    )
    tool = terminal_tool(tmp_path) if kind == "terminal" else execute_code_tool(tmp_path)
    arguments = {"command": [sys.executable, "-c", code]} if kind == "terminal" else {"code": code}
    tracemalloc.start()
    try:
        result = json.loads(tool.invoke(json.dumps(arguments)))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2_000_000  # Much smaller than the 6.5 MB emitted by the child.
    assert result["exit_code"] == 0
    assert result["output"].endswith("FINAL_STDERR\n")
    assert result["output_bytes"] == 100 * 65536 + len(b"FINAL_STDERR\n")
    assert result["truncated"] is True


@pytest.mark.parametrize(
    "code",
    [
        "import time; time.sleep(30)",
        "import os\nwhile True: os.write(1, b'x' * 65536)",
        "import os, time; os.close(1); os.close(2); time.sleep(30)",
    ],
    ids=["quiet", "noisy", "closed-output"],
)
def test_capture_timeout_reaps_the_command(tmp_path, monkeypatch, code):
    real_popen, processes = subprocess.Popen, []

    def tracked_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr("evomesh.capabilities.subprocess.Popen", tracked_popen)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        execute_code_tool(tmp_path, timeout=0.2).invoke(json.dumps({"code": code}))
    assert time.monotonic() - started < 5
    assert len(processes) == 1 and processes[0].returncode is not None
    assert processes[0].stdout.closed


@pytest.mark.parametrize("holds_output", [True, False])
def test_descendants_are_cleaned_after_leader_exit(tmp_path, holds_output):
    lock = tmp_path / "child.lock"
    ready = tmp_path / "child.ready"
    child = (
        "import fcntl, os, time; from pathlib import Path; "
        f"f = open({str(lock)!r}, 'w'); fcntl.flock(f, fcntl.LOCK_EX); "
        f"Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(30)"
    )
    redirection = "" if holds_output else ", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL"
    parent = (
        "import subprocess, sys, time; from pathlib import Path\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}]{redirection})\n"
        f"while not Path({str(ready)!r}).exists(): time.sleep(0.01)\n"
    )
    try:
        tool = execute_code_tool(tmp_path, timeout=2)
        if holds_output:
            with pytest.raises(TimeoutError):
                tool.invoke(json.dumps({"code": parent}))
        else:
            assert json.loads(tool.invoke(json.dumps({"code": parent})))["exit_code"] == 0
        assert ready.exists(), "Fixture child never started"
        # A dead orphan may briefly remain a zombie; released locks demonstrate
        # it no longer runs, without depending on OS-specific reaping timing.
        deadline = time.monotonic() + 2
        with lock.open("w") as stream:
            while True:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        pytest.fail("Descendant still holds its file lock after invocation")
                    time.sleep(0.01)
    finally:
        if ready.exists():
            try:
                os.kill(int(ready.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
