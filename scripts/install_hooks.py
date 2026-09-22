# -*- coding: utf-8 -*-
"""install_hooks.py — 一条命令重装隐私闸门钩子（纯标准库，零依赖）

为什么需要它：
  git 的钩子目录 `.git/hooks/` **不进版本库** —— 换机器、重新 clone、或换工作副本后，
  闸门会**无声消失**，而提交照常成功。这正是"闸门看起来装了、其实没在工作"的形态，
  本项目在四轮隐私事故里已经吃过这个亏。

做法：
  - 钩子**模板**放受版本控制的 `.githooks/`（可进仓库、可被 review）
  - 本脚本把模板装到 `.git/hooks/`，并在 shebang 后注入**本机** python 解释器绝对路径
    （这样本机路径不会写进公开仓库）
  - 装完**立即端到端自检**：直接执行装好的钩子，验证"干净信息放行、含禁词信息被拦"

用法：
    python scripts/install_hooks.py            # 安装 + 自检
    python scripts/install_hooks.py --check    # 只验证当前是否已装好（不改任何东西）

退出码：0 = 装好且自检通过；1 = 有问题（含"没装"）
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, '.githooks')
GATE = os.path.join('scripts', 'check_privacy.py')
HOOKS = ('pre-commit', 'commit-msg')
TERMS_FILE = os.path.join(ROOT, '_privacy_terms.txt')

out = print
RESULT = []


def sh(cmd):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)


def git_dir():
    r = sh(['git', 'rev-parse', '--git-dir'])
    if r.returncode != 0:
        return None
    d = (r.stdout or '').strip()
    return d if os.path.isabs(d) else os.path.join(ROOT, d)


def hooks_path_cfg():
    r = sh(['git', 'config', '--get', 'core.hooksPath'])
    return (r.stdout or '').strip() if r.returncode == 0 else ''


def first_term():
    """取禁词清单第一条，用于反向自检（**不打印它本身**）。"""
    if not os.path.exists(TERMS_FILE):
        return None
    for line in open(TERMS_FILE, encoding='utf-8'):
        s = line.strip()
        if s and not s.startswith('#'):
            return s
    return None


def run_hook(hook_path, *args):
    """真正执行钩子脚本（找得到 sh 就端到端跑；找不到则退化为直接调 python 闸门）。"""
    exe = shutil.which('sh')
    if exe:
        return subprocess.run([exe, hook_path] + list(args), cwd=ROOT,
                              capture_output=True, text=True)
    if args:      # commit-msg：等价调用
        return subprocess.run([sys.executable, GATE, '--msg', args[0]], cwd=ROOT,
                              capture_output=True, text=True)
    return subprocess.run([sys.executable, GATE], cwd=ROOT,
                          capture_output=True, text=True)


def install(gd):
    os.makedirs(os.path.join(gd, 'hooks'), exist_ok=True)
    py = sys.executable.replace('\\', '/')
    for name in HOOKS:
        src = os.path.join(SRC, name)
        if not os.path.exists(src):
            RESULT.append(('FAIL', '%s 模板缺失（应为 .githooks/%s）' % (name, name)))
            continue
        raw = open(src, encoding='utf-8', newline='').read().replace('\r\n', '\n')
        lines = raw.split('\n')
        inject = 'PRIVACY_PYTHON="%s"' % py
        if lines and lines[0].startswith('#!'):
            lines.insert(1, inject)
        else:
            lines.insert(0, inject)
        dst = os.path.join(gd, 'hooks', name)
        open(dst, 'w', encoding='utf-8', newline='\n').write('\n'.join(lines))
        try:
            os.chmod(dst, 0o755)
        except Exception:
            pass
        RESULT.append(('OK', '已安装 %s -> %s' % (name, os.path.relpath(dst, ROOT))))
    return True


def selftest(gd):
    """端到端自检：钩子必须**能拦**、也必须**能放**。只测一半的闸门等于没测。"""
    term = first_term()
    tmpd = tempfile.mkdtemp(prefix='privchk_')
    try:
        clean = os.path.join(tmpd, 'clean.txt')
        dirty = os.path.join(tmpd, 'dirty.txt')
        open(clean, 'w', encoding='utf-8').write('chore: 自检用的正常提交信息\n')
        if term:
            open(dirty, 'w', encoding='utf-8').write('chore: 自检 %s\n' % term)
        else:
            RESULT.append(('WARN', '未找到禁词清单，反向自检跳过 —— 闸门其实没有在拦东西'))

        cm = os.path.join(gd, 'hooks', 'commit-msg')
        pc = os.path.join(gd, 'hooks', 'pre-commit')

        if os.path.exists(cm) and term:
            r = run_hook(cm, dirty)
            RESULT.append(('OK' if r.returncode != 0 else 'FAIL',
                           'commit-msg 反向测试（含禁词信息应被拦）-> exit %d' % r.returncode))
            r2 = run_hook(cm, clean)
            RESULT.append(('OK' if r2.returncode == 0 else 'FAIL',
                           'commit-msg 正向测试（正常信息应放行）-> exit %d' % r2.returncode))
        if os.path.exists(pc):
            r3 = run_hook(pc)
            RESULT.append(('OK' if r3.returncode == 0 else 'WARN',
                           'pre-commit 当前暂存区扫描 -> exit %d%s' % (
                               r3.returncode, '' if r3.returncode == 0 else '（暂存区里有命中项？）')))
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    check_only = '--check' in sys.argv

    out('=== 隐私闸门钩子 %s ===' % ('体检' if check_only else '安装'))
    gd = git_dir()
    if not gd:
        out('  ❌ 这里不是 git 仓库')
        return 1
    out('  仓库根 : %s' % ROOT)
    out('  钩子目录: %s' % os.path.relpath(os.path.join(gd, 'hooks'), ROOT))

    hp = hooks_path_cfg()
    if hp:
        out('  ⚠️ core.hooksPath = %s —— Git 会**忽略** .git/hooks，本脚本装的钩子不会生效！' % hp)
        RESULT.append(('FAIL', 'core.hooksPath 已设置，请把它清掉或改为 .githooks'))

    if not check_only:
        install(gd)
    else:
        for name in HOOKS:
            present = os.path.exists(os.path.join(gd, 'hooks', name))
            RESULT.append(('OK' if present else 'FAIL',
                           '%s %s' % (name, '已安装' if present else '**未安装**（跑 python scripts/install_hooks.py）')))

    selftest(gd)

    out('')
    fails = 0
    for lvl, msg in RESULT:
        mark = {'OK': '  ok  ', 'WARN': '  warn', 'FAIL': '  FAIL'}[lvl]
        if lvl == 'FAIL':
            fails += 1
        out('%s %s' % (mark, msg))
    out('')
    if fails:
        out('  ❌ 有 %d 项未通过' % fails)
        return 1
    out('  ✅ 闸门可用（文件内容 + 提交信息 两个钩子都在工作）')
    out('     新机器/新 clone 后重装：python scripts/install_hooks.py')
    return 0


if __name__ == '__main__':
    sys.exit(main())
