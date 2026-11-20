"""
Negative Highlights darkens the bright tones that aren't clipped, like Lightroom, and auto no longer
crushes Highlights on a scene that is bright but not blown (white packaging, snow): those photos
came out of Lightroom with grey whites (~155/255) after auto wrote Highlights -75..-90.
"""
import numpy as np

import raw_review_core as core
import raw_review_auto as auto


def test_negative_highlights_pulls_bright_tones_and_keeps_midtones():
    L = np.linspace(0, 1, 1001, dtype=np.float32)
    out = core.highlights_darken(L, -100)
    assert np.all(np.diff(out) >= 0)                     # never reverses tones
    assert np.allclose(out[:300], L[:300])               # shadows and midtones untouched
    assert out[850] < 0.85 - 0.12                         # a bright white goes clearly darker
    assert out[1000] > out[850]                           # white still the brightest
    assert np.array_equal(core.highlights_darken(L, 0), L)
    assert np.array_equal(core.highlights_darken(L, 30), L)


def _bright_shelf(white=0.16):
    rng = np.random.default_rng(3)
    cam = rng.uniform(0.005, 0.06, (200, 300, 3)).astype(np.float32)
    cam[40:140, 40:260] = white + rng.normal(0, 0.01, (100, 220, 3))    # big white label
    cam[60:120, 80:220] = rng.uniform(0.005, 0.06, (60, 140, 3))         # printing on it
    return core.Decoded(cam=np.clip(cam, 0, 1).astype(np.float32),
                        color=core.CameraColor(np.linalg.inv(core.XYZ_FROM_SRGB), [1, 1, 1]),
                        baseline=1.0, source_size=(300, 200))


def test_auto_keeps_highlights_gentle_on_bright_unclipped_scene():
    dec = _bright_shelf()
    e = core.as_shot_edit(dec)
    auto.run_auto(dec, e, ["exposure", "levels", "shhi", "contrast"])
    assert e.highlights >= -40
    assert auto._blown_share(dec, e) <= auto.CENTER_MAX_CLIPPED + 0.005
    L = auto._finished_luma(dec, e)
    assert float(np.median(L[45:58, 45:255])) > 0.78     # the white label still looks white


def test_camera_offset_default_and_calibration_override():
    core.CAMERA_EV.clear()
    assert core.camera_ev("Sony ILCE-7M3") == -0.5        # built-in, measured on Lightroom exports
    assert core.camera_ev("Some Other Camera") == 0.0
    core.CAMERA_EV["Sony ILCE-7M3"] = -0.2                 # your own calibration wins
    try:
        assert core.camera_ev("Sony ILCE-7M3") == -0.2
        assert core.camera_ev_all()["Sony ILCE-7M3"] == -0.2
    finally:
        core.CAMERA_EV.clear()
