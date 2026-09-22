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
    python scripts/check_privacy.py --msg FILE   # 扫描一条提交信息（commit-msg 钩子用）
    python scripts/check_privacy.py --history    # 审计全部历史提交信息
    python scripts/check_privacy.py --terms F    # 指定禁词文件

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


def scan_text(txt, terms, label):
    return [(label, t, '禁词') for t in terms if t in txt]


def main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    all_mode = '--all' in sys.argv
    hist_mode = '--history' in sys.argv
    msg_file = None
    tpath = TERMS
    for i, a in enumerate(sys.argv):
        if a == '--terms' and i + 1 < len(sys.argv):
            tpath = sys.argv[i + 1]
        if a == '--msg' and i + 1 < len(sys.argv):
            msg_file = sys.argv[i + 1]

    terms = load_terms(tpath)
    if terms is None:
        print('  ⚠️ 未找到禁词文件 %s —— 只跑结构性规则（建议创建它以保证覆盖）' % tpath)
        terms = []

    # ---- 模式 A：提交信息（commit-msg 钩子用）----
    # 为什么要单独挂一个钩子：pre-commit 只看得见**文件内容**，看不见**提交信息**，
    # 而提交信息同样是公开的。本项目实测过这个洞 —— 为了说明"移除了什么"，
    # 把被删的原值又在提交信息里复述了一遍，等于换个地方再泄露一次。
    if msg_file:
        try:
            raw = open(msg_file, encoding='utf-8', errors='replace').read()
        except Exception as e:
            print('  ⚠️ 无法读取提交信息 %s：%s' % (msg_file, e))
            return 0
        body = '\n'.join(l for l in raw.splitlines() if not l.lstrip().startswith('#'))
        hits = scan_text(body, terms, 'COMMIT_MSG')
        if not hits:
            print('  ✅ 提交信息未含敏感词（对照 %d 条禁词）' % len(terms))
            return 0
        print('  ❌ 提交信息命中 %d 处，已阻断提交：' % len(hits))
        for _, what, _why in hits:
            print('     COMMIT_MSG  %s' % what)
        print()
        print('  处理方式：只描述**处理方式与文件清单**，绝不引用被移除的原值。')
        print('  紧急绕过：git commit --no-verify')
        return 1

    # ---- 模式 B：审计全部历史提交信息（改不了，只能重写历史）----
    if hist_mode:
        r = subprocess.run(['git', 'log', '--all', '--format=%H%x1f%s%n%b%x1e'],
                           capture_output=True, text=True, cwd=ROOT)
        recs = [x for x in (r.stdout or '').split('\x1e') if x.strip()]
        print('  审计 %d 条历史提交信息 × %d 条禁词' % (len(recs), len(terms)))
        hits = []
        for rec in recs:
            if '\x1f' not in rec:
                continue
            sha, body = rec.split('\x1f', 1)
            for t in terms:
                if t in body:
                    hits.append((sha[:8], t))
        if not hits:
            print('  ✅ 历史提交信息未含敏感词')
            return 0
        print('  ❌ 命中 %d 处（历史信息无法就地修改，需重写历史或删库重建）：' % len(hits))
        for sha, what in hits:
            print('     %-10s %s' % (sha, what))
        return 1

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
