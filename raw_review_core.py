"""
RAW Review - core logic (no GUI).

Colour science, smart auto white balance, auto straighten, preview rendering
and Lightroom XMP sidecar writing.

SAFETY CONTRACT
---------------
* RAW files are only ever OPENED FOR READING (via rawpy/LibRaw).
* Nothing in this program writes, moves, renames, re-saves or deletes a RAW
  file. The only files ever written are:
    - <photo name>.xmp            the Lightroom sidecar next to the RAW
    - <photo name>.xmp.bak        one-time backup of a sidecar that already
                                  existed before this app first touched it
* Every sidecar write checks the RAW's size + modification time before and
  after, and refuses to write anything whose name looks like a RAW file.
"""

from __future__ import annotations

import copy
import math
import os
import shutil
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import cv2
import numpy as np

# --------------------------------------------------------------------------
# Settings you may want to adjust after testing ONE photo in Lightroom
# --------------------------------------------------------------------------

# Direction of the straighten angle as Lightroom reads it. Inside this app a
# positive angle means "rotate counter-clockwise". If photos come out tilted
# the wrong way in Lightroom, change this to +1.0.
XMP_ANGLE_SIGN = -1.0

# Process version written ONLY into brand-new sidecars ("11.0" = PV2012/v5,
# fully supported by current Lightroom Classic; it offers to update it).
PROCESS_VERSION = "11.0"

# crs:PerspectiveUpright values (as decoded by the ExifTool project).
UPRIGHT_MODES = {"Off": 0, "Auto": 1, "Full": 2, "Level": 3, "Vertical": 4}

# Long edge of the working preview, in pixels.
WORK_LONG_EDGE = 1400

# RAW formats that use .xmp sidecars. DNG is deliberately excluded: Lightroom
# stores DNG edits inside the DNG itself, and this app never modifies RAWs.
RAW_EXTS = {
    ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".raf",
    ".orf", ".rw2", ".pef", ".srw", ".3fr", ".iiq", ".erf", ".kdc", ".mos",
    ".mrw", ".x3f", ".rwl", ".raw",
}
PROTECTED_EXTS = RAW_EXTS | {".dng", ".jpg", ".jpeg", ".tif", ".tiff", ".heic", ".png"}


# --------------------------------------------------------------------------
# Colour science
# --------------------------------------------------------------------------

XYZ_FROM_SRGB = np.array([
    [0.412453, 0.357580, 0.180423],
    [0.212671, 0.715160, 0.072169],
    [0.019334, 0.119193, 0.950227],
])
LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)

# Robertson isotemperature table (r = 1e6/T, u, v, slope) - same table and
# method the Adobe DNG SDK uses for Temperature/Tint, so the numbers line up
# with Lightroom's sliders.
_ROB = np.array([
    [0, 0.18006, 0.26352, -0.24341], [10, 0.18066, 0.26589, -0.25479],
    [20, 0.18133, 0.26846, -0.26876], [30, 0.18208, 0.27119, -0.28539],
    [40, 0.18293, 0.27407, -0.30470], [50, 0.18388, 0.27709, -0.32675],
    [60, 0.18494, 0.28021, -0.35156], [70, 0.18611, 0.28342, -0.37915],
    [80, 0.18740, 0.28668, -0.40955], [90, 0.18880, 0.28997, -0.44278],
    [100, 0.19032, 0.29326, -0.47888], [125, 0.19462, 0.30141, -0.58204],
    [150, 0.19962, 0.30921, -0.70471], [175, 0.20525, 0.31647, -0.84901],
    [200, 0.21142, 0.32312, -1.0182], [225, 0.21807, 0.32909, -1.2168],
    [250, 0.22511, 0.33439, -1.4512], [275, 0.23247, 0.33904, -1.7298],
    [300, 0.24010, 0.34308, -2.0637], [325, 0.24702, 0.34655, -2.4681],
    [350, 0.25591, 0.34951, -2.9641], [375, 0.26400, 0.35200, -3.5814],
    [400, 0.27218, 0.35407, -4.3633], [425, 0.28039, 0.35577, -5.3762],
    [450, 0.28863, 0.35714, -6.7262], [475, 0.29685, 0.35823, -8.5955],
    [500, 0.30505, 0.35907, -11.324], [525, 0.31320, 0.35968, -15.628],
    [550, 0.32129, 0.36011, -23.325], [575, 0.32931, 0.36038, -40.770],
    [600, 0.33724, 0.36051, -116.45],
])
_TINT_SCALE = -3000.0

TEMP_MIN, TEMP_MAX = 2000.0, 50000.0
TINT_MIN, TINT_MAX = -150.0, 150.0


