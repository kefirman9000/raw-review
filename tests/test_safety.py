"""
RAW Review's core promise: the RAW file is never changed, and only Lightroom's sidecar is written.
These tests use stand-in RAW files (random bytes with a RAW extension): writing a sidecar never needs
to decode the photo, so no camera files or rawpy are needed.
"""
import hashlib
import os
import time

import pytest

import raw_review_core as core


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


@pytest.fixture
def raw(tmp_path):
    p = tmp_path / "IMG_0001.CR3"
    p.write_bytes(os.urandom(256 * 1024))
    old = time.time() - 3600
    os.utime(p, (old, old))                      # a clearly older date, so any touch would show
    return str(p)


def edit(**kw):
    e = core.Edit()
    e.wb_mode = "custom"
    e.temp, e.tint, e.exposure, e.contrast = 4800.0, 7.0, 0.7, 12
    for k, v in kw.items():
        setattr(e, k, v)
    return e


def fingerprint(path):
    st = os.stat(path)
    return st.st_size, st.st_mtime_ns, sha(path)


# ---------------------------------------------------------------- the RAW is never touched

def test_new_sidecar_leaves_raw_identical(raw):
    before = fingerprint(raw)
    msg = core.write_xmp(raw, edit())
    assert fingerprint(raw) == before
    assert os.path.isfile(core.sidecar_path(raw))
    assert msg.startswith("Wrote")
    assert sorted(os.listdir(os.path.dirname(raw))) == ["IMG_0001.CR3", "IMG_0001.xmp"]   # no temp files left


def test_rewriting_sidecar_leaves_raw_identical(raw):
    before = fingerprint(raw)
    for exp in (0.3, -1.2, 2.0):
        core.write_xmp(raw, edit(exposure=exp, angle=1.5))
    assert fingerprint(raw) == before


def test_sidecar_name_is_never_a_photo(raw):
    assert core.sidecar_path(raw).endswith(".xmp")
    for bad in ("IMG_0001.CR3", "IMG_0001.jpg", "IMG_0001.dng", "IMG_0001.tif", "other.NEF"):
        with pytest.raises(core.RawSafetyError):
            core._check_target(raw, os.path.join(os.path.dirname(raw), bad))


def test_missing_raw_is_refused(tmp_path):
    with pytest.raises(FileNotFoundError):
        core.write_xmp(str(tmp_path / "gone.NEF"), edit())
    assert os.listdir(tmp_path) == []


# ---------------------------------------------------------------- round trip and merging

def test_settings_read_back(raw):
    core.write_xmp(raw, edit(), meta={"rating": 4, "label": "Green"})
    ex = core.read_existing(raw, core.Edit())
    e = ex["edit"]
    assert e is not None
    assert e.exposure == pytest.approx(0.7)
    assert e.contrast == 12
    assert e.wb_mode == "custom"
    assert e.temp == pytest.approx(4800, abs=1)
    assert ex["rating"] == 4 and ex["label"] == "Green"


FOREIGN_SIDECAR = """<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Adobe XMP Core 7.0">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
    crs:Clarity2012="25" crs:Exposure2012="-0.50">
   <dc:title><rdf:Alt><rdf:li xml:lang="x-default">Old Mill at Dawn</rdf:li></rdf:Alt></dc:title>
   <dc:subject><rdf:Bag><rdf:li>mill</rdf:li><rdf:li>river</rdf:li></rdf:Bag></dc:subject>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
"""


def test_existing_sidecar_keeps_other_settings_and_is_backed_up_once(raw):
    side = core.sidecar_path(raw)
    with open(side, "w", encoding="utf-8") as f:
        f.write(FOREIGN_SIDECAR)
    original = sha(side)
    core.write_xmp(raw, edit(exposure=0.4))
    core.write_xmp(raw, edit(exposure=0.9))
    text = open(side, encoding="utf-8").read()
    assert "Old Mill at Dawn" in text and "river" in text          # titles/keywords (e.g. IPTC Tagger's) kept
    assert 'Clarity2012="25"' in text or ">25<" in text            # a setting RAW Review doesn't control
    assert core.read_existing(raw, core.Edit())["edit"].exposure == pytest.approx(0.9)
    assert sha(side + ".bak") == original                          # the first version, kept once


# ---------------------------------------------------------------- undo approval

def test_undo_removes_a_sidecar_that_did_not_exist(raw, tmp_path):
    snap = core.snapshot_output(raw, str(tmp_path / "store"))
    before = fingerprint(raw)
    core.write_xmp(raw, edit())
    written = core.file_hash(core.sidecar_path(raw))
    core.restore_output(raw, snap, written)
    assert not os.path.exists(core.sidecar_path(raw))
    assert fingerprint(raw) == before


def test_undo_puts_back_the_previous_sidecar_exactly(raw, tmp_path):
    side = core.sidecar_path(raw)
    with open(side, "w", encoding="utf-8") as f:
        f.write(FOREIGN_SIDECAR)
    original = sha(side)
    snap = core.snapshot_output(raw, str(tmp_path / "store"))
    core.write_xmp(raw, edit())
    core.restore_output(raw, snap, core.file_hash(side))
    assert sha(side) == original


def test_undo_refuses_when_lightroom_saved_since(raw, tmp_path):
    snap = core.snapshot_output(raw, str(tmp_path / "store"))
    core.write_xmp(raw, edit())
    written = core.file_hash(core.sidecar_path(raw))
    with open(core.sidecar_path(raw), "a", encoding="utf-8") as f:
        f.write("\n<!-- Lightroom saved here -->\n")
    with pytest.raises(core.RawSafetyError):
        core.restore_output(raw, snap, written)
    assert os.path.exists(core.sidecar_path(raw))


def test_undo_never_targets_the_raw(raw, tmp_path):
    snap = core.snapshot_output(raw, str(tmp_path / "store"))
    snap["target"] = raw
    with pytest.raises(core.RawSafetyError):
        core.restore_output(raw, snap, None)
