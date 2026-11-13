"""Render the documented architecture as editable SVG and standalone PNG."""
from pathlib import Path
import html
import math
import os
import re

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
VERSION = '0.5.1.2'
source = OUT.parent / f'项目架构与流程-v{VERSION}.md'
mermaid = re.findall(r'```mermaid\n(.*?)\n```', source.read_text(encoding='utf-8-sig'), re.S)
if len(mermaid) != 2:
    raise ValueError('Architecture document must contain exactly two Mermaid diagrams')
for name, graph in zip(('项目架构图', '运行流程图'), mermaid):
    (OUT / f'{name}-v{VERSION}.mmd').write_text(graph + '\n', encoding='utf-8')
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


d = Drawing(f'项目架构图-v{VERSION}', 1680, 1980, '白龙桌宠 · 项目架构', f'v{VERSION}  /  原作者引擎 v0.5.1  /  Windows x64  /  按当前实现整理')
d.box(60, 160, 480, 150, '安装与交付', ['一键安装 / 一键卸载', '精确入口 · 自启动 · 归属与回滚'])
d.box(600, 160, 480, 150, 'Codex Desktop', ['宠物选择与智能伙伴显隐', '当前 owner · 现有聊天 · 本地状态'])
d.box(1140, 160, 480, 150, '运行环境与资源', ['内置 Python / PySide6 / FFmpeg', '原作者 v0.5.1 · 35 段 WebM'])
d.box(60, 420, 480, 230, '① 状态观察器 · PowerShell', ['codex_observer.ps1', '当前 owner 的精确宠物迁移 ID', '识别“智能伙伴”等固定标签', '观察宿主 / Mini → 状态文件'], '#f1ebf8', 22)
d.box(600, 420, 480, 230, '② 监督进程 · Python', ['bridge_supervisor.py', '单实例 · 宿主代际 · 按需启动', '退出期限 20 秒：仅回收持有的 worker', '原生 Mini 遮罩与显隐协同'], '#f1ebf8', 22)
d.box(1140, 420, 480, 230, '③ 白龙界面进程 · Python / Qt', ['bridge_pet.py / pet_lifecycle.py', '原作者引擎与透明动画', '首次退出启用 15 秒独立看门狗', '跟随宿主与明确用户操作'], '#f1ebf8', 22)
d.line([(300, 310), (300, 340), (740, 340), (740, 420)], '注册 / 自启动', (390, 329))
d.line([(840, 310), (840, 375), (300, 375), (300, 420)], '只读观察', (385, 407))
d.line([(540, 535), (600, 535)])
d.line([(1080, 535), (1140, 535)])
d.line([(1380, 310), (1380, 420)], '加载', (1400, 375))
d.box(60, 760, 1560, 160, '适配与渲染核心', ['bridge_loader / asset_source：加载原作者 EXE 引擎、校验动画与资源', 'V051Behavior：固定桌宠 / 独显偏好 / 用户与自动移动区分 / 互动与额度'], '#eee5f8', 25)
d.line([(1380, 650), (1380, 760)])
for x in (300, 840, 1380):
    d.line([(x, 920), (x, 1030)])
d.box(60, 1030, 480, 240, '聊天与历史', ['desktop_chat_link / desktop_ipc', '本地 IPC → 同一 Codex 聊天', 'conversation_sync → 历史同步', 'file_diff → 原文件 / 新文件对比'], size=22)
d.box(600, 1030, 480, 240, '任务与额度', ['原作者 DesktopTaskSync → 动作', 'activity_source / runtime → 状态', 'activity_cards → 跨项目任务卡', '原作者 QuotaWorker → 额度查询'], size=22)
d.box(1140, 1030, 480, 240, '窗口策略与动效', ['window_policy → role 显示许可', '独显：隐藏常驻层与自动提示', '右键 / 托盘 → 手动展开层', 'companion_layout / ui_motion → 定位动效'], size=22)
for x in (300, 840, 1380):
    d.line([(x, 1270), (x, 1350)])
d.box(60, 1350, 1560, 160, '呈现层 · PySide6 窗口', ['白龙本体 / 控制栏 / 任务卡 / 台词与任务提示 / 额度 / 聊天 / 历史 / 设置', '独显模式默认仅本体；保留右键与托盘，主动打开的聊天、历史、额度及设置仍可使用'], '#f1ebf8', 25)
d.box(60, 1580, 1560, 145, '数据边界与持久化', ['QSettings：固定 / 独显等偏好  ·  本机文件：布局与状态  ·  receipt 与文件哈希：安装归属', '迁移 ID 仅来自当前 owner；无 owner 字段才兼容旧结构，不按名称或任意 ID 推断。'], size=24)
d.box(60, 1785, 1560, 145, '退出与恢复边界', ['首次退出：界面看门狗 15 秒；可信 closing / stopped 或宿主关闭换代：监督期限 20 秒', '仅回收监督进程持有的白龙子进程；保留退出原因与宿主代际，旧退出不隐藏新宿主的白龙。'], '#eee5f8', 24)
d.save()

