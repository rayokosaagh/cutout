"""Cutout - background remover front end.

Two looks share one layout, Dark and Light, both Google Material 3: tonal
surfaces, pill buttons with state layers and ripples, outlined segmented
buttons, M3 checkboxes, snackbars.

Window controls are macOS-style traffic lights. Everything that changes state
animates. Short notices (saved, errors, actions) appear as a toast that goes
away on its own after 2.3 seconds.

Cuts are held for review, then saved with the chosen crop or aspect ratio,
size and background. Model files are managed by model_store; framing by fit;
formats, model sessions and browser drops come from cutout_core.
"""

import ctypes
import json
import math
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from PIL import Image
from PIL.ImageQt import ImageQt
from PySide6.QtCore import (QEasingCurve, QEvent, QObject, QPoint, QPointF, QRectF,
                            Qt, QTimer, QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QCursor, QFont, QFontDatabase,
                           QFontMetrics, QGuiApplication, QIcon, QImage, QKeySequence,
                           QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QShortcut)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QFileDialog,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QSizePolicy, QVBoxLayout, QWidget)

import cutout_core as core
import model_store
from enhance import enhance_rgba
from fit import SIZES, crop_to, fit_aspect, flatten, scale_long_edge

# Updated model files no longer match rembg's pinned checksums; model_store checks sizes instead.
os.environ.setdefault("MODEL_CHECKSUM_DISABLED", "1")

FONT = "Segoe UI"
APP_NAME = "Cutout"
APP_VERSION = "1.0.0"

# ------------------------------------------------------------------ theme --
# Material colours follow Google's Material 3 (Workspace) palette.

THEMES = {
    "dark": dict(
        dark=True,
        window=(19, 19, 20), card=(30, 31, 32), well=(24, 25, 26), menu=(40, 42, 44),
        primary=(168, 199, 250), on_primary=(6, 46, 111),
        sec_cont=(0, 74, 119), on_sec_cont=(194, 231, 255),
        outline=(142, 145, 143), outline_var=(68, 71, 70),
        ink=(227, 227, 227), ink_soft=(196, 199, 197), ink_faint=(142, 145, 143),
        track=(0, 74, 119), warn=(253, 214, 99),
        error=(242, 184, 181), success=(109, 213, 140),
        inverse=(227, 227, 227), on_inverse=(48, 48, 48), inverse_primary=(11, 87, 208),
        shadow=0),
    "light": dict(
        dark=False,
        window=(240, 244, 249), card=(255, 255, 255), well=(248, 250, 253), menu=(255, 255, 255),
        primary=(11, 87, 208), on_primary=(255, 255, 255),
        sec_cont=(211, 227, 253), on_sec_cont=(4, 30, 73),
        outline=(116, 119, 117), outline_var=(196, 199, 197),
        ink=(31, 31, 31), ink_soft=(68, 71, 70), ink_faint=(116, 119, 117),
        track=(211, 227, 253), warn=(138, 80, 0),
        error=(179, 38, 30), success=(20, 108, 46),
        inverse=(48, 48, 48), on_inverse=(242, 242, 242), inverse_primary=(168, 199, 250),
        shadow=1),
}
THEME_ORDER = ["dark", "light"]
T = dict(THEMES["dark"])


def qc(rgb, alpha=None):
    c = QColor(*rgb)
    if alpha is not None:
        c.setAlpha(int(max(0, min(255, alpha))))
    return c


def mix(a, b, t):
    """Blend two colours (rgb or rgba tuples) by t."""
    a, b = QColor(*a) if isinstance(a, tuple) else QColor(a), QColor(*b) if isinstance(b, tuple) else QColor(b)
    return QColor(int(a.red() + (b.red() - a.red()) * t), int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t), int(a.alpha() + (b.alpha() - a.alpha()) * t))


# ------------------------------------------------------------ animation --

def animate(owner, attr, end, ms=180, curve=QEasingCurve.OutCubic, done=None):
    """Tween a float attribute on `owner`, repainting it every frame."""
    anims = owner.__dict__.setdefault("_anims", {})
    anim = anims.get(attr)
    if anim is None:
        anim = QVariantAnimation(owner)
        anim.valueChanged.connect(lambda v, o=owner, n=attr: (setattr(o, n, float(v)), o.update()))
        anims[attr] = anim
    anim.stop()
    previous = anim.__dict__.pop("_done", None)
    if previous is not None:
        anim.finished.disconnect(previous)
    if done:
        anim.finished.connect(done)
        anim._done = done
    start = float(getattr(owner, attr))
    if abs(start - end) < 1e-4:
        setattr(owner, attr, float(end))
        owner.update()
        if done:
            done()
        return
    anim.setDuration(ms)
    anim.setEasingCurve(curve)
    anim.setStartValue(start)
    anim.setEndValue(float(end))
    anim.start()


# --------------------------------------------------------------- windows --

def set_frame(hwnd, dark=True):
    """Tell Windows whether the frame is dark or light, and ask for rounded corners."""
    try:
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(ctypes.c_int(1 if dark else 0)), 4)
        dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(ctypes.c_int(2)), 4)
    except Exception:
        pass


def round_region(widget, radius):
    """Clip the window to rounded corners."""
    try:
        r = widget.devicePixelRatioF()
        w, h = int(widget.width() * r), int(widget.height() * r)
        rad = 0 if widget.isMaximized() else int(radius * 2 * r)
        region = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, rad, rad)
        ctypes.windll.user32.SetWindowRgn(int(widget.winId()), region, True)
    except Exception:
        pass


# ---------------------------------------------------------------- paint --

def rounded(rect, radius):
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    return path


def soft_shadow(p, rect, radius, strength=1.0, spread=10, dy=3):
    """Cheap elevation shadow: stacked translucent rounded rects."""
    p.save()
    p.setPen(Qt.NoPen)
    for i in range(spread, 0, -2):
        a = int(10 * strength * (1 - i / (spread + 1)) ** 1.5)
        p.setBrush(QColor(0, 0, 0, a))
        p.drawRoundedRect(rect.adjusted(-i / 2, -i / 2 + dy, i / 2, i / 2 + dy), radius + i / 2, radius + i / 2)
    p.restore()


def tick_path(rect, progress):
    """Checkmark drawn up to `progress` (0..1) of its length."""
    x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
    pts = [QPointF(x + w * .2, y + h * .52), QPointF(x + w * .42, y + h * .74), QPointF(x + w * .8, y + h * .28)]
    lens = [math.dist((pts[0].x(), pts[0].y()), (pts[1].x(), pts[1].y())),
            math.dist((pts[1].x(), pts[1].y()), (pts[2].x(), pts[2].y()))]
    left = (lens[0] + lens[1]) * progress
    path = QPainterPath(pts[0])
    for i in range(2):
        if left <= 0:
            break
        t = min(left / lens[i], 1)
        a, b = pts[i], pts[i + 1]
        path.lineTo(a.x() + (b.x() - a.x()) * t, a.y() + (b.y() - a.y()) * t)
        left -= lens[i]
    return path


def draw_icon(p, name, rect, color, width=1.7):
    p.save()
    pen = QPen(color, width)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()

    def P(a, b):
        return QPointF(x + w * a, y + h * b)

    if name == "plus":
        p.drawLine(P(.5, .18), P(.5, .82)); p.drawLine(P(.18, .5), P(.82, .5))
    elif name in ("x", "close"):
        k = .22 if name == "x" else .28
        p.drawLine(P(k, k), P(1 - k, 1 - k)); p.drawLine(P(1 - k, k), P(k, 1 - k))
    elif name == "folder":
        path = QPainterPath(P(.1, .34))
        path.lineTo(P(.1, .22)); path.lineTo(P(.4, .22)); path.lineTo(P(.48, .3))
        path.lineTo(P(.9, .3)); path.lineTo(P(.9, .82)); path.lineTo(P(.1, .82)); path.closeSubpath()
        p.drawPath(path)
    elif name == "trash":
        p.drawLine(P(.16, .26), P(.84, .26))
        p.drawPolyline([P(.38, .26), P(.4, .14), P(.6, .14), P(.62, .26)])
        p.drawRoundedRect(QRectF(P(.24, .26), P(.76, .88)), 1.5, 1.5)
        p.drawLine(P(.42, .4), P(.42, .74)); p.drawLine(P(.58, .4), P(.58, .74))
    elif name == "image":
        p.drawRoundedRect(QRectF(P(.08, .14), P(.92, .86)), w * .12, w * .12)
        p.setBrush(color); p.drawEllipse(QRectF(P(.6, .28), P(.76, .44))); p.setBrush(Qt.NoBrush)
        p.drawPolyline([P(.12, .78), P(.38, .5), P(.58, .7), P(.7, .58), P(.88, .76)])
    elif name == "tick":
        p.drawPath(tick_path(rect, 1))
    elif name == "chev-down":
        p.drawPolyline([P(.25, .38), P(.5, .63), P(.75, .38)])
    elif name == "chev-right":
        p.drawPolyline([P(.38, .25), P(.63, .5), P(.38, .75)])
    elif name == "success":
        p.drawEllipse(QRectF(P(.06, .06), P(.94, .94)))
        p.drawPath(tick_path(QRectF(P(.18, .18), P(.82, .82)), 1))
    elif name == "error":
        p.drawEllipse(QRectF(P(.06, .06), P(.94, .94)))
        p.drawLine(P(.34, .34), P(.66, .66)); p.drawLine(P(.66, .34), P(.34, .66))
    elif name == "warning":
        tri = QPainterPath(P(.5, .08)); tri.lineTo(P(.94, .88)); tri.lineTo(P(.06, .88)); tri.closeSubpath()
        p.drawPath(tri)
        p.drawLine(P(.5, .36), P(.5, .6)); p.drawPoint(P(.5, .74))
    elif name == "info":
        p.drawEllipse(QRectF(P(.06, .06), P(.94, .94)))
        p.drawLine(P(.5, .44), P(.5, .72)); p.drawPoint(P(.5, .3))
    elif name == "retry":
        p.drawArc(QRectF(P(.16, .16), P(.84, .84)), 70 * 16, 280 * 16)
        tip = P(.5 + .34 * math.cos(math.radians(70)), .5 - .34 * math.sin(math.radians(70)))
        p.drawPolyline([QPointF(tip.x() - w * .2, tip.y() - h * .08), tip, QPointF(tip.x() - w * .04, tip.y() + h * .18)])
    elif name == "save":
        p.drawLine(P(.5, .12), P(.5, .62))
        p.drawPolyline([P(.3, .43), P(.5, .63), P(.7, .43)])
        p.drawPolyline([P(.14, .62), P(.14, .86), P(.86, .86), P(.86, .62)])
    elif name == "crop":
        p.drawPolyline([P(.28, .06), P(.28, .72), P(.94, .72)])
        p.drawPolyline([P(.06, .28), P(.72, .28), P(.72, .94)])
    elif name == "dropper":
        p.drawLine(P(.16, .84), P(.6, .4))
        p.drawLine(P(.48, .3), P(.7, .52))
        p.drawEllipse(QRectF(P(.58, .1), P(.9, .42)))
        p.drawLine(P(.16, .84), P(.1, .9))
    elif name == "gear":
        c = P(.5, .5)
        for k in range(8):
            a = math.pi * k / 4
            p.drawLine(QPointF(c.x() + w * .3 * math.cos(a), c.y() + h * .3 * math.sin(a)),
                       QPointF(c.x() + w * .44 * math.cos(a), c.y() + h * .44 * math.sin(a)))
        p.drawEllipse(c, w * .29, h * .29)
        p.drawEllipse(c, w * .11, h * .11)
    p.restore()


def font(size, weight=QFont.Normal):
    f = QFont(FONT)
    f.setPointSizeF(size)
    f.setWeight(weight)
    return f


# -------------------------------------------------------------- widgets --

class Themed(QWidget):
    """Base: repaints when the theme changes; claims presses so clicks never
    fall through to the window (which would start a window drag instead)."""

    def restyle(self):
        self.update()

    def mousePressEvent(self, e):
        e.accept()


class Panel(QWidget):
    def __init__(self, radius=26, parent=None):
        super().__init__(parent)
        self.radius = radius

    def restyle(self):
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(.8, .8, -.8, -.8)
        r = 24
        if T["shadow"]:
            soft_shadow(p, rect.adjusted(2, 2, -2, -2), r, 0.9, spread=8, dy=2)
        p.fillPath(rounded(rect, r), qc(T["card"]))
        if not T["dark"]:
            p.setPen(QPen(qc(T["outline_var"], 120), 1))
            p.drawPath(rounded(rect, r))


class Button(Themed):
    """Material filled / tonal / outlined pill or field, with state layer and ripple."""

    def __init__(self, text="", icon=None, kind="outlined", w=None, h=36,
                 radius=12, trail=None, on_click=None, parent=None):
        super().__init__(parent)
        self.text, self.icon, self.kind, self.trail = text, icon, kind, trail
        self.radius = radius
        self.on_click = on_click
        self.h = self.d = 0.0             # hover / press layers, animated 0..1
        self.en = 1.0                     # enabled, animated
        self.enabled = True
        self.ripples = []                 # [pos, progress]
        self.glow = 10 if kind == "primary" else 3    # padding around the body, kept for layout
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        fw = w if w else self._natural_width()
        self.setFixedSize(fw + self.glow * 2, h + self.glow * 2)

    def _natural_width(self):
        fm = QFontMetrics(font(10, QFont.DemiBold))
        width = fm.horizontalAdvance(self.text) + 36
        if self.icon:
            width += 24
        if self.trail:
            width += 22
        return max(width, 36)

    def set_text(self, text):
        self.text = text
        self.update()

    def set_enabled(self, value):
        self.enabled = value
        self.setCursor(Qt.PointingHandCursor if value else Qt.ArrowCursor)
        animate(self, "en", 1.0 if value else 0.0, 260)

    def enterEvent(self, _):
        if self.enabled:
            animate(self, "h", 1, 150)

    def leaveEvent(self, _):
        animate(self, "h", 0, 220)
        animate(self, "d", 0, 220)

    def mousePressEvent(self, e):
        e.accept()
        if e.button() == Qt.LeftButton and self.enabled:
            animate(self, "d", 1, 90)
            ripple = [e.position(), 0.0]
            self.ripples.append(ripple)
            anim = QVariantAnimation(self, duration=520, easingCurve=QEasingCurve.OutQuad, startValue=0.0, endValue=1.0)
            anim.valueChanged.connect(lambda v, r=ripple: (r.__setitem__(1, float(v)), self.update()))
            anim.finished.connect(lambda r=ripple: (self.ripples.remove(r) if r in self.ripples else None, self.update()))
            anim.start(QVariantAnimation.DeleteWhenStopped)

    def mouseReleaseEvent(self, e):
        animate(self, "d", 0, 260)
        if (e.button() == Qt.LeftButton and self.enabled
                and self.rect().contains(e.position().toPoint()) and self.on_click):
            self.on_click()

    def _body(self):
        g = self.glow
        return QRectF(self.rect()).adjusted(g + .6, g + .6, -g - .6, -g - .6)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = self._body()
        ink = self._paint_material(p, rect)
        self._paint_label(p, rect, ink)

    def _paint_material(self, p, rect):
        field = self.kind == "field"
        radius = 12 if field else rect.height() / 2
        path = rounded(rect, radius)
        en = self.en
        disabled_fill = qc(T["ink"], 30)
        if self.kind == "primary":
            fill, ink = mix(disabled_fill, qc(T["primary"]), en), mix(qc(T["ink"], 97), qc(T["on_primary"]), en)
            p.fillPath(path, fill)
        elif self.kind == "tonal":
            fill, ink = mix(disabled_fill, qc(T["sec_cont"]), en), mix(qc(T["ink"], 97), qc(T["on_sec_cont"]), en)
            p.fillPath(path, fill)
        else:
            border = qc(T["outline"]) if field else qc(T["outline"], 150 if not T["dark"] else 170)
            p.setPen(QPen(border, 1))
            p.drawPath(rounded(rect.adjusted(.5, .5, -.5, -.5), radius))
            ink = qc(T["ink"]) if field else qc(T["primary"])
            ink = mix(qc(T["ink"], 97), ink, en)
        # state layer + ripples, clipped to the shape
        p.save()
        p.setClipPath(path)
        layer = self.h * 20 + self.d * 12
        if layer:
            p.fillPath(path, QColor(ink.red(), ink.green(), ink.blue(), int(layer * en)))
        for pos, t in self.ripples:
            radius_max = math.hypot(rect.width(), rect.height())
            rr = radius_max * t
            p.setBrush(QColor(ink.red(), ink.green(), ink.blue(), int(34 * (1 - t))))
            p.setPen(Qt.NoPen)
            p.drawEllipse(pos, rr, rr)
        p.restore()
        return ink

    def _paint_label(self, p, rect, ink):
        p.setFont(font(10, QFont.DemiBold))
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(self.text)
        iw = 16 if self.icon else 0
        gap = 8 if (self.icon and self.text) else 0
        total = iw + gap + tw
        if self.kind == "field":
            x = rect.left() + 18
        else:
            x = rect.center().x() - total / 2 - (10 if self.trail else 0)
        if self.icon:
            draw_icon(p, self.icon, QRectF(x, rect.center().y() - 8, 16, 16), ink)
            x += iw + gap
        if self.text:
            p.setPen(ink)
            p.drawText(QRectF(x, rect.top(), tw + 2, rect.height()), Qt.AlignVCenter | Qt.AlignLeft, self.text)
        if self.trail:
            draw_icon(p, self.trail, QRectF(rect.right() - 28, rect.center().y() - 7, 14, 14), ink, 1.6)


