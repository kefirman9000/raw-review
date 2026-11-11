"""
RAW Review - automatic corrections (no GUI).

Auto exposure, levels (blacks/whites), shadows/highlights, contrast, colour
(vibrance/saturation), sharpening and noise reduction, the straighten modes,
and the helpers the batch editor uses.

Every tool works the same way: it measures the photo as it currently looks
(white balance and exposure included), then proposes slider values that make
it look "normal". All values are plain Lightroom-style slider numbers, so you
can tweak them afterwards. `strength` scales how far an auto tool moves a
slider away from its default (1.0 = full, 0.5 = half, 1.5 = push harder).

None of this touches a RAW file: it only reads the decoded preview data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np

import raw_review_core as core

LUMA = core.LUMA

# (key, label). The order here is the order the tools run in, because later
# tools measure the photo after the earlier ones have been applied.
AUTO_GROUPS = [
    ("wb", "White balance"),
    ("level", "Straighten / level"),
    ("exposure", "Exposure"),
    ("levels", "Levels (blacks & whites)"),
    ("shhi", "Shadows & highlights"),
    ("contrast", "Contrast"),
    ("colour", "Colour (vibrance & saturation)"),
    ("masks", "Sky & subject masks (Lightroom AI)"),
    ("sharpen", "Sharpening"),
    ("noise", "Noise reduction"),
]
GROUP_ORDER = [k for k, _ in AUTO_GROUPS]
GROUP_LABEL = dict(AUTO_GROUPS)

# ---- tuning knobs (what "normal" means) -----------------------------------
TARGET_KEY = 0.15          # target log-average scene brightness (linear, ~mid grey)
# Same idea for DNGs that carry a local HDR tone map (Pixel). Google's own rendering of those
# files lands at about 0.20-0.24, so aiming there treats the camera's exposure as the intent
# and only corrects scenes it got clearly wrong. (Used whether or not the tone map is switched on
# in the preview. It is off by default, because Lightroom Classic 11 does not apply it; Auto exposure
# and Shadows then make up the difference.) Calibrated on Pixel 7 Pro files; nudge it with
# "Exposure bias" in the Auto menu if your photos come out consistently lighter or darker.
TARGET_KEY_HDR = 0.21
EXPOSURE_DAMPING = 0.75    # move only this far toward the target (keeps low/high-key scenes moody)
HIGHLIGHT_ALLOWANCE = 1.0  # the 99th percentile may reach this (linear, i.e. just white) and no more
TARGET_SPREAD = 0.72       # target p5..p95 tonal spread in display units
TARGET_SATURATION = 0.30   # target average saturation of coloured pixels
SHARP_RATIO_CRISP = 2.8    # edge-acuity ratio of a crisp photo (see core.measure_quality)
SHARP_RATIO_SOFT = 1.6     # ... and of a very soft one
NOISE_FLOOR = 0.005        # luminance noise below this needs no reduction
# Histogram centring (see center_exposure): after the tone tools have run, exposure is nudged
# until the finished photo's median brightness sits in the middle of the histogram.
BLACKS_PULL = 0.6          # Auto levels moves the black point this share of the way to true black
BLACKS_FLOOR = -40.0       # ...and never further than this
TARGET_MEDIAN = 0.50       # median luminance of the finished photo (display units, 0..1): the middle
CENTER_MAX_CLIPPED = 0.005 # Highlights is pulled down until no more than this share is blown
# ---------------------------------------------------------------------------


@dataclass
class AutoOptions:
    groups: set = field(default_factory=lambda: set(GROUP_ORDER))
    level_mode: str = "Auto (best guess)"
    allow_upright: bool = True
    strength: float = 1.0
    exposure_bias: float = 0.0       # EV added to auto exposure (your taste / calibration)
    use_existing: bool = True        # start from settings the photo already has (Lightroom), if any
    punch: float = 0.0               # -50..50  "My auto settings": more / less contrast than auto picks
    colour: float = 0.0              # -50..50  more / less colour (vibrance) than auto picks
    rules: dict = field(default_factory=dict)   # {setting: [mode, value]}  see apply_rules()
    subject_crop: bool = False       # crop slightly so the main subject (plane, car...) is centred


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _small(dec: core.Decoded, long_edge: int = 600) -> np.ndarray:
    cam = dec.cam
    h, w = cam.shape[:2]
    s = long_edge / max(h, w)
    if s < 1.0:
        cam = cv2.resize(cam, (max(1, round(w * s)), max(1, round(h * s))),
                         interpolation=cv2.INTER_AREA)
    return cam


def _mul(dec: core.Decoded, edit: core.Edit):
    if edit.wb_mode == "custom":
        return dec.color.temp_tint_to_mul(edit.temp, edit.tint)
    return dec.color.as_shot_mul


def _stage(dec: core.Decoded, edit: core.Edit, stage: str):
    """
    The photo (gamma RGB, luminance) after the tone steps up to `stage`:
    "base" = WB + exposure only, "levels", "shhi", or "final" = all tone
    sliders (colour sliders are never included).
    """
    hl = edit.highlights if stage in ("shhi", "final") else 0.0   # recovery is part of Highlights
    g = core.base_gamma(_small(dec), dec.color, _mul(dec, edit), edit.exposure, hl)
    if stage != "base":
        g = core.apply_tone(g, edit, None if stage == "final" else stage, dec.color.base_contrast)
    return g, g @ LUMA


def _scaled(value: float, default: float, strength: float) -> float:
    return default + (value - default) * strength


def replace_note(edit: core.Edit, prefix: str, note: str):
    edit.notes = [n for n in edit.notes if not n.startswith(prefix)] + [note]


def _set(edit: core.Edit, key: str, value):
    s = core.SETTINGS[key]
    value = min(max(value, s.lo), s.hi)
    setattr(edit, key, int(round(value)) if s.integer else round(float(value), s.decimals))
    if s.optional:
        edit.touched.add(key)


# --------------------------------------------------------------------------
# Exposure / levels / shadows+highlights / contrast
# --------------------------------------------------------------------------

def auto_exposure(dec, edit, strength=1.0, bias=0.0) -> str:
    """Exposure so the scene's overall brightness looks normal; bright highlights are protected."""
    cam, mul = _small(dec), _mul(dec, edit)
    lin = core._cam_to_linear_srgb(cam, mul, dec.color)
    Y = lin @ LUMA
    # A DNG with a local HDR tone map (Pixel) is brightened unevenly: shadows lifted, highlights
    # held. Judge the photo as it will actually look, or the exposure would be counted twice.
    model = dec.color.model
    target = TARGET_KEY
    if model is not None and model.p.gain_table is not None:
        target = TARGET_KEY_HDR              # the camera's own brightness is the aim, tone map on or off
        if model.has_hdr_tone_map:
            gain = model.local_gain(cam, mul, 0.0, dec.color.base_ev)
            if gain is not None:
                Y = Y * gain
    Y = Y.ravel()
    Y = Y[Y > 1e-5]
    if Y.size < 100:
        return "Auto exposure: image too dark to measure"
    lo, hi = np.percentile(Y, [5, 98])
    core_px = Y[(Y >= lo) & (Y <= hi)]
    key = float(np.exp(np.mean(np.log(core_px))))
    ev = EXPOSURE_DAMPING * (math.log2(target / key) - dec.color.base_ev)
    guard = math.log2(HIGHLIGHT_ALLOWANCE / max(float(np.percentile(Y, 99)), 1e-4)) - dec.color.base_ev
    limited = ev > guard
    ev = min(ev, guard)
    ev = min(max(ev, -2.5), 3.0) * strength + bias
    ev = round(round(ev / 0.05) * 0.05, 2)
    edit.exposure = float(min(max(ev, -5.0), 5.0))
    return (f"Auto exposure: {edit.exposure:+.2f} EV"
            + (" (held back to protect highlights)" if limited else ""))


