"""Read-only update check against the public GitHub release list.

Safety rules (enforced by tests):
  * one HTTPS GET of the public releases/latest JSON - nothing else;
  * nothing is ever downloaded or executed: the user only gets a link to the
    release page and installs the zip by hand;
  * the link is accepted only when it points at this repository's GitHub
    release pages, so a tampered response cannot redirect the user elsewhere.
"""
from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass

REPOSITORY = "taserkan/sabasakal-mini-screen"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASE_PAGE_PREFIX = f"https://github.com/{REPOSITORY}/releases/"
CHECK_INTERVAL_SECONDS = 24 * 3600
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


@dataclass(frozen=True)
class UpdateInfo:
    latest: str
    url: str
    newer: bool


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.match(str(text or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None  # type: ignore[return-value]


def is_trusted_release_url(url: str) -> bool:
    return isinstance(url, str) and url.startswith(RELEASE_PAGE_PREFIX) and "\\" not in url


def evaluate_release(payload: dict, current_version: str) -> UpdateInfo:
    """Turn the GitHub JSON into an UpdateInfo; raises ValueError on odd data."""
    if not isinstance(payload, dict):
        raise ValueError("unexpected release payload")
    if payload.get("draft") or payload.get("prerelease"):
        raise ValueError("latest release is not a final release")
    tag = str(payload.get("tag_name") or "")
    latest = parse_version(tag)
    current = parse_version(current_version)
    if latest is None or current is None:
        raise ValueError(f"unreadable version: {tag!r}")
    url = str(payload.get("html_url") or "")
    if not is_trusted_release_url(url):
        url = RELEASE_PAGE_PREFIX + "latest"
    return UpdateInfo(".".join(map(str, latest)), url, latest > current)


def fetch_latest_release(current_version: str, timeout: float = 8.0) -> UpdateInfo:
    request = urllib.request.Request(
        LATEST_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"Sabasakal-Mini-Ekran/{current_version}",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read(256 * 1024).decode("utf-8"))
    return evaluate_release(payload, current_version)
