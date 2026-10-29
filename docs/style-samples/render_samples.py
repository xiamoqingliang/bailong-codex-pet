"""Capture isolated, real Qt widgets for two README style samples."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json
import math
import os
import subprocess
import tempfile

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from bridge_loader import load_original_module
original = load_original_module()
from v051_behavior import V051Behavior, configure_preferences
from asset_source import configure_asset_source
from bailong_toolbar import create_toolbar
from activity_cards import create_activity_cards
from companion_layout import observe_speech_layout
from bailong_theme import configure_fonts
from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import QApplication

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[1]
FPS, DURATION = 20, 8
W, H = 1200, 860
AREA = QRect(0, 0, 1100, 620)
app = QApplication.instance() or QApplication(['readme-style-samples'])
app.setQuitOnLastWindowClosed(False)
configure_fonts(app)


def text(painter, x, y, value, size=20, color='#655174', bold=False):
    font = QFont('Microsoft YaHei UI')
    font.setPixelSize(size)
    font.setBold(bold)
    painter.setFont(font)
    painter.setPen(QColor(color))
    painter.drawText(QPointF(x, y), value)


def card(painter, x, y, w, h, fill, stroke=None, radius=18):
    painter.setBrush(QColor(fill))
    painter.setPen(QPen(QColor(stroke), 1) if stroke else Qt.PenStyle.NoPen)
    painter.drawRoundedRect(QRectF(x, y, w, h), radius, radius)


def compose(pet, title, subtitle, footer, callouts=False, pointer=False, tag="风格样张"):
    image = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor('#fbf9ff'))
    painter = QPainter(image)
    painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
    text(painter, 50, 63, title, 34, '#5f447e', True)
    text(painter, 51, 103, subtitle, 19, '#8a789c')
    card(painter, 1010, 35, 140, 38, '#eee5f8', radius=12)
    text(painter, 1032, 61, 'v0.5.1.1', 19, '#84639f', True)
    card(painter, 42, 135, 1116, 642, '#25232d', '#d5c5e4', 22)
    painter.save()
    painter.translate(50, 145)
    painter.setClipRect(AREA)
    windows = [pet.quota_panel, pet.settings_panel, pet.bubble, getattr(pet, 'notification_bubble', None),
               pet.toolbar, pet.activity_cards, pet]
    for widget in windows:
        if widget is not None and widget.isVisible():
            painter.setOpacity(widget.windowOpacity())
            painter.drawPixmap(widget.pos(), widget.grab())
    painter.setOpacity(1)
    if callouts:
        text(painter, 64, 430, '常用操作，集中在这里', 21, '#eee5f6', True)
        text(painter, 64, 465, '聊天 · 额度 · 动作 · 设置', 17, '#b5a6c3')
        painter.setPen(QPen(QColor('#a78cbe'), 1.5))
        painter.drawPolyline([QPoint(280, 480), QPoint(320, 480), QPoint(320, 548), QPoint(406, 548)])
        text(painter, 790, 220, '任务进展，一眼看到', 21, '#eee5f6', True)
        text(painter, 790, 254, '查看状态，点击返回聊天', 17, '#b5a6c3')
        painter.setPen(QPen(QColor('#a78cbe'), 1.5))
        painter.drawPolyline([QPoint(778, 270), QPoint(736, 270), QPoint(736, 210), QPoint(716, 210)])
    if pointer:
        center = QPointF(pet.x() + pet.width() * .54, pet.y() + pet.height() * .55)
        painter.setPen(QPen(QColor('#d9c4ef'), 2))
        painter.setBrush(QColor(217, 196, 239, 28))
        painter.drawEllipse(center, 13, 13)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#f4edf9'))
        painter.drawEllipse(center, 3, 3)
    painter.restore()
    text(painter, 50, 820, footer, 18, '#8a789c')
    text(painter, 1027, 820, tag, 17, '#a08caf')
    painter.end()
    return image


def smooth(t):
    t = max(0, min(1, t))
    return t * t * (3 - 2 * t)


def pet_position(t):
    if t < 1:
        return 422, False, '右侧显示'
    if t < 2.2:
        return round(422 + (724-422) * smooth((t-1)/1.2)), True, '拖向右侧 · 空间不足时换到左边'
    if t < 3.5:
        return 724, False, '收回，再从另一侧展开'
    if t < 5.3:
        return round(724 + (120-724) * smooth((t-3.5)/1.8)), True, '拖向左侧 · 窗口自动向右换侧'
    if t < 6.1:
        return 120, False, '保持间距 · 完整显示设置内容'
    if t < 7.3:
        return round(120 + (422-120) * smooth((t-6.1)/1.2)), True, '同侧移动 · 跟随白龙的位置'
    return 422, False, '窗口换边 · 收回、换侧、展开'



def main():
    with tempfile.TemporaryDirectory(prefix='BailongStyleSample-') as temporary:
        configure_preferences(original, Path(temporary) / 'preferences.ini')
        configure_asset_source(original)
        class Pet(V051Behavior, original.Pet):
            toolbar = None
            exit_reason = None
            _ui_motion_disabled = True
        pet = Pet()
        pet.tick.stop()
        pet.visibility_timer.stop()
        pet.desktop_sync.stop()
        pet.sound_disabled = True
        pet.toolbar = create_toolbar(original, pet)
        pet.activity_cards = create_activity_cards(original, pet)
        observe_speech_layout(pet)
        screen = SimpleNamespace(availableGeometry=lambda: AREA, name=lambda: 'style-sample', devicePixelRatio=lambda: 1)
        with patch.object(pet, 'screen', return_value=screen), \
             patch.object(QApplication, 'screenAt', side_effect=lambda point: screen if AREA.contains(point) else None), \
             patch.object(QApplication, 'screens', return_value=[screen]):
            pet.move(422, 260)
            pet.play(pet.idles[0], 'idle')
            pet.show()
            pet.activity_cards.set_sessions([
                {'key': 'sample-docs', 'project': '白龙示例项目', 'title': '整理项目说明', 'state': 'running'},
                {'key': 'sample-ui', 'project': '界面预览', 'title': '生成界面预览图', 'state': 'running'},
                {'key': 'sample-check', 'project': '本地检查', 'title': '检查任务进展', 'state': 'running'},
            ])
            pet.activity_cards.set_active(True)
            pet.toolbar.set_active(True)
            pet.toolbar.sync_geometry()
            app.processEvents()
            for widget in (pet.toolbar, pet.activity_cards):
                if hasattr(widget, '_reveal_animation'):
                    widget._reveal_animation.setCurrentTime(widget._reveal_animation.duration())
                widget._ui_motion.finish()
            pet.index = 12
            still = compose(pet, '白龙、控制栏与任务卡', '完整动画 · 同会话聊天 · 跨项目任务状态',
                            '白紫配色与简洁标注 · 界面使用示例任务', callouts=True)
            assert still.save(str(OUT / '静态样张-功能总览.png'))

            pet.activity_cards.set_sessions([{'key': 'sample-motion', 'project': '白龙示例项目',
                                             'title': '整理项目说明', 'state': 'running'}])
            pet.show_settings()
            app.processEvents()
            assert pet.settings_panel.isVisible()
            for motion in pet._ui_motions:
                motion.finish()
            pet._ui_motion_disabled = False
            now = [1000.0]
            for motion in pet._ui_motions:
                motion.clock = lambda: now[0]
            timelines = {}
            swap_count = 0
            seen_swaps = set()
            frame_dir = Path(temporary) / 'frames'
            frame_dir.mkdir()
            for frame_index in range(FPS * DURATION):
                t = frame_index / FPS
                now[0] = 1000 + t
                for motion in list(pet._ui_motions):
                    if motion.animation is not None:
                        animation = motion.animation
                        key = id(animation)
                        start = timelines.setdefault(key, t)
                        animation.setCurrentTime(min(animation.duration(), round((t-start)*1000)))
                x, dragging, caption = pet_position(t)
                pet.dragging = dragging
                pet.move(x, 260)
                pet.toolbar.sync_geometry()
                for motion in list(pet._ui_motions):
                    if motion.animation is not None:
                        key = id(motion.animation)
                        timelines.setdefault(key, t)
                        if motion.role == 'settings' and motion.kind == 'swap' and key not in seen_swaps:
                            seen_swaps.add(key)
                            swap_count += 1
                pet.index = int(t * 24) % len(pet.frames)
                image = compose(pet, '窗口换边，自然跟随', '跟随白龙移动，空间不足时切换到另一侧',
                                caption, pointer=dragging)
                assert image.save(str(frame_dir / f'frame-{frame_index:03}.png'))
                if frame_index in (28, 42, 80, 100):
                    image.save(str(OUT / f'动效检查帧-{frame_index}.png'))
            pet.dragging = False
            assert swap_count >= 2, swap_count
            command = [str(ROOT / 'tools/ffmpeg.exe'), '-hide_banner', '-loglevel', 'error', '-y',
                       '-framerate', str(FPS), '-i', str(frame_dir / 'frame-%03d.png'),
                       '-filter_complex', '[0:v]split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=sierra2_4a',
                       '-loop', '0', str(OUT / '动态样张-窗口换边.gif')]
            subprocess.run(command, check=True, timeout=90, creationflags=subprocess.CREATE_NO_WINDOW)
            report = {'static': '静态样张-功能总览.png', 'animation': '动态样张-窗口换边.gif',
                      'size': [W, H], 'fps': FPS, 'durationSeconds': DURATION, 'actualUi': True,
                      'settingsSwapTimelines': swap_count, 'isolatedPreferences': True,
                      'modelRequests': 0, 'githubUploaded': False}
            (OUT / 'samples.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        pet.exit_reason = 'sample-complete'
        pet.close()
        app.processEvents()
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
