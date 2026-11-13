"""Render the documented architecture as editable SVG and standalone PNG."""
from pathlib import Path
import html
import math
import os

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from bridge_loader import load_original_module
load_original_module()
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(['bailong-diagrams'])
from bailong_theme import configure_fonts
configure_fonts(app)
OUT = Path(__file__).resolve().parent / 'diagrams'
OUT.mkdir(exist_ok=True)
INK, MUTED, LINE = '#55416b', '#88769b', '#a78ac1'


class Drawing:
    def __init__(self, name, width, height, title, subtitle):
        self.name, self.width, self.height = name, width, height
        self.image = QImage(width, height, QImage.Format.Format_ARGB32)
        self.image.fill(QColor('#fbf9ff'))
        self.p = QPainter(self.image)
        self.p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
                    f'<rect width="{width}" height="{height}" fill="#fbf9ff"/>']
        self.text(60, 70, title, 43, True)
        self.text(60, 118, subtitle, 23, color=MUTED)

    def text(self, x, baseline, text, size=25, bold=False, color=INK, center=False, max_width=None):
        font = QFont('Microsoft YaHei UI')
        font.setPixelSize(size)
        font.setBold(bold)
        self.p.setFont(font)
        while max_width and self.p.fontMetrics().horizontalAdvance(text) > max_width and size > 17:
            size -= 1
            font.setPixelSize(size)
            self.p.setFont(font)
        self.p.setPen(QColor(color))
        left = x - self.p.fontMetrics().horizontalAdvance(text) / 2 if center else x
        self.p.drawText(QPointF(left, baseline), text)
        anchor = 'middle' if center else 'start'
        self.svg.append(f'<text x="{x}" y="{baseline}" font-family="Microsoft YaHei UI,Microsoft YaHei,sans-serif" font-size="{size}" font-weight="{600 if bold else 400}" text-anchor="{anchor}" fill="{color}">{html.escape(text)}</text>')

    def box(self, x, y, w, h, title, lines=(), fill='#ffffff', size=23):
        self.p.setPen(QPen(QColor('#cbb9df'), 2))
        self.p.setBrush(QColor(fill))
        self.p.drawRoundedRect(QRectF(x, y, w, h), 22, 22)
        self.svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="22" fill="{fill}" stroke="#cbb9df" stroke-width="2"/>')
        self.text(x + 25, y + 44, title, 28, True, max_width=w-50)
        for i, line in enumerate(lines):
            self.text(x + 25, y + 84 + i * 34, line, size, max_width=w-50)

    def line(self, points, label=None, label_at=None, color=LINE):
        self.p.setBrush(Qt.BrushStyle.NoBrush)
        self.p.setPen(QPen(QColor(color), 3))
        for a, b in zip(points, points[1:]):
            self.p.drawLine(QPointF(*a), QPointF(*b))
        a, b = points[-2:]
        angle = math.atan2(b[1] - a[1], b[0] - a[0])
        arrow = [b, (b[0] - 13 * math.cos(angle - .45), b[1] - 13 * math.sin(angle - .45)),
                 (b[0] - 13 * math.cos(angle + .45), b[1] - 13 * math.sin(angle + .45))]
        self.p.setBrush(QColor(color))
        self.p.drawPolygon(QPolygonF([QPointF(*point) for point in arrow]))
        coords = ' '.join(f'{x},{y}' for x, y in points)
        triangle = ' '.join(f'{x},{y}' for x, y in arrow)
        self.svg.extend([f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="3" stroke-linejoin="round"/>',
                         f'<polygon points="{triangle}" fill="{color}"/>'])
        if label:
            self.text(*label_at, label, 22, color=MUTED)

    def diamond(self, cx, cy, w, h, lines):
        points = [(cx, cy-h/2), (cx+w/2, cy), (cx, cy+h/2), (cx-w/2, cy)]
        self.p.setPen(QPen(QColor(LINE), 2))
        self.p.setBrush(QColor('#eee5f8'))
        self.p.drawPolygon(QPolygonF([QPointF(*point) for point in points]))
        self.svg.append('<polygon points="'+' '.join(f'{x},{y}' for x,y in points)+'" fill="#eee5f8" stroke="'+LINE+'" stroke-width="2"/>')
        for i, line in enumerate(lines):
            self.text(cx, cy - (len(lines)-1)*18 + i*36 + 8, line, 25, True, center=True)

    def save(self):
        self.p.end()
        assert self.image.save(str(OUT / (self.name + '.png')))
        (OUT / (self.name + '.svg')).write_text('\n'.join(self.svg + ['</svg>']), encoding='utf-8')