class Segmented(Themed):
    """Material outlined segmented button, selected segment tonal with a check.
    The selection slides between segments."""

    def __init__(self, options, index=0, on_change=None, w=230, h=34, parent=None):
        super().__init__(parent)
        self.options, self.index, self.on_change = options, index, on_change
        self.setFixedSize(w, h)
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)
        self.pos_f = float(index)
        self.hot, self.h = -1, 0.0

    def select(self, index, notify=True):
        if index == self.index and notify:
            return
        self.index = index
        animate(self, "pos_f", index, 340, QEasingCurve.OutCubic)
        if notify and self.on_change:
            self.on_change(index)

    def _seg_at(self, x):
        return min(int(x // (self.width() / len(self.options))), len(self.options) - 1)

    def mouseMoveEvent(self, e):
        hot = self._seg_at(e.position().x())
        if hot != self.hot:
            self.hot = hot
            self.h = 0.0
            animate(self, "h", 1, 140)

    def leaveEvent(self, _):
        animate(self, "h", 0, 200)

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton or not self.rect().contains(e.position().toPoint()):
            return
        self.select(self._seg_at(e.position().x()))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(.6, .6, -.6, -.6)
        r = rect.height() / 2
        n = len(self.options)
        seg = rect.width() / n
        path = rounded(rect, r)
        p.save()
        p.setClipPath(path)
        knob = QRectF(rect.left() + seg * self.pos_f, rect.top(), seg, rect.height())
        p.fillRect(knob, qc(T["sec_cont"]))
        if self.hot >= 0 and self.h:
            cell = QRectF(rect.left() + seg * self.hot, rect.top(), seg, rect.height())
            p.fillRect(cell, qc(T["ink"], 18 * self.h))
        p.restore()
        p.setPen(QPen(qc(T["outline"], 190), 1))
        p.drawPath(rounded(rect.adjusted(.4, .4, -.4, -.4), r))
        for i in range(1, n):
            x = rect.left() + seg * i
            p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
        p.setFont(font(9.5, QFont.DemiBold))
        fm = p.fontMetrics()
        for i, label in enumerate(self.options):
            cell = QRectF(rect.left() + seg * i, rect.top(), seg, rect.height())
            near = max(0.0, 1 - abs(self.pos_f - i))
            ink = mix(qc(T["ink"]), qc(T["on_sec_cont"]), near)
            tw = fm.horizontalAdvance(label)
            icon_w = 16 * near
            x = cell.center().x() - (tw + icon_w + 4 * near) / 2
            if near > 0.05:
                p.save()
                p.setOpacity(near)
                pen = QPen(ink, 1.8); pen.setCapStyle(Qt.RoundCap); pen.setJoinStyle(Qt.RoundJoin)
                p.setPen(pen)
                p.drawPath(tick_path(QRectF(x, cell.center().y() - 7, 14, 14), near))
                p.restore()
            p.setPen(ink)
            p.drawText(QRectF(x + icon_w + 4 * near, cell.top(), tw + 2, cell.height()),
                       Qt.AlignVCenter | Qt.AlignLeft, label)


class Check(Themed):
    def __init__(self, text, on_toggle=None, parent=None):
        super().__init__(parent)
        self.text, self.on_toggle, self.checked = text, on_toggle, False
        self.c = 0.0            # check progress, animated
        self.h = 0.0            # hover
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(40 + QFontMetrics(font(10.5)).horizontalAdvance(text) + 8, 34)

    def set_checked(self, value):
        self.checked = value
        animate(self, "c", 1.0 if value else 0.0, 240, QEasingCurve.OutCubic)

    def enterEvent(self, _):
        animate(self, "h", 1, 150)

    def leaveEvent(self, _):
        animate(self, "h", 0, 220)

    def mouseReleaseEvent(self, e):
        if self.on_toggle and e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.on_toggle()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        cy = self.height() / 2
        c = self.c
        box = QRectF(9, cy - 9, 18, 18)
        if self.h:
            layer = mix(qc(T["ink"]), qc(T["primary"]), c)
            layer.setAlpha(int(22 * self.h))
            p.setPen(Qt.NoPen); p.setBrush(layer)
            p.drawEllipse(box.center(), 18, 18)
        p.setPen(QPen(mix(qc(T["ink_soft"]), qc(T["primary"]), c), 2))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(box.adjusted(1, 1, -1, -1), 2, 2)
        if c > 0.01:
            p.setPen(Qt.NoPen)
            p.setBrush(qc(T["primary"], 255 * min(1, c * 1.6)))
            p.drawRoundedRect(box, 2.5, 2.5)
            pen = QPen(qc(T["on_primary"]), 2.2); pen.setCapStyle(Qt.RoundCap); pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen); p.setBrush(Qt.NoBrush)
            p.drawPath(tick_path(box.adjusted(2, 2, -2, -2), c))
        ink = qc(T["ink"])
        tx = 38
        p.setPen(ink)
        p.setFont(font(10.5))
        p.drawText(QRectF(tx, 0, self.width() - tx, self.height()), Qt.AlignVCenter, self.text)


class Label(QLabel):
    def __init__(self, text, size=10.5, weight=QFont.Normal, role="ink", parent=None):
        super().__init__(text, parent)
        self.role = role
        self.setFont(font(size, weight))
        self.restyle()

    def restyle(self):
        c = T[self.role]
        self.setStyleSheet(f"color: rgb({c[0]},{c[1]},{c[2]}); background: transparent;")


class Progress(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.value, self.maximum, self.indeterminate, self.spin = 0, 1, False, 0.0
        self.shown = 0.0         # displayed fraction, eased toward the real one
        self.setFixedHeight(12)
        self.timer = QTimer(self, interval=16)
        self.timer.timeout.connect(self._tick)

    def restyle(self):
        self.update()

    def set(self, value=None, maximum=None, indeterminate=None):
        if value is not None:
            self.value = value
        if maximum is not None:
            self.maximum = max(maximum, 1)
        if indeterminate is not None:
            self.indeterminate = indeterminate
            self.timer.start() if indeterminate else self.timer.stop()
        target = min(self.value / self.maximum, 1.0)
        animate(self, "shown", target, 420 if target >= self.shown else 200, QEasingCurve.OutCubic)

    def _tick(self):
        self.spin = (self.spin + .010) % 1.0
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(0, self.height() / 2 - 3, self.width(), 6)
        r = rect.height() / 2
        p.fillPath(rounded(rect, r), qc(T["track"]))
        if self.indeterminate:
            w = rect.width() * .3
            x = rect.left() + (rect.width() + w) * self.spin - w
            fill = QRectF(max(rect.left(), x), rect.top(), 0, rect.height())
            fill.setRight(min(rect.right(), x + w))
        else:
            fill = QRectF(rect.left(), rect.top(), max(rect.height(), rect.width() * self.shown), rect.height())
        p.fillPath(rounded(fill, r), qc(T["primary"]))


class DropZone(Themed):
    """Dashed well: an empty-state prompt, or the queue of files."""

    def __init__(self, on_browse, parent=None):
        super().__init__(parent)
        self.on_browse = on_browse
        self.h = 0.0          # hover (empty state)
        self.hot = 0.0        # a drag is over the window
        self.list = QListWidget(self)
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setFrameShape(QListWidget.NoFrame)
        self.list.setFont(font(10))
        # Long names shorten in the middle, so the "ready" / "saved" tag stays visible.
        self.list.setTextElideMode(Qt.ElideMiddle)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.hide()
        self.setCursor(Qt.PointingHandCursor)
        self.restyle()

    def restyle(self):
        ink = T["ink"]
        sel_bg, sel_ink = T["sec_cont"], T["on_sec_cont"]
        hover = f"rgba({ink[0]},{ink[1]},{ink[2]},20)"
        handle = f"rgba({ink[0]},{ink[1]},{ink[2]},70)"
        self.list.setStyleSheet(
            "QListWidget { background: transparent; border: none; outline: none;"
            f" color: rgb({ink[0]},{ink[1]},{ink[2]}); padding: 6px; }}"
            "QListWidget::item { padding: 6px 10px; border-radius: 10px; margin: 1px 0; }"
            f"QListWidget::item:selected {{ background: rgb({sel_bg[0]},{sel_bg[1]},{sel_bg[2]});"
            f" color: rgb({sel_ink[0]},{sel_ink[1]},{sel_ink[2]}); }}"
            f"QListWidget::item:hover:!selected {{ background: {hover}; }}"
            "QScrollBar:vertical { background: transparent; width: 8px; margin: 4px; }"
            f"QScrollBar::handle:vertical {{ background: {handle}; border-radius: 3px; min-height: 30px; }}"
            "QScrollBar::add-line, QScrollBar::sub-line { height: 0; }")
        self.update()

    def set_empty(self, empty):
        self.list.setVisible(not empty)
        self.setCursor(Qt.PointingHandCursor if empty else Qt.ArrowCursor)
        self.update()

    def enterEvent(self, _):
        animate(self, "h", 1, 160)

    def leaveEvent(self, _):
        animate(self, "h", 0, 240)

    def resizeEvent(self, _):
        self.list.setGeometry(self.rect().adjusted(6, 6, -6, -6))

    def mouseReleaseEvent(self, e):
        if self.list.isHidden() and e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.on_browse()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        path = rounded(rect, 16)
        empty = self.list.isHidden()
        glow = max(self.hot, self.h * 0.5 if empty else 0)
        p.fillPath(path, mix(qc(T["well"]), qc(T["sec_cont"]), 0.35 * glow))
        dash = mix(qc(T["outline"], 170), qc(T["primary"]), glow)
        pen = QPen(dash, 1.2 + 0.8 * self.hot, Qt.CustomDashLine)
        pen.setDashPattern([5, 4])
        p.setPen(pen)
        p.drawPath(path)
        if not empty:
            return
        c = rect.center()
        lift = 3 * self.h
        tile = QRectF(c.x() - 38, c.y() - 70 - lift, 76, 76)
        p.setPen(Qt.NoPen)
        p.setBrush(qc(T["sec_cont"]))
        p.drawEllipse(tile)
        draw_icon(p, "image", tile.adjusted(20, 20, -20, -20), qc(T["on_sec_cont"]), 2.2)
        p.setPen(qc(T["ink"]))
        p.setFont(font(12.5, QFont.Medium))
        p.drawText(QRectF(rect.left(), c.y() + 18, rect.width(), 26), Qt.AlignCenter,
                   "Drop to add" if self.hot > .5 else "Drag & drop images here")
        p.setPen(qc(T["ink_soft"]))
        p.setFont(font(9.5))
        p.drawText(QRectF(rect.left(), c.y() + 44, rect.width(), 20), Qt.AlignCenter, "or click to browse")


class Preview(QWidget):
    """Crossfades between images and messages."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pixmap, self.message = None, "Add an image to begin"
        self.old = (None, "")
        self.f = 1.0          # fade progress of the current content
        self.busy_a = 0.0     # loader visibility, animated
        self.caption = ""
        self.t0 = time.perf_counter()
        self.spin = QTimer(self, interval=16)
        self.spin.timeout.connect(self.update)
        self.crop_hint = None     # (x0, y0, x1, y1) of the current image, shown dimmed outside
        self.reserve = 0          # px kept free at the bottom (the crop tool's bar)

    def restyle(self):
        self.update()

    def image_rect(self, pixmap=None, lift=0.0):
        """Where the current image is drawn."""
        pixmap = pixmap or self.pixmap
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        area = rect.adjusted(18, 18, -18, -18 - self.reserve)
        if not pixmap:
            return area
        size = pixmap.size().scaled(area.size().toSize(), Qt.KeepAspectRatio)
        target = QRectF(0, 0, size.width(), size.height())
        target.moveCenter(area.center() + QPointF(0, lift))
        return target

    def resizeEvent(self, _):
        for tool in self.findChildren(CropTool):
            tool.setGeometry(self.rect())

    def _swap(self, pixmap, message):
        if pixmap is self.pixmap and message == self.message:
            return
        self.old = (self.pixmap, self.message)
        self.pixmap, self.message = pixmap, message
        self.f = 0.0
        animate(self, "f", 1.0, 300, QEasingCurve.OutCubic)

    def show_pixmap(self, pixmap):
        self._swap(pixmap, "")

    def show_message(self, text):
        self._swap(None, text)

    def set_busy(self, on, caption=None):
        """Show (or fade away) the logo loader over the preview."""
        if caption is not None:
            self.caption = caption
        if on:
            if not self.spin.isActive():
                self.t0 = time.perf_counter()
                self.spin.start()
            animate(self, "busy_a", 1.0, 260)
        else:
            animate(self, "busy_a", 0.0, 380, QEasingCurve.InOutCubic,
                    done=lambda: self.spin.stop() if self.busy_a < .01 else None)
        self.update()

    def _loader(self, p, rect, path):
        a = self.busy_a
        p.save()
        # An image being cut shows faintly through; a placeholder is covered.
        p.fillPath(path, qc(T["well"], (215 if self.pixmap else 255) * a))
        p.setOpacity(a)
        t = time.perf_counter() - self.t0
        cycle = 1.5
        # The flap whips once round the ring, then rests where it belongs.
        turn = -360 * QEasingCurve(QEasingCurve.InOutCubic).valueForProgress((t % cycle) / cycle)
        breathe = 1 + .035 * math.sin(t * 2 * math.pi / cycle)
        c = rect.center() - QPointF(0, 16)
        draw_mark(p, c, 36 * breathe, T["ink"], flap_turn=turn)
        if self.caption:
            p.setPen(qc(T["ink_soft"]))
            p.setFont(font(10))
            text = QFontMetrics(font(10)).elidedText(self.caption, Qt.ElideMiddle, int(rect.width() - 60))
            p.drawText(QRectF(rect.left() + 30, c.y() + 52, rect.width() - 60, 24), Qt.AlignCenter, text)
        p.restore()

    def _content(self, p, rect, pixmap, message, alpha, lift):
        if alpha <= 0.01:
            return
        p.save()
        p.setOpacity(alpha)
        if pixmap:
            target = self.image_rect(pixmap, lift)
            p.setClipPath(rounded(target, 14))
            p.drawPixmap(target, pixmap, QRectF(pixmap.rect()))
            hint = self.crop_hint if pixmap is self.pixmap and not self.reserve else None
            if hint:
                x0, y0, x1, y1 = hint
                keep = QRectF(target.left() + x0 * target.width(), target.top() + y0 * target.height(),
                              (x1 - x0) * target.width(), (y1 - y0) * target.height())
                outside = QPainterPath()
                outside.addRect(target)
                inner = QPainterPath()
                inner.addRect(keep)
                p.fillPath(outside.subtracted(inner), QColor(0, 0, 0, 120))
                p.setPen(QPen(QColor(255, 255, 255, 200), 1.2, Qt.DashLine))
                p.drawRect(keep)
        elif message:
            c = rect.center() + QPointF(0, lift)
            circle = QRectF(c.x() - 48, c.y() - 70, 96, 96)
            p.setPen(Qt.NoPen); p.setBrush(qc(T["sec_cont"]))
            p.drawEllipse(circle)
            draw_icon(p, "image", circle.adjusted(28, 28, -28, -28), qc(T["on_sec_cont"]), 2.2)
            p.setPen(qc(T["ink_soft"]))
            p.setFont(font(11.5))
            p.drawText(QRectF(rect.left(), c.y() + 38, rect.width(), 28), Qt.AlignCenter, message)
        p.restore()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        path = rounded(rect, 16)
        p.fillPath(path, qc(T["well"]))
        p.save()
        p.setClipPath(path)
        self._content(p, rect, *self.old, 1 - self.f, -6 * self.f)
        self._content(p, rect, self.pixmap, self.message, self.f, 8 * (1 - self.f))
        p.restore()
        if self.busy_a > .01:
            self._loader(p, rect, path)


class Field(QLineEdit):
    def restyle(self):
        ink = T["ink"]
        o, pr, op = T["outline"], T["primary"], T["on_primary"]
        self.setStyleSheet(
            "QLineEdit { background: transparent;"
            f" color: rgb({ink[0]},{ink[1]},{ink[2]});"
            f" border: 1px solid rgb({o[0]},{o[1]},{o[2]}); border-radius: 12px; padding: 7px 14px;"
            f" selection-background-color: rgb({pr[0]},{pr[1]},{pr[2]}); selection-color: rgb({op[0]},{op[1]},{op[2]}); }}"
            f"QLineEdit:focus {{ border: 2px solid rgb({pr[0]},{pr[1]},{pr[2]}); padding: 6px 13px; }}")


class Menu(QWidget):
    """Popup (its own window) listing (title, note, warn) rows. Fades and slides in."""

    def __init__(self, rows, current, on_pick, width=300, parent=None):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint
                         | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.rows, self.current, self.on_pick = rows, current, on_pick
        self.row_h = 50 if any(n for _, n, _ in rows) else 38
        self.margin = 12                           # room for the shadow
        self.hot = -1
        self.a = 0.0                               # open progress
        self.closing = False
        self.setMouseTracking(True)
        self.setFixedSize(width + self.margin * 2, self.row_h * len(rows) + 12 + self.margin * 2)

    def row_rect(self, i):
        m = self.margin
        return QRectF(m + 6, m + 6 + i * self.row_h, self.width() - 2 * m - 12, self.row_h - 4)

    def row_at(self, y):
        i = int((y - self.margin - 6) // self.row_h)
        return i if 0 <= i < len(self.rows) else -1

    def showEvent(self, e):
        super().showEvent(e)
        self.activateWindow()
        QApplication.instance().installEventFilter(self)
        animate(self, "a", 1.0, 200, QEasingCurve.OutCubic)

    def dismiss(self):
        if self.closing:
            return
        self.closing = True
        QApplication.instance().removeEventFilter(self)
        animate(self, "a", 0.0, 130, QEasingCurve.InCubic, done=self.close)

    def eventFilter(self, obj, e):
        # Close on a click anywhere outside the menu, or when the app loses focus.
        if e.type() == QEvent.MouseButtonPress and not self.geometry().contains(QCursor.pos()):
            self.dismiss()
        elif e.type() == QEvent.ApplicationDeactivate:
            self.dismiss()
        return False

    def closeEvent(self, e):
        QApplication.instance().removeEventFilter(self)
        super().closeEvent(e)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.dismiss()

    def mouseMoveEvent(self, e):
        hot = self.row_at(e.position().y())
        if hot != self.hot:
            self.hot = hot
            self.update()

    def mousePressEvent(self, e):
        e.accept()

    def mouseReleaseEvent(self, e):
        i = self.row_at(e.position().y())
        if i < 0:
            return
        self.current = i
        self.update()
        self.dismiss()
        self.on_pick(i)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setOpacity(self.a)
        m = self.margin
        p.translate(0, -8 * (1 - self.a))
        rect = QRectF(self.rect()).adjusted(m + .6, m + .6, -m - .6, -m - .6)
        soft_shadow(p, rect, 16, 2.2, spread=14, dy=4)
        p.fillPath(rounded(rect, 16), qc(T["menu"]))
        for i, (title, note, warn) in enumerate(self.rows):
            row = self.row_rect(i)
            if i == self.current:
                p.setPen(Qt.NoPen); p.setBrush(qc(T["sec_cont"]))
                p.drawRoundedRect(row, 10, 10)
            elif i == self.hot:
                p.setPen(Qt.NoPen)
                p.setBrush(qc(T["ink"], 18))
                p.drawRoundedRect(row, 10, 10)
            selected = i == self.current
            p.setPen(qc(T["on_sec_cont"]) if selected else qc(T["ink"]))
            p.setFont(font(10.5, QFont.DemiBold))
            if note:
                p.drawText(row.adjusted(14, 6, -8, -row.height() / 2), Qt.AlignLeft | Qt.AlignVCenter, title)
                p.setPen(qc(T["warn"]) if warn else (qc(T["on_sec_cont"]) if selected else qc(T["ink_faint"])))
                p.setFont(font(9))
                p.drawText(row.adjusted(14, row.height() / 2 - 2, -8, -4), Qt.AlignLeft | Qt.AlignVCenter, note)
            else:
                p.drawText(row.adjusted(14, 0, -8, 0), Qt.AlignLeft | Qt.AlignVCenter, title)


class Light(QWidget):
    """One macOS traffic light."""

    COLORS = {"close": (255, 95, 87), "min": (254, 188, 46), "max": (40, 200, 64)}

    def __init__(self, kind, on_click, group):
        super().__init__(group)
        self.kind, self.on_click, self.group = kind, on_click, group
        self.d = 0.0
        self.setFixedSize(20, 20)
        self.setCursor(Qt.ArrowCursor)

    def mousePressEvent(self, e):
        e.accept()
        if e.button() == Qt.LeftButton:
            animate(self, "d", 1, 80)

    def mouseReleaseEvent(self, e):
        animate(self, "d", 0, 200)
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.on_click()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QRectF(3, 3, 14, 14)
        active = self.window().isActiveWindow() or self.group.g > 0.01
        base = QColor(*self.COLORS[self.kind])
        idle = QColor(88, 88, 92) if T["dark"] else QColor(206, 206, 210)
        fill = base if active else idle
        if self.d:
            fill = fill.darker(100 + int(22 * self.d))
        p.setPen(QPen(fill.darker(125), .8))
        p.setBrush(fill)
        p.drawEllipse(c)
        g = self.group.g
        if g > 0.01:
            ink = QColor(0, 0, 0, int(150 * g))
            pen = QPen(ink, 1.3); pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            m = c.adjusted(3.8, 3.8, -3.8, -3.8)
            if self.kind == "close":
                p.drawLine(m.topLeft(), m.bottomRight()); p.drawLine(m.topRight(), m.bottomLeft())
            elif self.kind == "min":
                p.drawLine(QPointF(m.left(), m.center().y()), QPointF(m.right(), m.center().y()))
            else:
                # Two small triangles pointing outward: macOS full-size.
                p.setPen(Qt.NoPen); p.setBrush(ink)
                k = .5
                a = QPainterPath(m.topLeft()); a.lineTo(m.left() + m.width() * k, m.top()); a.lineTo(m.left(), m.top() + m.height() * k); a.closeSubpath()
                b = QPainterPath(m.bottomRight()); b.lineTo(m.right() - m.width() * k, m.bottom()); b.lineTo(m.right(), m.bottom() - m.height() * k); b.closeSubpath()
                p.drawPath(a); p.drawPath(b)


class Traffic(QWidget):
    """Close, minimise, maximise - symbols appear when hovering the group."""

    def __init__(self, on_close, on_min, on_max, parent=None):
        super().__init__(parent)
        self.g = 0.0
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self.close_b = Light("close", on_close, self)
        self.min_b = Light("min", on_min, self)
        self.max_b = Light("max", on_max, self)
        for b in (self.close_b, self.min_b, self.max_b):
            row.addWidget(b)
        self.setFixedSize(60, 20)

    def update(self):
        super().update()
        for b in (self.close_b, self.min_b, self.max_b):
            b.update()

    def enterEvent(self, _):
        animate(self, "g", 1, 120)

    def leaveEvent(self, _):
        animate(self, "g", 0, 180)

    def mousePressEvent(self, e):
        e.accept()


class TitleBar(QWidget):
    """Drag to move, double-click to maximise."""

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.window().windowHandle().startSystemMove()

    def mouseDoubleClickEvent(self, _):
        self.window().toggle_max()


class Toast(QWidget):
    """Auto-dismissing notice: slides up, stays, fades away 2.3 s after it appears."""

    LIFETIME = 2300
    FADE = 220

    def __init__(self, parent):
        super().__init__(parent)
        self.a = 0.0
        self.kind, self.text = "info", ""
        self.timer = QTimer(self, singleShot=True)
        self.timer.timeout.connect(self._fade_out)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.hide()

    def show_message(self, text, kind="info"):
        self.kind, self.text = kind, text
        fm = QFontMetrics(font(10.5))
        width = min(max(fm.horizontalAdvance(text) + 76, 260), 560)
        self.resize(width, 50)
        self.place()
        self.show()
        self.raise_()
        animate(self, "a", 1.0, self.FADE, QEasingCurve.OutCubic)
        self.timer.start(self.LIFETIME - self.FADE)

    def place(self):
        # Sits in the empty stretch of the title row, between the title and the theme switch,
        # so it never covers a control.
        app = self.parentWidget()
        left = app.title.mapTo(app, QPoint(app.title.width(), 0)).x() + 24
        right = getattr(app, "settings_button", app.theme_switch).mapTo(app, QPoint(0, 0)).x() - 24
        width = min(self.width(), max(right - left, 240))
        if width != self.width():
            self.resize(width, self.height())
        y = app.theme_switch.mapTo(app, QPoint(0, app.theme_switch.height() // 2)).y() - self.height() // 2 + 2
        self.move(left + (right - left - width) // 2, y)

    def _fade_out(self):
        animate(self, "a", 0.0, self.FADE, QEasingCurve.InCubic,
                done=lambda: self.hide() if self.a < 0.01 else None)

    def mousePressEvent(self, e):
        e.accept()
        self.timer.stop()
        self._fade_out()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setOpacity(self.a)
        p.translate(0, -10 * (1 - self.a))
        rect = QRectF(self.rect()).adjusted(6, 4, -6, -8)
        soft_shadow(p, rect, 12, 2.0, spread=10, dy=3)
        p.fillPath(rounded(rect, 12), qc(T["inverse"]))
        ink = qc(T["on_inverse"])
        if self.kind not in ("success", "error", "warning"):
            icon_ink = qc(T["inverse_primary"])
        else:
            # Accent colours are tuned for the opposite surface; the inverse surface flips it.
            other = THEMES["light" if T["dark"] else "dark"]
            icon_ink = qc({"success": other["success"], "error": other["error"],
                           "warning": other["warn"]}[self.kind])
        icon = {"success": "success", "error": "error", "warning": "warning"}.get(self.kind, "info")
        draw_icon(p, icon, QRectF(rect.left() + 16, rect.center().y() - 9, 18, 18), icon_ink, 1.8)
        p.setPen(ink)
        p.setFont(font(10.5))
        p.drawText(rect.adjusted(46, 0, -16, 0), Qt.AlignVCenter | Qt.AlignLeft,
                   QFontMetrics(font(10.5)).elidedText(self.text, Qt.ElideRight, int(rect.width() - 62)))


class Confirm(QWidget):
    """Themed confirmation: dimmed window, a card, Cancel and a confirm button."""

    def __init__(self, parent, title, body, confirm_text, on_confirm, danger=False):
        super().__init__(parent)
        self.title, self.body, self.on_confirm = title, body, on_confirm
        self.a = 0.0
        self.setGeometry(parent.rect())
        self.card = QRectF(0, 0, 440, 200)
        self.cancel = Button("Cancel", kind="outlined", w=104, h=40, on_click=self.reject, parent=self)
        self.ok = Button(confirm_text, kind="primary", w=190, h=40, on_click=self.accept, parent=self)
        self.danger = danger
        self.setFocusPolicy(Qt.StrongFocus)
        self._layout()
        for b in (self.cancel, self.ok):
            b.hide()
        self.show()
        self.raise_()
        self.setFocus()
        animate(self, "a", 1.0, 220, QEasingCurve.OutCubic)
        # Buttons join once the card has mostly faded in.
        QTimer.singleShot(120, lambda: [b.show() for b in (self.cancel, self.ok)])

    def _layout(self):
        self.card.moveCenter(QPointF(self.width() / 2, self.height() / 2))
        c = self.card
        self.ok.move(int(c.right() - 24 - self.ok.width() + self.ok.glow), int(c.bottom() - 22 - self.ok.height() + self.ok.glow))
        self.cancel.move(int(self.ok.x() + self.ok.glow - 12 - self.cancel.width()), int(c.bottom() - 22 - self.cancel.height()))

    def _close(self, then=None):
        for b in (self.cancel, self.ok):
            b.hide()

        def finish():
            self.hide()
            self.deleteLater()
            if then:
                then()
        animate(self, "a", 0.0, 150, QEasingCurve.InCubic, done=finish)

    def accept(self):
        self._close(self.on_confirm)

    def reject(self):
        self._close()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.reject()
        elif e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.accept()

    def mousePressEvent(self, e):
        e.accept()
        if not self.card.contains(e.position()):
            self.reject()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(0, 0, 0, int(110 * self.a)))
        p.setOpacity(self.a)
        s = 0.96 + 0.04 * self.a
        c = self.card
        p.translate(c.center())
        p.scale(s, s)
        p.translate(-c.center())
        soft_shadow(p, c, 24, 2.4, spread=16, dy=6)
        p.fillPath(rounded(c, 24), qc(T["menu"]))
        accent = qc(T["error"]) if self.danger else qc(T["primary"])
        draw_icon(p, "warning" if self.danger else "info", QRectF(c.left() + 24, c.top() + 26, 22, 22), accent, 1.9)
        p.setPen(qc(T["ink"]))
        p.setFont(font(13, QFont.DemiBold))
        p.drawText(QRectF(c.left() + 58, c.top() + 20, c.width() - 80, 34), Qt.AlignVCenter, self.title)
        p.setPen(qc(T["ink_soft"]))
        p.setFont(font(10))
        p.drawText(QRectF(c.left() + 24, c.top() + 64, c.width() - 48, 70), Qt.TextWordWrap, self.body)


class Crossfade(QWidget):
    """Snapshot of the old theme, faded out over the new one."""

    def __init__(self, parent, pixmap):
        super().__init__(parent)
        self.pixmap, self.a = pixmap, 1.0
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setGeometry(parent.rect())
        self.show()
        self.raise_()
        animate(self, "a", 0.0, 380, QEasingCurve.InOutCubic, done=self.deleteLater)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setOpacity(self.a)
        p.drawPixmap(self.rect(), self.pixmap)


# --------------------------------------------------------------- models --

class ModelJobs(QObject):
    """Model downloads, update checks and deletes, off the UI thread. Owned by the app,
    so a download carries on when the Models panel is closed."""

    changed = Signal(str)            # a model's state changed ("" = all)
    notice = Signal(str, str)        # toast text, kind
    _update = Signal(str, dict)      # worker -> UI thread

    def __init__(self, parent):
        super().__init__(parent)
        self.state = {}              # name -> {job, status, kind, progress, update}
        self.cancels = {}
        self._update.connect(self._apply)

    def _apply(self, name, values):
        self.state.setdefault(name, {}).update(values)
        self.changed.emit(name)

    def job(self, name):
        return self.state.get(name, {}).get("job")

    def label(self, name):
        return next((label for n, label, _ in core.MODELS if n == name), name)

    def download(self, name):
        if self.job(name):
            return
        cancel = threading.Event()
        self.cancels[name] = cancel
        self._apply(name, dict(job="download", status="Starting download", kind="", progress=0.0))
        label = self.label(name)

        def report(got, total, rate, last=[0.0]):
            now = time.time()
            if now - last[0] < .12 and got < total:
                return
            last[0] = now
            self._update.emit(name, dict(progress=got / max(total, 1),
                                         status=f"{got / 1048576:.0f} of {total / 1048576:.0f} MB  ·  {rate:.1f} MB/s"))

        def work():
            try:
                model_store.download(name, report, cancel)
                self._update.emit(name, dict(job=None, progress=None, update=False, kind="ok",
                                             status=f"Downloaded  ·  {model_store.human(model_store.local_size(name) or 0)}"))
                self.notice.emit(f"Downloaded {label}", "success")
            except model_store.Cancelled:
                self._update.emit(name, dict(job=None, progress=None, kind="",
                                             status="Paused  ·  resumes where it stopped"))
                self.notice.emit(f"{label} download paused", "info")
            except Exception as exc:
                self._update.emit(name, dict(job=None, progress=None, kind="err", status="Download failed"))
                self.notice.emit(f"Couldn't download {label}: {exc}", "error")

        threading.Thread(target=work, daemon=True).start()

    def cancel(self, name):
        if name in self.cancels:
            self.cancels[name].set()

    def check_all(self):
        names = [n for n, _, _ in core.MODELS if model_store.is_downloaded(n) and not self.job(n)]
        if not names:
            self.notice.emit("No models downloaded yet", "info")
            return
        for n in names:
            self._apply(n, dict(job="check", status="Checking", kind=""))

        def work():
            updates, offline = 0, False
            for n in names:
                try:
                    result = model_store.check(n)
                except Exception:
                    offline = True
                    self._update.emit(n, dict(job=None, kind="err", status="Couldn't reach the server"))
                    continue
                if result == "update":
                    updates += 1
                    self._update.emit(n, dict(job=None, kind="warn", update=True, status="Update available"))
                elif result == "damaged":
                    updates += 1
                    self._update.emit(n, dict(job=None, kind="err", update=True, status="File damaged  ·  download it again"))
                else:
                    self._update.emit(n, dict(job=None, kind="ok", update=False, status="Up to date"))
            if offline and not updates:
                self.notice.emit("Couldn't check for updates: no connection", "error")
            elif updates:
                self.notice.emit(f"{updates} model update{'s' if updates != 1 else ''} available", "warning")
            else:
                self.notice.emit("All models are up to date", "success")

        threading.Thread(target=work, daemon=True).start()

    def delete(self, name):
        size = model_store.local_size(name) or 0
        model_store.delete(name)
        self.state.pop(name, None)
        self.changed.emit(name)
        self.notice.emit(f"Deleted {self.label(name)}  ·  {model_store.human(size)} freed", "success")


class ModelsDialog(QWidget):
    """Models panel: what is downloaded, how big it is, and download / update / delete."""

    ROW = 64
    HEAD = 92
    FOOT = 80

    def __init__(self, app):
        super().__init__(app)
        self.app, self.jobs = app, app.jobs
        self.a = 0.0
        self.setGeometry(app.rect())
        self.card = QRectF(0, 0, 660, self.HEAD + self.ROW * len(core.MODELS) + self.FOOT)
        self.rows = []
        for name, label, note in core.MODELS:
            act = Button("Download", kind="tonal", w=122, h=36, parent=self,
                         on_click=lambda n=name: self._act(n))
            rm = Button("", "trash", kind="outlined", w=40, h=36, parent=self,
                        on_click=lambda n=name: self._delete(n))
            self.rows.append((name, label, note, act, rm))
        self.check_b = Button("Check for updates", kind="outlined", w=180, h=40, parent=self, on_click=self._check)
        self.folder_b = Button("Open folder", "folder", kind="outlined", w=144, h=40, parent=self,
                               on_click=self._open_folder)
        self.done_b = Button("Done", kind="primary", w=112, h=40, parent=self, on_click=self.dismiss)
        self.buttons = [b for row in self.rows for b in row[3:]] + [self.check_b, self.folder_b, self.done_b]
        self.revealed = False                # buttons join once the card has mostly faded in
        for b in self.buttons:
            b.hide()
        self.setFocusPolicy(Qt.StrongFocus)
        self.jobs.changed.connect(self._sync)
        self._layout()
        self._sync("")
        self.show()
        self.raise_()
        self.setFocus()
        animate(self, "a", 1.0, 220, QEasingCurve.OutCubic)
        QTimer.singleShot(120, self._reveal)

    def _reveal(self):
        self.revealed = True
        for b in (self.check_b, self.folder_b, self.done_b):
            b.show()
        self._sync("")

    def restyle(self):
        self.update()

    def _row_rect(self, i):
        c = self.card
        return QRectF(c.left() + 20, c.top() + self.HEAD + i * self.ROW, c.width() - 40, self.ROW)

    def _layout(self):
        self.card.moveCenter(QPointF(self.width() / 2, self.height() / 2))
        c = self.card
        for i, (_n, _l, _note, act, rm) in enumerate(self.rows):
            row = self._row_rect(i)
            cy = row.center().y()
            rm.move(int(row.right() - 8 - rm.width() + rm.glow), int(cy - rm.height() / 2))
            act.move(int(rm.x() + rm.glow - 10 - act.width() + act.glow), int(cy - act.height() / 2))
        y = int(c.bottom() - 22 - self.done_b.height() + self.done_b.glow)
        self.done_b.move(int(c.right() - 28 - self.done_b.width() + self.done_b.glow), y)
        yb = int(c.bottom() - 22 - self.check_b.height() + self.check_b.glow)
        self.check_b.move(int(c.left() + 28 - self.check_b.glow), yb)
        self.folder_b.move(self.check_b.x() + self.check_b.width() - 2 * self.check_b.glow + 10, yb)

    def resizeEvent(self, _):
        self._layout()

    def _sync(self, _name):
        locked = self.app.busy
        for name, _label, _note, act, rm in self.rows:
            st = self.jobs.state.get(name, {})
            job, have = st.get("job"), model_store.is_downloaded(name)
            if job == "download":
                act.set_text("Cancel"); act.kind = "outlined"
            elif st.get("update"):
                act.set_text("Update"); act.kind = "tonal"
            elif not have:
                act.set_text("Download"); act.kind = "tonal"
            show_act = job == "download" or st.get("update") or (not have and job is None)
            show_rm = have and job is None
            act.set_enabled(not locked and job != "check")
            rm.set_enabled(not locked)
            if self.revealed:
                act.setVisible(bool(show_act))
                rm.setVisible(bool(show_rm))
            act.update()
        self.check_b.set_enabled(not locked)
        self.update()

    def _act(self, name):
        st = self.jobs.state.get(name, {})
        if st.get("job") == "download":
            self.jobs.cancel(name)
        else:
            self.jobs.download(name)

    def _delete(self, name):
        label = self.jobs.label(name)
        size = model_store.human(model_store.local_size(name) or 0)
        self.app.confirm(f"Delete {label}?", f"This frees {size} on this PC. You can download it again at any time.",
                         "Delete", lambda: self.jobs.delete(name), danger=True)

    def _check(self):
        self.jobs.check_all()

    def _open_folder(self):
        core.MODEL_CACHE.mkdir(parents=True, exist_ok=True)
        os.startfile(core.MODEL_CACHE)

    def dismiss(self):
        try:
            self.jobs.changed.disconnect(self._sync)
        except (RuntimeError, TypeError):
            pass
        self.revealed = False
        for b in self.buttons:
            b.hide()

        def finish():
            self.hide()
            self.deleteLater()
            if self.app._models is self:
                self.app._models = None
        animate(self, "a", 0.0, 150, QEasingCurve.InCubic, done=finish)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Escape, Qt.Key_Return, Qt.Key_Enter):
            self.dismiss()

    def mousePressEvent(self, e):
        e.accept()
        if not self.card.contains(e.position()):
            self.dismiss()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(0, 0, 0, int(110 * self.a)))
        p.setOpacity(self.a)
        s = 0.96 + 0.04 * self.a
        c = self.card
        p.translate(c.center())
        p.scale(s, s)
        p.translate(-c.center())
        soft_shadow(p, c, 24, 2.4, spread=16, dy=6)
        p.fillPath(rounded(c, 24), qc(T["menu"]))

        have = [n for n, _, _ in core.MODELS if model_store.is_downloaded(n)]
        p.setPen(qc(T["ink"]))
        p.setFont(font(14, QFont.DemiBold))
        p.drawText(QRectF(c.left() + 28, c.top() + 22, c.width() - 56, 30), Qt.AlignVCenter, "Models")
        p.setPen(qc(T["ink_soft"]))
        p.setFont(font(9.5))
        summary = (f"{len(have)} of {len(core.MODELS)} downloaded  ·  "
                   f"{model_store.human(model_store.disk_usage())} on this PC")
        if self.app.busy:
            summary += "  ·  finish the current run to make changes"
        p.drawText(QRectF(c.left() + 28, c.top() + 52, c.width() - 56, 22), Qt.AlignVCenter, summary)

        colours = {"ok": T["success"], "warn": T["warn"], "err": T["error"]}
        for i, (name, label, note, act, _rm) in enumerate(self.rows):
            row = self._row_rect(i)
            p.setPen(QPen(qc(T["outline_var"], 200), 1))
            p.drawLine(QPointF(row.left() + 8, row.top()), QPointF(row.right() - 8, row.top()))
            p.setPen(qc(T["ink"]))
            p.setFont(font(10.5, QFont.DemiBold))
            p.drawText(QRectF(row.left() + 8, row.top() + 12, 220, 22), Qt.AlignVCenter, label)
            p.setPen(qc(T["ink_faint"]))
            p.setFont(font(9))
            p.drawText(QRectF(row.left() + 8, row.top() + 34, 220, 18), Qt.AlignVCenter, note)

            st = self.jobs.state.get(name, {})
            size = model_store.local_size(name)
            status = st.get("status") or (f"Downloaded  ·  {model_store.human(size)}" if size is not None
                                          else "Not downloaded")
            kind = st.get("kind", "")
            ink = qc(colours[kind]) if kind in colours else qc(T["ink_soft"])
            left = row.left() + 230
            right = act.x() + act.glow - 14 if act.isVisible() else row.right() - 60
            text_rect = QRectF(left, row.top(), max(right - left, 60), row.height())
            prog = st.get("progress")
            if prog is not None:
                text_rect.adjust(0, -8, 0, -8)
                bar = QRectF(left, row.center().y() + 10, text_rect.width(), 4)
                p.setPen(Qt.NoPen)
                p.setBrush(qc(T["track"]))
                p.drawRoundedRect(bar, 2, 2)
                p.setBrush(qc(T["primary"]))
                p.drawRoundedRect(QRectF(bar.left(), bar.top(), max(4, bar.width() * prog), 4), 2, 2)
            p.setPen(ink)
            p.setFont(font(9.5))
            p.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft,
                       QFontMetrics(font(9.5)).elidedText(status, Qt.ElideRight, int(text_rect.width())))


# ---------------------------------------------------------------- brand --
# Logo layers are cut from assets/logo-source.png by make_logo_assets.py.

ASSETS = core.HERE / "assets"
MARK_R = 130.0                    # outer radius of the C in mark-*.png (square, centred on the ring)
WORD_C = (109.7, 169.5, 98.5)     # centre x, y and radius of the wordmark's c, in wordmark-*.png px
WORD_C_RIGHT = .199               # the c (with its flap) ends here, as a fraction of the width


class Brand:
    """Loads the logo layers once; the plain parts are recoloured to the theme ink."""

    _images, _tinted = {}, {}

    @classmethod
    def image(cls, name):
        if name not in cls._images:
            cls._images[name] = QImage(str(ASSETS / name))
        return cls._images[name]

    @classmethod
    def tinted(cls, name, rgb):
        key = (name, tuple(rgb[:3]))
        if key not in cls._tinted:
            src = cls.image(name).convertToFormat(QImage.Format_ARGB32_Premultiplied)
            p = QPainter(src)
            p.setCompositionMode(QPainter.CompositionMode_SourceIn)
            p.fillRect(src.rect(), QColor(*rgb[:3]))
            p.end()
            cls._tinted[key] = src
        return cls._tinted[key]

    @classmethod
    def ok(cls):
        return not cls.image("wordmark-mask.png").isNull() and not cls.image("mark-body.png").isNull()

    @classmethod
    def aspect(cls):
        img = cls.image("wordmark-mask.png")
        return img.width() / max(img.height(), 1) if not img.isNull() else 4.06


def draw_mark(p, centre, radius, ink, sweep=1.0, flap_turn=0.0, flap_a=1.0):
    """The C mark, outer radius `radius`, centred on `centre`. `sweep` (0..1) draws the
    ring on counter-clockwise from its top; the flap is turned `flap_turn` degrees
    (counter-clockwise) about the centre."""
    size = 320 * radius / MARK_R
    target = QRectF(centre.x() - size / 2, centre.y() - size / 2, size, size)
    p.save()
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    if sweep > 0:
        p.save()
        if sweep < 1:
            pie = QPainterPath(centre)
            pie.arcTo(target, 75, 360 * sweep)
            pie.closeSubpath()
            p.setClipPath(pie)
        p.drawImage(target, Brand.tinted("mark-body.png", ink))
        p.restore()
    if flap_a > 0:
        p.setOpacity(p.opacity() * flap_a)
        p.translate(centre)
        p.rotate(-flap_turn)
        p.translate(-centre)
        p.drawImage(target, Brand.image("mark-flap.png"))
    p.restore()


def draw_wordmark(p, rect, ink, x0=0.0, x1=1.0):
    """The "cutout" wordmark in `rect`; only the part between fractions x0..x1 of its width."""
    p.save()
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    if x0 > 0 or x1 < 1:
        left = rect.left() + rect.width() * x0
        p.setClipRect(QRectF(left, rect.top() - rect.height(), rect.width() * (x1 - x0), rect.height() * 3))
    p.drawImage(rect, Brand.tinted("wordmark-mask.png", ink))
    p.drawImage(rect, Brand.image("wordmark-flap.png"))
    p.restore()


def word_c(rect):
    """Centre and radius of the wordmark's c when the wordmark is drawn in `rect`."""
    k = rect.height() / Brand.image("wordmark-mask.png").height()
    return QPointF(rect.left() + WORD_C[0] * k, rect.top() + WORD_C[1] * k), WORD_C[2] * k


class Wordmark(QWidget):
    """The logo in the title row; the letters follow the theme ink."""

    H = 30

    def __init__(self, parent=None):
        super().__init__(parent)
        self.a = 1.0
        self.setFixedSize(int(math.ceil(self.H * Brand.aspect())) + 2, self.H + 6)

    def restyle(self):
        self.update()

    def word_rect(self):
        return QRectF(1, 3, self.H * Brand.aspect(), self.H)

    def paintEvent(self, _):
        if self.a <= .005:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setOpacity(self.a)
        if Brand.ok():
            draw_wordmark(p, self.word_rect(), T["ink"])
        else:
            p.setPen(qc(T["ink"]))
            p.setFont(font(22, QFont.Bold))
            p.drawText(self.rect(), Qt.AlignVCenter | Qt.AlignLeft, APP_NAME)


def phase(t, a, b, curve=QEasingCurve.OutCubic):
    """Eased progress (0..1) of time `t` through the span a..b."""
    x = min(max((t - a) / (b - a), 0.0), 1.0)
    return QEasingCurve(curve).valueForProgress(x)


def lerp(a, b, t):
    return a + (b - a) * t


class Intro(QWidget):
    """Opening sequence. The C draws itself on and its flap swings into place; it
    shrinks into the c of the wordmark as the other letters wipe in; then the
    wordmark flies up into the title while the app is cut out of the backdrop by a
    growing circle. Click or press a key to skip."""

    TOTAL = 2250

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.t = 0.0
        self.ui = None                       # snapshot of the app, revealed at the end
        self.started = False
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setGeometry(app.rect())
        self.anim = QVariantAnimation(self, startValue=0.0, endValue=float(self.TOTAL), duration=self.TOTAL)
        self.anim.valueChanged.connect(self._tick)
        self.anim.finished.connect(self.finish)
        app.title.a = 0.0
        self.show()
        self.raise_()

    def start(self):
        if self.started:
            return
        self.started = True
        self.hide()
        self.ui = self.app.grab()
        self.show()
        self.raise_()
        self.setFocus()
        self.anim.start()

    def _tick(self, v):
        self.t = float(v)
        self.update()

    def finish(self):
        if not self.isVisible():
            return
        self.anim.stop()
        self.app.title.a = 1.0
        self.app.title.update()
        self.hide()
        self.deleteLater()
        self.app._intro = None
        QTimer.singleShot(150, self.app._maybe_welcome)

    def mousePressEvent(self, e):
        e.accept()
        self.finish()

    def keyPressEvent(self, _):
        self.finish()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        t, W, H = self.t, self.width(), self.height()
        mid = QPointF(W / 2, H / 2)

        # The app, cut out of the backdrop by a growing circle and settling from a slight zoom.
        hole = phase(t, 1600, 2250, QEasingCurve.InOutCubic)
        if hole > 0 and self.ui is not None:
            p.save()
            zoom = 1.04 - .04 * hole
            p.translate(mid)
            p.scale(zoom, zoom)
            p.translate(-mid)
            p.drawPixmap(self.rect(), self.ui)
            p.restore()
        backdrop = QPainterPath()
        backdrop.addRect(QRectF(self.rect()))
        if hole > 0:
            radius = hole * math.hypot(W, H) / 2 + 1
            cut = QPainterPath()
            cut.addEllipse(mid, radius, radius)
            backdrop = backdrop.subtracted(cut)
        p.fillPath(backdrop, qc(T["window"]))
        if not Brand.ok():
            return

        # Where the wordmark sits: centred first, then in the title.
        big_h = min(104.0, H * .13)
        big = QRectF(0, 0, big_h * Brand.aspect(), big_h)
        big.moveCenter(mid)
        title = self.app.title
        home = title.word_rect().translated(QPointF(title.mapTo(self.app, QPoint(0, 0))))
        fly = phase(t, 1550, 2100, QEasingCurve.InOutCubic)
        word = QRectF(lerp(big.left(), home.left(), fly), lerp(big.top(), home.top(), fly),
                      lerp(big.width(), home.width(), fly), lerp(big.height(), home.height(), fly))
        ink = T["ink"]

        # The mark: draws on, gets its flap, then settles into the c of the wordmark.
        c_centre, c_radius = word_c(word)
        settle = phase(t, 850, 1250, QEasingCurve.InOutCubic)
        grow = phase(t, 100, 650)
        centre = QPointF(lerp(mid.x(), c_centre.x(), settle), lerp(mid.y(), c_centre.y(), settle))
        radius = lerp(78.0 * (.86 + .14 * grow), c_radius, settle)
        swap = phase(t, 1250, 1400, QEasingCurve.Linear)
        if swap < 1:
            # The mark stays solid underneath while the wordmark's own c fades in over it.
            draw_mark(p, centre, radius, ink,
                      sweep=phase(t, 100, 650, QEasingCurve.InOutCubic),
                      flap_turn=120 * (1 - phase(t, 450, 950, QEasingCurve.OutBack)),
                      flap_a=phase(t, 450, 650))
        if swap > 0:
            p.setOpacity(swap)
            draw_wordmark(p, word, ink, 0.0, WORD_C_RIGHT)
        letters = phase(t, 1150, 1500)
        if letters > 0:
            p.setOpacity(1)
            draw_wordmark(p, word, ink, WORD_C_RIGHT, WORD_C_RIGHT + (1 - WORD_C_RIGHT) * letters)


# --------------------------------------------------------------- export --

# (key, menu title, note, width / height or None)
RATIOS = [
    ("original", "Original", "Keep the photo's own framing", None),
    ("1:1", "1:1  Square", "Product grids, profile photos", 1.0),
    ("4:5", "4:5  Portrait", "Instagram posts", 4 / 5),
    ("3:4", "3:4  Portrait", "Prints, product pages", 3 / 4),
    ("2:3", "2:3  Portrait", "Pinterest, posters", 2 / 3),
    ("9:16", "9:16  Tall", "Stories, reels, phone wallpapers", 9 / 16),
    ("16:9", "16:9  Wide", "Slides, video thumbnails", 16 / 9),
    ("3:2", "3:2  Landscape", "Photo prints", 3 / 2),
    ("4:3", "4:3  Landscape", "Presentations", 4 / 3),
]
RATIO = {key: value for key, _t, _n, value in RATIOS}
# Crop tool ratios: free first, then the photo's own, then the export ratios.
CROP_RATIOS = [("free", "Free", "Any shape"), ("original", "Original", "The photo's own shape")] + \
    [(key, title, note) for key, title, note, value in RATIOS if value]

PRESET_COLOURS = [(255, 255, 255), (244, 241, 234), (229, 229, 229), (158, 158, 158),
                  (30, 30, 30), (0, 0, 0), (139, 92, 246), (59, 165, 255)]


def hex_of(rgb):
    return "#{:02X}{:02X}{:02X}".format(*rgb[:3])


def parse_hex(text):
    t = text.strip().lstrip("#")
    if len(t) == 3:
        t = "".join(ch * 2 for ch in t)
    if len(t) != 6:
        return None
    try:
        return tuple(int(t[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def export_cutout(cut, crop=None, ratio=None, size=None, background=None):
    """Frame a cutout for saving. A manual crop wins; otherwise the subject is centred in
    the chosen aspect ratio. `size` is the long edge in px (None keeps full resolution);
    `background` is None for transparency or an (r, g, b) colour."""
    if crop:
        cut = crop_to(cut, crop)
        if size:
            cut = scale_long_edge(cut, size)
    elif ratio:
        cut = fit_aspect(cut, ratio, size)
    elif size:
        cut = scale_long_edge(cut, size)
    if background:
        cut = flatten(cut, background)
    return cut


class SwatchButton(Button):
    """Field button showing the background colour (a checkerboard for transparent)."""

    def __init__(self, **kw):
        super().__init__("Transparent", kind="field", trail="chev-down", **kw)
        self.colour = None

    def set_colour(self, rgb):
        self.colour = rgb
        self.set_text("Transparent" if rgb is None else hex_of(rgb))

    def _paint_label(self, p, rect, ink):
        sw = QRectF(rect.left() + 14, rect.center().y() - 10, 20, 20)
        paint_swatch(p, sw, self.colour)
        p.setFont(font(10, QFont.DemiBold))
        p.setPen(ink)
        p.drawText(QRectF(sw.right() + 10, rect.top(), rect.width() - 80, rect.height()),
                   Qt.AlignVCenter | Qt.AlignLeft, self.text)
        draw_icon(p, self.trail, QRectF(rect.right() - 28, rect.center().y() - 7, 14, 14), ink, 1.6)


def paint_swatch(p, rect, colour, radius=None):
    """A colour chip; transparent shows as a small checkerboard."""
    p.save()
    path = QPainterPath()
    if radius is None:
        path.addEllipse(rect)
    else:
        path.addRoundedRect(rect, radius, radius)
    p.setClipPath(path)
    if colour is None:
        p.fillRect(rect, QColor(255, 255, 255))
        cell = max(rect.width() / 4, 4)
        y = rect.top()
        row = 0
        while y < rect.bottom():
            x = rect.left() + (cell if row % 2 else 0)
            while x < rect.right():
                p.fillRect(QRectF(x, y, cell, cell), QColor(204, 204, 204))
                x += cell * 2
            y += cell
            row += 1
    else:
        p.fillRect(rect, QColor(*colour[:3]))
    p.restore()
    p.setPen(QPen(qc(T["outline"], 170), 1))
    p.setBrush(Qt.NoBrush)
    p.drawPath(path)


class NumField(Field):
    """A narrow Field (RGB values): less side padding so three digits fit."""

    def restyle(self):
        super().restyle()
        self.setStyleSheet(self.styleSheet().replace("padding: 7px 14px", "padding: 7px 2px")
                           .replace("padding: 6px 13px", "padding: 6px 1px"))


class ColourField(QWidget):
    """Saturation (x) / value (y) square for the current hue."""

    def __init__(self, dialog):
        super().__init__(dialog)
        self.dialog = dialog
        self.setCursor(Qt.CrossCursor)

    def _set(self, pos):
        self.dialog.set_hsv(self.dialog.h, min(max(pos.x() / self.width(), 0), 1),
                            1 - min(max(pos.y() / self.height(), 0), 1))

    def mousePressEvent(self, e):
        e.accept()
        self._set(e.position())

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.LeftButton:
            self._set(e.position())

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect())
        path = rounded(rect, 12)
        if self.dialog.transparent:
            p.setOpacity(.35)                # resting until a colour is chosen
        p.setClipPath(path)
        p.fillRect(rect, QColor.fromHsvF(self.dialog.h, 1, 1))
        white = QLinearGradient(rect.topLeft(), rect.topRight())
        white.setColorAt(0, QColor(255, 255, 255, 255))
        white.setColorAt(1, QColor(255, 255, 255, 0))
        p.fillRect(rect, white)
        black = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        black.setColorAt(0, QColor(0, 0, 0, 0))
        black.setColorAt(1, QColor(0, 0, 0, 255))
        p.fillRect(rect, black)
        p.setClipping(False)
        c = QPointF(self.dialog.s * rect.width(), (1 - self.dialog.v) * rect.height())
        p.setPen(QPen(QColor(0, 0, 0, 120), 3))
        p.drawEllipse(c, 8, 8)
        p.setPen(QPen(QColor(255, 255, 255), 2))
        p.drawEllipse(c, 8, 8)


class HueBar(QWidget):
    def __init__(self, dialog):
        super().__init__(dialog)
        self.dialog = dialog
        self.setCursor(Qt.PointingHandCursor)

    def _set(self, pos):
        self.dialog.set_hsv(min(max(pos.x() / self.width(), 0), .9999), self.dialog.s, self.dialog.v)

    def mousePressEvent(self, e):
        e.accept()
        self._set(e.position())

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.LeftButton:
            self._set(e.position())

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0, 3, 0, -3)
        if self.dialog.transparent:
            p.setOpacity(.35)
        g = QLinearGradient(rect.topLeft(), rect.topRight())
        for i in range(7):
            g.setColorAt(i / 6, QColor.fromHsvF(min(i / 6, .9999), 1, 1))
        p.fillPath(rounded(rect, rect.height() / 2), g)
        x = self.dialog.h * self.width()
        knob = QRectF(min(max(x - 7, 0), self.width() - 14), 0, 14, self.height())
        p.setPen(QPen(QColor(0, 0, 0, 110), 3))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(knob, 6, 6)
        p.setPen(QPen(QColor(255, 255, 255), 2))
        p.drawRoundedRect(knob, 6, 6)


class ScreenPicker(QWidget):
    """Eyedropper: a frozen snapshot of the screen with a magnifying loupe; click picks
    the pixel under the cursor, Esc or right-click cancels."""

    ZOOM, CELLS = 12, 11

    def __init__(self, on_pick):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.on_pick = on_pick
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        self.shot = screen.grabWindow(0)
        self.image = self.shot.toImage()
        self.dpr = self.shot.devicePixelRatio()
        self.setGeometry(screen.geometry())
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)
        self.pos_ = self.mapFromGlobal(QCursor.pos())
        self.showFullScreen()
        self.activateWindow()
        self.setFocus()

    def _colour_at(self, pos):
        x = min(max(int(pos.x() * self.dpr), 0), self.image.width() - 1)
        y = min(max(int(pos.y() * self.dpr), 0), self.image.height() - 1)
        c = self.image.pixelColor(x, y)
        return c.red(), c.green(), c.blue()

    def mouseMoveEvent(self, e):
        self.pos_ = e.position().toPoint()
        self.update()

    def mousePressEvent(self, e):
        e.accept()
        if e.button() == Qt.LeftButton:
            colour = self._colour_at(e.position())
            self.close()
            self.on_pick(colour)
        else:
            self.close()
            self.on_pick(None)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close()
            self.on_pick(None)

    def paintEvent(self, _):
        p = QPainter(self)
        p.drawPixmap(self.rect(), self.shot)
        p.setRenderHint(QPainter.Antialiasing)
        size = self.ZOOM * self.CELLS
        pos = self.pos_
        x = pos.x() + 28 if pos.x() + 28 + size < self.width() else pos.x() - 28 - size
        y = pos.y() + 28 if pos.y() + 28 + size + 34 < self.height() else pos.y() - 28 - size - 34
        loupe = QRectF(x, y, size, size)
        clip = QPainterPath()
        clip.addEllipse(loupe)
        p.save()
        p.setClipPath(clip)
        half = self.CELLS // 2
        cx, cy = int(pos.x() * self.dpr), int(pos.y() * self.dpr)
        src = self.image.copy(cx - half, cy - half, self.CELLS, self.CELLS)
        p.setRenderHint(QPainter.SmoothPixmapTransform, False)
        p.drawImage(loupe, src)
        p.setPen(QPen(QColor(0, 0, 0, 40), 1))
        for i in range(1, self.CELLS):
            p.drawLine(QPointF(x + i * self.ZOOM, y), QPointF(x + i * self.ZOOM, y + size))
            p.drawLine(QPointF(x, y + i * self.ZOOM), QPointF(x + size, y + i * self.ZOOM))
        centre = QRectF(x + half * self.ZOOM, y + half * self.ZOOM, self.ZOOM, self.ZOOM)
        p.setPen(QPen(QColor(255, 255, 255), 2))
        p.drawRect(centre)
        p.setPen(QPen(QColor(0, 0, 0), 1))
        p.drawRect(centre.adjusted(-1.5, -1.5, 1.5, 1.5))
        p.restore()
        p.setPen(QPen(QColor(255, 255, 255), 3))
        p.drawEllipse(loupe)
        colour = self._colour_at(pos)
        label = QRectF(x + size / 2 - 58, y + size + 8, 116, 28)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(20, 20, 22, 230))
        p.drawRoundedRect(label, 14, 14)
        paint_swatch(p, QRectF(label.left() + 8, label.top() + 6, 16, 16), colour)
        p.setPen(QColor(255, 255, 255))
        p.setFont(font(9.5, QFont.DemiBold))
        p.drawText(label.adjusted(30, 0, -8, 0), Qt.AlignVCenter, hex_of(colour))


class ColourDialog(QWidget):
    """Background colour: transparent, or any colour from a saturation/value field, hue
    bar, hex or RGB values, presets, recent colours or the screen eyedropper."""

    SW = 26            # swatch size

    def __init__(self, app, colour, on_apply):
        super().__init__(app)
        self.app, self.on_apply = app, on_apply
        self.a = 0.0
        self.setGeometry(app.rect())
        self.card = QRectF(0, 0, 560, 520)
        self.original = colour
        self.transparent = colour is None
        start = QColor(*(colour or (255, 255, 255)))
        self.h, self.s, self.v = max(start.hueF(), 0.0), start.saturationF(), start.valueF()
        self.recent = [c for c in (parse_hex(x) for x in load_settings().get("recent_colours", [])) if c]
        self._syncing = False
        self.mode = Segmented(["Transparent", "Colour"], 0 if self.transparent else 1, self._mode, w=260, h=38, parent=self)
        self.field = ColourField(self)
        self.hue = HueBar(self)
        self.hex = Field(self)
        self.rgb = [NumField(self) for _ in range(3)]
        for f in [self.hex] + self.rgb:
            f.setFont(font(10))
            f.setFixedHeight(40)
            f.restyle()
        self.hex.setMaxLength(7)
        self.hex.editingFinished.connect(self._from_hex)
        for f in self.rgb:
            f.setMaxLength(3)
            f.setAlignment(Qt.AlignCenter)
            f.editingFinished.connect(self._from_rgb)
        self.pick = Button("Pick from screen", "dropper", kind="outlined", w=196, h=40, parent=self, on_click=self._eyedropper)
        self.cancel = Button("Cancel", kind="outlined", w=104, h=40, parent=self, on_click=self.reject)
        self.ok = Button("Apply", kind="primary", w=112, h=40, parent=self, on_click=self.accept)
        self.kids = [self.mode, self.field, self.hue, self.hex, self.pick, self.cancel, self.ok] + self.rgb
        for k in self.kids:
            k.hide()
        self._sync_fields()
        self.setFocusPolicy(Qt.StrongFocus)
        self._layout()
        self.show()
        self.raise_()
        self.setFocus()
        animate(self, "a", 1.0, 220, QEasingCurve.OutCubic)
        QTimer.singleShot(120, lambda: [k.show() for k in self.kids])

    # -- state
    def colour(self):
        c = QColor.fromHsvF(self.h, self.s, self.v)
        return c.red(), c.green(), c.blue()

    def set_hsv(self, h, s, v):
        self.h, self.s, self.v = h, s, v
        if self.transparent:
            self.transparent = False
            self.mode.select(1, notify=False)
        self._sync_fields()
        self.field.update()
        self.hue.update()
        self.update()

    def set_rgb(self, rgb):
        c = QColor(*rgb)
        self.set_hsv(max(c.hueF(), self.h if c.hueF() < 0 else 0.0), c.saturationF(), c.valueF())

    def _sync_fields(self):
        self._syncing = True
        rgb = self.colour()
        self.hex.setText(hex_of(rgb))
        for f, value in zip(self.rgb, rgb):
            f.setText(str(value))
        self._syncing = False

    def _from_hex(self):
        rgb = parse_hex(self.hex.text())
        if rgb:
            self.set_rgb(rgb)
        else:
            self._sync_fields()

    def _from_rgb(self):
        if self._syncing:
            return
        try:
            rgb = tuple(min(max(int(f.text() or 0), 0), 255) for f in self.rgb)
        except ValueError:
            self._sync_fields()
            return
        self.set_rgb(rgb)

    def _mode(self, index):
        self.transparent = index == 0
        self.field.update()
        self.hue.update()
        self.update()

    def _eyedropper(self):
        self._picker = ScreenPicker(self._picked)

    def _picked(self, rgb):
        self._picker = None
        self.app.activateWindow()
        self.app.raise_()
        if rgb:
            self.set_rgb(rgb)

    # -- geometry
    def _layout(self):
        self.card.moveCenter(QPointF(self.width() / 2, self.height() / 2))
        c = self.card
        left, top = int(c.left() + 28), int(c.top())
        self.mode.move(left, top + 84)
        self.field.setGeometry(left, top + 140, 280, 200)
        self.hue.setGeometry(left, top + 352, 280, 20)
        x2 = left + 300
        self.hex.setGeometry(x2 + 44, top + 228, int(c.right() - 28) - x2 - 44, 40)
        for i, f in enumerate(self.rgb):
            f.setGeometry(x2 + 44 + i * 58, top + 276, 52, 40)
        self.pick.move(x2 - self.pick.glow, top + 326 - self.pick.glow)
        g = self.ok.glow
        self.ok.move(int(c.right() - 28 - self.ok.width() + g), int(c.bottom() - 22 - self.ok.height() + g))
        self.cancel.move(int(self.ok.x() + g - 12 - self.cancel.width() + self.cancel.glow),
                         int(c.bottom() - 22 - self.cancel.height() + self.cancel.glow))

    def resizeEvent(self, _):
        self._layout()

    def restyle(self):
        self.update()

    def _swatches(self):
        c = self.card
        out, x, y = [], c.left() + 28, c.top() + 404
        for colour in PRESET_COLOURS:
            out.append((QRectF(x, y, self.SW, self.SW), colour))
            x += self.SW + 10
        x += 14
        for colour in self.recent[:6]:
            out.append((QRectF(x, y, self.SW, self.SW), colour))
            x += self.SW + 10
        return out

    # -- closing
    def accept(self):
        colour = None if self.transparent else self.colour()
        if colour is not None:
            recent = [hex_of(colour)] + [hex_of(c) for c in self.recent if c != colour]
            save_settings(recent_colours=recent[:6])
        self._close()
        self.on_apply(colour)

    def reject(self):
        self._close()

    def _close(self):
        for k in self.kids:
            k.hide()

        def finish():
            self.hide()
            self.deleteLater()
            if self.app._colour is self:
                self.app._colour = None
        animate(self, "a", 0.0, 150, QEasingCurve.InCubic, done=finish)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.reject()
        elif e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.accept()

    def mousePressEvent(self, e):
        e.accept()
        if not self.card.contains(e.position()):
            self.reject()
            return
        for rect, colour in self._swatches():
            if rect.adjusted(-3, -3, 3, 3).contains(e.position()):
                self.set_rgb(colour)
                return

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(0, 0, 0, int(110 * self.a)))
        p.setOpacity(self.a)
        sc = 0.96 + 0.04 * self.a
        c = self.card
        p.translate(c.center())
        p.scale(sc, sc)
        p.translate(-c.center())
        soft_shadow(p, c, 24, 2.4, spread=16, dy=6)
        p.fillPath(rounded(c, 24), qc(T["menu"]))
        left = c.left() + 28
        p.setPen(qc(T["ink"]))
        p.setFont(font(14, QFont.DemiBold))
        p.drawText(QRectF(left, c.top() + 22, 300, 30), Qt.AlignVCenter, "Background")
        p.setPen(qc(T["ink_soft"]))
        p.setFont(font(9.5))
        p.drawText(QRectF(left, c.top() + 52, c.width() - 56, 22), Qt.AlignVCenter,
                   "Fills behind every cutout you save. Transparent keeps a clear PNG.")
        x2 = left + 300
        # Before / after chip.
        chip = QRectF(x2, c.top() + 140, c.right() - 28 - x2, 58)
        half = QRectF(chip.left(), chip.top(), chip.width() / 2, chip.height())
        paint_swatch(p, half, self.original, radius=12)
        paint_swatch(p, QRectF(half.right(), chip.top(), chip.width() / 2, chip.height()),
                     None if self.transparent else self.colour(), radius=12)
        p.setFont(font(8.5))
        p.setPen(qc(T["ink_faint"]))
        p.drawText(QRectF(chip.left(), chip.bottom() + 2, chip.width() / 2, 16), Qt.AlignCenter, "Current")
        p.drawText(QRectF(half.right(), chip.bottom() + 2, chip.width() / 2, 16), Qt.AlignCenter, "New")
        p.setPen(qc(T["ink_soft"]))
        p.setFont(font(10))
        p.drawText(QRectF(x2, c.top() + 228, 44, 40), Qt.AlignVCenter, "Hex")
        p.drawText(QRectF(x2, c.top() + 276, 44, 40), Qt.AlignVCenter, "RGB")
        p.setPen(qc(T["ink"]))
        p.setFont(font(10.5, QFont.DemiBold))
        p.drawText(QRectF(left, c.top() + 380, 200, 20), Qt.AlignVCenter, "Presets and recent")
        for rect, colour in self._swatches():
            paint_swatch(p, rect, colour)
            if not self.transparent and colour == self.colour():
                p.setPen(QPen(qc(T["primary"]), 2))
                p.drawEllipse(rect.adjusted(-4, -4, 4, 4))