def auto_levels(dec, edit, strength=1.0) -> str:
    """Whites/Blacks so the darkest and brightest real tones sit just inside the range."""
    _g, L = _stage(dec, edit, "base")
    pb, pw = (float(v) for v in np.percentile(L, [0.3, 99.7]))
    if pw - pb < 0.05:
        return "Auto levels: image has almost no tonal range, left alone"
    full = (pw - pb) / 0.97
    lo = pb - 0.015 * full
    hi = lo + full
    # Only partway, and never past BLACKS_FLOOR: a hazy or daylight scene has no true black,
    # and forcing one crushed shadow detail (e.g. treelines went to -70 and beyond).
    blacks = min(max(-lo / 0.15 * 100.0 * BLACKS_PULL, BLACKS_FLOOR), 15.0)
    whites = min(max((1.0 - hi) / 0.30 * 100.0, -60.0), 10.0)   # never push whites into clipping
    _set(edit, "blacks", _scaled(blacks, 0, strength))
    _set(edit, "whites", _scaled(whites, 0, strength))
    return f"Auto levels: blacks {edit.blacks:+d}, whites {edit.whites:+d}"


def auto_shadows_highlights(dec, edit, strength=1.0) -> str:
    """Open up deep shadows and pull back bright areas, only as much as the photo needs."""
    _g, L = _stage(dec, edit, "levels")
    p15, p50, p92 = (float(v) for v in np.percentile(L, [15, 50, 92]))
    shadows = min(max((0.16 - p15) / 0.16 * 60.0, 0.0), 60.0)
    if p50 < 0.25:                       # a genuinely dark scene (night): keep it moody
        shadows *= 0.5
    highlights = -min(max((p92 - 0.68) / 0.25 * 80.0, 0.0), 80.0)
    clipped = float((L > 0.97).mean())           # share of near-white pixels
    highlights -= min(clipped * 500.0, 30.0)
    highlights = max(highlights, -100.0)
    _set(edit, "shadows", _scaled(shadows, 0, strength))
    _set(edit, "highlights", _scaled(highlights, 0, strength))
    return f"Auto shadows/highlights: shadows {edit.shadows:+d}, highlights {edit.highlights:+d}"


