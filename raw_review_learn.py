"""
Learning from your Lightroom corrections.

RAW Review remembers what it wrote to each photo's sidecar. When a photo comes back and its
Lightroom settings differ, the difference is your correction. Corrections that are consistent
(several photos, mostly the same direction, a meaningful amount) become suggestions for
"My auto settings": Brightness, Punch, Colour, and an "add" rule for the other settings.
Pure functions; the app stores the records (in its settings folder, never in your photos).
"""
from __future__ import annotations

import numpy as np

# Settings RAW Review learns from, and the smallest average change worth suggesting
LEARN_KEYS = {
    "exposure": 0.1, "contrast": 5, "highlights": 8, "shadows": 8, "whites": 5, "blacks": 5,
    "vibrance": 5, "saturation": 5, "temp": 150, "tint": 4,
}
MIN_PHOTOS = 3             # corrected photos needed before suggesting anything
MIN_AGREE = 0.7            # share of them that must change the setting in the same direction
# Bright / dark scenes: if your Exposure corrections line up with how high- or low-key the scene was
# (auto.scene_key), the exposure target follows the scene's key too much or too little. Fitted as
# correction = a + b * key; b is added to auto.KEY_FOLLOW, a becomes the plain Brightness.
KEY_MIN_PHOTOS = 5
KEY_MIN_SPAN = 1.0         # stops of scene key the photos must cover
KEY_MIN_SLOPE = 0.08       # EV per stop of key worth changing
KEY_MIN_CORR = 0.5         # how well the line must explain the corrections
LABELS = {"exposure": "Exposure", "contrast": "Contrast", "highlights": "Highlights", "shadows": "Shadows",
          "whites": "Whites", "blacks": "Blacks", "vibrance": "Vibrance", "saturation": "Saturation",
          "temp": "Temperature (K)", "tint": "Tint"}


def snapshot(edit) -> dict:
    """The learnable settings of an edit, as written to Lightroom."""
    out = {}
    for k in LEARN_KEYS:
        v = getattr(edit, k, None)
        if v is not None:
            out[k] = float(v)
    return out


def correction(written: dict, now) -> dict:
    """What you changed in Lightroom: {setting: Lightroom's value - RAW Review's value}."""
    diffs = {}
    for k, v in written.items():
        cur = getattr(now, k, None)
        if cur is None or k not in LEARN_KEYS:
            continue
        d = float(cur) - float(v)
        if abs(d) >= (0.04 if k == "exposure" else 1.0):         # rounding is not a correction
            diffs[k] = round(d, 3)
    return diffs


def summarize(records: list[dict], camera: str | None = None) -> list[dict]:
    """
    records: [{"camera": str, "diffs": {setting: delta}}], one per photo you corrected.
    Returns one row per setting: photos, changed, mean (over the photos that changed it),
    agree (share in the majority direction), suggest (bool).
    """
    recs = [r for r in records if camera is None or r.get("camera") == camera]
    rows = []
    for k, floor in LEARN_KEYS.items():
        vals = [r["diffs"][k] for r in recs if k in r.get("diffs", {})]
        if not vals:
            continue
        a = np.array(vals, float)
        pos, neg = int((a > 0).sum()), int((a < 0).sum())
        major = a[a > 0] if pos >= neg else a[a < 0]
        agree = len(major) / len(a)
        mean = float(np.median(major)) if len(major) else 0.0
        rows.append({"key": k, "label": LABELS[k], "photos": len(recs), "changed": len(a),
                     "mean": mean, "agree": agree,
                     "suggest": len(a) >= MIN_PHOTOS and agree >= MIN_AGREE and abs(mean) >= floor})
    kr = key_row(recs)
    if kr is not None:
        rows.append(kr)
    return rows


def key_row(recs: list[dict]) -> dict | None:
    """The bright/dark-scene row: do your Exposure corrections depend on the scene's key?
    Uses every corrected photo that has a scene key (no Exposure change counts as 0)."""
    pts = [(float(r["key"]), float(r.get("diffs", {}).get("exposure", 0.0)))
           for r in recs if r.get("key") is not None]
    if len(pts) < 3:
        return None
    x, y = (np.array(v, float) for v in zip(*pts))
    if float(x.max() - x.min()) < 0.3 or float(np.std(y)) < 1e-6:
        return None
    b, a = np.polyfit(x, y, 1)
    r = float(np.corrcoef(x, y)[0, 1])
    agree = r * r
    return {"key": "key_follow", "label": "Exposure in bright / dark scenes", "photos": len(recs),
            "changed": len(pts), "mean": float(b), "intercept": float(a), "agree": agree,
            "suggest": (len(pts) >= KEY_MIN_PHOTOS and float(x.max() - x.min()) >= KEY_MIN_SPAN
                        and abs(b) >= KEY_MIN_SLOPE and abs(r) >= KEY_MIN_CORR)}


def apply_to_options(opts, rows: list[dict], keys: set, camera: str | None = None,
                     tuning: dict | None = None) -> list[str]:
    """Fold the chosen suggestions into My auto settings (AutoOptions). Returns what changed.
    camera: the corrections came from this camera alone, so an Exposure correction goes to that
    camera's own bias (tuning["camera_bias"]) instead of Brightness for every camera.
    tuning: {"camera_bias": {camera: EV}, "key_follow": float}, updated in place."""
    done = []
    rules = dict(opts.rules or {})
    tuning = tuning if tuning is not None else {}
    kr = next((r for r in rows if r["key"] == "key_follow"), None)
    for r in rows:
        k, m = r["key"], r["mean"]
        if k not in keys:
            continue
        if k == "key_follow":
            kf = float(np.clip(tuning.get("key_follow", 0.35) + m, 0.0, 0.8))
            tuning["key_follow"] = round(kf, 2)
            done.append(f"Bright / dark scenes: exposure follows the scene {tuning['key_follow']:.2f} EV per stop")
        elif k == "exposure":
            if kr is not None and "key_follow" in keys:      # the line's intercept, not the plain median
                m = kr["intercept"]
            if camera:
                cb = tuning.setdefault("camera_bias", {})
                cb[camera] = round(round((cb.get(camera, 0.0) + m) / 0.05) * 0.05, 2)
                done.append(f"Brightness for {camera} {cb[camera]:+.2f} EV")
            else:
                opts.exposure_bias = round(round((opts.exposure_bias + m) / 0.05) * 0.05, 2)
                done.append(f"Brightness {opts.exposure_bias:+.2f} EV")
        elif k == "contrast":
            opts.punch = float(np.clip(round(opts.punch + m / 0.4), -50, 50))     # punch adds 0.4 contrast per step
            done.append(f"Punch {opts.punch:+.0f}")
        elif k == "vibrance":
            opts.colour = float(np.clip(round(opts.colour + m / 0.5), -50, 50))   # colour adds 0.5 vibrance per step
            done.append(f"Colour {opts.colour:+.0f}")
        else:
            old = rules.get(k)
            base = float(old[1]) if old and old[0] == "add" and len(old) > 1 else 0.0
            step = round(m) if k != "temp" else int(round(m / 50.0) * 50)
            rules[k] = ["add", base + step]
            done.append(f"{LABELS[k]}: add {base + step:+g}")
    opts.rules = rules
    return done
