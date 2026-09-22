# -*- coding: utf-8 -*-
"""patterns.py — 形态库扩展 + 自定义形态 DSL（README 路线图 P2-8）

在 backtest.py 的 12 个内置形态之外：
  · 内置**扩展形态库**（趋势/量能/位置/涨停基因 四族，20+ 条）
  · 支持用一行 **DSL** 描述任意自定义形态

用法：
  python patterns.py 601169                          # 跑扩展形态库
  python patterns.py 601169 --rule "close>ma5 and rsi14<40 and pctb<30"
  python patterns.py 601169 --list                    # 列出形态库与变量

DSL 变量（**信号日收盘时可见**，禁止用未来数据）：
  close/open/high/low  收盘/开盘/最高/最低          chg  当日涨幅(%)
  ma5/ma10/ma20/ma60   均线值                      devma5  收盘对 MA5 偏离(%)
  rsi6/rsi14/rsi24     RSI                         pctb  BOLL %B
  boll_up/boll_dn      BOLL 上下轨                 cci20/wr14/mom10  常规指标
  vr                   量比（对前 20 日均量）        streak  截至信号日的连续涨停数
  pos60/pos250         60/250 日位置(%)            gap  开盘跳空(%)
  upper                上影线占收盘(%)              yang  收阳(布尔)
  bull/bear            多头/空头排列                above5/above20  站上 MA5/MA20
函数：abs · min · max · round
"""
import sys
import io

import ds
from ds import kline_qq


# ------------------------------------------------------------------ 扩展形态库
LIBRARY = [
    ('涨停基因', 'streak==1 and chg>=9.5'),
    ('二连板', 'streak==2'),
    ('三连板及以上', 'streak>=3'),
    ('超卖反抽', 'rsi14<40 and above5'),
    ('贴下轨', 'pctb<20'),
    ('CCI 超卖', 'cci20<-100'),
    ('空头中站上MA5', 'bear and above5'),
    ('多头排列', 'bull'),
    ('多头+放量', 'bull and vr>=2'),
    ('触上轨+放量', 'close>=boll_up and vr>=1.5'),
    ('缩量回踩MA10', 'close<ma10 and devma5>-3 and vr<0.8'),
    ('长上影(>=5%)', 'upper>=5'),
    ('跳空高开(>=3%)', 'gap>=3'),
    ('跳空低开收阳', 'gap<=-2 and yang'),
    ('强势缩量(涨>=5%,量比<1)', 'chg>=5 and vr<1'),
    ('低位启动(位置<35%,涨>=5%)', 'pos250<35 and chg>=5'),
    ('高位滞涨(位置>85%)', 'pos250>85 and abs(chg)<1'),
    ('站上MA20', 'above20'),
    ('跌破MA20但MA5在上', 'close<ma20 and ma5>ma20'),
    ('温和放量', 'vr>=1.2 and vr<2 and chg>0'),
    ('WR 超卖', 'wr14<-80'),
    ('动量转弱', 'mom10<-5'),
]


def build_feats(k):
    """为每根 K 线构建 DSL 变量表（只算 i>=60 的）。"""
    fs = []
    for i in range(len(k)):
        if i < 60:
            fs.append(None)
            continue
        cl = [k[j]['c'] for j in range(i + 1)]
        hi = [k[j]['h'] for j in range(i + 1)]
        lo = [k[j]['l'] for j in range(i + 1)]
        c, o, prev = k[i]['c'], k[i]['o'], k[i - 1]['c']
        ma5, ma10, ma20, ma60 = ds.ma(cl, 5), ds.ma(cl, 10), ds.ma(cl, 20), ds.ma(cl, 60)
        b = ds.boll(cl)
        st = 0
        for j in range(i, 0, -1):
            if k[j - 1]['c'] and k[j]['c'] / k[j - 1]['c'] - 1 >= 0.095:
                st += 1
            else:
                break
        vols = [k[j]['v'] for j in range(max(0, i - 20), i)]
        avgv = sum(vols) / len(vols) if vols else 0
        fs.append({
            'close': c, 'open': o, 'high': k[i]['h'], 'low': k[i]['l'],
            'chg': (c / prev - 1) * 100 if prev else 0,
            'ma5': ma5, 'ma10': ma10, 'ma20': ma20, 'ma60': ma60,
            'devma5': (c / ma5 - 1) * 100 if ma5 else 0,
            'rsi6': ds.rsi(cl, 6), 'rsi14': ds.rsi(cl, 14), 'rsi24': ds.rsi(cl, 24),
            # ⚠️ ds.boll() 的键名是 mid / up / **low**（不是 'dn'），别按直觉写
            'pctb': b['pctb'], 'boll_up': b['up'], 'boll_dn': b['low'],
            'cci20': ds.cci(hi, lo, cl, 20), 'wr14': ds.wr(hi, lo, cl, 14), 'mom10': ds.mom(cl, 10),
            'vr': (k[i]['v'] / avgv) if avgv else 0,
            'streak': st,
            'pos60': ds.pos(hi, lo, c, 60), 'pos250': ds.pos(hi, lo, c, 250),
            'bull': bool(ma5 > ma10 > ma20 > ma60), 'bear': bool(ma5 < ma10 < ma20),
            'above5': bool(c > ma5), 'above20': bool(c > ma20),
            'gap': (o / prev - 1) * 100 if prev else 0,
            'upper': (k[i]['h'] - c) / c * 100 if c else 0,
            'yang': bool(c > o),
            'd': k[i]['d'],
        })
    return fs