class CropTool(QWidget):
    """Crop handles over the preview's original image. Corners resize (keeping the chosen
    ratio), edges resize freely in Free mode, dragging inside moves. The box is kept in
    fractions of the image, so it applies at full resolution."""

    HANDLE = 12
    MIN = 24

    def __init__(self, preview, app):
        super().__init__(preview)
        self.preview, self.app = preview, app
        self.box = QRectF(0, 0, 1, 1)          # fractions of the image
        self.ratio_key = "free"
        self.drag = None
        self.setMouseTracking(True)
        self.ratio_button = Button("Free", kind="field", w=170, h=38, trail="chev-down", parent=self,
                                   on_click=self._ratio_menu)
        self.reset_button = Button("Reset", kind="outlined", w=92, h=38, parent=self, on_click=self.reset)
        self.done_button = Button("Done", kind="primary", w=96, h=38, parent=self, on_click=self.app.finish_crop)
        self.hide()

    # -- mapping
    def image_rect(self):
        return self.preview.image_rect()

    def to_view(self, box):
        img = self.image_rect()
        return QRectF(img.left() + box.left() * img.width(), img.top() + box.top() * img.height(),
                      box.width() * img.width(), box.height() * img.height())

    def to_box(self, view):
        img = self.image_rect()
        return QRectF((view.left() - img.left()) / img.width(), (view.top() - img.top()) / img.height(),
                      view.width() / img.width(), view.height() / img.height())

    def ratio(self):
        if self.ratio_key == "free":
            return None
        if self.ratio_key == "original":
            img = self.image_rect()
            return img.width() / max(img.height(), 1)
        return RATIO[self.ratio_key]

    # -- lifecycle
    def start(self, box, ratio_key):
        self.box = QRectF(*box[:2], box[2] - box[0], box[3] - box[1]) if box else QRectF(0, 0, 1, 1)
        self.ratio_key = ratio_key if box else "free"
        self.ratio_button.set_text(dict((k, t) for k, t, _ in CROP_RATIOS)[self.ratio_key])
        self.setGeometry(self.preview.rect())
        self._layout()
        self.show()
        self.raise_()
        self.setFocus()

    def value(self):
        """(x0, y0, x1, y1) or None when the box is the whole image."""
        b = self.box
        if b.left() < .002 and b.top() < .002 and b.right() > .998 and b.bottom() > .998:
            return None
        return (max(b.left(), 0.0), max(b.top(), 0.0), min(b.right(), 1.0), min(b.bottom(), 1.0))

    def reset(self):
        self.box = QRectF(0, 0, 1, 1)
        self.ratio_key = "free"
        self.ratio_button.set_text("Free")
        self.update()

    def _layout(self):
        bar_y = self.height() - 50
        total = self.ratio_button.width() + self.reset_button.width() + self.done_button.width() + 8
        x = (self.width() - total) / 2
        for b in (self.ratio_button, self.reset_button, self.done_button):
            b.move(int(x), int(bar_y - b.glow + 6))
            x += b.width() + 4

    def resizeEvent(self, _):
        self._layout()

    def _ratio_menu(self):
        rows = [(title, note, False) for _k, title, note in CROP_RATIOS]
        current = [k for k, _t, _n in CROP_RATIOS].index(self.ratio_key)
        self.app._popup(Menu(rows, current, self._pick_ratio, width=240), self.ratio_button)

    def _pick_ratio(self, index):
        key, title, _note = CROP_RATIOS[index]
        self.ratio_key = key
        self.ratio_button.set_text(title)
        ratio = self.ratio()
        if ratio:
            # The largest box of that shape around the current centre.
            view = self.to_view(self.box)
            img = self.image_rect()
            w = min(img.width(), img.height() * ratio)
            h = w / ratio
            cx = min(max(view.center().x(), img.left() + w / 2), img.right() - w / 2)
            cy = min(max(view.center().y(), img.top() + h / 2), img.bottom() - h / 2)
            self.box = self.to_box(QRectF(cx - w / 2, cy - h / 2, w, h))
        self.update()

    # -- interaction
    def _hit(self, pos):
        v = self.to_view(self.box)
        corners = {"tl": v.topLeft(), "tr": v.topRight(), "bl": v.bottomLeft(), "br": v.bottomRight()}
        for name, pt in corners.items():
            if abs(pos.x() - pt.x()) <= self.HANDLE and abs(pos.y() - pt.y()) <= self.HANDLE:
                return name
        if self.ratio() is None:
            inside_x = v.left() < pos.x() < v.right()
            inside_y = v.top() < pos.y() < v.bottom()
            if inside_x and abs(pos.y() - v.top()) <= 8:
                return "t"
            if inside_x and abs(pos.y() - v.bottom()) <= 8:
                return "b"
            if inside_y and abs(pos.x() - v.left()) <= 8:
                return "l"
            if inside_y and abs(pos.x() - v.right()) <= 8:
                return "r"
        if v.contains(pos):
            return "move"
        return None

    CURSORS = {"tl": Qt.SizeFDiagCursor, "br": Qt.SizeFDiagCursor, "tr": Qt.SizeBDiagCursor,
               "bl": Qt.SizeBDiagCursor, "t": Qt.SizeVerCursor, "b": Qt.SizeVerCursor,
               "l": Qt.SizeHorCursor, "r": Qt.SizeHorCursor, "move": Qt.SizeAllCursor}

    def mousePressEvent(self, e):
        e.accept()
        if e.button() != Qt.LeftButton:
            return
        hit = self._hit(e.position())
        if hit:
            self.drag = (hit, e.position(), self.to_view(self.box))

    def mouseReleaseEvent(self, _):
        self.drag = None

    def mouseMoveEvent(self, e):
        pos = e.position()
        if not self.drag:
            self.setCursor(self.CURSORS.get(self._hit(pos), Qt.ArrowCursor))
            return
        kind, start, v0 = self.drag
        img = self.image_rect()
        v = QRectF(v0)
        if kind == "move":
            d = pos - start
            v.translate(d)
            v.moveLeft(min(max(v.left(), img.left()), img.right() - v.width()))
            v.moveTop(min(max(v.top(), img.top()), img.bottom() - v.height()))
        elif len(kind) == 2:
            # Corner: the opposite corner stays put.
            ax = v0.right() if "l" in kind else v0.left()
            ay = v0.bottom() if "t" in kind else v0.top()
            sx = -1 if "l" in kind else 1
            sy = -1 if "t" in kind else 1
            mx = min(max(pos.x(), img.left()), img.right())
            my = min(max(pos.y(), img.top()), img.bottom())
            w, h = max(abs(mx - ax), self.MIN), max(abs(my - ay), self.MIN)
            ratio = self.ratio()
            if ratio:
                w = max(w, h * ratio)
                room_w = (ax - img.left()) if sx < 0 else (img.right() - ax)
                room_h = (ay - img.top()) if sy < 0 else (img.bottom() - ay)
                w = min(w, room_w, room_h * ratio)
                h = w / ratio
            x = ax - w if sx < 0 else ax
            y = ay - h if sy < 0 else ay
            v = QRectF(x, y, w, h)
        else:
            if kind == "l":
                v.setLeft(min(max(pos.x(), img.left()), v0.right() - self.MIN))
            elif kind == "r":
                v.setRight(max(min(pos.x(), img.right()), v0.left() + self.MIN))
            elif kind == "t":
                v.setTop(min(max(pos.y(), img.top()), v0.bottom() - self.MIN))
            elif kind == "b":
                v.setBottom(max(min(pos.y(), img.bottom()), v0.top() + self.MIN))
        self.box = self.to_box(v)
        self.update()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.app.finish_crop()
        elif e.key() == Qt.Key_Escape:
            self.app.finish_crop(cancel=True)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        img = self.image_rect()
        v = self.to_view(self.box)
        shade = QPainterPath()
        shade.addRect(img)
        hole = QPainterPath()
        hole.addRect(v)
        p.fillPath(shade.subtracted(hole), QColor(0, 0, 0, 140))
        p.setPen(QPen(QColor(255, 255, 255, 110), 1))
        for i in (1, 2):
            p.drawLine(QPointF(v.left() + v.width() * i / 3, v.top()), QPointF(v.left() + v.width() * i / 3, v.bottom()))
            p.drawLine(QPointF(v.left(), v.top() + v.height() * i / 3), QPointF(v.right(), v.top() + v.height() * i / 3))
        p.setPen(QPen(QColor(255, 255, 255), 1.5))
        p.setBrush(Qt.NoBrush)
        p.drawRect(v)
        pen = QPen(QColor(255, 255, 255), 4)
        pen.setCapStyle(Qt.SquareCap)
        p.setPen(pen)
        L = 16
        for (x, y, dx, dy) in ((v.left(), v.top(), 1, 1), (v.right(), v.top(), -1, 1),
                               (v.left(), v.bottom(), 1, -1), (v.right(), v.bottom(), -1, -1)):
            p.drawLine(QPointF(x, y), QPointF(x + dx * L, y))
            p.drawLine(QPointF(x, y), QPointF(x, y + dy * L))
        if self.ratio() is None:
            for pt in (QPointF(v.center().x(), v.top()), QPointF(v.center().x(), v.bottom())):
                p.drawLine(pt - QPointF(8, 0), pt + QPointF(8, 0))
            for pt in (QPointF(v.left(), v.center().y()), QPointF(v.right(), v.center().y())):
                p.drawLine(pt - QPointF(0, 8), pt + QPointF(0, 8))
        # Size readout, in pixels of the original.
        w_px, h_px = self.app.crop_pixels(self.box)
        if w_px:
            tag = f"{w_px} × {h_px}"
            fm = QFontMetrics(font(9, QFont.DemiBold))
            r = QRectF(v.left() + 8, v.top() + 8, fm.horizontalAdvance(tag) + 16, 22)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 150))
            p.drawRoundedRect(r, 11, 11)
            p.setPen(QColor(255, 255, 255))
            p.setFont(font(9, QFont.DemiBold))
            p.drawText(r, Qt.AlignCenter, tag)