def auto_contrast(dec, edit, strength=1.0) -> str:
    """Contrast from how much of the tonal range the photo actually uses."""
    _g, L = _stage(dec, edit, "shhi")
    p5, p95 = (float(v) for v in np.percentile(L, [5, 95]))
    contrast = min(max((TARGET_SPREAD - (p95 - p5)) / TARGET_SPREAD * 80.0, -15.0), 25.0)
    _set(edit, "contrast", _scaled(contrast, 0, strength))
    return f"Auto contrast: {edit.contrast:+d}"


# --------------------------------------------------------------------------
# Colour
# --------------------------------------------------------------------------

def auto_colour(dec, edit, strength=1.0) -> str:
    """Vibrance up for flat colours, saturation down for over-cooked ones."""
    g, L = _stage(dec, edit, "final")
    mx, mn = g.max(axis=2), g.min(axis=2)
    sat = (mx - mn) / np.maximum(mx, 1e-4)
    m = (L > 0.08) & (L < 0.92)
    if m.sum() < 100:
        return "Auto colour: not enough midtones to judge"
    s = sat[m]
    if float((s > 0.08).mean()) < 0.10:            # basically black & white / neutral scene
        _set(edit, "vibrance", 0)
        _set(edit, "saturation", 0)
        return "Auto colour: scene is nearly neutral, left alone"
    mean_sat = float(s.mean())
    vib = min(max((TARGET_SATURATION - mean_sat) / TARGET_SATURATION * 50.0, -20.0), 35.0)
    sat_adj = -min(max((mean_sat - 0.50) * 60.0, 0.0), 20.0)
    _set(edit, "vibrance", _scaled(vib, 0, strength))
    _set(edit, "saturation", _scaled(sat_adj, 0, strength))
    return f"Auto colour: vibrance {edit.vibrance:+d}, saturation {edit.saturation:+d}"


# --------------------------------------------------------------------------
# Sharpening / noise reduction
# --------------------------------------------------------------------------