def xy_to_temp_tint(x: float, y: float) -> tuple[float, float]:
    """CIE xy chromaticity -> (Temperature K, Tint) as Lightroom shows them."""
    denom = 1.5 - x + 6.0 * y
    u, v = 2.0 * x / denom, 3.0 * y / denom
    last_dt = last_du = last_dv = 0.0
    for i in range(1, 31):
        du, dv = 1.0, _ROB[i, 3]
        ln = math.hypot(du, dv)
        du, dv = du / ln, dv / ln
        uu, vv = u - _ROB[i, 1], v - _ROB[i, 2]
        dt = -uu * dv + vv * du
        if dt <= 0 or i == 30:
            if dt > 0:
                dt = 0.0
            dt = -dt
            f = 0.0 if i == 1 or (last_dt + dt) == 0 else dt / (last_dt + dt)
            temp = 1.0e6 / (_ROB[i - 1, 0] * f + _ROB[i, 0] * (1 - f))
            uu = u - (_ROB[i - 1, 1] * f + _ROB[i, 1] * (1 - f))
            vv = v - (_ROB[i - 1, 2] * f + _ROB[i, 2] * (1 - f))
            du = du * (1 - f) + last_du * f
            dv = dv * (1 - f) + last_dv * f
            ln = math.hypot(du, dv)
            du, dv = du / ln, dv / ln
            return temp, (uu * du + vv * dv) * _TINT_SCALE
        last_dt, last_du, last_dv = dt, du, dv
    return 5000.0, 0.0  # unreachable


def temp_tint_to_xy(temp: float, tint: float) -> tuple[float, float]:
    """(Temperature K, Tint) -> CIE xy chromaticity."""
    r = 1.0e6 / temp
    offset = tint * (1.0 / _TINT_SCALE)
    for i in range(30):
        if r < _ROB[i + 1, 0] or i == 29:
            f = (_ROB[i + 1, 0] - r) / (_ROB[i + 1, 0] - _ROB[i, 0])
            u = _ROB[i, 1] * f + _ROB[i + 1, 1] * (1 - f)
            v = _ROB[i, 2] * f + _ROB[i + 1, 2] * (1 - f)
            uu1, vv1 = 1.0, _ROB[i, 3]
            ln1 = math.hypot(uu1, vv1)
            uu2, vv2 = 1.0, _ROB[i + 1, 3]
            ln2 = math.hypot(uu2, vv2)
            uu3 = uu1 / ln1 * f + uu2 / ln2 * (1 - f)
            vv3 = vv1 / ln1 * f + vv2 / ln2 * (1 - f)
            ln3 = math.hypot(uu3, vv3)
            u += uu3 / ln3 * offset
            v += vv3 / ln3 * offset
            d = u - 4.0 * v + 2.0
            return 1.5 * u / d, v / d
    return 0.3457, 0.3585  # unreachable


class CameraColor:
    """Camera colour model built from the camera's Adobe XYZ->camera matrix."""

    def __init__(self, cam_xyz, as_shot_mul):
        m3 = np.asarray(cam_xyz, dtype=np.float64)[:3, :3]
        self.valid = bool(np.isfinite(m3).all() and abs(np.linalg.det(m3)) > 1e-9)
        if self.valid:
            cam_rgb = m3 @ XYZ_FROM_SRGB
            cam_rgb = cam_rgb / cam_rgb.sum(axis=1, keepdims=True)
            self.rgb_cam = np.linalg.inv(cam_rgb).astype(np.float32)
            self.xyz_to_cam = m3
            self.cam_to_xyz = np.linalg.inv(m3)
        else:
            self.rgb_cam = np.eye(3, dtype=np.float32)
            self.xyz_to_cam = self.cam_to_xyz = None

        m = np.asarray(as_shot_mul, dtype=np.float64)[:3]
        if not (np.isfinite(m).all() and (m > 0).all()):
            m = self.temp_tint_to_mul(5500, 0) if self.valid else np.array([2.0, 1.0, 1.5])
        self.as_shot_mul = m / m[1]

    @staticmethod
    def neutral_to_mul(neutral) -> np.ndarray:
        n = np.maximum(np.asarray(neutral, dtype=np.float64), 1e-9)
        m = 1.0 / n
        return m / m[1]

    def mul_to_temp_tint(self, mul) -> tuple[float, float]:
        if not self.valid:
            return 5500.0, 0.0
        xyz = self.cam_to_xyz @ (1.0 / np.asarray(mul, dtype=np.float64))
        s = xyz.sum()
        return xy_to_temp_tint(xyz[0] / s, xyz[1] / s)

    def temp_tint_to_mul(self, temp, tint) -> np.ndarray:
        if not self.valid:
            return self.as_shot_mul.copy()
        x, y = temp_tint_to_xy(float(temp), float(tint))
        xyz = np.array([x / y, 1.0, (1.0 - x - y) / y])
        return self.neutral_to_mul(self.xyz_to_cam @ xyz)

    @property
    def as_shot_temp_tint(self) -> tuple[float, float]:
        return self.mul_to_temp_tint(self.as_shot_mul)


# --------------------------------------------------------------------------
# Decoding (read-only)
# --------------------------------------------------------------------------

