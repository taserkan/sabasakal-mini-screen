# SPDX-License-Identifier: GPL-3.0-or-later
"""Simulate Claude-design screen changes on the serial LCD and write GIFs.

The Rev-A panel paints each region top-to-bottom as its RGB565 bytes arrive.
This tool replays a transition at a given bus throughput so the look of the
old row-major tile update ("tiles") can be compared with the column wipe the
app now uses for large changes ("wipe"):

    python tools/render_transitions.py [output_dir] [--bytes-per-second N]

Default throughput is the value measured on the real Rev-A USBMONITOR_3_5
panel on 2026-10-10: a full 480x320 RGB565 frame (307 KB) takes 1.20 s, i.e.
~256 KB/s regardless of the nominal 115200 baud (USB CDC). A full repaint can
therefore never finish in 400 ms; only changes up to ~100 KB can. Cross-family
switches (desktop / CS2 / FACEIT / MSFS) differ in 200-307 KB even with a
shared background, so they use the column wipe: 8 strips of 60 px, each
landing in ~150 ms, which reads as a deliberate left-to-right wipe instead of
a 1.2 s top-to-bottom tear. Within-family changes (live <-> death) stay on the
tile path (~0.45 s). No crossfade frames are ever sent: each would cost 1.2 s.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from PIL import Image  # noqa: E402

import gui  # noqa: E402
from render_previews import contexts  # noqa: E402
from screens_new import render_new_screen  # noqa: E402

MEASURED_BYTES_PER_SECOND = 256_000
FRAME_MS = 20
PAIRS = (
    ("main", "cs2-menu"), ("cs2-menu", "cs2-live-dark"), ("cs2-live-dark", "cs2-death"),
    ("cs2-death", "cs2-faceit"), ("main", "msfs-cruise"), ("msfs-cruise", "msfs-approach"),
)


def simulate(old: Image.Image, new: Image.Image, regions, bps: float) -> list[Image.Image]:
    """Frames of the panel while ``regions`` of ``new`` stream in order."""
    frames = [old.copy()]
    canvas = old.copy()
    budget_per_frame = bps * FRAME_MS / 1000.0
    budget = 0.0
    for left, top, right, bottom in regions:
        row_bytes = (right - left) * 2
        y = top
        while y < bottom:
            budget += budget_per_frame if budget < row_bytes else 0
            rows = int(budget // row_bytes)
            if rows <= 0:
                frames.append(canvas.copy())
                continue
            rows = min(rows, bottom - y)
            box = (left, y, right, y + rows)
            canvas.paste(new.crop(box), box)
            budget -= rows * row_bytes
            y += rows
            if budget < row_bytes:
                frames.append(canvas.copy())
    frames.append(new.copy())
    return frames


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", default=str(ROOT / "docs" / "transitions"))
    parser.add_argument("--bytes-per-second", type=float, default=MEASURED_BYTES_PER_SECOND)
    args = parser.parse_args()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    screens = contexts()
    for a, b in PAIRS:
        old = render_new_screen(screens[a]).convert("RGB")
        new = render_new_screen(screens[b]).convert("RGB")
        kind, regions = gui.plan_new_design_update(new, old)
        tiles = gui._tight_changed_regions(new, old, gui.NEW_DESIGN_TILES)
        for label, regs in (("tiles-before", tiles), (kind, regions)):
            frames = simulate(old, new, regs, args.bytes_per_second)
            hold = [frames[0]] * 15 + frames + [frames[-1]] * 25
            path = out / f"{a}--{b}--{label}.gif"
            hold[0].save(path, save_all=True, append_images=hold[1:], duration=FRAME_MS, loop=0)
            ms = sum((r[2] - r[0]) * (r[3] - r[1]) * 2 for r in regs) / args.bytes_per_second * 1000
            print(f"{path.name}: {len(regs)} regions, ~{ms:.0f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
