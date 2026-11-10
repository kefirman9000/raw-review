"""The Upright preview: converging verticals come out vertical, horizons level, nothing to fix = no warp."""
import math

import cv2
import numpy as np

import raw_review_core as core


def _facade(keystone=150, roll=2.0, W=1200, H=800):
    img = np.full((H, W, 3), 200, np.uint8)
    for x in range(100, W, 110):
        cv2.line(img, (x, 40), (x, H - 40), (30, 30, 30), 4)
    for y in range(80, H, 140):
        cv2.line(img, (60, y), (W - 60, y), (60, 60, 60), 3)
    src = np.float32([[0, 0], [W, 0], [W, H], [0, H]])
    dst = np.float32([[keystone, 0], [W - keystone, 0], [W, H], [0, H]])     # camera tilted up
    K = cv2.getPerspectiveTransform(src, dst)
    R = np.vstack([cv2.getRotationMatrix2D((W / 2, H / 2), roll, 1.0), [0, 0, 1]])
    return cv2.warpPerspective(img, R @ K, (W, H), borderValue=(200, 200, 200))


def _worst(img):
    seg, _ln = core._segments(img)
    dx, dy = seg[:, 2] - seg[:, 0], seg[:, 3] - seg[:, 1]
    lean = np.degrees(np.arctan2(dx, dy))
    lean = np.where(lean > 90, lean - 180, np.where(lean < -90, lean + 180, lean))
    tilt = np.degrees(np.arctan2(dy, dx))
    tilt = np.where(tilt > 90, tilt - 180, np.where(tilt < -90, tilt + 180, tilt))
    return np.abs(lean[np.abs(lean) <= 25]).max(), np.abs(tilt[np.abs(tilt) <= 15]).max()


def _apply(img, mode):
    H = core.upright_matrix(img, mode)
    assert H is not None
    return cv2.warpPerspective(img, H, img.shape[1::-1], borderValue=(200, 200, 200))


def test_vertical_straightens_converging_verticals():
    img = _facade()
    assert _worst(img)[0] > 8                      # clearly converging to start with
    lean, tilt = _worst(_apply(img, "Vertical"))
    assert lean < 0.5 and tilt < 0.5


def test_level_levels_horizontals_only():
    lean, tilt = _worst(_apply(_facade(), "Level"))
    assert tilt < 0.5 and lean > 5                 # verticals are left converging, like Lightroom's Level


def test_nothing_to_fix_means_no_warp():
    img = _facade(keystone=0, roll=0.0)
    assert core.upright_matrix(img, "Vertical") is None
    assert core.upright_matrix(img, "Level") is None
    assert core.upright_matrix(img, "Off") is None


def test_display_mapping_round_trips_through_the_warp():
    H = core.upright_matrix(_facade(), "Full")
    g = core.Geometry(H=H)
    dx, dy = g.to_display(321.0, 456.0)
    sx, sy = g.to_source(dx, dy)
    assert math.isclose(sx, 321.0, abs_tol=1e-6) and math.isclose(sy, 456.0, abs_tol=1e-6)


def _facade_with_diagonals():
    img = _facade()
    for k in range(4):                                  # roof-truss diagonals ~20° off vertical
        cv2.line(img, (900 + 60 * k, 0), (1050 + 60 * k, 420), (90, 90, 90), 3)
    return img


def test_suggested_guides_are_real_verticals_not_diagonals():
    img = _facade_with_diagonals()
    guides = core.suggest_guides(img, "v")
    assert len(guides) >= 2
    h, w = img.shape[:2]
    for x1, y1, x2, y2 in guides:
        lean = math.degrees(math.atan2(abs(x2 - x1) * w, abs(y2 - y1) * h))
        assert lean < 12                                # no truss diagonals
    H = core.upright_matrix(img, "Guided", guides)
    # the facade's own verticals, through the camera tilt and then the correction, must be plumb
    W, Hh = 1200, 800
    K = cv2.getPerspectiveTransform(np.float32([[0, 0], [W, 0], [W, Hh], [0, Hh]]),
                                    np.float32([[150, 0], [W - 150, 0], [W, Hh], [0, Hh]]))
    R = np.vstack([cv2.getRotationMatrix2D((W / 2, Hh / 2), 2.0, 1.0), [0, 0, 1]])
    T = H @ R @ K
    for x in range(100, W, 110):
        a, b = T @ [x, 40, 1], T @ [x, Hh - 40, 1]
        a, b = a[:2] / a[2], b[:2] / b[2]
        assert abs(math.degrees(math.atan2(b[0] - a[0], b[1] - a[1]))) < 0.6


def test_constrained_crop_has_no_empty_corners():
    img = _facade()
    h, w = img.shape[:2]
    Hn = np.array(core.upright_all(img)["Vertical"]).reshape(3, 3)
    box = core.constrained_box(Hn, w, h)
    assert box is not None and 0.5 < box[2] - box[0] < 1
    warped = cv2.warpPerspective(np.full_like(img, 255), np.diag([w, h, 1.0]) @ Hn @ np.diag([1 / w, 1 / h, 1.0]),
                                 (w, h), borderValue=(0, 0, 0))
    l, t, r, b = (int(round(v)) for v in (box[0] * w + 1, box[1] * h + 1, box[2] * w - 1, box[3] * h - 1))
    assert warped[t:b, l:r].min() == 255                # photo everywhere inside the crop
    assert abs((box[2] - box[0]) * w / ((box[3] - box[1]) * h) - w / h) < 0.01   # photo's own shape


def test_lightroom_gets_the_correction_guides_and_crop(tmp_path):
    import re
    img = _facade()
    h, w = img.shape[:2]
    e = core.Edit()
    e.upright = "Guided"
    e.upright_guides = core.suggest_guides(img, "v")
    e.upright_matrices = core.upright_all(img, e.upright_guides)
    e.crop_box = core.constrained_box(e.upright_matrices["Guided"], w, h)
    for flip in (0, 6):
        raw = tmp_path / f"p{flip}.ARW"
        raw.write_bytes(b"x")
        core.write_xmp(str(raw), e, (w, h), flip=flip)
        x = open(core.sidecar_path(str(raw))).read()
        got = dict(re.findall(r'crs:(\w+)="([^"]*)"', x))
        assert got["PerspectiveUpright"] == "5" and got["UprightTransformCount"] == "6"
        assert got["CropConstrainToWarp"] == "1" and got["HasCrop"] == "True"
        assert int(got["UprightFourSegmentsCount"]) == len(e.upright_guides)
        M = np.array([float(v) for v in got["UprightTransform_5"].split(",")]).reshape(3, 3)
        F = core.shown_to_recorded(flip)
        expect = F @ np.array(e.upright_matrices["Guided"]).reshape(3, 3) @ np.linalg.inv(F)
        assert np.allclose(M, expect / expect[2, 2], atol=1e-6)
        if flip == 6:                                   # portrait: the crop is turned into the recorded frame
            l, t, r, b = e.crop_box
            assert math.isclose(float(got["CropLeft"]), t, abs_tol=1e-5)
            assert math.isclose(float(got["CropTop"]), 1 - r, abs_tol=1e-5)
    e.upright, e.upright_matrices = "Off", None          # switching Upright off removes what we wrote
    core.write_xmp(str(tmp_path / "p0.ARW"), e, (w, h))
    x = open(core.sidecar_path(str(tmp_path / "p0.ARW"))).read()
    assert "UprightTransform_" not in x and "UprightFourSegments" not in x