d = Drawing('项目架构图-v0.5.1.1', 1680, 1650, '白龙桌宠 · 项目架构', 'v0.5.1.1  /  原作者引擎 v0.5.1  /  Windows x64  /  按当前实现整理')
d.box(60, 160, 480, 130, '安装与交付', ['一键安装 / 一键卸载', '注册入口 · 自启动 · 归属与回滚'])
d.box(600, 160, 480, 130, 'Codex Desktop', ['宠物选择与显隐 · 现有聊天', '任务状态 · 本地会话数据库'])
d.box(1140, 160, 480, 130, '运行环境与资源', ['内置 Python / PySide6 / FFmpeg', '35 段去噪 WebM · 音效'])
d.box(60, 410, 480, 180, '① 状态观察器 · PowerShell', ['codex_observer.ps1', '观察进程、选中宠物及 Mini', '写入 codex-state.json'], '#f1ebf8')
d.box(600, 410, 480, 180, '② 监督进程 · Python', ['bridge_supervisor.py', '单实例 · 启停判断 · 故障恢复', '原生 Mini 遮罩与显隐协同'], '#f1ebf8')
d.box(1140, 410, 480, 180, '③ 白龙界面进程 · Python / Qt', ['bridge_pet.py', '独立桌宠窗口与附属界面', '跟随 Codex 状态与用户操作'], '#f1ebf8')
d.line([(300, 290), (300, 320), (740, 320), (740, 410)], '注册 / 自启动', (390, 309))
d.line([(840, 290), (840, 355), (300, 355), (300, 410)], '只读观察', (385, 390))
d.line([(540, 500), (600, 500)])
d.line([(1080, 500), (1140, 500)])
d.line([(1380, 290), (1380, 410)], '加载', (1400, 360))
d.box(60, 700, 1560, 155, '适配与渲染核心', ['bridge_loader：从原作者 EXE 加载引擎；asset_source：校验并选择动画资源', 'V051Behavior：固定桌宠 / 用户与自动移动区分 / 互动 / 额度 / 任务提醒'], '#eee5f8', 25)
d.line([(1380, 590), (1380, 700)])
for x in (300, 840, 1380):
    d.line([(x, 855), (x, 950)])
d.box(60, 950, 480, 220, '聊天与历史', ['desktop_chat_link / desktop_ipc', '本地 IPC → 同一 Codex 聊天', 'conversation_sync → 历史同步', 'file_diff → 原文件 / 新文件对比'], size=22)
d.box(600, 950, 480, 220, '任务与额度', ['原作者 DesktopTaskSync → 动作', 'activity_source / runtime → 任务卡', '原作者 QuotaWorker → 额度查询', '本地任务状态只读采集'], size=22)
d.box(1140, 950, 480, 220, '窗口策略与动效', ['window_policy → 层级 / 互斥', 'companion_layout → 定位 / 避让', 'ui_motion → 0.5 倍速过渡', 'bailong_theme → 白紫统一样式'], size=22)
for x in (300, 840, 1380):
    d.line([(x, 1170), (x, 1250)])
