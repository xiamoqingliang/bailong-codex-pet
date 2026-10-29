"""Shared palette for companion controls, menus and preferences."""
SCROLLBAR_STYLE = '''
QScrollBar:vertical { background:#f0eaf8; width:9px; margin:2px 0; border:none; border-radius:4px; }
QScrollBar::handle:vertical { background:#c7b6e5; min-height:26px; border-radius:4px; }
QScrollBar::handle:vertical:hover { background:#aa8ac6; }
QScrollBar::handle:vertical:pressed { background:#9473b7; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; border:none; background:transparent; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background:transparent; }
QScrollBar:horizontal { background:#f0eaf8; height:10px; margin:0 2px; border:none; border-radius:5px; }
QScrollBar::handle:horizontal { background:#c7b6e5; min-width:28px; border-radius:5px; }
QScrollBar::handle:horizontal:hover { background:#aa8ac6; }
QScrollBar::handle:horizontal:pressed { background:#9473b7; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width:0; border:none; background:transparent; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background:transparent; }
QAbstractScrollArea::corner { background:#fbf9ff; border:none; }
'''


def reveal_window(widget, visible):
    """Fade in once per reveal; polling never restarts the transition."""
    from PySide6.QtCore import QPropertyAnimation, QEasingCurve
    from PySide6.QtWidgets import QGraphicsOpacityEffect
    from ui_motion import OPEN_MS, CLOSE_MS
    if not hasattr(widget, '_reveal_animation'):
        effect = QGraphicsOpacityEffect(widget)
        effect.setOpacity(1.0)
        widget.setGraphicsEffect(effect)
        widget._reveal_effect = effect
        widget._reveal_animation = QPropertyAnimation(effect, b'opacity', widget)
        widget._reveal_animation.setDuration(OPEN_MS)
        widget._reveal_animation.setEasingCurve(QEasingCurve.Type.InOutSine)
        widget._reveal_target = widget.isVisible()
        def finish():
            if not widget._reveal_target:
                widget.hide()
        widget._reveal_animation.finished.connect(finish)
    if visible == widget._reveal_target and (widget.isVisible() or not visible):
        return
    widget._reveal_target = visible
    widget._reveal_animation.stop()
    widget._reveal_animation.setDuration(OPEN_MS if visible else CLOSE_MS)
    widget._reveal_animation.setEasingCurve(QEasingCurve.Type.OutCubic if visible else QEasingCurve.Type.InCubic)
    if visible:
        if not widget.isVisible():
            widget._reveal_effect.setOpacity(0.0)
        widget.show()
    elif not widget.isVisible():
        widget._reveal_effect.setOpacity(0.0)
        return
    widget._reveal_animation.setStartValue(widget._reveal_effect.opacity())
    widget._reveal_animation.setEndValue(1.0 if visible else 0.0)
    motion = getattr(widget, '_ui_motion', None)
    if not visible and motion is not None:
        motion.retract_for_fade()
    widget._reveal_animation.start()


def configure_fonts(app):
    from pathlib import Path
    import os
    from PySide6.QtGui import QFont, QFontDatabase
    if not QFontDatabase.families():
        for name in ('msyh.ttc', 'msyhbd.ttc'):
            path = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / name
            if path.is_file():
                QFontDatabase.addApplicationFont(str(path))
    app.setFont(QFont('Microsoft YaHei UI', 9))
    if SCROLLBAR_STYLE not in app.styleSheet():
        app.setStyleSheet(app.styleSheet() + SCROLLBAR_STYLE)


MENU_STYLE = '''
QMenu { background:#fbf9ff; color:#574369; border:1px solid #c7b6e5; padding:5px; }
QMenu::item { padding:6px 24px 6px 12px; border-radius:5px; }
QMenu::item:selected { background:#e5d8f4; }
QMenu::separator { background:#ded2ed; height:1px; margin:4px; }
QMenu::item:disabled { color:#a398ac; }
'''
PANEL_STYLE = '''
QFrame#SettingsPanel { background:#fbf9ff; color:#574369;
    border:1px solid #c7b6e5; border-radius:15px; }
QLabel { color:#574369; background:transparent; border:none; }
QLabel[section="true"] { color:#6b4d91; font-weight:600; }
QPushButton { background:#f0eaf8; color:#574369; border:1px solid transparent;
    border-radius:8px; padding:6px 12px; }
QPushButton:hover { background:#e5d8f4; }
QPushButton:pressed { background:#d9c7ed; }
QPushButton:focus { border:1px solid #9473b7; }
QCheckBox { color:#574369; spacing:8px; padding:5px 0; }
QCheckBox::indicator { width:16px; height:16px; border:1px solid #b09ac9;
    border-radius:5px; background:#fff; }
QCheckBox::indicator:checked { background:#9473b7; border:3px solid #d9c7ed; }
QSlider::groove:horizontal { height:6px; background:#e5d8f4; border-radius:3px; }
QSlider::sub-page:horizontal { background:#aa8ac6; border-radius:3px; }
QSlider::handle:horizontal { background:#9473b7; width:16px; margin:-5px 0; border-radius:8px; }
QScrollArea { background:transparent; border:none; }
''' + SCROLLBAR_STYLE
