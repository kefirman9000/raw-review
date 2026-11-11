"""
RAW Review - core logic (no GUI).

Colour science, smart auto white balance, auto straighten, preview rendering
and Lightroom XMP sidecar writing. (Auto exposure / levels / contrast / colour /
sharpening / noise reduction live in raw_review_auto.py.)

SAFETY CONTRACT
---------------
* RAW files are only ever OPENED FOR READING (via rawpy/LibRaw). The one
  exception is DNG, whose XMP packet is replaced (see _write_dng).
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
import struct
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET

from adobe_curve import ACR3_CURVE
from dataclasses import dataclass, field

import cv2
import numpy as np

import dng_profile

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

# Lightroom AI masks (Select Sky / Select Subject). RAW Review writes the mask *recipe* the
# same way Lightroom's Adaptive presets do; Lightroom computes the exact mask itself (it may
# ask you to run "Update AI Settings" the first time). Values from Lightroom's XMP format.
MASK_SUBTYPE = {"subject": "1", "sky": "2"}          # 0 background, 1 subject, 2 sky, 3 people
MASK_NAME = {"sky": "RAW Review: Sky", "subject": "RAW Review: Subject"}
# Local exposure in the XMP: written as EV / LOCAL_EXPOSURE_SCALE. Sources disagree on whether
# Lightroom stores local exposure in plain EV (1.0) or scaled (4.0). If a +0.50 EV sky
# adjustment shows up in Lightroom as +2.00 (or +0.13), change this number.
LOCAL_EXPOSURE_SCALE = 1.0

# crs:PerspectiveUpright values (as decoded by the ExifTool project).
UPRIGHT_MODES = {"Off": 0, "Auto": 1, "Full": 2, "Level": 3, "Vertical": 4, "Guided": 5}

# Straighten / level modes offered in the app.
#   "rotate" modes are measured by this app and written as a plain crop angle.
#   "Upright" modes are written as Lightroom's Upright setting; Lightroom itself does the
#   exact maths when it reads the sidecar. The preview shows an approximation (upright_matrix).
LEVEL_MODES = {
    "Off":                  {"kind": "off"},
    "Auto (best guess)":    {"kind": "rotate", "source": "auto"},
    "Horizon (rotate)":     {"kind": "rotate", "source": "horizon"},
    "Verticals (rotate)":   {"kind": "rotate", "source": "vertical"},
    "Upright: Level":       {"kind": "upright", "upright": "Level"},     # horizon only
    "Upright: Vertical":    {"kind": "upright", "upright": "Vertical"},  # keystone, verticals
    "Upright: Auto":        {"kind": "upright", "upright": "Auto"},      # balanced
    "Upright: Full":        {"kind": "upright", "upright": "Full"},      # architecture
    "Upright: Guided (verticals)": {"kind": "upright", "upright": "Guided", "guides": "v"},
    "Upright: Guided (verticals + horizontals)": {"kind": "upright", "upright": "Guided", "guides": "vh"},
    "Manual":               {"kind": "manual"},                          # slider moved by hand
}

# Exposure the preview adds on top of the raw data, standing in for the
# "baseline exposure" + default tone curve Lightroom applies to a raw file.
# Edit.exposure is the Lightroom Exposure value, so this keeps the preview and
# the automatic exposure in the same ballpark as Lightroom. If photos come out
# systematically too bright or dark in Lightroom, use the "Exposure bias" entry
# in the Auto menu (or change this one number).
PREVIEW_BASE_EV = 0.5

# Colour look for the ON-SCREEN preview of non-DNG raws. Lightroom's default profile
# (Adobe Color) is more colourful than the plain colorimetric render below, so the preview
# gets a gentle midtone saturation boost. Brightness is NOT changed: the plain render's
# brightness is what Lightroom shows for the same settings, and auto exposure is tuned on it.
# Display only; the auto tools measure the plain render. PREVIEW_LOOK = False turns it off.
PREVIEW_LOOK = True
LOOK_SATURATION = 0.0           # Adobe's curve (adobe_tone) now adds Lightroom's colour itself

# Long edge of the working preview, in pixels.
WORK_LONG_EDGE = 1400

# Formats this app opens. RAW files get a .xmp sidecar and are never touched.
# DNG is the exception: Lightroom ignores sidecars for DNG, so an EDITED COPY of the
# DNG (settings embedded in its XMP packet) is exported to its own folder under its
# own proper name. The source DNG is never modified.
DNG_EXPORT_DIR = "RAW Review export"     # created next to the source DNG
RAW_EXTS = {
    ".dng",
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
    """
    Camera colour model.

    Normally built from the camera's Adobe XYZ->camera matrix (LibRaw). For a DNG
    that carries its own profile, the DNG-spec model in dng_profile.py is used
    instead (see the "Camera profile (DNG)" menu); that is what makes files whose
    LibRaw matrix is empty (e.g. Google Pixel) show correct colours.
    """

    def __init__(self, cam_xyz, as_shot_mul, profile=None):
        self._dng = None
        if profile is not None:
            try:
                self._dng = dng_profile.DngColorModel(profile, xy_to_temp_tint, temp_tint_to_xy)
            except Exception:  # noqa: BLE001 - fall back to the LibRaw matrix
                self._dng = None
        self.profile = profile

        m3 = np.asarray(cam_xyz, dtype=np.float64)[:3, :3]
        self._legacy_valid = bool(np.isfinite(m3).all() and abs(np.linalg.det(m3)) > 1e-9)
        if self._legacy_valid:
            cam_rgb = m3 @ XYZ_FROM_SRGB
            cam_rgb = cam_rgb / cam_rgb.sum(axis=1, keepdims=True)
            self._legacy_rgb_cam = np.linalg.inv(cam_rgb).astype(np.float32)
            self._legacy_xyz_to_cam = m3
            self._legacy_cam_to_xyz = np.linalg.inv(m3)
        else:
            self._legacy_rgb_cam = np.eye(3, dtype=np.float32)
            self._legacy_xyz_to_cam = self._legacy_cam_to_xyz = None

        m = np.asarray(as_shot_mul, dtype=np.float64)[:3]
        if not (np.isfinite(m).all() and (m > 0).all()):
            if self._dng is not None and self._dng.as_shot_mul is not None:
                m = self._dng.as_shot_mul.copy()
            elif self.valid:
                m = self.temp_tint_to_mul(5500, 0)
            else:
                m = np.array([2.0, 1.0, 1.5])
        self.as_shot_mul = m / m[1]

    # -- which model is in charge right now
    @property
    def model(self):
        """The DNG-spec colour model, or None when the LibRaw matrix is used."""
        return self._dng if (self._dng is not None and dng_profile.OPTIONS.use_profile) else None

    @property
    def valid(self) -> bool:
        return True if self.model is not None else self._legacy_valid

    @property
    def rgb_cam(self):
        return self._legacy_rgb_cam

    @property
    def base_ev(self) -> float:
        """Exposure the preview adds on top of the raw data (stand-in for Lightroom's baseline)."""
        m = self.model
        if m is not None and m.base_ev is not None:
            return float(m.base_ev)
        return PREVIEW_BASE_EV

    @property
    def base_contrast(self) -> float:
        """The preview's own gentle S-curve; reduced when the profile's tone curve is applied."""
        m = self.model
        return 0.15 * (m.base_contrast_factor if m is not None else 1.0)

    def cam_to_srgb(self, mul) -> np.ndarray:
        """3x3 taking (camera RGB * mul) to linear sRGB (plain colorimetric, no profile look)."""
        m = self.model
        if m is not None:
            return m.cam_to_srgb(mul)
        return self._legacy_rgb_cam

    @staticmethod
    def neutral_to_mul(neutral) -> np.ndarray:
        n = np.maximum(np.asarray(neutral, dtype=np.float64), 1e-9)
        m = 1.0 / n
        return m / m[1]

    def mul_to_temp_tint(self, mul) -> tuple[float, float]:
        m = self.model
        if m is not None:
            return m.mul_to_temp_tint(mul)
        if not self._legacy_valid:
            return 5500.0, 0.0
        xyz = self._legacy_cam_to_xyz @ (1.0 / np.asarray(mul, dtype=np.float64))
        s = xyz.sum()
        return xy_to_temp_tint(xyz[0] / s, xyz[1] / s)

    def temp_tint_to_mul(self, temp, tint) -> np.ndarray:
        m = self.model
        if m is not None:
            return m.temp_tint_to_mul(temp, tint)
        if not self._legacy_valid:
            return self.as_shot_mul.copy()
        x, y = temp_tint_to_xy(float(temp), float(tint))
        xyz = np.array([x / y, 1.0, (1.0 - x - y) / y])
        return self.neutral_to_mul(self._legacy_xyz_to_cam @ xyz)

    @property
    def as_shot_temp_tint(self) -> tuple[float, float]:
        return self.mul_to_temp_tint(self.as_shot_mul)


# --------------------------------------------------------------------------
# Decoding (read-only)
# --------------------------------------------------------------------------

@dataclass
class ImageQuality:
    """Noise and softness measured on the decoded image before it is shrunk."""
    noise_luma: float = 0.0      # luminance noise, std-dev in 0..1 display units
    noise_chroma: float = 0.0    # colour (R-G, B-G) noise, same units
    sharp_ratio: float = 0.0     # edge-acuity ratio: ~3 crisp, ~1.5 very soft (0 = unknown)
    measured: bool = False


@dataclass
class Decoded:
    cam: np.ndarray          # HxWx3 float32, linear camera RGB, NOT white balanced, 0..1
    color: CameraColor
    baseline: float          # brightness normalisation used by auto white balance
    source_size: tuple       # (w, h) of the decoded half-size image
    quality: ImageQuality = field(default_factory=ImageQuality)
    masks: dict = field(default_factory=dict)      # preview mask estimates, computed on demand
    flip: int = 0            # LibRaw orientation: 0 none, 3 = 180°, 5 = 90° CCW, 6 = 90° CW (cam is shown upright)


def decode_raw(path: str) -> Decoded:
    """Open a RAW file READ-ONLY and return a small linear working copy."""
    import rawpy  # imported here so the rest of the module works without it

    kwargs = dict(
        use_camera_wb=False, use_auto_wb=False, user_wb=[1.0, 1.0, 1.0, 1.0],
        output_color=rawpy.ColorSpace.raw, gamma=(1, 1),
        no_auto_bright=True, output_bps=16,
    )
    profile = dng_profile.read_dng_profile(path) if path.lower().endswith(".dng") else None  # read-only
    with rawpy.imread(path) as raw:          # LibRaw opens the file read-only
        flip = int(getattr(raw.sizes, "flip", 0) or 0)
        try:
            rgb = raw.postprocess(half_size=True, **kwargs)
        except Exception:
            rgb = raw.postprocess(**kwargs)
        cam_xyz = np.array(raw.rgb_xyz_matrix, dtype=np.float64)
        as_shot = np.array(raw.camera_whitebalance, dtype=np.float64)

    dec = make_decoded(rgb.astype(np.float32) / 65535.0, cam_xyz, as_shot, profile)
    dec.flip = flip
    return dec


def _apply_lens_shading(cam_full: np.ndarray, profile) -> np.ndarray:
    """
    Flat-field the DNG's lens shading (OpcodeList2 GainMap). Pixel's wide-angle lenses lose
    a few stops in the corners, and Google's own rendering corrects that. Done on the
    linear camera data, before anything is measured. Returns cam_full unchanged when the
    file has no shading maps.
    """
    if profile is None or not dng_profile.APPLY_DNG_LENS_SHADING:
        return cam_full
    try:
        gain = profile.lens_shading_gain(*cam_full.shape[:2])
    except Exception:  # noqa: BLE001 - shading is a nicety; never block loading
        return cam_full
    if gain is None:
        return cam_full
    return np.minimum(cam_full * gain, 1.0).astype(np.float32)


def make_decoded(cam_full: np.ndarray, cam_xyz, as_shot_mul, profile=None) -> Decoded:
    h, w = cam_full.shape[:2]
    cam_full = _apply_lens_shading(cam_full, profile)
    s = WORK_LONG_EDGE / max(h, w)
    cam = cam_full
    if s < 1.0:
        cam = cv2.resize(cam_full, (max(1, round(w * s)), max(1, round(h * s))),
                         interpolation=cv2.INTER_AREA)
    cam = np.ascontiguousarray(cam, dtype=np.float32)
    color = CameraColor(cam_xyz, as_shot_mul, profile)
    lin = _cam_to_linear_srgb(cam, color.as_shot_mul, color)
    y = lin @ LUMA
    p = float(np.percentile(y, 99.0)) if y.size else 1.0
    baseline = float(np.clip(0.85 / max(p, 1e-6), 1.0, 6.0))
    try:
        quality = measure_quality(cam_full, color, baseline)
    except Exception:  # noqa: BLE001 - quality stats are optional, never block loading
        quality = ImageQuality()
    return Decoded(cam=cam, color=color, baseline=baseline, source_size=(w, h), quality=quality)


def _grad_mag(a: np.ndarray) -> np.ndarray:
    gx = np.zeros_like(a)
    gy = np.zeros_like(a)
    gx[:, 1:-1] = (a[:, 2:] - a[:, :-2]) * 0.5
    gy[1:-1, :] = (a[2:, :] - a[:-2, :]) * 0.5
    return np.hypot(gx, gy)


def measure_quality(cam_full: np.ndarray, color: CameraColor, baseline: float) -> ImageQuality:
    """
    Estimate noise and softness from the full-resolution decode (the 1400px
    working copy has the noise averaged away, so it can't be measured there).

    Noise: high-pass residual in the flattest mid-tone 8x8 blocks.
    Softness: how much a strong edge's gradient shrinks between a fine and a
    coarse blur. For a crisp edge that ratio is ~3.6 (the sigma ratio); the
    softer the edge, the closer it gets to 1.
    Measured in display (gamma) units after the same brightness normalisation
    as the preview, so an underexposed photo that will need lifting reports
    the noise you would actually see.
    """
    H, W = cam_full.shape[:2]
    cam = cam_full
    if H * W > 4_000_000:                       # centre crop keeps this quick on big sensors
        k = math.sqrt(4_000_000 / (H * W))
        ch, cw = int(H * k), int(W * k)
        y0, x0 = (H - ch) // 2, (W - cw) // 2
        cam = cam_full[y0:y0 + ch, x0:x0 + cw]
    lin = _cam_to_linear_srgb(cam, color.as_shot_mul, color) * np.float32(baseline)
    g = _srgb_encode(lin).astype(np.float32)
    Y = np.ascontiguousarray(g @ LUMA, dtype=np.float32)
    H, W = Y.shape
    B = 8
    h8, w8 = (H // B) * B, (W // B) * B
    if h8 < 64 or w8 < 64:
        return ImageQuality()

    def blocks(a):
        return a[:h8, :w8].reshape(h8 // B, B, w8 // B, B)

    # ---- noise -------------------------------------------------------
    hp = Y - cv2.GaussianBlur(Y, (0, 0), 1.0)
    lp = cv2.GaussianBlur(Y, (0, 0), 2.0)
    structure = blocks(_grad_mag(lp)).mean(axis=(1, 3))
    mean_y = blocks(Y).mean(axis=(1, 3))
    var_l = blocks(hp).var(axis=(1, 3))
    ok = (mean_y > 0.12) & (mean_y < 0.85)
    if ok.sum() < 40:
        ok = np.ones_like(ok)
    cand = np.flatnonzero(ok.ravel())
    order = cand[np.argsort(structure.ravel()[cand])]
    pick = order[:max(30, int(0.15 * len(order)))]
    # a Gaussian(1.0) high-pass keeps ~0.87 of white-noise std-dev
    noise_l = float(np.sqrt(np.median(var_l.ravel()[pick]))) / 0.87
    c1, c2 = g[..., 0] - g[..., 1], g[..., 2] - g[..., 1]
    v1 = blocks(c1 - cv2.GaussianBlur(c1, (0, 0), 1.0)).var(axis=(1, 3))
    v2 = blocks(c2 - cv2.GaussianBlur(c2, (0, 0), 1.0)).var(axis=(1, 3))
    noise_c = float(np.sqrt(np.median(((v1 + v2) * 0.5).ravel()[pick]))) / 0.87

    # ---- softness ----------------------------------------------------
    g1 = _grad_mag(cv2.GaussianBlur(Y, (0, 0), 0.7))
    g2 = _grad_mag(cv2.GaussianBlur(Y, (0, 0), 2.5))
    strong = g2 >= np.percentile(g2, 98.0)
    if strong.sum() < 50 or float(np.median(g2[strong])) < 0.01:
        ratio = 0.0                              # no real edges: can't tell
    else:
        ratio = float(np.median(g1[strong]) / np.median(g2[strong]))
    return ImageQuality(noise_l, noise_c, ratio, True)


def _cam_to_linear_srgb(cam, mul, color) -> np.ndarray:
    lin = (cam * np.asarray(mul, dtype=np.float32)) @ np.asarray(color.cam_to_srgb(mul), dtype=np.float32).T
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
    whites: int = 0              # -100..100  (white point)
    blacks: int = 0              # -100..100  (black point)
    lights: int = 0              # -100..100  tone curve, Lights region (crs:ParametricLights)
    saturation: int = 0
    sharp_amount: int = 40       # 0..150
    sharp_radius: float = 1.0    # 0.5..3.0
    sharp_detail: int = 25       # 0..100
    sharp_masking: int = 0       # 0..100
    nr_luma: int = 0             # 0..100
    nr_luma_detail: int = 50     # 0..100
    nr_color: int = 25           # 0..100
    angle: float = 0.0           # degrees, + = counter-clockwise
    upright: str = "Off"         # key of UPRIGHT_MODES
    # Upright as RAW Review solved it, so the preview and Lightroom show the same correction:
    # mode -> 9 numbers (row-major forward homography, 0..1 coordinates of the photo as shown).
    upright_matrices: dict | None = None
    upright_guides: list | None = None   # Guided: [[x1, y1, x2, y2], ...] 0..1 of the photo as shown
    constrain_crop: bool = True          # crop away the empty corners Upright leaves
    level_mode: str = "Off"      # key of LEVEL_MODES
    lens_profile: bool = True
    # Lightroom AI masks (Select Sky / Select Subject) with their local adjustments
    sky_on: bool = False
    sky_exposure: float = 0.0    # EV
    sky_contrast: int = 0
    sky_highlights: int = 0
    sky_dehaze: int = 0
    sky_saturation: int = 0
    sky_temp: int = 0            # local temperature, -100 (cooler) .. 100 (warmer)
    subj_on: bool = False
    subj_exposure: float = 0.0
    subj_contrast: int = 0
    subj_shadows: int = 0
    subj_texture: int = 0
    subj_clarity: int = 0
    subj_saturation: int = 0
    # Set when the settings were read from an existing Lightroom edit that has its own crop:
    # as long as the angle isn't changed here, that crop is left exactly as it was.
    lr_crop_angle: float | None = None
    # Subject crop: [left, top, right, bottom] as 0..1 fractions of the straightened frame, or None.
    # Same aspect ratio as the photo; set by the "center the subject" crop (see subject_crop_box).
    crop_box: list | None = None
    # Optional settings (whites, blacks, saturation, sharpening, noise) are only
    # written to the sidecar once they have been set, so an existing sidecar's
    # values are left alone until you or an auto tool touch them.
    touched: set = field(default_factory=set)
    notes: list = field(default_factory=list)

    def copy(self) -> "Edit":
        return copy.deepcopy(self)


@dataclass(frozen=True)
class Setting:
    key: str          # Edit attribute
    label: str
    lo: float
    hi: float
    step: float
    decimals: int
    default: float
    integer: bool = True
    optional: bool = False     # only written once touched (see Edit.touched)


# Every numeric slider, in display order. The UI and the batch editor are both
# built from this table.
SETTINGS = {s.key: s for s in [
    Setting("temp", "Temperature (K)", 2000, 50000, 50, 0, 5500, False),
    Setting("tint", "Tint", -150, 150, 1, 0, 0, False),
    Setting("exposure", "Exposure (EV)", -5, 5, 0.05, 2, 0, False),
    Setting("contrast", "Contrast", -100, 100, 1, 0, 0),
    Setting("highlights", "Highlights", -100, 100, 1, 0, 0),
    Setting("shadows", "Shadows", -100, 100, 1, 0, 0),
    Setting("whites", "Whites", -100, 100, 1, 0, 0, True, True),
    Setting("blacks", "Blacks", -100, 100, 1, 0, 0, True, True),
    Setting("lights", "Tone curve: Lights", -100, 100, 1, 0, 0, True, True),
    Setting("vibrance", "Vibrance", -100, 100, 1, 0, 0),
    Setting("saturation", "Saturation", -100, 100, 1, 0, 0, True, True),
    Setting("sharp_amount", "Amount", 0, 150, 1, 0, 40, True, True),
    Setting("sharp_radius", "Radius", 0.5, 3.0, 0.1, 1, 1.0, False, True),
    Setting("sharp_detail", "Detail", 0, 100, 1, 0, 25, True, True),
    Setting("sharp_masking", "Masking", 0, 100, 1, 0, 0, True, True),
    Setting("nr_luma", "Luminance", 0, 100, 1, 0, 0, True, True),
    Setting("nr_luma_detail", "Luminance detail", 0, 100, 1, 0, 50, True, True),
    Setting("nr_color", "Color", 0, 100, 1, 0, 25, True, True),
    Setting("angle", "Straighten (°, + = counter-clockwise)", -15, 15, 0.05, 2, 0, False),
    Setting("sky_exposure", "Exposure (EV)", -2, 2, 0.05, 2, 0, False),
    Setting("sky_contrast", "Contrast", -100, 100, 1, 0, 0),
    Setting("sky_highlights", "Highlights", -100, 100, 1, 0, 0),
    Setting("sky_dehaze", "Dehaze", -100, 100, 1, 0, 0),
    Setting("sky_saturation", "Saturation", -100, 100, 1, 0, 0),
    Setting("sky_temp", "Temperature", -100, 100, 1, 0, 0),
    Setting("subj_exposure", "Exposure (EV)", -2, 2, 0.05, 2, 0, False),
    Setting("subj_contrast", "Contrast", -100, 100, 1, 0, 0),
    Setting("subj_shadows", "Shadows", -100, 100, 1, 0, 0),
    Setting("subj_texture", "Texture", -100, 100, 1, 0, 0),
    Setting("subj_clarity", "Clarity", -100, 100, 1, 0, 0),
    Setting("subj_saturation", "Saturation", -100, 100, 1, 0, 0),
]}
MASK_KEYS = {"sky": [k for k in SETTINGS if k.startswith("sky_")],
             "subject": [k for k in SETTINGS if k.startswith("subj_")]}

# "Never use Lightroom AI masks" (Auto options): no Select Sky / Select Subject is ever turned on or written, and
# RAW Review's own ones are taken out of sidecars it writes. Masks you made in Lightroom are never touched.
# (Some stock agencies look for AI in a photo's metadata; a Select Subject mask is recorded in exported JPEGs.)
NO_AI_MASKS = False

# Friendly names used in the batch editor (the slider labels above are short
# because they sit under a group heading).
SETTING_NAMES = {
    "sharp_amount": "Sharpening amount", "sharp_radius": "Sharpening radius",
    "sharp_detail": "Sharpening detail", "sharp_masking": "Sharpening masking",
    "nr_luma": "Noise reduction: luminance", "nr_luma_detail": "Noise reduction: luminance detail",
    "nr_color": "Noise reduction: color", "angle": "Straighten angle",
    **{k: "Sky mask: " + SETTINGS[k].label for k in MASK_KEYS["sky"]},
    **{k: "Subject mask: " + SETTINGS[k].label for k in MASK_KEYS["subject"]},
}


def setting_name(key: str) -> str:
    return SETTING_NAMES.get(key) or SETTINGS[key].label


def change_setting(edit: Edit, key: str, action: str, amount: float = 0.0):
    """Increase / decrease / set / reset one setting, clamped to its range."""
    s = SETTINGS[key]
    cur = float(getattr(edit, key))
    if action == "increase":
        val = cur + abs(amount)
    elif action == "decrease":
        val = cur - abs(amount)
    elif action == "set":
        val = amount
    elif action == "reset":
        val = s.default
    else:
        raise ValueError(action)
    val = min(max(val, s.lo), s.hi)
    val = int(round(val)) if s.integer else round(val, s.decimals)
    setattr(edit, key, val)
    if s.optional:
        edit.touched.add(key)
    if key in ("temp", "tint"):
        edit.wb_mode = "custom"
    if key == "angle":
        edit.level_mode = "Manual"
    if key.startswith("sky_") and not NO_AI_MASKS:
        edit.sky_on = True
    elif key.startswith("subj_") and not NO_AI_MASKS:
        edit.subj_on = True


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
    M: np.ndarray | None = None   # 2x3 affine (working -> rotated); None = no rotation
    x0: int = 0
    y0: int = 0
    H: np.ndarray | None = None   # 3x3 Upright warp in working pixels, applied first; None = none

    def to_display(self, x, y):
        if self.H is not None:
            v = self.H @ np.array([x, y, 1.0])
            x, y = v[0] / v[2], v[1] / v[2]
        if self.M is not None:
            x, y = (self.M[0, 0] * x + self.M[0, 1] * y + self.M[0, 2],
                    self.M[1, 0] * x + self.M[1, 1] * y + self.M[1, 2])
        return x - self.x0, y - self.y0

    def to_source(self, x, y):
        rx, ry = x + self.x0, y + self.y0
        if self.M is not None:
            inv = cv2.invertAffineTransform(self.M)
            rx, ry = (inv[0, 0] * rx + inv[0, 1] * ry + inv[0, 2],
                      inv[1, 0] * rx + inv[1, 1] * ry + inv[1, 2])
        if self.H is not None:
            v = np.linalg.inv(self.H) @ np.array([rx, ry, 1.0])
            rx, ry = v[0] / v[2], v[1] / v[2]
        return rx, ry


def _box_pixels(w: int, h: int, box):
    """Pixel rectangle (x0, y0, cw, ch) of a 0..1 crop box inside a w x h image."""
    l, t, r, b = (min(max(float(v), 0.0), 1.0) for v in box)
    x0, y0 = int(round(l * w)), int(round(t * h))
    cw, ch = max(1, int(round((r - l) * w))), max(1, int(round((b - t) * h)))
    return x0, y0, min(cw, w - x0), min(ch, h - y0)


def rotate_and_crop(img: np.ndarray, deg: float, box=None):
    """Straighten (rotate + largest centred crop), then apply the optional subject crop box."""
    h, w = img.shape[:2]
    M, x0, y0 = None, 0, 0
    out = img
    if abs(deg) >= 1e-3:
        M = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), deg, 1.0)
        out = cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_LINEAR,
                             borderMode=cv2.BORDER_REPLICATE)
        k = crop_factor(w, h, deg)
        cw, ch = max(1, int(w * k)), max(1, int(h * k))
        x0, y0 = (w - cw) // 2, (h - ch) // 2
        out = out[y0:y0 + ch, x0:x0 + cw]
    if box:
        bx, by, bw, bh = _box_pixels(out.shape[1], out.shape[0], box)
        out = out[by:by + bh, bx:bx + bw]
        x0, y0 = x0 + bx, y0 + by
    if M is None and not box:
        return img, Geometry()
    return np.ascontiguousarray(out), Geometry(M, x0, y0)


