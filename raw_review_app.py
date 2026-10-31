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

import os
import sys
import traceback
from collections import OrderedDict

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, QTimer, Signal

import raw_review_auto as auto
import dng_profile
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


def make_thumb(dec, edit):
    import cv2
    img, _ = core.render(dec, edit)
    h, w = img.shape[:2]
    s = min(THUMB_W / w, THUMB_H / h)
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
                res.update(edit=edit, wb=wb, st=st, thumb=make_thumb(dec, edit))
            self.signals.done.emit(self.gen, self.path, res, "")
        except Exception as ex:  # noqa: BLE001
            traceback.print_exc()
            self.signals.done.emit(self.gen, self.path, None, f"{type(ex).__name__}: {ex}")


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
                                   {"edit": self.edit, "wb": res.get("wb"),
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
        gl = QtWidgets.QHBoxLayout(g)
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
        first = next((k for k in ("checked", "selected", "all", "current") if counts.get(k, 0) > 0
                      and (k != "selected" or counts[k] > 1)), "all")
        self.scope_btns[first].setChecked(True)
        lay.addWidget(g)

        ga = QtWidgets.QGroupBox("Run automatic tools")
        al = QtWidgets.QGridLayout(ga)
        self.auto_checks = {}
        for i, (key, label) in enumerate(auto.AUTO_GROUPS):
            cb = QtWidgets.QCheckBox(label)
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
        bb.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText("Apply")
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

    def spec(self) -> "auto.BatchSpec":
        scope = next(k for k, rb in self.scope_btns.items() if rb.isChecked())
        groups = [k for k in auto.GROUP_ORDER if self.auto_checks[k].isChecked()]
        changes = []
        for key, (cb, combo, spin) in self.rows.items():
            if cb.isChecked():
                changes.append((key, self.ACTIONS[combo.currentIndex()][1], spin.value()))
        return auto.BatchSpec(scope=scope, groups=groups, level_mode=self.level_combo.currentText(),
                              strength=self.strength.value() / 100.0,
                              exposure_bias=self.bias.value(), changes=changes)


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
        self.upd = UpdateSignals()
        self.upd.checked.connect(self.on_update_checked)
        self.upd.progress.connect(self.on_update_progress)
        self.upd.installed.connect(self.on_update_installed)
        self._upd_busy = False
        self._upd_progress = None
        self._upd_version = ""

        self.render_timer = QTimer(self)
        self.render_timer.setSingleShot(True)
        self.render_timer.setInterval(35)
        self.render_timer.timeout.connect(self.refresh_view)

        self._build_ui()
        self._build_shortcuts()
        self._set_controls_enabled(False)
        QTimer.singleShot(2500, lambda: self.check_updates(manual=False))   # quiet check

    # ---------------- UI ----------------
    def _opts_snapshot(self) -> "auto.AutoOptions":
        o = self.opts
        return auto.AutoOptions(set(o.groups), o.level_mode, o.allow_upright, o.strength, o.exposure_bias)

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
            a = menu.addAction("Auto " + label[0].lower() + label[1:])
            a.setCheckable(True)
            a.setChecked(True)
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
        self.act_upright = menu.addAction("Best guess may choose Upright: Vertical")
        self.act_upright.setCheckable(True)
        self.act_upright.setChecked(True)
        self.act_upright.toggled.connect(self._opts_changed)
        menu.addSeparator()
        sm = menu.addMenu("Auto strength")
        g2 = QtGui.QActionGroup(sm)
        for pct in (50, 75, 100, 125, 150):
            a = sm.addAction(f"{pct}%")
            a.setCheckable(True)
            g2.addAction(a)
            a.setChecked(pct == 100)
            a.triggered.connect(lambda _c=False, p=pct: setattr(self.opts, "strength", p / 100.0))
        bm = menu.addMenu("Exposure bias (brighter / darker)")
        g3 = QtGui.QActionGroup(bm)
        for ev in (-1.0, -0.5, -0.25, 0.0, 0.25, 0.5, 1.0):
            a = bm.addAction(f"{ev:+.2f} EV" if ev else "0 EV")
            a.setCheckable(True)
            g3.addAction(a)
            a.setChecked(ev == 0.0)
            a.triggered.connect(lambda _c=False, v=ev: setattr(self.opts, "exposure_bias", v))
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
        a = pm.addAction("Pixel local HDR tone map (match the camera's look)")
        a.setCheckable(True)
        a.setChecked(popt.hdr_tone_map)
        a.setToolTip("Applies the DNG's ProfileGainTableMap, the local tone map that lifts shadows and holds\n"
                     "highlights, so the preview matches the camera's own preview / JPEG.\n"
                     "Lightroom reads the same table from the DNG itself (the app never removes it). If a\n"
                     "photo looks darker in Lightroom than here, turn this off: the auto tools then\n"
                     "brighten with Exposure and Shadows instead.")
        a.toggled.connect(lambda on: self._set_profile_opt("hdr_tone_map", bool(on)))
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
            menu.addAction(auto.GROUP_LABEL[g], lambda g=g: self.run_auto_groups([g]))
        btn.setMenu(menu)
        return btn

    def _slider(self, key):
        s = core.SETTINGS[key]
        row = SliderRow(s.label, s.lo, s.hi, s.step, s.decimals, s.default)
        self.sliders[key] = row
        return row

    def _build_ui(self):
        self.opts = auto.AutoOptions()
        self.sliders = {}
        tb = self.addToolBar("Main")
        tb.setMovable(False)
        tb.addAction("Add Files…", self.open_files)
        tb.addAction("Add Folder…", self.open_folder)
        tb.addSeparator()
        tb.addWidget(self._build_auto_menu())
        self.act_batch = tb.addAction("Batch edit…", self.open_batch)
        self.act_batch.setToolTip("Run auto tools or raise/lower/set settings on checked, selected or all photos")
        self.act_undo_batch = tb.addAction("Undo batch", self.undo_batch)
        self.act_undo_batch.setEnabled(False)
        tb.addSeparator()
        self.chk_autowrite = QtWidgets.QCheckBox("Auto-write Lightroom XMP")
        self.chk_autowrite.setToolTip(
            "Write the .xmp sidecar for each photo as soon as it is analysed (and after batch edits),\n"
            "without waiting for you to approve. Off by default so you can review first.")
        tb.addWidget(self.chk_autowrite)
        tb.addSeparator()
        tb.addAction("Approve checked", self.approve_checked)
        tb.addAction("Approve All To-Review…", self.approve_all)
        tb.addAction("Clear List", self.clear_list)
        spacer = QtWidgets.QWidget()
        spacer.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        self.act_update = tb.addAction("Check for Updates", lambda: self.check_updates(manual=True))
        self.act_update.setToolTip(f"You have v{__version__}. Checks github.com/{updater.GITHUB_REPO}")

        split = QtWidgets.QSplitter()
        self.setCentralWidget(split)

        # Left: list with a check box per photo
        left = QtWidgets.QWidget()
        ll = QtWidgets.QVBoxLayout(left)
        ll.setContentsMargins(4, 4, 4, 4)
        self.list = QtWidgets.QListWidget()
        self.list.setIconSize(QtCore.QSize(THUMB_W, THUMB_H))
        self.list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.currentRowChanged.connect(self.on_row)
        self.list.itemChanged.connect(lambda _i: self._update_checked_label())
        self.count_lbl = QtWidgets.QLabel("No photos")
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
        cm.addAction("Invert", self.check_invert)
        chk_btn.setMenu(cm)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(chk_btn)
        top.addWidget(self.checked_lbl, 1)
        ll.addLayout(top)
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

        def group(title, auto_groups=None):
            box = QtWidgets.QGroupBox(title)
            lay = QtWidgets.QVBoxLayout(box)
            if auto_groups:
                lay.addWidget(self._auto_button(auto_groups), 0, Qt.AlignmentFlag.AlignRight)
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
        for k in ("exposure", "contrast", "highlights", "shadows", "whites", "blacks"):
            tl.addWidget(self._slider(k))
        cl2 = group("Colour", ["colour"])
        for k in ("vibrance", "saturation"):
            cl2.addWidget(self._slider(k))
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

        g_misc = QtWidgets.QGroupBox("This photo")
        ml = QtWidgets.QGridLayout(g_misc)
        self.btn_enhance = QtWidgets.QPushButton("Auto-enhance (E)")
        self.btn_enhance.setToolTip("Run every automatic tool ticked in 'Auto options'")
        self.btn_reset_auto = QtWidgets.QPushButton("Reset to auto proposal")
        self.btn_reset_shot = QtWidgets.QPushButton("Reset to As Shot")
        self.btn_copy = QtWidgets.QPushButton("Copy settings (Ctrl+C)")
        self.btn_paste = QtWidgets.QPushButton("Paste to checked/selected (Ctrl+V)")
        ml.addWidget(self.btn_enhance, 0, 0, 1, 2)
        ml.addWidget(self.btn_reset_auto, 1, 0)
        ml.addWidget(self.btn_reset_shot, 1, 1)
        ml.addWidget(self.btn_copy, 2, 0)
        ml.addWidget(self.btn_paste, 2, 1)
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
        self.btn_auto_wb.clicked.connect(self.do_auto_wb)
        self.btn_eyedrop.toggled.connect(self.on_eyedrop_toggle)
        self.btn_enhance.clicked.connect(self.auto_enhance)
        self.btn_reset_auto.clicked.connect(self.reset_auto)
        self.btn_reset_shot.clicked.connect(self.reset_shot)
        self.btn_copy.clicked.connect(self.copy_settings)
        self.btn_paste.clicked.connect(self.paste_settings)

        self.statusBar().showMessage("Drag RAW / DNG photos or folders onto the window to start.")

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
        sc("E", self.auto_enhance)
        sc("C", self.toggle_check_current)

    def _set_controls_enabled(self, on):
        widgets = [self.wb_combo, self.btn_auto_wb, self.btn_eyedrop, self.level_combo, self.btn_level,
                   self.chk_lens, self.btn_enhance, self.btn_reset_auto, self.btn_reset_shot,
                   self.btn_copy, self.btn_paste, self.btn_approve, self.btn_reject]
        widgets += list(self.sliders.values())
        for w in widgets:
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
        opts = self._opts_snapshot()
        for f in new:
            it = Item(f)
            li = QtWidgets.QListWidgetItem(os.path.basename(f))
            li.setData(Qt.ItemDataRole.UserRole, f)
            li.setToolTip(f)
            li.setFlags(li.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            li.setCheckState(Qt.CheckState.Unchecked)
            it.list_item = li
            self.items[f] = it
            self.list.addItem(li)
            self._update_list_item(it)
            self._requested.add(f)
            self.pool.start(DecodeTask(self.gen, f, True, opts, self.signals))

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
        self.list.clear()
        self.view.set_image(None)
        self.view.set_message("Drag RAW photos or folders here")
        self.notes_lbl.setText("")
        self._set_controls_enabled(False)
        self._update_count()
        self._update_checked_label()

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
            if self.chk_autowrite.isChecked() and it.status == "ready":
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
        for key, s in self.sliders.items():
            s.setValue(getattr(e, key))
        self.level_combo.setCurrentText(e.level_mode if e.level_mode in core.LEVEL_MODES else "Manual")
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
        for key, s in self.sliders.items():
            if key in ("temp", "tint"):
                continue
            st = core.SETTINGS[key]
            v = s.value()
            val = int(round(v)) if st.integer else round(v, st.decimals)
            old = getattr(e, key)
            if st.optional and (val != old or val != st.default):
                e.touched.add(key)          # now written to the sidecar
            if key == "angle" and abs(val - old) > 1e-9:
                e.level_mode = "Manual"
                self._loading_controls = True
                self.level_combo.setCurrentText("Manual")
                self._loading_controls = False
            setattr(e, key, val)
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
        if not before and it.list_item is not None:
            import cv2
            h, w = img.shape[:2]
            k = min(THUMB_W / w, THUMB_H / h)
            small = cv2.resize(img, (max(1, int(w * k)), max(1, int(h * k))), interpolation=cv2.INTER_AREA)
            it.list_item.setIcon(QtGui.QIcon(QtGui.QPixmap.fromImage(to_qimage(small))))
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
        self.run_auto_groups(["wb"])

    def _current_dec(self):
        it = self.current
        if it is None or it.edit is None:
            return None, None
        dec = self._dec(it)
        if dec is None:
            self.statusBar().showMessage("Still loading this photo, try again in a moment.", 4000)
        return it, dec

    def run_auto_groups(self, groups):
        it, dec = self._current_dec()
        if dec is None:
            return
        res = auto.run_auto(dec, it.edit, groups, self._opts_snapshot())
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
        self.run_auto_groups(list(groups))

    def do_level(self, mode):
        it, dec = self._current_dec()
        if dec is None:
            return
        if mode == "Manual":
            return
        auto.apply_level_mode(dec, it.edit, mode, self.opts.allow_upright)
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
            self.statusBar().showMessage("Settings copied. Check or select photos in the list, then Paste.", 6000)

    def paste_settings(self):
        if self.clip_edit is None:
            return
        targets = self.checked_items() or self.selected_items()
        n = 0
        for it in targets:
            if it.edit is None:
                continue
            it.edit = self.clip_edit.copy()
            it.edit.notes = ["Pasted settings"]
            self._mark_changed(it)
            n += 1
        self.load_controls()
        self.refresh_view()
        self.statusBar().showMessage(f"Pasted settings to {n} photo(s) (not yet approved).", 6000)

    # ---------------- checking photos / scopes ----------------
    def checked_items(self):
        return [it for it in self.items.values()
                if it.list_item.checkState() == Qt.CheckState.Checked]

    def selected_items(self):
        return [self.items[li.data(Qt.ItemDataRole.UserRole)] for li in self.list.selectedItems()]

    def scope_items(self, scope):
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

    def check_where(self, pred):
        self.list.blockSignals(True)
        for it in self.items.values():
            it.list_item.setCheckState(Qt.CheckState.Checked if pred(it) else Qt.CheckState.Unchecked)
        self.list.blockSignals(False)
        self._update_checked_label()

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

    # ---------------- batch edit ----------------
    def open_batch(self):
        if not self.items:
            self.statusBar().showMessage("Add some photos first.", 4000)
            return
        counts = {"current": 1 if self.current else 0, "selected": len(self.selected_items()),
                  "checked": len(self.checked_items()), "all": len(self.items)}
        dlg = BatchDialog(self, counts, self._opts_snapshot())
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self.run_batch(dlg.spec())

    def run_batch(self, spec):
        items = [it for it in self.scope_items(spec.scope) if it is not None and it.edit is not None]
        if not items:
            self.statusBar().showMessage("No analysed photos in that selection.", 5000)
            return
        self._undo = {it.path: (it.edit.copy(), it.status, it.wb) for it in items}
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
            it.edit = res["edit"]
            if res.get("wb") is not None:
                it.wb = res["wb"]
            it.list_item.setIcon(QtGui.QIcon(QtGui.QPixmap.fromImage(to_qimage(res["thumb"]))))
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
        self._mark_changed(it)
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
        for path, (edit, status, wb) in self._undo.items():
            it = self.items.get(path)
            if it is None:
                continue
            it.edit, it.wb = edit, wb
            it.status = "modified" if status == "approved" else status
            self._update_list_item(it)
        n = len(self._undo)
        self._undo = {}
        self.act_undo_batch.setEnabled(False)
        self._update_count()
        self.load_controls()
        self.refresh_view()
        self.statusBar().showMessage(f"Undid the last batch edit ({n} photo(s)). "
                                     "Any XMP already written is left as it was.", 8000)

    # ---------------- decisions ----------------
    def _write(self, it, quiet=False) -> bool:
        try:
            msg = core.write_xmp(it.path, it.edit, it.src_size)
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
