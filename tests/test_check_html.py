# -*- coding: utf-8 -*-
"""check_html.py 的验收测试（全离线）。

覆盖两个真实踩过的坑：
  1. `'<thead>'.count('<th') == 1` —— 子串计数把 2 列表头数成 3 列，**对正确报告误报**；
  2. `card()` 漏 `</div>` 时，`count('<section')==count('</section>')` **照样通过** ——
     最外层配对挡不住内层未闭合。

跑法：
    python -m unittest discover -s tests -v
"""
import io
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, 'scripts'))

import check_html  # noqa: E402


GOOD = '''<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<section class="card"><div class="cardhead">T</div><div class="cardbody">
<table><thead><tr><th>a</th><th>b</th></tr></thead>
<tbody><tr><td>1</td><td>2</td></tr><tr><td>3</td><td>4</td></tr></tbody></table>
</div></section></body></html>'''

MISSING_DIV = GOOD.replace('</table>\n</div></section>', '</table>\n</section>')
BAD_COLS = GOOD.replace('<td>2</td>', '<td>2</td><td>3</td>')
BAD_HARD = GOOD.replace('>T<', '>可用资金 3200<')
BAD_SOFT = GOOD.replace('>T<', '>成本价 12.345 元 1500 股<')


def write(text):
    fd, p = tempfile.mkstemp(suffix='.html')
    os.close(fd)
    io.open(p, 'w', encoding='utf-8').write(text)
    return p


class TestCountTag(unittest.TestCase):
    """★ 子串计数坑：标签名可能是别的标签名的前缀。"""

    def test_thead_not_counted_as_th(self):
        self.assertEqual(check_html._count_tag('<thead><tr><th>a</th>', 'th'), 1,
                         '<thead> 被当成 <th> —— 2 列表头会被数成 3 列')

    def test_tbody_not_counted_as_td(self):
        self.assertEqual(check_html._count_tag('<tbody><tr><td>1</td>', 'td'), 1)

    def test_plain_counts(self):
        self.assertEqual(check_html._count_tag('<th>a</th><th>b</th>', 'th'), 2)
        self.assertEqual(check_html._count_tag('<td>1</td>', 'th'), 0)


class TestStructure(unittest.TestCase):

    def tearDown(self):
        for p in getattr(self, '_tmp', []):
            try:
                os.remove(p)
            except OSError:
                pass

    def _run(self, text, *args):
        p = write(text)
        self._tmp = getattr(self, '_tmp', []) + [p]
        return check_html.main(['check_html.py', p] + list(args))

    def test_good_passes(self):
        self.assertEqual(self._run(GOOD), 0)

    def test_missing_div_detected(self):
        """card 漏 </div> 必须被检出（section 配对照样通过，但 div 不配对）。"""
        self.assertEqual(self._run(MISSING_DIV), 1)

    def test_bad_columns_detected(self):
        self.assertEqual(self._run(BAD_COLS), 1)

    def test_good_has_no_column_false_positive(self):
        """★ 正确报告的列数检查不得误报（这是修 `<thead>` 坑之前的表现）。"""
        self.assertEqual(check_html.check_tables(GOOD), [])


class TestPrivacy(unittest.TestCase):

    def tearDown(self):
        for p in getattr(self, '_tmp', []):
            try:
                os.remove(p)
            except OSError:
                pass

    def _run(self, text, *args):
        p = write(text)
        self._tmp = getattr(self, '_tmp', []) + [p]
        return check_html.main(['check_html.py', p] + list(args))

    def test_hard_privacy_fails_by_default(self):
        self.assertEqual(self._run(BAD_HARD), 1)

    def test_soft_privacy_passes_by_default(self):
        """个人复盘允许出现成本位/持股数 → 默认档不能判失败。"""
        self.assertEqual(self._run(BAD_SOFT), 0)

    def test_soft_privacy_fails_in_strict(self):
        """对外发布 --strict 必须判失败。"""
        self.assertEqual(self._run(BAD_SOFT, '--strict'), 1)

    def test_no_privacy_flag(self):
        self.assertEqual(self._run(BAD_HARD, '--no-privacy'), 0)


class TestExitCodes(unittest.TestCase):

    def test_no_args_returns_2(self):
        self.assertEqual(check_html.main(['check_html.py']), 2)

    def test_missing_file_returns_1(self):
        self.assertEqual(check_html.main(['check_html.py', '__not_exist__.html']), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
