# -*- coding: utf-8 -*-
"""check_html.py — 报告产物的**结构 + 脱敏**验收。

为什么要有（P111，2026-11-12 实景）
-----------------------------------
自写的 `card()` 模板漏了一个 `</div>`：
    '<section class="card"><div class="cardhead">..</div><div class="cardbody">..</section>'
我当时的断言是 `count('<section') == count('</section>')` —— **照样通过**。
section 确实配对，错的是它里面那个 div。浏览器会自动修复，
所以肉眼看大概率也正常，但 CSS 的 `.cardbody` padding 会漏到 section 外，
窄屏下表现为留白异常。

教训：**标签配对必须按标签逐个计数，最外层配对挡不住内层未闭合。**

检查项
------
  1. 全栈标签配对（HTMLParser，而不是数子串）
  2. 每个标签的开闭计数（快速定位是哪个标签）
  3. 每个 <table> 的表头 <th> 数与每行 <td> 数一致
  4. <section> / 卡片数量
  5. 脱敏：分**硬/软**两档
       · 硬隐私（config.json / 可用资金 / 本金 / 账户权益…）→ 命中即失败
       · 软隐私（成本价 / 买入价 / 持股数 / 三位小数金额）→ 默认只提示，
         **对外发布加 `--strict` 才判失败**。个人复盘需要用成本位做技术锚点，
         默认档不能把它当错误（否则每次都假阳性）。
  6. 未闭合的 <html>/<body> 结尾检查

退出码：0 全通过 / 1 有失败项（**不得用"警告"糊弄过去**）

用法
----
    python check_html.py path/to/report.html
    python check_html.py path/to/report.html --min-rows 30
    python check_html.py path/to/report.html --strict      # 对外发布前必跑
    python check_html.py path/to/report.html --no-privacy
"""
import sys
import re
import io
from html.parser import HTMLParser

VOID = {'br', 'img', 'meta', 'input', 'hr', 'link', 'source', 'col', 'area', 'base', 'embed'}

# 硬隐私：**任何情况下都不该出现在报告里**（出现在个人复盘也算失误，因为没有理由写）
PRIVACY_HARD = [
    'config.json', 'allowed_prefixes', '可用资金', '本金', '账户权益', '可用余额',
    '总资产', '资金账号', '股东账号',
]
# 软隐私：个人复盘允许（要拿成本位做技术锚点），**对外发布必须清掉**
# → 默认只提示不判失败；`--strict` 才当失败
PRIVACY_SOFT_WORDS = ['成本价', '买入价', '持仓成本', '建仓价']
# ⚠️ 模糊规则：只能提示，不能自动判定。
#    实测「0.014 元」是**价差**（10.02−10.00 这类），与「成本价 12.345 元」格式完全一样，
#    正则无法区分 → 若当硬判据会产生假阳性。
PRIVACY_SOFT_PATTERNS = [
    (r'\b\d{2,6}\s*股\b', '持股数（形如 1500 股）'),
    (r'\d+\.\d{3}\s*元', '三位小数金额（可能是成本价，也可能是价差）'),
]