def _srgb_encode(x):
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.0031308, 12.92 * x,
                    1.055 * np.power(np.maximum(x, 0.0031308), 1 / 2.4) - 0.055)


# Adobe's default tone curve. Lightroom applies it to every raw (unless the camera profile
# brings its own), brightening the midtones by about a stop and rolling off the highlights.
# The preview used to skip it, so Lightroom rendered RAW Review's settings ~1.3 stops brighter
# than the preview showed. PREVIEW_ADOBE_EV was fitted on two Lightroom exports of RAW
# Review's edits (with the curve, the preview matched Lightroom's tones to ~0.03).
PREVIEW_ADOBE_EV = 0.23
_ACR3_LUT = np.interp(np.linspace(0.0, 1.0, 4096), np.linspace(0.0, 1.0, len(ACR3_CURVE)),
                      ACR3_CURVE).astype(np.float32)


def adobe_tone(lin: np.ndarray) -> np.ndarray:
    """Adobe's default curve, applied like the DNG SDK's RGB tone: the brightest and darkest
    channel go through the curve and the middle one keeps its relative place, so hues hold."""
    lin = np.clip(lin, 0.0, 1.0, dtype=np.float32)
    n = np.float32(len(_ACR3_LUT) - 1)
    r, g, b = lin[..., 0], lin[..., 1], lin[..., 2]
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    mx2 = np.take(_ACR3_LUT, (mx * n + 0.5).astype(np.int32))
    mn2 = np.take(_ACR3_LUT, (mn * n + 0.5).astype(np.int32))
    span = mx - mn
    scale = np.divide(mx2 - mn2, span, out=np.zeros_like(span), where=span > 1e-6)
    out = lin - mn[..., None]
    out *= scale[..., None]
    out += mn2[..., None]
    return out


