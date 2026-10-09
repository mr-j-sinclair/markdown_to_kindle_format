#!/usr/bin/env python3
"""Crop a LinkedIn lightbox screenshot down to the photo itself.

Usage:
    .venv/bin/python3 crop_lightbox.py IN.png OUT.png [--expect W H] [--tolerance 0.03]

How it works (no fixed "dark = border" threshold, so dark photos survive):
  1. Border colours are sampled, not assumed: flat colours shared by >= 2
     corners, plus flat colours covering >= 3% of the screenshot's outer ring
     (backdrop, lightbox frame, side panel).
  2. Each pixel is "content" if it differs from every chosen border colour.
  3. Alternating column/row projections find the largest contiguous block of
     mostly-content lines (hysteresis, so dark bands inside the photo stay in;
     sparse side-panel/dimmed page text falls out; fringes beyond a sharp step
     are trimmed).
  4. This repeats inside each result ("peeling": dimmed page -> frame/side
     panel -> photo), and for every subset of the ring colours (a white photo
     touching the screenshot edge also shows up in the ring). Every box found
     is a candidate, plus every pairing of a found x-range with a found
     y-range (a dark photo edge that blends into the frame loses rows, but
     when the photo fills the frame height the frame's top/bottom are its).
  5. With --expect W H (the <img> natural size) the credible candidate (>= half
     the area of the largest directly found box) with the closest aspect wins,
     ties to the larger; none within --tolerance -> exit 2. Without it, the
     deepest clean corner-colour pass wins (best effort -- always pass --expect).

Prints the crop box, output size and aspect. Exits 2 (after still writing the
best guess) when --expect is given and the aspect is off by more than
--tolerance (relative), so the caller knows to check it. Exits 1 on bad
arguments, when no lightbox border is found, or when OUT is (an alias of) IN;
the raw input is never overwritten.
"""

from __future__ import annotations

import argparse
import math
import os
import sys

import numpy as np
from PIL import Image

COLOR_TOL = 10        # max per-channel difference still counted as "border colour"
FLAT_STD = 4.0        # corner patch must be this flat to count as chrome
SEED_FRAC = 0.6       # a line seeds a block if its content fraction >= SEED_FRAC * max
GROW_FRAC = 0.25      # ...and the block grows over lines >= GROW_FRAC * max (hysteresis)
TRIM_FRAC = 0.6       # a fringe beyond a sharp step is cut if sparser than TRIM_FRAC * median
STEP_JUMP = 0.3       # minimum content-fraction step that marks the photo's edge
RING_SHARE = 0.03    # perimeter colours covering >= 3% of the ring are border candidates
MAX_PALETTE = 5
MAX_PASSES = 4

Box = tuple[int, int, int, int]  # (left, top, right, bottom), right/bottom exclusive


def _merge(colours: list[np.ndarray]) -> list[np.ndarray]:
    out: list[np.ndarray] = []
    for c in colours:
        if not any(np.abs(c - o).max() <= COLOR_TOL for o in out):
            out.append(c)
    return out


