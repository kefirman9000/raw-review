"""Learning from Lightroom corrections."""
from types import SimpleNamespace

import raw_review_core as core
import raw_review_learn as learn
from raw_review_auto import AutoOptions


def _rec(**diffs):
    return {"camera": "SONY ILCE-7M3", "diffs": diffs}


def test_correction_ignores_rounding():
    wrote = learn.snapshot(core.Edit(exposure=0.5, contrast=10, temp=5200))
    now = core.Edit(exposure=0.52, contrast=10.4, temp=5600)
    assert learn.correction(wrote, now) == {"temp": 400}


def test_consistent_corrections_are_suggested():
    recs = [_rec(exposure=0.3, shadows=12), _rec(exposure=0.25), _rec(exposure=0.35, shadows=-20), _rec(exposure=0.3)]
    rows = {r["key"]: r for r in learn.summarize(recs)}
    assert rows["exposure"]["suggest"] and abs(rows["exposure"]["mean"] - 0.3) < 1e-9
    assert not rows["shadows"]["suggest"]          # two photos, opposite ways


def test_too_few_photos_is_not_suggested():
    rows = learn.summarize([_rec(exposure=0.5), _rec(exposure=0.5)])
    assert not rows[0]["suggest"]


def test_camera_filter():
    recs = [_rec(exposure=0.3)] * 3 + [{"camera": "Pixel 8", "diffs": {"exposure": -0.5}}] * 3
    assert learn.summarize(recs, "Pixel 8")[0]["mean"] == -0.5


def test_apply_to_options():
    opts = AutoOptions()
    rows = [{"key": "exposure", "mean": 0.3}, {"key": "contrast", "mean": 8.0},
            {"key": "vibrance", "mean": 10.0}, {"key": "temp", "mean": 330.0}, {"key": "shadows", "mean": 12.0}]
    done = learn.apply_to_options(opts, rows, {"exposure", "contrast", "vibrance", "temp", "shadows"})
    assert abs(opts.exposure_bias - 0.3) < 1e-9
    assert opts.punch == 20 and opts.colour == 20
    assert opts.rules["temp"] == ["add", 350] and opts.rules["shadows"] == ["add", 12]
    assert len(done) == 5
    learn.apply_to_options(opts, rows, {"shadows"})     # adds on top of an existing add rule
    assert opts.rules["shadows"] == ["add", 24]
