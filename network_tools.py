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
    vendor: str = ""  # matched BOOSTER_NAMES entry, e.g. "exitlag"


def _matches(*values: object) -> bool:
    return any(value and _PATTERN.search(str(value)) for value in values)


def vendor_of(*values: object) -> str:
    """The booster a name/path belongs to ("exitlag", "noping"...), or ""."""
    for value in values:
        match = _PATTERN.search(str(value or ""))
        if match:
            return match.group(0).lower()
    return ""


def vendors_with_app_open(process_names: Iterable[str], service_binaries: Iterable[str]) -> set[str]:
    """Boosters with a program running that is not one of their services."""
    services = {name.lower() for name in service_binaries}
    return {
        vendor for vendor in (vendor_of(name) for name in process_names
                              if str(name).lower() not in services)
        if vendor
    }


DISPLAY_NAMES = ("ExitLag", "NoPing", "WTFast", "Mudfish", "GearUP", "LagoFast", "Haste",
                 "Outfox", "PingZapper", "Battle Ping", "Speedify")


@dataclass(frozen=True)
class ScanReport:
    findings: tuple[Finding, ...]
    processes_checked: int
    services_checked: int
    # Executables of every installed booster service (running or not), so a
    # service's own process is never mistaken for the program's window.
    service_binaries: frozenset[str] = frozenset()


# Service name -> command line. Service configs almost never change; a
# reinstalled booster keeps its name, and a new one is simply a new key.
_SERVICE_BINPATHS: dict[str, str] = {}


def scan(
    processes: Callable[[], Iterable] = lambda: psutil.process_iter(["pid", "name", "exe"]),
    services: Callable[[], Iterable] = lambda: psutil.win_service_iter(),
) -> list[Finding]:
    """Running booster processes and services (nothing is changed)."""
    return list(scan_report(processes, services).findings)


def scan_report(
    processes: Callable[[], Iterable] = lambda: psutil.process_iter(["pid", "name", "exe"]),
    services: Callable[[], Iterable] = lambda: psutil.win_service_iter(),
) -> ScanReport:
    """Like scan(), plus how many processes and services were checked.

    A service's own executable is reported once, as the service: killing it
    as a process would look like a crash to Windows, and a service set to
    "restart on failure" (ExitLag's is) would simply come back.
    """
    services_found: list[Finding] = []
    service_binaries: set[str] = set()  # running booster services
    installed_binaries: set[str] = set()  # every booster service
    service_count = 0
    for service in services():
        service_count += 1
        try:
            # Name and display name come with the enumeration; the command
            # line costs a query per service, so it is remembered, and the
            # status is only asked of booster services.
            name, display = str(service.name()), str(service.display_name() or "")
            binpath = _SERVICE_BINPATHS.get(name)
            if binpath is None:
                binpath = _SERVICE_BINPATHS[name] = str(service.binpath() or "")
            vendor = vendor_of(name, display, binpath)
            if not vendor:
                continue
            status = service.status()
        except (psutil.Error, OSError, AttributeError):
            continue
        binary = _binary_name(binpath)
        if binary:
            installed_binaries.add(binary)
        if status not in ("running", "start_pending"):
            continue
        services_found.append(Finding("service", name, display or name, vendor=vendor))
        if binary:
            service_binaries.add(binary)
    processes_found: list[Finding] = []
    process_count = 0
    for process in processes():
        process_count += 1
        try:
            info = process.info
            name = str(info.get("name") or "")
            vendor = vendor_of(name, info.get("exe"))
            if not vendor or name.lower() in service_binaries:
                continue
            processes_found.append(Finding("process", name, name, int(info.get("pid") or 0), vendor))
        except (psutil.Error, AttributeError, TypeError, ValueError):
            continue
    return ScanReport(tuple(processes_found + services_found), process_count, service_count,
                      frozenset(installed_binaries))


def _binary_name(binpath) -> str:
    """File name of a service command line (quoted or not), lower case."""
    text = str(binpath or "").strip()
    if text.startswith('"'):
        text = text[1:].split('"', 1)[0]
    else:
        end = text.lower().find(".exe")
        text = text[: end + 4] if end >= 0 else text.split(" ", 1)[0]
    return text.replace("/", "\\").rsplit("\\", 1)[-1].lower()


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
