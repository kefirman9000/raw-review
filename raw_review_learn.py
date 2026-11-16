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
    return rows


def apply_to_options(opts, rows: list[dict], keys: set) -> list[str]:
    """Fold the chosen suggestions into My auto settings (AutoOptions). Returns what changed."""
    done = []
    rules = dict(opts.rules or {})
    for r in rows:
        k, m = r["key"], r["mean"]
        if k not in keys:
            continue
        if k == "exposure":
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
