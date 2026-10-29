"""Full added contents and all edit hunks in the before/after comparison."""
import unittest
from file_diff import comparison_rows, render_file_diff


class FileDiffTests(unittest.TestCase):
    def test_added_file_is_complete_verbatim_even_when_lines_look_like_a_patch(self):
        content = '\n'.join(['# 新文件', '-这是文件内容', '+也是文件内容', '@@ -1 +1 @@'] +
                            [f'代码第{i}行' for i in range(80)] + ['最后一行'])
        rows = comparison_rows(content, 'add')
        self.assertEqual([row['right'] for row in rows], content.splitlines())
        self.assertEqual(rows[-1]['new'], len(content.splitlines()))
        self.assertTrue(all(row['left'] == '' and row['change'] == 'add' for row in rows))

    def test_deleted_file_shows_all_original_content(self):
        content = '原文件第一行\n原文件第二行\n原文件最后一行'
        rows = comparison_rows(content, 'delete')
        self.assertEqual([row['left'] for row in rows], content.splitlines())
        self.assertTrue(all(row['right'] == '' and row['change'] == 'delete' for row in rows))

    def test_replacements_align_and_hunk_line_numbers_are_preserved(self):
        diff = '@@ -10,3 +10,3 @@\n 保留\n-旧值\n+新值\n 仍保留\n@@ -100,2 +100,1 @@\n-删除的行\n 末尾上下文'
        rows = comparison_rows(diff, 'update')
        changed = [row for row in rows if row['change'] == 'replace']
        self.assertEqual(changed, [{'left': '旧值', 'right': '新值', 'old': 11, 'new': 11, 'change': 'replace'}])
        deleted = next(row for row in rows if row['change'] == 'delete')
        self.assertEqual((deleted['old'], deleted['left']), (100, '删除的行'))
        self.assertEqual(rows[-1]['right'], '末尾上下文')

    def test_all_later_hunks_are_retained(self):
        diff = '\n'.join(f'@@ -{i * 20},1 +{i * 20},1 @@\n-旧值{i}\n+新值{i}' for i in range(1, 30))
        rows = comparison_rows(diff, 'update')
        self.assertEqual(sum(row['change'] == 'hunk' for row in rows), 29)
        self.assertEqual(rows[-1]['right'], '新值29')

    def test_file_headers_and_no_newline_marker_are_not_treated_as_code(self):
        rows = comparison_rows('--- old.py\n+++ new.py\n@@ -1 +1 @@\n-old\n+new\n\\ No newline at end of file', 'update')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]['left'], 'old')
        self.assertEqual(rows[-1]['right'], 'new')

    def test_html_has_both_columns_highlights_and_escaped_code(self):
        rendered = render_file_diff({'kind': 'update', 'diff': '@@ -1 +1 @@\n-<script>old</script>\n+<b>new</b>'})
        self.assertIn('原文件', rendered)
        self.assertIn('新文件', rendered)
        self.assertIn('#fbe9ec', rendered)
        self.assertIn('#e7f4eb', rendered)
        self.assertIn('&lt;script&gt;old', rendered)
        self.assertNotIn('<script>', rendered)
        self.assertNotIn('pre-wrap', rendered)
        self.assertIn('white-space:pre', rendered)