d.box(60, 1250, 1560, 145, '呈现层 · PySide6 窗口', ['白龙本体 / 控制栏 / 任务卡 / 互动消息框 / 任务消息提示', '额度面板 / 聊天小窗 / 独立历史窗口 / 固定尺寸设置面板'], '#f1ebf8', 25)
d.box(60, 1465, 1560, 130, '数据边界与持久化', ['偏好：QSettings  ·  布局与运行状态：本机文件  ·  安装归属：receipt 与文件哈希', '任务轮询与历史读取不发送新消息；仅用户明确提交聊天后，交给 Codex 执行。'], size=24)
d.save()

d = Drawing('运行流程图-v0.5.1.1', 1600, 2340, '白龙桌宠 · 运行与交付流程', 'v0.5.1.1  /  安装 → 状态判断 → 交互循环 → 卸载')
d.box(500, 155, 600, 90, '完整解压安装包，运行一键安装', fill='#eee5f8')
d.line([(800, 245), (800, 300)])
d.box(500, 300, 600, 130, '校验并注册白龙', ['检查资源 / 备份相关配置 / 写入归属', '设置宠物入口与自启动；旧版安全接管'])
d.line([(800, 430), (800, 485)])
d.box(500, 485, 600, 100, '监督进程 + 状态观察器', ['持续观察 Codex 与 Mini 状态'])
d.line([(800, 585), (800, 650)])
d.diamond(800, 740, 560, 180, ['Codex 正在运行，且', '选中白龙并允许显示？'])
d.line([(1080, 740), (1220, 740)], '否', (1120, 722))
d.box(1220, 680, 310, 120, '等待 / 收起', ['等待启动或选择变化'], size=21)
d.line([(1375, 680), (1375, 535), (1100, 535)])
d.line([(800, 830), (800, 940)], '是', (820, 882))
d.box(500, 940, 600, 140, '按需启动 / 复用白龙界面', ['加载原作者引擎与 35 段动画', '读取偏好，创建控件并同步显隐'])
d.line([(800, 1080), (800, 1140), (280, 1140), (280, 1220)])
d.line([(800, 1080), (800, 1220)], '并行处理', (825, 1168))
d.line([(800, 1080), (800, 1140), (1320, 1140), (1320, 1220)])
d.box(60, 1220, 440, 200, '任务变化', ['读取本地任务状态', '更新任务卡与任务动作', '发送进展 / 完成消息提示'], fill='#f1ebf8', size=24)
d.box(580, 1220, 440, 200, '用户交互', ['点击互动 / 聊天 / 查询额度', '聊天提交到现有 Codex 会话', '回复、历史与额度同步显示'], fill='#f1ebf8', size=23)
d.box(1100, 1220, 440, 200, '设置与移动', ['保存大小、音效等偏好', '固定桌宠：自发移动原地播', '用户拖拽 / 手动移动仍有效'], fill='#f1ebf8', size=23)
for x in (280, 800, 1320):
    d.line([(x, 1420), (x, 1490), (800, 1490), (800, 1545)])
d.box(100, 1545, 1400, 145, '统一更新窗口与动画', ['处理层级、设置互斥、左右 / 上下定位与避让；换边采用收回后展开的 0.5 倍速动效', '额度结果按有效期收起；任务卡、互动消息框及任务消息提示各按规则显示'], '#eee5f8', 24)
d.line([(800, 1690), (800, 1760)])
d.diamond(800, 1840, 480, 160, ['用户发起卸载？'])
d.line([(560, 1840), (30, 1840), (30, 535), (500, 535)], '否：继续观察与交互', (75, 1810))
d.line([(800, 1920), (800, 2020)], '是', (820, 1973))
d.box(400, 2020, 800, 145, '运行一键卸载白龙', ['核对归属 / 停止白龙助手 / 还原相关选择 / 清理自启动', '保留聊天记录、其他桌宠及用户后续设置'], size=25)
d.line([(800, 2165), (800, 2230)])
d.box(500, 2230, 600, 80, '卸载完成，可移走解压目录', fill='#eee5f8')
d.save()
print(str(OUT))
