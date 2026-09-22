# -*- coding: utf-8 -*-
"""smoketest.py — 随机股票全链路冒烟测试 + 已知错误模式扫描

为什么需要它：
  本项目的风险不是"崩溃"，而是"**看起来完全正常的错误**"——接口字段被静默读成 0、
  比较错了字段、单位不一致、返回结构变了。这类问题：不抛异常、数字自洽、只是悄悄失真。
  单靠人工review抓不住（实测 49 条坑里多数是偶然发现的）。

  所以做一个**每次改完代码就跑一遍**的冒烟：随机抽票 × 全套模块，
  再对源码扫一遍已知错误模式。

用法：
  python smoketest.py                 # 随机 3 只主板股 × 全套模块
  python smoketest.py --n 5           # 抽 5 只
  python smoketest.py --seed 42       # 固定种子（复现某次失败）
  python smoketest.py --static        # 只跑静态模式扫描（不联网，秒级）

设计原则：**随机是为了防"只测自己熟悉的票"**。固定种子用于复现失败案例。
"""
import sys
import io
import os
import re
import glob
import time
import random
import importlib
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 已知错误模式（每次踩坑后往这里加一条，让同类错误不再复发）
BAD_PATTERNS = [
    (r"b\[['\"]dn['\"]\]", 'ds.boll 的键是 low 不是 dn (P45)'),
    (r"\[['\"]hist['\"]\]", 'MACD 柱值键是 macd 不是 hist (P45 类)'),
    (r'break_even\([^,]+,\s*100\)', '保本价硬编码 100 股 (P49)'),
    (r"sys\.stdout\s*=\s*io\.TextIOWrapper", '顶层重绑 stdout 需在 __main__/main 内 (P44)'),
]


def static_scan():
    """静态：语法 + import + 已知错误模式。"""
    print('=' * 96)
    print('【静态扫描】语法 / import / 已知错误模式')
    print('=' * 96)
    bad = 0
    import ast
    for f in sorted(glob.glob(os.path.join(HERE, '*.py'))):
        base = os.path.basename(f)
        try:
            ast.parse(open(f, encoding='utf-8').read())
        except SyntaxError as e:
            print('  ❌ 语法 %s: %s (line %s)' % (base, e.msg, e.lineno))
            bad += 1
    print('  语法检查完成' if bad == 0 else '  %d 个语法错误' % bad)

    mods = [os.path.splitext(os.path.basename(p))[0]
            for p in sorted(glob.glob(os.path.join(HERE, '*.py')))
            if not os.path.basename(p).startswith('_')]
    for m in mods:
        try:
            importlib.import_module(m)
        except Exception as e:
            print('  ❌ import %s: %r' % (m, e))
            bad += 1
    print('  import 检查完成（%d 个模块）' % len(mods))

    # 模式扫描：排掉注释行，只看真代码
    hits = 0
    for f in sorted(glob.glob(os.path.join(HERE, '*.py'))):
        base = os.path.basename(f)
        if base in ('smoketest.py',):       # 本文件含模式定义，跳过
            continue
        for i, ln in enumerate(open(f, encoding='utf-8'), 1):
            s = ln.strip()
            if s.startswith('#') or s.startswith('"') or s.startswith("'"):
                continue
            for pat, desc in BAD_PATTERNS:
                if re.search(pat, ln):
                    # P44：只有当它不在 def/if 缩进块内时才算问题（顶层）
                    if 'stdout' in pat and (ln.startswith(' ') or ln.startswith('\t')):
                        continue
                    print('  ⚠️  %s:%d  %s' % (base, i, desc))
                    print('        %s' % s[:88])
                    hits += 1
    print('  模式扫描：%s' % ('未发现已知错误模式 ✅' if hits == 0 else '%d 处待确认' % hits))
    return bad


