"""Single source of truth for CPU/GPU/RAM colour thresholds (all designs)."""
from __future__ import annotations

# (warn, critical): value < warn -> normal, warn <= value < critical -> warning,
# value >= critical -> critical.
THRESHOLDS: dict[tuple[str, str], tuple[float, float]] = {
    ("cpu", "temp"): (60.0, 75.0),
    ("cpu", "load"): (60.0, 75.0),
    ("gpu", "temp"): (55.0, 65.0),
    ("gpu", "load"): (60.0, 80.0),
    ("ram", "load"): (70.0, 85.0),
}

NORMAL, WARNING, CRITICAL = 0, 1, 2


def level(component: str, kind: str, value: float | None) -> int | None:
    """Return NORMAL/WARNING/CRITICAL, or None when the value is unknown."""
    if value is None:
        return None
    warn, critical = THRESHOLDS[(component, kind)]
    if value >= critical:
        return CRITICAL
    if value >= warn:
        return WARNING
    return NORMAL
