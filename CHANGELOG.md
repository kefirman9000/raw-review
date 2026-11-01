# Changelog

## v1.6.0

- **My auto settings** (toolbar ★): your own recipe for automatic edits, applied to every photo you add, to
  Auto-enhance (E) and to batch auto runs, as if it were part of the AI edit.
  - *How auto edits should look*: Brightness (EV added to auto exposure), Punch (contrast), Colour (vibrance) and
    the Strength of auto corrections.
  - A rule for every setting: **Auto** (the app decides), **Always set to** a value, **Add to auto** (auto's value +
    yours) or **Don't touch** (keep the camera's / Lightroom's value). Also *Always on / Never* for lens corrections and
    the sky / subject masks.
  - Save / load your settings as a file to share or keep several looks; optionally re-apply them right away to every
    photo you haven't reviewed yet.
- **Hold \\ to compare** with the original (let go to come back), and a **Split** before / after view with a
  draggable divider.
- **Finer sliders**: click a slider, then the mouse wheel nudges it (Shift = finest steps); click the number to type.
  The wheel only moves a slider you've clicked, so scrolling the panel never changes a setting by accident.
- **Undo approval**: puts the photo's sidecar (or DNG export) back exactly as it was before you approved, or removes it
  if there wasn't one. Refuses if Lightroom has saved to it since, so nothing of Lightroom's is lost.
- **Find a photo** by file name (Ctrl+F) and a **thumbnail size** slider for the grid.
- **Analysis progress with time left**, and **Pause / Resume** for big folders.
- **Keyboard shortcuts & tips** on **?** / F1, and a short **welcome tour** on first launch (View menu shows it again).
- **Fold panels** you don't use by clicking their title (remembered), and **Interface size** 85-150% in the View menu.
- *Auto strength* and *Exposure bias* moved from the Auto options menu into My auto settings.

## v1.5.0

- **Starts from your existing Lightroom edits.** When a photo already has settings (its `.xmp` sidecar, or for a DNG
  its *RAW Review export* copy, which is the file Lightroom uses), those are loaded instead of the auto proposal:
  white balance, tone, colour, sharpening, noise, straighten / Upright, lens corrections and RAW Review's sky / subject
  masks, plus the star rating and colour label. *Reset to auto proposal* is still one click away, and the new
  *Reset to Lightroom settings* goes back. Switch it off in *Auto options → Start from settings a photo already has*.
  - **A crop you made in Lightroom is kept** as long as you don't change the straighten angle here, and Guided Upright
    is no longer reset.
- **Fix: DNG settings being reset in Lightroom.** Re-approving a DNG now builds on the exported copy's current
  settings (including edits Lightroom wrote into it, such as exposure or brush masks) instead of the original DNG's,
  so nothing you did in Lightroom is thrown away. If a photo is already in your Lightroom catalog, still use
  *Metadata → Read Metadata from Files* after re-approving.
- **Remembers your settings and your session.** Auto options, camera-profile options, window size and layout, the
  last folder, and the toggles (*Auto-write*, *Edits apply to*, *Relative*, *Show masks*) are saved. The photos you
  had open come back on the next launch, after an update, or after a crash, with their edits, approve / reject
  decisions, check marks, ratings and labels (saved every few seconds). *Clear List* starts fresh. Stored in a
  `.raw_review` folder in your user folder, never next to your photos.
- **Star ratings and colour labels** like Lightroom: **1-5** stars, **0** clears; **6** red, **7** yellow, **8** green,
  **9** blue (press again to clear). Works on several selected photos at once. Shown in the list and grid, and written
  to the XMP when you approve, so Lightroom shows them.

## v1.4.0

- **Automatic sky and subject masks.** New *Sky & subject masks* panel with two switchable adjustments, written to
  Lightroom as its own AI masks (**Select Sky** and **Select Subject**):
  - Sky: exposure, contrast, highlights, dehaze, saturation, temperature.
  - Subject: exposure, contrast, shadows, texture, clarity, saturation.
  - **Auto** turns them on only when the photo has a real sky (at least 8% of the frame, touching the top edge) or a
    clear subject, and picks gentle values: darker, deeper skies; a little lift and detail on the subject. Runs with
    Auto-enhance and can be switched off in *Auto options*.
  - Lightroom computes the exact mask itself (the same way it handles its own Adaptive presets), so the preview here
    is an approximation. Tick *Show masks* to see it (sky blue, subject orange). If Lightroom shows a warning on the
    mask, select the photos and use **Update AI Settings**.
  - **Masks you made in Lightroom are kept.** Only masks named "RAW Review: Sky" / "RAW Review: Subject" are replaced.
  - Works with *Edits apply to*, Copy / Paste and Batch edit.
- **Per-photo Undo / Redo.** **Ctrl+Z** undoes the last change to the photo you're looking at, **Ctrl+Y** (or
  Ctrl+Shift+Z) redoes it. Each photo keeps its own history (up to 100 steps). One slider drag is one step. Covers
  sliders, auto tools, the eyedropper, resets, Paste, Batch edit and changes copied from another photo.
  Undo changes the settings in the app; to update Lightroom, approve the photo again.

## v1.3.0

- **Edit several photos at once.** New *Edits apply to* box at the top of the controls: *This photo*, *Selected photos*
  or *Checked photos*. Any slider, white balance or lens change you make on the photo shown is applied to the others
  too, like Lightroom's Auto Sync. Only the setting you change is copied; everything else on each photo stays as it was.
  - **Relative** off: every photo gets the same value (e.g. Exposure +0.50).
  - **Relative** on: the same change is added to each photo's own value (e.g. +0.3 EV brighter on all of them, each
    from its own starting point). Values are kept inside each slider's range.
- **Grid view** (toolbar button or **G**): all photos as large thumbnails with their review colour and ☑ marks.
  Selecting in the grid selects in the list, double-click or **Enter** opens a photo, **G** goes back.
  With several photos selected in the grid, **A** approves and **X** rejects all of them.
- Thumbnails are now made larger and refresh in the background when settings change on photos you aren't looking at
  (multi-photo edits, Paste, Undo batch).

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
