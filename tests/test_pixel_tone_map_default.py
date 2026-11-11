"""
Pixel's local HDR tone map (ProfileGainTableMap) is opt-in: Lightroom Classic does not apply it,
so a preview that did was 1.3-1.9 stops brighter than Lightroom for the same settings.
"""
import dataclasses

import numpy as np
import pytest

import dng_profile


@pytest.fixture(autouse=True)
def restore_options():
    saved = dataclasses.asdict(dng_profile.OPTIONS)
    yield
    for k, v in saved.items():
        setattr(dng_profile.OPTIONS, k, v)


def model_with_tone_map():
    table = np.full((2, 2, 4), 3.0, np.float32)                    # lifts everything 3x
    gtm = dng_profile.GainTableMap(2, 2, 4, 1.0, 1.0, 0.0, 0.0, [0, 0, 0, 0, 1], table)
    prof = dng_profile.DngProfile(temps=[6504.0], cm=[np.eye(3)], fm=[None], hsm=[None], gain_table=gtm)
    return dng_profile.DngColorModel(prof, lambda x, y: (6504.0, 0.0), lambda t, tint: (0.3127, 0.3290))


def test_off_by_default():
    assert dng_profile.ProfileOptions().hdr_tone_map is False
    model = model_with_tone_map()
    assert not model.has_hdr_tone_map
    cam = np.full((4, 4, 3), 0.1, np.float32)
    plain = model.render_linear(cam, [1.0, 1.0, 1.0], 0.0, 0.0)
    dng_profile.OPTIONS.hdr_tone_map = True
    model._hsm_cache = None
    lifted = model.render_linear(cam, [1.0, 1.0, 1.0], 0.0, 0.0)
    assert model.has_hdr_tone_map
    assert lifted.mean() > 2.5 * plain.mean()                      # the table is what brightens


def test_old_settings_file_does_not_switch_it_back_on():
    dng_profile.apply_saved_options({"hdr_tone_map": True, "look_strength": 0.5, "nonsense": 1}, "1.9.9")
    assert dng_profile.OPTIONS.hdr_tone_map is False               # the old default, not a choice
    assert dng_profile.OPTIONS.look_strength == 0.5                # everything else is kept


def test_choice_made_since_then_is_kept():
    dng_profile.apply_saved_options({"hdr_tone_map": True}, "1.9.10")
    assert dng_profile.OPTIONS.hdr_tone_map is True
    dng_profile.apply_saved_options({"hdr_tone_map": False}, "v2.0.0")
    assert dng_profile.OPTIONS.hdr_tone_map is False


def test_missing_or_odd_settings_are_harmless():
    dng_profile.apply_saved_options(None, "")
    dng_profile.apply_saved_options({}, None)
    assert dng_profile.OPTIONS.hdr_tone_map is False
