"""Photos taken in the same light get the same exposure and white balance."""
import math

import numpy as np

import raw_review_auto as auto
import raw_review_core as core


def _p(key, t, gain=-10.0, light=10.0, camera="Sony ILCE-7M3"):
    return {"key": key, "camera": camera, "time": t, "gain": gain, "light": light}


def test_groups_follow_time_camera_and_light():
    photos = [_p("a", 0), _p("b", 30, light=11.5), _p("c", 60, light=9.2),       # one series
              _p("d", 1000), _p("e", 1030),                                     # later: another series
              _p("f", 1060, light=16.0),                                        # walked outside: new light
              _p("g", 1070, camera="Nikon D3S"), _p("x", None)]                 # other camera / no time
    assert auto.same_light_groups(photos) == [["a", "b", "c"], ["d", "e"]]


def test_same_light_means_same_brightness_and_white_balance():
    m = [{"key": "a", "gain": -10.0, "exposure": +0.6, "temp": 6600, "tint": 11},
         {"key": "b", "gain": -10.0, "exposure": -0.2, "temp": 5200, "tint": 5},
         {"key": "c", "gain": -10.0, "exposure": -0.4, "temp": 5250, "tint": 6},
         {"key": "d", "gain": -9.0, "exposure": -1.3, "temp": 5300, "tint": 4}]  # shot a stop brighter
    r = auto.match_same_light(m)
    finished = {k: r[k][0] + g["gain"] for k, g in zip("abcd", m)}
    assert max(finished.values()) - min(finished.values()) < 0.06       # same finished brightness
    assert len({r[k][1:] for k in "abcd"}) == 1                         # one white balance
    assert r["a"][1] == 5275 and r["a"][0] == -0.25     # median of exposure + gain: -10.25


def test_a_photo_far_off_keeps_its_own_values():
    m = [{"key": "a", "gain": -10.0, "exposure": 0.0, "temp": 5200, "tint": 5},
         {"key": "b", "gain": -10.0, "exposure": 0.1, "temp": 5300, "tint": 5},
         {"key": "c", "gain": -10.0, "exposure": 2.5, "temp": 9000, "tint": 30}]
    r = auto.match_same_light(m)
    assert r["c"] == (2.5, 9000, 30)


def test_shot_info_gain():
    s = core.ShotInfo(exposure_time=1 / 640, fnumber=6.3, iso=100)
    assert math.isclose(s.gain_ev, math.log2((1 / 640) / 6.3 ** 2), rel_tol=1e-9)
    assert core.ShotInfo().gain_ev is None
    assert core.read_shot_info("/no/such/file").camera == ""
