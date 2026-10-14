"""Supervised external processes: concurrent pipe draining, cancellation, timeouts and
tree-kill on abort.

The failure modes this exists for are all real:

* Reading ``stdout`` to EOF before touching ``stderr`` deadlocks the child once the stderr pipe
  buffer fills (both pipes are drained on threads here).
* Checking ``cancel()`` only when a line arrives means a stalled ffmpeg can never be cancelled
  (the poll loop checks it whether output is flowing or not).
* ``proc.kill()`` on Windows kills the launcher but not its children - ``soffice.exe`` hands the
  real work to ``soffice.bin`` - so aborts kill the whole tree via ``taskkill /T``.
* An app crash or a hard ``QThread.terminate`` leaves encoders running for hours; on Windows
  every child is attached to a kill-on-close Job object so it dies with the process.
"""

from __future__ import annotations

import contextlib
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TextIO

from slidecast.core.logging_setup import get_logger

log = get_logger("proc")

CancelFn = Callable[[], bool]
LineFn = Callable[[str], None]

_EOF = object()
_STDOUT_JOIN_LIMIT = 10.0  # how long to wait for the reader thread after the child exits


@dataclass
class ProcResult:
    returncode: int
    stdout: str
    stderr: str
    cancelled: bool = False
    timed_out: bool = False  # hard ``timeout`` exceeded
    stalled: bool = False  # no stdout output for ``stall_timeout``


def _pump_lines(stream: TextIO, out: queue.Queue[object]) -> None:
    try:
        for line in iter(stream.readline, ""):
            out.put(line)
    except (OSError, ValueError):
        pass
    finally:
        out.put(_EOF)


def _pump_text(stream: TextIO, buf: list[str]) -> None:
    with contextlib.suppress(OSError, ValueError):
        buf.append(stream.read())


def terminate_tree(proc: subprocess.Popen[str]) -> None:
    """Kill the process *and its children*, then reap it. Best effort; never raises."""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            with contextlib.suppress(Exception):
                proc.kill()
    else:
        try:
            os.killpg(os.getpgid(proc.pid), 9)  # SIGKILL
        except Exception:
            with contextlib.suppress(Exception):
                proc.kill()
    try:
        proc.wait(timeout=10)
    except Exception:
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)


# -- Windows kill-on-close job -----------------------------------------------------------------

_JOB: Any = None
_JOB_PROBED = False


def _kill_on_close_job() -> Any:
    """A Job object configured so every assigned process dies when this process exits.

    Even a hard crash or a ``QThread.terminate`` then cannot leave an ffmpeg or LibreOffice
    grinding away invisibly. Returns ``None`` on non-Windows platforms or if setup fails - the
    app still works, it just loses the safety net.
    """
    global _JOB, _JOB_PROBED
    if _JOB_PROBED:
        return _JOB
    _JOB_PROBED = True
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                (n, ctypes.c_ulonglong)
                for n in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class _ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimit),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(
            job, 9, ctypes.byref(info), ctypes.sizeof(info)
        ):  # 9 = JobObjectExtendedLimitInformation
            k32.CloseHandle(job)
            return None
        _JOB = job
    except Exception as exc:  # pragma: no cover - platform dependent
        log.debug("Kill-on-close job object unavailable: %s", exc)
    return _JOB


def _attach_to_kill_job(proc: subprocess.Popen[str]) -> None:
    job = _kill_on_close_job()
    if job is None:
        return
    try:
        import ctypes

        handle = getattr(proc, "_handle", None)  # process handle (Windows only)
        if handle is not None:
            # Children spawned later by this process join the job automatically, so
            # soffice.exe -> soffice.bin is covered.
            ctypes.WinDLL("kernel32", use_last_error=True).AssignProcessToJobObject(job, handle)
    except Exception as exc:  # pragma: no cover - platform dependent
        log.debug("Could not attach pid %s to kill job: %s", proc.pid, exc)