class Stack(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.errors = []
        self.counts = {}

    def handle_starttag(self, tag, attrs):
        self.counts[tag] = self.counts.get(tag, 0) + 1
        if tag not in VOID:
            self.stack.append((tag, self.getpos()))

    def handle_startendtag(self, tag, attrs):
        self.counts[tag] = self.counts.get(tag, 0) + 1

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack:
            self.errors.append('第 %d 行：多余的 </%s>' % (self.getpos()[0], tag))
            return
        top, pos = self.stack[-1]
        if top == tag:
            self.stack.pop()
        else:
            # 尝试向上找：说明中间有标签没闭合
            names = [t for t, _ in self.stack]
            if tag in names:
                idx = len(names) - 1 - names[::-1].index(tag)
                missing = names[idx+1:]
                self.errors.append(
                    '第 %d 行：</%s> 之前有未闭合标签 %s' % (
                        self.getpos()[0], tag, ', '.join('<%s>' % m for m in missing)))
                del self.stack[idx:]
            else:
                self.errors.append(
                    '第 %d 行：</%s> 没有对应开标签（栈顶是 <%s>）' % (
                        self.getpos()[0], tag, top))


def _count_tag(text, tag):
    """精确数某个标签的开标签个数。

    ⚠️ 不能用 `text.count('<th')` —— `<thead>` 也以 `<th` 开头，
       实测把 2 列表头数成 3 列，**误报表格列数不符**（假阳性）。
       必须按标签边界匹配：`<th` 后面只能是空白或 `>`。
    """
    return len(re.findall(r'<%s(?:[\s>/])' % re.escape(tag), text))


def check_tables(text):
    """每个 table 的 th 数与每行 td 数一致。返回问题列表。"""
    probs = []
    for ti, tb in enumerate(re.findall(r'<table[^>]*>.*?</table>', text, re.S), 1):
        head = re.search(r'<thead>.*?</thead>', tb, re.S)
        if not head:
            probs.append('表 %d：缺 <thead>（列数无从校验）' % ti)
            continue
        n = _count_tag(head.group(0), 'th')
        if n == 0:
            probs.append('表 %d：表头没有 <th>' % ti)
            continue
        for ri, row in enumerate(re.findall(r'<tr[^>]*>(.*?)</tr>', tb, re.S), 1):
            if _count_tag(row, 'th'):
                continue
            c = _count_tag(row, 'td')
            if c and c != n:
                probs.append('表 %d 第 %d 行：%d 列（表头 %d 列）' % (ti, ri, c, n))
    return probs


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    path = argv[1]
    min_rows = 0
    privacy = True
    strict = False
    i = 2
    while i < len(argv):
        if argv[i] == '--min-rows':
            min_rows = int(argv[i+1]); i += 2
        elif argv[i] == '--no-privacy':
            privacy = False; i += 1
        elif argv[i] == '--strict':
            strict = True; i += 1
        else:
            print('未知参数 %s' % argv[i]); return 2
    try:
        text = io.open(path, encoding='utf-8').read()
    except Exception as e:
        print('读取失败：%r' % e)
        return 1

    fail = []

    st = Stack()
    st.feed(text)
    print('=== 1. 标签配对 ===')
    if st.errors:
        for e in st.errors[:10]:
            print('  ✗ ' + e)
        fail.append('标签错配 %d 处' % len(st.errors))
    else:
        print('  ✓ 全栈配对通过')
    if st.stack:
        left = ['<%s>@第%d行' % (t, p[0]) for t, p in st.stack]
        print('  ✗ 未闭合：' + ', '.join(left[:10]))
        fail.append('未闭合标签 %d 个' % len(st.stack))

    print('\n=== 2. 关键标签计数（开 / 闭） ===')
    for tag in ('section', 'div', 'table', 'thead', 'tbody', 'tr', 'td', 'th'):
        n = st.counts.get(tag, 0)
        close = len(re.findall(r'</%s\s*>' % tag, text))
        flag = '✓' if n == close else '✗'
        if n != close:
            fail.append('%s 开闭不等 %d/%d' % (tag, n, close))
        print('  %s %-8s %4d / %4d' % (flag, '<%s>' % tag, n, close))

    print('\n=== 3. 表格列数一致性 ===')
    tprobs = check_tables(text)
    if tprobs:
        for p in tprobs[:10]:
            print('  ✗ ' + p)
        fail.append('表格列数不符 %d 处' % len(tprobs))
    else:
        print('  ✓ 全部表格列数一致')

    rows = text.count('<tr')
    print('\n=== 4. 规模 ===')
    print('  表格行 %d | section %d | 字符 %d' % (rows, st.counts.get('section', 0), len(text)))
    if min_rows and rows < min_rows:
        print('  ✗ 表格行 %d < 期望 %d' % (rows, min_rows))
        fail.append('表格行不足')

    print('\n=== 5. 结尾完整 ===')
    tail = text.rstrip()
    for t in ('</html>',):
        if t in tail[-200:]:
            print('  ✓ 含 %s' % t)
        else:
            print('  ✗ 结尾缺 %s' % t)
            fail.append('结尾缺 %s' % t)

    if privacy:
        print('\n=== 6. 脱敏 ===')
        hard = [t for t in PRIVACY_HARD if t in text]
        if hard:
            for h in hard:
                print('  ✗ 硬隐私词：%s' % h)
            fail.append('硬隐私命中 %d 项' % len(hard))
        else:
            print('  ✓ 未命中硬隐私词（config/资金/账户类）')

        soft_w = [t for t in PRIVACY_SOFT_WORDS if t in text]
        soft_p = []
        for pat, lab in PRIVACY_SOFT_PATTERNS:
            for m in re.finditer(pat, text):
                soft_p.append('%s（%r）' % (lab, m.group(0)))
        if soft_w or soft_p:
            tip = soft_w + soft_p[:5]
            if strict:
                for h in tip:
                    print('  ✗ 软隐私：%s' % h)
                fail.append('软隐私命中 %d 项（--strict）' % len(tip))
            else:
                print('  ⚠️ 检出 %d 处个人口径（个人复盘允许）：%s' % (len(tip), '；'.join(tip[:5])))
                print('     → **对外发布前请加 --strict 复核并清空**')
        else:
            print('  ✓ 未命中软隐私词')

    print('\n' + '=' * 60)
    if fail:
        print('验收失败：' + '；'.join(fail))
        return 1
    print('验收通过：结构自洽、列数一致、无硬隐私命中')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