# Highlights slider (negative = recover), modelled like Lightroom's: only tones above
# RECOVER_FROM (linear, ~0.63 on screen) move, and at -100 up to RECOVER_STOPS stops of
# highlight detail above white are brought back under white. Midtones are left alone.
RECOVER_FROM = 0.35
RECOVER_STOPS = 1.5            # Lightroom brings back ~1.5 stops at -100; 2.0 overstated it


def recover_highlights(lin: np.ndarray, highlights: float) -> np.ndarray:
    """Compress linear tones above RECOVER_FROM so that 2^(RECOVER_STOPS*amount) lands at white."""
    amount = min(max(-float(highlights) / 100.0, 0.0), 1.0)
    if amount <= 0.0:
        return lin
    t = np.float32(RECOVER_FROM)
    W = np.float32(2.0 ** (RECOVER_STOPS * amount))
    c = (1 - t) * (W - t) / (W - 1)                  # so that f(W) == 1 exactly
    Y = np.maximum(lin @ LUMA, np.float32(1e-6))
    over = np.maximum(Y - t, 0)
    fY = np.where(Y > t, t + over / (1 + over / c), Y)
    return lin * (fY / Y)[..., None]


def base_gamma(cam: np.ndarray, color: CameraColor, mul, exposure: float,
               highlights: float = 0.0) -> np.ndarray:
    """White balance + exposure + highlight recovery + shoulder + sRGB curve (no other tone sliders)."""
    model = color.model
    if model is not None:
        # DNG with its own profile: colour matrices + hue/sat map + look + tone curve
        lin = recover_highlights(model.render_linear(cam, mul, exposure, color.base_ev), highlights)
    else:
        lin = _cam_to_linear_srgb(cam, mul, color)
        lin *= np.float32(2.0 ** (exposure + PREVIEW_BASE_EV + PREVIEW_ADOBE_EV))
        lin = recover_highlights(lin, highlights)
        lin = adobe_tone(lin)                        # what Lightroom does by default
    return _srgb_encode(lin).astype(np.float32)


def apply_tone(g: np.ndarray, e: Edit, stop: str | None = None, base_c: float = 0.15) -> np.ndarray:
    """
    Tone sliders, in order: "levels" (whites/blacks) -> "shhi" (shadows/highlights)
    -> "contrast". `stop` ends after that stage (used by the auto tools to
    measure the image at each step); None applies everything.
    """
    L = np.clip(g @ LUMA, 1e-4, 1.0)
    L1 = L
    lo = -0.15 * e.blacks / 100.0                # blacks +: lift, -: crush
    hi = 1.0 - 0.30 * e.whites / 100.0           # whites +: brighten, -: pull down
    if lo != 0.0 or hi != 1.0:
        L1 = np.clip((L1 - lo) / max(hi - lo, 0.2), 0, 1)
    if stop != "levels":
        ms = 6.75 * L1 * (1 - L1) ** 2           # shadow mask, peaks at L=1/3
        mh = 6.75 * L1 ** 2 * (1 - L1)           # highlight mask, peaks at L=2/3
        # negative Highlights is applied before this, in base_gamma (recover_highlights)
        L1 = np.clip(L1 + 0.25 * (e.shadows / 100.0 * ms + max(e.highlights, 0) / 100.0 * mh), 0, 1)
        if stop != "shhi":
            c = base_c + e.contrast / 100.0      # small base S-curve, like a raw default
            S = L1 * L1 * (3 - 2 * L1)
            L1 = np.clip(L1 + c * (S - L1), 0, 1)
            if e.lights:                         # tone curve Lights: bump on 0.25..1, peak ~0.62
                bump = np.sin(np.pi * np.clip((L1 - 0.25) / 0.75, 0, 1))
                L1 = np.clip(L1 + 0.12 * e.lights / 100.0 * bump, 0, 1)
    return g * (L1 / L)[..., None]


def apply_colour(g: np.ndarray, e: Edit) -> np.ndarray:
    """Vibrance (boosts muted colours most) and saturation, around each pixel's channel mean.
    Written channel by channel: numpy's reductions across the colour axis were ~5x slower."""
    if not e.vibrance and not e.saturation:
        return g
    g = np.array(g, dtype=np.float32, copy=True)
    third = np.float32(1.0 / 3.0)
    if e.vibrance:
        r, gg, b = g[..., 0], g[..., 1], g[..., 2]
        mean = (r + gg + b) * third
        sat = np.maximum(np.maximum(r, gg), b) - np.minimum(np.minimum(r, gg), b)
        k = np.float32(1.0) + np.float32(e.vibrance / 100.0) * (np.float32(1.0) - np.clip(sat, 0, 1))
        for c in range(3):
            ch = g[..., c]
            ch -= mean
            ch *= k
            ch += mean
    if e.saturation:
        mean = (g[..., 0] + g[..., 1] + g[..., 2]) * third
        k = np.float32(1.0 + e.saturation / 100.0)
        for c in range(3):
            ch = g[..., c]
            ch -= mean
            ch *= k
            ch += mean
    return g


def apply_look(g: np.ndarray) -> np.ndarray:
    """
    Adobe Color-like colour for the on-screen preview (see PREVIEW_LOOK): a saturation boost
    that peaks in the midtones and fades out toward black and white. Luminance is kept, so
    the preview's brightness and histogram match what Lightroom shows.
    """
    g = np.clip(g, 0.0, 1.0, dtype=np.float32)
    L = g @ LUMA
    d = (L - np.float32(0.5)) * np.float32(2.0)
    k = np.float32(LOOK_SATURATION) * np.maximum(np.float32(1.0) - d * d, np.float32(0.0))
    out = g - L[..., None]
    out *= (np.float32(1.0) + k)[..., None]
    out += L[..., None]
    return np.clip(out, 0.0, 1.0, out=out)


def _small_view(dec: Decoded, long_edge: int = 320):
    cam = dec.cam
    H, W = cam.shape[:2]
    k = long_edge / max(H, W)
    small = cv2.resize(cam, (max(8, round(W * k)), max(8, round(H * k))), interpolation=cv2.INTER_AREA)
    lin = _cam_to_linear_srgb(small, dec.color.as_shot_mul, dec.color) * dec.baseline
    return np.clip(lin, 0, 4).astype(np.float32), (W, H)


def _upscale_mask(m: np.ndarray, size, feather: float) -> np.ndarray:
    m = cv2.GaussianBlur(m.astype(np.float32), (0, 0), feather)
    return np.clip(cv2.resize(m, size, interpolation=cv2.INTER_LINEAR), 0, 1).astype(np.float32)


