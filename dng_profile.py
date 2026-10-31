"""
RAW Review - DNG camera-profile support.

Why this exists
---------------
LibRaw (via rawpy) only hands over a colour matrix for some DNGs. For others,
notably Google Pixel DNGs, ``rgb_xyz_matrix`` comes back all zeros, so camera
RGB used to be treated as if it were already sRGB: flat, grey, wrong colours
and a white balance that could not be changed.

A DNG carries its own colour science in standard tags. This module reads them
(read-only, straight from the file's TIFF structure) and implements the parts
of the Adobe DNG specification that decide how colours look:

  * ColorMatrix1/2 + CalibrationIlluminant1/2  (interpolated by colour temperature)
  * ForwardMatrix1/2                           (camera -> XYZ D50, preferred)
  * AnalogBalance / CameraCalibration1/2
  * ProfileHueSatMap1/2                        (hue / saturation / value look)
  * ProfileLookTable                           (extra "look" layer)
  * ProfileToneCurve                           (the profile's contrast curve)
  * BaselineExposure                           (how much brighter the file wants to be)

Also applied (v1.2.0), because Google Pixel DNGs need them to look like Google's
own rendering:

  * ProfileGainTableMap  (Pixel's local HDR tone map, DNG 1.7): a spatial grid of
    gain-vs-brightness curves that lifts shadows and holds highlights
  * OpcodeList2 GainMap   (lens shading): flat-fields the vignetting and the
    colour shading in the corners

Not applied: the geometric lens-correction opcodes (OpcodeList3, WarpRectilinear)
and the newer ProfileGainTableMap2 layout. They are detected and reported.

Nothing here writes to any file.
"""

from __future__ import annotations

import os
import struct
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import numpy as np


# --------------------------------------------------------------------------
# User-facing options (changed from the "Camera profile" menu)
# --------------------------------------------------------------------------

@dataclass
class ProfileOptions:
    use_profile: bool = True        # use the DNG's own matrices/profile (False = LibRaw's, if any)
    look_strength: float = 1.0      # 0..1  ProfileHueSatMap + ProfileLookTable (0 = plain matrix colours)
    tone_curve: float = 1.0         # 0..1  ProfileToneCurve strength
    file_baseline_ev: bool = True   # use the file's BaselineExposure instead of PREVIEW_BASE_EV
    hdr_tone_map: bool = True       # apply the DNG's ProfileGainTableMap (Pixel local HDR tone map)
    hdr_strength: float = 1.0       # 0..1  how much of that tone map is applied (log-domain blend)


OPTIONS = ProfileOptions()


# --------------------------------------------------------------------------
# Constant matrices
# --------------------------------------------------------------------------

_PROPHOTO_TO_XYZ50 = np.array([
    [0.7977604896723027, 0.1351858371757403, 0.0313493495815248],
    [0.2880711282292934, 0.7118432178101014, 0.0000856539606053],
    [0.0000000000000000, 0.0000000000000000, 0.8251046025104601],
])
_XYZ50_TO_PROPHOTO = np.linalg.inv(_PROPHOTO_TO_XYZ50)
# Linear sRGB from XYZ with a D50 white point (Bradford-adapted, Lindbloom).
_XYZ50_TO_SRGB = np.array([
    [3.1338561, -1.6168667, -0.4906146],
    [-0.9787684, 1.9161415, 0.0334540],
    [0.0719453, -0.2289914, 1.4052427],
])
_PROPHOTO_TO_SRGB = (_XYZ50_TO_SRGB @ _PROPHOTO_TO_XYZ50).astype(np.float32)
_XYZ50_TO_PROPHOTO_F = _XYZ50_TO_PROPHOTO.astype(np.float32)
_D50_XY = (0.3457, 0.3585)

_BRADFORD = np.array([
    [0.8951, 0.2664, -0.1614],
    [-0.7502, 1.7135, 0.0367],
    [0.0389, -0.0685, 1.0296],
])
_BRADFORD_INV = np.linalg.inv(_BRADFORD)

# EXIF LightSource -> approximate colour temperature (K)
_LIGHT_SOURCE_K = {
    1: 5500, 2: 4200, 3: 2850, 4: 5500, 9: 5500, 10: 6500, 11: 7500,
    12: 6430, 13: 5000, 14: 4200, 15: 3450, 16: 2900, 17: 2856, 18: 4874,
    19: 6774, 20: 5500, 21: 6504, 22: 7500, 23: 5003, 24: 3200,
}


def _bradford(src_xy, dst_xy=_D50_XY) -> np.ndarray:
    def white(xy):
        x, y = xy
        return np.array([x / y, 1.0, (1.0 - x - y) / y])
    s = _BRADFORD @ white(src_xy)
    d = _BRADFORD @ white(dst_xy)
    return _BRADFORD_INV @ np.diag(d / s) @ _BRADFORD


# --------------------------------------------------------------------------
# Minimal read-only TIFF/DNG tag reader
# --------------------------------------------------------------------------

_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}

# Tags worth reading (big payloads are only read when listed here)
_T_SUBIFDS = 330
_T_UNIQUE_MODEL = 50708
_T_CM1, _T_CM2 = 50721, 50722
_T_CC1, _T_CC2 = 50723, 50724
_T_ANALOG = 50727
_T_NEUTRAL = 50728
_T_BASELINE_EV = 50730
_T_ILL1, _T_ILL2 = 50778, 50779
_T_PROFILE_NAME = 50936
_T_HSM_DIMS = 50937
_T_HSM1, _T_HSM2 = 50938, 50939
_T_TONE_CURVE = 50940
_T_FM1, _T_FM2 = 50964, 50965
_T_LOOK_DIMS = 50981
_T_LOOK_DATA = 50982
_T_HSM_ENC, _T_LOOK_ENC = 51107, 51108
_T_GAIN_TABLE_MAP = 52525
_T_GAIN_TABLE_MAP2 = 52544
_T_OPCODE2 = 51009
_T_CFA_PATTERN = 33422
_T_DEFAULT_CROP_ORIGIN, _T_DEFAULT_CROP_SIZE = 50719, 50720
_T_ACTIVE_AREA = 50829

