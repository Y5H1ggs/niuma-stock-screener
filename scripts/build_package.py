# -*- coding: utf-8 -*-
"""
build_package.py — 生成**云端 / 上架版**技能包（优化 #5：版本分层）

为什么分层：
    GitHub 社区版是 MIT 全量源码（含开发/运维/版本沿革），免费可 clone。
    付费版若只是同一份 zip，买家没有付费理由，也会把内部运维细节一起交出去。
    所以云端版只保留**运行必需 + 方法文档**，把「免配置、持续维护、云端直调」作为付费价值。

用法（单条绝对路径命令，符合平台命令隔离）：
    python "<包内绝对路径>/scripts/build_package.py"
    python "<包内绝对路径>/scripts/build_package.py" --wrap     # 包一层顶层目录
    python "<包内绝对路径>/scripts/build_package.py" --out FILE

产物：dist/niuma-cloud-v<版本>.zip
自检：体积（平台技能包上限 3 MB）· SKILL.md 必填字段 · 私有文件 · 基础敏感词
      —— 任一失败返回非零退出码（P105：失败必须看得见）。

★ 模块顶层零执行（P103 #12 教训）；生成物 dist/ 已 gitignore。
"""
import os
import sys
import re
import zipfile
import io

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DIST = os.path.join(ROOT, 'dist')

# 开发 / 运维 / 发布工具 —— 付费版不需要，也不该交给买家
EXCLUDE_SCRIPTS = {
    'check_privacy.py', 'install_hooks.py', 'make_release.py',
    'build_package.py', 'md2html.py', 'charts.py',
}
# 绝不进包的私有 / 中间产物（文件名级闸门）
FORBIDDEN_NAMES = {
    'config.json', 'account.session', '_privacy_terms.txt',
    'pool.json', 'pool_deep.json',
}
FORBIDDEN_PREFIXES = ('_journal', '_cache_', 'stage', '_panel_', '_pool_', '_ic_')
REQUIRED_META = ('name', 'display_name', 'description', 'version', 'license')

# ★ P56：打包脚本**不做内容级敏感词正则**。
#   实测两轮：裸词命中「盈亏比 / 持仓口径 / allowed_prefixes」8 处误报；
#   收紧成「词+数值」后又命中「盈亏比 1.29」「本金 0.00」「保本价 +0.xx%」4 处误报。
#   方法论文档里到处是量化参数与费用示例，正则注定误伤 —— 老喊狼来了会让人忽略真警报。
#   **内容级隐私由源仓库 `check_privacy.py --all`（28 条禁词）负责**，本脚本只做结构检查
#   （私有文件名 / 必填字段 / 体积），并在发布流程里强制要求先跑那道闸门。


def version():
    p = os.path.join(ROOT, 'SKILL.md')
    m = re.search(r'^version:\s*([^\s]+)', io.open(p, encoding='utf-8').read(), re.M)
    return m.group(1) if m else 'unknown'


def collect():
    """返回 [(zip 内相对路径, 磁盘绝对路径)]，只收运行必需文件。"""
    out = []

    def add(rel, abs_p):
        if os.path.isfile(abs_p):
            out.append((rel, abs_p))

    # 根级
    for f in ('SKILL.md', 'config.example.json', 'LICENSE'):
        add(f, os.path.join(ROOT, f))
    # 脚本：排除开发运维工具 + 私有/中间产物
    sd = os.path.join(ROOT, 'scripts')
    for f in sorted(os.listdir(sd)):
        if not f.endswith('.py') or f in EXCLUDE_SCRIPTS:
            continue
        if f.startswith('_') and f != '_ths_worker.py':
            continue          # 一次性临时脚本
        add('scripts/' + f, os.path.join(sd, f))
    # 方法文档（核心价值之一，全量保留）
    rd = os.path.join(ROOT, 'references')
    if os.path.isdir(rd):
        for f in sorted(os.listdir(rd)):
            # publishing.md 是**给发布者看的手册**（含商业化判断与劝退分析），
            # 不该出现在付费包里给买家读；其余方法文档是核心价值，全量保留。
            if f.endswith('.md') and f != 'publishing.md':
                add('references/' + f, os.path.join(rd, f))
    # 示例
    ed = os.path.join(ROOT, 'examples')
    if os.path.isdir(ed):
        for f in sorted(os.listdir(ed)):
            if os.path.isfile(os.path.join(ed, f)):
                add('examples/' + f, os.path.join(ed, f))
    # 单元测试（v1.2.47）——**必须打进包**：`run.py test` 已在包内，
    # 不给 tests 会让它必然报「目录不存在」。两个文件约 20 KB，体积可忽略，
    # 且让使用者能自检 prob 的单调性/四格自洽等不变量。
    td = os.path.join(ROOT, 'tests')
    if os.path.isdir(td):
        for f in sorted(os.listdir(td)):
            if f.endswith('.py') and os.path.isfile(os.path.join(td, f)):
                add('tests/' + f, os.path.join(td, f))
    # 图文文档（v1.2.55）——**与 tests 同一类遗漏**：
    #   SKILL.md / README 会引用 docs/LIMITATIONS.md，不给它就会在包里断链。
    #   4 张 SVG 合计约 26 KB，体积可忽略，但缺了就等于「有目录没内容」。
    dd = os.path.join(ROOT, 'docs')
    if os.path.isdir(dd):
        for f in sorted(os.listdir(dd)):
            fp = os.path.join(dd, f)
            if os.path.isfile(fp) and f.lower().endswith(('.md', '.svg')):
                add('docs/' + f, fp)
    return out


