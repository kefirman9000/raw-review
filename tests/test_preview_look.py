"""
The preview look is display only: it adds colour like Lightroom's default profile, keeps greys
grey and brightness unchanged, and never changes what the auto tools measure. Auto exposure
centres the finished photo's histogram.
"""
import numpy as np

import raw_review_core as core
import raw_review_auto as auto


def fake_decoded(seed=0):
    rng = np.random.default_rng(seed)
    cam = rng.uniform(0.02, 0.6, (120, 180, 3)).astype(np.float32)
    color = core.CameraColor(np.eye(3) * 0.8 + 0.05, [2.0, 1.0, 1.5])
    return core.Decoded(cam=cam, color=color, baseline=1.0, source_size=(180, 120))


def test_keeps_greys_and_brightness():
    grey = np.full((4, 4, 3), 0.4, np.float32)
    assert np.allclose(core.apply_look(grey), grey, atol=1e-6)
    rng = np.random.default_rng(1)
    g = rng.uniform(0, 1, (50, 50, 3)).astype(np.float32)
    lum_in, lum_out = g @ core.LUMA, core.apply_look(g) @ core.LUMA
    assert abs(float(np.median(lum_out) - np.median(lum_in))) < 0.01


def test_adds_saturation_and_keeps_hue_order():
    px = np.array([[[0.5, 0.35, 0.2]]], np.float32)  # orange
    out = core.apply_look(px)[0, 0]
    assert out[0] > out[1] > out[2]                 # same hue ordering
    sat_in = (px.max() - px.min()) / px.max()
    sat_out = (out.max() - out.min()) / out.max()
    assert sat_out > sat_in


def test_look_off_matches_plain_render_and_auto_is_unchanged():
    dec = fake_decoded()
    e = core.Edit()
    plain, _ = core.render(dec, e, look=False)
    shown, _ = core.render(dec, e)
    assert not np.array_equal(shown, plain)          # display gets the look...
    a, b = e.copy(), e.copy()
    auto.auto_levels(dec, a)
    core.PREVIEW_LOOK = False
    try:
        auto.auto_levels(dec, b)
    finally:
        core.PREVIEW_LOOK = True
    assert (a.blacks, a.whites) == (b.blacks, b.whites)   # ...the auto tools don't


def _dark_scene():
    rng = np.random.default_rng(3)
    cam = (rng.uniform(0.0, 1.0, (120, 180, 1)) ** 2.5 * rng.uniform(0.5, 1.0, (120, 180, 3)) * 0.12)
    ident = np.linalg.inv(core.XYZ_FROM_SRGB)
    return core.Decoded(cam=cam.astype(np.float32), color=core.CameraColor(ident, [1, 1, 1]),
                        baseline=1.0, source_size=(180, 120))


def test_auto_exposure_centres_the_histogram():
    dec = _dark_scene()
    e = core.as_shot_edit(dec)
    auto.run_auto(dec, e, ["exposure", "levels", "shhi", "contrast"])
    L = auto._finished_luma(dec, e)
    assert abs(float(np.median(L)) - auto.TARGET_MEDIAN) < 0.05
    assert float((L > 0.985).mean()) <= auto.CENTER_MAX_CLIPPED + 0.01


def test_exposure_bias_still_applies_after_centring():
    dec = _dark_scene()
    plain, biased = core.as_shot_edit(dec), core.as_shot_edit(dec)
    auto.run_auto(dec, plain, ["exposure"])
    auto.run_auto(dec, biased, ["exposure"], auto.AutoOptions(exposure_bias=0.5))
    assert abs((biased.exposure - plain.exposure) - 0.5) < 0.06