_WANTED = {
    _T_UNIQUE_MODEL, _T_CM1, _T_CM2, _T_CC1, _T_CC2, _T_ANALOG, _T_NEUTRAL,
    _T_BASELINE_EV, _T_ILL1, _T_ILL2, _T_PROFILE_NAME, _T_HSM_DIMS, _T_HSM1,
    _T_HSM2, _T_TONE_CURVE, _T_FM1, _T_FM2, _T_LOOK_DIMS, _T_LOOK_DATA,
    _T_HSM_ENC, _T_LOOK_ENC, _T_SUBIFDS,
    _T_CFA_PATTERN, _T_DEFAULT_CROP_ORIGIN, _T_DEFAULT_CROP_SIZE, _T_ACTIVE_AREA,
}
_PRESENCE_ONLY = {_T_GAIN_TABLE_MAP2}
# Big binary payloads, kept as raw bytes and parsed on demand
_RAW_BYTES = {_T_GAIN_TABLE_MAP, _T_OPCODE2}


def _read_ifd(data: bytes, off: int, bo: str, tags: dict, presence: set):
    if off <= 0 or off + 2 > len(data):
        return
    n = struct.unpack_from(bo + "H", data, off)[0]
    for i in range(n):
        e = off + 2 + i * 12
        if e + 12 > len(data):
            return
        tag, typ, cnt = struct.unpack_from(bo + "HHI", data, e)
        if tag in _PRESENCE_ONLY:
            presence.add(tag)
            continue
        if (tag not in _WANTED and tag not in _RAW_BYTES) or tag in tags:
            continue
        sz = _TYPE_SIZE.get(typ)
        if sz is None:
            continue
        total = sz * cnt
        if total <= 4:
            raw = data[e + 8:e + 8 + total]
        else:
            p = struct.unpack_from(bo + "I", data, e + 8)[0]
            raw = data[p:p + total]
            if len(raw) < total:
                continue
        tags[tag] = bytes(raw) if tag in _RAW_BYTES else _decode_value(typ, cnt, raw, bo)
        if tag in _RAW_BYTES:
            presence.add(tag)


def _decode_value(typ: int, cnt: int, raw: bytes, bo: str):
    if typ == 2:
        return raw[:cnt].split(b"\0")[0].decode("latin-1", "replace")
    if typ in (5, 10):
        v = np.frombuffer(raw, dtype=bo + ("u4" if typ == 5 else "i4")).astype(np.float64)
        num, den = v[0::2], v[1::2]
        return np.where(den != 0, num / np.where(den != 0, den, 1), 0.0)
    code = {1: "u1", 3: "u2", 4: "u4", 6: "i1", 7: "u1", 8: "i2", 9: "i4", 11: "f4", 12: "f8", 13: "u4"}[typ]
    return np.frombuffer(raw, dtype=bo + code).astype(np.float64)


