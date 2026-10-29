"""Complete stored file edits as aligned before/after rows; no file I/O."""
from __future__ import annotations
import html
from itertools import zip_longest
import re

HUNK = re.compile(r'^@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@')


def comparison_rows(diff, kind):
    lines = (diff or '').splitlines()
    if kind == 'add':
        # Codex stores added-file contents verbatim, not as + prefixed hunks.
        return [{'left': '', 'right': line, 'old': None, 'new': index, 'change': 'add'}
                for index, line in enumerate(lines, 1)]
    if kind == 'delete':
        return [{'left': line, 'right': '', 'old': index, 'new': None, 'change': 'delete'}
                for index, line in enumerate(lines, 1)]
    rows, removed, added = [], [], []
    old = new = None
    in_hunk = False

    def flush():
        for left, right in zip_longest(removed, added):
            rows.append({'left': left[1] if left else '', 'right': right[1] if right else '',
                         'old': left[0] if left else None, 'new': right[0] if right else None,
                         'change': 'replace' if left and right else 'delete' if left else 'add'})
        removed.clear()
        added.clear()

    for line in lines:
        match = HUNK.match(line)
        if match:
            flush()
            old, new = map(int, match.groups())
            in_hunk = True
            rows.append({'change': 'hunk', 'label': line})
            continue
        if not in_hunk and line.startswith(('--- ', '+++ ', 'diff --git ', 'index ', '*** ')):
            continue
        if line.startswith('\\ No newline'):
            continue
        if line.startswith('-'):
            removed.append((old, line[1:]))
            old = old + 1 if old is not None else None
        elif line.startswith('+'):
            added.append((new, line[1:]))
            new = new + 1 if new is not None else None
        else:
            flush()
            value = line[1:] if line.startswith(' ') else line
            rows.append({'left': value, 'right': value, 'old': old, 'new': new, 'change': 'context'})
            old = old + 1 if old is not None else None
            new = new + 1 if new is not None else None
    flush()
    return rows


def render_file_diff(change):
    if change.get('kind') == 'add':
        # One text block also keeps very large added files responsive; no
        # needless empty original column or per-line table cells are needed.
        return ('<h4 style="color:#4f8172">新增文件 · 完整内容</h4>'
                '<pre style="font-family:Consolas,monospace;white-space:pre;background-color:#e7f4eb">' +
                html.escape(change.get('diff') or '') + '</pre>')
    if change.get('kind') == 'delete':
        return ('<p style="color:#89739f">红色为已删除的原文件内容。</p>'
                '<table width="100%" border="0" cellspacing="1" cellpadding="3">'
                '<tr bgcolor="#e9e0f3"><th width="50%">原文件</th><th width="50%">新文件</th></tr>'
                '<tr><td valign="top" bgcolor="#fbe9ec"><pre style="font-family:Consolas,monospace;white-space:pre">' +
                html.escape(change.get('diff') or '') + '</pre></td>'
                '<td valign="top">文件已删除。</td></tr></table>')
    rows = comparison_rows(change.get('diff') or '', change.get('kind'))
    parts = ['<p style="color:#89739f">红色：删除或修改前；绿色：新增或修改后。</p>',
             '<table width="100%" border="0" cellspacing="1" cellpadding="3">',
             '<tr bgcolor="#e9e0f3"><th width="50%">原文件</th>'
             '<th width="50%">新文件</th></tr>']
    for row in rows:
        if row['change'] == 'hunk':
            parts.append('<tr bgcolor="#f0eaf8"><td colspan="2">' + html.escape(row['label']) + '</td></tr>')
            continue
        left_changed = row['change'] in ('delete', 'replace')
        right_changed = row['change'] in ('add', 'replace')
        parts.append('<tr>')
        for number, value, changed, color in ((row['old'], row['left'], left_changed, '#fbe9ec'),
                                               (row['new'], row['right'], right_changed, '#e7f4eb')):
            background = color if changed else '#fbf9ff'
            label = (str(number).rjust(4) + '  ') if number is not None else '      '
            parts.append('<td width="50%" valign="top" bgcolor="' + background +
                         '"><pre style="font-family:Consolas,monospace;white-space:pre;margin:0">' +
                         '<span style="color:#89739f">' + label + '</span>' +
                         (html.escape(value) if value else '&#160;') + '</pre></td>')
        parts.append('</tr>')
    if not rows:
        parts.append('<tr><td colspan="2">空文件或未提供文本差异。</td></tr>')
    parts.append('</table>')
    return ''.join(parts)


def file_placeholder(token):
    return '<p>[[BAILONG_FILE_' + token + ']]</p>'