def backtest_dsl(k, fs, expr, label):
    env = {'abs': abs, 'min': min, 'max': max, 'round': round}
    hits = []
    for i, f in enumerate(fs):
        if not f or i + 1 >= len(k):
            continue
        try:
            ok = eval(expr, {'__builtins__': {}}, dict(f, **env))
        except NameError as e:
            raise SystemExit('DSL 求值失败：%s\n  可用变量：%s' % (e, ', '.join(sorted(
                [x for x in f if not x.startswith('_')]))))
        except Exception as e:
            raise SystemExit('DSL 求值异常：%s' % e)
        if ok:
            hits.append((i, f))
    if not hits:
        return None
    res = []
    for i, f in hits:
        buy = k[i]['c']
        nx = k[i + 1]
        res.append(((nx['o'] / buy - 1) * 100, (nx['c'] / buy - 1) * 100,
                    (nx['h'] / buy - 1) * 100, (nx['l'] / buy - 1) * 100, nx['d']))
    n = len(res)
    return {
        'label': label, 'n': n,
        'wc': sum(1 for r in res if r[1] > 0) / n * 100,
        'ac': sum(r[1] for r in res) / n,
        'wo': sum(1 for r in res if r[0] > 0) / n * 100,
        'ah': sum(r[2] for r in res) / n,
        'al': sum(r[3] for r in res) / n,
        'p2': sum(1 for r in res if r[2] >= 2) / n * 100,
        'pb': sum(1 for r in res if r[3] <= -3) / n * 100,
        'cases': res,
    }


def fmt(r):
    warn = ' ⚠️N<10不可外推' if r['n'] < 10 else ''
    return ('%-26s N=%-4d 收胜%4.0f%% 均%+6.2f%% | 开胜%4.0f%% | 次高%+6.2f%% 次低%+6.2f%% '
            '| 冲2%%=%3.0f%% 破-3%%=%3.0f%%%s' % (
                r['label'], r['n'], r['wc'], r['ac'], r['wo'], r['ah'], r['al'],
                r['p2'], r['pb'], warn))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if '-h' in sys.argv or '--help' in sys.argv:
        print(__doc__)
        return
    if not args:
        raise SystemExit('用法: python patterns.py <代码> [--rule "表达式"] [--list]')
    code = args[0]
    if '--list' in sys.argv:
        print('变量：close open high low chg ma5 ma10 ma20 ma60 devma5 rsi6 rsi14 rsi24 pctb')
        print('      boll_up boll_dn cci20 wr14 mom10 vr streak pos60 pos250 gap upper')
        print('      yang bull bear above5 above20   函数：abs min max round')
        print('\n内置扩展形态库：')
        for nm, ex in LIBRARY:
            print('  %-26s %s' % (nm, ex))
        return

    k = kline_qq(code, 800)
    if not k:
        raise SystemExit('%s K线取数失败' % code)
    fs = build_feats(k)
    print('=' * 128)
    print('【形态回测 · %s】样本 %d 根  %s ~ %s' % (code, len(k), k[0]['d'], k[-1]['d']))
    print('口径：信号日收盘买入 / 次日卖出（不含费用，双边约 0.15~0.2%）')
    print('=' * 128)

    rules = []
    if '--rule' in sys.argv:
        rules.append(('自定义: ' + sys.argv[sys.argv.index('--rule') + 1], sys.argv[sys.argv.index('--rule') + 1]))
    else:
        rules = LIBRARY

    rows = []
    for nm, ex in rules:
        r = backtest_dsl(k, fs, ex, nm)
        if r:
            print(fmt(r))
            rows.append(r)
        else:
            print('%-26s 无样本' % nm)
    base = backtest_dsl(k, fs, 'True', '基准(任意日)')
    if base:
        print('-' * 128)
        print(fmt(base))

    if rows:
        best = max(rows, key=lambda r: r['wc'] if r['n'] >= 10 else -1)
        print('\n  样本≥10 中收胜率最高：**%s（%.0f%%，N=%d）**' % (best['label'], best['wc'], best['n']))
    print('\n  ⚠️ 形态越多，"看起来好"的那个越可能是运气 —— 样本 <10 一律不可外推；')
    print('     同一族的多个形态结论若互相矛盾，应按整族的**中位表现**判读，不要挑最有利的那条。')


if __name__ == '__main__':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    main()