def auto_sharpen(dec, edit, strength=1.0) -> str:
    """Sharpening from measured softness; masking rises with noise so grain isn't sharpened."""
    q = dec.quality
    if q.measured and q.sharp_ratio > 0:
        soft = min(max((SHARP_RATIO_CRISP - q.sharp_ratio)
                       / (SHARP_RATIO_CRISP - SHARP_RATIO_SOFT), 0.0), 1.0)
    else:
        soft = 0.25
    nn = min(max((q.noise_luma - 0.006) / 0.03, 0.0), 1.0) if q.measured else 0.0
    amount = (40 + 50 * soft) * (1 - 0.4 * nn)
    _set(edit, "sharp_amount", min(max(_scaled(amount, 40, strength), 15), 120))
    _set(edit, "sharp_radius", _scaled(1.0 + 0.4 * soft, 1.0, strength))
    _set(edit, "sharp_detail", _scaled(25 + 20 * soft * (1 - nn), 25, strength))
    _set(edit, "sharp_masking", _scaled(80 * nn, 0, strength))
    why = "measured" if q.measured else "not measured, typical values"
    return (f"Auto sharpening: amount {edit.sharp_amount}, radius {edit.sharp_radius:.1f}, "
            f"detail {edit.sharp_detail}, masking {edit.sharp_masking} ({why})")


def auto_noise(dec, edit, strength=1.0) -> str:
    """Luminance and colour noise reduction from the noise measured in flat mid-tone areas."""
    q = dec.quality
    if not q.measured:
        _set(edit, "nr_luma", 0)
        _set(edit, "nr_luma_detail", 50)
        _set(edit, "nr_color", 25)
        return "Auto noise reduction: noise not measured, defaults used"
    lum = min(max(2000.0 * (q.noise_luma - NOISE_FLOOR), 0.0), 75.0)
    color = min(max(25.0 + 800.0 * (q.noise_chroma - 0.006), 25.0), 75.0)
    detail = min(max(50.0 - 0.2 * lum, 25.0), 50.0)
    _set(edit, "nr_luma", _scaled(lum, 0, strength))
    _set(edit, "nr_luma_detail", detail)
    _set(edit, "nr_color", _scaled(color, 25, strength))
    return (f"Auto noise reduction: luminance {edit.nr_luma}, color {edit.nr_color} "
            f"(measured noise {q.noise_luma * 100:.1f}%)")


# --------------------------------------------------------------------------
# Straighten / level modes
# --------------------------------------------------------------------------

def apply_level_mode(dec, edit, mode: str, allow_upright: bool = True):
    """Run one of core.LEVEL_MODES on this photo. Returns a StraightenResult for rotate modes."""
    spec = core.LEVEL_MODES.get(mode, core.LEVEL_MODES["Off"])
    kind = spec["kind"]
    if kind == "manual":
        return None
    edit.level_mode = mode
    was_upright = edit.upright != "Off"
    edit.upright_matrices, edit.upright_guides = None, None
    if was_upright and edit.constrain_crop:
        edit.crop_box = None                        # that crop was for the old correction
    if kind == "off":
        edit.angle, edit.upright = 0.0, "Off"
        replace_note(edit, "Straighten", "Straighten: off")
        return None
    plain = edit.copy()
    plain.upright, plain.crop_box = "Off", None
    img, _ = core.render(dec, plain, rotate=False, look=False)
    if kind == "upright":
        edit.angle, edit.upright = 0.0, spec["upright"]
        if spec.get("guides"):
            edit.upright_guides = core.suggest_guides(img, spec["guides"])
        note = solve_upright(edit, img)
        replace_note(edit, "Straighten", note)
        return None
    st = core.auto_straighten(img, allow_upright, spec["source"])
    edit.angle, edit.upright = st.angle, st.upright
    if st.upright != "Off":                         # best guess chose Upright Vertical
        edit.level_mode = "Upright: " + st.upright
        solve_upright(edit, img)
    replace_note(edit, "Straighten", st.note)
    return st


def solve_upright(edit, img) -> str:
    """Solve every Upright mode on the (unwarped) photo, keep them on the edit, and constrain the crop."""
    edit.upright_matrices = core.upright_all(img, edit.upright_guides)
    H = edit.upright_matrices.get(edit.upright)
    h, w = img.shape[:2]
    if edit.constrain_crop:
        edit.crop_box = core.constrained_box(H, w, h)
    name = f"Upright {edit.upright}"
    if edit.upright == "Guided":
        n = len(edit.upright_guides or [])
        if n < 2:
            return f"Straighten: {name} – not enough clear lines to use as guides"
        name += f" ({n} guides suggested)"
    if H is None:
        return f"Straighten: {name} – nothing to correct"
    return f"Straighten: {name} (RAW Review's correction; Lightroom shows the same)" + (
        ", crop constrained" if edit.crop_box and edit.constrain_crop else "")


