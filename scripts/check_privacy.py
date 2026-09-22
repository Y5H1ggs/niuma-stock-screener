# -*- coding: utf-8 -*-
"""check_privacy.py — 提交前隐私闸门（纯标准库，零依赖）

用途：这是要公开的仓库，**绝不能把账户口径写进去**。
本脚本在 `git commit` 前扫描**暂存区**文件，命中禁词即拒绝提交。

设计要点（为什么禁词不写在脚本里）：
  ⚠️ 把"要禁的具体字符串"写进一个**会被提交的脚本**，等于把禁词本身又泄露一次。
     所以禁词存放在**仓库根的 `_privacy_terms.txt`**（已加入 .gitignore，不进仓库），
     本脚本只负责"读取禁词 → 扫描 → 拦截"。

`_privacy_terms.txt` 格式：一行一个禁词，`#` 开头为注释，空行忽略。

用法：
    python scripts/check_privacy.py              # 扫描暂存区（pre-commit 钩子用）
    python scripts/check_privacy.py --all        # 扫描工作区全部受版本控制的文件
    python scripts/check_privacy.py --terms F   # 指定禁词文件

退出码：0 = 通过；1 = 命中禁词（阻断提交）
"""
import io
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TERMS = os.path.join(ROOT, '_privacy_terms.txt')

# 与禁词文件无关的**结构性**规则。刻意留空：
#   试过「--cash 后跟 4 位以上数字」「本金 + 数字」这类规则，结果把文档里的
#   **示例值**（`--cash 5000`）也一并拦下 —— 假阳性会让闸门被 --no-verify 绕过，
#   反而失效。精确判断交给禁词文件（它不在仓库里，可以写得足够具体）。
STRUCT = []

SKIP_EXT = ('.png', '.jpg', '.jpeg', '.gif', '.pdf', '.zip', '.bundle')


def load_terms(path):
    if not os.path.exists(path):
        return None
    out = []
    for line in open(path, encoding='utf-8'):
        s = line.strip()
        if s and not s.startswith('#'):
            out.append(s)
    return out


def staged_files():
    r = subprocess.run(['git', 'diff', '--cached', '--name-only', '--diff-filter=ACM'],
                       capture_output=True, text=True, cwd=ROOT)
    return [f for f in (r.stdout or '').splitlines() if f.strip()]


def tracked_files():
    r = subprocess.run(['git', 'ls-files'], capture_output=True, text=True, cwd=ROOT)
    return [f for f in (r.stdout or '').splitlines() if f.strip()]


def main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    all_mode = '--all' in sys.argv
    tpath = TERMS
    for i, a in enumerate(sys.argv):
        if a == '--terms' and i + 1 < len(sys.argv):
            tpath = sys.argv[i + 1]

    terms = load_terms(tpath)
    if terms is None:
        print('  ⚠️ 未找到禁词文件 %s —— 只跑结构性规则（建议创建它以保证覆盖）' % tpath)
        terms = []

    files = tracked_files() if all_mode else staged_files()
    files = [f for f in files if not f.lower().endswith(SKIP_EXT)]
    print('  扫描 %d 个文件（%s）+ %d 条禁词' % (
        len(files), '工作区全部' if all_mode else '暂存区', len(terms)))

    hits = []
    import re
    for f in files:
        p = os.path.join(ROOT, f)
        if not os.path.exists(p):
            continue
        try:
            txt = open(p, encoding='utf-8', errors='replace').read()
        except Exception:
            continue
        for t in terms:
            if t in txt:
                hits.append((f, t, '禁词'))
        for pat, why in STRUCT:
            for m in re.finditer(pat, txt):
                hits.append((f, m.group(0)[:40], why))

    if not hits:
        print('  ✅ 未发现敏感内容')
        return 0
    print('  ❌ 命中 %d 处，已阻断提交：' % len(hits))
    seen = set()
    for f, what, why in hits:
        key = (f, what)
        if key in seen:
            continue
        seen.add(key)
        print('     %-32s %-24s %s' % (f, what, why))
    print()
    print('  处理方式：把真实值换成示例值/百分比；确需保留的临时文件请加进 .gitignore。')
    print('  禁词清单在 _privacy_terms.txt（不入仓库）；紧急绕过用 git commit --no-verify。')
    return 1


if __name__ == '__main__':
    sys.exit(main())
