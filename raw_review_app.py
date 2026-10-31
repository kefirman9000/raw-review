"""
RAW Review - review auto-edits for RAW photos and write Lightroom XMP sidecars.

Drag photos or folders onto the window. Each photo gets an automatic proposal
(smart white balance + straighten). Review each one:
    A  approve  -> writes/updates <photo>.xmp next to the RAW
    X  reject   -> nothing is written
    tweak any slider first if you want, then approve.

RAW files are NEVER modified, moved, renamed or deleted - they are only read.
"""

from __future__ import annotations

import os
import sys
import traceback
from collections import OrderedDict

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, QTimer, Signal

import raw_review_core as core
import updater
from version import __version__

CACHE_SIZE = 24          # decoded previews kept in memory
THUMB_W, THUMB_H = 120, 80

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
        self.status = "loading"
        self.edit: core.Edit | None = None
        self.auto_edit: core.Edit | None = None
        self.wb: core.WBResult | None = None
        self.color: core.CameraColor | None = None
        self.src_size = (3, 2)
        self.error = ""
        self.list_item: QtWidgets.QListWidgetItem | None = None


# --------------------------------------------------------------------------
# Background decoding
# --------------------------------------------------------------------------

class WorkerSignals(QObject):
    done = Signal(int, str, object, str)   # generation, path, result, error


class DecodeTask(QRunnable):
    def __init__(self, gen, path, analyze, opts, signals):
        super().__init__()
        self.gen, self.path, self.analyze, self.opts, self.signals = gen, path, analyze, opts, signals

    def run(self):
        try:
            dec = core.decode_raw(self.path)          # read-only
            res = {"dec": dec}
            if self.analyze:
                edit, wb, st = core.propose_edit(dec, **self.opts)
                res.update(edit=edit, wb=wb, st=st)
                img, _ = core.render(dec, edit)
                h, w = img.shape[:2]
                s = min(THUMB_W / w, THUMB_H / h)
                import cv2
                res["thumb"] = np.ascontiguousarray(
                    cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA))
            self.signals.done.emit(self.gen, self.path, res, "")
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

    def __init__(self):
        super().__init__()
        self.setMinimumSize(420, 300)
        self.img: QtGui.QImage | None = None
        self.overlay = []                # (x, y, QColor) in displayed-image coords
        self.message = "Drag RAW photos or folders here"
        self.badge = ""
        self.crosshair = False
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

    def mousePressEvent(self, e):
        if self.img is None or e.button() != Qt.MouseButton.LeftButton:
            return
        r, s = self._target()
        pos = e.position()
        if r.contains(pos):
            self.clicked.emit((pos.x() - r.x()) / s, (pos.y() - r.y()) / s)

    def mouseMoveEvent(self, e):
        self.setCursor(Qt.CursorShape.CrossCursor if self.crosshair else Qt.CursorShape.ArrowCursor)


