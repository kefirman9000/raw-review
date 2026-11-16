"""Lens vignetting removed in the preview, like Lightroom's lens profile correction."""
import numpy as np
import pytest

import raw_review_core as core

pytest.importorskip("lensfunpy")


def _shot(lens="FE 28-70mm F3.5-5.6 OSS", focal=28.0, f=3.5):
    return core.ShotInfo(make="SONY", model="ILCE-7M3", lens=lens, focal=focal, fnumber=f)


def test_known_lens_brightens_corners_only():
    gain, name = core._lens_vignette_gain(_shot(), 600, 400)
    assert "28-70" in name
    assert gain[0, 0] > 1.3 and abs(gain[200, 300] - 1.0) < 0.02


def test_unknown_lens_or_wrong_focal_is_left_alone():
    assert core._lens_vignette_gain(_shot(lens="E 70-300mm F4.5-6.3 A047", focal=300), 600, 400) == (None, None)
    assert core._lens_vignette_gain(_shot(focal=200), 600, 400) == (None, None)


def test_switch_off():
    core.LENS_VIGNETTE = False
    try:
        assert core._lens_vignette_gain(_shot(), 600, 400) == (None, None)
    finally:
        core.LENS_VIGNETTE = True


def test_applied_to_linear_data():
    cam = np.full((400, 600, 3), 0.1, np.float32)
    out, name = core._apply_lens_vignetting(cam, _shot())
    assert name and out[0, 0, 0] > 0.13 and abs(out[200, 300, 0] - 0.1) < 0.003
