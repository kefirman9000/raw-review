"""
RAW Review - review auto-edits for RAW photos and write Lightroom XMP sidecars.

Drag photos or folders onto the window. Each photo gets an automatic proposal
(white balance, straighten, exposure, levels, contrast, colour, sharpening and noise
reduction). Review each one:
    A  approve  -> writes/updates <photo>.xmp next to the RAW
    X  reject   -> nothing is written
    tweak any slider first if you want, then approve.

RAW files are NEVER modified, moved, renamed or deleted - they are only read.
"""

from __future__ import annotations

import base64
import copy
import json
import math
from collections import deque
import os
import sys
import time
import traceback
import types
from collections import OrderedDict

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, QTimer, Signal

import raw_review_auto as auto
import dng_profile
import raw_review_core as core
import raw_review_cull as cull
import updater
from version import __version__

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".raw_review")   # settings + last session (never your photos)
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
SESSION_FILE = os.path.join(CONFIG_DIR, "session.json")
LABEL_COLOURS = {"Red": "#d9534f", "Yellow": "#e6c229", "Green": "#4caf50", "Blue": "#3f7fd9", "Purple": "#9c5bd6"}
HELP_HTML = """
<h3>Keyboard</h3>
<table cellpadding="3">
<tr><td><b>A</b> / <b>X</b></td><td>Approve (write to Lightroom) / reject. In grid view: all selected photos</td></tr>
<tr><td><b>← →</b></td><td>Previous / next photo</td></tr>
<tr><td><b>Hold \\</b></td><td>Show the original while held. Tick <i>Split</i> for a draggable before/after view</td></tr>
<tr><td><b>G</b></td><td>Grid view on / off (double-click or Enter opens a photo)</td></tr>
<tr><td><b>1-5</b>, <b>0</b></td><td>Star rating, clear rating</td></tr>
<tr><td><b>6 7 8 9</b></td><td>Red / yellow / green / blue label (press again to clear). Right-click for Purple or to label several photos; labelled photos get a coloured band. <i>Check ▾ → By colour label</i> and <i>Batch edit → Colour label</i> work on photos by colour</td></tr>
<tr><td><i>Upright</i></td><td>Straighten mode → Upright Level / Vertical / Auto / Full / Guided. Guided suggests guide lines (blue = vertical, pink = horizontal); click one to swap it. Lightroom gets the same correction. <i>Constrain crop</i> removes the empty corners</td></tr>
<tr><td><b>R</b></td><td>Crop by hand: drag the corners or the frame, pick an aspect ratio in Geometry; Enter = done, Esc = cancel</td></tr>
<tr><td><b>J</b></td><td>Show clipping: blown highlights red, pure black blue (the histogram's triangles light up when it happens)</td></tr>
<tr><td><b>E</b> / <b>W</b></td><td>Auto-enhance (uses <i>My auto settings</i>) / auto white balance</td></tr>
<tr><td><b>C</b></td><td>Check / uncheck this photo</td></tr>
<tr><td><b>Ctrl+Z</b> / <b>Ctrl+Y</b></td><td>Undo / redo the last change to this photo</td></tr>
<tr><td><b>Ctrl+C</b> / <b>Ctrl+V</b></td><td>Copy settings / paste to checked or selected photos</td></tr>
<tr><td><b>Ctrl+F</b></td><td>Find a photo by file name</td></tr>
<tr><td><b>? or F1</b></td><td>This help</td></tr>
</table>
<h3>Sliders</h3>
<p>Click a slider, then use the mouse wheel to nudge it (hold <b>Shift</b> for the finest steps). Click the number to
type a value. Double-click a slider's name to reset it. (The wheel only moves a slider you've clicked, so scrolling the
panel never changes settings by accident.)</p>
<h3>Tips</h3>
<ul>
<li><b>My auto settings</b> (toolbar: <i>Auto ▾</i>) is your own recipe: how bright / punchy / colourful auto edits should be, and a
rule for any setting (always this value, add to auto, or don't touch). It's applied to every new photo automatically.</li>
<li><b>Edits apply to</b> (top of the controls) copies slider changes to selected or checked photos.</li>
<li>Click a panel's title to fold it away. <b>View</b> menu: interface size, fold / unfold all panels.</li>
<li>Your RAW files are only ever read. Approving writes a .xmp sidecar (or an edited DNG copy); <b>Undo approval</b>
puts it back exactly as it was.</li>
</ul>
"""

TOUR_PAGES = [
    ("Welcome to RAW Review",
     "Drag photos or whole folders onto the window. Each photo gets an automatic edit: white balance, straighten, "
     "exposure, tone, colour, sky / subject masks, sharpening and noise reduction.<br><br>"
     "Your RAW files are never changed: approved edits go to Lightroom as standard .xmp sidecars."),
    ("Review fast",
     "Press <b>A</b> to approve (write to Lightroom) or <b>X</b> to reject, and the next photo opens.<br>"
     "Hold <b>\\</b> to see the original. Press <b>G</b> for a grid of the whole shoot. "
     "<b>1-5</b> rate, <b>6-9</b> colour-label."),
    ("Make auto edits yours",
     "Open <b>Auto ▾ → My auto settings</b> in the toolbar: make auto edits brighter or moodier, punchier or softer, and set rules "
     "such as <i>Sharpening always 60</i> or <i>never touch white balance</i>. Every new photo follows them."),
    ("Tweak many at once",
     "Change <b>Edits apply to</b> (top of the controls) to <i>Selected</i> or <i>Checked photos</i> and every slider "
     "move goes to all of them. <b>Ctrl+Z</b> undoes per photo."),
    ("You're set",
     "Press <b>?</b> any time for all the keys. In Lightroom, import the folder (or use <i>Metadata → Read Metadata "
     "from Files</i> for photos already in your catalog)."),
]

CACHE_SIZE = 24          # decoded previews kept in memory
THUMB_W, THUMB_H = 120, 80      # list icons
GRID_W, GRID_H = 320, 213        # size thumbnails are made at (grid shows them at the chosen size)

STATUS_STYLE = {
    "loading":  ("Analyzing…", QtGui.QColor(90, 90, 90)),
    "ready":    ("To review",  None),
    "approved": ("Approved ✓ XMP written", QtGui.QColor(40, 110, 55)),
    "modified": ("Changed since approval", QtGui.QColor(150, 110, 20)),
    "rejected": ("Rejected ✗", QtGui.QColor(130, 45, 45)),
    "error":    ("Error",      QtGui.QColor(110, 30, 30)),
}


class Item:
    def __init__(self, path):
        self.path = path
        self._thumb = None          # last thumbnail (redrawn when the colour label changes)
        self._thumb_label = None
        self.status = "loading"
        self.edit: core.Edit | None = None
        self.auto_edit: core.Edit | None = None
        self.wb: core.WBResult | None = None
        self.color: core.CameraColor | None = None
        self.src_size = (3, 2)
        self.flip = 0                 # orientation (Lightroom's crop / Upright use the recorded frame)
        self.guide_rejects = []       # Guided Upright: guides you clicked away
        self.error = ""
        self.list_item: QtWidgets.QListWidgetItem | None = None
        self.grid_item: QtWidgets.QListWidgetItem | None = None
        self.rating: int | None = None    # 0-5 stars (None = leave the file's rating alone)
        self.label: str | None = None     # Lightroom colour label ("" = clear, None = leave alone)
        self.lr_edit = None               # the settings the photo already had (Lightroom), if any
        self.approve_undo = None          # (snapshot, written hash, status before) of the last approval
        self.undo: list = []          # earlier versions of .edit (newest last)
        self.redo: list = []
        self.undo_key = None          # what the last undo step was about (merges one slider drag)
        self.undo_time = 0.0
        self.cull: cull.CullStats | None = None   # sharpness, clipping, look-alike signature (suggestions only)
        self.edited = False           # ✎ you changed it after the auto proposal (or marked it yourself)


# --------------------------------------------------------------------------
# Background decoding
# --------------------------------------------------------------------------

class WorkerSignals(QObject):
    done = Signal(int, str, object, str)   # generation, path, result, error


def make_thumb(dec, edit):
    import cv2
    img, _ = core.render(dec, edit)
    h, w = img.shape[:2]
    s = min(GRID_W / w, GRID_H / h)
    return np.ascontiguousarray(
        cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA))


class DecodeTask(QRunnable):
    def __init__(self, gen, path, analyze, opts, signals):
        super().__init__()
        self.gen, self.path, self.analyze, self.opts, self.signals = gen, path, analyze, opts, signals

    def run(self):
        try:
            dec = core.decode_raw(self.path)          # read-only
            res = {"dec": dec}
            if self.analyze:
                edit, wb, st = auto.propose_edit(dec, self.opts)
                ex = core.read_existing(self.path, core.as_shot_edit(dec))      # read-only
                chosen = edit
                if ex["edit"] is not None and getattr(self.opts, "use_existing", True):
                    chosen = ex["edit"]
                res.update(edit=chosen, auto_edit=edit, existing=ex, wb=wb, st=st, thumb=make_thumb(dec, chosen))
                try:
                    res["cull"] = cull.measure(dec, edit)
                except Exception:  # noqa: BLE001 - suggestions are a nicety, never block a photo
                    traceback.print_exc()
            self.signals.done.emit(self.gen, self.path, res, "")
        except Exception as ex:  # noqa: BLE001
            traceback.print_exc()
            self.signals.done.emit(self.gen, self.path, None, f"{type(ex).__name__}: {ex}")


class ThumbSignals(QObject):
    done = Signal(int, str, object, object)   # generation, path, thumb, newly decoded image (or None)


class ThumbTask(QRunnable):
    """Re-render one photo's thumbnail after its settings changed (off the UI thread)."""
    def __init__(self, gen, path, edit, dec, signals):
        super().__init__()
        self.gen, self.path, self.edit, self.dec, self.signals = gen, path, edit.copy(), dec, signals

    def run(self):
        try:
            dec = self.dec
            fresh = None
            if dec is None:
                dec = fresh = core.decode_raw(self.path)      # read-only
            self.signals.done.emit(self.gen, self.path, make_thumb(dec, self.edit), fresh)
        except Exception:  # noqa: BLE001 - a stale thumbnail is harmless
            traceback.print_exc()


class BatchSignals(QObject):
    done = Signal(int, str, object, str)   # generation, path, result, error


class BatchTask(QRunnable):
    """Run auto tools + manual changes on one photo's copy of its edit (off the UI thread)."""
    def __init__(self, gen, path, edit, dec, spec, signals):
        super().__init__()
        self.gen, self.path, self.edit, self.dec = gen, path, edit.copy(), dec
        self.spec, self.signals = spec, signals

    def run(self):
        try:
            dec = self.dec if self.dec is not None else core.decode_raw(self.path)   # read-only
            res = auto.apply_batch(dec, self.edit, self.spec)
            self.signals.done.emit(self.gen, self.path,
                                   {"edit": self.edit, "wb": res.get("wb"), "auto_values": res.get("auto_values"),
                                    "thumb": make_thumb(dec, self.edit)}, "")
        except Exception as ex:  # noqa: BLE001
            traceback.print_exc()
            self.signals.done.emit(self.gen, self.path, None, f"{type(ex).__name__}: {ex}")


class FnTask(QRunnable):
    """Run a plain function on the thread pool."""
    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        self.fn()


class UpdateSignals(QObject):
    checked = Signal(object, str, bool)    # info, error, manual
    progress = Signal(str)
    installed = Signal(object, str)        # backup folder, error


def to_qimage(rgb: np.ndarray) -> QtGui.QImage:
    rgb = np.ascontiguousarray(rgb)
    h, w = rgb.shape[:2]
    return QtGui.QImage(rgb.data, w, h, 3 * w, QtGui.QImage.Format.Format_RGB888).copy()


# --------------------------------------------------------------------------
# Widgets
# --------------------------------------------------------------------------

class ImageView(QtWidgets.QWidget):
    clicked = Signal(float, float)       # pixel coords in the displayed image
    splitMoved = Signal(float)           # 0..1 position of the before/after divider

    def __init__(self):
        super().__init__()
        self.setMinimumSize(420, 300)
        self.img: QtGui.QImage | None = None
        self.overlay = []                # (x, y, QColor) in displayed-image coords
        self.lines = []                  # (x1, y1, x2, y2, QColor) in displayed-image coords (Upright guides)
        self.message = "Drag RAW photos or folders here"
        self.badge = ""
        self.crosshair = False
        self.split_on = False
        self.split_pos = 0.5
        self._dragging = False
        # Crop mode: the straightened photo with a draggable frame, box = [l, t, r, b] in 0..1
        self.crop_mode = False
        self.crop_box = [0.0, 0.0, 1.0, 1.0]
        self.crop_aspect = None             # width / height in pixels, None = free
        self._crop_drag = None              # ("move" | "new" | corner index, start pos, start box)
        self.setMouseTracking(True)

    def set_image(self, img, overlay=None, badge=""):
        self.img, self.overlay, self.badge = img, overlay or [], badge
        self.update()

    def set_message(self, text):
        self.message = text
        self.update()

    def _target(self):
        iw, ih = self.img.width(), self.img.height()
        s = min(self.width() / iw, self.height() / ih)
        w, h = iw * s, ih * s
        return QtCore.QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h), s

    def paintEvent(self, _e):
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor(28, 28, 30))
        if self.img is not None:
            r, s = self._target()
            p.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
            p.drawImage(r, self.img)
            p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            for x, y, col in self.overlay:
                p.setPen(QtGui.QPen(col, 1.5))
                p.drawEllipse(QtCore.QPointF(r.x() + x * s, r.y() + y * s), 3, 3)
            for x1, y1, x2, y2, col in self.lines:
                a = QtCore.QPointF(r.x() + x1 * s, r.y() + y1 * s)
                b = QtCore.QPointF(r.x() + x2 * s, r.y() + y2 * s)
                p.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 160), 4))
                p.drawLine(a, b)
                p.setPen(QtGui.QPen(col, 2))
                p.drawLine(a, b)
                p.setBrush(col)
                for pt in (a, b):
                    p.drawEllipse(pt, 4, 4)
                p.setBrush(Qt.BrushStyle.NoBrush)
            if self.crop_mode:
                self._paint_crop(p, r)
            if self.split_on and not self.crop_mode:
                x = r.x() + self.split_pos * r.width()
                p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255), 2))
                p.drawLine(QtCore.QPointF(x, r.y()), QtCore.QPointF(x, r.bottom()))
                p.setBrush(QtGui.QColor(255, 255, 255))
                p.drawEllipse(QtCore.QPointF(x, r.center().y()), 9, 9)
                p.setFont(QtGui.QFont(self.font().family(), 9, QtGui.QFont.Weight.Bold))
                for text, left in (("BEFORE", True), ("AFTER", False)):
                    fm = p.fontMetrics()
                    br = fm.boundingRect(text).adjusted(-6, -3, 6, 3)
                    br.moveTop(int(r.bottom()) - br.height() - 8)
                    br.moveLeft(int(x) - br.width() - 8 if left else int(x) + 8)
                    p.fillRect(br, QtGui.QColor(0, 0, 0, 160))
                    p.setPen(QtGui.QColor(240, 240, 240))
                    p.drawText(br, Qt.AlignmentFlag.AlignCenter, text)
        if self.badge:
            p.setFont(QtGui.QFont(self.font().family(), 10))
            fm = p.fontMetrics()
            br = fm.boundingRect(self.badge).adjusted(-8, -4, 8, 4)
            br.moveTopLeft(QtCore.QPoint(10, 10))
            p.fillRect(br, QtGui.QColor(0, 0, 0, 170))
            p.setPen(QtGui.QColor(240, 240, 240))
            p.drawText(br, Qt.AlignmentFlag.AlignCenter, self.badge)
        if self.message:
            p.setPen(QtGui.QColor(200, 200, 200))
            p.setFont(QtGui.QFont(self.font().family(), 13))
            if self.img is None:
                p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.message)
            else:
                p.drawText(self.rect().adjusted(10, 0, -10, -10),
                           Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignLeft, self.message)
        p.end()

    # ---- crop frame ----
    HANDLE = 12

    def _crop_rect(self):
        r, _s = self._target()
        l, t, rr, b = self.crop_box
        return QtCore.QRectF(r.x() + l * r.width(), r.y() + t * r.height(),
                             (rr - l) * r.width(), (b - t) * r.height())

    def _paint_crop(self, p, r):
        c = self._crop_rect()
        shade = QtGui.QColor(0, 0, 0, 150)
        p.fillRect(QtCore.QRectF(r.left(), r.top(), r.width(), c.top() - r.top()), shade)
        p.fillRect(QtCore.QRectF(r.left(), c.bottom(), r.width(), r.bottom() - c.bottom()), shade)
        p.fillRect(QtCore.QRectF(r.left(), c.top(), c.left() - r.left(), c.height()), shade)
        p.fillRect(QtCore.QRectF(c.right(), c.top(), r.right() - c.right(), c.height()), shade)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 110), 1))
        for f in (1 / 3, 2 / 3):                                  # rule of thirds
            p.drawLine(QtCore.QPointF(c.left() + f * c.width(), c.top()),
                       QtCore.QPointF(c.left() + f * c.width(), c.bottom()))
            p.drawLine(QtCore.QPointF(c.left(), c.top() + f * c.height()),
                       QtCore.QPointF(c.right(), c.top() + f * c.height()))
        p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255), 1.5))
        p.drawRect(c)
        p.setBrush(QtGui.QColor(255, 255, 255))
        for pt in self._corners(c):
            p.drawRect(QtCore.QRectF(pt.x() - 5, pt.y() - 5, 10, 10))

    @staticmethod
    def _corners(c):
        return [c.topLeft(), c.topRight(), c.bottomRight(), c.bottomLeft()]

    def _to_norm(self, pos):
        r, _s = self._target()
        return (min(max((pos.x() - r.x()) / max(r.width(), 1.0), 0.0), 1.0),
                min(max((pos.y() - r.y()) / max(r.height(), 1.0), 0.0), 1.0))

    def _aspect_norm(self):
        """Required (width / height) of the box in 0..1 units, or None for free."""
        if not self.crop_aspect or self.img is None:
            return None
        return self.crop_aspect * self.img.height() / self.img.width()

    def _box_from_corner(self, ax, ay, x, y):
        """Box spanning anchor (ax, ay) to the pointer (x, y), honouring the aspect ratio and the frame."""
        a = self._aspect_norm()
        w, h = abs(x - ax), abs(y - ay)
        if a:
            if w / max(h, 1e-6) > a:
                w = h * a
            else:
                h = w / a
        sx, sy = (1 if x >= ax else -1), (1 if y >= ay else -1)
        maxw = (1 - ax) if sx > 0 else ax
        maxh = (1 - ay) if sy > 0 else ay
        if w > maxw:
            w = maxw
            h = w / a if a else h
        if h > maxh:
            h = maxh
            w = h * a if a else w
        x2, y2 = ax + sx * w, ay + sy * h
        return [min(ax, x2), min(ay, y2), max(ax, x2), max(ay, y2)]

    def conform_crop(self):
        """Fit the current box to the aspect ratio, keeping its centre and staying inside the photo."""
        a = self._aspect_norm()
        if not a:
            return
        l, t, r, b = self.crop_box
        cx, cy, w, h = (l + r) / 2, (t + b) / 2, r - l, b - t
        if w / max(h, 1e-6) > a:
            w = h * a
        else:
            h = w / a
        if w > 1:
            w, h = 1.0, 1.0 / a
        if h > 1:
            h, w = 1.0, a
        cx = min(max(cx, w / 2), 1 - w / 2)
        cy = min(max(cy, h / 2), 1 - h / 2)
        self.crop_box = [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]
        self.update()

    def _crop_press(self, pos):
        c = self._crop_rect()
        for i, pt in enumerate(self._corners(c)):
            if abs(pos.x() - pt.x()) <= self.HANDLE and abs(pos.y() - pt.y()) <= self.HANDLE:
                self._crop_drag = (i, pos, list(self.crop_box))
                return
        if c.contains(pos):
            self._crop_drag = ("move", pos, list(self.crop_box))
        else:
            x, y = self._to_norm(pos)
            self._crop_drag = ("new", (x, y), list(self.crop_box))

    def _crop_move(self, pos):
        kind, start, box = self._crop_drag
        x, y = self._to_norm(pos)
        if kind == "move":
            sx, sy = self._to_norm(start)
            w, h = box[2] - box[0], box[3] - box[1]
            l = min(max(box[0] + x - sx, 0.0), 1.0 - w)
            t = min(max(box[1] + y - sy, 0.0), 1.0 - h)
            self.crop_box = [l, t, l + w, t + h]
        elif kind == "new":
            ax, ay = start
            if abs(x - ax) > 0.01 or abs(y - ay) > 0.01:
                self.crop_box = self._box_from_corner(ax, ay, x, y)
        else:
            opp = [(box[2], box[3]), (box[0], box[3]), (box[0], box[1]), (box[2], box[1])][kind]
            nb = self._box_from_corner(opp[0], opp[1], x, y)
            if nb[2] - nb[0] > 0.03 and nb[3] - nb[1] > 0.03:
                self.crop_box = nb
        self.update()

    def _split_to(self, pos):
        r, _s = self._target()
        self.split_pos = min(max((pos.x() - r.x()) / max(r.width(), 1.0), 0.0), 1.0)
        self.splitMoved.emit(self.split_pos)

    def mousePressEvent(self, e):
        if self.img is None or e.button() != Qt.MouseButton.LeftButton:
            return
        r, s = self._target()
        pos = e.position()
        if self.crop_mode:
            self._crop_press(pos)
            return
        if self.split_on and not self.crosshair:
            self._dragging = True
            self._split_to(pos)
            return
        if r.contains(pos):
            self.clicked.emit((pos.x() - r.x()) / s, (pos.y() - r.y()) / s)

    def mouseReleaseEvent(self, e):
        self._dragging = False
        self._crop_drag = None

    def mouseMoveEvent(self, e):
        if self.crop_mode and self.img is not None:
            if self._crop_drag is not None:
                self._crop_move(e.position())
                return
            c, pos = self._crop_rect(), e.position()
            near = any(abs(pos.x() - pt.x()) <= self.HANDLE and abs(pos.y() - pt.y()) <= self.HANDLE
                       for pt in self._corners(c))
            self.setCursor(Qt.CursorShape.SizeFDiagCursor if near else
                           Qt.CursorShape.SizeAllCursor if c.contains(pos) else Qt.CursorShape.CrossCursor)
            return
        if self._dragging and self.img is not None:
            self._split_to(e.position())
            return
        if self.crosshair:
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif self.split_on:
            self.setCursor(Qt.CursorShape.SplitHCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)


