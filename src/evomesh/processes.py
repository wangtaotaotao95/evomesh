"""Bounded POSIX process capture shared by tools and evolution validation."""

import os
import selectors
import signal
import subprocess
import time
from collections.abc import Callable


def capture_output(
    process: subprocess.Popen,
    timeout: float,
    *,
    tail_bytes: int = 20000,
    guard: Callable[[], None] | None = None,
) -> tuple[int, bytes, int]:
    """Drain output without a spool; guard failures terminate this command group.

    The caller must spawn with stdout=PIPE and start_new_session=True. Guards
    run between bounded reads/waits, including after EOF while the process lives.
    """
    deadline = time.monotonic() + timeout
    tail, size = b"", 0

    def remaining():
        if guard:
            guard()
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        return min(seconds, 0.1) if guard else seconds

    try:
        if type(tail_bytes) is not int or tail_bytes <= 0:
            raise ValueError("tail_bytes must be a positive integer")
        with process.stdout, selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                for key, _ in selector.select(remaining()):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fd)
                    else:
                        size += len(chunk)
                        tail = (tail + chunk)[-tail_bytes:]
            while True:
                wait = remaining()
                try:
                    code = process.wait(timeout=wait)
                    break
                except subprocess.TimeoutExpired:
                    # Check the guard again even when stdout closed early.
                    if not guard:
                        raise
        return code, tail, size
    except subprocess.TimeoutExpired:
        raise TimeoutError("Command timed out") from None
    finally:
        # Children can outlive the leader, including while holding output pipes.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
