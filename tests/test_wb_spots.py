"""
Auto white balance averages separate spots: one big off-white surface (a cream wall, a large
label) can't outvote several small true whites elsewhere in the frame.
"""
import numpy as np

import raw_review_core as core


def _scene(big_tint=(1.0, 0.93, 0.78), seed=0):
    """Neutral light (identity camera, as shot 1/1/1). A large cream surface plus five small white cards."""
    rng = np.random.default_rng(seed)
    cam = rng.uniform(0.02, 0.25, (240, 360, 3)).astype(np.float32)          # busy, coloured background
    cam[10:150, 10:200] = np.array(big_tint, np.float32) * 0.62               # one big cream area
    for (y, x) in [(170, 30), (175, 230), (40, 260), (120, 300), (200, 140)]:
        cam[y:y + 22, x:x + 30] = 0.60                                         # small true whites
    cam += rng.normal(0, 0.002, cam.shape).astype(np.float32)
    color = core.CameraColor(np.linalg.inv(core.XYZ_FROM_SRGB), [1, 1, 1])
    return core.Decoded(cam=np.clip(cam, 0, 1), color=color, baseline=1.0, source_size=(360, 240))


def _cast(dec, wb):
    mul = dec.color.temp_tint_to_mul(wb.temp, wb.tint)
    n = 1.0 / mul                                    # the colour that comes out grey
    return float(np.hypot(np.log(n[0] / n[1]), np.log(n[2] / n[1])))


def test_big_cream_surface_does_not_outvote_small_whites():
    dec = _scene()
    wb = core.auto_white_balance(dec)
    assert wb.applied
    assert _cast(dec, wb) < 0.06                     # stays close to the true (neutral) light
    assert "spots" in wb.note


def test_spot_neutral_drops_the_odd_one_out():
    sel = np.zeros((40, 40), bool)
    logc = np.zeros((40, 40, 3), np.float32)
    Y = np.ones((40, 40), np.float32)
    for y, x in [(2, 2), (2, 20), (20, 2)]:          # three grey spots
        sel[y:y + 5, x:x + 5] = True
    sel[25:38, 25:38] = True                         # one big orange spot
    logc[25:38, 25:38] = np.log([1.0, 0.8, 0.55])
    n_log, used, spots = core._spot_neutral(sel, logc, Y, True)
    assert spots == 3 and not used[30, 30]
    assert np.allclose(n_log, 0.0, atol=1e-6)
