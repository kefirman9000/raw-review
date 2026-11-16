"""Per-camera calibration of the preview against Lightroom exports."""
import os

import cv2
import numpy as np

import raw_review_calibrate as calib
import raw_review_core as core
from test_metering import _dec, _scene


def _pair(shift):
    dec = _dec(_scene(3))
    dec.color.camera = "Test Cam"
    e = core.as_shot_edit(dec)
    lr = e.copy()
    lr.exposure += shift
    lr_img = cv2.resize(core.render(dec, lr)[0], (200, 133), interpolation=cv2.INTER_AREA)   # exported smaller
    return dec, e, lr_img


def test_offset_is_found():
    dec, e, lr = _pair(0.4)
    assert abs(core.calibration_offset(dec, e, lr) - 0.4) < 0.05


def test_camera_offset_brightens_preview_and_is_then_zero():
    dec, e, lr = _pair(0.4)
    core.CAMERA_EV["Test Cam"] = 0.4
    try:
        assert abs(core.calibration_offset(dec, e, lr)) < 0.05
    finally:
        core.CAMERA_EV.clear()


def test_black_export_is_skipped():
    dec, e, _ = _pair(0)
    assert core.calibration_offset(dec, e, np.zeros((50, 50, 3), np.uint8)) is None


def test_summarize_and_pairs(tmp_path):
    rows = calib.summarize([{"camera": "A", "offset": v} for v in (0.3, 0.35, 0.25)] +
                           [{"camera": "B", "offset": 0.2}], {"A": 0.1})
    a, b = rows
    assert a["ok"] and a["new"] == 0.4 and not b["ok"]
    for n in ("_DSC1.jpg", "_DSC2-Edit.jpg", "_DSC10.jpg"):
        (tmp_path / n).write_bytes(b"x")
    raws = ["/r/_DSC1.ARW", "/r/_DSC2.ARW", "/r/_DSC3.ARW"]
    got = {os.path.basename(r): os.path.basename(j) for r, j in calib.find_pairs(raws, str(tmp_path))}
    assert got == {"_DSC1.ARW": "_DSC1.jpg", "_DSC2.ARW": "_DSC2-Edit.jpg"}