def runtime_smoke(n=3, seed=None, codes=None):
    """动态：随机抽票 × 全套模块。codes 非空时直接指定（用于复现失败案例）。"""
    import ds
    import cfg
    import predict
    import patterns
    import risk

    faults = []

    def T(tag, fn):
        t0 = time.time()
        try:
            return True, fn(), (time.time() - t0) * 1000
        except Exception as e:
            faults.append('%s : %r' % (tag, e))
            return False, traceback.format_exc().strip().split('\n')[-1][:120], (time.time() - t0) * 1000

    print()
    print('=' * 96)
    print('【动态冒烟】随机 %d 只主板股 × 全套模块%s' % (n, '' if seed is None else '（种子 %s）' % seed))
    print('=' * 96)

    rows = ds.clist(fid='f6', fs=ds.MARKET_MAIN, pages=6, fields='f12,f14,f2,f3,f6')
    pool = []
    for x in rows:
        c, nm, px = str(x.get('f12', '')), str(x.get('f14', '')), ds._fl(x.get('f2'))
        if not ds.allowed(c) or 'ST' in nm.upper() or '退' in nm:
            continue
        if 2.0 <= px <= 60.0:
            pool.append((c, nm))
    if codes:
        # ⚠️ 直接指定代码：**seed 不足以复现**——种子固定了随机序列，但 clist 的
        #    股票池会随成交额排序变化，同一 seed 两次抽到的票可能完全不同。
        #    要复现某个失败案例，必须用 --codes 锁定标的。
        _sn = ds.snapshot(codes)
        picks = [(c, (_sn.get(c) or {}).get('name', '') or '') for c in codes]
    else:
        if seed is not None:
            random.seed(seed)
        else:
            random.seed()
        picks = random.sample(pool, min(n, len(pool)))
    print('  %s: %s' % ('指定标的' if codes else '候选池 %d 只 → 抽中' % len(pool),
                        ', '.join('%s %s' % p for p in picks) if codes else
                        ', '.join('%s %s' % p for p in picks)))

    for code, nm in picks:
        print()
        print('  --- %s %s ---' % (code, nm))
        ok, snap, ms = T('%s/snapshot' % code, lambda: ds.snapshot([code]).get(code))
        if not ok or not snap:
            print('     snapshot ❌ %s' % snap)
            continue
        px = snap['price']
        print('     snapshot        ✅ %6.0fms  %.2f %+.2f%% 量比%.2f' % (ms, px, snap['chg'], snap['lb']))

        ok, r, ms = T('%s/kline' % code, lambda: ds.kline(code, 260))
        kl = r[0] if ok and r else []
        print('     kline           %s %6.0fms  %d 根(源 %s)' % ('✅' if kl else '❌', ms, len(kl), (r[1] if ok and r else '-')))

        ok, prof, ms = T('%s/profile' % code, lambda: ds.stock_profile(code))
        print('     stock_profile   %s %6.0fms  %s / %d 概念' % (
            '✅' if ok else '❌', ms, (prof or {}).get('industry'), len((prof or {}).get('concepts') or [])))
        prof = prof or {}

        ok, r, ms = T('%s/ulist' % code, lambda: ds.ulist([code]).get(code))
        print('     ulist           %s %6.0fms  f62=%s' % ('✅' if ok else '❌', ms, (r or {}).get('f62') if ok else r))

        ok, r, ms = T('%s/fflow' % code, lambda: ds.fflow(code, klt=1))
        print('     fflow           %s %6.0fms  %d 条' % ('✅' if ok else '❌', ms, len(r) if ok else 0))

        ok, fin, ms = T('%s/financials' % code, lambda: ds.financials(code))
        print('     financials      %s %6.0fms  %d 期' % ('✅' if ok else '❌', ms, len(fin) if ok else 0))

        ok, mb, ms = T('%s/main_business' % code, lambda: ds.main_business(code))
        if ok and isinstance(mb, dict):
            print('     main_business   ✅ %6.0fms  dict, by_product %d 条' % (ms, len(mb.get('by_product') or [])))
        else:
            print('     main_business   ⚠️ %6.0fms  返回 %s（契约应为 dict：date/by_product/by_industry/by_region）' % (ms, type(mb).__name__))

        if kl:
            def _ind():
                cl = [x['c'] for x in kl] + [px]
                hi = [x['h'] for x in kl] + [px]
                lo = [x['l'] for x in kl] + [px]
                return ds.indicators(cl, hi, lo, live=px), ds.boll(cl), ds.macd(cl)
            ok, r, ms = T('%s/indicators' % code, _ind)
            if ok:
                ind, b, m = r
                print('     indicators      ✅ %6.0fms  RSI14 %.1f %%B %.1f CCI %.1f MACD %.4f' % (
                    ms, ind['rsi14'], b['pctb'], ind['cci20'], m['macd']))
            else:
                print('     indicators      ❌ %6.0fms  %s' % (ms, r))

        ok, r, ms = T('%s/predict' % code, lambda: (lambda t, s: (predict.neighbourhood(
            ds.kline_qq(code, 800), t, rsi_bw=8, pctb_bw=15, cci_bw=80), s))(*predict.today_feats(code)))
        if ok and r[0]:
            st = predict.stats(r[0], r[1], cost=None, shares=500)
            if st.get('reliable', True):
                print('     predict         ✅ %6.0fms  N=%d P(收涨)%.0f%% P(曾涨≥1%%)%.0f%%' % (
                    ms, st['n'], st['P_close_up'], st['P_high_ge1']))
            else:
                print('     predict         ⚠️ %6.0fms  N=%d **样本不足(需≥10)，概率不可外推、不予展示**' % (ms, st['n']))
        else:
            print('     predict         ⚠️ %6.0fms  %s' % (ms, '邻域无样本' if ok else r))

        def _pat():
            k = ds.kline_qq(code, 800)
            fs = patterns.build_feats(k)
            return [(x, patterns.backtest_dsl(k, fs, ex, x)) for x, ex in patterns.LIBRARY[:3]]
        ok, r, ms = T('%s/patterns' % code, _pat)
        if ok:
            print('     patterns        ✅ %6.0fms  %s' % (ms, ' | '.join('%s:N=%s' % (a, (b['n'] if b else 0)) for a, b in r)))
        else:
            print('     patterns        ❌ %6.0fms  %s' % (ms, r))

        ok, r, ms = T('%s/cfg费用' % code, lambda: (cfg.buy_cost(px, 500), cfg.break_even(px, 500), cfg.stop_price(px, 500)))
        print('     cfg 费用        %s %6.0fms  买%.2f 保本%.3f 止损%.3f' % (
            '✅' if ok else '❌', ms, *(r if ok else (0, 0, 0))))

        ok, r, ms = T('%s/risk.check' % code, lambda: risk.check(code, cash=100000))
        rr = r or {}
        why = (rr.get('reasons') or ['-'])[0]
        print('     risk.check      %s %6.0fms  allow=%-5s 建议%5d股  ← %s' % (
            '✅' if ok else '❌', ms, rr.get('allow'), rr.get('suggest_shares', 0), why[:64]))

    return faults


def main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    n = 3
    seed = None
    codes = None
    for i, a in enumerate(sys.argv):
        if a == '--n':
            n = int(sys.argv[i + 1])
        if a == '--seed':
            seed = int(sys.argv[i + 1])
        if a == '--codes':
            codes = [x.strip() for x in sys.argv[i + 1].split(',') if x.strip()]

    bad = static_scan()
    faults = [] if '--static' in sys.argv else runtime_smoke(n, seed, codes)

    print()
    print('=' * 96)
    print('【结论】')
    print('=' * 96)
    if bad == 0 and not faults:
        print('  ✅ 静态 + 动态全部通过')
    else:
        if bad:
            print('  ❌ 静态 %d 项失败' % bad)
        for f in faults:
            print('  ❌ %s' % f)
    print('  ⚠️ 冒烟只验证"不崩溃 + 结构正确"，不验证"数字正确"。')
    print('     数字正确性要用 verify.py（双源比对）与 factors.py 的自证关系（如主力=大单+超大单）。')


if __name__ == '__main__':
    main()
