# Changelog

## v1.2.2

- **In-app updater is back.** A *Check for Updates* button in the toolbar, plus a quiet check at startup that turns it into
  *⬆ Update to vX* when a newer version is on GitHub. It shows what's new first and installs only after you click
  *Install & Restart*. The previous version is kept in `.update_backup/`. It only replaces the app's own files and never
  touches your photos.

## v1.2.1

- **DNG export no longer edits your DNG in place or makes `.dng.original` files.** Approving a DNG now exports an edited copy
  as `<name>.dng` into a `RAW Review export` folder next to the original. The filename is built from the source's base name
  plus a plain `.dng`; nothing is inherited from existing files. The source DNG is only read. Import the export folder into
  Lightroom.
- The exported file is checked (reopens as a raw file) before it is moved into place; Windows file-lock errors are retried
  and reported clearly.

## v1.2.0

- **Google Pixel DNGs now preview like Google's own rendering.** Two things were missing, and both are now implemented:
  - **Local HDR tone map** (`ProfileGainTableMap`, DNG 1.7). Pixel stores a spatial grid of gain-vs-brightness curves that
    lifts shadows and holds highlights. It is applied after the profile look table and before the tone curve, with the
    DNG's own input weights, which is the order that best reproduces the camera's embedded preview (measured against the
    JPEG inside the DNG: error down from RMSE 34.6 to 8.6 and the brightness bias from -30 to +0.7 levels on the
    main-camera test photo).
  - **Lens shading** (`OpcodeList2` GainMap). The corners of a Pixel raw are several times darker than the centre and
    cast colour; this is flat-fielded when the photo is loaded, before anything is measured.
- New **Auto options -> Camera profile**: *Pixel local HDR tone map* on/off and a strength setting (25-100%).
- **Auto exposure understands the tone map**, so it no longer counts the lift twice. For files that carry a tone map it aims
  at the brightness the camera itself rendered (`TARGET_KEY_HDR` in `raw_review_auto.py`, calibrated on Pixel 7 Pro files).
  This also applies with the tone map switched off, where Auto exposure and Shadows make up the difference.
- Reading is still read-only and writing is unchanged: only the DNG's XMP packet is replaced, so the tone map stays in the
  file for Lightroom to use. (Verified: the gain-table tag and the raw image data are byte-identical after a write.)
- Not applied: the geometric lens-distortion opcodes (`OpcodeList3`, WarpRectilinear), so wide-angle Pixel shots keep
  their distortion in the preview (Lightroom's lens profile handles that), and the newer `ProfileGainTableMap2` layout.

## v1.1.1

- **Fix: wrong colours on Google Pixel DNGs** (and any DNG whose LibRaw colour matrix is empty). The app was treating
  camera RGB as sRGB, which gave flat grey colours, and white balance (auto WB, Temperature/Tint) could not work.
  The app now reads the colour science from inside the DNG itself (new `dng_profile.py`):
  - ColorMatrix1/2 and CalibrationIlluminant1/2, interpolated by colour temperature as the DNG spec describes
  - ForwardMatrix1/2, AnalogBalance, CameraCalibration
  - ProfileHueSatMap, ProfileLookTable and ProfileToneCurve (the embedded profile's "look")
  - BaselineExposure, used instead of the fixed +0.5 EV
- New **Auto options -> Camera profile (DNG colour)** menu: use the DNG's profile on/off, look strength, tone curve
  strength, baseline exposure from file.
- Temperature/Tint now line up with Lightroom's sliders for these files, since they come from the real matrices.
- Not applied (detected, noted in the menu): Pixel's ProfileGainTableMap (local HDR tone map) and lens-shading opcodes.
  Pixel DNGs therefore preview darker than Google's own JPEG until Auto exposure / shadows run.

## v1.1.0

- New straighten modes: Horizon (rotate), Verticals (rotate), and Lightroom Upright Level / Vertical / Auto / Full
- Auto exposure, levels (blacks & whites), shadows & highlights, contrast and colour, each runnable on its own
- New sliders: Whites, Blacks, Saturation, Sharpening (amount, radius, detail, masking), Noise reduction (luminance, detail, color)
- Auto sharpening and noise reduction from measured noise and softness
- Check boxes per photo, Batch edit (auto tools or increase / decrease / set / reset any setting), Undo batch, Approve checked
- Auto options menu (which tools run, straighten mode, strength, exposure bias)
- Auto-write Lightroom XMP option
- DNG files are now supported (sidecar .xmp, the DNG itself is never modified)

## v1.0.0

First release.

- Drag and drop RAW photos or whole folders
- Smart auto white balance from near-white and near-black areas, with confidence flags and "show what it used"
- Eyedropper white balance
- Auto straighten, plus Upright: Vertical for converging buildings
- Sliders for Temperature, Tint, Exposure, Contrast, Highlights, Shadows, Vibrance, Straighten, Upright and lens corrections
- Approve (A) / Reject (X), before/after (\), copy/paste settings, Approve all
- Writes Lightroom XMP sidecars only; RAW files are never modified
