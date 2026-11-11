"""'Never use Lightroom AI masks': no Select Subject / Select Sky mask is added or written, and RAW Review's own
earlier ones come out of the sidecar. (Lightroom records these masks in exported JPEGs; agencies may look for AI.)"""
import os
import time

import pytest

import raw_review_auto as auto
import raw_review_core as core


@pytest.fixture
def raw(tmp_path):
    p = tmp_path / "_DSC8068.ARW"
    p.write_bytes(os.urandom(64 * 1024))
    old = time.time() - 3600
    os.utime(p, (old, old))
    return str(p)


@pytest.fixture(autouse=True)
def masks_allowed_again():
    yield
    core.NO_AI_MASKS = False


def sidecar_text(raw):
    return open(os.path.splitext(raw)[0] + ".xmp", encoding="utf-8").read()


def with_subject_mask():
    e = core.Edit()
    e.subj_on, e.subj_texture, e.subj_clarity = True, 10, 5
    return e


def test_masks_are_written_when_allowed(raw):
    core.write_xmp(raw, with_subject_mask())
    assert "Mask/Image" in sidecar_text(raw) and "Subject" in sidecar_text(raw)


def test_no_ai_mask_is_written_when_turned_off(raw):
    core.NO_AI_MASKS = True
    core.write_xmp(raw, with_subject_mask())
    assert "Mask/Image" not in sidecar_text(raw)


def test_earlier_raw_review_masks_come_out(raw):
    core.write_xmp(raw, with_subject_mask())
    assert "Mask/Image" in sidecar_text(raw)
    core.NO_AI_MASKS = True
    core.write_xmp(raw, with_subject_mask())                     # written again with the setting on
    assert "Mask/Image" not in sidecar_text(raw)


def test_sliders_and_auto_do_not_turn_masks_on(raw):
    core.NO_AI_MASKS = True
    e = core.Edit()
    core.change_setting(e, "subj_texture", "set", 20)
    assert not e.subj_on and not e.sky_on
    note = auto.auto_masks(None, e)
    assert "off" in note and not e.subj_on and not e.sky_on
