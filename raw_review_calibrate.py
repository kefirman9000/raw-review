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


SLOPE_MIN_PAIRS = 3    # photos before a slope with Exposure is fitted...
SLOPE_MIN_SPAN = 0.75  # ...and they must cover at least this range of Exposure (stops)
SLOPE_LIMIT = 0.3      # stops of offset per stop of Exposure, at most


def summarize(results: list[dict], current: dict, current_slope: dict | None = None) -> list[dict]:
    """
    results: [{"camera": str, "offset": float, "exposure": float (optional)}] (offset = stops
    Lightroom is brighter than the preview, measured with the current calibration applied).
    Returns per camera: photos, change (median), spread (interquartile range), new (current + change),
    slope / new_slope (offset per stop of Exposure), ok.

    When the exports cover a range of Exposure values (export a few photos at Exposure 0, +1 and +2),
    the offset is fitted as a line in Exposure instead of one number: Lightroom's Exposure rolls the
    highlights off as it goes up, so an offset measured at 0 drifts at the +0.5..+1 auto writes.
    The spread is then measured around that line.
    """
    current_slope = current_slope or {}
    rows = []
    for cam in sorted({r["camera"] for r in results}):
        rs = [r for r in results if r["camera"] == cam]
        v = np.array([r["offset"] for r in rs], float)
        x = np.array([float(r.get("exposure", 0.0) or 0.0) for r in rs], float)
        cur = float(current.get(cam, 0.0))
        cur_slope = float(current_slope.get(cam, 0.0))
        slope = 0.0
        if len(v) >= SLOPE_MIN_PAIRS and float(x.max() - x.min()) >= SLOPE_MIN_SPAN:
            b, a = np.polyfit(x, v, 1)
            slope = float(np.clip(b, -SLOPE_LIMIT, SLOPE_LIMIT))
            resid = v - slope * x
            change = float(np.median(resid))
            spread = float(np.subtract(*np.percentile(resid, [75, 25])))
        else:
            change = float(np.median(v))
            spread = float(np.subtract(*np.percentile(v, [75, 25]))) if len(v) > 1 else 0.0
        rows.append({"camera": cam, "photos": len(v), "change": round(change, 2), "spread": round(spread, 2),
                     "current": cur, "new": round(cur + change, 2),
                     "slope": round(slope, 3), "new_slope": round(cur_slope + slope, 3),
                     "ok": len(v) >= MIN_PAIRS and spread <= MAX_SPREAD and cam != ""})
    return rows
