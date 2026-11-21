"""
CAPRI // LIVING SIGNAL Motion Controller.
Centralizes timing, restrained OutCubic/OutQuart easing, animation lifetime,
clean QGraphicsOpacityEffect teardown, and persisted reduced-motion handling.
"""

from __future__ import annotations

import os
from typing import Callable, Optional

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QObject,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    QRect,
)
from PySide6.QtWidgets import QGraphicsOpacityEffect, QWidget


class MotionTokens:
    MICRO_MS = 120
    MESSAGE_MS = 165
    DISCLOSURE_MS = 180
    DRAWER_MS = 195
    CURVE_OUT = QEasingCurve.Type.OutCubic
    CURVE_SNAP = QEasingCurve.Type.OutQuart


def env_reduced_motion() -> bool:
    val = os.environ.get("CAPRI_REDUCED_MOTION", "").strip().lower()
    return val in ("1", "true", "yes", "on")


class MotionController(QObject):
    """Owns and coordinates state-driven UI transitions for a window."""

    def __init__(self, parent: Optional[QObject] = None, reduced_motion: bool = False):
        super().__init__(parent)
        self._reduced_motion = bool(reduced_motion)
        self._active: dict[str, QAbstractAnimation] = {}

    @property
    def reduced_motion(self) -> bool:
        return self._reduced_motion or env_reduced_motion()

    def set_reduced_motion(self, enabled: bool) -> None:
        self._reduced_motion = bool(enabled)
        if self.reduced_motion:
            self.stop_all()

    @property
    def is_instant(self) -> bool:
        """True when transitions should snap immediately (reduced motion or headless tests)."""
        return self.reduced_motion or os.environ.get("QT_QPA_PLATFORM") == "offscreen"

    def stop(self, key: str) -> None:
        anim = self._active.pop(key, None)
        if anim is not None:
            try:
                anim.stop()
                anim.deleteLater()
            except RuntimeError:
                pass

    def stop_all(self) -> None:
        for key in list(self._active.keys()):
            self.stop(key)

    def fade_in(
        self,
        widget: QWidget,
        duration: int = MotionTokens.MESSAGE_MS,
        key: Optional[str] = None,
    ) -> None:
        """Short restrained entrance for newly arriving live messages, cleaning up effect afterwards."""
        if not widget:
            return
        if self.is_instant:
            try:
                if widget.graphicsEffect() is not None:
                    widget.setGraphicsEffect(None)
            except RuntimeError:
                pass
            return

        anim_key = key or f"fade:{id(widget)}"
        self.stop(anim_key)
        try:
            effect = QGraphicsOpacityEffect(widget)
            effect.setOpacity(0.0)
            widget.setGraphicsEffect(effect)

            anim = QPropertyAnimation(effect, b"opacity", self)
            anim.setDuration(duration)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(MotionTokens.CURVE_OUT)

            def _cleanup():
                self._active.pop(anim_key, None)
                try:
                    # Detach QGraphicsOpacityEffect so Qt does not retain offscreen pixmap caches
                    widget.setGraphicsEffect(None)
                except RuntimeError:
                    pass

            anim.finished.connect(_cleanup)
            self._active[anim_key] = anim
            anim.start()
        except Exception:
            try:
                widget.setGraphicsEffect(None)
            except Exception:
                pass

    def animate_drawer_open(
        self,
        panel: QWidget,
        backdrop: QWidget,
        target_width: int,
        host_height: int,
    ) -> None:
        """Slide drawer in and fade backdrop in together without touching chat layout."""
        self.stop("drawer")
        end_rect = QRect(0, 0, target_width, host_height)
        backdrop.setGeometry(0, 0, backdrop.parentWidget().width() if backdrop.parentWidget() else target_width, host_height)
        backdrop.show()
        backdrop.raise_()

        if self.is_instant:
            try:
                if backdrop.graphicsEffect() is not None:
                    backdrop.setGraphicsEffect(None)
            except RuntimeError:
                pass
            panel.setGeometry(end_rect)
            panel.show()
            panel.raise_()
            return

        start_rect = QRect(-min(44, target_width // 4), 0, target_width, host_height)
        panel.setGeometry(start_rect)
        panel.show()
        panel.raise_()

        group = QParallelAnimationGroup(self)

        slide = QPropertyAnimation(panel, b"geometry", group)
        slide.setDuration(MotionTokens.DRAWER_MS)
        slide.setStartValue(start_rect)
        slide.setEndValue(end_rect)
        slide.setEasingCurve(MotionTokens.CURVE_SNAP)
        group.addAnimation(slide)

        bd_effect = QGraphicsOpacityEffect(backdrop)
        bd_effect.setOpacity(0.0)
        backdrop.setGraphicsEffect(bd_effect)
        fade = QPropertyAnimation(bd_effect, b"opacity", group)
        fade.setDuration(MotionTokens.DRAWER_MS)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        fade.setEasingCurve(MotionTokens.CURVE_OUT)
        group.addAnimation(fade)

        def _done():
            self._active.pop("drawer", None)
            try:
                backdrop.setGraphicsEffect(None)
                panel.setGeometry(end_rect)
            except RuntimeError:
                pass

        group.finished.connect(_done)
        self._active["drawer"] = group
        group.start()

    def animate_drawer_close(
        self,
        panel: QWidget,
        backdrop: QWidget,
        on_finished: Optional[Callable[[], None]] = None,
    ) -> None:
        """Synchronized drawer dismissal."""
        self.stop("drawer")
        if self.is_instant:
            try:
                backdrop.setGraphicsEffect(None)
            except RuntimeError:
                pass
            backdrop.hide()
            panel.hide()
            if on_finished:
                on_finished()
            return

        start_rect = panel.geometry()
        end_rect = QRect(-min(40, max(24, start_rect.width() // 4)), start_rect.y(), start_rect.width(), start_rect.height())
        group = QParallelAnimationGroup(self)

        slide = QPropertyAnimation(panel, b"geometry", group)
        slide.setDuration(MotionTokens.MICRO_MS + 40)
        slide.setStartValue(start_rect)
        slide.setEndValue(end_rect)
        slide.setEasingCurve(MotionTokens.CURVE_OUT)
        group.addAnimation(slide)

        bd_effect = QGraphicsOpacityEffect(backdrop)
        bd_effect.setOpacity(1.0)
        backdrop.setGraphicsEffect(bd_effect)
        fade = QPropertyAnimation(bd_effect, b"opacity", group)
        fade.setDuration(MotionTokens.MICRO_MS + 40)
        fade.setStartValue(1.0)
        fade.setEndValue(0.0)
        fade.setEasingCurve(MotionTokens.CURVE_OUT)
        group.addAnimation(fade)

        def _done():
            self._active.pop("drawer", None)
            try:
                backdrop.setGraphicsEffect(None)
                backdrop.hide()
                panel.hide()
            except RuntimeError:
                pass
            if on_finished:
                on_finished()

        group.finished.connect(_done)
        self._active["drawer"] = group
        group.start()

    def animate_disclosure(self, target: QWidget, show: bool) -> None:
        """Reveal or collapse disclosure details without layout jumps."""
        key = f"disclosure:{id(target)}"
        self.stop(key)
        if not show:
            target.hide()
            try:
                target.setGraphicsEffect(None)
            except RuntimeError:
                pass
            return

        target.show()
        if self.is_instant:
            try:
                target.setGraphicsEffect(None)
            except RuntimeError:
                pass
            return

        self.fade_in(target, duration=MotionTokens.DISCLOSURE_MS, key=key)