def estimate_sky(dec: Decoded):
    """(mask 0..1 at working size, fraction of frame, confidence 0..1). Cached on dec."""
    if "sky" in dec.masks:
        return dec.masks["sky"]
    lin, size = _small_view(dec)
    h, w = lin.shape[:2]
    Y = lin @ LUMA
    g = _srgb_encode(np.clip(Y, 0, 1))
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    tex = cv2.GaussianBlur(np.hypot(gx, gy), (0, 0), 2.0)
    r, gg, b = lin[..., 0], lin[..., 1], lin[..., 2]
    blueish = (b > gg * 1.02) & (b > r * 1.05)
    neutral = (np.abs(r - b) < 0.12 * (Y + 1e-3)) & (np.abs(gg - Y) < 0.15 * (Y + 1e-3))
    top_ref = float(np.percentile(g[: max(2, h // 4)], 75))
    bright = g > max(0.35, 0.55 * top_ref)
    cand = (tex < 0.06) & bright & (blueish | neutral)
    rows = np.linspace(1.0, 0.0, h, dtype=np.float32)[:, None]
    cand &= rows > 0.15                                           # sky is (almost) never at the very bottom
    n, lab, stats, _ = cv2.connectedComponentsWithStats(cand.astype(np.uint8), 8)
    keep = np.zeros_like(cand)
    for i in range(1, n):
        x, y, ww, hh, area = stats[i]
        if y <= 1 and area > 0.01 * h * w:                       # touches the top edge
            keep |= lab == i
    if keep.any():
        # clouds, birds, sun: holes completely surrounded by sky belong to the sky
        holes = (~keep).astype(np.uint8)
        nh, hl, hs, _ = cv2.connectedComponentsWithStats(holes, 8)
        for i in range(1, nh):
            x, y, ww, hh, area = hs[i]
            if x > 0 and y > 0 and x + ww < w and y + hh < h:
                keep |= hl == i
        keep = cv2.erode(keep.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0    # pull back from the horizon
    frac = float(keep.mean())
    if frac < 0.03:
        out = (np.zeros(dec.cam.shape[:2], np.float32), 0.0, 0.0)
    else:
        smooth = float(tex[keep].mean()) if keep.any() else 1.0
        blue_share = float((blueish & keep).sum() / max(keep.sum(), 1))
        conf = float(np.clip(min(1.0, frac / 0.12) * (1.0 - smooth / 0.06) * (0.6 + 0.4 * blue_share), 0, 1))
        out = (_upscale_mask(keep, size, 0.8), frac, conf)
    dec.masks["sky"] = out
    return out


def estimate_subject(dec: Decoded):
    """(mask, fraction, confidence): the sharp, central region that stands out from its surroundings."""
    if "subject" in dec.masks:
        return dec.masks["subject"]
    lin, size = _small_view(dec)
    h, w = lin.shape[:2]
    g = _srgb_encode(np.clip(lin @ LUMA, 0, 1))
    lap = np.abs(cv2.Laplacian(cv2.GaussianBlur(g, (0, 0), 0.8), cv2.CV_32F))
    focus = cv2.GaussianBlur(lap, (0, 0), max(h, w) / 40.0)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    centre = np.exp(-(((xx - w / 2) / (0.42 * w)) ** 2 + ((yy - h * 0.55) / (0.45 * h)) ** 2))
    sky_small = cv2.resize(estimate_sky(dec)[0], (w, h), interpolation=cv2.INTER_AREA)
    # the horizon / skyline is a sharp edge too, but it isn't the subject
    sky_bin = (sky_small > 0.5).astype(np.uint8)
    band = cv2.dilate(sky_bin, np.ones((15, 15), np.uint8)) - cv2.erode(sky_bin, np.ones((3, 3), np.uint8))
    lap = lap * (1 - band.astype(np.float32))
    focus = cv2.GaussianBlur(lap, (0, 0), max(h, w) / 90.0)
    score = focus / (np.percentile(focus, 98) + 1e-6) * (0.35 + 0.65 * centre) * (1 - sky_small)
    thr = max(float(np.percentile(score, 88)), 0.35 * float(np.percentile(score, 99.5)))
    m = (score > thr).astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    if n <= 1:
        out = (np.zeros(dec.cam.shape[:2], np.float32), 0.0, 0.0)
    else:
        best = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        blob = lab == best
        blob = cv2.morphologyEx(blob.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8)) > 0
        frac = float(blob.mean())
        inside = float(score[blob].mean())
        outside = float(score[~blob].mean()) if (~blob).any() else 0.0
        conf = float(np.clip((inside / (outside + 1e-6) - 1.5) / 3.0, 0, 1)) if 0.03 < frac < 0.7 else 0.0
        out = (_upscale_mask(blob, size, 1.5), frac, conf)
    dec.masks["subject"] = out
    return out


# --------------------------------------------------------------------------
# Subject crop: a minimal crop that puts the main subject (plane, car, ...) in the centre
# --------------------------------------------------------------------------
SUBJECT_CROP_MIN_SCALE = 0.6   # never keep less than this share of the width / height
SUBJECT_CROP_DEADBAND = 0.03   # subject already this close to the centre: no crop
SUBJECT_CROP_MIN_CONF = 0.35


def _spectral_residual(gray: np.ndarray) -> np.ndarray:
    """Hou & Zhang spectral-residual saliency of a small grey image (0..1 out)."""
    small = cv2.resize(gray, (64, 64), interpolation=cv2.INTER_AREA).astype(np.float32)
    f = np.fft.fft2(small)
    logamp = np.log(np.abs(f) + 1e-6)
    resid = logamp - cv2.blur(logamp, (3, 3))
    sal = np.abs(np.fft.ifft2(np.exp(resid + 1j * np.angle(f)))) ** 2
    sal = cv2.GaussianBlur(sal.astype(np.float32), (0, 0), 2.5)
    sal = cv2.resize(sal, (gray.shape[1], gray.shape[0]), interpolation=cv2.INTER_LINEAR)
    return sal / (float(sal.max()) + 1e-9)


def find_subject_box(rgb: np.ndarray):
    """
    Bounding box of the main subject in an 8-bit RGB image: ((l, t, r, b) as 0..1 fractions,
    confidence 0..1), or (None, 0.0). Looks for the region that stands out from the background
    by colour, sharpness and shape (a plane against sky, a car against a blurred road).
    """
    h0, w0 = rgb.shape[:2]
    k = 400.0 / max(h0, w0)
    img = cv2.resize(rgb, (max(16, round(w0 * k)), max(16, round(h0 * k))), interpolation=cv2.INTER_AREA)
    h, w = img.shape[:2]
    lab = cv2.cvtColor(img, cv2.COLOR_RGB2LAB).astype(np.float32)
    gray = lab[..., 0] / 255.0

    def norm(a):
        lo, hi = np.percentile(a, 2), np.percentile(a, 99.5)
        return np.clip((a - lo) / (hi - lo + 1e-6), 0, 1)

    # colour distance from the background (the frame's border is mostly background)
    bw = max(2, min(h, w) // 20)
    border = np.concatenate([lab[:bw].reshape(-1, 3), lab[-bw:].reshape(-1, 3),
                             lab[:, :bw].reshape(-1, 3), lab[:, -bw:].reshape(-1, 3)])
    bg = np.median(border, axis=0)
    spread = np.median(np.abs(border - bg), axis=0) + 4.0
    colour = norm(cv2.GaussianBlur(np.sqrt((((lab - bg) / spread) ** 2).sum(axis=2)), (0, 0), 2.0))
    # sharpness: the subject is in focus, the background often isn't
    lap = np.abs(cv2.Laplacian(cv2.GaussianBlur(gray, (0, 0), 0.8), cv2.CV_32F))
    focus = norm(cv2.GaussianBlur(lap, (0, 0), max(h, w) / 60.0))
    spec = norm(_spectral_residual(gray))
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    prior = np.exp(-(((xx - w / 2) / (0.6 * w)) ** 2 + ((yy - h / 2) / (0.6 * h)) ** 2))
    sal = (0.40 * colour + 0.30 * spec + 0.30 * focus) * (0.6 + 0.4 * prior)

    thr = max(float(np.percentile(sal, 85)), 0.5 * float(sal.max()))
    m = (sal > thr).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    n, lab_cc, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    best, best_score = 0, 0.0
    for i in range(1, n):
        x, y, ww, hh, area = stats[i]
        if area < 0.002 * h * w:
            continue
        edges = (x <= 0) + (y <= 0) + (x + ww >= w) + (y + hh >= h)
        if edges >= 2 or ww * hh > 0.6 * h * w:     # spans the frame: that's background / scenery
            continue
        score = float(sal[lab_cc == i].sum()) * (0.5 if edges else 1.0)
        if score > best_score:
            best, best_score = i, score
    if not best:
        return None, 0.0
    x, y, ww, hh, area = stats[best]
    blob = lab_cc == best
    # parts of the same subject (wing, tail, wheels) that fell just outside: merge nearby blobs
    near = cv2.dilate(blob.astype(np.uint8), np.ones((15, 15), np.uint8)) > 0
    for i in range(1, n):
        if i != best and stats[i][4] >= 0.0005 * h * w and near[lab_cc == i].any():
            xi, yi, wi, hi_, _a = stats[i]
            nx0, ny0 = min(x, xi), min(y, yi)
            nx1, ny1 = max(x + ww, xi + wi), max(y + hh, yi + hi_)
            x, y, ww, hh = nx0, ny0, nx1 - nx0, ny1 - ny0
    inside = np.zeros((h, w), bool)
    inside[y:y + hh, x:x + ww] = True
    ratio = float(sal[inside].mean()) / (float(sal[~inside].mean()) + 1e-6) if (~inside).any() else 0.0
    conf = float(np.clip((ratio - 1.6) / 2.0, 0, 1))
    return (x / w, y / h, (x + ww) / w, (y + hh) / h), conf


def subject_crop_box(subject, min_scale: float = SUBJECT_CROP_MIN_SCALE):
    """
    The smallest same-aspect crop (as [l, t, r, b] fractions) that puts the subject's centre in the
    middle of the frame, never cutting into the subject and never keeping less than `min_scale` of
    the width/height. If that would need more cropping, the subject is moved as close to the
    centre as the limit allows. Returns None when no crop is needed.
    """
    l, t, r, b = subject
    cx, cy = (l + r) / 2.0, (t + b) / 2.0
    if abs(cx - 0.5) < SUBJECT_CROP_DEADBAND and abs(cy - 0.5) < SUBJECT_CROP_DEADBAND:
        return None
    pad_x, pad_y = 0.04 + 0.08 * (r - l), 0.04 + 0.08 * (b - t)
    need = max(min_scale, (r - l) + 2 * pad_x, (b - t) + 2 * pad_y)
    if need >= 0.999:
        return None                                   # subject too big to crop around
    best = 2.0 * min(cx, 1 - cx, cy, 1 - cy)          # crop size that centres it exactly
    if best >= need:
        s = best
    else:                                             # can't fully centre: get as close as allowed
        s = need
        cx = min(max(cx, s / 2), 1 - s / 2)
        cy = min(max(cy, s / 2), 1 - s / 2)
        s = 2.0 * min(cx, 1 - cx, cy, 1 - cy)
    if s >= 0.995:
        return None
    return [round(float(v), 5) for v in (cx - s / 2, cy - s / 2, cx + s / 2, cy + s / 2)]


def _apply_local(g: np.ndarray, mask: np.ndarray, ev=0.0, contrast=0, highlights=0, shadows=0,
                 dehaze=0, saturation=0, temp=0, texture=0, clarity=0) -> np.ndarray:
    """Preview approximation of one Lightroom local adjustment, blended through `mask`."""
    if mask is None or not mask.any():
        return g
    loc = g
    ev_total = ev - 0.004 * dehaze
    if ev_total:
        loc = np.clip(loc * np.float32(2.0 ** (ev_total / 2.2)), 0, 1.5)
    if contrast or highlights or shadows or dehaze:
        e = Edit(contrast=int(contrast + 0.5 * dehaze), highlights=int(highlights), shadows=int(shadows))
        loc = apply_tone(loc, e, None, 0.0)
    if texture or clarity:
        L = loc @ LUMA
        for amt, sigma in ((texture, 1.5), (clarity, 6.0)):
            if amt:
                detail = L - cv2.GaussianBlur(L, (0, 0), sigma)
                loc = loc + (0.8 * amt / 100.0) * detail[..., None]
    sat = saturation + 0.3 * dehaze
    if sat:
        mean = loc.mean(axis=2, keepdims=True)
        loc = mean + (loc - mean) * (1 + sat / 100.0)
    if temp:
        k = 0.12 * temp / 100.0
        loc = loc * np.array([1 + k, 1.0, 1 - k], dtype=np.float32)
    return g + mask[..., None] * (loc - g)


def apply_masks(g: np.ndarray, dec: Decoded, e: Edit) -> np.ndarray:
    if e.sky_on:
        g = _apply_local(g, estimate_sky(dec)[0], e.sky_exposure, e.sky_contrast, e.sky_highlights, 0,
                         e.sky_dehaze, e.sky_saturation, e.sky_temp)
    if e.subj_on:
        g = _apply_local(g, estimate_subject(dec)[0], e.subj_exposure, e.subj_contrast, 0, e.subj_shadows,
                         0, e.subj_saturation, 0, e.subj_texture, e.subj_clarity)
    return g


def half_decoded(dec: Decoded, edit: Edit | None = None) -> Decoded:
    """
    A half-size copy of `dec` (cached on it) for a quick preview while a slider is being dragged:
    about 4x fewer pixels, so the photo follows the slider; the full-size render replaces it as
    soon as you stop. The sky / subject masks are the full ones shrunk, so they match exactly.
    Not for DNGs with their own profile (it may carry per-pixel maps sized to the full image).
    """
    half = dec.masks.get("_half")
    if half is None:
        h, w = dec.cam.shape[:2]
        cam = cv2.resize(dec.cam, (max(1, w // 2), max(1, h // 2)), interpolation=cv2.INTER_AREA)
        half = Decoded(cam=cam, color=dec.color, baseline=dec.baseline, source_size=dec.source_size,
                       quality=dec.quality, masks={})
        dec.masks["_half"] = half
    for k, v in dec.masks.items():
        if k.startswith("_upright:") and k not in half.masks:
            half.masks[k] = v
    hh, hw = half.cam.shape[:2]
    for key, on, fn in (("sky", edit is not None and edit.sky_on, estimate_sky),
                        ("subject", edit is not None and edit.subj_on, estimate_subject)):
        if key not in half.masks and (on or key in dec.masks):
            m, frac, conf = fn(dec)
            half.masks[key] = (cv2.resize(m, (hw, hh), interpolation=cv2.INTER_AREA), frac, conf)
    return half


def render(dec: Decoded, edit: Edit, original: bool = False, rotate: bool = True, show_masks: bool = False,
           look: bool = True):
    """Return (uint8 RGB image, Geometry). original=True shows the as-shot file.
    show_masks tints the (approximate) Sky mask blue and the Subject mask orange.
    look=True adds the Lightroom-style look for display (non-DNG raws, see PREVIEW_LOOK);
    the auto tools pass look=False so they measure the plain render they were tuned on."""
    color = dec.color
    if original or edit.wb_mode != "custom":
        mul = color.as_shot_mul
    else:
        mul = color.temp_tint_to_mul(edit.temp, edit.tint)

    g = base_gamma(dec.cam, color, mul, 0.0 if original else edit.exposure,
                   0.0 if original else edit.highlights)
    e = Edit() if original else edit
    g = apply_tone(g, e, None, color.base_contrast)
    g = apply_colour(g, e)
    if not original and (e.sky_on or e.subj_on):
        g = apply_masks(g, dec, e)
    if look and PREVIEW_LOOK and LOOK_SATURATION and color.model is None:   # DNGs use their own profile look
        g = apply_look(g)
    if show_masks and not original:
        for key, on, tint in (("sky", e.sky_on, (0.2, 0.6, 1.0)), ("subject", e.subj_on, (1.0, 0.55, 0.1))):
            if on:
                m = (estimate_sky(dec) if key == "sky" else estimate_subject(dec))[0][..., None] * 0.45
                g = g * (1 - m) + np.array(tint, dtype=np.float32) * m

    img = (np.clip(g, 0, 1) * 255 + 0.5).astype(np.uint8)
    Hup = None
    if rotate and not original and edit.upright != "Off":
        stored = (edit.upright_matrices or {}).get(edit.upright)
        if stored is not None:
            ih, iw = img.shape[:2]
            N = np.array([[iw, 0, 0], [0, ih, 0], [0, 0, 1]], float)
            Hup = N @ np.asarray(stored, float).reshape(3, 3) @ np.linalg.inv(N)
        elif edit.upright_matrices is None:          # not solved by RAW Review (e.g. read from Lightroom)
            Hup = upright_for(dec, edit.upright, img)
        if Hup is not None:
            img = cv2.warpPerspective(img, Hup, (img.shape[1], img.shape[0]), flags=cv2.INTER_LINEAR,
                                      borderMode=cv2.BORDER_CONSTANT, borderValue=UPRIGHT_PREVIEW_FILL)
    if rotate and not original:
        out, geo = rotate_and_crop(img, edit.angle, edit.crop_box)
        geo.H = Hup
        return out, geo
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

# --------------------------------------------------------------------------
# Upright preview (approximate). Lightroom computes the real correction from the sidecar's
# PerspectiveUpright; this shows something close so you can judge it here:
#   Level    rotate so near-horizontal lines are level
#   Vertical send the vertical vanishing point to infinity: converging verticals become parallel
#   Auto     Level + a partial Vertical correction (Lightroom's balanced mode)
#   Full     Vertical + the same for the horizontal vanishing point
# --------------------------------------------------------------------------
UPRIGHT_PREVIEW_FILL = (40, 40, 42)          # uncovered corners, like Lightroom without Constrain Crop


def _segments(rgb: np.ndarray, max_side: int = 1000):
    """Straight line segments (N x 4, in the given image's pixels) and their lengths."""
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    s = max_side / max(h, w)
    if s < 1:
        gray = cv2.resize(gray, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA)
    else:
        s = 1.0
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    med = float(np.median(gray))
    edges = cv2.Canny(gray, int(max(10, 0.66 * med)), int(min(255, max(40, 1.33 * med))))
    gh, gw = gray.shape
    diag = math.hypot(gw, gh)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 720, threshold=50,
                            minLineLength=int(0.06 * diag), maxLineGap=int(0.006 * diag) + 2)
    if lines is None:
        return np.zeros((0, 4)), np.zeros(0)
    seg = np.asarray(lines, dtype=float).reshape(-1, 4) / s
    return seg, np.hypot(seg[:, 2] - seg[:, 0], seg[:, 3] - seg[:, 1])


def _vanishing_point(seg, wts, cx, cy, scale):
    """Least-squares intersection of the segments' lines (homogeneous, centred / scaled coords)."""
    p1 = np.c_[(seg[:, 0] - cx) / scale, (seg[:, 1] - cy) / scale, np.ones(len(seg))]
    p2 = np.c_[(seg[:, 2] - cx) / scale, (seg[:, 3] - cy) / scale, np.ones(len(seg))]
    L = np.cross(p1, p2)
    L /= np.linalg.norm(L[:, :2], axis=1, keepdims=True) + 1e-12
    w = np.asarray(wts, float)
    for _ in range(3):                                    # re-weight: drop lines that disagree
        A = (L * w[:, None]).T @ L
        _vals, vecs = np.linalg.eigh(A)
        V = vecs[:, 0]
        r = np.abs(L @ V) / (np.linalg.norm(V[:2]) + 1e-12)
        w = np.asarray(wts, float) * (r < max(0.02, 2.5 * np.median(r) + 1e-9))
        if w.sum() == 0:
            break
    return V


def upright_matrix(rgb: np.ndarray, mode: str, guides=None, seg_cache=None):
    """3x3 warp (in this image's pixels) for Lightroom Upright `mode`, or None.
    Guided uses `guides` ([[x1, y1, x2, y2], ...] in 0..1) instead of every line in the photo."""
    if mode not in ("Level", "Vertical", "Auto", "Full", "Guided"):
        return None
    h, w = rgb.shape[:2]
    if mode == "Guided":
        if not guides:
            return None
        seg = np.array([[g[0] * w, g[1] * h, g[2] * w, g[3] * h] for g in guides], float)
        ln = np.hypot(seg[:, 2] - seg[:, 0], seg[:, 3] - seg[:, 1])
    else:
        seg, ln = seg_cache if seg_cache is not None else _segments(rgb)
    if len(seg) == 0:
        return None
    need = 2 if mode == "Guided" else 3
    dx, dy = seg[:, 2] - seg[:, 0], seg[:, 3] - seg[:, 1]
    ang = np.degrees(np.arctan2(dy, dx)) % 180.0
    # Real verticals lean at most ~15° even on a wide lens tilted up; beyond that it's usually
    # a diagonal (roof trusses, stairs) that would twist the correction.
    vert = np.abs(ang - 90.0) <= 15.0
    horz = (ang <= 15.0) | (ang >= 165.0)
    cx, cy, sc = w / 2.0, h / 2.0, max(w, h) / 2.0
    C = np.array([[1 / sc, 0, -cx / sc], [0, 1 / sc, -cy / sc], [0, 0, 1]])   # pixels -> centred units
    Hc = np.eye(3)

    def level_rotation():
        if horz.sum() < 2:
            return np.eye(3)
        t = np.degrees(np.arctan2(dy[horz], dx[horz]))
        t = np.where(t > 90, t - 180, np.where(t < -90, t + 180, t))
        a = math.radians(-_weighted_median(t, ln[horz]))
        if abs(math.degrees(a)) > 12:
            return np.eye(3)
        return np.array([[math.cos(a), -math.sin(a), 0], [math.sin(a), math.cos(a), 0], [0, 0, 1]])

    def vertical_fix(strength):
        if vert.sum() < need:
            return np.eye(3)
        V = _vanishing_point(seg[vert], ln[vert], cx, cy, sc)
        if abs(V[2]) < 1e-9 * (abs(V[0]) + abs(V[1])):
            px, py, inf = V[0], V[1], True                 # already parallel: just rotate upright
        else:
            px, py, inf = V[0] / V[2], V[1] / V[2], False
        phi = math.atan2(px, py)                           # rotate the vanishing point onto the y axis
        if phi > math.pi / 2:
            phi -= math.pi
        elif phi < -math.pi / 2:
            phi += math.pi
        if abs(math.degrees(phi)) > 15:
            return np.eye(3)
        R = np.array([[math.cos(phi), -math.sin(phi), 0], [math.sin(phi), math.cos(phi), 0], [0, 0, 1]])
        if inf:
            return R
        d = math.sin(phi) * px + math.cos(phi) * py        # its distance along the y axis
        if abs(d) < 1.2:                                   # absurdly strong: leave it
            return np.eye(3)
        P = np.array([[1, 0, 0], [0, 1, 0], [0, -strength / d, 1]])
        return P @ R

    def horizontal_fix(H0):
        if horz.sum() < need:
            return np.eye(3)
        hs = seg[horz].copy()
        for k in (0, 2):                                   # horizontal segments after the vertical fix
            v = H0 @ np.c_[(hs[:, k] - cx) / sc, (hs[:, k + 1] - cy) / sc, np.ones(len(hs))].T
            hs[:, k], hs[:, k + 1] = v[0] / v[2] * sc + cx, v[1] / v[2] * sc + cy
        V = _vanishing_point(hs, ln[horz], cx, cy, sc)
        if abs(V[2]) < 1e-9 or abs(V[0] / V[2]) < 1.2:
            return np.eye(3)
        hx, hy = V[0] / V[2], V[1] / V[2]
        P = np.array([[1, 0, 0], [0, 1, 0], [-1 / hx, 0, 1]])   # keeps verticals vertical
        S = np.array([[1, 0, 0], [-hy / hx, 1, 0], [0, 0, 1]])  # then level the now-parallel lines
        return S @ P

    if mode == "Level":
        Hc = level_rotation()
    elif mode == "Vertical":
        Hc = vertical_fix(1.0)
    elif mode == "Auto":
        Hc = vertical_fix(0.6)
        if np.allclose(Hc, np.eye(3)):
            Hc = level_rotation()
    elif mode in ("Full", "Guided"):
        Hc = vertical_fix(1.0)
        Hc = horizontal_fix(Hc) @ Hc
    # Negligible correction (measurement noise on a photo that's already straight): no warp.
    # The frame corners in centred units (-1..1) may move by at most ~0.15 % of the frame.
    cc = np.array([[-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1]], float).T * [[w / 2 / sc], [h / 2 / sc], [1]]
    q = Hc @ cc
    if np.abs(q[:2] / q[2] - cc[:2]).max() < 0.003:
        return None
    H = np.linalg.inv(C) @ Hc @ C
    # fit the warped photo back into the frame (Lightroom shows it whole, uncovered corners empty)
    corners = np.array([[0, 0, 1], [w, 0, 1], [w, h, 1], [0, h, 1]], float).T
    q = H @ corners
    q = q[:2] / q[2]
    x0, y0, x1, y1 = q[0].min(), q[1].min(), q[0].max(), q[1].max()
    k = min(w / (x1 - x0), h / (y1 - y0))
    T = np.array([[k, 0, (w - k * (x1 + x0)) / 2], [0, k, (h - k * (y1 + y0)) / 2], [0, 0, 1]])
    return T @ H


def suggest_guides(rgb: np.ndarray, kinds: str = "v", exclude=()) -> list:
    """
    Guide lines for Guided Upright, picked like you would: the longest clear verticals, one on
    each side of the photo when possible (and with "vh" two horizontals, one high and one low).
    Lightroom allows four guides. `exclude` = guides you rejected; lines along them are skipped.
    Returns [[x1, y1, x2, y2], ...] in 0..1 of the image.
    """
    h, w = rgb.shape[:2]
    seg, ln = _segments(rgb)
    if len(seg) == 0:
        return []
    n = np.c_[seg[:, 0] / w, seg[:, 1] / h, seg[:, 2] / w, seg[:, 3] / h]
    ang = np.degrees(np.arctan2(seg[:, 3] - seg[:, 1], seg[:, 2] - seg[:, 0])) % 180.0

    def near(a, b):                                   # same line? (angle and position)
        da = abs(math.degrees(math.atan2(a[3] - a[1], a[2] - a[0]) - math.atan2(b[3] - b[1], b[2] - b[0])))
        da = min(da % 180, 180 - da % 180)
        ma, mb = ((a[0] + a[2]) / 2, (a[1] + a[3]) / 2), ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
        # distance of b's midpoint from a's line
        dx, dy = a[2] - a[0], a[3] - a[1]
        d = abs(dy * (mb[0] - a[0]) - dx * (mb[1] - a[1])) / (math.hypot(dx, dy) + 1e-9)
        return da < 4 and d < 0.03

    def pick(mask, axis, count):
        idx = [i for i in np.argsort(-ln) if mask[i] and not any(near(n[i], x) for x in exclude)]
        chosen = []
        halves = [lambda c: c < 0.5, lambda c: c >= 0.5]
        for half in halves:                            # best line in each half first
            for i in idx:
                c = (n[i][axis] + n[i][axis + 2]) / 2
                if half(c) and not any(near(n[i], n[j]) for j in chosen):
                    chosen.append(i)
                    break
        for i in idx:                                  # then the next longest, if still short
            if len(chosen) >= count:
                break
            if i not in chosen and not any(near(n[i], n[j]) for j in chosen):
                chosen.append(i)
        return chosen[:count]

    cx, cy, sc = w / 2.0, h / 2.0, max(w, h) / 2.0

    def consistent(mask):
        """Lines in `mask` that agree on one vanishing point (a stray diagonal disagrees), and that point."""
        idx = np.where(mask)[0]
        if len(idx) < 2:
            return mask & False, None
        V = _vanishing_point(seg[idx], ln[idx] ** 2, cx, cy, sc)
        keep = np.zeros(len(seg), bool)
        for i in idx:
            mx, my = ((seg[i, 0] + seg[i, 2]) / 2 - cx) / sc, ((seg[i, 1] + seg[i, 3]) / 2 - cy) / sc
            if abs(V[2]) > 1e-9:
                tx, ty = V[0] / V[2] - mx, V[1] / V[2] - my
            else:
                tx, ty = V[0], V[1]
            dx, dy = seg[i, 2] - seg[i, 0], seg[i, 3] - seg[i, 1]
            c = abs(tx * dx + ty * dy) / (math.hypot(tx, ty) * math.hypot(dx, dy) + 1e-12)
            keep[i] = math.degrees(math.acos(min(1.0, c))) < 1.0
        return keep, V

    out = []
    vmask, _V = consistent((np.abs(ang - 90) <= 12) & (ln >= 0.12 * h))
    out += pick(vmask, 0, 2 if "h" in kinds else 3)
    if "h" in kinds:
        hmask, Vh = consistent(((ang <= 8) | (ang >= 172)) & (ln >= 0.12 * w))
        hp = pick(hmask, 1, 2)
        # only real "facade" horizontals: their vanishing point must be far away (a gentle correction);
        # edges receding into the distance (shelves down an aisle) would twist the photo instead
        if len(hp) == 2 and (Vh is None or abs(Vh[2]) < 1e-9 or abs(Vh[0] / Vh[2]) > 3.0):
            out += hp
    return [[round(float(v), 6) for v in n[i]] for i in out][:4]


def upright_all(rgb: np.ndarray, guides=None) -> dict:
    """Every Upright mode's correction for this photo (0..1 units, 9 numbers each, None = nothing to fix)."""
    h, w = rgb.shape[:2]
    N = np.array([[w, 0, 0], [0, h, 0], [0, 0, 1]], float)
    seg = _segments(rgb)
    out = {}
    for mode in ("Auto", "Full", "Level", "Vertical", "Guided"):
        H = upright_matrix(rgb, mode, guides, seg_cache=seg)
        out[mode] = None if H is None else [float(v) for v in (np.linalg.inv(N) @ H @ N).ravel()]
    return out


def constrained_box(Hn, w: int, h: int, margin: float = 0.004):
    """
    Lightroom's Constrain Crop: the largest crop at the photo's aspect ratio that has photo
    everywhere (no empty corners) after the Upright warp `Hn` (0..1 units, 9 numbers or 3x3).
    Returns [l, t, r, b] in 0..1, or None when no crop is needed.
    """
    if Hn is None:
        return None
    Hn = np.asarray(Hn, float).reshape(3, 3)
    q = Hn @ np.array([[0, 1, 1, 0], [0, 0, 1, 1], [1, 1, 1, 1]], float)
    quad = np.c_[q[0] / q[2] * w, q[1] / q[2] * h]          # the warped photo's corners, in pixels
    e1, e2 = quad[1] - quad[0], quad[2] - quad[1]
    sign = np.sign(e1[0] * e2[1] - e1[1] * e2[0])          # winding of the warped corners

    def inside(pts):
        for k in range(4):
            a, b = quad[k], quad[(k + 1) % 4]
            if np.any(sign * ((b[0] - a[0]) * (pts[:, 1] - a[1]) - (b[1] - a[1]) * (pts[:, 0] - a[0])) < -1e-9):
                return False
        return True

    best = None
    for fy in np.linspace(0.3, 0.7, 9):
        for fx in np.linspace(0.3, 0.7, 9):
            cx, cy = fx * w, fy * h
            lo, hi = 0.0, 1.0
            for _ in range(22):
                s = (lo + hi) / 2
                hw_, hh_ = s * w / 2, s * h / 2
                pts = np.array([[cx - hw_, cy - hh_], [cx + hw_, cy - hh_], [cx + hw_, cy + hh_], [cx - hw_, cy + hh_]])
                lo, hi = (s, hi) if inside(pts) else (lo, s)
            score = (round(lo, 4), -abs(fx - 0.5) - abs(fy - 0.5))   # biggest, then most central
            if best is None or score > best[0]:
                best = (score, cx, cy, lo)
    _sc, cx, cy, s = best
    if s >= 0.999:
        return None
    s *= 1 - margin
    hw_, hh_ = s / 2, s / 2
    return [round(float(cx / w - hw_), 5), round(float(cy / h - hh_), 5),
            round(float(cx / w + hw_), 5), round(float(cy / h + hh_), 5)]


def shown_to_recorded(flip: int):
    """3x3 map from 0..1 coordinates of the photo as shown to the photo as the camera recorded it
    (before turning portrait shots upright). Lightroom's crop and Upright use the recorded frame."""
    if flip == 6:      # shown = recorded turned 90° clockwise: u = 1 - t, v = s  ->  s = v, t = 1 - u
        return np.array([[0, 1, 0], [-1, 0, 1], [0, 0, 1]], float)
    if flip == 5:      # 90° counter-clockwise: u = t, v = 1 - s  ->  s = 1 - v, t = u
        return np.array([[0, -1, 1], [1, 0, 0], [0, 0, 1]], float)
    if flip == 3:      # 180°
        return np.array([[-1, 0, 1], [0, -1, 1], [0, 0, 1]], float)
    return np.eye(3)


def upright_for(dec, mode: str, rgb: np.ndarray):
    """Upright warp for `dec`, cached on it in 0..1 units so the half-size preview can reuse it."""
    key = "_upright:" + mode
    if key not in dec.masks:
        h, w = rgb.shape[:2]
        H = upright_matrix(rgb, mode)
        N = np.array([[w, 0, 0], [0, h, 0], [0, 0, 1]], float)
        dec.masks[key] = None if H is None else np.linalg.inv(N) @ H @ N
    Hn = dec.masks[key]
    if Hn is None:
        return None
    h, w = rgb.shape[:2]
    N = np.array([[w, 0, 0], [0, h, 0], [0, 0, 1]], float)
    return N @ Hn @ np.linalg.inv(N)


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


def auto_straighten(rgb: np.ndarray, allow_upright: bool = True,
                    source: str = "auto") -> StraightenResult:
    """
    Detect straight lines; near-vertical ones (buildings, door frames, poles)
    are trusted most, near-horizontal ones (horizons) are the fallback.
    If verticals converge strongly (camera tilted up at a building), suggest
    Lightroom's Upright 'Vertical' instead of a plain rotation.

    source = "auto"      best guess from both kinds of line (default)
             "horizon"   use ONLY near-horizontal lines (horizon, sea, shelves)
             "vertical"  use ONLY near-vertical lines (walls, poles, door frames)
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
    if source == "auto" and allow_upright and len(verts) >= 4:
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

    if source == "vertical":
        cands = [(v[0], v[1]) for v in verts]
        src = "vertical"
    elif source == "horizon":
        cands = [(-h_[0], h_[1]) for h_ in horiz]
        src = "horizontal"
    elif len(verts) >= 2 and vw >= 0.35 * (vw + hw):
        cands = [(v[0], v[1]) for v in verts]
        src = "vertical"
    else:
        cands = [(-h_[0], h_[1]) for h_ in horiz]
        src = "horizontal"
    if len(cands) < (2 if source != "auto" else 1):
        what = {"vertical": "vertical", "horizon": "horizon-like"}.get(source, "")
        return StraightenResult(0.0, "Off", 0.0, f"Straighten: not enough {what} lines".replace("  ", " "))

    vals = np.array([c[0] for c in cands])
    wts = np.array([c[1] for c in cands])
    angle = _weighted_median(vals, wts)
    conf = float(wts[np.abs(vals - angle) <= 1.0].sum() / wts.sum())

    if abs(angle) > (12 if source == "auto" else 15):
        return StraightenResult(0.0, "Off", conf,
                                f"Straighten: {angle:.1f}° looks intentional, left alone")
    if abs(angle) < 0.15 or conf < 0.25:
        return StraightenResult(0.0, "Off", conf, "Straighten: already level")
    angle = round(angle, 2)
    return StraightenResult(angle, "Off", conf,
                            f"Straighten: {angle:+.2f}° from {len(cands)} {src} lines "
                            f"(confidence {conf:.0%})")


# --------------------------------------------------------------------------
# XMP sidecars (the ONLY thing this program writes)
# --------------------------------------------------------------------------

NS = {
    "x": "adobe:ns:meta/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "crs": "http://ns.adobe.com/camera-raw-settings/1.0/",
    "rrv": "http://ns.rawreview.local/1.0/",
    "xmp": "http://ns.adobe.com/xap/1.0/",
}
LABELS = ["Red", "Yellow", "Green", "Blue", "Purple"]      # Lightroom's colour label names
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
    t = edit.touched
    if "whites" in t:
        v["Whites2012"] = f"{int(round(edit.whites)):+d}"
    if "blacks" in t:
        v["Blacks2012"] = f"{int(round(edit.blacks)):+d}"
    if "lights" in t:
        v["ParametricLights"] = f"{int(round(edit.lights)):+d}"
    if "saturation" in t:
        v["Saturation"] = f"{int(round(edit.saturation)):+d}"
    if t & {"sharp_amount", "sharp_radius", "sharp_detail", "sharp_masking"}:
        v["Sharpness"] = str(int(round(edit.sharp_amount)))
        v["SharpenRadius"] = f"{edit.sharp_radius:+.1f}"
        v["SharpenDetail"] = str(int(round(edit.sharp_detail)))
        v["SharpenEdgeMasking"] = str(int(round(edit.sharp_masking)))
    if t & {"nr_luma", "nr_luma_detail", "nr_color"}:
        v["LuminanceSmoothing"] = str(int(round(edit.nr_luma)))
        v["LuminanceNoiseReductionDetail"] = str(int(round(edit.nr_luma_detail)))
        v["ColorNoiseReduction"] = str(int(round(edit.nr_color)))
    remove = []
    if edit.wb_mode == "custom":
        v["WhiteBalance"] = "Custom"
        v["Temperature"] = str(int(round(edit.temp)))
        v["Tint"] = f"{int(round(edit.tint)):+d}"
    else:
        v["WhiteBalance"] = "As Shot"
        remove += ["Temperature", "Tint"]
    return v, remove


def _turn_crop(vals: dict, flip: int) -> dict:
    """Lightroom keeps the crop on the photo as recorded (before a portrait shot is turned upright)."""
    if not flip or "CropLeft" not in vals:
        return vals
    F = shown_to_recorded(flip)
    pts = np.array([[float(vals["CropLeft"]), float(vals["CropTop"]), 1.0],
                    [float(vals["CropRight"]), float(vals["CropBottom"]), 1.0]]).T
    q = F @ pts
    out = dict(vals)
    out.update({"CropLeft": f"{q[0].min():.6f}", "CropTop": f"{q[1].min():.6f}",
                "CropRight": f"{q[0].max():.6f}", "CropBottom": f"{q[1].max():.6f}"})
    return out


UPRIGHT_ORDER = ["Off", "Auto", "Full", "Level", "Vertical", "Guided"]     # UprightTransform_0 .. _5
UPRIGHT_VERSION = "151388160"
_UPRIGHT_KEYS = ([f"UprightTransform_{i}" for i in range(6)] + ["UprightTransformCount", "UprightVersion",
                 "UprightFourSegmentsCount", "UprightGuidedDependentDigest", "CropConstrainToWarp"]
                 + [f"UprightFourSegments_{i}" for i in range(4)])


def _write_upright(desc, edit: Edit, flip: int = 0):
    """
    Give Lightroom the Upright correction RAW Review solved, so it shows exactly the preview:
    UprightTransform_0.._5 (one per mode, row-major forward matrix in 0..1 of the recorded photo;
    Camera Raw renders the stored matrix), Guided's guides as UprightFourSegments_n "x1,y1,x2,y2",
    and Constrain Crop. Field formats as documented by the RAWmakase project from Camera Raw 18.7.
    """
    marker = _q("rrv", "UprightByRawReview")
    mats = edit.upright_matrices
    if edit.upright != "Off" and mats:
        F = shown_to_recorded(flip)
        Fi = np.linalg.inv(F)
        for i, mode in enumerate(UPRIGHT_ORDER):
            m = mats.get(mode) if mode != "Off" else None
            M = np.eye(3) if m is None else np.asarray(m, float).reshape(3, 3)
            R = F @ M @ Fi
            R = R / R[2, 2]
            _set(desc, _q("crs", f"UprightTransform_{i}"), ",".join(f"{v:.9f}" for v in R.ravel()))
        _set(desc, _q("crs", "UprightTransformCount"), "6")
        _set(desc, _q("crs", "UprightVersion"), UPRIGHT_VERSION)
        for i in range(4):
            _remove(desc, _q("crs", f"UprightFourSegments_{i}"))
        _remove(desc, _q("crs", "UprightGuidedDependentDigest"))   # Lightroom's record of other guides
        guides = edit.upright_guides if edit.upright == "Guided" else None
        if guides:
            _set(desc, _q("crs", "UprightFourSegmentsCount"), str(len(guides[:4])))
            for i, g in enumerate(guides[:4]):
                q = F @ np.array([[g[0], g[2]], [g[1], g[3]], [1.0, 1.0]])
                _set(desc, _q("crs", f"UprightFourSegments_{i}"),
                     ",".join(f"{v:.9f}" for v in (q[0, 0], q[1, 0], q[0, 1], q[1, 1])))
        else:
            _remove(desc, _q("crs", "UprightFourSegmentsCount"))
        _set(desc, _q("crs", "CropConstrainToWarp"), "1" if edit.constrain_crop else "0")
        desc.set(marker, "True")
    elif desc.get(marker) is not None:              # we wrote these before; Upright is off now
        for k in _UPRIGHT_KEYS:
            _remove(desc, _q("crs", k))
        desc.attrib.pop(marker, None)


def _crop_values(edit: Edit, src_w: float, src_h: float):
    """Lightroom crop: the straighten crop, narrowed to the subject crop box when one is set."""
    k = crop_factor(src_w, src_h, edit.angle)
    box = edit.crop_box or [0.0, 0.0, 1.0, 1.0]
    u, v = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0     # centre in the straightened frame
    # straightened frame -> rotated full frame (pixels, relative to the image centre)
    px, py = (u - 0.5) * k * src_w, (v - 0.5) * k * src_h
    # ... -> unrotated source (undo the preview's counter-clockwise rotation)
    a = math.radians(edit.angle)
    sx = math.cos(a) * px - math.sin(a) * py
    sy = math.sin(a) * px + math.cos(a) * py
    cx, cy = 0.5 + sx / src_w, 0.5 + sy / src_h
    hw, hh = k * (box[2] - box[0]) / 2.0, k * (box[3] - box[1]) / 2.0
    return {
        "HasCrop": "True",
        "CropAngle": f"{XMP_ANGLE_SIGN * edit.angle:.2f}",
        "CropLeft": f"{cx - hw:.6f}", "CropTop": f"{cy - hh:.6f}",
        "CropRight": f"{cx + hw:.6f}", "CropBottom": f"{cy + hh:.6f}",
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


def _mask_correction_values(edit: Edit, kind: str) -> dict:
    """Local adjustment values in Lightroom's XMP units (sliders / 100, exposure in EV)."""
    if kind == "sky":
        v = {"LocalExposure2012": edit.sky_exposure / LOCAL_EXPOSURE_SCALE,
             "LocalContrast2012": edit.sky_contrast / 100, "LocalHighlights2012": edit.sky_highlights / 100,
             "LocalDehaze": edit.sky_dehaze / 100, "LocalSaturation": edit.sky_saturation / 100,
             "LocalTemperature": edit.sky_temp / 100}
    else:
        v = {"LocalExposure2012": edit.subj_exposure / LOCAL_EXPOSURE_SCALE,
             "LocalContrast2012": edit.subj_contrast / 100, "LocalShadows2012": edit.subj_shadows / 100,
             "LocalTexture": edit.subj_texture / 100, "LocalClarity2012": edit.subj_clarity / 100,
             "LocalSaturation": edit.subj_saturation / 100}
    return {k: f"{val:.6f}" for k, val in v.items()}


def _sync_id(raw_path: str, kind: str, part: str) -> str:
    # Stable per photo + mask, so re-approving replaces the same mask instead of piling up new ones.
    return uuid.uuid5(uuid.NAMESPACE_URL, f"rawreview:{os.path.basename(raw_path)}:{kind}:{part}").hex.upper()


def _is_our_correction(li) -> bool:
    for node in [li] + list(li):
        name = node.get(_q("crs", "CorrectionName"))
        if name is None:
            el = node.find(_q("crs", "CorrectionName"))
            name = el.text if el is not None else None
        if (name or "").startswith("RAW Review:") or node.get(_q("rrv", "Mask")) is not None:
            return True
    return False


def _write_masks(desc, edit: Edit, raw_path: str):
    """
    Replace only RAW Review's own Sky / Subject corrections inside crs:MaskGroupBasedCorrections.
    Masks you made in Lightroom are kept as they are.
    """
    key = _q("crs", "MaskGroupBasedCorrections")
    group = desc.find(key)
    wanted = [] if NO_AI_MASKS else [k for k, on in (("sky", edit.sky_on), ("subject", edit.subj_on)) if on]
    if group is None and not wanted:
        return
    if group is None:
        group = ET.SubElement(desc, key)
    seq = group.find(_q("rdf", "Seq"))
    if seq is None:
        seq = ET.SubElement(group, _q("rdf", "Seq"))
    for li in list(seq):
        if _is_our_correction(li):
            seq.remove(li)
    for kind in wanted:
        li = ET.SubElement(seq, _q("rdf", "li"))
        attrs = {_q("crs", "What"): "Correction", _q("crs", "CorrectionAmount"): "1.000000",
                 _q("crs", "CorrectionActive"): "true", _q("crs", "CorrectionName"): MASK_NAME[kind],
                 _q("crs", "CorrectionSyncID"): _sync_id(raw_path, kind, "correction"),
                 _q("rrv", "Mask"): kind}
        attrs.update({_q("crs", k): v for k, v in _mask_correction_values(edit, kind).items()})
        d = ET.SubElement(li, _q("rdf", "Description"), attrs)
        cm = ET.SubElement(d, _q("crs", "CorrectionMasks"))
        s2 = ET.SubElement(cm, _q("rdf", "Seq"))
        ET.SubElement(s2, _q("rdf", "li"), {
            _q("crs", "What"): "Mask/Image", _q("crs", "MaskActive"): "true",
            _q("crs", "MaskName"): ("Sky 1" if kind == "sky" else "Subject 1"),
            _q("crs", "MaskBlendMode"): "0", _q("crs", "MaskInverted"): "false",
            _q("crs", "MaskSyncID"): _sync_id(raw_path, kind, "mask"),
            _q("crs", "MaskValue"): "1.000000", _q("crs", "MaskVersion"): "1",
            _q("crs", "MaskSubType"): MASK_SUBTYPE[kind],
            _q("crs", "ReferencePoint"): "0.500000 0.500000", _q("crs", "ErrorReason"): "0"})
    if len(seq) == 0:
        desc.remove(group)


def _write_meta(desc, meta):
    """Star rating / colour label. None = leave whatever the file has."""
    if not meta:
        return
    r = meta.get("rating")
    if r is not None:
        _set(desc, _q("xmp", "Rating"), str(int(r)))
    lab = meta.get("label")
    if lab is not None:
        if lab:
            _set(desc, _q("xmp", "Label"), lab)
        else:
            _remove(desc, _q("xmp", "Label"))


def _write_sidecar(raw_path: str, edit: Edit, src_size=(3, 2), meta=None, flip: int = 0) -> str:
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
    _write_masks(desc, edit, raw_path)
    _write_meta(desc, meta)

    marker = _q("rrv", "CropByRawReview")
    box_key = _q("rrv", "SubjectCrop")
    keep_lr_crop = (edit.lr_crop_angle is not None and abs(edit.angle - edit.lr_crop_angle) < 0.01
                    and not edit.crop_box and desc.get(marker) is None)
    if keep_lr_crop:
        pass                                  # the crop you made in Lightroom stays as it is
    elif abs(edit.angle) >= 0.01 or edit.crop_box:
        for k, val in _turn_crop(_crop_values(edit, *src_size), flip).items():
            _set(desc, _q("crs", k), val)
        desc.set(marker, "True")
        if edit.crop_box:
            desc.set(box_key, ",".join(f"{float(v):.5f}" for v in edit.crop_box))
        else:
            desc.attrib.pop(box_key, None)
    elif desc.get(marker) is not None:
        desc.attrib.pop(box_key, None)
        # We set a crop earlier and the angle is now 0: undo only our own crop.
        for k, val in {"HasCrop": "False", "CropAngle": "0", "CropLeft": "0",
                       "CropTop": "0", "CropRight": "1", "CropBottom": "1"}.items():
            _set(desc, _q("crs", k), val)
        desc.attrib.pop(marker, None)

    _write_upright(desc, edit, flip)

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


# --------------------------------------------------------------------------
# DNG: settings go INSIDE the DNG (Lightroom ignores sidecars for DNG)
# --------------------------------------------------------------------------

_XMP_TAG = 700


def _tiff_layout(data: bytes):
    if data[:2] == b"II":
        e = "<"
    elif data[:2] == b"MM":
        e = ">"
    else:
        raise RawSafetyError("This DNG isn't a TIFF-based file, so it can't be updated safely.")
    if struct.unpack(e + "H", data[2:4])[0] != 42:
        raise RawSafetyError("This DNG uses the BigTIFF layout, which isn't supported. Nothing was changed.")
    ifd0 = struct.unpack(e + "I", data[4:8])[0]
    n = struct.unpack(e + "H", data[ifd0:ifd0 + 2])[0]
    return e, ifd0, n


def _extract_tiff_xmp(data: bytes):
    """The XMP packet stored in IFD0 (tag 700), or None."""
    e, ifd0, n = _tiff_layout(data)
    for i in range(n):
        pos = ifd0 + 2 + 12 * i
        tag, _typ, count, off = struct.unpack(e + "HHII", data[pos:pos + 12])
        if tag == _XMP_TAG:
            return bytes(data[off:off + count]).rstrip(b"\x00 \r\n\t") if count > 4 else None
    return None


def _embed_xmp_in_tiff(data: bytes, xmp: bytes) -> bytes:
    """
    Return a copy of the TIFF/DNG with `xmp` as its XMP packet. The image data is not
    touched: the new packet is appended at the end and only the 12-byte directory entry
    (or, if there was none, a copy of the directory) is changed.
    """
    e, ifd0, n = _tiff_layout(data)
    out = bytearray(data)
    if len(out) % 2:
        out.append(0)
    xmp_off = len(out)
    out += xmp
    if len(out) % 2:
        out.append(0)
    entry = struct.pack(e + "HHII", _XMP_TAG, 1, len(xmp), xmp_off)
    base = ifd0 + 2
    for i in range(n):
        pos = base + 12 * i
        if struct.unpack(e + "H", data[pos:pos + 2])[0] == _XMP_TAG:
            out[pos:pos + 12] = entry
            return bytes(out)
    entries = [bytes(data[base + 12 * i: base + 12 * i + 12]) for i in range(n)] + [entry]
    entries.sort(key=lambda b: struct.unpack(e + "H", b[:2])[0])
    next_ifd = bytes(data[base + 12 * n: base + 12 * n + 4])
    new_ifd = len(out)
    out += struct.pack(e + "H", n + 1) + b"".join(entries) + next_ifd
    out[4:8] = struct.pack(e + "I", new_ifd)
    return bytes(out)


def _write_dng(dng_path: str, edit: Edit, src_size=(3, 2), out_dir: str | None = None, meta=None,
               flip: int = 0) -> str:
    """
    Export an edited COPY of the DNG for Lightroom: <out_dir>/<name>.dng, with the develop
    settings embedded in its XMP packet. The source DNG is only read, never changed, and
    no .original / backup files are made. The output name is built here from the source's
    base name plus a plain ".dng" extension; nothing is inherited from existing files.
    Default out_dir is a "RAW Review export" folder next to the source.
    """
    src = os.path.abspath(dng_path)
    if not os.path.isfile(src):
        raise FileNotFoundError(dng_path)
    before = raw_fingerprint(src)
    stem = os.path.splitext(os.path.basename(src))[0]
    out_dir = os.path.abspath(out_dir or os.path.join(os.path.dirname(src), DNG_EXPORT_DIR))
    target = os.path.join(out_dir, stem + ".dng")
    if os.path.normcase(target) == os.path.normcase(src):
        raise RawSafetyError("The export folder is the source folder; refusing to overwrite the original DNG.")
    os.makedirs(out_dir, exist_ok=True)

    with open(src, "rb") as f:
        data = f.read()
    existing = _extract_tiff_xmp(data)         # only used to keep ratings / keywords; unreadable XMP is replaced
    if os.path.isfile(target):
        # An earlier export exists (maybe edited in Lightroom since): build on ITS settings, so edits
        # made there (masks, other sliders, ratings) aren't thrown away by re-approving.
        try:
            with open(target, "rb") as f:
                prev = _extract_tiff_xmp(f.read())
            if prev:
                existing = prev
        except (OSError, RawSafetyError):
            pass

    with tempfile.TemporaryDirectory(prefix="rawreview-") as td:
        fake = os.path.join(td, "x.dng")                 # lets the normal sidecar merge logic run
        open(fake, "wb").close()
        side = os.path.join(td, "x.xmp")
        if existing:
            with open(side, "wb") as f:
                f.write(existing)
        _write_sidecar(fake, edit, src_size, meta, flip)
        with open(side, "rb") as f:
            body = f.read().decode("utf-8")
    packet = ('<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>\n' + body
              + '\n<?xpacket end="w"?>').encode("utf-8")
    new = _embed_xmp_in_tiff(data, packet)
    if _extract_tiff_xmp(new) != packet.rstrip(b"\x00 \r\n\t"):
        raise RawSafetyError("Couldn't verify the new settings inside the DNG. Nothing was exported.")

    fd, tmp = tempfile.mkstemp(prefix=".rawreview-", suffix=".dng.tmp", dir=out_dir)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(new)
        try:                                              # the result must still open as a raw file
            import rawpy
            with rawpy.imread(tmp):
                pass
        except ImportError:
            pass
        except Exception as ex:  # noqa: BLE001
            raise RawSafetyError(f"The exported DNG didn't open cleanly ({ex}). Nothing was exported.")
        last = None
        for _ in range(5):                                # Windows: Lightroom/Explorer may briefly lock the target
            try:
                os.replace(tmp, target)
                last = None
                break
            except PermissionError as ex:
                last = ex
                time.sleep(0.4)
        if last is not None:
            raise RawSafetyError(f"Couldn't write {target} (is it open in another program?): {last}")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)                                # only ever our own .dng.tmp file
    if raw_fingerprint(src) != before:
        raise RawSafetyError(f"{src} changed during export - stopping.")
    return f"Exported {stem}.dng to {out_dir} (import this folder into Lightroom)"


def write_xmp(raw_path: str, edit: Edit, src_size=(3, 2), out_dir: str | None = None, meta=None,
              flip: int = 0) -> str:
    """Write the Lightroom settings: a .xmp sidecar for RAW files, an exported edited .dng copy for DNG.
    flip = the photo's orientation (Decoded.flip): Lightroom's crop and Upright are on the recorded frame."""
    if os.path.splitext(raw_path)[1].lower() == ".dng":
        return _write_dng(raw_path, edit, src_size, out_dir, meta, flip)
    return _write_sidecar(raw_path, edit, src_size, meta, flip)


def is_raw(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in RAW_EXTS


# --------------------------------------------------------------------------
# Reading the settings a photo already has (Lightroom sidecar / DNG)
# --------------------------------------------------------------------------

def dng_export_path(dng_path: str) -> str:
    stem = os.path.splitext(os.path.basename(dng_path))[0]
    return os.path.join(os.path.dirname(os.path.abspath(dng_path)), DNG_EXPORT_DIR, stem + ".dng")


def _existing_xmp_bytes(raw_path: str):
    """The XMP that Lightroom would use for this photo, or None. Read-only."""
    try:
        if raw_path.lower().endswith(".dng"):
            for p in (dng_export_path(raw_path), raw_path):     # the export is what Lightroom has
                if os.path.isfile(p):
                    with open(p, "rb") as f:
                        x = _extract_tiff_xmp(f.read())
                    if x and b"camera-raw-settings" in x:
                        return x
            return None
        side = sidecar_path(raw_path)
        if os.path.isfile(side):
            with open(side, "rb") as f:
                return f.read()
    except (OSError, RawSafetyError):
        return None
    return None


def _crs_props(root):
    """All simple crs:/xmp: properties (attributes or child elements) in every rdf:Description."""
    props = {}
    for d in root.iter(_q("rdf", "Description")):
        for k, v in d.attrib.items():
            props.setdefault(k, v)
        for child in d:
            if len(child) == 0 and child.text is not None and child.tag not in props:
                props[child.tag] = child.text.strip()
    return props


def read_existing(raw_path: str, base: Edit):
    """
    Settings already saved for this photo (by Lightroom or by RAW Review), applied onto a copy of
    `base` (normally the As Shot edit). Returns {"edit": Edit or None, "rating": int|None,
    "label": str|None}. "edit" is None when the file has no develop settings. Never writes anything.
    """
    out = {"edit": None, "rating": None, "label": None}
    data = _existing_xmp_bytes(raw_path)
    if not data:
        return out
    try:
        text = data.decode("utf-8", "replace")
        i = text.find("<x:xmpmeta")
        j = text.rfind("</x:xmpmeta>")
        root = ET.fromstring(text[i:j + 12] if i >= 0 and j > i else text)
    except ET.ParseError:
        return out
    P = _crs_props(root)

    def g(name, ns="crs"):
        return P.get(_q(ns, name))

    try:
        out["rating"] = int(float(g("Rating", "xmp"))) if g("Rating", "xmp") is not None else None
    except ValueError:
        pass
    lab = g("Label", "xmp")
    out["label"] = lab if lab else None

    main = ("Exposure2012", "Contrast2012", "Highlights2012", "Shadows2012", "WhiteBalance", "Temperature")
    if g("HasSettings") is None and not any(g(k) is not None for k in main):
        return out

    e = base.copy()
    e.notes = []

    def num(name, default=None):
        v = g(name)
        try:
            return float(v) if v is not None else default
        except ValueError:
            return default

    simple = {"exposure": "Exposure2012", "contrast": "Contrast2012", "highlights": "Highlights2012",
              "shadows": "Shadows2012", "vibrance": "Vibrance", "whites": "Whites2012", "blacks": "Blacks2012",
              "lights": "ParametricLights", "saturation": "Saturation", "sharp_amount": "Sharpness", "sharp_radius": "SharpenRadius",
              "sharp_detail": "SharpenDetail", "sharp_masking": "SharpenEdgeMasking",
              "nr_luma": "LuminanceSmoothing", "nr_luma_detail": "LuminanceNoiseReductionDetail",
              "nr_color": "ColorNoiseReduction"}
    for key, name in simple.items():
        v = num(name)
        if v is None:
            continue
        st = SETTINGS[key]
        v = min(max(v, st.lo), st.hi)
        setattr(e, key, int(round(v)) if st.integer else round(v, st.decimals))
        if st.optional:
            e.touched.add(key)

    wb = g("WhiteBalance")
    t, ti = num("Temperature"), num("Tint")
    if wb == "As Shot" or (wb is None and t is None):
        e.wb_mode = "as_shot"
    elif t is not None:
        e.wb_mode = "custom"
        e.temp = min(max(t, SETTINGS["temp"].lo), SETTINGS["temp"].hi)
        e.tint = min(max(ti or 0.0, SETTINGS["tint"].lo), SETTINGS["tint"].hi)

    lp = g("LensProfileEnable")
    if lp is not None:
        e.lens_profile = lp in ("1", "True", "true")
    up = num("PerspectiveUpright")
    if up is not None:
        names = {v: k for k, v in UPRIGHT_MODES.items()}
        e.upright = names.get(int(up), "Off")

    if g("HasCrop") in ("True", "true"):
        ca = num("CropAngle", 0.0)
        ang = round(min(max(ca / XMP_ANGLE_SIGN, SETTINGS["angle"].lo), SETTINGS["angle"].hi), 2)
        e.angle = ang
        ours = any(k == _q("rrv", "CropByRawReview") for d in root.iter(_q("rdf", "Description")) for k in d.attrib)
        e.lr_crop_angle = None if ours else ang
        e.crop_box = None
        if ours:
            for d in root.iter(_q("rdf", "Description")):
                raw_box = d.get(_q("rrv", "SubjectCrop"))
                if raw_box:
                    try:
                        vals = [float(x) for x in raw_box.split(",")]
                        if len(vals) == 4 and 0 <= vals[0] < vals[2] <= 1 and 0 <= vals[1] < vals[3] <= 1:
                            e.crop_box = vals
                    except ValueError:
                        pass
    else:
        e.angle = 0.0
        e.crop_box = None
    e.level_mode = "Manual" if (abs(e.angle) >= 0.01 or e.upright != "Off") else "Off"

    # RAW Review's own sky / subject masks (other masks stay in the file untouched)
    e.sky_on = e.subj_on = False
    grp = None
    for d in root.iter(_q("rdf", "Description")):
        grp = d.find(_q("crs", "MaskGroupBasedCorrections"))
        if grp is not None:
            break
    if grp is not None:
        for li in grp.iter(_q("rdf", "li")):
            for node in [li] + list(li):
                name = node.get(_q("crs", "CorrectionName")) or ""
                kind = "sky" if name == MASK_NAME["sky"] else "subject" if name == MASK_NAME["subject"] else None
                if not kind:
                    continue

                def lv(n, scale=100.0):
                    try:
                        return float(node.get(_q("crs", n), "0")) * scale
                    except ValueError:
                        return 0.0
                if kind == "sky":
                    e.sky_on = True
                    e.sky_exposure = round(lv("LocalExposure2012", LOCAL_EXPOSURE_SCALE), 2)
                    e.sky_contrast, e.sky_highlights = round(lv("LocalContrast2012")), round(lv("LocalHighlights2012"))
                    e.sky_dehaze, e.sky_saturation = round(lv("LocalDehaze")), round(lv("LocalSaturation"))
                    e.sky_temp = round(lv("LocalTemperature"))
                else:
                    e.subj_on = True
                    e.subj_exposure = round(lv("LocalExposure2012", LOCAL_EXPOSURE_SCALE), 2)
                    e.subj_contrast, e.subj_shadows = round(lv("LocalContrast2012")), round(lv("LocalShadows2012"))
                    e.subj_texture, e.subj_clarity = round(lv("LocalTexture")), round(lv("LocalClarity2012"))
                    e.subj_saturation = round(lv("LocalSaturation"))
    e.notes = ["Started from the settings already saved for this photo (Lightroom)"]
    out["edit"] = e
    return out


def edit_to_dict(e: Edit) -> dict:
    d = {f: getattr(e, f) for f in e.__dataclass_fields__}
    d["touched"] = sorted(e.touched)
    d["notes"] = list(e.notes)
    return d


def edit_from_dict(d: dict) -> Edit:
    e = Edit()
    for k, v in (d or {}).items():
        if k in e.__dataclass_fields__:
            setattr(e, k, set(v) if k == "touched" else v)
    return e


# --------------------------------------------------------------------------
# Undo an approval: put back the sidecar / DNG export exactly as it was
# --------------------------------------------------------------------------

def output_path(raw_path: str) -> str:
    """The file RAW Review writes for this photo (never the photo itself)."""
    return dng_export_path(raw_path) if raw_path.lower().endswith(".dng") else sidecar_path(raw_path)


def _check_output(raw_path: str, target: str):
    t, r = os.path.normcase(os.path.abspath(target)), os.path.normcase(os.path.abspath(raw_path))
    if t == r or t != os.path.normcase(os.path.abspath(output_path(raw_path))):
        raise RawSafetyError(f"Refusing to touch {target}: it isn't RAW Review's own output for this photo.")
    ext = os.path.splitext(t)[1]
    if ext not in (".xmp", ".dng") or (ext == ".dng" and os.path.basename(os.path.dirname(t)) !=
                                       os.path.normcase(DNG_EXPORT_DIR)):
        raise RawSafetyError(f"Refusing to touch {target}.")


def file_hash(path: str):
    import hashlib
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def snapshot_output(raw_path: str, store_dir: str) -> dict:
    """Remember the output file as it is now (before an approval writes it)."""
    target = output_path(raw_path)
    snap = {"target": target, "existed": os.path.isfile(target), "data": None, "copy": None}
    if snap["existed"]:
        if target.lower().endswith(".xmp"):
            with open(target, "rb") as f:
                snap["data"] = f.read()
        else:                                           # DNG export: keep a copy on disk, not in memory
            os.makedirs(store_dir, exist_ok=True)
            snap["copy"] = os.path.join(store_dir, uuid.uuid4().hex + ".dng")
            shutil.copy2(target, snap["copy"])
    return snap


def restore_output(raw_path: str, snap: dict, written_hash) -> str:
    """
    Undo an approval. Only works while the file is still exactly what the approval wrote (so nothing
    Lightroom saved since is lost). An output that didn't exist before is removed again; the photo
    itself is never touched.
    """
    target = snap["target"]
    _check_output(raw_path, target)
    before = raw_fingerprint(raw_path)
    if os.path.isfile(target) and written_hash and file_hash(target) != written_hash:
        raise RawSafetyError(f"{os.path.basename(target)} has changed since it was approved (Lightroom may have "
                             "saved to it), so it was left alone.")
    if snap["existed"]:
        folder = os.path.dirname(target)
        fd, tmp = tempfile.mkstemp(prefix=".rawreview-", suffix=os.path.splitext(target)[1] + ".tmp", dir=folder)
        try:
            with os.fdopen(fd, "wb") as f:
                if snap["data"] is not None:
                    f.write(snap["data"])
                else:
                    with open(snap["copy"], "rb") as src:
                        shutil.copyfileobj(src, f)
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        msg = f"Put back the previous {os.path.basename(target)}"
    else:
        if os.path.isfile(target):
            os.remove(target)                               # RAW Review's own new file, unchanged since
        msg = f"Removed {os.path.basename(target)} (it didn't exist before this approval)"
    if snap.get("copy") and os.path.isfile(snap["copy"]):
        os.remove(snap["copy"])
    if raw_fingerprint(raw_path) != before:
        raise RawSafetyError(f"{raw_path} changed during undo - stopping.")
    return msg


# --------------------------------------------------------------------------
# Histogram + clipping overlay for the preview (display only)
# --------------------------------------------------------------------------
BLOWN_LEVEL = 254        # any channel at or above this (0..255) counts as blown
BLACK_LEVEL = 1          # every channel at or below this counts as pure black


@dataclass
class Histogram:
    r: np.ndarray            # 256 counts per channel
    g: np.ndarray
    b: np.ndarray
    lum: np.ndarray          # 256 counts of luminance
    median: float            # median luminance, 0..1 (what auto exposure aims at TARGET_MEDIAN)
    blown: float             # share of pixels with a channel blown to white
    black: float             # share of pixels crushed to pure black


def histogram(img: np.ndarray) -> Histogram:
    """Histogram of a displayed uint8 RGB image (subsampled for speed: ~250k pixels)."""
    step = max(1, int(np.sqrt(img.shape[0] * img.shape[1] / 250_000)))
    sub = img[::step, ::step]
    r, g, b = (np.ascontiguousarray(sub[..., i]).ravel() for i in range(3))
    # Rec.709 luminance in integer maths (54 + 183 + 19 = 256)
    lum = ((r.astype(np.uint16) * 54 + g.astype(np.uint16) * 183 + b.astype(np.uint16) * 19 + 128) >> 8)
    counts = [np.bincount(c, minlength=256)[:256] for c in (r, g, b, lum)]
    cum = np.cumsum(counts[3])
    median = float(np.searchsorted(cum, cum[-1] / 2)) / 255.0
    mx = np.maximum(np.maximum(r, g), b)
    return Histogram(*counts, median=median, blown=float((mx >= BLOWN_LEVEL).mean()),
                     black=float((mx <= BLACK_LEVEL).mean()))


def clip_overlay(img: np.ndarray) -> np.ndarray:
    """Lightroom-style clipping warning: blown areas red, pure black areas blue."""
    mx = np.maximum(np.maximum(img[..., 0], img[..., 1]), img[..., 2])
    out = img.copy()
    out[mx >= BLOWN_LEVEL] = (255, 0, 0)
    out[mx <= BLACK_LEVEL] = (0, 90, 255)
    return out