# ------------------------------------------------------------- settings --

DEFAULT_FOLDER = "Cutout"


def app_data():
    """%LOCALAPPDATA%/Cutout: settings, the log and the engine's compile cache."""
    data = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP_NAME
    try:
        data.mkdir(parents=True, exist_ok=True)
    except OSError:
        data = Path(tempfile.gettempdir()) / APP_NAME
        data.mkdir(parents=True, exist_ok=True)
    return data


def settings_path():
    return Path(os.environ.get("CUTOUT_SETTINGS") or app_data() / "settings.json")


def load_settings():
    try:
        return json.loads(settings_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(**values):
    data = load_settings()
    data.update(values)
    try:
        settings_path().write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


DELETE_MODES = ["trash", "permanent"]


def default_out():
    pictures = core.HOME / "Pictures"
    return (pictures if pictures.is_dir() else core.HOME) / DEFAULT_FOLDER


class SettingsDialog(QWidget):
    """The first-run welcome (first=True) and, later, Settings: the folder cutouts are
    saved in (its name and where it lives) and the theme the app opens in."""

    LABEL_W = 112

    def __init__(self, app, first=False):
        super().__init__(app)
        self.app, self.first = app, first
        self.a = 0.0
        self.error = ""
        self.setGeometry(app.rect())
        self.card = QRectF(0, 0, 620, 468 if first else 680)
        current = Path(app.out.text().strip() or default_out())
        self.name = Field(current.name or DEFAULT_FOLDER, self)
        self.where = Field(str(current.parent), self)
        for f in (self.name, self.where):
            f.setFont(font(10))
            f.setFixedHeight(42)
            f.restyle()
            f.textChanged.connect(self._changed)
            f.returnPressed.connect(self.accept)
        self.browse = Button("Browse", "folder", kind="outlined", w=118, h=40, parent=self, on_click=self._browse)
        self.theme = Segmented(["Dark", "Light"], THEME_ORDER.index(app.theme), self._theme, w=220, h=38, parent=self)
        self.delete_mode = app.delete_mode
        self.originals = Segmented(["Recycle Bin", "Delete permanently"], DELETE_MODES.index(app.delete_mode),
                                   self._originals, w=330, h=38, parent=self)
        self.ok = Button("Get started" if first else "Save", kind="primary", w=150 if first else 110, h=40,
                         parent=self, on_click=self.accept)
        self.extra = [] if first else [
            Button("Cancel", kind="outlined", w=104, h=40, parent=self, on_click=self.reject),
            Button("Manage models", kind="outlined", w=160, h=40, parent=self, on_click=self._models)]
        self.autosave = bool(app.autosave)
        self.after = Segmented(["Review first", "Save automatically"], int(self.autosave), self._after,
                               w=330, h=38, parent=self)
        self.kids = [self.name, self.where, self.browse, self.theme, self.ok] + self.extra
        if first:
            # The welcome asks only two things; these belong to Settings.
            self.originals.hide()
            self.after.hide()
        else:
            self.kids += [self.originals, self.after]
        for k in self.kids:
            k.hide()
        self.setFocusPolicy(Qt.StrongFocus)
        self._layout()
        self.show()
        self.raise_()
        self.setFocus()
        animate(self, "a", 1.0, 240, QEasingCurve.OutCubic)
        QTimer.singleShot(120, lambda: [k.show() for k in self.kids])

    # -- geometry
    def _layout(self):
        self.card.moveCenter(QPointF(self.width() / 2, self.height() / 2))
        c = self.card
        x = int(c.left() + 28 + self.LABEL_W)
        right = int(c.right() - 28)
        self.name.setGeometry(x, int(c.top() + 142), right - x, 42)
        bw = self.browse.width() - 2 * self.browse.glow
        self.where.setGeometry(x, int(c.top() + 196), right - x - bw - 10, 42)
        self.browse.move(right - bw - self.browse.glow, int(c.top() + 197 - self.browse.glow))
        self.theme.move(x, int(c.top() + 318))
        self.originals.move(x, int(c.top() + 418))
        self.after.move(x, int(c.top() + 520))
        g = self.ok.glow
        self.ok.move(int(c.right() - 28 - self.ok.width() + g), int(c.bottom() - 22 - self.ok.height() + g))
        if self.extra:
            cancel, models = self.extra
            cancel.move(int(self.ok.x() + g - 12 - cancel.width() + cancel.glow), int(c.bottom() - 22 - cancel.height() + cancel.glow))
            models.move(int(c.left() + 28 - models.glow), int(c.bottom() - 22 - models.height() + models.glow))

    def resizeEvent(self, _):
        self._layout()

    def restyle(self):
        self.update()

    # -- values
    def target(self):
        name, where = self.name.text().strip(), self.where.text().strip()
        if not name:
            return None, "Give the folder a name"
        if any(ch in name for ch in '<>:"/\\|?*') or name in (".", ".."):
            return None, 'A folder name can\'t contain  < > : " / \\ | ? *'
        if not where:
            return None, "Choose where the folder goes"
        return Path(where) / name, ""

    def _changed(self, _text):
        self.error = ""
        self.update()

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(self, "Where should the folder go?", self.where.text())
        if folder:
            self.where.setText(str(Path(folder)))

    def _theme(self, index):
        self.app.theme_switch.select(index)          # applies it; the app remembers it

    def sync_theme(self):
        index = THEME_ORDER.index(self.app.theme)
        if self.theme.index != index:
            self.theme.select(index, notify=False)

    def _models(self):
        self._close(self.app.open_models)

    def _after(self, index):
        self.autosave = bool(index)
        self.update()

    def _originals(self, index):
        self.delete_mode = DELETE_MODES[index]
        self.update()

    # -- closing
    def accept(self):
        target, error = self.target()
        if error:
            self.error = error
            self.update()
            return
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self.error = f"Can't create that folder: {exc}"
            self.update()
            return
        changed = str(target) != self.app.out.text().strip()
        self.app.out.setText(str(target))
        save_settings(out_dir=str(target), theme=self.app.theme, setup_done=True)
        if not self.first and self.delete_mode != self.app.delete_mode:
            self.app.set_delete_mode(self.delete_mode)
        if not self.first and self.autosave != self.app.autosave:
            self.app.autosave = self.autosave
            save_settings(autosave=self.autosave)
            self.app.notify("Cutouts will be saved as soon as they're cut" if self.autosave
                            else "Cutouts wait for you to review and save them")
        first = self.first
        self._close()
        if first:
            self.app.notify(f"All set  ·  cutouts go to {target}", "success")
        elif changed:
            self.app.notify(f"Saving to {target.name}", "success")

    def reject(self):
        if not self.first:
            self._close()

    def _close(self, then=None):
        for k in self.kids:
            k.hide()

        def finish():
            self.hide()
            self.deleteLater()
            if self.app._settings is self:
                self.app._settings = None
            if then:
                then()
        animate(self, "a", 0.0, 160, QEasingCurve.InCubic, done=finish)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.reject()
        elif e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.accept()

    def mousePressEvent(self, e):
        e.accept()
        if not self.card.contains(e.position()):
            self.reject()

    # -- paint
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(0, 0, 0, int(120 * self.a)))
        p.setOpacity(self.a)
        s = 0.96 + 0.04 * self.a
        c = self.card
        p.translate(c.center())
        p.scale(s, s)
        p.translate(-c.center())
        soft_shadow(p, c, 24, 2.4, spread=16, dy=6)
        p.fillPath(rounded(c, 24), qc(T["menu"]))
        left, width = c.left() + 28, c.width() - 56

        if self.first and Brand.ok():
            draw_mark(p, QPointF(c.right() - 58, c.top() + 50), 22, T["ink"])
        p.setPen(qc(T["ink"]))
        p.setFont(font(15, QFont.DemiBold))
        p.drawText(QRectF(left, c.top() + 24, width, 34), Qt.AlignVCenter,
                   "Welcome to Cutout" if self.first else "Settings")
        p.setPen(qc(T["ink_soft"]))
        p.setFont(font(10))
        sub = ("Two quick choices before you start. You can change both later in Settings."
               if self.first else "Where cutouts are saved, and how Cutout looks when it opens.")
        p.drawText(QRectF(left, c.top() + 60, width - (70 if self.first else 0), 44), Qt.TextWordWrap, sub)

        p.setPen(qc(T["ink"]))
        p.setFont(font(11, QFont.DemiBold))
        p.drawText(QRectF(left, c.top() + 108, width, 26), Qt.AlignVCenter, "Save cutouts to")
        p.setPen(qc(T["ink_soft"]))
        p.setFont(font(10))
        p.drawText(QRectF(left, c.top() + 142, self.LABEL_W, 42), Qt.AlignVCenter, "Folder name")
        p.drawText(QRectF(left, c.top() + 196, self.LABEL_W, 42), Qt.AlignVCenter, "Location")
        target, error = self.target()
        error = self.error or error
        info = QRectF(left + self.LABEL_W, c.top() + 244, width - self.LABEL_W, 22)
        p.setFont(font(9.5))
        if error:
            p.setPen(qc(T["error"]))
            p.drawText(info, Qt.AlignVCenter, error)
        else:
            exists = target.is_dir()
            p.setPen(qc(T["ink_soft"]))
            prefix = "Saving into existing folder  " if exists else "Will create  "
            text = QFontMetrics(font(9.5)).elidedText(prefix + str(target), Qt.ElideMiddle, int(info.width()))
            p.drawText(info, Qt.AlignVCenter, text)

        p.setPen(QPen(qc(T["outline_var"], 200), 1))
        p.drawLine(QPointF(left, c.top() + 286), QPointF(c.right() - 28, c.top() + 286))
        p.setPen(qc(T["ink"]))
        p.setFont(font(11, QFont.DemiBold))
        p.drawText(QRectF(left, c.top() + 318 + self.theme.height() / 2 - 13, self.LABEL_W, 26), Qt.AlignVCenter, "Theme")
        p.setPen(qc(T["ink_soft"]))
        p.setFont(font(9.5))
        p.drawText(QRectF(left + self.LABEL_W, c.top() + 364, width - self.LABEL_W, 22), Qt.AlignVCenter,
                   "Cutout opens in this theme. The switch in the title bar changes it too.")
        if self.first:
            return
        p.setPen(QPen(qc(T["outline_var"], 200), 1))
        p.drawLine(QPointF(left, c.top() + 400), QPointF(c.right() - 28, c.top() + 400))
        p.setPen(qc(T["ink"]))
        p.setFont(font(11, QFont.DemiBold))
        p.drawText(QRectF(left, c.top() + 418 + self.originals.height() / 2 - 13, self.LABEL_W, 26),
                   Qt.AlignVCenter, "Originals")
        p.setFont(font(9.5))
        if self.delete_mode == "permanent":
            p.setPen(qc(T["error"]))
            note = "With “Delete original” ticked, originals are erased once saved. They can't be recovered."
        else:
            p.setPen(qc(T["ink_soft"]))
            note = "With “Delete original” ticked, originals go to the Recycle Bin once saved."
        p.drawText(QRectF(left + self.LABEL_W, c.top() + 462, width - self.LABEL_W, 40), Qt.TextWordWrap, note)
        p.setPen(QPen(qc(T["outline_var"], 200), 1))
        p.drawLine(QPointF(left, c.top() + 502), QPointF(c.right() - 28, c.top() + 502))
        p.setPen(qc(T["ink"]))
        p.setFont(font(11, QFont.DemiBold))
        p.drawText(QRectF(left, c.top() + 520 + self.after.height() / 2 - 13, self.LABEL_W, 26),
                   Qt.AlignVCenter, "After cutting")
        p.setPen(qc(T["ink_soft"]))
        p.setFont(font(9.5))
        p.drawText(QRectF(left + self.LABEL_W, c.top() + 564, width - self.LABEL_W, 40), Qt.TextWordWrap,
                   "Cutouts are saved to the folder as soon as they're cut." if self.autosave else
                   "Check each cutout first, then Save it, or Retry with another model.")


