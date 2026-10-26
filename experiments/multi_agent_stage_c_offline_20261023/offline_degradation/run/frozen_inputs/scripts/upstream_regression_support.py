"""Run unchanged upstream assertions with retained scratch and no network."""
import inspect
from collections import Counter
import json
import os
from pathlib import Path
import sys
import tempfile


def install_guards(scratch: Path):
    scratch = scratch.resolve()
    original = tempfile.TemporaryDirectory

    class RetainedTemporaryDirectory(original):
        def cleanup(self):
            self._finalizer.detach()

        @classmethod
        def _cleanup(cls, name, **kwargs):
            pass  # Also retain directories whose owners are garbage collected.

    # Keep upstream context-manager scratch instead of recursively deleting it.
    # Assertions and all minisweagent classes stay unchanged.
    tempfile.TemporaryDirectory = RetainedTemporaryDirectory
    tempfile.tempdir = str(scratch)
    violations = []

    def audit(event, args):
        if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo", "shutil.rmtree", "os.rmdir"}:
            violations.append({"event": event})
            raise PermissionError("Offline regression forbids network and recursive cleanup: " + event)
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0])).resolve()
            if path == Path(os.devnull).resolve():
                return
            mode, flags = args[1], args[2]
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
            if (writing or path.name.startswith(".env")) and not path.is_relative_to(scratch.parent):
                violations.append({"event": "write_or_private_read_outside_scratch"})
                raise PermissionError("Upstream regression may only write/read private config inside its scratch")

    sys.addaudithook(audit)
    return violations


class UpstreamResults:
    def __init__(self, configuration, output, profile):
        self.configuration, self.output = configuration, Path(output)
        self.profile = profile
        self.collected, self.deselected, self.reports = [], [], []

    def pytest_collection_modifyitems(self, session, config, items):
        selected, dropped = [], []
        for item in items:
            reason = self.configuration["excluded_nodes"].get(item.nodeid.split("[", 1)[0]) if self.profile != "audit" else None
            if self.profile == "audit" and item.nodeid.startswith(("tests/run/test_local.py::", "tests/run/test_run_hello_world.py::")):
                reason = "Recorded trajectory requires POSIX shell and may write outside fixture directories."
            if not reason and inspect.iscoroutinefunction(item.obj):
                reason = "Async inspector tests require optional pytest-asyncio; not installed in current environment."
            if not reason and (item.get_closest_marker("slow") or "container_executable" in item.fixturenames):
                reason = "Upstream slow/container integration; excluded before fixture execution and downloads."
            if reason:
                dropped.append(item)
                self.deselected.append({"nodeid": item.nodeid, "reason": reason})
            else:
                selected.append(item)
        self.collected = [item.nodeid for item in selected]
        config.hook.pytest_deselected(items=dropped)
        items[:] = selected

        if self.profile == "adapter":
            from run_deepseek_smoke import GitBashEnvironment
            from minisweagent.environments.local import LocalEnvironmentConfig

            class AdapterUnderTest(GitBashEnvironment):
                def __init__(self, **kwargs):
                    config_class = kwargs.pop("config_class", LocalEnvironmentConfig)
                    config = config_class(**kwargs)
                    config.cwd = config.cwd or os.getcwd()
                    super().__init__(cwd=config.cwd, bash=os.environ["MINI_TASK_BASH"])
                    self.config = config

            for module in {item.module for item in selected}:
                # Deliberate adapter substitution, not an upstream-native result.
                # Reuse original test functions and real subprocess execution.
                module.LocalEnvironment = AdapterUnderTest

    def pytest_runtest_logreport(self, report):
        if report.when == "call" or report.failed or report.skipped:
            self.reports.append({"nodeid": report.nodeid, "phase": report.when, "outcome": report.outcome,
                                 "detail": str(report.longrepr) if report.failed or report.skipped else None})

    def save(self, exit_code, violations):
        data = {"profile": self.profile, "exit_code": int(exit_code), "selected": self.collected, "deselected": self.deselected,
                "counts": dict(Counter(row["outcome"] if row["phase"] == "call" or row["outcome"] == "skipped" else "error" for row in self.reports)),
                "reports": self.reports, "guard_violations": violations}
        self.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
