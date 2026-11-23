"""v1.9.25: exposure follows the scene's key, calibration fits a slope with Exposure, learning per camera."""
import raw_review_auto as auto
import raw_review_calibrate as calib
import raw_review_core as core
import raw_review_learn as learn


def test_calibration_fits_a_slope_with_exposure():
    res = [{"camera": "A", "offset": -0.5 + 0.2 * x, "exposure": x} for x in (0.0, 0.5, 1.0, 2.0)]
    r = calib.summarize(res, {"A": 0.0})[0]
    assert abs(r["slope"] - 0.2) < 1e-6 and abs(r["new"] + 0.5) < 0.01 and r["ok"]
    flat = calib.summarize([{"camera": "B", "offset": 0.3, "exposure": 0.0}] * 3, {})[0]
    assert flat["slope"] == 0.0


def test_camera_ev_uses_the_slope():
    core.CAMERA_EV_SLOPE["X"] = 0.1
    try:
        assert abs(core.camera_ev("X", 2.0) - 0.2) < 1e-9
    finally:
        core.CAMERA_EV_SLOPE.clear()


def test_target_follows_scene_key():
    class D:
        masks = {}
    hi, lo = D(), D()
    hi.masks = {"_scene_key": 2.0}
    lo.masks = {"_scene_key": -2.0}
    assert auto.exposure_target(hi) > auto.TARGET_MEDIAN > auto.exposure_target(lo)


def test_learn_key_slope_and_camera_bias():
    recs = [{"camera": "C", "key": k, "diffs": {"exposure": 0.3 * k + 0.1}} for k in (-2, -1, 0, 1, 2)]
    rows = learn.summarize(recs, "C")
    kr = next(r for r in rows if r["key"] == "key_follow")
    assert kr["suggest"] and abs(kr["mean"] - 0.3) < 1e-6

    class O:
        exposure_bias, punch, colour, rules = 0.0, 0.0, 0.0, {}
    tuning = {"camera_bias": {}, "key_follow": 0.35}
    learn.apply_to_options(O, rows, {"exposure", "key_follow"}, camera="C", tuning=tuning)
    assert tuning["key_follow"] == 0.65 and tuning["camera_bias"]["C"] == 0.1 and O.exposure_bias == 0.0
