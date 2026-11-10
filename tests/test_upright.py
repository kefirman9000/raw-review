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