class HistogramWidget(QtWidgets.QWidget):
    """Lightroom-style histogram of the preview, with clipping warnings.
    The dashed line marks the middle, where auto exposure aims the median."""
    clipToggled = Signal()

    TRI = 12                              # size of the clipping triangles

    def __init__(self):
        super().__init__()
        self.setFixedHeight(128)
        self.hist: core.Histogram | None = None
        self.clip_on = False
        self.before = False
        self.setToolTip("Histogram of the preview: dark tones left, bright tones right.\n"
                        "Dashed line = middle, where auto exposure aims.\n"
                        "Triangles light up when shadows (blue) or highlights (red) are clipped;\n"
                        "click them or press J to show clipping on the photo.")

    def set_histogram(self, hist, before=False):
        self.hist, self.before = hist, before
        self.update()

    def _plot_rect(self):
        return QtCore.QRectF(6, self.TRI + 6, self.width() - 12, self.height() - self.TRI - 30)

    def paintEvent(self, _e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QtGui.QColor(28, 28, 30))
        r = self._plot_rect()
        guide = QtGui.QPen(QtGui.QColor(70, 70, 74), 1)
        for f in (0.25, 0.75):
            x = r.x() + f * r.width()
            p.setPen(guide)
            p.drawLine(QtCore.QPointF(x, r.top()), QtCore.QPointF(x, r.bottom()))
        mid = QtGui.QPen(QtGui.QColor(150, 150, 155), 1, Qt.PenStyle.DashLine)
        p.setPen(mid)
        p.drawLine(QtCore.QPointF(r.center().x(), r.top()), QtCore.QPointF(r.center().x(), r.bottom()))
        h = self.hist
        if h is not None:
            peak = max(float(max(c[1:255].max() for c in (h.r, h.g, h.b, h.lum))), 1.0)

            def path(counts):
                ys = np.sqrt(np.minimum(counts / peak, 1.0))       # sqrt: small tones stay visible
                pp = QtGui.QPainterPath(QtCore.QPointF(r.left(), r.bottom()))
                for i, y in enumerate(ys):
                    pp.lineTo(r.left() + i / 255.0 * r.width(), r.bottom() - y * r.height())
                pp.lineTo(r.right(), r.bottom())
                pp.closeSubpath()
                return pp

            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QtGui.QColor(170, 170, 175, 90))
            p.drawPath(path(h.lum))
            p.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_Plus)
            for counts, col in ((h.r, (150, 30, 30)), (h.g, (30, 130, 40)), (h.b, (35, 55, 160))):
                p.setBrush(QtGui.QColor(*col, 200))
                p.drawPath(path(counts))
            p.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_SourceOver)
            mx = r.left() + h.median * r.width()
            p.setPen(QtGui.QPen(QtGui.QColor(255, 210, 90), 2))
            p.drawLine(QtCore.QPointF(mx, r.bottom() - 6), QtCore.QPointF(mx, r.bottom()))
        # clipping triangles
        for left in (True, False):
            clipped = h is not None and (h.black if left else h.blown) > 0.001
            col = QtGui.QColor(60, 130, 255) if left else QtGui.QColor(255, 60, 60)
            x0 = 6 if left else self.width() - 6 - self.TRI
            pts = [QtCore.QPointF(x0, 4), QtCore.QPointF(x0 + self.TRI, 4),
                   QtCore.QPointF(x0 if left else x0 + self.TRI, 4 + self.TRI)]
            p.setPen(QtGui.QPen(col if clipped else QtGui.QColor(110, 110, 115), 1.5))
            p.setBrush(col if (clipped and self.clip_on) else (col.darker(170) if clipped else Qt.BrushStyle.NoBrush))
            p.drawPolygon(QtGui.QPolygonF(pts))
        # readout
        p.setPen(QtGui.QColor(205, 205, 210))
        p.setFont(QtGui.QFont(self.font().family(), 9))
        if h is None:
            text = "No photo"
        else:
            text = (f"{'Before · ' if self.before else ''}Median {h.median:.2f}   "
                    f"Blown {h.blown * 100:.1f}%   Black {h.black * 100:.1f}%")
        p.drawText(QtCore.QRectF(6, self.height() - 20, self.width() - 12, 18),
                   Qt.AlignmentFlag.AlignCenter, text)
        p.end()

    def mousePressEvent(self, e):
        x, y = e.position().x(), e.position().y()
        if y <= self.TRI + 8 and (x <= self.TRI + 10 or x >= self.width() - self.TRI - 10):
            self.clipToggled.emit()


class FineSlider(QtWidgets.QSlider):
    """Mouse wheel nudges only a slider you've clicked (so scrolling the panel is safe); Shift = finest.
    auto_pos (slider units) draws a small tick where the automatic edit put this setting."""
    def __init__(self, *a):
        super().__init__(*a)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.auto_pos = None

    def paintEvent(self, e):
        super().paintEvent(e)
        if self.auto_pos is None or self.maximum() == self.minimum():
            return
        opt = QtWidgets.QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(QtWidgets.QStyle.ComplexControl.CC_Slider, opt,
                                             QtWidgets.QStyle.SubControl.SC_SliderGroove, self)
        handle = self.style().subControlRect(QtWidgets.QStyle.ComplexControl.CC_Slider, opt,
                                             QtWidgets.QStyle.SubControl.SC_SliderHandle, self)
        span = groove.width() - handle.width()
        frac = (self.auto_pos - self.minimum()) / (self.maximum() - self.minimum())
        x = groove.left() + handle.width() / 2 + frac * span
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setPen(QtGui.QPen(QtGui.QColor(230, 170, 60), 2))
        cy = groove.center().y()
        p.drawLine(QtCore.QPointF(x, cy + 5), QtCore.QPointF(x, cy + 10))     # tick under the groove
        p.end()

    def wheelEvent(self, e):
        if not self.hasFocus():
            e.ignore()
            return
        d = e.angleDelta().y() or e.angleDelta().x()
        notches = d / 120.0
        fine = bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        self.setValue(self.value() + int(round(notches * (1 if fine else 4))) or (1 if d > 0 else -1))
        e.accept()


class FineSpin(QtWidgets.QDoubleSpinBox):
    def __init__(self, *a):
        super().__init__(*a)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, e):
        if not self.hasFocus():
            e.ignore()
            return
        super().wheelEvent(e)


class SliderRow(QtWidgets.QWidget):
    changed = Signal(float)

    def __init__(self, label, lo, hi, step, decimals=0, default=0.0):
        super().__init__()
        self.scale = 1.0 / step
        self.default = default
        lay = QtWidgets.QGridLayout(self)
        lay.setContentsMargins(0, 2, 0 , 2)
        self.name = label
        self.auto_val = None
        self.lbl = QtWidgets.QLabel(label)
        self.lbl.setToolTip("Double-click to reset to the default")
        self.reset_btn = QtWidgets.QToolButton()
        self.reset_btn.setText("↺")
        self.reset_btn.setAutoRaise(True)
        self.reset_btn.setStyleSheet("QToolButton{color:#e6aa3c;font-weight:bold;padding:0 4px;}")
        self.reset_btn.clicked.connect(lambda: self.auto_val is not None and self.setValue(self.auto_val, emit=True))
        self.reset_btn.setVisible(False)
        self.slider = FineSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(int(round(lo * self.scale)), int(round(hi * self.scale)))
        self.slider.setToolTip("Click, then mouse wheel to nudge (Shift = finest). Double-click the name to reset.")
        self.spin = FineSpin()
        self.spin.setRange(lo, hi)
        self.spin.setDecimals(decimals)
        self.spin.setSingleStep(step)
        self.spin.setFixedWidth(78)
        self.spin.setKeyboardTracking(False)
        lay.addWidget(self.lbl, 0, 0)
        lay.addWidget(self.reset_btn, 0, 1, Qt.AlignmentFlag.AlignRight)
        lay.addWidget(self.spin, 0, 2, Qt.AlignmentFlag.AlignRight)
        lay.addWidget(self.slider, 1, 0, 1, 3)
        lay.setColumnStretch(0, 1)
        self.slider.valueChanged.connect(self._from_slider)
        self.spin.valueChanged.connect(self._from_spin)
        self.lbl.mouseDoubleClickEvent = lambda _e: self.setValue(self.default, emit=True)

    def set_auto(self, val):
        """Where the automatic edit put this setting (None = unknown): tick + 'changed by you' marker."""
        self.auto_val = val
        self.slider.auto_pos = None if val is None else int(round(val * self.scale))
        self.slider.update()
        self._refresh_mark()

    def _refresh_mark(self):
        changed = self.auto_val is not None and abs(self.value() - self.auto_val) > 0.5 / self.scale
        self.lbl.setText(f"{self.name}  <span style='color:#e6aa3c'>●</span>" if changed else self.name)
        self.reset_btn.setVisible(changed)
        if self.auto_val is not None:
            shown = f"{self.auto_val:.{self.spin.decimals()}f}"
            self.reset_btn.setToolTip(f"Back to the auto value ({shown})")
            self.slider.setToolTip(f"Auto value: {shown} (orange tick). Click, then mouse wheel to nudge "
                                   "(Shift = finest).")

    def _from_slider(self, v):
        val = v / self.scale
        self.spin.blockSignals(True)
        self.spin.setValue(val)
        self.spin.blockSignals(False)
        self._refresh_mark()
        self.changed.emit(val)

    def _from_spin(self, val):
        self.slider.blockSignals(True)
        self.slider.setValue(int(round(val * self.scale)))
        self.slider.blockSignals(False)
        self._refresh_mark()
        self.changed.emit(val)

    def value(self):
        return self.spin.value()

    def setValue(self, val, emit=False):
        for w in (self.slider, self.spin):
            w.blockSignals(True)
        self.spin.setValue(val)
        self.slider.setValue(int(round(val * self.scale)))
        for w in (self.slider, self.spin):
            w.blockSignals(False)
        self._refresh_mark()
        if emit:
            self.changed.emit(val)


