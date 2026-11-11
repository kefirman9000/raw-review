"""
Camera look: for a DNG that carries the camera's local tone map (Pixel), auto fits the tone
sliders to that rendering instead of centring the histogram.
"""
import dataclasses

import numpy as np
import pytest

import dng_profile
import raw_review_auto as auto
import raw_review_core as core

TONE = ("exposure", "levels", "shhi", "contrast", "colour")


@pytest.fixture(autouse=True)
def restore():
    saved, look = dataclasses.asdict(dng_profile.OPTIONS), auto.CAMERA_LOOK
    yield
    auto.CAMERA_LOOK = look
    for k, v in saved.items():
        setattr(dng_profile.OPTIONS, k, v)


def decoded(tone_map=True, seed=0):
    """A dark scene whose DNG asks for shadows x3 and highlights x0.7, like a Pixel file."""
    rng = np.random.default_rng(seed)
    cam = (rng.uniform(0.0, 1.0, (60, 80, 3)) ** 3 * 0.5).astype(np.float32)
    gains = np.linspace(3.0, 0.7, 33, dtype=np.float32)
    table = np.broadcast_to(gains, (2, 2, 33)).copy()
    gtm = dng_profile.GainTableMap(2, 2, 33, 1.0, 1.0, 0.0, 0.0, [0.1, 0.2, 0.04, 0.09, 0.27], table)
    prof = dng_profile.DngProfile(temps=[6504.0], cm=[np.eye(3)], fm=[None], hsm=[None],
                                  gain_table=gtm if tone_map else None, baseline_ev=0.5)
    color = core.CameraColor(np.zeros((4, 3)), [1.0, 1.0, 1.0], prof)
    return core.Decoded(cam=cam, color=color, baseline=1.0, source_size=(80, 60))


def luma(dec, edit, tone_map=None):
    lin = dec.color.model.render_linear(dec.cam, dec.color.as_shot_mul, edit.exposure, dec.color.base_ev,
                                        tone_map=tone_map)
    g = core._srgb_encode(core.recover_highlights(lin, edit.highlights)).astype(np.float32)
    return np.clip(core.apply_tone(g, edit, None, dec.color.base_contrast) @ core.LUMA, 0, 1)


def test_used_only_when_the_file_has_a_tone_map_the_preview_leaves_out():
    assert auto.has_camera_look(decoded())
    assert not auto.has_camera_look(decoded(tone_map=False))
    dng_profile.OPTIONS.hdr_tone_map = True                # preview already shows the camera's look
    assert not auto.has_camera_look(decoded())
    dng_profile.OPTIONS.hdr_tone_map = False
    auto.CAMERA_LOOK = False
    assert not auto.has_camera_look(decoded())


def test_sliders_reproduce_the_cameras_brightness():
    dec = decoded()
    target = luma(dec, core.Edit(), tone_map=True)         # the camera's own rendering
    edit = core.as_shot_edit(dec)
    before = float(np.sqrt(np.mean((luma(dec, edit) - target) ** 2)))
    auto.run_auto(dec, edit, TONE)
    after = float(np.sqrt(np.mean((luma(dec, edit) - target) ** 2)))
    assert edit.exposure > 0.3                             # the lift Lightroom leaves out
    assert after < 0.35 * before and after < 0.05
    assert abs(float(np.median(luma(dec, edit)) - np.median(target))) < 0.04
    assert edit.lights == 0 and edit.vibrance == 0 and edit.saturation == 0
    assert any("camera's own rendering" in n for n in edit.notes)
    for key, (lo, hi) in auto.CAMERA_LOOK_RANGES.items():
        assert lo <= getattr(edit, key) <= hi


def test_darker_than_centring_the_histogram():
    dec = decoded()
    look, centred = core.as_shot_edit(dec), core.as_shot_edit(dec)
    auto.run_auto(dec, look, TONE)
    auto.CAMERA_LOOK = False
    auto.run_auto(dec, centred, TONE)
    assert not any("camera's own rendering" in n for n in centred.notes)
    assert look.exposure < centred.exposure


def test_only_the_requested_tools_change_and_bias_is_added():
    dec = decoded()
    edit = core.as_shot_edit(dec)
    edit.contrast, edit.shadows, edit.vibrance = 7, 9, 11
    auto.run_auto(dec, edit, ["exposure"])
    assert (edit.contrast, edit.shadows, edit.vibrance) == (7, 9, 11)
    plain = edit.exposure
    again = core.as_shot_edit(dec)
    again.contrast, again.shadows = 7, 9
    auto.run_auto(dec, again, ["exposure"], auto.AutoOptions(exposure_bias=0.5))
    assert abs(again.exposure - (plain + 0.5)) < 0.051


def test_other_files_keep_the_generic_tools():
    dec = decoded(tone_map=False)
    edit = core.as_shot_edit(dec)
    auto.run_auto(dec, edit, TONE)
    assert any(n.startswith("Auto exposure") and "histogram centred" in n for n in edit.notes)
