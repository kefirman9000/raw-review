# RAW Review

**Batch auto-edit RAW photos, review each one, and send the approved edits to Lightroom without ever touching the RAW files.**

RAW Review analyzes every photo you drop into it and proposes an edit: a smart automatic
white balance and an automatic straighten. You flip through them, **approve** (`A`),
**tweak** with sliders, or **reject** (`X`). Approved edits are saved as standard Lightroom
`.xmp` sidecar files, so Lightroom Classic picks them up as normal, fully editable sliders.

**[⬇ Download (ZIP)](https://github.com/kefirman9000/raw-review/archive/refs/heads/main.zip)** · [What's new](CHANGELOG.md)

![RAW Review interface: photo list with review status, preview, and white balance, tone and geometry controls](docs/app.png)

![Before and after: a warm tungsten cast removed with one eyedropper click on the white spacecraft](docs/before-after.png)

<sub>Interface illustration and before/after rendered with RAW Review's own colour engine. Photo: NASA, International
Space Station (public domain). A tungsten-light cast was simulated, then removed with one eyedropper click (2998K / +12; the true cast was 3000K / +12).</sub>

## Features

- **Drag and drop** photos or whole folders (subfolders included).
- **Smart auto white balance**
  - Looks for the surfaces most likely to be truly neutral: the brightest unclipped *near-whites* and the darkest *near-blacks*.
  - Skips clipped highlights and textured areas, and cross-checks the whites against the blacks.
  - Rejects physically implausible light, so a blue shirt under tungsten won't fool it.
  - Low-confidence results are flagged **"check WB"**, and you can show exactly which spots it used.
- **Eyedropper** WB: click anything that should be white, grey or black.
- **Auto straighten** from verticals (buildings, door frames) or the horizon. When verticals converge strongly, it sets Lightroom's **Upright: Vertical** instead.
- **Sliders:** Temperature, Tint, Exposure, Contrast, Highlights, Shadows, Vibrance, Straighten, Upright mode, and lens profile corrections.
- **Before/after** toggle, copy/paste settings to many photos, and "Approve all".
- Temperature/Tint use the same conversion as Adobe's DNG SDK, so the numbers line up with Lightroom's sliders.
- **Built-in updater:** checks GitHub for new releases and installs only when you click *Install*.

## Your RAW files are never touched

- RAWs are **opened read-only**. Nothing is ever written to them, and they are never moved, renamed or deleted.
- The only file written is `photo.xmp` next to `photo.CR3` (Lightroom's normal sidecar).
- An existing sidecar is backed up once as `photo.xmp.bak`. Only the settings this app controls are changed; ratings, keywords, Clarity, masks etc. are kept.
- Every write checks the RAW's size and timestamp before and after, and refuses any filename that looks like a photo (RAW/DNG/JPG/TIFF…).
- **DNG files are skipped**, because Lightroom stores DNG edits *inside* the DNG.

## Install (Windows)

1. Install **Python 3.12** from [python.org](https://www.python.org/downloads/).
2. [Download the ZIP](https://github.com/kefirman9000/raw-review/archive/refs/heads/main.zip) (or *Code → Download ZIP* above) and unzip it.
3. Double-click **`RAW Review.bat`**. The first run sets up its own environment (a few minutes).

Other systems: `pip install -r requirements.txt`, then `python raw_review_app.py`.

## Keys

| Key | Action |
|---|---|
| `A` | Approve: write the XMP, go to the next unreviewed photo |
| `X` | Reject: nothing is written |
| `←` `→` | Previous / next photo |
| `\` | Before / after |
| `W` | Re-run auto WB |
| `Ctrl+C` / `Ctrl+V` | Copy settings / paste to the photos selected in the list |

## Getting the edits into Lightroom Classic

- **New photos:** just import them. Lightroom reads the sidecars automatically.
- **Photos already in your catalog:** select them, then *Metadata → Read Metadata from Files*.
- If *Automatically write changes into XMP* is on in Lightroom, it can overwrite sidecars with its own edits. Read the metadata in first.

## Updating

Click **Check for Updates** in the toolbar. It compares your version with the newest one on GitHub (a published Release, or the main branch). The app also checks quietly at startup and shows an
**⬆ Update** button when a new version is out. Nothing is installed until you click *Install & Restart*.
The previous version is kept in `.update_backup/`. The updater only replaces the app's own files and
never looks at your photos.

## Good to know

- The preview approximates Lightroom's rendering. What counts is the values written to the sidecar.
- Upright is calculated by Lightroom itself, so it isn't shown in the preview.
- If straightened photos tilt the wrong way in Lightroom, change `XMP_ANGLE_SIGN` at the top of `raw_review_core.py`.

## Built with

[rawpy](https://github.com/letmaik/rawpy) / LibRaw (read-only RAW decoding), NumPy, OpenCV, PySide6.
