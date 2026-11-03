"""
The Lightroom-style preview look is display only: it must brighten and enrich the picture like
Lightroom's default rendering, keep greys grey, and never change what the auto tools measure
(so the edits written to Lightroom stay the same).
"""
import numpy as np

import raw_review_core as core
import raw_review_auto as auto


def fake_decoded(seed=0):
    rng = np.random.default_rng(seed)
    cam = rng.uniform(0.02, 0.6, (120, 180, 3)).astype(np.float32)
    color = core.CameraColor(np.eye(3) * 0.8 + 0.05, [2.0, 1.0, 1.5])
    return core.Decoded(cam=cam, color=color, baseline=1.0, source_size=(180, 120))


def test_curve_is_monotone_and_spans_full_range():
    lut = core._LOOK_LUT
    assert lut[0] == 0.0 and abs(lut[-1] - 1.0) < 1e-6
    assert np.all(np.diff(lut) >= 0)


def test_brightens_midtones_and_keeps_greys_neutral():
    grey = np.full((4, 4, 3), 0.4, np.float32)
    out = core.apply_look(grey)
    assert np.allclose(out[..., 0], out[..., 1]) and np.allclose(out[..., 1], out[..., 2])
    assert out[0, 0, 0] > 0.55                       # ~1 stop brighter, like Lightroom


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
    assert shown.mean() > plain.mean()               # display gets the look...
    a, b = e.copy(), e.copy()
    auto.auto_levels(dec, a)
    core.PREVIEW_LOOK = False
    try:
        auto.auto_levels(dec, b)
    finally:
        core.PREVIEW_LOOK = True
    assert (a.blacks, a.whites) == (b.blacks, b.whites)   # ...the auto tools don't