def apply_subject_crop(dec, edit) -> str:
    """Minimal same-aspect crop that centres the main subject. Sets edit.crop_box (or clears it)."""
    plain = edit.copy()
    plain.crop_box = None
    img, _ = core.render(dec, plain, look=False)              # straightened, not yet subject-cropped
    subj, conf = core.find_subject_box(img)
    edit.crop_box = None
    if subj is None or conf < core.SUBJECT_CROP_MIN_CONF:
        note = "Subject crop: no clear subject found, not cropped"
    else:
        box = core.subject_crop_box(subj)
        if box is None:
            note = "Subject crop: subject already centred (or fills the frame), not cropped"
        else:
            edit.crop_box = box
            note = f"Subject crop: kept {100 * (box[2] - box[0]):.0f}% of the frame to centre the subject"
    replace_note(edit, "Subject crop", note)
    return note


# --------------------------------------------------------------------------
# Running the tools
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Sky / subject masks (Lightroom computes the exact masks; we decide whether and how much)
# --------------------------------------------------------------------------
SKY_MIN_FRACTION = 0.08    # sky must cover at least this much of the frame
SKY_MIN_CONFIDENCE = 0.45
SUBJECT_MIN_CONFIDENCE = 0.6


def _round_setting(key, val):
    st = core.SETTINGS[key]
    val = min(max(val, st.lo), st.hi)
    return int(round(val)) if st.integer else round(val, st.decimals)


def auto_masks(dec, edit, strength=1.0) -> str:
    """Turn on a Select Sky / Select Subject adjustment when the photo has a sky / a clear subject."""
    probe = edit.copy()
    probe.sky_on = probe.subj_on = False
    img, _ = core.render(dec, probe, rotate=False, look=False)
    L = (img.astype(np.float32) / 255.0) @ LUMA
    notes = []

    sm, sf, sc = core.estimate_sky(dec)
    if sf >= SKY_MIN_FRACTION and sc >= SKY_MIN_CONFIDENCE:
        sel = sm > 0.5
        sky_l = float(L[sel].mean())
        p95 = float(np.percentile(L[sel], 95))
        px = img[sel].astype(np.float32)
        blue = float(((px[:, 2] > px[:, 1] + 6) & (px[:, 2] > px[:, 0] + 12)).mean())
        edit.sky_on = True
        edit.sky_exposure = _round_setting("sky_exposure", min(0.0, -(sky_l - 0.62) * 1.2) * strength)
        edit.sky_highlights = _round_setting("sky_highlights", -min(45.0, max(0.0, (p95 - 0.82) * 220)) * strength)
        edit.sky_dehaze = _round_setting("sky_dehaze", 10 * strength)
        edit.sky_saturation = _round_setting("sky_saturation", (12 if blue > 0.4 else 0) * strength)
        edit.sky_contrast = _round_setting("sky_contrast", (5 if blue > 0.4 else 15) * strength)  # grey skies need contrast
        edit.sky_temp = 0
        notes.append(f"sky {sf:.0%} of frame ({'blue' if blue > 0.4 else 'cloudy'})")
    else:
        edit.sky_on = False

    mm, mf, mc = core.estimate_subject(dec)
    if mc >= SUBJECT_MIN_CONFIDENCE and 0.03 < mf < 0.6:
        sel = mm > 0.5
        subj_l, rest_l = float(L[sel].mean()), float(L[~sel].mean())
        edit.subj_on = True
        edit.subj_exposure = _round_setting("subj_exposure", min(0.35, max(0.0, (rest_l - subj_l) * 0.9 + 0.05)) * strength)
        edit.subj_shadows = _round_setting("subj_shadows", (12 if subj_l < 0.35 else 0) * strength)
        edit.subj_texture = _round_setting("subj_texture", 10 * strength)
        edit.subj_clarity = _round_setting("subj_clarity", 5 * strength)
        edit.subj_contrast = 0
        edit.subj_saturation = 0
        notes.append("clear subject")
    else:
        edit.subj_on = False

    if not notes:
        return "Auto masks: no sky or clear subject found, none added"
    return ("Auto masks: " + ", ".join(notes)
            + " (Lightroom computes the exact masks; use Update AI Settings if it asks)")


