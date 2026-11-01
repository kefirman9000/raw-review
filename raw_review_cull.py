"""
RAW Review - culling suggestions (no GUI).

Flags photos you'll probably want to reject, so you can find them quickly:

* soft / blurry: the edge-sharpness measured on the full-resolution image is low
  (missed focus, camera shake or subject motion)
* blown highlights: a large part of the frame is clipped in the RAW data itself,
  which no slider can bring back
* very dark: auto exposure had to lift the photo a lot (noisy shadows)
* bursts and near-duplicates: neighbouring photos (in file order) that look almost
  the same are grouped, and every one but the sharpest is suggested for rejection

These are suggestions only. Nothing is rejected until you choose to, and nothing
here reads or writes a file: it only looks at the decoded preview data.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

import raw_review_core as core

SOFT_RATIO = 1.75          # sharp_ratio below this: probably soft (~3 crisp, ~1.6 very soft; see core.measure_quality)
CLIPPED_FRACTION = 0.03    # more than this share of the frame clipped in the RAW: blown highlights
CLIP_LEVEL = 0.985         # a camera channel at or above this (0..1 of the white level) counts as clipped
DARK_EV = 2.0              # auto exposure lifted the photo by more than this many stops: very dark
SIMILAR = 0.92             # signature correlation at or above this: the same shot (burst / near-duplicate)
SIG_SIZE = 24              # the signature is a tiny SIG_SIZE x SIG_SIZE grey thumbnail


@dataclass
class CullStats:
    sharp: float = 0.0           # edge-sharpness ratio (0 = couldn't tell)
    clipped: float = 0.0         # share of the frame clipped in the RAW data
    lift_ev: float = 0.0         # how far auto exposure brightened the photo
    sig: np.ndarray | None = field(default=None, repr=False)   # for spotting near-duplicates

    @property
    def soft(self) -> bool:
        return 0.0 < self.sharp < SOFT_RATIO

    @property
    def blown(self) -> bool:
        return self.clipped > CLIPPED_FRACTION

    @property
    def dark(self) -> bool:
        return self.lift_ev > DARK_EV


def signature(dec: core.Decoded) -> np.ndarray:
    """A tiny, brightness-normalised grey thumbnail: two frames of the same scene correlate close to 1."""
    cam = dec.cam
    y = cam @ np.asarray(core.LUMA, dtype=np.float32)
    y = np.sqrt(np.clip(y * np.float32(dec.baseline), 0, 1))             # roughly perceptual
    small = cv2.resize(y, (SIG_SIZE, SIG_SIZE), interpolation=cv2.INTER_AREA).astype(np.float32).ravel()
    small -= small.mean()
    n = float(np.linalg.norm(small))
    return small / n if n > 1e-6 else small


def similarity(a: np.ndarray | None, b: np.ndarray | None) -> float:
    if a is None or b is None or a.shape != b.shape:
        return 0.0
    return float(np.dot(a, b))


def measure(dec: core.Decoded, auto_edit: core.Edit | None = None) -> CullStats:
    """Everything the cull suggestions need, from the decoded working copy (read-only)."""
    cam = dec.cam
    clipped = float(np.mean(cam.max(axis=2) >= CLIP_LEVEL)) if cam.size else 0.0
    lift = float(auto_edit.exposure) if auto_edit is not None else 0.0
    return CullStats(sharp=float(dec.quality.sharp_ratio or 0.0), clipped=clipped, lift_ev=lift,
                     sig=signature(dec))


def group_bursts(ordered: list[tuple[str, CullStats | None]], threshold: float = SIMILAR) -> list[list[str]]:
    """Runs of neighbouring photos (in the order given) that look almost the same. Groups of 2 or more only."""
    groups, run = [], []
    prev = None
    for path, st in ordered:
        if st is None or st.sig is None:
            if len(run) > 1:
                groups.append(run)
            run, prev = [], None
            continue
        if prev is not None and similarity(prev.sig, st.sig) >= threshold:
            run.append(path)
        else:
            if len(run) > 1:
                groups.append(run)
            run = [path]
        prev = st
    if len(run) > 1:
        groups.append(run)
    return groups


def best_of(group: list[str], stats: dict) -> str:
    """The keeper in a burst: the sharpest frame, then the one with the least clipping."""
    return max(group, key=lambda p: ((stats[p].sharp if stats.get(p) else 0.0),
                                     -(stats[p].clipped if stats.get(p) else 0.0)))


def suggestions(ordered: list[tuple[str, CullStats | None]], threshold: float = SIMILAR) -> dict[str, list[str]]:
    """path -> the reasons it may be worth rejecting (empty lists are left out).
    The sharpest frame of each burst is never flagged as a duplicate."""
    stats = {p: st for p, st in ordered}
    out: dict[str, list[str]] = {}
    for p, st in ordered:
        if st is None:
            continue
        why = []
        if st.soft:
            why.append("soft")
        if st.blown:
            why.append("blown highlights")
        if st.dark:
            why.append("very dark")
        if why:
            out[p] = why
    for group in group_bursts(ordered, threshold):
        keep = best_of(group, stats)
        for p in group:
            if p != keep:
                out.setdefault(p, []).append(f"similar to {_short(keep)} (sharper)")
    return out


def burst_labels(ordered: list[tuple[str, CullStats | None]], threshold: float = SIMILAR) -> dict[str, str]:
    """path -> 'burst 2/5' (with ' ★' on the keeper), for the photo list."""
    stats = {p: st for p, st in ordered}
    out = {}
    for group in group_bursts(ordered, threshold):
        keep = best_of(group, stats)
        for i, p in enumerate(group, 1):
            out[p] = f"burst {i}/{len(group)}" + (" ★" if p == keep else "")
    return out


def _short(path: str) -> str:
    import os
    return os.path.basename(path)
