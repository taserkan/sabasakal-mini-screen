"""Find and stop ping boosters / game VPNs left running in the background.

Tools such as ExitLag or NoPing keep a Windows service (and sometimes a
packet filter in the network stack) running after their window is closed;
that can add jitter or packet loss to a game played *without* them. This
module only:

* lists running processes and services whose name or executable path
  matches a known booster name, and
* on request terminates those processes and stops those services.

It never touches drivers, network adapters, routes, DNS or the game.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Iterable

import psutil

# Known ping boosters / game VPN clients (matched case-insensitively against
# process names, executable paths, service names and service binaries).
BOOSTER_NAMES = (
    "exitlag", "noping", "wtfast", "mudfish", "gearup", "lagofast", "hasteclient",
    "outfox", "pingzapper", "battleping", "speedify",
)
_PATTERN = re.compile("|".join(re.escape(name) for name in BOOSTER_NAMES), re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    kind: str  # "process" | "service"
    name: str  # process name or service name
    label: str  # what to show the user
    pid: int | None = None


def _matches(*values: object) -> bool:
    return any(value and _PATTERN.search(str(value)) for value in values)


def scan(
    processes: Callable[[], Iterable] = lambda: psutil.process_iter(["pid", "name", "exe"]),
    services: Callable[[], Iterable] = lambda: psutil.win_service_iter(),
) -> list[Finding]:
    """Running booster processes and services (nothing is changed)."""
    found: list[Finding] = []
    for process in processes():
        try:
            info = process.info
            if _matches(info.get("name"), info.get("exe")):
                found.append(Finding("process", str(info.get("name") or ""), str(info.get("name") or ""),
                                     int(info.get("pid") or 0)))
        except (psutil.Error, AttributeError, TypeError, ValueError):
            continue
    for service in services():
        try:
            data = service.as_dict()
        except (psutil.Error, OSError, AttributeError):
            continue
        if data.get("status") not in ("running", "start_pending") or not _matches(
                data.get("name"), data.get("display_name"), data.get("binpath")):
            continue
        found.append(Finding("service", str(data.get("name")), str(data.get("display_name") or data.get("name"))))
    return found


def _hidden() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def stop_all(
    findings: Iterable[Finding],
    stop_service: Callable[[str], bool] | None = None,
    end_process: Callable[[int], bool] | None = None,
) -> tuple[list[str], list[str]]:
    """Stop the services, then end the processes. Returns (done, failed) labels."""

    def default_stop_service(name: str) -> bool:
        result = subprocess.run(["sc.exe", "stop", name], capture_output=True, timeout=20,
                                creationflags=_hidden())
        # 0 = stop requested; 1062 = was not running.
        return result.returncode in (0, 1062)

    def default_end_process(pid: int) -> bool:
        try:
            process = psutil.Process(pid)
            if not _matches(process.name(), process.exe()):  # pid reused: never kill anything else
                return False
            process.terminate()
            try:
                process.wait(timeout=5)
            except psutil.TimeoutExpired:
                process.kill()
            return True
        except psutil.NoSuchProcess:
            return True
        except (psutil.Error, OSError):
            return False

    stop_service = stop_service or default_stop_service
    end_process = end_process or default_end_process
    done, failed = [], []
    findings = list(findings)
    for finding in [f for f in findings if f.kind == "service"] + [f for f in findings if f.kind == "process"]:
        try:
            ok = stop_service(finding.name) if finding.kind == "service" else end_process(int(finding.pid or 0))
        except Exception:
            ok = False
        (done if ok else failed).append(finding.label)
    return done, failed
