# Changelog

## v1.9.11

- **Pixel photos: auto now aims for the phone's own look.** With the tone map out of the preview (v1.9.10), auto
  centred the histogram like for any other camera, which pushed Exposure to about +2 and Highlights to -100 on shop
  shelves and came out bright and washed. The DNG already says how the phone meant the photo to look (its local tone
  map), so for these files Exposure, Shadows, Highlights, Contrast, Blacks and Whites are now fitted to match the
  brightness of the phone's rendering, using only sliders Lightroom applies. Vibrance and Saturation are left at 0
  (the profile's own colour). On eight Pixel 7 Pro test photos the result lands within 0.1-0.35 stops of the phone's
  embedded preview (was 0.5-1.5 stops brighter) with nothing blown; typical values are Exposure +0.6 to +1.4,
  Highlights -40 to -100, Shadows +12 to +25.
- It runs when the Exposure tool runs, and only changes the tools that are switched on. *Brightness*, *Punch* and
  *Colour* in My auto settings, auto strength and your rules still apply on top. Other cameras are unaffected.
- Switch it off under *Auto ▾ → Camera profile → Pixel: auto tone aims for the phone's own look*.
- Checked against Lightroom: with no edits, the preview of two Pixel 7 Pro photos now matches Lightroom Classic
  11.0.1's export of the same files to within 0.02 stops and a colour difference of about 1 (ΔE), hue unchanged.

## v1.9.10

- **Fixed: Pixel photos overexposed and washed out in the preview, then dark in Lightroom.** Pixel DNGs carry a
  local HDR tone map (`ProfileGainTableMap`) that makes the phone's own picture 1.3-1.9 stops brighter than the plain
  profile render. The preview applied it; Lightroom Classic 11.0.1 uses the DNG's embedded profile but does not
  (its unedited exports of Pixel 7 Pro files sit where the preview does with the table off). So the preview was that
  much brighter than Lightroom for the same settings: auto exposure centred a picture Lightroom never showed, pulled
  Highlights to -100 on top of the tone map (pastel reds, flat whites), and the result came out dark in Lightroom.
  The tone map is now **off by default**, so the preview shows what Lightroom shows and auto exposure sets the
  brightness itself. Expect auto Exposure about 1 to 1.5 stops higher on Pixel photos than before; photos approved
  with the old preview should be re-run through auto.
- The switch is still there (*Auto ▾ → Camera profile → Pixel local HDR tone map*) for a Lightroom that does apply the
  table. A setting saved by an older version is reset to off once; after that your choice is remembered.

## v1.9.9

- **☐ Uncheck all** button next to *☑ Check all* in the toolbar (greyed out when nothing is ticked).

## v1.9.8

- **Reset several photos at once:** a *Reset ▾* menu in the toolbar resets every ticked photo to its **auto settings**,
  to **As Shot** (no edits at all), or to the **settings it had in Lightroom**. It asks first when it's more than one
  photo, and *Auto ▾ → Undo batch* puts them all back (Ctrl+Z still undoes one photo). Right-click → *Reset N photos*
  does the same for the photos selected in the list.

## v1.9.7

- **Guided Upright with suggested guides:** Straighten mode → *Upright: Guided (verticals)* or *(verticals +
  horizontals)*. RAW Review picks the guide lines for you: the clearest true verticals (lines that agree on where
  the verticals converge, so roof trusses and other diagonals are skipped), plus level horizontals when they'd give
  a gentle correction (edges receding down an aisle are skipped). The guides are drawn on the photo (blue vertical,
  pink horizontal); click one to swap it for the next best line. Up to four guides, like Lightroom.
- **Lightroom shows exactly the preview:** RAW Review now writes the Upright correction it solved for every mode
  (`UprightTransform_0`–`_5`, which Camera Raw renders as stored) and Guided's guides (`UprightFourSegments_n`), so
  Lightroom shows the same correction as the preview instead of its own estimate. Switching Upright off removes
  what RAW Review wrote. Clicking an Upright button in Lightroom still re-analyses there.
- **Constrain crop:** on by default (Geometry). After an Upright correction the largest crop at the photo's own shape
  with no empty corners is applied and written (with `CropConstrainToWarp`), like Lightroom's checkbox.
- **Better Upright Vertical:** diagonals up to 25° off vertical (roof trusses) could twist the correction; only lines
  within 15° count now.
- **Fixed: crops on portrait photos.** Lightroom keeps crops on the photo as the camera recorded it (before turning a
  vertical shot upright); RAW Review wrote them on the photo as shown, so on portrait shots a crop or straighten
  crop could land in the wrong place in Lightroom. Crops, Upright corrections and guides are now turned correctly.

## v1.9.6

- **Upright shows in the preview:** Upright Level, Vertical, Auto and Full used to be written to Lightroom but not
  shown. The preview now applies an approximation: Level rotates so horizontal lines are level, Vertical makes
  converging verticals (buildings, shelves, poles) upright and parallel, Full also squares up the horizontals, and
  Auto does a balanced, partial correction. Like Lightroom without *Constrain crop*, the warped photo shows empty
  corners; crop it with ✂ Crop. Lightroom still computes the exact correction, so expect small differences. If a
  photo has no clear lines, the preview says so (Lightroom still applies Upright).

## v1.9.5

- **Livelier preview:** while you drag a slider the photo now follows it (about 15-20 updates a second instead of
  about 3): a quick half-size draft is drawn while you move, and the full-size preview replaces it as soon as you
  pause. The full render itself is ~40% faster too (vibrance/saturation went from 160 ms to ~25 ms).
- **Crop by hand:** *✂ Crop* in Geometry (or **R**) shows the whole straightened photo with a crop frame: drag the
  corners, drag inside to move, or drag a new frame. Aspect ratio: Original, Free, 1:1, 4:5, 5:4, 3:2, 2:3, 16:9.
  Enter (or the button again) applies it, Esc cancels, *Reset crop* removes it; Undo works. Written to Lightroom
  as its crop, so it's already done when you open the photo there.

## v1.9.4

- **Choose what Paste copies:** Paste now asks which parts to copy (white balance, exposure, tone, colour, masks,
  sharpening, noise reduction, straighten & lens). By default it pastes the look and keeps each photo's own white
  balance, exposure and straightening; your choice is remembered. It used to overwrite everything.
- **See what you changed:** a slider you've moved away from where auto put it gets an orange dot, and an orange
  tick under each slider marks the auto value. Re-running auto updates the ticks.
- **↺ back to auto:** the dot comes with a ↺ button that puts that one slider back to its auto value.
  (Double-clicking a slider's name still resets it to the default.)
- **Apply auto settings to selected / checked:** two toolbar buttons next to *Check all* (the checked one shows how
  many are ticked). They replace *⚡ Auto selected*; Ctrl+Shift+E and right-click still work.

## v1.9.3

- **Colour codes you can see:** photos with a colour label get a coloured band along the bottom and left of their
  thumbnail, in the list and the grid.
- **Label several photos at once:** right-click → *Colour label for N photos* (Red, Yellow, Green, Blue, Purple,
  or No label). Keys 6–9 still work too.
- **Batch edit by colour:** *Batch edit → Apply to → Colour label*, then click any mix of colours (and/or *No label*);
  the counts update and the button says how many photos it will change.
- **Check by colour:** *Check ▾ → By colour label* ticks every photo with that colour (e.g. to approve them).
- Fixed: "&" showed as "_" in auto tool names ("Shadows _highlights"); radio buttons are easier to read.

## v1.9.2

- **Tidier toolbar:** the row of fourteen buttons is now a few groups, so nothing is cut off any more.
  *Add ▾* (files, folder, clear list) · *Grid* · *Auto ▾* (auto selected photos, batch edit, undo batch,
  My auto settings, the tools that run on new photos, auto-write XMP) · *Cull suggestions* ·
  **☑ Check all** · **Approve checked (n)** · *▾* (Approve all to-review).
- **Check all** button next to *Approve checked*, which now shows how many photos are ticked and is greyed out when
  none are.

## v1.9.1

- **Fixed: photos overexposed in Lightroom.** Lightroom applies Adobe's default tone curve to every raw, which
  brightens the midtones by about a stop; RAW Review's preview didn't, so auto exposure centred a preview that was a
  stop darker than what Lightroom then showed. Two Lightroom exports of RAW Review's edits measured the gap: about
  1.3 stops in the midtones. The preview now applies the same Adobe curve (from Adobe's DNG SDK), so it shows what
  Lightroom will show for the same settings (matched to within ~0.03 on those exports), and auto exposure centres
  that. Expect auto exposure about a stop lower than before and less added contrast. The histogram's middle is back
  to the middle (0.50).

- **Always clear which photos you're editing:** when slider changes go to more than one photo, an amber banner sits
  above the photo ("Editing 3 photos at once…") with an **Only this photo** button, and every affected photo is
  marked 🔗 in the list. The app always starts on *This photo*; it no longer remembers a multi-photo choice from last
  time.
- **Batch edit never defaults to all photos:** it starts on your ticked photos, or several selected ones, otherwise
  just this photo. The button says how many it will change ("Apply to 2 photos").
- **Fixed: slider changes going wrong.** Moving a slider (other than white balance) hit an error part-way through:
  the value changed, but it wasn't added to Undo, wasn't copied to the other photos when editing several, and the
  photo wasn't marked as edited.
- Look: rounded buttons, card-style sections, visible checkboxes, styled sliders; the right panel fits its width.

## v1.9.0

- **Histogram:** a Lightroom-style histogram now sits at the top of the right-hand panel: luminance plus red, green
  and blue. The dashed line marks the middle, where auto exposure aims; a yellow tick shows the photo's median, with
  the median and the share of blown and pure-black pixels underneath. With *Before* held it shows the original.
- **Clipping warning (J):** tick *Clipping* under the photo, press **J**, or click the histogram's corner triangles
  to show blown highlights in red and pure black in blue (works in Split view too). The triangles light up when
  anything is clipped.
- **Auto exposure stops blowing out white subjects:** the histogram showed a white plane's fuselage blown on
  "centred" photos. Auto exposure's highlight check looked at overall brightness before colour, but on a white
  subject in warm light one colour channel blows first, and Vibrance can push it further. It now checks exactly what
  the histogram's warning shows (any channel, after colour), so Highlights is pulled down far enough. On the test
  photo, blown went from 2.5% to under 1% at the same brightness.

## v1.8.4

- **Auto levels no longer crushes the shadows:** on hazy or daylight scenes with no true black, Auto levels pulled
  Blacks very far (e.g. -71 on a treeline), making shadows heavy and the photo look harsher and more contrasty than it
  is. It now moves the black point only part of the way and never past -40.

## v1.8.3

- **Auto exposure a touch darker:** photos were often coming out slightly overexposed. The histogram's middle is
  now aimed a little below centre (0.45 instead of 0.50, about a third of a stop darker), and the preview assumes
  Highlights can recover a bit less (about 1.5 stops at -100, closer to Lightroom), so bright areas get pulled back
  harder when needed. Want it brighter or darker still? Set *Brightness* in My auto settings.

## v1.8.2

- **✎ Edited mark:** any photo you change after its auto proposal (sliders, white balance, straighten, crop, paste,
  ⚡ Auto selected, batch edit…) is labelled **✎ Edited** in the list and grid, and the count shows how many are
  edited. Reset to auto proposal clears it, and so does Undo batch. Re-applying My auto settings to all photos
  doesn't count as an edit. The mark is kept between sessions. Turn automatic marking off under *Select ▾*.
- **Mark by hand:** *Select ▾* or right-click → Mark as edited / Mark as not edited.
- **New Select ▾ button** (next to Check ▾): select Not edited, Edited, Not edited and not yet reviewed, Not yet
  reviewed, Checked, All shown or None, then use ⚡ Auto selected… or right-click on them. Check ▾ also has
  Edited / Not edited.
- **Show filter** next to the search box: Show all / not edited / edited / to review / approved / rejected, for the
  list and the grid. It sticks between sessions.

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