@dataclass
class Decoded:
    cam: np.ndarray          # HxWx3 float32, linear camera RGB, NOT white balanced, 0..1
    color: CameraColor
    baseline: float          # display brightness normalisation for the preview
    source_size: tuple       # (w, h) of the decoded half-size image


def decode_raw(path: str) -> Decoded:
    """Open a RAW file READ-ONLY and return a small linear working copy."""
    import rawpy  # imported here so the rest of the module works without it

    kwargs = dict(
        use_camera_wb=False, use_auto_wb=False, user_wb=[1.0, 1.0, 1.0, 1.0],
        output_color=rawpy.ColorSpace.raw, gamma=(1, 1),
        no_auto_bright=True, output_bps=16,
    )
    with rawpy.imread(path) as raw:          # LibRaw opens the file read-only
        try:
            rgb = raw.postprocess(half_size=True, **kwargs)
        except Exception:
            rgb = raw.postprocess(**kwargs)
        cam_xyz = np.array(raw.rgb_xyz_matrix, dtype=np.float64)
        as_shot = np.array(raw.camera_whitebalance, dtype=np.float64)

    return make_decoded(rgb.astype(np.float32) / 65535.0, cam_xyz, as_shot)


def make_decoded(cam_full: np.ndarray, cam_xyz, as_shot_mul) -> Decoded:
    h, w = cam_full.shape[:2]
    s = WORK_LONG_EDGE / max(h, w)
    cam = cam_full
    if s < 1.0:
        cam = cv2.resize(cam_full, (max(1, round(w * s)), max(1, round(h * s))),
                         interpolation=cv2.INTER_AREA)
    cam = np.ascontiguousarray(cam, dtype=np.float32)
    color = CameraColor(cam_xyz, as_shot_mul)
    lin = _cam_to_linear_srgb(cam, color.as_shot_mul, color)
    y = lin @ LUMA
    p = float(np.percentile(y, 99.0)) if y.size else 1.0
    baseline = float(np.clip(0.85 / max(p, 1e-6), 1.0, 6.0))
    return Decoded(cam=cam, color=color, baseline=baseline, source_size=(w, h))


def _cam_to_linear_srgb(cam, mul, color) -> np.ndarray:
    lin = (cam * np.asarray(mul, dtype=np.float32)) @ color.rgb_cam.T
    return np.maximum(lin, 0.0)


# --------------------------------------------------------------------------
# Edit model
# --------------------------------------------------------------------------

@dataclass
class Edit:
    wb_mode: str = "as_shot"     # "as_shot" or "custom"
    temp: float = 5500.0
    tint: float = 0.0
    exposure: float = 0.0        # EV, -5..+5
    contrast: int = 0            # -100..100
    highlights: int = 0
    shadows: int = 0
    vibrance: int = 0
    angle: float = 0.0           # degrees, + = counter-clockwise
    upright: str = "Off"         # key of UPRIGHT_MODES
    lens_profile: bool = True
    notes: list = field(default_factory=list)

    def copy(self) -> "Edit":
        return copy.deepcopy(self)


def as_shot_edit(dec: Decoded) -> Edit:
    t, ti = dec.color.as_shot_temp_tint
    return Edit(temp=round(t), tint=round(ti))


# --------------------------------------------------------------------------
# Preview rendering (approximation of Lightroom's look)
# --------------------------------------------------------------------------

def crop_factor(w: float, h: float, deg: float) -> float:
    """Largest same-aspect centred crop that fits inside the rotated image."""
    a = math.radians(abs(deg))
    c, s = math.cos(a), math.sin(a)
    return min(w / (w * c + h * s), h / (w * s + h * c))


@dataclass
class Geometry:
    """Maps between working-image coordinates and displayed-image coordinates."""
    M: np.ndarray | None = None   # 2x3 affine (working -> rotated)
    x0: int = 0
    y0: int = 0

    def to_display(self, x, y):
        if self.M is None:
            return x, y
        px = self.M[0, 0] * x + self.M[0, 1] * y + self.M[0, 2]
        py = self.M[1, 0] * x + self.M[1, 1] * y + self.M[1, 2]
        return px - self.x0, py - self.y0

    def to_source(self, x, y):
        if self.M is None:
            return x, y
        inv = cv2.invertAffineTransform(self.M)
        rx, ry = x + self.x0, y + self.y0
        return (inv[0, 0] * rx + inv[0, 1] * ry + inv[0, 2],
                inv[1, 0] * rx + inv[1, 1] * ry + inv[1, 2])


def rotate_and_crop(img: np.ndarray, deg: float):
    h, w = img.shape[:2]
    if abs(deg) < 1e-3:
        return img, Geometry()
    M = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), deg, 1.0)
    out = cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_REPLICATE)
    k = crop_factor(w, h, deg)
    cw, ch = max(1, int(w * k)), max(1, int(h * k))
    x0, y0 = (w - cw) // 2, (h - ch) // 2
    return np.ascontiguousarray(out[y0:y0 + ch, x0:x0 + cw]), Geometry(M, x0, y0)


