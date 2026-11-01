"""Cull suggestions (soft / blown / bursts) and the updater's choice of download."""
import io
import json

import numpy as np

import raw_review_cull as cull
import updater


def stats(sharp=3.0, clipped=0.0, lift=0.0, seed=0, noise=0.0):
    rng = np.random.default_rng(seed)
    base = rng.normal(size=cull.SIG_SIZE * cull.SIG_SIZE).astype(np.float32)
    if noise:
        base = base + np.random.default_rng(seed + 1000).normal(scale=noise, size=base.shape).astype(np.float32)
    base -= base.mean()
    base /= np.linalg.norm(base)
    return cull.CullStats(sharp=sharp, clipped=clipped, lift_ev=lift, sig=base)


def test_flags():
    assert stats(sharp=1.5).soft and not stats(sharp=3.0).soft
    assert not stats(sharp=0.0).soft                       # couldn't measure: no flag
    assert stats(clipped=0.2).blown and not stats(clipped=0.001).blown
    assert stats(lift=2.6).dark and not stats(lift=0.5).dark


def test_bursts_keep_the_sharpest():
    ordered = [("a", stats(sharp=2.4, seed=1)), ("b", stats(sharp=3.1, seed=1, noise=0.05)),
               ("c", stats(sharp=2.2, seed=1, noise=0.05)), ("d", stats(sharp=3.0, seed=2))]
    groups = cull.group_bursts(ordered)
    assert groups == [["a", "b", "c"]]
    why = cull.suggestions(ordered)
    assert "b" not in why                                  # the sharpest frame is never suggested
    assert any(w.startswith("similar to b") for w in why["a"])
    assert "d" not in why
    labels = cull.burst_labels(ordered)
    assert labels["b"].endswith("★") and labels["a"] == "burst 1/3"


def test_unanalysed_photo_breaks_a_burst():
    ordered = [("a", stats(seed=1)), ("b", None), ("c", stats(seed=1))]
    assert cull.group_bursts(ordered) == []


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


RELEASE = {"tag_name": "v9.0.0", "body": "notes", "html_url": "https://example/rel",
           "assets": [{"name": "RAW_Review-Windows-v9.0.0.zip", "browser_download_url": "https://x/win.zip"},
                      {"name": "RAW_Review-v9.0.0.zip", "browser_download_url": "https://x/src.zip"},
                      {"name": "SHA256SUMS", "browser_download_url": "https://x/sums"}]}


def test_source_install_takes_the_source_zip(monkeypatch):
    monkeypatch.setattr(updater, "FROZEN", False)
    monkeypatch.setattr(updater, "_open", lambda url, timeout: FakeResp(json.dumps(RELEASE).encode()))
    info = updater._from_release(5)
    assert info["url"] == "https://x/src.zip" and info["newer"]


def test_program_takes_the_windows_zip(monkeypatch):
    monkeypatch.setattr(updater, "FROZEN", True)
    monkeypatch.setattr(updater, "_open", lambda url, timeout: FakeResp(json.dumps(RELEASE).encode()))
    info = updater._from_release(5)
    assert info["url"] == "https://x/win.zip" and info["sums"] == "https://x/sums"


def test_program_without_windows_build_offers_nothing(monkeypatch):
    rel = dict(RELEASE, assets=[RELEASE["assets"][1]])
    monkeypatch.setattr(updater, "FROZEN", True)
    monkeypatch.setattr(updater, "_open", lambda url, timeout: FakeResp(json.dumps(rel).encode()))
    assert updater._from_release(5)["url"] is None


def test_measure_on_a_decoded_image():
    import raw_review_core as core
    rng = np.random.default_rng(3)
    cam = rng.uniform(0.05, 0.6, size=(120, 180, 3)).astype(np.float32)
    cam[:30, :60] = 1.0                                     # a clipped patch: 1/12 of the frame
    dec = core.Decoded(cam=cam, color=None, baseline=1.2, source_size=(180, 120),
                       quality=core.ImageQuality(sharp_ratio=1.4, measured=True))
    e = core.Edit()
    e.exposure = 0.3
    st = cull.measure(dec, e)
    assert st.soft and st.blown and not st.dark
    assert abs(st.clipped - 1 / 12) < 0.01
    assert cull.similarity(st.sig, cull.measure(dec, e).sig) > 0.999