def _tiff_tags(path: str):
    """Return (tags, presence) from IFD0 and its SubIFDs. Reads the file read-only."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:2] == b"II":
        bo = "<"
    elif data[:2] == b"MM":
        bo = ">"
    else:
        raise ValueError("not a TIFF/DNG file")
    if struct.unpack_from(bo + "H", data, 2)[0] != 42:
        raise ValueError("not a classic TIFF file")
    tags, presence = {}, set()
    _read_ifd(data, struct.unpack_from(bo + "I", data, 4)[0], bo, tags, presence)
    for sub in list(np.atleast_1d(tags.get(_T_SUBIFDS, []))):
        _read_ifd(data, int(sub), bo, tags, presence)
    return tags, presence


# --------------------------------------------------------------------------
# Profile data
# --------------------------------------------------------------------------

@dataclass
class _Table:
    dims: tuple          # (hue, sat, val) divisions
    data: np.ndarray     # (val, hue, sat, 3) float32: hue shift (deg), sat scale, val scale
    encoding: int = 0    # 0 linear, 1 sRGB



# --------------------------------------------------------------------------
# Pixel extras: lens shading (OpcodeList2 GainMap) and the local HDR tone map
# (ProfileGainTableMap)
# --------------------------------------------------------------------------

# Set to False if a future LibRaw starts applying the DNG's GainMap opcodes
# itself (the shading would then be corrected twice).
APPLY_DNG_LENS_SHADING = True


@dataclass
class GainMapOp:
    """One DNG GainMap opcode: a coarse grid of gains for one CFA phase (R, Gr, Gb or B)."""
    top: int
    left: int
    bottom: int
    right: int
    row_pitch: int
    col_pitch: int
    spacing_v: float
    spacing_h: float
    origin_v: float
    origin_h: float
    data: np.ndarray            # (rows, cols) float32 gain


def _parse_gain_maps(blob: bytes) -> list:
    """OpcodeList2 is always big-endian. Only GainMap (opcode 9, one plane) is used."""
    ops = []
    try:
        n = struct.unpack_from(">I", blob, 0)[0]
        p = 4
        for _ in range(min(n, 64)):
            oid, _ver, _flags, nbytes = struct.unpack_from(">IIII", blob, p)
            p += 16
            body = blob[p:p + nbytes]
            p += nbytes
            if oid != 9 or len(body) < 76:
                continue
            top, left, bot, right, _plane, planes, rp, cp, mv, mh = struct.unpack_from(">10I", body, 0)
            sv, sh, ov, oh = struct.unpack_from(">4d", body, 40)
            mplanes = struct.unpack_from(">I", body, 72)[0]
            if mv < 1 or mh < 1 or mplanes < 1 or rp < 1 or cp < 1 or len(body) < 76 + mv * mh * mplanes * 4:
                continue
            data = np.frombuffer(body, dtype=">f4", count=mv * mh * mplanes, offset=76)
            data = data.reshape(mv, mh, mplanes)[..., 0].astype(np.float32)
            if np.isfinite(data).all() and data.min() > 0:
                ops.append(GainMapOp(top, left, bot, right, rp, cp, sv, sh, ov, oh, data))
    except (struct.error, ValueError):
        return []
    return ops


def _interp_grid(grid: np.ndarray, fy: np.ndarray, fx: np.ndarray) -> np.ndarray:
    """Bilinear lookup of a (rows, cols) grid at fractional indices fy (h,), fx (w,) -> (h, w)."""
    rows, cols = grid.shape
    fy = np.clip(fy, 0.0, rows - 1.0)
    fx = np.clip(fx, 0.0, cols - 1.0)
    y0 = np.minimum(fy.astype(np.int32), max(rows - 2, 0))
    x0 = np.minimum(fx.astype(np.int32), max(cols - 2, 0))
    y1 = np.minimum(y0 + 1, rows - 1)
    x1 = np.minimum(x0 + 1, cols - 1)
    wy = (fy - y0).astype(np.float32)[:, None]
    wx = (fx - x0).astype(np.float32)[None, :]
    a = grid[np.ix_(y0, x0)]
    b = grid[np.ix_(y0, x1)]
    c = grid[np.ix_(y1, x0)]
    d = grid[np.ix_(y1, x1)]
    return (a + (b - a) * wx) * (1.0 - wy) + (c + (d - c) * wx) * wy


class GainTableMap:
    """
    DNG 1.7 ProfileGainTableMap: for every point of a coarse spatial grid, a table
    of gains indexed by pixel brightness. It is Pixel's (and Apple ProRAW's)
    local tone map: dark areas get lifted, bright areas held back, differently
    in different parts of the picture.

    Brightness of a pixel = w0*R + w1*G + w2*B + w3*min(R,G,B) + w4*max(R,G,B)
    (the DNG's own MapInputWeights), clipped to 0..1 and looked up linearly in
    the table. The gain multiplies R, G and B equally, so hue is preserved.
    """

    def __init__(self, v, h, n, spacing_v, spacing_h, origin_v, origin_h, weights, table):
        self.v, self.h, self.n = int(v), int(h), int(n)
        self.spacing_v, self.spacing_h = float(spacing_v), float(spacing_h)
        self.origin_v, self.origin_h = float(origin_v), float(origin_h)
        self.weights = np.asarray(weights, dtype=np.float32)
        self.flat = np.ascontiguousarray(table, dtype=np.float32).reshape(-1)    # (v, h, n) row-major
        self._geo = None                                                         # per-size cache

    @staticmethod
    def parse(blob: bytes):
        """Parse the tag payload. Returns None if it does not look like a valid table."""
        if blob is None or len(blob) < 64:
            return None
        for endian in ("<", ">"):             # the layout is the same; be liberal about byte order
            try:
                v, h = struct.unpack_from(endian + "II", blob, 0)
                sv, sh, ov, oh = struct.unpack_from(endian + "4d", blob, 8)
                n = struct.unpack_from(endian + "I", blob, 40)[0]
                w = np.frombuffer(blob, dtype=endian + "f4", count=5, offset=44)
            except (struct.error, ValueError):
                continue
            if not (1 <= v <= 4096 and 1 <= h <= 4096 and 2 <= n <= 65536):
                continue
            if len(blob) < 64 + v * h * n * 4 or not np.isfinite(w).all():
                continue
            if not (sv > 0 and sh > 0 and np.isfinite([sv, sh, ov, oh]).all()):
                continue
            tab = np.frombuffer(blob, dtype=endian + "f4", count=v * h * n, offset=64).astype(np.float32)
            if not np.isfinite(tab).all() or tab.min() <= 0.0:
                continue
            return GainTableMap(v, h, n, sv, sh, ov, oh, w.astype(np.float32), tab)
        return None

    # -- per-image-size geometry (spatial bilinear weights), cached -------------
    def _geometry(self, hh: int, ww: int):
        g = self._geo
        if g is not None and g[0] == (hh, ww):
            return g[1]
        yy = (np.arange(hh, dtype=np.float32) + 0.5) / hh          # normalised position of pixel centres
        xx = (np.arange(ww, dtype=np.float32) + 0.5) / ww
        fy = np.clip((yy - self.origin_v) / self.spacing_v, 0.0, self.v - 1.0)
        fx = np.clip((xx - self.origin_h) / self.spacing_h, 0.0, self.h - 1.0)
        y0 = np.minimum(fy.astype(np.int32), max(self.v - 2, 0))
        x0 = np.minimum(fx.astype(np.int32), max(self.h - 2, 0))
        cell = (y0[:, None] * self.h + x0[None, :]).astype(np.int32).reshape(-1)
        wy = np.broadcast_to((fy - y0)[:, None], (hh, ww)).astype(np.float32).reshape(-1)
        wx = np.broadcast_to((fx - x0)[None, :], (hh, ww)).astype(np.float32).reshape(-1)
        geo = (cell, wy, wx)
        self._geo = ((hh, ww), geo)
        return geo

    def prepare(self, hh: int, ww: int):
        self._geometry(hh, ww)

    def gain(self, x: np.ndarray, hh: int, ww: int, a: int = 0, b: int | None = None,
             strength: float = 1.0) -> np.ndarray:
        """
        Gain for pixels a..b (flat, row-major) of an hh x ww image.
        x is (3, b-a) linear RGB (any working space; the DNG measures it after the
        profile look and before the tone curve). Returns (b-a,) float32.
        """
        cell, wy, wx = self._geometry(hh, ww)
        b = cell.shape[0] if b is None else b
        w = self.weights
        mx = np.maximum(np.maximum(x[0], x[1]), x[2])
        mn = np.minimum(np.minimum(x[0], x[1]), x[2])
        v = w[0] * x[0] + w[1] * x[1] + w[2] * x[2] + w[3] * mn + w[4] * mx
        t = np.clip(v, 0.0, 1.0) * np.float32(self.n - 1)
        k0 = np.minimum(t.astype(np.int32), self.n - 2)
        wk = t - k0
        base = cell[a:b] * np.int32(self.n) + k0
        tab = self.flat
        step_x, step_y = self.n, self.h * self.n

        def node(off):
            i = base + np.int32(off)
            lo = tab.take(i)
            hi = tab.take(i + 1)
            return lo + (hi - lo) * wk

        # the neighbour cell must exist at the right / bottom edge
        g00, g01, g10, g11 = node(0), node(step_x), node(step_y), node(step_y + step_x)
        gx = wx[a:b]
        gy = wy[a:b]
        g0 = g00 + (g01 - g00) * gx
        g1 = g10 + (g11 - g10) * gx
        g = g0 + (g1 - g0) * gy
        if strength != 1.0:
            g = np.exp(np.float32(strength) * np.log(np.maximum(g, np.float32(1e-3)))).astype(np.float32)
        return g.astype(np.float32, copy=False)


@dataclass
class DngProfile:
    model: str = ""
    profile_name: str = ""
    temps: list = field(default_factory=list)       # ascending illuminant temps (K)
    cm: list = field(default_factory=list)          # XYZ -> camera (3x3), already x AnalogBalance x CameraCalibration
    fm: list = field(default_factory=list)          # camera -> XYZ D50 (3x3) or None
    hsm: list = field(default_factory=list)         # _Table or None, one per illuminant
    look: _Table | None = None
    tone_curve: tuple | None = None                 # (xs, ys)
    baseline_ev: float = 0.0
    as_shot_neutral: np.ndarray | None = None
    has_gain_table_map: bool = False          # a ProfileGainTableMap tag is present
    has_lens_shading: bool = False            # OpcodeList2 is present
    gain_table: GainTableMap | None = None    # parsed ProfileGainTableMap (applied when valid)
    gain_maps: list = field(default_factory=list)       # parsed GainMap opcodes (lens shading)
    has_unsupported_tone_map: bool = False    # a newer ProfileGainTableMap2 we cannot read
    cfa: np.ndarray | None = None             # 2x2 CFA colour codes (0 R, 1 G, 2 B)
    active_area: tuple | None = None          # (top, left, bottom, right) in the raw image
    crop: tuple | None = None                 # (x, y, w, h) default crop, relative to the active area

    # -- which part of the sensor a decoded array covers --------------------------
    def region_for(self, h: int, w: int):
        """
        (x0, y0, width, height) in raw-sensor pixels that a decoded array of h x w
        covers. LibRaw's half-size output is either the default crop or the whole
        active area, at half or full resolution; pick whichever matches. None when
        nothing is known about the sensor.
        """
        cands = []
        if self.active_area is not None:
            t, l, b, r = self.active_area
            full = (l, t, r - l, b - t)
            cands.append(full)
            if self.crop is not None:
                cx, cy, cw, ch = self.crop
                cands.insert(0, (l + cx, t + cy, cw, ch))
        if not cands:
            return None
        best, best_err = None, None
        for c in cands:
            for scale in (2.0, 1.0):
                err = abs(c[2] / scale - w) / w + abs(c[3] / scale - h) / h
                if best_err is None or err < best_err:
                    best, best_err = c, err
        return best

    def lens_shading_gain(self, h: int, w: int):
        """
        Per-channel (R, G, B) lens-shading gain for a decoded h x w array, as (h, w, 3)
        float32, or None if the DNG has no usable GainMap. Each GainMap belongs to one
        CFA phase; the two greens are averaged, so this is exact for a 2x2 binned image.
        """
        if not self.gain_maps or self.cfa is None or self.active_area is None:
            return None
        reg = self.region_for(h, w)
        if reg is None:
            return None
        x0, y0, rw, rh = reg
        t, l, b, r = self.active_area
        act_h, act_w = max(b - t - 1, 1), max(r - l - 1, 1)
        # raw-sensor coordinates of each decoded pixel's centre
        ys = y0 + (np.arange(h, dtype=np.float64) + 0.5) * (rh / h) - 0.5 - t
        xs = x0 + (np.arange(w, dtype=np.float64) + 0.5) * (rw / w) - 0.5 - l
        acc = np.zeros((h, w, 3), np.float32)
        cnt = np.zeros(3, np.float32)
        for op in self.gain_maps:
            colour = int(self.cfa[(op.top - t) % 2, (op.left - l) % 2])
            if not 0 <= colour <= 2:
                continue
            rows, cols = op.data.shape
            fy = ((ys / act_h) - op.origin_v) / op.spacing_v
            fx = ((xs / act_w) - op.origin_h) / op.spacing_h
            acc[..., colour] += _interp_grid(op.data, fy.astype(np.float32), fx.astype(np.float32))
            cnt[colour] += 1
        if (cnt == 0).any():
            return None
        return acc / cnt

    # -- interpolation between the two calibration illuminants (DNG spec: in 1/T)
    def weight(self, temp: float) -> float:
        """Weight of the FIRST (lower-temperature) illuminant."""
        if len(self.temps) < 2:
            return 1.0
        t1, t2 = self.temps[0], self.temps[1]
        if temp <= t1:
            return 1.0
        if temp >= t2:
            return 0.0
        return float((1.0 / temp - 1.0 / t2) / (1.0 / t1 - 1.0 / t2))

    def _mix(self, items, temp):
        g = self.weight(temp)
        if len(items) == 1 or g >= 1.0:
            return items[0]
        if g <= 0.0:
            return items[1]
        return g * items[0] + (1.0 - g) * items[1]

    def color_matrix(self, temp: float) -> np.ndarray:
        return self._mix(self.cm, temp)

    def forward_matrix(self, temp: float):
        if not self.fm or any(m is None for m in self.fm):
            return None
        return self._mix(self.fm, temp)

    def hsm_table(self, temp: float):
        tabs = [t for t in self.hsm if t is not None]
        if not tabs:
            return None
        if len(tabs) == 1 or len(self.hsm) < 2 or self.hsm[0] is None or self.hsm[1] is None:
            return tabs[0]
        g = self.weight(temp)
        if g >= 1.0:
            return self.hsm[0]
        if g <= 0.0:
            return self.hsm[1]
        return _Table(self.hsm[0].dims, (g * self.hsm[0].data + (1.0 - g) * self.hsm[1].data).astype(np.float32),
                      self.hsm[0].encoding)

    def describe(self) -> str:
        bits = []
        if any(f is not None for f in self.fm):
            bits.append("forward matrices")
        if any(t is not None for t in self.hsm):
            bits.append("hue/sat map")
        if self.look is not None:
            bits.append("look table")
        if self.tone_curve is not None:
            bits.append("tone curve")
        s = f"DNG profile '{self.profile_name or self.model or 'embedded'}': " + (", ".join(bits) or "matrices only")
        if self.gain_table is not None:
            s += " + local HDR tone map" if OPTIONS.hdr_tone_map else " (local HDR tone map off)"
        elif self.has_unsupported_tone_map:
            s += " (newer local tone map format not applied)"
        if self.gain_maps and APPLY_DNG_LENS_SHADING:
            s += " + lens shading"
        return s


def _mat(v, n=9):
    if v is None or len(v) < n:
        return None
    m = np.asarray(v[:n], dtype=np.float64).reshape(3, 3)
    return m if np.isfinite(m).all() else None


def _table(dims, data, enc) -> _Table | None:
    if dims is None or data is None or len(dims) < 3:
        return None
    hd, sd, vd = (int(x) for x in dims[:3])
    if hd < 1 or sd < 1 or vd < 1 or len(data) < hd * sd * vd * 3:
        return None
    arr = np.asarray(data[:hd * sd * vd * 3], dtype=np.float32).reshape(vd, hd, sd, 3)
    return _Table((hd, sd, vd), arr, int(enc))


def read_dng_profile(path: str) -> DngProfile | None:
    """Read the colour profile from a DNG. Returns None if it has no usable matrices."""
    try:
        tags, presence = _tiff_tags(path)
    except Exception:  # noqa: BLE001 - a missing/odd profile must never block loading
        return None

    cm1, cm2 = _mat(tags.get(_T_CM1)), _mat(tags.get(_T_CM2))
    if cm1 is None and cm2 is None:
        return None

    def scalar(tag, default=None):
        v = tags.get(tag)
        return default if v is None or len(v) == 0 else float(v[0])

    analog = tags.get(_T_ANALOG)
    ab = np.diag(np.asarray(analog[:3], dtype=np.float64)) if analog is not None and len(analog) >= 3 else np.eye(3)

    entries = []   # (temp, cm, fm, hsm)
    hsm_dims = tags.get(_T_HSM_DIMS)
    hsm_enc = scalar(_T_HSM_ENC, 0)
    for idx, (cm_tag, cc_tag, il_tag, fm_tag, hs_tag, dflt) in enumerate((
            (_T_CM1, _T_CC1, _T_ILL1, _T_FM1, _T_HSM1, 2856.0),
            (_T_CM2, _T_CC2, _T_ILL2, _T_FM2, _T_HSM2, 6504.0))):
        cm = _mat(tags.get(cm_tag))
        if cm is None:
            continue
        cc = _mat(tags.get(cc_tag))
        cm = ab @ (cc if cc is not None else np.eye(3)) @ cm
        ill = scalar(il_tag)
        temp = _LIGHT_SOURCE_K.get(int(ill), dflt) if ill is not None else dflt
        entries.append((float(temp), cm, _mat(tags.get(fm_tag)),
                        _table(hsm_dims, tags.get(hs_tag), hsm_enc)))
    entries.sort(key=lambda e: e[0])
    if len(entries) == 2 and abs(entries[0][0] - entries[1][0]) < 1.0:
        entries = entries[:1]

    tc = tags.get(_T_TONE_CURVE)
    curve = None
    if tc is not None and len(tc) >= 4:
        pts = np.asarray(tc[:len(tc) // 2 * 2], dtype=np.float64).reshape(-1, 2)
        if np.all(np.diff(pts[:, 0]) > 0):
            curve = (pts[:, 0].astype(np.float32), pts[:, 1].astype(np.float32))

    neutral = tags.get(_T_NEUTRAL)
    prof = DngProfile(
        model=str(tags.get(_T_UNIQUE_MODEL, "")),
        profile_name=str(tags.get(_T_PROFILE_NAME, "")),
        temps=[e[0] for e in entries],
        cm=[e[1] for e in entries],
        fm=[e[2] for e in entries],
        hsm=[e[3] for e in entries],
        look=_table(tags.get(_T_LOOK_DIMS), tags.get(_T_LOOK_DATA), scalar(_T_LOOK_ENC, 0)),
        tone_curve=curve,
        baseline_ev=scalar(_T_BASELINE_EV, 0.0),
        as_shot_neutral=(np.asarray(neutral[:3], dtype=np.float64)
                         if neutral is not None and len(neutral) >= 3 else None),
        has_gain_table_map=_T_GAIN_TABLE_MAP in presence,
        has_lens_shading=_T_OPCODE2 in presence,
        has_unsupported_tone_map=_T_GAIN_TABLE_MAP2 in presence,
    )
    prof.gain_table = GainTableMap.parse(tags.get(_T_GAIN_TABLE_MAP))
    prof.gain_maps = _parse_gain_maps(tags.get(_T_OPCODE2)) if tags.get(_T_OPCODE2) else []

    cfa = tags.get(_T_CFA_PATTERN)
    if cfa is not None and len(cfa) == 4 and set(int(c) for c in cfa) <= {0, 1, 2}:
        prof.cfa = np.asarray(cfa, dtype=np.int32).reshape(2, 2)
    aa = tags.get(_T_ACTIVE_AREA)
    if aa is not None and len(aa) >= 4 and aa[2] > aa[0] and aa[3] > aa[1]:
        prof.active_area = tuple(int(round(v)) for v in aa[:4])
    co, cs = tags.get(_T_DEFAULT_CROP_ORIGIN), tags.get(_T_DEFAULT_CROP_SIZE)
    if co is not None and cs is not None and len(co) >= 2 and len(cs) >= 2 and cs[0] > 0 and cs[1] > 0:
        prof.crop = (int(round(co[0])), int(round(co[1])), int(round(cs[0])), int(round(cs[1])))
    if prof.active_area is None and prof.gain_maps:
        prof.active_area = (0, 0, max(o.bottom for o in prof.gain_maps), max(o.right for o in prof.gain_maps))
    return prof


# --------------------------------------------------------------------------
# HSV helpers and table lookup (vectorised, float32, planar: x is (3, N))
# Planar layout keeps every operation on contiguous arrays, which is several
# times faster in NumPy than working on (N, 3) rows.
# --------------------------------------------------------------------------

def _rgb_to_hsv(x: np.ndarray):
    """x: (3,N). Returns h in [0,6), s in [0,1], v = max channel."""
    r, g, b = x[0], x[1], x[2]
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    d = mx - mn
    pos = d > 0
    safe = np.where(pos, d, np.float32(1.0))
    h = np.where(mx == r, (g - b) / safe, np.where(mx == g, 2.0 + (b - r) / safe, 4.0 + (r - g) / safe))
    h = np.where(h < 0, h + 6.0, h)
    h = np.where(pos, h, 0.0).astype(np.float32)
    s = (d / np.where(mx > 0, mx, np.float32(1.0))).astype(np.float32)
    return h, s, mx


def _hsv_to_rgb(h, s, v) -> np.ndarray:
    out = np.empty((3, h.shape[0]), dtype=np.float32)
    vs = v * s
    for c, n in enumerate((5.0, 3.0, 1.0)):
        k = h + np.float32(n)
        k = np.where(k >= 6.0, k - 6.0, k)
        out[c] = v - vs * np.clip(np.minimum(k, 4.0 - k), 0.0, 1.0)
    return out


def _srgb_enc(x):
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.0031308, 12.92 * x, 1.055 * np.power(np.maximum(x, 0.0031308), 1 / 2.4) - 0.055)


def _sample(tbl: np.ndarray, h, s, v):
    """Trilinear lookup. tbl (vd, hd, sd, 3); h in [0,6), s,v in [0,1]. Returns 3 planes."""
    vd, hd, sd, _ = tbl.shape
    chans = [np.ascontiguousarray(tbl[..., c]).reshape(-1) for c in range(3)]

    hf = h * np.float32(hd / 6.0)
    h0 = hf.astype(np.int32)                       # hf >= 0, so truncation == floor
    fh = hf - h0
    h0 = np.where(h0 >= hd, h0 - hd, h0)
    h1 = np.where(h0 + 1 >= hd, h0 + 1 - hd, h0 + 1)

    if sd > 1:
        sf = np.clip(s, 0.0, 1.0) * np.float32(sd - 1)
        s0 = np.minimum(sf.astype(np.int32), sd - 2)
        fs = sf - s0
        s1 = s0 + 1
    else:
        s0 = s1 = np.zeros_like(h0)
        fs = np.zeros_like(fh)

    if vd > 1:
        vf = np.clip(v, 0.0, 1.0) * np.float32(vd - 1)
        v0 = np.minimum(vf.astype(np.int32), vd - 2)
        fv = vf - v0
        vplanes = ((v0, None), (v0 + 1, fv))
    else:
        vplanes = ((np.zeros_like(h0), None),)

    out = [None, None, None]
    prev = None
    for vi, fvv in vplanes:
        base = vi * hd
        r0 = (base + h0) * sd
        r1 = (base + h1) * sd
        i00, i01, i10, i11 = r0 + s0, r0 + s1, r1 + s0, r1 + s1
        cur = []
        for c in range(3):
            t = chans[c]
            a = np.take(t, i00)
            a += (np.take(t, i01) - a) * fs
            b = np.take(t, i10)
            b += (np.take(t, i11) - b) * fs
            a += (b - a) * fh
            cur.append(a)
        if fvv is None:
            prev = cur
            out = cur
        else:
            out = [prev[c] + (cur[c] - prev[c]) * fvv for c in range(3)]
    return out


def _apply_table(x: np.ndarray, tab: _Table, strength: float) -> np.ndarray:
    """Apply a HueSatMap / LookTable to (3,N) linear ProPhoto. strength 0..1 scales the effect."""
    if strength <= 0.0:
        return x
    h, s, v = _rgb_to_hsv(x)
    vc = np.clip(v, 0.0, 1.0)
    if tab.encoding == 1:
        vc = _srgb_enc(vc).astype(np.float32)
    shift, sat, val = _sample(tab.data, h, s, vc)
    k = np.float32(strength)
    h2 = h + shift * (k * np.float32(6.0 / 360.0))
    h2 = np.where(h2 >= 6.0, h2 - 6.0, np.where(h2 < 0.0, h2 + 6.0, h2))
    s2 = np.clip(s * (1.0 + (sat - 1.0) * k), 0.0, 1.0)
    v2 = np.maximum(v * (1.0 + (val - 1.0) * k), 0.0)
    return _hsv_to_rgb(h2, s2, v2)


def _rgb_tone(x: np.ndarray, xs, ys) -> np.ndarray:
    """Hue-preserving tone curve (the DNG SDK's RGBTone): curve the brightest and darkest channel."""
    mx = np.maximum(np.maximum(x[0], x[1]), x[2])
    mn = np.minimum(np.minimum(x[0], x[1]), x[2])
    d = mx - mn
    mx2 = np.interp(mx, xs, ys).astype(np.float32)
    mn2 = np.interp(mn, xs, ys).astype(np.float32)
    ok = d > 1e-6
    scale = np.where(ok, (mx2 - mn2) / np.where(ok, d, np.float32(1.0)), np.float32(0.0)).astype(np.float32)
    out = np.empty_like(x)
    for c in range(3):
        out[c] = np.where(ok, mn2 + (x[c] - mn) * scale, mx2)
    return out


_POOL = None


def _parallel_columns(fn, x: np.ndarray, min_chunk: int = 150_000, with_range: bool = False) -> np.ndarray:
    """
    Run fn on slices of the pixel axis of a (3,N) array in threads (NumPy releases the GIL).
    With with_range=True, fn is called as fn(chunk, a, b) so it knows which pixels it got.
    """
    global _POOL
    n = x.shape[1]
    workers = min(8, os.cpu_count() or 1)
    parts = min(workers, max(1, n // min_chunk))
    if parts <= 1:
        return fn(x, 0, n) if with_range else fn(x)
    if _POOL is None:
        _POOL = ThreadPoolExecutor(max_workers=workers)
    edges = np.linspace(0, n, parts + 1).astype(int)
    out = np.empty_like(x, dtype=np.float32)

    def job(i):
        a, b = edges[i], edges[i + 1]
        chunk = np.ascontiguousarray(x[:, a:b])
        out[:, a:b] = fn(chunk, a, b) if with_range else fn(chunk)

    list(_POOL.map(job, range(parts)))
    return out


# --------------------------------------------------------------------------
# Colour model used by raw_review_core.CameraColor
# --------------------------------------------------------------------------

class DngColorModel:
    """Camera <-> XYZ <-> Temperature/Tint and the profile's look, per the DNG spec."""

    def __init__(self, profile: DngProfile, xy_to_temp_tint, temp_tint_to_xy):
        self.p = profile
        self._xy_to_tt = xy_to_temp_tint
        self._tt_to_xy = temp_tint_to_xy
        self._solve_cache: dict = {}
        self._hsm_cache = None          # (key, array)
        n = profile.as_shot_neutral
        self.as_shot_mul = None
        if n is not None and np.isfinite(n).all() and (n > 0).all():
            m = 1.0 / n
            self.as_shot_mul = m / m[1]

    # ---- white balance <-> temperature -------------------------------------
    @staticmethod
    def _neutral(mul) -> np.ndarray:
        n = 1.0 / np.maximum(np.asarray(mul, dtype=np.float64), 1e-9)
        return n / n[1]

    def _solve(self, mul):
        """(temperature K, white-point xy) of the light that makes `mul` neutral. DNG spec iteration."""
        key = tuple(np.round(np.asarray(mul, dtype=np.float64), 6))
        hit = self._solve_cache.get(key)
        if hit is not None:
            return hit
        n = self._neutral(mul)
        temp = 5000.0
        xy = (0.3457, 0.3585)
        for _ in range(40):
            try:
                xyz = np.linalg.solve(self.p.color_matrix(temp), n)
            except np.linalg.LinAlgError:
                break
            s = float(xyz.sum())
            if not np.isfinite(s) or abs(s) < 1e-12:
                break
            xy = (float(xyz[0] / s), float(xyz[1] / s))
            t_new = float(np.clip(self._xy_to_tt(*xy)[0], 2000.0, 50000.0))
            if abs(t_new - temp) < 0.5:
                temp = t_new
                break
            temp = temp + 0.8 * (t_new - temp)
        if len(self._solve_cache) > 256:
            self._solve_cache.clear()
        self._solve_cache[key] = (temp, xy)
        return temp, xy

    def mul_to_temp_tint(self, mul):
        _t, xy = self._solve(mul)
        return self._xy_to_tt(*xy)

    def temp_tint_to_mul(self, temp, tint):
        x, y = self._tt_to_xy(float(temp), float(tint))
        xyz = np.array([x / y, 1.0, (1.0 - x - y) / y])
        n = self.p.color_matrix(float(temp)) @ xyz
        n = np.maximum(n, 1e-9)
        m = 1.0 / n
        return m / m[1]

    # ---- matrices -----------------------------------------------------------
    def cam_to_xyz50(self, mul) -> np.ndarray:
        """3x3 that takes (camera RGB * mul) to XYZ with a D50 white point."""
        temp, xy = self._solve(mul)
        fm = self.p.forward_matrix(temp)
        if fm is not None:
            return fm
        cm = self.p.color_matrix(temp)
        n = self._neutral(mul)
        m = _bradford(xy) @ np.linalg.inv(cm) @ np.diag(n)
        white_y = (m @ np.ones(3))[1]
        return m / max(white_y, 1e-9)

    def cam_to_srgb(self, mul) -> np.ndarray:
        """Plain colorimetric camera -> linear sRGB (no profile look)."""
        return _XYZ50_TO_SRGB @ self.cam_to_xyz50(mul)

    # ---- settings that depend on the options ---------------------------------
    @property
    def base_ev(self) -> float | None:
        return self.p.baseline_ev if OPTIONS.file_baseline_ev else None

    @property
    def base_contrast_factor(self) -> float:
        """How much of the app's own gentle base S-curve is still needed (0 = profile curve covers it)."""
        return 1.0 - (OPTIONS.tone_curve if self.p.tone_curve is not None else 0.0)

    # ---- the profile render ---------------------------------------------------
    def render_linear(self, cam: np.ndarray, mul, exposure_ev: float, base_ev: float) -> np.ndarray:
        """
        Camera RGB -> linear sRGB with the profile applied, in the order the DNG spec gives:
        matrix -> ProPhoto -> HueSatMap -> exposure -> LookTable -> local HDR tone map
        (ProfileGainTableMap, if the file has one) -> tone curve -> sRGB.
        `cam` is HxWx3 linear camera RGB (not white balanced).
        """
        h, w = cam.shape[:2]
        temp, _xy = self._solve(mul)
        mulf = np.asarray(mul, dtype=np.float32)
        m = (_XYZ50_TO_PROPHOTO @ self.cam_to_xyz50(mul)).astype(np.float32)

        look = float(OPTIONS.look_strength)
        key = (cam.shape, int(cam.__array_interface__["data"][0]), tuple(np.round(mulf, 5)), round(look, 3))
        cached = self._hsm_cache
        if cached is not None and cached[0] == key:
            pp = cached[1]
        else:
            planar = np.ascontiguousarray(cam.reshape(-1, 3).T)               # (3, N)
            pp = np.maximum(m @ (planar * mulf[:, None]), 0.0).astype(np.float32)
            hsm = self.p.hsm_table(temp)
            if hsm is not None and look > 0.0:
                pp = _parallel_columns(lambda c: _apply_table(c, hsm, look), pp)
            self._hsm_cache = (key, pp)

        gain = np.float32(2.0 ** (exposure_ev + base_ev))
        look_tab = self.p.look if look > 0.0 else None
        tone = float(OPTIONS.tone_curve)
        curve = None
        if self.p.tone_curve is not None and tone > 0.0:
            xs, ys = self.p.tone_curve
            curve = (xs, xs + (ys - xs) * np.float32(tone))
        gtm = self._active_gain_table()
        hdr = float(OPTIONS.hdr_strength)
        if gtm is not None:
            gtm.prepare(h, w)

        def to_look(c):
            """Exposure -> soft shoulder -> profile look table (everything before the tone map)."""
            c = c * gain
            # soft highlight shoulder (same idea as the plain path), before the look/curve
            knee = np.float32(0.8)
            c = np.where(c < knee, c, knee + (1 - knee) * np.tanh((c - knee) / (1 - knee))).astype(np.float32)
            if look_tab is not None:
                c = _apply_table(c, look_tab, look)
            return c

        def finish(c, a, b):
            c = to_look(c)
            if gtm is not None:
                # Pixel's local HDR tone map: one gain per pixel, measured on the looked-at
                # image and applied to R, G and B alike (this order matches Google's own render).
                c = c * gtm.gain(c, h, w, a, b, hdr)[None, :]
            if curve is not None:
                c = _rgb_tone(np.clip(c, 0.0, 1.0), curve[0], curve[1])
            return np.maximum(_PROPHOTO_TO_SRGB @ c, 0.0)

        lin = _parallel_columns(finish, pp, with_range=True)                 # (3, N)
        return np.ascontiguousarray(lin.T).reshape(h, w, 3)

    # ---- the local HDR tone map -----------------------------------------------
    def _active_gain_table(self):
        """The DNG's ProfileGainTableMap if it should be applied, else None."""
        gt = self.p.gain_table
        if gt is None or not OPTIONS.hdr_tone_map or float(OPTIONS.hdr_strength) <= 0.0:
            return None
        return gt

    @property
    def has_hdr_tone_map(self) -> bool:
        return self._active_gain_table() is not None

    def local_gain(self, cam: np.ndarray, mul, exposure_ev: float, base_ev: float):
        """
        The HDR tone map's gain for every pixel of `cam` (h, w), as the preview would apply it
        at this exposure, or None if no tone map is active. Used by the auto tools so they
        judge the photo the way it will actually look.
        """
        gtm = self._active_gain_table()
        if gtm is None:
            return None
        h, w = cam.shape[:2]
        m = (_XYZ50_TO_PROPHOTO @ self.cam_to_xyz50(mul)).astype(np.float32)
        planar = np.ascontiguousarray(cam.reshape(-1, 3).T)
        pp = np.maximum(m @ (planar * np.asarray(mul, dtype=np.float32)[:, None]), 0.0).astype(np.float32)
        temp, _xy = self._solve(mul)
        look = float(OPTIONS.look_strength)
        hsm = self.p.hsm_table(temp)
        if hsm is not None and look > 0.0:
            pp = _apply_table(pp, hsm, look)
        c = pp * np.float32(2.0 ** (exposure_ev + base_ev))
        knee = np.float32(0.8)
        c = np.where(c < knee, c, knee + (1 - knee) * np.tanh((c - knee) / (1 - knee))).astype(np.float32)
        if self.p.look is not None and look > 0.0:
            c = _apply_table(c, self.p.look, look)
        gtm.prepare(h, w)
        return gtm.gain(c, h, w, 0, h * w, float(OPTIONS.hdr_strength)).reshape(h, w)