def _srgb_encode(x):
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.0031308, 12.92 * x,
                    1.055 * np.power(np.maximum(x, 0.0031308), 1 / 2.4) - 0.055)


def render(dec: Decoded, edit: Edit, original: bool = False, rotate: bool = True):
    """Return (uint8 RGB image, Geometry). original=True shows the as-shot file."""
    color = dec.color
    if original or edit.wb_mode != "custom":
        mul = color.as_shot_mul
    else:
        mul = color.temp_tint_to_mul(edit.temp, edit.tint)

    lin = _cam_to_linear_srgb(dec.cam, mul, color)
    ev = 0.0 if original else edit.exposure
    lin *= np.float32(dec.baseline * (2.0 ** ev))

    knee = 0.8                                   # soft highlight shoulder
    lin = np.where(lin < knee, lin, knee + (1 - knee) * np.tanh((lin - knee) / (1 - knee)))
    g = _srgb_encode(lin).astype(np.float32)

    if original:
        e = Edit()
    else:
        e = edit
    L = np.clip(g @ LUMA, 1e-4, 1.0)
    ms = 6.75 * L * (1 - L) ** 2                 # shadow mask, peaks at L=1/3
    mh = 6.75 * L ** 2 * (1 - L)                 # highlight mask, peaks at L=2/3
    L1 = np.clip(L + 0.25 * (e.shadows / 100.0 * ms + e.highlights / 100.0 * mh), 0, 1)
    c = 0.15 + e.contrast / 100.0                # small base S-curve, like a raw default
    S = L1 * L1 * (3 - 2 * L1)
    L2 = np.clip(L1 + c * (S - L1), 0, 1)
    g = g * (L2 / L)[..., None]

    if e.vibrance:
        mean = g.mean(axis=2, keepdims=True)
        sat = (g.max(axis=2) - g.min(axis=2))[..., None]
        g = mean + (g - mean) * (1 + e.vibrance / 100.0 * (1 - np.clip(sat, 0, 1)))

    img = (np.clip(g, 0, 1) * 255 + 0.5).astype(np.uint8)
    if rotate and not original:
        return rotate_and_crop(img, edit.angle)
    return np.ascontiguousarray(img), Geometry()


# --------------------------------------------------------------------------
# Smart auto white balance
# --------------------------------------------------------------------------

@dataclass
class WBResult:
    applied: bool
    temp: float
    tint: float
    confidence: float
    note: str
    bright_pts: np.ndarray = field(default_factory=lambda: np.zeros((0, 2)))
    dark_pts: np.ndarray = field(default_factory=lambda: np.zeros((0, 2)))


def _lowest_chroma(mask, chroma, frac, max_chroma):
    if mask.sum() == 0:
        return mask
    # Relative cap: as-shot may be far off, so "neutral" means "close to the
    # most neutral patch in this band", not an absolute chroma value.
    c = chroma[mask]
    thr = min(float(np.percentile(c, frac * 100)), float(c.min()) + max_chroma)
    return mask & (chroma <= thr)