# --------------------------------------------------------------------------
# "My auto settings": the user's own rules, applied after the automatic tools
# --------------------------------------------------------------------------
RULE_MODES = ["auto", "set", "add", "keep"]     # app decides / always this value / auto + value / don't touch
FLAG_RULES = {"lens_profile": "Lens corrections", "sky_on": "Sky mask", "subj_on": "Subject mask"}
FLAG_MODES = ["auto", "on", "off"]


def apply_rules(edit, base, rules) -> int:
    """
    Apply the user's rules to an auto edit. `base` is the edit before the auto tools ran (As Shot for a
    new photo), used by "keep" (don't touch). Returns how many rules changed something.
    """
    n = 0
    for key, rule in (rules or {}).items():
        try:
            mode, value = rule[0], float(rule[1]) if len(rule) > 1 else 0.0
        except (TypeError, ValueError, IndexError):
            continue
        if key in FLAG_RULES:
            if mode in ("on", "off"):
                want = mode == "on"
                if getattr(edit, key) != want:
                    n += 1
                setattr(edit, key, want)
            continue
        st = core.SETTINGS.get(key)
        if st is None or mode not in ("set", "add", "keep"):
            continue
        before = getattr(edit, key)
        if mode == "set":
            v = value
        elif mode == "add":
            v = getattr(edit, key) + value
        else:
            v = getattr(base, key)
        v = min(max(v, st.lo), st.hi)
        v = int(round(v)) if st.integer else round(v, st.decimals)
        setattr(edit, key, v)
        if st.optional:
            if mode == "keep" and key not in base.touched:
                edit.touched.discard(key)
            elif mode != "keep":
                edit.touched.add(key)
        if key in ("temp", "tint"):
            if mode == "keep":
                edit.wb_mode = base.wb_mode
                edit.temp, edit.tint = base.temp, base.tint
            else:
                edit.wb_mode = "custom"
        if key == "angle":
            if mode == "keep":
                edit.level_mode, edit.upright = base.level_mode, base.upright
            else:
                edit.level_mode = "Manual"
        if getattr(edit, key) != before:
            n += 1
    return n


def active_rules(rules) -> dict:
    return {k: r for k, r in (rules or {}).items() if r and r[0] not in ("auto",)}


def _finished_luma(dec, edit) -> np.ndarray:
    """Luminance of the photo with exposure and every tone slider applied (what the histogram shows)."""
    return _stage(dec, edit, "final")[1]


def _blown_share(dec, edit) -> float:
    """
    Share of the photo the preview shows as blown (the histogram's red warning): any channel,
    after tone and colour, with the preview's rounding. On a white subject in warm light one
    channel blows well before overall brightness does, and Vibrance can push it over too.
    """
    g = core.apply_colour(_stage(dec, edit, "final")[0], edit)
    mx = np.maximum(np.maximum(g[..., 0], g[..., 1]), g[..., 2])
    return float((mx >= (core.BLOWN_LEVEL - 0.5) / 255.0).mean())


