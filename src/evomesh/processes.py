"""Bounded POSIX process capture shared by tools and evolution validation."""

import math
import os
import selectors
import signal
import subprocess
import time
from collections.abc import Callable


def validate_seconds(value, name="timeout", *, minimum=0):
    """Reject invalid deadlines/leases before starting work or claiming SQL state."""
    valid = not isinstance(value, bool) and isinstance(value, (int, float))
    if valid:
        try:
            valid = math.isfinite(value) and value > 0 and value >= minimum
        except OverflowError:
            valid = False
    if not valid:
        suffix = f" and at least {minimum:g} seconds" if minimum else ""
        raise ValueError(f"{name} must be a finite positive number{suffix}")


class CaptureTimeout(TimeoutError):
    """Command deadline with bounded captured bytes, never output in its message."""

    def __init__(self, timeout: float, output: bytes, output_bytes: int):
        super().__init__("Command timed out")
        self.timeout = timeout
        self.output = output
        self.output_bytes = output_bytes


def capture_output(
    process: subprocess.Popen,
    timeout: float,
    *,
    tail_bytes: int = 20000,
    guard: Callable[[], None] | None = None,
    input_data: bytes | None = None,
) -> tuple[int, bytes, int]:
    """Drain output without a spool; guard failures terminate this command group.

    The caller must spawn with stdout=PIPE and start_new_session=True. Guards
    run between bounded reads/waits, including after EOF while the process lives.
    Deadlines raise CaptureTimeout with the tail and bytes captured before the
    deadline. Cleanup completes before the caller receives the exception.
    Optional input_data requires stdin=PIPE; input is written concurrently with
    output reads, then closed to signal EOF. An early reader close is allowed.
    """
    tail, size = b"", 0

    def remaining():
        if guard:
            guard()
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        return min(seconds, 0.1) if guard else seconds

    try:
        validate_seconds(timeout)
        deadline = time.monotonic() + timeout
        if type(tail_bytes) is not int or tail_bytes <= 0:
            raise ValueError("tail_bytes must be a positive integer")
        if input_data is not None and (not isinstance(input_data, bytes) or process.stdin is None):
            raise ValueError("input_data must be bytes with stdin=PIPE")
        data, sent = memoryview(input_data or b""), 0
        with process.stdout, selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            if process.stdin is not None:
                if data:
                    os.set_blocking(process.stdin.fileno(), False)
                    selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                else:
                    process.stdin.close()
            while selector.get_map():
                for key, _ in selector.select(remaining()):
                    if key.data == "stdin":
                        try:
                            sent += os.write(key.fd, data[sent : sent + 65536])
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            # A command may finish without consuming all input.
                            sent = len(data)
                        if sent == len(data):
                            selector.unregister(key.fd)
                            process.stdin.close()
                        continue
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
        raise CaptureTimeout(timeout, tail, size) from None
    finally:
        # Children can outlive the leader, including while holding output pipes.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        if process.stdout is not None:
            process.stdout.close()
        if process.stdin is not None:
            process.stdin.close()