d = Drawing(f'运行流程图-v{VERSION}', 1600, 2640, '白龙桌宠 · 运行与交付流程', f'v{VERSION}  /  安装 → 身份与显隐 → 交互循环 → 有界退出 → 卸载')
d.box(500, 155, 600, 90, '完整解压安装包，运行一键安装', fill='#eee5f8')
d.line([(800, 245), (800, 300)])
d.box(500, 300, 600, 150, '校验并注册白龙', ['检查资源 / 备份相关配置 / 写入归属', '精确宠物入口与自启动；旧版安全接管'])
d.line([(800, 450), (800, 505)])
d.box(500, 505, 600, 145, '监督进程 + 状态观察器', ['读取当前 owner 的精确宠物迁移 ID', '观察宿主与 Mini，识别“智能伙伴”标签'], size=23)
d.line([(800, 650), (800, 710)])
d.diamond(800, 800, 560, 180, ['Codex 正在运行，且', '选中白龙并允许显示？'])
d.line([(1080, 800), (1220, 800)], '否', (1120, 782))
d.box(1220, 730, 310, 130, '等待 / 收起', ['原宿主退出时关闭界面'], size=21)
d.line([(1375, 730), (1375, 580), (1100, 580)])
d.line([(800, 890), (800, 970)], '是', (820, 932))
d.box(500, 970, 600, 150, '按需启动白龙界面', ['加载原作者引擎与 35 段动画', '读取偏好，创建控件并同步显隐'])
d.line([(800, 1120), (800, 1190), (280, 1190), (280, 1260)])
d.line([(800, 1120), (800, 1260)], '并行处理', (825, 1172))
d.line([(800, 1120), (800, 1190), (1320, 1190), (1320, 1260)])
d.box(60, 1260, 440, 220, '任务变化', ['只读本地任务状态', '更新任务卡与原作者任务动作', '自动提示服从独显显示许可'], fill='#f1ebf8', size=23)
d.box(580, 1260, 440, 220, '用户交互', ['右键 / 托盘始终可用', '主动打开聊天 / 历史 / 额度', '聊天交给同一 Codex 会话执行'], fill='#f1ebf8', size=23)
d.box(1100, 1260, 440, 220, '设置与移动', ['保存大小、固定、独显等偏好', '设置入口始终可用', '独显不更改移动或音效偏好'], fill='#f1ebf8', size=23)
for x in (280, 800, 1320):
    d.line([(x, 1480), (x, 1520), (800, 1520), (800, 1550)])
d.box(100, 1550, 1400, 180, '统一处理窗口显示许可、层级、定位与动画', ['正常模式：沿用显隐、互斥、避让与半速过渡；独显模式：默认只保留本体', '常驻控制栏 / 任务卡 / 自动提示保持隐藏，明确手动打开的聊天、历史、额度和设置可显示', '切换模式清除临时许可与待显示提示；定时刷新、悬停和迟到 Show 均经过检查'], '#eee5f8', 22)
d.line([(800, 1730), (800, 1760)])
d.diamond(800, 1850, 560, 180, ['原宿主关闭或换代，', '或白龙已请求退出？'])
d.line([(1080, 1850), (1220, 1850)], '是', (1120, 1832))
d.box(1220, 1750, 320, 240, '有界退出与回收', ['界面看门狗：15 秒', '监督回收期限：20 秒', '仅持有的白龙子进程', '保留退出原因与宿主代际'], '#f1ebf8', 20)
d.line([(1380, 1990), (1570, 1990), (1570, 580), (1375, 580)])
d.line([(800, 1940), (800, 2050)], '否', (820, 1993))
d.diamond(800, 2130, 480, 160, ['用户发起卸载？'])
d.line([(560, 2130), (30, 2130), (30, 580), (500, 580)], '否：继续观察与交互', (75, 2100))
d.line([(800, 2210), (800, 2300)], '是', (820, 2254))
d.box(400, 2300, 800, 145, '运行一键卸载白龙', ['核对归属 / 停止自己的助手 / 还原相关选择 / 清理自启动', '保留聊天记录、其他桌宠及用户后续设置'], size=25)
d.line([(800, 2445), (800, 2510)])
d.box(500, 2510, 600, 80, '卸载完成，可移走解压目录', fill='#eee5f8')
d.save()
print(str(OUT))