def center_exposure(dec, edit, strength=1.0, bias=0.0) -> str:
    """
    Put the finished photo's histogram in the middle, the way you'd do it by hand:
    expose for the midtones, then crush Highlights as far as needed to bring the bright
    areas back under white, and lift the tone curve's Lights a little when Highlights had
    to go hard, so the upper midtones don't go flat. Auto exposure alone judged the raw
    data before the tone tools and held back for bright spots, so photos came out dark.
    Tone tools are not re-run here (they would fight the change).
    """
    s_exp, s_hl, s_lights = edit.exposure, edit.highlights, edit.lights

    def clipped(e):
        return _blown_share(dec, e)

    def seek():
        for _ in range(8):
            med = float(np.median(_finished_luma(dec, edit)))
            err = math.log2(TARGET_MEDIAN / max(med, 1e-3))
            if abs(err) < 0.03:
                break
            step = min(max(1.8 * err, -1.5), 1.5)    # display brightness ~ exposure^(1/2.2)
            new = min(max(edit.exposure + step, -2.5), 3.5)
            if abs(new - edit.exposure) < 0.02:
                break
            edit.exposure = new

    def recover():
        """Least-negative Highlights that keeps no more than CENTER_MAX_CLIPPED blown."""
        if clipped(edit) <= CENTER_MAX_CLIPPED:
            return
        trial = edit.copy()
        trial.highlights = -100
        if clipped(trial) > CENTER_MAX_CLIPPED:      # truly blown (lights, sun): crush fully
            edit.highlights = -100
            return
        lo, hi = -100, min(edit.highlights, 0)
        for _ in range(7):
            trial.highlights = (lo + hi) / 2
            lo, hi = (trial.highlights, hi) if clipped(trial) <= CENTER_MAX_CLIPPED else (lo, trial.highlights)
        edit.highlights = int(math.floor(lo))

    for _ in range(3):
        seek()
        recover()
        crush = max(-edit.highlights, 0)                # how hard Highlights is pulled down
        if crush > 30:
            edit.lights = int(min(round((crush - 30) * 0.5), 35))
    seek()

    def blend(v0, v1):
        return v0 + (v1 - v0) * strength
    ev = blend(s_exp, edit.exposure) + bias
    edit.exposure = float(min(max(round(round(ev / 0.05) * 0.05, 2), -5.0), 5.0))
    _set(edit, "highlights", blend(s_hl, edit.highlights))
    if edit.lights != s_lights:
        _set(edit, "lights", blend(s_lights, edit.lights))
    note = f"Auto exposure: {edit.exposure:+.2f} EV (histogram centred"
    if edit.highlights < 0:
        note += f"; highlights {edit.highlights:+d} to recover bright areas"
    if edit.lights != s_lights:
        note += f", tone curve lights {edit.lights:+d}"
    return note + ")"


def run_auto(dec, edit, groups, opts: AutoOptions | None = None, user_rules: bool = False) -> dict:
    """Run the chosen auto groups (in the right order) on `edit`. Returns {"wb": .., "level": ..}.
    user_rules=True also applies "My auto settings" (new photos, Auto-enhance, re-apply)."""
    opts = opts or AutoOptions()
    groups = set(groups)
    s = opts.strength
    out = {}
    base = edit.copy()
    centre_at = GROUP_ORDER.index("colour")      # after tone and colour (vibrance can blow a channel)
    centred = "exposure" not in groups

    def centre():
        replace_note(edit, "Auto exposure",
                     center_exposure(dec, edit, s, opts.exposure_bias))

    for i, g in enumerate(GROUP_ORDER):
        if not centred and i > centre_at:            # tone tools done: centre the histogram
            centre()
            centred = True
        if g not in groups:
            continue
        if g == "wb":
            wb = core.auto_white_balance(dec)
            if wb.applied:
                edit.wb_mode, edit.temp, edit.tint = "custom", round(wb.temp), round(wb.tint)
            replace_note(edit, "Auto WB", wb.note)
            out["wb"] = wb
        elif g == "level":
            out["level"] = apply_level_mode(dec, edit, opts.level_mode, opts.allow_upright)
        elif g == "exposure":
            # first estimate; center_exposure() refines it and adds the bias
            replace_note(edit, "Auto exposure", auto_exposure(dec, edit, s, 0.0))
        elif g == "levels":
            replace_note(edit, "Auto levels", auto_levels(dec, edit, s))
        elif g == "shhi":
            replace_note(edit, "Auto shadows", auto_shadows_highlights(dec, edit, s))
        elif g == "contrast":
            replace_note(edit, "Auto contrast", auto_contrast(dec, edit, s))
            if opts.punch:
                _set(edit, "contrast", edit.contrast + 0.4 * opts.punch)
        elif g == "colour":
            replace_note(edit, "Auto colour", auto_colour(dec, edit, s))
            if opts.colour:
                _set(edit, "vibrance", edit.vibrance + 0.5 * opts.colour)
        elif g == "masks":
            replace_note(edit, "Auto masks", auto_masks(dec, edit, s))
        elif g == "sharpen":
            replace_note(edit, "Auto sharpening", auto_sharpen(dec, edit, s))
        elif g == "noise":
            replace_note(edit, "Auto noise", auto_noise(dec, edit, s))
    if not centred:
        centre()
    # Subject crop runs with full proposals (new photos, Auto-enhance) and whenever straightening is
    # re-run, because it is measured on the straightened frame.
    if opts.subject_crop and (user_rules or "level" in groups):
        out["crop"] = apply_subject_crop(dec, edit)
    if user_rules:
        rules = active_rules(opts.rules)
        if rules:
            changed = apply_rules(edit, base, rules)
            replace_note(edit, "My auto settings", f"My auto settings: {len(rules)} rule(s) applied"
                         + (f", {changed} changed the auto edit" if changed else ""))
    return out