def create_file_views(view):
    """Native code panes occupy bounded inline objects in the chat document.

    Long file lines scroll inside these child widgets, so they cannot widen
    the surrounding conversation or move ordinary messages horizontally.
    """
    from PySide6.QtCore import QObject, QEvent, QTimer, QSizeF, Qt, QRect
    from PySide6.QtGui import (QColor, QFont, QTextCharFormat, QTextCursor,
                              QTextFormat, QSyntaxHighlighter, QPyTextObject)
    from PySide6.QtWidgets import (QFrame, QWidget, QVBoxLayout, QHBoxLayout,
                                 QLabel, QPlainTextEdit)
    from bailong_theme import SCROLLBAR_STYLE

    object_type = int(QTextFormat.ObjectTypes.UserObject) + 72
    token_property = int(QTextFormat.Property.UserProperty) + 72

    class LineColors(QSyntaxHighlighter):
        def __init__(self, document):
            super().__init__(document)
            self.styles = []

        def highlightBlock(self, value):
            index = self.currentBlock().blockNumber()
            style = self.styles[index] if index < len(self.styles) else 'context'
            palette = {'old': ('#fbe9ec', '#8f3547'), 'new': ('#e7f4eb', '#246b43'),
                       'hunk': ('#eee7f5', '#8061a3')}
            if style in palette:
                background, foreground = palette[style]
                fmt = QTextCharFormat()
                fmt.setBackground(QColor(background))
                fmt.setForeground(QColor(foreground))
                self.setFormat(0, len(value), fmt)

    class FilePanel(QFrame):
        def __init__(self):
            super().__init__(view.viewport())
            self.setObjectName('BailongFileComparison')
            self.setStyleSheet('QFrame#BailongFileComparison {background:#fbf9ff;border:1px solid #d9cbe5;border-radius:6px;}')
            row = QHBoxLayout(self)
            row.setContentsMargins(6, 6, 6, 6)
            row.setSpacing(8)
            self.labels, self.editors, self.highlighters = [], [], []
            for label, color in (('原文件', '#8f3547'), ('新文件', '#246b43')):
                column = QWidget(self)
                layout = QVBoxLayout(column)
                layout.setContentsMargins(0, 0, 0, 0)
                layout.setSpacing(4)
                heading = QLabel(label)
                heading.setStyleSheet(f'color:{color};font-weight:600;background:transparent;')
                editor = QPlainTextEdit()
                editor.setReadOnly(True)
                editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
                editor.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
                editor.setFont(QFont('Consolas', 10))
                editor.setStyleSheet('QPlainTextEdit {font-family:Consolas,"Microsoft YaHei UI";font-size:12px;'
                                    'background:white;color:#38294d;border:1px solid #e4daed;border-radius:3px;}' + SCROLLBAR_STYLE)
                editor.setAccessibleName(label + '代码')
                layout.addWidget(heading)
                layout.addWidget(editor, 1)
                row.addWidget(column, 1)
                self.labels.append(heading)
                self.editors.append(editor)
                self.highlighters.append(LineColors(editor.document()))
            self.before, self.after = self.editors
            self._syncing = False
            self._signature = None
            self._line_count = 1
            for index, editor in enumerate(self.editors):
                editor.verticalScrollBar().valueChanged.connect(
                    lambda value, index=index: self._sync_scroll(index, value, False))
                editor.horizontalScrollBar().valueChanged.connect(
                    lambda value, index=index: self._sync_scroll(index, value, True))

        def _sync_scroll(self, index, value, horizontal):
            if self._syncing:
                return
            self._syncing = True
            try:
                other = self.editors[1 - index]
                bar = other.horizontalScrollBar() if horizontal else other.verticalScrollBar()
                bar.setValue(value)
            finally:
                self._syncing = False

        def set_change(self, change):
            signature = (change.get('kind'), change.get('diff'))
            if signature == self._signature:
                return False
            old_height = self.preferred_height()
            kind, diff = signature
            if kind == 'add':
                bodies = ['', diff or '']
                styles = [[], ['new'] * max(1, len((diff or '').splitlines()))]
                labels = ['原文件（新增前为空）', '新文件（完整新增）']
            elif kind == 'delete':
                bodies = [diff or '', '']
                styles = [['old'] * max(1, len((diff or '').splitlines())), []]
                labels = ['原文件（已删除）', '新文件（内容为空）']
            else:
                rows = comparison_rows(diff or '', kind)
                left, right, left_styles, right_styles = [], [], [], []
                for item in rows:
                    if item['change'] == 'hunk':
                        left.append(item['label'])
                        right.append(item['label'])
                        left_styles.append('hunk')
                        right_styles.append('hunk')
                    else:
                        left.append(item['left'])
                        right.append(item['right'])
                        left_styles.append('old' if item['change'] in ('delete', 'replace') else 'context')
                        right_styles.append('new' if item['change'] in ('add', 'replace') else 'context')
                bodies, styles, labels = ['\n'.join(left), '\n'.join(right)], [left_styles, right_styles], ['原文件', '新文件']
            self._line_count = max(1, *(len(body.splitlines()) for body in bodies))
            self._syncing = True
            try:
                for index, editor in enumerate(self.editors):
                    horizontal, vertical = editor.horizontalScrollBar().value(), editor.verticalScrollBar().value()
                    self.highlighters[index].styles = styles[index]
                    editor.setPlainText(bodies[index])
                    self.highlighters[index].rehighlight()
                    self.labels[index].setText(labels[index])
                    editor.horizontalScrollBar().setValue(horizontal)
                    editor.verticalScrollBar().setValue(vertical)
            finally:
                self._syncing = False
            self._signature = signature
            return self.preferred_height() != old_height

        def preferred_height(self):
            return min(420, max(130, 54 + min(self._line_count, 22) * 16))

    class InlineFileObject(QPyTextObject):
        def intrinsicSize(self, document, position, fmt):
            panel = manager.panels.get(str(fmt.property(token_property)))
            return QSizeF(manager.available_width(), panel.preferred_height() if panel else 130)

        def drawObject(self, painter, rect, document, position, fmt):
            pass  # The native child widget paints this reserved rectangle.

    class FileViewManager(QObject):
        def __init__(self):
            super().__init__(view)
            self.panels = {}
            self.active = set()
            self.order = []
            self.positions = {}
            self.resize_pending = False
            self.width = self.available_width()
            self.handler = InlineFileObject(self)
            view.document().documentLayout().registerHandler(object_type, self.handler)
            view.verticalScrollBar().valueChanged.connect(self.update_positions)
            view.document().documentLayout().documentSizeChanged.connect(self.update_positions)
            view.viewport().installEventFilter(self)

        def available_width(self):
            return max(100, view.viewport().width() - int(view.document().documentMargin()) * 2 - 6)

        def sync(self, changes):
            self.positions = {}
            self.active = set(changes)
            self.order = list(changes)
            for token, panel in self.panels.items():
                if token not in self.active:
                    panel.hide()
            dirty = False
            for token, change in changes.items():
                if token not in self.panels:
                    self.panels[token] = FilePanel()
                dirty |= self.panels[token].set_change(change)
                marker = '[[BAILONG_FILE_' + token + ']]'
                cursor = view.document().find(marker)
                if not cursor.isNull():
                    fmt = QTextCharFormat()
                    fmt.setObjectType(object_type)
                    fmt.setProperty(token_property, token)
                    cursor.insertText('\ufffc', fmt)
                    dirty = True
            self.positions = {}
            block = view.document().begin()
            while block.isValid():
                iterator = block.begin()
                while not iterator.atEnd():
                    fragment = iterator.fragment()
                    if fragment.isValid() and fragment.charFormat().objectType() == object_type:
                        self.positions[str(fragment.charFormat().property(token_property))] = fragment.position()
                    iterator += 1
                block = block.next()
            if dirty:
                self.invalidate_sizes()
            self.update_positions()

        def invalidate_sizes(self):
            for token, position in self.positions.items():
                if token in self.active:
                    view.document().markContentsDirty(position, 1)
            view.document().documentLayout().documentSize()

        def before_document_reset(self):
            self.positions = {}

        def update_positions(self, *args):
            for token in self.active:
                position = self.positions.get(token)
                if position is None:
                    continue
                cursor = QTextCursor(view.document())
                cursor.setPosition(position)
                rect = view.cursorRect(cursor)
                panel = self.panels[token]
                panel.setGeometry(QRect(rect.left(), rect.top(), self.available_width(), panel.preferred_height()))
                panel.show()
                panel.raise_()

        def eventFilter(self, watched, event):
            if event.type() == QEvent.Type.Resize and not self.resize_pending:
                self.resize_pending = True
                QTimer.singleShot(0, self.resize_views)
            return super().eventFilter(watched, event)

        def resize_views(self):
            self.resize_pending = False
            width = self.available_width()
            if width != self.width:
                self.width = width
                self.invalidate_sizes()
            self.update_positions()

        def toPlainText(self):
            return '\n'.join(editor.toPlainText() for token in self.order
                             for editor in self.panels[token].editors)

    manager = FileViewManager()
    return manager
