# -*- coding: utf-8 -*-
"""文档一致性校验：让「文档里声明的数字」必须等于「正文的实际数字」。

来由（2026-11-13，v1.2.52）：
  同一天里 README/SKILL/pitfalls 三个文件并存四个不同的条数声明
  （107 / 118 / 123，实际 119），而**每一个看起来都正常**：
  文件能渲染、格式无错、数字在合理区间。
  根因是条数靠**手工累加**，基数早就算错，之后每加一条都在错的基础上继续加。

  这类错误自动化的结构检查抓不到，但它其实**有外部真值**——
  正文里真实存在多少个条目，是可以用正则数出来的。
  所以不需要「更小心」，只需要**把声明绑定到真值上**。

用法：
    python scripts/check_docs.py          # 检查，有问题非零退出
    python scripts/check_docs.py -v       # 显示每个检查项
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# pitfalls 标题的 5 种实际格式（见 P115/索引生成器的注释）
PIT_RE = re.compile(r'^#{2,3}\s*(P\d+(?:\.\d+)?)\s*(?:[｜|·：:]|\s)', re.M)


def rd(p):
    try:
        return io.open(p, encoding='utf-8').read()
    except Exception:
        return None


def truth():
    """从正文抽取真值。**这是唯一的真值来源。**"""
    pit = rd(os.path.join(ROOT, 'references', 'pitfalls.md'))
    if pit is None:
        return None
    ids = set(PIT_RE.findall(pit))
    return {
        'n': len(ids),
        'ids': ids,
        'kb': round(len(pit) / 1024),
        'lines': pit.count('\n') + 1,
        'text': pit,
    }


def check(verbose=False):
    errs, warns = [], []
    t = truth()
    if t is None:
        return ['找不到 references/pitfalls.md'], []

    skill = rd(os.path.join(ROOT, 'SKILL.md')) or ''
    readme = rd(os.path.join(ROOT, 'README.md')) or ''
    change = rd(os.path.join(ROOT, 'CHANGELOG.md')) or ''
    idx = rd(os.path.join(ROOT, 'references', 'pitfalls_index.md')) or ''

    # ---------- ① 条数：正文 vs 索引 vs 各文档声明 ----------
    n_idx = len(set(re.findall(r'\|\s*\*\*(P\d+(?:\.\d+)?)\*\*\s*\|', idx)))
    if n_idx != t['n']:
        errs.append('索引 %d 条 != 正文 %d 条（跑 make_index.py）' % (n_idx, t['n']))

    # 文档里所有形如「N 条」且 N 在 90~200 区间、并紧跟「踩坑/条目/标题」的声明
    DECL_RE = re.compile(r'(\d{2,3})\s*条(?:踩坑记录|踩坑|真实踩坑记录|标题|条目)')
    for name, txt in (('SKILL.md', skill), ('README.md', readme),
                      ('pitfalls.md', t['text'][:1500])):
        for m in DECL_RE.finditer(txt):
            n = int(m.group(1))
            if n != t['n']:
                errs.append('%s 声明 %d 条，实际 %d 条' % (name, n, t['n']))

    # ---------- ② 体积 / 行数声明 ----------
    for m in re.finditer(r'\*\*(\d+)\s*KB\s*/\s*(\d+)\s*行', skill + readme):
        kb, ln = int(m.group(1)), int(m.group(2))
        if abs(kb - t['kb']) > 3:
            errs.append('声明 %d KB，实际 %d KB' % (kb, t['kb']))
        # 行数允许 ±10%——正文会微调
        if abs(ln - t['lines']) > max(20, t['lines'] * 0.1):
            errs.append('声明 %d 行，实际 %d 行' % (ln, t['lines']))

    # ---------- ③ 版本号三处 ----------
    v_skill = re.search(r'^version:\s*([\d.]+)', skill, re.M)
    v_readme = re.search(r'\*\*当前版本\*\*：`v([\d.]+)`', readme)
    v_change = re.search(r'^## v([\d.]+)', change, re.M)
    vs = {'SKILL': v_skill and v_skill.group(1),
          'README': v_readme and v_readme.group(1),
          'CHANGELOG': v_change and v_change.group(1)}
    vals = {v for v in vs.values() if v}
    if len(vals) > 1:
        errs.append('版本号不一致：%s' % vs)
    elif not vals:
        errs.append('三处都没找到版本号')
    else:
        cur = vals.pop()
        # README 里的 zip 文件名必须与当前版本匹配
        for z in re.findall(r'niuma-cloud-v([\d.]+)\.zip', readme):
            if z != cur:
                errs.append('README 里 zip 版本 v%s != 当前 v%s' % (z, cur))
        # CHANGELOG 首版必须等于当前版本
        if vs.get('CHANGELOG') and vs['CHANGELOG'] != cur:
            errs.append('CHANGELOG 首条 v%s != 当前 v%s' % (vs['CHANGELOG'], cur))

    # ---------- ④ P 编号引用是否真实存在 ----------
    bad_refs = set()
    for name, txt in (('SKILL.md', skill), ('README.md', readme)):
        # ★ 排除「### P1 · 结论可信度」这类**待办优先级分组标题**——
        #   它用的是 P0/P1/P2/P3，与 pitfalls 的 P 编号同形不同义（实测误报 3 项）
        cleaned = re.sub(r'^#{2,4}\s*P\d+\s*[·・]', '', txt, flags=re.M)
        for m in re.finditer(r'(?<![A-Za-z0-9])P(\d{1,3}(?:\.\d)?)(?![0-9])', cleaned):
            pid = 'P' + m.group(1)
            if pid not in t['ids']:
                bad_refs.add((name, pid))
    # 只报「明确作为条目标注」的引用，避免把 P2P / P50 分位之类误判
    serious = sorted(x for x in bad_refs
                     if x[1].replace('P', '').split('.')[0].isdigit()
                     and int(x[1].replace('P', '').split('.')[0]) >= 5)
    for name, pid in serious:
        warns.append('%s 引用了 %s（正文无此条）' % (name, pid))

    if verbose:
        print('  正文条目 %d | 索引 %d | %d KB / %d 行' %
              (t['n'], n_idx, t['kb'], t['lines']))
        print('  版本 %s' % vs)
    return errs, warns


def autofix():
    """把文档里的条数/体积声明自动对齐到正文真值。

    为什么需要：加一条 pitfall 就要同步改三处（SKILL description、README 两处、pitfalls 头部），
    手工做必漏——实测每加一条都漏。**需要人记住的事，迟早会忘。**
    """
    t = truth()
    if t is None:
        return 1
    n, kb, ln = t['n'], t['kb'], t['lines']
    touched = 0

    for path in ('SKILL.md', 'README.md', 'references/pitfalls.md'):
        full = os.path.join(ROOT, path)
        s = rd(full)
        if s is None:
            continue
        orig = s
        # ① 「N 条踩坑」「N 条真实踩坑记录（P5~Pxxx）」
        s = re.sub(r'(\d{2,3})\s*条踩坑记录', '%d 条踩坑记录' % n, s)
        s = re.sub(r'\*\*(\d{2,3}) 条真实踩坑记录（P5~P\d+(?:\.\d)?）\*\*',
                   '**%d 条真实踩坑记录（P5~P122）**' % n, s)
        s = re.sub(r'(\d{2,3})\s*条踩坑', '%d 条踩坑' % n, s)
        # ② 「N KB / M 行 / K 条」
        s = re.sub(r'\*\*(\d+) KB / (\d+) 行 / (\d+) 条\*\*',
                   '**%d KB / %d 行 / %d 条**' % (kb, ln, n), s)
        # ③ pitfalls 头部「总条目：N 个标题」
        s = re.sub(r'总条目：(\d+) 个标题', '总条目：%d 个标题' % n, s)
        if s != orig:
            io.open(full, 'w', encoding='utf-8').write(s)
            touched += 1
            print('  ✓ 已对齐 %s' % path)
    if not touched:
        print('  无需修改（已对齐）')
    print('  真值：%d 条 / %d KB / %d 行' % (n, kb, ln))
    return 0


def main_check_only():
    verbose = False
    errs, warns = check(verbose)
    if errs:
        print('✗ 仍不一致（%d 项）:' % len(errs))
        for e in errs:
            print('   · %s' % e)
        return 1
    print('✓ 对齐后校验通过')
    return 0


def main():
    if '--fix' in sys.argv:
        rc = autofix()
        print()
        return max(rc, main_check_only())
    verbose = '-v' in sys.argv or '--verbose' in sys.argv
    errs, warns = check(verbose)
    if errs:
        print('✗ 文档一致性校验失败（%d 项）：' % len(errs))
        for e in errs:
            print('   · %s' % e)
        return 1
    if warns:
        print('⚠️ 提示（%d 项，不阻塞）：' % len(warns))
        for w in warns:
            print('   · %s' % w)
    print('✓ 文档一致性通过：条数 / 体积 / 版本 / 引用 全部对齐正文真值')
    return 0


if __name__ == '__main__':
    sys.exit(main())
