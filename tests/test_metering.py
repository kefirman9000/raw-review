"""Auto exposure meters like a photographer: big plain areas (white wall, sky, dark background)
must not swing the exposure of the rest of the photo."""
import numpy as np

import raw_review_core as core
import raw_review_auto as auto

IDENT = np.linalg.inv(core.XYZ_FROM_SRGB)


def _scene(seed=0, h=300, w=450):
    """A 'subject': textured blocks of different brightness, like shelves or a building."""
    rng = np.random.default_rng(seed)
    cam = np.zeros((h, w, 3), np.float32)
    for y in range(0, h, 30):
        for x in range(0, w, 30):
            cam[y:y + 30, x:x + 30] = rng.uniform(0.005, 0.12) * rng.uniform(0.8, 1.2, 3)
    cam *= rng.uniform(0.9, 1.1, (h, w, 1)).astype(np.float32)
    return cam


def _dec(cam):
    return core.Decoded(cam=cam, color=core.CameraColor(IDENT, [1, 1, 1]), baseline=1.0,
                        source_size=(cam.shape[1], cam.shape[0]))


def _with_plain(cam, value, top):
    h, w = cam.shape[:2]
    band = int(h * 0.3)
    plain = np.full((band, w, 3), value, np.float32) * (1 + 0.02 * np.random.default_rng(1)
                                                         .standard_normal((band, w, 1))).astype(np.float32)
    return (np.concatenate([plain, cam]) if top else np.concatenate([cam, plain])), band


def _scene_brightness(cam, rows):
    dec = _dec(cam)
    e = core.as_shot_edit(dec)
    auto.run_auto(dec, e, ["exposure", "levels", "shhi", "contrast", "colour"])
    L = (core.render(dec, e, look=False)[0] / 255.0) @ core.LUMA
    return float(np.median(L[rows]))


def test_plain_areas_barely_move_the_rest_of_the_photo():
    cam = _scene()
    h = cam.shape[0]
    L = cam @ core.LUMA
    base = _scene_brightness(cam, slice(0, h))
    bright, band = _with_plain(cam, float(np.percentile(L, 99)), top=True)
    dark, _ = _with_plain(cam, float(np.percentile(L, 1)), top=False)
    for img, rows in ((bright, slice(band, band + h)), (dark, slice(0, h))):
        shift = abs(np.log2(_scene_brightness(img, rows) / base) * 2.2)    # ~stops
        assert shift < 0.35, shift


def test_metering_ignores_how_the_photo_is_exposed():
    """The weights depend only on what's in the frame, not on how bright it was shot."""
    cam = _scene()
    w1 = auto.meter_weights(_dec(cam))
    w2 = auto.meter_weights(_dec(cam * 4))
    assert np.allclose(w1, w2, atol=1e-3)