class MyAutoDialog(QtWidgets.QDialog):
    """The user's own recipe for automatic edits: look targets + a rule per setting."""

    MODE_LABELS = ["Auto", "Always set to", "Add to auto", "Don't touch"]
    FLAG_LABELS = ["Auto", "Always on", "Never"]
    SECTIONS = [
        ("White balance", ["temp", "tint"]),
        ("Tone", ["exposure", "contrast", "highlights", "shadows", "whites", "blacks", "lights"]),
        ("Colour", ["vibrance", "saturation"]),
        ("Sharpening", ["sharp_amount", "sharp_radius", "sharp_detail", "sharp_masking"]),
        ("Noise reduction", ["nr_luma", "nr_luma_detail", "nr_color"]),
        ("Straighten", ["angle"]),
    ]

    def __init__(self, parent, opts):
        super().__init__(parent)
        self.setWindowTitle("My auto settings")
        self.resize(640, 760)
        lay = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel(
            "Your own recipe for automatic edits. It's applied to every photo you add, to <b>Auto-enhance (E)</b> "
            "and to batch auto runs, as if it were part of the AI edit. Single auto buttons (e.g. Auto WB) are not "
            "affected.")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        look = QtWidgets.QGroupBox("How auto edits should look")
        ll = QtWidgets.QVBoxLayout(look)
        self.s_bright = SliderRow("Brightness (EV added to auto exposure)", -1.5, 1.5, 0.05, 2, 0.0)
        self.s_punch = SliderRow("Punch (contrast compared with auto)", -50, 50, 1, 0, 0)
        self.s_colour = SliderRow("Colour (vibrance compared with auto)", -50, 50, 1, 0, 0)
        self.s_strength = SliderRow("Strength of auto corrections (%)", 25, 150, 5, 0, 100)
        self.s_bright.setValue(opts.exposure_bias)
        self.s_punch.setValue(opts.punch)
        self.s_colour.setValue(opts.colour)
        self.s_strength.setValue(round(opts.strength * 100))
        for w in (self.s_bright, self.s_punch, self.s_colour, self.s_strength):
            ll.addWidget(w)
        lay.addWidget(look)

        rules_box = QtWidgets.QGroupBox("Rules for individual settings")
        rb = QtWidgets.QVBoxLayout(rules_box)
        hint = QtWidgets.QLabel("<i>Always set to</i> = that value on every photo · <i>Add to auto</i> = auto's value "
                                "plus yours · <i>Don't touch</i> = keep the camera's / Lightroom's value")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#9ab;")
        rb.addWidget(hint)
        inner = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(inner)
        grid.setColumnStretch(0, 1)
        self.rows = {}
        rules = opts.rules or {}
        r = 0
        for title, keys in self.SECTIONS:
            h = QtWidgets.QLabel(f"<b>{title}</b>")
            grid.addWidget(h, r, 0, 1, 3)
            r += 1
            for key in keys:
                r = self._add_row(grid, r, key, rules.get(key))
        for title, prefix in (("Sky mask adjustments", "sky_"), ("Subject mask adjustments", "subj_")):
            grid.addWidget(QtWidgets.QLabel(f"<b>{title}</b>"), r, 0, 1, 3)
            r += 1
            for key in [k for k in core.SETTINGS if k.startswith(prefix)]:
                r = self._add_row(grid, r, key, rules.get(key))
        grid.addWidget(QtWidgets.QLabel("<b>On / off</b>"), r, 0, 1, 3)
        r += 1
        self.flags = {}
        for key, label in auto.FLAG_RULES.items():
            grid.addWidget(QtWidgets.QLabel(label), r, 0)
            cb = QtWidgets.QComboBox()
            cb.addItems(self.FLAG_LABELS)
            mode = (rules.get(key) or ["auto"])[0]
            cb.setCurrentIndex(auto.FLAG_MODES.index(mode) if mode in auto.FLAG_MODES else 0)
            grid.addWidget(cb, r, 1)
            self.flags[key] = cb
            r += 1
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        rb.addWidget(scroll)
        lay.addWidget(rules_box, 1)

        self.chk_reapply = QtWidgets.QCheckBox("Also re-apply now to photos I haven't reviewed yet")
        self.chk_reapply.setToolTip("Re-runs the auto edit with these settings on every photo still marked "
                                    "'To review'. Approved and rejected photos are left alone.")
        lay.addWidget(self.chk_reapply)
        row = QtWidgets.QHBoxLayout()
        b_reset = QtWidgets.QPushButton("Reset all to Auto")
        b_save = QtWidgets.QPushButton("Save to file…")
        b_load = QtWidgets.QPushButton("Load from file…")
        b_reset.clicked.connect(self._reset)
        b_save.clicked.connect(self._save)
        b_load.clicked.connect(self._load)
        for b in (b_reset, b_save, b_load):
            row.addWidget(b)
        row.addStretch(1)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok
                                        | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        row.addWidget(bb)
        lay.addLayout(row)

    def _add_row(self, grid, r, key, rule):
        st = core.SETTINGS[key]
        grid.addWidget(QtWidgets.QLabel(core.SETTINGS[key].label if key.startswith(("sky_", "subj_"))
                                        else core.setting_name(key)), r, 0)
        cb = QtWidgets.QComboBox()
        cb.addItems(self.MODE_LABELS)
        spin = QtWidgets.QDoubleSpinBox()
        spin.setDecimals(st.decimals)
        spin.setSingleStep(st.step)
        spin.setFixedWidth(90)
        mode, value = (rule or ["auto", st.default])[0], float((rule or ["auto", st.default])[1])

        def on_mode(i, spin=spin, st=st):
            if i == 2:
                span = st.hi - st.lo
                spin.setRange(-span, span)
            else:
                spin.setRange(st.lo, st.hi)
            spin.setEnabled(i in (1, 2))
        cb.currentIndexChanged.connect(on_mode)
        idx = auto.RULE_MODES.index(mode) if mode in auto.RULE_MODES else 0
        cb.setCurrentIndex(idx)
        on_mode(idx)
        spin.setValue(value if idx in (1, 2) else (0.0 if idx == 2 else st.default))
        grid.addWidget(cb, r, 1)
        grid.addWidget(spin, r, 2)
        self.rows[key] = (cb, spin)
        return r + 1

    def _reset(self):
        for key, (cb, spin) in self.rows.items():
            cb.setCurrentIndex(0)
            spin.setValue(core.SETTINGS[key].default)
        for cb in self.flags.values():
            cb.setCurrentIndex(0)
        for w, v in ((self.s_bright, 0.0), (self.s_punch, 0), (self.s_colour, 0), (self.s_strength, 100)):
            w.setValue(v)

    def values(self):
        rules = {}
        for key, (cb, spin) in self.rows.items():
            mode = auto.RULE_MODES[cb.currentIndex()]
            if mode != "auto":
                rules[key] = [mode, spin.value()]
        for key, cb in self.flags.items():
            mode = auto.FLAG_MODES[cb.currentIndex()]
            if mode != "auto":
                rules[key] = [mode, 0]
        return {"exposure_bias": round(self.s_bright.value(), 2), "punch": self.s_punch.value(),
                "colour": self.s_colour.value(), "strength": self.s_strength.value() / 100.0, "rules": rules}

    def _apply_values(self, v):
        self.s_bright.setValue(float(v.get("exposure_bias", 0.0)))
        self.s_punch.setValue(float(v.get("punch", 0)))
        self.s_colour.setValue(float(v.get("colour", 0)))
        self.s_strength.setValue(float(v.get("strength", 1.0)) * 100)
        rules = v.get("rules", {})
        for key, (cb, spin) in self.rows.items():
            mode, val = (rules.get(key) or ["auto", core.SETTINGS[key].default])[:2]
            cb.setCurrentIndex(auto.RULE_MODES.index(mode) if mode in auto.RULE_MODES else 0)
            spin.setValue(float(val))
        for key, cb in self.flags.items():
            mode = (rules.get(key) or ["auto"])[0]
            cb.setCurrentIndex(auto.FLAG_MODES.index(mode) if mode in auto.FLAG_MODES else 0)

    def _save(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save my auto settings", "my auto settings.json",
                                                        "Auto settings (*.json)")
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(self.values(), f, indent=1)
            except OSError as ex:
                QtWidgets.QMessageBox.warning(self, "Save", str(ex))

    def _load(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load auto settings", "", "Auto settings (*.json)")
        if path:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self._apply_values(json.load(f))
            except (OSError, ValueError) as ex:
                QtWidgets.QMessageBox.warning(self, "Load", f"Couldn't read that file:\n{ex}")


class TourDialog(QtWidgets.QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Welcome")
        self.resize(520, 300)
        lay = QtWidgets.QVBoxLayout(self)
        self.stack = QtWidgets.QStackedWidget()
        for title, body in TOUR_PAGES:
            w = QtWidgets.QLabel(f"<h2>{title}</h2><p style='font-size:13px'>{body}</p>")
            w.setWordWrap(True)
            w.setAlignment(Qt.AlignmentFlag.AlignTop)
            self.stack.addWidget(w)
        lay.addWidget(self.stack, 1)
        row = QtWidgets.QHBoxLayout()
        self.lbl = QtWidgets.QLabel("")
        self.b_back = QtWidgets.QPushButton("Back")
        self.b_next = QtWidgets.QPushButton("Next")
        b_skip = QtWidgets.QPushButton("Skip")
        self.b_back.clicked.connect(lambda: self._go(-1))
        self.b_next.clicked.connect(lambda: self._go(1))
        b_skip.clicked.connect(self.accept)
        row.addWidget(self.lbl)
        row.addStretch(1)
        for b in (b_skip, self.b_back, self.b_next):
            row.addWidget(b)
        lay.addLayout(row)
        self._go(0)

    def _go(self, d):
        i = self.stack.currentIndex() + d
        if i >= self.stack.count():
            self.accept()
            return
        self.stack.setCurrentIndex(max(0, i))
        i = self.stack.currentIndex()
        self.lbl.setText(f"{i + 1} / {self.stack.count()}")
        self.b_back.setEnabled(i > 0)
        self.b_next.setText("Done" if i == self.stack.count() - 1 else "Next")


# What "Paste settings" can copy, in groups you tick (like Lightroom's Paste dialog).
PASTE_GROUPS = [
    ("wb", "White balance", ("wb_mode", "temp", "tint")),
    ("exposure", "Exposure", ("exposure",)),
    ("tone", "Contrast, highlights, shadows, whites, blacks, tone curve", ("contrast", "highlights", "shadows",
                                                                          "whites", "blacks", "lights")),
    ("colour", "Vibrance & saturation", ("vibrance", "saturation")),
    ("masks", "Sky & subject masks", tuple(k for k in core.SETTINGS if k.startswith(("sky_", "subj_")))
     + ("sky_on", "subj_on")),
    ("sharpen", "Sharpening", tuple(k for k in core.SETTINGS if k.startswith("sharp_"))),
    ("noise", "Noise reduction", tuple(k for k in core.SETTINGS if k.startswith("nr_"))),
    ("geometry", "Straighten & lens correction", ("angle", "upright", "level_mode", "lr_crop_angle",
                                                  "lens_profile")),
]
PASTE_SHORT = {"wb": "White balance", "exposure": "Exposure", "tone": "Tone", "colour": "Colour",
               "masks": "Masks", "sharpen": "Sharpening", "noise": "Noise reduction", "geometry": "Straighten"}
# First time: the "look", not the settings that belong to each photo's own light and framing
PASTE_DEFAULT = {"tone", "colour", "masks", "sharpen", "noise"}


def paste_fields(edit_from, edit_to, groups):
    """Copy the fields of the ticked paste groups from one edit to another (optional settings too)."""
    for key, _label, fields in PASTE_GROUPS:
        if key not in groups:
            continue
        for f in fields:
            setattr(edit_to, f, copy.deepcopy(getattr(edit_from, f)))
            if f in core.SETTINGS and core.SETTINGS[f].optional:
                if f in edit_from.touched:
                    edit_to.touched.add(f)
                else:
                    edit_to.touched.discard(f)


class PasteDialog(QtWidgets.QDialog):
    """Pick which parts of the copied settings to paste."""

    def __init__(self, parent, n, src_name, chosen):
        super().__init__(parent)
        self.setWindowTitle("Paste settings")
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(QtWidgets.QLabel(f"Paste settings copied from <b>{src_name}</b> to <b>{n}</b> photo"
                                       + ("" if n == 1 else "s") + ".<br>Only the ticked parts change; "
                                       "everything else on those photos stays as it is."))
        self.checks = {}
        for key, label, _fields in PASTE_GROUPS:
            cb = QtWidgets.QCheckBox(label.replace("&", "&&"))
            cb.setChecked(key in chosen)
            self.checks[key] = cb
            lay.addWidget(cb)
        row = QtWidgets.QHBoxLayout()
        for text, on in (("All", True), ("None", False)):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(lambda _c, on=on: [c.setChecked(on) for c in self.checks.values()])
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        note = QtWidgets.QLabel("A subject crop is never pasted: it belongs to its own photo.")
        note.setStyleSheet("color:#888;font-size:11px;")
        lay.addWidget(note)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok
                                        | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self.ok = bb.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        self.ok.setText(f"Paste to {n} photo" + ("" if n == 1 else "s"))
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        for c in self.checks.values():
            c.toggled.connect(lambda _v: self.ok.setEnabled(bool(self.chosen())))

    def chosen(self):
        return {k for k, c in self.checks.items() if c.isChecked()}


class BatchDialog(QtWidgets.QDialog):
    """Pick which photos, which auto tools, and which settings to raise / lower / set."""

    ACTIONS = [("Increase by", "increase"), ("Decrease by", "decrease"),
               ("Set to", "set"), ("Reset to default", "reset")]

    def __init__(self, parent, counts: dict, opts: "auto.AutoOptions"):
        super().__init__(parent)
        self.setWindowTitle("Batch edit")
        self.resize(660, 760)
        lay = QtWidgets.QVBoxLayout(self)

        g = QtWidgets.QGroupBox("Apply to")
        gv = QtWidgets.QVBoxLayout(g)
        gl = QtWidgets.QHBoxLayout()
        gv.addLayout(gl)
        self.scope_btns = {}
        grp = QtWidgets.QButtonGroup(self)
        for key, label in [("current", "This photo"), ("selected", "Selected in list"),
                           ("checked", "Checked ☑"), ("all", "All photos")]:
            n = counts.get(key, 0)
            rb = QtWidgets.QRadioButton(f"{label} ({n})")
            rb.setEnabled(n > 0)
            grp.addButton(rb)
            gl.addWidget(rb)
            self.scope_btns[key] = rb
        # Default to what you deliberately picked (ticked, or several selected), else just this photo.
        # Never "All photos" unless you choose it.
        # Colour labels: tick one or more colours; the photos with any of them get the batch edit
        self.label_counts = counts.get("labels", {})
        g2 = QtWidgets.QWidget()
        ll = QtWidgets.QHBoxLayout(g2)
        ll.setContentsMargins(0, 0, 0, 0)
        rb = QtWidgets.QRadioButton("Colour label:")
        rb.setEnabled(any(self.label_counts.values()))
        rb.setToolTip("Photos with any of the colours you pick (label them with keys 6-9 or right-click)")
        grp.addButton(rb)
        self.scope_btns["label"] = rb
        ll.addWidget(rb)
        self.label_btns = {}
        for lab in list(core.LABELS) + ["None"]:
            n = self.label_counts.get("" if lab == "None" else lab, 0)
            b = QtWidgets.QPushButton(f"{'No label' if lab == 'None' else lab} ({n})")
            b.setCheckable(True)
            b.setEnabled(n > 0)
            col = LABEL_COLOURS.get(lab, "#6a6a70")
            b.setStyleSheet(f"QPushButton{{border-left:6px solid {col};padding:3px 8px;}}"
                            f"QPushButton:checked{{background:{col};border-color:{col};color:white;}}")
            b.toggled.connect(lambda on: (on and rb.setChecked(True), self._update_apply_text()))
            self.label_btns[lab] = b
            ll.addWidget(b)
        ll.addStretch(1)
        gv.addWidget(g2)
        first = next((k for k in ("checked", "selected") if counts.get(k, 0) > (0 if k == "checked" else 1)),
                     "current" if counts.get("current", 0) else "all")
        self.scope_btns[first].setChecked(True)
        self.counts = counts
        for rb in self.scope_btns.values():
            rb.toggled.connect(lambda _on: self._update_apply_text())
        lay.addWidget(g)

        ga = QtWidgets.QGroupBox("Run automatic tools")
        al = QtWidgets.QGridLayout(ga)
        self.auto_checks = {}
        for i, (key, label) in enumerate(auto.AUTO_GROUPS):
            cb = QtWidgets.QCheckBox(label.replace("&", "&&"))
            self.auto_checks[key] = cb
            if key == "level":
                self.level_combo = QtWidgets.QComboBox()
                self.level_combo.addItems([m for m in core.LEVEL_MODES if m != "Manual"])
                self.level_combo.setCurrentText(opts.level_mode)
                self.level_combo.activated.connect(lambda _i: self.auto_checks["level"].setChecked(True))
                al.addWidget(cb, i, 0)
                al.addWidget(self.level_combo, i, 1)
            else:
                al.addWidget(cb, i, 0, 1, 2)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Strength"))
        self.strength = QtWidgets.QDoubleSpinBox()
        self.strength.setRange(0, 200)
        self.strength.setSingleStep(10)
        self.strength.setSuffix(" %")
        self.strength.setValue(round(opts.strength * 100))
        self.strength.setToolTip("How far auto tools move the sliders: 50% = half way, 150% = push harder")
        row.addWidget(self.strength)
        row.addSpacing(16)
        row.addWidget(QtWidgets.QLabel("Exposure bias"))
        self.bias = QtWidgets.QDoubleSpinBox()
        self.bias.setRange(-2, 2)
        self.bias.setSingleStep(0.1)
        self.bias.setDecimals(2)
        self.bias.setSuffix(" EV")
        self.bias.setValue(opts.exposure_bias)
        self.bias.setToolTip("Added to Auto exposure: negative = darker overall, positive = brighter")
        row.addWidget(self.bias)
        row.addStretch(1)
        al.addLayout(row, len(auto.AUTO_GROUPS), 0, 1, 2)
        lay.addWidget(ga)

        gm = QtWidgets.QGroupBox("Change settings (runs after the automatic tools)")
        ml = QtWidgets.QVBoxLayout(gm)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(inner)
        self.rows = {}
        for i, (key, st) in enumerate(core.SETTINGS.items()):
            cb = QtWidgets.QCheckBox(core.setting_name(key))
            combo = QtWidgets.QComboBox()
            for label, _a in self.ACTIONS:
                combo.addItem(label)
            spin = QtWidgets.QDoubleSpinBox()
            spin.setDecimals(st.decimals)
            spin.setSingleStep(st.step)
            spin.setMinimumWidth(90)
            self.rows[key] = (cb, combo, spin)
            self._action_changed(key, 0)
            combo.activated.connect(lambda idx, k=key: self._action_changed(k, idx))
            spin.valueChanged.connect(lambda _v, c=cb: c.setChecked(True))
            combo.activated.connect(lambda _i, c=cb: c.setChecked(True))
            grid.addWidget(cb, i, 0)
            grid.addWidget(combo, i, 1)
            grid.addWidget(spin, i, 2)
        grid.setColumnStretch(0, 1)
        scroll.setWidget(inner)
        ml.addWidget(scroll)
        lay.addWidget(gm, 1)

        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok |
                                        QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self.apply_btn = bb.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        self._update_apply_text()
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _action_changed(self, key, idx):
        st = core.SETTINGS[key]
        cb, combo, spin = self.rows[key]
        action = self.ACTIONS[idx][1]
        spin.blockSignals(True)
        if action == "set":
            spin.setRange(st.lo, st.hi)
            spin.setValue(st.default)
        else:
            spin.setRange(0, st.hi - st.lo)
            spin.setValue(max(st.step, round((st.hi - st.lo) / 20 / st.step) * st.step))
        spin.setEnabled(action != "reset")
        spin.blockSignals(False)

    def _accept(self):
        if not self.spec().groups and not self.spec().changes:
            QtWidgets.QMessageBox.information(self, "Batch edit", "Tick at least one automatic tool or setting.")
            return
        self.accept()

    def _update_apply_text(self):
        if not hasattr(self, "apply_btn"):
            return
        key = next((k for k, rb in self.scope_btns.items() if rb.isChecked()), "current")
        if key == "label":
            n = sum(self.label_counts.get("" if lab == "None" else lab, 0)
                    for lab, b in self.label_btns.items() if b.isChecked())
        else:
            n = self.counts.get(key, 0)
        self.apply_btn.setText(f"Apply to {n} photo" + ("" if n == 1 else "s"))
        self.apply_btn.setEnabled(n > 0)

    def spec(self) -> "auto.BatchSpec":
        scope = next(k for k, rb in self.scope_btns.items() if rb.isChecked())
        if scope == "label":
            scope = "label:" + ",".join(lab for lab, b in self.label_btns.items() if b.isChecked())
        groups = [k for k in auto.GROUP_ORDER if self.auto_checks[k].isChecked()]
        changes = []
        for key, (cb, combo, spin) in self.rows.items():
            if cb.isChecked():
                changes.append((key, self.ACTIONS[combo.currentIndex()][1], spin.value()))
        return auto.BatchSpec(scope=scope, groups=groups, level_mode=self.level_combo.currentText(),
                              strength=self.strength.value() / 100.0,
                              exposure_bias=self.bias.value(), changes=changes)


class ApplyAutoDialog(QtWidgets.QDialog):
    """Run chosen auto tools, with their own options, on exactly the photos selected in the list / grid."""

    def __init__(self, parent, names: list, opts: "auto.AutoOptions", last: dict | None = None):
        super().__init__(parent)
        n = len(names)
        self.setWindowTitle(f"Auto settings for {n} selected photo{'s' if n != 1 else ''}")
        self.resize(520, 640)
        self.opts = opts
        lay = QtWidgets.QVBoxLayout(self)

        head = QtWidgets.QLabel(f"<b>Applies to these {n} photo{'s' if n != 1 else ''} only</b> "
                                "(select others in the list or grid first to change this):")
        head.setWordWrap(True)
        lay.addWidget(head)
        shown = ", ".join(names[:8]) + (f"  … and {n - 8} more" if n > 8 else "")
        who = QtWidgets.QLabel(shown)
        who.setWordWrap(True)
        who.setStyleSheet("color:#999;")
        lay.addWidget(who)

        ga = QtWidgets.QGroupBox("Automatic tools to run")
        al = QtWidgets.QGridLayout(ga)
        self.checks = {}
        for i, (key, label) in enumerate(auto.AUTO_GROUPS):
            cb = QtWidgets.QCheckBox(label.replace("&", "&&"))
            self.checks[key] = cb
            if key == "level":
                self.level_combo = QtWidgets.QComboBox()
                self.level_combo.addItems([m for m in core.LEVEL_MODES if m not in ("Manual", "Off")])
                self.level_combo.activated.connect(lambda _i: self.checks["level"].setChecked(True))
                al.addWidget(cb, i, 0)
                al.addWidget(self.level_combo, i, 1)
            else:
                al.addWidget(cb, i, 0, 1, 2)
        quick = QtWidgets.QHBoxLayout()
        b_all = QtWidgets.QPushButton("All")
        b_none = QtWidgets.QPushButton("None")
        b_mine = QtWidgets.QPushButton("Same as for new photos")
        b_mine.setToolTip("Tick the same tools and options that new photos get")
        b_all.clicked.connect(lambda: [c.setChecked(True) for c in self.checks.values()])
        b_none.clicked.connect(lambda: [c.setChecked(False) for c in self.checks.values()])
        b_mine.clicked.connect(lambda: self._fill(self._from_opts()))
        for b in (b_all, b_none, b_mine):
            quick.addWidget(b)
        quick.addStretch(1)
        al.addLayout(quick, len(auto.AUTO_GROUPS), 0, 1, 2)
        lay.addWidget(ga)

        go = QtWidgets.QGroupBox("Options for these photos")
        ol = QtWidgets.QVBoxLayout(go)
        self.chk_crop = QtWidgets.QCheckBox("Crop to centre the subject (planes, cars…)")
        self.chk_upright = QtWidgets.QCheckBox("Straighten best guess may choose Upright: Vertical")
        self.chk_mine = QtWidgets.QCheckBox("Apply My auto settings (punch, colour, your rules)")
        self.chk_fresh = QtWidgets.QCheckBox("Start from As Shot (throw away these photos' current edits first)")
        self.chk_fresh.setToolTip("Off: the tools adjust each photo as it is now.\n"
                                  "On: each photo is reset to As Shot first, like a brand-new proposal.")
        for w in (self.chk_crop, self.chk_upright, self.chk_mine, self.chk_fresh):
            ol.addWidget(w)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Strength"))
        self.strength = QtWidgets.QDoubleSpinBox()
        self.strength.setRange(0, 200)
        self.strength.setSingleStep(10)
        self.strength.setSuffix(" %")
        self.strength.setToolTip("How far auto tools move the sliders: 50% = half way, 150% = push harder")
        row.addWidget(self.strength)
        row.addSpacing(16)
        row.addWidget(QtWidgets.QLabel("Exposure bias"))
        self.bias = QtWidgets.QDoubleSpinBox()
        self.bias.setRange(-2, 2)
        self.bias.setSingleStep(0.1)
        self.bias.setDecimals(2)
        self.bias.setSuffix(" EV")
        self.bias.setToolTip("Added to Auto exposure: negative = darker, positive = brighter")
        row.addWidget(self.bias)
        row.addStretch(1)
        ol.addLayout(row)
        lay.addWidget(go)
        lay.addStretch(1)

        note = QtWidgets.QLabel("Undo with ↶ Undo batch (toolbar) or Ctrl+Z on each photo. Nothing is written "
                                "to Lightroom until you approve (unless Auto-write is on).")
        note.setWordWrap(True)
        note.setStyleSheet("color:#888;font-size:11px;")
        lay.addWidget(note)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok |
                                        QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText(f"Apply to {n} photo{'s' if n != 1 else ''}")
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._fill(last or self._from_opts())

    def _from_opts(self) -> dict:
        o = self.opts
        groups = set(o.groups)
        if o.level_mode != "Off":
            groups.add("level")
        return {"groups": sorted(groups), "level_mode": o.level_mode, "subject_crop": o.subject_crop,
                "allow_upright": o.allow_upright, "mine": True, "fresh": False,
                "strength": o.strength, "exposure_bias": o.exposure_bias}

    def _fill(self, v: dict):
        groups = set(v.get("groups", []))
        for k, cb in self.checks.items():
            cb.setChecked(k in groups)
        mode = v.get("level_mode", "Auto (best guess)")
        self.level_combo.setCurrentText(mode if mode in core.LEVEL_MODES and mode not in ("Manual", "Off")
                                        else "Auto (best guess)")
        self.chk_crop.setChecked(bool(v.get("subject_crop", False)))
        self.chk_upright.setChecked(bool(v.get("allow_upright", True)))
        self.chk_mine.setChecked(bool(v.get("mine", True)))
        self.chk_fresh.setChecked(bool(v.get("fresh", False)))
        self.strength.setValue(round(float(v.get("strength", 1.0)) * 100))
        self.bias.setValue(float(v.get("exposure_bias", 0.0)))

    def values(self) -> dict:
        return {"groups": [k for k in auto.GROUP_ORDER if self.checks[k].isChecked()],
                "level_mode": self.level_combo.currentText(), "subject_crop": self.chk_crop.isChecked(),
                "allow_upright": self.chk_upright.isChecked(), "mine": self.chk_mine.isChecked(),
                "fresh": self.chk_fresh.isChecked(), "strength": self.strength.value() / 100.0,
                "exposure_bias": self.bias.value()}

    def _accept(self):
        v = self.values()
        if not v["groups"] and not v["subject_crop"]:
            QtWidgets.QMessageBox.information(self, self.windowTitle(), "Tick at least one tool to run.")
            return
        self.accept()

    def spec(self) -> "auto.BatchSpec":
        v, o = self.values(), self.opts
        mine = v["mine"]
        return auto.BatchSpec(scope="selected", groups=v["groups"], level_mode=v["level_mode"],
                              allow_upright=v["allow_upright"], strength=v["strength"],
                              exposure_bias=v["exposure_bias"], punch=o.punch if mine else 0.0,
                              colour=o.colour if mine else 0.0, rules=dict(o.rules) if mine else {},
                              user_rules=mine, subject_crop=v["subject_crop"])


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"RAW Review v{__version__} – auto edits → Lightroom XMP (RAW + DNG)")
        self.resize(1500, 900)
        self.setAcceptDrops(True)

        self.items: "OrderedDict[str, Item]" = OrderedDict()
        self.cache: "OrderedDict[str, core.Decoded]" = OrderedDict()
        self.current: Item | None = None
        self.geom = core.Geometry()
        self.gen = 0
        self.clip_edit: core.Edit | None = None
        self._loading_controls = False
        self._requested = set()

        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(max(1, min(4, QtCore.QThread.idealThreadCount() - 1)))
        self.signals = WorkerSignals()
        self.signals.done.connect(self.on_decoded)
        self.batch_signals = BatchSignals()
        self.batch_signals.done.connect(self.on_batch_done)
        self._undo = {}
        self._batch_progress = None
        self._batch_total = self._batch_done = self._batch_errors = 0
        self.thumb_signals = ThumbSignals()
        self.thumb_signals.done.connect(self.on_thumb_done)
        self._thumb_pending = set()
        self.thumb_timer = QTimer(self)
        self.thumb_timer.setSingleShot(True)
        self.thumb_timer.setInterval(350)
        self.thumb_timer.timeout.connect(self._flush_thumbs)
        self._cull_why: dict = {}      # path -> reasons it may be worth rejecting (suggestions only)
        self._cull_burst: dict = {}    # path -> "burst 2/5" (★ = the sharpest frame)
        self.cull_timer = QTimer(self)
        self.cull_timer.setSingleShot(True)
        self.cull_timer.setInterval(400)
        self.cull_timer.timeout.connect(self._refresh_cull)
        self._syncing = False
        self.upd = UpdateSignals()
        self.upd.checked.connect(self.on_update_checked)
        self.upd.progress.connect(self.on_update_progress)
        self.upd.installed.connect(self.on_update_installed)
        self._upd_busy = False
        self._upd_progress = None
        self._upd_version = ""

        self.render_timer = QTimer(self)
        self.render_timer.setSingleShot(True)
        self.render_timer.setInterval(15)
        self.render_timer.timeout.connect(self.refresh_view)
        # While a slider moves the preview is drawn at half size (~4x faster) so it follows the
        # slider; the full-size render replaces it once you pause.
        self._fast_next = False
        self.settle_timer = QTimer(self)
        self.settle_timer.setSingleShot(True)
        self.settle_timer.setInterval(220)
        self.settle_timer.timeout.connect(self.refresh_view)

        self._restore_map = {}
        self._last_session_text = ""
        self.last_dir = ""
        self._queue = deque()                 # photos waiting to be analysed
        self._analyzing = set()
        self._an_total = self._an_done = 0
        self._an_t0 = 0.0
        self._paused = False
        self._hold_before = False
        self._split_cache = None              # (before, after) images for the split view
        self._build_ui()
        self._build_shortcuts()
        self._set_controls_enabled(False)
        self._apply_saved_window(self._settings)
        self.autosave = QTimer(self)
        self.autosave.setInterval(8000)
        self.autosave.timeout.connect(self.save_state)
        self.autosave.start()
        QTimer.singleShot(0, self.restore_session)
        QtWidgets.QApplication.instance().installEventFilter(self)
        if not self._settings.get("tour_done"):
            QTimer.singleShot(600, self.show_tour)
        QTimer.singleShot(2500, lambda: self.check_updates(manual=False))   # quiet check

    # ---------------- UI ----------------
    def _opts_snapshot(self) -> "auto.AutoOptions":
        return copy.deepcopy(self.opts)

    def _set_no_ai_masks(self, on):
        core.NO_AI_MASKS = bool(on)
        self._settings["no_ai_masks"] = core.NO_AI_MASKS
        self._show_no_ai_masks()
        if on and hasattr(self, "box_sky") and (self.box_sky.isChecked() or self.box_subj.isChecked()):
            self.box_sky.setChecked(False)          # (the current photo's masks go: written without them)
            self.box_subj.setChecked(False)
        if hasattr(self, "statusBar"):
            self.statusBar().showMessage("AI masks off: no Select Subject / Select Sky is added or written."
                                         if on else "AI masks allowed again (Sky & subject masks).", 6000)

    def _show_no_ai_masks(self):
        if not hasattr(self, "box_sky"):
            return
        for b in (self.box_sky, self.box_subj):
            b.setEnabled(not core.NO_AI_MASKS)
            b.setToolTip("Off: Never use Lightroom AI masks is ticked (Auto options)" if core.NO_AI_MASKS else "")

    def _opts_changed(self, *_):
        groups = {k for k, a in self.auto_actions.items() if a.isChecked()}
        if self.opts.level_mode != "Off":
            groups.add("level")
        self.opts.groups = groups
        self.opts.allow_upright = self.act_upright.isChecked()

    def _build_auto_menu(self):
        btn = QtWidgets.QToolButton()
        btn.setText("Auto options ▾")
        btn.setToolTip("Which automatic tools run on photos you add (and on 'Auto-enhance')")
        btn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QtWidgets.QMenu(btn)
        self.auto_actions = {}
        for key, label in auto.AUTO_GROUPS:
            if key == "level":
                continue
            a = menu.addAction(("Auto " + label[0].lower() + label[1:]).replace("&", "&&"))
            a.setCheckable(True)
            a.setChecked(key in self.opts.groups)
            a.toggled.connect(self._opts_changed)
            self.auto_actions[key] = a
        menu.addSeparator()
        sub = menu.addMenu("Straighten mode for new photos")
        grp = QtGui.QActionGroup(sub)
        grp.setExclusive(True)
        for mode in core.LEVEL_MODES:
            if mode == "Manual":
                continue
            a = sub.addAction(mode)
            a.setCheckable(True)
            grp.addAction(a)
            a.setChecked(mode == self.opts.level_mode)
            a.triggered.connect(lambda _c=False, m=mode: self._set_level_opt(m))
        self.act_existing = menu.addAction("Start from settings a photo already has (Lightroom)")
        self.act_existing.setCheckable(True)
        self.act_existing.setChecked(self.opts.use_existing)
        self.act_existing.setToolTip("When a photo already has a Lightroom sidecar (or a DNG export), load those\n"
                                     "settings instead of the auto proposal. 'Reset to auto proposal' still works.\n"
                                     "Applies to photos added from now on.")
        self.act_existing.toggled.connect(lambda on: setattr(self.opts, "use_existing", bool(on)))
        menu.addSeparator()
        self.act_upright = menu.addAction("Best guess may choose Upright: Vertical")
        self.act_upright.setCheckable(True)
        self.act_upright.setChecked(self.opts.allow_upright)
        self.act_upright.toggled.connect(self._opts_changed)
        self.act_subject_crop = menu.addAction("Auto crop to centre the subject (planes, cars…)")
        self.act_subject_crop.setCheckable(True)
        self.act_subject_crop.setChecked(self.opts.subject_crop)
        self.act_subject_crop.setToolTip("Finds the main subject and crops just enough (same aspect ratio, at most\n"
                                         "40% off each side) to put it in the centre. Off by default.\n"
                                         "Runs on new photos, Auto-enhance and straighten re-runs.")
        self.act_subject_crop.toggled.connect(lambda on: setattr(self.opts, "subject_crop", bool(on)))
        self.act_no_ai = menu.addAction("Never use Lightroom AI masks (Select Subject / Select Sky)")
        self.act_no_ai.setCheckable(True)
        self.act_no_ai.setChecked(core.NO_AI_MASKS)
        self.act_no_ai.setToolTip("No Select Subject or Select Sky mask is added, by Auto or by hand, and the ones RAW\n"
                                  "Review added before are taken out of a photo's sidecar when it's written again.\n"
                                  "Lightroom records these AI masks in the JPEGs it exports, and some stock agencies\n"
                                  "look for AI in a photo's metadata. Masks you made in Lightroom yourself are kept.")
        self.act_no_ai.toggled.connect(self._set_no_ai_masks)
        menu.addSeparator()
        menu.addAction("My auto settings… (brightness, strength, your rules)", self.open_my_auto)
        menu.addSeparator()
        pm = menu.addMenu("Camera profile (DNG colour)")
        popt = dng_profile.OPTIONS
        a = pm.addAction("Use the DNG's own colour profile")
        a.setCheckable(True)
        a.setChecked(popt.use_profile)
        a.setToolTip("Reads the colour matrices and look from inside the DNG instead of relying on LibRaw.\n"
                     "Needed for Google Pixel DNGs, whose LibRaw matrix is empty.")
        a.toggled.connect(lambda on: self._set_profile_opt("use_profile", bool(on)))
        lm = pm.addMenu("Profile look strength (hue/sat map + look table)")
        g4 = QtGui.QActionGroup(lm)
        for pct in (0, 25, 50, 75, 100):
            a = lm.addAction("Off (plain matrix colours)" if pct == 0 else f"{pct}%")
            a.setCheckable(True)
            g4.addAction(a)
            a.setChecked(abs(popt.look_strength - pct / 100.0) < 1e-6)
            a.triggered.connect(lambda _c=False, p=pct: self._set_profile_opt("look_strength", p / 100.0))
        tm = pm.addMenu("Profile tone curve strength")
        g5 = QtGui.QActionGroup(tm)
        for pct in (0, 50, 100):
            a = tm.addAction("Off" if pct == 0 else f"{pct}%")
            a.setCheckable(True)
            g5.addAction(a)
            a.setChecked(abs(popt.tone_curve - pct / 100.0) < 1e-6)
            a.triggered.connect(lambda _c=False, p=pct: self._set_profile_opt("tone_curve", p / 100.0))
        a = pm.addAction("Use the file's baseline exposure")
        a.setCheckable(True)
        a.setChecked(popt.file_baseline_ev)
        a.setToolTip("Brightness the camera asks for (Pixel: +0.55 EV) instead of the app's fixed +0.5 EV")
        a.toggled.connect(lambda on: self._set_profile_opt("file_baseline_ev", bool(on)))
        pm.addSeparator()
        a = pm.addAction("Pixel local HDR tone map (the phone's look, not Lightroom's)")
        a.setCheckable(True)
        a.setChecked(popt.hdr_tone_map)
        a.setToolTip("Applies the DNG's ProfileGainTableMap, the local tone map that lifts shadows and holds\n"
                     "highlights, so the preview matches the phone's own preview / JPEG.\n"
                     "Off by default: Lightroom Classic 11 does not apply this table, so with it on the\n"
                     "preview is 1.3-1.9 stops brighter than Lightroom shows for the same settings, and the\n"
                     "auto edits come out dark there. Turn it on only if your Lightroom shows Pixel photos\n"
                     "as bright as the phone does with no edits.")
        a.toggled.connect(lambda on: self._set_profile_opt("hdr_tone_map", bool(on)))
        a = pm.addAction("Pixel: auto tone aims for the phone's own look")
        a.setCheckable(True)
        a.setChecked(auto.CAMERA_LOOK)
        a.setToolTip("For DNGs that carry the phone's local tone map (Pixel): Auto sets Exposure, Shadows,\n"
                     "Highlights, Contrast, Blacks and Whites to match the brightness of the phone's own\n"
                     "rendering, using sliders Lightroom applies. Off: the same tools as every other camera\n"
                     "(histogram centred). Takes effect the next time auto runs on a photo.")
        a.toggled.connect(lambda on: setattr(auto, "CAMERA_LOOK", bool(on)))
        hm = pm.addMenu("HDR tone map strength")
        g6 = QtGui.QActionGroup(hm)
        for pct in (25, 50, 75, 100):
            a = hm.addAction(f"{pct}%")
            a.setCheckable(True)
            g6.addAction(a)
            a.setChecked(abs(popt.hdr_strength - pct / 100.0) < 1e-6)
            a.triggered.connect(lambda _c=False, p=pct: self._set_profile_opt("hdr_strength", p / 100.0))
        note = pm.addAction("Lens shading (vignette) from the DNG is always corrected")
        note.setEnabled(False)
        btn.setMenu(menu)
        return btn

    def _set_profile_opt(self, name, value):
        setattr(dng_profile.OPTIONS, name, value)
        self.refresh_view()

    def _set_level_opt(self, mode):
        self.opts.level_mode = mode
        self._opts_changed()

    def _auto_button(self, groups, title="Auto ▾"):
        """A small drop-down: run all of these auto tools, or just one."""
        btn = QtWidgets.QToolButton()
        btn.setText(title)
        btn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QtWidgets.QMenu(btn)
        if len(groups) > 1:
            menu.addAction("All of these", lambda: self.run_auto_groups(groups))
            menu.addSeparator()
        for g in groups:
            menu.addAction(auto.GROUP_LABEL[g].replace("&", "&&"), lambda g=g: self.run_auto_groups([g]))
        btn.setMenu(menu)
        return btn

    def _slider(self, key):
        s = core.SETTINGS[key]
        row = SliderRow(s.label, s.lo, s.hi, s.step, s.decimals, s.default)
        self.sliders[key] = row
        return row

    def _build_ui(self):
        self.opts = auto.AutoOptions()
        self._settings = self._load_json(SETTINGS_FILE)
        self._apply_saved_options(self._settings)
        self.sliders = {}
        tb = self.addToolBar("Main")
        tb.setMovable(False)

        def menu_button(text, tip=""):
            btn = QtWidgets.QToolButton()
            btn.setText(text)
            btn.setToolTip(tip)
            btn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
            m = QtWidgets.QMenu(btn)
            btn.setMenu(m)
            tb.addWidget(btn)
            return m

        # Add ▾
        m_add = menu_button("Add ▾", "Add photos, or clear the list")
        m_add.addAction("Add Files…", self.open_files)
        m_add.addAction("Add Folder…", self.open_folder)
        m_add.addSeparator()
        m_add.addAction("Clear List", self.clear_list)
        tb.addSeparator()
        self.act_grid = tb.addAction("▦ Grid (G)")
        self.act_grid.setCheckable(True)
        self.act_grid.setToolTip("Switch between one large photo and a grid of all photos (G). "
                                 "Double-click or Enter opens a photo.")
        self.act_grid.toggled.connect(self.set_grid)
        tb.addSeparator()

        # Auto ▾
        m_auto = menu_button("Auto ▾", "Run automatic edits on several photos, and choose how auto works")
        self.act_batch = m_auto.addAction("Batch edit…", self.open_batch)
        self.act_batch.setToolTip("Run auto tools or raise/lower/set settings on checked, selected or all photos")
        self.act_undo_batch = m_auto.addAction("Undo batch", self.undo_batch)
        self.act_undo_batch.setEnabled(False)
        m_auto.addSeparator()
        self.act_myauto = m_auto.addAction("★ My auto settings…", self.open_my_auto)
        self.act_myauto.setToolTip("Your own recipe for auto edits: brightness, punch, colour, and a rule per setting")
        self._auto_tools_btn = self._build_auto_menu()          # keep a reference: it owns the menu
        tools = self._auto_tools_btn.menu()
        tools.setTitle("Tools that run on new photos")
        m_auto.addMenu(tools)
        m_auto.addSeparator()
        self.chk_autowrite = QtWidgets.QCheckBox("Auto-write Lightroom XMP")   # state lives here (saved setting)
        self.act_autowrite = m_auto.addAction("Auto-write Lightroom XMP")
        self.act_autowrite.setCheckable(True)
        self.act_autowrite.setToolTip(
            "Write the .xmp sidecar for each photo as soon as it is analysed (and after batch edits),\n"
            "without waiting for you to approve. Off by default so you can review first.")
        self.act_autowrite.toggled.connect(self.chk_autowrite.setChecked)
        self.chk_autowrite.toggled.connect(self.act_autowrite.setChecked)
        m_auto.setToolTipsVisible(True)
        tb.addSeparator()

        self.act_cull = tb.addAction("Cull suggestions…", self.open_cull)
        self.act_cull.setToolTip("Photos that look soft, blown out or very dark, and the weaker frames of bursts:\n"
                                 "check them to look them over, or reject them in one go")
        tb.addSeparator()

        # Check all · Approve checked · Approve ▾
        act = tb.addAction("☑ Check all", lambda: self.check_where(lambda it: True))
        act.setToolTip("Tick every photo in the list (the Check ▾ menu on the left has more choices)")
        self.act_uncheck_all = tb.addAction("☐ Uncheck all", lambda: self.check_where(lambda it: False))
        self.act_uncheck_all.setToolTip("Untick every photo in the list")
        self.act_uncheck_all.setEnabled(False)
        self.act_auto_sel = tb.addAction("Apply auto settings to selected…", lambda: self.open_apply_auto("selected"))
        self.act_auto_sel.setShortcut(QtGui.QKeySequence("Ctrl+Shift+E"))
        self.act_auto_sel.setToolTip("Run auto tools, with options you pick, on the photos selected in the list or "
                                     "grid\n(Ctrl/Shift-click to select several). Also on right-click. (Ctrl+Shift+E)")
        self.act_auto_chk = tb.addAction("Apply auto settings to checked…", lambda: self.open_apply_auto("checked"))
        self.act_auto_chk.setToolTip("Run auto tools, with options you pick, on every ticked ☑ photo")
        self.act_auto_chk.setEnabled(False)
        self.act_approve_checked = tb.addAction("Approve checked", self.approve_checked)
        self.act_approve_checked.setToolTip("Approve the ticked photos and write their Lightroom XMP")
        self.act_approve_checked.setEnabled(False)
        m_appr = menu_button("▾", "More ways to approve")
        m_appr.addAction("Approve All To-Review…", self.approve_all)
        tb.addSeparator()
        m_reset = menu_button("Reset ▾", "Reset the ticked photos: to the auto settings, As Shot (no edits),\n"
                                         "or the settings they had in Lightroom")
        m_reset.setToolTipsVisible(True)
        m_reset.aboutToShow.connect(lambda m=m_reset: self._reset_menu(
            m, self.checked_items, lambda n: f"{n} checked photo" + ("" if n == 1 else "s")))
        spacer = QtWidgets.QWidget()
        spacer.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        tb.addWidget(self._build_view_menu())
        tb.addAction("? Help", self.show_help)
        self.act_update = tb.addAction("Check for Updates", lambda: self.check_updates(manual=True))
        self.act_update.setToolTip(f"You have v{__version__}. Checks github.com/{updater.GITHUB_REPO}")

        split = self.split = QtWidgets.QSplitter()
        self.setCentralWidget(split)

        # Left: list with a check box per photo
        left = QtWidgets.QWidget()
        ll = QtWidgets.QVBoxLayout(left)
        ll.setContentsMargins(4, 4, 4, 4)
        self.list = QtWidgets.QListWidget()
        self.list.setIconSize(QtCore.QSize(THUMB_W, THUMB_H))
        self.list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.currentRowChanged.connect(self.on_row)
        self.list.itemChanged.connect(self._on_list_item_changed)
        self.list.itemSelectionChanged.connect(self._sync_grid_from_list)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(lambda pos: self._photo_menu(self.list, pos))
        self.count_lbl = QtWidgets.QLabel("No photos")
        self.count_lbl.setWordWrap(True)
        self.checked_lbl = QtWidgets.QLabel("0 checked")
        chk_btn = QtWidgets.QToolButton()
        chk_btn.setText("Check ▾")
        chk_btn.setToolTip("Tick photos to edit or approve them together (Batch edit, Approve checked, Paste)")
        chk_btn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        cm = QtWidgets.QMenu(chk_btn)
        cm.addAction("All", lambda: self.check_where(lambda it: True))
        cm.addAction("None", lambda: self.check_where(lambda it: False))
        cm.addAction("Selected in list", self.check_selected)
        cm.addAction("Not yet reviewed", lambda: self.check_where(lambda it: it.status in ("ready", "modified")))
        cm.addAction("Low-confidence WB", lambda: self.check_where(
            lambda it: bool(it.wb and it.wb.applied and it.wb.confidence < 0.5)))
        cm.addAction("Suggested rejects (soft, bursts…)", lambda: self.check_where(
            lambda it: it.path in self._cull_why and it.status in ("ready", "modified")))
        lm = cm.addMenu("By colour label")
        for lab in core.LABELS:
            lm.addAction(self._label_icon(lab), lab, lambda lab=lab: self.check_where(lambda it: it.label == lab))
        lm.addSeparator()
        lm.addAction("No label", lambda: self.check_where(lambda it: not it.label))
        cm.addAction("Invert", self.check_invert)
        chk_btn.setMenu(cm)
        cm.addAction("Edited ✎", lambda: self.check_where(lambda it: it.edited))
        cm.addAction("Not edited", lambda: self.check_where(lambda it: not it.edited and it.edit is not None))
        sel_btn = QtWidgets.QToolButton()
        sel_btn.setText("Select ▾")
        sel_btn.setToolTip("Select photos by state (then Apply auto settings to selected, right-click, Approve…)")
        sel_btn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        sm = QtWidgets.QMenu(sel_btn)
        sm.addAction("Not edited", lambda: self.select_where(lambda it: not it.edited and it.edit is not None))
        sm.addAction("Edited ✎", lambda: self.select_where(lambda it: it.edited))
        sm.addAction("Not edited and not yet reviewed", lambda: self.select_where(
            lambda it: not it.edited and it.status in ("ready", "modified")))
        sm.addAction("Not yet reviewed", lambda: self.select_where(lambda it: it.status in ("ready", "modified")))
        sm.addAction("Checked ☑", lambda: self.select_where(
            lambda it: it.list_item.checkState() == Qt.CheckState.Checked))
        sm.addAction("All shown", lambda: self.select_where(lambda it: True))
        sm.addAction("None", lambda: self.select_where(lambda it: False))
        sm.addSeparator()
        sm.addAction("Mark selected as edited ✎", lambda: self.set_edited(self.selected_items(), True))
        sm.addAction("Mark selected as not edited", lambda: self.set_edited(self.selected_items(), False))
        sm.addSeparator()
        self.act_mark_edited = sm.addAction("Mark photos ✎ edited automatically when I change them")
        self.act_mark_edited.setCheckable(True)
        self.act_mark_edited.setChecked(bool(self._settings.get("mark_edited", True)))
        self.act_mark_edited.toggled.connect(lambda on: self._settings.__setitem__("mark_edited", bool(on)))
        sel_btn.setMenu(sm)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(sel_btn)
        top.addWidget(chk_btn)
        top.addWidget(self.checked_lbl, 1)
        ll.addLayout(top)
        ll.addWidget(self.list)
        ll.addWidget(self.count_lbl)
        self.btn_pause = QtWidgets.QPushButton("⏸ Pause analysis")
        self.btn_pause.setCheckable(True)
        self.btn_pause.setVisible(False)
        self.btn_pause.toggled.connect(self.set_paused)
        ll.addWidget(self.btn_pause)
        split.addWidget(left)

        # Center: image + nav
        center = QtWidgets.QWidget()
        cl = QtWidgets.QVBoxLayout(center)
        cl.setContentsMargins(4, 4, 4, 4)
        self.view = ImageView()
        self.view.clicked.connect(self.on_image_click)
        self.grid = QtWidgets.QListWidget()
        self.grid.setViewMode(QtWidgets.QListView.ViewMode.IconMode)
        self.grid.setIconSize(QtCore.QSize(GRID_W, GRID_H))
        self.grid.setGridSize(QtCore.QSize(GRID_W + 24, GRID_H + 52))
        self.grid.setResizeMode(QtWidgets.QListView.ResizeMode.Adjust)
        self.grid.setMovement(QtWidgets.QListView.Movement.Static)
        self.grid.setWordWrap(True)
        self.grid.setUniformItemSizes(True)
        self.grid.setSpacing(4)
        self.grid.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.grid.setStyleSheet("QListWidget{background:#1c1c1e;}")
        self.grid.itemSelectionChanged.connect(self._sync_list_from_grid)
        self.grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(lambda pos: self._photo_menu(self.grid, pos))
        self.grid.currentRowChanged.connect(self._grid_current_changed)
        self.grid.itemActivated.connect(lambda _i: self.act_grid.setChecked(False))   # double-click / Enter
        self.stack = QtWidgets.QStackedWidget()
        self.stack.addWidget(self.view)
        self.stack.addWidget(self.grid)
        bar = QtWidgets.QHBoxLayout()
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Find photo by name…  (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.apply_filter)
        bar.addWidget(self.search, 1)
        self.show_combo = QtWidgets.QComboBox()
        self.show_combo.addItems(["Show all", "Show not edited", "Show edited ✎", "Show to review",
                                  "Show approved", "Show rejected"])
        self.show_combo.setToolTip("Only show photos in this state (in the list and the grid)")
        self.show_combo.currentIndexChanged.connect(lambda _i: self._refilter())
        bar.addWidget(self.show_combo)
        self.thumb_lbl = QtWidgets.QLabel("Thumbnail size")
        self.thumb_size = QtWidgets.QSlider(Qt.Orientation.Horizontal)
        self.thumb_size.setRange(110, GRID_W)
        self.thumb_size.setValue(240)
        self.thumb_size.setFixedWidth(160)
        self.thumb_size.valueChanged.connect(self.set_thumb_size)
        bar.addWidget(self.thumb_lbl)
        bar.addWidget(self.thumb_size)
        cl.addLayout(bar)
        self.sync_banner = QtWidgets.QFrame()
        self.sync_banner.setObjectName("syncBanner")
        self.sync_banner.setStyleSheet(
            "#syncBanner{background:#5a4510;border:1px solid #c79a2a;border-radius:6px;}"
            "#syncBanner QLabel{color:#ffe7a8;font-weight:bold;}")
        sbl = QtWidgets.QHBoxLayout(self.sync_banner)
        sbl.setContentsMargins(10, 4, 6, 4)
        self.sync_banner_lbl = QtWidgets.QLabel("")
        sbl.addWidget(self.sync_banner_lbl, 1)
        self.sync_only_btn = QtWidgets.QPushButton("Only this photo")
        self.sync_only_btn.setToolTip("Stop copying changes to other photos")
        self.sync_only_btn.clicked.connect(lambda: self.scope_combo.setCurrentIndex(0))
        sbl.addWidget(self.sync_only_btn)
        self.sync_banner.hide()
        cl.addWidget(self.sync_banner)
        cl.addWidget(self.stack, 1)
        self.view.splitMoved.connect(lambda _p: self._compose_split())
        self.notes_lbl = QtWidgets.QLabel("")
        self.notes_lbl.setWordWrap(True)
        self.notes_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        cl.addWidget(self.notes_lbl)
        nav = QtWidgets.QHBoxLayout()
        self.btn_prev = QtWidgets.QPushButton("◀ Prev")
        self.btn_reject = QtWidgets.QPushButton("✗ Reject  (X)")
        self.btn_approve = QtWidgets.QPushButton("✓ Approve && write XMP  (A)")
        self.btn_next = QtWidgets.QPushButton("Next ▶")
        self.chk_before = QtWidgets.QCheckBox("Before  (hold \\)")
        self.chk_split = QtWidgets.QCheckBox("Split")
        self.chk_split.setToolTip("Before / after side by side: drag the divider")
        self.chk_split.toggled.connect(self._split_toggled)
        self.chk_clip = QtWidgets.QCheckBox("Clipping  (J)")
        self.chk_clip.setToolTip("Show blown highlights in red and pure black in blue (like Lightroom's J)")
        self.chk_clip.toggled.connect(lambda _v: self.refresh_view())
        self.btn_reject.setStyleSheet("QPushButton{background:#7a2e2e;border-color:#8e3a3a;color:white;padding:8px 18px;}"
                                      "QPushButton:hover{background:#8e3636;}")
        self.btn_approve.setStyleSheet("QPushButton{background:#2e7a42;border-color:#3a8e50;color:white;padding:8px 18px;"
                                       "font-weight:bold;}QPushButton:hover{background:#36904d;}")
        for w in (self.btn_prev, self.chk_before, self.chk_split, self.chk_clip):
            nav.addWidget(w)
        nav.addStretch(1)
        nav.addWidget(self.btn_reject)
        nav.addWidget(self.btn_approve)
        nav.addStretch(1)
        nav.addWidget(self.btn_next)
        cl.addLayout(nav)
        self.btn_prev.clicked.connect(lambda: self.step(-1))
        self.btn_next.clicked.connect(lambda: self.step(1))
        self.btn_reject.clicked.connect(self.reject)
        self.btn_approve.clicked.connect(self.approve)
        self.chk_before.toggled.connect(lambda _v: self.refresh_view())
        split.addWidget(center)

        # Right: controls
        panel = QtWidgets.QWidget()
        pl = QtWidgets.QVBoxLayout(panel)
        self.hist = HistogramWidget()
        self.hist.clipToggled.connect(lambda: self.chk_clip.toggle())
        pl.addWidget(self.hist)

        g_scope = QtWidgets.QGroupBox("Edits apply to")
        sl = QtWidgets.QGridLayout(g_scope)
        self.scope_combo = QtWidgets.QComboBox()
        self.scope_combo.addItems(["This photo", "Selected photos", "Checked photos"])
        self.scope_combo.setToolTip("Slider and white-balance changes go to this photo only, or to every "
                                    "selected / checked photo as well (like Lightroom's Auto Sync)")
        self.chk_relative = QtWidgets.QCheckBox("Relative")
        self.chk_relative.setToolTip("Off: every photo gets the same value (e.g. Exposure +0.50).\n"
                                     "On: the same change is added to each photo's own value "
                                     "(e.g. +0.3 EV brighter on all of them).")
        self.scope_lbl = QtWidgets.QLabel("")
        self.scope_lbl.setStyleSheet("color:#9ab;")
        self.scope_lbl.setWordWrap(True)
        sl.addWidget(self.scope_combo, 0, 0)
        sl.addWidget(self.chk_relative, 0, 1)
        sl.addWidget(self.scope_lbl, 1, 0, 1, 2)
        self.scope_combo.currentIndexChanged.connect(lambda _i: self._update_scope_label())
        self.chk_relative.toggled.connect(lambda _v: self._update_scope_label())
        pl.addWidget(g_scope)

        self._panels = {}
        collapsed = set(self._settings.get("collapsed", []))

        def group(title, auto_groups=None):
            box = QtWidgets.QGroupBox()
            box.setObjectName("fold")             # foldable: header is inside, no title gap
            outer = QtWidgets.QVBoxLayout(box)
            outer.setContentsMargins(6, 4, 6, 6)
            head = QtWidgets.QHBoxLayout()
            btn = QtWidgets.QToolButton()
            btn.setStyleSheet("QToolButton{border:none;font-weight:bold;}")
            btn.setToolTip("Click to fold / unfold")
            head.addWidget(btn, 1, Qt.AlignmentFlag.AlignLeft)
            if auto_groups:
                head.addWidget(self._auto_button(auto_groups), 0, Qt.AlignmentFlag.AlignRight)
            outer.addLayout(head)
            content = QtWidgets.QWidget()
            lay = QtWidgets.QVBoxLayout(content)
            lay.setContentsMargins(0, 0, 0, 0)
            outer.addWidget(content)

            def set_open(on, btn=btn, content=content, title=title):
                content.setVisible(on)
                btn.setText(("▾  " if on else "▸  ") + title.replace("&", "&&"))   # & would be a shortcut marker
            btn.clicked.connect(lambda _c=False, c=content, f=set_open: f(not c.isVisible()))
            set_open(title not in collapsed)
            self._panels[title] = (content, set_open)
            pl.addWidget(box)
            return lay

        g_wb = QtWidgets.QGroupBox("White balance")
        wl = QtWidgets.QVBoxLayout(g_wb)
        row = QtWidgets.QHBoxLayout()
        self.wb_combo = QtWidgets.QComboBox()
        self.wb_combo.addItems(["As Shot", "Custom"])
        self.btn_auto_wb = QtWidgets.QPushButton("Auto")
        self.btn_auto_wb.setToolTip("Find the most neutral near-white and near-black areas and balance on them")
        self.btn_eyedrop = QtWidgets.QPushButton("Eyedropper")
        self.btn_eyedrop.setCheckable(True)
        self.btn_eyedrop.setToolTip("Click something in the photo that should be white/grey/black")
        row.addWidget(self.wb_combo, 1)
        row.addWidget(self.btn_auto_wb)
        row.addWidget(self.btn_eyedrop)
        wl.addLayout(row)
        wl.addWidget(self._slider("temp"))
        wl.addWidget(self._slider("tint"))
        self.chk_samples = QtWidgets.QCheckBox("Show what Auto WB used")
        self.chk_samples.setToolTip("Cyan = near-white areas, magenta = near-black areas")
        self.chk_samples.toggled.connect(lambda _v: self.refresh_view())
        wl.addWidget(self.chk_samples)
        pl.addWidget(g_wb)

        tl = group("Tone", ["exposure", "levels", "shhi", "contrast"])
        for k in ("exposure", "contrast", "highlights", "shadows", "whites", "blacks", "lights"):
            tl.addWidget(self._slider(k))
        cl2 = group("Colour", ["colour"])
        for k in ("vibrance", "saturation"):
            cl2.addWidget(self._slider(k))
        ml_ = group("Sky & subject masks (Lightroom AI)", ["masks"])
        self.box_sky = QtWidgets.QGroupBox("Sky")
        self.box_sky.setCheckable(True)
        self.box_sky.setChecked(False)
        bl = QtWidgets.QVBoxLayout(self.box_sky)
        for k in core.MASK_KEYS["sky"]:
            bl.addWidget(self._slider(k))
        self.box_subj = QtWidgets.QGroupBox("Subject")
        self.box_subj.setCheckable(True)
        self.box_subj.setChecked(False)
        bl2 = QtWidgets.QVBoxLayout(self.box_subj)
        for k in core.MASK_KEYS["subject"]:
            bl2.addWidget(self._slider(k))
        ml_.addWidget(self.box_sky)
        ml_.addWidget(self.box_subj)
        self._show_no_ai_masks()
        self.chk_masks = QtWidgets.QCheckBox("Show masks (sky blue, subject orange)")
        self.chk_masks.toggled.connect(lambda _v: self.refresh_view())
        ml_.addWidget(self.chk_masks)
        mnote = QtWidgets.QLabel("Written as Lightroom's Select Sky / Select Subject. Lightroom makes the exact "
                                 "mask itself, so the preview here is approximate. If Lightroom shows a warning on "
                                 "the mask, select the photos and use Update AI Settings.")
        mnote.setWordWrap(True)
        mnote.setStyleSheet("color:#888;font-size:11px;")
        ml_.addWidget(mnote)

        sl = group("Sharpening", ["sharpen"])
        for k in ("sharp_amount", "sharp_radius", "sharp_detail", "sharp_masking"):
            sl.addWidget(self._slider(k))
        nl = group("Noise reduction", ["noise"])
        for k in ("nr_luma", "nr_luma_detail", "nr_color"):
            nl.addWidget(self._slider(k))
        note = QtWidgets.QLabel("Sharpening and noise reduction are written to Lightroom but "
                                "aren't shown in this preview.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#888;font-size:11px;")
        nl.addWidget(note)

        gl = group("Geometry")
        gl.addWidget(self._slider("angle"))
        row2 = QtWidgets.QHBoxLayout()
        row2.addWidget(QtWidgets.QLabel("Straighten mode"))
        self.level_combo = QtWidgets.QComboBox()
        self.level_combo.addItems(list(core.LEVEL_MODES.keys()))
        self.level_combo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.level_combo.setMinimumContentsLength(12)     # long mode names must not widen the panel
        self.level_combo.view().setMinimumWidth(320)
        self.level_combo.setToolTip(
            "Horizon / Verticals (rotate): measured here, written as a crop angle.\n"
            "Upright: Level / Vertical / Auto / Full: Lightroom's own Upright; Lightroom computes it\n"
            "(Full = architecture), so it isn't shown in this preview.")
        row2.addWidget(self.level_combo, 1)
        self.btn_level = QtWidgets.QPushButton("Re-run")
        self.btn_level.setToolTip("Run the selected straighten mode again")
        row2.addWidget(self.btn_level)
        gl.addLayout(row2)
        self.chk_lens = QtWidgets.QCheckBox("Lens profile corrections + remove CA")
        gl.addWidget(self.chk_lens)
        crow = QtWidgets.QHBoxLayout()
        self.btn_crop = QtWidgets.QPushButton("✂ Crop")
        self.btn_crop.setCheckable(True)
        self.btn_crop.setToolTip("Crop by hand (R): drag the corners, drag inside to move, or drag a new frame.\n"
                                 "Enter or click again = done, Esc = cancel. Written to Lightroom as its crop.")
        self.btn_crop.toggled.connect(self.on_crop_toggle)
        crow.addWidget(self.btn_crop)
        self.crop_aspect_combo = QtWidgets.QComboBox()
        self.crop_aspect_combo.addItems(["Original", "Free", "1:1", "4:5", "5:4", "3:2", "2:3", "16:9"])
        self.crop_aspect_combo.setToolTip("Aspect ratio of the crop frame")
        self.crop_aspect_combo.currentIndexChanged.connect(lambda _i: self._crop_aspect_changed())
        crow.addWidget(self.crop_aspect_combo, 1)
        self.btn_crop_reset = QtWidgets.QPushButton("Reset crop")
        self.btn_crop_reset.setToolTip("Remove the crop (keeps the straightening)")
        self.btn_crop_reset.clicked.connect(self.reset_crop)
        crow.addWidget(self.btn_crop_reset)
        gl.addLayout(crow)
        self.chk_constrain = QtWidgets.QCheckBox("Constrain crop (no empty corners after Upright)")
        self.chk_constrain.setChecked(True)
        self.chk_constrain.setToolTip("Like Lightroom's Constrain Crop: crop away the empty corners an Upright\n"
                                      "correction leaves (largest crop at the photo's shape)")
        self.chk_constrain.toggled.connect(self.on_constrain)
        gl.addWidget(self.chk_constrain)
        self.chk_subject_crop = QtWidgets.QCheckBox("Crop to centre the subject")
        self.chk_subject_crop.setToolTip("Minimal crop (same aspect ratio) that puts the main subject - a plane,\n"
                                         "a car - in the middle of the frame. Untick to remove the crop.")
        gl.addWidget(self.chk_subject_crop)

        g_misc = QtWidgets.QGroupBox("This photo")
        ml = QtWidgets.QGridLayout(g_misc)
        self.btn_enhance = QtWidgets.QPushButton("Auto-enhance (E)")
        self.btn_enhance.setToolTip("Run every automatic tool ticked in Auto ▾ → Tools that run on new photos")
        self.btn_reset_auto = QtWidgets.QPushButton("Reset to auto")
        self.btn_reset_auto.setToolTip("Go back to the automatic proposal for this photo")
        self.btn_reset_shot = QtWidgets.QPushButton("Reset to As Shot")
        self.btn_reset_shot.setToolTip("Go back to the camera's settings: no edits")
        self.btn_reset_lr = QtWidgets.QPushButton("Reset to Lightroom's")
        self.btn_reset_lr.setToolTip("Go back to the settings this photo already had when it was added "
                                     "(its Lightroom sidecar / DNG export)")
        self.btn_reset_lr.clicked.connect(self.reset_lr)
        self.btn_undo = QtWidgets.QPushButton("↶ Undo")
        self.btn_redo = QtWidgets.QPushButton("↷ Redo")
        self.btn_undo.setToolTip("Undo the last change to this photo, Ctrl+Z (each photo keeps its own history)")
        self.btn_redo.setToolTip("Redo (Ctrl+Y or Ctrl+Shift+Z)")
        self.btn_undo.clicked.connect(self.undo_edit)
        self.btn_redo.clicked.connect(self.redo_edit)
        self.btn_copy = QtWidgets.QPushButton("Copy settings")
        self.btn_copy.setToolTip("Copy this photo's settings (Ctrl+C)")
        self.btn_paste = QtWidgets.QPushButton("Paste settings")
        self.btn_paste.setToolTip("Paste the copied settings to the checked / selected photos (Ctrl+V)")
        ml.addWidget(self.btn_enhance, 0, 0, 1, 2)
        ml.addWidget(self.btn_reset_auto, 1, 0)
        ml.addWidget(self.btn_reset_shot, 1, 1)
        ml.addWidget(self.btn_copy, 2, 0)
        ml.addWidget(self.btn_paste, 2, 1)
        ml.addWidget(self.btn_undo, 3, 0)
        ml.addWidget(self.btn_redo, 3, 1)
        ml.addWidget(self.btn_reset_lr, 4, 0, 1, 2)
        self.btn_undo_approve = QtWidgets.QPushButton("Undo approval")
        self.btn_undo_approve.setToolTip("Put this photo's sidecar / DNG export back exactly as it was before you "
                                         "approved it (only while Lightroom hasn't changed it since)")
        self.btn_undo_approve.clicked.connect(self.undo_approval)
        ml.addWidget(self.btn_undo_approve, 6, 0, 1, 2)
        self.rating_lbl = QtWidgets.QLabel("")
        self.rating_lbl.setToolTip("Star rating 1-5 (0 clears) and colour label 6 red, 7 yellow, 8 green, 9 blue "
                                   "(press again to clear). Written to Lightroom when you approve.")
        ml.addWidget(self.rating_lbl, 5, 0, 1, 2)
        pl.addWidget(g_misc)

        safety = QtWidgets.QLabel(
            "RAW files are opened read-only and never changed, moved or renamed; approving writes only a .xmp "
            "sidecar next to them (an existing one is backed up once as .xmp.bak). For DNG files (Lightroom "
            "ignores DNG sidecars) an edited copy is exported as <name>.dng into a 'RAW Review export' "
            "folder next to the original, which is left untouched. Import that folder into Lightroom.")
        safety.setWordWrap(True)
        safety.setStyleSheet("color:#888;font-size:11px;")
        pl.addWidget(safety)
        pl.addStretch(1)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(panel)
        scroll.setMinimumWidth(340)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)   # panel fits its width
        split.addWidget(scroll)
        split.setSizes([260, 880, 360])

        # aliases used by the white-balance code
        self.s_temp, self.s_tint, self.s_angle = self.sliders["temp"], self.sliders["tint"], self.sliders["angle"]

        # wiring
        self.wb_combo.currentIndexChanged.connect(self.on_control)
        for key, s in self.sliders.items():
            s.changed.connect(self.on_wb_slider if key in ("temp", "tint") else self.on_control)
        self.level_combo.activated.connect(lambda _i: self.do_level(self.level_combo.currentText()))
        self.btn_level.clicked.connect(lambda: self.do_level(self.level_combo.currentText()))
        self.chk_lens.toggled.connect(self.on_control)
        self.chk_subject_crop.toggled.connect(self.on_subject_crop)
        self.box_sky.toggled.connect(self.on_control)
        self.box_subj.toggled.connect(self.on_control)
        self.btn_auto_wb.clicked.connect(self.do_auto_wb)
        self.btn_eyedrop.toggled.connect(self.on_eyedrop_toggle)
        self.btn_enhance.clicked.connect(self.auto_enhance)
        self.btn_reset_auto.clicked.connect(self.reset_auto)
        self.btn_reset_shot.clicked.connect(self.reset_shot)
        self.btn_copy.clicked.connect(self.copy_settings)
        self.btn_paste.clicked.connect(self.paste_settings)

        self.statusBar().showMessage("Drag RAW / DNG photos or folders onto the window to start.")
        self._update_scope_label()

    def _build_shortcuts(self):
        def sc(key, fn):
            s = QtGui.QShortcut(QtGui.QKeySequence(key), self)
            s.activated.connect(fn)
            return s
        sc("A", self.approve)
        sc("X", self.reject)
        sc("Right", lambda: self.step(1))
        sc("Left", lambda: self.step(-1))
        sc("?", self.show_help)
        sc("F1", self.show_help)
        sc("Ctrl+F", lambda: (self.search.setFocus(), self.search.selectAll()))
        sc("Ctrl+C", self.copy_settings)
        sc("Ctrl+V", self.paste_settings)
        sc("W", self.do_auto_wb)
        sc("E", self.auto_enhance)
        sc("C", self.toggle_check_current)
        sc("G", lambda: self.act_grid.toggle())
        sc("J", lambda: self.chk_clip.toggle())
        sc("R", lambda: self.btn_crop.toggle())
        # Enter / Esc finish or cancel a crop; only active while cropping (Enter opens photos in the grid)
        self._crop_keys = [sc("Return", lambda: self.btn_crop.setChecked(False)),
                           sc("Enter", lambda: self.btn_crop.setChecked(False)),
                           sc("Escape", self.cancel_crop)]
        for k in self._crop_keys:
            k.setEnabled(False)
        for n in range(6):
            sc(str(n), lambda n=n: self.set_rating(n))
        for key, lab in zip("6789", core.LABELS):
            sc(key, lambda lab=lab: self.set_label(lab))
        sc("Ctrl+Z", self.undo_edit)
        sc("Ctrl+Y", self.redo_edit)
        sc("Ctrl+Shift+Z", self.redo_edit)

    def _set_controls_enabled(self, on):
        widgets = [self.wb_combo, self.btn_auto_wb, self.btn_eyedrop, self.level_combo, self.btn_level,
                   self.chk_lens, self.chk_subject_crop, self.chk_constrain, self.btn_crop, self.btn_crop_reset,
                   self.crop_aspect_combo, self.btn_enhance, self.btn_reset_auto, self.btn_reset_shot,
                   self.btn_copy, self.btn_paste, self.btn_approve, self.btn_reject]
        mask_keys = set(core.MASK_KEYS["sky"]) | set(core.MASK_KEYS["subject"])
        widgets += [w for k, w in self.sliders.items() if k not in mask_keys]
        widgets += [self.box_sky, self.box_subj, self.chk_masks, self.btn_undo, self.btn_redo]
        self.btn_reset_lr.setEnabled(bool(on and self.current is not None and self.current.lr_edit is not None))
        self.btn_undo_approve.setEnabled(bool(on and self.current is not None
                                              and getattr(self.current, "approve_undo", None)))
        for w in widgets:
            w.setEnabled(on)
        if on:
            self._update_undo_buttons()

    # ---------------- adding files ----------------
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        e.acceptProposedAction()
        self.add_paths(paths)

    def open_files(self):
        exts = " ".join("*" + x for x in sorted(core.RAW_EXTS)) + " " + \
            " ".join("*" + x.upper() for x in sorted(core.RAW_EXTS))
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Add RAW photos", self.last_dir, f"RAW photos ({exts})")
        self.add_paths(files)

    def open_folder(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Add a folder (subfolders included)", self.last_dir)
        if d:
            self.add_paths([d])

    def add_paths(self, paths):
        found = []
        def consider(p):
            if os.path.splitext(p)[1].lower() in core.RAW_EXTS:      # includes .dng
                found.append(os.path.abspath(p))
        for p in paths:
            if os.path.isdir(p):
                for root, dirs, files in os.walk(p):     # only lists, never changes anything
                    dirs.sort()
                    for fn in sorted(files):
                        consider(os.path.join(root, fn))
            elif os.path.isfile(p):
                consider(p)

        new = [f for f in found if f not in self.items]
        if found:
            self.last_dir = os.path.dirname(found[0])
        opts = self._opts_snapshot()
        for f in new:
            it = Item(f)
            li = QtWidgets.QListWidgetItem(os.path.basename(f))
            li.setData(Qt.ItemDataRole.UserRole, f)
            li.setToolTip(f)
            li.setFlags(li.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            saved = self._restore_map.get(f)
            li.setCheckState(Qt.CheckState.Checked if saved and saved.get("checked") else Qt.CheckState.Unchecked)
            it.list_item = li
            gi = QtWidgets.QListWidgetItem(os.path.basename(f))
            gi.setData(Qt.ItemDataRole.UserRole, f)
            gi.setToolTip(f)
            gi.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            it.grid_item = gi
            if saved:
                it.rating = saved.get("rating")
                it.label = saved.get("label")
            self.items[f] = it
            self.list.addItem(li)
            self.grid.addItem(gi)
            self._update_list_item(it)
            self._requested.add(f)
            self._queue.append((self.gen, f, opts))
        if new:
            if not self._analyzing and self._an_done >= self._an_total:
                self._an_total = self._an_done = 0
                self._an_t0 = time.monotonic()
            self._an_total += len(new)
            self._pump()

        msg = f"Added {len(new)} photo(s)."
        if len(found) > len(new):
            msg += f" {len(found) - len(new)} already in the list."
        if not found:
            msg = "No RAW/DNG photos found in what was dropped."
        self.statusBar().showMessage(msg, 10000)
        if new and self.list.currentRow() < 0:
            self.list.setCurrentRow(0)
        self._update_count()

    def clear_list(self):
        if not self.items:
            return
        self.gen += 1                  # ignore results still in flight
        self.pool.clear()
        self.items.clear()
        self.cache.clear()
        self._requested.clear()
        self.current = None
        self._undo = {}
        self.act_undo_batch.setEnabled(False)
        self._restore_map = {}
        self._queue.clear()
        self._analyzing.clear()
        self._an_total = self._an_done = 0
        self._cull_why, self._cull_burst = {}, {}
        self.list.clear()
        self.grid.clear()
        self._thumb_pending.clear()
        self.view.set_image(None)
        self.view.set_message("Drag RAW photos or folders here")
        self.notes_lbl.setText("")
        self._set_controls_enabled(False)
        self._update_count()
        self._update_checked_label()

    # ---------------- background results ----------------
    def _pump(self):
        limit = max(1, self.pool.maxThreadCount())
        while self._queue and not self._paused and len(self._analyzing) < limit:
            gen, f, opts = self._queue.popleft()
            if gen != self.gen or f not in self.items:
                continue
            self._analyzing.add(f)
            self.pool.start(DecodeTask(gen, f, True, opts, self.signals))
        self._update_count()

    def set_paused(self, on):
        self._paused = bool(on)
        self.btn_pause.setText("▶ Resume analysis" if on else "⏸ Pause analysis")
        if not on:
            self._pump()
        self._update_count()

    def on_decoded(self, gen, path, res, err):
        if path in self._analyzing and gen == self.gen:
            self._analyzing.discard(path)
            self._an_done += 1
            self._pump()
        if gen != self.gen or path not in self.items:
            return
        self._requested.discard(path)
        it = self.items[path]
        if res is None:
            it.status, it.error = "error", err
            self._update_list_item(it)
            if it is self.current:
                self.view.set_image(None)
                self.view.set_message(f"Could not read this file:\n{err}")
            self._update_count()
            return

        dec = res["dec"]
        self._cache_put(path, dec)
        it.color, it.src_size = dec.color, (dec.cam.shape[1], dec.cam.shape[0])
        it.flip = dec.flip
        if "edit" in res:
            it.edit, it.auto_edit, it.wb = res["edit"], res.get("auto_edit", res["edit"]).copy(), res["wb"]
            if res.get("cull") is not None:
                it.cull = res["cull"]
                self.cull_timer.start()
            ex = res.get("existing") or {}
            it.lr_edit = ex.get("edit").copy() if ex.get("edit") is not None else None
            if it.rating is None and ex.get("rating") is not None:
                it.rating = ex["rating"]
            if it.label is None and ex.get("label"):
                it.label = ex["label"]
            saved = self._restore_map.pop(path, None)
            if saved is not None:                       # last session: put back your edit and decision
                if saved.get("edit"):
                    it.edit = core.edit_from_dict(saved["edit"])
                st = saved.get("status", "ready")
                it.status = st if st in ("ready", "approved", "modified", "rejected") else "ready"
                it.edited = bool(saved.get("edited", False))
            if it.status == "loading":
                it.status = "ready"
            self._set_thumb(it, res["thumb"])
            if saved is not None:
                self._queue_thumb(it)
            self._update_list_item(it)
            self._update_count()
            if self.chk_autowrite.isChecked() and it.status == "ready" and saved is None:
                self._write(it, quiet=True)
        if it is self.current:
            self.load_controls()
            self.refresh_view()

    def _cache_put(self, path, dec):
        self.cache[path] = dec
        self.cache.move_to_end(path)
        while len(self.cache) > CACHE_SIZE:
            self.cache.popitem(last=False)

    def _dec(self, it):
        dec = self.cache.get(it.path)
        if dec is not None:
            self.cache.move_to_end(it.path)
            return dec
        if it.status != "error" and it.path not in self._requested:
            self._requested.add(it.path)
            self.pool.start(DecodeTask(self.gen, it.path, False, {}, self.signals))
        return None

    # ---------------- list / navigation ----------------
    def _update_list_item(self, it):
        if getattr(it, "_thumb", None) is not None and getattr(it, "_thumb_label", None) != it.label:
            self._set_thumb(it, it._thumb)            # label changed: redraw the thumbnail's colour band
        label, col = STATUS_STYLE[it.status]
        has = (os.path.exists(core.dng_export_path(it.path)) if it.path.lower().endswith(".dng")
               else os.path.exists(core.sidecar_path(it.path)))
        sidecar = " · has XMP" if has and it.status != "approved" else ""
        extra_meta = self._meta_text(it)
        extra = ""
        if it.wb is not None and it.wb.applied and it.wb.confidence < 0.5 and it.status == "ready":
            extra = " · check WB"
        extra += self._cull_text(it)
        if it.edited:
            extra = " · ✎ Edited" + extra
        link = "🔗 " if it.path in getattr(self, "_linked", ()) else ""
        it.list_item.setText(f"{link}{os.path.basename(it.path)}\n{label}{extra}{sidecar}"
                             + (f"\n{extra_meta}" if extra_meta else ""))
        it.list_item.setBackground(QtGui.QBrush(col) if col else QtGui.QBrush())
        it.list_item.setForeground(QtGui.QBrush(QtGui.QColor("white")) if col else QtGui.QBrush())
        if it.error:
            it.list_item.setToolTip(f"{it.path}\n{it.error}")
        self._update_grid_item(it)
        if hasattr(self, "show_combo"):                      # keep the "Show …" filter right for this photo
            q = self.search.text().strip().lower()
            hide = (bool(q) and q not in os.path.basename(it.path).lower()) or not self._show_ok(it)
            if it.list_item.isHidden() != hide:
                it.list_item.setHidden(hide)
                if it.grid_item is not None:
                    it.grid_item.setHidden(hide)

    # ---------------- cull suggestions ----------------
    def _ordered_items(self):
        out = []
        for r in range(self.list.count()):
            it = self.items.get(self.list.item(r).data(Qt.ItemDataRole.UserRole))
            if it is not None:
                out.append(it)
        return out

    def _refresh_cull(self):
        """Work out the suggested rejects again (soft, blown, very dark, look-alikes) and relabel what changed."""
        ordered = [(it.path, it.cull) for it in self._ordered_items()]
        why, burst = cull.suggestions(ordered), cull.burst_labels(ordered)
        changed = {p for p in set(why) | set(self._cull_why) if why.get(p) != self._cull_why.get(p)}
        changed |= {p for p in set(burst) | set(self._cull_burst) if burst.get(p) != self._cull_burst.get(p)}
        self._cull_why, self._cull_burst = why, burst
        for p in changed:
            it = self.items.get(p)
            if it is not None and it.list_item is not None:
                self._update_list_item(it)
        self._update_cull_action()

    def _cull_text(self, it) -> str:
        bits = []
        if it.path in self._cull_burst:
            bits.append(self._cull_burst[it.path])
        if it.status in ("ready", "modified"):
            for w in self._cull_why.get(it.path, []):
                if not w.startswith("similar"):
                    bits.append(w + "?")
        return "".join(" · " + b for b in bits)

    def cull_candidates(self):
        """Photos suggested for rejection that you haven't approved or rejected yet."""
        return [it for it in self._ordered_items()
                if it.path in self._cull_why and it.status in ("ready", "modified")]

    def _update_cull_action(self):
        n = len(self.cull_candidates())
        self.act_cull.setText(f"Cull suggestions ({n})…" if n else "Cull suggestions…")

    def open_cull(self):
        todo = self.cull_candidates()
        if not todo:
            QtWidgets.QMessageBox.information(
                self, "Cull suggestions",
                "Nothing to suggest right now: no unreviewed photo looks soft, blown out, very dark or like a "
                "weaker copy of its neighbour." + ("\n\nSome photos are still being analysed."
                                                  if self._queue or self._analyzing else ""))
            return
        counts = {}
        for it in todo:
            for w in self._cull_why[it.path]:
                key = "a weaker frame of a burst or near-duplicate" if w.startswith("similar") else w
                counts[key] = counts.get(key, 0) + 1
        lines = "".join(f"<li>{n} {k}</li>" for k, n in sorted(counts.items(), key=lambda kv: -kv[1]))
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Cull suggestions")
        box.setIcon(QtWidgets.QMessageBox.Icon.Question)
        box.setText(f"<b>{len(todo)} photo{'s' if len(todo) != 1 else ''} you may want to reject:</b><ul>{lines}</ul>")
        box.setInformativeText(
            "Check them to look them over first (Grid view shows them side by side; X rejects the selected ones), "
            "or reject them all now. In a burst, the sharpest frame (★) is never suggested.\n\n"
            "Rejecting only marks them: nothing is written, moved or deleted, and you can approve any of them later.")
        details = "\n".join(f"{os.path.basename(it.path)}: {', '.join(self._cull_why[it.path])}" for it in todo)
        box.setDetailedText(details)
        check_btn = box.addButton("Check them", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        reject_btn = box.addButton(f"Reject {len(todo)}", QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(check_btn)
        box.exec()
        if box.clickedButton() is check_btn:
            paths = {it.path for it in todo}
            self.check_where(lambda it: it.path in paths)
            self.statusBar().showMessage(f"Checked {len(todo)} suggested reject(s).", 6000)
        elif box.clickedButton() is reject_btn:
            for it in todo:
                it.status = "rejected"
                self._update_list_item(it)
            self._update_count()
            self._update_cull_action()
            self.statusBar().showMessage(f"Rejected {len(todo)} photo(s). Nothing was written or deleted.", 8000)

    def _update_grid_item(self, it):
        gi = it.grid_item
        if gi is None or it.list_item is None:
            return
        label, col = STATUS_STYLE[it.status]
        mark = "☑ " if it.list_item.checkState() == Qt.CheckState.Checked else ""
        low = " · check WB" if (it.wb is not None and it.wb.applied and it.wb.confidence < 0.5
                                and it.status == "ready") else ""
        low += self._cull_text(it)
        if it.edited:
            low = " · ✎ Edited" + low
        meta = self._meta_text(it)
        link = "🔗 " if it.path in getattr(self, "_linked", ()) else ""
        gi.setText(f"{link}{mark}{os.path.basename(it.path)}\n{label}{low}" + (f"\n{meta}" if meta else ""))
        gi.setBackground(QtGui.QBrush(col) if col else QtGui.QBrush())
        gi.setForeground(QtGui.QBrush(QtGui.QColor("white")))
        if it.error:
            gi.setToolTip(f"{it.path}\n{it.error}")

    def _set_thumb(self, it, rgb):
        it._thumb, it._thumb_label = rgb, it.label
        pm = QtGui.QPixmap.fromImage(to_qimage(rgb))
        col = LABEL_COLOURS.get(it.label or "")
        if col:                                   # colour label: a band along the bottom and a stripe on the left
            p = QtGui.QPainter(pm)
            band = max(6, pm.height() // 9)
            p.fillRect(0, pm.height() - band, pm.width(), band, QtGui.QColor(col))
            p.fillRect(0, 0, max(4, band // 2), pm.height(), QtGui.QColor(col))
            p.end()
        icon = QtGui.QIcon(pm)
        if it.list_item is not None:
            it.list_item.setIcon(icon)
        if it.grid_item is not None:
            it.grid_item.setIcon(icon)

    def _queue_thumb(self, it):
        self._thumb_pending.add(it.path)
        self.thumb_timer.start()

    def _flush_thumbs(self):
        pending, self._thumb_pending = self._thumb_pending, set()
        for path in pending:
            it = self.items.get(path)
            if it is None or it.edit is None:
                continue
            self.pool.start(ThumbTask(self.gen, path, it.edit, self.cache.get(path), self.thumb_signals))

    def on_thumb_done(self, gen, path, thumb, fresh):
        it = self.items.get(path)
        if gen != self.gen or it is None:
            return
        if fresh is not None and path not in self.cache:
            self._cache_put(path, fresh)
        self._set_thumb(it, thumb)

    # ---------------- grid view / selection sync ----------------
    def set_grid(self, on):
        self.stack.setCurrentIndex(1 if on else 0)
        if on:
            self._sync_grid_from_list()
            cur = self.list.currentRow()
            if cur >= 0:
                self.grid.scrollToItem(self.grid.item(cur))
            self.grid.setFocus()
        else:
            self.refresh_view()
            self.view.setFocus()
        self.act_grid.setText("▣ Single view (G)" if on else "▦ Grid view (G)")

    def in_grid(self):
        return self.stack.currentIndex() == 1

    def _sync_grid_from_list(self):
        if self._syncing:
            return
        self._syncing = True
        try:
            rows = {self.list.row(li) for li in self.list.selectedItems()}
            for r in range(self.grid.count()):
                self.grid.item(r).setSelected(r in rows)
            cur = self.list.currentRow()
            if cur >= 0 and self.grid.currentRow() != cur:
                self.grid.setCurrentRow(cur, QtCore.QItemSelectionModel.SelectionFlag.NoUpdate)
        finally:
            self._syncing = False
        self._update_scope_label()

    def _sync_list_from_grid(self):
        if self._syncing:
            return
        self._syncing = True
        try:
            rows = {self.grid.row(gi) for gi in self.grid.selectedItems()}
            for r in range(self.list.count()):
                self.list.item(r).setSelected(r in rows)
        finally:
            self._syncing = False
        self._update_scope_label()

    def _grid_current_changed(self, row):
        if self._syncing or row < 0 or row == self.list.currentRow():
            return
        self._syncing = True
        try:
            self.list.setCurrentRow(row, QtCore.QItemSelectionModel.SelectionFlag.NoUpdate)
        finally:
            self._syncing = False
        self.on_row(row)

    def _on_list_item_changed(self, li):
        self._update_checked_label()
        it = self.items.get(li.data(Qt.ItemDataRole.UserRole))
        if it is not None:
            self._update_grid_item(it)
        self._update_scope_label()

    # ---------------- "Edits apply to" ----------------
    def edit_scope(self):
        return ("current", "selected", "checked")[self.scope_combo.currentIndex()]

    def _scope_targets(self):
        """Photos (other than the current one) that slider changes are copied to."""
        sc = self.edit_scope()
        if sc == "current":
            return []
        return [t for t in self.scope_items(sc) if t is not self.current and t.edit is not None]

    def _update_scope_label(self):
        if not hasattr(self, "scope_lbl"):
            return
        sc = self.edit_scope()
        targets = self._scope_targets() if sc != "current" else []
        n = len(targets)
        how = "added to each photo's own values" if self.chk_relative.isChecked() else "copied exactly"
        word = "selected" if sc == "selected" else "checked"
        if sc == "current":
            self.scope_lbl.setText("Changes affect only the photo shown.")
        else:
            self.scope_lbl.setText(f"This photo + {n} other {word} photo(s) · changes {how}"
                                   if n else f"No other {word} photos – only this photo changes.")
        # Make multi-photo editing impossible to miss: a banner over the photo and 🔗 in the list
        if hasattr(self, "sync_banner"):
            if n:
                self.sync_banner_lbl.setText(
                    f"🔗  Editing {n + 1} photos at once: slider changes are {how} to every 🔗 photo "
                    f"({word} in the list)")
            self.sync_banner.setVisible(bool(n))
        linked = {t.path for t in targets}
        if n and self.current is not None:
            linked.add(self.current.path)
        old = getattr(self, "_linked", set())
        self._linked = linked
        for path in old ^ linked:
            it = self.items.get(path)
            if it is not None and it.list_item is not None:
                self._update_list_item(it)
                self._update_grid_item(it)

    def _propagate(self, old, new):
        """Copy the settings that just changed on the current photo to the scope's other photos."""
        targets = self._scope_targets()
        if not targets:
            return
        relative = self.chk_relative.isChecked()
        changed = [k for k in core.SETTINGS if getattr(old, k) != getattr(new, k)]
        wb_to_shot = new.wb_mode == "as_shot" and old.wb_mode != "as_shot"
        lens = old.lens_profile != new.lens_profile
        sky_flip = old.sky_on != new.sky_on
        subj_flip = old.subj_on != new.subj_on
        if (not changed and not wb_to_shot and not lens and old.wb_mode == new.wb_mode
                and not sky_flip and not subj_flip):
            return
        key = ("sync",) + tuple(changed)
        for t in targets:
            self._record_undo(t, t.edit.copy(), key=key)
            te = t.edit
            if sky_flip:
                te.sky_on = new.sky_on
            if subj_flip:
                te.subj_on = new.subj_on
            if new.wb_mode == "as_shot":
                if wb_to_shot:
                    te.wb_mode = "as_shot"
                    if t.color is not None:
                        te.temp, te.tint = (round(v) for v in t.color.as_shot_temp_tint)
            elif old.wb_mode != new.wb_mode and not relative:
                te.wb_mode, te.temp, te.tint = "custom", new.temp, new.tint
            for k in changed:
                st = core.SETTINGS[k]
                if k in ("temp", "tint"):
                    if new.wb_mode == "as_shot":
                        continue                     # the sliders just snapped to this photo's as-shot
                    if relative:
                        if te.wb_mode != "custom" and t.color is not None:
                            te.temp, te.tint = (round(v) for v in t.color.as_shot_temp_tint)
                        val = getattr(te, k) + (getattr(new, k) - getattr(old, k))
                    else:
                        val = getattr(new, k)
                    te.wb_mode = "custom"
                else:
                    val = (getattr(te, k) + (getattr(new, k) - getattr(old, k))) if relative else getattr(new, k)
                val = min(max(val, st.lo), st.hi)
                val = int(round(val)) if st.integer else round(val, st.decimals)
                setattr(te, k, val)
                if st.optional:
                    te.touched.add(k)
                if k == "angle":
                    te.level_mode = "Manual"
            if lens:
                te.lens_profile = new.lens_profile
            self._mark_changed(t)
            self._queue_thumb(t)
        self.statusBar().showMessage(
            f"Change applied to this photo and {len(targets)} other photo(s)"
            + (" (relative)." if relative else "."), 4000)

    def _update_count(self):
        n = len(self.items)
        c = {k: 0 for k in STATUS_STYLE}
        for it in self.items.values():
            c[it.status] += 1
        busy = bool(self._queue or self._analyzing)
        prog = ""
        if busy and self._an_total:
            done = self._an_done
            prog = f"\nAnalyzing {min(done + 1, self._an_total)} of {self._an_total}"
            if self._paused:
                prog += " · paused"
            elif done >= 2:
                left = (time.monotonic() - self._an_t0) / done * (self._an_total - done)
                prog += f" · about {int(left // 60)} min {int(left % 60):02d} s left" if left >= 60 else \
                    f" · about {int(left)} s left"
        self.count_lbl.setText(
            f"{n} photos · {c['ready'] + c['modified']} to review · {c['approved']} approved · "
            f"{c['rejected']} rejected" + (f" · {c['error']} errors" if c['error'] else "")
            + f" · ✎ {sum(1 for it in self.items.values() if it.edited)} edited" + prog)
        if hasattr(self, "btn_pause"):
            self.btn_pause.setVisible(busy or self._paused)
        if hasattr(self, "act_cull"):
            self._update_cull_action()

    def on_row(self, row):
        if row < 0:
            return
        if self.view.crop_mode:
            self.btn_crop.setChecked(False)           # moving on: apply the crop to the photo you were on
        path = self.list.item(row).data(Qt.ItemDataRole.UserRole)
        self.current = self.items.get(path)
        self.btn_eyedrop.setChecked(False)
        self.load_controls()
        if not self.in_grid():
            self.refresh_view()
        if not self._syncing and self.grid.currentRow() != row:
            self._syncing = True
            try:
                self.grid.setCurrentRow(row, QtCore.QItemSelectionModel.SelectionFlag.NoUpdate)
                self.grid.scrollToItem(self.grid.item(row))
            finally:
                self._syncing = False
        self._update_scope_label()

    def step(self, d):
        n = self.list.count()
        r = self.list.currentRow()
        while 0 <= r + d < n:
            r += d
            if not self.list.item(r).isHidden():
                self.list.setCurrentRow(r)
                return

    def next_to_review(self):
        """Move to the next photo that still needs a decision."""
        n = self.list.count()
        start = self.list.currentRow()
        for k in range(1, n + 1):
            r = (start + k) % n
            it = self.items[self.list.item(r).data(Qt.ItemDataRole.UserRole)]
            if it.status in ("ready", "modified", "loading"):
                self.list.setCurrentRow(r)
                return
        self.statusBar().showMessage("All photos reviewed. 🎉", 8000)

    # ---------------- controls <-> edit ----------------
    def load_controls(self):
        it = self.current
        ok = it is not None and it.edit is not None
        self._set_controls_enabled(ok)
        if not ok:
            self.notes_lbl.setText(it.error if it else "")
            return
        e = it.edit
        self._loading_controls = True
        self.wb_combo.setCurrentIndex(1 if e.wb_mode == "custom" else 0)
        ae = it.auto_edit
        for key, s in self.sliders.items():
            s.auto_val = None                      # no marker flash while switching photos
            s.setValue(getattr(e, key))
            s.set_auto(getattr(ae, key) if ae is not None else None)
        self.level_combo.setCurrentText(e.level_mode if e.level_mode in core.LEVEL_MODES else "Manual")
        self.chk_lens.setChecked(e.lens_profile)
        self.chk_subject_crop.setChecked(bool(e.crop_box))
        self.chk_constrain.setChecked(e.constrain_crop)
        self.box_sky.setChecked(e.sky_on)
        self.box_subj.setChecked(e.subj_on)
        self._loading_controls = False
        self._update_undo_buttons()
        self._update_rating_label()
        self.notes_lbl.setText("   ".join(e.notes))

    def on_wb_slider(self, _v):
        if self._loading_controls:
            return
        self._loading_controls = True
        self.wb_combo.setCurrentIndex(1)
        self._loading_controls = False
        self.on_control()

    def on_control(self, *_):
        it = self.current
        if self._loading_controls or it is None or it.edit is None:
            return
        e = it.edit
        old = e.copy()
        e.wb_mode = "custom" if self.wb_combo.currentIndex() == 1 else "as_shot"
        if e.wb_mode == "as_shot" and it.color is not None:
            t, ti = it.color.as_shot_temp_tint
            self._loading_controls = True
            self.s_temp.setValue(round(t))
            self.s_tint.setValue(round(ti))
            self._loading_controls = False
        e.temp, e.tint = self.s_temp.value(), self.s_tint.value()
        for key, s in self.sliders.items():
            if key in ("temp", "tint"):
                continue
            st = core.SETTINGS[key]
            v = s.value()
            val = int(round(v)) if st.integer else round(v, st.decimals)
            prev = getattr(e, key)          # (not "old": that's the whole edit before this change)
            if st.optional and (val != prev or val != st.default):
                e.touched.add(key)          # now written to the sidecar
            if key == "angle" and abs(val - prev) > 1e-9:
                e.level_mode = "Manual"
                self._loading_controls = True
                self.level_combo.setCurrentText("Manual")
                self._loading_controls = False
            setattr(e, key, val)
        e.lens_profile = self.chk_lens.isChecked()
        e.sky_on = self.box_sky.isChecked() and not core.NO_AI_MASKS
        e.subj_on = self.box_subj.isChecked() and not core.NO_AI_MASKS
        changed = tuple(k for k in core.SETTINGS if getattr(old, k) != getattr(e, k))
        flags = tuple(f for f in ("wb_mode", "lens_profile", "sky_on", "subj_on", "level_mode")
                      if getattr(old, f) != getattr(e, f))
        if not changed and not flags:
            return
        self._record_undo(it, old, key=changed + flags)
        self._update_undo_buttons()
        self._mark_changed(it)
        self._propagate(old, e)
        self._fast_next = True
        self.render_timer.start()

    def _mark_changed(self, it):
        relabel = False
        if it.status == "approved":
            it.status = "modified"
            relabel = True
        if not it.edited and self._settings.get("mark_edited", True):
            it.edited = True
            relabel = True
        if relabel:
            self._update_list_item(it)
            self._update_count()

    def set_edited(self, items, on: bool):
        for it in items:
            if it.edited != on:
                it.edited = on
                self._update_list_item(it)
        self._update_count()
        s = "s" if len(items) != 1 else ""
        self.statusBar().showMessage(f"Marked {len(items)} photo{s} as {'edited ✎' if on else 'not edited'}.", 4000)

    # ---------------- rendering ----------------
    def refresh_view(self):
        it = self.current
        if it is None:
            return
        if it.edit is None:
            self.view.set_image(None)
            self.hist.set_histogram(None)
            self.view.set_message("Analyzing…" if it.status == "loading" else f"Could not read:\n{it.error}")
            return
        dec = self._dec(it)
        if dec is None:
            self.view.set_image(None)
            self.hist.set_histogram(None)
            self.view.set_message("Loading preview…")
            return
        before = self.chk_before.isChecked() or self._hold_before
        split = self.chk_split.isChecked() and not before
        fast = self._fast_next and not before and dec.color.model is None
        self._fast_next = False
        if fast:
            dec = core.half_decoded(dec, it.edit)      # quick draft while the slider moves
            self.settle_timer.start()
        else:
            self.settle_timer.stop()
        shown = it.edit
        if self.view.crop_mode:
            before, split = False, False
            shown = it.edit.copy()
            shown.crop_box = None                     # crop mode: the whole straightened frame + the frame
        img, self.geom = core.render(dec, shown, original=before,
                                     show_masks=self.chk_masks.isChecked() and not before)
        if split:
            orig, _g = core.render(dec, it.edit, original=True, rotate=False)
            orig, _g = core.rotate_and_crop(orig, it.edit.angle, it.edit.crop_box)
            if orig.shape != img.shape:
                import cv2
                orig = cv2.resize(orig, (img.shape[1], img.shape[0]), interpolation=cv2.INTER_AREA)
            self._split_cache = (orig, img)
        if not before and not fast and it.list_item is not None:
            import cv2
            h, w = img.shape[:2]
            k = min(GRID_W / w, GRID_H / h)
            small = cv2.resize(img, (max(1, int(w * k)), max(1, int(h * k))), interpolation=cv2.INTER_AREA)
            self._set_thumb(it, small)
            self._thumb_pending.discard(it.path)
        overlay = []
        if (self.chk_samples.isChecked() and not before and it.wb is not None and it.wb.applied):
            for pts, col in ((it.wb.bright_pts, QtGui.QColor(0, 230, 255)),
                             (it.wb.dark_pts, QtGui.QColor(255, 60, 220))):
                for x, y in pts:
                    dx, dy = self.geom.to_display(x, y)
                    if 0 <= dx < img.shape[1] and 0 <= dy < img.shape[0]:
                        overlay.append((dx, dy, col))
        badge = "BEFORE (as shot)" if before else ""
        if self.view.crop_mode:
            badge = "Crop: drag the corners or the frame · Enter = done · Esc = cancel"
        if not before and it.edit.upright != "Off":
            if it.edit.upright == "Guided":
                n = len(it.edit.upright_guides or [])
                badge = (f"Upright Guided: {n} suggested guides (blue = vertical, pink = horizontal) · "
                         "click a guide to swap it for the next best line" if n >= 2 else
                         "Upright Guided: not enough clear lines for guides")
            elif self.geom.H is None:
                badge = f"Upright {it.edit.upright}: nothing to correct here"
            elif it.edit.upright_matrices:
                badge = f"Upright {it.edit.upright}: Lightroom will show this same correction"
            else:
                badge = f"Upright {it.edit.upright}: approximate preview – Lightroom computes its own"
        elif not before and not self.view.crop_mode and (it.edit.sky_on or it.edit.subj_on):
            on = " + ".join(n for n, f in (("Sky", it.edit.sky_on), ("Subject", it.edit.subj_on)) if f)
            badge = f"{on} mask: approximate preview – Lightroom computes the exact mask"
        self.view.split_on = split
        self.hist.clip_on = self.chk_clip.isChecked()
        self.hist.set_histogram(core.histogram(img), before)     # the photo as shown (split: the edited one)
        if split:
            img = self._split_image()
            badge = ""
        else:
            self._split_cache = None
            if self.chk_clip.isChecked():
                img = core.clip_overlay(img)
        self.view.lines = [] if (before or split) else self._guide_lines(it, dec)
        self.view.set_image(to_qimage(img), overlay, badge)
        self.view.set_message("Click a neutral spot…" if self.btn_eyedrop.isChecked() else "")

    # ---------------- tools ----------------
    def do_auto_wb(self):
        self.run_auto_groups(["wb"])

    def _current_dec(self):
        it = self.current
        if it is None or it.edit is None:
            return None, None
        dec = self._dec(it)
        if dec is None:
            self.statusBar().showMessage("Still loading this photo, try again in a moment.", 4000)
        return it, dec

    def run_auto_groups(self, groups, user_rules=False):
        it, dec = self._current_dec()
        if dec is None:
            return
        self._record_undo(it, it.edit.copy())
        res = auto.run_auto(dec, it.edit, groups, self._opts_snapshot(), user_rules=user_rules)
        self._remember_auto(it, auto.auto_values(it.edit, groups))
        if res.get("wb") is not None:
            it.wb = res["wb"]
            self.chk_samples.setChecked(True)
        self._mark_changed(it)
        self.load_controls()
        self.refresh_view()

    def auto_enhance(self):
        groups = set(self.opts.groups)
        groups.discard("level")                 # straightening has its own control
        if "wb" in groups and self.current and self.current.edit and self.current.edit.wb_mode == "custom":
            pass                                 # re-running WB is fine; it is the user's choice
        self.run_auto_groups(list(groups), user_rules=True)

    def do_level(self, mode):
        it, dec = self._current_dec()
        if dec is None:
            return
        if mode == "Manual":
            return
        self._record_undo(it, it.edit.copy())
        it.guide_rejects = []
        auto.apply_level_mode(dec, it.edit, mode, self.opts.allow_upright)
        self._remember_auto(it, {"angle": it.edit.angle})
        self._mark_changed(it)
        self.load_controls()
        self.refresh_view()

    def on_subject_crop(self, on):
        if self._loading_controls:
            return
        it, dec = self._current_dec()
        if dec is None:
            return
        self._record_undo(it, it.edit.copy())
        if on:
            note = auto.apply_subject_crop(dec, it.edit)
            if not it.edit.crop_box:
                self.statusBar().showMessage(note, 6000)
        else:
            it.edit.crop_box = None
            auto.replace_note(it.edit, "Subject crop", "Subject crop: off")
        self._mark_changed(it)
        self.load_controls()
        self.refresh_view()

    # ---------------- Upright: constrain crop, guides ----------------
    def _plain_render(self, it, dec):
        """The photo with no Upright and no crop (what Upright is solved on)."""
        plain = it.edit.copy()
        plain.upright, plain.crop_box, plain.upright_matrices = "Off", None, None
        return core.render(dec, plain, rotate=False, look=False)[0]

    def on_constrain(self, on):
        if self._loading_controls:
            return
        it, dec = self._current_dec()
        if dec is None:
            return
        self._record_undo(it, it.edit.copy())
        e = it.edit
        e.constrain_crop = on
        if e.upright != "Off" and e.upright_matrices:
            h, w = dec.cam.shape[:2]
            e.crop_box = core.constrained_box(e.upright_matrices.get(e.upright), w, h) if on else None
        self._mark_changed(it)
        self.load_controls()
        self.refresh_view()

    def _guide_lines(self, it, dec):
        """Guided Upright's guides in displayed-image coordinates (blue vertical, pink horizontal)."""
        e = it.edit
        if e.upright != "Guided" or not e.upright_guides:
            return []
        h, w = dec.cam.shape[:2]
        out = []
        for g in e.upright_guides:
            x1, y1 = self.geom.to_display(g[0] * w, g[1] * h)
            x2, y2 = self.geom.to_display(g[2] * w, g[3] * h)
            vertical = abs(g[3] - g[1]) * h > abs(g[2] - g[0]) * w
            out.append((x1, y1, x2, y2, QtGui.QColor(80, 190, 255) if vertical else QtGui.QColor(255, 110, 200)))
        return out

    def _click_guide(self, x, y) -> bool:
        """Clicking a suggested guide swaps it for the next best line of the same kind."""
        it, dec = self._current_dec()
        if dec is None or it.edit.upright != "Guided" or not self.view.lines:
            return False
        best, bd = None, 12.0 / max(self.view._target()[1], 1e-6)       # ~12 screen pixels
        for i, (x1, y1, x2, y2, _c) in enumerate(self.view.lines):
            dx, dy = x2 - x1, y2 - y1
            t = min(max(((x - x1) * dx + (y - y1) * dy) / max(dx * dx + dy * dy, 1e-9), 0), 1)
            d = math.hypot(x - (x1 + t * dx), y - (y1 + t * dy))
            if d < bd:
                best, bd = i, d
        if best is None:
            return False
        kinds = core.LEVEL_MODES.get(it.edit.level_mode, {}).get("guides", "v")
        self._record_undo(it, it.edit.copy())
        it.guide_rejects.append(it.edit.upright_guides[best])
        img = self._plain_render(it, dec)
        it.edit.upright_guides = core.suggest_guides(img, kinds, exclude=it.guide_rejects)
        note = auto.solve_upright(it.edit, img)
        auto.replace_note(it.edit, "Straighten", note)
        self._mark_changed(it)
        self.load_controls()
        self.refresh_view()
        n = len(it.edit.upright_guides or [])
        self.statusBar().showMessage(f"Guide swapped for the next best line ({n} guides)." if n >= 2 else
                                     "No more clear lines to use – re-run the mode to start over.", 5000)
        return True

    # ---------------- hand crop ----------------
    def _crop_aspect_value(self, img_w, img_h):
        t = self.crop_aspect_combo.currentText()
        if t == "Free":
            return None
        if t == "Original":
            return img_w / max(img_h, 1)
        a, b = (float(v) for v in t.split(":"))
        return a / b

    def _crop_aspect_changed(self):
        v = self.view
        if v.crop_mode and v.img is not None:
            v.crop_aspect = self._crop_aspect_value(v.img.width(), v.img.height())
            v.conform_crop()

    def on_crop_toggle(self, on):
        it = self.current
        if on:
            if it is None or it.edit is None:
                self.btn_crop.setChecked(False)
                return
            self.view.crop_mode = True
            for k in self._crop_keys:
                k.setEnabled(True)
            self.view.crop_box = list(it.edit.crop_box or [0.0, 0.0, 1.0, 1.0])
            self.refresh_view()                       # shows the whole straightened photo
            self._crop_aspect_changed()
            if not it.edit.crop_box:
                self.view.conform_crop()
        else:
            if self.view.crop_mode and not getattr(self, "_crop_cancel", False):
                self._commit_crop()
            self._crop_cancel = False
            self.view.crop_mode = False
            for k in self._crop_keys:
                k.setEnabled(False)
            self.refresh_view()

    def cancel_crop(self):
        if self.view.crop_mode:
            self._crop_cancel = True
            self.btn_crop.setChecked(False)

    def _commit_crop(self):
        it = self.current
        if it is None or it.edit is None:
            return
        box = [round(float(x), 5) for x in self.view.crop_box]
        if box[0] <= 0.002 and box[1] <= 0.002 and box[2] >= 0.998 and box[3] >= 0.998:
            box = None                                # the whole frame: no crop
        if box == it.edit.crop_box:
            return
        self._record_undo(it, it.edit.copy())
        it.edit.crop_box = box
        auto.replace_note(it.edit, "Subject crop", "Crop: set by hand" if box else "Crop: none")
        self._mark_changed(it)
        self.load_controls()
        self.statusBar().showMessage("Crop applied – written to Lightroom when approved." if box
                                     else "Crop removed.", 4000)

    def reset_crop(self):
        if self.view.crop_mode:
            self.view.crop_box = [0.0, 0.0, 1.0, 1.0]
            self._crop_aspect_changed()
            self.view.update()
            return
        it = self.current
        if it is None or it.edit is None or not it.edit.crop_box:
            return
        self._record_undo(it, it.edit.copy())
        it.edit.crop_box = None
        auto.replace_note(it.edit, "Subject crop", "Crop: none")
        self._mark_changed(it)
        self.load_controls()
        self.refresh_view()

    @staticmethod
    def _replace_note(edit, prefix, note):
        auto.replace_note(edit, prefix, note)

    def on_eyedrop_toggle(self, on):
        self.view.crosshair = on
        self.view.set_message("Click a neutral spot…" if on else "")

    def on_image_click(self, x, y):
        it = self.current
        if not self.btn_eyedrop.isChecked() and self._click_guide(x, y):
            return
        if not self.btn_eyedrop.isChecked() or it is None or it.edit is None:
            return
        dec = self._dec(it)
        if dec is None:
            return
        sx, sy = self.geom.to_source(x, y)
        res = core.wb_from_point(dec, sx, sy)
        self.btn_eyedrop.setChecked(False)
        if res is None:
            self.statusBar().showMessage("That spot is clipped or too dark – try another neutral area.", 6000)
            return
        self._record_undo(it, it.edit.copy())
        it.edit.wb_mode, it.edit.temp, it.edit.tint = "custom", round(res[0]), round(res[1])
        self._replace_note(it.edit, "Auto WB", f"Auto WB: eyedropper → {round(res[0])}K / tint {round(res[1]):+d}")
        it.wb = None
        self._mark_changed(it)
        self.load_controls()
        self.refresh_view()

    def _remember_auto(self, it, values):
        """Auto just decided these settings: they become the photo's auto values (ticks, ↺, Reset to auto)."""
        if it.auto_edit is None:
            it.auto_edit = it.edit.copy()
        for k, v in values.items():
            setattr(it.auto_edit, k, v)

    # ---------------- reset several photos ----------------
    RESET_KINDS = {
        "auto": ("auto settings", "the automatic edit RAW Review proposed"),
        "shot": ("As Shot", "the camera's settings: no edits at all"),
        "lr": ("Lightroom settings", "the settings the photo already had in Lightroom (photos without any are skipped)"),
    }

    def reset_photos(self, items, kind, ask=True):
        """Reset these photos' edits to their auto proposal, As Shot, or their Lightroom settings.
        Undo with Undo batch (all of them) or Ctrl+Z (one photo)."""
        items = [it for it in items if it is not None and it.edit is not None]
        if kind == "auto":
            items = [it for it in items if it.auto_edit is not None]
        elif kind == "shot":
            items = [it for it in items if it.color is not None]
        else:
            items = [it for it in items if it.lr_edit is not None]
        name, what = self.RESET_KINDS[kind]
        if not items:
            self.statusBar().showMessage(f"No photos to reset to {name}.", 5000)
            return
        if ask and len(items) > 1:
            r = QtWidgets.QMessageBox.question(
                self, "Reset photos",
                f"Reset {len(items)} photos to {name}?\n\nThat's {what}. All other edits on them are "
                "replaced. You can undo it with Auto ▾ → Undo batch.")
            if r != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        self._undo = {it.path: (it.edit.copy(), it.status, it.wb, it.edited) for it in items}
        self.act_undo_batch.setEnabled(True)
        for it in items:
            self._record_undo(it, it.edit.copy())
            if kind == "auto":
                it.edit = it.auto_edit.copy()
                it.edited = False
            elif kind == "shot":
                it.edit = core.as_shot_edit(types.SimpleNamespace(color=it.color))
                it.edit.notes = ["Reset to As Shot"]
            else:
                it.edit = it.lr_edit.copy()
            it.guide_rejects = []
            self._mark_changed(it)
            if kind == "auto":
                it.edited = False
            self._update_list_item(it)
            self._update_grid_item(it)
            self._queue_thumb(it)
        self._update_count()
        self.load_controls()
        self.refresh_view()
        self.statusBar().showMessage(f"Reset {len(items)} photo(s) to {name} (not yet approved). "
                                     "Auto ▾ → Undo batch puts them back.", 8000)

    def _reset_menu(self, menu, items_fn, label_fn):
        """Fill `menu` with the three resets for the photos items_fn() returns."""
        menu.clear()
        n = len(items_fn())
        for kind in ("auto", "shot", "lr"):
            name = self.RESET_KINDS[kind][0]
            a = menu.addAction(f"Reset {label_fn(n)} to {name}", lambda k=kind: self.reset_photos(items_fn(), k))
            a.setEnabled(n > 0)
            a.setToolTip(self.RESET_KINDS[kind][1])

    def reset_auto(self):
        it = self.current
        if it and it.auto_edit and it.edit is not None:
            self._record_undo(it, it.edit.copy())
            it.edit = it.auto_edit.copy()
            self._mark_changed(it)
            it.edited = False                        # back to the untouched auto proposal
            self._update_list_item(it)
            self._update_count()
            self.load_controls()
            self.refresh_view()

    def reset_shot(self):
        it = self.current
        dec = self._dec(it) if it else None
        if dec is not None and it.edit is not None:
            self._record_undo(it, it.edit.copy())
            it.edit = core.as_shot_edit(dec)
            it.edit.notes = ["Reset to As Shot"]
            self._mark_changed(it)
            self.load_controls()
            self.refresh_view()

    def copy_settings(self):
        if self.current and self.current.edit:
            self.clip_edit = self.current.edit.copy()
            self.clip_name = os.path.basename(self.current.path)
            self.statusBar().showMessage("Settings copied. Check or select photos in the list, then Paste.", 6000)

    def paste_settings(self):
        if self.clip_edit is None:
            return
        targets = [it for it in (self.checked_items() or self.selected_items()) if it.edit is not None]
        if not targets:
            self.statusBar().showMessage("Tick or select the photos to paste to first.", 5000)
            return
        dlg = PasteDialog(self, len(targets), getattr(self, "clip_name", "the copied photo"),
                          set(self._settings.get("paste_groups") or PASTE_DEFAULT))
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        groups = dlg.chosen()
        self._settings["paste_groups"] = sorted(groups)
        names = ", ".join(PASTE_SHORT[key] for key, _label, _f in PASTE_GROUPS if key in groups)
        for it in targets:
            self._record_undo(it, it.edit.copy())
            paste_fields(self.clip_edit, it.edit, groups)
            it.edit.notes = [n for n in it.edit.notes if not n.startswith("Pasted")] + [f"Pasted: {names}"]
            self._mark_changed(it)
            self._queue_thumb(it)
        self.load_controls()
        self.refresh_view()
        self.statusBar().showMessage(f"Pasted {names} to {len(targets)} photo(s) (not yet approved).", 6000)

    # ---------------- checking photos / scopes ----------------
    def checked_items(self):
        return [it for it in self.items.values()
                if it.list_item.checkState() == Qt.CheckState.Checked]

    def selected_items(self):
        return [self.items[li.data(Qt.ItemDataRole.UserRole)] for li in self.list.selectedItems()]

    def scope_items(self, scope):
        if scope.startswith("label:"):
            want = {("" if x == "None" else x) for x in scope[6:].split(",") if x}
            return [it for it in self.items.values() if (it.label or "") in want]
        if scope == "current":
            return [self.current] if self.current else []
        if scope == "selected":
            return self.selected_items()
        if scope == "checked":
            return self.checked_items()
        return list(self.items.values())

    def _update_checked_label(self):
        n = sum(1 for it in self.items.values() if it.list_item.checkState() == Qt.CheckState.Checked)
        self.checked_lbl.setText(f"{n} checked")
        if hasattr(self, "act_approve_checked"):
            self.act_approve_checked.setText(f"Approve checked ({n})" if n else "Approve checked")
            self.act_approve_checked.setEnabled(bool(n))
            self.act_auto_chk.setText(f"Apply auto settings to checked ({n})…" if n
                                      else "Apply auto settings to checked…")
            self.act_auto_chk.setEnabled(bool(n))
            self.act_uncheck_all.setEnabled(bool(n))

    def check_where(self, pred):
        self.list.blockSignals(True)
        for it in self.items.values():
            it.list_item.setCheckState(Qt.CheckState.Checked if pred(it) else Qt.CheckState.Unchecked)
        self.list.blockSignals(False)
        self._update_checked_label()
        for it in self.items.values():
            self._update_grid_item(it)
        self._update_scope_label()

    def check_selected(self):
        sel = {id(it) for it in self.selected_items()}
        self.check_where(lambda it: id(it) in sel)

    def check_invert(self):
        self.check_where(lambda it: it.list_item.checkState() != Qt.CheckState.Checked)

    def toggle_check_current(self):
        it = self.current
        if it is None:
            return
        on = it.list_item.checkState() == Qt.CheckState.Checked
        it.list_item.setCheckState(Qt.CheckState.Unchecked if on else Qt.CheckState.Checked)

    # ---------------- auto settings for selected photos ----------------
    def _photo_menu(self, view, pos):
        clicked = view.itemAt(pos)
        if clicked is not None and not clicked.isSelected():
            view.clearSelection()                       # right-clicking an unselected photo: act on it
            clicked.setSelected(True)
        n = len([it for it in self.selected_items() if it.edit is not None])
        if not n:
            return
        s = "s" if n != 1 else ""
        menu = QtWidgets.QMenu(view)
        menu.addAction(f"Apply auto settings to {n} selected photo{s}…", lambda: self.open_apply_auto("selected"))
        menu.addAction(f"Auto-enhance {n} photo{s} (tools for new photos)", self.auto_enhance_selected)
        menu.addAction(f"Crop {n} photo{s} to centre the subject",
                       lambda: self._run_on_selected(auto.BatchSpec(scope="selected", subject_crop=True)))
        menu.addAction(f"Remove subject crop from {n} photo{s}", self.remove_crop_selected)
        menu.addSeparator()
        sel = self.selected_items()
        rm = menu.addMenu(f"Reset {n} photo{s}")
        rm.setToolTipsVisible(True)
        self._reset_menu(rm, lambda: list(sel), lambda k: f"{k} selected photo" + ("" if k == 1 else "s"))
        lm = menu.addMenu(f"Colour label for {n} photo{s}")
        for i, lab in enumerate(core.LABELS):
            a = lm.addAction(self._label_icon(lab), lab + (f"    ({'6789'[i]})" if i < 4 else ""),
                             lambda lab=lab: self.set_label_on(sel, lab))
        lm.addSeparator()
        lm.addAction("No label", lambda: self.set_label_on(sel, ""))
        menu.addAction(f"Mark {n} photo{s} as edited ✎", lambda: self.set_edited(sel, True))
        menu.addAction(f"Mark {n} photo{s} as not edited", lambda: self.set_edited(sel, False))
        menu.addSeparator()
        menu.addAction(f"Paste settings to {n} photo{s}", self.paste_settings).setEnabled(self.clip_edit is not None)
        menu.addAction("Batch edit (raise / lower / set sliders)…", self.open_batch)
        menu.exec(view.viewport().mapToGlobal(pos))

    def _selected_ready(self):
        return [it for it in self.selected_items() if it is not None and it.edit is not None]

    def _run_on_selected(self, spec):
        items = self._selected_ready()
        if not items:
            self.statusBar().showMessage("Select one or more analysed photos first "
                                         "(Ctrl-click or Shift-click in the list or grid).", 6000)
            return
        self._run_batch_on(items, spec)

    def open_apply_auto(self, which="selected"):
        if which == "checked":
            items = [it for it in self.checked_items() if it.edit is not None]
            if not items:
                self.statusBar().showMessage("Tick ☑ one or more analysed photos first.", 6000)
                return
        else:
            items = self._selected_ready()
            if not items:
                self.statusBar().showMessage("Select one or more analysed photos first "
                                             "(Ctrl-click or Shift-click in the list or grid).", 6000)
                return
        dlg = ApplyAutoDialog(self, [os.path.basename(it.path) for it in items], self._opts_snapshot(),
                              self._settings.get("apply_auto_last"))
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        v = dlg.values()
        self._settings["apply_auto_last"] = v
        spec = dlg.spec()
        spec.fresh = v["fresh"]
        self._run_batch_on(items, spec)

    def auto_enhance_selected(self):
        o = self.opts
        groups = [g for g in auto.GROUP_ORDER if g in o.groups and g != "level"]
        self._run_on_selected(auto.BatchSpec(
            scope="selected", groups=groups, level_mode=o.level_mode, allow_upright=o.allow_upright,
            strength=o.strength, exposure_bias=o.exposure_bias, punch=o.punch, colour=o.colour,
            rules=dict(o.rules), user_rules=True, subject_crop=o.subject_crop))

    def remove_crop_selected(self):
        items = [it for it in self._selected_ready() if it.edit.crop_box]
        for it in items:
            self._record_undo(it, it.edit.copy())
            it.edit.crop_box = None
            auto.replace_note(it.edit, "Subject crop", "Subject crop: off")
            self._mark_changed(it)
            self._queue_thumb(it)
        self.load_controls()
        self.refresh_view()
        self.statusBar().showMessage(f"Removed the subject crop from {len(items)} photo(s).", 5000)

    # ---------------- batch edit ----------------
    def open_batch(self):
        if not self.items:
            self.statusBar().showMessage("Add some photos first.", 4000)
            return
        labels = {}
        for it in self.items.values():
            labels[it.label or ""] = labels.get(it.label or "", 0) + 1
        counts = {"current": 1 if self.current else 0, "selected": len(self.selected_items()),
                  "checked": len(self.checked_items()), "all": len(self.items), "labels": labels}
        dlg = BatchDialog(self, counts, self._opts_snapshot())
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self.run_batch(dlg.spec())

    def run_batch(self, spec):
        items = [it for it in self.scope_items(spec.scope) if it is not None and it.edit is not None]
        if not items:
            self.statusBar().showMessage("No analysed photos in that selection.", 5000)
            return
        self._run_batch_on(items, spec)

    def _run_batch_on(self, items, spec, mark_edited=True):
        self._batch_mark = mark_edited
        self._undo = {it.path: (it.edit.copy(), it.status, it.wb, it.edited) for it in items}
        self.act_undo_batch.setEnabled(True)
        if not spec.needs_decode:                      # plain setting changes are instant
            for it in items:
                auto.apply_changes(it.edit, spec.changes)
                it.edit.notes = [n for n in it.edit.notes] + ["Batch edit applied"]
                self._after_batch_item(it)
            self._batch_finished(len(items), 0)
            return
        self._batch_total, self._batch_done, self._batch_errors = len(items), 0, 0
        self._batch_progress = QtWidgets.QProgressDialog("Applying to photos…", "", 0, len(items), self)
        self._batch_progress.setWindowTitle("Batch edit")
        self._batch_progress.setCancelButton(None)
        self._batch_progress.setWindowModality(Qt.WindowModality.WindowModal)
        self._batch_progress.setMinimumDuration(0)
        self._batch_progress.setValue(0)
        for it in items:
            self.pool.start(BatchTask(self.gen, it.path, it.edit, self.cache.get(it.path),
                                      spec, self.batch_signals))

    def on_batch_done(self, gen, path, res, err):
        if gen != self.gen or path not in self.items:
            return
        it = self.items[path]
        if res is None:
            self._batch_errors += 1
            self.statusBar().showMessage(f"{os.path.basename(path)}: {err}", 8000)
        else:
            if it.edit is not None:
                self._record_undo(it, it.edit.copy())
            it.edit = res["edit"]
            if res.get("wb") is not None:
                it.wb = res["wb"]
            if res.get("auto_values"):
                self._remember_auto(it, res["auto_values"])
            self._set_thumb(it, res["thumb"])
            self._after_batch_item(it)
        self._batch_done += 1
        if self._batch_progress:
            self._batch_progress.setValue(self._batch_done)
        if self._batch_done >= self._batch_total:
            if self._batch_progress:
                self._batch_progress.close()
                self._batch_progress = None
            self._batch_finished(self._batch_total - self._batch_errors, self._batch_errors)

    def _after_batch_item(self, it):
        if it.status in ("rejected",):
            it.status = "ready"
        was = it.edited
        self._mark_changed(it)
        if not getattr(self, "_batch_mark", True):
            it.edited = was                             # re-applying auto to everything isn't "your" edit
        if self.chk_autowrite.isChecked():
            self._write(it, quiet=True)
        else:
            self._update_list_item(it)

    def _batch_finished(self, n, errors):
        self._update_count()
        self.load_controls()
        self.refresh_view()
        msg = f"Batch edit applied to {n} photo(s). Review and approve to write XMP." \
            if not self.chk_autowrite.isChecked() else f"Batch edit applied and XMP written for {n} photo(s)."
        if errors:
            msg += f" {errors} failed."
        self.statusBar().showMessage(msg, 10000)

    def undo_batch(self):
        if not self._undo:
            return
        for path, (edit, status, wb, *rest) in self._undo.items():
            it = self.items.get(path)
            if it is None:
                continue
            if rest:
                it.edited = rest[0]
            if it.edit is not None:
                self._record_undo(it, it.edit.copy())
            it.edit, it.wb = edit, wb
            it.status = "modified" if status == "approved" else status
            self._update_list_item(it)
            self._queue_thumb(it)
        n = len(self._undo)
        self._undo = {}
        self.act_undo_batch.setEnabled(False)
        self._update_count()
        self.load_controls()
        self.refresh_view()
        self.statusBar().showMessage(f"Undid the last batch edit ({n} photo(s)). "
                                     "Any XMP already written is left as it was.", 8000)

    # ---------------- compare ----------------
    def eventFilter(self, obj, ev):
        t = ev.type()
        if t in (QtCore.QEvent.Type.KeyPress, QtCore.QEvent.Type.KeyRelease) and ev.key() == Qt.Key.Key_Backslash:
            fw = QtWidgets.QApplication.focusWidget()
            if (self.isActiveWindow() and not isinstance(fw, (QtWidgets.QLineEdit, QtWidgets.QAbstractSpinBox))):
                if not ev.isAutoRepeat():
                    self._hold_before = t == QtCore.QEvent.Type.KeyPress
                    self.refresh_view()
                return True
        return super().eventFilter(obj, ev)

    def _split_toggled(self, on):
        self.refresh_view()

    def _split_image(self):
        before, after = self._split_cache
        x = int(round(self.view.split_pos * after.shape[1]))
        img = core.clip_overlay(after) if self.chk_clip.isChecked() else after.copy()
        img[:, :x] = before[:, :x]
        return img

    def _compose_split(self):
        if self._split_cache is not None and self.view.split_on:
            self.view.set_image(to_qimage(self._split_image()), [], "")

    # ---------------- find / grid size ----------------
    def _show_ok(self, it) -> bool:
        mode = self.show_combo.currentIndex() if hasattr(self, "show_combo") else 0
        if it is None or mode == 0:
            return True
        return [True, not it.edited, it.edited, it.status in ("ready", "modified", "loading"),
                it.status == "approved", it.status == "rejected"][mode]

    def _refilter(self):
        if hasattr(self, "search"):
            self.apply_filter(self.search.text(), quiet=True)

    def select_where(self, pred):
        """Select (in the list and the grid) every shown photo that matches."""
        self.list.blockSignals(True)
        first = None
        n = 0
        for r in range(self.list.count()):
            li = self.list.item(r)
            it = self.items.get(li.data(Qt.ItemDataRole.UserRole))
            on = it is not None and not li.isHidden() and bool(pred(it))
            li.setSelected(on)
            if on:
                n += 1
                if first is None:
                    first = r
        self.list.blockSignals(False)
        self._sync_grid_from_list()
        cur = self.list.currentItem()
        if first is not None and (cur is None or not cur.isSelected()):
            self.list.setCurrentRow(first, QtCore.QItemSelectionModel.SelectionFlag.NoUpdate)
        self.statusBar().showMessage(f"{n} photo(s) selected.", 4000)

    def apply_filter(self, text, quiet=False):
        q = text.strip().lower()
        shown = 0
        for r in range(self.list.count()):
            li = self.list.item(r)
            hide = bool(q) and q not in os.path.basename(li.data(Qt.ItemDataRole.UserRole)).lower()
            hide = hide or not self._show_ok(self.items.get(li.data(Qt.ItemDataRole.UserRole)))
            li.setHidden(hide)
            gi = self.grid.item(r)
            if gi is not None:
                gi.setHidden(hide)
            shown += not hide
        if q and not quiet:
            self.statusBar().showMessage(f"{shown} photo(s) match '{text.strip()}'", 3000)
        if q or self.show_combo.currentIndex():
            cur = self.list.currentItem()
            if cur is None or cur.isHidden():
                for r in range(self.list.count()):
                    if not self.list.item(r).isHidden():
                        self.list.setCurrentRow(r)
                        break

    def set_thumb_size(self, w):
        h = int(w * GRID_H / GRID_W)
        self.grid.setIconSize(QtCore.QSize(w, h))
        self.grid.setGridSize(QtCore.QSize(w + 24, h + 52))

    # ---------------- my auto settings ----------------
    def open_my_auto(self):
        dlg = MyAutoDialog(self, self.opts)
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        v = dlg.values()
        self.opts.exposure_bias, self.opts.punch, self.opts.colour = v["exposure_bias"], v["punch"], v["colour"]
        self.opts.strength, self.opts.rules = v["strength"], v["rules"]
        self.save_state()
        n = len(auto.active_rules(v["rules"]))
        self.statusBar().showMessage(f"My auto settings saved ({n} rule(s)). New photos and Auto-enhance use them.", 6000)
        if dlg.chk_reapply.isChecked():
            self.reapply_my_auto()

    def reapply_my_auto(self):
        items = [it for it in self.items.values() if it.status == "ready" and it.edit is not None]
        if not items:
            self.statusBar().showMessage("No photos waiting for review to re-apply to.", 5000)
            return
        o = self.opts
        groups = [g for g in auto.GROUP_ORDER if g in o.groups]
        spec = auto.BatchSpec(scope="all", groups=groups, level_mode=o.level_mode, allow_upright=o.allow_upright,
                              strength=o.strength, exposure_bias=o.exposure_bias, punch=o.punch, colour=o.colour,
                              rules=dict(o.rules), user_rules=True, subject_crop=o.subject_crop)
        self._run_batch_on(items, spec, mark_edited=False)

    # ---------------- view menu / help ----------------
    def _build_view_menu(self):
        btn = QtWidgets.QToolButton()
        btn.setText("View ▾")
        btn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QtWidgets.QMenu(btn)
        menu.addAction("Fold all panels", lambda: self._set_all_panels(False))
        menu.addAction("Unfold all panels", lambda: self._set_all_panels(True))
        menu.addSeparator()
        sm = menu.addMenu("Interface size (after restart)")
        g = QtGui.QActionGroup(sm)
        cur = float(self._settings.get("ui_scale", 1.0))
        for pct in (85, 100, 115, 130, 150):
            a = sm.addAction(f"{pct}%")
            a.setCheckable(True)
            g.addAction(a)
            a.setChecked(abs(cur * 100 - pct) < 1)
            a.triggered.connect(lambda _c=False, p=pct: self._set_ui_scale(p / 100.0))
        menu.addSeparator()
        menu.addAction("Keyboard shortcuts && tips (?)", self.show_help)
        menu.addAction("Show the welcome tour again", self.show_tour)
        btn.setMenu(menu)
        return btn

    def _set_all_panels(self, on):
        for content, set_open in self._panels.values():
            set_open(on)

    def _set_ui_scale(self, v):
        self._settings["ui_scale"] = v
        self.save_state()
        QtWidgets.QMessageBox.information(self, "Interface size",
                                          f"Interface size {int(v * 100)}% applies the next time RAW Review starts.")

    def show_help(self):
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Keyboard shortcuts & tips")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(HELP_HTML)
        box.exec()

    def show_tour(self):
        TourDialog(self).exec()
        self._settings["tour_done"] = True
        self.save_state()

    # ---------------- undo an approval ----------------
    def undo_approval(self):
        it = self.current
        if it is None or not it.approve_undo:
            return
        snap, written, status_before = it.approve_undo
        try:
            msg = core.restore_output(it.path, snap, written)
        except core.RawSafetyError as ex:
            QtWidgets.QMessageBox.warning(self, "Undo approval", str(ex))
            return
        except OSError as ex:
            QtWidgets.QMessageBox.warning(self, "Undo approval", f"Couldn't undo: {ex}")
            return
        it.approve_undo = None
        it.status = status_before if status_before in ("ready", "modified", "rejected") else "ready"
        self._update_list_item(it)
        self._update_count()
        self.load_controls()
        self.statusBar().showMessage(msg + ". In Lightroom, read the metadata again if the photo is in your catalog.",
                                     8000)

    # ---------------- ratings & colour labels ----------------
    def _meta_text(self, it):
        parts = []
        if it.rating:
            parts.append("★" * it.rating + "☆" * (5 - it.rating))
        if it.label:
            parts.append(f"● {it.label}")
        return "  ".join(parts)

    def _rating_targets(self):
        sel = [t for t in self.selected_items() if t is not None]
        if len(sel) > 1:
            return sel
        return [self.current] if self.current else []

    def set_rating(self, n):
        targets = self._rating_targets()
        for t in targets:
            t.rating = n
            self._mark_changed(t)
            self._update_list_item(t)
        if targets:
            self.statusBar().showMessage(
                (f"{'★' * n} rating" if n else "Rating cleared") + f" on {len(targets)} photo(s) "
                "– written to Lightroom when approved.", 3000)
        self._update_rating_label()

    def set_label(self, lab):
        targets = self._rating_targets()
        same = all(t.label == lab for t in targets)
        for t in targets:
            t.label = "" if same else lab           # pressing the same key again clears it, like Lightroom
            self._mark_changed(t)
            self._update_list_item(t)
        if targets:
            self.statusBar().showMessage(("Label cleared" if same else f"{lab} label") +
                                         f" on {len(targets)} photo(s).", 3000)
        self._update_rating_label()

    def set_label_on(self, items, lab):
        """Give these photos colour label `lab` ("" clears it)."""
        items = [t for t in items if t is not None]
        for t in items:
            t.label = lab
            self._mark_changed(t)
            self._update_list_item(t)
            self._update_grid_item(t)
        if items:
            self.statusBar().showMessage(("Label cleared" if not lab else f"{lab} label") +
                                         f" on {len(items)} photo(s) – written to Lightroom when approved.", 4000)
        self._update_rating_label()

    def _label_icon(self, lab):
        pm = QtGui.QPixmap(12, 12)
        pm.fill(Qt.GlobalColor.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setBrush(QtGui.QColor(LABEL_COLOURS.get(lab, "#00000000")))
        p.setPen(QtGui.QPen(QtGui.QColor("#aaa"), 1))
        p.drawEllipse(1, 1, 10, 10)
        p.end()
        return QtGui.QIcon(pm)

    def _update_rating_label(self):
        it = self.current
        if it is None:
            self.rating_lbl.setText("")
            return
        stars = "★" * (it.rating or 0) + "☆" * (5 - (it.rating or 0))
        lab = it.label or ""
        col = LABEL_COLOURS.get(lab, "#888")
        self.rating_lbl.setText(f"<span style='font-size:15px'>{stars}</span>"
                                + (f"&nbsp;&nbsp;<span style='color:{col}'>● {lab}</span>" if lab else "")
                                + "&nbsp;&nbsp;<span style='color:#777'>(keys 0-5, 6-9)</span>")

    def reset_lr(self):
        it = self.current
        if it is None or it.edit is None or it.lr_edit is None:
            return
        self._record_undo(it, it.edit.copy())
        it.edit = it.lr_edit.copy()
        self._mark_changed(it)
        self._queue_thumb(it)
        self.load_controls()
        self.refresh_view()
        self.statusBar().showMessage("Back to the settings this photo had in Lightroom.", 4000)

    # ---------------- remembering settings & the session ----------------
    @staticmethod
    def _load_json(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _json_default(o):
        if isinstance(o, np.generic):
            return o.item()
        if isinstance(o, (set, frozenset)):
            return sorted(o)
        raise TypeError(type(o).__name__)

    def _save_json(self, path, data):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1, default=self._json_default)
            os.replace(tmp, path)
        except OSError:
            pass                                   # remembering is a convenience; never block the app

    def _apply_saved_options(self, st):
        o = st.get("auto") or {}
        if "groups" in o:
            self.opts.groups = set(g for g in o["groups"] if g in auto.GROUP_ORDER)
            known = set(o.get("known_groups", auto.GROUP_ORDER))
            self.opts.groups |= {g for g in auto.GROUP_ORDER if g not in known}   # tools added in an update: on
        for k in ("level_mode", "allow_upright", "strength", "exposure_bias", "use_existing", "punch", "colour",
                  "rules", "subject_crop"):
            if k in o:
                setattr(self.opts, k, o[k])
        if self.opts.level_mode not in core.LEVEL_MODES:
            self.opts.level_mode = "Auto (best guess)"
        dng_profile.apply_saved_options(st.get("dng_profile"), st.get("version", ""))
        auto.CAMERA_LOOK = bool(o.get("camera_look", auto.CAMERA_LOOK))
        core.NO_AI_MASKS = bool(st.get("no_ai_masks", False))
        self.last_dir = st.get("last_dir", "") if os.path.isdir(st.get("last_dir", "") or "") else ""

    def _apply_saved_window(self, st):
        try:
            if st.get("geometry"):
                self.restoreGeometry(QtCore.QByteArray(base64.b64decode(st["geometry"])))
            if st.get("splitter"):
                self.split.setSizes([int(x) for x in st["splitter"]])
        except Exception:  # noqa: BLE001
            pass
        self.chk_autowrite.setChecked(bool(st.get("auto_write", False)))
        self.chk_relative.setChecked(bool(st.get("relative", False)))
        self.scope_combo.setCurrentIndex(0)       # always start on "This photo" (never silently multi-photo)
        self.chk_samples.setChecked(bool(st.get("show_wb_samples", False)))
        self.chk_masks.setChecked(bool(st.get("show_masks", False)))
        self.chk_clip.setChecked(bool(st.get("show_clipping", False)))
        try:
            self.thumb_size.setValue(int(st.get("thumb_size", 240)))
        except (TypeError, ValueError):
            pass
        self.chk_split.setChecked(bool(st.get("split", False)))
        try:
            self.show_combo.setCurrentIndex(int(st.get("show_filter", 0)))
        except (TypeError, ValueError):
            pass

    def _settings_dict(self):
        o = self.opts
        return {
            "version": __version__,
            "auto": {"groups": sorted(o.groups), "known_groups": list(auto.GROUP_ORDER), "level_mode": o.level_mode, "allow_upright": o.allow_upright,
                     "strength": o.strength, "exposure_bias": o.exposure_bias, "use_existing": o.use_existing,
                     "punch": o.punch, "colour": o.colour, "rules": o.rules, "subject_crop": o.subject_crop,
                     "camera_look": auto.CAMERA_LOOK},
            "no_ai_masks": core.NO_AI_MASKS,
            "dng_profile": {k: getattr(dng_profile.OPTIONS, k) for k in dng_profile.OPTIONS.__dataclass_fields__},
            "geometry": base64.b64encode(bytes(self.saveGeometry())).decode("ascii"),
            "splitter": self.split.sizes(),
            "last_dir": self.last_dir,
            "auto_write": self.chk_autowrite.isChecked(),
            "relative": self.chk_relative.isChecked(),
            "show_wb_samples": self.chk_samples.isChecked(),
            "show_masks": self.chk_masks.isChecked(),
            "show_clipping": self.chk_clip.isChecked(),
            "apply_auto_last": self._settings.get("apply_auto_last"),
            "paste_groups": self._settings.get("paste_groups"),
            "mark_edited": self._settings.get("mark_edited", True),
            "show_filter": self.show_combo.currentIndex(),
            "collapsed": [t for t, (c, _f) in self._panels.items() if not c.isVisible()],
            "ui_scale": self._settings.get("ui_scale", 1.0),
            "tour_done": self._settings.get("tour_done", False),
            "thumb_size": self.thumb_size.value(),
            "split": self.chk_split.isChecked(),
        }

    def _session_dict(self):
        photos = []
        for r in range(self.list.count()):
            path = self.list.item(r).data(Qt.ItemDataRole.UserRole)
            it = self.items.get(path)
            if it is None:
                continue
            saved = self._restore_map.get(path)            # not analysed yet: keep what we restored
            photos.append({
                "path": path,
                "status": it.status if it.edit is not None else (saved or {}).get("status", "ready"),
                "checked": it.list_item.checkState() == Qt.CheckState.Checked,
                "rating": it.rating, "label": it.label,
                "edited": it.edited if it.edit is not None else bool((saved or {}).get("edited")),
                "edit": core.edit_to_dict(it.edit) if it.edit is not None else (saved or {}).get("edit"),
            })
        return {"version": __version__, "current": self.list.currentRow(), "photos": photos}

    def save_state(self):
        self._save_json(SETTINGS_FILE, self._settings_dict())
        sess = self._session_dict()
        text = json.dumps(sess, sort_keys=True, default=self._json_default)
        if text != self._last_session_text:
            self._save_json(SESSION_FILE, sess)
            self._last_session_text = text

    def restore_session(self):
        if len(sys.argv) > 1:
            return                                         # opened with photos on the command line
        sess = self._load_json(SESSION_FILE)
        photos = [p for p in sess.get("photos", []) if isinstance(p, dict) and os.path.isfile(p.get("path", ""))]
        missing = len(sess.get("photos", [])) - len(photos)
        if not photos:
            return
        self._restore_map = {p["path"]: p for p in photos}
        self.add_paths([p["path"] for p in photos])
        cur = sess.get("current", 0)
        if isinstance(cur, int) and 0 <= cur < self.list.count():
            self.list.setCurrentRow(cur)
        self._update_checked_label()
        self.statusBar().showMessage(
            f"Restored your last session: {len(photos)} photo(s) with their edits and decisions"
            + (f" ({missing} no longer found)" if missing else "") + ". Clear List starts fresh.", 10000)

    # ---------------- per-photo undo / redo ----------------
    UNDO_LIMIT = 100
    UNDO_MERGE_SECONDS = 1.0

    def _record_undo(self, it, before, key=None):
        """Save `before` (the photo's settings before a change) as an undo step.
        Repeated changes to the same slider within a second (one drag) become one step."""
        now = time.monotonic()
        if key is not None and key == it.undo_key and now - it.undo_time < self.UNDO_MERGE_SECONDS and it.undo:
            it.undo_time = now
            it.redo.clear()
            return
        it.undo.append(before.copy())
        del it.undo[:-self.UNDO_LIMIT]
        it.redo.clear()
        it.undo_key, it.undo_time = key, now

    def _update_undo_buttons(self):
        it = self.current
        has = it is not None and it.edit is not None
        self.btn_undo.setEnabled(bool(has and it.undo))
        self.btn_redo.setEnabled(bool(has and it.redo))
        if has:
            self.btn_undo.setText(f"↶ Undo{'  ·  ' + str(len(it.undo)) if it.undo else ''}")

    def _restore(self, it, edit, verb):
        it.edit = edit
        it.undo_key = None                 # the next slider move starts a new step
        self._mark_changed(it)
        self._queue_thumb(it)
        self.load_controls()
        self.refresh_view()
        self.statusBar().showMessage(f"{verb} on {os.path.basename(it.path)}"
                                     f"  ({len(it.undo)} undo / {len(it.redo)} redo left)", 3000)

    def undo_edit(self):
        it = self.current
        if it is None or it.edit is None or not it.undo:
            return
        it.redo.append(it.edit.copy())
        self._restore(it, it.undo.pop(), "Undo")

    def redo_edit(self):
        it = self.current
        if it is None or it.edit is None or not it.redo:
            return
        it.undo.append(it.edit.copy())
        self._restore(it, it.redo.pop(), "Redo")

    # ---------------- decisions ----------------
    def _write(self, it, quiet=False) -> bool:
        status_before = it.status
        try:
            snap = core.snapshot_output(it.path, os.path.join(CONFIG_DIR, "undo"))
        except OSError:
            snap = None
        try:
            msg = core.write_xmp(it.path, it.edit, it.src_size,
                                 meta={"rating": it.rating, "label": it.label}, flip=it.flip)
            if snap is not None:
                old = it.approve_undo
                if old and old[0].get("copy") and os.path.isfile(old[0]["copy"]):
                    os.remove(old[0]["copy"])           # only our own temporary copy
                it.approve_undo = (snap, core.file_hash(snap["target"]),
                                   status_before if status_before != "approved" else "modified")
        except core.RawSafetyError as ex:
            QtWidgets.QMessageBox.critical(self, "Stopped for safety", str(ex))
            return False
        except Exception as ex:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "Could not write the edit", f"{it.path}\n\n{ex}")
            return False
        it.status = "approved"
        self._update_list_item(it)
        self._update_count()
        if it.path.lower().endswith(".dng"):
            msg += " – in Lightroom use Metadata → Read Metadata from Files (or import it)"
        self.statusBar().showMessage(msg, 8000)
        return True

    def _grid_selection(self):
        """In grid view with several photos selected, Approve/Reject act on all of them."""
        if not self.in_grid():
            return []
        sel = [it for it in self.selected_items() if it.edit is not None and it.status != "loading"]
        return sel if len(sel) > 1 else []

    def approve(self):
        many = self._grid_selection()
        if many:
            self._approve_many(many)
            return
        it = self.current
        if it is None or it.edit is None:
            return
        if self._write(it):
            self.next_to_review()

    def reject(self):
        many = self._grid_selection()
        if many:
            for t in many:
                t.status = "rejected"
                self._update_list_item(t)
            self._update_count()
            self.statusBar().showMessage(f"Rejected {len(many)} photo(s). Nothing was written or deleted.", 6000)
            return
        it = self.current
        if it is None or it.edit is None:
            return
        was_written = it.status in ("approved", "modified")
        it.status = "rejected"
        self._update_list_item(it)
        self._update_count()
        if was_written:
            self.statusBar().showMessage(
                "Rejected. The sidecar written earlier was left in place (this app never deletes files).", 8000)
        self.next_to_review()

    def approve_checked(self):
        todo = [it for it in self.checked_items() if it.edit is not None and it.status != "loading"]
        if not todo:
            self.statusBar().showMessage("No analysed photos are checked.", 5000)
            return
        self._approve_many(todo)

    def approve_all(self):
        todo = [it for it in self.items.values() if it.status in ("ready", "modified") and it.edit]
        if not todo:
            self.statusBar().showMessage("Nothing waiting for review.", 5000)
            return
        self._approve_many(todo)

    def _approve_many(self, todo):
        low = sum(1 for it in todo if it.wb and it.wb.applied and it.wb.confidence < 0.5)
        msg = f"Write XMP sidecars for {len(todo)} photo(s) with their current settings?"
        if low:
            msg += f"\n\n{low} of them have low-confidence auto WB (marked 'check WB')."
        if QtWidgets.QMessageBox.question(self, "Approve", msg) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        done = 0
        for it in todo:
            if not self._write(it):
                break
            done += 1
            QtWidgets.QApplication.processEvents()
        self.statusBar().showMessage(f"Wrote {done} sidecar(s).", 8000)

    # ---------------- updates ----------------
    def check_updates(self, manual=True):
        if self._upd_busy:
            return
        self._upd_busy = True
        if manual:
            self.statusBar().showMessage("Checking GitHub for updates…")

        def job():
            try:
                self.upd.checked.emit(updater.check_latest(), "", manual)
            except Exception as ex:  # noqa: BLE001
                self.upd.checked.emit(None, str(ex), manual)
        self.pool.start(FnTask(job))

    def on_update_checked(self, info, err, manual):
        self._upd_busy = False
        if err:
            if manual:
                QtWidgets.QMessageBox.warning(self, "Updates", f"Couldn't reach GitHub:\n{err}")
            return
        if not info["newer"]:
            self.act_update.setText("Check for Updates")
            if manual:
                QtWidgets.QMessageBox.information(self, "Updates", f"You're up to date (v{__version__}).")
            return
        self.act_update.setText(f"⬆ Update to v{info['version']}")
        if not manual:
            self.statusBar().showMessage(
                f"RAW Review v{info['version']} is available – click the Update button in the toolbar.", 15000)
            return
        self.offer_update(info)

    def offer_update(self, info):
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Update available")
        box.setIcon(QtWidgets.QMessageBox.Icon.Information)
        box.setText(f"<b>RAW Review v{info['version']}</b> is available (you have v{__version__}).")
        box.setInformativeText(
            "Install it now? The app will restart.\n\n"
            "Your photos and the sidecars you've already approved aren't affected. "
            "Photos you haven't reviewed yet will need to be dragged in again.\n\n"
            "Click 'Show Details' to see what's new.")
        box.setDetailedText(info["notes"] or "No release notes.")
        install_btn = box.addButton("Install && Restart", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Not now", QtWidgets.QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not install_btn:
            return

        self._upd_progress = QtWidgets.QProgressDialog("Downloading update…", "", 0, 0, self)
        self._upd_progress.setWindowTitle("Updating RAW Review")
        self._upd_progress.setCancelButton(None)
        self._upd_progress.setWindowModality(Qt.WindowModality.WindowModal)
        self._upd_progress.setMinimumDuration(0)
        self._upd_progress.show()
        self._upd_version = info["version"]

        def job():
            try:
                backup = updater.install(info, progress=self.upd.progress.emit)
                self.upd.installed.emit(backup, "")
            except Exception as ex:  # noqa: BLE001
                traceback.print_exc()
                self.upd.installed.emit(None, str(ex))
        self.pool.start(FnTask(job))

    def on_update_progress(self, msg):
        if self._upd_progress:
            self._upd_progress.setLabelText(msg)

    def on_update_installed(self, backup, err):
        if self._upd_progress:
            self._upd_progress.close()
            self._upd_progress = None
        if err:
            QtWidgets.QMessageBox.critical(
                self, "Update failed",
                f"The update couldn't be installed:\n{err}\n\n"
                + ("Nothing was changed." if updater.FROZEN else
                   f"If any files were already replaced, the previous version is saved in "
                   f"{os.path.join(updater.APP_DIR, '.update_backup')}."))
            return
        QtWidgets.QMessageBox.information(
            self, "Updated", f"v{self._upd_version} is ready. RAW Review will now close and restart"
            + (" (it takes a few seconds to copy the new version in)." if updater.FROZEN else "."))
        updater.restart()
        self.close()

    def closeEvent(self, e):
        self.save_state()
        self.pool.clear()
        self.gen += 1
        self.pool.waitForDone(3000)
        super().closeEvent(e)


ACCENT = "#4a80d4"
STYLESHEET = f"""
QPushButton {{
    background: #3c3c41; border: 1px solid #4c4c53; border-radius: 5px;
    padding: 5px 10px; color: #e8e8ea;
}}
QPushButton:hover {{ background: #46464c; border-color: #5a5a62; }}
QPushButton:pressed {{ background: #2f2f33; }}
QPushButton:checked {{ background: {ACCENT}; border-color: {ACCENT}; color: white; }}
QPushButton:disabled {{ background: #333337; border-color: #3c3c41; color: #77777d; }}
QToolButton {{ border: 1px solid transparent; border-radius: 4px; padding: 3px 6px; }}
QToolButton:hover {{ background: #46464c; border-color: #55555c; }}
QToolButton:pressed, QToolButton:checked {{ background: #2f2f33; }}
QToolButton::menu-indicator {{ image: none; }}
QGroupBox {{
    border: 1px solid #3c3c42; border-radius: 8px; margin-top: 16px; padding: 8px 6px 6px 6px;
    background: #2f2f32;
}}
QGroupBox#fold {{ margin-top: 4px; padding-top: 4px; }}
QGroupBox::title {{
    subcontrol-origin: margin; subcontrol-position: top left; left: 8px; padding: 0 4px;
    color: #c8c8cc; font-weight: bold;
}}
QLineEdit, QComboBox, QAbstractSpinBox {{
    background: #232326; border: 1px solid #48484f; border-radius: 4px; padding: 3px 6px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QComboBox:focus, QAbstractSpinBox:focus {{ border-color: {ACCENT}; }}
QComboBox QAbstractItemView {{ background: #2a2a2d; border: 1px solid #48484f; selection-background-color: {ACCENT}; }}
QCheckBox {{ spacing: 6px; }}
QCheckBox::indicator {{
    width: 14px; height: 14px; border: 1px solid #8a8a92; border-radius: 3px; background: #26262a;
}}
QCheckBox::indicator:hover {{ border-color: #b0b0b8; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; image: none; }}
QRadioButton {{ spacing: 6px; }}
QRadioButton::indicator {{
    width: 14px; height: 14px; border: 1px solid #8a8a92; border-radius: 8px; background: #26262a;
}}
QRadioButton::indicator:hover {{ border-color: #b0b0b8; }}
QRadioButton::indicator:checked {{ background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
    stop:0 white, stop:0.35 white, stop:0.45 {ACCENT}, stop:1 {ACCENT}); border-color: {ACCENT}; }}
QRadioButton::indicator:disabled {{ border-color: #55555c; background: #2a2a2d; }}
QSlider::groove:horizontal {{ height: 4px; background: #4a4a52; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: #6f8fbf; border-radius: 2px; }}
QSlider::handle:horizontal {{
    width: 14px; height: 14px; margin: -5px 0; border-radius: 7px;
    background: #e8e8ea; border: 1px solid #9a9aa2;
}}
QSlider::handle:horizontal:hover {{ background: #ffffff; border-color: {ACCENT}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #4c4c53; border-radius: 4px; min-height: 30px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: #4c4c53; border-radius: 4px; min-width: 30px; margin: 2px; }}
QScrollBar::handle:hover {{ background: #5e5e66; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ background: none; border: none; width: 0; height: 0; }}
QMenu {{ background: #2c2c30; border: 1px solid #48484f; padding: 4px; }}
QMenu::item {{ padding: 5px 18px; border-radius: 4px; }}
QMenu::item:selected {{ background: {ACCENT}; color: white; }}
QMenu::separator {{ height: 1px; background: #48484f; margin: 4px 6px; }}
QToolTip {{ background: #3a3a3e; color: #ececee; border: 1px solid #55555c; padding: 4px 6px; }}
QSplitter::handle {{ background: #26262a; }}
QSplitter::handle:hover {{ background: {ACCENT}; }}
QStatusBar {{ color: #b8b8be; }}
QListWidget {{ border: none; }}
QListWidget::item:selected {{ background: #3b5f96; border-radius: 4px; }}
"""


def apply_theme(app):
    """Dark Fusion theme for the whole app."""
    app.setStyle("Fusion")
    pal = QtGui.QPalette()
    for role, c in [
        (QtGui.QPalette.ColorRole.Window, (45, 45, 48)), (QtGui.QPalette.ColorRole.WindowText, (230, 230, 230)),
        (QtGui.QPalette.ColorRole.Base, (32, 32, 34)), (QtGui.QPalette.ColorRole.AlternateBase, (45, 45, 48)),
        (QtGui.QPalette.ColorRole.Text, (230, 230, 230)), (QtGui.QPalette.ColorRole.Button, (58, 58, 62)),
        (QtGui.QPalette.ColorRole.ButtonText, (230, 230, 230)), (QtGui.QPalette.ColorRole.Highlight, (70, 110, 170)),
        (QtGui.QPalette.ColorRole.HighlightedText, (255, 255, 255)), (QtGui.QPalette.ColorRole.ToolTipBase, (60, 60, 60)),
        (QtGui.QPalette.ColorRole.ToolTipText, (230, 230, 230)),
        (QtGui.QPalette.ColorRole.PlaceholderText, (140, 140, 148)),
        (QtGui.QPalette.ColorRole.Link, (110, 160, 230)),
    ]:
        pal.setColor(role, QtGui.QColor(*c))
    for role in (QtGui.QPalette.ColorRole.WindowText, QtGui.QPalette.ColorRole.Text,
                 QtGui.QPalette.ColorRole.ButtonText):
        pal.setColor(QtGui.QPalette.ColorGroup.Disabled, role, QtGui.QColor(120, 120, 126))
    app.setPalette(pal)
    app.setStyleSheet(STYLESHEET)


def main():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            scale = float(json.load(f).get("ui_scale", 1.0))
        if 0.5 <= scale <= 2.5 and abs(scale - 1.0) > 0.01 and "QT_SCALE_FACTOR" not in os.environ:
            os.environ["QT_SCALE_FACTOR"] = str(scale)
    except (OSError, ValueError, AttributeError):
        pass
    app = QtWidgets.QApplication(sys.argv)
    apply_theme(app)
    w = MainWindow()
    w.show()
    if len(sys.argv) > 1:
        w.add_paths(sys.argv[1:])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
