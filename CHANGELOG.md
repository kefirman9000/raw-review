# Changelog

## v1.8.1

- **Auto settings for just the photos you pick.** Select photos in the list or grid (Ctrl-click / Shift-click),
  then click **⚡ Auto selected…** in the toolbar, press **Ctrl+Shift+E**, or right-click them. One window shows
  exactly which photos it will change and lets you choose, for those photos only: which auto tools run, the
  straighten mode, subject crop, Upright, My auto settings, strength and exposure bias, and whether to start from
  As Shot. Your global Auto options are not changed, and the window remembers your last choices.
- **Right-click menu on photos:** Auto settings for the selection, Auto-enhance them, crop them to centre the
  subject, remove the subject crop, paste settings, or open Batch edit. Right-clicking a photo that isn't selected
  acts on that photo alone.
- *Undo batch* reverts the whole run; Ctrl+Z still works per photo.

## v1.8.0

- **New option: auto crop to centre the subject.** Turn on *Auto options ▾ → Auto crop to centre the subject
  (planes, cars…)*. RAW Review finds the main subject (a plane against the sky, a car against a blurred background)
  and crops just enough to put it in the middle of the frame: same aspect ratio, never cutting into the subject,
  and never trimming more than 40% of the width or height. If that limit stops it from centring fully, it gets the
  subject as close to the centre as it can. Subjects already near the centre are left uncropped. Off by default.
- **Per-photo toggle:** *Geometry → Crop to centre the subject* adds or removes the crop on the current photo,
  whatever the option is set to. The preview shows the crop, and it's written to Lightroom as a normal crop
  (on top of any straighten angle), so you can still adjust it there. Pasting settings keeps each photo's own crop.

## v1.7.3

- **Exposes for the middle, crushes highlights instead of staying dark:** v1.7.2 held exposure back whenever bright
  areas would blow out, so shots with white shelves, signs or windows still came out dark. Auto exposure now always
  puts the histogram's middle in the middle, then pulls **Highlights** down as far as needed to bring the bright
  areas back, and when Highlights has to go hard, lifts the tone curve's **Lights** a little so the upper midtones
  don't go flat.
- **New slider: Tone curve: Lights** (Tone panel), written to Lightroom's parametric tone curve
  (`crs:ParametricLights`). Like Whites/Blacks it's only written once it's set, so your own Lightroom curves are
  left alone. Also available as a rule in My auto settings.
- **Highlights in the preview now behaves like Lightroom's:** pulling Highlights down recovers the brightest areas
  (up to about two stops over white) and leaves the midtones alone. Before, it mostly darkened the midtones.

## v1.7.2

- **Auto exposure centres the histogram:** photos were coming out about a stop dark, with most of the histogram on
  the left in RAW Review and in Lightroom. Auto exposure now checks the finished photo (after Levels, Shadows &
  highlights and Contrast) and sets Exposure so its median brightness sits in the middle of the histogram, holding
  back only as far as needed to keep bright areas from blowing out. *Exposure bias* in My auto settings still
  applies on top.
- **Preview brightness matches Lightroom again:** v1.7.1's preview look also brightened the picture, which made
  previews look brighter than the same settings do in Lightroom. The look now only adds colour (like Lightroom's
  Adobe Color profile, a little gentler than before) and leaves brightness alone.

## v1.7.1

- **Previews now look like Lightroom:** the on-screen preview of RAW files was about a stop darker and noticeably
  less colourful than the same photo in Lightroom. It now applies a Lightroom-style default look (brighter midtones,
  richer colour), fitted against Lightroom exports of the same files. This only changes what you see: the auto edits
  and the settings written to Lightroom are exactly the same as before. DNGs keep using their own embedded profile.

## v1.7.0

- **Windows program, no Python needed:** each release now includes `RAW_Review-Windows-v….zip`. Unzip it and run
  **RAW Review.exe**. It updates itself the same way: *Check for Updates* downloads the new version, checks it against
  the fingerprint published with the release, and copies it in when RAW Review restarts. Running from the source
  with *RAW Review.bat* still works as before.
- **Cull suggestions** (toolbar): photos that look **soft** (missed focus or shake), have **blown highlights** in the
  RAW itself, or are **very dark**, plus the weaker frames of **bursts and near-duplicates** (neighbouring photos
  that look almost the same; the sharpest one, marked ★, is never suggested). The list shows the reason
  (e.g. *soft?*, *burst 2/5*). *Check them* ticks them so you can look them over in the grid, or *Reject* them in one
  go. Rejecting only marks them; nothing is written or deleted. Also under **Check ▾ → Suggested rejects**.
- **Safety tests** run on every change: the RAW stays byte-for-byte the same, sidecars keep other programs' data
  (titles, keywords, settings RAW Review doesn't control), the original sidecar is backed up once, and *Undo approval*
  puts things back exactly or refuses when Lightroom has saved since.

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