def corner_border_colours(arr: np.ndarray) -> list[np.ndarray]:
    """Colours shared by at least two flat corner patches of ``arr``."""
    h, w, _ = arr.shape
    k = max(1, min(5, h // 4, w // 4))
    patches = [arr[:k, :k], arr[:k, w - k:], arr[h - k:, :k], arr[h - k:, w - k:]]
    flat = [p.reshape(-1, 3).mean(0) for p in patches if p.reshape(-1, 3).std(0).max() <= FLAT_STD]
    return _merge([c for c in flat if sum(np.abs(c - o).max() <= COLOR_TOL for o in flat) >= 2])


def ring_palette(arr: np.ndarray) -> list[np.ndarray]:
    """Flat colours that cover a noticeable share of the screenshot's outer ring."""
    ring = np.concatenate([arr[:2].reshape(-1, 3), arr[-2:].reshape(-1, 3),
                           arr[:, :2].reshape(-1, 3), arr[:, -2:].reshape(-1, 3)])
    q = (ring // 4).astype(np.int32)
    keys, counts = np.unique(q[:, 0] * 4096 + q[:, 1] * 64 + q[:, 2], return_counts=True)
    order = np.argsort(-counts)
    picked = []
    for i in order:
        if counts[i] < RING_SHARE * len(ring):
            break
        k = keys[i]
        picked.append(np.array([k // 4096, (k // 64) % 64, k % 64], dtype=float) * 4 + 1.5)
    return _merge(picked)[:MAX_PALETTE]


def content_mask(arr: np.ndarray, border: list[np.ndarray]) -> np.ndarray:
    a = arr.astype(np.int16)
    mask = np.ones(arr.shape[:2], dtype=bool)
    for c in border:
        mask &= np.abs(a - c.astype(np.int16)).max(axis=2) > COLOR_TOL
    return mask


def runs(keep: np.ndarray, gap: int) -> list[tuple[int, int]]:
    """Contiguous True runs as [start, end) pairs, bridging gaps <= ``gap``."""
    out: list[list[int]] = []
    for i in np.flatnonzero(keep):
        if out and i - out[-1][1] <= gap:
            out[-1][1] = i + 1
        else:
            out.append([i, i + 1])
    return [(s, e) for s, e in out]


def best_run(frac: np.ndarray) -> tuple[tuple[int, int], bool]:
    """Largest-mass block of content lines (hysteresis), plus whether it clearly dominates."""
    top = frac.max()
    seed = frac >= SEED_FRAC * top
    rs = [r for r in runs(frac >= GROW_FRAC * top, gap=max(2, len(frac) // 50)) if seed[r[0]:r[1]].any()]
    masses = sorted(((frac[s:e].sum(), (s, e)) for s, e in rs), reverse=True)
    dominant = len(masses) == 1 or masses[1][0] < 0.3 * masses[0][0]
    s, e = masses[0][1]
    return trim_fringe(frac, s, e), dominant


def trim_fringe(frac: np.ndarray, s: int, e: int) -> tuple[int, int]:
    """Cut sparse fringe lines (e.g. dimmed page text bridged onto the photo's edge).

    Within the outer 15% of the block at each end, cut at the sharpest step into
    the block when the lines outside that step are clearly sparser than the block.
    """
    med = float(np.median(frac[s:e]))
    span = max(2, (e - s) * 15 // 100)
    d = np.diff(frac[s:s + span + 1])            # rises entering from the start
    if d.size and d.max() >= STEP_JUMP:
        cut = s + int(d.argmax()) + 1
        if frac[s:cut].mean() < TRIM_FRAC * med:
            s = cut
    d = -np.diff(frac[e - span - 1:e])           # falls leaving at the end
    if d.size and d.max() >= STEP_JUMP:
        cut = e - span + int(d.argmax())
        if frac[cut:e].mean() < TRIM_FRAC * med:
            e = cut
    return s, e


def find_block(mask: np.ndarray) -> tuple[Box, bool] | None:
    h, w = mask.shape
    if not mask.any():
        return None
    y0, y1, x0, x1 = 0, h, 0, w
    clean = True
    for _ in range(4):  # alternate projections until stable
        cols = mask[y0:y1].mean(axis=0)
        if not cols.any():
            return None
        (nx0, nx1), dom_x = best_run(cols)
        rows = mask[:, nx0:nx1].mean(axis=1)
        (ny0, ny1), dom_y = best_run(rows)
        clean = dom_x and dom_y
        if (nx0, nx1, ny0, ny1) == (x0, x1, y0, y1):
            break
        x0, x1, y0, y1 = nx0, nx1, ny0, ny1
    return (int(x0), int(y0), int(x1), int(y1)), clean


def peel(arr: np.ndarray, start: Box, border: list[np.ndarray] | None) -> list[tuple[Box, bool]]:
    """Successively peeled boxes inside ``start`` (absolute coords) with a 'clean' flag.

    The first pass uses ``border`` (or the shared corner colours if None); later
    passes re-sample the corners of the previous box.
    """
    out = []
    box = start
    for _ in range(MAX_PASSES):
        region = arr[box[1]:box[3], box[0]:box[2]]
        colours = border if border is not None else corner_border_colours(region)
        border = None
        if not colours:
            break
        found = find_block(content_mask(region, colours))
        if found is None:
            break
        (x0, y0, x1, y1), clean = found
        if (x1 - x0) < 16 or (y1 - y0) < 16:
            break
        if (x0, y0, x1, y1) == (0, 0, region.shape[1], region.shape[0]):
            break  # nothing peeled
        box = (box[0] + x0, box[1] + y0, box[0] + x1, box[1] + y1)
        out.append((box, clean))
        if not clean:
            break
    return out


def candidates(arr: np.ndarray) -> tuple[list[tuple[Box, bool]], list[Box], list[Box]]:
    """(default peeling chain, all candidates largest first, directly found boxes).

    "Directly found" excludes the crossed pairings and the whole screenshot;
    it is the reference for how big a credible photo box must be.
    """
    h, w, _ = arr.shape
    full = (0, 0, w, h)
    default = peel(arr, full, None)
    pal = ring_palette(arr)
    boxes = {b for b, _ in default}
    for bits in range(1, 2 ** len(pal)):
        subset = [c for i, c in enumerate(pal) if bits >> i & 1]
        for b, _ in peel(arr, full, subset):
            boxes.add(b)
    # Cross candidates: a photo whose dark edge blends into the frame loses rows
    # (or columns), but when it fills the frame's height (width) the frame's
    # edges are the photo's -- so also pair each found x-range with each y-range.
    xs = {(b[0], b[2]) for b in boxes}
    ys = {(b[1], b[3]) for b in boxes}
    crossed = {(x0, y0, x1, y1) for x0, x1 in xs for y0, y1 in ys}
    direct = [b for b in boxes if b != full]
    return default, sorted(boxes | crossed, key=lambda b: -area(b)), direct


def aspect(b: Box) -> float:
    return (b[2] - b[0]) / (b[3] - b[1])


def area(b: Box) -> int:
    return (b[2] - b[0]) * (b[3] - b[1])


def choose_expected(boxes: list[Box], want: float, tolerance: float,
                    reference: list[Box] | None = None) -> Box:
    """Credible candidate with the smallest aspect error (ties: larger area).

    Credible = at least half the area of the largest directly found box
    (``reference``, default ``boxes``), whatever its aspect, so a small
    fragment (one logo of a grid) can't win just by having the right shape.
    If no credible box is within tolerance, the closest credible one is
    returned and the caller reports the mismatch.
    """
    err = lambda b: abs(aspect(b) / want - 1)
    ref = max(area(b) for b in (reference or boxes))
    credible = [b for b in boxes if area(b) >= 0.5 * ref] or boxes
    return min(credible, key=lambda b: (round(err(b), 4), -area(b)))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--expect", nargs=2, type=int, metavar=("W", "H"),
                    help="natural size of the displayed <img>; checks the crop's aspect")
    ap.add_argument("--tolerance", type=float, default=0.03,
                    help="allowed relative aspect difference (default 0.03)")

    def usage_error(msg: str):  # exit 1, keeping exit 2 for "aspect mismatch" only
        ap.print_usage(sys.stderr)
        sys.exit(f"error: {msg}")

    ap.error = usage_error
    args = ap.parse_args()

    if args.expect and min(args.expect) <= 0:
        ap.error("--expect W H must both be positive")
    if not math.isfinite(args.tolerance) or args.tolerance < 0:
        ap.error("--tolerance must be a finite number >= 0")
    if not os.path.isfile(args.input):
        ap.error(f"input not found: {args.input}")
    same = os.path.abspath(args.input) == os.path.abspath(args.output)
    if not same and os.path.exists(args.output):
        same = os.path.samefile(args.input, args.output)  # symlink / hard-link alias
    if same:
        print("error: OUT must differ from IN (the raw screenshot is kept)", file=sys.stderr)
        return 1

    img = Image.open(args.input).convert("RGB")
    arr = np.asarray(img)
    default, boxes, direct = candidates(arr)
    if not boxes:
        print("error: no lightbox border found; crop manually", file=sys.stderr)
        return 1

    if args.expect:
        want = args.expect[0] / args.expect[1]
        box = choose_expected(boxes, want, args.tolerance, direct)
    else:
        clean = [b for b, ok in default if ok]
        box = clean[-1] if clean else (default[0][0] if default else boxes[0])

    img.crop(box).save(args.output)
    w, h = box[2] - box[0], box[3] - box[1]
    print(f"crop box (left, top, right, bottom): {box}")
    print(f"output: {args.output} {w}x{h} aspect {aspect(box):.4f}")
    if len(boxes) > 1:
        print("other candidates: " + ", ".join(
            f"{b} {aspect(b):.3f}" for b in boxes[:8] if b != box))
    if args.expect:
        diff = abs(aspect(box) / want - 1)
        print(f"expected aspect {want:.4f} ({args.expect[0]}x{args.expect[1]}); difference {diff:.1%}")
        if diff > args.tolerance:
            print(f"ASPECT MISMATCH: {diff:.1%} > {args.tolerance:.1%} -- inspect the crop and "
                  "re-zoom/crop manually", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