def auto_white_balance(dec: Decoded) -> WBResult:
    """
    Find surfaces that are most likely truly neutral - near-white highlights
    and near-black shadows - and balance on them.

    1. Work on ~256px patch averages (noise-free), skip clipped patches (a
       clipped channel lies about colour) and textured patches (edges mix colours).
    2. Candidates: bright band (top ~35% of unclipped luminance) and dark band
       (just above the noise floor), both smooth.
    3. Iterate: under the current balance, keep the least-coloured 15% of each
       band, re-estimate the neutral from them (highlights weighted more than
       shadows, which are noisier and flare-prone). Repeat until it settles.
    4. Keep the result physically plausible (near the daylight/tungsten locus)
       and blend back toward As Shot when the evidence is weak.
    """
    color = dec.color
    t0, ti0 = color.as_shot_temp_tint
    if not color.valid:
        return WBResult(False, t0, ti0, 0.0, "Auto WB unavailable (no colour matrix for this camera)")

    cam = dec.cam
    H, W = cam.shape[:2]
    sc = 256.0 / max(H, W)
    sw, sh = max(8, round(W * sc)), max(8, round(H * sc))
    small = cv2.resize(cam, (sw, sh), interpolation=cv2.INTER_AREA)
    sq = cv2.resize(cam * cam, (sw, sh), interpolation=cv2.INTER_AREA)
    clip_full = (cam.max(axis=2) >= 0.97).astype(np.float32)
    clipped = cv2.resize(clip_full, (sw, sh), interpolation=cv2.INTER_AREA) > 0.0

    gw = np.array([0.25, 0.5, 0.25], dtype=np.float32)
    std = np.sqrt(np.maximum(sq - small * small, 0.0))
    texture = (std @ gw) / ((small @ gw) + 1e-4)
    smooth = texture < 0.12

    Y = (_cam_to_linear_srgb(small, color.as_shot_mul, color) @ LUMA) * dec.baseline
    valid = (~clipped) & (Y > 0.01) & (small.min(axis=2) > 1e-5)
    if valid.sum() < 30:
        return WBResult(False, t0, ti0, 0.0, "Auto WB: image too dark/clipped, kept As Shot")

    yv = Y[valid]
    # "Closest to white": the brightest unclipped surfaces.
    # "Closest to black": the darkest surfaces that are still above the noise.
    # A true white is close in brightness to the brightest unclipped surface;
    # a beige wall at 40% of that is not, even if it happens to look grey.
    y_top = float(np.percentile(yv, 99.5))
    bright = valid & (Y >= max(0.6 * y_top, float(np.percentile(yv, 85))))
    if bright.sum() < 20:
        bright = valid & (Y >= np.percentile(yv, 97))
    lo_a, lo_b = np.percentile(yv, [2, 10])
    dark = valid & (Y >= max(lo_a, 0.02)) & (Y <= lo_b)

    logc = np.log(np.maximum(small, 1e-6))

    def chroma_of(mul):
        lm = np.log(mul)
        rg = (logc[..., 0] + lm[0]) - (logc[..., 1] + lm[1])
        bg = (logc[..., 2] + lm[2]) - (logc[..., 1] + lm[1])
        return np.hypot(rg, bg)

    def estimate(band, weight_by_y):
        """Iteratively balance on the least-coloured part of one band."""
        mul = color.as_shot_mul.copy()
        sel = np.zeros_like(band)
        if band.sum() < 3:
            return None
        for _ in range(8):
            chroma = chroma_of(mul)
            sel = _lowest_chroma(band & smooth, chroma, 0.25, 0.35)
            if sel.sum() < 4:
                sel = _lowest_chroma(band, chroma, 0.20, 0.35)
            if sel.sum() < 3:
                return None
            w = Y[sel] if weight_by_y else np.ones(int(sel.sum()), np.float32)
            n_log = (logc[sel] * w[:, None]).sum(axis=0) / w.sum()
            new_mul = color.neutral_to_mul(np.exp(n_log))
            done = np.abs(np.log(new_mul / mul)).max() < 0.002
            mul = new_mul
            if done:
                break
        spread = float(np.sqrt(np.mean(chroma_of(mul)[sel] ** 2)))
        _t, tint = color.mul_to_temp_tint(mul)
        return {"mul": mul, "sel": sel, "n": int(sel.sum()), "spread": spread, "tint": tint}

    est_b = estimate(bright, True)
    est_d = estimate(dark, False)
    if est_b is None and est_d is None:
        return WBResult(False, t0, ti0, 0.0, "Auto WB: no neutral surfaces found, kept As Shot")

    def locus_score(e):
        # Real light sources sit near the daylight/tungsten locus: big tint = suspicious
        # (e.g. a blue object under tungsten looks grey to the camera).
        return math.exp(-max(0.0, abs(e["tint"]) - 15) / 25.0)

    agree_msg = ""
    if est_b and est_d:
        diff = float(np.abs(np.log(est_b["mul"] / est_d["mul"])).max())
        if diff < 0.12:
            # White and black agree: combine (highlights weighted 2:1, less noise).
            mul = np.exp((2 * np.log(est_b["mul"]) + np.log(est_d["mul"])) / 3)
            sel_b, sel_d = est_b["sel"], est_d["sel"]
            agree_msg = "white+black agree"
        else:
            sb = locus_score(est_b) * 1.3                  # whites are more trustworthy
            sd = locus_score(est_d)
            pick = est_b if sb >= sd else est_d
            mul = pick["mul"]
            sel_b = est_b["sel"] if pick is est_b else np.zeros_like(valid)
            sel_d = est_d["sel"] if pick is est_d else np.zeros_like(valid)
            agree_msg = "used " + ("whites" if pick is est_b else "blacks") + " (black/white disagreed)"
    else:
        pick = est_b or est_d
        mul = pick["mul"]
        sel_b = pick["sel"] if pick is est_b else np.zeros_like(valid)
        sel_d = pick["sel"] if pick is est_d else np.zeros_like(valid)
        agree_msg = "whites only" if pick is est_b else "blacks only"

    n_sel = int(sel_b.sum() + sel_d.sum())
    sel = sel_b | sel_d
    # Brightness-weighted: dark patches are noisier, so they shouldn't dominate.
    spread = float(np.sqrt(np.average(chroma_of(mul)[sel] ** 2, weights=Y[sel])))
    _t, tint_now = color.mul_to_temp_tint(mul)
    conf = float(min(1.0, n_sel / 40.0) * math.exp(-spread / 0.08)
                 * locus_score({"tint": tint_now}))
    if agree_msg == "white+black agree":
        conf = min(1.0, conf * 1.2)
    else:
        # Only one kind of evidence: a big jump away from the camera's own
        # As Shot needs corroboration (an orange wall in daylight looks exactly
        # like a white wall under tungsten).
        t_new, _ = color.mul_to_temp_tint(mul)
        d_mired = abs(1e6 / max(t_new, 1000) - 1e6 / max(t0, 1000))
        conf *= (0.6 if "disagreed" in agree_msg else 0.8)
        conf *= math.exp(-max(0.0, d_mired - 30) / 80.0)

    # Weak evidence -> blend toward As Shot.
    if conf < 0.5:
        a = conf / 0.5
        mul = np.exp(a * np.log(mul) + (1 - a) * np.log(color.as_shot_mul))

    temp, tint = color.mul_to_temp_tint(mul)
    t_c, ti_c = float(np.clip(temp, 2000, 12000)), float(np.clip(tint, -60, 60))
    clamped = (t_c, ti_c) != (temp, tint)
    temp, tint = t_c, ti_c

    def pts(mask):
        ys, xs = np.nonzero(mask)
        return np.stack([(xs + 0.5) * W / sw, (ys + 0.5) * H / sh], axis=1)

    note = (f"Auto WB: {round(temp)}K / tint {round(tint):+d} - {agree_msg}, "
            f"{int(sel_b.sum())} light + {int(sel_d.sum())} dark patches "
            f"(confidence {conf:.0%}{', clamped' if clamped else ''})")
    return WBResult(True, temp, tint, conf, note, pts(sel_b), pts(sel_d))