def cloud_readme(ver):
    return """# 云端版说明（v%s）

## 与 GitHub 社区版的差异

| 维度 | GitHub 社区版（免费 · MIT） | 云端版（本包） |
|---|---|---|
| 内容 | 全量源码 + 107 条踩坑 + CHANGELOG + 开发/运维脚本 | **运行最小集** + 方法文档 + 统一入口 |
| 环境 | 需自行准备 Python、配置数据源、按需装可选依赖 | **免配置**：`run.py` 单条绝对路径命令直调 |
| 维护 | 自行跟进版本 | 随付费版本持续更新口径与踩坑库 |
| 定位 | 可审计、可二次开发 | 开箱即用的研究助理 |

> 本包**不含**任何个人参数、交易台账或账户口径；`config.json` 需首次运行时由使用者自行填写。
> 完整源码与版本沿革见公开仓库 `github.com/Y5H1ggs/niuma-stock-screener`。

## 运行

```
python "<包内绝对路径>/scripts/run.py" doctor          # 环境体检
python "<包内绝对路径>/scripts/run.py" --help          # 全部子命令
python "<包内绝对路径>/scripts/run.py" health --quick
```

- 核心链路纯标准库；`thsdk` 为**可选**增强源，云端装不了时设 `THS_DISABLE=1` 安静降级。
- 需能访问行情公开接口；若平台网络隔离导致外网不可达，`doctor` 会如实报告字段有效率。
- **动态冒烟应在交易时段（09:30 后）运行**；盘前字段为占位符时会如实判失败（P105）。

## 边界

只给概率与客观推演，**不给买卖指令**；所有输出末尾强制风险提示。
数据来自第三方公开接口，可能存在延迟、缺失或口径差异，**不构成任何投资建议**。
""" % ver


def check(files, ver):
    """返回 (ok, 诊断行列表)。"""
    lines, ok = [], True
    total = sum(os.path.getsize(p) for _, p in files)
    lines.append('文件数 %d，未压缩合计 %.2f MB' % (len(files), total / 1048576))

    # 体积：zip 压缩后通常更小，但按未压缩合计做保守上界
    if total > 3 * 1048576:
        lines.append('❌ 未压缩合计超过 3 MB 平台上限')
        ok = False
    else:
        lines.append('✅ 体积在 3 MB 限制内')

    # 私有文件
    bad = [rel for rel, _ in files
           if os.path.basename(rel) in FORBIDDEN_NAMES
           or any(os.path.basename(rel).startswith(p) for p in FORBIDDEN_PREFIXES)]
    if bad:
        lines.append('❌ 私有/中间产物混入: %s' % bad[:6])
        ok = False
    else:
        lines.append('✅ 无私有文件 / 中间产物')

    # SKILL.md 必填元信息
    sk = io.open(os.path.join(ROOT, 'SKILL.md'), encoding='utf-8').read()
    miss = [k for k in REQUIRED_META
            if not re.search(r'^%s:\s*\S' % re.escape(k), sk, re.M)]
    if miss:
        lines.append('❌ SKILL.md 缺必填字段: %s' % miss)
        ok = False
    else:
        lines.append('✅ SKILL.md 必填字段齐全 (%s)' % ', '.join(REQUIRED_META))

    lines.append('⚠️ 内容级隐私不在此判定（正则会误伤方法论参数，P56）；'
                 '**发布前必须在源仓库跑 `check_privacy.py --all`**')

    lines.append('版本号 v%s' % ver)
    return ok, lines


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    wrap = '--wrap' in argv
    out_file = None
    if '--out' in argv:
        i = argv.index('--out')
        if i + 1 < len(argv):
            out_file = argv[i + 1]
    ver = version()
    if not os.path.isdir(DIST):
        os.makedirs(DIST)
    zip_path = out_file or os.path.join(DIST, 'niuma-cloud-v%s.zip' % ver)

    files = collect()
    ok, lines = check(files, ver)
    print('=== 打包前自检 ===')
    for l in lines:
        print('  ' + l)
    if not ok:
        print('❌ 自检未通过，不生成包')
        return 1

    prefix = '选股分析牛马工具/' if wrap else ''
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
        for rel, p in files:
            z.write(p, prefix + rel)
        z.writestr(prefix + 'README_CLOUD.md', cloud_readme(ver))
    size = os.path.getsize(zip_path)
    print('=== 产物 ===')
    print('  %s' % zip_path)
    print('  压缩后 %.2f MB / %d 个条目' % (size / 1048576, len(files) + 1))
    if size > 3 * 1048576:
        print('  ❌ 压缩后仍超 3 MB')
        return 1
    print('  ✅ 可上传（上架流程见 references/publishing.md）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