class SliderRow(QtWidgets.QWidget):
    changed = Signal(float)

    def __init__(self, label, lo, hi, step, decimals=0, default=0.0):
        super().__init__()
        self.scale = 1.0 / step
        self.default = default
        lay = QtWidgets.QGridLayout(self)
        lay.setContentsMargins(0, 2, 0 , 2)
        self.lbl = QtWidgets.QLabel(label)
        self.lbl.setToolTip("Double-click to reset")
        self.slider = QtWidgets.QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(int(round(lo * self.scale)), int(round(hi * self.scale)))
        self.spin = QtWidgets.QDoubleSpinBox()
        self.spin.setRange(lo, hi)
        self.spin.setDecimals(decimals)
        self.spin.setSingleStep(step)
        self.spin.setFixedWidth(78)
        self.spin.setKeyboardTracking(False)
        lay.addWidget(self.lbl, 0, 0)
        lay.addWidget(self.spin, 0, 1, Qt.AlignmentFlag.AlignRight)
        lay.addWidget(self.slider, 1, 0, 1, 2)
        self.slider.valueChanged.connect(self._from_slider)
        self.spin.valueChanged.connect(self._from_spin)
        self.lbl.mouseDoubleClickEvent = lambda _e: self.setValue(self.default, emit=True)

    def _from_slider(self, v):
        val = v / self.scale
        self.spin.blockSignals(True)
        self.spin.setValue(val)
        self.spin.blockSignals(False)
        self.changed.emit(val)

    def _from_spin(self, val):
        self.slider.blockSignals(True)
        self.slider.setValue(int(round(val * self.scale)))
        self.slider.blockSignals(False)
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
        if emit:
            self.changed.emit(val)


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"RAW Review v{__version__} – auto edits → Lightroom XMP")
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
        self.upd = UpdateSignals()
        self.upd.checked.connect(self.on_update_checked)
        self.upd.progress.connect(self.on_update_progress)
        self.upd.installed.connect(self.on_update_installed)
        self._upd_busy = False
        self._upd_progress = None

        self.render_timer = QTimer(self)
        self.render_timer.setSingleShot(True)
        self.render_timer.setInterval(35)
        self.render_timer.timeout.connect(self.refresh_view)

        self._build_ui()
        self._build_shortcuts()
        self._set_controls_enabled(False)
        QTimer.singleShot(2500, lambda: self.check_updates(manual=False))   # quiet check

    # ---------------- UI ----------------
    def _build_ui(self):
        tb = self.addToolBar("Main")
        tb.setMovable(False)
        tb.addAction("Add Files…", self.open_files)
        tb.addAction("Add Folder…", self.open_folder)
        tb.addSeparator()
        self.chk_auto_wb = QtWidgets.QCheckBox("Auto WB")
        self.chk_auto_st = QtWidgets.QCheckBox("Auto straighten")
        self.chk_upright = QtWidgets.QCheckBox("Allow Upright suggestions")
        for c in (self.chk_auto_wb, self.chk_auto_st, self.chk_upright):
            c.setChecked(True)
            tb.addWidget(c)
        self.chk_auto_wb.setToolTip("Applied to photos added from now on")
        tb.addSeparator()
        tb.addAction("Approve All To-Review…", self.approve_all)
        tb.addAction("Clear List", self.clear_list)
        spacer = QtWidgets.QWidget()
        spacer.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        self.act_update = tb.addAction("Check for Updates", lambda: self.check_updates(manual=True))
        self.act_update.setToolTip(f"You have v{__version__}. Checks github.com/{updater.GITHUB_REPO}")

        split = QtWidgets.QSplitter()
        self.setCentralWidget(split)

        # Left: list
        left = QtWidgets.QWidget()
        ll = QtWidgets.QVBoxLayout(left)
        ll.setContentsMargins(4, 4, 4, 4)
        self.list = QtWidgets.QListWidget()
        self.list.setIconSize(QtCore.QSize(THUMB_W, THUMB_H))
        self.list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.currentRowChanged.connect(self.on_row)
        self.count_lbl = QtWidgets.QLabel("No photos")
        ll.addWidget(self.list)
        ll.addWidget(self.count_lbl)
        split.addWidget(left)

        # Center: image + nav
        center = QtWidgets.QWidget()
        cl = QtWidgets.QVBoxLayout(center)
        cl.setContentsMargins(4, 4, 4, 4)
        self.view = ImageView()
        self.view.clicked.connect(self.on_image_click)
        cl.addWidget(self.view, 1)
        self.notes_lbl = QtWidgets.QLabel("")
        self.notes_lbl.setWordWrap(True)
        self.notes_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        cl.addWidget(self.notes_lbl)
        nav = QtWidgets.QHBoxLayout()
        self.btn_prev = QtWidgets.QPushButton("◀ Prev")
        self.btn_reject = QtWidgets.QPushButton("✗ Reject  (X)")
        self.btn_approve = QtWidgets.QPushButton("✓ Approve && write XMP  (A)")
        self.btn_next = QtWidgets.QPushButton("Next ▶")
        self.chk_before = QtWidgets.QCheckBox("Before  (\\)")
        self.btn_reject.setStyleSheet("QPushButton{background:#6b2a2a;color:white;padding:8px 16px;}")
        self.btn_approve.setStyleSheet("QPushButton{background:#2a6b3a;color:white;padding:8px 16px;font-weight:bold;}")
        for w in (self.btn_prev, self.chk_before):
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
        self.s_temp = SliderRow("Temperature (K)", 2000, 50000, 50, 0, 5500)
        self.s_tint = SliderRow("Tint", -150, 150, 1, 0, 0)
        wl.addWidget(self.s_temp)
        wl.addWidget(self.s_tint)
        self.chk_samples = QtWidgets.QCheckBox("Show what Auto WB used")
        self.chk_samples.setToolTip("Cyan = near-white areas, magenta = near-black areas")
        self.chk_samples.toggled.connect(lambda _v: self.refresh_view())
        wl.addWidget(self.chk_samples)
        pl.addWidget(g_wb)

        g_tone = QtWidgets.QGroupBox("Tone")
        tl = QtWidgets.QVBoxLayout(g_tone)
        self.s_exp = SliderRow("Exposure (EV)", -5, 5, 0.05, 2, 0)
        self.s_con = SliderRow("Contrast", -100, 100, 1)
        self.s_hi = SliderRow("Highlights", -100, 100, 1)
        self.s_sh = SliderRow("Shadows", -100, 100, 1)
        self.s_vib = SliderRow("Vibrance", -100, 100, 1)
        for s in (self.s_exp, self.s_con, self.s_hi, self.s_sh, self.s_vib):
            tl.addWidget(s)
        pl.addWidget(g_tone)

        g_geo = QtWidgets.QGroupBox("Geometry")
        gl = QtWidgets.QVBoxLayout(g_geo)
        self.s_angle = SliderRow("Straighten (°, + = counter-clockwise)", -15, 15, 0.05, 2, 0)
        gl.addWidget(self.s_angle)
        row2 = QtWidgets.QHBoxLayout()
        row2.addWidget(QtWidgets.QLabel("Upright"))
        self.up_combo = QtWidgets.QComboBox()
        self.up_combo.addItems(list(core.UPRIGHT_MODES.keys()))
        self.up_combo.setToolTip("Lightroom computes Upright itself; it isn't shown in this preview")
        row2.addWidget(self.up_combo, 1)
        self.btn_auto_st = QtWidgets.QPushButton("Auto straighten")
        row2.addWidget(self.btn_auto_st)
        gl.addLayout(row2)
        self.chk_lens = QtWidgets.QCheckBox("Lens profile corrections + remove CA")
        gl.addWidget(self.chk_lens)
        pl.addWidget(g_geo)

        g_misc = QtWidgets.QGroupBox("This photo")
        ml = QtWidgets.QGridLayout(g_misc)
        self.btn_reset_auto = QtWidgets.QPushButton("Reset to auto proposal")
        self.btn_reset_shot = QtWidgets.QPushButton("Reset to As Shot")
        self.btn_copy = QtWidgets.QPushButton("Copy settings (Ctrl+C)")
        self.btn_paste = QtWidgets.QPushButton("Paste to selected (Ctrl+V)")
        ml.addWidget(self.btn_reset_auto, 0, 0)
        ml.addWidget(self.btn_reset_shot, 0, 1)
        ml.addWidget(self.btn_copy, 1, 0)
        ml.addWidget(self.btn_paste, 1, 1)
        pl.addWidget(g_misc)

        safety = QtWidgets.QLabel(
            "RAW files are opened read-only and never changed, moved or renamed. "
            "Approving writes only a .xmp sidecar next to the photo "
            "(an existing sidecar is backed up once as .xmp.bak).")
        safety.setWordWrap(True)
        safety.setStyleSheet("color:#888;font-size:11px;")
        pl.addWidget(safety)
        pl.addStretch(1)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(panel)
        scroll.setMinimumWidth(320)
        split.addWidget(scroll)
        split.setSizes([260, 900, 340])

        # wiring
        self.wb_combo.currentIndexChanged.connect(self.on_control)
        for s in (self.s_temp, self.s_tint):
            s.changed.connect(self.on_wb_slider)
        for s in (self.s_exp, self.s_con, self.s_hi, self.s_sh, self.s_vib, self.s_angle):
            s.changed.connect(self.on_control)
        self.up_combo.currentIndexChanged.connect(self.on_control)
        self.chk_lens.toggled.connect(self.on_control)
        self.btn_auto_wb.clicked.connect(self.do_auto_wb)
        self.btn_auto_st.clicked.connect(self.do_auto_straighten)
        self.btn_eyedrop.toggled.connect(self.on_eyedrop_toggle)
        self.btn_reset_auto.clicked.connect(self.reset_auto)
        self.btn_reset_shot.clicked.connect(self.reset_shot)
        self.btn_copy.clicked.connect(self.copy_settings)
        self.btn_paste.clicked.connect(self.paste_settings)

        self.statusBar().showMessage("Drag RAW photos or folders onto the window to start.")

    def _build_shortcuts(self):
        def sc(key, fn):
            s = QtGui.QShortcut(QtGui.QKeySequence(key), self)
            s.activated.connect(fn)
        sc("A", self.approve)
        sc("X", self.reject)
        sc("Right", lambda: self.step(1))
        sc("Left", lambda: self.step(-1))
        sc("\\", lambda: self.chk_before.toggle())
        sc("Ctrl+C", self.copy_settings)
        sc("Ctrl+V", self.paste_settings)
        sc("W", self.do_auto_wb)

    def _set_controls_enabled(self, on):
        for w in (self.wb_combo, self.btn_auto_wb, self.btn_eyedrop, self.s_temp, self.s_tint,
                  self.s_exp, self.s_con, self.s_hi, self.s_sh, self.s_vib, self.s_angle,
                  self.up_combo, self.btn_auto_st, self.chk_lens, self.btn_reset_auto,
                  self.btn_reset_shot, self.btn_copy, self.btn_paste, self.btn_approve,
                  self.btn_reject):
            w.setEnabled(on)

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
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Add RAW photos", "", f"RAW photos ({exts})")
        self.add_paths(files)

    def open_folder(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Add a folder (subfolders included)")
        if d:
            self.add_paths([d])

    def add_paths(self, paths):
        found, dng = [], 0
        def consider(p):
            nonlocal dng
            ext = os.path.splitext(p)[1].lower()
            if ext in core.RAW_EXTS:
                found.append(os.path.abspath(p))
            elif ext == ".dng":
                dng += 1
        for p in paths:
            if os.path.isdir(p):
                for root, dirs, files in os.walk(p):     # only lists, never changes anything
                    dirs.sort()
                    for fn in sorted(files):
                        consider(os.path.join(root, fn))
            elif os.path.isfile(p):
                consider(p)

        new = [f for f in found if f not in self.items]
        opts = dict(auto_wb=self.chk_auto_wb.isChecked(),
                    straighten=self.chk_auto_st.isChecked(),
                    allow_upright=self.chk_upright.isChecked())
        for f in new:
            it = Item(f)
            li = QtWidgets.QListWidgetItem(os.path.basename(f))
            li.setData(Qt.ItemDataRole.UserRole, f)
            li.setToolTip(f)
            it.list_item = li
            self.items[f] = it
            self.list.addItem(li)
            self._update_list_item(it)
            self._requested.add(f)
            self.pool.start(DecodeTask(self.gen, f, True, opts, self.signals))

        msg = f"Added {len(new)} photo(s)."
        if len(found) > len(new):
            msg += f" {len(found) - len(new)} already in the list."
        if dng:
            msg += f" Skipped {dng} DNG file(s) (Lightroom keeps DNG edits inside the file, which this app never modifies)."
        if not found and not dng:
            msg = "No RAW photos found in what was dropped."
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
        self.list.clear()
        self.view.set_image(None)
        self.view.set_message("Drag RAW photos or folders here")
        self.notes_lbl.setText("")
        self._set_controls_enabled(False)
        self._update_count()

    # ---------------- background results ----------------
    def on_decoded(self, gen, path, res, err):
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
        if "edit" in res:
            it.edit, it.auto_edit, it.wb = res["edit"], res["edit"].copy(), res["wb"]
            if it.status == "loading":
                it.status = "ready"
            it.list_item.setIcon(QtGui.QIcon(QtGui.QPixmap.fromImage(to_qimage(res["thumb"]))))
            self._update_list_item(it)
            self._update_count()
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
        label, col = STATUS_STYLE[it.status]
        sidecar = " · has XMP" if os.path.exists(core.sidecar_path(it.path)) and it.status != "approved" else ""
        extra = ""
        if it.wb is not None and it.wb.applied and it.wb.confidence < 0.5 and it.status == "ready":
            extra = " · check WB"
        it.list_item.setText(f"{os.path.basename(it.path)}\n{label}{extra}{sidecar}")
        it.list_item.setBackground(QtGui.QBrush(col) if col else QtGui.QBrush())
        it.list_item.setForeground(QtGui.QBrush(QtGui.QColor("white")) if col else QtGui.QBrush())
        if it.error:
            it.list_item.setToolTip(f"{it.path}\n{it.error}")

    def _update_count(self):
        n = len(self.items)
        c = {k: 0 for k in STATUS_STYLE}
        for it in self.items.values():
            c[it.status] += 1
        self.count_lbl.setText(
            f"{n} photos · {c['ready'] + c['modified']} to review · {c['approved']} approved · "
            f"{c['rejected']} rejected" + (f" · {c['loading']} analyzing" if c['loading'] else "")
            + (f" · {c['error']} errors" if c['error'] else ""))

    def on_row(self, row):
        if row < 0:
            return
        path = self.list.item(row).data(Qt.ItemDataRole.UserRole)
        self.current = self.items.get(path)
        self.btn_eyedrop.setChecked(False)
        self.load_controls()
        self.refresh_view()

    def step(self, d):
        if self.list.count() == 0:
            return
        r = min(max(0, self.list.currentRow() + d), self.list.count() - 1)
        self.list.setCurrentRow(r)

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
        self.s_temp.setValue(e.temp)
        self.s_tint.setValue(e.tint)
        self.s_exp.setValue(e.exposure)
        self.s_con.setValue(e.contrast)
        self.s_hi.setValue(e.highlights)
        self.s_sh.setValue(e.shadows)
        self.s_vib.setValue(e.vibrance)
        self.s_angle.setValue(e.angle)
        self.up_combo.setCurrentText(e.upright)
        self.chk_lens.setChecked(e.lens_profile)
        self._loading_controls = False
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
        e.wb_mode = "custom" if self.wb_combo.currentIndex() == 1 else "as_shot"
        if e.wb_mode == "as_shot" and it.color is not None:
            t, ti = it.color.as_shot_temp_tint
            self._loading_controls = True
            self.s_temp.setValue(round(t))
            self.s_tint.setValue(round(ti))
            self._loading_controls = False
        e.temp, e.tint = self.s_temp.value(), self.s_tint.value()
        e.exposure, e.contrast = self.s_exp.value(), int(self.s_con.value())
        e.highlights, e.shadows = int(self.s_hi.value()), int(self.s_sh.value())
        e.vibrance, e.angle = int(self.s_vib.value()), self.s_angle.value()
        e.upright = self.up_combo.currentText()
        e.lens_profile = self.chk_lens.isChecked()
        self._mark_changed(it)
        self.render_timer.start()

    def _mark_changed(self, it):
        if it.status == "approved":
            it.status = "modified"
            self._update_list_item(it)
            self._update_count()

    # ---------------- rendering ----------------
    def refresh_view(self):
        it = self.current
        if it is None:
            return
        if it.edit is None:
            self.view.set_image(None)
            self.view.set_message("Analyzing…" if it.status == "loading" else f"Could not read:\n{it.error}")
            return
        dec = self._dec(it)
        if dec is None:
            self.view.set_image(None)
            self.view.set_message("Loading preview…")
            return
        before = self.chk_before.isChecked()
        img, self.geom = core.render(dec, it.edit, original=before)
        overlay = []
        if (self.chk_samples.isChecked() and not before and it.wb is not None and it.wb.applied):
            for pts, col in ((it.wb.bright_pts, QtGui.QColor(0, 230, 255)),
                             (it.wb.dark_pts, QtGui.QColor(255, 60, 220))):
                for x, y in pts:
                    dx, dy = self.geom.to_display(x, y)
                    if 0 <= dx < img.shape[1] and 0 <= dy < img.shape[0]:
                        overlay.append((dx, dy, col))
        badge = "BEFORE (as shot)" if before else ""
        if not before and it.edit.upright != "Off":
            badge = f"Upright: {it.edit.upright} (applied by Lightroom, not shown here)"
        self.view.set_image(to_qimage(img), overlay, badge)
        self.view.set_message("Click a neutral spot…" if self.btn_eyedrop.isChecked() else "")

    # ---------------- tools ----------------
    def do_auto_wb(self):
        it = self.current
        if it is None or it.edit is None:
            return
        dec = self._dec(it)
        if dec is None:
            return
        wb = core.auto_white_balance(dec)
        it.wb = wb
        if wb.applied:
            it.edit.wb_mode, it.edit.temp, it.edit.tint = "custom", round(wb.temp), round(wb.tint)
        self._replace_note(it.edit, "Auto WB", wb.note)
        self._mark_changed(it)
        self.chk_samples.setChecked(True)
        self.load_controls()
        self.refresh_view()

    def do_auto_straighten(self):
        it = self.current
        if it is None or it.edit is None:
            return
        dec = self._dec(it)
        if dec is None:
            return
        img, _ = core.render(dec, it.edit, rotate=False)
        st = core.auto_straighten(img, self.chk_upright.isChecked())
        it.edit.angle, it.edit.upright = st.angle, st.upright
        self._replace_note(it.edit, "Straighten", st.note)
        self._mark_changed(it)
        self.load_controls()
        self.refresh_view()

    @staticmethod
    def _replace_note(edit, prefix, note):
        edit.notes = [n for n in edit.notes if not n.startswith(prefix)] + [note]

    def on_eyedrop_toggle(self, on):
        self.view.crosshair = on
        self.view.set_message("Click a neutral spot…" if on else "")

    def on_image_click(self, x, y):
        it = self.current
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
        it.edit.wb_mode, it.edit.temp, it.edit.tint = "custom", round(res[0]), round(res[1])
        self._replace_note(it.edit, "Auto WB", f"Auto WB: eyedropper → {round(res[0])}K / tint {round(res[1]):+d}")
        it.wb = None
        self._mark_changed(it)
        self.load_controls()
        self.refresh_view()

    def reset_auto(self):
        it = self.current
        if it and it.auto_edit:
            it.edit = it.auto_edit.copy()
            self._mark_changed(it)
            self.load_controls()
            self.refresh_view()

    def reset_shot(self):
        it = self.current
        dec = self._dec(it) if it else None
        if dec is not None:
            it.edit = core.as_shot_edit(dec)
            it.edit.notes = ["Reset to As Shot"]
            self._mark_changed(it)
            self.load_controls()
            self.refresh_view()

    def copy_settings(self):
        if self.current and self.current.edit:
            self.clip_edit = self.current.edit.copy()
            self.statusBar().showMessage("Settings copied. Select photos in the list, then Paste.", 6000)

    def paste_settings(self):
        if self.clip_edit is None:
            return
        n = 0
        for li in self.list.selectedItems():
            it = self.items[li.data(Qt.ItemDataRole.UserRole)]
            if it.edit is None:
                continue
            it.edit = self.clip_edit.copy()
            it.edit.notes = ["Pasted settings"]
            self._mark_changed(it)
            n += 1
        self.load_controls()
        self.refresh_view()
        self.statusBar().showMessage(f"Pasted settings to {n} photo(s) (not yet approved).", 6000)

    # ---------------- decisions ----------------
    def _write(self, it) -> bool:
        try:
            msg = core.write_xmp(it.path, it.edit, it.src_size)
        except core.RawSafetyError as ex:
            QtWidgets.QMessageBox.critical(self, "Stopped for safety", str(ex))
            return False
        except Exception as ex:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "Could not write sidecar", f"{it.path}\n\n{ex}")
            return False
        it.status = "approved"
        self._update_list_item(it)
        self._update_count()
        self.statusBar().showMessage(msg, 8000)
        return True

    def approve(self):
        it = self.current
        if it is None or it.edit is None:
            return
        if self._write(it):
            self.next_to_review()

    def reject(self):
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

    def approve_all(self):
        todo = [it for it in self.items.values() if it.status in ("ready", "modified") and it.edit]
        if not todo:
            self.statusBar().showMessage("Nothing waiting for review.", 5000)
            return
        low = sum(1 for it in todo if it.wb and it.wb.applied and it.wb.confidence < 0.5)
        msg = f"Write XMP sidecars for {len(todo)} photo(s) with their current settings?"
        if low:
            msg += f"\n\n{low} of them have low-confidence auto WB (marked 'check WB')."
        if QtWidgets.QMessageBox.question(self, "Approve all", msg) != QtWidgets.QMessageBox.StandardButton.Yes:
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
                QtWidgets.QMessageBox.information(
                    self, "Updates", f"You're up to date (v{__version__}).")
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
                f"If any files were already replaced, the previous version is saved in "
                f"{os.path.join(updater.APP_DIR, '.update_backup')}.")
            return
        QtWidgets.QMessageBox.information(
            self, "Updated", f"Updated to v{self._upd_version}. RAW Review will now restart.")
        updater.restart()
        self.close()

    def closeEvent(self, e):
        self.pool.clear()
        self.gen += 1
        self.pool.waitForDone(3000)
        super().closeEvent(e)


def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    pal = QtGui.QPalette()
    for role, c in [
        (QtGui.QPalette.ColorRole.Window, (45, 45, 48)), (QtGui.QPalette.ColorRole.WindowText, (230, 230, 230)),
        (QtGui.QPalette.ColorRole.Base, (32, 32, 34)), (QtGui.QPalette.ColorRole.AlternateBase, (45, 45, 48)),
        (QtGui.QPalette.ColorRole.Text, (230, 230, 230)), (QtGui.QPalette.ColorRole.Button, (58, 58, 62)),
        (QtGui.QPalette.ColorRole.ButtonText, (230, 230, 230)), (QtGui.QPalette.ColorRole.Highlight, (70, 110, 170)),
        (QtGui.QPalette.ColorRole.HighlightedText, (255, 255, 255)), (QtGui.QPalette.ColorRole.ToolTipBase, (60, 60, 60)),
        (QtGui.QPalette.ColorRole.ToolTipText, (230, 230, 230)),
    ]:
        pal.setColor(role, QtGui.QColor(*c))
    app.setPalette(pal)
    w = MainWindow()
    w.show()
    if len(sys.argv) > 1:
        w.add_paths(sys.argv[1:])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