def wb_from_point(dec: Decoded, x: float, y: float, radius: int = 3):
    """Eyedropper: Temperature/Tint that makes the clicked spot neutral."""
    H, W = dec.cam.shape[:2]
    xi, yi = int(round(x)), int(round(y))
    if not (0 <= xi < W and 0 <= yi < H):
        return None
    patch = dec.cam[max(0, yi - radius):yi + radius + 1, max(0, xi - radius):xi + radius + 1]
    n = np.median(patch.reshape(-1, 3), axis=0)
    if (n <= 1e-5).any() or patch.max() >= 0.97:
        return None
    t, ti = dec.color.mul_to_temp_tint(dec.color.neutral_to_mul(n))
    return float(np.clip(t, TEMP_MIN, TEMP_MAX)), float(np.clip(ti, TINT_MIN, TINT_MAX))


# --------------------------------------------------------------------------
# Auto straighten
# --------------------------------------------------------------------------

@dataclass
class StraightenResult:
    angle: float
    upright: str
    confidence: float
    note: str


def _weighted_median(vals, wts):
    order = np.argsort(vals)
    v, w = np.asarray(vals)[order], np.asarray(wts)[order]
    c = np.cumsum(w)
    return float(v[np.searchsorted(c, c[-1] / 2.0)])


def auto_straighten(rgb: np.ndarray, allow_upright: bool = True) -> StraightenResult:
    """
    Detect straight lines; near-vertical ones (buildings, door frames, poles)
    are trusted most, near-horizontal ones (horizons) are the fallback.
    If verticals converge strongly (camera tilted up at a building), suggest
    Lightroom's Upright 'Vertical' instead of a plain rotation.
    Angle convention: + = rotate counter-clockwise.
    """
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    s = 1000.0 / max(h, w)
    if s < 1:
        gray = cv2.resize(gray, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA)
        h, w = gray.shape
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    med = float(np.median(gray))
    lo, hi = int(max(10, 0.66 * med)), int(min(255, max(40, 1.33 * med)))
    edges = cv2.Canny(gray, lo, hi)
    diag = math.hypot(w, h)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 720, threshold=50,
                            minLineLength=int(0.06 * diag), maxLineGap=int(0.006 * diag) + 2)
    if lines is None or len(lines) == 0:
        return StraightenResult(0.0, "Off", 0.0, "Straighten: no straight lines found")

    verts, horiz = [], []
    for x1, y1, x2, y2 in np.asarray(lines, dtype=float).reshape(-1, 4):
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy)
        if abs(dy) > abs(dx):
            if y2 > y1:                       # orient bottom -> top
                x1, y1, x2, y2 = x2, y2, x1, y1
            lean = math.degrees(math.atan2(x2 - x1, y1 - y2))   # + = top leans right
            if abs(lean) <= 20:
                verts.append((lean, length, (x1 + x2) / 2.0 / w))
        else:
            if x2 < x1:
                x1, y1, x2, y2 = x2, y2, x1, y1
            tilt = math.degrees(math.atan2(y1 - y2, x2 - x1))   # + = right end higher
            if abs(tilt) <= 15:
                horiz.append((tilt, length))

    vw = sum(v[1] for v in verts)
    hw = sum(h_[1] for h_ in horiz)
    if vw + hw == 0:
        return StraightenResult(0.0, "Off", 0.0, "Straighten: no near-level lines found")

    # Keystone check (verticals converging toward the top).
    if allow_upright and len(verts) >= 4:
        left = [v for v in verts if v[2] < 0.45]
        right = [v for v in verts if v[2] > 0.55]
        if len(left) >= 2 and len(right) >= 2:
            ml = np.average([v[0] for v in left], weights=[v[1] for v in left])
            mr = np.average([v[0] for v in right], weights=[v[1] for v in right])
            converge = ml - mr
            if abs(converge) > 2.0 and vw > 0.3 * (vw + hw):
                return StraightenResult(
                    0.0, "Vertical", min(1.0, abs(converge) / 6.0),
                    f"Straighten: verticals converge {converge:.1f}° -> Upright Vertical "
                    "(Lightroom computes the correction)")

    if len(verts) >= 2 and vw >= 0.35 * (vw + hw):
        cands = [(v[0], v[1]) for v in verts]
        src = "vertical"
    else:
        cands = [(-h_[0], h_[1]) for h_ in horiz]
        src = "horizontal"
    if not cands:
        return StraightenResult(0.0, "Off", 0.0, "Straighten: not enough lines")

    vals = np.array([c[0] for c in cands])
    wts = np.array([c[1] for c in cands])
    angle = _weighted_median(vals, wts)
    conf = float(wts[np.abs(vals - angle) <= 1.0].sum() / wts.sum())

    if abs(angle) > 12:
        return StraightenResult(0.0, "Off", conf,
                                f"Straighten: {angle:.1f}° looks intentional, left alone")
    if abs(angle) < 0.15 or conf < 0.25:
        return StraightenResult(0.0, "Off", conf, "Straighten: already level")
    angle = round(angle, 2)
    return StraightenResult(angle, "Off", conf,
                            f"Straighten: {angle:+.2f}° from {len(cands)} {src} lines "
                            f"(confidence {conf:.0%})")