# -- the runner ---------------------------------------------------------------------------------


def run(
    cmd: list[str],
    *,
    timeout: float | None = None,
    stall_timeout: float | None = None,
    cancel: CancelFn | None = None,
    on_stdout: LineFn | None = None,
    merge_stderr: bool = False,
    poll_interval: float = 0.1,
) -> ProcResult:
    """Run ``cmd`` with both pipes drained, honouring cancel and timeouts.

    * ``timeout`` - hard wall-clock limit; the process tree is killed when it expires.
    * ``stall_timeout`` - kills the process when stdout has been silent for that long (an
      encoder that stops reporting progress is dead even if it has not crashed).
    * ``cancel`` - polled every ``poll_interval`` seconds; fires whether output flows or not.
    * ``on_stdout`` - called with each stdout line on the *calling* thread.
    * ``merge_stderr`` - fold stderr into stdout (for tools whose diagnostics go to either).

    Never raises for a child that ran: the outcome is reported on :class:`ProcResult`.
    ``OSError`` from ``Popen`` (missing binary, etc.) still propagates.
    """
    popen_kwargs: dict[str, Any] = {}
    if sys.platform != "win32":
        popen_kwargs["start_new_session"] = True  # own process group for killpg
    else:
        # A compiled build has no console of its own, so every child would otherwise flash one
        # up - hundreds of them over a render. Both pipes are captured either way.
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **popen_kwargs,
    )
    _attach_to_kill_job(proc)

    assert proc.stdout is not None
    lines: queue.Queue[object] = queue.Queue()
    stdout_buf: list[str] = []

    def _take(item: str) -> None:
        stdout_buf.append(item)
        if on_stdout is not None:
            on_stdout(item)

    stdout_thread = threading.Thread(target=_pump_lines, args=(proc.stdout, lines), daemon=True)
    stdout_thread.start()
    stderr_buf: list[str] = []
    stderr_thread: threading.Thread | None = None
    if not merge_stderr:
        assert proc.stderr is not None
        stderr_thread = threading.Thread(
            target=_pump_text, args=(proc.stderr, stderr_buf), daemon=True
        )
        stderr_thread.start()

    started = last_output = time.monotonic()
    cancelled = timed_out = stalled = False
    stdout_open = True
    give_up_drain_at: float | None = None
    try:
        while True:
            try:
                item = lines.get(timeout=poll_interval)
            except queue.Empty:
                item = None
            if item is _EOF:
                stdout_open = False
            elif item is not None:
                last_output = time.monotonic()
                _take(str(item))
            now = time.monotonic()
            rc = proc.poll()
            if rc is not None:
                if not stdout_open:
                    break
                # Exited but the reader is still pumping; wait a little, then take what we have.
                if give_up_drain_at is None:
                    give_up_drain_at = now + _STDOUT_JOIN_LIMIT
                elif now >= give_up_drain_at:
                    break
                continue
            if cancel is not None and cancel():
                cancelled = True
                break
            if timeout is not None and now - started >= timeout:
                timed_out = True
                break
            if stall_timeout is not None and now - last_output >= stall_timeout:
                stalled = True
                break
    finally:
        if cancelled or timed_out or stalled:
            terminate_tree(proc)
        if proc.poll() is None:
            proc.wait()
        stdout_thread.join(timeout=2.0)
        if stderr_thread is not None:
            stderr_thread.join(timeout=2.0)

    # Deliver whatever the reader captured before we stopped listening.
    try:
        while True:
            item = lines.get_nowait()
            if item is not _EOF:
                _take(str(item))
    except queue.Empty:
        pass

    rc = proc.returncode if proc.returncode is not None else -1
    return ProcResult(
        returncode=rc,
        stdout="".join(stdout_buf),
        stderr="".join(stderr_buf),
        cancelled=cancelled,
        timed_out=timed_out,
        stalled=stalled,
    )
