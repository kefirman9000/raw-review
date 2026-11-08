"""Subject crop: the crop centres an off-centre subject, and is written to / read from the sidecar."""
import os

import cv2
import numpy as np

import raw_review_core as core


def _sky_with_plane(cx, cy):
    img = np.zeros((600, 900, 3), np.uint8)
    img[:] = (120, 170, 230)
    x, y = int(cx * 900), int(cy * 600)
    cv2.ellipse(img, (x, y), (70, 12), 0, 0, 360, (235, 235, 240), -1)
    return img


def test_off_centre_subject_gets_centred():
    box, conf = core.find_subject_box(_sky_with_plane(0.65, 0.4))
    assert box is not None and conf >= core.SUBJECT_CROP_MIN_CONF
    crop = core.subject_crop_box(box)
    assert crop is not None
    sx, sy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    assert abs((sx - crop[0]) / (crop[2] - crop[0]) - 0.5) < 0.05
    assert abs((sy - crop[1]) / (crop[3] - crop[1]) - 0.5) < 0.05
    assert crop[2] - crop[0] >= core.SUBJECT_CROP_MIN_SCALE - 1e-6
    assert abs((crop[2] - crop[0]) - (crop[3] - crop[1])) < 1e-6        # same aspect ratio


def test_centred_subject_is_not_cropped():
    assert core.subject_crop_box((0.45, 0.45, 0.55, 0.55)) is None


def test_crop_box_round_trips_through_sidecar(tmp_path):
    raw = tmp_path / "IMG_0002.NEF"
    raw.write_bytes(os.urandom(4096))
    before = raw.read_bytes()
    core.write_xmp(str(raw), core.Edit(crop_box=[0.2, 0.1, 0.8, 0.7]), (6000, 4000))
    got = core.read_existing(str(raw), core.Edit())["edit"]
    assert got.crop_box == [0.2, 0.1, 0.8, 0.7]
    core.write_xmp(str(raw), core.Edit(), (6000, 4000))                 # crop removed again
    assert 'HasCrop="False"' in open(core.sidecar_path(str(raw))).read()
    assert raw.read_bytes() == before