# --------------------------------------------------------------------------
# One-shot proposal
# --------------------------------------------------------------------------

def propose_edit(dec: Decoded, auto_wb=True, straighten=True, allow_upright=True):
    edit = as_shot_edit(dec)
    wb = st = None
    if auto_wb:
        wb = auto_white_balance(dec)
        if wb.applied:
            edit.wb_mode, edit.temp, edit.tint = "custom", round(wb.temp), round(wb.tint)
        edit.notes.append(wb.note)
    if straighten:
        img, _ = render(dec, edit, rotate=False)
        st = auto_straighten(img, allow_upright)
        edit.angle, edit.upright = st.angle, st.upright
        edit.notes.append(st.note)
    return edit, wb, st


# --------------------------------------------------------------------------
# XMP sidecars (the ONLY thing this program writes)
# --------------------------------------------------------------------------

NS = {
    "x": "adobe:ns:meta/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "crs": "http://ns.adobe.com/camera-raw-settings/1.0/",
    "rrv": "http://ns.rawreview.local/1.0/",
}
for _p, _u in NS.items():
    ET.register_namespace(_p, _u)


class RawSafetyError(Exception):
    pass


def sidecar_path(raw_path: str) -> str:
    return os.path.splitext(raw_path)[0] + ".xmp"


def raw_fingerprint(path: str):
    st = os.stat(path)
    return st.st_size, st.st_mtime_ns


def _check_target(raw_path: str, target: str):
    ext = os.path.splitext(target)[1].lower()
    if ext not in (".xmp", ".bak", ".tmp"):
        raise RawSafetyError(f"Refusing to write non-sidecar file: {target}")
    if ext in PROTECTED_EXTS or os.path.normcase(os.path.abspath(target)) == \
            os.path.normcase(os.path.abspath(raw_path)):
        raise RawSafetyError(f"Refusing to write over a photo file: {target}")


def crs_values(edit: Edit):
    """Return (values to set, keys to remove), keys without the crs: prefix."""
    v = {
        "HasSettings": "True",
        "Exposure2012": f"{edit.exposure:+.2f}",
        "Contrast2012": f"{int(round(edit.contrast)):+d}",
        "Highlights2012": f"{int(round(edit.highlights)):+d}",
        "Shadows2012": f"{int(round(edit.shadows)):+d}",
        "Vibrance": f"{int(round(edit.vibrance)):+d}",
        "LensProfileEnable": "1" if edit.lens_profile else "0",
        "AutoLateralCA": "1" if edit.lens_profile else "0",
        "PerspectiveUpright": str(UPRIGHT_MODES.get(edit.upright, 0)),
    }
    remove = []
    if edit.wb_mode == "custom":
        v["WhiteBalance"] = "Custom"
        v["Temperature"] = str(int(round(edit.temp)))
        v["Tint"] = f"{int(round(edit.tint)):+d}"
    else:
        v["WhiteBalance"] = "As Shot"
        remove += ["Temperature", "Tint"]
    return v, remove


