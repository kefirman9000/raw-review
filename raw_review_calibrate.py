"""
Calibrating the preview to Lightroom, per camera.

You export a few photos from Lightroom as JPEGs; RAW Review renders the same raws with the
same settings (read from their sidecars) and finds how many stops brighter Lightroom made
them. The typical difference per camera becomes that camera's offset (core.CAMERA_EV), so the
preview, and auto exposure, match what you'll see in Lightroom. Nothing is written to your photos.
"""
from __future__ import annotations

import os
import re

import numpy as np

JPEG_EXTS = {".jpg", ".jpeg"}
MIN_PAIRS = 2          # photos per camera before an offset is offered
MAX_SPREAD = 0.5       # stops; above this the exports disagree too much to trust


def find_pairs(raw_paths: list[str], export_dir: str) -> list[tuple[str, str]]:
    """
    Match raws to Lightroom exports by file name: "_DSC7878.ARW" ↔ "_DSC7878.jpg", also
    "_DSC7878-Edit.jpg" or "_DSC7878 (2).jpg". Looks in export_dir and one level of subfolders.
    """
    jpgs = {}
    for root, dirs, files in os.walk(export_dir):
        if os.path.relpath(root, export_dir).count(os.sep) > 0:
            dirs[:] = []
        for f in files:
            stem, ext = os.path.splitext(f)
            if ext.lower() in JPEG_EXTS:
                jpgs.setdefault(stem.lower(), os.path.join(root, f))
    pairs = []
    for raw in raw_paths:
        stem = os.path.splitext(os.path.basename(raw))[0].lower()
        hit = jpgs.get(stem)
        if hit is None:
            pat = re.compile(re.escape(stem) + r"[\s_\-(].*")
            hit = next((p for s, p in sorted(jpgs.items()) if pat.fullmatch(s)), None)
        if hit is not None:
            pairs.append((raw, hit))
    return pairs


def summarize(results: list[dict], current: dict) -> list[dict]:
    """
    results: [{"camera": str, "offset": float}] (offset = stops Lightroom is brighter than the
    preview, measured with the current calibration applied). Returns per camera: photos,
    change (median), spread (interquartile range), new (current + change), ok.
    """
    rows = []
    for cam in sorted({r["camera"] for r in results}):
        v = np.array([r["offset"] for r in results if r["camera"] == cam], float)
        change = float(np.median(v))
        spread = float(np.subtract(*np.percentile(v, [75, 25]))) if len(v) > 1 else 0.0
        cur = float(current.get(cam, 0.0))
        rows.append({"camera": cam, "photos": len(v), "change": round(change, 2), "spread": round(spread, 2),
                     "current": cur, "new": round(cur + change, 2),
                     "ok": len(v) >= MIN_PAIRS and spread <= MAX_SPREAD and cam != ""})
    return rows
