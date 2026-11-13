# -*- coding: utf-8 -*-
"""从 references/pitfalls.md 自动生成 pitfalls_index.md（速查表）。

为什么要它（v1.2.50，2026-11-13）：
  pitfalls.md 已长到 168 KB / 2760 行。每次任务都整篇读进来，token 消耗远超实际需要。
  但索引**绝不能手写**——手写必然与正文脱节（P115 的教训：写两遍必错一遍）。
  本脚本每次从正文重新抽取，跑完自校验条目数，对不上就非零退出。

用法：
    python scripts/make_index.py            # 生成/更新 references/pitfalls_index.md
    python scripts/make_index.py --check    # 只校验索引是否与正文同步（CI 用）
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, 'references', 'pitfalls.md')
DST = os.path.join(ROOT, 'references', 'pitfalls_index.md')

# 实测正文有 5 种标题格式：`## P19｜…` / `### P42 …` / `## P31 · …` / `## P37：…` / `### P37.1 …`
# 必须统一覆盖，否则会漏掉一半条目（首次实现只匹配到 50/111 条）
HEAD_RE = re.compile(
    r'^#{2,3}\s*(P\d+(?:\.\d+)?)\s*(?:[｜|·：:]|\s)\s*(.+?)\s*$', re.M)


def parse(text):
    """切出每条：编号 / 标题 / 一句话判据 / 关键词。"""
    # 按标题切段
    marks = list(HEAD_RE.finditer(text))
    items = []
    for i, m in enumerate(marks):
        pid, title = m.group(1), m.group(2).strip()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end():end]

        # ★ 主判据 = 标题本身。标题是撰写时刻意精炼的，信息密度最高；
        #   从正文首行抽的「gist」质量差（常抓到列表符号和半句），实测不可用。
        gist = re.sub(r'\*\*|`|~~', '', title)
        gist = re.sub(r'[⚠️✅🔴★]+', '', gist)
        gist = re.sub(r'\s+', ' ', gist).strip()
        if len(gist) > 52:
            gist = gist[:50] + '…'

        # 关键词：正文里出现的代码/字段名（帮助 grep 定位）
        toks = re.findall(r'`([A-Za-z_][A-Za-z0-9_.]{2,})`', body)
        seen_t = []
        for t in toks:
            if t not in seen_t:
                seen_t.append(t)
        kw = ' '.join(seen_t[:3]) if seen_t else '—'
        items.append((pid, gist, kw))
    return items


def build(items):
    order = []
    seen = set()
    for pid, gist, kw in items:          # 注意顺序：(编号, 判据, 关键词)
        if pid in seen:
            continue
        seen.add(pid)
        order.append((pid, gist, kw))

    # 按编号排序。★ 存在 `P37.1` 这类小数编号，不能直接 int()
    def sort_key(z):
        parts = z[0][1:].split('.')
        return (int(parts[0]), int(parts[1]) if len(parts) > 1 else -1)

    order.sort(key=sort_key)
    out = ['# pitfalls 速查索引',
           '',
           '> **本文件由 `scripts/make_index.py` 自动生成，禁止手改。**',
           '> 只读这里（约 10 KB）就能定位问题；需要细节再用编号去 `pitfalls.md` 搜。',
           '> 全文 %d 条，正文约 %d KB —— **默认不要整篇读正文**。' % (len(order), 0),
           '',
           '| 编号 | 一句话判据 | 关键词 |',
           '|---|---|---|']
    for pid, gist, kw in order:
        out.append('| **%s** | %s | %s |' % (pid, gist.replace('|', '/'), kw.replace('|', '/')))
    out.append('')
    out.append('## 怎么用')
    out.append('')
    out.append('1. 先在本表**搜索症状词**（如「负值」「末根」「降级」「量纲」）；')
    out.append('2. 命中后 `grep -A 40 "^## %s" references/pitfalls.md` 只看那一节；' % 'P115')
    out.append('3. 新增条目后跑 `python scripts/make_index.py` 重新生成，**不要手动补表**。')
    out.append('')
    return '\n'.join(out), len(order)


def main():
    if not os.path.exists(SRC):
        print('找不到 %s' % SRC)
        return 2
    src = io.open(SRC, encoding='utf-8').read()
    items = parse(src)
    if not items:
        print('未解析到任何条目 —— 标题格式可能变了，先看 HEAD_RE')
        return 1
    text, n = build(items)
    text = text.replace('约 %d KB' % 0, '约 %d KB' % (len(src) // 1024))

    if '--check' in sys.argv:
        if not os.path.exists(DST):
            print('索引不存在，请先运行 make_index.py')
            return 1
        cur = io.open(DST, encoding='utf-8').read()
        if cur.strip() != text.strip():
            print('索引与正文不一致（正文 %d 条），请重新生成' % n)
            return 1
        print('索引同步 ✓（%d 条）' % n)
        return 0

    io.open(DST, 'w', encoding='utf-8').write(text)
    print('已生成 %s' % DST)
    print('  条目 %d | 索引 %d 字符（正文 %d 字符，压缩到 %.1f%%）'
          % (n, len(text), len(src), len(text) * 100.0 / len(src)))
    # 自校验：编号不得重复、不得漏
    ids_src = set(m.group(1) for m in HEAD_RE.finditer(src))
    ids_idx = set(re.findall(r'\|\s*\*\*(P\d+(?:\.\d+)?)\*\*\s*\|', text))
    if ids_src != ids_idx:
        print('✗ 校验失败：正文 %d 条，索引 %d 条，差集 %s'
              % (len(ids_src), len(ids_idx), (ids_src ^ ids_idx)))
        return 1
    print('  自校验 ✓ 编号集合一致（%d 条）' % len(ids_src))
    return 0


if __name__ == '__main__':
    sys.exit(main())