def _crop_values(edit: Edit, src_w: float, src_h: float):
    k = crop_factor(src_w, src_h, edit.angle)
    lo, hi = 0.5 - k / 2.0, 0.5 + k / 2.0
    return {
        "HasCrop": "True",
        "CropAngle": f"{XMP_ANGLE_SIGN * edit.angle:.2f}",
        "CropLeft": f"{lo:.6f}", "CropTop": f"{lo:.6f}",
        "CropRight": f"{hi:.6f}", "CropBottom": f"{hi:.6f}",
    }


def _q(prefix, name):
    return f"{{{NS[prefix]}}}{name}"


def _new_tree():
    root = ET.Element(_q("x", "xmpmeta"), {_q("x", "xmptk"): "RAW Review"})
    rdf = ET.SubElement(root, _q("rdf", "RDF"))
    desc = ET.SubElement(rdf, _q("rdf", "Description"), {_q("rdf", "about"): ""})
    desc.set(_q("crs", "ProcessVersion"), PROCESS_VERSION)
    return ET.ElementTree(root), desc


def _register_file_namespaces(path):
    try:
        for _ev, (prefix, uri) in ET.iterparse(path, events=("start-ns",)):
            if prefix and prefix not in NS:
                try:
                    ET.register_namespace(prefix, uri)
                except ValueError:
                    pass
    except ET.ParseError:
        pass


def _has(desc, key):
    return desc.get(key) is not None or desc.find(key) is not None


def _set(desc, key, value):
    for child in desc.findall(key):
        desc.remove(child)
    desc.set(key, value)


def _remove(desc, key):
    desc.attrib.pop(key, None)
    for child in desc.findall(key):
        desc.remove(child)


def write_xmp(raw_path: str, edit: Edit, src_size=(3, 2)) -> str:
    """
    Write/update the Lightroom sidecar for raw_path. The RAW itself is never
    opened for writing; its size and timestamp are verified unchanged.
    Returns a short human-readable message.
    """
    if not os.path.isfile(raw_path):
        raise FileNotFoundError(raw_path)
    target = sidecar_path(raw_path)
    _check_target(raw_path, target)
    before = raw_fingerprint(raw_path)

    existed = os.path.exists(target)
    tree = desc = None
    backup_msg = ""
    if existed:
        _register_file_namespaces(target)
        try:
            tree = ET.parse(target)
            descs = tree.getroot().iter(_q("rdf", "Description"))
            descs = list(descs)
            crs_desc = [d for d in descs if any(k.startswith("{" + NS["crs"]) for k in d.attrib)]
            desc = (crs_desc or descs or [None])[0]
            if desc is None:
                rdf = next(tree.getroot().iter(_q("rdf", "RDF")), None)
                if rdf is None:
                    tree = None
                else:
                    desc = ET.SubElement(rdf, _q("rdf", "Description"), {_q("rdf", "about"): ""})
        except ET.ParseError:
            tree = None
        backup = target + ".bak"
        _check_target(raw_path, backup)
        ours = tree is not None and (
            tree.getroot().get(_q("x", "xmptk")) == "RAW Review"
            or any(k.startswith("{" + NS["rrv"]) for d in tree.getroot().iter() for k in d.attrib))
        if not ours and not os.path.exists(backup):
            shutil.copy2(target, backup)
            backup_msg = f" (original sidecar backed up to {os.path.basename(backup)})"
        if tree is None:
            backup_msg += " (old sidecar was unreadable, replaced)"

    if tree is None:
        tree, desc = _new_tree()
    elif not _has(desc, _q("crs", "ProcessVersion")):
        desc.set(_q("crs", "ProcessVersion"), PROCESS_VERSION)

    values, remove = crs_values(edit)
    for k, val in values.items():
        _set(desc, _q("crs", k), val)
    for k in remove:
        _remove(desc, _q("crs", k))

    marker = _q("rrv", "CropByRawReview")
    if abs(edit.angle) >= 0.01:
        for k, val in _crop_values(edit, *src_size).items():
            _set(desc, _q("crs", k), val)
        desc.set(marker, "True")
    elif desc.get(marker) is not None:
        # We set a crop earlier and the angle is now 0: undo only our own crop.
        for k, val in {"HasCrop": "False", "CropAngle": "0", "CropLeft": "0",
                       "CropTop": "0", "CropRight": "1", "CropBottom": "1"}.items():
            _set(desc, _q("crs", k), val)
        desc.attrib.pop(marker, None)

    data = ET.tostring(tree.getroot(), encoding="unicode")
    folder = os.path.dirname(os.path.abspath(target))
    fd, tmp = tempfile.mkstemp(prefix=".rawreview-", suffix=".xmp.tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(data)
        _check_target(raw_path, target)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)   # only ever our own .xmp.tmp file

    if raw_fingerprint(raw_path) != before:
        raise RawSafetyError(f"{raw_path} changed while its sidecar was written - stopping.")
    return f"{'Updated' if existed else 'Wrote'} {os.path.basename(target)}{backup_msg}"


def is_raw(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in RAW_EXTS
