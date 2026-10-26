"""Local process interface. Environment/role guards remain with their callers.

This backend is not an OS sandbox. Commands are executed on the current host.
"""
from dataclasses import dataclass
import math
import os
from pathlib import Path
import subprocess
import time
from typing import Callable, Protocol


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    output: bytes | str
    elapsed_seconds: float


class ExecutionBackend(Protocol):
    def run_argv(self, argv: list[str], *, cwd: Path | str, timeout: float,
                 env: dict[str, str] | None = None, check: Callable = lambda: None,
                 text: bool = False) -> ProcessResult: ...


def terminate_owned_process(process):
    """Terminate only this newly created process and its Windows descendants."""
    if process.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW)
        if process.poll() is None:process.kill()


class LocalProcessBackend:
    backend_id = "windows_git_bash_v1"

    def describe(self):
        return {"id": self.backend_id, "transport": "local_subprocess", "os_isolation": False,
                "scope": "Trusted fixed tasks on this host; environment, filesystem and role policies are caller responsibilities."}

    def run_argv(self, argv, *, cwd, timeout, env=None, check=lambda: None, text=False):
        if not isinstance(argv, list) or not argv or any(not isinstance(v, str) or not v or '\0' in v for v in argv):
            raise ValueError("Expected a nonempty explicit argv list")
        if type(timeout) not in (float, int) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Expected a positive finite process timeout")
        check()
        started = time.monotonic();deadline = started + timeout
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=text, **({"encoding": "utf-8", "errors": "replace"} if text else {}),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            while True:
                check()
                try:
                    output, _ = process.communicate(timeout=min(.25, max(.001, deadline - time.monotonic())))
                    check()
                    return ProcessResult(process.returncode, output, round(time.monotonic() - started, 6))
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:raise subprocess.TimeoutExpired(argv, timeout) from None
        except BaseException as error:
            terminate_owned_process(process)
            output, _ = process.communicate(timeout=5)
            if isinstance(error, subprocess.TimeoutExpired):error.output = output
            raise


class WindowsGitBashBackend(LocalProcessBackend):
    def __init__(self, bash):self.bash = str(bash)

    def run_shell(self, command, *, cwd, env, timeout):
        try:
            result = self.run_argv([self.bash, "--noprofile", "--norc", "-c", command],
                cwd=cwd, env=env, timeout=timeout, text=True)
            return {"output": result.output, "returncode": result.returncode, "exception_info": ""}
        except subprocess.TimeoutExpired as error:
            return {"output": error.output or "", "returncode": -1, "exception_info": "Command timed out.",
                    "extra": {"exception_type": "TimeoutExpired", "exception": "Command timed out."}}
