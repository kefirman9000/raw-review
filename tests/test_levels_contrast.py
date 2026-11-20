"""
Auto gets contrast from the levels: Whites up / Blacks down until a sliver of the photo is at
white / black, exposure re-centred, Contrast left at 0. And auto Vibrance never desaturates.
"""
import numpy as np

import raw_review_core as core
import raw_review_auto as auto


def _flat_scene(seed=0):
    rng = np.random.default_rng(seed)
    base = rng.uniform(0.05, 0.22, (200, 300, 1)).astype(np.float32)        # flat, low-contrast greys
    tint = rng.uniform(0.85, 1.15, (200, 300, 3)).astype(np.float32)
    cam = np.clip(base * tint, 0, 1)
    return core.Decoded(cam=cam, color=core.CameraColor(np.linalg.inv(core.XYZ_FROM_SRGB), [1, 1, 1]),
                        baseline=1.0, source_size=(300, 200))


def test_contrast_comes_from_levels_not_contrast_slider():
    dec = _flat_scene()
    e = core.as_shot_edit(dec)
    auto.run_auto(dec, e, ["exposure", "levels", "shhi", "contrast", "colour"])
    assert e.contrast == 0
    assert e.whites > 10 and e.blacks < -10                  # range filled by the levels
    L = auto._finished_luma(dec, e)
    assert auto._blown_share(dec, e) <= 0.006
    old = core.as_shot_edit(dec)                             # the Contrast-slider way, for comparison
    auto.LEVELS_CONTRAST = False
    try:
        auto.run_auto(dec, old, ["exposure", "levels", "shhi", "contrast", "colour"])
    finally:
        auto.LEVELS_CONTRAST = True
    Lo = auto._finished_luma(dec, old)
    spread = lambda a: float(np.percentile(a, 99.5) - np.percentile(a, 0.5))
    assert spread(L) > spread(Lo) - 0.03                # about the range Contrast +25 gave, without bending midtones


def test_auto_vibrance_never_negative_on_colourful_scene():
    rng = np.random.default_rng(1)
    cam = np.zeros((120, 180, 3), np.float32)
    cam[..., 0] = rng.uniform(0.05, 0.5, (120, 180))       # strongly coloured, like toys / packaging
    cam[..., 1] = rng.uniform(0.01, 0.08, (120, 180))
    cam[..., 2] = rng.uniform(0.01, 0.4, (120, 180))
    dec = core.Decoded(cam=cam, color=core.CameraColor(np.linalg.inv(core.XYZ_FROM_SRGB), [1, 1, 1]),
                       baseline=1.0, source_size=(180, 120))
    e = core.as_shot_edit(dec)
    auto.auto_colour(dec, e)
    assert e.vibrance >= 0


def test_auto_colour_adds_colour_and_saturated_reds_do_not_pull_highlights():
    rng = np.random.default_rng(2)
    cam = rng.uniform(0.02, 0.12, (160, 240, 3)).astype(np.float32)
    cam[40:120, 60:180] = [0.30, 0.02, 0.02]                 # a big vivid red label
    dec = core.Decoded(cam=cam, color=core.CameraColor(np.linalg.inv(core.XYZ_FROM_SRGB), [1, 1, 1]),
                       baseline=1.0, source_size=(240, 160))
    e = core.as_shot_edit(dec)
    auto.run_auto(dec, e, ["exposure", "levels", "shhi", "contrast", "colour"])
    assert e.vibrance >= auto.VIBRANCE_BASE
    assert e.highlights > -60                                # not crushed because the red channel clips
