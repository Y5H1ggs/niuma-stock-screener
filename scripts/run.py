# -*- coding: utf-8 -*-
"""
run.py — **云端 / 上架版统一入口**（单条绝对路径命令调用）

为什么要有它（优化 #3）：
    付费技能在平台云端运行，平台对技能做「文件系统 / 网络 / 命令」三维隔离。
    公开部署案例的实测约束是：**单条命令 + 脚本绝对路径**，
    不要用 `cd xxx && ...`、不要管道 / 重定向 —— 否则容易在沙箱里被拦。
    本入口用 `__file__` 定位自身目录，按子命令 dispatch 到包内脚本，
    调用方全程只需一条命令：

        python "<包内绝对路径>/scripts/run.py" health
        python "<包内绝对路径>/scripts/run.py" health --quick
        python "<包内绝对路径>/scripts/run.py" report 600000
        python "<包内绝对路径>/scripts/run.py" doctor       # 云端环境体检

    ★ 不需要 cd、不需要设置 PYTHONPATH；入口会把自身目录插进 sys.path。

设计约束：
  · **白名单分发**：只允许调用下表内的脚本，不接受任意路径（安全审核加分项）。
  · 模块顶层零执行 —— 全部在 main() + __main__ 守卫内（P103 #12 教训）。
  · 子脚本抛 SystemExit 转换为进程退出码，不吞异常也不崩溃。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

COMMANDS = {
    # 体检 / 自检
    'health': 'health.py', 'selfcheck': 'selfcheck.py', 'smoketest': 'smoketest.py',
    'review': 'market_review.py',
    'ths': 'ds_ths.py', 'verify': 'verify.py',
    # 主链路
    'mood': 'mood.py', 'scan': 'scan_pool.py', 'sector': 'sector_compare.py',
    'backtest': 'backtest.py', 'predict': 'predict.py', 'report': 'report.py',
    'risk': 'risk.py', 'journal': 'journal.py', 'calibrate': 'calibrate.py',
    'winrate': 'winrate_select.py', 'patterns': 'patterns.py', 'fees': 'fees.py',
    'pit': 'pit.py',
    'makeindex': 'make_index.py', 'docscheck': 'check_docs.py',
    # 概率 / 产物验收
    'prob': 'prob.py', 'checkhtml': 'check_html.py',
    'valuation': 'valuation.py', 'fundamentals': 'fundamentals.py',
    'recommend': 'recommend.py',
    # 因子 / 模型 / 辅助
    'make_pool': 'make_pool.py', 'factors': 'factors.py',
    'build_panel': 'build_panel.py', 'factor_ic': 'factor_ic.py',
    'analyze_panel': 'analyze_panel.py', 'model': 'model.py',
    'cfg': 'cfg.py', 'ds': 'ds.py', 'md2html': 'md2html.py',
}

REQUIRED_FILES = ['ds.py', 'cfg.py', 'report.py', 'winrate_select.py',
                  'smoketest.py', 'health.py', 'ds_ths.py', '_ths_worker.py',
                  'prob.py', 'check_html.py', 'make_index.py', 'check_docs.py']


def doctor():
    """云端环境体检：一次输出运行条件，便于上架验收与排障。**不因取数失败而崩溃**。"""
    import platform
    print('=== 运行环境 ===')
    print('  Python      : %s' % platform.python_version())
    print('  解释器      : %s' % sys.executable)
    print('  包内脚本目录: %s' % HERE)
    missing = [f for f in REQUIRED_FILES if not os.path.isfile(os.path.join(HERE, f))]
    print('  核心文件    : %s' % ('齐全 ✅' if not missing else '缺失 %s ❌' % missing))

    print('=== 可选增强源 ===')
    try:
        import ds_ths
        print('  ds_ths      : %s' % ds_ths.status())
    except Exception as e:
        print('  ds_ths      : 导入失败 %r' % e)

    print('=== 数据源连通（仅探测，不作为结论）===')
    try:
        import ds
        rows = ds.clist(fid='f6', fs=ds.MARKET_MAIN, pages=1,
                        fields='f12,f14,f2,f3,f6')
        q = ds.clist_quality()
        print('  clist 行数  : %d' % len(rows))
        print('  分页完整    : %s' % q.get('complete'))
        print('  字段有效率  : %s' % (q.get('fields') or {}))
        print('  fields_usable: %s' % q.get('fields_usable'))
        if not q.get('fields_usable'):
            print('  ⚠️ 行情字段为占位符 —— 常见于盘前/网络隔离，'
                  '动态冒烟会如实判失败（P105），非代码缺陷')
    except Exception as e:
        print('  clist       : 不可用 %r（云端可能限制外网，见 publishing.md）' % e)
    print('=== 结论 ===')
    print('  doctor 只报运行条件，不判断策略结论；交易时段的完整自检请跑 smoketest。')
    return 0


def run_tests():
    """跑 `tests/` 下的单元测试（**全离线**，用合成数据，不联网）。

    为什么单独给一个子命令：`prob.py` 的两个核心断言都曾经自己写错过
    （单调性方向反、EV 的 N 显示成 1），而这类「校验器自己出错」靠看输出很难发现。
    """
    import unittest
    root = os.path.dirname(HERE)
    tests_dir = os.path.join(root, 'tests')
    if not os.path.isdir(tests_dir):
        print('tests/ 目录不存在（打包版可能未附带）')
        return 2
    suite = unittest.TestLoader().discover(tests_dir)
    res = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if res.wasSuccessful() else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ('-h', '--help', 'help'):
        print(__doc__)
        print('可用子命令:')
        for k in sorted(COMMANDS):
            print('  %-14s -> %s' % (k, COMMANDS[k]))
        print('  %-14s -> 云端环境体检' % 'doctor')
        print('  %-14s -> 单元测试（离线）' % 'test')
        return 0

    cmd = argv[0]
    if cmd == 'doctor':
        return doctor()
    if cmd == 'test':
        return run_tests()
    if cmd not in COMMANDS:
        print('未知子命令: %s（用 --help 查看可用项）' % cmd)
        return 2

    target = os.path.join(HERE, COMMANDS[cmd])
    if not os.path.isfile(target):
        print('脚本不存在: %s' % target)
        return 2

    import runpy
    old_argv = sys.argv
    sys.argv = [target] + argv[1:]
    try:
        runpy.run_path(target, run_name='__main__')
        return 0
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else (0 if e.code is None else 1)
    except Exception as e:
        print('[run.py] 子命令 %s 执行失败: %r' % (cmd, e))
        return 1
    finally:
        sys.argv = old_argv


if __name__ == '__main__':
    sys.exit(main())