# --------------------------------------------------------------- engine --

class Signals(QObject):
    status = Signal(str)
    progress = Signal(float, float)
    indeterminate = Signal(bool)
    result = Signal(str, str)              # source, cutout in the session folder
    failed = Signal(str, str)
    summary = Signal(int, int, int)        # cut, total, originals deleted (auto-save)
    saved = Signal(str, str, bool)         # source, saved file, original deleted
    save_done = Signal(int, int, int, str) # saved, total, originals deleted, first error
    error = Signal(str)
    done = Signal()
    web_done = Signal(list, list)


def save_cutout(source, cutout, opts):
    """Export one session cutout into the output folder with the current framing and
    background; copy or delete the original as asked. Returns (saved path, deleted)."""
    out_dir = Path(opts["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(cutout) as im:
        result = im.convert("RGBA")
    result = export_cutout(result, opts["crops"].get(source), opts["ratio"], opts["size"], opts["background"])
    dest = core.unique(out_dir / f"{Path(source).stem}_cutout.png")
    result.save(dest, "PNG", optimize=True)
    if opts["keep"]:
        try:
            shutil.copy2(source, out_dir / Path(source).name)
        except Exception:
            pass
    deleted = False
    # `delete` is None, "trash" or "permanent"; only once the cutout is safely saved.
    if opts["delete"] and dest.exists() and dest.stat().st_size > 0 and Path(source).exists():
        try:
            if opts["delete"] == "permanent":
                os.remove(source)
                deleted = True
            elif core.HAS_TRASH:
                core.send2trash(str(Path(source)))
                deleted = True
        except Exception:
            pass
    return dest, deleted


def save_items(items, opts, sig):
    """Save [(source, cutout)] on a worker thread."""
    saved = deleted = 0
    first_error = ""
    for index, (source, cutout) in enumerate(items, start=1):
        sig.status.emit(f"Saving {index} of {len(items)}  ·  {Path(source).name}")
        try:
            dest, gone = save_cutout(source, cutout, opts)
            saved += 1
            deleted += gone
            sig.saved.emit(source, str(dest), gone)
        except Exception as exc:
            first_error = first_error or f"{Path(source).name}: {exc}"
        sig.progress.emit(index, len(items))
    sig.save_done.emit(saved, len(items), deleted, first_error)


def process(files, model_name, session_dir, alpha, do_enhance, autosave, sig):
    """Cut each file into the session folder (model, cut, enhance). Framing, background
    and saving happen afterwards, so they can change without cutting again; with
    `autosave` (the save options) each cutout is also saved straight away."""
    ready = app_data() / "engine-ready"
    if not ready.exists():
        # The engine compiles itself once; the installer normally does this ahead of time.
        sig.status.emit("Setting up  ·  the first run takes about a minute")
    from rembg import remove
    ready.touch()
    try:
        if not core.is_downloaded(model_name):
            last = [0.0]

            def report(got, total, rate):
                now = time.time()
                if now - last[0] < .15 and got < total:
                    return
                last[0] = now
                sig.progress.emit(got, max(total, 1))
                sig.status.emit(f"Downloading  {got/1048576:.0f} / {total/1048576:.0f} MB"
                                f"   ·   {got/max(total,1)*100:.0f}%   ·   {rate:.1f} MB/s")

            sig.status.emit("Preparing download")
            model_store.download(model_name, report)
        sig.status.emit("Loading model")
        session = core.get_session(model_name)
        sig.indeterminate.emit(False)
        sig.progress.emit(0, len(files))
    except Exception as exc:
        sig.indeterminate.emit(False)
        sig.error.emit(f"Could not load the model: {exc}")
        sig.done.emit()
        return

    matting = {}
    if alpha:
        matting = dict(alpha_matting=True, alpha_matting_foreground_threshold=240,
                       alpha_matting_background_threshold=10, alpha_matting_erode_size=10)
    staging = tempfile.mkdtemp(prefix="cutout-")
    done = trashed = 0
    for index, source in enumerate(files, start=1):
        sig.status.emit(f"{index} of {len(files)}  ·  {Path(source).name}")
        try:
            openable = core.ensure_openable(Path(source), staging)
            with Image.open(openable) as im:
                result = remove(im.convert("RGBA"), session=session, **matting)
            if do_enhance:
                result = enhance_rgba(result, strength="normal")
            cutout = core.unique(Path(session_dir) / f"{Path(source).stem}.png")
            result.save(cutout, "PNG", compress_level=1)
            done += 1
            sig.result.emit(source, str(cutout))
            if autosave:
                dest, gone = save_cutout(source, cutout, autosave)
                trashed += gone
                sig.saved.emit(source, str(dest), gone)
        except Exception as exc:
            sig.failed.emit(Path(source).name, str(exc))
            sig.status.emit(f"Failed: {Path(source).name}  ·  {exc}")
        finally:
            sig.progress.emit(index, len(files))
    shutil.rmtree(staging, ignore_errors=True)
    sig.summary.emit(done, len(files), trashed)
    sig.done.emit()


# ------------------------------------------------------------------ app ---

class App(QWidget):
    EDGE = 7

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle(APP_NAME)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.resize(1180, 820)
        self.setMinimumSize(1060, 760)
        self.settings = load_settings()
        self.theme = self.settings.get("theme") if self.settings.get("theme") in THEMES else "dark"
        T.clear()
        T.update(THEMES[self.theme])
        self._settings = None
        self._welcome_checked = False
        self.delete_mode = self.settings.get("delete_mode") if self.settings.get("delete_mode") in DELETE_MODES else "trash"
        self.files, self.results, self.failures = [], {}, []
        self.current, self.pane, self.busy = None, 0, False
        self.model_index = 0
        self.flags = dict(alpha=False, enhance=False, keep=False, delete=False)
        saved = load_settings().get("export", {})
        self.ratio_key = saved.get("ratio") if saved.get("ratio") in RATIO else "original"
        self.out_size = saved.get("size") if saved.get("size") in SIZES else None
        self.background = parse_hex(saved.get("background") or "")
        self.crops = {}               # source path -> (x0, y0, x1, y1), fractions of the image
        self.saved = {}               # source path -> saved file
        self.autosave = bool(load_settings().get("autosave", False))
        self.session = Path(tempfile.mkdtemp(prefix="cutout-session-"))
        self._colour = None
        self._crop_tool = None
        self._menu = None
        self.sig = Signals()
        self._wire_signals()
        self._build()
        self.toast = Toast(self)
        self.jobs = ModelJobs(self)
        self.jobs.notice.connect(self.notify)
        self.jobs.changed.connect(lambda _n: self._refresh_model_note())
        self._models = None
        self._intro = Intro(self) if Brand.ok() and not os.environ.get("CUTOUT_NO_INTRO") else None
        QShortcut(QKeySequence.Paste, self, activated=self.on_paste)
        self._sync_queue()

    # -- layout -----------------------------------------------------------

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 14, 22, 22)
        root.setSpacing(14)

        top = TitleBar(self)
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(6)
        lights = QHBoxLayout()
        self.traffic = Traffic(self.close, self.showMinimized, self.toggle_max)
        lights.addWidget(self.traffic)
        lights.addStretch(1)
        tl.addLayout(lights)
        # Kept in [min, max, close] order for callers that index them.
        self.win_buttons = [self.traffic.min_b, self.traffic.max_b, self.traffic.close_b]
        row = QHBoxLayout()
        self.title = Wordmark()
        row.addWidget(self.title)
        row.addStretch(1)
        self.settings_button = Button("Settings", "gear", kind="outlined", w=122, h=38, on_click=self.open_settings)
        row.addWidget(self.settings_button)
        row.addSpacing(6)
        self.theme_switch = Segmented(["Dark", "Light"], THEME_ORDER.index(self.theme), self.set_theme, w=200, h=38)
        row.addWidget(self.theme_switch)
        tl.addLayout(row)
        root.addWidget(top)

        mid = QHBoxLayout()
        mid.setSpacing(18)
        root.addLayout(mid, 1)

        # left: queue
        self.left = Panel()
        ll = QVBoxLayout(self.left)
        ll.setContentsMargins(22, 18, 22, 20)
        ll.setSpacing(4)
        ll.addWidget(Label("Drag images here", 12, QFont.DemiBold))
        ll.addWidget(Label("from Explorer or straight from a browser  ·  Ctrl+V pastes a link", 9, role="ink_soft"))
        ll.addSpacing(10)
        self.drop = DropZone(self.add_files)
        self.drop.list.itemSelectionChanged.connect(self._on_select)
        ll.addWidget(self.drop, 1)
        ll.addSpacing(12)
        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        for text, icon, fn, kind in (("Add", "plus", self.add_files, "tonal"),
                                     ("Folder", "folder", self.add_folder, "outlined"),
                                     ("Remove", "trash", self.remove_selected, "outlined"),
                                     ("Clear", "x", self.clear, "outlined")):
            buttons.addWidget(Button(text, icon, kind=kind, w=98, h=40, on_click=fn))
        buttons.addStretch(1)
        ll.addLayout(buttons)
        self.left.setFixedWidth(470)
        mid.addWidget(self.left)

        # right: preview
        self.right = Panel()
        rl = QVBoxLayout(self.right)
        rl.setContentsMargins(22, 16, 22, 22)
        rl.setSpacing(12)
        head = QHBoxLayout()
        head.addWidget(Label("Preview", 12, QFont.DemiBold))
        head.addStretch(1)
        self.crop_button = Button("Crop", "crop", kind="outlined", w=100, h=38, on_click=self.toggle_crop)
        head.addWidget(self.crop_button)
        head.addSpacing(4)
        self.pane_switch = Segmented(["Original", "Cutout"], 0, self.set_pane, w=220, h=38)
        head.addWidget(self.pane_switch)
        rl.addLayout(head)
        self.preview = Preview()
        rl.addWidget(self.preview, 1)
        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.retry_button = Button("Retry", "retry", kind="outlined", w=112, h=40, on_click=self.open_retry_menu)
        actions.addWidget(self.retry_button)
        self.result_note = Label("", 9.5, role="ink_soft")
        actions.addWidget(self.result_note)
        actions.addStretch(1)
        self.save_all_button = Button("Save all", kind="outlined", w=140, h=40, on_click=self.save_all)
        actions.addWidget(self.save_all_button)
        self.save_button = Button("Save", "save", kind="primary", w=120, h=40, on_click=self.save_current)
        actions.addWidget(self.save_button)
        rl.addLayout(actions)
        mid.addWidget(self.right, 1)

        # bottom: options
        self.bottom = Panel()
        bl = QVBoxLayout(self.bottom)
        bl.setContentsMargins(26, 20, 26, 20)
        bl.setSpacing(14)

        upper = QHBoxLayout()
        opts = QVBoxLayout()
        opts.setSpacing(12)
        r1 = QHBoxLayout()
        r1.setSpacing(14)
        r1.addWidget(Label("Model", 12, QFont.DemiBold))
        r1.addSpacing(14)
        self.model_button = Button(core.MODELS[0][1], kind="field", w=300, h=42, trail="chev-down",
                                   on_click=self.open_model_menu)
        r1.addWidget(self.model_button)
        self.model_note = Label("", 9.5, role="ink_soft")
        r1.addWidget(self.model_note)
        r1.addStretch(1)
        opts.addLayout(r1)
        r2 = QHBoxLayout()
        r2.setSpacing(12)
        r2.addWidget(Label("Export", 12, QFont.DemiBold))
        r2.addSpacing(10)
        self.aspect_button = Button(self._ratio_title(), kind="field", w=176, h=40, trail="chev-down",
                                    on_click=self.open_aspect_menu)
        r2.addWidget(self.aspect_button)
        self.size_button = Button(self._size_title(), kind="field", w=132, h=40, trail="chev-down",
                                  on_click=self.open_size_menu)
        r2.addWidget(self.size_button)
        self.bg_button = SwatchButton(w=168, h=40, on_click=self.open_colour)
        self.bg_button.set_colour(self.background)
        r2.addWidget(self.bg_button)
        r2.addStretch(1)
        opts.addLayout(r2)
        upper.addLayout(opts, 1)

        checks = QVBoxLayout()
        checks.setSpacing(8)
        self.chk = {}
        for row_items in ((("alpha", "Refine edges"), ("enhance", "Enhance details")),
                          (("keep", "Keep original"), ("delete", "Delete original"))):
            line = QHBoxLayout()
            line.setSpacing(24)
            for key, text in row_items:
                box = Check(text)
                box.on_toggle = (lambda k=key, b=box: self.toggle(k, b))
                box.setFixedWidth(184)
                self.chk[key] = box
                line.addWidget(box)
            checks.addLayout(line)
        upper.addLayout(checks)
        bl.addLayout(upper)
        self._label_delete()

        r3 = QHBoxLayout()
        r3.setSpacing(12)
        r3.addWidget(Label("Save to", 12, QFont.DemiBold))
        r3.addSpacing(10)
        self.out = Field(self.settings.get("out_dir") or str(default_out()))
        self.out.editingFinished.connect(lambda: save_settings(out_dir=self.out.text().strip()))
        self.out.setFont(font(10))
        self.out.setFixedHeight(42)
        r3.addWidget(self.out, 1)
        r3.addWidget(Button("Browse", kind="outlined", w=106, h=40, on_click=self.pick_outdir))
        r3.addWidget(Button("Open", kind="outlined", w=96, h=40, on_click=self.open_outdir))
        bl.addLayout(r3)

        sep = QWidget()
        sep.setFixedHeight(1)
        self.sep = sep
        bl.addWidget(sep)

        r4 = QHBoxLayout()
        r4.setSpacing(18)
        self.run_button = Button("Remove backgrounds", kind="primary", w=270, h=48, radius=14, on_click=self.run)
        r4.addWidget(self.run_button)
        self.bar = Progress()
        self.bar.setFixedWidth(460)
        r4.addWidget(self.bar)
        r4.addStretch(1)
        self.status = Label("Ready", 10.5, role="ink_soft")
        r4.addWidget(self.status)
        bl.addLayout(r4)
        root.addWidget(self.bottom)

        self._refresh_model_note()
        self._restyle_all()

    # -- notices ------------------------------------------------------------

    def notify(self, text, kind="info"):
        self.toast.show_message(text, kind)

    def confirm(self, title, body, confirm_text, on_confirm, danger=False):
        self._confirm = Confirm(self, title, body, confirm_text, on_confirm, danger)

    # -- window chrome ----------------------------------------------------

    def showEvent(self, e):
        super().showEvent(e)
        set_frame(int(self.winId()), T["dark"])
        round_region(self, 14)
        if self._intro is not None and not self._intro.started:
            # Let the layout settle first: the intro snapshots the finished window.
            QTimer.singleShot(60, lambda: self._intro and self._intro.start())
        elif self._intro is None:
            QTimer.singleShot(250, self._maybe_welcome)

    def _maybe_welcome(self):
        """First launch: ask where cutouts go and which theme to use."""
        if self._welcome_checked:
            return
        self._welcome_checked = True
        if self.settings.get("setup_done") or os.environ.get("CUTOUT_NO_WELCOME") or self._settings:
            return
        self._settings = SettingsDialog(self, first=True)

    def open_settings(self):
        if self._settings is None:
            self._settings = SettingsDialog(self)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        round_region(self, 14)
        intro = getattr(self, "_intro", None)
        if intro is not None:
            if intro.started:
                intro.finish()               # the snapshot no longer fits
            else:
                intro.setGeometry(self.rect())
        if hasattr(self, "toast") and self.toast.isVisible():
            self.toast.place()
        for child in (self.findChildren(Confirm) + self.findChildren(ModelsDialog)
                      + self.findChildren(SettingsDialog) + self.findChildren(ColourDialog)):
            child.setGeometry(self.rect())
            child._layout()

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == QEvent.ActivationChange and hasattr(self, "traffic"):
            self.traffic.update()

    def toggle_max(self):
        self.showNormal() if self.isMaximized() else self.showMaximized()

    def _edges(self, pos):
        if self.isMaximized():
            return Qt.Edges()
        e, w, h = self.EDGE, self.width(), self.height()
        x, y = pos.x(), pos.y()
        # The rounded corners cut the exact corner pixels away, so near a corner
        # a grab on either edge resizes both ways.
        c = 22
        left, right = x <= e, x >= w - e
        top, bottom = y <= e, y >= h - e
        if left or right:
            top, bottom = top or y <= c, bottom or y >= h - c
        if top or bottom:
            left, right = left or x <= c, right or x >= w - c
        edges = Qt.Edges()
        if left:
            edges |= Qt.LeftEdge
        if right:
            edges |= Qt.RightEdge
        if top:
            edges |= Qt.TopEdge
        if bottom:
            edges |= Qt.BottomEdge
        return edges

    def mouseMoveEvent(self, e):
        edges = self._edges(e.position().toPoint())
        cursors = {Qt.LeftEdge: Qt.SizeHorCursor, Qt.RightEdge: Qt.SizeHorCursor,
                   Qt.TopEdge: Qt.SizeVerCursor, Qt.BottomEdge: Qt.SizeVerCursor,
                   Qt.LeftEdge | Qt.TopEdge: Qt.SizeFDiagCursor, Qt.RightEdge | Qt.BottomEdge: Qt.SizeFDiagCursor,
                   Qt.RightEdge | Qt.TopEdge: Qt.SizeBDiagCursor, Qt.LeftEdge | Qt.BottomEdge: Qt.SizeBDiagCursor}
        self.setCursor(cursors.get(edges, Qt.ArrowCursor))

    def mousePressEvent(self, e):
        edges = self._edges(e.position().toPoint())
        if e.button() == Qt.LeftButton:
            if edges:
                self.windowHandle().startSystemResize(edges)
            else:
                self.windowHandle().startSystemMove()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(.6, .6, -.6, -.6)
        p.fillRect(self.rect(), qc(T["window"]))
        p.setPen(QPen(qc(T["outline_var"], 160), 1))
        p.drawPath(rounded(rect, 14))
        line = qc(T["outline_var"], 200)
        if hasattr(self, "sep"):
            g = self.sep.mapTo(self, self.sep.rect().topLeft())
            p.setPen(QPen(line, 1))
            p.drawLine(g.x(), g.y(), g.x() + self.sep.width(), g.y())

    # -- theme ------------------------------------------------------------

    def set_theme(self, index):
        snapshot = self.grab() if self.isVisible() else None
        self.theme = THEME_ORDER[index]
        T.clear()
        T.update(THEMES[self.theme])
        set_frame(int(self.winId()), T["dark"])
        save_settings(theme=self.theme)
        if self._settings is not None:
            self._settings.sync_theme()
        self._restyle_all()
        if self.current:
            self._refresh_preview()          # checkerboard follows the theme
        if snapshot is not None:
            Crossfade(self, snapshot)

    def _restyle_all(self):
        for w in self.findChildren(QWidget):
            if hasattr(w, "restyle"):
                w.restyle()
        self.out.restyle()
        self.update()

    # -- options ----------------------------------------------------------

    def toggle(self, key, box):
        permanent = self.delete_mode == "permanent"
        if key == "delete" and not self.flags["delete"] and not permanent and not core.HAS_TRASH:
            self.notify("Can't delete originals: send2trash is not installed", "error")
            return
        self.flags[key] = not self.flags[key]
        box.set_checked(self.flags[key])
        if key == "delete" and self.flags[key]:
            self.notify("Originals will be permanently deleted after saving" if permanent
                        else "Originals will go to the Recycle Bin after saving", "warning")

    def set_delete_mode(self, mode):
        """Recycle Bin or permanent deletion for "Delete original"; remembered."""
        self.delete_mode = mode
        save_settings(delete_mode=mode)
        self._label_delete()
        self.notify("Originals will be erased permanently when deleted" if mode == "permanent"
                    else "Deleted originals will go to the Recycle Bin", "warning" if mode == "permanent" else "info")

    def _label_delete(self):
        box = self.chk["delete"]
        box.text = "Delete permanently" if self.delete_mode == "permanent" else "Delete original"
        box.update()

    # -- export options ------------------------------------------------------

    def _ratio_title(self):
        return next(t for k, t, _n, _v in RATIOS if k == self.ratio_key)

    def _size_title(self):
        return "Full size" if self.out_size is None else f"{self.out_size} px"

    def _save_export(self):
        save_settings(export={"ratio": self.ratio_key, "size": self.out_size,
                              "background": hex_of(self.background) if self.background else None})

    def open_aspect_menu(self):
        rows = [(title, note, False) for _k, title, note, _v in RATIOS]
        current = [k for k, *_ in RATIOS].index(self.ratio_key)
        self._popup(Menu(rows, current, self._pick_aspect, width=300), self.aspect_button)

    def _pick_aspect(self, index):
        self.ratio_key = RATIOS[index][0]
        self.aspect_button.set_text(self._ratio_title())
        self._save_export()
        if self.pane == 1:
            self._refresh_preview()
        if self.ratio_key == "original":
            self.notify("Export keeps each photo's own framing")
        else:
            self.notify(f"Export: subject centred in {self.ratio_key}  ·  a manual crop still wins")

    def open_colour(self):
        if self._colour is None:
            self._colour = ColourDialog(self, self.background, self._apply_colour)

    def _apply_colour(self, colour):
        self.background = colour
        self.bg_button.set_colour(colour)
        self._save_export()
        if self.pane == 1:
            self._refresh_preview()
        self.notify("Background: transparent" if colour is None else f"Background: {hex_of(colour)}",
                    "success")

    def crop_pixels(self, box):
        """Size in pixels of the original that `box` (fractions) covers."""
        size = getattr(self, "_crop_size", None)
        if not size:
            return 0, 0
        return max(int(round(box.width() * size[0])), 1), max(int(round(box.height() * size[1])), 1)

    def toggle_crop(self):
        if self._crop_tool is not None and self._crop_tool.isVisible():
            self.finish_crop()
            return
        if not self.current:
            self.notify("Add an image to crop", "warning")
            return
        if self.pane != 0:
            self.pane_switch.select(0)
        try:
            with Image.open(self.current) as im:
                self._crop_size = im.size
        except Exception:
            self.notify("Can't open that image to crop", "error")
            return
        if self._crop_tool is None:
            self._crop_tool = CropTool(self.preview, self)
        self.preview.reserve = 56
        self.preview.update()
        self._cropping = self.current
        self._crop_tool.start(self.crops.get(self.current), getattr(self, "_crop_ratio", "free"))
        self.crop_button.set_text("Done")

    def finish_crop(self, cancel=False):
        tool = self._crop_tool
        if tool is None or not tool.isVisible():
            return
        source = getattr(self, "_cropping", None)
        if not cancel and source:
            value = tool.value()
            self._crop_ratio = tool.ratio_key
            name = Path(source).name
            if value:
                self.crops[source] = value
                w, h = self.crop_pixels(tool.box)
                self.notify(f"Cropped {name} to {w} × {h}", "success")
            elif source in self.crops:
                del self.crops[source]
                self.notify(f"Crop removed from {name}")
            self._mark_cropped(source)
        tool.hide()
        self.preview.reserve = 0
        self.crop_button.set_text("Crop")
        self._refresh_preview()

    def _mark_cropped(self, source):
        self._mark(source)
        if self.pane == 1 and source == self.current:
            self._refresh_preview()

    def _mark(self, source):
        """Queue label: name, then cropped / ready / saved."""
        if source not in self.files:
            return
        tags = []
        if source in self.crops:
            tags.append("cropped")
        if source in self.saved:
            tags.append("saved")
        elif source in self.results:
            tags.append("ready")
        text = Path(source).name + "".join(f"   ·  {t}" for t in tags)
        self.drop.list.item(self.files.index(source)).setText(text)

    def _unsaved(self):
        return [s for s in self.files if s in self.results and s not in self.saved]

    def _sync_actions(self):
        """Save / Save all / Retry follow the current image and the run."""
        if not hasattr(self, "save_button"):
            return
        cur = self.current
        has = bool(cur and cur in self.results and Path(self.results[cur]).exists())
        unsaved = self._unsaved()
        self.save_button.set_enabled(has and not self.busy)
        self.save_button.set_text("Saved" if has and cur in self.saved else "Save")
        self.save_all_button.set_enabled(bool(unsaved) and not self.busy)
        self.save_all_button.set_text(f"Save all ({len(unsaved)})" if unsaved else "Save all")
        self.retry_button.set_enabled(bool(cur) and not self.busy)
        if not cur:
            note = ""
        elif self.busy:
            note = "Cutting…"
        elif cur in self.saved:
            note = f"Saved as {Path(self.saved[cur]).name}"
        elif has:
            note = "Not saved yet"
        else:
            note = "Not cut yet"
        self.result_note.setText(note)

    def _summary_review(self, done, total):
        self.set_status(f"Done  ·  {done} of {total} cut  ·  review, then Save")
        if done and not self.failures:
            self.notify(f"{done} cutout{'s' if done != 1 else ''} ready  ·  Save the ones you like", "success")
        elif done:
            self.notify(f"{done} of {total} cut  ·  {len(self.failures)} failed ({self.failures[0][0]})", "warning")
        else:
            reason = self.failures[0][1] if self.failures else "unknown error"
            self.notify(f"Couldn't cut that: {reason}", "error")
        if done:
            if not self.current and self.files:
                self.current = self.files[0]
            self.pane_switch.select(1)
        self._refresh_model_note()

    def save_current(self):
        cur = self.current
        if not cur or cur not in self.results:
            self.notify("Cut this image first", "warning")
            return
        self._save([cur])

    def save_all(self):
        items = self._unsaved()
        if not items:
            self.notify("Everything is saved")
            return
        self._save(items)

    def _save(self, sources):
        if self.busy:
            return
        self.finish_crop()
        opts = self._save_opts()
        try:
            Path(opts["out_dir"]).mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self.notify(f"Can't save there: {exc}", "error")
            return
        items = [(s, self.results[s]) for s in sources if s in self.results]

        def go():
            self.busy = True
            self._sync_actions()
            self._sync_queue()
            self.bar.set(0, len(items))
            self._save_delete_mode = self.delete_mode
            threading.Thread(target=save_items, args=(items, opts, self.sig), daemon=True).start()

        if opts["delete"]:
            n = len(items)
            files = f"{n} original file{'s' if n != 1 else ''}"
            if opts["delete"] == "permanent":
                self.confirm("Permanently delete originals?",
                             f"{files} will be erased from this PC once saved. They won't go to the "
                             "Recycle Bin and can't be recovered.", "Save and delete", go, danger=True)
            else:
                self.confirm("Delete originals?", f"{files} will be moved to the Recycle Bin once saved.",
                             "Save and delete", go, danger=True)
            return
        go()

    def _on_saved(self, source, dest, deleted):
        self.saved[source] = dest
        self._mark(source)
        self._sync_actions()

    def _on_save_done(self, saved, total, deleted, error):
        self.busy = False
        self._sync_queue()
        self._sync_actions()
        folder = Path(self.out.text()).name or self.out.text()
        where = "deleted" if getattr(self, "_save_delete_mode", "trash") == "permanent" else "to Recycle Bin"
        extra = f" · {deleted} original{'s' if deleted != 1 else ''} {where}" if deleted else ""
        if saved == total and saved:
            name = Path(self.saved[self.current]).name if total == 1 and self.current in self.saved else None
            text = f"Saved {name} to {folder}" if name else f"Saved {saved} cutouts to {folder}"
            self.notify(text + extra, "success")
            self.set_status(f"Saved {saved} to {folder}")
        elif saved:
            self.notify(f"Saved {saved} of {total} · {error}", "warning")
        else:
            self.notify(f"Couldn't save: {error}", "error")

    def open_retry_menu(self):
        if not self.current or self.busy:
            return
        rows = [(label, note, False) for _n, label, note in core.MODELS]
        alpha = self.flags["alpha"]
        rows.append(("Refine edges " + ("off" if alpha else "on"),
                     "Same model, with edge refinement " + ("off" if alpha else "on"), False))
        self._popup(Menu(rows, self.model_index, self._retry, width=320), self.retry_button)

    def _retry(self, index):
        if index < len(core.MODELS):
            if index != self.model_index:
                self._pick_model(index)
        else:
            self.toggle("alpha", self.chk["alpha"])
        name = core.MODELS[self.model_index][1]
        refine = "  ·  refined edges" if self.flags["alpha"] else ""
        self.notify(f"Retrying with {name}{refine}")
        self._start([self.current])

    def _close_anyway(self):
        self._close_ok = True
        self.close()

    def closeEvent(self, e):
        unsaved = self._unsaved()
        if unsaved and not getattr(self, "_close_ok", False):
            e.ignore()
            n = len(unsaved)
            self.confirm("Close without saving?",
                         f"{n} cutout{'s' if n != 1 else ''} {'haven' if n != 1 else 'hasn'}'t been saved "
                         "and will be lost.", "Close anyway", self._close_anyway, danger=True)
            return
        shutil.rmtree(self.session, ignore_errors=True)
        super().closeEvent(e)

    def _popup(self, menu, anchor):
        if self._menu is not None and self._menu.isVisible():
            self._menu.dismiss()
        pos = anchor.mapToGlobal(anchor.rect().bottomLeft())
        screen = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        m = menu.margin
        if pos.y() + menu.height() > screen.availableGeometry().bottom():
            pos = anchor.mapToGlobal(anchor.rect().topLeft())
            pos.setY(pos.y() - menu.height() + m - 4)
        else:
            pos.setY(pos.y() + 4 - m)
        pos.setX(pos.x() + anchor.glow - m)
        menu.move(pos)
        self._menu = menu          # keep a reference, or Python frees the window at once
        menu.show()

    def open_model_menu(self):
        rows = [(label, note if core.is_downloaded(name) else note + "  ·  not downloaded",
                 not core.is_downloaded(name)) for name, label, note in core.MODELS]
        rows.append(("Manage models…", "Download, update or delete models", False))
        self._popup(Menu(rows, self.model_index, self._pick_model, width=320), self.model_button)

    def open_models(self):
        if self._models is None:
            self._models = ModelsDialog(self)

    def _pick_model(self, index):
        if index >= len(core.MODELS):
            self.open_models()
            return
        self.model_index = index
        name, label, _note = core.MODELS[index]
        self.model_button.set_text(label)
        self._refresh_model_note()
        extra = "" if core.is_downloaded(name) else "  ·  downloads on first run"
        self.notify(f"Model: {label}{extra}")

    def _refresh_model_note(self):
        name, _label, note = core.MODELS[self.model_index]
        ready = core.is_downloaded(name)
        self.model_note.setText(note if ready else note + "  ·  not downloaded")
        self.model_note.role = "ink_soft" if ready else "warn"
        self.model_note.restyle()

    def open_size_menu(self):
        rows = [("Full size", "Keep the original resolution", False)] + \
            [(f"{v} px", "Long edge", False) for v in SIZES]
        current = 0 if self.out_size is None else SIZES.index(self.out_size) + 1
        self._popup(Menu(rows, current, self._pick_size, width=220), self.size_button)

    def _pick_size(self, index):
        self.out_size = None if index == 0 else SIZES[index - 1]
        self.size_button.set_text(self._size_title())
        self._save_export()
        self.notify("Export at full resolution" if self.out_size is None
                    else f"Export with a {self.out_size} px long edge")

    def pick_outdir(self):
        folder = QFileDialog.getExistingDirectory(self, "Save cutouts to", self.out.text())
        if folder:
            self.out.setText(str(Path(folder)))
            save_settings(out_dir=str(Path(folder)))
            self.notify(f"Saving to {Path(folder).name or folder}", "success")

    def open_outdir(self):
        target = Path(self.out.text())
        try:
            target.mkdir(parents=True, exist_ok=True)
            os.startfile(target)
        except Exception as exc:
            self.notify(f"Can't open that folder: {exc}", "error")

    # -- files ------------------------------------------------------------

    def accept_paths(self, paths, quiet=False):
        added = 0
        for raw in paths:
            p = Path(raw)
            if p.is_dir():
                found = [c for c in sorted(p.iterdir()) if c.is_file()
                         and c.suffix.lower() in (core.PIL_NATIVE | core.CONVERTIBLE)]
            elif p.is_file() and p.suffix.lower() in (core.PIL_NATIVE | core.CONVERTIBLE):
                found = [p]
            else:
                found = []
            for c in found:
                if str(c) not in self.files:
                    self.files.append(str(c))
                    self.drop.list.addItem(c.name)
                    added += 1
        if added:
            if not self.drop.list.selectedItems():
                self.drop.list.setCurrentRow(0)
            self.set_status("Ready  ·  hit Remove backgrounds")
            if not quiet:
                self.notify(f"Added {added} image{'s' if added != 1 else ''}", "success")
        elif paths and not quiet:
            self.notify("No new images there (already queued or not a supported format)", "warning")
        self._sync_queue()
        return added

    def add_files(self):
        picked, _ = QFileDialog.getOpenFileNames(
            self, "Choose images", str(self._browse_dir()),
            "Images (" + " ".join(f"*{e}" for e in sorted(core.PIL_NATIVE | core.CONVERTIBLE)) + ");;All files (*.*)")
        if picked:
            self.accept_paths(picked)

    def _browse_dir(self):
        pictures = core.HOME / "Pictures"
        return pictures if pictures.is_dir() else core.HOME

    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder", str(self._browse_dir()))
        if folder:
            self.accept_paths([folder])

    def remove_selected(self):
        lst = self.drop.list
        rows = sorted((lst.row(i) for i in lst.selectedItems()), reverse=True)
        if not rows:
            self.notify("Select an image in the list to remove", "warning")
            return
        # Keep list and file queue in step: selection signals fired mid-removal
        # would otherwise map rows onto the not-yet-updated queue.
        lst.blockSignals(True)
        self.finish_crop(cancel=True)
        for row in rows:
            lst.takeItem(row)
            self.crops.pop(self.files[row], None)
            self.results.pop(self.files[row], None)
            self.saved.pop(self.files[row], None)
            del self.files[row]
        lst.blockSignals(False)
        self.set_status(f"{len(self.files)} queued")
        if self.files:
            lst.setCurrentRow(min(rows[-1], len(self.files) - 1))
            self._on_select()
        else:
            self.current = None
            self._refresh_preview()
        self._sync_queue()
        self.notify(f"Removed {len(rows)} image{'s' if len(rows) != 1 else ''}")

    def clear(self):
        unsaved = self._unsaved()
        if unsaved and not getattr(self, "_clear_ok", False):
            n = len(unsaved)
            self.confirm("Clear unsaved cutouts?",
                         f"{n} cutout{'s' if n != 1 else ''} {'haven' if n != 1 else 'hasn'}'t been saved "
                         "and will be lost.", "Clear anyway", self._clear_anyway, danger=True)
            return
        self._clear_ok = False
        had = len(self.files)
        self.finish_crop(cancel=True)
        self.crops.clear()
        self.saved.clear()
        self.drop.list.clear()
        self.files.clear()
        self.results.clear()
        self.current = None
        self._refresh_preview()
        self.set_status("Ready")
        self._sync_queue()
        if had:
            self.notify("Queue cleared")

    def _clear_anyway(self):
        self._clear_ok = True
        self.clear()

    def _sync_queue(self):
        self.drop.set_empty(not self.files)
        self.run_button.set_enabled(bool(self.files) and not self.busy)
        self._sync_actions()

    def _on_select(self):
        items = self.drop.list.selectedItems()
        if items and self.files[self.drop.list.row(items[0])] != self.current:
            self.finish_crop()               # keep what was drawn on the previous image
        if items:
            self.current = self.files[self.drop.list.row(items[0])]
            self._refresh_preview()
            self._sync_actions()

    # -- drag & drop / paste ---------------------------------------------------

    def dragEnterEvent(self, e):
        md = e.mimeData()
        if md.hasUrls() or md.hasHtml() or md.hasText() or md.hasImage():
            e.acceptProposedAction()
            animate(self.drop, "hot", 1, 160)

    def dragLeaveEvent(self, _):
        animate(self.drop, "hot", 0, 220)

    def dropEvent(self, e):
        animate(self.drop, "hot", 0, 220)
        md = e.mimeData()
        local = [u.toLocalFile() for u in md.urls() if u.isLocalFile() and Path(u.toLocalFile()).exists()]
        if local:
            self.accept_paths(local)
            return
        payload = md.html() if md.hasHtml() else (
            md.text() if md.hasText() else "\n".join(u.toString() for u in md.urls()))
        if payload and core.webdrop.looks_like_web_drop(payload):
            self.fetch_web(payload)
        else:
            self.notify("That drop didn't contain an image", "error")

    def on_paste(self):
        text = QGuiApplication.clipboard().text()
        if text and core.webdrop.looks_like_web_drop(text):
            self.fetch_web(text)
        else:
            self.notify("Nothing to paste: copy an image link first", "warning")

    def fetch_web(self, payload):
        sources, referer = core.webdrop.extract_sources(payload)
        if not sources:
            self.notify("Nothing downloadable in that drop", "error")
            return

        def work():
            saved, failures = [], []
            for i, source in enumerate(sources, start=1):
                label = source if len(source) < 60 else source[:57] + "..."
                self.sig.status.emit(f"Fetching {i} of {len(sources)}  ·  {label}")
                try:
                    saved.append(str(core.webdrop.fetch(source, app_data() / "web-images", referer=referer)))
                except Exception as exc:
                    failures.append(f"{label}: {exc}")
                    continue
                if saved:
                    break
            self.sig.web_done.emit(saved, failures)

        self.set_status("Fetching from the web...")
        self.notify("Downloading image…")
        threading.Thread(target=work, daemon=True).start()

    # -- preview ----------------------------------------------------------

    def set_pane(self, index):
        if index != 0:
            self.finish_crop()
        self.pane = index
        self._refresh_preview()

    def _refresh_preview(self):
        source = self.current
        self.preview.crop_hint = self.crops.get(source) if self.pane == 0 else None
        if not source:
            self.preview.show_message("Add an image to begin")
            return
        try:
            if self.pane == 0:
                with Image.open(source) as im:
                    img = im.convert("RGB").copy()
            else:
                out = self.results.get(source)
                if not out or not Path(out).exists():
                    self.preview.show_message("Not processed yet")
                    return
                with Image.open(out) as im:
                    cut = im.convert("RGBA")
                cut.thumbnail((1600, 1600), Image.LANCZOS)
                # Shown as it will be saved: crop or aspect framing, then the background.
                cut = export_cutout(cut, self.crops.get(source), RATIO[self.ratio_key], None, self.background)
                if self.background is None:
                    img = core.checkerboard(cut.size, square=max(cut.width // 60, 10), dark=T["dark"])
                    img.paste(cut, (0, 0), cut)
                else:
                    img = cut
            img.thumbnail((1600, 1600), Image.LANCZOS)
            self.preview.show_pixmap(QPixmap.fromImage(ImageQt(img.convert("RGBA")).copy()))
        except Exception:
            self.preview.show_message("No preview")

    # -- run --------------------------------------------------------------

    def set_status(self, text):
        self.status.setText(text)
        if self.busy:
            self.preview.caption = text
            self.preview.update()

    def run(self):
        if self.busy or not self.files:
            return
        if not self.autosave:
            self._start()                    # nothing is saved or deleted until Save
            return
        name, label, _note = core.MODELS[self.model_index]
        if self.jobs.job(name) == "download":
            self.notify(f"{label} is still downloading  ·  see Manage models", "warning")
            return
        if self.flags["delete"]:
            n = len(self.files)
            files = f"{n} original file{'s' if n != 1 else ''}"
            if self.delete_mode == "permanent":
                self.confirm("Permanently delete originals?",
                             f"{files} will be erased from this PC after their cutouts are saved. "
                             "They won't go to the Recycle Bin and can't be recovered. Files that fail to save are kept.",
                             "Delete permanently", self._start, danger=True)
            else:
                self.confirm("Delete originals?",
                             f"{files} will be moved to the Recycle Bin after "
                             "their cutouts are saved. Files that fail to save are kept.",
                             "Move to Recycle Bin", self._start, danger=True)
            return
        self._start()

    def _save_opts(self):
        return dict(out_dir=self.out.text().strip(), crops=dict(self.crops), ratio=RATIO[self.ratio_key],
                    size=self.out_size, background=self.background, keep=self.flags["keep"],
                    delete=self.delete_mode if self.flags["delete"] else None)

    def _start(self, files=None, model_index=None):
        files = list(files or self.files)
        autosave = None
        if self.autosave:
            try:
                Path(self.out.text()).mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                self.notify(f"Can't save there: {exc}", "error")
                return
            self.finish_crop()
            autosave = self._save_opts()
        self.busy = True
        self.jobs.changed.emit("")           # the Models panel locks while a run uses a model
        self.failures = []
        self.preview.set_busy(True, "Starting")
        self._sync_queue()
        self.bar.set(0, len(files))
        self.finish_crop()
        self._run_count = len(files)
        self._run_autosave = autosave is not None
        self._run_delete_mode = self.delete_mode
        model = core.MODELS[self.model_index if model_index is None else model_index][0]
        args = (files, model, str(self.session), self.flags["alpha"], self.flags["enhance"], autosave, self.sig)
        threading.Thread(target=process, args=args, daemon=True).start()
        self._sync_actions()

    def _wire_signals(self):
        s = self.sig
        s.status.connect(self.set_status)
        s.progress.connect(lambda v, m: self.bar.set(v, m))
        s.indeterminate.connect(lambda on: self.bar.set(indeterminate=on))
        s.result.connect(self._on_result)
        s.failed.connect(lambda name, err: self.failures.append((name, err)))
        s.summary.connect(self._on_summary)
        s.error.connect(lambda msg: (self.set_status(msg), self.notify(msg, "error")))
        s.done.connect(self._on_done)
        s.web_done.connect(self._on_web_done)
        s.saved.connect(self._on_saved)
        s.save_done.connect(self._on_save_done)

    def _on_result(self, source, dest):
        self.results[source] = dest
        self.saved.pop(source, None)          # a new cut replaces any earlier one
        self._mark(source)
        self._sync_actions()
        if self.current == source and self.pane == 1:
            self._refresh_preview()
        if self.current == source:
            self.pane_switch.select(1)

    def _on_summary(self, done, total, trashed):
        if not getattr(self, "_run_autosave", True):
            self._summary_review(done, total)
            return
        where = "deleted" if getattr(self, "_run_delete_mode", "trash") == "permanent" else "to Recycle Bin"
        msg = f"Done  ·  {done} of {total} cut out"
        if trashed:
            msg += f"  ·  {trashed} original(s) {where}"
        self.set_status(msg)
        folder = Path(self.out.text()).name or self.out.text()
        if done and not self.failures:
            extra = f" · {trashed} original{'s' if trashed != 1 else ''} {where}" if trashed else ""
            self.notify(f"Saved {done} cutout{'s' if done != 1 else ''} to {folder}{extra}", "success")
        elif done:
            self.notify(f"Saved {done} of {total} · {len(self.failures)} failed ({self.failures[0][0]})", "warning")
        else:
            reason = self.failures[0][1] if self.failures else "unknown error"
            self.notify(f"Nothing saved: {reason}", "error")
        if done:
            if not self.current and self.files:
                self.current = self.files[0]
            self.pane_switch.select(1)
        self._refresh_model_note()

    def _on_done(self):
        self.busy = False
        self.preview.set_busy(False)
        self._sync_actions()
        self.jobs.changed.emit("")           # the Models panel unlocks
        self._sync_queue()

    def _on_web_done(self, saved, failures):
        if saved:
            self.accept_paths(saved, quiet=True)
            self.set_status(f"Added {len(saved)} from the web" + (f"  ·  {len(failures)} failed" if failures else ""))
            self.notify("Added image from the web", "success")
        else:
            self.set_status("Could not fetch that image")
            self.notify("Could not download that image", "error")


def prepare_runtime():
    """Somewhere to write for a windowed (and possibly installed, read-only) app:
    numba's compile cache and a log for anything printed, in %LOCALAPPDATA%/Cutout."""
    data = app_data()
    if getattr(sys, "frozen", False):
        # numba keys its compile cache by paths relative to the working directory, so a
        # fixed one lets the installer's warm-up serve every later launch.
        os.chdir(Path(sys.executable).parent)
    os.environ.setdefault("NUMBA_CACHE_DIR", str(data / "numba-cache"))
    if sys.stdout is None or sys.stderr is None:
        # A windowed app has no console; progress bars inside libraries would crash writing to None.
        log = open(data / "cutout.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log


def self_check():
    """`Cutout.exe --check`: load the engine and cut a generated image with edge refinement,
    to diagnose an install. Prints the result (to the log when windowed); exit code 0 = OK."""
    import traceback
    from PIL import ImageDraw
    try:
        from rembg import remove
        began = time.time()
        name = core.MODELS[0][0]
        if not model_store.is_downloaded(name):
            model_store.download(name, lambda *_: None)
        session = core.get_session(name)
        img = Image.new("RGB", (320, 240), (40, 120, 200))
        ImageDraw.Draw(img).ellipse((90, 50, 230, 190), fill=(240, 200, 60))
        out = remove(img, session=session, alpha_matting=True, alpha_matting_foreground_threshold=240,
                     alpha_matting_background_threshold=10, alpha_matting_erode_size=10)
        ok = out.mode == "RGBA" and out.getchannel("A").getextrema() == (0, 255)
        # Formats that need codecs bundled with the app.
        formats = []
        for ext in (".avif", ".heic"):
            if ext not in core.PIL_NATIVE:
                formats.append(f"{ext} unavailable")
                continue
            probe = Path(tempfile.gettempdir()) / f"cutout-check{ext}"
            img.save(probe)
            with Image.open(probe) as back:
                back.load()
                if back.size != img.size:
                    formats.append(f"{ext} unreadable")
            probe.unlink()
        ok = ok and not formats
        print(f"{APP_NAME} {APP_VERSION} self-check: {'OK' if ok else 'FAILED'} "
              f"({name}, {time.time() - began:.1f}s{', ' + ', '.join(formats) if formats else ', avif+heic ok'})",
              flush=True)
        return 0 if ok else 1
    except Exception:
        print(f"{APP_NAME} {APP_VERSION} self-check: ERROR", flush=True)
        traceback.print_exc()
        return 2


def main():
    prepare_runtime()
    if "--check" in sys.argv:
        sys.exit(self_check())
    if "--warmup" in sys.argv:
        # Run by the installer: compile the engine now so the first cut starts straight away.
        # Importing compiles most of it; edge refinement compiles on first use, so run a tiny one.
        try:
            import numpy as np
            import rembg  # noqa: F401
            from pymatting import estimate_alpha_cf, estimate_foreground_ml
            img = np.random.default_rng(0).random((24, 24, 3))
            trimap = np.full((24, 24), 0.5)
            trimap[:6], trimap[-6:] = 0.0, 1.0
            estimate_foreground_ml(img, estimate_alpha_cf(img, trimap))
            (app_data() / "engine-ready").touch()
            sys.exit(0)
        except Exception:
            import traceback
            traceback.print_exc()
            sys.exit(1)
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    try:
        # Its own taskbar identity, so the taskbar shows the Cutout icon rather than Python's.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Cutout.App")
    except Exception:
        pass
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    if (ASSETS / "cutout.ico").exists():
        app.setWindowIcon(QIcon(str(ASSETS / "cutout.ico")))
    global FONT
    if core.INTER.exists():
        fid = QFontDatabase.addApplicationFont(str(core.INTER))
        families = QFontDatabase.applicationFontFamilies(fid)
        if families:
            FONT = families[0]
    app.setFont(font(10))
    window = App()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