def propose_edit(dec, opts: AutoOptions | None = None):
    """The first proposal for a freshly added photo. Returns (edit, wb result, straighten result)."""
    opts = opts or AutoOptions()
    edit = core.as_shot_edit(dec)
    res = run_auto(dec, edit, opts.groups, opts, user_rules=True)
    return edit, res.get("wb"), res.get("level")


# --------------------------------------------------------------------------
# Batch editing
# --------------------------------------------------------------------------

@dataclass
class BatchSpec:
    scope: str = "checked"                     # "current" | "selected" | "checked" | "all"
    groups: list = field(default_factory=list)  # auto groups to run
    level_mode: str = "Auto (best guess)"
    allow_upright: bool = True
    strength: float = 1.0
    exposure_bias: float = 0.0
    changes: list = field(default_factory=list)  # (setting key, action, amount)
    punch: float = 0.0
    colour: float = 0.0
    rules: dict = field(default_factory=dict)
    user_rules: bool = False                     # also apply "My auto settings"
    subject_crop: bool = False
    fresh: bool = False                          # reset to As Shot before running the tools

    @property
    def needs_decode(self) -> bool:
        return bool(self.groups) or self.subject_crop or self.fresh


# The settings each automatic tool decides (exposure centring also sets Highlights and Lights).
# Used to remember "where auto put it" so the panel can mark what you changed afterwards.
GROUP_KEYS = {
    "wb": ("temp", "tint"),
    "level": ("angle",),
    "exposure": ("exposure", "highlights", "lights"),
    "levels": ("blacks", "whites"),
    "shhi": ("shadows", "highlights"),
    "contrast": ("contrast",),
    "colour": ("vibrance", "saturation"),
    "masks": tuple(k for k in core.SETTINGS if k.startswith(("sky_", "subj_"))),
    "sharpen": tuple(k for k in core.SETTINGS if k.startswith("sharp_")),
    "noise": tuple(k for k in core.SETTINGS if k.startswith("nr_")),
}


def auto_values(edit, groups) -> dict:
    """The values of the settings these auto tools decide, as they are in `edit` right now."""
    keys = {k for g in groups for k in GROUP_KEYS.get(g, ())}
    return {k: getattr(edit, k) for k in keys}


def apply_batch(dec, edit, spec: BatchSpec) -> dict:
    """Apply a BatchSpec to one photo's edit: auto tools first, then manual changes."""
    out = {}
    if spec.fresh:
        start = core.as_shot_edit(dec)
        for f in edit.__dataclass_fields__:
            setattr(edit, f, getattr(start, f))
    if spec.groups:
        out = run_auto(dec, edit, spec.groups,
                       AutoOptions(groups=set(spec.groups), level_mode=spec.level_mode,
                                   allow_upright=spec.allow_upright, strength=spec.strength,
                                   exposure_bias=spec.exposure_bias, punch=spec.punch, colour=spec.colour,
                                   rules=spec.rules, subject_crop=spec.subject_crop),
                       user_rules=spec.user_rules)
    if spec.subject_crop and "crop" not in out:          # asked for on its own, without straighten
        out["crop"] = apply_subject_crop(dec, edit)
    if spec.groups:
        out["auto_values"] = auto_values(edit, spec.groups)   # before the manual changes below
    apply_changes(edit, spec.changes)
    return out


def apply_changes(edit, changes):
    for key, action, amount in changes:
        core.change_setting(edit, key, action, amount)
