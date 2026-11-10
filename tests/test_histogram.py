"""Histogram + clipping overlay shown beside the preview, and the clipping check auto exposure uses."""
import numpy as np

import raw_review_core as core
import raw_review_auto as auto


def test_histogram_median_and_clipping_shares():
    img = np.full((100, 100, 3), 128, np.uint8)
    img[:10] = 255                        # 10% blown
    img[10:15] = 0                        # 5% pure black
    img[15:20, :, 0] = 254                # one channel at the blown level counts too
    h = core.histogram(img)
    assert abs(h.median - 128 / 255) < 0.01
    assert abs(h.blown - 0.15) < 1e-6
    assert abs(h.black - 0.05) < 1e-6
    assert h.lum.sum() == 100 * 100 and len(h.r) == 256


def test_clip_overlay_marks_only_clipped_pixels():
    img = np.array([[[255, 200, 200], [0, 0, 1], [120, 130, 140]]], np.uint8)
    out = core.clip_overlay(img)
    assert tuple(out[0, 0]) == (255, 0, 0)       # blown -> red
    assert tuple(out[0, 1]) == (0, 90, 255)      # black -> blue
    assert tuple(out[0, 2]) == (120, 130, 140)   # untouched
    assert tuple(img[0, 0]) == (255, 200, 200)   # original not modified


def test_auto_exposure_measures_clipping_like_the_histogram():
    """Auto exposure's clipping check must agree with the preview's histogram warning, including
    a single blown channel, the preview's rounding and Vibrance. (It used to check overall
    brightness before colour, missed a warm-lit white plane, and left it blown.)"""
    rng = np.random.default_rng(11)
    for trial in range(8):
        cam = (rng.uniform(0, 1, (200, 300, 1)) ** 2 * 0.12 * np.ones(3)).astype(np.float32)
        y0, x0 = rng.integers(20, 120), rng.integers(20, 150)
        cam[y0:y0 + 30, x0:x0 + 120] = rng.uniform(0.35, 0.9) * np.array([1.0, 0.78, 0.6])   # warm white
        cam *= rng.uniform(0.85, 1.15, (200, 300, 3)).astype(np.float32)                      # texture
        dec = core.Decoded(cam=cam, color=core.CameraColor(np.linalg.inv(core.XYZ_FROM_SRGB), [1, 1, 1]),
                           baseline=1.0, source_size=(300, 200))
        e = core.as_shot_edit(dec)
        e.exposure = float(rng.uniform(0.5, 2.0))
        e.highlights = int(rng.integers(-60, 0))
        e.vibrance = int(rng.integers(0, 40))
        shown = core.histogram(core.render(dec, e, look=False)[0]).blown
        assert abs(auto._blown_share(dec, e) - shown) < 0.003, (trial, auto._blown_share(dec, e), shown)
